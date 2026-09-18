#!/usr/bin/env python3
"""Read-only schema verification for the fact layer (deploy/migrate.py --verify-existing).

PURPOSE
    Answer one question about an EXISTING TiDB database: "is it already equivalent
    to the current migration set (sql/01,02,04-08)?" — WITHOUT mutating anything.

    This exists because the fact layer was validated live against the `test`
    database BEFORE the schema_migrations tracking table was introduced. So the
    tables exist but nothing is recorded as applied, and a naive re-apply of
    01_schema.sql fails with "table already exists". Before we can safely baseline
    schema_migrations we must PROVE the live schema matches what the migrations
    would produce.

DESIGN
    * The EXPECTED schema is hand-encoded below from sql/01,02,04-08 (one delta per
      migration), so the comparison is explicit and reviewable and needs no live DB.
    * `build_report(actual)` is PURE — it takes an introspection snapshot (plain
      dicts/sets) and returns a Report. It is unit-tested offline with fake
      snapshots (tests/test_verify_schema.py).
    * `inspect(run)` is the only DB-touching part: it runs SELECT / SHOW /
      INFORMATION_SCHEMA queries ONLY (never DDL/DML) through a caller-supplied
      `run(sql, params)` and returns the snapshot dict. The caller wraps it in a
      READ ONLY transaction for defense in depth.

    Per migration we report MATCH / DIFFER / UNKNOWN:
      MATCH   every checkable thing was found and equal.
      DIFFER  something was found and it does NOT equal expected (details given).
      UNKNOWN we could not inspect it (query failed / feature not introspectable).
    A migration rolls up to DIFFER if any finding differs, else UNKNOWN if any is
    unknown, else MATCH.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Type / value normalization (so cosmetic differences are not false DIFFERs)
# ---------------------------------------------------------------------------
# TiDB may render integer types with a display width (bigint(20)); strip it so
# `bigint` == `bigint(20)`. Vector/decimal/varchar/enum widths are meaningful and
# are kept.
_INT_WIDTH = re.compile(r"\b(bigint|int|smallint|tinyint|mediumint)\(\d+\)")


def normalize_type(t: str | None) -> str:
    """Canonicalize a COLUMN_TYPE for comparison (lowercase, no spaces, no int width)."""
    t = (t or "").strip().lower()
    t = _INT_WIDTH.sub(r"\1", t)
    t = re.sub(r"\s+", "", t)          # our type strings contain no significant spaces
    return t


def normalize_default(d) -> str:
    """Canonicalize a COLUMN_DEFAULT literal for comparison (strip quotes/space)."""
    if d is None:
        return ""
    s = str(d).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s.lower()


# ---------------------------------------------------------------------------
# Expected schema — encoded from sql/01,02,04-08, one delta per migration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Col:
    table: str
    name: str
    type: str                 # expected COLUMN_TYPE (pre-normalized-friendly)
    nullable: bool
    auto_random: bool = False
    default: str | None = None   # only checked when set (skips CURRENT_TIMESTAMP etc.)


@dataclass(frozen=True)
class Idx:
    table: str
    name: str
    columns: tuple            # ordered column names
    unique: bool


@dataclass(frozen=True)
class Seed:
    table: str
    key: tuple                # columns forming the logical identity of a seed row
    check: tuple              # columns whose values must match
    rows: tuple               # tuple of dicts (all key+check columns present)


# --- Migration 01: base tables -------------------------------------------------
M01_COLUMNS = (
    # fact_subject (entity_id is added later by 05)
    Col("fact_subject", "subject_id", "bigint", False, auto_random=True),
    Col("fact_subject", "tenant_id", "varchar(64)", False),
    Col("fact_subject", "subject", "varchar(255)", False),
    Col("fact_subject", "predicate", "varchar(128)", False),
    Col("fact_subject", "subject_vec", "vector(1024)", True),
    Col("fact_subject", "last_event_hash", "char(64)", True),
    Col("fact_subject", "created_at", "datetime", True),
    # fact_event (many columns added by 04/05/06)
    Col("fact_event", "event_id", "bigint", False, auto_random=True),
    Col("fact_event", "subject_id", "bigint", False),
    Col("fact_event", "tenant_id", "varchar(64)", False),
    Col("fact_event", "op", "enum('assert','supersede','retract','correct')", False),
    Col("fact_event", "value", "json", False),
    Col("fact_event", "source", "varchar(255)", False),
    Col("fact_event", "confidence", "decimal(4,3)", True),
    Col("fact_event", "agent_id", "varchar(128)", False),
    Col("fact_event", "session_id", "varchar(128)", False),
    Col("fact_event", "tool_turn", "int", True),
    Col("fact_event", "supersedes_event_id", "bigint", True),
    Col("fact_event", "observed_at", "datetime(6)", False),
    Col("fact_event", "prev_hash", "char(64)", False),
    Col("fact_event", "event_hash", "char(64)", False),
    # fact_current (columns added by 04/06)
    Col("fact_current", "subject_id", "bigint", False),
    Col("fact_current", "tenant_id", "varchar(64)", False),
    Col("fact_current", "value", "json", False),
    Col("fact_current", "winning_event_id", "bigint", False),
    Col("fact_current", "status",
        "enum('active','superseded','retracted','disputed')", False, default="active"),
    Col("fact_current", "updated_at", "datetime", True),
    # predicate_rule
    Col("predicate_rule", "predicate", "varchar(128)", False),
    Col("predicate_rule", "value_type",
        "enum('scalar','monotonic','set','freetext')", False),
    # source_rank
    Col("source_rank", "source", "varchar(255)", False),
    Col("source_rank", "rank", "int", False),
)
M01_INDEXES = (
    Idx("fact_subject", "PRIMARY", ("subject_id",), True),
    Idx("fact_subject", "uk_identity", ("tenant_id", "subject", "predicate"), True),
    Idx("fact_event", "PRIMARY", ("event_id",), True),
    Idx("fact_event", "idx_subject", ("tenant_id", "subject_id", "observed_at"), False),
    Idx("fact_event", "idx_session", ("tenant_id", "session_id"), False),
    Idx("fact_event", "idx_chain", ("subject_id", "prev_hash"), False),
    Idx("fact_current", "PRIMARY", ("subject_id",), True),
    Idx("fact_current", "idx_tenant_status", ("tenant_id", "status"), False),
    Idx("predicate_rule", "PRIMARY", ("predicate",), True),
    Idx("source_rank", "PRIMARY", ("source",), True),
)

# --- Migration 02: registry seed ----------------------------------------------
M02_SEEDS = (
    Seed("predicate_rule", ("predicate",), ("value_type",), (
        {"predicate": "status", "value_type": "scalar"},
        {"predicate": "last_seen", "value_type": "monotonic"},
        {"predicate": "known_aliases", "value_type": "set"},
        {"predicate": "notes", "value_type": "freetext"},
    )),
    Seed("source_rank", ("source",), ("rank",), (
        {"source": "system_of_record", "rank": 100},
        {"source": "verified_integration", "rank": 80},
        {"source": "user_assertion", "rank": 60},
        {"source": "agent_inference", "rank": 40},
    )),
)

# --- Migration 04: authority_policy table + seed + ALTERs ----------------------
M04_COLUMNS = (
    Col("authority_policy", "policy_id", "bigint", False, auto_random=True),
    Col("authority_policy", "tenant_id", "varchar(64)", False, default="*"),
    Col("authority_policy", "predicate", "varchar(128)", False),
    Col("authority_policy", "source", "varchar(255)", False),
    Col("authority_policy", "source_type", "varchar(64)", True),
    Col("authority_policy", "authority", "int", False),
    Col("authority_policy", "priority", "int", False, default="0"),
    Col("authority_policy", "policy_version", "varchar(64)", False),
    Col("authority_policy", "effective_from", "datetime(6)", False,
        default="1970-01-01 00:00:00.000000"),
    Col("authority_policy", "effective_to", "datetime(6)", True),
    Col("authority_policy", "created_at", "datetime", True),
    # ALTERs onto existing tables
    Col("fact_current", "policy_version", "varchar(64)", True),
    Col("fact_current", "resolution_reason", "varchar(255)", True),
    Col("fact_event", "outcome", "varchar(24)", True),
    Col("fact_event", "policy_version", "varchar(64)", True),
)
M04_INDEXES = (
    Idx("authority_policy", "PRIMARY", ("policy_id",), True),
    Idx("authority_policy", "idx_authority_lookup",
        ("source", "predicate", "effective_from"), False),
)
_AUTH_V1 = "authority-v1"
_FRAUD_V1 = "fraud-policy-v1"
M04_SEEDS = (
    Seed("authority_policy",
         ("tenant_id", "predicate", "source"),
         ("source_type", "authority", "priority", "policy_version"), (
        {"tenant_id": "*", "predicate": "*", "source": "system_of_record",
         "source_type": "system", "authority": 100, "priority": 0, "policy_version": _AUTH_V1},
        {"tenant_id": "*", "predicate": "*", "source": "verified_integration",
         "source_type": "system", "authority": 80, "priority": 0, "policy_version": _AUTH_V1},
        {"tenant_id": "*", "predicate": "*", "source": "user_assertion",
         "source_type": "user", "authority": 60, "priority": 0, "policy_version": _AUTH_V1},
        {"tenant_id": "*", "predicate": "*", "source": "agent_inference",
         "source_type": "agent", "authority": 40, "priority": 0, "policy_version": _AUTH_V1},
        {"tenant_id": "*", "predicate": "fraud_status", "source": "human_investigator",
         "source_type": "human", "authority": 100, "priority": 10, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "fraud_status", "source": "fraud_review",
         "source_type": "system", "authority": 95, "priority": 10, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "fraud_status", "source": "fraud_model",
         "source_type": "model", "authority": 70, "priority": 5, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "fraud_status", "source": "agent_inference",
         "source_type": "agent", "authority": 55, "priority": 5, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "fraud_status", "source": "user_assertion",
         "source_type": "user", "authority": 20, "priority": 1, "policy_version": _FRAUD_V1},
    )),
)

# --- Migration 05: entity / entity_alias / fact_evidence + ALTERs + backfill ---
M05_COLUMNS = (
    Col("entity", "entity_id", "bigint", False, auto_random=True),
    Col("entity", "tenant_id", "varchar(64)", False),
    Col("entity", "entity_type", "varchar(64)", False),
    Col("entity", "canonical_key", "varchar(255)", False),
    Col("entity", "display_name", "varchar(255)", True),
    Col("entity", "created_at", "datetime", True),
    Col("entity", "updated_at", "datetime", True),
    Col("entity_alias", "alias_id", "bigint", False, auto_random=True),
    Col("entity_alias", "tenant_id", "varchar(64)", False),
    Col("entity_alias", "entity_id", "bigint", False),
    Col("entity_alias", "alias_key", "varchar(255)", False),
    Col("entity_alias", "alias_type", "varchar(64)", True),
    Col("entity_alias", "created_at", "datetime", True),
    Col("fact_evidence", "evidence_id", "bigint", False, auto_random=True),
    Col("fact_evidence", "tenant_id", "varchar(64)", False),
    Col("fact_evidence", "event_id", "bigint", False),
    Col("fact_evidence", "subject_id", "bigint", False),
    Col("fact_evidence", "entity_id", "bigint", True),
    Col("fact_evidence", "evidence_type", "varchar(64)", False),
    Col("fact_evidence", "evidence_ref", "varchar(255)", False),
    Col("fact_evidence", "relationship", "varchar(24)", False, default="SUPPORTS"),
    Col("fact_evidence", "metadata", "json", True),
    Col("fact_evidence", "observed_at", "datetime(6)", True),
    Col("fact_evidence", "created_at", "datetime", True),
    # ALTERs
    Col("fact_subject", "entity_id", "bigint", True),
    Col("fact_event", "assessor_type", "varchar(32)", True),
)
M05_INDEXES = (
    Idx("entity", "PRIMARY", ("entity_id",), True),
    Idx("entity", "uk_entity_canonical", ("tenant_id", "canonical_key"), True),
    Idx("entity", "idx_entity_type", ("tenant_id", "entity_type"), False),
    Idx("entity_alias", "PRIMARY", ("alias_id",), True),
    Idx("entity_alias", "uk_alias", ("tenant_id", "alias_key"), True),
    Idx("entity_alias", "idx_alias_entity", ("tenant_id", "entity_id"), False),
    Idx("fact_evidence", "PRIMARY", ("evidence_id",), True),
    Idx("fact_evidence", "idx_evidence_event", ("tenant_id", "event_id"), False),
    Idx("fact_evidence", "idx_evidence_subject", ("tenant_id", "subject_id"), False),
    Idx("fact_evidence", "idx_evidence_ref",
        ("tenant_id", "evidence_type", "evidence_ref"), False),
)

# --- Migration 06: temporal columns + index -----------------------------------
M06_COLUMNS = (
    Col("fact_event", "valid_from", "datetime(6)", True),
    Col("fact_event", "valid_to", "datetime(6)", True),
    Col("fact_event", "asserted_at", "datetime(6)", True),
    Col("fact_event", "ingested_at", "datetime", True),
    Col("fact_current", "valid_from", "datetime(6)", True),
    Col("fact_current", "valid_to", "datetime(6)", True),
)
M06_INDEXES = (
    Idx("fact_event", "idx_event_valid", ("tenant_id", "subject_id", "valid_from"), False),
)

# --- Migration 07: idempotency table ------------------------------------------
M07_COLUMNS = (
    Col("fact_idempotency", "tenant_id", "varchar(64)", False),
    Col("fact_idempotency", "idempotency_key", "varchar(255)", False),
    Col("fact_idempotency", "event_id", "bigint", True),
    Col("fact_idempotency", "result_json", "json", True),
    Col("fact_idempotency", "created_at", "datetime", True),
)
M07_INDEXES = (
    Idx("fact_idempotency", "PRIMARY", ("tenant_id", "idempotency_key"), True),
)

# --- Migration 08: domain predicates + liability authority --------------------
M08_SEEDS = (
    Seed("predicate_rule", ("predicate",), ("value_type",), (
        {"predicate": "fraud_status", "value_type": "scalar"},
        {"predicate": "liability_status", "value_type": "scalar"},
    )),
    Seed("authority_policy",
         ("tenant_id", "predicate", "source"),
         ("source_type", "authority", "priority", "policy_version"), (
        {"tenant_id": "*", "predicate": "liability_status", "source": "human_investigator",
         "source_type": "human", "authority": 100, "priority": 10, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "liability_status", "source": "risk_engine",
         "source_type": "system", "authority": 90, "priority": 10, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "liability_status", "source": "fraud_model",
         "source_type": "model", "authority": 70, "priority": 5, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "liability_status", "source": "agent_inference",
         "source_type": "agent", "authority": 55, "priority": 5, "policy_version": _FRAUD_V1},
        {"tenant_id": "*", "predicate": "liability_status", "source": "user_assertion",
         "source_type": "user", "authority": 20, "priority": 1, "policy_version": _FRAUD_V1},
    )),
)

# Per-migration bundle: (number, name, headline, creates, columns, indexes, seeds)
MIGRATIONS = (
    dict(num=1, name="01_schema.sql", headline="schema objects",
         creates=("fact_subject", "fact_event", "fact_current",
                  "predicate_rule", "source_rank"),
         columns=M01_COLUMNS, indexes=M01_INDEXES, seeds=()),
    dict(num=2, name="02_seed.sql", headline="seed data",
         creates=(), columns=(), indexes=(), seeds=M02_SEEDS),
    dict(num=4, name="04_authority_policy.sql", headline="authority_policy + ALTERs",
         creates=("authority_policy",),
         columns=M04_COLUMNS, indexes=M04_INDEXES, seeds=M04_SEEDS),
    dict(num=5, name="05_entity_evidence.sql", headline="entity/evidence + ALTERs + backfill",
         creates=("entity", "entity_alias", "fact_evidence"),
         columns=M05_COLUMNS, indexes=M05_INDEXES, seeds=()),
    dict(num=6, name="06_temporal.sql", headline="temporal columns + index",
         creates=(), columns=M06_COLUMNS, indexes=M06_INDEXES, seeds=()),
    dict(num=7, name="07_idempotency.sql", headline="fact_idempotency",
         creates=("fact_idempotency",),
         columns=M07_COLUMNS, indexes=M07_INDEXES, seeds=()),
    dict(num=8, name="08_domain_predicates.sql", headline="domain predicates + authority",
         creates=(), columns=(), indexes=(), seeds=M08_SEEDS),
)

# The full set of objects the migration set is expected to produce, for the
# "anything NOT represented by the migrations?" check.
EXPECTED_TABLES = frozenset(t for m in MIGRATIONS for t in m["creates"])
_ALL_COLS = [c for m in MIGRATIONS for c in m["columns"]]
EXPECTED_COLUMNS = {}
for _c in _ALL_COLS:
    EXPECTED_COLUMNS.setdefault(_c.table, set()).add(_c.name)
# Vector configuration (fact_subject.subject_vec + its HNSW index) and the
# fact_event TiFlash replica are 01_schema features that plain column/index
# introspection does not fully capture; verified specially.
VECTOR_TABLE = "fact_subject"
VECTOR_COLUMN = "subject_vec"
VECTOR_DIM = 1024
VECTOR_INDEX = "idx_subject_vec"
TIFLASH_TABLES = ("fact_event",)
# Data tables that legitimately hold live rows (not seed data). Their row counts
# are reported for context but never treated as "unexpected".
DATA_TABLES = ("fact_subject", "fact_event", "fact_current",
               "entity", "entity_alias", "fact_evidence", "fact_idempotency")


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------
MATCH, DIFFER, UNKNOWN = "MATCH", "DIFFER", "UNKNOWN"


@dataclass
class Finding:
    status: str
    object: str
    expected: str = ""
    actual: str = ""


@dataclass
class MigrationResult:
    num: int
    name: str
    headline: str
    findings: list = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(f.status == DIFFER for f in self.findings):
            return DIFFER
        if any(f.status == UNKNOWN for f in self.findings):
            return UNKNOWN
        return MATCH


@dataclass
class Report:
    migrations: list = field(default_factory=list)
    unexpected_tables: list = field(default_factory=list)
    unexpected_columns: list = field(default_factory=list)   # "table.column" strings
    unexpected_seed_rows: list = field(default_factory=list)  # human-readable strings
    schema_migrations_present: bool = False
    schema_migrations_rows: list = field(default_factory=list)  # recorded filenames
    data_row_counts: dict = field(default_factory=dict)
    backfill: dict = field(default_factory=dict)              # {"null_entity_id": n, "total": n}
    tiflash_counts: dict = field(default_factory=dict)        # table -> actual replica_count
    notes: list = field(default_factory=list)

    @property
    def all_match(self) -> bool:
        return all(m.status == MATCH for m in self.migrations)

    @property
    def any_unknown(self) -> bool:
        return any(m.status == UNKNOWN for m in self.migrations)

    @property
    def schema_equivalent(self) -> bool:
        """True if every tracked schema object matches and there is nothing outside
        the migration set (IGNORING the schema_migrations tracking table itself,
        whose presence/emptiness is handled separately in the baseline decision)."""
        return (self.all_match and not self.any_unknown
                and not self.unexpected_tables and not self.unexpected_columns
                and not self.unexpected_seed_rows)

    @property
    def clean_extra(self) -> bool:
        return not (self.unexpected_tables or self.unexpected_columns
                    or self.unexpected_seed_rows or self.schema_migrations_present)

    @property
    def baseline_safe(self) -> bool:
        """A baseline can be introduced cleanly ONLY if the schema is equivalent AND
        no schema_migrations table already exists (nothing to reconcile)."""
        return self.schema_equivalent and not self.schema_migrations_present


# ---------------------------------------------------------------------------
# Pure comparison — snapshot dict -> Report
# ---------------------------------------------------------------------------
def _check_columns(cols, actual, findings) -> None:
    acols = actual.get("columns", {})
    for c in cols:
        tbl = acols.get(c.table)
        if tbl is None:
            findings.append(Finding(UNKNOWN, f"{c.table}.{c.name}",
                                    "column present", "table not inspected/absent"))
            continue
        got = tbl.get(c.name)
        if got is None:
            findings.append(Finding(DIFFER, f"{c.table}.{c.name}",
                                    f"{c.type} column", "absent"))
            continue
        col_findings = []
        exp_t, got_t = normalize_type(c.type), normalize_type(got.get("type"))
        if exp_t != got_t:
            col_findings.append(Finding(DIFFER, f"{c.table}.{c.name} type",
                                        exp_t, got_t or "(none)"))
        if bool(c.nullable) != bool(got.get("nullable")):
            col_findings.append(Finding(DIFFER, f"{c.table}.{c.name} nullable",
                                        str(c.nullable), str(got.get("nullable"))))
        if c.auto_random:
            ar = got.get("auto_random")
            if ar is None:
                col_findings.append(Finding(UNKNOWN, f"{c.table}.{c.name} AUTO_RANDOM",
                                            "AUTO_RANDOM", "not introspectable"))
            elif not ar:
                col_findings.append(Finding(DIFFER, f"{c.table}.{c.name} AUTO_RANDOM",
                                            "AUTO_RANDOM", "not AUTO_RANDOM"))
        if c.default is not None:
            exp_d, got_d = normalize_default(c.default), normalize_default(got.get("default"))
            if exp_d != got_d:
                col_findings.append(Finding(DIFFER, f"{c.table}.{c.name} default",
                                            c.default, str(got.get("default"))))
        # Emit an explicit MATCH so the report shows the column WAS verified, not
        # just silently absent from the findings list.
        findings.extend(col_findings or [Finding(MATCH, f"{c.table}.{c.name}")])


def _check_indexes(idxs, actual, findings) -> None:
    aidx = actual.get("indexes", {})
    apk = actual.get("pk", {})
    for ix in idxs:
        if ix.name == "PRIMARY":
            got = apk.get(ix.table)
            if got is None:
                findings.append(Finding(UNKNOWN, f"{ix.table} PRIMARY KEY",
                                        ",".join(ix.columns), "not inspected"))
            elif tuple(got) != tuple(ix.columns):
                findings.append(Finding(DIFFER, f"{ix.table} PRIMARY KEY",
                                        ",".join(ix.columns), ",".join(got)))
            else:
                findings.append(Finding(MATCH, f"{ix.table} PRIMARY KEY"))
            continue
        tbl = aidx.get(ix.table)
        if tbl is None:
            findings.append(Finding(UNKNOWN, f"{ix.table}.{ix.name}",
                                    "index present", "table not inspected"))
            continue
        got = tbl.get(ix.name)
        if got is None:
            findings.append(Finding(DIFFER, f"{ix.table}.{ix.name}",
                                    f"index ({','.join(ix.columns)})", "absent"))
            continue
        ix_findings = []
        if tuple(got.get("columns", ())) != tuple(ix.columns):
            ix_findings.append(Finding(DIFFER, f"{ix.table}.{ix.name} columns",
                                       ",".join(ix.columns),
                                       ",".join(got.get("columns", ()))))
        if bool(got.get("unique")) != bool(ix.unique):
            ix_findings.append(Finding(DIFFER, f"{ix.table}.{ix.name} unique",
                                       str(ix.unique), str(got.get("unique"))))
        findings.extend(ix_findings or [Finding(MATCH, f"{ix.table}.{ix.name}")])


def _row_key(row, key):
    return tuple(str(row.get(k)) for k in key)


def _check_seeds(seeds, actual, findings, unexpected_sink=None) -> None:
    seed_rows = actual.get("seed_rows", {})
    for s in seeds:
        got_rows = seed_rows.get(s.table)
        if got_rows is None:
            findings.append(Finding(UNKNOWN, f"{s.table} seed",
                                    f"{len(s.rows)} row(s)", "table not inspected"))
            continue
        by_key = {_row_key(r, s.key): r for r in got_rows}
        for exp in s.rows:
            k = _row_key(exp, s.key)
            keydesc = "/".join(str(exp.get(x)) for x in s.key)
            got = by_key.get(k)
            if got is None:
                findings.append(Finding(DIFFER, f"{s.table}[{keydesc}]",
                                        "present", "missing"))
                continue
            diffs = []
            for col in s.check:
                if str(exp.get(col)) != str(got.get(col)):
                    diffs.append(f"{col}: {exp.get(col)} != {got.get(col)}")
            if diffs:
                findings.append(Finding(DIFFER, f"{s.table}[{keydesc}]",
                                        "; ".join(f"{c}={exp.get(c)}" for c in s.check),
                                        "; ".join(diffs)))
            else:
                findings.append(Finding(MATCH, f"{s.table}[{keydesc}]"))


def _collect_unexpected_seed_rows(actual) -> list:
    """Rows in the registry/seed tables that no migration seed accounts for.

    Only the seed-owned tables are examined (predicate_rule, source_rank,
    authority_policy). Extra rows there mean the DB carries registry data beyond
    the migration set (e.g. added during live validation)."""
    expected = {}  # table -> set(logical keys) + how to build a key
    keyers = {}
    for m in MIGRATIONS:
        for s in m["seeds"]:
            keyers[s.table] = s.key
            expected.setdefault(s.table, set())
            for r in s.rows:
                expected[s.table].add(_row_key(r, s.key))
    extras = []
    seed_rows = actual.get("seed_rows", {})
    for table, key in keyers.items():
        got_rows = seed_rows.get(table)
        if got_rows is None:
            continue
        for r in got_rows:
            if _row_key(r, key) not in expected[table]:
                desc = "/".join(f"{k}={r.get(k)}" for k in key)
                extras.append(f"{table}[{desc}]")
    return sorted(set(extras))


def _check_vector(actual, findings) -> None:
    vc = actual.get("vector_columns", {})
    tbl = vc.get(VECTOR_TABLE)
    if tbl is None or VECTOR_COLUMN not in tbl:
        findings.append(Finding(UNKNOWN, f"{VECTOR_TABLE}.{VECTOR_COLUMN} vector",
                                f"vector({VECTOR_DIM})", "not introspectable"))
    else:
        dim = tbl.get(VECTOR_COLUMN)
        if dim == VECTOR_DIM:
            findings.append(Finding(MATCH, f"{VECTOR_TABLE}.{VECTOR_COLUMN} vector"))
        else:
            findings.append(Finding(DIFFER, f"{VECTOR_TABLE}.{VECTOR_COLUMN} vector dim",
                                    str(VECTOR_DIM), str(dim)))
    vi = actual.get("vector_indexes", {})
    present = vi.get(VECTOR_TABLE)
    if present is None:
        findings.append(Finding(UNKNOWN, f"{VECTOR_TABLE}.{VECTOR_INDEX} HNSW index",
                                "present", "not introspectable"))
    elif VECTOR_INDEX in present:
        findings.append(Finding(MATCH, f"{VECTOR_TABLE}.{VECTOR_INDEX} HNSW index"))
    else:
        findings.append(Finding(DIFFER, f"{VECTOR_TABLE}.{VECTOR_INDEX} HNSW index",
                                "present", "absent"))


def _check_tiflash(actual, findings) -> None:
    tf = actual.get("tiflash")
    for t in TIFLASH_TABLES:
        if tf is None:
            findings.append(Finding(UNKNOWN, f"{t} TiFlash replica",
                                    "replica >= 1", "not introspectable"))
            continue
        info = tf.get(t)
        if not info or int(info.get("replica_count", 0)) < 1:
            findings.append(Finding(DIFFER, f"{t} TiFlash replica",
                                    "replica >= 1", "no replica"))
        else:
            note = "" if info.get("available") else " (not yet available)"
            findings.append(Finding(MATCH, f"{t} TiFlash replica x{info['replica_count']}{note}"))


def build_report(actual: dict) -> Report:
    """Compare an introspection snapshot against the expected migration set."""
    report = Report()
    atables = actual.get("tables")
    for m in MIGRATIONS:
        mr = MigrationResult(m["num"], m["name"], m["headline"])
        # table existence for what this migration creates
        for t in m["creates"]:
            if atables is None:
                mr.findings.append(Finding(UNKNOWN, f"table {t}", "present", "not inspected"))
            elif t in atables:
                mr.findings.append(Finding(MATCH, f"table {t}"))
            else:
                mr.findings.append(Finding(DIFFER, f"table {t}", "present", "absent"))
        _check_columns(m["columns"], actual, mr.findings)
        _check_indexes(m["indexes"], actual, mr.findings)
        _check_seeds(m["seeds"], actual, mr.findings)
        if m["num"] == 1:
            _check_vector(actual, mr.findings)
            _check_tiflash(actual, mr.findings)
        report.migrations.append(mr)

    # Unexpected objects / data ------------------------------------------------
    if atables is not None:
        known = set(EXPECTED_TABLES) | {"schema_migrations"}
        report.unexpected_tables = sorted(t for t in atables if t not in known)
        report.schema_migrations_present = "schema_migrations" in atables
    report.schema_migrations_rows = [r.get("filename")
                                     for r in actual.get("schema_migrations_rows", [])]
    acols = actual.get("columns", {})
    extra_cols = []
    for table, exp_cols in EXPECTED_COLUMNS.items():
        got = acols.get(table)
        if not got:
            continue
        for col in got:
            if col not in exp_cols:
                extra_cols.append(f"{table}.{col}")
    report.unexpected_columns = sorted(extra_cols)
    report.unexpected_seed_rows = _collect_unexpected_seed_rows(actual)

    # Context: live data + backfill state -------------------------------------
    report.data_row_counts = dict(actual.get("row_counts", {}))
    report.tiflash_counts = {t: v.get("replica_count")
                             for t, v in (actual.get("tiflash") or {}).items()}
    if "fact_subject_null_entity" in actual:
        report.backfill = {
            "null_entity_id": actual.get("fact_subject_null_entity"),
            "total": actual.get("row_counts", {}).get("fact_subject"),
        }
    report.notes = list(actual.get("errors", []))
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_report(report: Report) -> str:
    L = []
    L.append("== READ-ONLY SCHEMA VERIFICATION (no changes made) ==\n")
    for m in report.migrations:
        L.append(f"Migration {m.num:02d} ({m.name}) — {m.headline}: {m.status}")
        shown = [f for f in m.findings if f.status != MATCH]
        n_match = sum(1 for f in m.findings if f.status == MATCH)
        if n_match:
            L.append(f"    {n_match} check(s) MATCH")
        for f in shown:
            line = f"    [{f.status}] {f.object}"
            if f.expected or f.actual:
                line += f"  expected: {f.expected!r}  actual: {f.actual!r}"
            L.append(line)
        L.append("")

    L.append("== unexpected objects / data (NOT in migrations 01,02,04-08) ==")
    if report.schema_migrations_present:
        if report.schema_migrations_rows:
            L.append("  - schema_migrations table EXISTS and records: "
                     + ", ".join(report.schema_migrations_rows))
        else:
            L.append("  - schema_migrations table EXISTS but is EMPTY "
                     "(records no applied migrations — e.g. left by a failed apply run)")
    if report.unexpected_tables:
        for t in report.unexpected_tables:
            L.append(f"  - unexpected table: {t}")
    if report.unexpected_columns:
        for c in report.unexpected_columns:
            L.append(f"  - unexpected column: {c}")
    if report.unexpected_seed_rows:
        for r in report.unexpected_seed_rows:
            L.append(f"  - extra registry/seed row: {r}")
    if report.clean_extra:
        L.append("  (none — no objects/data outside the migration set on the tracked surface)")
    L.append("")

    if report.data_row_counts:
        L.append("== live data present (informational; not part of schema equivalence) ==")
        for t, n in sorted(report.data_row_counts.items()):
            L.append(f"  {t}: {n} row(s)")
        if report.backfill:
            nn = report.backfill.get("null_entity_id")
            tot = report.backfill.get("total")
            state = "complete" if nn == 0 else f"INCOMPLETE ({nn} rows with NULL entity_id)"
            L.append(f"  05 backfill (fact_subject.entity_id): {state} of {tot} row(s)")
            if nn:
                L.append("    NOTE: 05's schema (entity/alias/evidence tables, entity_id")
                L.append("    column) MATCHes, but its one-time DATA backfill is not")
                L.append("    reflected in current rows. entity_id is nullable and set by")
                L.append("    the live write path, so rows written after the backfill")
                L.append("    legitimately carry NULL — this does NOT change schema")
                L.append("    equivalence, but re-running 05 would not reproduce it.")
        L.append("")

    if report.notes:
        L.append("== inspection notes / could-not-verify ==")
        for n in report.notes:
            L.append(f"  - {n}")
        L.append("")

    L.append("== verdict ==")
    if report.schema_equivalent:
        L.append("  SCHEMA EQUIVALENT — every migration's objects MATCH and nothing")
        L.append("  outside migrations 01,02,04-08 was found on the tracked surface.")
        sm_empty = report.schema_migrations_present and not report.schema_migrations_rows
        sm_pop = report.schema_migrations_present and report.schema_migrations_rows
        if not report.schema_migrations_present:
            L.append("  A baseline can be introduced cleanly: CREATE schema_migrations")
            L.append("  and record 01,02,04-08 as already applied.")
        elif sm_empty:
            L.append("  schema_migrations already EXISTS but is EMPTY (from the failed")
            L.append("  apply). A baseline appears SAFE — record 01,02,04-08 into the")
            L.append("  existing empty table (operator decision; not done here).")
        elif sm_pop:
            missing = [m["name"] for m in MIGRATIONS
                       if m["name"] not in report.schema_migrations_rows]
            L.append("  schema_migrations already records: "
                     + ", ".join(report.schema_migrations_rows))
            if missing:
                L.append("  Missing baseline rows for: " + ", ".join(missing))
        if report.backfill.get("null_entity_id"):
            L.append("  CAVEAT: 05's one-time data backfill is not reflected in current")
            L.append("  rows (see live-data section) — schema still matches.")
    else:
        reasons = []
        diffs = [f"{m.num:02d}" for m in report.migrations if m.status == DIFFER]
        unk = [f"{m.num:02d}" for m in report.migrations if m.status == UNKNOWN]
        if diffs:
            reasons.append(f"migrations DIFFER: {', '.join(diffs)}")
        if unk:
            reasons.append(f"migrations UNKNOWN (could not verify): {', '.join(unk)}")
        if report.unexpected_tables:
            reasons.append(f"{len(report.unexpected_tables)} unexpected table(s)")
        if report.unexpected_columns:
            reasons.append(f"{len(report.unexpected_columns)} unexpected column(s)")
        if report.unexpected_seed_rows:
            reasons.append(f"{len(report.unexpected_seed_rows)} extra registry row(s)")
        L.append("  NOT proven equivalent — do NOT baseline. Reasons:")
        for r in reasons:
            L.append(f"    - {r}")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Read-only introspection (the only DB-touching part)
# ---------------------------------------------------------------------------
_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_VEC_COL_RE = re.compile(r"`subject_vec`\s+vector\((\d+)\)", re.IGNORECASE)
_VEC_IDX_RE = re.compile(r"vector\s+index\s+`?([A-Za-z0-9_]+)`?", re.IGNORECASE)


def _autorandom_for(show_create: str, col: str) -> bool:
    """True if the given column line in SHOW CREATE TABLE carries AUTO_RANDOM."""
    for line in show_create.splitlines():
        s = line.strip()
        if s.startswith(f"`{col}`"):
            return "AUTO_RANDOM" in s.upper()
    return False


def inspect(run) -> dict:
    """Build an introspection snapshot using ONLY read-only queries.

    `run(sql, params)` must execute a single read query and return a list of
    tuples (positional). This function issues nothing but SELECT / SHOW against
    INFORMATION_SCHEMA and the seed/registry tables — never DDL or DML.
    """
    actual: dict = {"errors": []}

    def safe(label, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - collect, keep going, never raise
            actual["errors"].append(f"{label}: {type(e).__name__}: {e}")
            return None

    # tables
    def _tables():
        rows = run("SELECT table_name FROM information_schema.tables "
                   "WHERE table_schema = DATABASE()", {})
        return {r[0] for r in rows}
    tset = safe("tables", _tables)
    if tset is not None:
        actual["tables"] = tset
    present = actual.get("tables", set())

    # columns
    def _columns():
        rows = run("SELECT table_name, column_name, column_type, is_nullable, "
                   "column_default FROM information_schema.columns "
                   "WHERE table_schema = DATABASE() ORDER BY table_name, ordinal_position", {})
        cols: dict = {}
        for tname, cname, ctype, isnull, cdefault in rows:
            cols.setdefault(tname, {})[cname] = {
                "type": ctype,
                "nullable": (str(isnull).upper() == "YES"),
                "default": cdefault,
                "auto_random": None,   # filled from SHOW CREATE below
            }
        return cols
    cols = safe("columns", _columns)
    if cols is not None:
        actual["columns"] = cols

    # indexes + primary keys
    def _stats():
        rows = run("SELECT table_name, index_name, non_unique, seq_in_index, column_name "
                   "FROM information_schema.statistics WHERE table_schema = DATABASE() "
                   "ORDER BY table_name, index_name, seq_in_index", {})
        idx: dict = {}
        pk: dict = {}
        for tname, iname, nonuniq, seq, cname in rows:
            if iname == "PRIMARY":
                pk.setdefault(tname, []).append(cname)
            else:
                d = idx.setdefault(tname, {}).setdefault(
                    iname, {"columns": [], "unique": (int(nonuniq) == 0)})
                # column_name is NULL for expression/vector indexes; skip Nones
                if cname is not None:
                    d["columns"].append(cname)
        # freeze column lists to tuples
        for t in idx:
            for i in idx[t]:
                idx[t][i]["columns"] = tuple(idx[t][i]["columns"])
        return idx, pk
    stats = safe("indexes", _stats)
    if stats is not None:
        actual["indexes"], actual["pk"] = stats

    # TiFlash replicas
    def _tiflash():
        rows = run("SELECT table_name, replica_count, available "
                   "FROM information_schema.tiflash_replica WHERE table_schema = DATABASE()", {})
        return {r[0]: {"replica_count": int(r[1]),
                       "available": bool(int(r[2]))} for r in rows}
    tf = safe("tiflash", _tiflash)
    if tf is not None:
        actual["tiflash"] = tf

    # SHOW CREATE TABLE for AUTO_RANDOM + vector column/index detection
    actual["vector_columns"] = {}
    actual["vector_indexes"] = {}
    for t in sorted(present):
        if not _IDENT_RE.match(t):
            continue

        def _show(tbl=t):
            rows = run(f"SHOW CREATE TABLE `{tbl}`", {})
            return rows[0][1] if rows else ""
        ddl = safe(f"show create {t}", _show)
        if not ddl:
            continue
        # AUTO_RANDOM per column
        if "columns" in actual and t in actual["columns"]:
            for cname, meta in actual["columns"][t].items():
                meta["auto_random"] = _autorandom_for(ddl, cname)
        # vector column + index (only fact_subject has them, but detect generically)
        mvc = _VEC_COL_RE.search(ddl)
        if mvc and t == VECTOR_TABLE:
            actual["vector_columns"].setdefault(t, {})[VECTOR_COLUMN] = int(mvc.group(1))
        for mvi in _VEC_IDX_RE.finditer(ddl):
            actual["vector_indexes"].setdefault(t, set()).add(mvi.group(1))

    # Seed / registry rows
    def _predicate_rule():
        rows = run("SELECT predicate, value_type FROM predicate_rule", {})
        return [{"predicate": r[0], "value_type": r[1]} for r in rows]

    def _source_rank():
        rows = run("SELECT source, `rank` FROM source_rank", {})
        return [{"source": r[0], "rank": r[1]} for r in rows]

    def _authority_policy():
        rows = run("SELECT tenant_id, predicate, source, source_type, authority, "
                   "priority, policy_version FROM authority_policy", {})
        return [{"tenant_id": r[0], "predicate": r[1], "source": r[2],
                 "source_type": r[3], "authority": r[4], "priority": r[5],
                 "policy_version": r[6]} for r in rows]

    seed_rows: dict = {}
    for name, fn in (("predicate_rule", _predicate_rule),
                     ("source_rank", _source_rank),
                     ("authority_policy", _authority_policy)):
        if name in present:
            got = safe(f"seed {name}", fn)
            if got is not None:
                seed_rows[name] = got
    actual["seed_rows"] = seed_rows

    # schema_migrations contents (the tracking table). It may exist but be EMPTY —
    # e.g. a failed apply that created the table then errored on 01. Read what it
    # actually records so the report can distinguish "absent" / "empty" / "populated".
    if "schema_migrations" in present:
        def _sm():
            rows = run("SELECT filename, checksum FROM schema_migrations "
                       "ORDER BY filename", {})
            return [{"filename": r[0], "checksum": r[1]} for r in rows]
        sm = safe("schema_migrations", _sm)
        if sm is not None:
            actual["schema_migrations_rows"] = sm

    # Live data row counts + 05 backfill state
    counts: dict = {}
    for t in DATA_TABLES:
        if t in present and _IDENT_RE.match(t):
            def _count(tbl=t):
                rows = run(f"SELECT COUNT(*) FROM `{tbl}`", {})
                return int(rows[0][0])
            n = safe(f"count {t}", _count)
            if n is not None:
                counts[t] = n
    actual["row_counts"] = counts

    if ("fact_subject" in present
            and "columns" in actual
            and "entity_id" in actual["columns"].get("fact_subject", {})):
        def _null_entity():
            rows = run("SELECT COUNT(*) FROM fact_subject WHERE entity_id IS NULL", {})
            return int(rows[0][0])
        nn = safe("backfill fact_subject.entity_id", _null_entity)
        if nn is not None:
            actual["fact_subject_null_entity"] = nn

    return actual
