# TiDB Governed Fact Layer — Build Spec (Phase A)

A governed fact-of-record service for AI agents, exposed as tool handlers behind an Amazon Bedrock AgentCore Gateway. Facts are written through a deterministic adjudication path (contradiction test) into TiDB, with per-tenant isolation enforced by Cedar policy before any query runs, and a tamper-evident audit log of every change.

This phase ships the handlers as **AWS Lambda targets** behind the Gateway. Standalone MCP-server hosting, registry publication, and evaluation integration are later phases and out of scope here.

> **Status — superseded in places (see `ARCHITECTURE.md`, which is authoritative).**
> Passages below that describe the request interceptor *injecting* verified
> identity / `agent_id` / `session_id` / a tenant-scoped credential into the
> handler (a `_fact_ctx`-style side channel) reflect the ORIGINAL design. The
> proven implementation: the interceptor **independently verifies the JWT and
> passes the request through** (no injected identity/credential); an MCP Lambda
> target receives **only the tool arguments**, so a handler reads `tenant_id` — the
> **requested data scope** — from the tool argument and resolves the DB credential
> from configuration. `tenant_id` is a scope selector, **not** authenticated
> identity; authorization is the Gateway governance layer's job (reference profile:
> Cognito access-token claim + Cedar tenant-equality; enforced only in ENFORCE).
> This spec is retained as historical build context.

---

## Definition of done

The build is complete when all of the following hold end to end:

- An agent invokes `record_fact`, `vector_search`, and `query_tenant_metrics` through the Gateway.
- `record_fact` writes an event **and** upserts the current-truth row in one transaction.
- A contradicting assertion supersedes the current value, the superseded event remains in the log, and the hash chain verifies.
- A corroborating assertion raises confidence without discarding prior events.
- A call whose `tenant_id` argument does not match the caller's identity claim is **denied by Cedar before it reaches TiDB** (visible in the CloudWatch policy log).
- The two deploy-confirm checks (see Acceptance tests) pass.

---

## Scope

**In:** cluster schema, TiDB roles + resource control groups, Secrets Manager wiring, three tool handlers, Gateway + Lambda targets, request interceptor, JWT inbound auth, Cedar policy engine, and a validation agent.

**Out (do not build):**
- Standalone MCP server on AgentCore Runtime.
- AWS Agent Registry publication.
- Custom evaluator / evaluation integration.
- Any conversational / short-term "what did we talk about" store — that tier is AgentCore Memory's job, not this service's.
- Any free-form SQL tool or generic `db_execute` / `db_query` surface.
- Free-form aggregation (a later Code Interpreter path).

---

## Environment inputs (supplied by the operator, not guessed)

This is a **standalone** build — its own Gateway, its own Cognito app client, its own thin validation. It does not attach to any existing agent or Gateway.

- **Region:** one region for the TiDB cluster, the Secrets Manager secret, the Lambdas, and the Gateway. The reference TiDB cluster and secret currently live in `eu-central-1` — put the rest there too, or the handler can't read its secret.
- TiDB Cloud cluster: connection host/port, and the tier. The reference cluster is **TiDB Cloud Serverless** (a.k.a. Starter), which supports the HNSW vector index. Per TiDB docs, vector search is available on **Starter/Serverless, Essential, and Dedicated**, plus **Self-Managed** — **not** Premium (Dedicated/Self-Managed need TiDB v8.4.0+). So it is not Dedicated-gated, but Premium would not work. (Note: Serverless does not support resource-control DDL, which is why `03_roles.sql` creates no resource group.)
- AWS Secrets Manager ARN of the scoped TiDB connection secret (same region).
- Cognito: a **dedicated** app client for the fact layer — pool ID, discovery URL, allowed client IDs. Configure it to mint `tenant_id` (see token requirement); do not reuse a `username`-only client from another app.
- A name for the fact layer's Gateway (created in task 5); its ARN after creation.
- Embedding model + vector dimension for `subject_vec`.

**Token requirement (hard):** the Cognito app client must mint `tenant_id` as a first-class JWT claim — a custom attribute plus token customization. Cedar compares the claim literally and cannot derive it in-policy. Do this before the Cedar step or the isolation rule cannot be written.

---

## Hard constraints

