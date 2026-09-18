#!/usr/bin/env bash
# Operator-facing TiDB schema migration command (Gate B — TiDB schema).
#
# INDEPENDENT of AWS/CDK: this talks to TiDB (eu-central-1) over the MySQL wire
# protocol. It is NEVER called by deploy/deploy.sh or the CDK — you run it by hand.
#
# Default behaviour does NOT mutate TiDB. The real apply requires APPLY_FOR_REAL=1.
#
#   ./deploy/apply_migrations.sh --check                 # read-only: config + target + state
#   ./deploy/apply_migrations.sh --dry-run               # read-only: + pending migrations
#   ./deploy/apply_migrations.sh --verify-existing       # read-only: does the EXISTING DB
#                                                        #   already match sql/01,02,04-08?
#                                                        #   (never creates schema_migrations)
#   ./deploy/apply_migrations.sh --baseline-existing     # read-only: verify + show what a
#                                                        #   baseline WOULD record (no write)
#   APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh --baseline-existing   # WRITES: record the
#                                                        #   seven verified migrations as applied
#                                                        #   (only after verification passes)
#   APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh        # MUTATES: apply pending (core set)
#   APPLY_FOR_REAL=1 ./deploy/apply_migrations.sh --with-roles   # also apply 03_roles LAST
#
# Credentials come from Secrets Manager (MIGRATION_SECRET_ARN) or a local URL
# (TIDB_MIGRATION_URL / TIDB_TEST_URL). Never pass a password on the command line.

set -euo pipefail
cd "$(dirname "$0")/.."
# Load operator env if present (ARNs/URLs/role name — never secret material).
[ -f deploy/env.sh ] && source deploy/env.sh || true

PASSTHRU=()
MODE=""
for arg in "$@"; do
  case "$arg" in
    --check)             MODE="--check" ;;
    --dry-run)           MODE="--dry-run" ;;
    --verify-existing)   MODE="--verify-existing" ;;
    --baseline-existing) MODE="--baseline-existing" ;;
    --with-roles) PASSTHRU+=("--with-roles") ;;
    -h|--help)
      sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# ${arr[@]+"${arr[@]}"} expands to nothing on an empty array even under `set -u`
# in bash 3.2 (macOS default) — avoids passing a spurious empty argument.
if [ -n "$MODE" ]; then
  # Explicit read-only mode.
  exec python3 deploy/migrate.py "$MODE" ${PASSTHRU[@]+"${PASSTHRU[@]}"}
fi

if [ "${APPLY_FOR_REAL:-0}" = "1" ]; then
  echo "APPLY_FOR_REAL=1 — this WILL mutate TiDB."
  exec python3 deploy/migrate.py --apply ${PASSTHRU[@]+"${PASSTHRU[@]}"}
fi

# No mode + no APPLY_FOR_REAL -> safe preview, no mutation.
echo "No mode given and APPLY_FOR_REAL is not set -> showing a dry-run (no mutation)."
echo "To apply for real:  APPLY_FOR_REAL=1 $0 ${PASSTHRU[*]:-}"
echo
exec python3 deploy/migrate.py --dry-run ${PASSTHRU[@]+"${PASSTHRU[@]}"}
