"""Embedding provider for dedup. NAME-ONLY input, both sides, symmetric.

Default: Vertex AI text-embedding-005 (768-dim) -- you're on GCP, near-free.
To switch to a local model (vendor-free, but hosted in the job):
  1. set EMBED_DIM to the model's dimension (BGE-M3 = 1024)
  2. reimplement embed_texts() with sentence-transformers
  3. match the vector(N) dim in products_embedding_setup.sql
The rest of the pipeline only touches embed_texts() + EMBED_DIM.
"""
from functools import lru_cache

EMBED_DIM = 768
_MODEL = "text-embedding-005"
_TASK = "SEMANTIC_SIMILARITY"
_BATCH = 250  # Vertex per-request cap


@lru_cache(maxsize=1)
def _model():
    import os
    import vertexai
    from vertexai.language_models import TextEmbeddingModel
    # Explicit init so embedding works outside a fully-defaulted env (Cloud Shell,
    # a backfill Job, local). project=None falls back to the ADC default project;
    # location pins the region text-embedding-005 is served from.
    vertexai.init(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=os.environ.get("GOOGLE_CLOUD_REGION", "us-central1"),
    )
    return TextEmbeddingModel.from_pretrained(_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed names. Returns one 768-vector per input, order preserved."""
    from vertexai.language_models import TextEmbeddingInput
    out: list[list[float]] = []
    model = _model()
    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        inputs = [TextEmbeddingInput(t or "", _TASK) for t in chunk]
        out.extend(e.values for e in model.get_embeddings(inputs))
    return out


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]
