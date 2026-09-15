"""get_fact_history — the append-only event lineage for (tenant, subject, predicate).

Returns every assessment/resolution event in observed order: op, outcome, value,
source, assessor_type, confidence, policy_version, temporal fields, and the
hash-chain links. Tenant-scoped, typed, no free-form SQL.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from .common import context
from .common.db import get_engine

_MAX_EVENTS = 500


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

    limit = min(int(args.get("limit", _MAX_EVENTS)), _MAX_EVENTS)
    t = ctx.tenant_id
    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        subj = cx.execute(
            text("SELECT subject_id FROM fact_subject "
                 "WHERE tenant_id=:t AND subject=:s AND predicate=:p"),
            {"t": t, "s": subject, "p": predicate},
        ).first()
        if subj is None:
            return {"error": "fact not found", "error_type": "NotFound",
                    "tenant_id": t, "subject": subject, "predicate": predicate}
        rows = cx.execute(
            text("SELECT event_id, op, outcome, value, source, assessor_type, "
                 "       confidence, policy_version, supersedes_event_id, "
                 "       valid_from, valid_to, asserted_at, observed_at, "
                 "       prev_hash, event_hash "
                 "FROM fact_event WHERE tenant_id=:t AND subject_id=:id "
                 "ORDER BY observed_at ASC, event_id ASC LIMIT :k"),
            {"t": t, "id": int(subj.subject_id), "k": limit},
        ).mappings().all()

    history = [
        {
            "event_id": int(r["event_id"]), "op": r["op"], "outcome": r["outcome"],
            "value": _loads(r["value"]), "source": r["source"],
            "assessor_type": r["assessor_type"],
            "confidence": (float(r["confidence"]) if r["confidence"] is not None else None),
            "policy_version": r["policy_version"],
            "supersedes_event_id": (int(r["supersedes_event_id"]) if r["supersedes_event_id"] is not None else None),
            "valid_from": str(r["valid_from"]) if r["valid_from"] else None,
            "valid_to": str(r["valid_to"]) if r["valid_to"] else None,
            "asserted_at": str(r["asserted_at"]) if r["asserted_at"] else None,
            "observed_at": str(r["observed_at"]) if r["observed_at"] else None,
            "prev_hash": r["prev_hash"], "event_hash": r["event_hash"],
        }
        for r in rows
    ]
    return {"tenant_id": t, "subject": subject, "predicate": predicate,
            "count": len(history), "history": history}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
