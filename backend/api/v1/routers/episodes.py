"""Episode routes: upload, list, detail, transcription and transcript access."""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from api.deps import get_current_user, rate_limit, _load_user, decode_token, ACCESS_TOKEN_TYPE, CREDENTIALS_EXCEPTION
from core.config import settings
from core.redis_client import cache_delete_prefix
from db.session import get_db
from models import Clip, Episode, RenderJob, Transcript, User
from models.enums import EpisodeStatus, JobStatus, JobType
from schemas.episode import (
    EpisodeRead,
    EpisodeUpdate,
    EpisodeUploadResponse,
    TranscriptRead,
    TranscribeRequest,
    TranscribeResponse,
)
from services import storage
from services.media_probe import probe_media

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/episodes", tags=["Episodes"])


def _episode_read(episode: Episode, clip_count: int) -> EpisodeRead:
    """Build an ``EpisodeRead`` from an already-resolved clip count."""
    payload = EpisodeRead.model_validate(episode)
    payload.has_transcript = episode.transcript is not None
    payload.clip_count = int(clip_count)
    return payload


def _to_episode_read(db: Session, episode: Episode) -> EpisodeRead:
    """Attach derived counters (transcript presence, clip count) to a schema."""
    clip_count = db.scalar(
        select(func.count(Clip.id)).where(Clip.episode_id == episode.id)
    ) or 0
    return _episode_read(episode, int(clip_count))


def _clip_counts(db: Session, episode_ids: Sequence[int]) -> dict[int, int]:
    """Resolve clip counts for many episodes in a single grouped query.

    ``list_episodes`` used to call ``_to_episode_read`` per row, issuing one
    ``COUNT(*)`` per episode (an N+1 that made the dashboard slow as the
    library grew). One ``GROUP BY`` replaces the whole loop.
    """
    ids = [int(episode_id) for episode_id in episode_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(Clip.episode_id, func.count(Clip.id))
        .where(Clip.episode_id.in_(ids))
        .group_by(Clip.episode_id)
    ).all()
    return {int(episode_id): int(count) for episode_id, count in rows}


def get_episode_or_404(db: Session, episode_id: int, user: User) -> Episode:
    """Fetch an episode owned by the current user or raise 404."""
    episode = db.scalar(
        select(Episode)
        .options(selectinload(Episode.transcript))
        .where(Episode.id == episode_id, Episode.owner_id == user.id)
    )
    if episode is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found"
        )
    return episode


