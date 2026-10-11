"""Watch-progress tracking, the completion trigger, and assessment generation."""
import logging

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import Conflict, PipelineError
from app.llm.base import render_prompt
from app.llm.client import get_llm
from app.llm.schemas import OBJECTIVE_TYPES, GeneratedAssessment, GeneratedQuestion
from app.models import (
    Assessment, AssessmentStatus, Chunk, Job, JobKind, Learner, Question, Video, VideoStatus,
    WatchProgress,
)
from app.pipeline.ingest import format_chunks, set_stage
from app.retrieval import store

log = logging.getLogger(__name__)

MIN_VALID_QUESTIONS = 2
MIN_QUESTIONS = 3
QUESTIONS_PER_CHUNK = 2


def question_count(chunk_count: int) -> int:
    """How many questions to ask for a video with this many transcript chunks.

    A chunk is about 180 words, roughly one explained idea. Asking the full set of a
    90-second clip would stretch thin content, so short videos get fewer questions,
    up to the configured maximum for a full lesson.
    """
    wanted = max(MIN_QUESTIONS, QUESTIONS_PER_CHUNK * chunk_count)
    return min(settings.questions_per_assessment, wanted)


def retake(db: Session, assessment: Assessment) -> Job:
    """Throw away the current attempt and queue a fresh set of questions.

    The old questions are left in place for now: `generate` reads them so it can tell
    the model not to repeat them, then deletes them.
    """
    busy = (AssessmentStatus.GENERATING, AssessmentStatus.SUBMITTED)
    if assessment.status in busy:
        raise Conflict(f"Assessment is '{assessment.status}'; wait for it to finish first.")
    assessment.report = None  # removes the stored report row
    assessment.status, assessment.error = AssessmentStatus.GENERATING, None
    job = Job(kind=JobKind.GENERATE, assessment_id=assessment.id, video_id=assessment.video_id)
    db.add(job)
    db.commit()
    log.info("assessment retake", extra={"assessment_id": assessment.id})
    return job


def record_progress(
    db: Session, learner: Learner, video: Video, percent: float
) -> tuple[WatchProgress, Assessment | None, Job | None]:
    """Save how much the learner has watched; start an assessment at the threshold.

    Returns (progress, assessment, job). `job` is set only when this call created the
    assessment, so the caller knows to enqueue it.
    """
    progress = db.scalar(
        select(WatchProgress).where(
            WatchProgress.learner_id == learner.id, WatchProgress.video_id == video.id
        )
    )
    if progress is None:
        progress = WatchProgress(learner_id=learner.id, video_id=video.id, percent=0.0)
        db.add(progress)
    progress.percent = max(progress.percent, percent)  # scrubbing backwards never lowers it

    find_existing = select(Assessment).where(
        Assessment.learner_id == learner.id, Assessment.video_id == video.id
    )
    assessment = db.scalar(find_existing)
    db.commit()  # the progress update is saved whatever happens below
    if assessment is not None or progress.percent < settings.completion_threshold:
        return progress, assessment, None

    if video.status != VideoStatus.READY:
        raise Conflict("The video is still being processed; an assessment cannot be created yet.")

    assessment = Assessment(learner_id=learner.id, video_id=video.id)
    db.add(assessment)
    try:
        db.flush()
    except IntegrityError:
        # Two progress updates crossed the threshold at once; the unique constraint
        # let only one through. Use the assessment the other request created.
        db.rollback()
        return progress, db.scalar(find_existing), None
    job = Job(kind=JobKind.GENERATE, assessment_id=assessment.id, video_id=video.id)
    db.add(job)
    db.commit()
    log.info("completion threshold reached", extra={
        "learner_id": learner.id, "video_id": video.id, "assessment_id": assessment.id})
    return progress, assessment, job


