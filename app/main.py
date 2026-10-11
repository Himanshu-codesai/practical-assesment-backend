import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from app.api import assessments, system, videos
from app.config import settings
from app.db import init_db
from app.errors import AppError
from app.logging_config import configure_logging, request_id_var
from app.warmup import warm_up_in_background
from app.retrieval.store import ensure_collection

configure_logging(settings.log_level)
log = logging.getLogger("app")

DESCRIPTION = """
Backend of an AI-powered LMS: API + Celery worker, PostgreSQL, Redis, and Qdrant
hybrid retrieval (dense + BM25).

Typical flow:
1. `POST /learners` and `POST /videos` (poll `GET /jobs/{id}` until `succeeded`)
2. `POST /videos/{id}/search` or `/ask` to query the video's content
3. `POST /videos/{id}/progress` — crossing the completion threshold generates an assessment
4. `GET /assessments/{id}` → `POST /assessments/{id}/submit`
5. `GET /assessments/{id}/result` and `GET /assessments/{id}/report`
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_collection()
    warm_up_in_background()
    log.info("startup complete", extra={"llm_model": settings.llm_model})
    yield


app = FastAPI(title="AI LMS Backend", version="1.0.0",
              description=DESCRIPTION, lifespan=lifespan)
app.include_router(videos.router)
app.include_router(assessments.router)
app.include_router(system.router)

# A single-file test console (no build step). It is a client of the REST API above,
# exactly like scripts/demo.py; the backend does not depend on it.
UI_FILE = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui")


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#1f5fa8"/>'
    '<path d="M12 9.5v13l11-6.5z" fill="#fff"/></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/ui", include_in_schema=False)
def test_console():
    return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Give every request an ID, put it on all log lines, and write one access log entry."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    if request.url.path != "/health":
        log.info("request", extra={
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": response.status_code,
            "ms": round((time.perf_counter() - started) * 1000),
        })
    return response


def error_body(request: Request, code: str, message: str) -> dict:
    request_id = getattr(request.state, "request_id", None)
    return {"error": {"code": code, "message": message, "request_id": request_id}}


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError):
    log.warning("handled error", extra={"code": exc.code, "detail": exc.message})
    return JSONResponse(status_code=exc.status_code,
                        content=error_body(request, exc.code, exc.message))


@app.exception_handler(RequestValidationError)
async def handle_invalid_input(request: Request, exc: RequestValidationError):
    """FastAPI's own input checks, reported in the same shape as every other error."""
    problems = "; ".join(
        f"{'.'.join(str(part) for part in e['loc'][1:]) or 'body'}: {e['msg']}"
        for e in exc.errors())
    return JSONResponse(status_code=422, content=error_body(request, "bad_request", problems))


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500,
                        content=error_body(request, "internal_error",
                                           "An unexpected error occurred."))
