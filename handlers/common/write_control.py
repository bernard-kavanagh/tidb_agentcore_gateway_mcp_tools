"""Write-control admission gate — pure, DB-free, stdlib-only.

The floor is enforced BEFORE the deterministic contradiction test
(handlers/common/adjudication.py) runs on the record_fact write path: a
low-confidence assertion is rejected outright and never reaches the log. This
is admission control, not adjudication — it decides *whether* to consider an
assertion, never *who wins* a contradiction.

Kept dependency-free (like adjudication/hashing) so it is exhaustively
unit-testable without a live cluster. Env-overridable like the other tunables
(see embeddings.EMBED_DIM).
"""

from __future__ import annotations

import os

DEFAULT_MIN_CONFIDENCE = 0.85


def min_confidence() -> float:
    return float(os.environ.get("WRITE_CONTROL_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE))


def enforce_write_control_floor(confidence) -> float:
    """Reject any assertion whose confidence is below the write-control floor.

    Never clamps or silently adjusts — raises ValueError naming both the floor
    and the offending value. A missing confidence cannot clear the floor.
    """
    floor = min_confidence()
    if confidence is None:
        raise ValueError(
            f"confidence is required and must be >= write-control floor {floor}"
        )
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        raise ValueError(
            f"confidence {confidence!r} is not a number; "
            f"write-control floor is {floor}"
        )
    if value < floor:
        raise ValueError(
            f"confidence {value} is below the write-control floor {floor}"
        )
    return value
