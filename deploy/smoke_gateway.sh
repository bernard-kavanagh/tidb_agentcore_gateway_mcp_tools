#!/usr/bin/env bash
# Gate C smoke test — exercise the full request path through the AgentCore
# Gateway and classify WHICH layer answers, so a failure is unambiguous:
#
#   AUTH      — JWT rejected by the Gateway CUSTOM_JWT authorizer (401/403)
#   CEDAR     — authorization denial (ENFORCE) / logged decision (LOG_ONLY)
#   HANDLER   — Lambda defense-in-depth rejected a tenant mismatch
#   LAMBDA    — tool executed but errored
#   TIDB      — reached the DB but connectivity/query failed
#   ALLOWED   — 200 with a tool result
#
# It proves deploy-confirm A (context.input.tenant_id populated) and the
# interceptor/Cedar ordering (see DEPLOYMENT.md "Interceptor / Cedar ordering").
#
# This test needs REAL tokens + endpoint (nothing hardcoded). Provide via env:
#   GATEWAY_MCP_URL   the gateway's MCP endpoint URL
#   JWT_TENANT_A      a valid Cognito JWT whose tenant_id claim = TENANT_A_ID
#   JWT_TENANT_B      a valid Cognito JWT whose tenant_id claim = TENANT_B_ID
#   TENANT_A_ID       e.g. "acme"     TENANT_B_ID e.g. "globex"
# If any are missing this prints guidance and exits 2 (it is a guide, not a gate).

set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy/env.sh ] && source deploy/env.sh || true

missing=0
for v in GATEWAY_MCP_URL JWT_TENANT_A JWT_TENANT_B TENANT_A_ID TENANT_B_ID; do
  [ -n "${!v:-}" ] || { echo "  (need env: $v)"; missing=1; }
done
if [ "$missing" = "1" ]; then
  cat <<'EOF'

Provide real values, e.g.:
  export GATEWAY_MCP_URL="https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp"
  export TENANT_A_ID=acme  TENANT_B_ID=globex
  export JWT_TENANT_A="$(aws cognito-idp initiate-auth ... | jq -r .AuthenticationResult.IdToken)"
  export JWT_TENANT_B="..."
Then re-run: bash deploy/smoke_gateway.sh
EOF
  exit 2
fi

# One MCP tools/call over JSON-RPC 2.0. NOTE: confirm the exact MCP envelope the
# installed Gateway expects (agentcore/docs) — kept simple and explicit here.
call() { # <jwt> <tenant_arg> -> prints "HTTP_STATUS\n<body>"
  local jwt="$1" tenant="$2"
  curl -sS -o /tmp/smoke_body.$$ -w '%{http_code}' \
    -X POST "$GATEWAY_MCP_URL" \
    -H "Authorization: Bearer $jwt" \
    -H 'Content-Type: application/json' \
    --data @- <<JSON
{ "jsonrpc":"2.0","id":1,"method":"tools/call",
  "params":{ "name":"GetFact___get_fact",
             "arguments":{ "tenant_id":"$tenant","subject":"smoke:preflight","predicate":"status" } } }
JSON
  echo
  cat /tmp/smoke_body.$$; rm -f /tmp/smoke_body.$$
}

classify() { # <http_status> <body>
python3 - "$@" <<'PY'
import sys
status, body = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
b = body.lower()
if status in ("401", "403") and "cedar" not in b and "tenant" not in b:
    print("AUTH", "JWT rejected by Gateway authorizer"); raise SystemExit
if "cedar" in b or "not authorized" in b or "authorizationdecision" in b or "forbidden" in b:
    print("CEDAR", "authorization denial"); raise SystemExit
if "does not match verified claim" in b or "verified tenant" in b or "permissionerror" in b:
    print("HANDLER", "tenant mismatch caught by Lambda defense-in-depth"); raise SystemExit
if "operationalerror" in b or "pymysql" in b or "can't connect" in b or "tls" in b:
    print("TIDB", "reached DB but connectivity/query failed"); raise SystemExit
if status == "200" and '"error"' not in b:
    print("ALLOWED", "200 tool result"); raise SystemExit
print("LAMBDA", f"tool error (status {status})")
PY
}

scenario() { # <label> <jwt> <tenant_arg> <expectation>
  echo "── $1 (expect: $4)"
  local resp status body
  resp="$(call "$2" "$3")"; status="${resp%%$'\n'*}"; body="${resp#*$'\n'}"
  echo "   -> $(classify "$status" "$body")"
}

echo "== Gate C Gateway smoke (Cedar mode: ${CEDAR_MODE:-unknown}) =="
scenario "1. tenant A -> A's data" "$JWT_TENANT_A" "$TENANT_A_ID" "ALLOWED"
scenario "2. tenant B -> B's data" "$JWT_TENANT_B" "$TENANT_B_ID" "ALLOWED"
scenario "3. tenant A -> B's data (cross-tenant)" "$JWT_TENANT_A" "$TENANT_B_ID" \
         "CEDAR in ENFORCE / HANDLER in LOG_ONLY — never ALLOWED"
scenario "4. missing tenant claim" "$JWT_TENANT_A" "" "AUTH or CEDAR/HANDLER — never ALLOWED"

cat <<'EOF'

Deploy-confirm checks:
  A. context.input.tenant_id populated -> scenarios 1/2 return ALLOWED (Cedar saw the arg).
  B. interceptor credential -> a tenant-scoped TiDB session -> scenarios 1/2 return real data.
Denied-request-never-reaches-TiDB:
  For scenario 3 (and 4), confirm in CloudWatch that the tool Lambda's log group shows
  NO record_fact/get_fact TiDB query for that correlation id. In LOG_ONLY the Cedar
  DECISION is logged (shadow) while the handler blocks the data — inspect the policy log.
EOF
