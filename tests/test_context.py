"""Offline tests for the reworked handler context (real MCP target contract).

Covers: tenant_id read from the tool argument; fail-closed on missing/empty;
legacy _fact_ctx can never override the argument; explicit credential modes
(shared vs per-tenant, no silent fallback); the optional direct-invocation guard;
a static tenant-scoping audit of all nine handlers; and that mutation safeguards
remain intact.

    python3 -m pytest tests/test_context.py
"""

from __future__ import annotations

import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.common import context  # noqa: E402

_SHARED = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:fact-layer/tidb/runtime-XyZ012"
_PER_A = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:tenant-a-AbC123"


def _env(monkey: dict):
    """Apply env overrides; return a restore callable."""
    prev = {k: os.environ.get(k) for k in monkey}
    for k, v in monkey.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    def restore():
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


# ── tenant from tool argument (shared mode is the current design) ─────────────
def test_valid_event_tenant_a_reaches_context():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        ctx, args = context.extract({"tenant_id": "tenant-A", "subject": "s", "predicate": "status"})
        assert ctx.tenant_id == "tenant-A"
        assert ctx.tenant_secret_arn == _SHARED
        assert ctx.credential_mode == "shared"
        assert args["subject"] == "s"
        # provenance is not available at an MCP target — non-null markers, tool_turn None
        assert ctx.agent_id == "unknown" and ctx.session_id == "unknown"
        assert ctx.tool_turn is None
    finally:
        r()


def test_missing_tenant_fails_closed():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        for ev in ({"subject": "s"}, {}):
            try:
                context.extract(ev)
            except PermissionError as e:
                assert "tenant_id" in str(e)
            else:
                raise AssertionError("missing tenant_id must fail closed")
    finally:
        r()


def test_empty_or_malformed_tenant_fails_closed():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        for bad in ("", "   ", "\t", None):
            try:
                context.extract({"tenant_id": bad})
            except PermissionError:
                pass
            else:
                raise AssertionError(f"tenant_id={bad!r} must fail closed")
    finally:
        r()


def test_legacy_fact_ctx_cannot_override_argument():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        # A stale/forged _fact_ctx claiming a different tenant must be ignored;
        # the authoritative selector is the tool argument.
        ev = {"tenant_id": "tenant-A", "subject": "s",
              "_fact_ctx": {"tenant_id": "tenant-EVIL", "tenant_secret_arn": "arn:aws:secretsmanager:x:y:secret:evil"}}
        ctx, args = context.extract(ev)
        assert ctx.tenant_id == "tenant-A", "tool argument must win over legacy _fact_ctx"
        assert ctx.tenant_secret_arn == _SHARED
        assert "_fact_ctx" not in args
    finally:
        r()


# ── credential model: explicit modes, no silent fallback ──────────────────────
def test_per_tenant_mode_resolves_mapped_arn():
    r = _env({"TENANT_SECRET_MAP": f'{{"tenant-A":"{_PER_A}"}}', "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        ctx, _ = context.extract({"tenant_id": "tenant-A"})
        assert ctx.credential_mode == "per_tenant"
        assert ctx.tenant_secret_arn == _PER_A
    finally:
        r()


def test_per_tenant_unknown_tenant_no_silent_shared_fallback():
    r = _env({"TENANT_SECRET_MAP": f'{{"tenant-A":"{_PER_A}"}}', "FACT_LAYER_SECRET_ARN": _SHARED})
    try:
        try:
            context.extract({"tenant_id": "tenant-B"})  # not in the map
        except PermissionError as e:
            assert "no per-tenant DB credential" in str(e)
        else:
            raise AssertionError("unknown tenant must NOT silently use the shared credential")
    finally:
        r()


def test_no_credential_configured_fails_closed():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": None})
    try:
        try:
            context.extract({"tenant_id": "tenant-A"})
        except ValueError as e:
            assert "no DB credential configured" in str(e)
        else:
            raise AssertionError("no credential configured must fail closed")
    finally:
        r()


# ── direct/bypass invocation guard (explicit + tested) ────────────────────────
class _Ctx:
    def __init__(self, custom): self.client_context = type("cc", (), {"custom": custom})


def test_bypass_guard_off_by_default_allows():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED, "FACT_REQUIRE_GATEWAY_CONTEXT": None})
    try:
        ctx, _ = context.extract({"tenant_id": "tenant-A"}, lambda_context=None)
        assert ctx.tenant_id == "tenant-A"  # default relies on the IAM boundary (documented)
    finally:
        r()


def test_bypass_guard_on_blocks_direct_invoke():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED, "FACT_REQUIRE_GATEWAY_CONTEXT": "1"})
    try:
        try:
            context.extract({"tenant_id": "tenant-A"}, lambda_context=None)  # no gateway marker
        except PermissionError as e:
            assert "did not arrive via the AgentCore Gateway" in str(e)
        else:
            raise AssertionError("guard ON must block a direct (non-Gateway) invoke")
    finally:
        r()


def test_bypass_guard_on_allows_gateway_invocation():
    r = _env({"TENANT_SECRET_MAP": "{}", "FACT_LAYER_SECRET_ARN": _SHARED, "FACT_REQUIRE_GATEWAY_CONTEXT": "1"})
    try:
        gw = _Ctx({"bedrockAgentCoreGatewayId": "fact-layer-gw-example001",
                   "bedrockAgentCoreToolName": "GetFact___get_fact"})
        ctx, _ = context.extract({"tenant_id": "tenant-A"}, lambda_context=gw)
        assert ctx.tenant_id == "tenant-A"
    finally:
        r()


# ── static audit: every handler scopes its DB access by the tenant argument ───
_TENANT_TOOLS = ["get_fact", "get_fact_history", "list_disputes", "search_entities",
                 "vector_search", "query_tenant_metrics", "explain_fact",
                 "retract_fact", "record_fact"]
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_all_handlers_extract_and_scope_by_tenant():
    for name in _TENANT_TOOLS:
        src = open(os.path.join(_ROOT, "handlers", f"{name}.py")).read()
        assert "context.extract(event" in src, f"{name}: must use context.extract"
        assert re.search(r"tenant_id\s*=\s*:t", src), f"{name}: must bind a tenant_id=:t predicate"
        assert re.search(r'"t":\s*(t|tenant|ctx\.tenant_id)', src), f"{name}: must bind :t to ctx.tenant_id"


def test_no_handler_reads_tenant_from_legacy_side_channel():
    for name in _TENANT_TOOLS:
        src = open(os.path.join(_ROOT, "handlers", f"{name}.py")).read()
        assert "_fact_ctx" not in src, f"{name}: must not reference the retired _fact_ctx channel"


# ── mutation safeguards remain intact (unchanged modules) ─────────────────────
def test_write_control_floor_still_enforced():
    from handlers.common.write_control import enforce_write_control_floor
    for bad in (None,):
        try:
            enforce_write_control_floor(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("missing confidence must be rejected by the write-control floor")


def test_normalizers_still_reject_invalid():
    from handlers.common.entity import normalize_relationship, normalize_assessor_type
    for fn, bad in ((normalize_relationship, "NOPE"), (normalize_assessor_type, "alien")):
        try:
            fn(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} must reject invalid input")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
