"""Unit tests for the deterministic parts of the pipeline. No database, network or LLM."""
from app.llm.schemas import TopicSpan, VideoOutline
from app.models import Chunk
from app.pipeline.chunking import build_chunks, format_timestamp
from app.pipeline.ingest import assign_topics
from app.pipeline.transcript import (
    Segment, clean_text, parse_transcript_file, youtube_id, youtube_link,
)

SRT = """1
00:00:01,000 --> 00:00:04,000
Hello and <i>welcome</i>.

2
00:00:04,000 --> 00:00:09,500
[Music]

3
00:01:02,250 --> 00:01:05,000
Today we talk
about vectors.
"""

VTT = """WEBVTT

00:01.000 --> 00:04.000
A vector has a direction.

01:00:00.000 --> 01:00:02.000
And a length.
"""


def test_youtube_id_from_common_url_shapes():
    assert youtube_id("https://www.youtube.com/watch?v=aircAruvnKk&t=10s") == "aircAruvnKk"
    assert youtube_id("https://youtu.be/aircAruvnKk") == "aircAruvnKk"
    assert youtube_id("https://www.youtube.com/shorts/aircAruvnKk") == "aircAruvnKk"
    assert youtube_id("https://example.com/video.mp4") is None
    assert youtube_id(None) is None


def test_youtube_link_opens_at_the_start_of_the_passage():
    url = "https://youtu.be/aircAruvnKk"
    assert youtube_link(url, 259.8) == "https://www.youtube.com/watch?v=aircAruvnKk&t=259s"
    assert youtube_link(url, 0) == "https://www.youtube.com/watch?v=aircAruvnKk&t=0s"
    assert youtube_link(url, None) is None          # transcript without timestamps
    assert youtube_link(None, 30) is None           # uploaded file
    assert youtube_link("https://example.com/v.mp4", 30) is None


def test_clean_text_removes_caption_noise():
    assert clean_text("  [Music]  so   <b>this</b> is\nit (applause) ") == "so this is it"


def test_parse_srt_keeps_timestamps_and_drops_noise_only_cues():
    segments = parse_transcript_file("lesson.srt", SRT)
    assert [s.text for s in segments] == ["Hello and welcome.", "Today we talk about vectors."]
    assert (segments[0].start, segments[0].end) == (1.0, 4.0)
    assert segments[1].start == 62.25


def test_parse_vtt_with_and_without_hours():
    segments = parse_transcript_file("lesson.vtt", VTT)
    assert [s.start for s in segments] == [1.0, 3600.0]


def test_parse_plain_text_has_no_timestamps():
    segments = parse_transcript_file("notes.txt", "First idea. Second idea! Third?")
    assert len(segments) == 3
    assert segments[0].start is None


def _segments(count: int, words_each: int = 10) -> list[Segment]:
    return [
        Segment(" ".join(["word"] * (words_each - 1)) + f" s{i}.", float(i * 5), float(i * 5 + 5))
        for i in range(count)
    ]


def test_chunks_reach_target_size_and_keep_time_range():
    chunks = build_chunks(_segments(40), target_words=50, overlap_segments=0)
    assert len(chunks) == 8
    assert all(len(c.text.split()) == 50 for c in chunks)
    assert (chunks[0].start, chunks[0].end) == (0.0, 25.0)
    assert [c.index for c in chunks] == list(range(8))


def test_chunks_overlap_and_cover_every_segment():
    segments = _segments(23)
    chunks = build_chunks(segments, target_words=50, overlap_segments=2)
    # The second chunk starts with the last two segments of the first.
    assert chunks[1].text.startswith(" ".join(s.text for s in segments[3:5]))
    joined = " ".join(c.text for c in chunks)
    assert all(f" s{i}." in joined for i in range(23))


def test_chunk_waits_for_a_sentence_end_within_the_slack():
    segments = [Segment("word " * 9 + "word", 0, 1) for _ in range(5)]  # 50 words, no full stop
    segments += [Segment("and the end.", 1, 2), Segment("Next sentence here.", 2, 3)]
    chunks = build_chunks(segments, target_words=50, overlap_segments=0)
    assert chunks[0].text.endswith("and the end.")


def test_no_trailing_chunk_made_only_of_overlap():
    chunks = build_chunks(_segments(5), target_words=50, overlap_segments=2)
    assert len(chunks) == 1


def test_format_timestamp():
    assert format_timestamp(190.7) == "03:10"
    assert format_timestamp(3725) == "1:02:05"
    assert format_timestamp(None) == "--:--"


def test_assign_topics_uses_the_last_topic_that_has_started():
    chunks = [Chunk(index=i, text="x") for i in range(6)]
    outline = VideoOutline(summary="s", topics=[
        TopicSpan(name="Later", summary="", start_chunk=4),
        TopicSpan(name="Intro", summary="", start_chunk=1),  # unordered, and starts after chunk 0
    ])
    assign_topics(chunks, outline)
    assert [c.topic for c in chunks] == ["Intro", "Intro", "Intro", "Intro", "Later", "Later"]


def test_search_text_puts_the_video_and_topic_in_front_of_the_chunk():
    from app.models import Video
    from app.retrieval.store import text_for_search

    video = Video(title="Photosynthesis", topics=[
        {"name": "How it works", "summary": "How a leaf makes sugar."},
        {"name": "Chlorophyll", "summary": "Why plants are green."},
    ])
    chunk = Chunk(index=0, text="This is why it works.", topic="How it works", video=video)
    assert text_for_search(chunk) == (
        "Video: Photosynthesis. Topic: How it works. How a leaf makes sugar.\n"
        "This is why it works."
    )
    assert chunk.text == "This is why it works."        # the stored text is not changed


def test_search_text_copes_with_a_chunk_that_has_no_topic():
    from app.models import Video
    from app.retrieval.store import text_for_search

    chunk = Chunk(index=0, text="Hello.", topic=None, video=Video(title="Intro", topics=None))
    assert text_for_search(chunk) == "Video: Intro. Topic: General.\nHello."
