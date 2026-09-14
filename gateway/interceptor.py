"""Gateway request interceptor — runs BEFORE Cedar.

Enforcement order: interceptor -> Cedar -> tool. The interceptor enriches the
request so Cedar evaluates the enriched context and the handler receives a
verified identity plus a tenant-scoped credential reference. A denied call never
reaches TiDB.

Responsibilities:
  1. Read the VERIFIED caller identity from the Cognito JWT (validated inbound by
     the Gateway authorizer). The tenant_id is a first-class claim.
  2. Inject provenance context (tenant_id, agent_id, session_id, tool_turn) that
     the caller cannot forge, under the key our handlers read (`_fact_ctx`).
  3. Swap to the tenant-scoped credential the handler will use: resolve the
     tenant to its Secrets Manager ARN and inject it (never the secret material).

DEPLOY-CONFIRM (verify against the current AgentCore request-interceptor docs;
the exact event/response shape is platform-specific and evolves):
  * where verified claims arrive on the interceptor event, and
  * the response shape that injects context Cedar + the target will see
    (so context.input.tenant_id populates — deploy-confirm A — and the injected
    credential reaches a tenant-scoped TiDB session — deploy-confirm B).

The claim-extraction and tenant->ARN mapping below are the parts that are ours;
the surrounding envelope is the piece to confirm at deploy.
"""

from __future__ import annotations

import json
import os

# tenant_id -> tenant-scoped Secrets Manager ARN. Supplied by the operator as a
# JSON env value (ARNs only, never secret material). If a tenant is absent we
# fall back to a single scoped ARN so early validation is runnable.
_TENANT_SECRET_MAP = json.loads(os.environ.get("TENANT_SECRET_MAP", "{}"))
_DEFAULT_SECRET_ARN = os.environ.get("FACT_LAYER_SECRET_ARN")


def _claims(event: dict) -> dict:
    """Extract verified JWT claims. Adjust the path to the confirmed event shape."""
    for path in (
        ("requestContext", "authorizer", "jwt", "claims"),
        ("requestContext", "authorizer", "claims"),
        ("claims",),
    ):
        node = event
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            return node
    return {}


def _secret_arn_for(tenant_id: str) -> str | None:
    return _TENANT_SECRET_MAP.get(tenant_id, _DEFAULT_SECRET_ARN)


def handler(event, _lambda_context=None):
    claims = _claims(event)
    tenant_id = claims.get("tenant_id")
    if not tenant_id:
        # Fail closed: no verified tenant claim -> do not enrich; Cedar will deny
        # (permit requires principal.hasTag("tenant_id")).
        return {"_fact_ctx": {}, "_error": "missing tenant_id claim"}

    ctx = {
        "tenant_id": tenant_id,
        "agent_id": claims.get("sub") or claims.get("client_id") or "unknown",
        "session_id": claims.get("session_id") or claims.get("jti") or "unknown",
        "tenant_secret_arn": _secret_arn_for(tenant_id),
    }
    turn = claims.get("tool_turn")
    if turn is not None:
        ctx["tool_turn"] = int(turn)

    # Inject under the key handlers/common/context.py reads. The exact merge into
    # the outgoing request envelope is the deploy-confirm item.
    return {"_fact_ctx": ctx}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
