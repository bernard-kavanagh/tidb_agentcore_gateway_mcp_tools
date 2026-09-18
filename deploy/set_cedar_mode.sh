#!/usr/bin/env bash
# Gate D — flip the Cedar policy engine between LOG_ONLY and ENFORCE WITHOUT
# redeploying Lambdas or recreating the gateway. Operator-controlled, dry-run by
# default (DEPLOY_FOR_REAL=1 to apply).
#
#   bash deploy/set_cedar_mode.sh ENFORCE     # after Gate C tests pass
#   bash deploy/set_cedar_mode.sh LOG_ONLY    # roll back to shadow mode
#
# Mechanism: the policy engine's enforcement mode is the `mode` field of the
# gateway's policyEngineConfiguration, updated via the control-plane
# update-gateway. No `agentcore add policy --mode` (that flag does not exist).

set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/env.sh

MODE="${1:-}"
case "$MODE" in
  LOG_ONLY|ENFORCE) ;;
  *) echo "usage: $0 <LOG_ONLY|ENFORCE>"; exit 2 ;;
esac

require() { [ -n "${!1:-}" ] || { echo "missing env: $1"; exit 1; }; }
require AWS_REGION
require GATEWAY_IDENTIFIER
require GATEWAY_NAME
require POLICY_ENGINE_ARN

run() { if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then "$@"; else echo "  [dry-run] $*"; fi }

if [ "$MODE" = "ENFORCE" ]; then
  echo "!! Switching Cedar to ENFORCE — cross-tenant/invalid requests will be DENIED."
  echo "   Confirm Gate C smoke tests passed first (bash deploy/smoke_gateway.sh)."
fi

POLICY_CFG="$(mktemp)"
cat > "$POLICY_CFG" <<JSON
{ "arn": "${POLICY_ENGINE_ARN}", "mode": "${MODE}" }
JSON

run aws bedrock-agentcore-control update-gateway \
  --region "$AWS_REGION" \
  --gateway-identifier "$GATEWAY_IDENTIFIER" \
  --name "$GATEWAY_NAME" \
  --authorizer-type CUSTOM_JWT \
  --policy-engine-configuration "file://$POLICY_CFG"

echo "  policy engine mode -> $MODE"