1. `fact_event.event_id` and `fact_subject.subject_id` are `AUTO_RANDOM`, never `AUTO_INCREMENT`. The event log is monotonic; `AUTO_INCREMENT` on a clustered PK causes TiKV write hotspots.
2. `fact_current` is a **write-through** table, upserted in the **same transaction** as the `fact_event` append. TiDB has no incrementally-maintained materialized view — do not attempt one.
3. The event log is **append-only**. Corrections and retractions are new events, never row mutations. The log is **tamper-evident** (hash chain) — do not describe or treat it as immutable.
4. Handlers connect using a **tenant-scoped TiDB role**, with credentials read from Secrets Manager **by ARN**. Never place secrets in environment variables.
5. Reads are **typed and enumerated**. `query_tenant_metrics` exposes named metrics only — no caller-supplied SQL.
6. The contradiction test's authoritative decision is **deterministic and rule-based**. Any semantic/LLM check is a pre-filter that may only route a case to `disputed`; it must never decide a supersede.
7. Recency supersedes **only** for predicates typed `monotonic`. Never apply a blanket time-window overwrite.
8. TiFlash replication is asynchronous. If freshness is described anywhere in comments or docs, say "sub-second under normal load," not "sub-second fresh."

---

## Task list (ordered)

### 1. Schema

Create the tables below (this block is synced to the built `sql/01_schema.sql` — that file is authoritative). The HNSW vector index on `fact_subject.subject_vec` is declared **inline**, which makes TiDB auto-create the required TiFlash replica on `fact_subject`; set the TiFlash replica on `fact_event` explicitly. The vector-index syntax is version-specific — it was verified against the current TiDB `vector-search-index` docs, not hardcoded from memory.

```sql
-- Identity of a fact: one row per (tenant, subject, predicate)
-- The inline VECTOR INDEX makes TiDB auto-create the required TiFlash replica here.
CREATE TABLE fact_subject (
  subject_id      BIGINT       AUTO_RANDOM PRIMARY KEY,
  tenant_id       VARCHAR(64)  NOT NULL,
  subject         VARCHAR(255) NOT NULL,
  predicate       VARCHAR(128) NOT NULL,
  subject_vec     VECTOR(1024),                     -- Titan Text Embeddings V2 (1024)
  -- Per-subject hash-chain tip: event_hash of this subject's most recent event.
  -- The write path locks this row (SELECT ... FOR UPDATE) to serialize per-subject
  -- writes and read prev_hash atomically. NULL until the first event (genesis).
  last_event_hash CHAR(64)     DEFAULT NULL,
  created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_identity (tenant_id, subject, predicate),
  VECTOR INDEX idx_subject_vec ((VEC_COSINE_DISTANCE(subject_vec)))
);

-- Append-only audit log: many events -> one subject; many events -> one session
CREATE TABLE fact_event (
  event_id            BIGINT      AUTO_RANDOM PRIMARY KEY,
  subject_id          BIGINT      NOT NULL,
  tenant_id           VARCHAR(64) NOT NULL,
  op                  ENUM('assert','supersede','retract','correct') NOT NULL,
  value               JSON        NOT NULL,
  source              VARCHAR(255) NOT NULL,
  confidence          DECIMAL(4,3),
  agent_id            VARCHAR(128) NOT NULL,
  session_id          VARCHAR(128) NOT NULL,
  tool_turn           INT,
  supersedes_event_id BIGINT,
  observed_at         DATETIME(6) NOT NULL,                -- set by the handler (in the hash); microsecond precision
  prev_hash           CHAR(64)    NOT NULL,                -- previous event_hash for THIS subject (genesis for first)
  event_hash          CHAR(64)    NOT NULL,                -- sha256(canonical event fields || prev_hash)
  KEY idx_subject (tenant_id, subject_id, observed_at),
  KEY idx_session (tenant_id, session_id),
  KEY idx_chain   (subject_id, prev_hash)                  -- walk the per-subject chain during verification
);
ALTER TABLE fact_event SET TIFLASH REPLICA 1;

-- Write-through current truth: one row per fact identity, upserted in-txn with the event
CREATE TABLE fact_current (
  subject_id       BIGINT      PRIMARY KEY,
  tenant_id        VARCHAR(64) NOT NULL,
  value            JSON        NOT NULL,
  winning_event_id BIGINT      NOT NULL,
  status           ENUM('active','superseded','retracted','disputed') NOT NULL DEFAULT 'active',
  updated_at       DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_tenant_status (tenant_id, status)
);

-- Conflict rules, declared per predicate (start minimal; extend as predicates are added)
CREATE TABLE predicate_rule (
  predicate  VARCHAR(128) PRIMARY KEY,
  value_type ENUM('scalar','monotonic','set','freetext') NOT NULL
);

-- Source authority ranking for the contradiction authority gate (higher wins)
CREATE TABLE source_rank (
  source VARCHAR(255) PRIMARY KEY,
  rank   INT NOT NULL
);
```

