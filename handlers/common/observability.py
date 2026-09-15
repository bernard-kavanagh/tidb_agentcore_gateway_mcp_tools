"""Structured, secret-safe observability for the fact layer.

Emits one JSON line per event to stdout (CloudWatch picks it up). Deliberately
logs only NON-sensitive fields: tenant, predicate, outcome, policy_version,
status, opaque ids (subject_id/event_id), latency, correlation id, and error
CODES. It never logs the asserted value, the raw subject string, secrets, tokens,
or credentials.

Usage:
    from .common.observability import log_event, correlation_id
    cid = correlation_id(ctx)
    log_event("record_fact", tenant_id=ctx.tenant_id, predicate=p,
              outcome=res["outcome"], policy_version=res["policy_version"],
              status=res["status"], subject_id=res["subject_id"],
              event_id=res["event_id"], duration_ms=..., correlation_id=cid)
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Fields that must never be logged even if a caller passes them by mistake.
_DENYLIST = {"value", "subject", "password", "secret", "token", "credential",
             "authorization", "result_json"}


def correlation_id(ctx) -> str:
    """A non-secret correlation id from the request context (session + agent)."""
    session = getattr(ctx, "session_id", "unknown")
    agent = getattr(ctx, "agent_id", "unknown")
    return f"{session}:{agent}"


def log_event(event_type: str, **fields: Any) -> None:
    """Emit one structured JSON log line, dropping any denylisted/None fields."""
    rec = {"event": event_type}
    for k, v in fields.items():
        if v is None or k.lower() in _DENYLIST:
            continue
        rec[k] = v
    try:
        line = json.dumps(rec, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        line = json.dumps({"event": event_type, "_log_error": "unserializable fields"})
    print(line, file=sys.stdout)
