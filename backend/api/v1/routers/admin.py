"""Superuser administration routes for account management."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.deps import get_current_superuser
from core.redis_client import cache_delete
from db.session import get_db
from models import Clip, Episode, User
from schemas.admin import UserAdminRead, UserAdminUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["Admin"])


def _user_admin_read(db: Session, user: User) -> UserAdminRead:
    """Serialise a user together with its content counters."""
    episode_count = (
        db.scalar(
            select(func.count(Episode.id)).where(Episode.owner_id == user.id)
        )
        or 0
    )
    clip_count = (
        db.scalar(select(func.count(Clip.id)).where(Clip.owner_id == user.id)) or 0
    )
    payload = UserAdminRead.model_validate(user)
    payload.episode_count = int(episode_count)
    payload.clip_count = int(clip_count)
    return payload


@router.get(
    "/users",
    response_model=list[UserAdminRead],
    summary="List user accounts (superuser only)",
)
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
    q: str = Query(default=None, max_length=200, description="Email/name filter"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[UserAdminRead]:
    """Return every account, newest first, with its content counters."""
    query = select(User).order_by(User.created_at.desc()).offset(skip).limit(limit)
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(
            (User.email.ilike(pattern)) | (User.full_name.ilike(pattern))
        )
    users = list(db.scalars(query).all())
    return [_user_admin_read(db, user) for user in users]


@router.patch(
    "/users/{user_id}",
    response_model=UserAdminRead,
    summary="Activate / deactivate a user or grant superuser (superuser only)",
)
def update_user(
    user_id: int,
    payload: UserAdminUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
) -> UserAdminRead:
    """Toggle account flags. Self-lockout is rejected with 400."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    if user_id == current_user.id and (
        payload.is_active is False or payload.is_superuser is False
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate or demote your own account",
        )

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.is_superuser is not None:
        user.is_superuser = payload.is_superuser

    db.add(user)
    db.commit()
    db.refresh(user)
    # The auth dependency caches identity snapshots in Redis; drop the entry so
    # a deactivated account is rejected on the next request instead of after
    # the cache TTL.
    cache_delete(f"user:{user.id}")
    logger.info(
        "Admin %s updated user %s (is_active=%s, is_superuser=%s)",
        current_user.id,
        user_id,
        user.is_active,
        user.is_superuser,
    )
    return _user_admin_read(db, user)