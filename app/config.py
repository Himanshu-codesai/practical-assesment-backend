"""All runtime configuration in one place, read from environment variables / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://lms:lms@db:5432/lms"
    redis_url: str = "redis://redis:6379/0"
    qdrant_url: str = "http://qdrant:6333"
    qdrant_collection: str = "video_chunks"
    log_level: str = "INFO"

    # LLM server: anything that accepts POST /v1/messages. The default is the `llm`
    # service in docker-compose.yml.
    llm_base_url: str = "http://llm:8787"
    llm_api_key: str = ""  # sent as the x-api-key header when set
    # Backup server, asked when the primary fails. Empty means no fallback.
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""  # defaults to llm_model
    llm_model: str = "claude-opus-5-5"
    llm_max_tokens: int = 16000
    llm_timeout_seconds: float = 600.0

    # Embeddings run locally (ONNX via fastembed), so no API key is needed.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    sparse_model: str = "Qdrant/bm25"
    model_cache_dir: str = "/models"

    # Transcription
    whisper_model: str = "base"  # tiny | base | small | medium | large-v3
    media_dir: str = "/data/media"
    max_upload_mb: int = 500

    # Pipeline
    chunk_target_words: int = 180
    chunk_overlap_segments: int = 2

    # Retrieval
    retrieval_top_k: int = 5
    retrieval_mode: str = "hybrid"  # hybrid | dense | keyword
    # Each retriever fetches top_k * this many candidates before the results are fused.
    hybrid_candidate_multiplier: int = 4

    # Background jobs
    job_max_retries: int = 3
    job_retry_base_seconds: int = 10

    # Assessment
    completion_threshold: float = 80.0  # percent watched that triggers an assessment
    questions_per_assessment: int = 8
    max_context_chunks: int = 14


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
