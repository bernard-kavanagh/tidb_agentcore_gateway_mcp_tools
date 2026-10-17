# Governed Fact Layer for AI Agents

**Built with TiDB, Amazon Bedrock AgentCore Gateway, MCP and Cedar.**

> **Give AI agents persistent knowledge without making every AI assertion a fact.**

AI agents can retrieve information, reason over evidence and make assessments. But
when multiple agents, models and humans disagree, which assertion should an
organisation trust?

The **Governed Fact Layer** is a durable, auditable system for recording
assertions, applying authority policies, resolving contradictions and retrieving
the currently accepted resolution. Unlike conventional agent memory, it keeps
three things distinct:

| | | |
|---|---|---|
| **Evidence** | *Why?* | The external observations and records supporting an assertion — a **reference** back to a source record (an investigation, a chargeback, a model run), never a copy. |
| **Assessment** | *What do we think?* | What an agent, model, system or human believes, with confidence, at a time — one append-only event. |
| **Resolution** | *What do we currently accept, and how was it decided?* | What the organisation currently accepts under an **explicit, versioned authority policy** — the current value plus the reason and the policy version that produced it. |

The reference implementation provides:

- Deterministic, predicate-specific fact **adjudication**
- Append-only, **tamper-evident** assertion history (hash chain)
- Evidence **provenance** and explainable resolutions
- Explicit **contradiction, dispute and retraction** handling
- **Temporal** validity and historical assertions
- **Tenant-scoped**, policy-governed tool access
- **Semantic retrieval** alongside transactional facts

## Why it matters

An AI agent should be able to remember a previous investigation without treating
every previous conclusion as authoritative. The Fact Layer separates the agent's
*reasoning process* from the organisation's *governed knowledge*:

> **Agents generate assessments. Authority policies determine resolutions. The Fact Layer preserves the evidence and history.**

An agent inference is an **assessment** — it does **not** automatically become a
confirmed **resolution**. Confirmation requires an authoritative source under the
authority policy.

## What this project is

A **domain-neutral reference implementation** of governed institutional memory for
AI agents. The included **fraud-investigation scenario** demonstrates the
architecture, but the core fact model knows nothing about fraud — predicates,
values and authority mappings are all data.

