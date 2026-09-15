"""get_fact — the current resolved fact for (tenant_id, subject, predicate).

Typed, tenant-scoped, no free-form SQL. For the full "why" use explain_fact.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from .common import context
from .common.db import get_engine


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
        subject = args["subject"]
        predicate = args["predicate"]
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    t = ctx.tenant_id
    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        row = cx.execute(
            text("SELECT fs.subject_id, fs.entity_id, fc.value, fc.status, "
                 "       fc.winning_event_id, fc.policy_version, fc.resolution_reason, "
                 "       fc.valid_from, fc.valid_to, fc.updated_at "
                 "FROM fact_subject fs "
                 "LEFT JOIN fact_current fc ON fc.subject_id = fs.subject_id "
                 "WHERE fs.tenant_id=:t AND fs.subject=:s AND fs.predicate=:p"),
            {"t": t, "s": subject, "p": predicate},
        ).first()

    if row is None:
        return {"error": "fact not found", "error_type": "NotFound",
                "tenant_id": t, "subject": subject, "predicate": predicate}
    return {
        "tenant_id": t,
        "subject": subject,
        "predicate": predicate,
        "entity_id": (int(row.entity_id) if row.entity_id is not None else None),
        "value": _loads(row.value),
        "status": row.status,
        "winning_event_id": (int(row.winning_event_id) if row.winning_event_id is not None else None),
        "policy_version": row.policy_version,
        "resolution_reason": row.resolution_reason,
        "valid_from": str(row.valid_from) if row.valid_from else None,
        "valid_to": str(row.valid_to) if row.valid_to else None,
        "updated_at": str(row.updated_at) if row.updated_at else None,
    }


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
