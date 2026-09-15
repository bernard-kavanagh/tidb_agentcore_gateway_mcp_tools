-- TiDB Governed Fact Layer — Migration 05: canonical entities + evidence references
-- Builds on 01/04. Idempotent where TiDB supports IF NOT EXISTS.
--
-- Makes the Evidence -> Assessment -> Resolution model concrete:
--   * entity        : a canonical real-world subject (customer/device/ip/card/...),
--                     so facts across investigations can reference the SAME entity
--                     instead of only a free-form subject string.
--   * fact_evidence : references BACK to fraud-domain observations (transaction,
--                     alert, investigation, model_run, chargeback, ...). The fact
--                     layer stores the REFERENCE, never a copy of the domain record.
--   * fact_event    : gains assessor_type (human/system/model/agent/user) so an
--                     assertion records what KIND of assessor produced it.
--
-- Compatibility: entity_id on fact_subject is NULLABLE; subject/predicate identity
-- is unchanged. Existing rows are backfilled below. Existing callers keep working.

-- ---------------------------------------------------------------------------
-- 1. entity — canonical subject identity
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entity (
  entity_id     BIGINT       AUTO_RANDOM PRIMARY KEY,
  tenant_id     VARCHAR(64)  NOT NULL,
  entity_type   VARCHAR(64)  NOT NULL,               -- customer|account|device|ip|card|merchant|transaction|investigation|...
  canonical_key VARCHAR(255) NOT NULL,               -- deterministic: "<entity_type>:<normalized-id>"
  display_name  VARCHAR(255) DEFAULT NULL,
  created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_entity_canonical (tenant_id, canonical_key),
  KEY idx_entity_type (tenant_id, entity_type)
);

-- Optional alias table (kept minimal): alternate keys that map to one canonical entity.
-- Alias -> entity mapping is asserted, never inferred here (no auto entity resolution).
CREATE TABLE IF NOT EXISTS entity_alias (
  alias_id    BIGINT       AUTO_RANDOM PRIMARY KEY,
  tenant_id   VARCHAR(64)  NOT NULL,
  entity_id   BIGINT       NOT NULL,
  alias_key   VARCHAR(255) NOT NULL,
  alias_type  VARCHAR(64)  DEFAULT NULL,
  created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_alias (tenant_id, alias_key),
  KEY idx_alias_entity (tenant_id, entity_id)
);

-- Link the fact's identity to its canonical entity (nullable for back-compat).
ALTER TABLE fact_subject ADD COLUMN IF NOT EXISTS entity_id BIGINT DEFAULT NULL;
-- (No FK constraint: keep the hot write table free of cross-table locks; the
-- handler enforces the reference. Add one later only if the cluster version
-- supports FKs on hot tables comfortably.)

-- ---------------------------------------------------------------------------
-- 2. fact_evidence — references to fraud-domain observations (NOT copies)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_evidence (
  evidence_id   BIGINT       AUTO_RANDOM PRIMARY KEY,
  tenant_id     VARCHAR(64)  NOT NULL,
  event_id      BIGINT       NOT NULL,               -- the asserting assessment (fact_event) this evidence backs
  subject_id    BIGINT       NOT NULL,               -- denormalized for per-subject evidence lookup
  entity_id     BIGINT       DEFAULT NULL,
  evidence_type VARCHAR(64)  NOT NULL,               -- transaction|alert|investigation|model_run|chargeback|device_signal|document|...
  evidence_ref  VARCHAR(255) NOT NULL,               -- external id in the fraud system (e.g. TX-123, INV-847)
  relationship  VARCHAR(24)  NOT NULL DEFAULT 'SUPPORTS',  -- SUPPORTS|CONTRADICTS|DERIVED_FROM|TRIGGERED_BY|REVIEWED_IN
  metadata      JSON         DEFAULT NULL,
  observed_at   DATETIME(6)  DEFAULT NULL,
  created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
  KEY idx_evidence_event   (tenant_id, event_id),
  KEY idx_evidence_subject (tenant_id, subject_id),
  KEY idx_evidence_ref     (tenant_id, evidence_type, evidence_ref)
);

-- ---------------------------------------------------------------------------
-- 3. fact_event — record the KIND of assessor (additive; NOT hashed)
-- ---------------------------------------------------------------------------
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS assessor_type VARCHAR(32) DEFAULT NULL;

-- ---------------------------------------------------------------------------
-- 4. Backfill: mint a canonical entity per existing (tenant, subject) and link it.
--    entity_type defaults to 'unknown' for legacy subjects with no declared type;
--    canonical_key = "unknown:<subject>" so it is deterministic and unique.
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO entity (tenant_id, entity_type, canonical_key, display_name)
SELECT DISTINCT fs.tenant_id, 'unknown', CONCAT('unknown:', fs.subject), fs.subject
FROM fact_subject fs
WHERE fs.entity_id IS NULL;

UPDATE fact_subject fs
JOIN entity e
  ON e.tenant_id = fs.tenant_id
 AND e.canonical_key = CONCAT('unknown:', fs.subject)
SET fs.entity_id = e.entity_id
WHERE fs.entity_id IS NULL;
