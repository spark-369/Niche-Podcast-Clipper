"""Celery worker entry point.

Run with::

    celery -A worker.celery_app worker --loglevel=info

The module re-exports `celery_app` from :mod:`core.celery_app` and imports the
task modules so the worker registers every task, including the eager-mode
``beat`` schedule used to preload local models.
"""

import logging

from core.celery_app import celery_app
from core.logging_config import configure_logging
from tasks import ai_tasks, video_tasks  # noqa: F401  (registers the tasks)

configure_logging()
logger = logging.getLogger(__name__)

celery_app.conf.update(
    beat_schedule={
        "prewarm-local-models": {
            "task": "tasks.ai.prewarm_models",
            "schedule": 60 * 60 * 6,  # every 6 hours
            "kwargs": {"skip_asr": False},
        },
    },
)

logger.debug("Celery worker module loaded with tasks: %s", sorted(celery_app.tasks))

__all__ = ["celery_app"]
