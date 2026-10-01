"""Global search across the caller's episodes and clips."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from api.deps import get_current_user
from core.config import settings
from db.session import get_db
from models import Clip, Episode, User
from schemas.search import ClipSearchHit, EpisodeSearchHit, SearchResponse

router = APIRouter(tags=["Search"])


@router.get(
    "/search",
    response_model=SearchResponse,
    summary="Search episodes and clips in one request",
)
def search(
    q: str = Query(min_length=1, max_length=200, description="Free-text query"),
    limit: int = Query(default=10, ge=1, le=50, description="Max hits per group"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    """Case-insensitive substring search across the caller's own library.

    Episodes match on title/description; clips match on title, hook, summary
    and transcript text. Every query is scoped by ``owner_id`` so results can
    never leak across accounts.
    """
    query = q.strip()
    pattern = f"%{query}%"

    episode_rows = db.execute(
        select(Episode, func.count(Clip.id))
        .outerjoin(Clip, Clip.episode_id == Episode.id)
        .where(
            Episode.owner_id == current_user.id,
            or_(Episode.title.ilike(pattern), Episode.description.ilike(pattern)),
        )
        .group_by(Episode.id)
        .order_by(Episode.created_at.desc())
        .limit(limit)
    ).all()
    episodes = [
        EpisodeSearchHit(
            id=episode.id,
            title=episode.title,
            description=episode.description,
            status=episode.status,
            duration_seconds=episode.duration_seconds,
            clip_count=int(count or 0),
            created_at=episode.created_at,
        )
        for episode, count in episode_rows
    ]

    clips = list(
        db.scalars(
            select(Clip)
            .where(
                Clip.owner_id == current_user.id,
                or_(
                    Clip.title.ilike(pattern),
                    Clip.hook.ilike(pattern),
                    Clip.summary.ilike(pattern),
                    Clip.transcript_text.ilike(pattern),
                ),
            )
            .order_by(Clip.virality_score.desc(), Clip.created_at.desc())
            .limit(limit)
        ).all()
    )
    clip_hits = [
        ClipSearchHit(
            id=clip.id,
            episode_id=clip.episode_id,
            title=clip.title,
            hook=clip.hook,
            virality_score=clip.virality_score,
            status=clip.status,
            is_favorite=bool(clip.is_favorite),
            thumbnail_url=(
                f"{settings.API_V1_PREFIX}/clips/{clip.id}/thumbnail"
                if clip.thumbnail_path
                else None
            ),
            created_at=clip.created_at,
        )
        for clip in clips
    ]

    return SearchResponse(
        query=query,
        total=len(episodes) + len(clip_hits),
        episodes=episodes,
        clips=clip_hits,
    )