"""Gateway REQUEST interceptor — AgentCore MCP interceptor contract.

Runs BEFORE Cedar on every MCP request. Contract (AgentCore Gateway docs,
"Types of interceptors"):

  EVENT (MCP REQUEST interceptor):
    {
      "interceptorInputVersion": "1.0",
      "mcp": {
        "rawGatewayRequest": {"body": "<raw>"},
        "gatewayRequest": {
          "path": "/mcp", "httpMethod": "POST",
          "headers": {"Authorization": "Bearer <jwt>", ...},   # ONLY if
                                                                # inputConfiguration.passRequestHeaders=true
          "body": {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}
        }
      }
    }

  RETURN (must be exactly this envelope):
    {"interceptorOutputVersion":"1.0","mcp":{"transformedGatewayRequest":{"body": <body>}}}
    ...or a transformedGatewayResponse to short-circuit (deny).

Responsibilities (this revision — deliberately minimal, see Gate C plan):
  1. Pull the bearer token from mcp.gatewayRequest.headers.Authorization.
  2. INDEPENDENTLY and FULLY verify the JWT against the configured Cognito
     issuer/JWKS: RS256 signature, `iss`, `exp`, `token_use == access`, and
     `client_id` in the allowed set. Base64-decoding claims is NOT authentication.
  3. On verification FAILURE -> fail closed (deny via transformedGatewayResponse).
     This is an AUTHENTICATION failure, distinct from tenant authorization.
  4. On success -> PASS THROUGH the original MCP body UNCHANGED.

Deliberately NOT done here (owned by Cedar / a later interceptor revision):
  * We do NOT overwrite params.arguments.tenant_id with the JWT tenant.
  * We do NOT short-circuit tenant mismatch / missing tenant_id — those are the
    B/C cases the next LOG_ONLY experiment must let Cedar decide. A valid token
    with a mismatched or absent tenant_id claim is passed through unchanged.

Security notes:
  * The bearer token is NEVER logged or returned.
  * There is NO `_fact_ctx` side-channel — an MCP target Lambda only receives the
    tool arguments. Delivering a *verified* tenant to the handler is a separate
    design task; this revision does not invent an undocumented channel.

Requires PyJWT + cryptography in the Lambda bundle (see requirements.txt).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

import jwt  # PyJWT

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_DISCOVERY_URL = os.environ.get("COGNITO_DISCOVERY_URL", "")
_ALLOWED_CLIENTS = [c.strip() for c in os.environ.get("COGNITO_ALLOWED_CLIENTS", "").split(",") if c.strip()]
_EXPECTED_TOKEN_USE = os.environ.get("EXPECTED_TOKEN_USE", "access")

# Cached OIDC metadata + JWKS client (resolved once per warm Lambda).
_OIDC: dict | None = None
_JWKS = None


class InterceptorAuthError(Exception):
    """Authentication failure — the request must be denied (fail closed)."""


# ── envelope helpers ──────────────────────────────────────────────────────────
def _passthrough(body) -> dict:
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayRequest": {"body": body}}}


def _deny(req_id, message: str, status: int = 403) -> dict:
    # Generic message only — never include the token or claim values.
    return {"interceptorOutputVersion": "1.0",
            "mcp": {"transformedGatewayResponse": {
                "statusCode": status,
                "body": {"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32001, "message": message}}}}}


def _gateway_request(event: dict) -> dict:
    return ((event or {}).get("mcp") or {}).get("gatewayRequest") or {}


def _extract_body(event: dict) -> dict:
    body = _gateway_request(event).get("body")
    return body if isinstance(body, dict) else {}


def _extract_bearer(event: dict) -> str | None:
    headers = _gateway_request(event).get("headers") or {}
    # Header names are case-insensitive.
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if not auth or not isinstance(auth, str):
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]
    return None


# ── JWT verification ──────────────────────────────────────────────────────────
def _load_oidc() -> dict:
    global _OIDC
    if _OIDC is None:
        if not _DISCOVERY_URL:
            raise InterceptorAuthError("COGNITO_DISCOVERY_URL not configured")
        with urllib.request.urlopen(_DISCOVERY_URL, timeout=5) as r:  # noqa: S310 (trusted issuer URL)
            _OIDC = json.loads(r.read().decode())
    return _OIDC


def _jwks_client():
    global _JWKS
    if _JWKS is None:
        _JWKS = jwt.PyJWKClient(_load_oidc()["jwks_uri"])
    return _JWKS


def verify_claims(token: str, *, key, issuer: str, allowed_clients: list[str],
                  expected_token_use: str = "access", leeway: int = 0) -> dict:
    """Full JWT verification. Returns claims or raises InterceptorAuthError.

    `key` is an RSA public key (PEM / cryptography key / PyJWK) — resolved from
    JWKS in production, injected directly in tests. Access tokens carry no `aud`,
    so audience is not verified; the caller identity is bound via `client_id`.
    """
    try:
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=issuer,
            options={"verify_aud": False, "require": ["exp", "iat", "iss"]},
            leeway=leeway,
        )
    except jwt.InvalidTokenError as e:
        raise InterceptorAuthError(f"jwt invalid: {type(e).__name__}") from e
    if claims.get("token_use") != expected_token_use:
        raise InterceptorAuthError("unexpected token_use")
    if allowed_clients and claims.get("client_id") not in allowed_clients:
        raise InterceptorAuthError("client_id not allowed")
    return claims


def _verify_request(token: str) -> dict:
    """Resolve the signing key from JWKS and verify. Raises InterceptorAuthError."""
    oidc = _load_oidc()
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
    except Exception as e:  # noqa: BLE001 — any JWKS/parse failure is an auth failure
        raise InterceptorAuthError("could not resolve signing key") from e
    return verify_claims(token, key=signing_key, issuer=oidc["issuer"],
                         allowed_clients=_ALLOWED_CLIENTS,
                         expected_token_use=_EXPECTED_TOKEN_USE)


# ── handler ───────────────────────────────────────────────────────────────────
def handler(event, _lambda_context=None):
    body = _extract_body(event)
    req_id = body.get("id")
    method = body.get("method", "unknown")

    token = _extract_bearer(event)
    if not token:
        # No Authorization header reached us. Requires
        # inputConfiguration.passRequestHeaders=true on the interceptor config.
        logger.info("interceptor deny: no bearer header (method=%s)", method)
        return _deny(req_id, "unauthorized: missing bearer token")

    try:
        claims = _verify_request(token)
    except InterceptorAuthError as e:
        logger.info("interceptor deny: %s (method=%s)", e, method)
        return _deny(req_id, "unauthorized: token verification failed")

    # Authenticated. Log the decision + whether a tenant_id claim is present
    # (boolean only — never the value or the token). Tenant AUTHORIZATION
    # (match/absent) is intentionally left to Cedar for the LOG_ONLY experiment.
    logger.info("interceptor allow: method=%s has_tenant_claim=%s",
                method, "tenant_id" in claims)
    return _passthrough(body)


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
