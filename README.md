# TiDB Governed Fact Layer — Phase A

A governed fact-of-record service for AI agents: facts are written through a
**deterministic adjudication path** into TiDB and served as tool handlers behind
an Amazon Bedrock AgentCore Gateway, with **per-tenant Cedar isolation** enforced
before any query runs and a **tamper-evident audit log** (per-subject hash chain)
of every change.

See `SPEC.md` for the full build spec and `CLAUDE.md` for the guardrails.

## Confirmed build decisions (v1)

| Decision | Choice |
|---|---|
| Hash-chain scope | **Per-subject** — chain tip tracked on `fact_subject.last_event_hash`; the write path `SELECT ... FOR UPDATE`s that row to serialize per-subject writes |
| Embedding | **Titan Text Embeddings V2 @ 1024** → `VECTOR(1024)`, cosine distance |
| Predicates | `status` (scalar), `last_seen` (monotonic), `known_aliases` (set), `notes` (freetext) |
| Source ranks | system_of_record=100, verified_integration=80, user_assertion=60, agent_inference=40 |

## Layout

```
sql/
  01_schema.sql          5 tables; AUTO_RANDOM PKs; inline HNSW index; TiFlash replicas
  02_seed.sql            predicate_rule + source_rank starter rows
  03_roles.sql           tenant-scoped role + resource control group (append-only grants)
handlers/
  common/                secrets-by-ARN, SQLAlchemy engine, hashing/chain, embeddings, context, adjudication
  record_fact.py         the ACID write path + contradiction test
  vector_search.py       typed semantic recall (tenant-scoped)
  query_tenant_metrics.py enumerated named metrics only
gateway/
  schemas/*.schema.json  tool inputSchemas (type:object at top, no json wrapper)
  interceptor.py         pre-Cedar request interceptor (inject identity + tenant-scoped cred)
cedar/
  tenant_isolation.cedar param-vs-claim permits, ENFORCE deny-by-default
  policy_eval.py         offline policy evaluation (deploy-confirm A)
deploy/
  env.example.sh         operator inputs (ARNs/IDs/URLs only — no secrets)
  deploy.sh              gateway + targets + interceptor + policy (dry-run by default)
tests/
  test_offline.py        adjudication + hash-chain tests (no live cluster)
```

## What is verified now (no cluster needed)

```bash
python3 tests/test_offline.py     # 17/17: contradiction test (all 4 value_types + authority gate) + hash chain
python3 cedar/policy_eval.py      # 6/6: tenant isolation permits, incl. cross-tenant deny + deploy-confirm A logic
```

The deterministic adjudication and the tamper-evidence are provable without
infrastructure — that is the moat, and it is green.

## Deploy (needs operator env inputs)

1. `cp deploy/env.example.sh deploy/env.sh` and fill in region, TiDB host/secret ARN,
   Cognito discovery URL + client IDs, gateway name. **Token requirement (hard):**
   the Cognito app client must mint `tenant_id` as a first-class JWT claim before
   the Cedar step, or the isolation rule cannot be written.
2. Apply schema + seed + role to the cluster:
   `mysql ... < sql/01_schema.sql && mysql ... < sql/02_seed.sql && mysql ... < sql/03_roles.sql`
   (store the role's password in the Secrets Manager secret; never in the SQL).
3. Package & deploy the four Lambdas (`handlers/` + `requirements.txt`), record their ARNs in `env.sh`.
4. `bash deploy/deploy.sh` (dry-run). Verify the AgentCore CLI flags against
   `agentcore --help`, then re-run with `DEPLOY_FOR_REAL=1`.

## Acceptance tests → how each is exercised

| # | Test | How |
|---|---|---|
| 1 | Write + read | `record_fact` on a new `(tenant,subject,predicate)` → 1 `fact_event` + 1 `fact_current` in one txn; `vector_search` returns it |
| 2 | Adjudication | higher-authority contradiction → `supersede` event, `fact_current` re-points, prior event stays, chain verifies (`test_offline` covers the logic; live path exercises the txn) |
| 3 | Corroboration | agreeing assert appends + keeps prior events; subject history > 1 event |
| 4 | Dispute | equal/lower-authority contradiction → `fact_current.status='disputed'`, both events retained |
| 5 | Tenant isolation | cross-tenant `context.input.tenant_id` denied by Cedar; deny in CloudWatch policy log; TiDB never queried (`policy_eval.py` pre-checks the logic) |
| 6 | Deploy-confirm A | `context.input.tenant_id` populates for the tool call (offline pre-check in `policy_eval.py`; confirm on the live gateway) |
| 7 | Deploy-confirm B | interceptor-injected tenant-scoped credential opens the correct TiDB session end to end |

Tests 2–6 are verified at the logic level now; 1–7 complete against the live
gateway + cluster once env inputs are supplied.

## Non-negotiables honoured

- `AUTO_RANDOM` on every clustered PK — never `AUTO_INCREMENT`.
- `fact_current` upserted in the **same transaction** as the `fact_event` append (no materialized view).
- `fact_event` append-only; corrections/retractions are new events; hash-chained (tamper-evident, not immutable). The role holds no `UPDATE`/`DELETE` on it.
- Secrets by ARN only; no secret material in env or code.
- No free-form SQL surface; reads typed and enumerated.
- Adjudication deterministic and rule-based; recency supersedes **only** for `monotonic`.
- `tenant_id` compared literally in Cedar against a first-class JWT claim.
