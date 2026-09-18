"""Pure deployment-configuration validation for the fact layer.

No AWS calls, no secrets — this only validates the *shape* of operator-supplied
configuration (ARNs, regions, tenant map, embedding dims, Cedar mode) so the
same checks run in unit tests, in `validate_config.py`, and can be imported by
the CDK app to fail fast at synth time.

Region model (the two regions are DELIBERATELY different):
    AWS_REGION / deploy region : eu-west-1     (Lambdas, Gateway, Bedrock)
    TIDB_REGION                : eu-central-1   (existing cluster + its secret)
The Secrets Manager region is taken from the secret ARN, so a eu-central-1
secret is reachable from an eu-west-1 Lambda.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

DEPLOY_REGION_DEFAULT = "eu-west-1"
TIDB_REGION_DEFAULT = "eu-central-1"
CEDAR_MODES = ("LOG_ONLY", "ENFORCE")

# arn:aws:secretsmanager:<region>:<account>:secret:<name>
_SECRET_ARN_RE = re.compile(
    r"^arn:aws[a-z\-]*:secretsmanager:"
    r"(?P<region>[a-z0-9\-]+):"
    r"(?P<account>\d{12}):secret:(?P<name>.+)$"
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, m: str) -> None:
        self.errors.append(m)

    def warn(self, m: str) -> None:
        self.warnings.append(m)

    def note(self, m: str) -> None:
        self.info.append(m)


def parse_secret_arn(arn: str) -> dict:
    """Return {region, account, name} for a Secrets Manager ARN, or raise ValueError."""
    if not arn:
        raise ValueError("secret ARN is empty")
    m = _SECRET_ARN_RE.match(arn.strip())
    if not m:
        raise ValueError(
            "not a Secrets Manager ARN "
            "(expected arn:aws:secretsmanager:<region>:<account>:secret:<name>)"
        )
    return m.groupdict()


def validate_secret_arn(arn: str, report: Report, *, label: str = "FACT_LAYER_SECRET_ARN") -> None:
    if not arn or arn.startswith("<") or "REGION" in arn or "<ACCT>" in arn:
        report.error(f"{label} is unset or still a placeholder")
        return
    try:
        parsed = parse_secret_arn(arn)
    except ValueError as e:
        report.error(f"{label}: {e}")
        return
    report.note(f"{label} region = {parsed['region']} (Secrets Manager client uses this)")


def validate_tenant_secret_map(raw: str, report: Report) -> dict:
    if not raw or raw.strip() in ("", "{}"):
        report.note("TENANT_SECRET_MAP empty -> single-secret fallback (Phase-A ok)")
        return {}
    try:
        m = json.loads(raw)
    except json.JSONDecodeError as e:
        report.error(f"TENANT_SECRET_MAP is not valid JSON: {e}")
        return {}
    if not isinstance(m, dict):
        report.error("TENANT_SECRET_MAP must be a JSON object of tenant_id -> ARN")
        return {}
    for tenant, arn in m.items():
        try:
            parse_secret_arn(str(arn))
        except ValueError as e:
            report.error(f"TENANT_SECRET_MAP['{tenant}']: {e}")
    return m


def validate_regions(aws_region: str, tidb_region: str, bedrock_region: str,
                     report: Report) -> None:
    if not aws_region:
        report.error("AWS_REGION (deploy region) is unset")
    if not tidb_region:
        report.error("TIDB_REGION is unset")
    if aws_region and tidb_region and aws_region == tidb_region:
        report.warn(
            f"AWS_REGION == TIDB_REGION ({aws_region}); cross-region path is not "
            "exercised. Expected deploy=eu-west-1, TiDB=eu-central-1."
        )
    if aws_region and aws_region != DEPLOY_REGION_DEFAULT:
        report.warn(f"AWS_REGION={aws_region} (expected {DEPLOY_REGION_DEFAULT})")
    # Bedrock must run in the deploy region, NOT the TiDB region.
    if bedrock_region and tidb_region and bedrock_region == tidb_region and tidb_region != aws_region:
        report.error(
            f"BEDROCK_REGION={bedrock_region} equals TIDB_REGION; Bedrock must run "
            f"in the deploy region ({aws_region or DEPLOY_REGION_DEFAULT})"
        )
    if bedrock_region and aws_region and bedrock_region != aws_region:
        report.warn(f"BEDROCK_REGION={bedrock_region} != AWS_REGION={aws_region}")


def validate_cedar_mode(mode: str, report: Report) -> None:
    if mode not in CEDAR_MODES:
        report.error(f"CEDAR_MODE={mode!r} must be one of {CEDAR_MODES}")
    elif mode == "ENFORCE":
        report.warn("CEDAR_MODE=ENFORCE — initial deployment should start LOG_ONLY (Gate C)")


def validate_embed_dim(raw: str, report: Report) -> None:
    try:
        dim = int(raw)
    except (TypeError, ValueError):
        report.error(f"EMBED_DIM={raw!r} is not an integer")
        return
    if dim <= 0:
        report.error(f"EMBED_DIM={dim} must be positive")
    if dim != 1024:
        report.warn(f"EMBED_DIM={dim} != 1024 (Titan V2 default / VECTOR(1024) schema)")


def validate_cognito(discovery_url: str, allowed_clients: str, report: Report) -> None:
    if not discovery_url or discovery_url.startswith("<") or "<POOL_ID>" in discovery_url:
        report.error("COGNITO_DISCOVERY_URL is unset or a placeholder")
    elif not discovery_url.endswith("/.well-known/openid-configuration"):
        report.warn("COGNITO_DISCOVERY_URL does not end with /.well-known/openid-configuration")
    if not allowed_clients or allowed_clients.startswith("<"):
        report.error("COGNITO_ALLOWED_CLIENTS is unset or a placeholder")


def validate_config(cfg: dict) -> Report:
    """Validate a config mapping (env-var-style keys). Returns a Report."""
    report = Report()
    validate_regions(cfg.get("AWS_REGION", ""), cfg.get("TIDB_REGION", ""),
                     cfg.get("BEDROCK_REGION", ""), report)
    validate_secret_arn(cfg.get("FACT_LAYER_SECRET_ARN", ""), report)
    validate_tenant_secret_map(cfg.get("TENANT_SECRET_MAP", ""), report)
    validate_cedar_mode(cfg.get("CEDAR_MODE", "LOG_ONLY"), report)
    validate_embed_dim(cfg.get("EMBED_DIM", "1024"), report)
    validate_cognito(cfg.get("COGNITO_DISCOVERY_URL", ""),
                     cfg.get("COGNITO_ALLOWED_CLIENTS", ""), report)
    return report


# Keys the deployment reads; used by validate_config.py and the CDK app.
CONFIG_KEYS = (
    "AWS_REGION", "TIDB_REGION", "BEDROCK_REGION",
    "FACT_LAYER_SECRET_ARN", "TENANT_SECRET_MAP",
    "EMBED_MODEL_ID", "EMBED_DIM",
    "COGNITO_DISCOVERY_URL", "COGNITO_ALLOWED_CLIENTS",
    "GATEWAY_NAME", "CEDAR_MODE",
)
