#!/usr/bin/env bash
# Gate D — flip the Cedar policy engine between LOG_ONLY and ENFORCE WITHOUT
# recreating the gateway or redeploying Lambdas.
#
#   bash deploy/set_cedar_mode.sh ENFORCE     # after Gate C auth tests pass
#   bash deploy/set_cedar_mode.sh LOG_ONLY    # roll back to shadow mode
#
# SAFETY: update-gateway is a FULL REPLACE. This script does a read-modify-write:
#   1. get-gateway (the complete current definition),
#   2. preserve name/role/authorizer/protocol/interceptor AND the current policy
#      engine ARN verbatim,
#   3. change ONLY policyEngineConfiguration.mode,
#   4. re-send the complete definition via update-gateway.
# It never constructs a partial definition, so it cannot drop the authorizer,
# allowed clients, interceptor, role, protocol, or policy engine.
#
# DRY-RUN BY DEFAULT. Set DEPLOY_FOR_REAL=1 to apply. Rollback = re-run LOG_ONLY.

set -euo pipefail
cd "$(dirname "$0")/.."
source "${ENV_FILE:-deploy/env.sh}"
source deploy/gateway_common.sh

need_cmd aws jq

MODE="${1:-}"
case "$MODE" in
  LOG_ONLY|ENFORCE) ;;
  *) echo "usage: $0 <LOG_ONLY|ENFORCE>"; exit 2 ;;
esac

require AWS_REGION
require GATEWAY_NAME

# Resolve the gateway id from its name (or use GATEWAY_IDENTIFIER if pinned).
GW_ID="${GATEWAY_IDENTIFIER:-}"
[ -n "$GW_ID" ] || GW_ID="$(gateway_id_by_name || true)"
[ -n "$GW_ID" ] || { echo "gateway '$GATEWAY_NAME' not found in $AWS_REGION — deploy it first" >&2; exit 1; }

# Show what is attached now (read-only) so the change is auditable.
cur_mode="$(gateway_field "$GW_ID" .policyEngineConfiguration.mode || true)"
cur_pe_arn="$(gateway_field "$GW_ID" .policyEngineConfiguration.arn || true)"
echo "== Cedar mode change on gateway '$GATEWAY_NAME' (id=$GW_ID) =="
echo "  current: mode=${cur_mode:-<none>} engine=${cur_pe_arn:-<none-attached>}"
echo "  target : mode=$MODE"
[ -n "$cur_pe_arn" ] || { echo "no policy engine attached yet — run deploy/deploy.sh first" >&2; exit 1; }

if [ "$MODE" = "ENFORCE" ]; then
  echo "  !! ENFORCE will DENY cross-tenant / missing-claim requests at the Gateway."
  echo "     Confirm Gate C authorization tests passed (bash deploy/smoke_gateway.sh)."
fi
if [ "$MODE" = "$cur_mode" ]; then
  echo "  already in $MODE — nothing to do."; exit 0
fi

# Preserve the attached engine ARN; change only the mode.
gateway_full_update "$GW_ID" "$cur_pe_arn" "$MODE"
echo "  policy engine mode -> $MODE"
