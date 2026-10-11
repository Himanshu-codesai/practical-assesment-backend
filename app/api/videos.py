import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import schemas
from app.config import settings
from app.db import get_db
from app.errors import BadRequest, Conflict, NotFound
from app.models import Job, JobKind, JobStatus, Learner, Video, VideoStatus
from app.pipeline.chunking import format_timestamp
from app.pipeline.transcript import (
    MEDIA_EXTENSIONS, TRANSCRIPT_EXTENSIONS, placeholder_title, youtube_id, youtube_link,
)
from app.retrieval import store
from app.retrieval.store import SearchHit
from app.services import assessment as assessment_service
from app.services import qa
from app.tasks import Dispatcher, get_dispatcher

log = logging.getLogger(__name__)
router = APIRouter(prefix="/videos", tags=["videos"])

UPLOAD_DIR = Path(settings.media_dir) / "uploads"
ALLOWED_UPLOADS = TRANSCRIPT_EXTENSIONS | MEDIA_EXTENSIONS


def get_video(video_id: int, db: Session = Depends(get_db)) -> Video:
    video = db.get(Video, video_id)
    if video is None:
        raise NotFound(f"Video {video_id} not found.")
    return video


def get_ready_video(video: Video = Depends(get_video)) -> Video:
    if video.status != VideoStatus.READY:
        raise Conflict(f"Video {video.id} is '{video.status}'; it must be 'ready' first.")
    return video


def to_hit(hit: SearchHit) -> schemas.ChunkHit:
    chunk = hit.chunk
    return schemas.ChunkHit(
        chunk_id=chunk.id, video_id=chunk.video_id, chunk_index=chunk.index, topic=chunk.topic,
        start=format_timestamp(chunk.start_seconds), end=format_timestamp(chunk.end_seconds),
        url=youtube_link(chunk.video.source_url, chunk.start_seconds),
        score=hit.score, text=chunk.text,
    )


def _start_ingest(db: Session, dispatcher: Dispatcher, video: Video) -> schemas.VideoAccepted:
    db.add(video)
    db.flush()
    job = Job(kind=JobKind.INGEST, video_id=video.id)
    db.add(job)
    db.commit()  # commit before enqueueing so the worker can always see the rows
    dispatcher.enqueue(job)
    log.info("video registered", extra={"video_id": video.id, "job_id": job.id})
    return schemas.VideoAccepted(video=video, job=job)


@router.post("", response_model=schemas.VideoAccepted, status_code=status.HTTP_202_ACCEPTED,
             summary="Register a video by URL and start processing it")
