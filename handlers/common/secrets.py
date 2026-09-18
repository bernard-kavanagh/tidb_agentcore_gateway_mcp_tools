"""TiDB connection credentials resolved from AWS Secrets Manager BY ARN.

Hard rule: secret *material* never lives in environment variables or code.
Only the ARN is passed around (via the interceptor context or a config ARN);
the material is fetched from Secrets Manager at runtime and cached in-process
for the life of the warm Lambda container.

CROSS-REGION: the fact-layer Lambdas run in the deployment region (eu-west-1)
but the TiDB secret may live in a *different* region (the reference secret is in
eu-central-1, alongside the cluster). A Secrets Manager client is regional, so
we resolve the client's region from the secret ARN itself
(`arn:aws:secretsmanager:<region>:<acct>:secret:<name>`) rather than from the
Lambda's own AWS_REGION. This lets an eu-west-1 Lambda read an eu-central-1
secret with no config drift. Falls back to AWS_REGION (the Lambda's region) when
the ARN carries no parseable region.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3


def region_from_arn(secret_arn: str) -> str | None:
    """Return the region field of an ARN, or None if it is not parseable.

    ARN layout: arn:partition:service:REGION:account:resource... — the region is
    the 4th colon-delimited field (index 3).
    """
    if not secret_arn or not secret_arn.startswith("arn:"):
        return None
    parts = secret_arn.split(":")
    if len(parts) < 4 or not parts[3]:
        return None
    return parts[3]


@lru_cache(maxsize=8)
def _client(region: str):
    return boto3.client("secretsmanager", region_name=region)


@lru_cache(maxsize=16)
def get_secret(secret_arn: str) -> dict:
    """Fetch and parse a JSON secret by ARN. Cached per warm container.

    The Secrets Manager region is taken from the ARN (cross-region safe); if the
    ARN has no region we fall back to the Lambda's own AWS_REGION.

    Expected JSON keys: host, port, username, password, database,
    and optionally ssl_ca (path to a CA bundle inside the Lambda package/layer).
    """
    if not secret_arn:
        raise ValueError("secret_arn is required; secrets are resolved by ARN only")
    region = region_from_arn(secret_arn) or os.environ.get("AWS_REGION")
    if not region:
        raise ValueError(
            f"cannot determine Secrets Manager region for {secret_arn}: no region "
            "in ARN and AWS_REGION is unset"
        )
    resp = _client(region).get_secret_value(SecretId=secret_arn)
    material = resp.get("SecretString")
    if material is None:
        raise ValueError(f"secret {secret_arn} has no SecretString")
    return json.loads(material)
