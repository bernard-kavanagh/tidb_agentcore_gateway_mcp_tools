# Operator-supplied environment inputs. Copy to deploy/env.sh and fill in.
# NONE of these are guessed — they come from the operator (see SPEC "Environment
# inputs" and DEPLOYMENT.md). Only ARNs/IDs/URLs live here; NO secret material.

# ── Regions (INTENTIONALLY two) ───────────────────────────────────────────────
# Deployment region: Lambdas, AgentCore Gateway, Cognito, Bedrock embeddings.
export AWS_REGION="eu-west-1"
# TiDB cluster region: the existing cluster stays here. Documentation + preflight;
# the DB is reached over its PUBLIC TLS endpoint, not an AWS regional API.
export TIDB_REGION="eu-central-1"
# Bedrock (Titan embeddings) region — must be the DEPLOY region, where model
# access is enabled. Never the TiDB region.
export BEDROCK_REGION="eu-west-1"
# NOTE: inside Lambda, AWS_REGION is a RESERVED variable auto-set to the
# function's region (eu-west-1). The CDK does NOT set it (CloudFormation rejects
# it). Secrets Manager derives its region from the secret ARN below, so an
# eu-central-1 secret is read from an eu-west-1 Lambda with no drift.

# ── TiDB ──────────────────────────────────────────────────────────────────────
export TIDB_DB="fact_layer"                 # schema/database name used in 03_roles.sql grants

# Secrets Manager ARN of the scoped TiDB connection secret. It typically lives in
# TIDB_REGION (eu-central-1) alongside the cluster; the handler reads it
# cross-region by parsing the region OUT of this ARN.
# Secret JSON keys: host, port, username, password, database, [ssl_ca]
export FACT_LAYER_SECRET_ARN="arn:aws:secretsmanager:eu-central-1:<ACCT>:secret:<NAME>"

# Optional per-tenant credential swapping (interceptor). JSON: {"tenantA":"arn:...","tenantB":"arn:..."}
export TENANT_SECRET_MAP='{}'
# Optional comma-separated list of tenant secret ARNs to grant the Lambda role
# GetSecretValue on (the values of TENANT_SECRET_MAP). CDK reads this as context.
export TENANT_SECRET_ARNS=""

# ── Cognito — DEDICATED app client, minting tenant_id as a first-class claim ──
# New pool lives in the DEPLOY region (eu-west-1).
export COGNITO_DISCOVERY_URL="https://cognito-idp.eu-west-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration"
export COGNITO_ALLOWED_CLIENTS="<CLIENT_ID_1>,<CLIENT_ID_2>"

# ── Gateway ───────────────────────────────────────────────────────────────────
export GATEWAY_NAME="fact-layer-gw"
export GATEWAY_ARN=""                        # filled after the gateway is created (for Cedar template)
export GATEWAY_IDENTIFIER=""                 # gateway id for `aws bedrock-agentcore-control update-gateway`

# Cedar policy engine — START IN LOG_ONLY (Gate C). Flip to ENFORCE at Gate D
# with deploy/set_cedar_mode.sh (no redeploy of Lambdas needed).
export CEDAR_MODE="LOG_ONLY"
export POLICY_ENGINE_NAME="fact-layer-cedar"
export POLICY_ENGINE_ARN=""                  # filled after the policy engine is created (for mode switch)

# ── Lambda ARNs (from the CDK stack outputs — see cdk/) ──────────────────────
export RECORD_FACT_ARN=""
export VECTOR_SEARCH_ARN=""
export QUERY_METRICS_ARN=""
export EXPLAIN_FACT_ARN=""
export GET_FACT_ARN=""
export GET_FACT_HISTORY_ARN=""
export LIST_DISPUTES_ARN=""
export SEARCH_ENTITIES_ARN=""
export RETRACT_FACT_ARN=""

# Request interceptor Lambda ARN (CDK output InterceptorLambdaArn). Wired to the
# Gateway via `update-gateway --interceptor-configurations` (NOT a hardcoded ARN).
export INTERCEPTOR_LAMBDA_ARN=""

# Preflight Lambda name (CDK output PreflightLambdaName) for smoke_tidb.sh.
export PREFLIGHT_LAMBDA_NAME="fact-Preflight"

# ── Embeddings (Bedrock, DEPLOY region) ──────────────────────────────────────
export EMBED_MODEL_ID="amazon.titan-embed-text-v2:0"
export EMBED_DIM="1024"
