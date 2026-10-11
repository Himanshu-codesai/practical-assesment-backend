"""Load the slow things once at start-up instead of during the first request.

Without this, the first search waits about 15 seconds while the embedding models load.
"""
import logging
import threading
import time

from app.llm.client import get_llm
from app.pipeline.embeddings import embed_query, sparse_query

log = logging.getLogger(__name__)


def warm_up() -> None:
    started = time.perf_counter()
    try:
        embed_query("warm up")
        sparse_query("warm up")
        get_llm()
        log.info("warm-up done", extra={"seconds": round(time.perf_counter() - started, 1)})
    except Exception:
        # Not fatal: whatever failed to load here is loaded on first use instead.
        log.exception("warm-up failed")


def warm_up_in_background() -> None:
    """Start-up does not wait for this, so the API answers health checks straight away."""
    threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
