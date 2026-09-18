#!/usr/bin/env python3
"""TiDB schema migration runner for the fact layer (operator-controlled).

Applies sql/01..sql/08 to the EXISTING TiDB cluster (eu-central-1). This is
DELIBERATELY independent of AWS/CDK deployment: it talks to TiDB over the MySQL
wire protocol, not to any AWS regional service, and it is NEVER invoked from
deploy/deploy.sh or the CDK. The operator runs it explicitly.

Design decisions (see DEPLOYMENT.md "Gate B — TiDB schema"):

* Tracking: the repo had NO migration-version table, and the migrations are only
  PARTIALLY idempotent (01/02 plain CREATE/INSERT; 04 has non-idempotent INSERTs
  into an AUTO_RANDOM table). So we add the smallest safe mechanism — a
  `schema_migrations` table recording each applied file + its sha256 — and apply
  a file at most once. State is read from that table, never inferred from whether
  tables happen to exist.

* SQL parsing: we do NOT split on ';'. Each file is sent whole to TiDB via a
  multi-statement connection, so the SERVER parses comments / quoted strings /
  multiline statements. (The files contain no DELIMITER blocks or semicolons
  inside string literals — verified — so whole-file execution is safe.)

* Migration 03 (roles/grants) is handled explicitly, never silently skipped: it
  has <FACT_ROLE>/<PASSWORD_FROM_SECRET>/<DB> placeholders AND grants on tables
  created by 05/07, so it is operator-specific and must run LAST. It is excluded
  from the default set and applied only with --with-roles (which renders the
  placeholders, pulling the role password from Secrets Manager — never argv/log).

* Credentials: resolved from Secrets Manager by ARN (MIGRATION_SECRET_ARN, the
  preferred path, reusing the repo's existing mechanism) or, for local/test use,
  a full URL env var (TIDB_MIGRATION_URL / TIDB_TEST_URL). No password ever
  appears in the repo, argv, or logs.

Modes (all read-only except --apply, which additionally requires APPLY_FOR_REAL=1):
    --check           validate config + report target/state; NO connection mutation
    --dry-run         + list the pending migrations that WOULD apply; no mutation
    --verify-existing inspect an EXISTING DB read-only and report, migration by
                      migration, whether it already matches sql/01,02,04-08. Never
                      creates schema_migrations, never writes. Answers "is this DB
                      equivalent to the migration set, so a baseline is safe?"
    --apply           create the tracking table if needed and apply pending migrations
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL_DIR = REPO_ROOT / "sql"
ROLES_FILE = "03_roles.sql"
TRACKING_TABLE = "schema_migrations"

# A migration filename is NN_name.sql (two-digit numeric prefix drives order).
_MIGRATION_RE = re.compile(r"^(\d{2})_.*\.sql$")
# Obvious unconfigured/placeholder markers in a resolved connection.
_PLACEHOLDER_MARKERS = ("<", ">", "PLACEHOLDER", "CHANGEME", "EXAMPLE", "YOUR-", "XXXX")


class ConfigError(Exception):
    """Raised for unconfigured / placeholder / missing connection settings."""


# ---------------------------------------------------------------------------
# Pure planning (no DB, no AWS — unit-testable)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Migration:
    name: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return sha256_hex(self.path.read_bytes())

    @property
    def number(self) -> int:
        return int(self.name[:2])


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_migrations(sql_dir: Path, *, include_roles: bool = False) -> list[Migration]:
    """Return migrations in deterministic numeric order.

    The core set is every NN_*.sql EXCEPT 03_roles.sql. When include_roles is
    set, 03 is appended LAST (it grants on tables created by 05/07, so it cannot
    run in strict numeric position).
    """
    files = []
    for p in sql_dir.iterdir():
        if _MIGRATION_RE.match(p.name):
            files.append(p)
    files.sort(key=lambda p: p.name)  # NN_ prefix => numeric == lexical order

    core = [Migration(p.name, p) for p in files if p.name != ROLES_FILE]
    if not include_roles:
        return core
    roles = [Migration(p.name, p) for p in files if p.name == ROLES_FILE]
    return core + roles  # roles LAST, after the core schema exists


@dataclass
class Plan:
    pending: list[Migration] = field(default_factory=list)
    already: list[Migration] = field(default_factory=list)
    drift: list[tuple[Migration, str]] = field(default_factory=list)  # (mig, recorded_checksum)


def plan_migrations(migrations: list[Migration], applied: dict[str, str]) -> Plan:
    """Diff the discovered migrations against the applied state.

    applied maps filename -> recorded checksum. A file already recorded with a
    matching checksum is skipped; a matching name with a DIFFERENT checksum is
    schema drift (an applied migration was edited) and is reported, not silently
    re-run.
    """
    plan = Plan()
    for m in migrations:
        if m.name in applied:
            if applied[m.name] == m.checksum:
                plan.already.append(m)
            else:
                plan.drift.append((m, applied[m.name]))
        else:
            plan.pending.append(m)
    return plan


# ---------------------------------------------------------------------------
# Baseline planning (record already-applied migrations) — pure, unit-testable
# ---------------------------------------------------------------------------
@dataclass
class BaselinePlan:
    """The decision + row set for baselining the existing schema_migrations table.

    `allowed` is True only when every safety gate passed. `to_insert` are the
    (filename, checksum) rows that would be written; `already` are the seven-set
    rows already present with a matching checksum (idempotent no-ops); `drift`
    are seven-set rows present with a DIFFERENT checksum (a hard failure);
    `other_existing` are recorded filenames outside the seven (e.g. 03_roles.sql).
    """
    allowed: bool = False
    refusals: list[str] = field(default_factory=list)
    to_insert: list[tuple[str, str]] = field(default_factory=list)
    already: list[tuple[str, str]] = field(default_factory=list)
    drift: list[tuple[str, str, str]] = field(default_factory=list)  # (name, recorded, expected)
    other_existing: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=lambda: [ROLES_FILE])


# The seven migrations this baseline records (03_roles.sql is intentionally NOT here).
BASELINE_FILES = ("01_schema.sql", "02_seed.sql", "04_authority_policy.sql",
                  "05_entity_evidence.sql", "06_temporal.sql", "07_idempotency.sql",
                  "08_domain_predicates.sql")


def plan_baseline(report, core_migrations: list[Migration],
                  existing_rows: dict[str, str]) -> BaselinePlan:
    """Decide whether the seven verified migrations can be baselined.

    Gated on the read-only verification `report` (a verify_schema.Report) AND on the
    existing schema_migrations contents. Refuses — writing nothing — if the schema
    is not proven equivalent, if unexpected objects exist, or if existing rows
    conflict. Never infers equivalence from table existence: the whole gate is the
    deep per-object verification carried in `report`.
    """
    plan = BaselinePlan()

    # --- Gate on verification (safety checks 3,4,5,6,8) ----------------------
    differ = [f"{m.num:02d}" for m in report.migrations if m.status == "DIFFER"]
    unknown = [f"{m.num:02d}" for m in report.migrations if m.status == "UNKNOWN"]
    if differ:
        plan.refusals.append(f"verification has DIFFER migrations: {', '.join(differ)}")
    if unknown:
        plan.refusals.append(f"verification has UNKNOWN migrations (could not verify): "
                             f"{', '.join(unknown)}")
    if report.unexpected_tables:
        plan.refusals.append("unexpected table(s) present: "
                             + ", ".join(report.unexpected_tables))
    if report.unexpected_columns:
        plan.refusals.append("unexpected column(s) present: "
                             + ", ".join(report.unexpected_columns))
    if report.unexpected_seed_rows:
        plan.refusals.append("unexpected registry/seed row(s) present: "
                             + ", ".join(report.unexpected_seed_rows))

    # --- Analyze existing schema_migrations rows (safety checks 2,7) ---------
    core_names = {m.name for m in core_migrations}
    for m in core_migrations:
        rec = existing_rows.get(m.name)
        if rec is None:
            plan.to_insert.append((m.name, m.checksum))
        elif rec == m.checksum:
            plan.already.append((m.name, m.checksum))
        else:
            plan.drift.append((m.name, rec, m.checksum))

    unknown_existing = []
    for fname in sorted(existing_rows):
        if fname not in core_names:
            plan.other_existing.append(fname)
            if fname != ROLES_FILE:  # 03 is a known, intentionally-excluded migration
                unknown_existing.append(fname)

    if plan.drift:
        plan.refusals.append(
            "checksum drift — schema_migrations already records a DIFFERENT checksum "
            "for: " + ", ".join(f"{n} (recorded {rec[:12]}… != expected {exp[:12]}…)"
                                 for n, rec, exp in plan.drift))
    if unknown_existing:
        plan.refusals.append(
            "schema_migrations contains unrelated/unknown migration rows: "
            + ", ".join(unknown_existing))

    plan.allowed = not plan.refusals
    return plan


# ---------------------------------------------------------------------------
# Secret-safe helpers
# ---------------------------------------------------------------------------
def redact_url(url: str) -> str:
    """Mask the password in a mysql URL for display."""
    return re.sub(r"(://[^:/@]+:)([^@]*)(@)", r"\1***\3", url)


def redact_secret(text: str, secret: str | None) -> str:
    """Remove a known secret substring from arbitrary text (errors, rendered SQL)."""
    if secret:
        text = text.replace(secret, "***")
    return text


def detect_placeholders(info: "ConnInfo") -> list[str]:
    """Return a list of obvious-placeholder problems in a resolved connection."""
    problems: list[str] = []
    if not info.host:
        problems.append("host is empty")
    if not info.database:
        problems.append("database is empty")
    if not info.user:
        problems.append("username is empty")
    for label, val in (("host", info.host), ("database", info.database),
                       ("username", info.user)):
        up = (val or "").upper()
        if any(mark in up for mark in _PLACEHOLDER_MARKERS):
            problems.append(f"{label} looks like a placeholder: {val!r}")
    if info.port in (0, None):
        problems.append("port is unset")
    return problems


# ---------------------------------------------------------------------------
# Connection configuration (Secrets Manager ARN preferred; URL for local/test)
# ---------------------------------------------------------------------------
@dataclass
class ConnInfo:
    host: str
    port: int
    user: str
    password: str          # never printed/logged
    database: str
    ssl_ca: str | None
    source: str            # human-readable, non-secret description of where creds came from

    def safe_display(self) -> dict:
        host = self.host or ""
        # Show enough of the host to identify the cluster without being cute;
        # the hostname is not a secret, but we never show the password.
        return {
            "host": host or "(unset)",
            "port": self.port,
            "database": self.database or "(unset)",
            "user": self.user or "(unset)",
            "tls": "on" + (" (pinned CA)" if self.ssl_ca else " (default trust store)"),
            "source": self.source,
        }


def resolve_conn() -> ConnInfo:
    """Resolve migration DB credentials. Priority:

    1. MIGRATION_SECRET_ARN  -> Secrets Manager (preferred; admin/DDL creds).
    2. TIDB_MIGRATION_URL    -> full mysql+pymysql URL (local/test).
    3. TIDB_TEST_URL         -> same, reusing the repo's existing test convention.
    Raises ConfigError if none are set.
    """
    arn = os.environ.get("MIGRATION_SECRET_ARN", "").strip()
    if arn:
        if any(m in arn.upper() for m in _PLACEHOLDER_MARKERS):
            raise ConfigError(f"MIGRATION_SECRET_ARN looks like a placeholder: {arn}")
        # Lazy import: only need boto3 on the ARN path (keeps URL/offline path light).
        sys.path.insert(0, str(REPO_ROOT))
        from handlers.common.secrets import get_secret  # noqa: E402
        creds = get_secret(arn)
        return ConnInfo(
            host=creds.get("host", ""), port=int(creds.get("port", 4000) or 4000),
            user=creds.get("username", ""), password=creds.get("password", ""),
            database=creds.get("database", ""), ssl_ca=creds.get("ssl_ca"),
            source=f"secret:{arn.split(':')[-1]}",
        )

    url = os.environ.get("TIDB_MIGRATION_URL") or os.environ.get("TIDB_TEST_URL")
    if url:
        from sqlalchemy.engine import make_url
        u = make_url(url)
        q = u.query or {}
        return ConnInfo(
            host=u.host or "", port=int(u.port or 4000),
            user=u.username or "", password=u.password or "",
            database=u.database or "", ssl_ca=q.get("ssl_ca"),
            source="env:TIDB_MIGRATION_URL" if os.environ.get("TIDB_MIGRATION_URL")
                   else "env:TIDB_TEST_URL",
        )

    raise ConfigError(
        "no migration DB connection configured — set MIGRATION_SECRET_ARN "
        "(preferred, Secrets Manager) or TIDB_MIGRATION_URL (local/test). "
        "Never put the password in the repo or on the command line."
    )


# ---------------------------------------------------------------------------
# Roles (03) rendering — explicit, secret-safe
# ---------------------------------------------------------------------------
def render_roles_sql(raw_sql: str, *, role: str, db: str, password: str) -> str:
    """Substitute 03_roles.sql placeholders. Returned SQL contains the real
    password (for execution only) — callers MUST redact it before any output."""
    return (raw_sql
            .replace("<FACT_ROLE>", role)
            .replace("<DB>", db)
            .replace("<PASSWORD_FROM_SECRET>", password))


def roles_password_and_params() -> tuple[str, str, str]:
    """Resolve (role, db, password) for --with-roles from env + Secrets Manager.

    Fails fast (never silently skips) if the profile is requested but unconfigured.
    """
    role = os.environ.get("FACT_ROLE", "").strip()
    db = os.environ.get("TIDB_DB", "").strip()
    secret_arn = os.environ.get("FACT_ROLE_SECRET_ARN", "").strip()
    missing = [n for n, v in (("FACT_ROLE", role), ("TIDB_DB", db),
                              ("FACT_ROLE_SECRET_ARN", secret_arn)) if not v]
    if missing:
        raise ConfigError(
            "--with-roles needs " + ", ".join(missing) + ". The scoped role's "
            "password is read from FACT_ROLE_SECRET_ARN (Secrets Manager), never "
            "from the repo or the command line."
        )
    sys.path.insert(0, str(REPO_ROOT))
    from handlers.common.secrets import get_secret
    password = get_secret(secret_arn).get("password", "")
    if not password:
        raise ConfigError(f"secret {secret_arn} has no 'password' key for the scoped role")
    return role, db, password


# ---------------------------------------------------------------------------
# DB executor (the only mutating surface; injected so tests avoid a live DB)
# ---------------------------------------------------------------------------
class DbExecutor:
    """Applies whole SQL files over a multi-statement TiDB connection and
    maintains the schema_migrations tracking table."""

    def __init__(self, info: ConnInfo):
        self.info = info
        self._engine = None

    def _engine_lazy(self):
        if self._engine is None:
            from sqlalchemy import create_engine
            from pymysql.constants import CLIENT
            url = (f"mysql+pymysql://{self.info.user}:{self.info.password}"
                   f"@{self.info.host}:{self.info.port}/{self.info.database}")
            ssl_arg = {"ca": self.info.ssl_ca} if self.info.ssl_ca else {}
            self._engine = create_engine(
                url,
                connect_args={
                    "ssl": ssl_arg,                       # TLS on (matches handlers/common/db.py)
                    "client_flag": CLIENT.MULTI_STATEMENTS,  # server parses whole files
                    "connect_timeout": 10,
                },
                future=True,
            )
        return self._engine

    def applied_migrations(self) -> dict[str, str]:
        """Read the tracking table -> {filename: checksum}. Empty if it is absent."""
        from sqlalchemy import text
        eng = self._engine_lazy()
        with eng.connect() as cx:
            exists = cx.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"),
                {"t": TRACKING_TABLE}).scalar()
            if not exists:
                return {}
            rows = cx.execute(text(
                f"SELECT filename, checksum FROM {TRACKING_TABLE}")).all()
            return {r[0]: r[1] for r in rows}

    def ensure_tracking(self) -> None:
        from sqlalchemy import text
        eng = self._engine_lazy()
        with eng.begin() as cx:
            cx.execute(text(
                f"CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} ("
                "  filename   VARCHAR(255) PRIMARY KEY,"
                "  checksum   CHAR(64)     NOT NULL,"
                "  applied_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)"
                ")"))

    def execute_file(self, sql_text: str) -> None:
        """Send a whole file to TiDB as one multi-statement query (server-side
        parsing — no client-side ';' splitting), draining every result set."""
        eng = self._engine_lazy()
        raw = eng.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute(sql_text)
            while cur.nextset():          # drain any result sets so the next call is clean
                pass
            cur.close()
            raw.commit()                  # commit trailing DML (DDL auto-commits in TiDB)
        finally:
            raw.close()

    def record(self, name: str, checksum: str) -> None:
        from sqlalchemy import text
        eng = self._engine_lazy()
        with eng.begin() as cx:
            cx.execute(text(
                f"INSERT INTO {TRACKING_TABLE} (filename, checksum) VALUES (:f, :c)"),
                {"f": name, "c": checksum})

    def baseline_insert(self, rows: list[tuple[str, str]]) -> None:
        """Insert all baseline (filename, checksum) rows in ONE transaction.

        Atomic as far as the INSERTs go: either every row is committed or (on any
        error, e.g. a concurrent duplicate) the whole transaction rolls back so no
        partial baseline is left behind."""
        from sqlalchemy import text
        eng = self._engine_lazy()
        with eng.begin() as cx:
            for name, checksum in rows:
                cx.execute(text(
                    f"INSERT INTO {TRACKING_TABLE} (filename, checksum) VALUES (:f, :c)"),
                    {"f": name, "c": checksum})

    def applied_rows(self) -> list[tuple[str, str, str]]:
        """Read (filename, checksum, applied_at) from the tracking table, ordered."""
        from sqlalchemy import text
        eng = self._engine_lazy()
        with eng.connect() as cx:
            exists = cx.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"),
                {"t": TRACKING_TABLE}).scalar()
            if not exists:
                return []
            rows = cx.execute(text(
                f"SELECT filename, checksum, applied_at FROM {TRACKING_TABLE} "
                "ORDER BY filename")).all()
            return [(r[0], r[1], str(r[2])) for r in rows]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def apply_plan(pending: list[Migration], executor, *, dry_run: bool,
               out=print, redact_pw: str | None = None) -> int:
    """Apply pending migrations in order, stopping on the first failure.

    Returns the count applied. Raises RuntimeError on a failed migration (so the
    caller exits non-zero) after leaving every LATER migration untouched.
    """
    applied = 0
    for m in pending:
        if dry_run:
            out(f"  would apply  {m.name}")
            continue
        out(f"  applying     {m.name} ...")
        try:
            executor.execute_file(_sql_for(m, redact_pw))
            executor.record(m.name, m.checksum)
        except Exception as e:  # noqa: BLE001 - report + stop, never continue
            msg = redact_secret(str(e), redact_pw)
            out(f"  FAILED       {m.name}: {type(e).__name__}: {msg}")
            out(f"  STOPPED — {len(pending) - applied - 1} later migration(s) not attempted.")
            raise RuntimeError(f"migration {m.name} failed") from None
        out(f"  applied      {m.name}")
        applied += 1
    return applied


# A migration's SQL, with 03 rendered from env/secret when it is in the plan.
_RENDERED_ROLES: dict[str, str] = {}


def _sql_for(m: Migration, redact_pw: str | None) -> str:
    if m.name == ROLES_FILE and m.name in _RENDERED_ROLES:
        return _RENDERED_ROLES[m.name]
    return m.sql


def _print_baseline_intent(plan: BaselinePlan, report, out=print) -> None:
    """Show exactly what the baseline WOULD record before any write. No secrets."""
    out("== baseline intent ==")
    # schema_migrations current state
    if not report.schema_migrations_present:
        out("  schema_migrations : ABSENT — would be created (existing structure)")
    elif report.schema_migrations_rows:
        out("  schema_migrations : EXISTS, records "
            f"{len(report.schema_migrations_rows)} row(s): "
            + ", ".join(report.schema_migrations_rows))
    else:
        out("  schema_migrations : EXISTS but EMPTY (from the failed apply run)")

    out("  would baseline (record as already applied):")
    for name, ck in plan.to_insert:
        out(f"    + {name}   sha256={ck}")
    if plan.already:
        out("  already baselined (matching checksum — will NOT re-insert):")
        for name, ck in plan.already:
            out(f"    = {name}   sha256={ck}")
    if plan.other_existing:
        out("  other recorded rows (left untouched): " + ", ".join(plan.other_existing))

    out("  intentionally EXCLUDED (left pending/optional):")
    for name in plan.excluded:
        out(f"    - {name}   (operator-specific roles/grants; not applied by the "
            "admin test harness)")

    # Documented, non-failing configuration drift + data caveats.
    fe_replica = report.tiflash_counts.get("fact_event")
    if fe_replica is not None and int(fe_replica) != 1:
        out("  NOTE (TiFlash drift, NOT a failure): fact_event replica expected by "
            f"01_schema.sql = 1, actual = {fe_replica}. A replica exists and "
            "verification otherwise passed; replica count is left unchanged.")
    if report.backfill.get("null_entity_id"):
        out("  NOTE (05 backfill): 05 is baselined because its SCHEMA/STATE was "
            "verified. The historical entity_id backfill is NOT replayed and existing "
            "data is NOT modified (rows retain NULL entity_id).")

    if plan.refusals:
        out("  REFUSALS:")
        for r in plan.refusals:
            out(f"    ! {r}")


def _show_recorded_and_state(executor, core_migrations, info, sql_dir, out=print) -> None:
    """After a baseline, show the recorded rows + run the read-only state check."""
    rows = executor.applied_rows()
    out("\n== recorded in schema_migrations ==")
    for filename, checksum, applied_at in rows:
        out(f"  {filename}   sha256={checksum}   applied_at={applied_at}")
    if info is not None and sql_dir is not None:
        applied = {f: c for f, c, _ in rows}
        state_plan = plan_migrations(core_migrations, applied)
        out("\n== migration-state check (read-only) ==")
        _print_preflight(info, sql_dir, core_migrations, state_plan, out=out)


def run_baseline(report, core_migrations: list[Migration], existing_rows: dict[str, str],
                 executor, *, apply_for_real: bool, out=print,
                 info=None, sql_dir=None, redact_pw: str | None = None) -> int:
    """Plan + (optionally) execute the baseline. Returns a process exit code.

    Read-only unless apply_for_real is True. The DB is touched ONLY via
    executor.ensure_tracking()/baseline_insert()/applied_rows(); with a fake
    executor this whole function runs offline, which is how the tests exercise the
    "no mutation without APPLY_FOR_REAL" and idempotency guarantees.
    """
    plan = plan_baseline(report, core_migrations, existing_rows)
    _print_baseline_intent(plan, report, out=out)

    if not plan.allowed:
        out("\nREFUSING TO BASELINE — verification/precondition failed. Nothing written.")
        return 6

    if not plan.to_insert:
        out("\nAlready baselined — all seven migrations are recorded with matching "
            "checksums. Nothing to do (idempotent).")
        _show_recorded_and_state(executor, core_migrations, info, sql_dir, out=out)
        return 0

    if not apply_for_real:
        out("\nDRY-RUN (APPLY_FOR_REAL not set): the rows above WOULD be inserted. "
            "Nothing was written. Re-run with APPLY_FOR_REAL=1 to baseline for real.")
        return 0

    out(f"\n== writing baseline ({len(plan.to_insert)} row(s), atomic) ==")
    executor.ensure_tracking()   # existing structure; no-op if the table already exists
    try:
        executor.baseline_insert(plan.to_insert)
    except Exception as e:  # noqa: BLE001
        out(f"  FAILED to write baseline: {type(e).__name__}: "
            f"{redact_secret(str(e), redact_pw)}")
        out("  (transaction rolled back — no partial baseline written)")
        return 1
    out("  BASELINE WRITTEN.")
    _show_recorded_and_state(executor, core_migrations, info, sql_dir, out=out)
    return 0


def inspect_readonly(executor: "DbExecutor") -> dict:
    """Run the read-only schema introspection inside a transaction we only READ from.

    This is the ONLY path used by --verify-existing. It never creates
    schema_migrations and never mutates. Read-only is guaranteed two ways:

      1. By construction — verify_schema.inspect issues nothing but SELECT / SHOW /
         INFORMATION_SCHEMA, and this function calls no writing method.
      2. By transaction discipline — we open a transaction and ROLLBACK at the end
         (never commit), so even an accidental write would be discarded.

    We ask for `START TRANSACTION READ ONLY` (server-enforced) when the cluster
    allows it, and fall back to a plain transaction otherwise. We deliberately do
    NOT flip tidb_enable_noop_functions — that would be a session mutation.
    """
    import verify_schema  # local import (deploy/ is on sys.path via the runner)
    eng = executor._engine_lazy()
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        try:
            cur.execute("START TRANSACTION READ ONLY")   # server-enforced, when supported
        except Exception:                                # noqa: BLE001
            raw.rollback()                               # clear any partial state
            cur.execute("START TRANSACTION")             # plain txn; we only read + rollback

        def run(sql: str, params=None):
            cur.execute(sql, params or None)
            return cur.fetchall()

        snapshot = verify_schema.inspect(run)
        raw.rollback()  # end the txn WITHOUT writing anything
        return snapshot
    finally:
        raw.close()


def _print_preflight(info: ConnInfo, sql_dir: Path, migrations: list[Migration],
                     plan: Plan | None, out=print) -> None:
    d = info.safe_display()
    out("== target (no secrets) ==")
    out(f"  host      : {d['host']}")
    out(f"  port      : {d['port']}")
    out(f"  database  : {d['database']}")
    out(f"  user      : {d['user']}")
    out(f"  tls       : {d['tls']}")
    out(f"  creds from : {d['source']}")
    out(f"  sql dir   : {sql_dir}")
    out(f"  migrations: {', '.join(m.name for m in migrations)}")
    if plan is not None:
        out("== migration state ==")
        out(f"  applied   : {', '.join(m.name for m in plan.already) or '(none)'}")
        out(f"  pending   : {', '.join(m.name for m in plan.pending) or '(none)'}")
        if plan.drift:
            out("  DRIFT     : " + ", ".join(f"{m.name} (recorded checksum differs)"
                                             for m, _ in plan.drift))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TiDB fact-layer migration runner")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="validate config + report target/state (read-only, no connect mutation)")
    mode.add_argument("--dry-run", action="store_true",
                      help="read-only: also list pending migrations that WOULD apply")
    mode.add_argument("--verify-existing", action="store_true",
                      help="read-only: inspect an existing DB and report whether it "
                           "already matches the migration set (no schema_migrations, no writes)")
    mode.add_argument("--baseline-existing", action="store_true",
                      help="record the seven verified migrations as already applied in "
                           "schema_migrations. Read-only unless APPLY_FOR_REAL=1. Runs the "
                           "read-only verification first and refuses on any DIFFER/UNKNOWN "
                           "or conflicting rows. Excludes 03_roles.sql.")
    mode.add_argument("--apply", action="store_true",
                      help="apply pending migrations (also requires APPLY_FOR_REAL=1)")
    ap.add_argument("--with-roles", action="store_true",
                    help="also apply 03_roles.sql LAST (renders placeholders; needs "
                         "FACT_ROLE, TIDB_DB, FACT_ROLE_SECRET_ARN)")
    ap.add_argument("--sql-dir", default=str(DEFAULT_SQL_DIR), help="migration directory")
    args = ap.parse_args(argv)

    sql_dir = Path(args.sql_dir)
    if not sql_dir.is_dir():
        print(f"ERROR: sql dir not found: {sql_dir}", file=sys.stderr)
        return 2

    migrations = discover_migrations(sql_dir, include_roles=args.with_roles)
    if not migrations:
        print(f"ERROR: no migrations found in {sql_dir}", file=sys.stderr)
        return 2

    # Resolve + validate the connection config (no connection yet).
    try:
        info = resolve_conn()
    except ConfigError as e:
        print(f"ERROR (config): {e}", file=sys.stderr)
        return 2
    problems = detect_placeholders(info)
    if problems:
        print("ERROR: connection looks unconfigured / placeholder:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    redact_pw = info.password or None

    # --check: report target only (read-only; do not even read the tracking table
    # so it works before the DB is reachable is NOT the goal — we DO connect to
    # report real state, but we never mutate).
    executor = DbExecutor(info)

    # --verify-existing: pure read-only schema inspection of an EXISTING database.
    # Short-circuits BEFORE reading migration state — it never needs (or creates)
    # schema_migrations, and it never mutates. Answers whether the live schema is
    # already equivalent to sql/01,02,04-08 (so a baseline could be introduced).
    if args.verify_existing:
        import verify_schema  # local import; deploy/ is on sys.path via the runner
        _print_preflight(info, sql_dir, migrations, None)
        try:
            snapshot = inspect_readonly(executor)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: read-only inspection failed: {type(e).__name__}: "
                  f"{redact_secret(str(e), redact_pw)}", file=sys.stderr)
            return 3
        report = verify_schema.build_report(snapshot)
        print()
        print(verify_schema.render_report(report))
        return 0

    # --baseline-existing: verify (read-only) THEN record the seven verified
    # migrations as applied. The only write is the baseline INSERT, and only when
    # APPLY_FOR_REAL=1. 03_roles.sql is always excluded.
    if args.baseline_existing:
        import verify_schema  # local import; deploy/ is on sys.path via the runner
        core = discover_migrations(sql_dir)  # the seven — roles excluded regardless of flag
        _print_preflight(info, sql_dir, core, None)
        try:
            snapshot = inspect_readonly(executor)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: read-only inspection failed: {type(e).__name__}: "
                  f"{redact_secret(str(e), redact_pw)}", file=sys.stderr)
            return 3
        report = verify_schema.build_report(snapshot)
        print()
        print(verify_schema.render_report(report))
        print()
        existing_rows = {r["filename"]: r["checksum"]
                         for r in snapshot.get("schema_migrations_rows", [])}
        apply_for_real = os.environ.get("APPLY_FOR_REAL") == "1"
        return run_baseline(report, core, existing_rows, executor,
                            apply_for_real=apply_for_real, out=print,
                            info=info, sql_dir=sql_dir, redact_pw=redact_pw)

    # Roles rendering (in memory only) if requested — fail fast on misconfig.
    roles_pw = None
    if args.with_roles:
        try:
            role, db, roles_pw = roles_password_and_params()
        except ConfigError as e:
            print(f"ERROR (--with-roles): {e}", file=sys.stderr)
            return 2
        for m in migrations:
            if m.name == ROLES_FILE:
                _RENDERED_ROLES[m.name] = render_roles_sql(m.sql, role=role, db=db,
                                                           password=roles_pw)
        redact_pw = redact_pw or roles_pw

    # Read applied state (SELECT only) to build the plan. For --check we still
    # read it (read-only) so we can report state; if the DB is unreachable this
    # raises and we surface it.
    try:
        applied = executor.applied_migrations()
    except Exception as e:  # noqa: BLE001
        # Connection/read failure — report safely (no password) and stop.
        print(f"ERROR: could not read migration state: {type(e).__name__}: "
              f"{redact_secret(str(e), redact_pw)}", file=sys.stderr)
        return 3

    the_plan = plan_migrations(migrations, applied)
    _print_preflight(info, sql_dir, migrations, the_plan)

    if the_plan.drift:
        print("ERROR: an already-applied migration was edited (checksum drift). "
              "Refusing to proceed. Review the listed files.", file=sys.stderr)
        return 4

    if args.check:
        print("\nCHECK OK — no changes made.")
        return 0

    if args.dry_run or not args.apply:
        print("\n== dry-run (no mutation) ==")
        apply_plan(the_plan.pending, executor, dry_run=True)
        if not args.dry_run:
            print("\n(no mode given; defaulted to dry-run. Use --apply with "
                  "APPLY_FOR_REAL=1 to mutate.)")
        return 0

    # --apply path: double-gate on APPLY_FOR_REAL (defense in depth vs the wrapper).
    if os.environ.get("APPLY_FOR_REAL") != "1":
        print("REFUSING to apply: set APPLY_FOR_REAL=1 to mutate TiDB.", file=sys.stderr)
        return 5
    if not the_plan.pending:
        print("\nNothing to apply — all migrations already recorded.")
        return 0

    print(f"\n== applying {len(the_plan.pending)} migration(s) to {info.safe_display()['host']} ==")
    executor.ensure_tracking()
    try:
        n = apply_plan(the_plan.pending, executor, dry_run=False, redact_pw=redact_pw)
    except RuntimeError:
        return 1
    print(f"\nDONE — applied {n} migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
