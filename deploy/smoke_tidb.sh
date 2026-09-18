#!/usr/bin/env bash
# Smoke test: prove the eu-west-1 Lambda can reach the eu-central-1 TiDB cluster
# over public TLS. Invokes the deployed fact-Preflight Lambda (non-destructive:
# DNS -> TCP -> TLS -> auth -> SELECT 1) and prints which layer, if any, failed.
#
# Read-only against TiDB. Requires the CDK compute tier (Gate B) deployed.
#
#   source deploy/env.sh && bash deploy/smoke_tidb.sh

set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/env.sh

FN="${PREFLIGHT_LAMBDA_NAME:-fact-Preflight}"
REGION="${AWS_REGION:-eu-west-1}"
OUT="$(mktemp)"

echo "Invoking $FN in $REGION (TiDB region: ${TIDB_REGION:-eu-central-1}) ..."
aws lambda invoke \
  --function-name "$FN" \
  --region "$REGION" \
  --cli-binary-format raw-in-base64-out \
  --payload '{}' \
  "$OUT" >/dev/null

echo "--- preflight result ---"
python3 - "$OUT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
ok = r.get("ok")
print(f"ok            : {ok}")
print(f"stages_passed : {r.get('stages_passed')}")
print(f"tidb_region   : {r.get('tidb_region')}   secret_region: {r.get('secret_region')}")
print(f"host:port     : {r.get('host')}:{r.get('port')}")
if r.get("server"):
    s = r["server"]
    print(f"server        : version={s.get('version')} user={s.get('current_user')} "
          f"host={s.get('server_hostname')} db={s.get('db')}")
if not ok:
    print(f"FAILED_STAGE  : {r.get('failed_stage')}")
    print(f"error         : {r.get('error') or r.get('error_detail')}")
    # Classify for the operator.
    stage = r.get("failed_stage")
    hint = {
        "config": "secret ARN missing/malformed or FACT_LAYER_SECRET_ARN unset",
        "dns":    "TiDB hostname does not resolve — check the secret's host value",
        "tcp":    "TCP blocked — Lambda has no route to the public TiDB endpoint",
        "tls":    "TLS handshake failed — check endpoint/port and CA",
        "auth":   "credentials rejected — check secret username/password/role grants",
        "query":  "connected+authed but read failed — check DB name/grants",
    }.get(stage, "see error above")
    print(f"hint          : {hint}")
    sys.exit(1)
print("PASS: cross-region Lambda -> TiDB connectivity verified (non-destructive).")
PY
