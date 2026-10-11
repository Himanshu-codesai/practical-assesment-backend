"""Vector store adapter: Qdrant with hybrid retrieval (dense + BM25, fused with RRF).

Each chunk is stored as one Qdrant point holding two named vectors:
  "dense" - semantic embedding, cosine similarity
  "bm25"  - sparse keyword vector, IDF applied by Qdrant

A hybrid search runs both retrievers and merges the two ranked lists with Reciprocal
Rank Fusion. RRF only uses each result's rank (score = sum of 1 / (k + rank)), so the
two retrievers' incomparable score scales never have to be calibrated against each other.

PostgreSQL stays the source of truth for chunk text; Qdrant only holds vectors and ids.
"""
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, TypeVar

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import PipelineError
from app.models import Chunk
from app.pipeline.embeddings import embed_documents, embed_query, sparse_documents, sparse_query

log = logging.getLogger(__name__)

DENSE, SPARSE = "dense", "bm25"
MODES = ("hybrid", "dense", "keyword")
T = TypeVar("T")


@dataclass
class SearchHit:
    chunk: Chunk
    score: float  # RRF score in hybrid mode; cosine or BM25 score otherwise


@lru_cache
def client() -> QdrantClient:
    if settings.qdrant_url == ":memory:":  # in-process mode, used by tests
        return QdrantClient(location=":memory:")
    return QdrantClient(url=settings.qdrant_url, timeout=30)


def _call(operation: Callable[[], T]) -> T:
    """Run a Qdrant request, retrying once on a transient failure.

    Qdrant closes idle keep-alive connections with a 408. A pooled connection that sat
    idle (for example while Whisper ran for a minute) then fails on its next use even
    though the server is healthy, so one immediate retry on a fresh connection fixes it.
    Anything that still fails is raised as retryable, so the worker backs off and retries.
    """
    for attempt in (1, 2):
        try:
            return operation()
        except (UnexpectedResponse, ResponseHandlingException) as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and status != 408 and status < 500:
                raise  # a real client error (bad request): retrying cannot help
            if attempt == 2:
                raise PipelineError(f"Qdrant request failed: {exc}", retryable=True) from exc
            log.warning("qdrant request failed, retrying once", extra={"status": status})


def ensure_collection() -> None:
    if client().collection_exists(settings.qdrant_collection):
        return
    try:
        _create_collection()
    except Exception:
        # The API and the worker both call this at startup; whoever loses the race is fine.
        if not client().collection_exists(settings.qdrant_collection):
            raise


def _create_collection() -> None:
    client().create_collection(
        settings.qdrant_collection,
        vectors_config={
            DENSE: models.VectorParams(size=settings.embedding_dim, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
    )
    # Every search filters by video, so index that payload field.
    client().create_payload_index(
        settings.qdrant_collection, field_name="video_id",
        field_schema=models.PayloadSchemaType.INTEGER,
    )
    log.info("created qdrant collection", extra={"collection": settings.qdrant_collection})


def _video_filter(video_id: int | None) -> models.Filter | None:
    if video_id is None:
        return None
    return models.Filter(
        must=[models.FieldCondition(key="video_id", match=models.MatchValue(value=video_id))]
    )


def text_for_search(chunk: Chunk) -> str:
    """What gets embedded for a chunk: one line of context, then the chunk itself.

    A chunk on its own can lose its surroundings ("this is why it works" - what is "this"?).
    Putting the video title and the chunk's topic in front tells the search what the passage
    is about. This is contextual retrieval. Only the search copy gets the line: the stored
    chunk text, and so every citation shown to a learner, stays the speaker's own words.
    """
    video = chunk.video
    summary = next((t["summary"] for t in video.topics or [] if t["name"] == chunk.topic), "")
    context = f"Video: {video.title}. Topic: {chunk.topic or 'General'}. {summary}".strip()
    return f"{context}\n{chunk.text}"


def index_chunks(chunks: list[Chunk]) -> None:
    texts = [text_for_search(c) for c in chunks]
    points = [
        models.PointStruct(
            id=chunk.id,  # the PostgreSQL primary key, so a hit maps straight back to a row
            vector={DENSE: dense, SPARSE: models.SparseVector(indices=indices, values=values)},
            payload={"video_id": chunk.video_id, "chunk_index": chunk.index},
        )
        for chunk, dense, (indices, values) in zip(
            chunks, embed_documents(texts), sparse_documents(texts)
        )
    ]
    _call(lambda: client().upsert(settings.qdrant_collection, points=points, wait=True))


def delete_video(video_id: int) -> None:
    _call(lambda: client().delete(
        settings.qdrant_collection,
        points_selector=models.FilterSelector(filter=_video_filter(video_id)),
        wait=True,
    ))


def search(
    db: Session, query: str, video_id: int | None = None, top_k: int = 5, mode: str | None = None
) -> list[SearchHit]:
    mode = mode or settings.retrieval_mode
    video_filter = _video_filter(video_id)
    dense = embed_query(query)
    indices, values = sparse_query(query)
    sparse = models.SparseVector(indices=indices, values=values)

    if mode == "dense":
        request = dict(query=dense, using=DENSE, query_filter=video_filter)
    elif mode == "keyword":
        request = dict(query=sparse, using=SPARSE, query_filter=video_filter)
    else:
        candidates = top_k * settings.hybrid_candidate_multiplier
        request = dict(
            prefetch=[
                models.Prefetch(query=dense, using=DENSE, filter=video_filter, limit=candidates),
                models.Prefetch(query=sparse, using=SPARSE, filter=video_filter, limit=candidates),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
        )
    points = _call(
        lambda: client().query_points(settings.qdrant_collection, limit=top_k, **request)
    ).points

    rows = db.scalars(select(Chunk).where(Chunk.id.in_([p.id for p in points]))).all()
    by_id = {chunk.id: chunk for chunk in rows}
    # Skip ids with no row: a point can outlive its chunk if a database transaction rolled back.
    return [SearchHit(by_id[p.id], round(p.score, 4)) for p in points if p.id in by_id]
