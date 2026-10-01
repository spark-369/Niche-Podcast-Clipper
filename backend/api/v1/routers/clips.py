"""Clip routes: highlight generation, editing, rendering and downloads."""

import csv
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from api.deps import get_current_user, rate_limit, rate_limit_auth, _load_user, decode_token, ACCESS_TOKEN_TYPE, CREDENTIALS_EXCEPTION
from core.config import settings
from core.redis_client import cache_delete_prefix
from db.session import get_db
from models import Clip, ClipShare, Episode, RenderJob, Transcript, User
from models.enums import ClipStatus, JobStatus, JobType
from schemas.clip import (
    BatchRenderRequest,
    BatchRenderResponse,
    CaptionStyle,
    CaptionStylePresetRead,
    CaptionTimelineGroup,
    CaptionTimelineRead,
    CaptionTimelineWord,
    ClipGenerateRequest,
    ClipGenerateResponse,
    ClipRead,
    ClipRenderRequest,
    ClipRenderResponse,
    ClipShareCreate,
    ClipShareRead,
    ClipUpdate,
    FramingOptions,
    RenderJobRead,
)
from services import caption_styles, storage
from services.subtitles import render_subtitles
from services.video import renderer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/clips", tags=["Clips"])


def _with_urls(clip: Clip, user_id: int) -> ClipRead:
    """Attach local download/thumbnail/preview URLs to a clip payload."""
    payload = ClipRead.model_validate(clip)
    if clip.output_path:
        payload.download_url = (
            f"{settings.API_V1_PREFIX}/clips/{clip.id}/download"
        )
    if clip.thumbnail_path:
        payload.thumbnail_url = (
            f"{settings.API_V1_PREFIX}/clips/{clip.id}/thumbnail"
        )
    return payload


def get_clip_or_404(db: Session, clip_id: int, user: User) -> Clip:
    """Fetch a clip owned by the current user or raise 404."""
    clip = db.scalar(
        select(Clip).where(Clip.id == clip_id, Clip.owner_id == user.id)
    )
    if clip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Clip not found")
    return clip