Optional lineage dimension, only if richer cross-session rollups are wanted (the `session_id` FK already lives on `fact_event`):

```sql
CREATE TABLE agent_session (
  session_id VARCHAR(128) PRIMARY KEY,
  actor_id   VARCHAR(128),
  agent_id   VARCHAR(128),
  started_at DATETIME
);
```

Join keys: `fact_event.subject_id → fact_subject.subject_id` and `fact_event.session_id → agent_session.session_id`. Enforce in the write path; if using TiDB `FOREIGN KEY` constraints, confirm the cluster version supports them for a hot write table first.

### 2. TiDB roles (+ resource control groups — Dedicated only)

Create a scoped TiDB role per agent-type (or per tenant), granting only the DML the handlers need on these tables. This role is the last-line data scope, independent of Cedar.

> **Tier caveat (this deployment is Serverless):** resource control groups are a **Dedicated-tier** feature — `CREATE RESOURCE GROUP` is unsupported on Serverless, so `03_roles.sql` creates none. The "one caller's load cannot starve others" guarantee is therefore **not provided at the DB layer** on Serverless (capacity is metered by shared cluster RU/quota, not per-caller). If per-tenant fairness / noisy-neighbor protection is required, enforce it above the DB — e.g. per-tenant Lambda reserved/max concurrency or Gateway throttling — or move to Dedicated.

### 3. Secrets Manager

Store the scoped connection secret. Handlers resolve it by ARN at runtime. No secret material in environment variables or code.

### 4. Tool handlers

Python Lambda handlers, connecting to TiDB over the MySQL wire protocol (SQLAlchemy) using the scoped role from step 2.

**`record_fact`** — the core. Inputs: `tenant_id`, `subject`, `predicate`, `value`, `source`, `confidence`; plus `agent_id`, `session_id`, and tenant-scoped credentials arriving from the interceptor context. One ACID transaction:

1. Resolve or create the `fact_subject` row for `(tenant_id, subject, predicate)`.
2. Look up the current winner in `fact_current`.
3. If no winner → append an `assert` event (compute `event_hash` over the event fields plus the prior chain hash), insert `fact_current`. Commit.
4. If a winner exists → look up the predicate's `value_type` in `predicate_rule` and apply its conflict rule against the current value:
   - **`scalar`** — equal → corroboration; unequal (both valid) → contradiction.
   - **`monotonic`** — later observation → update (supersede). This is the only rule where recency supersedes.
   - **`set`** — new member → union into the current value (append `assert`, merge).
   - **`freetext`** — default to dispute; a semantic pre-filter may confirm the routing but must not decide a supersede.
5. Resolve the branch:
   - **Corroboration** → append `assert` (raise confidence), leave `fact_current`. Do not discard prior events.
   - **Update / set-extend** → append the appropriate event, update `fact_current`.
   - **Contradiction** → authority gate: compare `source_rank` of the incoming source vs the current winner's source.
     - incoming outranks current → append `supersede` (`supersedes_event_id` = current `winning_event_id`), re-point `fact_current`.
     - equal or lower authority → append both events, set `fact_current.status = 'disputed'`. **No recency tiebreak** — equal-authority contradictions always dispute; recency only ever supersedes for `monotonic` predicates (see Hard constraints). This is stronger than "recency breaks ties," and it is the version that governs.
6. Commit. Return `event_id`, resulting `status`, and any competing `event_id`s when disputed.

Every path appends to the log; nothing is ever overwritten in place. The superseded event stays and remains hash-chained — that is the audit.

**`vector_search`** — typed: given a query embedding and `tenant_id`, return the nearest `fact_subject` rows (with their current values) filtered to the caller's tenant. No caller SQL.

**`query_tenant_metrics`** — enumerated named metrics only (e.g. fact counts, dispute counts, supersede rate per tenant). No caller SQL. Route analytical reads to the TiFlash replica where appropriate.

