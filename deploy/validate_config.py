#!/usr/bin/env python3
"""Validate the operator deployment configuration WITHOUT touching AWS.

Reads config from the environment (source deploy/env.sh first) and runs the pure
checks in deploy/config.py. Prints a report and exits non-zero on any error, so
it can gate the deploy script (Gate A). Never prints secret material — it only
inspects ARNs/regions/shapes.

    source deploy/env.sh && python3 deploy/validate_config.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CONFIG_KEYS, validate_config  # noqa: E402


def main() -> int:
    cfg = {k: os.environ.get(k, "") for k in CONFIG_KEYS}
    report = validate_config(cfg)

    print("== fact-layer deployment config validation ==")
    print(f"  deploy region (AWS_REGION) : {cfg.get('AWS_REGION') or '(unset)'}")
    print(f"  TiDB region  (TIDB_REGION) : {cfg.get('TIDB_REGION') or '(unset)'}")
    print(f"  Bedrock region             : {cfg.get('BEDROCK_REGION') or '(unset)'}")
    print(f"  Cedar mode                 : {cfg.get('CEDAR_MODE') or 'LOG_ONLY'}")
    print()
    for m in report.info:
        print(f"  info  {m}")
    for m in report.warnings:
        print(f"  WARN  {m}")
    for m in report.errors:
        print(f"  ERROR {m}")
    print()
    if report.ok:
        print(f"OK: config valid ({len(report.warnings)} warning(s))")
        return 0
    print(f"FAIL: {len(report.errors)} error(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
