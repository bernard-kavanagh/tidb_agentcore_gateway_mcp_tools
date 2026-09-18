"""Offline tests for the request interceptor + handler context extraction.

Covers tenant-isolation defense-in-depth at the code layer (independent of the
Gateway/Cedar): the interceptor extracts the VERIFIED tenant claim and the
handler context refuses a caller-supplied tenant that disagrees with it.

    python3 tests/test_interceptor.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Interceptor reads env at import; set a default secret ARN before importing it.
os.environ.setdefault("FACT_LAYER_SECRET_ARN",
                      "arn:aws:secretsmanager:eu-central-1:111122223333:secret:tidb-XyZ012")

from gateway import interceptor  # noqa: E402
from handlers.common import context  # noqa: E402


def _jwt_event(claims: dict) -> dict:
    """An event shaped like the Gateway authorizer's verified-claims envelope."""
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


# --------------------------------------------------------------------------
# interceptor: verified tenant extraction
# --------------------------------------------------------------------------
def test_interceptor_extracts_verified_tenant():
    out = interceptor.handler(_jwt_event({"tenant_id": "acme", "sub": "agent-7"}))
    ctx = out["_fact_ctx"]
    assert ctx["tenant_id"] == "acme", "tenant_id must come from the verified claim"
    assert ctx["agent_id"] == "agent-7", "agent_id from sub"
    assert ctx.get("tenant_secret_arn"), "a secret ARN must be injected"


def test_interceptor_missing_tenant_fails_closed():
    out = interceptor.handler(_jwt_event({"sub": "agent-7"}))  # no tenant_id
    assert out["_fact_ctx"] == {}, "no enrichment without a verified tenant"
    assert "missing tenant_id" in out.get("_error", ""), "must signal the missing claim"


def test_interceptor_per_tenant_secret_mapping(monkey=None):
    # A per-tenant ARN overrides the default when present in TENANT_SECRET_MAP.
    prev = dict(interceptor._TENANT_SECRET_MAP)
    try:
        interceptor._TENANT_SECRET_MAP = {
            "acme": "arn:aws:secretsmanager:eu-central-1:111122223333:secret:acme-Abc123"
        }
        assert interceptor._secret_arn_for("acme").endswith("acme-Abc123")
        # Unknown tenant falls back to the default scoped ARN (not None).
        assert interceptor._secret_arn_for("globex") == interceptor._DEFAULT_SECRET_ARN
    finally:
        interceptor._TENANT_SECRET_MAP = prev


# --------------------------------------------------------------------------
# handler context: defense-in-depth tenant match
# --------------------------------------------------------------------------
def _ctx_event(tenant_claim: str, tool_args: dict) -> dict:
    """Event as a handler sees it: interceptor context + tool args at top level."""
    ev = dict(tool_args)
    ev["_fact_ctx"] = {
        "tenant_id": tenant_claim,
        "agent_id": "a", "session_id": "s",
        "tenant_secret_arn": "arn:aws:secretsmanager:eu-central-1:111122223333:secret:tidb-XyZ012",
    }
    return ev


def test_context_allows_matching_tenant():
    ctx, args = context.extract(_ctx_event("acme", {"tenant_id": "acme", "subject": "x"}))
    assert ctx.tenant_id == "acme"
    assert args["subject"] == "x"


def test_context_rejects_tenant_mismatch():
    # tool arg tenant_id != verified claim -> fail closed (never trust the arg).
    try:
        context.extract(_ctx_event("acme", {"tenant_id": "globex", "subject": "x"}))
    except PermissionError as e:
        assert "does not match verified claim" in str(e)
    else:
        raise AssertionError("cross-tenant argument must raise PermissionError")


def test_context_missing_identity_fails_closed():
    try:
        context.extract({"tenant_id": "acme", "subject": "x"})  # no _fact_ctx
    except PermissionError as e:
        assert "no verified tenant_id" in str(e)
    else:
        raise AssertionError("absent interceptor identity must raise PermissionError")


def test_context_uses_injected_tenant_not_arg():
    # When the arg omits tenant_id, the verified injected claim is authoritative.
    ctx, _ = context.extract(_ctx_event("acme", {"subject": "x"}))
    assert ctx.tenant_id == "acme", "handler must use the injected verified tenant"


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
