-- TiDB Governed Fact Layer — Migration 04: predicate-specific, versioned authority
-- Builds on 01_schema.sql + 02_seed.sql. Idempotent where TiDB supports IF NOT EXISTS.
--
-- WHY: a source's authority depends on WHAT is being asserted. A chargeback
-- processor is authoritative for `chargeback` but not automatically for
-- `fraud_status`; a user is low-authority for `fraud_status`. The old global
-- source_rank (02_seed.sql) cannot express that. This migration introduces a
-- versioned, predicate-specific authority policy the adjudicator consults, and
-- records the policy_version that produced each resolution so historical
-- resolutions are never silently recomputed under a newer policy.
--
-- source_rank is intentionally LEFT IN PLACE (reference/back-compat); the
-- adjudication path now reads authority_policy. The wildcard rows below mirror
-- the legacy ranks, so any predicate WITHOUT a specific policy adjudicates
-- exactly as it did before this migration.

CREATE TABLE IF NOT EXISTS authority_policy (
  policy_id      BIGINT       AUTO_RANDOM PRIMARY KEY,   -- AUTO_RANDOM: no TiKV hotspot
  tenant_id      VARCHAR(64)  NOT NULL DEFAULT '*',      -- '*' = applies to all tenants
  predicate      VARCHAR(128) NOT NULL,                  -- '*' = any predicate (fallback)
  source         VARCHAR(255) NOT NULL,                  -- the asserting source
  source_type    VARCHAR(64)  DEFAULT NULL,              -- optional grouping (human/system/model/agent/user)
  authority      INT          NOT NULL,                  -- higher wins the authority gate
  priority       INT          NOT NULL DEFAULT 0,        -- tie-break among matching rows (higher first)
  policy_version VARCHAR(64)  NOT NULL,                  -- recorded on every resolution it produces
  effective_from DATETIME(6)  NOT NULL DEFAULT '1970-01-01 00:00:00.000000',
  effective_to   DATETIME(6)  DEFAULT NULL,              -- NULL = open-ended
  created_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
  KEY idx_authority_lookup (source, predicate, effective_from)
);

-- Back-compat: wildcard rows == the legacy global source_rank. A predicate with
-- no specific policy falls back to these, preserving pre-migration behavior.
INSERT INTO authority_policy (predicate, source, source_type, authority, priority, policy_version) VALUES
  ('*', 'system_of_record',     'system', 100, 0, 'authority-v1'),
  ('*', 'verified_integration', 'system',  80, 0, 'authority-v1'),
  ('*', 'user_assertion',       'user',    60, 0, 'authority-v1'),
  ('*', 'agent_inference',      'agent',   40, 0, 'authority-v1');

-- Fraud demo: predicate-specific authority for `fraud_status`. Note user_assertion
-- is LOW (20) here — so a customer's "I'm legitimate" is retained as CONTRARY
-- evidence against a fraud_review/human confirmation, NOT an automatic dispute.
-- Two comparably-authoritative sources (e.g. two human reviewers) DO dispute.
INSERT INTO authority_policy (predicate, source, source_type, authority, priority, policy_version) VALUES
  ('fraud_status', 'human_investigator', 'human',  100, 10, 'fraud-policy-v1'),
  ('fraud_status', 'fraud_review',       'system',  95, 10, 'fraud-policy-v1'),
  ('fraud_status', 'fraud_model',        'model',   70,  5, 'fraud-policy-v1'),
  ('fraud_status', 'agent_inference',    'agent',   55,  5, 'fraud-policy-v1'),
  ('fraud_status', 'user_assertion',     'user',    20,  1, 'fraud-policy-v1');

-- Resolution provenance: which policy produced the current truth, and why.
ALTER TABLE fact_current ADD COLUMN IF NOT EXISTS policy_version    VARCHAR(64)  DEFAULT NULL;
ALTER TABLE fact_current ADD COLUMN IF NOT EXISTS resolution_reason VARCHAR(255) DEFAULT NULL;

-- Per-event adjudication provenance. These are ADDITIVE audit columns and are
-- deliberately NOT part of the per-subject hash chain (hashing.HASHED_FIELDS is
-- unchanged) — the chain still commits to exactly the same content as before.
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS outcome        VARCHAR(24) DEFAULT NULL;
ALTER TABLE fact_event ADD COLUMN IF NOT EXISTS policy_version VARCHAR(64) DEFAULT NULL;
