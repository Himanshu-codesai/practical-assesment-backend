"""Answer evaluation.

Two strategies, chosen per question type:
- mcq / true_false are graded in code. It is exact, free and repeatable, and there is
  nothing for an LLM to add.
- short_answer / scenario are graded by the LLM against the question's rubric and the
  transcript chunk it came from. All open answers of a submission go in one request.
"""
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.errors import BadRequest, Conflict, PipelineError
from app.llm.base import render_prompt
from app.llm.client import get_llm
from app.llm.schemas import OBJECTIVE_TYPES, GradingResult
from app.models import Answer, Assessment, AssessmentStatus, Job, JobKind, Question
from app.pipeline.ingest import set_stage
from app.services.report import build_report

log = logging.getLogger(__name__)

CORRECT, PARTIAL, INCORRECT = "correct", "partially_correct", "incorrect"

# Written "answers" that say nothing. They score zero in code, with no model call.
NON_ANSWERS = {
    "idk", "i dont know", "i do not know", "dont know", "do not know", "no idea", "not sure",
    "no clue", "na", "n a", "none", "nothing", "skip", "pass",
}


def is_non_answer(text: str | None) -> bool:
    """True for an empty answer, punctuation only, or a phrase like "idk"."""
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower().replace("'", "")).split()
    return not words or " ".join(words) in NON_ANSWERS


@dataclass
class Grade:
    score: float
    feedback: str
    improvement_areas: list[str] = field(default_factory=list)

    @property
    def correctness(self) -> str:
        return score_to_correctness(self.score)


# Score needed for each label. A rubric has 2 to 4 points, so the smallest real credit is
# one point of four (0.25): from there up an answer is "partially correct", never "incorrect".
CORRECT_FROM = 0.85
PARTIAL_FROM = 0.25


def score_to_correctness(score: float) -> str:
    """The label is derived from the score in code so the two can never disagree."""
    if score >= CORRECT_FROM:
        return CORRECT
    if score >= PARTIAL_FROM:
        return PARTIAL
    return INCORRECT


def grade_objective(question: Question, selected_option: int | None) -> Grade:
    right = question.options[question.correct_option]
    if selected_option is None:
        return Grade(0.0, f"No answer was given. The correct answer is \"{right}\". "
                          f"{question.explanation}", [question.topic])
    if selected_option == question.correct_option:
        return Grade(1.0, f"Correct. {question.explanation}")
    return Grade(0.0, f"Incorrect. The correct answer is \"{right}\". {question.explanation}",
                 [question.topic])


def submit(db: Session, assessment: Assessment, answers: list) -> Job:
    """Store the learner's answers and create the evaluation job."""
    if assessment.status != AssessmentStatus.READY:
        raise Conflict(f"Assessment is '{assessment.status}', so answers cannot be submitted.")

    questions = {q.id: q for q in assessment.questions}
    given = {}
    for item in answers:
        question = questions.get(item.question_id)
        if question is None:
            raise BadRequest(f"Question {item.question_id} is not part of this assessment.")
        if question.type in OBJECTIVE_TYPES and item.selected_option is not None:
            if not 0 <= item.selected_option < len(question.options):
                raise BadRequest(f"Question {question.id}: selected_option is out of range.")
        given[question.id] = item

    db.execute(delete(Answer).where(Answer.question_id.in_(questions)))
    for question in questions.values():  # unanswered questions get an empty answer row
        item = given.get(question.id)
        text = (item.text or "").strip() if item else ""
        db.add(Answer(
            question_id=question.id,
            selected_option=item.selected_option if item else None,
            text=text or None,
        ))
    assessment.status = AssessmentStatus.SUBMITTED
    job = Job(kind=JobKind.EVALUATE, assessment_id=assessment.id, video_id=assessment.video_id)
    db.add(job)
    db.commit()
    return job


def _as_data(text: str) -> str:
    """Stop an answer from closing the tag that marks it as data, to pose as instructions."""
    return re.sub(r"</?\s*(learner_answer|question)\b[^>]*>", "", text, flags=re.I)


def _grading_item(question: Question) -> str:
    rubric = "\n".join(f"- {point}" for point in question.rubric)
    excerpt = question.source_chunk.text if question.source_chunk else "(not available)"
    return (
        f"<question question_id={question.id}>\n"
        f"Question: {question.prompt}\n"
        f"Rubric:\n{rubric}\n"
        f"Model answer: {question.reference_answer}\n"
        f"Transcript excerpt: {excerpt}\n"
        f"<learner_answer>\n{_as_data(question.answer.text)}\n</learner_answer>\n"
        f"</question>"
    )


def evaluate(db: Session, job: Job) -> None:
    assessment = db.get(Assessment, job.assessment_id)
    grades: dict[int, Grade] = {}
    open_questions = []

    set_stage(db, job, "grade_objective")
    for question in assessment.questions:
        answer = question.answer
        if question.type in OBJECTIVE_TYPES:
            grades[question.id] = grade_objective(question, answer.selected_option)
        elif is_non_answer(answer.text):
            grades[question.id] = Grade(
                0.0, f"No answer was given. A good answer: {question.reference_answer}",
                [question.topic])
        else:
            open_questions.append(question)

    if open_questions:
        set_stage(db, job, "grade_open_answers")
        system, prompt = render_prompt(
            "grading",
            title=assessment.video.title,
            items="\n\n".join(_grading_item(q) for q in open_questions),
        )
        result = get_llm().generate_structured(
            task="grading", system=system, prompt=prompt, schema=GradingResult
        )
        by_id = {g.question_id: g for g in result.grades}
        for question in open_questions:
            graded = by_id.get(question.id)
            if graded is None:
                raise PipelineError(f"No grade returned for question {question.id}.",
                                    retryable=True)
            grades[question.id] = Grade(
                score=min(1.0, max(0.0, graded.score)),  # clamp: the schema cannot bound it
                feedback=graded.feedback,
                improvement_areas=graded.improvement_areas,
            )

    for question in assessment.questions:
        grade, answer = grades[question.id], question.answer
        answer.score = grade.score
        answer.correctness = grade.correctness
        answer.feedback = grade.feedback
        answer.improvement_areas = grade.improvement_areas
    db.flush()

    set_stage(db, job, "report")
    build_report(db, assessment)
    assessment.status = AssessmentStatus.EVALUATED
    log.info("assessment evaluated", extra={"assessment_id": assessment.id})
