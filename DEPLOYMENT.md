# Deployment — governed fact layer (eu-west-1)

Staged, operator-gated deployment of the fact layer. **Nothing here mutates AWS
until you opt in** (`DEPLOY_FOR_REAL=1` for scripts, `cdk deploy` for the compute
tier). Read this end-to-end before running anything.

> **Status — see `ARCHITECTURE.md` (authoritative).** CDK owns **only** the Gate-B
> compute tier (the Lambdas/IAM/logs). The **Gateway, targets, Cedar policy
> engine/policies, and the Cognito reference pool** are created via the **raw
> AgentCore control-plane** (`deploy/deploy.sh`, `deploy/cognito_setup.sh`,
> `deploy/set_cedar_mode.sh`) — **not** CDK. Passages below describing the
> interceptor *injecting* a verified tenant / secret ARN, or the handler comparing
> the tool argument against an *injected* claim, reflect the original design and
> are **superseded**: the interceptor independently verifies the JWT and passes the
> request through; handlers read `tenant_id` (the requested data scope) from the
> tool argument. Reminder: **LOG_ONLY does not block** (decision is shadow/logged
> only); **ENFORCE** blocks.

## Architecture

```
Caller
  │  Cognito JWT  (tenant_id is a first-class claim)
  ▼
AgentCore Gateway (eu-west-1, CUSTOM_JWT authorizer)
  │
  ├─ request interceptor Lambda  (independently verifies the inbound JWT via JWKS;
  │                               passes the request through — no identity/cred injection)
  ▼
Cedar policy engine  (LOG_ONLY first → ENFORCE at Gate D; param-vs-claim isolation)
  ▼
9 typed MCP tool Lambdas (eu-west-1)
  record_fact · explain_fact · get_fact · get_fact_history · list_disputes ·
  search_entities · retract_fact · vector_search · query_tenant_metrics
  ▼
TiDB Cloud (eu-central-1)  — public endpoint, TLS, secret-by-ARN, tenant-scoped role
```

Enforcement order is **interceptor → Cedar → tool**; a denied call never reaches
TiDB. Embeddings call Bedrock in **eu-west-1** (the deploy region), never the
TiDB region.

## Regions (deliberately two)

| Thing | Region | Why |
|---|---|---|
| Lambdas, Gateway, Cognito, Bedrock | **eu-west-1** | The deployment region |
| TiDB cluster + its Secrets Manager secret | **eu-central-1** | Existing cluster; **not** recreated |

- `AWS_REGION` inside Lambda is **reserved** and auto-set to eu-west-1 — the CDK
  does not (and cannot) set it.
- `BEDROCK_REGION=eu-west-1`, `TIDB_REGION=eu-central-1` are explicit env vars.
- The Secrets Manager client region is **derived from the secret ARN**
  (`handlers/common/secrets.py`), so an eu-west-1 Lambda reads an eu-central-1
  secret with no drift.

### Cross-region cost / latency

Lambda (eu-west-1) → TiDB (eu-central-1) crosses regions on every query: expect
added **round-trip latency** (~tens of ms per round-trip; the write path holds a
row lock, so minimize round-trips) and **inter-region data-transfer charges** on
egress. This is accepted for the demo — do **not** relocate the cluster. If
connectivity fails, fix connectivity (see Gate B / `smoke_tidb.sh`); do not
redesign around the latency.

## Required operator-supplied values (nothing hardcoded, no secrets)

Copy `deploy/env.example.sh` → `deploy/env.sh` and fill:

| Value | Where it comes from |
|---|---|
| `FACT_LAYER_SECRET_ARN` | Secrets Manager ARN of the TiDB secret (eu-central-1). JSON keys: `host, port, username, password, database, [ssl_ca]` |
| `TENANT_SECRET_MAP` / `TENANT_SECRET_ARNS` | Optional per-tenant secret ARNs |
| `COGNITO_DISCOVERY_URL`, `COGNITO_ALLOWED_CLIENTS` | Cognito pool minting `tenant_id` (eu-west-1) |
| `GATEWAY_NAME`, `GATEWAY_ARN`, `GATEWAY_IDENTIFIER` | Gateway name up front; ARN/id after creation |
| `POLICY_ENGINE_NAME`, `POLICY_ENGINE_ARN` | Cedar engine name up front; ARN after creation |
| Lambda ARNs + `INTERCEPTOR_LAMBDA_ARN` + `PREFLIGHT_LAMBDA_NAME` | **CDK stack outputs** (Gate B) |

