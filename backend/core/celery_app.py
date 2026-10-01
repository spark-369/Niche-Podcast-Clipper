"""Celery application used for background transcription and rendering jobs."""

import logging
import os

from celery import Celery
from celery.signals import worker_process_init, worker_ready

from core.config import settings

logger = logging.getLogger(__name__)


def _default_concurrency() -> int:
    """A sane worker prefork pool size for CPU-bound local inference.

    Celery's prefork default is ``cpu_count()`` — on this box that is 16
    processes, each loading its own Whisper/Flan-T5 copy (gigabytes of RAM) and
    each running the numerical thread budget, i.e. 16 × 8 threads contending
    for a handful of cores. That oversubscription is what makes the machine
    appear to hang. Default to a small pool; operators can still override with
    ``CELERY_CONCURRENCY`` or an explicit ``--concurrency N``.
    """
    env = os.getenv("CELERY_CONCURRENCY")
    if env:
        try:
            value = int(env)
            if value > 0:
                return value
        except ValueError:
            logger.warning("Ignoring invalid CELERY_CONCURRENCY=%r", env)
    return settings.CELERY_CONCURRENCY

celery_app = Celery(
    "niche_podcast_clipper",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["tasks.ai_tasks", "tasks.video_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=60 * 60 * 2,          # hard limit: 2 hours
    task_soft_time_limit=60 * 60 * 110,   # soft limit: 110 minutes
    worker_max_tasks_per_child=50,        # was 8: every recycle reloads the models (~3s) in the fresh child
    worker_prefetch_multiplier=1,
    # Guard against Celery's cpu_count() prefork default (16 processes here):
    # each child loads its own Whisper/Flan-T5 copy and multiplies the thread
    # budget, which oversubscribes and hangs the machine.
    worker_concurrency=_default_concurrency(),
    result_expires=60 * 60 * 24 * 7,
    broker_connection_retry_on_startup=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_default_queue="clipper",
    # acks_late + Redis: a task left unacknowledged for longer than the
    # visibility timeout is *redelivered to another worker* — i.e. a
    # transcription running past 1h (the default) silently started twice and
    # everything ran at half speed. Must exceed the longest task time limit.
    broker_transport_options={"visibility_timeout": 60 * 60 * 6},
    result_backend_transport_options={"visibility_timeout": 60 * 60 * 6},
)


@worker_process_init.connect
def _tune_torch_for_worker(**_kwargs) -> None:
    """Tune torch inside each forked worker child (CPU inference helpers).

    Bound torch, OpenCV (OpenMP) and NumPy (OpenBLAS/MKL) to a shared per-process
    thread budget so several concurrent workers do not each request every CPU
    core and oversubscribe the scheduler (which presents as a 100%-CPU hang).
    ``TORCH_NUM_THREADS``/``OMP_NUM_THREADS`` or the ``AI_NUM_THREADS`` setting
    still take precedence over the auto budget.
    """
    try:
        import warnings

        import torch

        from core.threads import apply_thread_limits

        apply_thread_limits()

        # Speech models hit denormal floats on CPU; flushing them is free speed.
        torch.set_flush_denormal(True)
        # Flush-to-zero makes numpy warn once per process that the smallest
        # subnormal is zero — that is exactly the intended effect, not a
        # problem, so silence the notice.
        warnings.filterwarnings(
            "ignore", message="The value of the smallest subnormal.*"
        )
    except Exception:  # pragma: no cover - torch optional at runtime
        pass


@worker_ready.connect
def _cleanup_media_on_worker_start(**_kwargs) -> None:
    """Reclaim storage once per worker start (safe to run repeatedly).

    Every clip is supposed to hold exactly one rendered MP4 plus its thumbnail
    and every episode one source file; this deletes superseded renders, transcode
    and mux temporaries, abandoned uploads and legacy companion previews.
    """
    try:
        from services.media_cleanup import cleanup_media_storage

        cleanup_media_storage()
    except Exception as exc:  # pragma: no cover - cleanup must never block startup
        logger.warning("Media cleanup failed: %s", exc)


@celery_app.task(name="core.celery_app.ping")
def ping() -> str:
    """Trivial task used to verify a worker is alive."""
    return "pong"
