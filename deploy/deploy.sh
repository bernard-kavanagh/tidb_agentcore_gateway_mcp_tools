#!/usr/bin/env bash
# Deploy the fact layer as Gateway + Lambda targets (Phase A).
#
# PREREQUISITE — verify CLI syntax before running. AgentCore CLI flags evolve;
# the command shapes below follow SPEC.md but MUST be checked against the
# installed CLI first:
#     agentcore --help
#     agentcore add gateway --help
#     agentcore add gateway-target --help
# Then remove the `echo` guard at the bottom and run for real.
#
# Order matters and mirrors the dependency chain: schema/roles/secret must exist,
# and the Lambdas must be packaged & deployed (their ARNs in env.sh) before the
# Gateway targets can reference them.

set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/env.sh

# Dry-run guard: prints commands instead of executing until the operator has
# verified CLI flags and sets DEPLOY_FOR_REAL=1.
run() {
  if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then "$@"; else echo "  [dry-run] $*"; fi
}

require() { [ -n "${!1:-}" ] || { echo "missing env: $1"; exit 1; }; }
require AWS_REGION
require FACT_LAYER_SECRET_ARN
require COGNITO_DISCOVERY_URL
require COGNITO_ALLOWED_CLIENTS
require RECORD_FACT_ARN
require VECTOR_SEARCH_ARN
require QUERY_METRICS_ARN
require INTERCEPTOR_LAMBDA_ARN

echo "== 1. Create the fact layer's own Gateway (JWT inbound, dedicated Cognito client) =="
run agentcore add gateway \
  --name "$GATEWAY_NAME" \
  --authorizer-type CUSTOM_JWT \
  --discovery-url "$COGNITO_DISCOVERY_URL" \
  --allowed-clients "$COGNITO_ALLOWED_CLIENTS"

echo "== 2. Register each handler as a Lambda target (target names drive Cedar action names) =="
run agentcore add gateway-target --type lambda-function-arn --name RecordFact \
  --lambda-arn "$RECORD_FACT_ARN" --tool-schema-file gateway/schemas/record_fact.schema.json \
  --gateway "$GATEWAY_NAME"
run agentcore add gateway-target --type lambda-function-arn --name VectorSearch \
  --lambda-arn "$VECTOR_SEARCH_ARN" --tool-schema-file gateway/schemas/vector_search.schema.json \
  --gateway "$GATEWAY_NAME"
run agentcore add gateway-target --type lambda-function-arn --name QueryTenantMetrics \
  --lambda-arn "$QUERY_METRICS_ARN" --tool-schema-file gateway/schemas/query_tenant_metrics.schema.json \
  --gateway "$GATEWAY_NAME"

echo "== 3. Attach the request interceptor (runs before Cedar) =="
# Verify the exact flag name for interceptor attachment against `agentcore --help`.
run agentcore update gateway --name "$GATEWAY_NAME" \
  --request-interceptor-arn "$INTERCEPTOR_LAMBDA_ARN"

echo "== 4. Cedar policy engine in ENFORCE (deny-by-default, forbid-overrides-permit) =="
# Render the gateway ARN into a TEMP copy and pass that to agentcore. Keep
# cedar/tenant_isolation.cedar as a clean template in git — never sed -i the
# tracked file (also, -i syntax differs between BSD/macOS and GNU sed).
RENDERED_POLICY="$(mktemp)"
sed "s|<GATEWAY_ARN>|$GATEWAY_ARN|g" cedar/tenant_isolation.cedar > "$RENDERED_POLICY"
echo "   rendered policy -> $RENDERED_POLICY"
run agentcore add policy --gateway "$GATEWAY_NAME" \
  --policy-file "$RENDERED_POLICY" --mode ENFORCE

echo "== 5. Deploy =="
run agentcore deploy

echo
echo "Post-deploy gates (see README 'Acceptance tests'):"
echo "  - deploy-confirm A: context.input.tenant_id populates for record_fact"
echo "  - deploy-confirm B: interceptor credential opens the tenant-scoped TiDB session"
