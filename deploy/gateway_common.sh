#!/usr/bin/env bash
# Shared helpers for the Gate C raw AgentCore control-plane workflow.
# Sourced by deploy/deploy.sh and deploy/set_cedar_mode.sh. No mutations happen
# on source — only function definitions. Read probes are allowed (they never
# mutate); mutations go through run() and are skipped unless DEPLOY_FOR_REAL=1.

# ── dry-run + guards ─────────────────────────────────────────────────────────
run() {  # execute a MUTATING command, or print it prefixed in dry-run
  if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then
    echo "  [exec] $*" >&2
    "$@"
  else
    echo "  [dry-run] $*" >&2
  fi
}

require() { [ -n "${!1:-}" ] || { echo "missing env: $1" >&2; exit 1; }; }

need_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || { echo "missing required tool: $c" >&2; exit 1; }
  done
}

# ── control-plane wrapper (read-only unless wrapped in run) ───────────────────
ac() { aws bedrock-agentcore-control --region "$AWS_REGION" "$@"; }

# ── gateway lookups (read-only) ──────────────────────────────────────────────
# Echo the gatewayId for GATEWAY_NAME, or empty string if it does not exist.
gateway_id_by_name() {
  ac list-gateways --output json 2>/dev/null \
    | jq -r --arg n "$GATEWAY_NAME" '.items[]? | select(.name==$n) | .gatewayId' \
    | head -n1
}

# Echo a single top-level field from get-gateway for a given id.
gateway_field() {  # <gateway-id> <jq-path e.g. .gatewayArn>
  ac get-gateway --gateway-identifier "$1" --output json 2>/dev/null | jq -r "$2 // empty"
}

# Echo the policyEngineId for POLICY_ENGINE_NAME, or empty string.
policy_engine_id_by_name() {
  ac list-policy-engines --output json 2>/dev/null \
    | jq -r --arg n "$POLICY_ENGINE_NAME" '.policyEngines[]? | select(.name==$n) | .policyEngineId' \
    | head -n1
}

# ── safe full-replace update-gateway ─────────────────────────────────────────
# update-gateway is a FULL REPLACE: read the current definition and re-send every
# field, changing ONLY the policy-engine-configuration (arn + mode). Never build a
# partial definition — that would silently drop the authorizer/interceptor/role.
#
#   gateway_full_update <gateway-id> <policy-engine-arn> <LOG_ONLY|ENFORCE>
#
# If <policy-engine-arn> is empty, the current gateway's policy-engine arn is
# preserved (used by set_cedar_mode.sh, which only flips the mode).
gateway_full_update() {
  local gw_id="$1" pe_arn="$2" mode="$3"
  local def name role auth_type auth_cfg proto_type proto_cfg interceptors cur_pe_arn

  def="$(ac get-gateway --gateway-identifier "$gw_id" --output json)" \
    || { echo "get-gateway failed for $gw_id" >&2; return 1; }

  name="$(jq -r '.name' <<<"$def")"
  role="$(jq -r '.roleArn' <<<"$def")"
  auth_type="$(jq -r '.authorizerType' <<<"$def")"
  auth_cfg="$(jq -c '.authorizerConfiguration' <<<"$def")"
  proto_type="$(jq -r '.protocolType // empty' <<<"$def")"
  proto_cfg="$(jq -c '.protocolConfiguration // empty' <<<"$def")"
  interceptors="$(jq -c '.interceptorConfigurations // empty' <<<"$def")"
  cur_pe_arn="$(jq -r '.policyEngineConfiguration.arn // empty' <<<"$def")"

  [ -n "$pe_arn" ] || pe_arn="$cur_pe_arn"
  [ -n "$pe_arn" ] || { echo "no policy-engine ARN to attach/preserve" >&2; return 1; }

  # Fail closed if the read-back is missing anything a full replace must preserve.
  for pair in "name:$name" "roleArn:$role" "authorizerType:$auth_type" \
              "authorizerConfiguration:$auth_cfg"; do
    [ -n "${pair#*:}" ] && [ "${pair#*:}" != "null" ] || {
      echo "refusing full update: current gateway is missing ${pair%%:*}" >&2; return 1; }
  done

  local pe_cfg; pe_cfg="$(jq -nc --arg a "$pe_arn" --arg m "$mode" '{arn:$a,mode:$m}')"

  local args=( update-gateway
    --gateway-identifier "$gw_id"
    --name "$name"
    --role-arn "$role"
    --authorizer-type "$auth_type"
    --authorizer-configuration "$auth_cfg"
    --policy-engine-configuration "$pe_cfg" )
  [ -n "$proto_type" ] && args+=( --protocol-type "$proto_type" )
  [ -n "$proto_cfg" ] && [ "$proto_cfg" != "null" ] && args+=( --protocol-configuration "$proto_cfg" )
  [ -n "$interceptors" ] && [ "$interceptors" != "null" ] && args+=( --interceptor-configurations "$interceptors" )

  echo "  full-replace update-gateway: preserving name/role/authorizer/protocol/interceptor;" >&2
  echo "  setting policyEngine arn=${pe_arn##*/} mode=$mode" >&2
  run ac "${args[@]}"
}
