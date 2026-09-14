"""TiDB connection credentials resolved from AWS Secrets Manager BY ARN.

Hard rule: secret *material* never lives in environment variables or code.
Only the ARN is passed around (via the interceptor context or a config ARN);
the material is fetched from Secrets Manager at runtime and cached in-process
for the life of the warm Lambda container.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

_REGION = os.environ.get("AWS_REGION", "eu-central-1")


@lru_cache(maxsize=8)
def _client():
    return boto3.client("secretsmanager", region_name=_REGION)


@lru_cache(maxsize=16)
def get_secret(secret_arn: str) -> dict:
    """Fetch and parse a JSON secret by ARN. Cached per warm container.

    Expected JSON keys: host, port, username, password, database,
    and optionally ssl_ca (path to a CA bundle inside the Lambda package/layer).
    """
    if not secret_arn:
        raise ValueError("secret_arn is required; secrets are resolved by ARN only")
    resp = _client().get_secret_value(SecretId=secret_arn)
    material = resp.get("SecretString")
    if material is None:
        raise ValueError(f"secret {secret_arn} has no SecretString")
    return json.loads(material)
