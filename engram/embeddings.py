"""Lazy singleton wrappers around FastEmbed (dense) and qdrant_edge's BM25 (sparse)."""
from functools import lru_cache

from fastembed import TextEmbedding
from qdrant_edge import Bm25, SparseVector

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_SIZE = 384


@lru_cache(maxsize=1)
def _dense_model() -> TextEmbedding:
    return TextEmbedding(model_name=DENSE_MODEL)


@lru_cache(maxsize=1)
def _bm25() -> Bm25:
    return Bm25()


def embed_dense(text: str) -> list[float]:
    return next(_dense_model().query_embed(text)).tolist()


def embed_dense_batch(texts: list[str], batch_size: int = 256) -> list[list[float]]:
    return [v.tolist() for v in _dense_model().embed(texts, batch_size=batch_size)]


def embed_sparse_query(text: str) -> SparseVector:
    s = _bm25().embed_query(text)
    return SparseVector(indices=s.indices, values=s.values)


def embed_sparse_document(text: str) -> SparseVector:
    s = _bm25().embed_document(text)
    return SparseVector(indices=s.indices, values=s.values)


def document_vectors(texts: list[str]) -> list[dict]:
    """One batched dense pass + BM25 per text -> named vectors ready for a Point."""
    dense = embed_dense_batch(texts)
    return [{"dense": d, "bm25": embed_sparse_document(t)} for d, t in zip(dense, texts)]
