"""Request and response bodies of the REST API."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- learners ----
class LearnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["Asha Rao"])


class LearnerOut(ORMModel):
    id: int
    name: str


class LearnerVideoStatus(BaseModel):
    """Where one learner stands on one video. Videos are shared; this part is per learner."""

    video_id: int
    percent_watched: float
    assessment_id: int | None
    assessment_status: str | None
    score_percentage: float | None = Field(description="Set once the assessment is evaluated")


# ---- jobs ----
class JobOut(ORMModel):
    id: int
    kind: str
    status: str
    stage: str | None
    error: str | None
    attempts: int
    video_id: int | None
    assessment_id: int | None
    created_at: datetime
    updated_at: datetime


# ---- videos ----
class VideoCreate(BaseModel):
    source_url: str = Field(
        pattern=r"^https?://", examples=["https://www.youtube.com/watch?v=aircAruvnKk"],
        description="YouTube or any other site yt-dlp can download from",
    )
    title: str | None = Field(default=None, max_length=300)
    transcript_method: Literal["auto", "captions", "whisper"] = Field(
        default="auto",
        description="auto: captions if the video has them, otherwise Whisper. "
                    "captions: captions only (YouTube). whisper: always transcribe the audio.",
    )


class TopicOut(BaseModel):
    name: str
    summary: str


class VideoOut(ORMModel):
    id: int
    title: str
    source_type: str
    source_url: str | None
    status: str
    error: str | None
    duration_seconds: float | None
    transcript_method: str
    transcript_source: str | None
    summary: str | None
    topics: list[TopicOut] | None
    created_at: datetime


class TranscriptSegment(BaseModel):
    start: str = Field(description="mm:ss, or --:-- when the source had no timestamps")
    end: str
    url: str | None = Field(description="Opens the video at this line (YouTube sources only)")
    text: str


class TranscriptOut(BaseModel):
    video_id: int
    source: str = Field(description="Where the text came from: captions, whisper or file")
    segments: list[TranscriptSegment]


class VideoAccepted(BaseModel):
    video: VideoOut
    job: JobOut


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, examples=["How does a neuron's activation get computed?"])
    top_k: int = Field(default=5, ge=1, le=20)
    mode: Literal["hybrid", "dense", "keyword"] | None = Field(
        default=None, description="Override the retrieval mode to compare strategies"
    )


class ChunkHit(BaseModel):
    chunk_id: int
    video_id: int
    chunk_index: int
    topic: str | None
    start: str
    end: str
    url: str | None = Field(description="Opens the video at this passage (YouTube sources only)")
    score: float
    text: str


class SearchResponse(BaseModel):
    query: str
    mode: str
    results: list[ChunkHit]


class AskRequest(BaseModel):
    question: str = Field(min_length=2, examples=["Why are hidden layers needed?"])


class AskResponse(BaseModel):
    question: str
    answerable: bool
    answer: str
    citations: list[ChunkHit]
    cached: bool = Field(default=False, description="True when this is a saved earlier answer")


# ---- progress and assessments ----
class ProgressUpdate(BaseModel):
    learner_id: int
    percent_watched: float = Field(ge=0, le=100, examples=[85])


class ProgressOut(BaseModel):
    learner_id: int
    video_id: int
    percent_watched: float
    threshold: float
    assessment_id: int | None
    assessment_status: str | None
    job_id: int | None = Field(description="Set when this call triggered assessment generation")


class QuestionOut(ORMModel):
    """What the learner sees. Deliberately excludes the answer key."""

    id: int
    position: int
    type: str
    bloom_level: str
    topic: str
    prompt: str
    options: list[str]


class AssessmentOut(ORMModel):
    id: int
    learner_id: int
    video_id: int
    status: str
    error: str | None
    created_at: datetime
    questions: list[QuestionOut]


class AnswerIn(BaseModel):
    question_id: int
    selected_option: int | None = Field(
        default=None, description="Index into the question's options (mcq, true_false)"
    )
    text: str | None = Field(default=None, max_length=5000,
                             description="Written answer (short_answer, scenario)")


class SubmitRequest(BaseModel):
    answers: list[AnswerIn]


class SubmitAccepted(BaseModel):
    assessment_id: int
    status: str
    job: JobOut


class QuestionResult(BaseModel):
    question_id: int
    type: str
    topic: str
    prompt: str
    options: list[str]
    learner_selected_option: int | None
    learner_text: str | None
    correct_option: int | None
    reference_answer: str | None
    score: float
    max_score: float
    correctness: str
    feedback: str
    improvement_areas: list[str]


class AssessmentResult(BaseModel):
    assessment_id: int
    status: str
    score: float
    max_score: float
    percentage: float
    results: list[QuestionResult]
