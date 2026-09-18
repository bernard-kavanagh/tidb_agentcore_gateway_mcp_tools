"""FactLayerComputeStack — Lambda/IAM/logging tier for the fact layer.

What it creates (Gate B only):
  * one shared, least-privilege Lambda execution role
  * 9 typed MCP tool Lambdas (one per tool — NEVER collapsed into one target)
  * the request interceptor Lambda
  * a non-destructive preflight Lambda (TiDB connectivity diagnostic)
  * an explicit CloudWatch LogGroup per function (with retention)

Least privilege granted to the role:
  * secretsmanager:GetSecretValue on the fact-layer secret ARN (+ optional
    per-tenant ARNs) ONLY — cross-region to eu-central-1 is fine (IAM is global)
  * bedrock:InvokeModel on the single Titan embedding model in the Bedrock region
  * logs:CreateLogStream / PutLogEvents on this stack's log groups only
No AdministratorAccess, no wildcard resource on secrets, no DB credentials
anywhere (resolved at runtime by ARN).

Secrets are NEVER embedded: the operator passes the ARN via `-c
factLayerSecretArn=...`; only the ARN (an identifier) is referenced.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Annotations,
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from constructs import Construct

# Repo root = parent of the cdk/ directory (this file is cdk/fact_layer/*.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

_PLACEHOLDER_MARKER = "PLACEHOLDER-REPLACE-AT-DEPLOY"

# name suffix -> handler module path. The 9 typed tools + interceptor + preflight.
# Tool names line up with the Gateway target names in deploy/deploy.sh and the
# Cedar action names in cedar/tenant_isolation.cedar.
TOOL_HANDLERS = {
    "RecordFact": "handlers.record_fact.lambda_handler",
    "VectorSearch": "handlers.vector_search.lambda_handler",
    "QueryTenantMetrics": "handlers.query_tenant_metrics.lambda_handler",
    "ExplainFact": "handlers.explain_fact.lambda_handler",
    "GetFact": "handlers.get_fact.lambda_handler",
    "GetFactHistory": "handlers.get_fact_history.lambda_handler",
    "ListDisputes": "handlers.list_disputes.lambda_handler",
    "SearchEntities": "handlers.search_entities.lambda_handler",
    "RetractFact": "handlers.retract_fact.lambda_handler",
}
INTERCEPTOR_HANDLER = "gateway.interceptor.lambda_handler"
PREFLIGHT_HANDLER = "handlers.preflight.lambda_handler"


class FactLayerStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        ctx = self.node.try_get_context
        secret_arn = ctx("factLayerSecretArn")
        tenant_secret_arns = [a.strip() for a in (ctx("tenantSecretArns") or "").split(",") if a.strip()]
        bedrock_region = ctx("bedrockRegion") or "eu-west-1"
        tidb_region = ctx("tidbRegion") or "eu-central-1"
        embed_model_id = ctx("embedModelId") or "amazon.titan-embed-text-v2:0"
        embed_dim = str(ctx("embedDim") or "1024")
        tenant_secret_map = ctx("tenantSecretMap") or "{}"
        agentcore_principal = ctx("agentCoreServicePrincipal") or "bedrock-agentcore.amazonaws.com"
        retention_days = int(ctx("logRetentionDays") or "14")

        if not secret_arn or _PLACEHOLDER_MARKER in secret_arn:
            Annotations.of(self).add_error(
                "factLayerSecretArn is the placeholder. Deploy with "
                "`-c factLayerSecretArn=arn:aws:secretsmanager:<region>:<acct>:secret:<name>`. "
                "(cdk synth still runs for review; cdk deploy must not use the placeholder.)"
            )

        # --- shared least-privilege execution role (no managed basic-exec: we
        # grant logs narrowly to this stack's groups only) ---
        exec_role = iam.Role(
            self, "FactLayerExecRole",
            role_name="fact-layer-lambda-exec",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Least-privilege role for fact-layer Lambdas: scoped secret "
                        "read + single Bedrock model + own log groups.",
        )

        # Secrets Manager: GetSecretValue on the specific ARN(s) only.
        secret_resources = [secret_arn] + tenant_secret_arns if secret_arn else tenant_secret_arns
        if secret_resources:
            exec_role.add_to_policy(iam.PolicyStatement(
                sid="ReadFactLayerSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=secret_resources,
            ))

        # Bedrock: InvokeModel on exactly the Titan embedding model, in the
        # Bedrock (deploy) region — never the TiDB region.
        exec_role.add_to_policy(iam.PolicyStatement(
            sid="InvokeTitanEmbedding",
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{bedrock_region}::foundation-model/{embed_model_id}"],
        ))

        # Runtime env shared by every function. NOTE: AWS_REGION is a RESERVED
        # Lambda variable (auto-set to this stack's region, eu-west-1) — setting
        # it here would be rejected by CloudFormation, so we don't. Secrets
        # Manager derives its region from the secret ARN; Bedrock uses
        # BEDROCK_REGION; TIDB_REGION is documentation + preflight only.
        common_env = {
            "FACT_LAYER_SECRET_ARN": secret_arn or "",
            "TENANT_SECRET_MAP": tenant_secret_map,
            "BEDROCK_REGION": bedrock_region,
            "TIDB_REGION": tidb_region,
            "EMBED_MODEL_ID": embed_model_id,
            "EMBED_DIM": embed_dim,
        }

        # One bundled code asset (handlers/ + gateway/ + pip deps) reused by all
        # functions — CDK dedupes by asset hash. Requires Docker at synth/deploy.
        #
        # `-c codeStub=1` swaps in trivial inline code so the construct graph /
        # template can be synthesized for REVIEW on a machine without Docker. It
        # is never used for a real deploy (the functions would be empty stubs).
        if ctx("codeStub"):
            Annotations.of(self).add_warning(
                "codeStub=1: functions use placeholder inline code — REVIEW ONLY, "
                "do not deploy. Remove the flag (and run Docker) for a real bundle."
            )
            code = _lambda.Code.from_inline("def handler(event, context):\n    return {}\n")
        else:
            code = _lambda.Code.from_asset(
                str(REPO_ROOT),
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output "
                        "&& cp -au handlers /asset-output/ "
                        "&& cp -au gateway /asset-output/",
                    ],
                ),
            )

        def make_fn(suffix: str, handler: str, *, purpose: str,
                    gateway_invokable: bool, memory: int = 512,
                    timeout_s: int = 30) -> _lambda.Function:
            fn_name = f"fact-{suffix}"
            log_group = logs.LogGroup(
                self, f"{suffix}LogGroup",
                log_group_name=f"/aws/lambda/{fn_name}",
                retention=_retention(retention_days),
                removal_policy=RemovalPolicy.DESTROY,
            )
            fn = _lambda.Function(
                self, f"{suffix}Fn",
                function_name=fn_name,
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler=handler,
                code=code,
                role=exec_role,
                environment=common_env,
                timeout=Duration.seconds(timeout_s),
                memory_size=memory,
                log_group=log_group,
                description=purpose,
            )
            # Least-privilege logging: writes to this function's own group only.
            log_group.grant_write(exec_role)
            if gateway_invokable:
                # Allow the AgentCore Gateway service to invoke this target /
                # interceptor. Scoped by source account to reduce confused-deputy
                # risk; tighten to the Gateway ARN with source_arn once it exists.
                fn.add_permission(
                    "AgentCoreInvoke",
                    principal=iam.ServicePrincipal(agentcore_principal),
                    action="lambda:InvokeFunction",
                    source_account=self.account,
                )
            return fn

        # --- 9 typed tool Lambdas (each Gateway-invokable) ---
        tool_fns: dict[str, _lambda.Function] = {}
        for suffix, handler in TOOL_HANDLERS.items():
            tool_fns[suffix] = make_fn(
                suffix, handler,
                purpose=f"Fact-layer typed MCP tool: {suffix}",
                gateway_invokable=True,
            )

        # --- request interceptor (Gateway-invokable, runs before Cedar) ---
        interceptor_fn = make_fn(
            "Interceptor", INTERCEPTOR_HANDLER,
            purpose="Request interceptor: extracts verified tenant_id, injects "
                    "provenance + tenant-scoped secret ARN.",
            gateway_invokable=True, memory=256, timeout_s=15,
        )

        # --- preflight diagnostic (operator-invoked directly; NOT via Gateway) ---
        preflight_fn = make_fn(
            "Preflight", PREFLIGHT_HANDLER,
            purpose="Non-destructive TiDB connectivity preflight (DNS/TCP/TLS/auth/read).",
            gateway_invokable=False, memory=256, timeout_s=30,
        )

        # --- outputs: ARNs the operator drops into deploy/env.sh ---
        CfnOutput(self, "ExecRoleArn", value=exec_role.role_arn,
                  description="Lambda execution role ARN")
        CfnOutput(self, "InterceptorLambdaArn", value=interceptor_fn.function_arn,
                  description="INTERCEPTOR_LAMBDA_ARN for update-gateway --interceptor-configurations")
        CfnOutput(self, "PreflightLambdaName", value=preflight_fn.function_name,
                  description="Invoke with: aws lambda invoke --function-name <this>")
        for suffix, fn in tool_fns.items():
            CfnOutput(self, f"{suffix}Arn", value=fn.function_arn,
                      description=f"{suffix} tool Lambda ARN")


def _retention(days: int) -> logs.RetentionDays:
    """Map an integer day count to the nearest CloudWatch retention enum."""
    table = {
        1: logs.RetentionDays.ONE_DAY,
        3: logs.RetentionDays.THREE_DAYS,
        5: logs.RetentionDays.FIVE_DAYS,
        7: logs.RetentionDays.ONE_WEEK,
        14: logs.RetentionDays.TWO_WEEKS,
        30: logs.RetentionDays.ONE_MONTH,
        60: logs.RetentionDays.TWO_MONTHS,
        90: logs.RetentionDays.THREE_MONTHS,
    }
    return table.get(days, logs.RetentionDays.TWO_WEEKS)
