#!/usr/bin/env bash
# Cognito prerequisite for the fact-layer AgentCore Gateway (Gate C).
#
# Creates a DEDICATED eu-west-1 user pool that mints a first-class `tenant_id`
# ID-token claim, per the accepted tenant-claim contract. This is SEPARATE from
# CDK (Gate B's FactLayerComputeStack stays frozen) and SEPARATE from the
# CustomerSupport pool in us-east-1 (never touched).
#
# It provisions, idempotently:
#   1. IAM role  fact-layer-cognito-pretoken-role   (trust: lambda.amazonaws.com)
#   2. Lambda    fact-cognito-pretoken              (pre-token-generation, V1)
#   3. User pool fact-layer-pool                    (custom attr `tenant_id`)
#   4. Invoke permission for cognito-idp + attach the trigger to the pool
#   5. App client fact-layer-gw-client             (NO client secret)
#   6. Three test identities for Cases A/B/C:
#        tenant-a-tester (custom:tenant_id=tenant-A)
#        tenant-b-tester (custom:tenant_id=tenant-B)
#        tenant-c-noclaim (NO custom:tenant_id -> token omits tenant_id)
#
# DRY-RUN BY DEFAULT. No AWS mutation happens unless DEPLOY_FOR_REAL=1. Read-only
# probes (sts / list / get) run in both modes so the plan reflects real state.
# It NEVER sets or prints passwords/secrets: test-user passwords are set by the
# operator out-of-band (the exact command is printed at the end).

set -euo pipefail
cd "$(dirname "$0")/.."
source "${ENV_FILE:-deploy/env.sh}"   # only for AWS_REGION

# ── guards + helpers ──────────────────────────────────────────────────────────
run() {
  if [ "${DEPLOY_FOR_REAL:-0}" = "1" ]; then echo "  [exec] $*" >&2; "$@";
  else echo "  [dry-run] $*" >&2; fi
}
need_cmd() { for c in "$@"; do command -v "$c" >/dev/null 2>&1 || { echo "missing tool: $c" >&2; exit 1; }; done; }
need_cmd aws jq zip python3

REGION="${AWS_REGION:-eu-west-1}"
# Hard safety rails: this script only ever operates on eu-west-1 fact-layer names.
[ "$REGION" = "eu-west-1" ] || { echo "refusing: REGION=$REGION (fact-layer Cognito is eu-west-1 only)" >&2; exit 1; }

POOL_NAME="fact-layer-pool"
CLIENT_NAME="fact-layer-gw-client"
TRIGGER_FN="fact-cognito-pretoken"
TRIGGER_ROLE="fact-layer-cognito-pretoken-role"
TENANT_A="tenant-A"; TENANT_B="tenant-B"
USER_A="tenant-a-tester"; USER_B="tenant-b-tester"; USER_C="tenant-c-noclaim"

# Never allow this script to name a CustomerSupport resource.
case "$POOL_NAME$CLIENT_NAME$TRIGGER_FN" in
  *CustomerSupport*) echo "refusing: name collides with CustomerSupport" >&2; exit 1 ;;
esac

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "== Cognito prerequisite (account=$ACCOUNT_ID region=$REGION) =="
echo "   mode: $([ "${DEPLOY_FOR_REAL:-0}" = 1 ] && echo 'DEPLOY_FOR_REAL (mutating)' || echo 'DRY-RUN (no mutations)')"

cog() { aws cognito-idp --region "$REGION" "$@"; }

# ── 1. Pre-token Lambda role (idempotent) ─────────────────────────────────────
echo "== 1. IAM role: $TRIGGER_ROLE =="
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${TRIGGER_ROLE}"
if aws iam get-role --role-name "$TRIGGER_ROLE" >/dev/null 2>&1; then
  echo "  role exists -> reuse ($ROLE_ARN)"
else
  run aws iam create-role --role-name "$TRIGGER_ROLE" \
    --assume-role-policy-document file://deploy/iam/cognito-pretoken-trust.json \
    --description "Fact-layer Cognito pre-token-generation trigger role"
