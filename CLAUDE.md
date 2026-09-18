# CLAUDE.md — TiDB Governed Fact Layer (Phase A)

Governed fact-of-record service for AI agents: facts are written through a deterministic adjudication path into TiDB and served as tool handlers behind an Amazon Bedrock AgentCore Gateway, with per-tenant Cedar isolation enforced *before* any query runs and a tamper-evident audit log of every change.

**Full build spec: `SPEC.md`. Read it before starting.** This file is the always-on guardrails; the spec carries the DDL, the `record_fact` procedure, the Cedar policy, and the acceptance tests.

## Non-negotiable rules

- **Never `AUTO_INCREMENT` on a clustered PK.** Event and subject PKs are `AUTO_RANDOM` — the log is monotonic and `AUTO_INCREMENT` hotspots TiKV.
- **`fact_current` is write-through, upserted in the SAME transaction as the `fact_event` append.** TiDB has no materialized view — do not build or assume one.
- **The event log is append-only.** Corrections and retractions are new events, never row mutations. It is tamper-evident (hash chain) — never call it or treat it as immutable.
- **Secrets by ARN from Secrets Manager only.** Never env-var secrets, never secrets in code.
- **No free-form SQL surface.** Reads are typed and enumerated; `query_tenant_metrics` exposes named metrics only. No generic `db_execute` / `db_query`.
- **The contradiction test decides deterministically.** Rule-based authoritative path (predicate registry + authority gate). Any semantic/LLM check may only route a case to `disputed` — it never decides a supersede. Recency supersedes only for `monotonic` predicates; never a blanket time-window overwrite. Do not reproduce a "supersede everything within N days" rule.
- **`tenant_id` is compared literally in Cedar** (Cedar has no string concatenation) — it must arrive as a first-class JWT claim on the token.

## Stack

- TiDB Cloud **Serverless** (a.k.a. Starter — our cluster), MySQL wire protocol, SQLAlchemy. The HNSW vector index **is** supported on Serverless. Per TiDB docs, vector search is available on TiDB Cloud **Starter/Serverless, Essential, and Dedicated**, plus **Self-Managed** — notably **not** Premium (Dedicated/Self-Managed require TiDB v8.4.0+). So it is not "Dedicated-only," but it is also not universal. Serverless does **not** support resource-control DDL (`CREATE RESOURCE GROUP`).
- AWS: AgentCore Gateway / Policy (Cedar) / Identity (Cognito JWT), Lambda, Secrets Manager.
- Python Lambda handlers.

## How to work here

- **Standalone build.** The fact layer has its own Gateway, its own Cognito app client, and its own thin validation — do not attach it to any existing agent or Gateway. The caller is just a caller, not the product; the deliverable is the governed fact layer. Keep `subject` / `predicate` generic and domain-neutral.
- **Don't guess environment values.** Region, cluster host, Secrets ARN, Cognito pool / discovery URL / client IDs, gateway ARN, and embedding model + vector dimension all come from the operator — see the spec's "Environment inputs." Ask if not provided; do not invent ARNs, endpoints, or IDs.
- **Verify tool syntax against current sources before running it.** AgentCore CLI flags evolve — check `agentcore --help` / current docs before deploy commands. Same for the TiDB vector-index DDL, which is version-specific.
- **Phase A only.** Do not build: a standalone MCP server on AgentCore Runtime, Agent Registry publication, evaluation/optimization integration, any conversational or short-term "what did we talk about" store (that tier is AgentCore Memory's job), or free-form aggregation.
- **The acceptance tests in the spec are the definition of done.** Treat the two deploy-confirm checks (`context.input.tenant_id` populating; the interceptor credential reaching a tenant-scoped TiDB session) as gates.

## Decide before cutting the write path

Four operator inputs (the spec's open items): the starting predicates and their `value_type`, the `source → rank` values, the hash-chain scope (per-subject vs per-tenant), and the embedding dimension. Hash-chain scope changes both the schema and the write path — confirm it first.
