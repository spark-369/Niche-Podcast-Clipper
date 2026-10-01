"""Aggregated dashboard statistics for the authenticated user."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.deps import get_current_user
from core.config import settings
from db.session import get_db
from models import Clip, Episode, RenderJob, User
from schemas.clip import RenderJobRead

router = APIRouter(tags=["Stats"])


@router.get(
    "/stats/overview",
    summary="Dashboard analytics for the current user",
)
def stats_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Return the counters that power the dashboard in a single request.

    Includes per-status totals, storage usage, favourite count, the top clips
    by virality score, and recent render activity over the last seven days.
    All queries are scoped to the caller's own rows.
    """
    owner_id = current_user.id

    episode_status_rows = db.execute(
        select(Episode.status, func.count(Episode.id))
        .where(Episode.owner_id == owner_id)
        .group_by(Episode.status)
    ).all()
    clip_status_rows = db.execute(
        select(Clip.status, func.count(Clip.id))
        .where(Clip.owner_id == owner_id)
        .group_by(Clip.status)
    ).all()

    total_episode_bytes = (
        db.scalar(
            select(func.coalesce(func.sum(Episode.file_size_bytes), 0)).where(
                Episode.owner_id == owner_id
            )
        )
        or 0
    )
    total_output_bytes = (
        db.scalar(
            select(func.coalesce(func.sum(Clip.output_size_bytes), 0)).where(
                Clip.owner_id == owner_id
            )
        )
        or 0
    )
    clip_aggregates = db.execute(
        select(
            func.count(Clip.id),
            func.count(Clip.output_path),
            func.coalesce(func.avg(Clip.duration_seconds), 0.0),
        ).where(Clip.owner_id == owner_id)
    ).one()
    total_clips, rendered_clips, avg_duration = clip_aggregates
    favorite_count = (
        db.scalar(
            select(func.count(Clip.id)).where(
                Clip.owner_id == owner_id, Clip.is_favorite.is_(True)
            )
        )
        or 0
    )

    top_clips = list(
        db.scalars(
            select(Clip)
            .where(Clip.owner_id == owner_id)
            .order_by(Clip.virality_score.desc(), Clip.created_at.desc())
            .limit(5)
        ).all()
    )

    owned_episode_ids = select(Episode.id).where(Episode.owner_id == owner_id)
    recent_jobs = list(
        db.scalars(
            select(RenderJob)
            .where(RenderJob.episode_id.in_(owned_episode_ids))
            .order_by(RenderJob.created_at.desc())
            .limit(5)
        ).all()
    )
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    renders_last_7_days = (
        db.scalar(
            select(func.count(RenderJob.id))
            .where(RenderJob.episode_id.in_(owned_episode_ids))
            .where(RenderJob.created_at >= week_ago)
        )
        or 0
    )

    return {
        "episodes": {
            "total": int(sum(count for _, count in episode_status_rows)),
            "by_status": {status_: int(count) for status_, count in episode_status_rows},
            "total_source_bytes": int(total_episode_bytes),
        },
        "clips": {
            "total": int(total_clips or 0),
            "rendered": int(rendered_clips or 0),
            "favorites": int(favorite_count),
            "average_duration_seconds": round(float(avg_duration or 0.0), 3),
            "by_status": {status_: int(count) for status_, count in clip_status_rows},
            "total_output_bytes": int(total_output_bytes),
        },
        "activity": {
            "renders_last_7_days": int(renders_last_7_days),
            "recent_jobs": [
                RenderJobRead.model_validate(job).model_dump(mode="json")
                for job in recent_jobs
            ],
        },
        "top_clips": [
            {
                "id": clip.id,
                "episode_id": clip.episode_id,
                "title": clip.title,
                "virality_score": clip.virality_score,
                "status": clip.status,
                "is_favorite": bool(clip.is_favorite),
                "thumbnail_url": (
                    f"{settings.API_V1_PREFIX}/clips/{clip.id}/thumbnail"
                    if clip.thumbnail_path
                    else None
                ),
            }
            for clip in top_clips
        ],
    }