@router.post(
    "/generate",
    response_model=ClipGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue local highlight extraction for an episode (Celery task)",
    dependencies=[Depends(rate_limit)],
)
def generate_clips(
    payload: ClipGenerateRequest,
    episode_id: int = Query(..., description="Episode to analyse"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipGenerateResponse:
    """Queue highlight extraction on the local worker and return a job handle.

    The scoring run is a Celery task: it replaces this episode's clips when it
    finishes and reports progress through its ``render_jobs`` mirror row, which
    clients poll for live percent/stage updates that survive page refreshes.
    """
    episode = db.scalar(
        select(Episode).where(
            Episode.id == episode_id, Episode.owner_id == current_user.id
        )
    )
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    transcript = db.scalar(
        select(Transcript).where(Transcript.episode_id == episode.id)
    )
    if transcript is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Transcribe this episode before generating clips",
        )

    # The task REPLACES every clip of the episode; two concurrent runs would
    # race on that deletion, so an active job short-circuits to its own handle.
    stale_before = datetime.now(timezone.utc) - timedelta(hours=6)
    active_job = db.scalar(
        select(RenderJob).where(
            RenderJob.episode_id == episode.id,
            RenderJob.job_type == JobType.HIGHLIGHT_EXTRACTION.value,
            RenderJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            RenderJob.created_at > stale_before,
        )
    )
    if active_job is not None:
        return ClipGenerateResponse(
            episode_id=episode.id,
            job_id=active_job.id,
            task_id=active_job.task_id,
            status=JobStatus(active_job.status),
            cached=False,
            message="Highlight extraction is already running",
        )

    job = RenderJob(
        episode_id=episode.id,
        job_type=JobType.HIGHLIGHT_EXTRACTION.value,
        status=JobStatus.PENDING.value,
        progress=0,
        message="Queued for highlight extraction",
    )
    db.add(job)
    db.commit()

    from tasks.ai_tasks import extract_highlights_task

    task = extract_highlights_task.delay(
        episode.id,
        payload.clip_count,
        payload.min_duration_seconds,
        payload.max_duration_seconds,
        payload.temperature,
        payload.focus,
        job.id,
        payload.use_cache,
        payload.caption_style.model_dump(mode="json"),
        payload.framing.model_dump(mode="json"),
    )
    job.task_id = task.id
    db.add(job)
    db.commit()

    return ClipGenerateResponse(
        episode_id=episode.id,
        job_id=job.id,
        task_id=task.id,
        status=JobStatus.PENDING,
        cached=False,
        message="Highlight extraction queued on the local worker",
    )


@router.get("", response_model=list[ClipRead], summary="List clips")
def list_clips(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    episode_id: Optional[int] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    favorite: Optional[bool] = Query(default=None, description="Only starred clips"),
    search: Optional[str] = Query(
        default=None, max_length=100, description="Case-insensitive text filter"
    ),
) -> list[ClipRead]:
    """Return the current user's clips, optionally filtered by episode."""
    query = (
        select(Clip)
        .where(Clip.owner_id == current_user.id, Clip.virality_score >= min_score)
        .order_by(Clip.virality_score.desc(), Clip.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if episode_id is not None:
        query = query.where(Clip.episode_id == episode_id)
    if favorite is not None:
        query = query.where(Clip.is_favorite.is_(favorite))
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                Clip.title.ilike(pattern),
                Clip.hook.ilike(pattern),
                Clip.summary.ilike(pattern),
                Clip.transcript_text.ilike(pattern),
            )
        )

    clips = list(db.scalars(query).all())
    return [_with_urls(clip, current_user.id) for clip in clips]


@router.get("/presets", response_model=list[CaptionStylePresetRead], summary="Caption presets")
def list_caption_presets() -> list[CaptionStylePresetRead]:
    """Return every built-in caption theme for the frontend gallery."""
    return [
        CaptionStylePresetRead(
            name=preset["name"], label=preset["label"], style=CaptionStyle(**preset["style"])
        )
        for preset in caption_styles.list_presets()
    ]


@router.get(
    "/export",
    summary="Export the clip library as CSV",
    response_class=PlainTextResponse,
)
def export_clips_csv(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    episode_id: Optional[int] = Query(default=None),
) -> PlainTextResponse:
    """Download every clip's metadata as a spreadsheet-friendly CSV file.

    Titles and hooks are exported plain; wrap any field that may contain a
    comma, quote or newline in quotes per RFC 4180 (``csv.writer`` handles it).
    """
    query = (
        select(Clip)
        .where(Clip.owner_id == current_user.id)
        .order_by(Clip.created_at.desc())
    )
    if episode_id is not None:
        query = query.where(Clip.episode_id == episode_id)
    clips = list(db.scalars(query).all())

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(
        [
            "id",
            "episode_id",
            "title",
            "hook",
            "summary",
            "start_time",
            "end_time",
            "duration_seconds",
            "virality_score",
            "hook_score",
            "emotion_score",
            "quotability_score",
            "status",
            "is_favorite",
            "output_size_bytes",
            "created_at",
        ]
    )
    for clip in clips:
        writer.writerow(
            [
                clip.id,
                clip.episode_id,
                clip.title,
                clip.hook,
                clip.summary,
                clip.start_time,
                clip.end_time,
                clip.duration_seconds,
                clip.virality_score,
                clip.hook_score,
                clip.emotion_score,
                clip.quotability_score,
                clip.status,
                clip.is_favorite,
                clip.output_size_bytes,
                clip.created_at.isoformat() if clip.created_at else "",
            ]
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return PlainTextResponse(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="clips-export-{stamp}.csv"'
        },
    )


@router.post(
    "/render-batch",
    response_model=BatchRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue renders for several clips at once",
    dependencies=[Depends(rate_limit)],
)
def render_clips_batch(
    payload: BatchRenderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BatchRenderResponse:
    """Dispatch one OpenCV render job per clip, skipping the impossible ones.

    A clip is skipped (never fatal for the whole batch) when it does not belong
    to the caller or is already rendering. Duplicate ids in the payload are
    collapsed so a clip is never queued twice in one submission.
    """
    unique_ids: list[int] = []
    for clip_id in payload.clip_ids:
        if clip_id not in unique_ids:
            unique_ids.append(clip_id)

    owned = {
        clip.id: clip
        for clip in db.scalars(
            select(Clip).where(Clip.id.in_(unique_ids), Clip.owner_id == current_user.id)
        ).all()
    }

    response = BatchRenderResponse()
    from tasks.video_tasks import render_clip_task

    for clip_id in unique_ids:
        clip = owned.get(clip_id)
        if clip is None:
            response.skipped[clip_id] = "Clip not found in your library"
            continue
        if clip.status == ClipStatus.RENDERING.value:
            response.skipped[clip_id] = "A render is already in progress"
            continue

        clip.status = ClipStatus.RENDERING.value
        clip.progress = 0
        clip.error_message = None
        job = RenderJob(
            episode_id=clip.episode_id,
            clip_id=clip.id,
            job_type=JobType.RENDER.value,
            status=JobStatus.PENDING.value,
            progress=0,
            message="Queued for OpenCV rendering",
        )
        db.add(job)
        db.add(clip)
        db.commit()
        db.refresh(job)

        task = render_clip_task.apply_async(args=[clip.id, job.id], priority=payload.priority)
        job.task_id = task.id
        db.add(job)
        db.commit()
        db.refresh(job)
        logger.info("Batch render job %s queued for clip %s", job.id, clip.id)

        response.queued.append(
            ClipRenderResponse(
                clip_id=clip.id,
                job_id=job.id,
                task_id=task.id,
                status=JobStatus.PENDING,
                message="Render job dispatched to the Celery worker",
            )
        )
    return response


@router.get("/{clip_id}", response_model=ClipRead, summary="Clip detail")
def read_clip(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipRead:
    """Return a single clip with its caption and framing configuration."""
    clip = get_clip_or_404(db, clip_id, current_user)
    return _with_urls(clip, current_user.id)


@router.patch("/{clip_id}", response_model=ClipRead, summary="Update clip / caption style")
def update_clip(
    clip_id: int,
    payload: ClipUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipRead:
    """Tweak clip boundaries, text, caption styling or framing.

    This is the editor's plain "Save". It persists the title, hook, summary, cut
    boundaries, the caption text, the caption styling and the framing. It does
    **not** render: the editor follows a save with a ``/render`` call, so the
    burned captions are always produced from the values stored here.
    """
    clip = get_clip_or_404(db, clip_id, current_user)

    if payload.title is not None:
        clip.title = payload.title
    if payload.hook is not None:
        clip.hook = payload.hook
    if payload.summary is not None:
        clip.summary = payload.summary
    if payload.transcript_text is not None:
        # The fallback caption text used when the clip has no word timings. The
        # render endpoint stores the same value, so the burned captions and this
        # field always describe each other.
        clip.transcript_text = payload.transcript_text
    if payload.start_time is not None:
        clip.start_time = payload.start_time
    if payload.end_time is not None:
        clip.end_time = payload.end_time
    if clip.end_time <= clip.start_time:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="end_time must be greater than start_time",
        )
    clip.duration_seconds = round(clip.end_time - clip.start_time, 3)

    if payload.caption_style is not None:
        clip.caption_style = payload.caption_style.model_dump(mode="json")
    if payload.framing is not None:
        clip.framing = payload.framing.model_dump(mode="json")
    if payload.is_favorite is not None:
        # The star flag never changes the rendered pixels, so it must not
        # trigger the render invalidation below; the dedicated
        # POST /clips/{id}/favorite toggle exists for the same reason.
        clip.is_favorite = payload.is_favorite

    # Any *content or styling* edit invalidates the previously rendered file.
    if clip.output_path:
        storage.delete_path(clip.output_path)
        clip.output_path = None
        clip.output_size_bytes = 0
        clip.status = ClipStatus.READY.value

    db.add(clip)
    db.commit()
    db.refresh(clip)
    cache_delete_prefix(f"clip:{clip.id}")
    return _with_urls(clip, current_user.id)


@router.delete(
    "/{clip_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a clip"
)
def delete_clip(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a clip and its rendered artifacts."""
    clip = get_clip_or_404(db, clip_id, current_user)
    storage.delete_path(str(Path("clips") / str(current_user.id) / str(clip.id)))
    db.delete(clip)
    db.commit()


@router.post(
    "/{clip_id}/render",
    response_model=ClipRenderResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Dispatch an OpenCV render job to the Celery worker",
    dependencies=[Depends(rate_limit)],
)
def render_clip_endpoint(
    clip_id: int,
    payload: ClipRenderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipRenderResponse:
    """Queue caption burning + 9:16 auto-cropping for a clip."""
    clip = get_clip_or_404(db, clip_id, current_user)

    if payload.caption_style is not None:
        clip.caption_style = payload.caption_style.model_dump(mode="json")
    if payload.framing is not None:
        clip.framing = payload.framing.model_dump(mode="json")
    if payload.transcript is not None:
        # Stored here rather than only in the PATCH so the text a render burns
        # is always the text on record, even when the editor saves and renders
        # in one step.
        clip.transcript_text = payload.transcript.strip() or clip.transcript_text

    clip.status = ClipStatus.RENDERING.value
    clip.progress = 0
    clip.error_message = None

    job = RenderJob(
        episode_id=clip.episode_id,
        clip_id=clip.id,
        job_type=JobType.RENDER.value,
        status=JobStatus.PENDING.value,
        progress=0,
        message="Queued for OpenCV rendering",
    )
    db.add(job)
    db.add(clip)
    db.commit()
    db.refresh(job)

    from tasks.video_tasks import render_clip_task
    task = render_clip_task.apply_async(
        args=[clip.id, job.id], priority=payload.priority
    )
    job.task_id = task.id
    db.add(job)
    db.commit()
    db.refresh(job)

    logger.info("Render job %s queued for clip %s (task %s)", job.id, clip.id, task.id)
    return ClipRenderResponse(
        clip_id=clip.id,
        job_id=job.id,
        task_id=task.id,
        status=JobStatus.PENDING,
        message="Render job dispatched to the Celery worker",
    )


@router.get("/{clip_id}/status", response_model=RenderJobRead, summary="Render progress")
def clip_render_status(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RenderJobRead:
    """Return the latest render job state for polling clients."""
    clip = get_clip_or_404(db, clip_id, current_user)
    job = db.scalar(
        select(RenderJob)
        .where(RenderJob.clip_id == clip.id)
        .order_by(RenderJob.created_at.desc())
    )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No render job found for this clip"
        )
    return RenderJobRead.model_validate(job)


@router.get("/{clip_id}/download", summary="Download the rendered clip")
def download_clip(
    clip_id: int,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Stream the rendered vertical MP4 from local storage."""
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

    clip = get_clip_or_404(db, clip_id, user)
    if not clip.output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This clip has not been rendered yet"
        )
    path = storage.absolute_media_path(clip.output_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Rendered file is missing on disk"
        )
    safe_title = storage.sanitize_filename(clip.title or f"clip-{clip.id}")[:80]
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{safe_title}.mp4",
    )


@router.get("/{clip_id}/thumbnail", summary="Download the clip thumbnail")
def download_thumbnail(
    clip_id: int,
    token: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Stream the generated JPEG thumbnail."""
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

    clip = get_clip_or_404(db, clip_id, user)
    if not clip.thumbnail_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No thumbnail available"
        )

    thumbnail_path = storage.absolute_media_path(clip.thumbnail_path)
    if not thumbnail_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail file is missing on disk",
        )
    # The route previously fell through without returning, so every thumbnail
    # request answered 500 despite the clip having a valid JPEG on disk.
    return FileResponse(
        thumbnail_path,
        media_type="image/jpeg",
        filename=f"clip-{clip.id}-thumb.jpg",
    )


@router.get(
    "/{clip_id}/caption-timeline",
    response_model=CaptionTimelineRead,
    summary="Caption groups with clip-relative timings",
)
def clip_caption_timeline(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    transcript: Optional[str] = Query(
        default=None,
        max_length=4000,
        description="Draft transcript, so edited caption text is included",
    ),
    start_time: Optional[float] = Query(default=None, ge=0.0),
    end_time: Optional[float] = Query(default=None, ge=0.0),
    caption_style_json: Optional[str] = Query(
        default=None, alias="caption_style", description="Draft caption style (JSON)"
    ),
):
    """Return every caption group for a clip in one small JSON payload.

    The editor needs to know which words are on screen at an arbitrary playback
    position, many times a second. Fetching a re-encoded JPEG per position meant
    a video seek, a full composite and a JPEG encode several times per second —
    the requests queued up and the preview visibly froze. Returning the whole
    timeline once lets the client composite the overlay itself on every
    animation frame, so captions track playback exactly.

    Timings are clip-relative (already rebased off `start_time`) and therefore
    directly comparable to `video.currentTime` in the browser.
    """
    import json

    clip = get_clip_or_404(db, clip_id, current_user)

    caption_style = dict(clip.caption_style or {})
    if caption_style_json:
        try:
            caption_style.update(json.loads(caption_style_json))
        except ValueError:
            logger.warning("Ignoring invalid caption_style JSON in timeline request")

    preview_start = clip.start_time if start_time is None else start_time
    preview_end = clip.end_time if end_time is None else end_time
    if preview_end <= preview_start:
        preview_end = preview_start + max(clip.duration_seconds, 0.5)
    preview_transcript = clip.transcript_text if transcript is None else transcript

    from services.video import captions as caption_utils

    words_per_line = int(caption_style.get("words_per_line", 3))
    max_lines = int(caption_style.get("max_lines", 2))
    groups = caption_utils.group_caption_lines(
        caption_utils.words_from_payload(
            clip.words or [], preview_start, preview_end, preview_transcript
        ),
        words_per_line,
        max_lines,
    )

    payload_groups = [
        CaptionTimelineGroup(
            words=[
                CaptionTimelineWord(text=word.text, start=word.start, end=word.end)
                for word in group
            ],
            start=group[0].start if group else 0.0,
            end=group[-1].end if group else 0.0,
        )
        for group in groups
        if group
    ]

    return CaptionTimelineRead(
        clip_id=clip.id,
        duration_seconds=round(max(preview_end - preview_start, 0.0), 3),
        words_per_line=words_per_line,
        max_lines=max_lines,
        groups=payload_groups,
    )


@router.get("/{clip_id}/preview", summary="Render a single composited preview frame")
def preview_clip_frame(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    timestamp: Optional[float] = Query(
        default=None, description="Clip-relative preview time in seconds"
    ),
    caption_style_json: Optional[str] = Query(
        default=None, alias="caption_style", description="Draft caption style (JSON)"
    ),
    framing_json: Optional[str] = Query(
        default=None, alias="framing", description="Draft framing options (JSON)"
    ),
    title: Optional[str] = Query(
        default=None,
        max_length=512,
        description="Draft clip title, so the title card previews too",
    ),
    start_time: Optional[float] = Query(
        default=None, ge=0.0, description="Draft clip start (seconds)"
    ),
    end_time: Optional[float] = Query(
        default=None, ge=0.0, description="Draft clip end (seconds)"
    ),
    transcript: Optional[str] = Query(
        default=None,
        max_length=4000,
        description="Draft transcript, so edited caption text previews live",
    ),
):
    """Return a JPEG frame showing the current crop and caption styling.

    This lets the React caption editor render an accurate preview without
    re-encoding the whole clip. Draft (unsaved) style, framing, title and cut
    overrides can be supplied as query parameters so the preview matches what is
    on screen — including edits to the Timing and Text sections, which would
    otherwise only appear after a save.
    """
    import json

    import cv2
    import numpy as np

    clip = get_clip_or_404(db, clip_id, current_user)
    episode = db.scalar(select(Episode).where(Episode.id == clip.episode_id))
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Episode not found")

    caption_style = dict(clip.caption_style or {})
    framing = dict(clip.framing or {})
    if caption_style_json:
        try:
            caption_style.update(json.loads(caption_style_json))
        except ValueError:
            logger.warning("Ignoring invalid caption_style JSON in preview request")
    if framing_json:
        try:
            framing.update(json.loads(framing_json))
        except ValueError:
            logger.warning("Ignoring invalid framing JSON in preview request")

    # Fall back to the persisted values so an unspecified draft field still
    # previews the clip as stored.
    preview_title = clip.title if title is None else title
    preview_start = clip.start_time if start_time is None else start_time
    preview_end = clip.end_time if end_time is None else end_time
    if preview_end <= preview_start:
        preview_end = preview_start + max(clip.duration_seconds, 0.5)
    # The editor's unsaved transcript takes precedence: `words_from_payload`
    # falls back to distributing this text when the stored word timings no
    # longer match it, so typing a new line updates the previewed captions
    # without waiting for a save.
    preview_transcript = clip.transcript_text if transcript is None else transcript

    source_path = storage.absolute_media_path(episode.media_path)
    if not source_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Source media is missing on disk"
        )

    clip_offset = (
        timestamp if timestamp is not None else min((preview_end - preview_start) / 2, 3.0)
    )
    absolute_time = preview_start + max(clip_offset, 0.0)

    try:
        if episode.has_video_stream:
            frame = renderer.preview_frame(
                source_path,
                absolute_time,
                caption_style,
                framing,
                words=clip.words,
                clip_start=preview_start,
                clip_end=preview_end,
                title=preview_title if clip_offset < 1.5 else "",
                transcript_text=preview_transcript or "",
            )
        else:
            frame = None
    except Exception as exc:  # pragma: no cover - codec dependent
        logger.warning("Preview render failed for clip %s: %s", clip.id, exc)
        frame = None

    if frame is None:
        width = int(framing.get("output_width", settings.OUTPUT_WIDTH))
        height = int(framing.get("output_height", settings.OUTPUT_HEIGHT))
        frame = np.full(
            (height, width, 3),
            caption_styles.caption_style_to_dict(caption_style).get(
                "background_color", "#101828"
            )
            and renderer.hex_bgr_tuple(
                caption_styles.caption_style_to_dict(caption_style).get(
                    "background_color"
                )
                or "#101828"
            ),
            dtype=np.uint8,
        )
        from services.video import captions as caption_utils

        groups = caption_utils.group_caption_lines(
            caption_utils.words_from_payload(
                clip.words or [], preview_start, preview_end, preview_transcript
            ),
            int(caption_style.get("words_per_line", 3)),
            int(caption_style.get("max_lines", 2)),
        )
        caption_utils.draw_captions(
            frame, max(clip_offset, 0.0), groups, caption_style
        )

    ok, encoded = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88]
    )
    if not ok:  # pragma: no cover - encoding failure
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not encode the preview frame",
        )
    from fastapi import Response

    return Response(
        content=encoded.tobytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{clip_id}/jobs", response_model=list[RenderJobRead], summary="Clip render history")
def list_clip_jobs(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[RenderJobRead]:
    """Return the render job history for a clip."""
    clip = get_clip_or_404(db, clip_id, current_user)
    jobs = list(
        db.scalars(
            select(RenderJob)
            .where(RenderJob.clip_id == clip.id)
            .order_by(RenderJob.created_at.desc())
            .limit(20)
        ).all()
    )
    return [RenderJobRead.model_validate(job) for job in jobs]


@router.post(
    "/{clip_id}/favorite",
    response_model=ClipRead,
    summary="Toggle the favourite (star) flag on a clip",
)
def toggle_clip_favorite(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipRead:
    """Star or unstar a clip and return the updated payload."""
    clip = get_clip_or_404(db, clip_id, current_user)
    clip.is_favorite = not bool(clip.is_favorite)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    cache_delete_prefix(f"clip:{clip.id}")
    return _with_urls(clip, current_user.id)


def _share_read(share: ClipShare) -> ClipShareRead:
    """Build the owner-facing payload for a share link, including its URL."""
    payload = ClipShareRead.model_validate(share)
    payload.url = f"{settings.API_V1_PREFIX}/public/clips/{share.token}"
    return payload


@router.post(
    "/{clip_id}/shares",
    response_model=ClipShareRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a public share link for a clip",
    dependencies=[Depends(rate_limit_auth)],
)
def create_clip_share(
    clip_id: int,
    payload: Optional[ClipShareCreate] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ClipShareRead:
    """Mint an unguessable public URL for this clip's rendered deliverable.

    Anyone with the token can view the clip's metadata and download the MP4 —
    no account required. Optionally attach an expiry (1-365 days); links can
    always be revoked via ``DELETE /clips/{id}/shares/{share_id}``.
    """
    clip = get_clip_or_404(db, clip_id, current_user)
    body = payload or ClipShareCreate()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        if body.expires_in_days
        else None
    )
    share = ClipShare(
        clip_id=clip.id,
        owner_id=current_user.id,
        token=secrets.token_urlsafe(24),
        expires_at=expires_at,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    logger.info("Share link created for clip %s by user %s", clip.id, current_user.id)
    return _share_read(share)


@router.get(
    "/{clip_id}/shares",
    response_model=list[ClipShareRead],
    summary="List the share links created for a clip",
)
def list_clip_shares(
    clip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ClipShareRead]:
    """Return every share link for the clip, newest first."""
    clip = get_clip_or_404(db, clip_id, current_user)
    shares = list(
        db.scalars(
            select(ClipShare)
            .where(ClipShare.clip_id == clip.id)
            .order_by(ClipShare.created_at.desc())
        ).all()
    )
    return [_share_read(share) for share in shares]


@router.delete(
    "/{clip_id}/shares/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a share link",
)
def revoke_clip_share(
    clip_id: int,
    share_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Soft-revoke a share link so its token stops resolving immediately."""
    clip = get_clip_or_404(db, clip_id, current_user)
    share = db.scalar(
        select(ClipShare).where(
            ClipShare.id == share_id, ClipShare.clip_id == clip.id
        )
    )
    if share is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found"
        )
    share.revoked_at = datetime.now(timezone.utc)
    db.add(share)
    db.commit()


@router.get(
    "/{clip_id}/captions-file",
    response_class=PlainTextResponse,
    summary="Export the clip captions as an SRT or WebVTT file",
)
def export_clip_captions(
    clip_id: int,
    fmt: str = Query(
        default="srt",
        pattern="^(srt|vtt|webvtt)$",
        description="Subtitle format: 'srt' or 'vtt'",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PlainTextResponse:
    """Download the clip's caption track as a standalone subtitle file.

    Uses the same word-timing pipeline as the burned captions, so an SRT from
    here always lines up with the rendered deliverable — including after the
    transcript was edited in the clip editor.
    """
    clip = get_clip_or_404(db, clip_id, current_user)
    try:
        document, media_type = render_subtitles(
            fmt,
            clip.words or [],
            clip.start_time,
            clip.end_time,
            clip.transcript_text or "",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    extension = "vtt" if media_type == "text/vtt" else "srt"
    safe_title = storage.sanitize_filename(clip.title or f"clip-{clip.id}")[:80]
    return PlainTextResponse(
        content=document,
        media_type=f"{media_type}; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_title}.{extension}"'
        },
    )