fi
# Least privilege: basic execution (CloudWatch Logs) only.
run aws iam attach-role-policy --role-name "$TRIGGER_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# ── 2. Pre-token Lambda (idempotent) ──────────────────────────────────────────
echo "== 2. Lambda: $TRIGGER_FN (pre-token-generation V1) =="
ZIP="$(mktemp -u).zip"
( cd deploy/cognito && zip -j -q "$ZIP" pretoken.py )   # local packaging only
echo "  packaged deploy/cognito/pretoken.py -> $ZIP (handler pretoken.handler)"
TRIGGER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${TRIGGER_FN}"
if aws lambda get-function --function-name "$TRIGGER_FN" --region "$REGION" >/dev/null 2>&1; then
  echo "  function exists -> update code ($TRIGGER_ARN)"
  run aws lambda update-function-code --function-name "$TRIGGER_FN" --region "$REGION" \
    --zip-file "fileb://$ZIP"
else
  run aws lambda create-function --function-name "$TRIGGER_FN" --region "$REGION" \
    --runtime python3.12 --handler pretoken.handler --timeout 5 \
    --role "$ROLE_ARN" --zip-file "fileb://$ZIP" \
    --description "Emit bare tenant_id ID-token claim from custom:tenant_id"
fi

# ── 3. User pool (idempotent by name) ─────────────────────────────────────────
echo "== 3. User pool: $POOL_NAME (custom attr tenant_id) =="
POOL_ID="$(cog list-user-pools --max-results 60 --output json 2>/dev/null \
  | jq -r --arg n "$POOL_NAME" '.UserPools[]?|select(.Name==$n)|.Id' | head -n1)"
SCHEMA="$(mktemp)"
jq -n '[{Name:"tenant_id",AttributeDataType:"String",Mutable:true,Required:false,
         StringAttributeConstraints:{MinLength:"1",MaxLength:"64"}}]' > "$SCHEMA"
if [ -n "$POOL_ID" ]; then
  echo "  pool exists -> reuse (id=$POOL_ID)"
else
  echo "  schema -> custom:tenant_id (String, 1..64, mutable)"
  run cog create-user-pool --pool-name "$POOL_NAME" \
    --schema "file://$SCHEMA" \
    --admin-create-user-config AllowAdminCreateUserOnly=true \
    --username-configuration CaseSensitive=false \
    --mfa-configuration OFF
  POOL_ID="$(cog list-user-pools --max-results 60 --output json 2>/dev/null \
    | jq -r --arg n "$POOL_NAME" '.UserPools[]?|select(.Name==$n)|.Id' | head -n1)"
fi
POOL_ARN=""
[ -n "$POOL_ID" ] && POOL_ARN="arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}"

# ── 4. Permit cognito-idp to invoke the trigger, then attach it (V2_0) ─────────
# V2_0 = access-token claim customization for USER identities (our USER_PASSWORD
# flow). Requires the Essentials or Plus feature plan (our pool is ESSENTIALS).
# We set PreTokenGenerationConfig (V2_0) — NOT the legacy V1 PreTokenGeneration —
# so the trigger customizes the ACCESS token (the canonical Gateway bearer token).
echo "== 4. Wire pre-token trigger to the pool (V2_0 access-token customization) =="
if [ -n "$POOL_ID" ]; then
  # add-permission is idempotent-ish: ignore "already exists" on re-run.
  run aws lambda add-permission --function-name "$TRIGGER_FN" --region "$REGION" \
    --statement-id "cognito-${POOL_ID}" --action lambda:InvokeFunction \
    --principal cognito-idp.amazonaws.com --source-arn "$POOL_ARN"
  LAMBDA_CFG="$(mktemp)"
  jq -n --arg arn "$TRIGGER_ARN" \
    '{PreTokenGenerationConfig: {LambdaArn: $arn, LambdaVersion: "V2_0"}}' > "$LAMBDA_CFG"
  run cog update-user-pool --user-pool-id "$POOL_ID" \
    --lambda-config "file://$LAMBDA_CFG" \
    --admin-create-user-config AllowAdminCreateUserOnly=true \
    --mfa-configuration OFF
else
  echo "  (dry-run) pool not created yet; would add-permission (source=<pool-arn>) then"
  echo "  (dry-run) update-user-pool --lambda-config PreTokenGenerationConfig={LambdaArn=$TRIGGER_ARN,LambdaVersion=V2_0}"