The secret **material** is never in env/CDK/code/logs — only the ARN is referenced.

## Gate A — prerequisites (no resource creation)

1. `source deploy/env.sh && python3 deploy/validate_config.py` — validates ARN
   shapes, the two-region model, Cedar mode, Cognito placeholders. Exit 0 to proceed.
2. Confirm: correct AWS account + region (`aws sts get-caller-identity`), CDK
   bootstrap present in eu-west-1 (reuse — never re-bootstrap), Bedrock **Titan
   Text Embeddings V2** model access enabled in eu-west-1, the TiDB secret exists,
   Cognito app client mints `tenant_id`.

## Gate B — TiDB schema (independent of AWS)

Apply the fact-layer schema to the **existing** TiDB cluster. This step is
**decoupled from AWS**: it talks to TiDB over the MySQL wire protocol, is **never**
run by `deploy/deploy.sh` or the CDK, and does not create or touch any AWS
resource. Do it before (or in parallel with) the CDK compute tier — the Lambdas
can't serve real traffic until the tables exist.

- The **TiDB cluster stays in `eu-central-1`** (not recreated, endpoint unchanged).
- The **AWS Lambda/AgentCore infrastructure is in `eu-west-1`** (Gate B-CDK below,
  Gate C). Schema application does not depend on any of it.

**Credentials** come from the existing Secrets Manager mechanism (preferred) or a
local URL — never the repo, argv, or logs. Migrations need DDL/admin rights, so
use an **admin** secret, *not* the scoped app-role secret:

```bash
export MIGRATION_SECRET_ARN=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<admin-secret>
# — or, for local/test only (URL held in an env var, never committed):
export TIDB_MIGRATION_URL='mysql+pymysql://<admin>:<pw>@<host>:4000/fact_layer'
```

### Migration tracking (added)

The repo had **no** version table and the migrations are only *partially*
idempotent (`01`/`02` plain `CREATE`/`INSERT`; `04` non-idempotent `INSERT`s).
The runner adds the smallest safe mechanism: a **`schema_migrations`** table
(`filename` PK, `checksum`, `applied_at`) recording each applied file. State is
read from that table — **never** inferred from whether tables happen to exist —
so each file is applied at most once, and an already-applied file that was later
**edited** is caught as checksum drift and refused (not silently re-run).

### Read-only commands (never mutate TiDB)

```bash
./deploy/apply_migrations.sh --check     # validate config; show host/port/db/TLS,
                                         # discovered files, and applied/pending state
./deploy/apply_migrations.sh --dry-run   # the above + the ordered list that WOULD apply
```

`--check`/`--dry-run` connect only to `SELECT` the tracking table; they make no
changes. The preflight prints host/port/database/TLS/dir/files/state and **never**
a password. If the connection looks like a placeholder (`<HOST>`, empty db, …) it
fails fast rather than guessing.

### Mutating command (explicit opt-in)

```bash
APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh              # apply pending core migrations
```

Default behaviour (no `APPLY_FOR_REAL`) is a dry-run — nothing is written. The
runner applies pending migrations in numeric order, prints each as it goes,
records it in `schema_migrations`, and **stops on the first failure** with a
non-zero exit.

### Verify status

```bash
./deploy/apply_migrations.sh --check      # "applied:" vs "pending:" lines
# or directly:  SELECT filename, applied_at FROM schema_migrations ORDER BY filename;
```

### Migration 03 (roles) — what it does and whether it's optional

`03_roles.sql` creates a **scoped, least-privilege TiDB role** (SELECT/INSERT/UPDATE
on the fact tables, no DDL/DELETE — the append-only enforcement) and is the
last-line data scope behind Cedar. (It originally also created a resource group
for load isolation; that is a Dedicated-tier feature and was removed —
`CREATE RESOURCE GROUP` is unsupported on this Serverless cluster.) It is
**operator-specific and applied separately** for two concrete reasons:

1. It has `<FACT_ROLE>` / `<PASSWORD_FROM_SECRET>` / `<DB>` **placeholders** — not
   executable as-is.
2. It **`GRANT`s on `entity` / `entity_alias` / `fact_evidence` (created in `05`)
   and `fact_idempotency` (created in `07`)**, so despite its "03" number it must
   run **after** the core schema, not in numeric position.

