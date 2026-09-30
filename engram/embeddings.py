"""Lazy singleton wrappers around FastEmbed dense model + qdrant_edge's BM25."""
from functools import lru_cache

from fastembed import TextEmbedding
from qdrant_edge import Bm25

DENSE_MODEL = "BAAI/bge-small-en-v1.5"
DENSE_SIZE = 384


@lru_cache(maxsize=1)
def _dense_model() -> TextEmbedding:
    return TextEmbedding(model_name=DENSE_MODEL)


@lru_cache(maxsize=1)
def _bm25() -> Bm25:
    return Bm25()


def embed_dense(text: str) -> list[float]:
    return list(next(_dense_model().embed([text])))


def embed_dense_batch(texts: list[str]) -> list[list[float]]:
    return [list(v) for v in _dense_model().embed(texts)]


def embed_sparse_query(text: str):
    return _bm25().embed_query(text)


def embed_sparse_document(text: str):
    return _bm25().embed_document(text)
