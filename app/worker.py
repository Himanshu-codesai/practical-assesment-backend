"""Background work: a Celery worker fed by a Redis queue.

What this buys over running tasks inside the API process:
- Durability: a job waits in Redis until a worker confirms it finished (acks_late), so a
  crashed or redeployed worker does not lose it.
- Automatic retries with exponential backoff for failures marked retryable
  (rate limits, network errors, a malformed LLM reply).
- Independent scaling: the API stays fast because it never runs slow work itself.
"""
import logging
from typing import Callable

from celery import Celery, signals
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.logging_config import configure_logging, request_id_var
from app.models import (
    Assessment, AssessmentStatus, Job, JobKind, JobStatus, Video, VideoStatus,
)

log = logging.getLogger(__name__)

celery_app = Celery("lms", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,  # acknowledge after the task finishes, not when it starts
    task_reject_on_worker_lost=True,  # a killed worker's job goes back on the queue
    worker_prefetch_multiplier=1,  # long tasks: do not reserve more than one at a time
    worker_hijack_root_logger=False,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,  # job state lives in PostgreSQL, not in a Celery result backend
)

QUEUES = {JobKind.INGEST: "ingest", JobKind.GENERATE: "assess", JobKind.EVALUATE: "assess"}


@signals.setup_logging.connect
def _use_json_logging(**_):
    configure_logging(settings.log_level)


@signals.worker_process_init.connect
def _warm_up(**_):
    from app.warmup import warm_up_in_background

    warm_up_in_background()


def _handlers() -> dict[str, Callable[[Session, Job], None]]:
    # Imported here so the API process, which only enqueues, never loads the heavy modules.
    from app.pipeline.ingest import ingest_video
    from app.services import assessment, evaluation

    return {
        JobKind.INGEST: ingest_video,
        JobKind.GENERATE: assessment.generate,
        JobKind.EVALUATE: evaluation.evaluate,
    }


def mark_failed(db: Session, job: Job, message: str) -> None:
    """Record the failure on the job and on the thing it was working on."""
    job.status, job.error = JobStatus.FAILED, message
    if job.kind == JobKind.INGEST:
        video = db.get(Video, job.video_id)
        video.status, video.error = VideoStatus.FAILED, message
    else:
        assessment = db.get(Assessment, job.assessment_id)
        assessment.error = message
        if job.kind == JobKind.GENERATE:
            assessment.status = AssessmentStatus.FAILED
        # A failed evaluation leaves the assessment "submitted": the answers are kept
        # and the job can be retried.
    db.commit()


@celery_app.task(bind=True, name="run_job", max_retries=settings.job_max_retries)
def run_job(self, job_id: int, request_id: str = "-") -> None:
    request_id_var.set(request_id)  # worker logs carry the ID of the HTTP request that queued it
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None or job.status == JobStatus.SUCCEEDED:
            return  # a redelivered message for work that is already done
        job.status, job.error, job.attempts = JobStatus.RUNNING, None, job.attempts + 1
        db.commit()
        try:
            _handlers()[job.kind](db, job)
            job.status, job.stage = JobStatus.SUCCEEDED, "done"
            db.commit()
            log.info("job succeeded", extra={"job_id": job.id, "kind": job.kind})
        except Exception as exc:
            db.rollback()
            message = str(exc) or type(exc).__name__
            if getattr(exc, "retryable", False) and self.request.retries < self.max_retries:
                delay = settings.job_retry_base_seconds * 2 ** self.request.retries
                job.status = JobStatus.QUEUED
                job.error = f"Attempt {job.attempts} failed ({message}). Retrying in {delay}s."
                db.commit()
                log.warning("job will be retried", extra={
                    "job_id": job_id, "attempt": job.attempts, "delay": delay, "reason": message})
                raise self.retry(exc=exc, countdown=delay)
            log.exception("job failed", extra={"job_id": job_id, "kind": job.kind})
            mark_failed(db, job, message)
