-- TiDB Governed Fact Layer — Phase A schema
-- Run against the TiDB Cloud cluster (Dedicated/Premium; the HNSW vector index is tier-gated).
--
-- Confirmed operator decisions baked in here:
--   * Hash-chain scope : PER-SUBJECT. The chain tip is tracked on fact_subject.last_event_hash
--                        (see note below) because event_id is AUTO_RANDOM and therefore NOT
--                        monotonic — you cannot order events by PK to find "the latest event".
--   * Embedding        : Titan Text Embeddings V2 @ 1024 dims  ->  VECTOR(1024)
--   * Vector distance  : cosine (VEC_COSINE_DISTANCE) — correct for Titan text embeddings.
--
-- Hard constraints honoured:
--   * AUTO_RANDOM on every clustered PK — never AUTO_INCREMENT (avoids TiKV write hotspot).
--   * fact_current is a WRITE-THROUGH table, upserted in the same txn as the fact_event append.
--   * fact_event is APPEND-ONLY and tamper-EVIDENT (hash chain) — not immutable.

-- ---------------------------------------------------------------------------
-- 1. fact_subject — the fact's identity (one row per tenant+subject+predicate)
-- ---------------------------------------------------------------------------
-- The HNSW vector index is declared inline, which makes TiDB auto-create the
-- required TiFlash replica for this table. Vector indexes require fixed-dim
-- vectors and a TiFlash replica.
CREATE TABLE fact_subject (
  subject_id      BIGINT       AUTO_RANDOM PRIMARY KEY,
  tenant_id       VARCHAR(64)  NOT NULL,
  subject         VARCHAR(255) NOT NULL,
  predicate       VARCHAR(128) NOT NULL,
  subject_vec     VECTOR(1024),                     -- Titan Text Embeddings V2 (1024)
  -- Per-subject hash-chain tip: event_hash of the most recently appended event
  -- for this subject. The write path locks this row (SELECT ... FOR UPDATE) to
  -- serialize concurrent writes to the same subject and read prev_hash atomically.
  -- NULL until the first event; the first event's prev_hash is the genesis constant.
  last_event_hash CHAR(64)     DEFAULT NULL,
  created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_identity (tenant_id, subject, predicate),
  VECTOR INDEX idx_subject_vec ((VEC_COSINE_DISTANCE(subject_vec)))
);

-- ---------------------------------------------------------------------------
-- 2. fact_event — append-only audit log (many events -> one subject/session)
-- ---------------------------------------------------------------------------
CREATE TABLE fact_event (
  event_id            BIGINT      AUTO_RANDOM PRIMARY KEY,   -- monotonic log; AUTO_RANDOM avoids hotspot
  subject_id          BIGINT      NOT NULL,                  -- -> fact_subject.subject_id
  tenant_id           VARCHAR(64) NOT NULL,
  op                  ENUM('assert','supersede','retract','correct') NOT NULL,
  value               JSON        NOT NULL,
  source              VARCHAR(255) NOT NULL,
  confidence          DECIMAL(4,3),
  agent_id            VARCHAR(128) NOT NULL,
  session_id          VARCHAR(128) NOT NULL,
  tool_turn           INT,
  supersedes_event_id BIGINT,                                -- prior event this overrides (supersede/correct)
  observed_at         DATETIME(6) NOT NULL,                  -- set by the handler (in the hash); microsecond precision
  prev_hash           CHAR(64)    NOT NULL,                  -- previous event_hash for THIS subject (genesis for first)
  event_hash          CHAR(64)    NOT NULL,                  -- sha256(canonical event fields || prev_hash)
  KEY idx_subject (tenant_id, subject_id, observed_at),
  KEY idx_session (tenant_id, session_id),
  KEY idx_chain   (subject_id, prev_hash)                    -- walk the per-subject chain during verification
);
-- Columnar replica for lineage/audit rollups and query_tenant_metrics analytics.
ALTER TABLE fact_event SET TIFLASH REPLICA 1;

-- ---------------------------------------------------------------------------
-- 3. fact_current — write-through current truth (NOT a materialized view)
-- ---------------------------------------------------------------------------
-- Upserted in the SAME transaction as the fact_event append. TiDB has no
-- incrementally-maintained materialized view — do not build or assume one.
CREATE TABLE fact_current (
  subject_id       BIGINT      PRIMARY KEY,          -- -> fact_subject.subject_id (1:1)
  tenant_id        VARCHAR(64) NOT NULL,
  value            JSON        NOT NULL,
  winning_event_id BIGINT      NOT NULL,             -- the event that currently holds
  status           ENUM('active','superseded','retracted','disputed') NOT NULL DEFAULT 'active',
  updated_at       DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_tenant_status (tenant_id, status)
);

-- ---------------------------------------------------------------------------
-- 4. predicate_rule — conflict rule per predicate (the contradiction test's registry)
-- ---------------------------------------------------------------------------
CREATE TABLE predicate_rule (
  predicate  VARCHAR(128) PRIMARY KEY,
  value_type ENUM('scalar','monotonic','set','freetext') NOT NULL
);

-- ---------------------------------------------------------------------------
-- 5. source_rank — source authority ranking for the authority gate (higher wins)
-- ---------------------------------------------------------------------------
CREATE TABLE source_rank (
  source VARCHAR(255) PRIMARY KEY,
  `rank` INT NOT NULL          -- `rank` is a reserved word in TiDB 8.x; must be quoted
);

-- ---------------------------------------------------------------------------
-- Optional lineage dimension (deferred for v1; session_id already lives on fact_event).
-- Uncomment only if richer cross-session rollups are wanted.
-- ---------------------------------------------------------------------------
-- CREATE TABLE agent_session (
--   session_id VARCHAR(128) PRIMARY KEY,
--   actor_id   VARCHAR(128),
--   agent_id   VARCHAR(128),
--   started_at DATETIME
-- );
