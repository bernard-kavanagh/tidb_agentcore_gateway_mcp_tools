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
source, confidence. Optional Evidence->Assessment->Resolution fields (all
backward-compatible): entity_id OR (entity_type [+ canonical_key/entity_key]) to
bind the fact to a canonical entity; assessor_type (human|system|model|agent|user);
evidence[] = references back to fraud-domain observations
({evidence_type, evidence_ref, relationship?, metadata?, observed_at?}) — stored
as references, never copies. tenant_id is the requested data scope (tool argument),
applied deterministically to every tenant-scoped operation; the DB credential is
resolved from configuration for that scope. Identity/provenance (agent_id,
session_id, tool_turn) is not propagated to an MCP target — it is recorded as
"unknown"/None, not an authenticated identity.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .common import context, embeddings
from .common.adjudication import adjudicate
from .common.authority import resolve_authority
from .common.db import get_engine
from .common.observability import correlation_id, log_event
from .common.entity import (
    normalize_assessor_type,
    normalize_relationship,
    resolve_or_create_entity,
)
from .common.hashing import GENESIS_HASH, compute_event_hash
from .common.write_control import enforce_write_control_floor


def _utc_now_str() -> str:
    # Naive UTC, microsecond precision. This exact string is both hashed and
    # stored in fact_event.observed_at DATETIME(6) so the hash is reproducible.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


class _IdempotentReplay(Exception):
    """Raised inside the write txn when the idempotency key is already claimed, to
    roll back this attempt so the original committed result can be replayed."""


