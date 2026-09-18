#!/usr/bin/env python3
"""CDK app for the fact-layer AWS compute tier (Gate B).

Provisions ONLY the Lambda / IAM / logging portion — the 9 typed MCP tool
Lambdas, the request interceptor Lambda, a non-destructive preflight Lambda, one
least-privilege execution role, and log groups. The AgentCore Gateway, Cognito,
Cedar policy engine, and interceptor wiring are Gate C (see deploy/deploy.sh and
DEPLOYMENT.md) — deliberately NOT in this stack so the compute tier can be
deployed and tested against TiDB before any Gateway exists.

Regions (intentionally split):
    deploy region  : eu-west-1     (this stack, Lambdas, Bedrock)
    TiDB region    : eu-central-1  (existing cluster + its Secrets Manager secret)

Operator-supplied context (nothing hardcoded, no secrets):
    cdk deploy \
      -c factLayerSecretArn=arn:aws:secretsmanager:eu-central-1:<acct>:secret:<name> \
      -c tenantSecretArns=arn:...,arn:...        # optional, comma-separated
      -c bedrockRegion=eu-west-1  -c tidbRegion=eu-central-1

The stack reuses the account's existing eu-west-1 CDK bootstrap; it never
bootstraps or touches CDKToolkit.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from fact_layer.fact_layer_stack import FactLayerStack

app = cdk.App()

# Deploy region is fixed to eu-west-1 (override with -c region=... only if you
# know why). Account comes from the ambient CDK credentials, never hardcoded.
region = app.node.try_get_context("region") or "eu-west-1"
account = os.environ.get("CDK_DEFAULT_ACCOUNT") or app.node.try_get_context("account")

FactLayerStack(
    app,
    "FactLayerComputeStack",
    env=cdk.Environment(account=account, region=region),
    description="Fact-layer typed MCP tool Lambdas + interceptor + IAM (eu-west-1). "
                "TiDB stays in eu-central-1; reached over public TLS by ARN.",
)

app.synth()