It is a reference implementation with documented limitations and production-hardening
work (see [Assurance & limitations](#assurance--limitations)). It is **not** a
complete agent-orchestration framework, and **not** a replacement for every
enterprise data source.

---

## 30-second contradiction demo

Three agents/sources assert on the same fact. The Fact Layer adjudicates each one
**deterministically** and keeps the whole story explainable. This is the
differentiator — not the storage.

```text
subject = customer:123   predicate = fraud.account_status   (policy: fraud-policy-v1)

1.  model_scorer  asserts "suspected"   conf 0.70   evidence: RISK-RUN-55
        └─> ASSERTED        current = suspected          (first assessment on record)

2.  human_investigator asserts "confirmed"  conf 0.98  evidence: INV-847, CB-991
        └─> SUPERSEDED      current = confirmed          (strictly higher authority
                                                          for this predicate; prior
                                                          event retained, not deleted)

3.  end_user (the customer) asserts "legitimate"  conf 0.95
        └─> REJECTED        current = confirmed          (strictly LOWER authority —
                                                          kept as CONTRARY EVIDENCE,
                                                          does not destabilise truth)

   …a second comparable-authority reviewer disagreeing instead would yield:
        └─> DISPUTED        status  = disputed           (both retained; recency does
                                                          NOT break the tie)
```

Then ask *why*:

```text
explain_fact(customer:123, fraud.account_status)
 → current            = confirmed
   authority          = human_investigator  (fraud-policy-v1)
   supporting_evidence= [INV-847, CB-991]
   contrary_evidence  = [end_user: "legitimate"]
   history            = [ASSERTED → SUPERSEDED → REJECTED]
```

The policy and the evidence are visible at every step. The outcome does **not**
depend on who asserted *last*; it depends on who is authoritative *for this
predicate*.

**Reproduce the adjudication logic offline — no AWS, no cluster:**

```bash
python3 tests/test_offline.py      # 34/34 — every outcome above, plus hash-chain tamper-evidence
```

---

## Architecture

```text
Caller (agent / test client)
   │  Cognito JWT  (identity)
   ▼
AgentCore Gateway ── request interceptor (independent JWT verification)
   │                        ▼
   │                  Cedar governance (deny-by-default authorization)
   ▼
Typed MCP tool handlers (AWS Lambda) ──────────────────────────────────────────
   record_fact · explain_fact · get_fact · get_fact_history · list_disputes ·
   search_entities · retract_fact · vector_search · query_tenant_metrics
   │
   │   ┌──────────────────────────────────────────────────────────┐
   │   │  AUTHORITY ENGINE (deterministic, server-side)            │
   │   │  predicate × source × tenant × time → effective authority │
   │   │  → adjudication outcome (CORROBORATE / SUPERSEDE /        │
   │   │     REJECT / DISPUTE / RETRACT) under a pinned            │
   │   │     policy_version                                        │
   │   └──────────────────────────────────────────────────────────┘
   ▼
TiDB (SQLAlchemy/PyMySQL, secret-by-ARN, tenant-scoped role)
   entity · fact_subject · fact_event (append-only, hash-chained) · fact_current ·
   fact_evidence · authority_policy · predicate_rule · fact_idempotency
```

One TiDB cluster serves three roles: the OLTP write path, the analytical engine
(TiFlash replica for audit/metrics rollups), and the vector index (HNSW on
`fact_subject.subject_vec`). No separate vector database.

> **Enforcement order is interceptor → Cedar → tool; a denied call never reaches
> TiDB.** This ordering is v3.2 Week-1 **blocker #1** and is **PENDING
> VERIFICATION** against live AWS behaviour — treat it as the current build's
> assumption, not confirmed AWS behaviour.

---

## What makes it different

- **Authority is predicate-specific and versioned.** Authority is *not* global. A
  chargeback processor is authoritative for `chargeback`; a fraud reviewer or human
  for `fraud_status`; a customer is *low* authority for `fraud_status`. This lives
  in `authority_policy` (`predicate × source × tenant × time → authority`), and
  every resolution records the `policy_version` that produced it — so a historical
  resolution is **never silently recomputed** under a newer policy.

- **Contradictions are resolved deterministically, with explicit outcomes.**
  `CORROBORATED` / `SUPERSEDED` / `REJECTED` / `DISPUTED` / `RETRACTED` — rule-based,
  not model-decided. A lower-authority contradiction is retained as *contrary
  evidence*, not silently dropped. Recency supersedes **only** for `monotonic`
  predicates — there is no blanket "overwrite everything within N days" rule.

- **Provenance is first-class.** Assertions carry `source`, `assessor_type`
  (human / system / model / agent / user) and `confidence`, and attach
  `fact_evidence` references (SUPPORTS / CONTRADICTS / DERIVED_FROM / TRIGGERED_BY
  / REVIEWED_IN) pointing back to source records by id. `explain_fact` reassembles
  all of it to answer *"why do we believe this?"*.

- **History is append-only and tamper-evident.** Corrections and retractions are
  **new events**, never row mutations. Events are hash-chained per subject, so
  mutation or reordering of the log is detectable. Four distinct clocks are kept —
  `valid_from`/`valid_to` (true in the world), `asserted_at` (claim made),
  `observed_at` (source observation), `ingested_at` (system) — the first three are
  material and folded into the hash.

- **Tenant isolation is enforced before any query runs.** Every tenant-scoped tool
  applies `WHERE tenant_id = :t` deterministically; authorization to select a scope
  is decided at the governance boundary (reference profile: Cognito + Cedar
  tenant-equality, deny-by-default). Authorization is kept separate from adjudication.

- **No free-form SQL surface.** Reads are typed and enumerated; `query_tenant_metrics`
  exposes named metrics only. The LLM never gets raw SQL — there is deliberately no
  `execute_sql` / `db_query` tool.

### How it compares to conventional agent memory

The Fact Layer **complements** episodic memory, RAG and ordinary databases — it does
not replace all of them.

| Approach | Good at | Missing for governed knowledge |
|---|---|---|
| **Episodic / "what did we talk about" memory** | Recalling conversation turns | No authority, no contradiction resolution, no provenance — every past turn is equally "true" |
| **RAG over a vector store** | Finding relevant documents | Retrieves text, not *decisions*; no notion of who is authoritative or what the org currently accepts |
| **A plain SQL/Aurora table** | Durable rows + transactions | No adjudication, no append-only tamper-evidence, no predicate-specific authority or explainability out of the box |
| **Governed Fact Layer** | Deciding and preserving *what the organisation currently accepts* | — (purpose-built for exactly this; delegates conversational recall to AgentCore Memory) |

> Conversational "what did we talk about" storage is **out of scope** — that is
> AgentCore Memory's job. The Fact Layer is the **governed fact of record**.

---

## Try it locally

No AWS and no TiDB cluster are needed to exercise the deterministic core — the
adjudication, authority selection, tamper-evidence and Cedar isolation logic are all
provable offline.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python3 tests/test_offline.py        # adjudication (all outcomes), authority, hashing, write-control
python3 cedar/policy_eval.py         # tenant-isolation permits incl. cross-tenant deny
python3 -m pytest tests/ -q --ignore=tests/test_integration.py   # full offline suite
```

To run the full write path, idempotency and cross-tenant isolation checks against a
**live TiDB**, point `$TIDB_TEST_URL` at a scratch database with the schema applied:

```bash
# apply sql/01_schema.sql, 04–07, then 02_seed.sql to a scratch DB, then:
TIDB_TEST_URL='mysql+pymysql://user:pass@host:4000/scratch?ssl_verify_cert=true' \
    python3 tests/test_integration.py
```

## Deploy to AWS

**Full runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md).** Deployment is staged and
operator-gated — **nothing mutates AWS until you opt in**. Two regions on purpose:
Lambdas / Gateway / Cognito / Bedrock run in **eu-west-1**; the TiDB cluster and its
secret stay in **eu-central-1** over the public TLS endpoint.

| Gate | What it does | Command (dry-run → for-real) |
|---|---|---|
| **A — prerequisites** | Confirm account/region, CDK bootstrap, Bedrock access, TiDB secret, Cognito | `source deploy/env.sh && python3 deploy/validate_config.py` |
| **B — TiDB schema** | Apply migrations to the eu-central-1 cluster (`schema_migrations` tracks applied files; roles opt-in) | `./deploy/apply_migrations.sh --dry-run` → `APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh` |
| **B — Lambda/IAM (CDK)** | Provision the 9 tool Lambdas + interceptor + least-priv role + log groups | `cd cdk && cdk deploy -c factLayerSecretArn=…` then `bash deploy/smoke_tidb.sh` |
| **C — Gateway (Cedar LOG_ONLY)** | Gateway (`--protocol-type MCP`, CUSTOM_JWT) + 9 targets + interceptor + Cedar in shadow mode | `bash deploy/deploy.sh` → `DEPLOY_FOR_REAL=1 bash deploy/deploy.sh` |
| **D — enforcement** | Flip Cedar LOG_ONLY → ENFORCE (no redeploy) | `DEPLOY_FOR_REAL=1 bash deploy/set_cedar_mode.sh ENFORCE` |

> **Verify tool syntax before running deploy commands.** AgentCore CLI flags and the
> TiDB vector-index DDL are version-specific — check `agentcore --help` / current docs
> first. Secrets are referenced by ARN from Secrets Manager only; never env-var
> secrets.

### MCP tools (typed; no free-form SQL)

| Tool | Kind | Purpose |
|---|---|---|
| `record_fact` | write | governed assert + adjudication (idempotent via `idempotency_key`) |
| `retract_fact` | write | explicit retraction (append-only, hash-chained) |
| `explain_fact` | read | full provenance for the current fact (priority tool) |
| `get_fact` | read | current resolved value / status |
| `get_fact_history` | read | append-only event lineage |
| `list_disputes` | read | facts in a disputed resolution |
| `search_entities` | read | canonical entity lookup |
| `vector_search` | read | tenant-scoped semantic recall (server-side embedding) |
| `query_tenant_metrics` | read | enumerated named metrics only |

---

## Assurance & limitations

### Test evidence (reproducible)

CI runs the offline suite on every push ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
Behaviours that touch TiDB are **gated** on `$TIDB_TEST_URL` and skip cleanly, so CI
never falsely reports them as run.

| Matrix | What it proves | Needs | CI |
|---|---|---|---|
| **Offline logic** — `tests/` (144 passing), incl. `test_offline.py` (34), `test_interceptor.py` (11), `test_deploy_config.py` (12), `test_context.py`, `test_tls.py`, migration/verifier suites | Adjudication (all outcomes), authority selection, canonical keys, temporal hashing, hash chain, write control, interceptor JWT verification, TLS hardening, config/cross-region correctness | nothing | ✅ runs |
| **Cedar isolation** — `cedar/policy_eval.py` (11 cases) | Tenant-isolation permits incl. cross-tenant deny | nothing | ✅ runs |
| **TiDB integration** — `tests/test_integration.py` | Idempotency + cross-tenant read isolation on the real write path | live TiDB via `$TIDB_TEST_URL` | ⏭️ skipped (no cluster in CI) |
| **Deployed Gateway security** — `deploy/smoke_*.sh`, Cedar ENFORCE probes | End-to-end interceptor/Cedar/tenant enforcement | deployed AWS stack | manual, operator-run |

Advertised counts are not a substitute for running them — the commands above
reproduce every offline number.

### Known limitations

- **Hash chain = tamper-EVIDENT, not immutable.** It detects mutation/reordering of
  the append-only log; it is not an external immutable ledger. An external anchor
  (e.g. periodic digest to a WORM store) is an optional production extension, not built
  here.
- **Confidence is not a calibrated probability** unless the source establishes one.
  Do not combine correlated model outputs as independent evidence.
- **Entity resolution is deterministic canonical keying**, not identity resolution —
  the service does not decide whether two customers are the same person unless asked
  to assert it.
- **No reconciliation queue / evidence-weighted mode / confidence decay** —
  single-mode adjudication only.
- **Integration/concurrency tests require a live TiDB** and are not executed in CI.

### Status & production-hardening roadmap

This repo tracks against a consensus build plan
([`docs/BUILD_PLAN_v3.2.md`](docs/BUILD_PLAN_v3.2.md)). Status uses one vocabulary —
**DESIGN FROZEN / PENDING VERIFICATION / IMPLEMENTED / PASSED LIVE VALIDATION** — and
a design being agreed does **not** make it implemented.

| Capability | Status | Notes |
|---|---|---|
| Deterministic adjudication, authority selection, v1 hash chain | **IMPLEMENTED** | Provable offline today |
| Predicate-keyed, versioned `authority_policy` + `policy_version` | **IMPLEMENTED** | On the write path |
| Tenant isolation (reference profile) | **IMPLEMENTED** | Current build: `tenant_id` is a caller-supplied **selector** authorized by Cedar tenant-equality |
| Interceptor-resolved tenancy (`tenant_id` removed from LLM-facing schemas) | **DESIGN FROZEN → Week 2** | The v3.2 target; **not** the running model |
| Interceptor → Cedar ordering | **PENDING VERIFICATION** | Week-1 blocker #1 |
| KMS asymmetric receipts (verify-only Fact Layer) | **DESIGN FROZEN → Week 2** | No receipt/KMS code exists yet |
| Hash v2 + `evaluated_at` adjudication clock + stale-incumbent threshold | **DESIGN FROZEN → Week 1/3** | — |
| Domain-neutrality pass (strip fraud wording from core) | **DESIGN FROZEN → Week 2** | Fraud wording still lives in some handlers/schemas |
| Source Adapter SDK (`TiDBSource` / `PostgresSource`), domain packs, `factlayer` CLI | **DESIGN FROZEN → Week 4–8** | — |

---

## Repository boundary

**This repo owns** the generic Fact Layer: the typed MCP tool handlers (facts,
history, provenance, disputes, search/similarity, metrics), their TiDB data model and
deterministic adjudication, the tool schemas, and the reference AgentCore Gateway /
Cedar / Cognito wiring. **It does not own** any use case — business / investigation
context (tenant + subject + case) and fraud/support/retail semantics live in the
**orchestration layer** (e.g.
[`fraud_on-aws`](https://github.com/bernard-kavanagh/fraud_on-aws)), which establishes
that context and calls these generic tools.

### Document precedence & authority

[`docs/BUILD_PLAN_v3.2.md`](docs/BUILD_PLAN_v3.2.md) **(Consensus Baseline v3.2)** is
the build authority — read it first. Precedence when sources disagree:

> **`BUILD_PLAN_v3.2` > [`ARCHITECTURE.md`](ARCHITECTURE.md) (except where its banner
> marks a section superseded) > [`SPEC.md`](SPEC.md) / [`DEPLOYMENT.md`](DEPLOYMENT.md)
> (historical).** Guardrails live in [`CLAUDE.md`](CLAUDE.md).

> **⚠️ Tenancy & identity — current build vs v3.2 target.** This README describes the
> **current deployed build**, in which `tenant_id` is a caller-supplied **tool
> argument** authorized by Cedar tenant-equality. Under v3.2 that is **obsolete**:
> tenant becomes **never model input** — the trusted interceptor resolves exactly one
> tenant from asserted identity and `tenant_id` is removed from LLM-facing schemas.
> The v3.2 receipt/attestation, adapter-SDK, domain-pack and CLI pillars are **not yet
> built**. `SPEC.md` and `DEPLOYMENT.md` are retained as historical Phase-A material.
