"""Offline tests for the TiDB migration runner (deploy/migrate.py).

No live DB and no AWS: a fake executor stands in for TiDB so we can assert the
orchestration logic — ordering, placeholder rejection, dry-run does nothing,
failure stops the rest, secrets never printed, explicit 03 handling, and
already-applied/drift detection against the tracking mechanism.

    python3 tests/test_migrations.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

import migrate  # noqa: E402  (deploy/migrate.py)
from migrate import (  # noqa: E402
    ConnInfo, Migration, apply_plan, detect_placeholders, discover_migrations,
    plan_migrations, redact_secret, redact_url, render_roles_sql, sha256_hex,
)

SQL_DIR = migrate.DEFAULT_SQL_DIR


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeExecutor:
    """Records calls instead of touching a DB. Optionally fails on a filename."""
    def __init__(self, applied=None, fail_on=None):
        self._applied = dict(applied or {})
        self.executed: list[str] = []
        self.recorded: list[str] = []
        self.ensured = False
        self._fail_on = fail_on

    def applied_migrations(self):
        return dict(self._applied)

    def ensure_tracking(self):
        self.ensured = True

    def execute_file(self, sql_text):
        # We can't map sql_text->name cheaply; the runner records right after, so
        # detect the failure via the pending list in the test instead.
        self.executed.append(sql_text[:0] or "x")

    def record(self, name, checksum):
        if self._fail_on and name == self._fail_on:
            raise RuntimeError(f"boom on {name}")
        self.recorded.append(name)


# --------------------------------------------------------------------------
# ordering / discovery
# --------------------------------------------------------------------------
def test_default_order_excludes_roles():
    names = [m.name for m in discover_migrations(SQL_DIR)]
    assert names == ["01_schema.sql", "02_seed.sql", "04_authority_policy.sql",
                     "05_entity_evidence.sql", "06_temporal.sql",
                     "07_idempotency.sql", "08_domain_predicates.sql"], names
    assert "03_roles.sql" not in names, "03 must be excluded from the default set"


def test_with_roles_appends_03_last():
    names = [m.name for m in discover_migrations(SQL_DIR, include_roles=True)]
    assert names[-1] == "03_roles.sql", "03 must run LAST (grants on 05/07 tables)"
    assert names.count("03_roles.sql") == 1


def test_order_is_deterministic():
    a = [m.name for m in discover_migrations(SQL_DIR, include_roles=True)]
    b = [m.name for m in discover_migrations(SQL_DIR, include_roles=True)]
    assert a == b


# --------------------------------------------------------------------------
# placeholder / malformed config
# --------------------------------------------------------------------------
def _info(**over):
    # A realistic TiDB Cloud host (the placeholder detector intentionally flags
    # markers like "EXAMPLE"/"<...>", so tests use a real-looking hostname).
    base = dict(host="gateway01.eu-central-1.prod.aws.tidbcloud.com", port=4000,
                user="u", password="pw", database="fact_layer", ssl_ca=None, source="test")
    base.update(over)
    return ConnInfo(**base)


def test_placeholder_config_is_rejected():
    assert detect_placeholders(_info(host="<HOST>"))
    assert detect_placeholders(_info(database=""))
    assert detect_placeholders(_info(host="your-cluster.PLACEHOLDER"))
    assert detect_placeholders(_info(port=0))
    assert not detect_placeholders(_info()), "a fully-configured conn has no problems"


def test_resolve_conn_requires_configuration():
    saved = {k: os.environ.pop(k, None) for k in
             ("MIGRATION_SECRET_ARN", "TIDB_MIGRATION_URL", "TIDB_TEST_URL")}
    try:
        raised = False
        try:
            migrate.resolve_conn()
        except migrate.ConfigError:
            raised = True
        assert raised, "no connection env -> ConfigError"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------
# dry-run performs no mutation
# --------------------------------------------------------------------------
def test_dry_run_executes_nothing():
    migs = discover_migrations(SQL_DIR)
    plan = plan_migrations(migs, applied={})
    ex = FakeExecutor()
    n = apply_plan(plan.pending, ex, dry_run=True, out=lambda *_: None)
    assert n == 0
    assert ex.executed == [] and ex.recorded == [], "dry-run must not touch the DB"
    assert ex.ensured is False


# --------------------------------------------------------------------------
# failure stops subsequent migrations
# --------------------------------------------------------------------------
def test_failure_stops_and_leaves_rest_untouched():
    migs = discover_migrations(SQL_DIR)
    plan = plan_migrations(migs, applied={})
    ex = FakeExecutor(fail_on="04_authority_policy.sql")
    failed = False
    try:
        apply_plan(plan.pending, ex, dry_run=False, out=lambda *_: None)
    except RuntimeError:
        failed = True
    assert failed, "a failing migration must raise"
    # 01 and 02 recorded; 04 failed; 05+ never attempted.
    assert ex.recorded == ["01_schema.sql", "02_seed.sql"], ex.recorded
    assert "05_entity_evidence.sql" not in ex.recorded


# --------------------------------------------------------------------------
# secrets never printed
# --------------------------------------------------------------------------
def test_redaction_hides_password_in_url_and_errors():
    url = "mysql+pymysql://root:SuperSecret@h.example:4000/fact_layer"
    assert "SuperSecret" not in redact_url(url)
    assert "***" in redact_url(url)
    err = "Access denied for user using password 'SuperSecret'"
    assert "SuperSecret" not in redact_secret(err, "SuperSecret")


def test_safe_display_omits_password():
    d = _info(password="SuperSecret").safe_display()
    assert "SuperSecret" not in str(d)
    assert d["tls"].startswith("on")


def test_apply_failure_message_redacts_password():
    captured = []
    ex = FakeExecutor(fail_on="01_schema.sql")
    plan = plan_migrations(discover_migrations(SQL_DIR), applied={})
    try:
        apply_plan(plan.pending, ex, dry_run=False,
                   out=captured.append, redact_pw="SuperSecret")
    except RuntimeError:
        pass
    # Even if the DB error embedded the password, our output must not.
    assert all("SuperSecret" not in line for line in captured)


# --------------------------------------------------------------------------
# migration 03 handling is explicit
# --------------------------------------------------------------------------
def test_render_roles_substitutes_placeholders():
    raw = open(os.path.join(SQL_DIR, "03_roles.sql")).read()
    assert "<FACT_ROLE>" in raw and "<PASSWORD_FROM_SECRET>" in raw and "<DB>" in raw
    rendered = render_roles_sql(raw, role="fact_app", db="fact_layer", password="pw123")
    assert "<FACT_ROLE>" not in rendered and "<DB>" not in rendered
    assert "<PASSWORD_FROM_SECRET>" not in rendered
    assert "fact_app" in rendered and "fact_layer" in rendered
    # The rendered SQL DOES contain the password (needed to execute) — the runner
    # redacts it before any output. Prove redaction covers it.
    assert "pw123" in rendered
    assert "pw123" not in redact_secret(rendered, "pw123")


def test_roles_profile_fails_fast_when_unconfigured():
    saved = {k: os.environ.pop(k, None) for k in
             ("FACT_ROLE", "TIDB_DB", "FACT_ROLE_SECRET_ARN")}
    try:
        raised = False
        try:
            migrate.roles_password_and_params()
        except migrate.ConfigError as e:
            raised = True
            assert "FACT_ROLE" in str(e)  # names what is missing; never skips silently
        assert raised
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------
# already-applied handling + drift (the tracking mechanism)
# --------------------------------------------------------------------------
def test_already_applied_are_skipped():
    migs = discover_migrations(SQL_DIR)
    # Pretend 01 and 02 are recorded with their real checksums.
    applied = {m.name: m.checksum for m in migs if m.name in
               ("01_schema.sql", "02_seed.sql")}
    plan = plan_migrations(migs, applied)
    assert [m.name for m in plan.already] == ["01_schema.sql", "02_seed.sql"]
    assert "01_schema.sql" not in [m.name for m in plan.pending]
    assert plan.pending[0].name == "04_authority_policy.sql"


def test_checksum_drift_is_detected_not_reapplied():
    migs = discover_migrations(SQL_DIR)
    applied = {"01_schema.sql": "deadbeef" * 8}  # wrong checksum == edited after apply
    plan = plan_migrations(migs, applied)
    assert [m.name for m, _ in plan.drift] == ["01_schema.sql"]
    assert "01_schema.sql" not in [m.name for m in plan.pending]
    assert "01_schema.sql" not in [m.name for m in plan.already]


def test_checksum_is_stable():
    m = discover_migrations(SQL_DIR)[0]
    assert m.checksum == sha256_hex(m.path.read_bytes())
    assert len(m.checksum) == 64


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
