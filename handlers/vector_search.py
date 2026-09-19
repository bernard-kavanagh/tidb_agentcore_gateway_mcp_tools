"""vector_search — typed semantic recall, tenant-scoped. No caller SQL.

Input (per vector_search.schema.json): tenant_id, query_text, top_k (optional,
capped). The handler embeds query_text with the same Titan model used at write
time and returns the nearest fact_subject rows *filtered to the caller's tenant*,
each with its current value/status.

Tenant filtering is defense in depth — Cedar authorizes the call and the scoped
TiDB role bounds the data; the WHERE clause is the third layer.
"""

from __future__ import annotations

from sqlalchemy import text

from .common import context, embeddings
from .common.db import get_engine

_MAX_TOP_K = 50
_DEFAULT_TOP_K = 10


def handler(event, _lambda_context=None):
    try:
        ctx, args = context.extract(event, _lambda_context)
        query_text = args["query_text"]
    except (KeyError, PermissionError, ValueError) as e:
        return {"error": str(e), "error_type": type(e).__name__}

    top_k = min(int(args.get("top_k", _DEFAULT_TOP_K)), _MAX_TOP_K)
    qvec = embeddings.to_vector_literal(embeddings.embed(query_text))

    engine = get_engine(ctx.tenant_secret_arn)
    with engine.connect() as cx:
        rows = cx.execute(
            text(
                "SELECT fs.subject_id, fs.subject, fs.predicate, "
                "       VEC_COSINE_DISTANCE(fs.subject_vec, :qvec) AS distance, "
                "       fc.value AS current_value, fc.status "
                "FROM fact_subject fs "
                "LEFT JOIN fact_current fc ON fc.subject_id = fs.subject_id "
                "WHERE fs.tenant_id = :t AND fs.subject_vec IS NOT NULL "
                # Repeat the full expression (not the alias) so the optimizer can
                # match the HNSW vector-index plan instead of a scan + filesort.
                "ORDER BY VEC_COSINE_DISTANCE(fs.subject_vec, :qvec) "
                "LIMIT :k"
            ),
            {"qvec": qvec, "t": ctx.tenant_id, "k": top_k},
        ).mappings().all()

    results = [
        {
            "subject_id": int(r["subject_id"]),
            "subject": r["subject"],
            "predicate": r["predicate"],
            "distance": float(r["distance"]),
            "current_value": r["current_value"],   # JSON string as stored
            "status": r["status"],
        }
        for r in rows
    ]
    return {"tenant_id": ctx.tenant_id, "count": len(results), "results": results}


def lambda_handler(event, context_):  # noqa: N802
    return handler(event, context_)
