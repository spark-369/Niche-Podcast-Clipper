"""Health and local-AI capability endpoints."""

import logging
import shutil

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.celery_app import celery_app
from core.config import settings
from core.redis_client import redis_available
from db.session import get_db
from schemas.common import HealthResponse
from services import storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Health"])

APP_VERSION = "1.0.0"


def _database_available(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover - connection dependent
        logger.warning("Database health check failed: %s", exc)
        return False


def _disk_usage() -> tuple[int, int] | tuple[None, None]:
    """Return ``(total, free)`` bytes for the volume hosting media storage."""
    try:
        usage = shutil.disk_usage(storage.storage_root())
        return usage.total, usage.free
    except OSError as exc:  # pragma: no cover - unusual filesystems
        logger.warning("Disk usage probe failed: %s", exc)
        return None, None


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """Report API, PostgreSQL, Redis and disk availability."""
    database_ok = _database_available(db)
    redis_ok = redis_available()
    disk_total, disk_free = _disk_usage()
    return HealthResponse(
        status="ok" if (database_ok and redis_ok) else "degraded",
        app_name=settings.PROJECT_NAME,
        environment=settings.ENVIRONMENT,
        version=APP_VERSION,
        database=database_ok,
        redis=redis_ok,
        disk_total_bytes=disk_total,
        disk_free_bytes=disk_free,
    )


@router.get("/health/models", summary="Local model configuration")
def model_health() -> dict:
    """Describe which local Hugging Face checkpoints this instance will load.

    Models load lazily on first use, so this endpoint never blocks.
    """
    return {
        "asr_model": settings.ASR_MODEL_NAME,
        "highlight_model": settings.HIGHLIGHT_MODEL_NAME,
        "device": settings.AI_DEVICE,
        "chunk_length_s": settings.ASR_CHUNK_LENGTH_S,
        "hf_home": settings.HF_HOME,
        "offline_mode": settings.TRANSFORMERS_OFFLINE,
        "on_premise": True,
        "notes": (
            "Models are downloaded once into HF_HOME and then executed locally on CPU. "
            "No transcript data leaves this machine."
        ),
    }


@router.get("/health/worker", summary="Celery worker availability")
def worker_health() -> dict:
    """Check whether at least one Celery worker responds to a broker ping."""
    try:
        replies = celery_app.control.ping(timeout=1.5) or []
        return {"workers_online": len(replies), "healthy": len(replies) > 0}
    except Exception as exc:  # pragma: no cover - broker dependent
        return {"workers_online": 0, "healthy": False, "error": str(exc)[:300]}
