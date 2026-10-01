"""Media storage garbage collection.

The storage tree is meant to stay minimal:

    episodes/<owner>/<episode>/<file>   one source file per episode
    episodes/<owner>/incoming/          upload staging (transient)
    clips/<owner>/<clip>/<file>         one rendered MP4 + one thumbnail
    tmp/                                scratch space (transient)

Everything else is garbage and removed here: superseded renders, transcode and
mux temporaries, abandoned uploads, the legacy caption-free companion renders
and — the biggest leak — whole directories whose database rows are already
gone (deleting an episode cascades its clip rows, which previously stranded
their rendered files on disk forever).
"""

import logging
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from db.session import SessionLocal
from models import Clip, Episode
from services import storage

logger = logging.getLogger(__name__)

# Temporaries the render pipeline leaves behind when it crashes: the
# mp4v->H.264 transcode target, the audio mux working file and the extracted
# audio segment (normally deleted right after the mux).
_TEMP_SUFFIXES = (".h264.mp4", ".mux.mp4", ".m4a")
# The legacy caption-free companion render (pre client-side-overlay editor).
_LEGACY_PREVIEW_SUFFIX = "_preview.mp4"
# Upload staging gets a longer leash: a large upload over a slow link can
# legitimately take hours before it is moved into the episode directory.
_INCOMING_MAX_AGE_SECONDS = 24 * 3600.0


def _age_seconds(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:  # pragma: no cover - raced with the writer
        return 0.0


def _rmdir_if_empty(directory: Path) -> bool:
    """Remove a directory when it holds nothing; True when it was removed."""
    try:
        next(directory.iterdir())
    except StopIteration:
        try:
            directory.rmdir()
            return True
        except OSError:  # pragma: no cover - vanished or unreadable
            return False
    except OSError:  # pragma: no cover - vanished or unreadable
        return False
    return False


def _rmtree_if_old(directory: Path, min_age_seconds: float) -> bool:
    """Remove a whole directory tree when every file in it is old enough."""
    try:
        for path in directory.rglob("*"):
            if path.is_file() and _age_seconds(path) < min_age_seconds:
                return False
    except OSError:  # pragma: no cover - vanished or unreadable
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()


def cleanup_media_storage(min_age_seconds: float = 3600.0) -> dict[str, int]:
    """Delete stored files and directories that no database row references.

    Args:
        min_age_seconds: Files younger than this are left alone — a render or
            extraction being written right now has no row reference yet, and
            the age guard keeps an in-flight job from being truncated.

    Returns:
        Counters for logging: removed files/directories and bytes reclaimed.
    """
    removed = {"files": 0, "dirs": 0, "bytes": 0}
    root = storage.storage_root()
    db = SessionLocal()
    try:
        expected_clip_dirs: dict[Path, set[Path]] = {}
        for clip in db.scalars(select(Clip)).all():
            directory = storage.clip_dir(clip.owner_id, clip.id)
            expected_clip_dirs[directory] = {
                storage.absolute_media_path(rel)
                for rel in (clip.output_path, clip.thumbnail_path)
                if rel
            }

        expected_episode_dirs: dict[Path, set[Path]] = {}
        for episode in db.scalars(select(Episode)).all():
            directory = storage.episode_dir(episode.owner_id, episode.id)
            expected_episode_dirs[directory] = (
                {storage.absolute_media_path(episode.media_path)}
                if episode.media_path
                else set()
            )

        def _unlink(path: Path) -> None:
            try:
                size = path.stat().st_size
                path.unlink()
                removed["files"] += 1
                removed["bytes"] += size
            except OSError:  # pragma: no cover - raced with another process
                pass

        def _sweep_files(directory: Path, keep: set[Path]) -> None:
            if not directory.exists():
                return
            for path in sorted(directory.glob("*")):
                if not path.is_file() or path in keep:
                    continue
                if _age_seconds(path) < min_age_seconds:
                    continue
                _unlink(path)

        # Clips: one MP4 + one thumbnail per clip row; whole directories
        # without a clip row (a deleted episode cascades the clip rows) are
        # removed entirely.
        clips_root = root / "clips"
        if clips_root.is_dir():
            for owner_dir in clips_root.iterdir():
                if not owner_dir.is_dir():
                    continue
                for directory in owner_dir.iterdir():
                    if not directory.is_dir():
                        continue
                    if directory in expected_clip_dirs:
                        _sweep_files(directory, expected_clip_dirs[directory])
                    elif _rmtree_if_old(directory, min_age_seconds):
                        removed["dirs"] += 1
                        continue
                    if _rmdir_if_empty(directory):
                        removed["dirs"] += 1
                if _rmdir_if_empty(owner_dir):
                    removed["dirs"] += 1

        # Episodes: one source file per episode row; orphan directories and
        # stale incoming staging are pruned.
        episodes_root = root / "episodes"
        if episodes_root.is_dir():
            for owner_dir in episodes_root.iterdir():
                if not owner_dir.is_dir():
                    continue
                for entry in owner_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    if entry.name == "incoming":
                        # A dead upload only ever lives here (it is moved into
                        # the episode directory on success).
                        for path in sorted(entry.glob("*")):
                            if not path.is_file():
                                continue
                            if _age_seconds(path) >= _INCOMING_MAX_AGE_SECONDS:
                                _unlink(path)
                        if _rmdir_if_empty(entry):
                            removed["dirs"] += 1
                    elif entry in expected_episode_dirs:
                        _sweep_files(entry, expected_episode_dirs[entry])
                        if _rmdir_if_empty(entry):
                            removed["dirs"] += 1
                    elif _rmtree_if_old(entry, min_age_seconds):
                        removed["dirs"] += 1
                if _rmdir_if_empty(owner_dir):
                    removed["dirs"] += 1

        # Scratch space: nothing referenced ever lives in these trees. They are
        # no longer created by ``ensure_storage`` either, so once emptied they
        # are removed entirely.
        for name in ("tmp", "audio", "thumbnails"):
            scratch = root / name
            if not scratch.is_dir():
                continue
            for path in scratch.iterdir():
                if path.is_file():
                    if _age_seconds(path) >= min_age_seconds:
                        _unlink(path)
                elif path.is_dir() and _rmtree_if_old(path, min_age_seconds):
                    removed["dirs"] += 1
            if _rmdir_if_empty(scratch):
                removed["dirs"] += 1
    finally:
        db.close()

    if removed["files"] or removed["dirs"]:
        logger.info(
            "Media cleanup removed %d files and %d directories (%.1f MB reclaimed)",
            removed["files"],
            removed["dirs"],
            removed["bytes"] / (1024 * 1024),
        )
    else:
        logger.debug("Media cleanup found nothing to remove")
    try:
        remaining_files = 0
        remaining_bytes = 0
        for path in root.rglob("*"):
            if path.is_file():
                remaining_files += 1
                remaining_bytes += path.stat().st_size
        logger.info(
            "Media storage now holds %d files (%s)",
            remaining_files,
            storage.human_size(remaining_bytes),
        )
    except OSError:  # pragma: no cover - reporting only
        pass
    return removed