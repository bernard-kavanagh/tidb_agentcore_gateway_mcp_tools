"""list_disputes — facts currently in a disputed resolution for the caller's tenant.

Surfaces genuine comparable-authority conflicts (status='disputed') so an operator
or agent can triage them. Typed, tenant-scoped, no free-form SQL.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from .common import context
from .common.db import get_engine

_MAX_ROWS = 100


def _loads(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except (TypeError, json.JSONDecodeError):
        return v


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event)
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    limit = min(int(args.get("limit", _MAX_ROWS)), _MAX_ROWS)
    t = ctx.tenant_id
    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        rows = cx.execute(
            text("SELECT fs.subject, fs.predicate, fs.entity_id, fc.value, "
                 "       fc.winning_event_id, fc.policy_version, fc.resolution_reason, "
                 "       fc.updated_at "
                 "FROM fact_current fc "
                 "JOIN fact_subject fs ON fs.subject_id = fc.subject_id "
                 "WHERE fc.tenant_id=:t AND fc.status='disputed' "
                 "ORDER BY fc.updated_at DESC LIMIT :k"),
            {"t": t, "k": limit},
        ).mappings().all()

    disputes = [
        {
            "subject": r["subject"], "predicate": r["predicate"],
            "entity_id": (int(r["entity_id"]) if r["entity_id"] is not None else None),
            "current_value": _loads(r["value"]),
            "policy_version": r["policy_version"],
            "resolution_reason": r["resolution_reason"],
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        }
        for r in rows
    ]
    return {"tenant_id": t, "count": len(disputes), "disputes": disputes}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
