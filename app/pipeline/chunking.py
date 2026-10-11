"""Group small caption segments into retrieval-sized chunks that keep their timestamps."""
from dataclasses import dataclass

from app.pipeline.transcript import Segment

# After reaching the target size, keep going this many more words looking for a sentence end.
SENTENCE_SLACK_WORDS = 40


@dataclass
class ChunkDraft:
    index: int
    text: str
    start: float | None
    end: float | None


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith((".", "?", "!"))


def build_chunks(
    segments: list[Segment], target_words: int = 180, overlap_segments: int = 2
) -> list[ChunkDraft]:
    """Greedy chunker.

    Captions arrive as ~2-second fragments, far too small to embed on their own.
    We merge them until a chunk reaches `target_words`, prefer to stop at a sentence
    boundary, and repeat the last `overlap_segments` fragments at the start of the
    next chunk so an idea that straddles a boundary is still retrievable.
    """
    chunks: list[ChunkDraft] = []
    current: list[Segment] = []
    words = 0
    fresh = 0  # segments in `current` that are not overlap carried from the previous chunk

    def emit() -> None:
        chunks.append(
            ChunkDraft(
                index=len(chunks),
                text=" ".join(s.text for s in current),
                start=current[0].start,
                end=current[-1].end,
            )
        )

    for segment in segments:
        current.append(segment)
        words += len(segment.text.split())
        fresh += 1
        at_target = words >= target_words and _ends_sentence(segment.text)
        if at_target or words >= target_words + SENTENCE_SLACK_WORDS:
            emit()
            current = current[-overlap_segments:] if overlap_segments else []
            words = sum(len(s.text.split()) for s in current)
            fresh = 0

    if fresh:
        emit()
    return chunks


def format_timestamp(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
