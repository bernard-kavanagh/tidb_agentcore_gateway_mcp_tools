# TiDB Governed Fact Layer — AgentCore MCP Server Build Plan (v2)

**What this is:** the build order for the reference TiDB **governed fact layer**, exposed as an MCP server behind AWS AgentCore Gateway. This is the integration pattern the rest of the AgentCore stack builds on. The deliverable is not "a scalable DB behind the primitives." It is the **system of record and adjudication engine** that the AgentCore evaluation/optimization loop structurally lacks. Nothing downstream demonstrates the moat until the governed *write* path exists and is trustworthy.

Draft for red-lining. Schema is illustrative, not final.

---

## Architecture at a glance

```
Agent (code-based Runtime agent)
   │  invoke
   ▼
AgentCore Gateway  ──(SigV4, outbound)──►  TiDB MCP Server (on Runtime, IAM/SigV4 inbound)
   │                                            │
   │  ① request interceptor (Lambda):           │  record_fact / vector_search /
   │     swap caller token → tenant-scoped        │  query_tenant_metrics
   │     credentials, inject tenant context       ▼
   │  ② Cedar policy engine (ENFORCE):         TiDB Cloud
   │     param.tenant_id == caller.claim          fact_subject · fact_event · fact_current
   │     deny-by-default, forbid-wins             (TiKV row + TiFlash columnar)
   ▼
CloudWatch (Policy decisions, OTEL traces)
```

Enforcement order at the Gateway is **interceptor → Cedar → tool**. The interceptor enriches the request (tenant-scoped creds) *before* Cedar evaluates the enriched context, and a denied call never reaches TiDB.

