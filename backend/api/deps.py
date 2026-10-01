"""Shared FastAPI dependencies: database, auth and rate limiting."""

import hashlib
import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import ExpiredSignatureError, JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from core.redis_client import cache_get_json, cache_set_json, check_rate_limit
from core.security import ACCESS_TOKEN_TYPE, decode_token
from db.session import get_db
from models import User

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/token", auto_error=False
)

USER_CACHE_TTL = 300
CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def _load_user(db: Session, user_id: int) -> Optional[User]:
    """Fetch a user, going through Redis first to avoid repetitive queries.

    Only non-sensitive identity fields are cached (never ``hashed_password``),
    and the returned object is a detached snapshot. Endpoints that *mutate* the
    user must re-fetch the persistent row via ``db.get`` — see
    ``auth.update_current_user``.
    """
    cache_key = f"user:{user_id}"
    cached = cache_get_json(cache_key)
    if cached:
        return User(
            id=cached["id"],
            email=cached["email"],
            full_name=cached.get("full_name"),
            hashed_password="",
            is_active=cached.get("is_active", True),
            is_superuser=cached.get("is_superuser", False),
            created_at=cached.get("created_at"),
            updated_at=cached.get("updated_at"),
        )

    user = db.scalar(select(User).where(User.id == user_id))
    if user is not None:
        cache_set_json(
            cache_key,
            {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active,
                "is_superuser": user.is_superuser,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            },
            ttl=USER_CACHE_TTL,
        )
    return user


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the authenticated user from a JWT bearer token."""
    if not token:
        raise CREDENTIALS_EXCEPTION
    try:
        payload = decode_token(token, token_type=ACCESS_TOKEN_TYPE)
        user_id = int(payload["sub"])
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (JWTError, ValueError, KeyError):
        raise CREDENTIALS_EXCEPTION

    user = _load_user(db, user_id)
    if user is None:
        raise CREDENTIALS_EXCEPTION
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
        )
    return user


def get_current_superuser(current_user: User = Depends(get_current_user)) -> User:
    """Require a superuser account."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Superuser privileges required"
        )
    return current_user


def rate_limit(request: Request) -> None:
    """Dependency that applies a fixed-window rate limit per client identity."""
    identity = _client_identity(request, prefix="api")

    allowed, _remaining = check_rate_limit(identity)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests, please slow down",
            headers={"Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)},
        )


def rate_limit_auth(request: Request) -> None:
    """Strict per-IP rate limit for credential endpoints.

    Login/register/refresh endpoints are the brute-force surface: a distinct,
    much smaller budget (default 10 requests/minute per IP) keeps password
    guessing from iterating at the general API pace. Fails open when Redis is
    unavailable, like the general limiter.
    """
    identity = _client_identity(request, prefix="auth")
    allowed, _remaining = check_rate_limit(
        identity,
        limit=settings.AUTH_RATE_LIMIT_REQUESTS,
        window=settings.AUTH_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts, please try again shortly",
            headers={"Retry-After": str(settings.AUTH_RATE_LIMIT_WINDOW_SECONDS)},
        )


def _client_identity(request: Request, prefix: str) -> str:
    """Build the rate-limit bucket key for a request.

    Combines the client IP with a stable digest of the bearer token so the
    per-token quota is per-token (not per-restart): builtin ``hash()`` is
    salted per process (PYTHONHASHSEED), so the same token used to produce a
    different bucket in every API worker/restart and the quota was effectively
    random. A stable SHA-256 digest fixes that.
    """
    identity = request.client.host if request.client else "anonymous"
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        digest = hashlib.sha256(auth_header.encode("utf-8")).hexdigest()[:16]
        identity = f"{identity}:{digest}"
    return f"{prefix}:{identity}"
