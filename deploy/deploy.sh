#!/usr/bin/env bash
# Gate C — wire the AgentCore Gateway in front of the already-deployed Lambdas.
#
# PRE-REQUISITE (Gate B): the Lambda/IAM tier is deployed via CDK (see cdk/) and
# its output ARNs are in deploy/env.sh. This script does NOT create Lambdas, IAM,
# secrets, Cognito, or the TiDB cluster — only the Gateway, its 9 targets, the
# request-interceptor wiring, and the Cedar policy engine (LOG_ONLY first).
#
# DRY-RUN BY DEFAULT: prints every mutating command with a [dry-run] prefix.
# Set DEPLOY_FOR_REAL=1 only after you have verified the CLI flags below against
# the installed binaries:
#     agentcore --help ; agentcore add gateway --help ; agentcore add gateway-target --help
#     agentcore add policy-engine --help ; agentcore add policy --help
#     aws bedrock-agentcore-control update-gateway help
#
# Ordering mirrors the dependency chain and the runtime enforcement order
# (JWT -> interceptor -> Cedar -> tool): gateway, then targets, then interceptor,
# then the Cedar engine attached in LOG_ONLY.

set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/env.sh

# Dry-run guard.
run() {
  if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then "$@"; else echo "  [dry-run] $*"; fi
}
require() { [ -n "${!1:-}" ] || { echo "missing env: $1"; exit 1; }; }

# Fail fast on config shape (Gate A) before any mutation.
echo "== 0. Validate config (no AWS calls) =="
python3 deploy/validate_config.py || { echo "config invalid — fix deploy/env.sh"; exit 1; }

require AWS_REGION
require FACT_LAYER_SECRET_ARN
require COGNITO_DISCOVERY_URL
require COGNITO_ALLOWED_CLIENTS
require GATEWAY_NAME
require INTERCEPTOR_LAMBDA_ARN
require POLICY_ENGINE_NAME
for v in RECORD_FACT_ARN VECTOR_SEARCH_ARN QUERY_METRICS_ARN EXPLAIN_FACT_ARN \
         GET_FACT_ARN GET_FACT_HISTORY_ARN LIST_DISPUTES_ARN SEARCH_ENTITIES_ARN \
         RETRACT_FACT_ARN; do require "$v"; done
CEDAR_MODE="${CEDAR_MODE:-LOG_ONLY}"

echo "== 1. Create the fact layer's own Gateway (MCP protocol, JWT inbound) =="
# CORRECTED: MCP gateways require --protocol-type MCP (default is None).
run agentcore add gateway \
  --name "$GATEWAY_NAME" \
  --protocol-type MCP \
  --authorizer-type CUSTOM_JWT \
  --discovery-url "$COGNITO_DISCOVERY_URL" \
  --allowed-clients "$COGNITO_ALLOWED_CLIENTS"

echo "== 2. Register each handler as a Lambda target (target name -> Cedar action) =="
add_target() { # <TargetName> <lambda-arn> <schema-file>
  run agentcore add gateway-target --type lambda-function-arn --host Lambda \
    --name "$1" --lambda-arn "$2" --tool-schema-file "$3" --gateway "$GATEWAY_NAME"
}
add_target RecordFact         "$RECORD_FACT_ARN"      gateway/schemas/record_fact.schema.json
add_target VectorSearch       "$VECTOR_SEARCH_ARN"    gateway/schemas/vector_search.schema.json
add_target QueryTenantMetrics "$QUERY_METRICS_ARN"    gateway/schemas/query_tenant_metrics.schema.json
add_target ExplainFact        "$EXPLAIN_FACT_ARN"     gateway/schemas/explain_fact.schema.json
add_target GetFact            "$GET_FACT_ARN"         gateway/schemas/get_fact.schema.json
add_target GetFactHistory     "$GET_FACT_HISTORY_ARN" gateway/schemas/get_fact_history.schema.json
add_target ListDisputes       "$LIST_DISPUTES_ARN"    gateway/schemas/list_disputes.schema.json
add_target SearchEntities     "$SEARCH_ENTITIES_ARN"  gateway/schemas/search_entities.schema.json
add_target RetractFact        "$RETRACT_FACT_ARN"     gateway/schemas/retract_fact.schema.json

echo "== 3. Attach the request interceptor (runs BEFORE Cedar) =="
# CORRECTED: `agentcore update gateway --request-interceptor-arn` does NOT exist.
# The real mechanism is the control-plane update-gateway with
# --interceptor-configurations. The ARN is injected from env (never hardcoded).
require GATEWAY_IDENTIFIER
INTERCEPTOR_CFG="$(mktemp)"
cat > "$INTERCEPTOR_CFG" <<JSON
[
  {
    "interceptor": { "lambda": { "arn": "${INTERCEPTOR_LAMBDA_ARN}" } },
    "interceptionPoints": ["REQUEST"]
  }
]
JSON
echo "   interceptor config -> $INTERCEPTOR_CFG (arn from \$INTERCEPTOR_LAMBDA_ARN)"
# update-gateway is a full update: re-supply the required identity/authorizer
# fields alongside the interceptor config.
run aws bedrock-agentcore-control update-gateway \
  --region "$AWS_REGION" \
  --gateway-identifier "$GATEWAY_IDENTIFIER" \
  --name "$GATEWAY_NAME" \
  --authorizer-type CUSTOM_JWT \
  --interceptor-configurations "file://$INTERCEPTOR_CFG"

echo "== 4. Cedar policy engine — START IN ${CEDAR_MODE} (Gate C uses LOG_ONLY) =="
# CORRECTED: no `agentcore add policy --gateway ... --mode ...`. Create a
# policy-engine attached to the gateway, then add the policy to that engine.
run agentcore add policy-engine \
  --name "$POLICY_ENGINE_NAME" \
  --attach-to-gateways "$GATEWAY_NAME" \
  --attach-mode "$CEDAR_MODE"

# Render the gateway ARN into a TEMP copy of the tracked template (never sed -i
# the git-tracked file; BSD/GNU sed -i differ).
require GATEWAY_ARN
RENDERED_POLICY="$(mktemp)"
sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" cedar/tenant_isolation.cedar > "$RENDERED_POLICY"
echo "   rendered policy -> $RENDERED_POLICY"
run agentcore add policy \
  --engine "$POLICY_ENGINE_NAME" \
  --source "$RENDERED_POLICY"

echo
echo "Next:"
echo "  - Gate C tests: bash deploy/smoke_gateway.sh   (JWT/interceptor/Cedar/Lambda/TiDB layers)"
echo "  - Gate C TiDB : bash deploy/smoke_tidb.sh       (invokes fact-Preflight)"
echo "  - Gate D (only after Gate C passes): bash deploy/set_cedar_mode.sh ENFORCE"
echo
echo "Post-deploy gates (see DEPLOYMENT.md):"
echo "  - deploy-confirm A: context.input.tenant_id populates for record_fact"
echo "  - deploy-confirm B: interceptor credential opens the tenant-scoped TiDB session"
