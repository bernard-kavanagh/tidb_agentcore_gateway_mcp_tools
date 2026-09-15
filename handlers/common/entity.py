"""Canonical entity identity + evidence-reference helpers.

Deliberately NOT general-purpose entity resolution: we do not decide whether two
customers are the same person. We provide a DETERMINISTIC canonical key so facts
across investigations can reference the same entity, and a resolve-or-create that
is idempotent on (tenant_id, canonical_key).

Pure helpers (stdlib-only, unit-testable without a cluster):
  * normalize_identifier / build_canonical_key
  * normalize_relationship / normalize_assessor_type

DB helper (lazy SQLAlchemy import):
  * resolve_or_create_entity(cx, ...)
"""

from __future__ import annotations

import re

# Evidence relationship vocabulary (how a referenced observation relates to the assertion).
EVIDENCE_RELATIONSHIPS = frozenset(
    {"SUPPORTS", "CONTRADICTS", "DERIVED_FROM", "TRIGGERED_BY", "REVIEWED_IN"}
)
_DEFAULT_RELATIONSHIP = "SUPPORTS"

# Who/what produced an assessment.
ASSESSOR_TYPES = frozenset({"human", "system", "model", "agent", "user"})


def normalize_identifier(raw: str) -> str:
    """Lowercase, trim, collapse internal whitespace. Deterministic + stable."""
    s = str(raw).strip().lower()
    return re.sub(r"\s+", " ", s)


def build_canonical_key(entity_type: str, raw_identifier: str) -> str:
    """Deterministic canonical key: '<entity_type>:<normalized-identifier>'."""
    etype = normalize_identifier(entity_type) or "unknown"
    ident = normalize_identifier(raw_identifier)
    return f"{etype}:{ident}"


def normalize_relationship(rel: str | None) -> str:
    """Validate/normalize an evidence relationship; default SUPPORTS."""
    if rel is None:
        return _DEFAULT_RELATIONSHIP
    r = str(rel).strip().upper()
    if r not in EVIDENCE_RELATIONSHIPS:
        raise ValueError(
            f"invalid evidence relationship {rel!r}; allowed: {sorted(EVIDENCE_RELATIONSHIPS)}"
        )
    return r


def normalize_assessor_type(assessor_type: str | None) -> str | None:
    """Validate/normalize an assessor type; None passes through (unknown)."""
    if assessor_type is None:
        return None
    a = str(assessor_type).strip().lower()
    if a not in ASSESSOR_TYPES:
        raise ValueError(
            f"invalid assessor_type {assessor_type!r}; allowed: {sorted(ASSESSOR_TYPES)}"
        )
    return a


def resolve_or_create_entity(
    cx, *, tenant_id: str, entity_type: str, raw_identifier: str,
    display_name: str | None = None,
) -> tuple[int, str]:
    """Return (entity_id, canonical_key), creating the entity if absent.

    Idempotent on (tenant_id, canonical_key) via INSERT IGNORE + SELECT. Imports
    SQLAlchemy lazily so the pure helpers above stay dependency-free for tests.
    """
    from sqlalchemy import text  # lazy

    canonical_key = build_canonical_key(entity_type, raw_identifier)
    etype = normalize_identifier(entity_type) or "unknown"
    cx.execute(
        text(
            "INSERT IGNORE INTO entity (tenant_id, entity_type, canonical_key, display_name) "
            "VALUES (:t, :et, :ck, :dn)"
        ),
        {"t": tenant_id, "et": etype, "ck": canonical_key,
         "dn": display_name or raw_identifier},
    )
    entity_id = cx.execute(
        text("SELECT entity_id FROM entity WHERE tenant_id=:t AND canonical_key=:ck"),
        {"t": tenant_id, "ck": canonical_key},
    ).scalar()
    return int(entity_id), canonical_key
