"""Offline tests for the two pieces that need no live cluster:
the deterministic contradiction test and the per-subject hash chain.

These cover acceptance tests 2 (adjudication), 3 (corroboration), and 4 (dispute)
at the logic level, plus tamper-evidence. Run directly:

    python3 tests/test_offline.py

(also importable as pytest test_* functions).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from handlers.common.adjudication import adjudicate  # noqa: E402
from handlers.common.hashing import (  # noqa: E402
    GENESIS_HASH,
    compute_event_hash,
    verify_chain,
)
from handlers.common.write_control import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    enforce_write_control_floor,
)
from handlers.common.authority import select_authority  # noqa: E402
from handlers.common.entity import (  # noqa: E402
    build_canonical_key,
    normalize_assessor_type,
    normalize_relationship,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _event(subject_id, op, value, prev_hash, *, source="s", observed_at="2026-01-01 00:00:00.000000"):
    """Build a fully-hashed event dict the way the write path does."""
    fields = {
        "subject_id": subject_id, "tenant_id": "t1", "op": op, "value": value,
        "source": source, "confidence": "0.900", "agent_id": "a", "session_id": "sess",
        "tool_turn": 1, "supersedes_event_id": None,
        "observed_at": observed_at, "prev_hash": prev_hash,
    }
    fields["event_hash"] = compute_event_hash(fields)
    return fields


def _chain(subject_id, values):
    """Build a linked per-subject chain from a list of values."""
    events, prev = [], GENESIS_HASH
    for i, v in enumerate(values):
        ev = _event(subject_id, "assert", v, prev, observed_at=f"2026-01-01 00:00:0{i}.000000")
        ev["event_id"] = 1000 + i
        events.append(ev)
        prev = ev["event_hash"]
    return events


# --------------------------------------------------------------------------
# hashing / chain
# --------------------------------------------------------------------------
def test_hash_is_deterministic_and_value_sensitive():
    a = _event(1, "assert", {"x": 1}, GENESIS_HASH)
    b = _event(1, "assert", {"x": 1}, GENESIS_HASH)
    c = _event(1, "assert", {"x": 2}, GENESIS_HASH)
    assert a["event_hash"] == b["event_hash"], "same fields must hash identically"
    assert a["event_hash"] != c["event_hash"], "different value must change the hash"


def test_temporal_fields_are_hashed():
    # valid_from/valid_to/asserted_at are material claims -> committed to the chain.
    base = {
        "subject_id": 1, "tenant_id": "t1", "op": "assert", "value": {"x": 1},
        "source": "s", "confidence": "0.900", "agent_id": "a", "session_id": "sess",
        "tool_turn": 1, "supersedes_event_id": None,
        "observed_at": "2026-01-01 00:00:00.000000", "prev_hash": GENESIS_HASH,
    }
    h_none = compute_event_hash(base)                      # no validity window
    h_vf = compute_event_hash({**base, "valid_from": "2026-01-01 00:00:00.000000"})
    h_at = compute_event_hash({**base, "asserted_at": "2026-01-02 00:00:00.000000"})
    assert h_none != h_vf, "valid_from must change the hash"
    assert h_none != h_at, "asserted_at must change the hash"
    # Back-compat: an event dict WITHOUT temporal keys still hashes deterministically.
    assert compute_event_hash(base) == h_none


def test_valid_chain_verifies():
    ok, errors = verify_chain(_chain(7, [{"v": 1}, {"v": 2}, {"v": 3}]))
    assert ok, errors


def test_tampered_value_breaks_content_integrity():
    events = _chain(7, [{"v": 1}, {"v": 2}, {"v": 3}])
    events[1]["value"] = {"v": 999}  # mutate in place without re-hashing
    ok, errors = verify_chain(events)
    assert not ok and any("content hash mismatch" in e for e in errors), errors


def test_broken_link_breaks_structural_integrity():
    events = _chain(7, [{"v": 1}, {"v": 2}, {"v": 3}])
    events[2]["prev_hash"] = "f" * 64  # detach the last event
    ok, errors = verify_chain(events)
    assert not ok, "a severed link must fail verification"


def test_empty_chain_is_trivially_valid():
    ok, errors = verify_chain([])
    assert ok and not errors


# --------------------------------------------------------------------------
# adjudication — every value_type branch + authority gate
# --------------------------------------------------------------------------
def test_first_assert_creates():
    d = adjudicate(value_type="scalar", incoming_value="Pro", current_value=None,
                   has_current=False, incoming_rank=40, current_rank=-1)
    assert d.kind == "assert_new" and d.op == "assert" and d.repoint_current


def test_scalar_equal_corroborates():
    d = adjudicate(value_type="scalar", incoming_value="Pro", current_value="Pro",
                   has_current=True, incoming_rank=40, current_rank=100)
    assert d.kind == "corroborate" and d.op == "assert" and not d.repoint_current


def test_scalar_higher_authority_supersedes():
    d = adjudicate(value_type="scalar", incoming_value="Enterprise", current_value="Pro",
                   has_current=True, incoming_rank=100, current_rank=60)
    assert d.kind == "supersede" and d.op == "supersede" and d.repoint_current
    assert d.new_current_value == "Enterprise"


def test_scalar_equal_authority_disputes_no_recency_supersede():
    d = adjudicate(value_type="scalar", incoming_value="Enterprise", current_value="Pro",
                   has_current=True, incoming_rank=60, current_rank=60)
    assert d.kind == "dispute" and d.op == "assert" and not d.repoint_current and d.disputed


def test_scalar_lower_authority_rejected_not_disputed():
    # A strictly lower-authority contradiction is REJECTED (retained as contrary
    # evidence), NOT disputed — the higher-authority winner is not destabilized.
    d = adjudicate(value_type="scalar", incoming_value="Enterprise", current_value="Pro",
                   has_current=True, incoming_rank=40, current_rank=100)
    assert d.kind == "rejected", d.kind
    assert d.op == "assert" and not d.repoint_current
    assert not d.disputed, "lower-authority contradiction must not create a dispute"
    assert d.new_status is None, "current resolution must be left untouched"
    assert d.outcome == "REJECTED", d.outcome


def test_monotonic_later_supersedes():
    d = adjudicate(value_type="monotonic", incoming_value="2026-02-01", current_value="2026-01-01",
                   has_current=True, incoming_rank=40, current_rank=100)
    # recency legitimately supersedes for monotonic — even against a higher-rank source
    assert d.kind == "update" and d.op == "supersede" and d.repoint_current


def test_monotonic_earlier_is_stale_not_superseding():
    d = adjudicate(value_type="monotonic", incoming_value="2025-12-01", current_value="2026-01-01",
                   has_current=True, incoming_rank=100, current_rank=40)
    assert d.kind == "stale" and d.op == "assert" and not d.repoint_current


def test_monotonic_equal_corroborates():
    d = adjudicate(value_type="monotonic", incoming_value="2026-01-01", current_value="2026-01-01",
                   has_current=True, incoming_rank=40, current_rank=40)
    assert d.kind == "corroborate"


def test_set_new_member_unions():
    d = adjudicate(value_type="set", incoming_value=["b"], current_value=["a"],
                   has_current=True, incoming_rank=40, current_rank=40)
    assert d.kind == "set_extend" and d.op == "assert" and d.repoint_current
    assert sorted(d.new_current_value) == ["a", "b"]


def test_set_subset_corroborates():
    d = adjudicate(value_type="set", incoming_value=["a"], current_value=["a", "b"],
                   has_current=True, incoming_rank=40, current_rank=40)
    assert d.kind == "corroborate" and not d.repoint_current


def test_freetext_identical_corroborates():
    d = adjudicate(value_type="freetext", incoming_value="hi", current_value="hi",
                   has_current=True, incoming_rank=40, current_rank=40)
    assert d.kind == "corroborate"


def test_freetext_differ_disputes():
    d = adjudicate(value_type="freetext", incoming_value="hello", current_value="hi",
                   has_current=True, incoming_rank=100, current_rank=40)
    # even a higher-rank source does not auto-supersede free text
    assert d.kind == "dispute" and not d.repoint_current and d.disputed


# --------------------------------------------------------------------------
# adjudication outcome labels (agent-facing)
# --------------------------------------------------------------------------
def test_outcome_labels():
    assert adjudicate(value_type="scalar", incoming_value="a", current_value=None,
                      has_current=False, incoming_rank=40, current_rank=-1).outcome == "ASSERTED"
    assert adjudicate(value_type="scalar", incoming_value="a", current_value="a",
                      has_current=True, incoming_rank=40, current_rank=40).outcome == "CORROBORATED"
    assert adjudicate(value_type="scalar", incoming_value="b", current_value="a",
                      has_current=True, incoming_rank=100, current_rank=40).outcome == "SUPERSEDED"
    assert adjudicate(value_type="scalar", incoming_value="b", current_value="a",
                      has_current=True, incoming_rank=60, current_rank=60).outcome == "DISPUTED"
    assert adjudicate(value_type="scalar", incoming_value="b", current_value="a",
                      has_current=True, incoming_rank=20, current_rank=95).outcome == "REJECTED"


# --------------------------------------------------------------------------
# predicate-specific, versioned authority selection (pure)
# --------------------------------------------------------------------------
_POLICY_ROWS = [
    {"predicate": "*", "source": "user_assertion", "authority": 60, "priority": 0,
     "policy_version": "authority-v1", "effective_from": "1970-01-01 00:00:00.000000",
     "effective_to": None, "tenant_id": "*"},
    {"predicate": "fraud_status", "source": "user_assertion", "authority": 20, "priority": 1,
     "policy_version": "fraud-policy-v1", "effective_from": "1970-01-01 00:00:00.000000",
     "effective_to": None, "tenant_id": "*"},
    {"predicate": "fraud_status", "source": "fraud_review", "authority": 95, "priority": 10,
     "policy_version": "fraud-policy-v1", "effective_from": "1970-01-01 00:00:00.000000",
     "effective_to": None, "tenant_id": "*"},
]


def test_authority_exact_predicate_beats_wildcard():
    # user_assertion is 60 globally but only 20 for fraud_status — the specific policy wins.
    m = select_authority(_POLICY_ROWS, predicate="fraud_status", source="user_assertion")
    assert m.matched and m.authority == 20 and m.policy_version == "fraud-policy-v1"


def test_authority_falls_back_to_wildcard():
    # A predicate with no specific policy uses the wildcard (legacy) authority.
    m = select_authority(_POLICY_ROWS, predicate="status", source="user_assertion")
    assert m.matched and m.authority == 60 and m.policy_version == "authority-v1"


def test_authority_no_match_is_lowest_and_unversioned():
    m = select_authority(_POLICY_ROWS, predicate="fraud_status", source="unknown_source")
    assert not m.matched and m.authority == 0 and m.policy_version == "unversioned"


def test_authority_respects_effective_window():
    rows = [
        {"predicate": "status", "source": "s", "authority": 50, "priority": 0,
         "policy_version": "v1", "effective_from": "2026-01-01 00:00:00.000000",
         "effective_to": "2026-06-01 00:00:00.000000", "tenant_id": "*"},
        {"predicate": "status", "source": "s", "authority": 90, "priority": 0,
         "policy_version": "v2", "effective_from": "2026-06-01 00:00:00.000000",
         "effective_to": None, "tenant_id": "*"},
    ]
    # As-of inside the v1 window resolves to v1; after the cutover resolves to v2.
    early = select_authority(rows, predicate="status", source="s", as_of="2026-03-01 00:00:00.000000")
    late = select_authority(rows, predicate="status", source="s", as_of="2026-09-01 00:00:00.000000")
    assert early.policy_version == "v1" and early.authority == 50
    assert late.policy_version == "v2" and late.authority == 90


def test_authority_tenant_specific_beats_wildcard():
    rows = [
        {"predicate": "status", "source": "s", "authority": 50, "priority": 0,
         "policy_version": "v1", "effective_from": "1970-01-01 00:00:00.000000",
         "effective_to": None, "tenant_id": "*"},
        {"predicate": "status", "source": "s", "authority": 80, "priority": 0,
         "policy_version": "v1-tenantA", "effective_from": "1970-01-01 00:00:00.000000",
         "effective_to": None, "tenant_id": "tenantA"},
    ]
    m = select_authority(rows, predicate="status", source="s", tenant_id="tenantA")
    assert m.authority == 80 and m.policy_version == "v1-tenantA"
    other = select_authority(rows, predicate="status", source="s", tenant_id="tenantB")
    assert other.authority == 50 and other.policy_version == "v1"


# --------------------------------------------------------------------------
# canonical entity identity + evidence relationships (pure)
# --------------------------------------------------------------------------
def test_canonical_key_is_deterministic_and_normalized():
    # Same real-world id in different representations -> same canonical key.
    a = build_canonical_key("customer", "  John@Example.com ")
    b = build_canonical_key("Customer", "john@example.com")
    assert a == b == "customer:john@example.com"


def test_canonical_key_distinct_types_do_not_collide():
    assert build_canonical_key("device", "123") != build_canonical_key("card", "123")


def test_relationship_validation():
    assert normalize_relationship(None) == "SUPPORTS"       # default
    assert normalize_relationship("contradicts") == "CONTRADICTS"
    try:
        normalize_relationship("bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid relationship must raise")


def test_assessor_type_validation():
    assert normalize_assessor_type(None) is None
    assert normalize_assessor_type("Human") == "human"
    try:
        normalize_assessor_type("robot")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid assessor_type must raise")


# --------------------------------------------------------------------------
# write-control floor — the admission gate that runs BEFORE adjudication
# --------------------------------------------------------------------------
def test_write_control_rejects_sub_floor_confidence():
    # Just under the default floor -> rejected, naming both floor and value.
    try:
        enforce_write_control_floor(DEFAULT_MIN_CONFIDENCE - 0.01)
    except ValueError as e:
        msg = str(e)
        assert str(DEFAULT_MIN_CONFIDENCE) in msg, msg
        assert "0.84" in msg, msg
    else:
        raise AssertionError("sub-floor confidence must be rejected")


def test_write_control_allows_at_or_above_floor():
    # At the floor and above -> returned unchanged, never clamped.
    assert enforce_write_control_floor(DEFAULT_MIN_CONFIDENCE) == DEFAULT_MIN_CONFIDENCE
    assert enforce_write_control_floor(0.99) == 0.99


def test_write_control_env_override():
    prev = os.environ.get("WRITE_CONTROL_MIN_CONFIDENCE")
    os.environ["WRITE_CONTROL_MIN_CONFIDENCE"] = "0.5"
    try:
        # 0.6 clears a lowered floor that would fail against the default.
        assert enforce_write_control_floor(0.6) == 0.6
    finally:
        if prev is None:
            del os.environ["WRITE_CONTROL_MIN_CONFIDENCE"]
        else:
            os.environ["WRITE_CONTROL_MIN_CONFIDENCE"] = prev


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
