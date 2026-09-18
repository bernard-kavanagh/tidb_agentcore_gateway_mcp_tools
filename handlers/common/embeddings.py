"""Subject embeddings via Amazon Bedrock — Titan Text Embeddings V2 @ 1024 dims.

Used by record_fact (to populate fact_subject.subject_vec on subject creation)
and by vector_search (to embed the query text). Cosine distance in TiDB, so we
request normalized embeddings.

REGION: Bedrock is invoked in the DEPLOYMENT region, NOT the TiDB region. The
Lambda runs in eu-west-1, so embeddings must call Bedrock in eu-west-1 (where
Titan V2 model access is enabled) — never inherit the eu-central-1 TiDB region.
We read BEDROCK_REGION explicitly and fall back to the Lambda's own AWS_REGION
(auto-set to eu-west-1 for a function deployed there), never a hardcoded region.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

# Bedrock region: explicit BEDROCK_REGION, else the Lambda's own region
# (AWS_REGION, auto-set by the runtime), else the deployment region default.
_REGION = (os.environ.get("BEDROCK_REGION")
           or os.environ.get("AWS_REGION")
           or "eu-west-1")
_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))


def build_embedding_text(*, subject: str, predicate: str, value,
                         entity_type: str | None = None,
                         context_summary: str | None = None) -> str:
    """Normalized, DETERMINISTIC semantic representation to embed for recall.

    We embed a normalized "<entity_type> | <subject> | <predicate> = <value> |
    <context>" string rather than the raw subject id, so semantic search retrieves
    relevant institutional knowledge (similar entities / predicates / conclusions),
    not just literal id matches. Deterministic so embeddings are repeatable in tests.
    Pure/stdlib-only (boto3 is imported lazily below), so it is unit-testable.
    """
    val = value if isinstance(value, str) else json.dumps(value, sort_keys=True,
                                                          separators=(",", ":"),
                                                          ensure_ascii=False)
    parts = [
        (str(entity_type) if entity_type else None),
        str(subject),
        f"{predicate} = {val}",
        (str(context_summary) if context_summary else None),
    ]
    return " | ".join(p for p in parts if p)


@lru_cache(maxsize=1)
def _client():
    import boto3  # lazy: keeps build_embedding_text importable without boto3
    return boto3.client("bedrock-runtime", region_name=_REGION)


def embed(text: str) -> list[float]:
    """Return a normalized `EMBED_DIM`-length embedding for `text`."""
    body = json.dumps({"inputText": text, "dimensions": EMBED_DIM, "normalize": True})
    resp = _client().invoke_model(modelId=_MODEL_ID, body=body)
    vec = json.loads(resp["body"].read())["embedding"]
    if len(vec) != EMBED_DIM:
        raise ValueError(f"embedding dim {len(vec)} != expected {EMBED_DIM}")
    return vec


def to_vector_literal(vec: list[float]) -> str:
    """Format a vector for TiDB's VECTOR type: '[f1,f2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
