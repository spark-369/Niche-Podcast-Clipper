"""Common API schemas: pagination, health and error envelopes."""

from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class Page(BaseModel, Generic[DataT]):
    """Generic paginated response envelope."""

    items: List[DataT] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 1
        return max((self.total + self.page_size - 1) // self.page_size, 1)


class MessageResponse(BaseModel):
    """Simple message envelope."""

    message: str
    detail: str | None = None


class HealthResponse(BaseModel):
    """Service health report."""

    status: str = "ok"
    app_name: str
    environment: str
    version: str
    database: bool = False
    redis: bool = False
    # Host disk headroom for the media storage volume (None when probing fails,
    # e.g. unusual filesystems). Operators alert on ``disk_free_bytes``.
    disk_total_bytes: Optional[int] = None
    disk_free_bytes: Optional[int] = None


class CaptionPresetResponse(BaseModel):
    """Wrapper around the caption preset list endpoint."""

    presets: list["CaptionStylePresetRead"] = []  # noqa: F821


from schemas.clip import CaptionStylePresetRead  # noqa: E402,F401

CaptionPresetResponse.model_rebuild()
