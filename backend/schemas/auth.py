"""Pydantic v2 schemas for authentication and JWT tokens."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Token(BaseModel):
    """OAuth2 compatible token response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class TokenPayload(BaseModel):
    """Decoded JWT payload."""

    sub: str
    type: str
    exp: int
    iat: int
    jti: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    """Body used to exchange a refresh token for a new access token."""

    refresh_token: str


class LoginRequest(BaseModel):
    """JSON login body (alternative to the OAuth2 form endpoint)."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class RegisterRequest(BaseModel):
    """Sign-up payload."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: Optional[str] = Field(default=None, max_length=255)


class UserRead(BaseModel):
    """Public representation of a user account."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool
    is_superuser: bool
    created_at: datetime


class UserUpdate(BaseModel):
    """Mutable user profile fields."""

    full_name: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, min_length=8, max_length=128)
