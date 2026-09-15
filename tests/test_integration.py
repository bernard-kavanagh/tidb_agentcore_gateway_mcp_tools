"""Integration tests that require a live TiDB (idempotency, tenant isolation, the
full write path). They are GATED on $TIDB_TEST_URL and SKIP cleanly when it is
unset, so CI without a cluster stays green and we never falsely claim they ran.

Run against a scratch TiDB database with the schema applied:

    mysql ... < sql/01_schema.sql
    mysql ... < sql/04_authority_policy.sql
    mysql ... < sql/05_entity_evidence.sql
    mysql ... < sql/06_temporal.sql
    mysql ... < sql/07_idempotency.sql
    mysql ... < sql/02_seed.sql
    TIDB_TEST_URL='mysql+pymysql://user:pass@host:4000/scratch?ssl_verify_cert=true' \
        python3 tests/test_integration.py

Bedrock embeddings and Secrets Manager are stubbed so the test exercises the
adjudication/idempotency/tenant logic against real TiDB without external calls.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TIDB_TEST_URL = os.environ.get("TIDB_TEST_URL")


def _event(tenant, **overrides):
    ev = {
        "_fact_ctx": {
            "tenant_id": tenant, "agent_id": "itest-agent",
            "session_id": "itest-sess", "tenant_secret_arn": "stub",
        },
        "subject": "account-itest", "predicate": "status", "value": "active",
        "source": "system_of_record", "confidence": 0.9,
    }
    ev.update(overrides)
    return ev


def _wire_stubs():
    """Point every handler at the test engine and stub Bedrock/Secrets."""
    from sqlalchemy import create_engine
    from handlers import (record_fact as rf, get_fact, explain_fact,
                          get_fact_history, list_disputes, search_entities,
                          retract_fact)
    from handlers.common import embeddings

    engine = create_engine(TIDB_TEST_URL, pool_pre_ping=True, future=True)
    for mod in (rf, get_fact, explain_fact, get_fact_history, list_disputes,
                search_entities, retract_fact):
        mod.get_engine = lambda _arn, _e=engine: _e            # bypass Secrets Manager
    embeddings.embed = lambda _text: [0.0] * embeddings.EMBED_DIM  # bypass Bedrock
    return rf, engine


def _read_mods():
    from handlers import get_fact, explain_fact
    return get_fact, explain_fact


def test_cross_tenant_read_isolation(rf):
    # Defense in depth BEHIND Cedar: even if a handler runs, its tenant-scoped
    # WHERE means tenant B cannot see tenant A's fact.
    get_fact, explain_fact = _read_mods()
    subj = "xtenant-" + os.urandom(6).hex()
    rf.handler(_event("itest-a", subject=subj, source="system_of_record"))
    # tenant B asks for tenant A's subject -> not found (never leaks).
    ev_b = {"_fact_ctx": {"tenant_id": "itest-b", "agent_id": "a",
                          "session_id": "s", "tenant_secret_arn": "stub"},
            "subject": subj, "predicate": "status"}
    assert get_fact.handler(ev_b).get("error_type") == "NotFound"
    assert explain_fact.handler(ev_b).get("error_type") == "NotFound"


def test_idempotent_write_replays(rf):
    key = "idem-" + os.urandom(6).hex()
    ev = _event("itest-a", subject=f"acct-{key}", idempotency_key=key)
    r1 = rf.handler(ev)
    assert "error" not in r1, r1
    r2 = rf.handler(ev)  # retry — same logical request
    assert r2.get("idempotent_replay") is True, r2
    assert r1["event_id"] == r2["event_id"], "retry must not create a new event"


def test_distinct_idempotency_keys_create_distinct_events(rf):
    base = "acct-" + os.urandom(6).hex()
    r1 = rf.handler(_event("itest-a", subject=base, idempotency_key="k1-" + base))
    r2 = rf.handler(_event("itest-a", subject=base, idempotency_key="k2-" + base))
    assert r1["event_id"] != r2["event_id"]


def test_no_key_is_not_idempotent(rf):
    base = "acct-" + os.urandom(6).hex()
    r1 = rf.handler(_event("itest-a", subject=base))
    r2 = rf.handler(_event("itest-a", subject=base))  # corroboration, new event each time
    assert r1["event_id"] != r2["event_id"]


TESTS = [
    test_idempotent_write_replays,
    test_distinct_idempotency_keys_create_distinct_events,
    test_no_key_is_not_idempotent,
    test_cross_tenant_read_isolation,
]


if __name__ == "__main__":
    if not TIDB_TEST_URL:
        print("SKIP: set $TIDB_TEST_URL (scratch TiDB, schema applied) to run integration tests")
        sys.exit(0)
    rf, _ = _wire_stubs()
    failed = 0
    for t in TESTS:
        try:
            t(rf)
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} integration tests passed")
    sys.exit(1 if failed else 0)
