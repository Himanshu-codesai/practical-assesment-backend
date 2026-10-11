"""Database tables. Status values are plain strings so they are easy to read in SQL."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VideoStatus:
    PENDING, PROCESSING, READY, FAILED = "pending", "processing", "ready", "failed"


class JobStatus:
    QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"


class JobKind:
    INGEST, GENERATE, EVALUATE = "ingest", "generate_assessment", "evaluate"


class AssessmentStatus:
    GENERATING, READY, SUBMITTED, EVALUATED, FAILED = (
        "generating", "ready", "submitted", "evaluated", "failed",
    )


class Learner(Base):
    __tablename__ = "learners"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(20))  # youtube | upload
    source_url: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=VideoStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    # Cached transcript segments [{text, start, end}], so a re-run never re-transcribes.
    # deferred: the list endpoints never read it, so they should not load it.
    transcript: Mapped[list[dict] | None] = mapped_column(JSONB, deferred=True)
    # What the caller asked for: auto | captions | whisper.
    transcript_method: Mapped[str] = mapped_column(
        String(20), default="auto", server_default="auto")
    # What was actually used: captions | whisper | file.
    transcript_source: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)
    topics: Mapped[list[dict] | None] = mapped_column(JSONB)  # [{name, summary}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Chunk.index"
    )


class Chunk(Base):
    __tablename__ = "chunks"
    # Text and timestamps live here; the vectors live in Qdrant under the same id.
    __table_args__ = (UniqueConstraint("video_id", "index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    topic: Mapped[str | None] = mapped_column(String(200))

    video: Mapped[Video] = relationship(back_populates="chunks")


class Job(Base):
    """One row per background task, so clients can poll progress."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    stage: Mapped[str | None] = mapped_column(String(50))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    assessment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WatchProgress(Base):
    __tablename__ = "watch_progress"
    __table_args__ = (UniqueConstraint("learner_id", "video_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    percent: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Assessment(Base):
    __tablename__ = "assessments"
    # One assessment per learner and video: the threshold trigger must fire only once.
    __table_args__ = (UniqueConstraint("learner_id", "video_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("learners.id", ondelete="CASCADE"))
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default=AssessmentStatus.GENERATING)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    learner: Mapped[Learner] = relationship()
    video: Mapped[Video] = relationship()
    questions: Mapped[list["Question"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", order_by="Question.position",
        lazy="selectin",
    )
    report: Mapped["Report | None"] = relationship(
        back_populates="assessment", cascade="all, delete-orphan", uselist=False
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))  # mcq | true_false | short_answer | scenario
    bloom_level: Mapped[str] = mapped_column(String(20))
    topic: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSONB, default=list)
    correct_option: Mapped[int | None] = mapped_column(Integer)  # objective questions only
    reference_answer: Mapped[str | None] = mapped_column(Text)  # open questions only
    rubric: Mapped[list[str]] = mapped_column(JSONB, default=list)
    explanation: Mapped[str] = mapped_column(Text, default="")
    points: Mapped[float] = mapped_column(Float, default=1.0)
    # The chunk the question was generated from: grounding and "rewatch here" links.
    source_chunk_id: Mapped[int | None] = mapped_column(
        ForeignKey("chunks.id", ondelete="SET NULL")
    )

    assessment: Mapped[Assessment] = relationship(back_populates="questions")
    source_chunk: Mapped[Chunk | None] = relationship()
    answer: Mapped["Answer | None"] = relationship(
        back_populates="question", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), unique=True
    )
    selected_option: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)
    # Filled in by evaluation:
    score: Mapped[float | None] = mapped_column(Float)  # 0.0 to 1.0
    correctness: Mapped[str | None] = mapped_column(String(20))
    feedback: Mapped[str | None] = mapped_column(Text)
    improvement_areas: Mapped[list[str]] = mapped_column(JSONB, default=list)

    question: Mapped[Question] = relationship(back_populates="answer")


class CachedAnswer(Base):
    """A saved reply of the Ask endpoint, so the same question on a video is answered once."""

    __tablename__ = "cached_answers"
    __table_args__ = (UniqueConstraint("video_id", "question_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    question_key: Mapped[str] = mapped_column(Text)  # the question, normalised
    response: Mapped[dict] = mapped_column(JSON)  # the full AskResponse body
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessments.id", ondelete="CASCADE"), unique=True
    )
    data: Mapped[dict] = mapped_column(JSON)  # JSON, not JSONB: keeps the report's key order
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assessment: Mapped[Assessment] = relationship(back_populates="report")