So it is **excluded from the default set** and applied **last** only when you opt in:

```bash
# On Serverless the user name carries the cluster prefix and must be <=32 chars total.
export FACT_ROLE=QxfqVLLusLjpqEY.fact_runtime  TIDB_DB=fact_layer
export FACT_ROLE_SECRET_ARN=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<role-secret>
APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh --with-roles
```

The role password is read from `FACT_ROLE_SECRET_ARN` (Secrets Manager) at
runtime, substituted **in memory only**, and redacted from all output. It is
**required for the production/demo runtime** (the handlers connect as this scoped
role via `FACT_LAYER_SECRET_ARN`); it was intentionally skipped in the test
harness because that harness connected as an admin and did not need a scoped role.
`--with-roles` **fails fast** if `FACT_ROLE`/`TIDB_DB`/`FACT_ROLE_SECRET_ARN` are
unset — it never silently skips.

### Recover from a failed migration

TiDB DDL **auto-commits per statement** (it is not transactional), so a file that
fails partway may have applied some of its statements. The runner records a file
in `schema_migrations` **only on full success**, so a failed file is *not* marked
applied. Because `01`/`02`/`04` are not self-idempotent, blindly re-running a
half-applied file can fail on an already-created object. To recover:

1. Read the printed error (which statement/object failed) — the file name is shown.
2. Inspect TiDB and finish or undo that file's partial objects (e.g. `DROP` the
   table/column it got partway through), so the file can run cleanly from the top.
3. Re-run `--dry-run` to confirm it is still pending, then `APPLY_FOR_REAL=1` again.

Never hand-insert a row into `schema_migrations` to "skip" a file — that hides
missing objects the app depends on.

### Serverless (Starter) tier caveats

This cluster is **TiDB Cloud Serverless** (a.k.a. Starter), not Dedicated. A few
schema/design details behave differently here:

- **Vector search is supported.** Per TiDB docs, vector search + the HNSW index are
  available on Starter/Serverless, Essential, and Dedicated, plus Self-Managed —
  **not** Premium (Dedicated/Self-Managed need TiDB v8.4.0+). So it is *not*
  Dedicated-gated. (`01_schema.sql`'s header comment still says "Dedicated/Premium;
  tier-gated" — that is **stale but intentionally frozen**: the file is an applied
  migration and editing it would trip checksum drift in `apply_migrations.sh`.
  Treat this section as the source of truth, not that comment.)
- **No resource control groups.** `CREATE RESOURCE GROUP` is unsupported on
  Serverless, so `03_roles.sql` creates none. The DB-layer "one caller can't starve
  others" guarantee does **not** hold here — enforce fairness above the DB
  (per-tenant Lambda concurrency / Gateway throttling) or move to Dedicated.
- **TiFlash replica count is platform-managed.** `01_schema.sql` requests
  `SET TIFLASH REPLICA 1`, but Serverless controls the actual count — the live
  `fact_event` table sits at replica_count=2. This is expected; `verify_schema`
  checks `>= 1`, and `migrate.py` reports any count delta as a NOTE, not a failure.
- **User names carry the 16-char cluster prefix and cap at 32 chars total** — see
  the `FACT_ROLE` note above (`QxfqVLLusLjpqEY.fact_runtime`).

## Gate B — Lambda / IAM (CDK)

Deploy **only** the compute tier, then test it against TiDB before any Gateway exists.

```bash
cd cdk && pip install -r requirements.txt      # needs Docker for bundling
cdk synth  -c factLayerSecretArn=$FACT_LAYER_SECRET_ARN     # review, no mutation
cdk diff   -c factLayerSecretArn=$FACT_LAYER_SECRET_ARN
cdk deploy -c factLayerSecretArn=$FACT_LAYER_SECRET_ARN \
           [-c tenantSecretArns=$TENANT_SECRET_ARNS]        # operator-gated mutation
```

Record the stack outputs (ARNs) into `deploy/env.sh`, then:

```bash
source deploy/env.sh && bash deploy/smoke_tidb.sh          # invokes fact-Preflight
```

`smoke_tidb.sh` proves DNS → TCP → TLS → auth → `SELECT 1` cross-region and
classifies any failure layer. Verify: TLS on, auth works, a read returns, it
reaches the configured instance, and **no secret appears in the logs**.

