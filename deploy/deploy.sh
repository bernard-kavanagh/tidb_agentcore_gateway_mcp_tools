#!/usr/bin/env bash
# Gate C — put an AgentCore Gateway in front of the nine governed fact-layer
# tools, using the RAW control-plane API (aws bedrock-agentcore-control ...).
# It does NOT create Lambdas, IAM tool roles, secrets, Cognito, or TiDB — those
# are Gate B. It creates ONLY:
#   1. the least-privilege Gateway invocation role (fact-layer-gateway-invoke),
#   2. the Gateway (MCP, CUSTOM_JWT, interceptor),
#   3. the nine Lambda tool targets,
#   4. the Cedar policy engine + the nine tenant-isolation policies,
#   5. the LOG_ONLY attachment of the engine to the Gateway (full-replace update).
#
# Runtime enforcement order it wires up: JWT -> interceptor -> Cedar -> tool.
#
# DRY-RUN BY DEFAULT. Every mutation is printed with a [dry-run] prefix. Set
# DEPLOY_FOR_REAL=1 to actually mutate AWS. Read-only probes (get/list/sts) run
# in both modes so the plan reflects real state; they never mutate.
#
# Idempotent where practical: existing role/gateway/targets/engine/policies are
# detected by name and reused instead of recreated. Fail-fast (set -e). No secret
# material is ever printed — only ARNs/IDs/URLs.
#
# Prereqs verified against the installed CLI (aws 2.35, agentcore 0.28.1):
#   aws bedrock-agentcore-control create-gateway | create-gateway-target
#     | create-policy-engine | create-policy | update-gateway | get-gateway
#     | list-gateways | list-gateway-targets | list-policy-engines | list-policies

set -euo pipefail
cd "$(dirname "$0")/.."
source "${ENV_FILE:-deploy/env.sh}"
source deploy/gateway_common.sh

need_cmd aws jq python3

# ── 0. Config shape (no AWS calls) ────────────────────────────────────────────
echo "== 0. Validate config (no AWS calls) =="
python3 deploy/validate_config.py || { echo "config invalid — fix deploy/env.sh" >&2; exit 1; }

require AWS_REGION
require COGNITO_DISCOVERY_URL
require COGNITO_ALLOWED_CLIENTS
require GATEWAY_NAME
require POLICY_ENGINE_NAME
require INTERCEPTOR_LAMBDA_ARN
for v in RECORD_FACT_ARN VECTOR_SEARCH_ARN QUERY_METRICS_ARN EXPLAIN_FACT_ARN \
         GET_FACT_ARN GET_FACT_HISTORY_ARN LIST_DISPUTES_ARN SEARCH_ENTITIES_ARN \
         RETRACT_FACT_ARN; do require "$v"; done
