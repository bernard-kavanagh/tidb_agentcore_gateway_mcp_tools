"""Offline tests for deploy/render_cedar_policies.py.

The splitter turns cedar/tenant_isolation.cedar into per-action AgentCore policy
objects for `create-policy`. These tests lock the two properties Gate C depends
on: (1) a 1:1, SEMANTICS-PRESERVING split (one policy per permit, no rule
rewritten), and (2) valid create-policy names/lengths + ARN substitution.

    python3 -m pytest tests/test_cedar_render.py
"""

from __future__ import annotations

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

import render_cedar_policies as rcp  # noqa: E402

_ARN = "arn:aws:bedrock-agentcore:eu-west-1:111122223333:gateway/fact-layer-gw-ABCDEFGHIJ"

# The nine tools the Gateway must expose — every one needs an isolation policy.
_EXPECTED_ACTIONS = {
    "RecordFact___record_fact",
    "VectorSearch___vector_search",
    "QueryTenantMetrics___query_tenant_metrics",
    "ExplainFact___explain_fact",
    "GetFact___get_fact",
    "GetFactHistory___get_fact_history",
    "ListDisputes___list_disputes",
    "SearchEntities___search_entities",
    "RetractFact___retract_fact",
}


def test_one_policy_per_tool():
    policies = rcp.render(_ARN)
    assert len(policies) == 9
    actions = set()
    for p in policies:
        m = re.search(r'AgentCore::Action::"([^"]+)"', p["statement"])
        assert m, f"no action in {p['name']}"
        actions.add(m.group(1))
    assert actions == _EXPECTED_ACTIONS


def test_names_valid_for_create_policy():
    for p in rcp.render(_ARN):
        assert re.match(r"^[A-Za-z][A-Za-z0-9_]*$", p["name"]), p["name"]
        assert len(p["name"]) <= 48


def test_statement_lengths_in_api_bounds():
    for p in rcp.render(_ARN):
        assert 35 <= len(p["statement"]) <= 10000


def test_arn_is_substituted_and_no_placeholder_leaks():
    for p in rcp.render(_ARN):
        assert "<GATEWAY_ARN>" not in p["statement"]
        assert _ARN in p["statement"]


def test_semantics_preserved_verbatim():
    # Every statement must carry the exact param-vs-claim isolation condition,
    # unaltered. If a rule were rewritten/weakened this check fails.
    cond = "context.input.tenant_id == principal.getTag(\"tenant_id\")"
    policies = rcp.render(_ARN)
    for p in policies:
        assert 'principal.hasTag("tenant_id")' in p["statement"]
        assert cond in p["statement"]
        assert p["statement"].startswith("permit(")
        assert p["statement"].rstrip().endswith("};")


def test_check_mode_without_arn_keeps_placeholder():
    # Used by deploy.sh dry-run when the gateway ARN is not yet known.
    policies = rcp.render(None)
    assert len(policies) == 9
    assert all("<GATEWAY_ARN>" in p["statement"] for p in policies)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
