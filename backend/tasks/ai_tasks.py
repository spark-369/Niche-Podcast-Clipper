"""Celery tasks for local ASR transcription and highlight extraction."""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select

from core.celery_app import celery_app
from core.config import settings
from core.redis_client import cache_delete_prefix, cache_get_json, cache_set_json
from db.session import SessionLocal
from models import Clip, Episode, RenderJob, Transcript, TranscriptSegment
from models.enums import ClipStatus, EpisodeStatus, JobStatus
from services import caption_styles, storage
from services.ai import highlights as highlight_service
from services.ai.transcription import transcribe_file

logger = logging.getLogger(__name__)


def _mark_job(
    db,
    job_id: Optional[int],
    status: str,
    progress: Optional[int] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update the RenderJob mirror row that the API exposes to clients."""
    if job_id is None:
        return
    job = db.scalar(select(RenderJob).where(RenderJob.id == job_id))
    if job is None:
        return
    job.status = status
    if progress is not None:
        job.progress = progress
    if message:
        job.message = message
    if error:
        job.error_message = error
    elif status == JobStatus.RUNNING.value:
        # A retry that starts again is no longer failed; drop the stale error
        # so a job that eventually succeeds does not keep an old message.
        job.error_message = None
    if status == JobStatus.RUNNING.value and job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    if status in (JobStatus.SUCCESS.value, JobStatus.FAILURE.value):
        job.finished_at = datetime.now(timezone.utc)
    # Commit here — the same mistake the render task made originally: without
    # a commit the mirror row stays `pending / 0%` for the whole inference and
    # the dashboard progress bar looks frozen ("stuck") until the task ends.
    db.add(job)
    try:
        db.commit()
    except Exception as exc:  # pragma: no cover - transient DB issue
        logger.warning("Could not persist job %s state %s: %s", job_id, status, exc)
        db.rollback()


@celery_app.task(
    bind=True,
    name="tasks.ai.transcribe_episode",
    max_retries=2,
    default_retry_delay=30,
)
def transcribe_episode_task(
    self,
    episode_id: int,
    language: Optional[str] = None,
    word_timestamps: bool = True,
    job_id: Optional[int] = None,
) -> dict[str, Any]:
    """Transcribe an episode with the local Whisper pipeline.

    Persists the transcript and its word-aligned segments, then caches the
    payload in Redis so repeated reads skip the database.
    """
    db = SessionLocal()
    try:
        episode = db.scalar(select(Episode).where(Episode.id == episode_id))
        if episode is None:
            raise ValueError(f"Episode {episode_id} no longer exists")

        episode.status = EpisodeStatus.TRANSCRIBING.value
        episode.error_message = None
        db.add(episode)
        db.commit()
        _mark_job(
            db,
            job_id,
            JobStatus.RUNNING.value,
            progress=5,
            message="Loading audio and running local ASR",
        )

        media_path = storage.absolute_media_path(episode.media_path)
        if not media_path.exists():
            raise FileNotFoundError(f"Media file missing on disk: {media_path}")

        result = transcribe_file(
            media_path,
            language=language or episode.language,
            word_timestamps=word_timestamps,
        )
        _mark_job(
            db,
            job_id,
            JobStatus.RUNNING.value,
            progress=70,
            message="Persisting transcript",
        )

        transcript = db.scalar(
            select(Transcript).where(Transcript.episode_id == episode.id)
        )
        if transcript is None:
            transcript = Transcript(episode_id=episode.id)
        else:
            for segment in list(transcript.segments):
                db.delete(segment)

        transcript.text = result.text
        transcript.language = result.language
        transcript.model_name = result.model_name
        transcript.duration_seconds = result.duration_seconds or episode.duration_seconds
        transcript.word_count = result.word_count
        transcript.raw_payload = {
            "language": result.raw_payload.get("language"),
            "chunk_count": len(result.raw_payload.get("chunks") or []),
        }
        db.add(transcript)
        db.flush()

        for segment in result.segments:
            db.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    index=segment.index,
                    start_time=segment.start,
                    end_time=segment.end,
                    text=segment.text,
                    words=[word.to_dict() for word in segment.words],
                )
            )

        episode.status = EpisodeStatus.TRANSCRIBED.value
        episode.language = result.language
        episode.error_message = None
        if not episode.duration_seconds:
            episode.duration_seconds = result.duration_seconds
        db.add(episode)
        db.commit()

        cache_set_json(
            f"transcript:{episode.id}",
            result.to_dict(),
            ttl=settings.CACHE_TTL_SECONDS * 6,
        )
        cache_delete_prefix(f"highlights:{episode.id}")
        _mark_job(
            db,
            job_id,
            JobStatus.SUCCESS.value,
            progress=100,
            message=f"Transcribed {result.word_count} words",
        )

        logger.info(
            "Episode %s transcribed (%s words, language=%s)",
            episode.id,
            result.word_count,
            result.language,
        )
        return {
            "episode_id": episode.id,
            "word_count": result.word_count,
            "language": result.language,
            "segments": len(result.segments),
        }
    except Exception as exc:
        logger.exception("Transcription task failed for episode %s", episode_id)
        failed = db.scalar(select(Episode).where(Episode.id == episode_id))
        if failed is not None:
            failed.status = EpisodeStatus.FAILED.value
            failed.error_message = str(exc)[:2000]
            db.add(failed)
            db.commit()
        _mark_job(db, job_id, JobStatus.FAILURE.value, error=str(exc)[:2000])

        # "Episode no longer exists" is a permanent, data-condition error — the
        # row is gone (deleted, or was never committed by a different writer).
        # Retrying cannot resurrect it, so every retry just re-raises the same
        # ValueError and floods the queue with duplicate receipts. Do not retry.
        if isinstance(exc, ValueError) and "no longer exists" in str(exc):
            raise
        if isinstance(exc, (FileNotFoundError,)):
            # A missing media file on disk is also permanent (the upload was
            # cleaned up or the path was renamed) — retrying won't restore it.
            raise

        if self.request.retries < 2:
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="tasks.ai.extract_highlights",
    max_retries=1,
    default_retry_delay=20,
)
def extract_highlights_task(
    self,
    episode_id: int,
    clip_count: int = 5,
    min_duration: float = 15.0,
    max_duration: float = 60.0,
    temperature: float = 0.4,
    focus: Optional[str] = None,
    job_id: Optional[int] = None,
    use_cache: bool = True,
    caption_style_payload: Optional[dict[str, Any]] = None,
    framing_payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Extract highlights with the local seq2seq model and persist clips.

    Replaces every clip of the episode (deleting their rendered artifacts) so
    generation is idempotent, and reports progress through the ``render_jobs``
    mirror row for live client polling.
    """
    db = SessionLocal()
    try:
        episode = db.scalar(select(Episode).where(Episode.id == episode_id))
        if episode is None:
            raise ValueError(f"Episode {episode_id} no longer exists")

        transcript = db.scalar(
            select(Transcript).where(Transcript.episode_id == episode.id)
        )
        if transcript is None:
            raise ValueError("Episode must be transcribed before extracting highlights")

        # Same cache key the old synchronous endpoint used: repeated generation
        # with identical options skips the model entirely.
        cache_key = (
            f"highlights:{episode.id}:{clip_count}:{min_duration}:"
            f"{max_duration}:{temperature}:{focus or 'none'}"
        )
        highlights: list[dict[str, Any]] = []
        cached = False
        if use_cache:
            cached_payload = cache_get_json(cache_key)
            if cached_payload:
                highlights = list(cached_payload)
                cached = True

        if not highlights:
            _mark_job(
                db,
                job_id,
                JobStatus.RUNNING.value,
                progress=10,
                message="Scoring transcript with the local highlight model",
            )

            segments = [
                {
                    "start_time": segment.start_time,
                    "end_time": segment.end_time,
                    "text": segment.text,
                    "words": segment.words or [],
                }
                for segment in transcript.segments
            ]
            generated = highlight_service.generate_highlights(
                segments=segments,
                clip_count=clip_count,
                min_duration=min_duration,
                max_duration=max_duration,
                temperature=temperature,
                focus=focus,
            )
            highlights = [item.to_dict() for item in generated]
            if use_cache and highlights:
                cache_set_json(cache_key, highlights)

        _mark_job(
            db,
            job_id,
            JobStatus.RUNNING.value,
            progress=70,
            message=f"Creating {len(highlights)} clips",
        )

        # Replace the previous clips of this episode (and their rendered
        # artifacts) so re-generation never leaves duplicates behind.
        for old in db.scalars(select(Clip).where(Clip.episode_id == episode.id)).all():
            storage.delete_path(
                str(Path("clips") / str(episode.owner_id) / str(old.id))
            )
            db.delete(old)
        db.commit()

        caption_style = (
            caption_style_payload
            if caption_style_payload
            else caption_styles.caption_style_to_dict(None)
        )
        framing = (
            framing_payload if framing_payload else caption_styles.framing_to_dict(None)
        )

        created: list[int] = []
        for item in highlights:
            clip = Clip(
                episode_id=episode.id,
                owner_id=episode.owner_id,
                title=item.get("title") or "Untitled highlight",
                hook=item.get("hook") or "",
                summary=item.get("summary") or "",
                start_time=float(item.get("start") or 0.0),
                end_time=float(item.get("end") or 0.0),
                duration_seconds=round(
                    float(item.get("end") or 0.0) - float(item.get("start") or 0.0), 3
                ),
                transcript_text=item.get("text") or "",
                words=item.get("words") or [],
                virality_score=float(item.get("virality_score") or 0.0),
                hook_score=float(item.get("hook_score") or 0.0),
                emotion_score=float(item.get("emotion_score") or 0.0),
                quotability_score=float(item.get("quotability_score") or 0.0),
                keywords=item.get("keywords") or [],
                caption_style=caption_style,
                framing=framing,
                status=ClipStatus.READY.value,
            )
            db.add(clip)
            db.flush()
            created.append(clip.id)

        db.commit()
        _mark_job(
            db,
            job_id,
            JobStatus.SUCCESS.value,
            progress=100,
            message=f"Created {len(created)} clips" + (" (cached)" if cached else ""),
        )
        logger.info("Highlight task created %d clips for episode %s", len(created), episode.id)
        return {
            "episode_id": episode.id,
            "clip_ids": created,
            "cached": cached,
            "model_name": settings.HIGHLIGHT_MODEL_NAME,
        }
    except Exception as exc:
        logger.exception("Highlight extraction failed for episode %s", episode_id)
        _mark_job(db, job_id, JobStatus.FAILURE.value, error=str(exc)[:2000])
        if self.request.retries < 1:
            raise self.retry(exc=exc)
        raise
    finally:
        db.close()


@celery_app.task(name="tasks.ai.prewarm_models")
def prewarm_models_task(skip_asr: bool = False) -> dict[str, bool]:
    """Optionally preload the local models so the first job is fast."""
    from services.ai.model_registry import warmup_models

    return warmup_models("highlight" if skip_asr else "both")
