"""Offline tests for deployment configuration + cross-region correctness.

Covers: Secrets Manager ARN parsing/validation, the deliberate two-region model
(deploy=eu-west-1, TiDB=eu-central-1), Bedrock-region separation, tenant map and
Cedar-mode validation, and the runtime helpers that make cross-region work
(secrets.region_from_arn deriving the SM region from the ARN; embeddings NOT
inheriting the TiDB region).

    python3 tests/test_deploy_config.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "deploy"))

import config as depcfg  # noqa: E402  (deploy/config.py)
from handlers.common.secrets import region_from_arn  # noqa: E402

_GOOD_SECRET = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:fact-layer/tidb-Ab12Cd"


def _base_cfg(**over) -> dict:
    cfg = {
        "AWS_REGION": "eu-west-1",
        "TIDB_REGION": "eu-central-1",
        "BEDROCK_REGION": "eu-west-1",
        "FACT_LAYER_SECRET_ARN": _GOOD_SECRET,
        "TENANT_SECRET_MAP": "{}",
        "EMBED_DIM": "1024",
        "CEDAR_MODE": "LOG_ONLY",
        "COGNITO_DISCOVERY_URL":
            "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_abc/.well-known/openid-configuration",
        "COGNITO_ALLOWED_CLIENTS": "client-123",
    }
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------
# ARN parsing / region derivation (the cross-region linchpin)
# --------------------------------------------------------------------------
def test_secret_arn_parses_region():
    assert depcfg.parse_secret_arn(_GOOD_SECRET)["region"] == "eu-central-1"


def test_region_from_arn_matches_secret_region():
    # This is what secrets.py uses at runtime: a eu-west-1 Lambda still targets
    # eu-central-1 Secrets Manager because the region comes from the ARN.
    assert region_from_arn(_GOOD_SECRET) == "eu-central-1"
    assert region_from_arn("not-an-arn") is None
    assert region_from_arn("") is None


def test_bad_secret_arn_rejected():
    r = depcfg.Report()
    depcfg.validate_secret_arn("arn:aws:s3:::bucket/obj", r)
    assert not r.ok, "a non-secretsmanager ARN must be an error"


def test_placeholder_secret_arn_rejected():
    r = depcfg.Report()
    depcfg.validate_secret_arn("arn:aws:secretsmanager:eu-central-1:<ACCT>:secret:<NAME>", r)
    assert not r.ok, "placeholder ARN must be flagged"


# --------------------------------------------------------------------------
# region model
# --------------------------------------------------------------------------
def test_valid_two_region_config_ok():
    rep = depcfg.validate_config(_base_cfg())
    assert rep.ok, f"expected valid config, got errors: {rep.errors}"


def test_same_region_warns():
    rep = depcfg.validate_config(_base_cfg(TIDB_REGION="eu-west-1"))
    assert rep.ok, "same region is a warning, not an error"
    assert any("cross-region path is not exercised" in w for w in rep.warnings)


def test_bedrock_in_tidb_region_is_error():
    # Bedrock must run in the deploy region, never the TiDB region.
    rep = depcfg.validate_config(_base_cfg(BEDROCK_REGION="eu-central-1"))
    assert not rep.ok, "Bedrock in the TiDB region must be an error"


# --------------------------------------------------------------------------
# tenant map / cedar mode / embed dim
# --------------------------------------------------------------------------
def test_tenant_map_valid_and_invalid():
    ok = depcfg.validate_config(_base_cfg(
        TENANT_SECRET_MAP='{"acme":"%s"}' % _GOOD_SECRET))
    assert ok.ok, f"valid tenant map rejected: {ok.errors}"
    bad_json = depcfg.validate_config(_base_cfg(TENANT_SECRET_MAP="{not json"))
    assert not bad_json.ok, "malformed tenant map JSON must error"
    bad_arn = depcfg.validate_config(_base_cfg(TENANT_SECRET_MAP='{"acme":"nope"}'))
    assert not bad_arn.ok, "bad ARN in tenant map must error"


def test_cedar_mode_validation():
    assert depcfg.validate_config(_base_cfg(CEDAR_MODE="LOG_ONLY")).ok
    enforce = depcfg.validate_config(_base_cfg(CEDAR_MODE="ENFORCE"))
    assert enforce.ok and any("LOG_ONLY" in w for w in enforce.warnings), \
        "ENFORCE is valid but should warn to start LOG_ONLY"
    assert not depcfg.validate_config(_base_cfg(CEDAR_MODE="ON")).ok


def test_embed_dim_validation():
    assert depcfg.validate_config(_base_cfg(EMBED_DIM="1024")).ok
    assert not depcfg.validate_config(_base_cfg(EMBED_DIM="abc")).ok
    assert not depcfg.validate_config(_base_cfg(EMBED_DIM="0")).ok


def test_cognito_placeholder_rejected():
    rep = depcfg.validate_config(_base_cfg(
        COGNITO_DISCOVERY_URL="https://cognito-idp.eu-west-1.amazonaws.com/<POOL_ID>/.well-known/openid-configuration"))
    assert not rep.ok, "placeholder discovery URL must error"


# --------------------------------------------------------------------------
# embeddings region defaulting (no eu-central-1 inheritance)
# --------------------------------------------------------------------------
def test_embeddings_region_prefers_bedrock_then_aws_region():
    # Re-import embeddings under controlled env to assert the resolution order.
    for k in ("BEDROCK_REGION", "AWS_REGION"):
        os.environ.pop(k, None)
    os.environ["AWS_REGION"] = "eu-west-1"
    sys.modules.pop("handlers.common.embeddings", None)
    from handlers.common import embeddings as e1
    assert e1._REGION == "eu-west-1", "Bedrock must default to the Lambda region, not eu-central-1"

    os.environ["BEDROCK_REGION"] = "eu-west-1"
    os.environ["AWS_REGION"] = "eu-central-1"  # even if the runtime region were TiDB's
    sys.modules.pop("handlers.common.embeddings", None)
    from handlers.common import embeddings as e2
    assert e2._REGION == "eu-west-1", "explicit BEDROCK_REGION must win"


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