### 5. Create the Gateway and register the Lambda targets

Create the fact layer's **own** Gateway with JWT inbound auth (its dedicated Cognito app client), then register each handler as a Lambda target with a tool schema (name, natural-language description, input schema). The action name each Cedar policy references is `<targetName>___<toolName>`. Verify exact CLI flags against the current AgentCore CLI; the shape is:

```
agentcore add gateway        --name fact-layer-gw --authorizer-type CUSTOM_JWT \
                             --discovery-url <cognito-discovery-url> --allowed-clients <client-ids>
agentcore add gateway-target --type lambda-function-arn --name RecordFact \
                             --lambda-arn <arn> --tool-schema-file record_fact.schema.json \
                             --gateway fact-layer-gw
# ...repeat for vector_search, query_tenant_metrics
agentcore deploy
```

The tool `inputSchema` must place `"type": "object"` directly inside `inputSchema` with no nested `json` wrapper.

### 6. Request interceptor + JWT inbound

Configure a **request interceptor** Lambda on the Gateway. It runs before Cedar. Its job: read the verified caller identity, inject `tenant_id` / `agent_id` / `session_id` context, and swap to the tenant-scoped credential the handler will use for its TiDB connection. JWT inbound auth is Cognito, validating tokens that carry the `tenant_id` claim.

### 7. Cedar policy engine

Create a policy engine, attach it to the Gateway in `ENFORCE` mode (deny-by-default, forbid-overrides-permit). Write the tenant-isolation permit for each tool. The `record_fact` rule:

```cedar
permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"RecordFact___record_fact",
  resource == AgentCore::Gateway::"<gateway-arn>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};
```

Add the equivalent tenant-guarded permit for `vector_search` and `query_tenant_metrics`. With the engine attached in `ENFORCE`, any tool call lacking a matching permit is denied at the Gateway.

### 8. Validation

Thin, standalone validation — no full agent required. Drive the three tools through the full path (Identity → request interceptor → Cedar → Lambda → TiDB) with either a direct `agentcore invoke` against the Gateway or a minimal test caller. The point is to exercise `record_fact` adjudication and the isolation rule, not to build an app.

Keep the framing straight: the deliverable is the governed fact layer; the caller is just a caller. Keep `subject` / `predicate` generic and domain-neutral.

---

## Acceptance tests

1. **Write + read.** `record_fact` on a new `(tenant, subject, predicate)` creates exactly one `fact_event` and one `fact_current` row in a single transaction; `vector_search` returns the subject.
2. **Adjudication.** A contradicting `assert` from a higher-authority source produces a `supersede` event, re-points `fact_current`, leaves the prior event in the log, and the hash chain verifies across both.
3. **Corroboration.** An agreeing `assert` appends and raises confidence without removing prior events; recall of the subject's history returns more than one event.
4. **Dispute.** A contradicting `assert` from an equal/lower-authority source leaves `fact_current.status = 'disputed'` with both competing events retained.
5. **Tenant isolation.** A call whose `context.input.tenant_id` differs from the caller's `tenant_id` claim is denied by Cedar; the deny appears in the CloudWatch policy log; TiDB is never queried.
6. **Deploy-confirm A.** `context.input.tenant_id` is populated for the tool call as the Cedar rule expects. (Offline pre-check: evaluate the policy against a mock request before deploying.)
7. **Deploy-confirm B.** The interceptor-injected tenant-scoped credential opens the correct tenant-scoped TiDB session end to end — request → injected credential → handler → scoped connection.

---

## Operator decisions (resolved)

These four were the open items before cutting the write path; all are now decided and implemented:

- **Predicates + `value_type`:** the current seeded set lives in `sql/02_seed.sql` — `status` (scalar), `last_seen` (monotonic), `known_aliases` (set), `notes` (freetext). Extend by adding rows there.
- **`source → rank` values:** seeded in `sql/02_seed.sql` — `system_of_record`=100, `verified_integration`=80, `user_assertion`=60, `agent_inference`=40.
- **Hash-chain scope:** **per-subject** — implemented via `fact_subject.last_event_hash` (the chain tip), with the write path locking that row per subject.
- **Embedding model / dimension:** **Amazon Titan Text Embeddings V2, 1024-dim** (`VECTOR(1024)`, cosine distance).