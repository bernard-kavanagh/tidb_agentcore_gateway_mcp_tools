#!/usr/bin/env python3
"""
Thin MCP test caller for the TiDB Governed Fact Layer (Phase A).

This is SPEC.md Task 8 (Validation), implemented as a direct MCP client against
the Gateway — no agent, no AgentCore Runtime, no Strands wrapper. The Gateway is
agent-agnostic: anything that can authenticate and speak MCP over HTTP can call
it. This is that "anything."

IMPORTANT — this script guesses reasonable argument names for the three tools
(tenant_id, subject, predicate, value, source, confidence for record_fact;
tenant_id/query_text/top_k for vector_search; tenant_id/metric for
query_tenant_metrics) based on SPEC.md's description. It has NOT been checked
against your actual tool_schema.json files. Open those and adjust the
`arguments` dicts below if the real field names differ — a mismatch here looks
like a tool error, not a Gateway/Cedar problem, so rule this out first.

Install:
  pip install mcp boto3

Environment variables:
  FACT_LAYER_GATEWAY_URL   Gateway's MCP endpoint
                           (the AGENTCORE_GATEWAY_<name>_URL value after deploy)
  COGNITO_CLIENT_ID        the fact layer's dedicated Cognito app client ID
  COGNITO_USERNAME         test user (must carry a tenant_id claim — see below)
  COGNITO_PASSWORD         test user password
  AWS_REGION               region the Cognito pool lives in

Usage:
  # Smoke test — confirms Gateway registration worked at all
  python3 test_fact_layer.py list-tools

  # Sanity-check the token before anything else. If tenant_id is missing here,
  # every call below will be denied by Cedar and it is NOT a Gateway/Cedar bug.
  python3 test_fact_layer.py list-tools --show-claims

  # Call one tool directly with raw JSON
  python3 test_fact_layer.py call record_fact \\
      '{"tenant_id":"acme","subject":"account-alpha","predicate":"status",
        "value":"active","source":"system_of_record","confidence":0.9}'

  # Run a full acceptance-test scenario (SPEC.md "Acceptance tests" 1-4)
  python3 test_fact_layer.py scenario write-read
  python3 test_fact_layer.py scenario supersede
  python3 test_fact_layer.py scenario corroborate
  python3 test_fact_layer.py scenario dispute

  # Acceptance test 5 — tenant isolation. Requires a token whose tenant_id
  # claim differs from --other-tenant-id, so the call is a genuine cross-tenant
  # attempt. Expect a Cedar DENY here, then go confirm it in CloudWatch:
  #   agentcore logs --since 15m --query "policy"
  # That CloudWatch line, not this script's output, is what makes Phase A done —
  # this script proves the call was attempted and rejected; the log proves the
  # rejection happened at the Gateway, before TiDB was ever touched.
  python3 test_fact_layer.py scenario cross-tenant-deny --other-tenant-id other-co
"""

import argparse
import asyncio
import base64
import json
import os
import sys

