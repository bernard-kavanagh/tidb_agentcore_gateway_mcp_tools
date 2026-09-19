"""Offline tests for the Cognito pre-token-generation trigger (V2 access token).

Locks the issuance contract: source `custom:tenant_id` -> bare top-level
`tenant_id` on the ACCESS token (and ID token) via the V2_0
`claimsAndScopeOverrideDetails`; absent/empty source -> tenant_id OMITTED on both
tokens (never defaulted or synthesized). Covers Cases A (tenant-A) and C (absent),
plus the V1 fallback shape.

    python3 -m pytest tests/test_pretoken.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "deploy", "cognito"))

import pretoken  # noqa: E402


def _v2_event(user_attrs: dict, scopes=None) -> dict:
    return {
        "version": "2",
        "triggerSource": "TokenGeneration_Authentication",
        "region": "eu-west-1",
        "userPoolId": "eu-west-1_TEST",
        "userName": "tester",
        "callerContext": {"clientId": "client123"},
        "request": {"userAttributes": user_attrs,
                    "scopes": scopes if scopes is not None else ["aws.cognito.signin.user.admin"],
                    "groupConfiguration": {}},
        "response": {},
    }


def _v1_event(user_attrs: dict) -> dict:
    return {"triggerSource": "TokenGeneration_Authentication",
            "request": {"userAttributes": user_attrs, "groupConfiguration": {}},
            "response": {}}


def _access_claims(ev: dict) -> dict:
    return (ev.get("response", {}).get("claimsAndScopeOverrideDetails", {})
            .get("accessTokenGeneration", {}).get("claimsToAddOrOverride", {}))


def _id_claims_v2(ev: dict) -> dict:
    return (ev.get("response", {}).get("claimsAndScopeOverrideDetails", {})
            .get("idTokenGeneration", {}).get("claimsToAddOrOverride", {}))


# ── Case A: access token carries bare tenant_id=tenant-A ──────────────────────
def test_case_a_access_token_tenant_a():
    out = pretoken.handler(_v2_event({"sub": "u1", "custom:tenant_id": "tenant-A"}))
    assert _access_claims(out) == {"tenant_id": "tenant-A"}
    assert _id_claims_v2(out) == {"tenant_id": "tenant-A"}  # also on ID token
    # bare only — never the custom: prefixed form
    assert "custom:tenant_id" not in _access_claims(out)


def test_case_b_value_verbatim_on_access_token():
    out = pretoken.handler(_v2_event({"custom:tenant_id": "tenant-B"}))
    assert _access_claims(out) == {"tenant_id": "tenant-B"}


# ── Case C: no attribute -> claim omitted on BOTH tokens ─────────────────────
def test_case_c_missing_attribute_omits_on_both_tokens():
    out = pretoken.handler(_v2_event({"sub": "u3", "email": "x@example.com"}))
    assert out["response"].get("claimsAndScopeOverrideDetails") is None
    assert _access_claims(out) == {}
    assert _id_claims_v2(out) == {}


def test_empty_attribute_treated_as_absent():
    for val in ("", "   ", "\t"):
        out = pretoken.handler(_v2_event({"custom:tenant_id": val}))
        assert out["response"].get("claimsAndScopeOverrideDetails") is None, f"{val!r} must omit"


def test_never_synthesizes_default():
    out = pretoken.handler(_v2_event({}))
    assert _access_claims(out) == {}


def test_v1_fallback_id_token_only():
    # A V1 event (no version/scopes) still works, emitting the V1 shape.
    out = pretoken.handler(_v1_event({"custom:tenant_id": "tenant-A"}))
    claims = out["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert claims == {"tenant_id": "tenant-A"}
    assert "claimsAndScopeOverrideDetails" not in out["response"]


def test_v1_fallback_missing_omits():
    out = pretoken.handler(_v1_event({"sub": "u9"}))
    assert out["response"].get("claimsOverrideDetails") is None


def test_full_event_echoed_back():
    ev = _v2_event({"custom:tenant_id": "tenant-A"})
    out = pretoken.handler(ev)
    assert out["request"]["userAttributes"]["custom:tenant_id"] == "tenant-A"
    assert out["version"] == "2"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
