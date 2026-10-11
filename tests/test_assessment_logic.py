"""Unit tests for question validation, grading and report maths. No database or LLM."""
import pytest

from app.llm.schemas import GeneratedQuestion
from app.models import Question
from app.services.assessment import question_count, validate_question
from app.services.evaluation import grade_objective, is_non_answer, score_to_correctness
from app.services.qa import question_key
from app.services.report import compute_stats, performance_level


def generated(**overrides) -> GeneratedQuestion:
    base = dict(
        type="mcq", bloom_level="understand", topic="Vectors", prompt="Which is true?",
        options=["a", "b", "c", "d"], correct_option_index=2, reference_answer="", rubric=[],
        explanation="Because.", source_chunk_index=3,
    )
    return GeneratedQuestion(**{**base, **overrides})


def test_valid_questions_pass():
    assert validate_question(generated(), {3}) is None
    assert validate_question(
        generated(type="true_false", options=["False", "True"], correct_option_index=1), {3}
    ) is None
    assert validate_question(
        generated(type="scenario", options=[], correct_option_index=-1,
                  reference_answer="An answer.", rubric=["point"]), {3}
    ) is None


@pytest.mark.parametrize("overrides, reason", [
    ({"source_chunk_index": 99}, "chunk"),
    ({"options": ["a", "b", "c"]}, "4 options"),
    ({"options": ["a", "A ", "c", "d"]}, "duplicate"),
    ({"correct_option_index": 4}, "out of range"),
    ({"correct_option_index": -1}, "out of range"),
    ({"type": "true_false", "options": ["Yes", "No"], "correct_option_index": 0}, "True and False"),
    ({"type": "short_answer", "options": [], "rubric": []}, "rubric"),
    ({"prompt": "  "}, "empty"),
])
def test_invalid_questions_are_rejected_with_a_reason(overrides, reason):
    assert reason in validate_question(generated(**overrides), {3})


def stored_question() -> Question:
    return Question(type="mcq", topic="Vectors", options=["a", "b", "c", "d"], correct_option=2,
                    explanation="Because c.")


def test_objective_grading_is_exact():
    right = grade_objective(stored_question(), 2)
    wrong = grade_objective(stored_question(), 0)
    blank = grade_objective(stored_question(), None)
    assert (right.score, right.correctness, right.improvement_areas) == (1.0, "correct", [])
    assert (wrong.score, wrong.correctness, wrong.improvement_areas) == (0.0, "incorrect", ["Vectors"])
    assert '"c"' in wrong.feedback and "Because c." in wrong.feedback
    assert blank.score == 0.0 and "No answer" in blank.feedback


@pytest.mark.parametrize("score, label", [
    (1.0, "correct"), (0.85, "correct"), (0.84, "partially_correct"),
    (0.5, "partially_correct"), (0.33, "partially_correct"),  # one of two, one of three
    (0.25, "partially_correct"), (0.24, "incorrect"), (0.0, "incorrect"),
])
def test_score_to_correctness_boundaries(score, label):
    assert score_to_correctness(score) == label


def test_performance_level_boundaries():
    assert [performance_level(p) for p in (80, 79.9, 50, 49.9)] == [
        "strong", "developing", "developing", "needs_work"]


def test_report_stats_are_weighted_and_grouped():
    rows = [
        {"topic": "Vectors", "bloom_level": "apply", "points": 1, "score": 1.0, "correctness": "correct"},
        {"topic": "Vectors", "bloom_level": "understand", "points": 1, "score": 0.5, "correctness": "partially_correct"},
        {"topic": "Matrices", "bloom_level": "apply", "points": 2, "score": 0.0, "correctness": "incorrect"},
    ]
    stats = compute_stats(rows)
    assert stats["overall"] == {
        "score": 1.5, "max_score": 4, "percentage": 37.5, "level": "needs_work", "questions": 3,
        "correct": 1, "partially_correct": 1, "incorrect": 1,
    }
    topics = {t["topic"]: t for t in stats["topic_analysis"]}
    assert topics["Vectors"]["percentage"] == 75.0 and topics["Vectors"]["level"] == "developing"
    assert topics["Matrices"]["percentage"] == 0.0 and topics["Matrices"]["questions"] == 1
    skills = {s["bloom_level"]: s for s in stats["skill_analysis"]}
    assert skills["apply"]["percentage"] == pytest.approx(33.3)


@pytest.mark.parametrize("chunks, expected", [(1, 3), (2, 4), (3, 6), (4, 8), (20, 8)])
def test_question_count_scales_with_video_length_up_to_the_maximum(chunks, expected):
    assert question_count(chunks) == expected


@pytest.mark.parametrize("text", [None, "", "   ", "idk", "IDK.", "I don't know", "no idea!", "???", "n/a", "-"])
def test_non_answers_are_recognised(text):
    assert is_non_answer(text)


@pytest.mark.parametrize("text", ["ATP", "no", "I don't know exactly, but it uses light", "pass the electrons on"])
def test_real_answers_are_not_treated_as_non_answers(text):
    assert not is_non_answer(text)


def test_question_key_ignores_case_spacing_and_end_punctuation():
    assert question_key("  What is   ReLU? ") == question_key("what is relu") == "what is relu"
    assert question_key("What is ReLU?") != question_key("Why is ReLU used?")


def test_report_stats_handle_no_questions():
    assert compute_stats([])["overall"]["percentage"] == 0.0