import boto3
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def get_token(username: str | None = None, password: str | None = None) -> str:
    """Authenticate against the fact layer's dedicated Cognito app client.
    The resulting access token MUST carry a tenant_id claim (SPEC.md's hard
    token requirement) — this function does not check that; --show-claims does.
    """
    client_id = os.environ.get("COGNITO_CLIENT_ID")
    if not client_id:
        print("Error: $COGNITO_CLIENT_ID is not set.", file=sys.stderr)
        sys.exit(1)
    username = username or os.environ.get("COGNITO_USERNAME")
    password = password or os.environ.get("COGNITO_PASSWORD")
    if not username or not password:
        print("Error: set $COGNITO_USERNAME / $COGNITO_PASSWORD or pass --username/--password.",
              file=sys.stderr)
        sys.exit(1)

    cognito = boto3.client("cognito-idp", region_name=os.environ.get("AWS_REGION"))
    resp = cognito.initiate_auth(
        AuthFlow="USER_PASSWORD_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]["AccessToken"]


def decode_claims(token: str) -> dict:
    """Local, unverified decode of the JWT payload — for a sanity print only.
    Signature verification is Cognito/Gateway's job, not this script's."""
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


# --------------------------------------------------------------------------
# MCP calls
# --------------------------------------------------------------------------

async def call_tool(gateway_url: str, token: str, tool_name: str, arguments: dict):
    """Returns either the MCP CallToolResult, or a dict with _error=True if the
    call raised before getting a structured MCP response (a common shape for a
    Gateway/Cedar-level deny, which may surface as an HTTP/transport error
    rather than a graceful tool result)."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(gateway_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool_name, arguments=arguments)
    except Exception as e:
        return {"_error": True, "exception": f"{type(e).__name__}: {e}"}


async def list_tools(gateway_url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(gateway_url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            resp = await session.list_tools()
            return resp.tools


def print_result(label: str, result) -> None:
    print(f"\n--- {label} ---")
    if isinstance(result, dict) and result.get("_error"):
        print(f"EXCEPTION before a structured MCP response — likely a Gateway/Cedar")
        print(f"deny, an auth failure, or a connectivity issue (check which):")
        print(f"  {result['exception']}")
        return
    if getattr(result, "isError", False):
        print("MCP TOOL ERROR (isError=True) — likely a Cedar deny surfaced as a tool")
        print("error rather than a transport exception, or a handler-side rejection:")
    if hasattr(result, "content"):
        for block in result.content:
            text = getattr(block, "text", None)
            print(text if text is not None else block)
    else:
        print(json.dumps(result, indent=2, default=str))


# --------------------------------------------------------------------------
# Acceptance-test scenarios (SPEC.md "Acceptance tests" 1-4; 5 is separate below)
# --------------------------------------------------------------------------

async def scenario_write_read(gateway_url, token):
    """Test 1: a new (tenant, subject, predicate) creates one event + one
    current row in a single transaction, then vector_search recalls it.
    query_text is embedded server-side with the same Titan model used at write
    time, so this is a REAL semantic-recall check, not a reachability-only
    placeholder. Uses the seeded scalar predicate `status`."""
    r1 = await call_tool(gateway_url, token, "record_fact", {
        "tenant_id": "acme", "subject": "account-alpha", "predicate": "status",
        "value": "active", "source": "system_of_record", "confidence": 0.85,
    })
    print_result("write_read: record_fact (first assert on a fresh subject)", r1)

    r2 = await call_tool(gateway_url, token, "vector_search", {
        "tenant_id": "acme",
        "query_text": "account-alpha status",
        "top_k": 3,
    })
    print_result("write_read: vector_search (real recall — query_text embedded server-side)", r2)


async def scenario_supersede(gateway_url, token):
    """Test 2: a HIGHER-authority contradicting assert supersedes the current
    winner. Uses seeded ranks: agent_inference (40) < system_of_record (100)."""
    base = {"tenant_id": "acme", "subject": "account-beta", "predicate": "status"}
    r1 = await call_tool(gateway_url, token, "record_fact", {
        **base, "value": "trial", "source": "agent_inference", "confidence": 0.6,
    })
    print_result("supersede: initial assert (lower-authority source: agent_inference, rank 40)", r1)

    r2 = await call_tool(gateway_url, token, "record_fact", {
        **base, "value": "enterprise", "source": "system_of_record", "confidence": 0.95,
    })
    print_result(
        "supersede: contradicting assert, higher authority (system_of_record, rank 100) "
        "(expect: supersede — fact_current re-points, prior event stays in the log)",
        r2,
    )


async def scenario_corroborate(gateway_url, token):
    """Test 3: an agreeing assert appends without discarding prior events —
    recall is not capped at one. Agreement never invokes the authority gate, so
    the two seeded sources' ranks are irrelevant here."""
    base = {"tenant_id": "acme", "subject": "account-gamma", "predicate": "status",
            "value": "active"}
    r1 = await call_tool(gateway_url, token, "record_fact", {
        **base, "source": "verified_integration", "confidence": 0.7,
    })
    print_result("corroborate: initial assert", r1)

    r2 = await call_tool(gateway_url, token, "record_fact", {
        **base, "source": "user_assertion", "confidence": 0.8,
    })
    print_result(
        "corroborate: agreeing assert from a second source "
        "(expect: corroboration — the new confidence is recorded on the new "
        "fact_event; fact_current is intentionally left unchanged, both events "
        "retained in the log. fact_current has no confidence column by design.)",
        r2,
    )


async def scenario_dispute(gateway_url, token):
    """Test 4: EQUAL-authority contradiction -> disputed, both events kept, no
    recency tiebreak. No two seeded sources share a rank, so a genuine
    equal-authority tie uses the SAME source asserting two different values
    back-to-back (recency only ever supersedes for `monotonic` predicates)."""
    base = {"tenant_id": "acme", "subject": "account-delta", "predicate": "status"}
    r1 = await call_tool(gateway_url, token, "record_fact", {
        **base, "value": "active", "source": "user_assertion", "confidence": 0.8,
    })
    print_result("dispute: initial assert (source: user_assertion)", r1)

    r2 = await call_tool(gateway_url, token, "record_fact", {
        **base, "value": "suspended", "source": "user_assertion", "confidence": 0.8,
    })
    print_result(
        "dispute: contradicting assert, SAME source (equal authority) "
        "(expect: fact_current.status='disputed', BOTH events retained, no recency tiebreak)",
        r2,
    )


async def scenario_cross_tenant_deny(gateway_url, token, other_tenant_id: str):
    """Test 5 — tenant isolation. The caller's token carries tenant_id=X; this
    call asserts a fact for tenant_id=other_tenant_id (must differ from X).
    Expect a Cedar DENY. This script can only show the call was rejected;
    proving it happened BEFORE TiDB was touched requires checking CloudWatch —
    see the printed instruction below. That CloudWatch line is deploy-confirm A."""
    r = await call_tool(gateway_url, token, "record_fact", {
        "tenant_id": other_tenant_id, "subject": "account-should-be-denied", "predicate": "status",
        "value": "active", "source": "system_of_record", "confidence": 0.5,
    })
    print_result(
        f"cross_tenant_deny: record_fact with tenant_id='{other_tenant_id}' "
        f"(expect: Gateway/Cedar DENY — this is the isolation rule firing)",
        r,
    )
    print("\n--> Now confirm in CloudWatch that the deny happened at the Gateway,")
    print("    not inside the handler after a TiDB connection was already opened:")
    print('      agentcore logs --since 15m --query "policy"')
    print("    Look for a denied decision citing this call, and confirm no")
    print("    corresponding fact_event row was written for account-should-be-denied.")


SCENARIOS = {
    "write-read": scenario_write_read,
    "supersede": scenario_supersede,
    "corroborate": scenario_corroborate,
    "dispute": scenario_dispute,
}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--gateway-url", default=os.environ.get("FACT_LAYER_GATEWAY_URL"),
                         help="Gateway MCP endpoint (default: $FACT_LAYER_GATEWAY_URL)")
    parser.add_argument("--username", default=None, help="override $COGNITO_USERNAME")
    parser.add_argument("--password", default=None, help="override $COGNITO_PASSWORD")
    parser.add_argument("--show-claims", action="store_true",
                         help="print decoded JWT claims before calling — do this first")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-tools", help="list tools the Gateway advertises (confirms registration)")

    p_call = sub.add_parser("call", help="call one tool directly with raw JSON arguments")
    p_call.add_argument("tool_name")
    p_call.add_argument("arguments_json")

    p_scenario = sub.add_parser("scenario", help="run a named acceptance-test scenario")
    p_scenario.add_argument("name", choices=list(SCENARIOS.keys()) + ["cross-tenant-deny"])
    p_scenario.add_argument(
        "--other-tenant-id", default="other-tenant",
        help="tenant_id used in cross-tenant-deny; must differ from the caller's token claim",
    )

    args = parser.parse_args()

    if not args.gateway_url:
        print("Error: --gateway-url or $FACT_LAYER_GATEWAY_URL is required.", file=sys.stderr)
        sys.exit(1)

    token = get_token(args.username, args.password)

    if args.show_claims:
        claims = decode_claims(token)
        print("--- decoded JWT claims (local decode, unverified) ---")
        print(json.dumps(claims, indent=2))
        if "tenant_id" not in claims:
            print("\n*** WARNING: no 'tenant_id' claim on this token. ***")
            print("*** Every call below will be denied by Cedar. Fix Cognito token")
            print("*** customization (SPEC.md's hard token requirement) before continuing. ***\n")

    if args.command == "list-tools":
        tools = asyncio.run(list_tools(args.gateway_url, token))
        print("\n--- tools advertised by the Gateway ---")
        for t in tools:
            print(f"  {t.name}: {t.description}")
        expected = {"record_fact", "vector_search", "query_tenant_metrics"}
        missing = expected - {t.name for t in tools}
        if missing:
            print(f"\n*** WARNING: expected tools not found: {missing} ***")
            print("*** Check the Gateway target registration, not the handler code. ***")

    elif args.command == "call":
        arguments = json.loads(args.arguments_json)
        result = asyncio.run(call_tool(args.gateway_url, token, args.tool_name, arguments))
        print_result(args.tool_name, result)

    elif args.command == "scenario":
        if args.name == "cross-tenant-deny":
            asyncio.run(scenario_cross_tenant_deny(args.gateway_url, token, args.other_tenant_id))
        else:
            asyncio.run(SCENARIOS[args.name](args.gateway_url, token))


if __name__ == "__main__":
    main()