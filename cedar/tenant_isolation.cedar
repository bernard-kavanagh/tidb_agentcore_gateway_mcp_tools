// Tenant isolation for the fact layer Gateway.
// Engine attached in ENFORCE mode: deny-by-default, forbid-overrides-permit.
// Any tool call without a matching permit below is denied at the Gateway,
// before the request reaches the Lambda / TiDB.
//
// The rule that earns its keep is param-vs-claim: the tool argument tenant_id
// must equal the caller's tenant_id identity claim. Cedar has no string
// concatenation, so the claim is compared literally and must arrive as a
// first-class JWT claim (mapped to a principal tag by the Gateway authorizer).
//
// Action naming is <targetName>___<toolName> — keep the target names in the
// deploy script (RecordFact / VectorSearch / QueryTenantMetrics) in sync.
// Replace <GATEWAY_ARN> at deploy time.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"RecordFact___record_fact",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"VectorSearch___vector_search",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"QueryTenantMetrics___query_tenant_metrics",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

// --- E->A->R read/write tools (same param-vs-claim tenant isolation) ---
// Every one of these tools takes tenant_id in its input, so the identical rule
// governs them. Target names must match deploy.sh: ExplainFact / GetFact /
// GetFactHistory / ListDisputes / SearchEntities / RetractFact.

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"ExplainFact___explain_fact",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GetFact___get_fact",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"GetFactHistory___get_fact_history",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"ListDisputes___list_disputes",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"SearchEntities___search_entities",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};

permit(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"RetractFact___retract_fact",
  resource == AgentCore::Gateway::"<GATEWAY_ARN>"
) when {
  principal.hasTag("tenant_id")
  && context has input
  && context.input has tenant_id
  && context.input.tenant_id == principal.getTag("tenant_id")
};