@router.post(
    "/upload",
    response_model=EpisodeUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an audio or video episode to local storage",
    dependencies=[Depends(rate_limit)],
)
def upload_episode(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="MP3, MP4, WAV, M4A, MOV, MKV or WebM"),
    title: Optional[str] = Form(default=None),
    description: Optional[str] = Form(default=None),
    auto_transcribe: bool = Form(default=False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EpisodeUploadResponse:
    """Persist an uploaded media file into ``./media_storage`` and probe it.

    Declared ``def`` (not ``async def``) on purpose: streaming a multi-gigabyte
    upload to disk plus probing it with OpenCV are blocking operations, so
    FastAPI runs this in its threadpool instead of stalling the event loop.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A filename is required"
        )

    try:
        stored_filename = storage.build_stored_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        )

    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    directory = storage.episode_dir(current_user.id, "incoming")
    destination = directory / stored_filename

    size = 0
    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit",
                    )
                buffer.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not store the upload: {exc}",
        ) from exc

    episode = Episode(
        owner_id=current_user.id,
        title=(title or Path(file.filename).stem or "Untitled episode")[:512],
        description=description,
        original_filename=storage.sanitize_filename(file.filename),
        stored_filename=stored_filename,
        media_path=storage.relative_media_path(destination),
        mime_type=file.content_type,
        file_size_bytes=size,
        status=EpisodeStatus.UPLOADED.value,
    )
    db.add(episode)
    db.commit()
    db.refresh(episode)

    # Move the file into the final per-episode directory now that we have an id.
    final_directory = storage.episode_dir(current_user.id, episode.id)
    final_path = final_directory / stored_filename
    try:
        destination.replace(final_path)
        episode.media_path = storage.relative_media_path(final_path)
    except OSError as exc:  # pragma: no cover - filesystem race
        logger.warning("Could not relocate upload: %s", exc)

    try:
        probe = probe_media(final_path)
        episode.duration_seconds = probe.duration_seconds
        episode.width = probe.width
        episode.height = probe.height
        episode.fps = probe.fps
        episode.has_video_stream = probe.has_video_stream
        episode.media_type = probe.media_type
    except Exception as exc:  # pragma: no cover - codec dependent
        logger.warning("Media probe failed for episode %s: %s", episode.id, exc)
        episode.error_message = "Media probe failed; metadata may be incomplete"

    db.add(episode)
    db.commit()
    db.refresh(episode)

    if auto_transcribe:
        from tasks.ai_tasks import transcribe_episode_task

        # Mark queued immediately: the dashboard guard and the status badge
        # must reflect the in-flight transcription, otherwise a manual
        # "Transcribe" click right after upload dispatches a *second* task for
        # the same file. The task moves it to TRANSCRIBING when picked up.
        episode.status = EpisodeStatus.QUEUED.value
        # Mirror row so the client can poll live progress after upload.
        job = RenderJob(
            episode_id=episode.id,
            job_type=JobType.TRANSCRIPTION.value,
            status=JobStatus.PENDING.value,
            progress=0,
            message="Queued for local ASR",
        )
        db.add(job)
        db.commit()

        background_tasks.add_task(
            transcribe_episode_task.delay, episode.id, None, True, job.id
        )

    # Without this the route returned ``None``, which FastAPI rejected against
    # the declared ``response_model`` — every upload answered 500.
    return EpisodeUploadResponse(
        episode=_to_episode_read(db, episode),
        message=(
            "Episode uploaded; transcription queued on the local ASR worker"
            if auto_transcribe
            else "Episode uploaded successfully"
        ),
    )


@router.get(
    "",
    response_model=list[EpisodeRead],
    summary="List the current user's episodes",
)
def list_episodes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: Optional[EpisodeStatus] = Query(default=None, alias="status"),
) -> list[EpisodeRead]:
    """Return a page of episodes ordered newest first."""
    query = (
        select(Episode)
        .options(selectinload(Episode.transcript))
        .where(Episode.owner_id == current_user.id)
        .order_by(Episode.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if status_filter is not None:
        query = query.where(Episode.status == status_filter.value)

    episodes = list(db.scalars(query).all())
    counts = _clip_counts(db, [episode.id for episode in episodes])
    return [_episode_read(episode, counts.get(episode.id, 0)) for episode in episodes]


@router.get("/{episode_id}", response_model=EpisodeRead, summary="Episode detail")
def read_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EpisodeRead:
    """Return a single episode with derived counters."""
    episode = get_episode_or_404(db, episode_id, current_user)
    return _to_episode_read(db, episode)


@router.patch("/{episode_id}", response_model=EpisodeRead, summary="Update an episode")
def update_episode(
    episode_id: int,
    payload: EpisodeUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> EpisodeRead:
    """Update the title/description of an episode."""
    episode = get_episode_or_404(db, episode_id, current_user)
    if payload.title is not None:
        episode.title = payload.title
    if payload.description is not None:
        episode.description = payload.description
    db.add(episode)
    db.commit()
    db.refresh(episode)
    return _to_episode_read(db, episode)


@router.delete(
    "/{episode_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an episode"
)
def delete_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete an episode and every derived artifact from local storage."""
    episode = get_episode_or_404(db, episode_id, current_user)
    storage.delete_path(str(Path("episodes") / str(current_user.id) / str(episode.id)))
    db.delete(episode)
    db.commit()
    cache_delete_prefix(f"transcript:{episode_id}")
    cache_delete_prefix(f"highlights:{episode_id}")


@router.post(
    "/{episode_id}/transcribe",
    response_model=TranscribeResponse,
    summary="Trigger local Hugging Face ASR on an episode",
    dependencies=[Depends(rate_limit)],
)
def transcribe_episode(
    episode_id: int,
    payload: TranscribeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TranscribeResponse:
    """Queue (or return a cached) transcription produced by the local ASR model."""
    episode = get_episode_or_404(db, episode_id, current_user)

    if episode.transcript is not None and not payload.force:
        return TranscribeResponse(
            episode_id=episode.id,
            status=EpisodeStatus(episode.status),
            cached=True,
            message="Transcript already exists; pass force=true to re-run",
        )

    # Guard against double submissions (double clicks, request retries): a
    # transcription that is queued or already running must not be enqueued
    # again, otherwise two workers transcribe the same file in parallel. The
    # check looks for an *active* job mirror row instead of trusting the
    # episode status alone, so a stale QUEUED/TRANSCRIBING status left behind
    # by a crashed worker cannot lock the episode out of transcription forever.
    if not payload.force and episode.status in (
        EpisodeStatus.QUEUED,
        EpisodeStatus.TRANSCRIBING,
    ):
        stale_before = datetime.now(timezone.utc) - timedelta(hours=6)
        active_job = db.scalar(
            select(RenderJob).where(
                RenderJob.episode_id == episode.id,
                RenderJob.job_type == JobType.TRANSCRIPTION.value,
                RenderJob.status.in_(
                    [JobStatus.PENDING.value, JobStatus.RUNNING.value]
                ),
                RenderJob.created_at > stale_before,
            )
        )
        if active_job is not None:
            return TranscribeResponse(
                episode_id=episode.id,
                job_id=active_job.id,
                status=EpisodeStatus(episode.status),
                cached=True,
                message="Transcription is already queued or running",
            )

    from tasks.ai_tasks import transcribe_episode_task

    episode.status = EpisodeStatus.QUEUED.value
    # Mirror row so the dashboard can poll live progress (percent + stage).
    job = RenderJob(
        episode_id=episode.id,
        job_type=JobType.TRANSCRIPTION.value,
        status=JobStatus.PENDING.value,
        progress=0,
        message="Queued for local ASR",
    )
    db.add(episode)
    db.add(job)
    db.commit()

    task = transcribe_episode_task.delay(
        episode.id, payload.language, payload.word_timestamps, job.id
    )
    job.task_id = task.id
    db.add(job)
    db.commit()

    return TranscribeResponse(
        episode_id=episode.id,
        job_id=job.id,
        task_id=task.id,
        status=EpisodeStatus.QUEUED,
        cached=False,
        message="Transcription queued on the local ASR worker",
    )


@router.get(
    "/{episode_id}/transcript",
    response_model=TranscriptRead,
    summary="Fetch the stored transcript",
)
def read_transcript(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TranscriptRead:
    """Return the timestamped transcript for an episode."""
    get_episode_or_404(db, episode_id, current_user)
    transcript = db.scalar(
        select(Transcript)
        .options(selectinload(Transcript.segments))
        .where(Transcript.episode_id == episode_id)
    )
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This episode has not been transcribed yet",
        )
    return TranscriptRead.model_validate(transcript)


@router.get(
    "/{episode_id}/media",
    summary="Stream the stored media file (supports HTTP range requests)",
)
def stream_episode_media(
    episode_id: int,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Serve the source media straight from local storage for previews."""
    user = None
    if token:
        try:
            payload = decode_token(token, token_type=ACCESS_TOKEN_TYPE)
            user_id = int(payload["sub"])
            user = _load_user(db, user_id)
        except Exception:
            user = None

    if user is None:
        raise CREDENTIALS_EXCEPTION

    episode = get_episode_or_404(db, episode_id, user)
    media_path = storage.absolute_media_path(episode.media_path)
    if not media_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Media file is missing on disk"
        )
    media_type = episode.mime_type or "application/octet-stream"
    return FileResponse(
        media_path,
        media_type=media_type,
        filename=episode.original_filename or media_path.name,
    )


@router.get(
    "/{episode_id}/stats",
    summary="Aggregate episode statistics",
)
def episode_stats(
    episode_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return counters used by the dashboard cards."""
    episode = get_episode_or_404(db, episode_id, current_user)
    clips = list(
        db.scalars(select(Clip).where(Clip.episode_id == episode.id)).all()
    )
    rendered = [clip for clip in clips if clip.output_path]
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "duration_seconds": episode.duration_seconds,
        "has_transcript": episode.transcript is not None,
        "clip_count": len(clips),
        "rendered_clip_count": len(rendered),
        "best_virality_score": round(
            max([clip.virality_score for clip in clips], default=0.0), 3
        ),
        "total_output_bytes": sum(clip.output_size_bytes for clip in rendered),
    }
