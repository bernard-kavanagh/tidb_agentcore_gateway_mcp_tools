"""retract_fact — explicitly retract the current fact for (tenant, subject, predicate).

Appends a hash-chained op='retract' event (append-only: history is preserved) and
sets the resolution status to 'retracted'. This is a lifecycle transition, not a
deletion — the prior events remain in the log and the chain stays continuous.

Tenant-scoped; the retracting source's authority + policy_version are recorded on
the resolution. Typed, no free-form SQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .common import context
from .common.authority import resolve_authority
from .common.db import get_engine
from .common.hashing import compute_event_hash


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event, _lambda_context)
        subject = args["subject"]
        predicate = args["predicate"]
        source = args["source"]
        reason = args.get("reason")
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    t = ctx.tenant_id
    observed_at = _utc_now_str()
    engine = get_engine(ctx.tenant_secret_arn)
    try:
        with engine.begin() as cx:
            subj = cx.execute(
                text("SELECT subject_id, last_event_hash FROM fact_subject "
                     "WHERE tenant_id=:t AND subject=:s AND predicate=:p FOR UPDATE"),
                {"t": t, "s": subject, "p": predicate},
            ).first()
            if subj is None:
                raise ValueError("fact not found")
            subject_id = int(subj.subject_id)
            prev_hash = subj.last_event_hash
            if prev_hash is None:
                raise ValueError("cannot retract a subject with no events")

            cur = cx.execute(
                text("SELECT value, winning_event_id, status FROM fact_current "
                     "WHERE tenant_id=:t AND subject_id=:id"),
                {"t": t, "id": subject_id},
            ).first()
            if cur is None:
                raise ValueError("no current resolution to retract")

            policy_version = resolve_authority(
                cx, tenant_id=t, predicate=predicate, source=source, as_of=observed_at,
            ).policy_version

            # value on the retract event = the value being retracted (audit).
            retracted_value = json.loads(cur.value) if cur.value is not None else None
            fields = {
                "subject_id": subject_id, "tenant_id": t, "op": "retract",
                "value": retracted_value, "source": source, "confidence": None,
                "agent_id": ctx.agent_id, "session_id": ctx.session_id,
                "tool_turn": ctx.tool_turn,
                "supersedes_event_id": int(cur.winning_event_id),
                "valid_from": None, "valid_to": None, "asserted_at": observed_at,
                "observed_at": observed_at, "prev_hash": prev_hash,
            }
            event_hash = compute_event_hash(fields)
            ins = cx.execute(
                text("INSERT INTO fact_event "
                     "(subject_id, tenant_id, op, value, source, confidence, agent_id, "
                     " session_id, tool_turn, supersedes_event_id, valid_from, valid_to, "
                     " asserted_at, observed_at, prev_hash, event_hash, outcome, "
                     " policy_version, assessor_type) "
                     "VALUES (:subject_id, :tenant_id, :op, :value, :source, :confidence, "
                     " :agent_id, :session_id, :tool_turn, :supersedes_event_id, "
                     " :valid_from, :valid_to, :asserted_at, :observed_at, :prev_hash, "
                     " :event_hash, 'RETRACTED', :policy_version, :assessor_type)"),
                {**fields, "value": json.dumps(retracted_value), "event_hash": event_hash,
                 "policy_version": policy_version, "assessor_type": args.get("assessor_type")},
            )
            event_id = int(ins.lastrowid or cx.execute(text("SELECT LAST_INSERT_ID()")).scalar())

            cx.execute(
                text("UPDATE fact_subject SET last_event_hash=:h WHERE subject_id=:id"),
                {"h": event_hash, "id": subject_id},
            )
            cx.execute(
                text("UPDATE fact_current SET status='retracted', winning_event_id=:e, "
                     "    policy_version=:pv, resolution_reason=:reason WHERE subject_id=:id"),
                {"e": event_id, "pv": policy_version,
                 "reason": (reason[:255] if reason else "retracted"), "id": subject_id},
            )

        return {"tenant_id": t, "subject": subject, "predicate": predicate,
                "event_id": event_id, "status": "retracted",
                "outcome": "RETRACTED", "policy_version": policy_version}
    except ValueError as e:
        return {"error": str(e), "error_type": "ValueError"}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
