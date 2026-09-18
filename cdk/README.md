# Fact-layer CDK (compute tier — Gate B)

Provisions the Lambda / IAM / logging portion of the fact layer in **eu-west-1**.
The TiDB cluster stays in **eu-central-1** and is reached over its public TLS
endpoint using credentials resolved from Secrets Manager **by ARN** — this stack
never sees a database credential.

## What it creates

| Resource | Notes |
|---|---|
| `fact-layer-lambda-exec` IAM role | Shared, least-privilege. `secretsmanager:GetSecretValue` on the given ARN(s) only; `bedrock:InvokeModel` on the single Titan model; logs to its own groups only. No AdministratorAccess. |
| 9 tool Lambdas | `fact-RecordFact` … `fact-RetractFact`. One per typed tool (never collapsed). |
| `fact-Interceptor` Lambda | Request interceptor (extracts verified `tenant_id`). |
| `fact-Preflight` Lambda | Non-destructive TiDB connectivity check; operator-invoked. |
| Log groups | One per function, retention configurable. |
| Invoke permissions | AgentCore Gateway service principal may invoke the tool + interceptor functions (scoped by source account; tighten to the Gateway ARN post-creation). |

## Prerequisites

- Docker running (CDK bundles the Python deps into each function asset).
- The account is already CDK-bootstrapped in eu-west-1 — **reuse it**, do not re-bootstrap.
- Python deps: `pip install -r requirements.txt` (in this `cdk/` dir).

## Not in this stack: the TiDB schema

Database migrations are **not** applied by CDK. They run separately against the
eu-central-1 cluster via `deploy/apply_migrations.sh` (see DEPLOYMENT.md →
"Gate B — TiDB schema"). This stack only provisions the eu-west-1 compute tier;
the two are intentionally decoupled.

## Deploy (Gate B — only when the operator opts in)

```bash
cd cdk
pip install -r requirements.txt

# Review first — no AWS mutation:
cdk synth  -c factLayerSecretArn=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<name>
cdk diff   -c factLayerSecretArn=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<name>

# Deploy (real mutation — operator-gated):
cdk deploy -c factLayerSecretArn=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<name> \
           [-c tenantSecretArns=arn:...,arn:...] \
           [-c tenantSecretMap='{"acme":"arn:..."}']
```

Context values (`-c key=value`) override `cdk.json` defaults. Nothing is
hardcoded; the secret ARN is **required at deploy** (the `cdk.json` placeholder
only lets `cdk synth` run for review and is rejected by an error annotation).

## Region model

- `AWS_REGION` is **not** set as a function env var — it is a reserved Lambda
  variable auto-set to this stack's region (eu-west-1). Setting it would be
  rejected by CloudFormation.
- `BEDROCK_REGION=eu-west-1` (embeddings run in the deploy region).
- `TIDB_REGION=eu-central-1` (documentation + preflight).
- Secrets Manager region is derived from the secret ARN, so an eu-central-1
  secret is read from an eu-west-1 Lambda with no drift.

## Outputs

`InterceptorLambdaArn`, `PreflightLambdaName`, `ExecRoleArn`, and each tool ARN —
drop these into `deploy/env.sh` for Gate C (Gateway + targets + Cedar).

## Cleanup

`cdk destroy` removes everything in this stack (log groups included —
`RemovalPolicy.DESTROY`). It never touches CDKToolkit, us-east-1 CustomerSupport,
Cognito, the TiDB cluster, or the Secrets Manager secret.
