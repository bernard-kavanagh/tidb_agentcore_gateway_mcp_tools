"""Offline evaluation of the tenant-isolation permits (deploy-confirm A).

This models the EXACT `when` conditions in tenant_isolation.cedar and traces
MATCH / no-match per condition for mock requests, so the isolation rule can be
validated before any infra exists. It is a faithful logical simulation of these
specific permits (deny-by-default, only permits present -> default deny), not the
Cedar engine itself. The authoritative check remains the deployed ENFORCE engine
with the deny visible in the CloudWatch policy log.

Run:  python3 cedar/policy_eval.py
"""

from __future__ import annotations

import sys

ACTIONS = {
    "RecordFact___record_fact",
    "VectorSearch___vector_search",
    "QueryTenantMetrics___query_tenant_metrics",
}


def evaluate(request: dict) -> tuple[str, list[str]]:
    """Return (decision, trace). decision is 'Allow' or 'Deny'.

    A request dict looks like:
      {
        "action": "RecordFact___record_fact",
        "principal_tags": {"tenant_id": "acme"},   # from the JWT claim
        "context": {"input": {"tenant_id": "acme", ...}},
      }
    """
    trace: list[str] = []

    # No permit covers an unknown action -> default deny.
    if request.get("action") not in ACTIONS:
        trace.append(f"no permit for action {request.get('action')!r} -> default deny")
        return "Deny", trace

    tags = request.get("principal_tags") or {}
    ctx = request.get("context") or {}
    conds = []

    c1 = "tenant_id" in tags
    conds.append(c1)
    trace.append(f"principal.hasTag(\"tenant_id\"): {'MATCH' if c1 else 'NO'}")

    c2 = "input" in ctx
    conds.append(c2)
    trace.append(f"context has input: {'MATCH' if c2 else 'NO'}")

    has_input_tenant = c2 and "tenant_id" in (ctx.get("input") or {})
    conds.append(has_input_tenant)
    trace.append(f"context.input has tenant_id: {'MATCH' if has_input_tenant else 'NO'}")

    if c1 and has_input_tenant:
        c4 = ctx["input"]["tenant_id"] == tags["tenant_id"]
        trace.append(
            f"context.input.tenant_id ({ctx['input']['tenant_id']!r}) == "
            f"principal.getTag (\"{tags['tenant_id']}\"): {'MATCH' if c4 else 'NO'}"
        )
    else:
        c4 = False
        trace.append("context.input.tenant_id == principal.getTag: SKIPPED (prereq failed)")
    conds.append(c4)

    decision = "Allow" if all(conds) else "Deny"
    trace.append(f"=> permit {'satisfied -> Allow' if decision == 'Allow' else 'not satisfied -> default Deny'}")
    return decision, trace


# --- mock requests covering acceptance tests 5 & 6 ---
CASES = [
    ("matching tenant -> Allow", "Allow", {
        "action": "RecordFact___record_fact",
        "principal_tags": {"tenant_id": "acme"},
        "context": {"input": {"tenant_id": "acme", "subject": "s", "predicate": "status"}},
    }),
    ("cross-tenant argument -> Deny (isolation)", "Deny", {
        "action": "RecordFact___record_fact",
        "principal_tags": {"tenant_id": "acme"},
        "context": {"input": {"tenant_id": "globex", "subject": "s", "predicate": "status"}},
    }),
    ("missing tenant tag on principal -> Deny", "Deny", {
        "action": "RecordFact___record_fact",
        "principal_tags": {},
        "context": {"input": {"tenant_id": "acme"}},
    }),
    ("context.input.tenant_id absent -> Deny (deploy-confirm A)", "Deny", {
        "action": "RecordFact___record_fact",
        "principal_tags": {"tenant_id": "acme"},
        "context": {"input": {"subject": "s"}},
    }),
    ("vector_search matching tenant -> Allow", "Allow", {
        "action": "VectorSearch___vector_search",
        "principal_tags": {"tenant_id": "acme"},
        "context": {"input": {"tenant_id": "acme", "query_text": "q"}},
    }),
    ("unknown action -> Deny (deny-by-default)", "Deny", {
        "action": "SomethingElse___do_it",
        "principal_tags": {"tenant_id": "acme"},
        "context": {"input": {"tenant_id": "acme"}},
    }),
]


if __name__ == "__main__":
    failures = 0
    for name, expected, req in CASES:
        decision, trace = evaluate(req)
        ok = decision == expected
        failures += 0 if ok else 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: got {decision}, expected {expected}")
        for line in trace:
            print(f"        - {line}")
        print()
    print(f"{len(CASES) - failures}/{len(CASES)} cases passed")
    sys.exit(1 if failures else 0)
