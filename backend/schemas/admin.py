"""Pydantic schemas for superuser administration endpoints."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserAdminRead(BaseModel):
    """Administrative view of a user account."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool
    is_superuser: bool
    created_at: datetime
    episode_count: int = 0
    clip_count: int = 0


class UserAdminUpdate(BaseModel):
    """Mutable administration fields; identity fields are immutable here."""

    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None
    full_name: Optional[str] = Field(default=None, max_length=255)