fi

# ── 5. App client (NO secret) ─────────────────────────────────────────────────
echo "== 5. App client: $CLIENT_NAME (no secret, USER_PASSWORD_AUTH) =="
CLIENT_ID=""
if [ -n "$POOL_ID" ]; then
  CLIENT_ID="$(cog list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 --output json 2>/dev/null \
    | jq -r --arg n "$CLIENT_NAME" '.UserPoolClients[]?|select(.ClientName==$n)|.ClientId' | head -n1)"
fi
if [ -n "$CLIENT_ID" ]; then
  echo "  client exists -> reuse (id=$CLIENT_ID)"
elif [ -n "$POOL_ID" ]; then
  run cog create-user-pool-client --user-pool-id "$POOL_ID" \
    --client-name "$CLIENT_NAME" --no-generate-secret \
    --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --prevent-user-existence-errors ENABLED \
    --id-token-validity 60 --token-validity-units IdToken=minutes
  CLIENT_ID="$(cog list-user-pool-clients --user-pool-id "$POOL_ID" --max-results 60 --output json 2>/dev/null \
    | jq -r --arg n "$CLIENT_NAME" '.UserPoolClients[]?|select(.ClientName==$n)|.ClientId' | head -n1)"
else
  echo "  (dry-run) would create no-secret client with USER_PASSWORD_AUTH + REFRESH"
fi

# ── 6. Test identities for Cases A/B/C (attributes only; NO passwords set) ─────
echo "== 6. Test identities (Case A/B/C) — attributes only, no passwords =="
make_user() {  # <username> [tenant-value]
  local user="$1" tenant="${2:-}"
  if [ -n "$POOL_ID" ] && cog admin-get-user --user-pool-id "$POOL_ID" --username "$user" >/dev/null 2>&1; then
    echo "  user $user exists -> skip"; return 0
  fi
  if [ -n "$tenant" ]; then
    run cog admin-create-user --user-pool-id "${POOL_ID:-<POOL_ID>}" --username "$user" \
      --message-action SUPPRESS \
      --user-attributes "Name=custom:tenant_id,Value=$tenant"
  else
    # Case C: create WITHOUT custom:tenant_id so the token omits tenant_id.
    run cog admin-create-user --user-pool-id "${POOL_ID:-<POOL_ID>}" --username "$user" \
      --message-action SUPPRESS
  fi
}
make_user "$USER_A" "$TENANT_A"
make_user "$USER_B" "$TENANT_B"
make_user "$USER_C"            # no tenant attribute -> Case C

# ── Summary (non-secret) ──────────────────────────────────────────────────────
DISCOVERY="https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID:-<POOL_ID>}/.well-known/openid-configuration"
echo
echo "== Cognito prerequisite — planned/created (non-secret) =="
printf "  %-24s %s\n" "pool.name"      "$POOL_NAME"
printf "  %-24s %s\n" "pool.id"        "${POOL_ID:-<pending>}"
printf "  %-24s %s\n" "client.name"    "$CLIENT_NAME"
printf "  %-24s %s\n" "client.id"      "${CLIENT_ID:-<pending>}"
printf "  %-24s %s\n" "trigger.fn"     "$TRIGGER_FN"
printf "  %-24s %s\n" "trigger.role"   "$TRIGGER_ROLE"
printf "  %-24s %s\n" "discovery.url"  "$DISCOVERY"
echo
echo "After a REAL run, put these NON-SECRET values into deploy/env.sh (gitignored):"
echo "  export COGNITO_DISCOVERY_URL=\"$DISCOVERY\""
echo "  export COGNITO_ALLOWED_CLIENTS=\"${CLIENT_ID:-<CLIENT_ID>}\""
echo
echo "Then YOU set test-user passwords out-of-band (secrets — never via this script):"
echo "  aws cognito-idp admin-set-user-password --region $REGION --user-pool-id ${POOL_ID:-<POOL_ID>} \\"
echo "    --username $USER_A --password '<choose>' --permanent   # repeat for $USER_B; leave $USER_C as-is or set too"
echo
echo "Validate + verify (no ENFORCE): source deploy/env.sh && python3 deploy/validate_config.py"
