"""query_tenant_metrics — enumerated named metrics only. No caller SQL.

The caller picks a metric by NAME from a fixed registry; every metric is a
pre-written, tenant-parameterized query. There is no generic aggregation and no
free-form SQL surface — that is a deliberate governance boundary (a later Code
Interpreter path handles ad-hoc analytics).

Analytical rollups over the append log are steered to the TiFlash columnar
replica with an optimizer hint. TiFlash replication is async, so freshness is
"sub-second under normal load," not "sub-second fresh."
"""

from __future__ import annotations

from sqlalchemy import text

from .common import context
from .common.db import get_engine

# name -> SQL returning a single scalar `val`, parameterized by :t only.
# Point-lookup/status metrics hit TiKV (fact_current); log rollups hint TiFlash.
_METRICS: dict[str, str] = {
    "fact_count":
        "SELECT COUNT(*) AS val FROM fact_current WHERE tenant_id=:t",
    "active_count":
        "SELECT COUNT(*) AS val FROM fact_current WHERE tenant_id=:t AND status='active'",
    "disputed_count":
        "SELECT COUNT(*) AS val FROM fact_current WHERE tenant_id=:t AND status='disputed'",
    "superseded_count":
        "SELECT COUNT(*) AS val FROM fact_current WHERE tenant_id=:t AND status='superseded'",
    "retracted_count":
        "SELECT COUNT(*) AS val FROM fact_current WHERE tenant_id=:t AND status='retracted'",
    "event_count":
        "SELECT /*+ READ_FROM_STORAGE(TIFLASH[fact_event]) */ "
        "COUNT(*) AS val FROM fact_event WHERE tenant_id=:t",
    "supersede_rate":
        "SELECT /*+ READ_FROM_STORAGE(TIFLASH[fact_event]) */ "
        "COALESCE(SUM(op='supersede')/NULLIF(COUNT(*),0), 0) AS val "
        "FROM fact_event WHERE tenant_id=:t",
    "dispute_rate":
        "SELECT COALESCE("
        "  (SELECT COUNT(*) FROM fact_current WHERE tenant_id=:t AND status='disputed')"
        "  / NULLIF((SELECT COUNT(*) FROM fact_current WHERE tenant_id=:t), 0), 0"
        ") AS val",
}


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event, _lambda_context)
        metric = args["metric"]
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    sql = _METRICS.get(metric)
    if sql is None:
        return {
            "error": f"unknown metric '{metric}'",
            "allowed_metrics": sorted(_METRICS),
        }

    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        val = cx.execute(text(sql), {"t": ctx.tenant_id}).scalar()

    return {"tenant_id": ctx.tenant_id, "metric": metric, "value": float(val or 0)}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
