"""Public, unauthenticated share-link routes.

Every response here is keyed by an unguessable share token minted through
``POST /clips/{id}/shares``. Only *active* (non-revoked, non-expired) links
resolve; metadata never includes the owner's identity or any internal path.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import rate_limit
from core.config import settings
from db.session import get_db
from models import Clip, ClipShare
from schemas.clip import PublicClipRead
from services import storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["Public shares"])


def _get_active_share(db: Session, token: str) -> ClipShare:
    """Resolve a share token or fail with 404/410."""
    share = db.scalar(select(ClipShare).where(ClipShare.token == token))
    if share is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found"
        )
    if not share.is_active:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This share link has expired or been revoked",
        )
    return share


def _get_active_share_clip(db: Session, share: ClipShare) -> Clip:
    """Load the clip a share points at (shares are deleted with their clip)."""
    clip = db.scalar(select(Clip).where(Clip.id == share.clip_id))
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Clip no longer exists"
        )
    return clip


@router.get(
    "/clips/{token}",
    response_model=PublicClipRead,
    summary="View a shared clip's public metadata",
    dependencies=[Depends(rate_limit)],
)
def get_shared_clip(
    token: str,
    db: Session = Depends(get_db),
) -> PublicClipRead:
    """Return the shareable metadata for a clip and count the view."""
    share = _get_active_share(db, token)
    clip = _get_active_share_clip(db, share)

    share.view_count += 1
    db.add(share)
    db.commit()

    return PublicClipRead(
        title=clip.title,
        hook=clip.hook,
        summary=clip.summary,
        duration_seconds=clip.duration_seconds,
        virality_score=clip.virality_score,
        keywords=clip.keywords,
        thumbnail_url=(
            f"{settings.API_V1_PREFIX}/public/clips/{token}/thumbnail"
            if clip.thumbnail_path
            else None
        ),
        download_url=(
            f"{settings.API_V1_PREFIX}/public/clips/{token}/download"
            if clip.output_path
            else None
        ),
        view_count=share.view_count,
        expires_at=share.expires_at,
        created_at=clip.created_at,
    )


@router.get(
    "/clips/{token}/download",
    summary="Download the shared clip's rendered MP4",
    dependencies=[Depends(rate_limit)],
)
def download_shared_clip(
    token: str,
    db: Session = Depends(get_db),
):
    """Stream the rendered vertical MP4 to anyone holding the share token."""
    share = _get_active_share(db, token)
    clip = _get_active_share_clip(db, share)

    if not clip.output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This clip has not been rendered yet",
        )
    path = storage.absolute_media_path(clip.output_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rendered file is missing on disk",
        )
    safe_title = storage.sanitize_filename(clip.title or f"clip-{clip.id}")[:80]
    return FileResponse(path, media_type="video/mp4", filename=f"{safe_title}.mp4")


@router.get(
    "/clips/{token}/thumbnail",
    summary="Download the shared clip's thumbnail",
    dependencies=[Depends(rate_limit)],
)
def shared_clip_thumbnail(
    token: str,
    db: Session = Depends(get_db),
):
    """Stream the clip thumbnail JPEG for link previews."""
    share = _get_active_share(db, token)
    clip = _get_active_share_clip(db, share)

    if not clip.thumbnail_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No thumbnail available for this clip",
        )
    path = storage.absolute_media_path(clip.thumbnail_path)
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail is missing on disk",
        )
    return FileResponse(path, media_type="image/jpeg")