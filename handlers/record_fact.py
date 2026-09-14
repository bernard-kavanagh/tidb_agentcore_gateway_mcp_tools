"""record_fact — the governed write path.

One ACID transaction:
  1. resolve-or-create fact_subject, LOCKED (SELECT ... FOR UPDATE) so writes to
     the same subject serialize — this is the per-subject hash-chain boundary;
  2. read the current winner in fact_current;
  3. run the deterministic contradiction test (handlers/common/adjudication.py);
  4. append a fact_event with the per-subject hash chain;
  5. advance fact_subject.last_event_hash (the chain tip);
  6. upsert fact_current write-through, in the SAME transaction.

Nothing is ever overwritten in place: supersede/dispute always append and the
prior event stays in the log, hash-chained. That is the audit.

Tool input (per record_fact.schema.json): tenant_id, subject, predicate, value,
source, confidence. Identity/provenance (agent_id, session_id, tool_turn) and
the tenant-scoped DB credential come from the interceptor context, never the
caller.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text

from .common import context, embeddings
from .common.adjudication import adjudicate
from .common.db import get_engine
from .common.hashing import GENESIS_HASH, compute_event_hash


def _utc_now_str() -> str:
    # Naive UTC, microsecond precision. This exact string is both hashed and
    # stored in fact_event.observed_at DATETIME(6) so the hash is reproducible.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _rank(cx, source: str) -> int:
    r = cx.execute(
        text("SELECT rank FROM source_rank WHERE source = :s"), {"s": source}
    ).scalar()
    return int(r) if r is not None else 0  # unknown source -> lowest authority


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event)
        subject = args["subject"]
        predicate = args["predicate"]
        value = args["value"]
        source = args["source"]
        confidence = args.get("confidence")
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    tenant = ctx.tenant_id
    engine = get_engine(ctx.tenant_secret_arn)

    # Embed the subject OUTSIDE the transaction if it looks new, so the Bedrock
    # round-trip never happens while holding the subject row lock.
    subject_vec = None
    with engine.connect() as probe:
        exists = probe.execute(
            text("SELECT 1 FROM fact_subject "
                 "WHERE tenant_id=:t AND subject=:s AND predicate=:p"),
            {"t": tenant, "s": subject, "p": predicate},
        ).first()
    if exists is None:
        subject_vec = embeddings.to_vector_literal(embeddings.embed(subject))

    observed_at = _utc_now_str()

    try:
        with engine.begin() as cx:
            # --- predicate must be declared in the registry (rule-driven) ---
            value_type = cx.execute(
                text("SELECT value_type FROM predicate_rule WHERE predicate=:p"),
                {"p": predicate},
            ).scalar()
            if value_type is None:
                raise ValueError(f"predicate '{predicate}' is not in predicate_rule")

            # --- 1. resolve-or-create subject, then lock it ---
            cx.execute(
                text("INSERT IGNORE INTO fact_subject "
                     "(tenant_id, subject, predicate, subject_vec, last_event_hash) "
                     "VALUES (:t, :s, :p, :vec, NULL)"),
                {"t": tenant, "s": subject, "p": predicate, "vec": subject_vec},
            )
            subj = cx.execute(
                text("SELECT subject_id, last_event_hash FROM fact_subject "
                     "WHERE tenant_id=:t AND subject=:s AND predicate=:p FOR UPDATE"),
                {"t": tenant, "s": subject, "p": predicate},
            ).first()
            subject_id = int(subj.subject_id)
            prev_hash = subj.last_event_hash or GENESIS_HASH

            # --- 2. current winner ---
            cur = cx.execute(
                text("SELECT value, winning_event_id, status FROM fact_current "
                     "WHERE subject_id=:id"),
                {"id": subject_id},
            ).first()
            has_current = cur is not None
            current_value = json.loads(cur.value) if has_current else None

            incoming_rank = _rank(cx, source)
            current_rank = -1
            if has_current:
                cur_source = cx.execute(
                    text("SELECT source FROM fact_event WHERE event_id=:e"),
                    {"e": cur.winning_event_id},
                ).scalar()
                current_rank = _rank(cx, cur_source)

            # --- 3. deterministic contradiction test ---
            d = adjudicate(
                value_type=value_type,
                incoming_value=value,
                current_value=current_value,
                has_current=has_current,
                incoming_rank=incoming_rank,
                current_rank=current_rank,
            )
            supersedes = cur.winning_event_id if (d.is_supersede and has_current) else None

            # --- 4. append the event with the per-subject hash chain ---
            fields = {
                "subject_id": subject_id,
                "tenant_id": tenant,
                "op": d.op,
                "value": value,               # the event records what was asserted
                "source": source,
                "confidence": confidence,
                "agent_id": ctx.agent_id,
                "session_id": ctx.session_id,
                "tool_turn": ctx.tool_turn,
                "supersedes_event_id": supersedes,
                "observed_at": observed_at,
                "prev_hash": prev_hash,
            }
            event_hash = compute_event_hash(fields)
            ins = cx.execute(
                text(
                    "INSERT INTO fact_event "
                    "(subject_id, tenant_id, op, value, source, confidence, agent_id, "
                    " session_id, tool_turn, supersedes_event_id, observed_at, "
                    " prev_hash, event_hash) "
                    "VALUES (:subject_id, :tenant_id, :op, :value, :source, :confidence, "
                    " :agent_id, :session_id, :tool_turn, :supersedes_event_id, "
                    " :observed_at, :prev_hash, :event_hash)"
                ),
                {**fields, "value": json.dumps(value), "event_hash": event_hash},
            )
            event_id = ins.lastrowid or cx.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            event_id = int(event_id)

            # --- 5. advance the chain tip ---
            cx.execute(
                text("UPDATE fact_subject SET last_event_hash=:h WHERE subject_id=:id"),
                {"h": event_hash, "id": subject_id},
            )

            # --- 6. write-through fact_current (same transaction) ---
            competing: list[int] = []
            if not has_current:
                cx.execute(
                    text("INSERT INTO fact_current "
                         "(subject_id, tenant_id, value, winning_event_id, status) "
                         "VALUES (:id, :t, :v, :e, :st)"),
                    {"id": subject_id, "t": tenant,
                     "v": json.dumps(d.new_current_value),
                     "e": event_id, "st": d.new_status},
                )
                status = d.new_status
            elif d.repoint_current:
                cx.execute(
                    text("UPDATE fact_current "
                         "SET value=:v, winning_event_id=:e, status=:st WHERE subject_id=:id"),
                    {"v": json.dumps(d.new_current_value), "e": event_id,
                     "st": d.new_status, "id": subject_id},
                )
                status = d.new_status
            elif d.disputed:
                cx.execute(
                    text("UPDATE fact_current SET status='disputed' WHERE subject_id=:id"),
                    {"id": subject_id},
                )
                status = "disputed"
                competing = [int(cur.winning_event_id), event_id]
            else:
                # corroborate / stale: current value + winner unchanged.
                status = cur.status

        return {
            "event_id": event_id,
            "status": status,
            "decision": d.kind,
            "op": d.op,
            "subject_id": subject_id,
            "competing_event_ids": competing,
        }
    except ValueError as e:
        return {"error": str(e), "error_type": "ValueError"}


# AWS Lambda entrypoint.
def lambda_handler(event, context_):  # noqa: N802 (AWS naming)
    return handler(event, context_)
