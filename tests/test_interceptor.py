"""Offline tests for the AgentCore MCP REQUEST interceptor + handler context.

Interceptor (gateway/interceptor.py) — new AgentCore contract:
  * full RS256 JWT verification (signature/iss/exp/token_use/client_id),
  * contract-valid envelope (interceptorOutputVersion + mcp.transformedGatewayRequest),
  * pass-through of the original MCP body unchanged after auth (tenant NOT rewritten,
    tenant mismatch/absent NOT short-circuited — Cedar decides that later),
  * fail-closed deny on auth failure / missing header,
  * no bearer-token or claim-value leakage.

Handler context (handlers/common/context.py) tests are retained UNCHANGED — that
module is not modified in this change (its rework is tracked separately).

    python3 -m pytest tests/test_interceptor.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Module-level config read at import.
os.environ.setdefault("COGNITO_ALLOWED_CLIENTS", "client123")
os.environ.setdefault("COGNITO_DISCOVERY_URL", "https://issuer.test/pool/.well-known/openid-configuration")
os.environ.setdefault("FACT_LAYER_SECRET_ARN",
                      "arn:aws:secretsmanager:eu-central-1:111122223333:secret:tidb-XyZ012")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
import jwt  # noqa: E402

from gateway import interceptor  # noqa: E402

_ISSUER = "https://issuer.test/pool"
_CLIENT = "client123"


def _keypair():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


_PRIV, _PUB = _keypair()
_OTHER_PRIV, _OTHER_PUB = _keypair()


def _token(priv=_PRIV, **overrides):
    now = int(time.time())
    payload = {"sub": "u1", "iss": _ISSUER, "iat": now, "exp": now + 3600,
               "token_use": "access", "client_id": _CLIENT, "tenant_id": "tenant-A"}
    payload.update(overrides)
    return jwt.encode(payload, priv, algorithm="RS256")


def _mcp_event(body: dict, auth: str | None = "PLACEHOLDER") -> dict:
    headers = {} if auth is None else {"Authorization": auth}
    return {"interceptorInputVersion": "1.0",
            "mcp": {"rawGatewayRequest": {"body": "<raw>"},
                    "gatewayRequest": {"path": "/mcp", "httpMethod": "POST",
                                       "headers": headers, "body": body}}}


_CALL_BODY = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "GetFact___get_fact",
                         "arguments": {"tenant_id": "tenant-B", "subject": "s", "predicate": "status"}}}


# ── verify_claims: full RS256 verification ────────────────────────────────────
def test_verify_valid_token():
    claims = interceptor.verify_claims(_token(), key=_PUB, issuer=_ISSUER,
                                       allowed_clients=[_CLIENT])
    assert claims["tenant_id"] == "tenant-A"
    assert claims["client_id"] == _CLIENT


def test_verify_rejects_bad_signature():
    tok = _token(priv=_OTHER_PRIV)  # signed by a different key
    _expect_auth_error(lambda: interceptor.verify_claims(tok, key=_PUB, issuer=_ISSUER,
                                                         allowed_clients=[_CLIENT]))


def test_verify_rejects_wrong_issuer():
    _expect_auth_error(lambda: interceptor.verify_claims(
        _token(), key=_PUB, issuer="https://evil.example/pool", allowed_clients=[_CLIENT]))


def test_verify_rejects_expired():
    tok = _token(exp=int(time.time()) - 10)
    _expect_auth_error(lambda: interceptor.verify_claims(tok, key=_PUB, issuer=_ISSUER,
                                                         allowed_clients=[_CLIENT]))


def test_verify_rejects_wrong_client():
    _expect_auth_error(lambda: interceptor.verify_claims(
        _token(client_id="someoneelse"), key=_PUB, issuer=_ISSUER, allowed_clients=[_CLIENT]))


def test_verify_rejects_wrong_token_use():
    _expect_auth_error(lambda: interceptor.verify_claims(
        _token(token_use="id"), key=_PUB, issuer=_ISSUER, allowed_clients=[_CLIENT]))


def _expect_auth_error(fn):
    try:
        fn()
    except interceptor.InterceptorAuthError:
        return
    raise AssertionError("expected InterceptorAuthError")


# ── handler: envelope + pass-through + deny ────────────────────────────────────
def _with_stub_verify(claims_or_exc):
    """Context manager-ish: swap interceptor._verify_request, restore after."""
    prev = interceptor._verify_request
    def stub(_tok):
        if isinstance(claims_or_exc, Exception):
            raise claims_or_exc
        return claims_or_exc
    interceptor._verify_request = stub
    return prev


def test_handler_valid_envelope_and_passthrough_preserves_arg_tenant():
    prev = _with_stub_verify({"sub": "u", "client_id": _CLIENT, "token_use": "access",
                              "tenant_id": "tenant-A"})  # JWT says tenant-A
    try:
        out = interceptor.handler(_mcp_event(_CALL_BODY, auth="Bearer x.y.z"))
    finally:
        interceptor._verify_request = prev
    # contract-valid REQUEST envelope
    assert out["interceptorOutputVersion"] == "1.0"
    tgr = out["mcp"]["transformedGatewayRequest"]
    # body passed through UNCHANGED — tenant-B argument NOT overwritten by JWT tenant-A
    assert tgr["body"] == _CALL_BODY
    assert tgr["body"]["params"]["arguments"]["tenant_id"] == "tenant-B"
    assert "transformedGatewayResponse" not in out["mcp"]


def test_handler_missing_header_denies():
    out = interceptor.handler(_mcp_event(_CALL_BODY, auth=None))
    resp = out["mcp"]["transformedGatewayResponse"]
    assert resp["statusCode"] == 403
    assert resp["body"]["error"]["message"].startswith("unauthorized")
    assert "transformedGatewayRequest" not in out["mcp"]


def test_handler_malformed_bearer_denies():
    for bad in ("Basic zzz", "Bearer", "token-without-scheme"):
        out = interceptor.handler(_mcp_event(_CALL_BODY, auth=bad))
        assert "transformedGatewayResponse" in out["mcp"], f"{bad!r} must deny"


def test_handler_verification_failure_denies():
    prev = _with_stub_verify(interceptor.InterceptorAuthError("jwt invalid: InvalidSignatureError"))
    try:
        out = interceptor.handler(_mcp_event(_CALL_BODY, auth="Bearer bad.tok.en"))
    finally:
        interceptor._verify_request = prev
    assert out["mcp"]["transformedGatewayResponse"]["statusCode"] == 403


# ── no token / claim leakage ──────────────────────────────────────────────────
def test_no_token_leakage_in_response_or_logs(caplog=None):
    secret_token = "Bearer LEAKY.SECRET.TOKENVALUE1234567890"
    prev = _with_stub_verify(interceptor.InterceptorAuthError("jwt invalid: InvalidSignatureError"))
    logs = _CaptureLogs()
    logging.getLogger().addHandler(logs)
    try:
        out = interceptor.handler(_mcp_event(_CALL_BODY, auth=secret_token))
    finally:
        interceptor._verify_request = prev
        logging.getLogger().removeHandler(logs)
    blob = json.dumps(out)
    assert "LEAKY.SECRET.TOKENVALUE1234567890" not in blob, "token must not appear in response"
    assert "LEAKY.SECRET.TOKENVALUE1234567890" not in logs.text(), "token must not appear in logs"


class _CaptureLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
    def emit(self, record):
        self.records.append(record)
    def text(self):
        return "\n".join(self.format(r) for r in self.records)


# handler-context (handlers/common/context.py) tests now live in tests/test_context.py
# (the module was reworked to the real MCP target contract; no _fact_ctx side channel).


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