CEDAR_MODE="${CEDAR_MODE:-LOG_ONLY}"
[ "$CEDAR_MODE" = "LOG_ONLY" ] || { echo "deploy.sh only ever attaches LOG_ONLY; use set_cedar_mode.sh for ENFORCE" >&2; exit 1; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "  account=$ACCOUNT_ID region=$AWS_REGION"
echo "  mode: $([ "${DEPLOY_FOR_REAL:-0}" = 1 ] && echo 'DEPLOY_FOR_REAL (mutating)' || echo 'DRY-RUN (no mutations)')"

# ── 1. Gateway invocation role (least privilege, idempotent) ──────────────────
echo "== 1. Gateway invocation role: fact-layer-gateway-invoke =="
ROLE_NAME="fact-layer-gateway-invoke"
GATEWAY_ROLE_ARN="${GATEWAY_ROLE_ARN:-arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}}"

# Least-privilege invoke policy: exactly the 9 tool Lambdas + the interceptor,
# by full ARN (and :* to cover version/alias qualifiers). No wildcards, no other
# services — no Secrets Manager, TiDB, Bedrock, or arbitrary lambda:*.
INVOKE_POLICY="$(mktemp)"
jq -n --arg r "$RECORD_FACT_ARN" --arg v "$VECTOR_SEARCH_ARN" --arg q "$QUERY_METRICS_ARN" \
      --arg e "$EXPLAIN_FACT_ARN" --arg g "$GET_FACT_ARN" --arg h "$GET_FACT_HISTORY_ARN" \
      --arg l "$LIST_DISPUTES_ARN" --arg s "$SEARCH_ENTITIES_ARN" --arg rt "$RETRACT_FACT_ARN" \
      --arg i "$INTERCEPTOR_LAMBDA_ARN" '
  ($ARGS.named | to_entries | map(.value) | map(., . + ":*")) as $arns
  | { Version:"2012-10-17",
      Statement:[ { Sid:"InvokeFactLayerTools", Effect:"Allow",
                    Action:"lambda:InvokeFunction", Resource:($arns|unique) } ] }' \
  > "$INVOKE_POLICY"
echo "  invoke policy -> $INVOKE_POLICY (Resource = 9 tools + interceptor only)"

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo "  role exists -> reuse ($GATEWAY_ROLE_ARN)"
else
  run aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document file://deploy/iam/gateway-invoke-trust.json \
    --description "AgentCore fact-layer Gateway invoke role (Gate C)"
fi
run aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "invoke-fact-layer-tools" \
  --policy-document "file://$INVOKE_POLICY"

# ── 2. Gateway (MCP, CUSTOM_JWT, interceptor) — idempotent by name ────────────
echo "== 2. Gateway: $GATEWAY_NAME (MCP / CUSTOM_JWT / interceptor) =="
AUTH_CFG="$(mktemp)"
jq -n --arg url "$COGNITO_DISCOVERY_URL" \
      --arg clients "$COGNITO_ALLOWED_CLIENTS" '
  { customJWTAuthorizer: { discoveryUrl:$url,
      allowedClients: ($clients | split(",") | map(gsub("^\\s+|\\s+$";""))) } }' \
  > "$AUTH_CFG"

INTERCEPTOR_CFG="$(mktemp)"
jq -n --arg arn "$INTERCEPTOR_LAMBDA_ARN" '
  [ { interceptor: { lambda: { arn:$arn } }, interceptionPoints:["REQUEST"] } ]' \
  > "$INTERCEPTOR_CFG"
echo "  authorizer -> customJWTAuthorizer(discoveryUrl, allowedClients)"
echo "  interceptor -> $INTERCEPTOR_LAMBDA_ARN @ REQUEST"

GW_ID="$(gateway_id_by_name || true)"
if [ -n "$GW_ID" ]; then
  echo "  gateway exists -> reuse (id=$GW_ID)"
else
  run ac create-gateway \
    --name "$GATEWAY_NAME" \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type CUSTOM_JWT \
    --authorizer-configuration "file://$AUTH_CFG" \
    --interceptor-configurations "file://$INTERCEPTOR_CFG"
  GW_ID="$(gateway_id_by_name || true)"
fi

if [ -z "$GW_ID" ]; then
  # Dry-run: the gateway was not actually created, so its ARN is unknown. Show the
  # rest of the plan with placeholders and stop before policy rendering.
  GW_ARN="<GATEWAY_ARN-known-after-create>"
  GW_URL="<GATEWAY_MCP_URL-known-after-create>"
  echo "  (dry-run) gateway not created yet; ARN/URL resolved on real deploy"
else
  GW_ARN="$(gateway_field "$GW_ID" .gatewayArn)"
  GW_URL="$(gateway_field "$GW_ID" .gatewayUrl)"
  echo "  gatewayId=$GW_ID"
  echo "  gatewayArn=$GW_ARN"
  echo "  gatewayUrl=$GW_URL"
fi

# ── 3. Nine Lambda tool targets — idempotent by name ──────────────────────────
echo "== 3. Register nine Lambda tool targets =="
CRED_PROVIDERS='[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]'
existing_targets=""
if [ -n "$GW_ID" ] && [[ "$GW_ID" != \<* ]]; then
  existing_targets="$(ac list-gateway-targets --gateway-identifier "$GW_ID" --output json 2>/dev/null \
    | jq -r '.items[]?.name' || true)"
fi

add_target() {  # <TargetName> <lambda-arn> <schema-file>
  local name="$1" arn="$2" schema="$3"
  if grep -qx "$name" <<<"$existing_targets"; then
    echo "  target $name exists -> skip"; return 0
  fi
  local tc; tc="$(mktemp)"
  # target-configuration.mcp.lambda.toolSchema.inlinePayload = [ <schema object> ]
  jq -n --arg arn "$arn" --slurpfile schema "$schema" '
    { mcp: { lambda: { lambdaArn:$arn, toolSchema: { inlinePayload: $schema } } } }' > "$tc"
  run ac create-gateway-target \
    --gateway-identifier "${GW_ID:-<GATEWAY_ID>}" \
    --name "$name" \
    --target-configuration "file://$tc" \
    --credential-provider-configurations "$CRED_PROVIDERS"
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

# ── 4. Cedar policy engine — idempotent by name ───────────────────────────────
echo "== 4. Cedar policy engine: $POLICY_ENGINE_NAME =="
PE_ID="$(policy_engine_id_by_name || true)"
if [ -n "$PE_ID" ]; then
  echo "  policy engine exists -> reuse (id=$PE_ID)"
else
  run ac create-policy-engine --name "$POLICY_ENGINE_NAME" \
    --description "Fact-layer tenant isolation (Cedar) — Gate C"
  PE_ID="$(policy_engine_id_by_name || true)"
fi
PE_ARN=""
if [ -n "$PE_ID" ]; then
  PE_ARN="$(ac list-policy-engines --output json 2>/dev/null \
    | jq -r --arg n "$POLICY_ENGINE_NAME" '.policyEngines[]?|select(.name==$n)|.policyEngineArn' | head -n1)"
  echo "  policyEngineId=$PE_ID"
  echo "  policyEngineArn=$PE_ARN"
fi

# ── 5. Nine tenant-isolation policies (rendered with the real gateway ARN) ─────
echo "== 5. Deploy the nine tenant-isolation Cedar policies (verbatim from file) =="
# create-policy takes ONE Cedar statement each; render_cedar_policies.py splits
# cedar/tenant_isolation.cedar into 9 objects with ZERO semantic change and
# substitutes the concrete gateway ARN. Never rewrites/weakens the policy.
if [[ "$GW_ARN" == \<* ]]; then
  echo "  (dry-run) gateway ARN unknown; showing planned policies with placeholder ARN"
  python3 deploy/render_cedar_policies.py --check
else
  existing_policies=""
  if [ -n "$PE_ID" ] && [[ "$PE_ID" != \<* ]]; then
    existing_policies="$(ac list-policies --policy-engine-id "$PE_ID" --output json 2>/dev/null \
      | jq -r '.policies[]?.name' || true)"
  fi
  POLICIES_JSON="$(python3 deploy/render_cedar_policies.py --gateway-arn "$GW_ARN")"
  n="$(jq 'length' <<<"$POLICIES_JSON")"
  for idx in $(seq 0 $((n-1))); do
    pname="$(jq -r ".[$idx].name" <<<"$POLICIES_JSON")"
    pstmt="$(jq -r ".[$idx].statement" <<<"$POLICIES_JSON")"
    if grep -qx "$pname" <<<"$existing_policies"; then
      echo "  policy $pname exists -> skip"; continue
    fi
    def="$(mktemp)"; jq -n --arg s "$pstmt" '{cedar:{statement:$s}}' > "$def"
    run ac create-policy --name "$pname" --policy-engine-id "$PE_ID" \
      --definition "file://$def"
  done
fi

# ── 5b. Role reconciliation: scoped policy-engine authorization perms ─────────
# The Gateway execution role needs GetPolicyEngine + AuthorizeAction +
# PartiallyAuthorizeActions to attach AND use the Cedar engine (required even in
# LOG_ONLY; without them update-gateway fails and all calls default-deny).
# Installed as a SEPARATE inline policy so the Lambda-invoke policy is never
# rewritten, and scoped to the EXACT resolved engine + gateway ARNs (no wildcards,
# no bedrock:InvokeModel, no Secrets Manager, no workload-token).
echo "== 5b. Reconcile Gateway role policy-engine permissions (scoped) =="
if [ -n "$GW_ID" ] && [[ "$GW_ID" != \<* ]] && [ -n "$PE_ARN" ] && [ -n "$GW_ARN" ] && [[ "$GW_ARN" != \<* ]]; then
  PE_AUTHZ_POLICY="$(mktemp)"
  jq -n --arg pe "$PE_ARN" --arg gw "$GW_ARN" '
    { Version:"2012-10-17",
      Statement:[
        { Sid:"PolicyEngineGetConfig", Effect:"Allow",
          Action:["bedrock-agentcore:GetPolicyEngine"], Resource:[$pe] },
        { Sid:"PolicyEngineAuthorize", Effect:"Allow",
          Action:["bedrock-agentcore:AuthorizeAction","bedrock-agentcore:PartiallyAuthorizeActions"],
          Resource:[$pe, $gw] }
      ] }' > "$PE_AUTHZ_POLICY"
  echo "  policy-engine authz -> engine=$PE_ARN"
  echo "                         gateway=$GW_ARN (exact ARNs; no wildcards)"
  run aws iam put-role-policy --role-name "$ROLE_NAME" \
    --policy-name "policy-engine-authorization" \
    --policy-document "file://$PE_AUTHZ_POLICY"
else
  echo "  (dry-run) would install scoped GetPolicyEngine + AuthorizeAction + PartiallyAuthorizeActions"
  echo "  (dry-run) on role $ROLE_NAME (engine + gateway ARNs resolved at real deploy)"
fi

# ── 6. Attach the engine to the gateway in LOG_ONLY (full-replace update) ──────
echo "== 6. Attach policy engine in LOG_ONLY (full-replace update-gateway) =="
if [ -z "$GW_ID" ] || [[ "$GW_ID" == \<* ]] || [ -z "$PE_ARN" ]; then
  echo "  (dry-run) would full-replace update-gateway to attach policyEngine=<arn> mode=LOG_ONLY"
  echo "  (dry-run) preserving name/role/authorizer/protocol/interceptor via get-gateway read-modify-write"
else
  gateway_full_update "$GW_ID" "$PE_ARN" "LOG_ONLY"
fi

# ── Summary (non-secret IDs/ARNs) ─────────────────────────────────────────────
echo
echo "== Gate C planned/created resources (non-secret) =="
printf "  %-22s %s\n" "gateway.name"        "$GATEWAY_NAME"
printf "  %-22s %s\n" "gateway.id"          "${GW_ID:-<pending>}"
printf "  %-22s %s\n" "gateway.arn"         "${GW_ARN:-<pending>}"
printf "  %-22s %s\n" "gateway.mcpUrl"      "${GW_URL:-<pending>}"
printf "  %-22s %s\n" "gateway.role"        "$GATEWAY_ROLE_ARN"
printf "  %-22s %s\n" "policyEngine.name"   "$POLICY_ENGINE_NAME"
printf "  %-22s %s\n" "policyEngine.id"     "${PE_ID:-<pending>}"
printf "  %-22s %s\n" "policyEngine.arn"    "${PE_ARN:-<pending>}"
printf "  %-22s %s\n" "cedar.mode"          "LOG_ONLY"
echo
echo "After a REAL deploy, record these back into deploy/env.sh:"
echo "  GATEWAY_IDENTIFIER, GATEWAY_ARN, GATEWAY_URL, GATEWAY_ROLE_ARN, POLICY_ENGINE_ARN"
echo
echo "Next:"
echo "  - Authorization-path tests (LOG_ONLY): bash deploy/smoke_gateway.sh"
echo "  - Cross-region TiDB reachability:       bash deploy/smoke_tidb.sh"
echo "  - Flip to ENFORCE (Gate D, after approval): bash deploy/set_cedar_mode.sh ENFORCE"
