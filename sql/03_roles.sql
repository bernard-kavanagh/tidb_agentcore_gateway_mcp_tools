-- TiDB scoped role + resource control group — the last-line data scope,
-- independent of Cedar. Cedar authorizes the CALL; this role scopes the DATA.
--
-- Run as an admin on the cluster. Replace the <PLACEHOLDERS>. The role's
-- password is stored in the Secrets Manager secret the handlers read by ARN —
-- keep it out of source and out of env vars.
--
-- Grant ONLY the DML the handlers actually issue on the five tables:
--   record_fact          : INSERT/UPDATE/SELECT on fact_subject, fact_event, fact_current;
--                          SELECT on predicate_rule, source_rank, authority_policy
--   vector_search        : SELECT on fact_subject, fact_current
--   query_tenant_metrics : SELECT on fact_current, fact_event
-- No DDL, no DELETE (the log is append-only), no access to other schemas.

-- 1. Resource control group so one caller's load cannot starve others.
--    Tune RU_PER_SEC to the cluster; BURSTABLE lets it borrow idle capacity.
CREATE RESOURCE GROUP IF NOT EXISTS rg_fact_layer
  RU_PER_SEC = 2000
  BURSTABLE;

-- 2. The scoped application user (one per tenant or per agent-type; here a
--    single fact-layer role — extend with one CREATE USER per tenant if you
--    issue per-tenant secrets).
CREATE USER '<FACT_ROLE>'@'%'
  IDENTIFIED BY '<PASSWORD_FROM_SECRET>'
  RESOURCE GROUP rg_fact_layer;

-- 3. Minimal DML grants on the fact-layer schema only.
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_subject    TO '<FACT_ROLE>'@'%';
GRANT SELECT, INSERT         ON <DB>.fact_event       TO '<FACT_ROLE>'@'%';  -- append-only: no UPDATE/DELETE
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_current     TO '<FACT_ROLE>'@'%';
GRANT SELECT, INSERT, UPDATE ON <DB>.entity           TO '<FACT_ROLE>'@'%';  -- resolve-or-create canonical entities
GRANT SELECT, INSERT         ON <DB>.entity_alias     TO '<FACT_ROLE>'@'%';
GRANT SELECT, INSERT         ON <DB>.fact_evidence    TO '<FACT_ROLE>'@'%';  -- append evidence references
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_idempotency TO '<FACT_ROLE>'@'%';  -- retry-safe writes
GRANT SELECT                 ON <DB>.predicate_rule   TO '<FACT_ROLE>'@'%';
GRANT SELECT                 ON <DB>.source_rank      TO '<FACT_ROLE>'@'%';  -- legacy/reference
GRANT SELECT                 ON <DB>.authority_policy TO '<FACT_ROLE>'@'%';  -- predicate-specific authority (adjudication)

FLUSH PRIVILEGES;

-- Note on append-only enforcement: fact_event is granted SELECT + INSERT only,
-- so even a compromised handler cannot mutate or delete a logged event — the
-- tamper-evidence (hash chain) is backed by a privilege the role does not hold.
