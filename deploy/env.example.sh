# Operator-supplied environment inputs. Copy to deploy/env.sh and fill in.
# NONE of these are guessed — they come from the operator (see SPEC "Environment inputs").
# Only ARNs/IDs/URLs live here; NO secret material.

# One region for cluster, secret, Lambdas, and Gateway (reference cluster is eu-central-1).
export AWS_REGION="eu-central-1"

# TiDB Cloud (Dedicated/Premium — HNSW vector index is tier-gated).
export TIDB_DB="fact_layer"                 # schema/database name used in 03_roles.sql grants

# Secrets Manager ARN of the scoped TiDB connection secret (same region).
# Secret JSON keys: host, port, username, password, database, [ssl_ca]
export FACT_LAYER_SECRET_ARN="arn:aws:secretsmanager:eu-central-1:<ACCT>:secret:<NAME>"

# Optional per-tenant credential swapping (interceptor). JSON: {"tenantA":"arn:...","tenantB":"arn:..."}
export TENANT_SECRET_MAP='{}'

# Cognito — DEDICATED app client for the fact layer, minting tenant_id as a first-class claim.
export COGNITO_DISCOVERY_URL="https://cognito-idp.eu-central-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration"
export COGNITO_ALLOWED_CLIENTS="<CLIENT_ID_1>,<CLIENT_ID_2>"

# Gateway.
export GATEWAY_NAME="fact-layer-gw"
export GATEWAY_ARN=""                        # filled in after the gateway is created

# Lambda ARNs (filled in after packaging/deploying the handler functions).
export RECORD_FACT_ARN=""
export VECTOR_SEARCH_ARN=""
export QUERY_METRICS_ARN=""

# Request interceptor Lambda ARN (gateway/interceptor.py, deployed as its own function).
# Runs before Cedar; injects identity + tenant-scoped credential. Required by deploy.sh.
export INTERCEPTOR_LAMBDA_ARN=""

# Embeddings (Bedrock). Titan Text Embeddings V2 @ 1024 — matches VECTOR(1024).
export EMBED_MODEL_ID="amazon.titan-embed-text-v2:0"
export EMBED_DIM="1024"
