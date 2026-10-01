"""Local filesystem storage helpers for uploads, renders and thumbnails."""

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Optional

from core.config import settings

logger = logging.getLogger(__name__)

SUBDIRS = ("episodes", "clips")


def storage_root() -> Path:
    """Return the absolute storage root, creating it when needed."""
    root = Path(settings.STORAGE_DIR).expanduser()
    if not root.is_absolute():
        # Resolve relative to the backend package directory (where main.py lives).
        root = (Path(__file__).resolve().parents[2] / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_storage() -> Path:
    """Create the storage root plus every well-known subdirectory."""
    root = storage_root()
    for name in SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix and suffix not in settings.ALLOWED_MEDIA_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{suffix}'. "
            f"Allowed: {', '.join(settings.ALLOWED_MEDIA_EXTENSIONS)}"
        )
    return suffix or ".mp4"


def build_stored_filename(original_filename: str) -> str:
    """Create a collision free, timestamped filename for local storage."""
    suffix = _safe_suffix(original_filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:12]}{suffix}"


def episode_dir(owner_id: int, episode_id: int | str) -> Path:
    """Directory holding an episode's source media and derived artifacts."""
    path = storage_root() / "episodes" / str(owner_id) / str(episode_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def clip_dir(owner_id: int, clip_id: int | str) -> Path:
    """Directory holding a rendered clip and its thumbnail."""
    path = storage_root() / "clips" / str(owner_id) / str(clip_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_stream(src: BinaryIO, destination: Path) -> int:
    """Stream an uploaded file to disk, returning the number of bytes written."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with destination.open("wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    return size


def relative_media_path(path: Path | str) -> str:
    """Store paths relative to the storage root so the tree stays portable."""
    try:
        return str(Path(path).resolve().relative_to(storage_root()))
    except ValueError:
        return str(path)


def absolute_media_path(relative_path: str) -> Path:
    """Resolve a stored relative path back to an absolute filesystem path."""
    candidate = Path(relative_path)
    if candidate.is_absolute():
        return candidate
    return (storage_root() / candidate).resolve()


def delete_path(path: Optional[str]) -> bool:
    """Remove a stored file (or directory); never raises."""
    if not path:
        return False
    try:
        target = absolute_media_path(path)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
        return True
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("Could not delete %s: %s", path, exc)
        return False


def human_size(num_bytes: int) -> str:
    """Format a byte count for human readable output."""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def sanitize_filename(filename: str, fallback: str = "episode") -> str:
    """Strip directory components and unsafe characters from a filename."""
    name = os.path.basename(filename or "").strip() or fallback
    safe = "".join(ch if ch.isalnum() or ch in " ._-()[]" else "_" for ch in name)
    return safe[:200] or fallback
