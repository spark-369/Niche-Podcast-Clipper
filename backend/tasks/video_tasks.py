"""Celery task that renders vertical clips with OpenCV."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from core.celery_app import celery_app
from core.redis_client import cache_delete_prefix
from db.session import SessionLocal
from models import Clip, Episode, RenderJob
from models.enums import ClipStatus, JobStatus
from services import storage
from services.video import captions as caption_utils
from services.video import renderer

logger = logging.getLogger(__name__)


def _update_progress(db, clip: Clip, job: Optional[RenderJob], value: int) -> None:
    """Persist render progress on both the clip and its job mirror.

    The previous version only marked the objects dirty, so the polling clients
    (``/clips/{id}/status`` and ``/jobs/{id}``) kept reading ``progress=1`` until
    the render finished. Committing here — callers throttle how often this runs —
    makes the progress bar move. A transient DB error must not kill the render,
    so failures roll back instead of propagating.
    """
    value = max(1, min(int(value), 99))
    clip.progress = value
    db.add(clip)
    if job is not None:
        job.progress = value
        job.status = JobStatus.RUNNING.value
        db.add(job)
    try:
        db.commit()
    except Exception as exc:  # pragma: no cover - transient DB issue
        logger.warning("Could not persist render progress for clip %s: %s", clip.id, exc)
        db.rollback()


def _progress_reporter(db, clip: Clip, job: Optional[RenderJob]):
    """Build a throttled progress callback that commits at most every 2%.

    The renderer fires its callback every ~15 frames — hundreds of times for a
    long clip — so committing on each call would hammer the database, while
    never committing (the old behaviour) left the UI stuck at 1%.
    """
    last = {"value": -1}

    def report(value: int) -> None:
        value = max(1, min(int(value), 99))
        if value - last["value"] < 2:
            return
        last["value"] = value
        _update_progress(db, clip, job, value)

    return report


@celery_app.task(
    bind=True,
    name="tasks.video.render_clip",
    max_retries=1,
    default_retry_delay=20,
)
def render_clip_task(self, clip_id: int, job_id: Optional[int] = None) -> dict[str, Any]:
    """Burn captions and auto-crop a clip into a vertical 9:16 video.

    Handles both video sources (OpenCV frame loop) and audio-only sources
    (animated gradient canvas with burned captions).
    """
    db = SessionLocal()
    try:
        clip = db.scalar(select(Clip).where(Clip.id == clip_id))
        if clip is None:
            raise ValueError(f"Clip {clip_id} no longer exists")

        episode = db.scalar(select(Episode).where(Episode.id == clip.episode_id))
        if episode is None:
            raise ValueError(f"Episode {clip.episode_id} no longer exists")

        source_path = storage.absolute_media_path(episode.media_path)
        if not source_path.exists():
            raise FileNotFoundError(f"Source media missing on disk: {source_path}")

        job = db.scalar(select(RenderJob).where(RenderJob.id == job_id)) if job_id else None
        if job is not None:
            job.message = "Rendering with OpenCV"
            job.started_at = job.started_at or datetime.now(timezone.utc)
            db.add(job)
            db.commit()

        clip.status = ClipStatus.RENDERING.value
        clip.error_message = None
        clip.progress = 1
        db.add(clip)
        db.commit()

        output_directory = storage.clip_dir(clip.owner_id, clip.id)
        output_path = renderer.resolve_render_path(output_directory, clip.id)
        # The editor overlays draft captions client-side on the episode's source
        # media, so a render produces exactly ONE video per clip (this file) and
        # one thumbnail — no caption-free companion render anymore.
        caption_style = clip.caption_style or {}
        framing = clip.framing or {}

        on_progress = _progress_reporter(db, clip, job)

        if episode.has_video_stream:
            result = renderer.render_clip(
                source_path=source_path,
                output_path=output_path,
                start_time=clip.start_time,
                end_time=clip.end_time,
                caption_style=caption_style,
                framing=framing,
                words=clip.words or [],
                transcript_text=clip.transcript_text or "",
                title=clip.title or "",
                progress_callback=on_progress,
            )
        else:
            groups = caption_utils.group_caption_lines(
                caption_utils.words_from_payload(
                    clip.words or [],
                    clip.start_time,
                    clip.end_time,
                    clip.transcript_text or "",
                ),
                int(caption_style.get("words_per_line", 3)),
                int(caption_style.get("max_lines", 2)),
            )
            result = renderer.render_audio_only(
                output_path=output_path,
                width=int(framing.get("output_width", 1080)),
                height=int(framing.get("output_height", 1920)),
                fps=float(framing.get("fps", 30) or 30.0),
                caption_style=caption_style,
                groups=groups,
                duration=max(clip.duration_seconds, clip.end_time - clip.start_time),
                title=clip.title or "",
                progress_callback=on_progress,
            )

        clip.output_path = storage.relative_media_path(result.output_path)
        clip.output_size_bytes = result.size_bytes
        if result.thumbnail_path:
            clip.thumbnail_path = storage.relative_media_path(result.thumbnail_path)
        clip.progress = 100
        clip.status = ClipStatus.RENDERED.value
        clip.error_message = (
            "; ".join(result.warnings) if result.warnings else None
        )
        db.add(clip)
        db.commit()

        if job is not None:
            job.status = JobStatus.SUCCESS.value
            job.progress = 100
            job.message = (
                f"Rendered {result.frame_count} frames at "
                f"{result.width}x{result.height}"
            )
            job.payload = result.to_dict()
            job.finished_at = datetime.now(timezone.utc)
            db.add(job)
            db.commit()

        cache_delete_prefix(f"clip:{clip.id}")
        logger.info(
            "Clip %s rendered to %s (%.1f MB)",
            clip.id,
            clip.output_path,
            result.size_bytes / (1024 * 1024),
        )
        return result.to_dict()
    except Exception as exc:
        logger.exception("Render task failed for clip %s", clip_id)
        clip = db.scalar(select(Clip).where(Clip.id == clip_id))
        if clip is not None:
            clip.status = ClipStatus.FAILED.value
            clip.error_message = str(exc)[:2000]
            db.add(clip)
            db.commit()
        if job_id:
            job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
            if job is not None:
                job.status = JobStatus.FAILURE.value
                job.error_message = str(exc)[:2000]
                job.finished_at = datetime.now(timezone.utc)
                db.add(job)
                db.commit()
        if self.request.retries < 1:
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()

