"""Pydantic schemas for the global search endpoint."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EpisodeSearchHit(BaseModel):
    """One episode matching a search query."""

    id: int
    title: str
    description: Optional[str] = None
    status: str
    duration_seconds: float
    clip_count: int = 0
    created_at: datetime


class ClipSearchHit(BaseModel):
    """One clip matching a search query."""

    id: int
    episode_id: int
    title: str
    hook: str
    virality_score: float
    status: str
    is_favorite: bool = False
    thumbnail_url: Optional[str] = None
    created_at: datetime


class SearchResponse(BaseModel):
    """Grouped search results across the caller's library."""

    query: str
    total: int = 0
    episodes: list[EpisodeSearchHit] = []
    clips: list[ClipSearchHit] = []