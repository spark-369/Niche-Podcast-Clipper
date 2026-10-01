"""Route modules for API v1."""

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

__all__ = ["admin", "auth", "clips", "episodes", "health", "jobs", "public", "search", "stats"]