## Gate C — AgentCore Gateway (Cedar in LOG_ONLY)

```bash
# dry-run first (prints every command):
bash deploy/deploy.sh
# real (operator-gated):
DEPLOY_FOR_REAL=1 bash deploy/deploy.sh
```

`deploy/deploy.sh` (in order): creates the Gateway (`--protocol-type MCP`,
CUSTOM_JWT) → registers the 9 Lambda targets → attaches the interceptor via
`aws bedrock-agentcore-control update-gateway --interceptor-configurations` →
creates the Cedar policy engine attached in **LOG_ONLY** and loads the policy.

Then run the layered smoke test (provide real tokens/endpoint via env):

```bash
bash deploy/smoke_gateway.sh
```

It classifies each scenario as **AUTH / CEDAR / HANDLER / LAMBDA / TIDB /
ALLOWED** so failures are unambiguous, and covers: tenant A → A (allow), tenant B
→ B (allow), tenant A → B (never allowed), missing claim (never allowed), and how
to confirm a denied request never reached TiDB.

## Gate D — enforcement (operator-controlled)

Only after Gate C passes:

```bash
DEPLOY_FOR_REAL=1 bash deploy/set_cedar_mode.sh ENFORCE
```

Flips the Cedar engine LOG_ONLY → ENFORCE via `update-gateway
--policy-engine-configuration` — no Lambda redeploy, no gateway recreation.
Roll back with `... set_cedar_mode.sh LOG_ONLY`.

## Interceptor / Cedar ordering — assumption + how to prove it

**Intended order:** JWT → interceptor (verify/extract tenant) → Cedar → tool.

**What actually gates tenant isolation, and where it comes from:**

- **Cedar** compares `context.input.tenant_id` (the *tool argument*) against
  `principal.getTag("tenant_id")` (the JWT claim mapped to a principal tag by the
  **authorizer**). Neither side of that comparison depends on the interceptor's
  injected context — so Cedar isolation holds **regardless** of whether the
  interceptor runs before or after it.
- The **interceptor** independently verifies the inbound JWT (JWKS/RS256) and
  passes the MCP request through unchanged — it does **not** inject identity or a
  credential (an MCP Lambda target receives only the tool arguments; there is no
  `_fact_ctx` side channel). See `ARCHITECTURE.md`.
- The **handler** (`handlers/common/context.py`) reads `tenant_id` — the requested
  data scope — from the tool argument and applies it to every tenant-scoped query.
  In **LOG_ONLY** Cedar only logs its decision (it does **not** block); tenant
  isolation in that mode rests on the per-query `tenant_id` predicate. **ENFORCE**
  is what makes Cedar actively block a cross-tenant call.

Empirically established under LOG_ONLY (evidence table in `ARCHITECTURE.md`):

- **L0 PROVEN** (authorizer), **L4 PROVEN** (interceptor JWT verification);
  **L2 / L3 SUPPORTED BY CONTROLLED INFERENCE** — the tenant claim reaches Cedar
  as `principal.getTag("tenant_id")` and the argument as `context.input.tenant_id`
  (behavioral evidence: A=ALLOW, B=DENY, C=DENY). Not claimed as direct proof.
- **Credential path** — the handler resolves the DB secret ARN from configuration
  for the requested scope (current reference profile: a shared runtime credential;
  per-tenant credentials are an optional defense-in-depth profile).

If AgentCore's real ordering differs from the assumption (e.g. the interceptor
cannot influence what Cedar sees), **report the observed behavior** and the
smallest architecture-compatible change (e.g. rely on the authorizer→principal-tag
mapping for the Cedar claim, which already does not need the interceptor) — do
not invent a silent workaround. `smoke_gateway.sh` + the CloudWatch policy log
(LOG_ONLY) are the tools to observe it.

## What is NOT touched

us-east-1 CustomerSupport AgentCore, existing Cognito, existing IAM roles,
`CDKToolkit`, unrelated EKS/TiDB/workshop resources, and the TiDB cluster itself.
This is a new isolated stack (`FactLayerComputeStack`) + a new Gateway in eu-west-1.

## Cleanup

`cdk destroy` (compute tier, log groups included) + remove the Gateway / targets /
policy engine (`agentcore remove …`). Secrets, Cognito, and the TiDB cluster are
operator-owned and left in place. See the plan in the repo history for the full
reverse-dependency order.
