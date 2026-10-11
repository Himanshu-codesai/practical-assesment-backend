"""Shapes the LLM must return. app/llm/client.py writes each one into the prompt as a
JSON schema and validates the reply against it.

Limits such as "at most 3" or "0.0 to 1.0" are written as descriptions for the model and
enforced in our own code, where a bad value can be clamped or dropped instead of failing.
"""
from typing import Literal

from pydantic import BaseModel, Field

QuestionType = Literal["mcq", "true_false", "short_answer", "scenario"]
BloomLevel = Literal["remember", "understand", "apply", "analyze"]
OBJECTIVE_TYPES = {"mcq", "true_false"}


class TopicSpan(BaseModel):
    name: str = Field(description="Short topic title, 2 to 6 words")
    summary: str = Field(description="One sentence on what this part of the video teaches")
    start_chunk: int = Field(description="Index of the first chunk that belongs to this topic")


class VideoOutline(BaseModel):
    summary: str = Field(description="3 to 5 sentence summary of the whole video")
    topics: list[TopicSpan]


class GeneratedQuestion(BaseModel):
    type: QuestionType
    bloom_level: BloomLevel
    topic: str = Field(description="Exactly one of the topic names given in the prompt")
    prompt: str = Field(description="The question shown to the learner")
    options: list[str] = Field(
        description="mcq: exactly 4 options. true_false: ['True', 'False']. Otherwise empty."
    )
    correct_option_index: int = Field(
        description="Index into options of the correct answer; -1 for open questions"
    )
    reference_answer: str = Field(
        description="Model answer for short_answer/scenario questions; empty otherwise"
    )
    rubric: list[str] = Field(
        description="2 to 4 key points a full-credit open answer must contain; empty otherwise"
    )
    explanation: str = Field(description="Why the correct answer is correct, shown as feedback")
    source_chunk_index: int = Field(description="Index of the chunk this question is based on")


class GeneratedAssessment(BaseModel):
    questions: list[GeneratedQuestion]


class OpenAnswerGrade(BaseModel):
    question_id: int
    score: float = Field(description="0.0 to 1.0: the fraction of rubric points the answer covers")
    feedback: str = Field(description="1 or 2 short sentences, 35 words at most")
    improvement_areas: list[str] = Field(description="Concepts to revisit; empty if none")


class GradingResult(BaseModel):
    grades: list[OpenAnswerGrade]


class ReportNarrative(BaseModel):
    summary: str = Field(description="One or two short sentences, 35 words at most")
    strengths: list[str] = Field(description="At most 3 short phrases; empty if none")
    weaknesses: list[str] = Field(description="At most 3 short phrases; empty if none")
    improvement_suggestions: list[str] = Field(description="At most 3 short instructions")


class GroundedAnswer(BaseModel):
    answerable: bool = Field(description="False if the excerpts do not contain the answer")
    answer: str
    cited_chunk_indexes: list[int] = Field(description="Chunks that support the answer")
