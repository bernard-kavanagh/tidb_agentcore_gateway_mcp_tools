# TiDB Governed Fact Layer

A governed **fact-of-record** service for AI agents: durable assertions written
through a **deterministic adjudication path** into TiDB and served as typed tool
handlers behind an Amazon Bedrock **AgentCore Gateway**, with **per-tenant Cedar
isolation** enforced before any query runs and a **tamper-evident, append-only
audit log** of every change.

It is the institutional memory for the [`fraud_on-aws`](https://github.com/bernard-kavanagh/fraud_on-aws)
fraud architecture — but it is domain-neutral and knows nothing about fraud.

### Repository boundary

**This repo owns** the generic Fact Layer: the typed MCP tool handlers (facts,
history, provenance, disputes, search/similarity, metrics), their TiDB data model
and deterministic adjudication, the tool schemas, and the reference AgentCore
Gateway / Cedar / Cognito wiring. **It does not own** any use case — business /
investigation context (tenant + customer/subject + transaction/case) and
fraud/support/retail semantics live in the **orchestration layer**
([`fraud_on-aws`](https://github.com/bernard-kavanagh/fraud_on-aws)), which
establishes that context and calls these generic tools. `tenant_id` is a
**requested data scope**; **authorization** to select it belongs to the deployment
governance boundary (reference profile: Cognito + Cedar tenant-equality — not a
universal dependency of the tools).

> **`ARCHITECTURE.md` is authoritative** for the Fact Layer vs governance boundary,
> the interceptor / tenant-scope / credential model, and the authorization
> profiles. Guardrails: `CLAUDE.md`. `SPEC.md` (build spec) and `DEPLOYMENT.md`
> (runbook) are original Phase-A material — **historical where they differ from
> `ARCHITECTURE.md`**.

## 1. The problem

An agent that investigates the same customer twice re-derives everything from
scratch: it has no durable, trustworthy memory of what was already concluded, by
whom, on what evidence, or how a conflict was resolved. Ordinary "agent memory"
(a blob store, or a flat vector table of past turns) is insufficient here because
a fraud conclusion is **governed data**: it needs provenance, authority, temporal
validity, contradiction handling, and an audit trail — not just recall.

## 2. Evidence → Assessment → Resolution

The core distinction this service enforces:

| Concept | Question | Here it is |
|---|---|---|
| **Evidence** | "Why?" | a REFERENCE back to a fraud-domain observation (transaction, alert, investigation, model run, chargeback…). Never a copy. |
| **Assessment** | "What do we think?" | an assertion by a source/assessor, with confidence, at a time — one `fact_event`. |
| **Resolution** | "What do we currently believe, and how was it decided?" | the current truth (`fact_current`) + the reason + the authority **policy version** that produced it. |

An agent inference is an **assessment**; it does NOT automatically become a
confirmed **resolution**. Confirmation requires an authoritative source under the
authority policy.

## 3. Architecture

```
Caller (agent / test client)
   │  Cognito JWT (tenant_id is a first-class claim)
   ▼
AgentCore Gateway ── request interceptor (independent JWT verification; pass-through)
   │                        ▼
   │                     Cedar governance (deny-by-default) — param-vs-claim tenant isolation (reference profile)
   ▼
Typed Lambda tools ──────────────────────────────────────────────────────────
   record_fact · explain_fact · get_fact · get_fact_history ·
   list_disputes · search_entities · retract_fact · vector_search · query_tenant_metrics
   ▼
TiDB (SQLAlchemy/PyMySQL, secret-by-ARN, tenant-scoped role)
   entity · fact_subject · fact_event (append-only, hash-chained) · fact_current ·
   fact_evidence · authority_policy · predicate_rule · fact_idempotency
```

Enforcement order is **interceptor → Cedar → tool**; a denied call never reaches TiDB.

## 4. Authority policy (predicate-specific, versioned)

Authority is **not** global. A source's authority depends on *what* it asserts:
a chargeback processor is authoritative for `chargeback`, a fraud reviewer/human
for `fraud_status`, a customer is *low* authority for `fraud_status`. This lives
in `authority_policy` (`predicate × source × tenant × time → authority`,
versioned). The adjudicator asks "how authoritative is this source **for this
predicate**?" (`handlers/common/authority.py`). Wildcard (`predicate='*'`) rows
preserve the legacy global ranks for predicates without a specific policy.

Every resolution records the `policy_version` that produced it, so a historical
resolution is **never silently recomputed** under a newer policy.

## 5. Adjudication (deterministic, explicit outcomes)

`handlers/common/adjudication.py` (pure, rule-based) yields explicit outcomes:

- **CORROBORATED** — same value; recall keeps prior events.
- **SUPERSEDED** — a strictly higher-authority contradiction re-points current truth (prior event retained).
- **REJECTED** — a strictly *lower*-authority contradiction is retained as **contrary evidence** and does **not** destabilize the winner (a customer's "I'm legitimate" does not overturn a fraud_review confirmation).
- **DISPUTED** — comparable-authority contradiction; both retained; status becomes `disputed`. Recency never breaks the tie (recency supersedes only for `monotonic` predicates).
- **RETRACTED** — explicit lifecycle retraction (`retract_fact`).

## 6. Provenance & evidence

Assertions carry `source`, `assessor_type` (human/system/model/agent/user), and
`confidence`, and can attach `fact_evidence` references
(SUPPORTS/CONTRADICTS/DERIVED_FROM/TRIGGERED_BY/REVIEWED_IN) pointing back to the
fraud system by id. `explain_fact` reassembles all of it — current value,
authority, supporting vs contrary evidence, assessments, superseded/disputed
assertions, and outcome history — so an agent can answer *"why do we believe this?"*.

## 7. Temporal semantics

Four distinct times, not one: `valid_from`/`valid_to` (when the fact is true in
the real world), `asserted_at` (when the claim was made), `observed_at` (source
observation), `ingested_at` (system clock). The first three are **material** and
folded into the hash chain; `ingested_at` is bookkeeping and is not.

## 8. Tenant isolation & security

`tenant_id` in a tool call is the **requested data scope** (a selector), *not* an
authenticated identity. The handler validates it and applies it deterministically
to every tenant-scoped query. **Authorization** that the caller may select that
scope belongs to the **Gateway governance layer** — the reference profile is
Cognito JWT + Cedar tenant-equality (`context.input.tenant_id ==
principal.getTag("tenant_id")`, deny-by-default, enforced in ENFORCE; shadow-only
in LOG_ONLY). Other authorization profiles (self-service, workforce/delegated,
service workload, global/read-only) can be substituted **without changing the
tools**. See `ARCHITECTURE.md` for the authoritative Fact Layer vs governance
contract. Authorization is kept separate from adjudication (what happens to an
assertion).

## 9. MCP tools (typed; no free-form SQL)

| Tool | Kind | Purpose |
|---|---|---|
| `record_fact` | write | governed assert + adjudication (idempotent via `idempotency_key`) |
| `retract_fact` | write | explicit retraction (append-only, hash-chained) |
| `explain_fact` | read | full provenance for the current fact (priority tool) |
| `get_fact` | read | current resolved value/status |
| `get_fact_history` | read | append-only event lineage |
| `list_disputes` | read | facts in a disputed resolution |
| `search_entities` | read | canonical entity lookup |
| `vector_search` | read | semantic recall (server-side Titan V2 embedding) |
| `query_tenant_metrics` | read | enumerated named metrics only |

There is deliberately **no `execute_sql`/generic query tool** — the LLM never gets raw SQL.

## 10. TiDB's role

One TiDB cluster is the OLTP store (write path), the analytical engine (TiFlash
replica for audit/metrics rollups), and the vector index (HNSW on
`fact_subject.subject_vec`, cosine, Titan Text Embeddings V2 @ 1024). Subjects are
embedded as a **normalized representation** (`entity | subject | predicate = value
| context`), not the raw id, so semantic recall finds relevant institutional
knowledge. No separate vector database.

## 11. Example (fraud investigation memory)

```
record_fact(tenant, subject="customer:123", predicate="fraud_status",
            value="confirmed", source="human_investigator", assessor_type="human",
            confidence=0.98,
            evidence=[{"evidence_type":"investigation","evidence_ref":"INV-847"},
                      {"evidence_type":"chargeback","evidence_ref":"CB-991"}],
            valid_from="2026-03-01T00:00:00Z", idempotency_key="inv-847-final")
# → SUPERSEDED/ASSERTED under fraud-policy-v1

# later investigation:
explain_fact(tenant, "customer:123", "fraud_status")
# → current=confirmed, authority=human_investigator/fraud-policy-v1,
#   supporting_evidence=[INV-847, CB-991], contrary_evidence=[...customer said legitimate...],
#   history=[ASSESSED, CONFIRMED]
```

A later **lower-authority** contrary assertion (`user_assertion: legitimate`) is
**REJECTED** (retained as contrary evidence, `confirmed` stands). Two
**comparable-authority** contradictions **DISPUTE**.

## 12. Deployment

**Full runbook: [`DEPLOYMENT.md`](DEPLOYMENT.md).** IaC for the compute tier lives
in [`cdk/`](cdk/). Two regions, on purpose: the Lambdas/Gateway/Cognito/Bedrock
run in **eu-west-1**; the TiDB cluster (and its secret) stay in **eu-central-1**
and are reached over the public TLS endpoint. `AWS_REGION` is reserved inside
Lambda (auto = eu-west-1); the Secrets Manager region is derived from the secret
ARN, so a eu-west-1 Lambda reads the eu-central-1 secret with no drift.

Staged, operator-gated — nothing mutates AWS until you opt in:

- **Gate A — prerequisites.** `source deploy/env.sh && python3 deploy/validate_config.py`;
  confirm account/region, CDK bootstrap (reuse), Bedrock model access, TiDB secret, Cognito.
- **Gate B — TiDB schema (independent of AWS).** Apply migrations to the existing
  eu-central-1 cluster with the operator-controlled runner (dry-run by default;
  `schema_migrations` tracks applied files; `03_roles` is opt-in and runs last):
  `./deploy/apply_migrations.sh --dry-run` → `APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh`
  (`--with-roles` for the scoped role). Never run by CDK/`deploy.sh`.
- **Gate B — Lambda/IAM (CDK).** `cd cdk && cdk deploy -c factLayerSecretArn=…`
  provisions the 9 tool Lambdas + interceptor + preflight + least-priv role + log
  groups. Then `bash deploy/smoke_tidb.sh` proves cross-region TLS/auth/read.
- **Gate C — Gateway (Cedar LOG_ONLY).** `bash deploy/deploy.sh` (dry-run) →
  `DEPLOY_FOR_REAL=1 bash deploy/deploy.sh`: Gateway (`--protocol-type MCP`,
  CUSTOM_JWT) + 9 targets + interceptor (`update-gateway --interceptor-configurations`)
  + Cedar engine in LOG_ONLY. `bash deploy/smoke_gateway.sh` classifies each layer.
- **Gate D — enforcement.** `DEPLOY_FOR_REAL=1 bash deploy/set_cedar_mode.sh ENFORCE`
  (flips LOG_ONLY→ENFORCE via `update-gateway --policy-engine-configuration`; no redeploy).

See DEPLOYMENT.md for the interceptor/Cedar ordering assumption and its deploy-confirm checks.

## 13. Testing

```bash
python3 tests/test_offline.py       # 34/34: adjudication (all outcomes), authority selection,
                                    #        canonical keys, temporal hashing, embedding text,
                                    #        observability redaction, hash chain, write control
python3 tests/test_interceptor.py   # 7/7:  interceptor tenant extraction, missing/mismatch,
                                    #        handler defense-in-depth tenant match
python3 tests/test_deploy_config.py # 12/12: secret-ARN parsing, two-region model, Bedrock-region
                                    #        separation, tenant map / Cedar mode / embed dim
python3 cedar/policy_eval.py        # 11/11: tenant-isolation permits incl. cross-tenant deny
TIDB_TEST_URL=... python3 tests/test_integration.py   # idempotency + cross-tenant read
                                    # isolation against a live TiDB (SKIPs without a cluster)
```

The deterministic adjudication, authority selection, tamper-evidence, and Cedar
isolation logic are provable **offline**. Behaviors that touch TiDB
(idempotency, cross-tenant data scoping, the full write path) require a live
cluster via `$TIDB_TEST_URL` and are gated so CI never falsely reports them.

## 14. Known limitations

- **Hash chain = tamper-EVIDENT, not immutable.** It detects mutation/reordering
  of the append-only log; it is not an external immutable ledger. An external
  anchor (e.g. periodic digest to a WORM store) is an optional production
  extension, not built here.
- **Confidence is not a calibrated probability** unless the source establishes
  one. Name model outputs appropriately (`model_score`/`risk_score`) in metadata;
  do not combine correlated model outputs as independent evidence.
- **Entity resolution is deterministic canonical keying**, not identity
  resolution — the service does not decide whether two customers are the same
  person unless asked to assert it.
- **No reconciliation queue / evidence-weighted mode / confidence decay** —
  single-mode adjudication only; those remain POC-phase design decisions.
- Integration/concurrency tests require a live TiDB; they were not executed in
  the environment that produced this revision.
