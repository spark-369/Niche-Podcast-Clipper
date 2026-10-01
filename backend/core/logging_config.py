"""Centralised logging configuration shared by the API and Celery workers."""

import json
import logging
import sys
from datetime import datetime, timezone

from core.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record for machine-readable log aggregation.

    ``extra`` fields pushed by the request middleware (``request_id``,
    ``request_path``, ``method``) are surfaced as top-level keys so tools like
    Loki/Datadog/ELK can group traces by request id. The human-readable message
    lives under ``message``; exception info is captured as structured fields.
    """

    _RESERVED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    ) | {"message", "asctime", "taskName", "request_id"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Surface non-standard extras (request_id, path, etc.) verbatim.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc_text"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Configure root logging once, with a level derived from settings."""
    level = logging.DEBUG if settings.DEBUG else logging.INFO
    root = logging.getLogger()

    # Preserve any request-id extra fields when formatting.
    handler = logging.StreamHandler(sys.stdout)
    if settings.LOG_FORMAT_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    # ``%(message)s`` needs the record fully populated; attach a filter that
    # defaults missing request_id so the key is always present in JSON output.
    handler.addFilter(_RequestIdDefaultingFilter())
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = True

    # Silence chatty third party loggers.
    for noisy in ("httpx", "urllib3", "transformers.tokenization_utils_base"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _RequestIdDefaultingFilter(logging.Filter):
    """Ensure every record carries a ``request_id`` (empty outside requests)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = ""
        return True


logger = logging.getLogger("niche_clipper")
