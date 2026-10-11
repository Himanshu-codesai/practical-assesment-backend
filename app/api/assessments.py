from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.errors import Conflict, NotFound
from app.models import Assessment, AssessmentStatus
from app.services import assessment as assessment_service
from app.services import evaluation
from app.tasks import Dispatcher, get_dispatcher

router = APIRouter(prefix="/assessments", tags=["assessments"])


def get_assessment(assessment_id: int, db: Session = Depends(get_db)) -> Assessment:
    assessment = db.get(Assessment, assessment_id)
    if assessment is None:
        raise NotFound(f"Assessment {assessment_id} not found.")
    return assessment


def get_evaluated(assessment: Assessment = Depends(get_assessment)) -> Assessment:
    if assessment.status != AssessmentStatus.EVALUATED:
        raise Conflict(
            f"Assessment is '{assessment.status}'; results exist once it is 'evaluated'.")
    return assessment


@router.get("", response_model=list[schemas.AssessmentOut], summary="List assessments")
def list_assessments(
    learner_id: int | None = None, video_id: int | None = None, db: Session = Depends(get_db)
):
    statement = select(Assessment).order_by(Assessment.id)
    if learner_id is not None:
        statement = statement.where(Assessment.learner_id == learner_id)
    if video_id is not None:
        statement = statement.where(Assessment.video_id == video_id)
    return db.scalars(statement).all()


@router.get("/{assessment_id}", response_model=schemas.AssessmentOut,
            summary="The assessment and its questions (without the answer key)")
def read_assessment(assessment: Assessment = Depends(get_assessment)):
    return assessment


@router.post("/{assessment_id}/submit", response_model=schemas.SubmitAccepted,
             status_code=status.HTTP_202_ACCEPTED, summary="Submit answers for evaluation")
def submit_answers(
    body: schemas.SubmitRequest,
    assessment: Assessment = Depends(get_assessment),
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    """Unanswered questions score zero. Poll the returned job, then fetch the result and report."""
    job = evaluation.submit(db, assessment, body.answers)
    dispatcher.enqueue(job)
    return schemas.SubmitAccepted(assessment_id=assessment.id, status=assessment.status, job=job)


@router.post("/{assessment_id}/retake", response_model=schemas.SubmitAccepted,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Discard this attempt and generate a new set of questions")
def retake_assessment(
    assessment: Assessment = Depends(get_assessment),
    db: Session = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
):
    """The answers and report of the current attempt are deleted. The new questions are
    written to differ from the previous ones."""
    job = assessment_service.retake(db, assessment)
    dispatcher.enqueue(job)
    return schemas.SubmitAccepted(assessment_id=assessment.id, status=assessment.status, job=job)


@router.get("/{assessment_id}/result", response_model=schemas.AssessmentResult,
            summary="Per-question score, correctness, feedback and areas of improvement")
def read_result(assessment: Assessment = Depends(get_evaluated)):
    results = [
        schemas.QuestionResult(
            question_id=q.id, type=q.type, topic=q.topic, prompt=q.prompt, options=q.options,
            learner_selected_option=q.answer.selected_option, learner_text=q.answer.text,
            correct_option=q.correct_option, reference_answer=q.reference_answer,
            score=round(q.answer.score * q.points, 2), max_score=q.points,
            correctness=q.answer.correctness, feedback=q.answer.feedback,
            improvement_areas=q.answer.improvement_areas,
        )
        for q in assessment.questions
    ]
    score = round(sum(r.score for r in results), 2)
    max_score = sum(r.max_score for r in results)
    return schemas.AssessmentResult(
        assessment_id=assessment.id, status=assessment.status, score=score, max_score=max_score,
        percentage=round(100 * score / max_score, 1) if max_score else 0.0, results=results,
    )


@router.get("/{assessment_id}/report", summary="The learning report for a completed assessment")
def read_report(assessment: Assessment = Depends(get_evaluated)) -> dict:
    return assessment.report.data
