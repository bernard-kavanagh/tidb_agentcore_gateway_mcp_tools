"""Predicate-specific, versioned source authority.

Replaces the global source_rank lookup on the adjudication path. Authority is
resolved as a function of (predicate, source, tenant, time), not source alone:
"how authoritative is this source FOR THIS PREDICATE right now?".

Split in two so the ranking is exhaustively unit-testable without a cluster:
  * select_authority(...)  — PURE (stdlib only). Given candidate policy rows,
                             pick the winning row and return its authority +
                             policy_version. This is the logic under test.
  * resolve_authority(cx)  — thin DB wrapper: load candidate rows for a
                             (tenant, predicate, source) then call select_authority.

Matching rules (deterministic):
  1. A row matches if its source == source, its tenant is the caller's tenant or
     the wildcard '*', its predicate is the exact predicate or the wildcard '*',
     and `as_of` falls in [effective_from, effective_to).
  2. Among matches, prefer: exact predicate over '*'  ->  exact tenant over '*'
     ->  higher priority  ->  higher authority  ->  latest effective_from.
  3. No match -> authority 0 (lowest), policy_version 'unversioned', matched=False.
     (Mirrors the old "unknown source -> rank 0" default.)

`as_of` is compared as an ISO-8601 string (lexicographic order == chronological
for zero-padded ISO), matching how observed_at is produced on the write path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

_OPEN_PAST = "0000-00-00 00:00:00.000000"
_OPEN_FUTURE = "9999-12-31 23:59:59.999999"


@dataclass(frozen=True)
class AuthorityMatch:
    authority: int
    policy_version: str
    matched: bool
    predicate: str | None = None      # the predicate the winning row matched on ('*' if wildcard)
    tenant_id: str | None = None
    source_type: str | None = None


def _as_str(v: Any, default: str) -> str:
    return default if v is None else str(v)


def _covers(row: dict, as_of: str) -> bool:
    ef = _as_str(row.get("effective_from"), _OPEN_PAST)
    et = row.get("effective_to")
    if as_of < ef:
        return False
    if et is not None and as_of >= str(et):
        return False
    return True


def select_authority(
    rows: Sequence[dict],
    *,
    predicate: str,
    source: str,
    tenant_id: str = "*",
    as_of: str | None = None,
) -> AuthorityMatch:
    """Pick the authoritative policy row. Pure; no I/O."""
    as_of = as_of or _OPEN_FUTURE  # None -> "now-ish": accept any open/covering row
    candidates = []
    for r in rows:
        if str(r.get("source")) != source:
            continue
        r_pred = str(r.get("predicate"))
        if r_pred not in (predicate, "*"):
            continue
        r_tenant = str(r.get("tenant_id", "*"))
        if r_tenant not in (tenant_id, "*"):
            continue
        if not _covers(r, as_of):
            continue
        candidates.append(r)

    if not candidates:
        return AuthorityMatch(authority=0, policy_version="unversioned", matched=False,
                              predicate=None, tenant_id=None)

    def rank_key(r: dict):
        exact_pred = 1 if str(r.get("predicate")) == predicate else 0
        exact_tenant = 1 if str(r.get("tenant_id", "*")) == tenant_id else 0
        return (
            exact_pred,
            exact_tenant,
            int(r.get("priority", 0)),
            int(r.get("authority", 0)),
            _as_str(r.get("effective_from"), _OPEN_PAST),
        )

    best = max(candidates, key=rank_key)
    return AuthorityMatch(
        authority=int(best.get("authority", 0)),
        policy_version=str(best.get("policy_version", "unversioned")),
        matched=True,
        predicate=str(best.get("predicate")),
        tenant_id=str(best.get("tenant_id", "*")),
        source_type=(None if best.get("source_type") is None else str(best.get("source_type"))),
    )


def resolve_authority(cx, *, tenant_id: str, predicate: str, source: str,
                      as_of: str | None = None) -> AuthorityMatch:
    """Load candidate policy rows for (tenant, predicate, source) and select.

    Imports SQLAlchemy lazily so the pure `select_authority` above is importable
    (and testable) without SQLAlchemy installed.
    """
    from sqlalchemy import text  # lazy

    rows = cx.execute(
        text(
            "SELECT predicate, source, source_type, authority, priority, "
            "       policy_version, effective_from, effective_to, tenant_id "
            "FROM authority_policy "
            "WHERE source = :s "
            "  AND predicate IN (:p, '*') "
            "  AND (tenant_id = :t OR tenant_id = '*')"
        ),
        {"s": source, "p": predicate, "t": tenant_id},
    ).mappings().all()
    return select_authority(
        [dict(r) for r in rows],
        predicate=predicate, source=source, tenant_id=tenant_id, as_of=as_of,
    )
