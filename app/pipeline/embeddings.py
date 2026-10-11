"""Local embeddings via fastembed (ONNX, CPU): a dense model and a BM25 sparse model.

Dense vectors capture meaning ("squish into a range" finds "sigmoid").
Sparse BM25 vectors capture exact words (names, formulas, acronyms like "ReLU").
Hybrid retrieval needs both.
"""
from functools import lru_cache

from app.config import settings


@lru_cache
def _dense():
    from fastembed import TextEmbedding  # imported lazily: loading the model takes seconds

    return TextEmbedding(settings.embedding_model, cache_dir=settings.model_cache_dir)


@lru_cache
def _sparse():
    from fastembed import SparseTextEmbedding

    return SparseTextEmbedding(settings.sparse_model, cache_dir=settings.model_cache_dir)


def embed_documents(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _dense().embed(texts)]


def embed_query(text: str) -> list[float]:
    # bge models are trained with a special prefix for queries; query_embed adds it.
    return next(iter(_dense().query_embed(text))).tolist()


def sparse_documents(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """BM25 term weights as (token ids, values). Qdrant applies the IDF part at query time."""
    return [(e.indices.tolist(), e.values.tolist()) for e in _sparse().embed(texts)]


def sparse_query(text: str) -> tuple[list[int], list[float]]:
    embedding = next(iter(_sparse().query_embed(text)))
    return embedding.indices.tolist(), embedding.values.tolist()
