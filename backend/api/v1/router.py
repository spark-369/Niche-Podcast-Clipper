"""API v1 router aggregation."""

from fastapi import APIRouter

from api.v1.routers import (
    admin,
    auth,
    clips,
    episodes,
    health,
    jobs,
    public,
    search,
    stats,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(episodes.router)
api_router.include_router(clips.router)
api_router.include_router(jobs.router)
api_router.include_router(search.router)
api_router.include_router(stats.router)
api_router.include_router(public.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
