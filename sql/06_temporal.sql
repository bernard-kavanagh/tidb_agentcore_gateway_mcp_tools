-- TiDB Governed Fact Layer — Migration 06: explicit temporal semantics
-- Builds on 01/04/05. Idempotent where TiDB supports IF NOT EXISTS.
--
-- Distinguishes four times that were previously collapsed onto observed_at:
--   valid_from / valid_to : the period the fact is TRUE in the real world
--                           (e.g. "device X belonged to customer Y last week").
--   asserted_at           : when the assertion was MADE (defaults to observed_at).
--   observed_at           : when the source observed it (handler-set; hashed).
--   ingested_at           : when THIS service recorded the row (DB clock, NOT hashed
--                           because it is non-deterministic system bookkeeping).
--
-- valid_from/valid_to/asserted_at are MATERIAL claims and are folded into the
-- per-subject hash chain (see hashing.HASHED_FIELDS). ingested_at is not.

ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS valid_from  DATETIME(6) DEFAULT NULL;
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS valid_to    DATETIME(6) DEFAULT NULL;
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS asserted_at DATETIME(6) DEFAULT NULL;
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS ingested_at DATETIME    DEFAULT CURRENT_TIMESTAMP;

-- Current resolution carries the validity window of the winning assertion so
-- "as-of" reads and explain_fact can report when the current truth holds.
ALTER TABLE fact_current ADD COLUMN IF NOT EXISTS valid_from DATETIME(6) DEFAULT NULL;
ALTER TABLE fact_current ADD COLUMN IF NOT EXISTS valid_to   DATETIME(6) DEFAULT NULL;

-- Index to support as-of / validity-window queries per subject.
ALTER TABLE fact_event ADD INDEX IF NOT EXISTS idx_event_valid (tenant_id, subject_id, valid_from);
