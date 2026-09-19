# Architecture — Fact Layer vs Governance boundary (authoritative)

This document records the finalized product boundary for the TiDB Governed Fact
Layer. Where earlier prose (README §8 historically, `SPEC.md`, `DEPLOYMENT.md`)
describes the interceptor as "injecting verified identity/credentials" that the
handlers consume, that reflects an earlier design and is **superseded by this
document**. No handler logic, SQL, Cedar policy, Cognito, Gateway, interceptor, or
IAM behavior is changed by recording this contract — it is terminology/positioning.

## The three distinct concepts

- **Business/investigation context** — established by the calling application
  (e.g. "authenticated support agent + customer John + tenant Acme + case X",
  or "transaction T123 triggered a fraud review for John at Acme Bank"). The Fact
  Layer does not discover who John is from nothing.
- **Requested data scope** — the `tenant_id` (and `subject`/`entity`/`query`)
  arguments the agent passes to a tool. `tenant_id` is a **selector**, not an
  authenticated identity.
- **Authorization** — whether the authenticated caller is *permitted* to select
  that scope. This is decided by the Gateway/governance layer, not the tool.

## Fact Layer contract (the 9 MCP tools)

- Each tenant-scoped tool takes an **explicit `tenant_id`** (required) plus its
  typed arguments; the schemas and their `tenant_id` requirement are stable.
- The handler **validates** the selector (`handlers/common/context.py`,
  fail-closed on missing/empty) and **deterministically applies** it to every
  tenant-scoped operation (`WHERE tenant_id = :t`). No free-form SQL.
- The tools are **use-case-agnostic and domain-neutral** (`subject`/`predicate`/
  `value`). Fraud, customer support, retail, reconciliation, etc. are
  **orchestration above** the Fact Layer — those domain semantics must **not** be
  baked into the tools.
- `tenant_id` is **not** authenticated identity, **not** interceptor-derived, and
  carries **no mandatory Cognito/Cedar dependency** inside the Lambda.
- Not every query is subject-scoped: e.g. tenant-wide precedent
  (`tenant_id` + a `predicate`/semantic query, not a specific subject) is normal;
  a non-tenant "official docs" lookup is a **separate** composed target/skill, not
  one of these 9 tools.

## Governance contract (deployment concern, not a Fact-Layer dependency)

- **Invariant (non-negotiable):** a tenant filter is **not** an authorization
  mechanism. The Fact Layer must always be deployed **behind a tenant-scope
  authorizer and fail closed without one.** (Proven by the B test: caller
  authenticated as tenant-A requesting `tenant_id=tenant-B` → Cedar DENY.)
- The **mechanism is chosen to match the tenancy model** and may be substituted
  without changing the tools:
  - **same-tenant (reference profile):** Cognito JWT `tenant_id` claim →
    AgentCore CUSTOM_JWT → Cedar tenant-equality
    (`context.input.tenant_id == principal.getTag("tenant_id")`, deny-by-default).
    Enforced only in **ENFORCE**; **LOG_ONLY** records the decision without
    enforcing it.
  - **self-service** — end-user scoped to their own `user_id`/tenant.
  - **workforce / delegated** — employee acting on a selected customer/tenant,
    established server-side by the calling application.
  - **service workload** — a workload whose IAM role is already restricted to one
    tenant (single-tenant-per-caller); per-request claim check may be unnecessary.
  - **global / read-only** — non-tenant or anonymized shared facts (a deliberate,
    separately governed path — never achieved by loosening `tenant_id`).
  - *(These alternatives are illustrative; only the same-tenant profile is
    implemented today. They are not built here.)*
- **Tenancy-model caveat:** multi-tenant-per-caller (one gateway, many tenants —
  the agent/SaaS case) **requires** a per-request check (Cedar claim-equality, or
  a trusted upstream that mints the tenant). IAM alone cannot scope per-request
  tenant; it only fits single-tenant-per-caller.

## Current reference deployment

- **Identity (reference configuration):** Cognito user pool `fact-layer-pool`
  (eu-west-1), access-token `tenant_id` claim via a V2 pre-token trigger,
  no-secret app client. Cognito is the **reference** identity provider; a
  different IdP can be substituted.
- **Authorization:** `FactLayerCedar` policy engine, 9 per-action tenant-equality
  policies, attached to gateway `fact-layer-gw` in **LOG_ONLY** (ENFORCE is the
  next gate).
- **Credentials:** shared runtime credential profile today
  (`TENANT_SECRET_MAP={}` + `FACT_LAYER_SECRET_ARN`), so isolation rests on
  Cedar + the per-query `tenant_id` predicate — **not** per-tenant DB roles.
  Per-tenant credentials are an optional profile (`TENANT_SECRET_MAP` populated),
  not a Fact-Layer requirement.

## Post-demo follow-ups (recorded, not implemented here)

- The request **interceptor** is currently pass-through after independent JWT
  verification; its apparent duplication with the Gateway authorizer is a
  post-demo simplification candidate — **not** removed or redesigned now.
- `FACT_REQUIRE_GATEWAY_CONTEXT` is **optional, off** hardening; it is not a
  security boundary (the Lambda `client_context` marker is caller-supplied), and
  is not enabled or redesigned here.
- Confused-deputy hardening: add `aws:SourceAccount` + `aws:SourceArn=<gateway
  ARN>` to the Gateway execution role **trust** policy (documented best practice;
  separate IAM change).