def select_context(db: Session, video: Video) -> list[Chunk]:
    """Choose the transcript chunks the questions will be written from.

    A short video fits in the prompt whole. For a longer one we retrieve the best
    chunks for each topic, which keeps the prompt bounded and guarantees every topic
    is represented instead of only the start of the video.
    """
    if len(video.chunks) <= settings.max_context_chunks:
        return list(video.chunks)

    topics = video.topics or []
    per_topic = max(1, settings.max_context_chunks // max(1, len(topics)))
    picked: dict[int, Chunk] = {}
    for topic in topics:
        query = f"{topic['name']}: {topic['summary']}"
        for hit in store.search(db, query, video_id=video.id, top_k=per_topic):
            picked[hit.chunk.id] = hit.chunk
    return sorted(picked.values(), key=lambda c: c.index)


def validate_question(q: GeneratedQuestion, chunk_indexes: set[int]) -> str | None:
    """Return why a generated question is unusable, or None if it is fine.

    The reply was already validated against the JSON schema; these are the rules a schema
    cannot express.
    """
    if q.source_chunk_index not in chunk_indexes:
        return "cites a chunk that was not provided"
    if not q.prompt.strip():
        return "empty prompt"
    if q.type in OBJECTIVE_TYPES:
        expected = 4 if q.type == "mcq" else 2
        if len(q.options) != expected:
            return f"{q.type} needs {expected} options, got {len(q.options)}"
        labels = {o.strip().lower() for o in q.options}
        if len(labels) != expected:
            return "duplicate options"
        if q.type == "true_false" and labels != {"true", "false"}:
            return "true_false options must be True and False"
        if not 0 <= q.correct_option_index < expected:
            return "correct_option_index out of range"
    elif not q.reference_answer.strip() or not q.rubric:
        return "open question without a reference answer or rubric"
    return None


def avoid_note(previous_prompts: list[str]) -> str:
    """On a retake, tell the model which questions were already asked. Empty the first time."""
    if not previous_prompts:
        return ""
    return ("The learner has already been asked the questions below. Test the same material "
            "from different angles and do not repeat them:\n"
            + "\n".join(f"- {prompt}" for prompt in previous_prompts))


def generate(db: Session, job: Job) -> None:
    assessment = db.get(Assessment, job.assessment_id)
    video = assessment.video
    assessment.status, assessment.error = AssessmentStatus.GENERATING, None
    # On a retake the previous questions still exist: remember them so the new set differs.
    previous = [q.prompt for q in assessment.questions]
    db.execute(delete(Question).where(Question.assessment_id == assessment.id))  # idempotent
    db.expire(assessment, ["questions"])

    set_stage(db, job, "retrieve_context")
    context = select_context(db, video)
    by_index = {c.index: c for c in context}

    set_stage(db, job, "generate_questions")
    system, prompt = render_prompt(
        "assessment",
        title=video.title,
        summary=video.summary or "",
        topics="\n".join(f"- {t['name']}: {t['summary']}" for t in video.topics or []),
        count=question_count(len(video.chunks)),
        avoid=avoid_note(previous),
        chunks=format_chunks(context),
    )
    generated = get_llm().generate_structured(
        task="assessment", system=system, prompt=prompt, schema=GeneratedAssessment
    )

    set_stage(db, job, "validate")
    topic_names = {t["name"] for t in video.topics or []}
    position = 0
    for q in generated.questions:
        problem = validate_question(q, set(by_index))
        if problem:
            log.warning("question rejected", extra={"reason": problem, "prompt": q.prompt[:80]})
            continue
        source = by_index[q.source_chunk_index]
        objective = q.type in OBJECTIVE_TYPES
        correct = q.correct_option_index
        if q.type == "true_false":  # stored as ["True", "False"] whatever order the model used
            correct = 0 if q.options[correct].strip().lower() == "true" else 1
        db.add(Question(
            assessment_id=assessment.id,
            position=position,
            type=q.type,
            bloom_level=q.bloom_level,
            # Fall back to the source chunk's topic if the model invented a topic name.
            topic=q.topic if q.topic in topic_names else (source.topic or "General"),
            prompt=q.prompt.strip(),
            options=["True", "False"] if q.type == "true_false" else q.options,
            correct_option=correct if objective else None,
            reference_answer=None if objective else q.reference_answer,
            rubric=[] if objective else q.rubric,
            explanation=q.explanation,
            source_chunk_id=source.id,
        ))
        position += 1

    if position < MIN_VALID_QUESTIONS:
        raise PipelineError(
            f"Only {position} of {len(generated.questions)} generated questions were valid.",
            retryable=True,
        )
    assessment.status = AssessmentStatus.READY
    log.info("assessment ready", extra={"assessment_id": assessment.id, "questions": position})
