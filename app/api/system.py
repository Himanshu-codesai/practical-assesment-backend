"""Learners, jobs and health: the small endpoints."""
import redis
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app import schemas
from app.config import settings
from app.db import get_db
from app.errors import Conflict, NotFound
from app.models import Assessment, AssessmentStatus, Job, JobStatus, Learner, WatchProgress
from app.retrieval import store
from app.tasks import Dispatcher, get_dispatcher

router = APIRouter()


@router.post("/learners", response_model=schemas.LearnerOut,
             status_code=status.HTTP_201_CREATED, tags=["learners"], summary="Create a learner")
def create_learner(body: schemas.LearnerCreate, db: Session = Depends(get_db)):
    learner = Learner(name=body.name)
    db.add(learner)
    db.commit()
    return learner


@router.get("/learners", response_model=list[schemas.LearnerOut], tags=["learners"],
            summary="List learners")
def list_learners(db: Session = Depends(get_db)):
    return db.scalars(select(Learner).order_by(Learner.id)).all()


@router.get("/learners/{learner_id}/progress", response_model=list[schemas.LearnerVideoStatus],
            tags=["learners"], summary="A learner's watch progress and assessment on each video")
def learner_progress(learner_id: int, db: Session = Depends(get_db)):
    """Videos are shared by all learners. This returns the part that is personal: how much
    of each video this learner has watched, and the status and score of their assessment.
    Videos the learner has not started are not listed."""
    if db.get(Learner, learner_id) is None:
        raise NotFound(f"Learner {learner_id} not found.")
    watched = {p.video_id: p.percent for p in db.scalars(
        select(WatchProgress).where(WatchProgress.learner_id == learner_id))}
    assessments = {a.video_id: a for a in db.scalars(
        select(Assessment).where(Assessment.learner_id == learner_id))}
    rows = []
    for video_id in sorted(set(watched) | set(assessments)):
        assessment = assessments.get(video_id)
        evaluated = assessment is not None and assessment.status == AssessmentStatus.EVALUATED
        rows.append(schemas.LearnerVideoStatus(
            video_id=video_id,
            percent_watched=watched.get(video_id, 0.0),
            assessment_id=assessment.id if assessment else None,
            assessment_status=assessment.status if assessment else None,
            score_percentage=(assessment.report.data["overall_performance"]["percentage"]
                              if evaluated and assessment.report else None),
        ))
    return rows


@router.delete("/learners/{learner_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["learners"],
               summary="Delete a learner and everything that belongs to them")
def delete_learner(learner_id: int, db: Session = Depends(get_db)):
    """Removes the learner with their watch progress, assessments, answers and reports.
    Videos are shared, so they stay. Refused while one of the learner's quizzes is being
    written or graded."""
    if db.get(Learner, learner_id) is None:
        raise NotFound(f"Learner {learner_id} not found.")
    active = db.scalar(
        select(Job.id).join(Assessment, Job.assessment_id == Assessment.id).where(
            Assessment.learner_id == learner_id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])))
    if active is not None:
        raise Conflict(f"Job {active} is still working for this learner. Try again when it finishes.")
    # One statement: the foreign keys are ON DELETE CASCADE, so the database removes the rest.
    db.execute(delete(Learner).where(Learner.id == learner_id))
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def get_job(job_id: int, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound(f"Job {job_id} not found.")
    return job


@router.get("/jobs/{job_id}", response_model=schemas.JobOut, tags=["jobs"],
            summary="Status and current stage of a background job")
def read_job(job: Job = Depends(get_job)):
    return job


@router.post("/jobs/{job_id}/retry", response_model=schemas.JobOut, tags=["jobs"],
             status_code=status.HTTP_202_ACCEPTED, summary="Run a failed job again")
def retry_job(
    job: Job = Depends(get_job),
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    """Retryable failures are retried automatically; this is for jobs that ran out of attempts.
    Every job is idempotent, so a retry continues from cached work (e.g. the transcript)."""
    if job.status != JobStatus.FAILED:
        raise Conflict(f"Job is '{job.status}'; only failed jobs can be retried.")
    job.status, job.error = JobStatus.QUEUED, None
    db.commit()
    dispatcher.enqueue(job)
    return job


@router.get("/health", tags=["system"], summary="Liveness and dependency check")
def health(db: Session = Depends(get_db)):
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    try:
        store.client().get_collection(settings.qdrant_collection)
        checks["qdrant"] = "ok"
    except Exception as exc:
        checks["qdrant"] = f"error: {type(exc).__name__}"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks,
                 "llm_model": settings.llm_model},
    )
