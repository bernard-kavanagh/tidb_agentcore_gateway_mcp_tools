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

-- 1. Resource control group: OMITTED. This cluster is TiDB Cloud Serverless,
--    where CREATE RESOURCE GROUP / SET RESOURCE GROUP are not supported
--    (see https://docs.pingcap.com/tidbcloud/limited-sql-features). Resource
--    isolation between callers is a Dedicated-tier feature — on Serverless the
--    platform meters capacity by RU/quota, not by user-attached resource groups.
--    The data-scope guarantee below (least-privilege grants) is unaffected.

-- 2. The scoped application user (one per tenant or per agent-type; here a
--    single fact-layer role — extend with one CREATE USER per tenant if you
--    issue per-tenant secrets). NOTE: this Serverless cluster namespaces users
--    under a cluster prefix (e.g. 'QxfqVLLusLjpqEY.'); the app then connects as
--    '<prefix>.<FACT_ROLE>'. Confirm whether CREATE USER prepends the prefix
--    automatically or FACT_ROLE must include it, and set FACT_ROLE to match how
--    the handlers' secret authenticates.
CREATE USER '<FACT_ROLE>'@'%'
  IDENTIFIED BY '<PASSWORD_FROM_SECRET>';

-- 3. Minimal DML grants on the fact-layer schema only.
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_subject    TO '<FACT_ROLE>'@'%';
GRANT SELECT, INSERT         ON <DB>.fact_event       TO '<FACT_ROLE>'@'%';  -- append-only: no UPDATE/DELETE
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_current     TO '<FACT_ROLE>'@'%';
GRANT SELECT, INSERT         ON <DB>.entity           TO '<FACT_ROLE>'@'%';  -- resolve-or-create canonical entities (INSERT IGNORE + SELECT; never UPDATEd by any handler)
-- entity_alias: intentionally NOT granted — no handler reads or writes it at
-- runtime (verified against handler DML). Add a grant here if/when a handler
-- starts using it; keeping it off is genuine least-privilege.
GRANT SELECT, INSERT         ON <DB>.fact_evidence    TO '<FACT_ROLE>'@'%';  -- append evidence references
GRANT SELECT, INSERT, UPDATE ON <DB>.fact_idempotency TO '<FACT_ROLE>'@'%';  -- retry-safe writes
GRANT SELECT                 ON <DB>.predicate_rule   TO '<FACT_ROLE>'@'%';
GRANT SELECT                 ON <DB>.source_rank      TO '<FACT_ROLE>'@'%';  -- legacy/reference
GRANT SELECT                 ON <DB>.authority_policy TO '<FACT_ROLE>'@'%';  -- predicate-specific authority (adjudication)

FLUSH PRIVILEGES;

-- Note on append-only enforcement: fact_event is granted SELECT + INSERT only,
-- so even a compromised handler cannot mutate or delete a logged event — the
-- tamper-evidence (hash chain) is backed by a privilege the role does not hold.
