"""Retrieval-augmented question answering over one video (the RAG read path)."""
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.base import render_prompt
from app.llm.client import get_llm
from app.llm.schemas import GroundedAnswer
from app.models import CachedAnswer, Video
from app.pipeline.ingest import format_chunks
from app.retrieval import store
from app.retrieval.store import SearchHit


def question_key(question: str) -> str:
    """Normalise a question for the cache: case, spacing and end punctuation do not matter."""
    return re.sub(r"\s+", " ", question).strip().strip("?!. ").lower()


def cached_answer(db: Session, video: Video, question: str) -> dict | None:
    return db.scalar(select(CachedAnswer.response).where(
        CachedAnswer.video_id == video.id, CachedAnswer.question_key == question_key(question)))


def save_answer(db: Session, video: Video, question: str, response: dict) -> None:
    db.add(CachedAnswer(video_id=video.id, question_key=question_key(question), response=response))
    try:
        db.commit()
    except IntegrityError:  # the same question was asked twice at once; one copy is enough
        db.rollback()


def answer_question(
    db: Session, video: Video, question: str
) -> tuple[GroundedAnswer, list[SearchHit]]:
    hits = store.search(db, question, video_id=video.id, top_k=settings.retrieval_top_k)
    ordered = sorted((h.chunk for h in hits), key=lambda c: c.index)
    system, prompt = render_prompt(
        "answer", title=video.title, question=question, chunks=format_chunks(ordered)
    )
    answer = get_llm().generate_structured(
        task="answer", system=system, prompt=prompt, schema=GroundedAnswer
    )
    # Return only the retrieved chunks the model says it used, as citations.
    cited = [h for h in hits if h.chunk.index in set(answer.cited_chunk_indexes)]
    return answer, cited