def create_video(
    body: schemas.VideoCreate,
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    """Returns immediately with a job to poll at `GET /jobs/{id}`."""
    video_id = youtube_id(body.source_url)
    if body.transcript_method == "captions" and not video_id:
        raise BadRequest("Captions are only available for YouTube links. Use auto or whisper.")
    video = Video(
        title=body.title or (placeholder_title(video_id) if video_id else body.source_url),
        source_type="youtube" if video_id else "url",
        source_url=body.source_url,
        transcript_method=body.transcript_method,
    )
    return _start_ingest(db, dispatcher, video)


@router.post("/upload", response_model=schemas.VideoAccepted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Upload a video, audio or transcript file")
def upload_video(
    title: str = Form(max_length=300),
    file: UploadFile = File(description="Media (.mp4, .mp3, .wav, ...) or a transcript "
                                        "(.srt, .vtt, .txt)"),
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_UPLOADS:
        raise BadRequest(f"Unsupported file type '{extension}'. Allowed: {sorted(ALLOWED_UPLOADS)}")
    if file.size is not None and file.size > settings.max_upload_mb * 1024 * 1024:
        raise BadRequest(f"File is larger than {settings.max_upload_mb} MB.")

    video = Video(title=title, source_type="upload")
    db.add(video)
    db.flush()  # get the id for the file name
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"video_{video.id}{extension}"
    with path.open("wb") as target:  # streamed to disk: media files do not fit in memory
        shutil.copyfileobj(file.file, target)
    video.file_path = str(path)  # on a volume shared with the worker
    return _start_ingest(db, dispatcher, video)


@router.get("", response_model=list[schemas.VideoOut], summary="List videos")
def list_videos(db: Session = Depends(get_db)):
    return db.scalars(select(Video).order_by(Video.id)).all()


@router.get("/{video_id}", response_model=schemas.VideoOut, summary="Video details and status")
def read_video(video: Video = Depends(get_video)):
    return video


@router.get("/{video_id}/transcript", response_model=schemas.TranscriptOut,
            summary="The transcript the pipeline extracted, line by line")
def read_transcript(video: Video = Depends(get_video)):
    """The raw material everything else is built from: the caption lines, the Whisper output
    or the uploaded file, before chunking. Available as soon as the transcribe stage is done."""
    if not video.transcript:
        raise Conflict(f"Video {video.id} has no transcript yet (status: {video.status}).")
    return schemas.TranscriptOut(
        video_id=video.id,
        source=video.transcript_source or "unknown",
        segments=[
            schemas.TranscriptSegment(
                start=format_timestamp(line["start"]), end=format_timestamp(line["end"]),
                url=youtube_link(video.source_url, line["start"]), text=line["text"],
            )
            for line in video.transcript
        ],
    )


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Delete a video and everything derived from it")
def delete_video(video: Video = Depends(get_video), db: Session = Depends(get_db)):
    """Removes the video, its transcript chunks and search vectors, watch progress, and every
    assessment, answer and report made for it. Refused while a job for the video is running."""
    active = db.scalar(select(Job.id).where(
        Job.video_id == video.id, Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
    if active is not None:
        raise Conflict(f"Job {active} is still working on this video. Try again when it finishes.")

    video_id, file_path = video.id, video.file_path
    # One statement: the foreign keys are ON DELETE CASCADE, so the database removes the
    # chunks, jobs, progress, assessments, questions, answers and reports with it.
    db.execute(delete(Video).where(Video.id == video_id))
    db.commit()
    # The database is the source of truth, so it goes first. If a step below fails, what is
    # left over is harmless: search ignores vectors that have no chunk row.
    store.delete_video(video_id)
    if file_path:
        Path(file_path).unlink(missing_ok=True)  # the uploaded file, if there was one
    log.info("video deleted", extra={"video_id": video_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{video_id}/search", response_model=schemas.SearchResponse,
             summary="Retrieve the transcript chunks most relevant to a query")
def search_video(
    body: schemas.SearchRequest,
    video: Video = Depends(get_ready_video),
    db: Session = Depends(get_db),
):
    mode = body.mode or settings.retrieval_mode
    hits = store.search(db, body.query, video_id=video.id, top_k=body.top_k, mode=mode)
    return schemas.SearchResponse(query=body.query, mode=mode, results=[to_hit(h) for h in hits])


@router.post("/{video_id}/ask", response_model=schemas.AskResponse,
             summary="Answer a question from the video's content, with citations")
def ask_video(
    body: schemas.AskRequest,
    video: Video = Depends(get_ready_video),
    db: Session = Depends(get_db),
):
    saved = qa.cached_answer(db, video, body.question)
    if saved is not None:
        return schemas.AskResponse(**{**saved, "question": body.question, "cached": True})

    answer, cited = qa.answer_question(db, video, body.question)
    response = schemas.AskResponse(
        question=body.question, answerable=answer.answerable, answer=answer.answer,
        citations=[to_hit(h) for h in cited],
    )
    qa.save_answer(db, video, body.question, response.model_dump(exclude={"cached"}))
    return response


@router.post("/{video_id}/progress", response_model=schemas.ProgressOut,
             summary="Report how much of the video a learner has watched")
def update_progress(
    body: schemas.ProgressUpdate,
    video: Video = Depends(get_video),
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    """When `percent_watched` reaches the completion threshold, an assessment is generated
    automatically (once per learner and video). The response then carries its id."""
    learner = db.get(Learner, body.learner_id)
    if learner is None:
        raise NotFound(f"Learner {body.learner_id} not found.")
    progress, assessment, job = assessment_service.record_progress(
        db, learner, video, body.percent_watched
    )
    if job is not None:
        dispatcher.enqueue(job)
    return schemas.ProgressOut(
        learner_id=learner.id,
        video_id=video.id,
        percent_watched=progress.percent,
        threshold=settings.completion_threshold,
        assessment_id=assessment.id if assessment else None,
        assessment_status=assessment.status if assessment else None,
        job_id=job.id if job else None,
    )
