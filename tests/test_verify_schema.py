"""Offline tests for the read-only schema verifier (deploy/verify_schema.py).

No live DB and no AWS: we hand-build introspection snapshots (the same shape
verify_schema.inspect() would return from TiDB) and assert build_report()'s
migration-by-migration MATCH / DIFFER / UNKNOWN verdicts, the unexpected-object
detection, and the baseline-safe decision.

    python3 tests/test_verify_schema.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

import verify_schema as vs  # noqa: E402
from verify_schema import (  # noqa: E402
    DIFFER, MATCH, UNKNOWN, build_report, normalize_default, normalize_type,
)


# --------------------------------------------------------------------------
# A snapshot that EXACTLY matches the expected migration set — the golden case.
# Built programmatically from the same spec so it stays in lockstep.
# --------------------------------------------------------------------------
def _golden_snapshot() -> dict:
    columns: dict = {}
    pk: dict = {}
    indexes: dict = {}
    for m in vs.MIGRATIONS:
        for c in m["columns"]:
            columns.setdefault(c.table, {})[c.name] = {
                "type": c.type,
                "nullable": c.nullable,
                "default": c.default,
                "auto_random": c.auto_random,
            }
        for ix in m["indexes"]:
            if ix.name == "PRIMARY":
                pk[ix.table] = list(ix.columns)
            else:
                indexes.setdefault(ix.table, {})[ix.name] = {
                    "columns": tuple(ix.columns), "unique": ix.unique}

    seed_rows: dict = {}
    for m in vs.MIGRATIONS:
        for s in m["seeds"]:
            seed_rows.setdefault(s.table, [])
            for r in s.rows:
                seed_rows[s.table].append(dict(r))

    tables = set(vs.EXPECTED_TABLES)
    return {
        "tables": tables,
        "columns": columns,
        "pk": pk,
        "indexes": indexes,
        "tiflash": {"fact_event": {"replica_count": 1, "available": True}},
        "vector_columns": {"fact_subject": {"subject_vec": 1024}},
        "vector_indexes": {"fact_subject": {"idx_subject_vec"}},
        "seed_rows": seed_rows,
        "row_counts": {"fact_subject": 3, "fact_event": 9},
        "fact_subject_null_entity": 0,
        "errors": [],
    }


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------
def test_normalize_type_strips_int_width_keeps_others():
    assert normalize_type("bigint(20)") == "bigint"
    assert normalize_type("INT(11)") == "int"
    assert normalize_type("varchar(64)") == "varchar(64)"
    assert normalize_type("decimal(4,3)") == "decimal(4,3)"
    assert normalize_type("vector(1024)") == "vector(1024)"
    assert normalize_type("enum('a','b')") == "enum('a','b')"


def test_normalize_default_strips_quotes():
    assert normalize_default("'active'") == "active"
    assert normalize_default("*") == "*"
    assert normalize_default(None) == ""
    assert normalize_default(0) == "0"


# --------------------------------------------------------------------------
# golden snapshot -> everything MATCH, baseline safe
# --------------------------------------------------------------------------
def test_golden_snapshot_all_match_and_baseline_safe():
    rep = build_report(_golden_snapshot())
    for m in rep.migrations:
        bad = [f for f in m.findings if f.status != MATCH]
        assert m.status == MATCH, f"migration {m.num} {m.status}: {bad}"
    assert rep.unexpected_tables == []
    assert rep.unexpected_columns == []
    assert rep.unexpected_seed_rows == []
    assert rep.schema_migrations_present is False
    assert rep.baseline_safe is True
    assert rep.schema_equivalent is True
    # render must not raise and should state EQUIVALENT + a clean-baseline path
    out = vs.render_report(rep)
    assert "EQUIVALENT" in out
    assert "baseline can be introduced" in out


# --------------------------------------------------------------------------
# the ACTUAL live situation: schema matches, schema_migrations EXISTS but EMPTY
# (left by the failed apply). Schema is equivalent; a clean "introduce" is not
# possible but baselining the empty table is.
# --------------------------------------------------------------------------
def test_empty_schema_migrations_is_equivalent_but_not_clean_introduce():
    snap = _golden_snapshot()
    snap["tables"].add("schema_migrations")
    snap["schema_migrations_rows"] = []   # exists but records nothing
    rep = build_report(snap)
    assert rep.schema_equivalent is True
    assert rep.schema_migrations_present is True
    assert rep.schema_migrations_rows == []
    assert rep.baseline_safe is False     # table already exists -> not a clean introduce
    out = vs.render_report(rep)
    assert "EMPTY" in out
    assert "SCHEMA EQUIVALENT" in out


# --------------------------------------------------------------------------
# a missing table -> that migration DIFFERs, baseline not safe
# --------------------------------------------------------------------------
def test_missing_table_flags_differ():
    snap = _golden_snapshot()
    snap["tables"].discard("fact_idempotency")
    snap["columns"].pop("fact_idempotency", None)
    snap["pk"].pop("fact_idempotency", None)
    rep = build_report(snap)
    m07 = next(m for m in rep.migrations if m.num == 7)
    assert m07.status == DIFFER
    assert any(f.object == "table fact_idempotency" and f.status == DIFFER
               for f in m07.findings)
    assert rep.baseline_safe is False


# --------------------------------------------------------------------------
# a wrong column type -> DIFFER with expected/actual detail
# --------------------------------------------------------------------------
def test_wrong_column_type_flags_differ_with_detail():
    snap = _golden_snapshot()
    snap["columns"]["fact_event"]["confidence"]["type"] = "decimal(10,2)"
    rep = build_report(snap)
    m01 = next(m for m in rep.migrations if m.num == 1)
    assert m01.status == DIFFER
    f = next(f for f in m01.findings if f.object == "fact_event.confidence type")
    assert "decimal(4,3)" in f.expected and "decimal(10,2)" in f.actual


# --------------------------------------------------------------------------
# int display width difference is NOT a false positive
# --------------------------------------------------------------------------
def test_int_display_width_is_not_a_false_diff():
    snap = _golden_snapshot()
    snap["columns"]["fact_event"]["subject_id"]["type"] = "bigint(20)"
    snap["columns"]["fact_event"]["tool_turn"]["type"] = "int(11)"
    rep = build_report(snap)
    m01 = next(m for m in rep.migrations if m.num == 1)
    assert m01.status == MATCH, [f for f in m01.findings if f.status != MATCH]


# --------------------------------------------------------------------------
# missing AUTO_RANDOM is a real difference
# --------------------------------------------------------------------------
def test_missing_auto_random_flags_differ():
    snap = _golden_snapshot()
    snap["columns"]["fact_subject"]["subject_id"]["auto_random"] = False
    rep = build_report(snap)
    m01 = next(m for m in rep.migrations if m.num == 1)
    assert m01.status == DIFFER
    assert any("AUTO_RANDOM" in f.object for f in m01.findings if f.status == DIFFER)


# --------------------------------------------------------------------------
# not-introspectable AUTO_RANDOM / vector / tiflash -> UNKNOWN, not MATCH
# --------------------------------------------------------------------------
def test_uninspectable_features_are_unknown_not_match():
    snap = _golden_snapshot()
    snap["columns"]["fact_subject"]["subject_id"]["auto_random"] = None
    snap["vector_columns"] = {}
    snap["vector_indexes"] = {}
    snap["tiflash"] = None
    rep = build_report(snap)
    m01 = next(m for m in rep.migrations if m.num == 1)
    assert m01.status == UNKNOWN
    assert rep.baseline_safe is False


# --------------------------------------------------------------------------
# missing seed row -> 02 DIFFERs
# --------------------------------------------------------------------------
def test_missing_seed_row_flags_differ():
    snap = _golden_snapshot()
    snap["seed_rows"]["predicate_rule"] = [
        r for r in snap["seed_rows"]["predicate_rule"] if r["predicate"] != "notes"]
    rep = build_report(snap)
    m02 = next(m for m in rep.migrations if m.num == 2)
    assert m02.status == DIFFER
    assert any("notes" in f.object and f.status == DIFFER for f in m02.findings)


# --------------------------------------------------------------------------
# wrong seed value (rank) -> DIFFER
# --------------------------------------------------------------------------
def test_wrong_seed_value_flags_differ():
    snap = _golden_snapshot()
    for r in snap["seed_rows"]["source_rank"]:
        if r["source"] == "system_of_record":
            r["rank"] = 999
    rep = build_report(snap)
    m02 = next(m for m in rep.migrations if m.num == 2)
    assert m02.status == DIFFER
    f = next(f for f in m02.findings if "system_of_record" in f.object)
    assert "999" in f.actual


# --------------------------------------------------------------------------
# extra registry row -> reported as unexpected, baseline not safe
# --------------------------------------------------------------------------
def test_extra_registry_row_is_unexpected():
    snap = _golden_snapshot()
    snap["seed_rows"]["predicate_rule"].append(
        {"predicate": "custom_live_pred", "value_type": "scalar"})
    rep = build_report(snap)
    assert any("custom_live_pred" in x for x in rep.unexpected_seed_rows)
    assert rep.baseline_safe is False


# --------------------------------------------------------------------------
# unexpected table + column detection
# --------------------------------------------------------------------------
def test_unexpected_table_and_column_detected():
    snap = _golden_snapshot()
    snap["tables"].add("legacy_scratch")
    snap["columns"]["fact_event"]["debug_note"] = {
        "type": "varchar(10)", "nullable": True, "default": None, "auto_random": False}
    rep = build_report(snap)
    assert "legacy_scratch" in rep.unexpected_tables
    assert "fact_event.debug_note" in rep.unexpected_columns
    assert rep.baseline_safe is False


# --------------------------------------------------------------------------
# schema_migrations already present -> flagged, baseline not "safe to introduce"
# --------------------------------------------------------------------------
def test_schema_migrations_present_blocks_baseline():
    snap = _golden_snapshot()
    snap["tables"].add("schema_migrations")
    rep = build_report(snap)
    assert rep.schema_migrations_present is True
    assert "schema_migrations" not in rep.unexpected_tables  # known, not "unexpected"
    assert rep.baseline_safe is False


# --------------------------------------------------------------------------
# incomplete 05 backfill is surfaced
# --------------------------------------------------------------------------
def test_backfill_incomplete_is_reported():
    snap = _golden_snapshot()
    snap["fact_subject_null_entity"] = 2
    snap["row_counts"]["fact_subject"] = 5
    rep = build_report(snap)
    assert rep.backfill["null_entity_id"] == 2
    out = vs.render_report(rep)
    assert "INCOMPLETE" in out


# --------------------------------------------------------------------------
# a totally empty snapshot (nothing inspected) -> all UNKNOWN, not MATCH
# --------------------------------------------------------------------------
def test_empty_snapshot_is_unknown_never_match():
    rep = build_report({"errors": ["could not connect"]})
    assert all(m.status in (UNKNOWN, DIFFER) for m in rep.migrations)
    assert not rep.all_match
    assert rep.baseline_safe is False


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
