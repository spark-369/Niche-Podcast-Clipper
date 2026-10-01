"""Niche Podcast Clipper - FastAPI application entry point.

Starts the API, wires CORS, security headers, rate limiting and the local
Hugging Face model lifecycle. Run with::

    uvicorn main:app --reload --port 8000
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from api.deps import rate_limit
from api.v1.router import api_router
from core.config import settings
from core.logging_config import configure_logging
from core.redis_client import redis_available
from db.init_db import ensure_database
from db.session import engine
from services import storage

APP_VERSION = "1.0.0"

configure_logging()
logger = logging.getLogger(__name__)


def _preload_models() -> None:
    """Load local checkpoints in a daemon thread so startup is not blocked."""
    import threading

    def _worker() -> None:
        from services.ai.model_registry import warmup_models

        status_map = warmup_models("both")
        logger.info("Local model warmup status: %s", status_map)

    thread = threading.Thread(target=_worker, name="model-warmup", daemon=True)
    thread.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown lifecycle."""
    logger.info("Starting %s v%s (%s)", settings.PROJECT_NAME, APP_VERSION, settings.ENVIRONMENT)

    # Bound torch / OpenCV (OpenMP) / NumPy (OpenBLAS) to a shared per-process
    # thread budget before any probe or warmup runs, so a few API workers plus
    # the Celery pool can't oversubscribe every core and hang the machine.
    try:
        from core.threads import apply_thread_limits

        apply_thread_limits()
    except Exception as exc:  # pragma: no cover - must never block startup
        logger.warning("Could not apply thread limits: %s", exc)

    root = storage.ensure_storage()
    logger.info("Local media storage ready at %s", root)

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("PostgreSQL connection established")
    except Exception as exc:  # pragma: no cover - infra dependent
        message = str(exc).strip().splitlines()[-1]
        if "password authentication failed" in message:
            logger.error(
                "PostgreSQL rejected the credentials in DATABASE_URL (%s). Fix with:\n"
                "  sudo -u postgres psql -c \"ALTER USER postgres PASSWORD 'postgres';\"\n"
                "  sudo -u postgres psql -c \"CREATE DATABASE podcast_clipper OWNER postgres;\"\n"
                "or point DATABASE_URL in backend/.env at a role that already exists.",
                settings.DATABASE_URL,
            )
        elif "Connection refused" in message or "could not connect" in message:
            logger.error(
                "PostgreSQL is not reachable at %s. Start it with: "
                "sudo systemctl start postgresql",
                settings.DATABASE_URL,
            )
        else:
            logger.warning("Database unavailable at startup: %s", message)
    else:
        # Create the schema on a fresh database and apply pending Alembic
        # migrations so no manual `alembic upgrade head` step is required.
        if settings.AUTO_MIGRATE_DB:
            try:
                ensure_database()
            except Exception as exc:  # pragma: no cover - infra dependent
                logger.warning("Automatic database bootstrap failed: %s", exc)

    if redis_available():
        logger.info("Redis connection established")
    else:  # pragma: no cover - infra dependent
        logger.warning("Redis unavailable: caching and queuing are degraded")

    # Reclaim storage on API boot too (mirrors the worker-startup cleanup):
    # every clip holds one MP4 + one thumbnail and every episode one source
    # file; anything else the tree accumulated is garbage. Safe to run
    # repeatedly and from multiple workers — deletions are guarded by file age.
    try:
        from services.media_cleanup import cleanup_media_storage

        cleanup_report = cleanup_media_storage()
        logger.info(
            "Media storage cleanup removed %d files / %d directories (%.1f MB reclaimed)",
            cleanup_report["files"],
            cleanup_report["dirs"],
            cleanup_report["bytes"] / (1024 * 1024),
        )
    except Exception as exc:  # pragma: no cover - cleanup must never block startup
        logger.warning("Media storage cleanup failed: %s", exc)

    if settings.ENVIRONMENT == "development" and not settings.TRANSFORMERS_OFFLINE:
        _preload_models()

    yield

    from services.ai.model_registry import release_models

    release_models()
    logger.info("Shutdown complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "Turn long-form podcast episodes into viral vertical clips using locally "
        "hosted Hugging Face models, Redis-backed queues and OpenCV rendering."
    ),
    version=APP_VERSION,
    docs_url="/docs" if settings.ENABLE_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
    lifespan=lifespan,
)


# --- Middleware -------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