**Sequencing (you're not in a hurry — de-risk the moat before the plumbing):**

- **Phase A — prove the moat.** Wrap `record_fact` + reads as Lambda(s), register as Gateway *Lambda* targets, wire the interceptor + Cedar. ~1 day. Validates the full Identity → interceptor → Cedar → TiDB chain and the contradiction test with zero hosting.
- **Phase B — graduate to the MCP server.** Lift the *same* handlers into a standalone TiDB MCP server on Runtime (IAM/SigV4 inbound, so Gateway can sign outbound). Register as an MCP-server target. Same tool code, second deployment shape.
- **Phase C — discovery + loop-closer.** Publish to AWS Agent Registry (preview) via URL-based discovery pointing at the live endpoint. Add the custom evaluator that checks answers against the fact of record.

---

## Data model — subject-grain, event-sourced

Three tables. The identity table (the "one"), the append log (the "many"), and the write-through current table (the point-lookup surface).

### 1. `fact_subject` — the fact's identity (the "one" side of every join)

```sql
CREATE TABLE fact_subject (
  subject_id   BIGINT       AUTO_RANDOM PRIMARY KEY,   -- surrogate for FK joins
  tenant_id    VARCHAR(64)  NOT NULL,
  subject      VARCHAR(255) NOT NULL,                  -- the entity the fact is about
  predicate    VARCHAR(128) NOT NULL,                  -- the attribute asserted
  subject_vec  VECTOR(1024),                           -- semantic recall (HNSW; supported on Serverless — see note)
  created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_identity (tenant_id, subject, predicate)
);
```

The `UNIQUE KEY (tenant_id, subject, predicate)` is the grain. One row per distinct fact-identity, regardless of how many sessions touch it. `subject_vec` gives "find facts semantically like this" — add an HNSW vector index per the [vector-search-index docs](https://docs.pingcap.com/ai/vector-search-index.md). **Note:** per TiDB docs, the HNSW vector index is available on TiDB Cloud Serverless/Starter, Essential, and Dedicated, plus Self-Managed — **not** Premium (Dedicated/Self-Managed need v8.4.0+). It is not Dedicated-gated. This cluster runs on Serverless, which supports it.

### 2. `fact_event` — the append log (the "many" side; the audit trail)

```sql
CREATE TABLE fact_event (
  event_id            BIGINT      AUTO_RANDOM PRIMARY KEY,   -- AUTO_RANDOM: append log is monotonic → avoid TiKV hotspot
  subject_id          BIGINT      NOT NULL,                  -- → fact_subject  (many events → one fact)
  tenant_id           VARCHAR(64) NOT NULL,
  op                  ENUM('assert','supersede','retract','correct') NOT NULL,
  value               JSON        NOT NULL,                  -- the asserted value
  source              VARCHAR(255) NOT NULL,                 -- provenance: where it came from
  confidence          DECIMAL(4,3),                          -- provenance: how sure
  agent_id            VARCHAR(128) NOT NULL,                 -- provenance: which agent identity wrote it
  session_id          VARCHAR(128) NOT NULL,                 -- → agent_session (many events → one session)
  tool_turn           INT,                                   -- provenance: which turn in the loop
  supersedes_event_id BIGINT,                                -- adjudication: which prior event this overrides
  observed_at         DATETIME    DEFAULT CURRENT_TIMESTAMP,
  prev_hash           CHAR(64),                              -- tamper-evidence: hash chain link
  event_hash          CHAR(64)    NOT NULL,                  -- tamper-evidence: hash(this event + prev_hash)
  KEY idx_subject (tenant_id, subject_id, observed_at),
  KEY idx_session (tenant_id, session_id)
);
ALTER TABLE fact_event SET TIFLASH REPLICA 1;                -- columnar replica for lineage/audit rollups
```

`subject_id` and `session_id` are the two many-to-one join keys. Enforce them in the write path; TiDB does support `FOREIGN KEY` constraints, but confirm the cluster version before relying on them for a hot write table — app-level enforcement is typical here. **Do not put a TTL on this table** — it is the audit trail; expiring it breaks tamper-evidence. (Short-term retention belongs on a separate projection table — see note below.)

### 3. `fact_current` — write-through current truth (NOT a materialized view)

```sql
CREATE TABLE fact_current (
  subject_id       BIGINT      PRIMARY KEY,               -- one current-truth row per fact identity
  tenant_id        VARCHAR(64) NOT NULL,
  value            JSON        NOT NULL,
  winning_event_id BIGINT      NOT NULL,                  -- points at the event that currently holds
  status           ENUM('active','superseded','retracted','disputed') NOT NULL DEFAULT 'active',
  updated_at       DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_tenant_status (tenant_id, status)
);
```

**Critical:** TiDB has no native incrementally-maintained materialized view. `fact_current` is a **write-through projection** — the write path upserts it in the *same ACID transaction* as the `fact_event` append. That single transaction is exactly the "transactional supersede" claim: the event and the new current truth commit together or not at all. This is stronger than a materialized view (no refresh lag, no CDC sync) and it's the ACID boundary you point at under scrutiny.

### 4. `agent_session` — optional lineage dimension

A thin dimension (`session_id` PK, `actor_id`, `agent_id`, `started_at`, …) so lineage queries can join session metadata. Only if you want richer "who/when across sessions" rollups; the `session_id` column on `fact_event` already carries the FK.

### The retrieval / short-term tier — not in TiDB, by design

No conversational/short-term table here. In the AgentCore co-sell, **AgentCore Memory is that tier** ("what did we talk about"), wired to the fact layer via a self-managed strategy (Phase C) — rebuilding it in TiDB is the unified-substrate drift. TiDB stays pure fact-of-record. (A non-AgentCore deployment that needs the tier in-database can add a separate TTL'd table then, kept distinct — retrieval projection ≠ system of record.)

---

## The contradiction test (adjudication) — the §5 fix

This is the one component that is genuinely yours, and it fixes a real bug. **The buggy version — do not lift it:** `write_reasoning_checkpoint` supersedes every prior confirmed diagnosis within 30 days with no contradiction test — it overwrites on time-of-write alone, caps episodic recall at one, and silently destroys the prior claim. Every clause of that is wrong.

**The corrected principle:** the authoritative supersede/dispute decision is **deterministic and recorded** — never an LLM judgment. If adjudication is an LLM vibe-check, the record isn't provable and the moat is gone. Recency is *evidence inside* the test, applied only where it's semantically meaningful — never the test itself.

**Where the rules live — a predicate registry.** Each predicate is declared once with a value type and a conflict rule, as a table/config rather than logic buried in the handler, so it's auditable and extensible:

| Predicate class | Example | Conflict rule |
| --- | --- | --- |
| **Scalar / enum** | `plan` = Pro\|Enterprise; `status` = active\|expired | new ≠ current, both valid → **contradiction** |
| **Monotonic / temporal** | `balance_as_of`, `last_seen` | later observation → **update** (the *only* place recency legitimately supersedes) |
| **Set-valued** | `known_aliases`, `entitlements` | new value → **union / extend**, not supersede |
| **Free-text / unstructured** | `notes`, `description` | default → **dispute** (never silent overwrite) |

**The decision procedure** on an incoming `assert` for `(tenant_id, subject, predicate)`:

1. No current winner → append `assert`, insert `fact_current`. Done.
2. Winner exists → evaluate the predicate's conflict rule against the current value:
   - **Agreement / corroboration** → append `assert` (raises confidence), current unchanged. *Recall is not capped — the log keeps every corroboration.*
   - **Update** (monotonic predicate, later observation) → append `supersede`, re-point `fact_current`.
   - **Set extension** → append `assert`, merge into the current value.
   - **Contradiction** → go to the authority gate; never auto-supersede on recency.
3. **Authority gate** (contradictions only) — supersede vs dispute is decided by *source authority and confidence*, not recency:
   - new source outranks current → append `supersede`, re-point current.
   - same/lower authority, genuinely contradictory → append both, set `fact_current.status = 'disputed'`, surface for adjudication. **No recency tiebreak** — equal-or-lower-authority contradictions on non-monotonic predicates always go to `disputed`; recency only ever supersedes for `monotonic` predicates.

   (Source-authority ranking is itself a small `source → rank` config — a red-line item, not hardcoded.)

**The semantic layer is bounded.** Embedding/LLM similarity may run as a *pre-filter* to flag likely conflicts on free-text predicates and route them to `disputed` — it never makes the authoritative supersede decision. The provable decision stays rule-based and recorded.

**Bug → fix, clause by clause:**

| Buggy behaviour | Corrected behaviour |
| --- | --- |
| supersedes within a 30-day window | no time window; recency applies only to monotonic predicates |
| no contradiction test | the per-predicate conflict rule *is* the test |
| overwrites on recency alone | supersede gated by contradiction + source authority + confidence |
| caps recall at one | corroboration appends, sets union, disputes keep both |
| silently destroys the prior claim | the superseded event stays in the append log — that's the audit |

Recency is *evidence in* the test, never *the* test. That distinction is the moat — AgentCore Memory's async consolidation does dedup/merge, not this.

---

## Four moat properties → schema (one-to-one)

| Moat property | Where it lives | Contrast case |
| --- | --- | --- |
| **Adjudication** | contradiction test + `op='supersede'` + `status='disputed'` | Memory async dedup/merge — not adjudication |
| **Provenance** | `source`, `confidence`, `agent_id`, `session_id`, `tool_turn` columns | Lab 5 Correctness = LLM judging a transcript, no provenance |
| **Tamper-evidence** | `prev_hash` → `event_hash` chain, append-only log | say "tamper-evident," never "immutable" |
| **Correctability** | `op='correct'`/`'retract'` as new events; `fact_current` re-points | Optimization tunes prompts, can't correct a fact |

---

## The write tool: `record_fact`

**Contract (Phase A, as a Lambda handler; same code in Phase B MCP server):**

- Inputs: `tenant_id`, `subject`, `predicate`, `value`, `source`, `confidence`, plus context injected by the interceptor (`agent_id`, `session_id`, tenant-scoped DB credentials).
- Connects over the MySQL wire protocol (SQLAlchemy), using the **scoped role** from the roles step — never a generic `db_execute`.
- **One ACID transaction:** resolve current → run contradiction test → append `fact_event` (with hash chain) → upsert `fact_current`. Commit together.
- Returns: `event_id`, resulting `status`, and — if disputed — the competing `event_id`s.

This is the tool that turns "TiDB scales well" into "TiDB governs the fact." If you build one differentiated thing, build this.

Reads stay typed and enumerated: `vector_search`, `query_tenant_metrics` (named metrics, **no free SQL**). Free-form aggregation goes to the Code Interpreter path, not a `run_aggregation` tool Cedar can't constrain.

---

## Governance chain

```
Cluster + schema
  → TiDB roles (scoped per agent-type / tenant)   [Resource Control Groups: Dedicated-only, N/A on Serverless]
  → Secrets Manager (ARN-only; scoped connection secret — never env vars)
  → Tool handlers (record_fact + typed reads; MySQL wire via SQLAlchemy)
  → [Phase A] Gateway Lambda targets   OR   [Phase B] MCP-server target on Runtime
  → Gateway request interceptor (inject tenant-scoped creds) + JWT inbound (Lab 4 pattern)
  → Cedar policy (param.tenant_id == caller.claim; deny-by-default; forbid writes without role)
  → E2E validation agent (code-based Runtime agent — NOT the managed harness)
```

Three points to hold:

- **Secrets Manager is a step, not an afterthought.** Read the scoped secret by ARN. You already burned a rotation cycle on env-var exposure — don't reintroduce it.
- **The Cedar rule that earns its keep** is param-vs-claim — `context.input.tenant_id == principal.getTag("tenant_id")` in one `when` clause. Confirmed against AWS docs (it's the caller-isolation pattern AWS publishes for Memory namespaces). Not "no writes to production tables" — that's the TiDB role's job. Cedar authorizes the *call*; the DB role scopes the *data*; layered, they're what an enterprise review asks for.
- **Token design is a hard requirement — lock it before Phase A.** The IdP (Cognito / Okta / Entra) must mint `tenant_id` as a first-class JWT claim. Cedar has no string concatenation, so the claim is compared literally and cannot be derived in-policy — the exact tenant value has to arrive on the token. Retrofitting it means re-issuing tokens.

---

## Build order / dependency chain

```
1. Cluster + schema (fact_subject, fact_event, fact_current, TiFlash replica, vector index)
2. TiDB roles (Resource Control Groups omitted — unsupported on Serverless; see governance chain note)
3. Secrets Manager (scoped secret, ARN-only)
4. Handlers: record_fact (the ACID txn + contradiction test) + typed reads
5. Phase A: Lambda + Gateway targets  →  validate full chain end to end
6. Request interceptor (tenant-scoped creds) + JWT inbound
7. Cedar policies (param-vs-claim, deny-by-default)
8. E2E validation agent
──────── moat proven ────────
9.  Phase B: standalone MCP server on Runtime (IAM/SigV4) → MCP-server target
10. Phase C: Registry publish (preview) + custom-evaluator loop-closer
```

Each step needs the one before it: Cedar (7) before handlers exist (4) has nothing to enforce; the interceptor (6) before roles exist (2) has nothing to scope into.

---

## HTAP mapping (so it earns its keep, not a slide claim)

- **Write** (`record_fact`: append event + upsert current, one txn) → TiKV row store.
- **Read current truth** (point lookup on `fact_current.subject_id`) → TiKV.
- **Full-lineage audit** ("every change to this fact — who, when, superseding what") and cross-tenant analytical rollups → TiFlash columnar replica. Same cluster, no separate warehouse, no dual-write.

**Freshness caveat:** TiFlash replication is async. Say "sub-second under normal load," never "sub-second fresh" unconditionally.

---

## Accuracy guardrails baked in (for the Christoph read)

- `fact_current` is **write-through**, not a materialized view — TiDB has none.
- Event PK is **`AUTO_RANDOM`** — `AUTO_INCREMENT` on a clustered PK causes the hotspot.
- **Tamper-evident**, not immutable. Correctability via new events, not mutation.
- HNSW vector index is **available on Serverless** (Starter) — not Dedicated-gated; this cluster is Serverless.
- TiFlash freshness is **async** → "sub-second under normal load."
- AgentCore **Optimization is public preview**; **Registry is preview** and mid-namespace-migration (`bedrock-agentcore` → `agent-registry`, old namespace retiring Sep 2026). Don't hard-code the old one.
- The fact layer is **distinct from Lab 2's SEMANTIC/SUMMARIZATION** namespaces. Retrieval projection ≠ system of record.
- Don't claim TiDB "improves your GoalSuccessRate." Claim it makes correctness **provable and correctable** rather than sampled and judged. Different axis.

---

## Confirm in Phase A (deploy-gated — external claims wait on these)

The Cedar param-vs-claim rule and the pre-Cedar interceptor are both confirmed at the doc level, with direct precedents. Two things the docs don't settle — narrow deploy smoke-tests, roughly half a day, and the gate before anything goes external:

- **Does `context.input` populate for a custom MCP-server-target tool?** Every published `context.input` example is a Lambda or Memory target. It should follow the tool's advertised input schema regardless of target type — but confirm `tenant_id` arrives under `context.input.tenant_id` for the MCP server's `record_fact` before relying on the isolation rule.
- **Does the interceptor-injected credential open the tenant-scoped TiDB session end to end?** Request → injected tenant-scoped cred → handler → scoped connection. Documented as a capability; the wire is a test, not a doc claim.

Offline shortcut for the first-order Cedar question: AgentCore Cedar policies can be evaluated client-side — register the policy plus a mock `record_fact` request (principal tags, action, gateway ARN, `context.input`) and trace MATCH / no-match per condition, before standing up infra.

---

## Open for red-line

- Schema types/lengths (`VARCHAR` widths, `VECTOR` dim, `JSON` vs typed value columns).
- Contradiction test: designed above (rule-based authoritative + semantic pre-filter; rules in a predicate registry). Residual: the `source → rank` authority config, and how minimal the v1 predicate registry starts.
- Whether `agent_session` is worth the extra table for v1, or defer.
- Hash-chain design: per-subject chain vs per-tenant global chain (affects audit query shape and write contention).
- The custom-evaluator loop-closer: scope for Phase C, or pull earlier as the demo centerpiece.
