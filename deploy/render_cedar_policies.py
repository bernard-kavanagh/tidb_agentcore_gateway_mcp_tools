#!/usr/bin/env python3
"""Render cedar/tenant_isolation.cedar into AgentCore Policy objects.

AgentCore `create-policy` takes ONE Cedar statement per policy object
(`--definition cedar={statement=...}`), so the 9-permit tenant-isolation file is
deployed as 9 policy objects — a 1:1 decomposition of the permits already in the
file, with ZERO semantic change (no rule is rewritten, merged, or weakened). This
is the "minimum required policy objects" split the Gate C brief calls for.

What it does, deterministically and offline:
  * substitutes the concrete gateway ARN for the `<GATEWAY_ARN>` placeholder,
  * splits the file into individual `permit(...)/forbid(...)` statements,
  * derives a stable policy name per statement from its Cedar action
    (`<TargetName>___<tool>` -> `TenantIsolation_<TargetName>`), and
  * emits `[{"name","statement"}, ...]` as JSON (or writes .cedar files).

It never invents rules: every statement is a verbatim slice of the tracked file.

Usage:
    python3 deploy/render_cedar_policies.py --gateway-arn <arn>        # JSON to stdout
    python3 deploy/render_cedar_policies.py --gateway-arn <arn> --out-dir build/policies
    python3 deploy/render_cedar_policies.py --check                    # validate split only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CEDAR_FILE = os.path.join(_ROOT, "cedar", "tenant_isolation.cedar")

# create-policy statement length constraints (from the control-plane API).
_MIN_STATEMENT = 35
_MAX_STATEMENT = 10000
# create-policy name pattern: [A-Za-z][A-Za-z0-9_]* , max 48.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ACTION_RE = re.compile(r'AgentCore::Action::"([A-Za-z0-9_]+)___')


def _strip_line_comments(text: str) -> str:
    """Drop // line comments so they cannot interfere with statement splitting.

    Cedar has no string literal that legitimately contains `//` in this policy
    set, so line-comment stripping is safe and keeps the split unambiguous.
    """
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        out.append(line[:idx] if idx != -1 else line)
    return "\n".join(out)


def split_statements(text: str) -> list[str]:
    """Split a Cedar policy set into individual permit/forbid statements.

    Statements are terminated by `};`. Each returned statement is a verbatim
    (comment-stripped, whitespace-normalized) slice of the source — never
    rewritten.
    """
    body = _strip_line_comments(text)
    chunks = [c.strip() for c in body.split("};")]
    statements: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if "permit(" not in chunk and "forbid(" not in chunk:
            continue
        statements.append(chunk + "\n};")
    return statements


def _name_for(statement: str, index: int) -> str:
    m = _ACTION_RE.search(statement)
    name = f"TenantIsolation_{m.group(1)}" if m else f"TenantIsolation_p{index}"
    name = name[:48]
    if not _NAME_RE.match(name):
        raise ValueError(f"derived policy name is invalid: {name!r}")
    return name


def render(gateway_arn: str | None) -> list[dict]:
    with open(_CEDAR_FILE, encoding="utf-8") as f:
        text = f.read()

    if gateway_arn:
        text = text.replace("<GATEWAY_ARN>", gateway_arn)

    statements = split_statements(text)
    if not statements:
        raise ValueError("no permit/forbid statements found in the Cedar file")

    policies: list[dict] = []
    seen: set[str] = set()
    for i, stmt in enumerate(statements):
        name = _name_for(stmt, i)
        if name in seen:
            name = f"{name}_{i}"[:48]
        seen.add(name)

        if not (_MIN_STATEMENT <= len(stmt) <= _MAX_STATEMENT):
            raise ValueError(
                f"{name}: statement length {len(stmt)} outside "
                f"[{_MIN_STATEMENT},{_MAX_STATEMENT}]"
            )
        policies.append({"name": name, "statement": stmt})
    return policies


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gateway-arn", default=os.environ.get("GATEWAY_ARN") or None)
    ap.add_argument("--out-dir", help="write <name>.cedar files here instead of JSON")
    ap.add_argument("--check", action="store_true",
                    help="validate the split without requiring a gateway ARN")
    args = ap.parse_args(argv)

    if not args.check and not args.gateway_arn:
        print("ERROR: --gateway-arn is required (or set GATEWAY_ARN, or use --check)",
              file=sys.stderr)
        return 2

    policies = render(args.gateway_arn)

    if args.check:
        unresolved = sum("<GATEWAY_ARN>" in p["statement"] for p in policies)
        print(f"OK: {len(policies)} policy object(s) from {os.path.relpath(_CEDAR_FILE, _ROOT)}")
        for p in policies:
            print(f"  - {p['name']}  ({len(p['statement'])} chars)")
        if unresolved:
            print(f"  note: {unresolved} still contain <GATEWAY_ARN> (no ARN supplied)")
        return 0

    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        for p in policies:
            path = os.path.join(args.out_dir, f"{p['name']}.cedar")
            with open(path, "w", encoding="utf-8") as f:
                f.write(p["statement"] + "\n")
        print(f"wrote {len(policies)} policy file(s) to {args.out_dir}", file=sys.stderr)
        return 0

    json.dump(policies, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
