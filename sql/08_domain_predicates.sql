-- TiDB Governed Fact Layer — Migration 08: domain predicates for callers
-- Builds on 02_seed.sql (predicate registry) + 04 (authority policy).
--
-- record_fact rejects any predicate not in predicate_rule. The fraud caller
-- (fraud_on-aws) writes verdicts under domain predicates, and 04 already declared
-- predicate-specific AUTHORITY for `fraud_status` — this registers the predicates
-- themselves (their value_type) so the write path accepts them, and adds matching
-- authority for `liability_status` (betting). Both are scalar: a differing value
-- is a contradiction resolved by the authority gate.
--
-- The service stays domain-neutral; these are just additional registry rows a
-- fraud/betting deployment seeds. INSERT IGNORE keeps it idempotent.

INSERT IGNORE INTO predicate_rule (predicate, value_type) VALUES
  ('fraud_status',     'scalar'),
  ('liability_status', 'scalar');

-- Predicate-specific authority for liability_status (mirrors fraud_status in 04).
INSERT IGNORE INTO authority_policy (predicate, source, source_type, authority, priority, policy_version) VALUES
  ('liability_status', 'human_investigator', 'human',  100, 10, 'fraud-policy-v1'),
  ('liability_status', 'risk_engine',        'system',  90, 10, 'fraud-policy-v1'),
  ('liability_status', 'fraud_model',        'model',   70,  5, 'fraud-policy-v1'),
  ('liability_status', 'agent_inference',    'agent',   55,  5, 'fraud-policy-v1'),
  ('liability_status', 'user_assertion',     'user',    20,  1, 'fraud-policy-v1');
