"""search_entities — find canonical entities for the caller's tenant.

Structured lookup by entity_type and/or a name/key substring (NOT semantic vector
search — that is vector_search over facts). Tenant-scoped, typed, no free-form SQL.
"""

from __future__ import annotations

from sqlalchemy import text

from .common import context
from .common.db import get_engine

_MAX_ROWS = 50


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event)
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    entity_type = args.get("entity_type")
    query = args.get("query")
    limit = min(int(args.get("limit", _MAX_ROWS)), _MAX_ROWS)

    sql = ("SELECT entity_id, entity_type, canonical_key, display_name, "
           "       created_at, updated_at "
           "FROM entity WHERE tenant_id=:t")
    params = {"t": ctx.tenant_id, "k": limit}
    if entity_type:
        sql += " AND entity_type=:et"
        params["et"] = entity_type
    if query:
        sql += " AND (canonical_key LIKE :q OR display_name LIKE :q)"
        params["q"] = f"%{query}%"
    sql += " ORDER BY updated_at DESC LIMIT :k"

    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        rows = cx.execute(text(sql), params).mappings().all()

    entities = [
        {
            "entity_id": int(r["entity_id"]), "entity_type": r["entity_type"],
            "canonical_key": r["canonical_key"], "display_name": r["display_name"],
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        }
        for r in rows
    ]
    return {"tenant_id": ctx.tenant_id, "count": len(entities), "entities": entities}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
