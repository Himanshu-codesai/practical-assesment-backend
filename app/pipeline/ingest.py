"""The video processing pipeline: transcript -> chunks -> topics -> vector index.

Every stage is safe to run again. The transcript is cached on the video row, chunks
are rebuilt from scratch, and indexing overwrites, so a failed job can simply be retried.
"""
import logging
from dataclasses import asdict

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.base import render_prompt
from app.llm.client import get_llm
from app.llm.schemas import VideoOutline
from app.models import CachedAnswer, Chunk, Job, Video, VideoStatus
from app.pipeline.chunking import build_chunks, format_timestamp
from app.pipeline.transcript import (
    Segment, fetch_youtube_title, load_segments, placeholder_title, youtube_id,
)
from app.retrieval import store

log = logging.getLogger(__name__)


def set_stage(db: Session, job: Job, stage: str) -> None:
    job.stage = stage
    db.commit()  # committed straight away so GET /jobs/{id} shows live progress
    log.info("job stage", extra={"job_id": job.id, "stage": stage})


def format_chunks(chunks: list[Chunk]) -> str:
    """Render chunks for a prompt. The [chunk N ...] header is how the model cites them."""
    return "\n\n".join(
        f"[chunk {c.index} | {c.topic or 'untagged'} | "
        f"{format_timestamp(c.start_seconds)}-{format_timestamp(c.end_seconds)}]\n{c.text}"
        for c in chunks
    )


def assign_topics(chunks: list[Chunk], outline: VideoOutline) -> None:
    """Tag each chunk with the last topic that starts at or before it."""
    spans = sorted(outline.topics, key=lambda t: t.start_chunk)
    for chunk in chunks:
        started = [t for t in spans if t.start_chunk <= chunk.index]
        chunk.topic = (started[-1] if started else spans[0]).name if spans else "General"


def ingest_video(db: Session, job: Job) -> None:
    video = db.get(Video, job.video_id)
    video.status, video.error = VideoStatus.PROCESSING, None

    set_stage(db, job, "transcribe")
    video_id = youtube_id(video.source_url)
    if video_id and video.title == placeholder_title(video_id):  # no title was given
        video.title = fetch_youtube_title(video.source_url) or video.title
    if not video.transcript:
        loaded, video.transcript_source = load_segments(video)
        video.transcript = [asdict(s) for s in loaded]
    segments = [Segment(**s) for s in video.transcript]
    video.duration_seconds = round(max((s.end or 0 for s in segments), default=0), 1) or None

    set_stage(db, job, "chunk")
    store.delete_video(video.id)
    db.execute(delete(Chunk).where(Chunk.video_id == video.id))
    db.execute(delete(CachedAnswer).where(CachedAnswer.video_id == video.id))
    drafts = build_chunks(segments, settings.chunk_target_words, settings.chunk_overlap_segments)
    chunks = [
        Chunk(video_id=video.id, index=d.index, text=d.text,
              start_seconds=d.start, end_seconds=d.end)
        for d in drafts
    ]
    db.add_all(chunks)
    db.flush()

    set_stage(db, job, "outline")
    system, prompt = render_prompt("outline", title=video.title, chunks=format_chunks(chunks))
    outline = get_llm().generate_structured(
        task="outline", system=system, prompt=prompt, schema=VideoOutline
    )
    assign_topics(chunks, outline)
    video.summary = outline.summary
    video.topics = [{"name": t.name, "summary": t.summary} for t in outline.topics]

    set_stage(db, job, "index")
    store.index_chunks(chunks)

    video.status = VideoStatus.READY
    log.info("video ready", extra={"video_id": video.id, "chunks": len(chunks),
                                   "topics": len(video.topics)})
