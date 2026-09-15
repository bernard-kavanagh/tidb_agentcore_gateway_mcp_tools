-- TiDB Governed Fact Layer — Migration 07: idempotent writes
-- Builds on 01/04/05/06. Idempotent where TiDB supports IF NOT EXISTS.
--
-- AgentCore / Lambda / MCP clients retry. A retried logical assertion must NOT
-- append a duplicate fact_event. The guarantee is at the DATABASE level, not just
-- in application code:
--
--   * (tenant_id, idempotency_key) is the PRIMARY KEY.
--   * record_fact claims the key with an INSERT at the START of the write
--     transaction. A concurrent duplicate's INSERT blocks on the key until the
--     first txn commits, then fails with a duplicate-key error — so the loser's
--     whole transaction (including any event append) rolls back. No duplicate
--     event is ever produced.
--   * On commit the row is updated with the produced event_id + result, so a
--     later retry replays the SAME result instead of re-doing the work.

CREATE TABLE IF NOT EXISTS fact_idempotency (
  tenant_id       VARCHAR(64)  NOT NULL,
  idempotency_key VARCHAR(255) NOT NULL,
  event_id        BIGINT       DEFAULT NULL,   -- the event the first call produced
  result_json     JSON         DEFAULT NULL,   -- the first call's full result (replayed on retry)
  created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, idempotency_key)     -- uniqueness + per-key serialization lock
);
