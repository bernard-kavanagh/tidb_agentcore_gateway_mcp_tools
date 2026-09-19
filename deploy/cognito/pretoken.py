"""Cognito Pre-Token-Generation trigger for the fact-layer user pool.

Emits a bare `tenant_id` claim onto the **access token** (the canonical Gateway
bearer token) — and the ID token — derived from the `custom:tenant_id` user
attribute.

Issuance contract (EXACT):
  * SOURCE : the user attribute `custom:tenant_id`.
  * EMIT   : a top-level `tenant_id` claim (no `custom:` prefix) on the ACCESS
             token (via accessTokenGeneration) and the ID token.
  * ABSENT : if the source attribute is absent OR empty/whitespace, OMIT
             `tenant_id` entirely on both tokens — never default, never synthesize.

Trigger version:
  Access-token claim customization requires the V2_0 (user identities) trigger
  event, available on the Essentials/Plus feature plan. V2/V3 events carry
  `request.scopes` and expect the `claimsAndScopeOverrideDetails` response with an
  `accessTokenGeneration` block. This handler emits that shape for V2/V3 events
  and falls back to the V1 `claimsOverrideDetails` (ID-token only) for V1 events,
  so the same code is correct whichever version the pool is configured to send.

Reserved claims (iss/exp/token_use/sub/client_id/...) cannot be overridden;
`tenant_id` is a non-schema custom claim and is addable to both tokens.

Handler entry point: pretoken.handler (runtime python3.12).
"""

from __future__ import annotations

SOURCE_ATTR = "custom:tenant_id"
EMIT_CLAIM = "tenant_id"


def _tenant_id(event: dict) -> str | None:
    """Return the source tenant id, or None if absent/empty (never a default)."""
    request = event.get("request") or {}
    attrs = request.get("userAttributes") or {}
    raw = attrs.get(SOURCE_ATTR)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None  # empty / whitespace-only -> treated as absent


def _is_v2_plus(event: dict) -> bool:
    """V2_0/V3_0 events carry a version of 2/3 and include request.scopes."""
    version = str(event.get("version") or "")
    if version in ("2", "3"):
        return True
    return "scopes" in (event.get("request") or {})


def build_response(event: dict) -> dict:
    """Populate event['response'] per the issuance contract; return the event.

    When the source attribute is absent/empty we add NOTHING (no override block),
    so the issued tokens simply have no tenant_id — the missing-claim (Case C)
    vector, on BOTH the access and ID tokens.
    """
    event.setdefault("response", {})
    tenant_id = _tenant_id(event)
    if tenant_id is None:
        return event  # omit entirely — do not default or synthesize

    if _is_v2_plus(event):
        # V2_0/V3_0: customize BOTH access and ID tokens.
        event["response"]["claimsAndScopeOverrideDetails"] = {
            "accessTokenGeneration": {"claimsToAddOrOverride": {EMIT_CLAIM: tenant_id}},
            "idTokenGeneration": {"claimsToAddOrOverride": {EMIT_CLAIM: tenant_id}},
        }
    else:
        # V1_0 fallback: ID token only (access-token claims not available in V1).
        event["response"]["claimsOverrideDetails"] = {
            "claimsToAddOrOverride": {EMIT_CLAIM: tenant_id}
        }
    return event


def handler(event, _context=None):
    return build_response(event)


def lambda_handler(event, context):  # noqa: N802 (Cognito convention)
    return handler(event, context)
