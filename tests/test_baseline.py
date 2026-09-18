"""Offline tests for the schema_migrations baseline operation (deploy/migrate.py).

No live DB and no AWS. A fake executor stands in for TiDB and we construct
verify_schema.Report objects directly (the same objects the read-only verifier
produces) so we can assert the baseline decision logic:

  * baseline is allowed only after every migration MATCHes
  * refusal on any DIFFER / UNKNOWN / unexpected object / conflicting row
  * exactly the seven migrations are recorded (03_roles.sql excluded)
  * checksums recorded correspond to the current file contents
  * idempotent repeat (already-present rows are not re-inserted)
  * checksum drift is rejected
  * NOTHING is written without APPLY_FOR_REAL=1

    python3 tests/test_baseline.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

import migrate  # noqa: E402
import verify_schema as vs  # noqa: E402
from migrate import (  # noqa: E402
    BASELINE_FILES, discover_migrations, plan_baseline, run_baseline,
)

SQL_DIR = migrate.DEFAULT_SQL_DIR
CORE = discover_migrations(SQL_DIR)                 # the seven, roles excluded
CORE_CK = {m.name: m.checksum for m in CORE}


# --------------------------------------------------------------------------
# fakes / builders
# --------------------------------------------------------------------------
class FakeExecutor:
    """Records baseline writes instead of touching a DB."""
    def __init__(self, existing_rows=None):
        # existing_rows: list of (filename, checksum, applied_at)
        self._rows = list(existing_rows or [])
        self.ensured = False
        self.inserted: list[tuple[str, str]] = []
        self.fail_on_insert = False

    def ensure_tracking(self):
        self.ensured = True

    def baseline_insert(self, rows):
        if self.fail_on_insert:
            raise RuntimeError("boom")
        # atomic: only commit to the fake store if none raise
        for name, ck in rows:
            self.inserted.append((name, ck))
            self._rows.append((name, ck, "2026-09-17 12:00:00.000000"))

    def applied_rows(self):
        return list(self._rows)


def _report(*, differ=(), unknown=(), unexpected_tables=(), unexpected_columns=(),
            unexpected_seed_rows=(), sm_present=False, sm_rows=(),
            null_entity=0, tiflash_fact_event=1) -> vs.Report:
    """Build a verify_schema.Report with the seven migrations, defaulting to all MATCH."""
    migs = []
    for m in vs.MIGRATIONS:
        mr = vs.MigrationResult(m["num"], m["name"], m["headline"])
        if m["num"] in differ:
            mr.findings.append(vs.Finding(vs.DIFFER, f"table {m['name']}", "present", "absent"))
        elif m["num"] in unknown:
            mr.findings.append(vs.Finding(vs.UNKNOWN, f"table {m['name']}", "present", "?"))
        else:
            mr.findings.append(vs.Finding(vs.MATCH, f"table {m['name']}"))
        migs.append(mr)
    rep = vs.Report(migrations=migs)
    rep.unexpected_tables = list(unexpected_tables)
    rep.unexpected_columns = list(unexpected_columns)
    rep.unexpected_seed_rows = list(unexpected_seed_rows)
    rep.schema_migrations_present = sm_present
    rep.schema_migrations_rows = list(sm_rows)
    rep.backfill = {"null_entity_id": null_entity, "total": 11}
    rep.tiflash_counts = {"fact_event": tiflash_fact_event}
    return rep


def _quiet():
    return lambda *a, **k: None


# --------------------------------------------------------------------------
# happy path: all MATCH, empty schema_migrations -> plan records seven, excludes 03
# --------------------------------------------------------------------------
def test_plan_records_exactly_seven_and_excludes_03():
    plan = plan_baseline(_report(sm_present=True, sm_rows=()), CORE, {})
    assert plan.allowed
    names = [n for n, _ in plan.to_insert]
    assert names == list(BASELINE_FILES)
    assert len(names) == 7
    assert "03_roles.sql" not in names
    assert plan.excluded == ["03_roles.sql"]


def test_recorded_checksums_match_current_files():
    plan = plan_baseline(_report(sm_present=True), CORE, {})
    for name, ck in plan.to_insert:
        assert ck == CORE_CK[name], f"{name} checksum must match current file"
        assert len(ck) == 64


# --------------------------------------------------------------------------
# refusals: DIFFER / UNKNOWN / unexpected objects
# --------------------------------------------------------------------------
def test_refuse_when_verification_differs():
    plan = plan_baseline(_report(differ=(7,)), CORE, {})
    assert not plan.allowed
    assert any("DIFFER" in r for r in plan.refusals)
    assert plan.to_insert  # computed, but allowed is False so nothing is written


def test_refuse_when_verification_unknown():
    plan = plan_baseline(_report(unknown=(6,)), CORE, {})
    assert not plan.allowed
    assert any("UNKNOWN" in r for r in plan.refusals)


def test_refuse_on_unexpected_objects():
    p1 = plan_baseline(_report(unexpected_tables=("legacy_scratch",)), CORE, {})
    p2 = plan_baseline(_report(unexpected_columns=("fact_event.debug",)), CORE, {})
    p3 = plan_baseline(_report(unexpected_seed_rows=("predicate_rule[predicate=x]",)), CORE, {})
    assert not p1.allowed and not p2.allowed and not p3.allowed


# --------------------------------------------------------------------------
# conflicting existing rows -> checksum drift -> refuse
# --------------------------------------------------------------------------
def test_refuse_on_checksum_drift():
    existing = {"01_schema.sql": "deadbeef" * 8}  # wrong checksum recorded
    plan = plan_baseline(_report(sm_present=True, sm_rows=("01_schema.sql",)), CORE, existing)
    assert not plan.allowed
    assert any("drift" in r.lower() for r in plan.refusals)
    assert [n for n, _, _ in plan.drift] == ["01_schema.sql"]


def test_refuse_on_unknown_existing_row():
    existing = {"99_mystery.sql": "a" * 64}
    plan = plan_baseline(_report(sm_present=True, sm_rows=("99_mystery.sql",)), CORE, existing)
    assert not plan.allowed
    assert any("unrelated" in r or "unknown" in r for r in plan.refusals)


def test_existing_03_row_is_compatible_not_a_refusal():
    # 03 recorded already is fine (it is a known, intentionally-excluded migration).
    existing = {"03_roles.sql": "b" * 64}
    plan = plan_baseline(_report(sm_present=True, sm_rows=("03_roles.sql",)), CORE, existing)
    assert plan.allowed
    assert plan.other_existing == ["03_roles.sql"]
    assert [n for n, _ in plan.to_insert] == list(BASELINE_FILES)


# --------------------------------------------------------------------------
# idempotency: all seven already recorded with matching checksums
# --------------------------------------------------------------------------
def test_idempotent_when_all_seven_already_present():
    existing = dict(CORE_CK)  # all seven, correct checksums
    plan = plan_baseline(_report(sm_present=True, sm_rows=tuple(BASELINE_FILES)), CORE, existing)
    assert plan.allowed
    assert plan.to_insert == []
    assert len(plan.already) == 7


def test_idempotent_repeat_writes_nothing():
    existing_rows = [(n, CORE_CK[n], "2026-09-17 12:00:00") for n in BASELINE_FILES]
    ex = FakeExecutor(existing_rows=existing_rows)
    rc = run_baseline(_report(sm_present=True, sm_rows=tuple(BASELINE_FILES)),
                      CORE, dict(CORE_CK), ex, apply_for_real=True, out=_quiet())
    assert rc == 0
    assert ex.inserted == [], "a second baseline must not insert duplicate rows"


# --------------------------------------------------------------------------
# no mutation without APPLY_FOR_REAL=1
# --------------------------------------------------------------------------
def test_no_write_without_apply_for_real():
    ex = FakeExecutor()
    rc = run_baseline(_report(sm_present=True), CORE, {}, ex,
                      apply_for_real=False, out=_quiet())
    assert rc == 0
    assert ex.inserted == [] and ex.ensured is False, "dry-run must not touch the DB"


def test_write_happens_with_apply_for_real():
    ex = FakeExecutor()
    rc = run_baseline(_report(sm_present=True), CORE, {}, ex,
                      apply_for_real=True, out=_quiet())
    assert rc == 0
    assert ex.ensured is True
    assert [n for n, _ in ex.inserted] == list(BASELINE_FILES)
    # checksums written are the current-file checksums
    for name, ck in ex.inserted:
        assert ck == CORE_CK[name]


def test_refusal_writes_nothing_even_with_apply_for_real():
    ex = FakeExecutor()
    rc = run_baseline(_report(differ=(1,)), CORE, {}, ex,
                      apply_for_real=True, out=_quiet())
    assert rc == 6
    assert ex.inserted == [] and ex.ensured is False


def test_insert_failure_is_reported_and_nonzero():
    ex = FakeExecutor()
    ex.fail_on_insert = True
    rc = run_baseline(_report(sm_present=True), CORE, {}, ex,
                      apply_for_real=True, out=_quiet())
    assert rc == 1
    assert ex.inserted == []


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
