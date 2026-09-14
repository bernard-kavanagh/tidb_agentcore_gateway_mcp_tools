"""Subject embeddings via Amazon Bedrock — Titan Text Embeddings V2 @ 1024 dims.

Used by record_fact (to populate fact_subject.subject_vec on subject creation)
and by vector_search (to embed the query text). Cosine distance in TiDB, so we
request normalized embeddings.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3

_REGION = os.environ.get("AWS_REGION", "eu-central-1")
_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))


@lru_cache(maxsize=1)
def _client():
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
