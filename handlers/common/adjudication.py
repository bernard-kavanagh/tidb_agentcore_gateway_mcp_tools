"""The contradiction test — deterministic, rule-based, DB-free.

This module decides supersede vs corroborate vs dispute from values, the
predicate's value_type, and source authority ranks. It is intentionally pure
(no DB, no network, no LLM) so it is exhaustively unit-testable and so the
authoritative decision is provable and recorded — never an LLM judgment.

Non-negotiables encoded here:
  * Recency supersedes ONLY for `monotonic` predicates. There is no time-window
    overwrite anywhere else — not even to break ties within an authority tier.
  * A contradiction only supersedes when the incoming source strictly outranks
    the current winner. Equal-or-lower authority -> dispute (both retained).
  * `set` predicates union; they never supersede.
  * `freetext` defaults to dispute; a semantic pre-filter (outside this module)
    may only confirm that routing — it must never turn a dispute into a supersede.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _as_member_set(v: Any) -> set[str]:
    """Normalize a set-valued value (list or scalar) to a set of canonical members."""
    members = v if isinstance(v, list) else [v]
    return {_canon(m) for m in members}


def _members_to_list(members: set[str]) -> list[Any]:
    """Back to JSON values, sorted for a stable stored representation."""
    return [json.loads(m) for m in sorted(members)]


# Adjudication kind -> stable, agent-facing outcome label. The `kind` is the
# internal branch; the `outcome` is what the audit log / explain_fact report.
_OUTCOME = {
    "assert_new": "ASSERTED",
    "corroborate": "CORROBORATED",
    "supersede": "SUPERSEDED",
    "update": "SUPERSEDED",
    "set_extend": "SUPERSEDED",
    "stale": "REJECTED",       # monotonic earlier observation: recorded, did not win
    "rejected": "REJECTED",    # lower-authority contradiction: retained as contrary evidence
    "dispute": "DISPUTED",
}


@dataclass
class Decision:
    kind: str                          # assert_new|corroborate|update|stale|set_extend|supersede|rejected|dispute
    op: str                            # fact_event.op to append: assert|supersede
    repoint_current: bool              # move fact_current.winning_event_id to the new event
    new_current_value: Any = None      # value to store in fact_current when repointing
    new_status: str | None = None      # fact_current.status to set (None = leave as-is)
    supersedes_event_id: int | None = None
    disputed: bool = False             # competing events retained; caller returns competing ids
    notes: str = ""

    # Convenience for the write path / tests.
    @property
    def is_supersede(self) -> bool:
        return self.op == "supersede"

    @property
    def outcome(self) -> str:
        """Stable outcome label (CORROBORATED/SUPERSEDED/REJECTED/DISPUTED/ASSERTED)."""
        return _OUTCOME.get(self.kind, self.kind.upper())


def adjudicate(
    *,
    value_type: str,
    incoming_value: Any,
    current_value: Any,
    has_current: bool,
    incoming_rank: int,
    current_rank: int,
) -> Decision:
    """Decide the outcome of an incoming assert against the current winner.

    `incoming_rank`/`current_rank` come from source_rank (unknown source -> 0).
    For `monotonic`, ordering of the values themselves is the recency signal
    (e.g. an ISO-8601 last_seen string, or a number).
    """
    # 1. No current winner -> first assert.
    if not has_current:
        return Decision(
            kind="assert_new", op="assert", repoint_current=True,
            new_current_value=incoming_value, new_status="active",
            notes="no prior winner",
        )

    # 2. Winner exists -> evaluate the predicate's conflict rule.
    if value_type == "scalar":
        if _canon(incoming_value) == _canon(current_value):
            return Decision(kind="corroborate", op="assert", repoint_current=False,
                            notes="scalar equal -> corroboration")
        return _authority_gate(incoming_value, incoming_rank, current_rank,
                               reason="scalar unequal")

    if value_type == "monotonic":
        return _monotonic(incoming_value, current_value)

    if value_type == "set":
        cur = _as_member_set(current_value)
        inc = _as_member_set(incoming_value)
        if inc <= cur:
            return Decision(kind="corroborate", op="assert", repoint_current=False,
                            notes="set subset -> corroboration")
        merged = _members_to_list(cur | inc)
        return Decision(kind="set_extend", op="assert", repoint_current=True,
                        new_current_value=merged, new_status="active",
                        notes="set union (never a supersede)")

    if value_type == "freetext":
        if _canon(incoming_value) == _canon(current_value):
            return Decision(kind="corroborate", op="assert", repoint_current=False,
                            notes="freetext identical -> corroboration")
        # Default to dispute. A semantic pre-filter may confirm this routing but
        # must never decide a supersede.
        return Decision(kind="dispute", op="assert", repoint_current=False,
                        new_status="disputed", disputed=True,
                        notes="freetext differs -> dispute (never silent overwrite)")

    raise ValueError(f"unknown value_type: {value_type!r}")


def _authority_gate(incoming_value, incoming_rank, current_rank, *, reason) -> Decision:
    """Contradiction resolution by predicate-specific authority. Three outcomes:

      * incoming STRICTLY outranks current  -> SUPERSEDE (re-point current).
      * incoming STRICTLY lower authority    -> REJECTED. The event is retained in
        the log as CONTRARY evidence, but the current winner is NOT destabilized
        (a customer's "I'm legitimate" does not overturn a fraud_review
        confirmation). No dispute.
      * comparable (equal) authority          -> DISPUTED. Both competing events
        retained; current status becomes 'disputed'. Recency never breaks the tie
        (non-monotonic recency supersede is forbidden).
    """
    if incoming_rank > current_rank:
        return Decision(
            kind="supersede", op="supersede", repoint_current=True,
            new_current_value=incoming_value, new_status="active",
            notes=f"{reason}; incoming outranks ({incoming_rank}>{current_rank}) -> supersede",
        )
    if incoming_rank < current_rank:
        return Decision(
            kind="rejected", op="assert", repoint_current=False,
            new_status=None, disputed=False,
            notes=(f"{reason}; incoming lower authority ({incoming_rank}<{current_rank}) "
                   f"-> rejected (retained as contrary evidence; current unchanged)"),
        )
    return Decision(
        kind="dispute", op="assert", repoint_current=False,
        new_status="disputed", disputed=True,
        notes=f"{reason}; comparable authority ({incoming_rank}=={current_rank}) -> disputed",
    )


def _monotonic(incoming_value, current_value) -> Decision:
    """Later observation supersedes — the only place recency legitimately wins."""
    try:
        later = incoming_value > current_value
        equal = incoming_value == current_value
    except TypeError:
        # Not orderable -> cannot assert recency; treat as an unresolved conflict.
        return Decision(kind="dispute", op="assert", repoint_current=False,
                        new_status="disputed", disputed=True,
                        notes="monotonic values not orderable -> dispute")
    if equal:
        return Decision(kind="corroborate", op="assert", repoint_current=False,
                        notes="monotonic equal -> corroboration")
    if later:
        return Decision(kind="update", op="supersede", repoint_current=True,
                        new_current_value=incoming_value, new_status="active",
                        notes="monotonic later observation -> supersede")
    # Earlier observation: record it, but it does not supersede the current value.
    return Decision(kind="stale", op="assert", repoint_current=False,
                    notes="monotonic earlier observation -> recorded, not superseding")
