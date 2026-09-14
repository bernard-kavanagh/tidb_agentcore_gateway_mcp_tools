"""Interceptor-injected request context + tool-argument extraction.

Enforcement order at the Gateway is: request interceptor -> Cedar -> tool.
By the time a handler runs:
  * Cedar has already verified `context.input.tenant_id == principal tenant claim`
    (the tool argument matches the caller's identity), and
  * the request interceptor has injected the *verified* identity and swapped in
    the tenant-scoped DB credential reference.

The handler therefore trusts the INTERCEPTOR-injected `tenant_id` for all DB
work (never the raw tool argument), and asserts the two agree as defense in
depth. `agent_id` / `session_id` / `tool_turn` are provenance the caller does
not get to forge — they come from the injected context, not the tool input.

Injected-context contract (what the interceptor puts on the event):

    event["_fact_ctx"] = {
        "tenant_id":        "<verified JWT tenant claim>",
        "agent_id":         "<caller identity>",
        "session_id":       "<session>",
        "tool_turn":        <int, optional>,
        "tenant_secret_arn":"arn:aws:secretsmanager:...:secret:...",  # tenant-scoped role
    }

Tool arguments (per the tool's inputSchema) arrive at the top level of the
event, or nested under "input"/"arguments" depending on target wiring — we
accept either and strip the injected-context key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_CTX_KEYS = ("_fact_ctx", "context", "bedrockAgentCoreContext")
_ARG_KEYS = ("input", "arguments", "body")

# Phase-A fallback: a single scoped secret ARN, used only if the interceptor did
# not inject a per-tenant one yet. Per-tenant credential swapping is the target
# state (deploy-confirm B); this keeps offline/early validation runnable.
_DEFAULT_SECRET_ARN = os.environ.get("FACT_LAYER_SECRET_ARN")


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    agent_id: str
    session_id: str
    tenant_secret_arn: str
    tool_turn: int | None = None


def _find_context(event: dict) -> dict:
    for k in _CTX_KEYS:
        if isinstance(event.get(k), dict):
            return event[k]
    return {}


def _find_args(event: dict) -> dict:
    for k in _ARG_KEYS:
        if isinstance(event.get(k), dict):
            return dict(event[k])
    # Otherwise the tool args are the top level minus the injected-context key.
    return {k: v for k, v in event.items() if k not in _CTX_KEYS}


def extract(event: dict) -> tuple[ToolContext, dict[str, Any]]:
    """Return (verified ToolContext, tool_args). Raises if identity is absent."""
    raw = _find_context(event)
    tenant_id = raw.get("tenant_id")
    if not tenant_id:
        raise PermissionError(
            "no verified tenant_id in request context — the interceptor must "
            "inject the identity before the handler runs"
        )
    secret_arn = raw.get("tenant_secret_arn") or _DEFAULT_SECRET_ARN
    if not secret_arn:
        raise ValueError(
            "no tenant-scoped secret ARN in context and FACT_LAYER_SECRET_ARN unset"
        )

    ctx = ToolContext(
        tenant_id=str(tenant_id),
        agent_id=str(raw.get("agent_id", "unknown")),
        session_id=str(raw.get("session_id", "unknown")),
        tenant_secret_arn=str(secret_arn),
        tool_turn=raw.get("tool_turn"),
    )
    args = _find_args(event)

    # Defense in depth: if the tool argument carries tenant_id it must match the
    # verified claim. Cedar already enforced this; a mismatch here means the
    # interceptor/Cedar wiring is broken, so fail closed.
    arg_tenant = args.get("tenant_id")
    if arg_tenant is not None and str(arg_tenant) != ctx.tenant_id:
        raise PermissionError(
            f"tenant_id argument ({arg_tenant}) does not match verified claim "
            f"({ctx.tenant_id})"
        )
    return ctx, args