if settings.is_production:
    from fastapi.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1"] + [
            origin.split("://")[-1].split(":")[0]
            for origin in settings.FRONTEND_ORIGINS
        ],
    )


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Attach timing, a request id and hardening headers to every response.

    The request id is propagated to log records via the logger ``extra`` context
    so a single request's logs can be correlated end-to-end. It is also echoed
    back to the client in ``X-Request-Id`` so support can be quoted to correlate
    client-side reports with server logs.
    """
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    # Bind the request id to this request's log records. Use a contextvar-free
    # approach: stash it on request.state so the exception handler can reuse it.
    request.state.request_id = request_id
    request.state.started = started

    try:
        response = await call_next(request)
    except Exception:
        # Let the generic handler turn this into JSON; it re-reads request.state.
        raise

    _apply_security_headers(request, response, started)
    response.headers["X-Request-Id"] = request_id
    return response


def _apply_security_headers(request: Request, response, started: float) -> None:
    """Attach timing and security headers shared by normal + error responses."""
    response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-App-Version"] = APP_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if settings.is_production:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Turn any uncaught exception into a safe JSON 500.

    Plain Starlette would return a text/plain 500 with no correlation id and
    (in debug tooling) stack traces. This handler guarantees a stable JSON shape,
    masks internals in production, and tags the error with the same request id
    that every log line for this request already carries.
    """
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    logger.exception(
        "Unhandled error on %s %s (request_id=%s)",
        request.method,
        request.url.path,
        request_id,
        extra={"request_id": request_id, "request_path": request.url.path},
    )
    detail = "Internal server error" if settings.is_production else str(exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": detail,
            "request_id": request_id,
        },
        headers={"X-Request-Id": request_id},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a concise 422 body with a request id (validation failures leak no
    internals, but the default message is verbose and client-unfriendly)."""
    request_id = getattr(request.state, "request_id", None) or uuid4().hex
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Request validation failed", "request_id": request_id},
        headers={"X-Request-Id": request_id},
    )



@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Return 400 instead of 500 for invalid user input bubbling up.

    In production the raw exception text is masked: ValueError messages can
    carry internal context (file paths, ids, library internals) that should not
    be echoed to untrusted clients.
    """
    if settings.is_production:
        logger.warning("ValueError on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid request"},
        )
    logger.warning("ValueError on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)}
    )


# --- Routes -----------------------------------------------------------------
app.include_router(api_router, prefix=settings.API_V1_PREFIX)

# Serve rendered media locally. This mount is a development convenience that
# bypasses JWT authentication, so it is DISABLED in production; the
# authenticated /clips/{id}/download and /thumbnail routes remain the only
# access path there.
if not settings.is_production:
    media_directory = Path(storage.storage_root())
    app.mount(
        "/media",
        StaticFiles(directory=str(media_directory), check_dir=False),
        name="media",
    )
else:
    logger.info("Static /media mount disabled in production (JWT routes only)")


@app.get("/", tags=["Root"], summary="Service banner")
def read_root() -> dict:
    """Landing payload with links to the interactive documentation."""
    return {
        "app": settings.PROJECT_NAME,
        "version": APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "docs": "/docs",
        "redoc": "/redoc",
        "api": settings.API_V1_PREFIX,
        "ai": {
            "asr_model": settings.ASR_MODEL_NAME,
            "highlight_model": settings.HIGHLIGHT_MODEL_NAME,
            "on_premise": True,
        },
        "storage": str(storage.storage_root()),
    }


@app.get("/ping", tags=["Root"], summary="Liveness probe")
def ping() -> dict:
    """Minimal liveness probe that never touches the database."""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/ready", tags=["Root"], summary="Readiness probe")
def ready() -> JSONResponse:
    """Readiness probe that verifies the service's dependencies before traffic.

    Unlike ``/ping`` (liveness — "is the process up?"), this checks database and
    Redis so a container orchestrator / load balancer can stop routing requests
    when a dependency is missing, while still letting the container live long
    enough to be restarted.
    """
    checks = {"database": False, "redis": False}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:  # pragma: no cover - infra dependent
        logger.warning("Readiness: database check failed: %s", exc)

    checks["redis"] = redis_available()

    ready = all(checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ready" if ready else "not_ready", "checks": checks, "version": APP_VERSION},
    )


if __name__ == "__main__":  # pragma: no cover - manual execution helper
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=settings.DEBUG)
