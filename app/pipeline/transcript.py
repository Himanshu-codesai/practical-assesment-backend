"""Turn a video source into timestamped transcript segments.

Sources, cheapest first:
1. An uploaded caption/transcript file (.srt, .vtt, .txt) is parsed directly.
2. A YouTube URL uses the video's own captions when they exist (seconds, free).
3. Anything else (a video without captions, another site, an uploaded media file)
   is transcribed locally with faster-whisper, which also gives timestamps.
"""
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.errors import PipelineError
from app.models import Video

log = logging.getLogger(__name__)

TRANSCRIPT_EXTENSIONS = {".srt", ".vtt", ".txt"}
PERMANENT_DOWNLOAD_ERRORS = ("private video", "video unavailable", "unsupported url", "removed")
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".wav", ".ogg", ".flac"}

_YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")
_TIMING = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,](\d{3})"
)
_TAG = re.compile(r"<[^>]+>")
_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*(?:music|applause|laughter)[^)]*\)", re.I)


@dataclass
class Segment:
    text: str
    start: float | None  # seconds from the start of the video; None when unknown
    end: float | None


def clean_text(text: str) -> str:
    """Drop caption noise such as [Music] and HTML-style tags, and collapse whitespace."""
    return re.sub(r"\s+", " ", _NOISE.sub(" ", _TAG.sub("", text))).strip()


def youtube_id(url: str | None) -> str | None:
    match = _YOUTUBE_ID.search(url or "")
    return match.group(1) if match else None


def youtube_link(source_url: str | None, seconds: float | None) -> str | None:
    """A link that opens the YouTube video at a given moment, or None if that is not possible
    (an uploaded file, a non-YouTube source, or a chunk without a timestamp)."""
    video_id = youtube_id(source_url)
    if not video_id or seconds is None:
        return None
    return f"https://www.youtube.com/watch?v={video_id}&t={int(seconds)}s"


def placeholder_title(video_id: str) -> str:
    """The title a video gets when the caller did not give one."""
    return f"YouTube video {video_id}"


def fetch_youtube_title(url: str) -> str | None:
    """Look up a video's real title through YouTube's public oEmbed endpoint (no key).

    Best effort: any failure returns None and the placeholder title stays.
    """
    import httpx

    try:
        response = httpx.get("https://www.youtube.com/oembed",
                             params={"url": url, "format": "json"}, timeout=10)
        response.raise_for_status()
        return (response.json().get("title") or "").strip()[:300] or None
    except Exception as exc:
        log.warning("could not fetch video title", extra={"reason": str(exc)[:200]})
        return None


def fetch_youtube_captions(video_id: str) -> list[Segment]:
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import CouldNotRetrieveTranscript

    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    except CouldNotRetrieveTranscript as exc:
        raise PipelineError(f"No English captions available for YouTube video {video_id}: "
                            f"{type(exc).__name__}") from exc
    except Exception as exc:  # network problems are worth a retry
        raise PipelineError(f"Could not reach YouTube: {exc}", retryable=True) from exc

    segments = []
    for snippet in fetched:
        text = clean_text(snippet.text)
        if text:
            segments.append(Segment(text, snippet.start, snippet.start + snippet.duration))
    return segments


def _seconds(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def parse_transcript_file(filename: str, content: str) -> list[Segment]:
    """Parse .srt / .vtt (timestamped) or .txt (plain text, no timestamps)."""
    if filename.lower().endswith(".txt"):
        return _parse_plain_text(content)

    segments = []
    for block in re.split(r"\r?\n\s*\r?\n", content):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            match = _TIMING.search(line)
            if match:
                text = clean_text(" ".join(lines[i + 1:]))
                if text:
                    parts = match.groups()
                    segments.append(Segment(text, _seconds(*parts[:4]), _seconds(*parts[4:])))
                break
    return segments


def _parse_plain_text(content: str) -> list[Segment]:
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(content))
    return [Segment(s, None, None) for s in sentences if s]


@lru_cache
def _whisper():
    from faster_whisper import WhisperModel  # heavy import, only the worker needs it

    return WhisperModel(settings.whisper_model, device="cpu", compute_type="int8",
                        download_root=settings.model_cache_dir)


def transcribe_audio(path: str) -> list[Segment]:
    """Speech-to-text with timestamps. Whisper decodes audio and video files alike."""
    started = time.perf_counter()
    # vad_filter skips silence, which is both faster and avoids hallucinated text.
    result, info = _whisper().transcribe(path, vad_filter=True)
    segments = []
    for item in result:  # a generator: transcription happens as we iterate
        text = clean_text(item.text)
        if text:
            segments.append(Segment(text, round(item.start, 2), round(item.end, 2)))
    log.info("whisper transcription done", extra={
        "audio_seconds": round(info.duration), "language": info.language,
        "seconds": round(time.perf_counter() - started, 1), "model": settings.whisper_model})
    return segments


def download_audio(url: str) -> str:
    """Download just the audio track of an online video with yt-dlp."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": f"{settings.media_dir}/%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            return downloader.prepare_filename(info)
    except DownloadError as exc:
        # A private or removed video will not appear on a second try; a network error might.
        permanent = any(word in str(exc).lower() for word in PERMANENT_DOWNLOAD_ERRORS)
        raise PipelineError(
            f"Could not download audio from {url}: {exc}", retryable=not permanent) from exc


def load_segments(video: Video) -> tuple[list[Segment], str]:
    """Return (segments, source) where source is "file", "captions" or "whisper"."""
    segments, source = _load(video)
    if not segments:
        raise PipelineError("The transcript is empty.")
    log.info("transcript loaded", extra={"video_id": video.id, "segments": len(segments),
                                         "source": source})
    return segments, source


def _load(video: Video) -> tuple[list[Segment], str]:
    if video.file_path:
        if Path(video.file_path).suffix.lower() in TRANSCRIPT_EXTENSIONS:
            with open(video.file_path, encoding="utf-8", errors="replace") as handle:
                return parse_transcript_file(video.file_path, handle.read()), "file"
        return transcribe_audio(video.file_path), "whisper"

    # The caller chose a method when adding the video: auto, captions or whisper.
    video_id = youtube_id(video.source_url)
    if video_id and video.transcript_method != "whisper":
        try:
            return fetch_youtube_captions(video_id), "captions"
        except PipelineError as exc:
            # YouTube unreachable is worth a retry. "captions only" must not fall back.
            if exc.retryable or video.transcript_method == "captions":
                raise
            log.info("no captions, falling back to whisper", extra={"video_id": video.id})

    audio_path = download_audio(video.source_url)
    try:
        return transcribe_audio(audio_path), "whisper"
    finally:
        # The transcript is cached on the video row, so the audio is no longer needed.
        Path(audio_path).unlink(missing_ok=True)
