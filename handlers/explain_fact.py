"""explain_fact — the priority read tool: WHY is the current fact what it is?

Given (tenant_id, subject, predicate) it returns, optimized for agent consumption:
  * the current resolution (value, status, policy_version, reason, validity window),
  * the winning assertion's provenance (source, assessor_type, confidence, authority),
  * supporting evidence and CONTRARY evidence (references back to the fraud domain),
  * assessments (source/assessor + confidence over time),
  * superseded / rejected / disputed prior assertions,
  * the outcome history (ASSERTED -> ... -> current),
so the agent can EXPLAIN a conclusion rather than merely return a verdict.

Tenant isolation: tenant_id is taken from the interceptor-verified context, never
the raw argument; every query is scoped to it (defense in depth behind Cedar).
No free-form SQL.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from .common import context
from .common.authority import resolve_authority
from .common.db import get_engine

_MAX_EVENTS = 200


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
        subj = cx.execute(
            text("SELECT subject_id, entity_id FROM fact_subject "
                 "WHERE tenant_id=:t AND subject=:s AND predicate=:p"),
            {"t": t, "s": subject, "p": predicate},
        ).first()
        if subj is None:
            return {"error": "fact not found", "error_type": "NotFound",
                    "tenant_id": t, "subject": subject, "predicate": predicate}
        subject_id = int(subj.subject_id)

        cur = cx.execute(
            text("SELECT value, status, winning_event_id, policy_version, "
                 "       resolution_reason, valid_from, valid_to, updated_at "
                 "FROM fact_current WHERE tenant_id=:t AND subject_id=:id"),
            {"t": t, "id": subject_id},
        ).first()

        events = cx.execute(
            text("SELECT event_id, op, value, source, confidence, assessor_type, "
                 "       outcome, policy_version, supersedes_event_id, "
                 "       valid_from, valid_to, asserted_at, observed_at "
                 "FROM fact_event WHERE tenant_id=:t AND subject_id=:id "
                 "ORDER BY observed_at ASC, event_id ASC LIMIT :k"),
            {"t": t, "id": subject_id, "k": _MAX_EVENTS},
        ).mappings().all()

        evidence = cx.execute(
            text("SELECT event_id, evidence_type, evidence_ref, relationship, "
                 "       metadata, observed_at "
                 "FROM fact_evidence WHERE tenant_id=:t AND subject_id=:id "
                 "ORDER BY created_at ASC"),
            {"t": t, "id": subject_id},
        ).mappings().all()

        current_value = _loads(cur.value) if cur else None
        winning_source = None
        winning_assessor = None
        winning_authority = None
        if cur is not None:
            win = cx.execute(
                text("SELECT source, assessor_type FROM fact_event "
                     "WHERE tenant_id=:t AND event_id=:e"),
                {"t": t, "e": cur.winning_event_id},
            ).first()
            if win is not None:
                winning_source = win.source
                winning_assessor = win.assessor_type
                winning_authority = resolve_authority(
                    cx, tenant_id=t, predicate=predicate, source=win.source,
                ).authority

    # ---- shape the response (contrary vs supporting split) ----
    supporting_assertions, contrary_assertions, assessments, history = [], [], [], []
    for e in events:
        val = _loads(e["value"])
        rec = {
            "event_id": int(e["event_id"]), "op": e["op"], "value": val,
            "source": e["source"], "assessor_type": e["assessor_type"],
            "confidence": (float(e["confidence"]) if e["confidence"] is not None else None),
            "outcome": e["outcome"], "policy_version": e["policy_version"],
            "valid_from": str(e["valid_from"]) if e["valid_from"] else None,
            "valid_to": str(e["valid_to"]) if e["valid_to"] else None,
            "observed_at": str(e["observed_at"]) if e["observed_at"] else None,
        }
        history.append({"event_id": rec["event_id"], "outcome": e["outcome"],
                        "value": val, "observed_at": rec["observed_at"]})
        if e["confidence"] is not None:
            assessments.append({"source": e["source"], "assessor_type": e["assessor_type"],
                                "confidence": rec["confidence"], "value": val})
        # An assertion agreeing with the current value supports it; a differing one
        # is contrary (it was rejected/superseded/disputed, retained in the log).
        if cur is not None and val == current_value:
            supporting_assertions.append(rec)
        elif cur is not None:
            contrary_assertions.append(rec)

    supporting_evidence, contrary_evidence = [], []
    for ev in evidence:
        item = {"evidence_type": ev["evidence_type"], "evidence_ref": ev["evidence_ref"],
                "relationship": ev["relationship"], "metadata": _loads(ev["metadata"]),
                "observed_at": str(ev["observed_at"]) if ev["observed_at"] else None}
        (contrary_evidence if ev["relationship"] == "CONTRADICTS" else supporting_evidence).append(item)

    return {
        "tenant_id": t,
        "subject": subject,
        "predicate": predicate,
        "entity_id": (int(subj.entity_id) if subj.entity_id is not None else None),
        "current_value": current_value,
        "resolution": {
            "status": (cur.status if cur else None),
            "reason": (cur.resolution_reason if cur else None),
            "policy_version": (cur.policy_version if cur else None),
            "valid_from": (str(cur.valid_from) if cur and cur.valid_from else None),
            "valid_to": (str(cur.valid_to) if cur and cur.valid_to else None),
            "updated_at": (str(cur.updated_at) if cur and cur.updated_at else None),
        },
        "authority": {
            "winning_source": winning_source,
            "winning_assessor_type": winning_assessor,
            "authority": winning_authority,
            "policy_version": (cur.policy_version if cur else None),
        },
        "supporting_evidence": supporting_evidence,
        "contrary_evidence": contrary_evidence,
        "assessments": assessments,
        "supporting_assertions": supporting_assertions,
        "contrary_assertions": contrary_assertions,
        "history": history,
    }


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
