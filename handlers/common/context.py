"""Tool-argument context for MCP Lambda target handlers (AgentCore real contract).

An AgentCore MCP Lambda target receives ONLY the tool-arguments map — the
properties from the tool's inputSchema (see the AgentCore "Lambda function input
format" docs). There is **no** `_fact_ctx` / `bedrockAgentCoreContext` side
channel, and the request interceptor does **not** inject `tenant_secret_arn` /
`agent_id` / `session_id` (that channel does not exist in the documented
contract). This module reflects that reality:

  * `tenant_id` is read from the TOOL ARGUMENTS — the requested TENANT SELECTOR
    (the data scope). Authorizing that the caller may select this scope belongs to
    the Gateway governance layer (reference profile: Cedar tenant-equality,
    `principal.getTag("tenant_id") == context.input.tenant_id`, deny-by-default —
    enforced only in ENFORCE; in LOG_ONLY the decision is shadow-only). Inside the
    Lambda it is NOT treated as an authenticated identity. It parameterizes every
    tenant-scoped SQL predicate and selects the DB credential.
  * the DB secret ARN is resolved for that tenant — see CREDENTIAL MODEL.
  * `agent_id` / `session_id` / `tool_turn` PROVENANCE is NOT available at an MCP
    target. `fact_event.agent_id`/`session_id` are NOT NULL, so a non-identity
    marker ("unknown") is used; `tool_turn` is None. These are provenance metadata,
    never a security control. (Propagating real caller provenance to the target is
    a separate future design — it must not resurrect an undocumented side channel.)

CREDENTIAL MODEL (explicit modes, never an accidental silent fallback):
  * per_tenant : TENANT_SECRET_MAP is a non-empty JSON object {tenant_id: arn}.
                 The tenant MUST be present, else FAIL CLOSED — an unknown tenant
                 does NOT silently fall back to the shared credential.
  * shared     : TENANT_SECRET_MAP empty ({}/unset) AND FACT_LAYER_SECRET_ARN set
                 -> the shared runtime credential serves ALL tenants. This is the
                 CURRENT deployed design; tenant isolation then rests on (1) Cedar
                 at the Gateway and (2) the per-query tenant_id SQL predicates —
                 NOT on per-tenant DB roles. `credential_mode` records this so it
                 is observable, not implicit.
  * neither    : FAIL CLOSED (no DB credential configured).

DIRECT-INVOCATION BOUNDARY:
  The tenant_id argument is trustworthy ONLY because Cedar authorized it at the
  Gateway. A direct AWS `lambda:InvokeFunction` (bypassing the Gateway) carries no
  Cedar decision and no JWT, so the argument would be unauthorized. Direct
  invocation is gated by IAM: only principals holding lambda:InvokeFunction on
  these functions (the Gateway invoke role + AWS admins) can do it — never an
  external agent caller. Optional defense in depth: set
  FACT_REQUIRE_GATEWAY_CONTEXT=1 to require the AgentCore Gateway invocation
  marker (Lambda client_context) and FAIL CLOSED on a direct invoke.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

# Legacy keys that MUST NOT be treated as authenticated context anymore. If a
# caller (or a stale interceptor) puts one on the event, it is ignored — it can
# never override the actual tool argument.
_LEGACY_CTX_KEYS = ("_fact_ctx", "context", "bedrockAgentCoreContext")
# Some target wirings nest the arguments; accept either shape.
_ARG_WRAPPERS = ("input", "arguments", "body")

_PROVENANCE_UNKNOWN = "unknown"  # fact_event.agent_id/session_id are NOT NULL


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    tenant_secret_arn: str
    credential_mode: str            # "per_tenant" | "shared"
    agent_id: str = _PROVENANCE_UNKNOWN
    session_id: str = _PROVENANCE_UNKNOWN
    tool_turn: int | None = None


def _tool_args(event: dict) -> dict[str, Any]:
    """Return the tool-argument map, stripping any legacy context keys."""
    if not isinstance(event, dict):
        return {}
    for k in _ARG_WRAPPERS:
        if isinstance(event.get(k), dict):
            return dict(event[k])
    return {k: v for k, v in event.items() if k not in _LEGACY_CTX_KEYS}


def _require_gateway_context() -> bool:
    return os.environ.get("FACT_REQUIRE_GATEWAY_CONTEXT", "0").strip().lower() in ("1", "true", "yes")


def _is_gateway_invocation(lambda_context: Any) -> bool:
    """True if the Lambda was invoked via the AgentCore Gateway.

    The Gateway sets bedrockAgentCore* keys in the invocation's client_context;
    a direct invoke does not.
    """
    cc = getattr(lambda_context, "client_context", None)
    custom = getattr(cc, "custom", None) or {}
    if not isinstance(custom, dict):
        return False
    return bool(custom.get("bedrockAgentCoreGatewayId") or custom.get("bedrockAgentCoreToolName"))


def _resolve_secret_arn(tenant_id: str) -> tuple[str, str]:
    """Return (secret_arn, credential_mode). Fails closed on misconfiguration."""
    raw = (os.environ.get("TENANT_SECRET_MAP") or "").strip()
    tmap: dict = {}
    if raw and raw != "{}":
        try:
            tmap = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"TENANT_SECRET_MAP is not valid JSON: {e}") from e
        if not isinstance(tmap, dict):
            raise ValueError("TENANT_SECRET_MAP must be a JSON object of tenant_id -> ARN")

    if tmap:
        # per-tenant mode: strict. No silent fallback to the shared credential.
        arn = tmap.get(tenant_id)
        if not arn:
            raise PermissionError(
                f"no per-tenant DB credential configured for tenant '{tenant_id}'"
            )
        return str(arn), "per_tenant"

    shared = os.environ.get("FACT_LAYER_SECRET_ARN")
    if shared:
        # explicit shared-runtime-credential mode (current deployed design)
        return shared, "shared"

    raise ValueError(
        "no DB credential configured: TENANT_SECRET_MAP is empty and "
        "FACT_LAYER_SECRET_ARN is unset"
    )


def extract(event: dict, lambda_context: Any = None) -> tuple[ToolContext, dict[str, Any]]:
    """Return (ToolContext, tool_args) from an MCP target event. Fails closed.

    `tenant_id` comes from the tool arguments (the requested tenant selector);
    a missing/empty value raises PermissionError.
    """
    args = _tool_args(event)

    if _require_gateway_context() and not _is_gateway_invocation(lambda_context):
        raise PermissionError(
            "request did not arrive via the AgentCore Gateway "
            "(FACT_REQUIRE_GATEWAY_CONTEXT is enabled)"
        )

    raw_tenant = args.get("tenant_id")
    if raw_tenant is None or not str(raw_tenant).strip():
        raise PermissionError(
            "tenant_id argument is required and must be non-empty "
            "(the Gateway-authorized tenant selector)"
        )
    tenant_id = str(raw_tenant).strip()

    secret_arn, mode = _resolve_secret_arn(tenant_id)
    ctx = ToolContext(tenant_id=tenant_id, tenant_secret_arn=secret_arn, credential_mode=mode)
    return ctx, args