def handler(event, _lambda_context=None):
    t0 = time.perf_counter()
    try:
        ctx, args = context.extract(event, _lambda_context)
        subject = args["subject"]
        predicate = args["predicate"]
        value = args["value"]
        source = args["source"]
        confidence = args.get("confidence")
        # Optional Evidence->Assessment->Resolution fields (all backward-compatible;
        # existing callers that send only subject/predicate/value/source/confidence
        # keep working unchanged):
        arg_entity_id = args.get("entity_id")
        entity_type = args.get("entity_type")
        entity_key = args.get("canonical_key") or args.get("entity_key")  # raw identifier
        assessor_type = normalize_assessor_type(args.get("assessor_type"))
        # Temporal claims (optional ISO-8601 strings). valid_from/valid_to are the
        # real-world validity window; asserted_at defaults to observed_at below.
        arg_valid_from = args.get("valid_from")
        arg_valid_to = args.get("valid_to")
        arg_asserted_at = args.get("asserted_at")
        idempotency_key = args.get("idempotency_key")
        context_summary = args.get("context_summary")  # enriches the semantic embedding
        evidence = args.get("evidence") or []
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list of {evidence_type, evidence_ref, ...}")
        for ev in evidence:
            if not isinstance(ev, dict) or "evidence_type" not in ev or "evidence_ref" not in ev:
                raise ValueError("each evidence item needs evidence_type and evidence_ref")
            normalize_relationship(ev.get("relationship"))  # validate up front (pure)
        # Write-control gate: reject sub-floor confidence BEFORE adjudication.
        enforce_write_control_floor(confidence)
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    tenant = ctx.tenant_id
    engine = get_engine(ctx.tenant_secret_arn)

    # Idempotent replay (fast path, no txn): if this key already produced a
    # result, return it verbatim so a retry never re-does the work.
    if idempotency_key:
        with engine.connect() as probe:
            prior = probe.execute(
                text("SELECT result_json FROM fact_idempotency "
                     "WHERE tenant_id=:t AND idempotency_key=:k"),
                {"t": tenant, "k": idempotency_key},
            ).first()
        if prior is not None and prior.result_json:
            res = json.loads(prior.result_json)
            res["idempotent_replay"] = True
            return res

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
        # Embed a normalized representation (entity + predicate + value + context),
        # not the raw subject id, so semantic recall finds relevant knowledge.
        embed_text = embeddings.build_embedding_text(
            subject=subject, predicate=predicate, value=value,
            entity_type=entity_type, context_summary=context_summary,
        )
        subject_vec = embeddings.to_vector_literal(embeddings.embed(embed_text))

    observed_at = _utc_now_str()
    # asserted_at defaults to observed_at when the caller does not distinguish them.
    asserted_at = arg_asserted_at or observed_at

    try:
        with engine.begin() as cx:
            # Claim the idempotency key FIRST, atomically with the write. A
            # concurrent duplicate blocks on the key here, then fails duplicate-key
            # once the winner commits -> this whole txn rolls back (no duplicate
            # event) and the committed result is replayed below.
            if idempotency_key:
                try:
                    cx.execute(
                        text("INSERT INTO fact_idempotency (tenant_id, idempotency_key) "
                             "VALUES (:t, :k)"),
                        {"t": tenant, "k": idempotency_key},
                    )
                except IntegrityError:
                    raise _IdempotentReplay()

            # --- predicate must be declared in the registry (rule-driven) ---
            value_type = cx.execute(
                text("SELECT value_type FROM predicate_rule WHERE predicate=:p"),
                {"p": predicate},
            ).scalar()
            if value_type is None:
                raise ValueError(f"predicate '{predicate}' is not in predicate_rule")

            # --- entity resolution (optional; deterministic canonical key) ---
            # We do NOT do general-purpose entity resolution: an explicit entity_id
            # is used as-is, else (entity_type + identifier) resolves-or-creates a
            # canonical entity idempotently on (tenant, canonical_key).
            resolved_entity_id = int(arg_entity_id) if arg_entity_id is not None else None
            if resolved_entity_id is None and entity_type:
                resolved_entity_id, _ = resolve_or_create_entity(
                    cx, tenant_id=tenant, entity_type=entity_type,
                    raw_identifier=entity_key or subject,
                )

            # --- 1. resolve-or-create subject, then lock it ---
            cx.execute(
                text("INSERT IGNORE INTO fact_subject "
                     "(tenant_id, subject, predicate, subject_vec, last_event_hash, entity_id) "
                     "VALUES (:t, :s, :p, :vec, NULL, :eid)"),
                {"t": tenant, "s": subject, "p": predicate, "vec": subject_vec,
                 "eid": resolved_entity_id},
            )
            subj = cx.execute(
                text("SELECT subject_id, last_event_hash FROM fact_subject "
                     "WHERE tenant_id=:t AND subject=:s AND predicate=:p FOR UPDATE"),
                {"t": tenant, "s": subject, "p": predicate},
            ).first()
            subject_id = int(subj.subject_id)
            prev_hash = subj.last_event_hash or GENESIS_HASH
            # Link a pre-existing subject to a newly-resolved entity if it had none.
            if resolved_entity_id is not None:
                cx.execute(
                    text("UPDATE fact_subject SET entity_id=:eid "
                         "WHERE subject_id=:id AND entity_id IS NULL"),
                    {"eid": resolved_entity_id, "id": subject_id},
                )

            # --- 2. current winner ---
            cur = cx.execute(
                text("SELECT value, winning_event_id, status FROM fact_current "
                     "WHERE subject_id=:id"),
                {"id": subject_id},
            ).first()
            has_current = cur is not None
            current_value = json.loads(cur.value) if has_current else None

            # Predicate-specific, versioned authority (authority_policy) — not a
            # global source rank. Record the policy_version that produced this
            # resolution so it is never silently recomputed under a newer policy.
            inc_auth = resolve_authority(
                cx, tenant_id=tenant, predicate=predicate, source=source,
                as_of=observed_at,
            )
            incoming_rank = inc_auth.authority
            policy_version = inc_auth.policy_version
            current_rank = -1
            if has_current:
                cur_source = cx.execute(
                    text("SELECT source FROM fact_event WHERE event_id=:e"),
                    {"e": cur.winning_event_id},
                ).scalar()
                current_rank = resolve_authority(
                    cx, tenant_id=tenant, predicate=predicate, source=cur_source,
                    as_of=observed_at,
                ).authority

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
                "valid_from": arg_valid_from,
                "valid_to": arg_valid_to,
                "asserted_at": asserted_at,
                "observed_at": observed_at,
                "prev_hash": prev_hash,
            }
            event_hash = compute_event_hash(fields)
            ins = cx.execute(
                text(
                    "INSERT INTO fact_event "
                    "(subject_id, tenant_id, op, value, source, confidence, agent_id, "
                    " session_id, tool_turn, supersedes_event_id, valid_from, valid_to, "
                    " asserted_at, observed_at, "
                    " prev_hash, event_hash, outcome, policy_version, assessor_type) "
                    "VALUES (:subject_id, :tenant_id, :op, :value, :source, :confidence, "
                    " :agent_id, :session_id, :tool_turn, :supersedes_event_id, "
                    " :valid_from, :valid_to, :asserted_at, "
                    " :observed_at, :prev_hash, :event_hash, :outcome, :policy_version, "
                    " :assessor_type)"
                ),
                # outcome + policy_version + assessor_type are additive audit columns,
                # NOT part of the hash (hashing.HASHED_FIELDS is unchanged).
                {**fields, "value": json.dumps(value), "event_hash": event_hash,
                 "outcome": d.outcome, "policy_version": policy_version,
                 "assessor_type": assessor_type},
            )
            event_id = ins.lastrowid or cx.execute(text("SELECT LAST_INSERT_ID()")).scalar()
            event_id = int(event_id)

            # --- 5. advance the chain tip ---
            cx.execute(
                text("UPDATE fact_subject SET last_event_hash=:h WHERE subject_id=:id"),
                {"h": event_hash, "id": subject_id},
            )

            # --- 5b. attach evidence references (references, never domain copies) ---
            for ev in evidence:
                meta = ev.get("metadata")
                cx.execute(
                    text("INSERT INTO fact_evidence "
                         "(tenant_id, event_id, subject_id, entity_id, evidence_type, "
                         " evidence_ref, relationship, metadata, observed_at) "
                         "VALUES (:t, :eid, :sid, :ent, :etype, :eref, :rel, :meta, :obs)"),
                    {"t": tenant, "eid": event_id, "sid": subject_id,
                     "ent": resolved_entity_id,
                     "etype": str(ev["evidence_type"]), "eref": str(ev["evidence_ref"]),
                     "rel": normalize_relationship(ev.get("relationship")),
                     "meta": json.dumps(meta) if meta is not None else None,
                     "obs": ev.get("observed_at")},
                )

            # --- 6. write-through fact_current (same transaction) ---
            competing: list[int] = []
            reason = d.notes[:255] if d.notes else None
            if not has_current:
                cx.execute(
                    text("INSERT INTO fact_current "
                         "(subject_id, tenant_id, value, winning_event_id, status, "
                         " policy_version, resolution_reason, valid_from, valid_to) "
                         "VALUES (:id, :t, :v, :e, :st, :pv, :reason, :vf, :vt)"),
                    {"id": subject_id, "t": tenant,
                     "v": json.dumps(d.new_current_value),
                     "e": event_id, "st": d.new_status,
                     "pv": policy_version, "reason": reason,
                     "vf": arg_valid_from, "vt": arg_valid_to},
                )
                status = d.new_status
            elif d.repoint_current:
                cx.execute(
                    text("UPDATE fact_current "
                         "SET value=:v, winning_event_id=:e, status=:st, "
                         "    policy_version=:pv, resolution_reason=:reason, "
                         "    valid_from=:vf, valid_to=:vt "
                         "WHERE subject_id=:id"),
                    {"v": json.dumps(d.new_current_value), "e": event_id,
                     "st": d.new_status, "pv": policy_version, "reason": reason,
                     "vf": arg_valid_from, "vt": arg_valid_to,
                     "id": subject_id},
                )
                status = d.new_status
            elif d.disputed:
                cx.execute(
                    text("UPDATE fact_current SET status='disputed', "
                         "    policy_version=:pv, resolution_reason=:reason "
                         "WHERE subject_id=:id"),
                    {"id": subject_id, "pv": policy_version, "reason": reason},
                )
                status = "disputed"
                competing = [int(cur.winning_event_id), event_id]
            else:
                # corroborate / stale / REJECTED: winner + current value unchanged.
                # The event is still appended to the log as contrary/corroborating
                # evidence; the resolution row is intentionally left untouched.
                status = cur.status

            result = {
                "event_id": event_id,
                "status": status,
                "decision": d.kind,
                "outcome": d.outcome,
                "op": d.op,
                "policy_version": policy_version,
                "subject_id": subject_id,
                "entity_id": resolved_entity_id,
                "evidence_count": len(evidence),
                "competing_event_ids": competing,
            }
            # Record the result against the claimed key, in the SAME txn, so a
            # retry replays it and a crash never leaves a claimed-but-empty key.
            if idempotency_key:
                cx.execute(
                    text("UPDATE fact_idempotency SET event_id=:e, result_json=:r "
                         "WHERE tenant_id=:t AND idempotency_key=:k"),
                    {"e": event_id, "r": json.dumps(result),
                     "t": tenant, "k": idempotency_key},
                )

        log_event("record_fact", tenant_id=tenant, predicate=predicate,
                  outcome=result["outcome"], status=result["status"],
                  policy_version=result["policy_version"],
                  subject_id=result["subject_id"], event_id=result["event_id"],
                  disputed=(result["status"] == "disputed"),
                  evidence_count=result["evidence_count"],
                  correlation_id=correlation_id(ctx),
                  duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return result
    except _IdempotentReplay:
        with engine.connect() as probe:
            prior = probe.execute(
                text("SELECT result_json FROM fact_idempotency "
                     "WHERE tenant_id=:t AND idempotency_key=:k"),
                {"t": tenant, "k": idempotency_key},
            ).first()
        if prior is not None and prior.result_json:
            res = json.loads(prior.result_json)
            res["idempotent_replay"] = True
            log_event("record_fact", tenant_id=ctx.tenant_id, predicate=predicate,
                      outcome=res.get("outcome"), status=res.get("status"),
                      idempotent_replay=True, correlation_id=correlation_id(ctx),
                      duration_ms=round((time.perf_counter() - t0) * 1000, 1))
            return res
        # Claim committed but result not yet visible (shouldn't happen: both are one
        # txn) — surface a retryable conflict rather than risk a duplicate.
        return {"error": "idempotency key in progress; retry", "error_type": "Conflict"}
    except ValueError as e:
        log_event("record_fact.error", tenant_id=getattr(ctx, "tenant_id", None),
                  error_type="ValueError", correlation_id=correlation_id(ctx),
                  duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return {"error": str(e), "error_type": "ValueError"}


# AWS Lambda entrypoint.
def lambda_handler(event, context_):  # noqa: N802 (AWS naming)
    return handler(event, context_)
