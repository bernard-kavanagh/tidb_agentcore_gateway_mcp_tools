-- TiDB Governed Fact Layer — Phase A registry seed
-- Confirmed v1 starter sets. Extend by INSERTing rows; the handler is data-driven,
-- so new predicates/sources need no code change.

-- Predicate registry — one predicate per value_type so every adjudication branch
-- is exercisable. Domain-neutral names.
--   scalar    : new != current (both valid) -> contradiction -> authority gate
--   monotonic : later observation supersedes (the ONLY place recency supersedes)
--   set       : new member -> union/extend, never supersede
--   freetext  : default -> dispute (a semantic pre-filter may confirm routing, never decide)
INSERT INTO predicate_rule (predicate, value_type) VALUES
  ('status',        'scalar'),
  ('last_seen',     'monotonic'),
  ('known_aliases', 'set'),
  ('notes',         'freetext');

-- Source authority ranking — higher rank wins the authority gate on a contradiction.
-- Unknown sources default to rank 0 in the handler.
INSERT INTO source_rank (source, rank) VALUES
  ('system_of_record',    100),
  ('verified_integration', 80),
  ('user_assertion',       60),
  ('agent_inference',      40);
