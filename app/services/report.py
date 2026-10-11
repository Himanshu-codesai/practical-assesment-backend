"""The learning report.

Every number in the report is computed here from the stored grades. The LLM is only
asked to write the narrative (summary, strengths, weaknesses, suggestions) on top of
those numbers, so it cannot invent a score or a topic.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.llm.base import render_prompt
from app.llm.client import get_llm
from app.llm.schemas import ReportNarrative
from app.models import Assessment, Question, Report
from app.pipeline.chunking import format_timestamp
from app.pipeline.transcript import youtube_link

log = logging.getLogger(__name__)


def performance_level(percentage: float) -> str:
    if percentage >= 80:
        return "strong"
    if percentage >= 50:
        return "developing"
    return "needs_work"


def _group(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    result = []
    for name, items in groups.items():
        earned = sum(r["score"] * r["points"] for r in items)
        possible = sum(r["points"] for r in items)
        percentage = round(100 * earned / possible, 1) if possible else 0.0
        result.append({
            key: name,
            "questions": len(items),
            "score": round(earned, 2),
            "max_score": possible,
            "percentage": percentage,
            "level": performance_level(percentage),
        })
    return result


def compute_stats(rows: list[dict]) -> dict:
    """Aggregate graded questions. Each row: topic, bloom_level, points, score, correctness."""
    earned = sum(r["score"] * r["points"] for r in rows)
    possible = sum(r["points"] for r in rows)
    percentage = round(100 * earned / possible, 1) if possible else 0.0
    counts = defaultdict(int)
    for row in rows:
        counts[row["correctness"]] += 1
    return {
        "overall": {
            "score": round(earned, 2),
            "max_score": possible,
            "percentage": percentage,
            "level": performance_level(percentage),
            "questions": len(rows),
            "correct": counts["correct"],
            "partially_correct": counts["partially_correct"],
            "incorrect": counts["incorrect"],
        },
        "topic_analysis": _group(rows, "topic"),
        "skill_analysis": _group(rows, "bloom_level"),
    }


def _rewatch(question: Question, source_url: str | None) -> dict | None:
    chunk = question.source_chunk
    if chunk is None or chunk.start_seconds is None:
        return None
    segment = {
        "topic": question.topic,
        "from": format_timestamp(chunk.start_seconds),
        "to": format_timestamp(chunk.end_seconds),
    }
    url = youtube_link(source_url, chunk.start_seconds)
    if url:
        segment["url"] = url
    return segment


MAX_NARRATIVE_ITEMS = 3  # the prompt asks for at most this many; enforced here as well


def _answers(question: Question) -> tuple[str | None, str | None]:
    """(what the learner answered, what a right answer is), both as readable text."""
    answer = question.answer
    if question.options:
        chosen = answer.selected_option
        return (question.options[chosen] if chosen is not None else None,
                question.options[question.correct_option])
    return answer.text, question.reference_answer


def build_report(db: Session, assessment: Assessment) -> Report:
    video = assessment.video
    rows, breakdown, rewatch = [], [], []
    for question in assessment.questions:
        answer = question.answer
        rows.append({
            "topic": question.topic, "bloom_level": question.bloom_level,
            "points": question.points, "score": answer.score, "correctness": answer.correctness,
        })
        segment = _rewatch(question, video.source_url)
        learner_answer, correct_answer = _answers(question)
        breakdown.append({
            "question_id": question.id,
            "type": question.type,
            "topic": question.topic,
            "bloom_level": question.bloom_level,
            "prompt": question.prompt,
            "learner_answer": learner_answer,
            "correct_answer": correct_answer,
            "score": answer.score,
            "correctness": answer.correctness,
            "feedback": answer.feedback,
            "improvement_areas": answer.improvement_areas,
            "video_segment": segment,
        })
        if answer.correctness != "correct" and segment and segment not in rewatch:
            rewatch.append(segment)

    stats = compute_stats(rows)
    system, prompt = render_prompt(
        "report",
        learner=assessment.learner.name,
        title=video.title,
        stats=json.dumps({**stats, "questions": breakdown}, indent=2),
    )
    narrative = get_llm().generate_structured(
        task="report", system=system, prompt=prompt, schema=ReportNarrative
    )

    data = {
        "assessment_id": assessment.id,
        "learner": {"id": assessment.learner_id, "name": assessment.learner.name},
        "video": {"id": video.id, "title": video.title},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_performance": stats["overall"],
        "topic_analysis": stats["topic_analysis"],
        "skill_analysis": stats["skill_analysis"],
        "strengths": narrative.strengths[:MAX_NARRATIVE_ITEMS],
        "weaknesses": narrative.weaknesses[:MAX_NARRATIVE_ITEMS],
        "improvement_suggestions": narrative.improvement_suggestions[:MAX_NARRATIVE_ITEMS],
        "ai_summary": narrative.summary,
        "recommended_rewatch": rewatch,
        "question_breakdown": breakdown,
    }
    if assessment.report is None:
        assessment.report = Report(data=data)
    else:
        assessment.report.data = data
    db.flush()
    return assessment.report
