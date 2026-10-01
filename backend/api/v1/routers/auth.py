"""Authentication routes: register, OAuth2 token, refresh and profile."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import ExpiredSignatureError, JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_current_user, rate_limit_auth
from core.config import settings
from core.redis_client import cache_delete, cache_get_json, cache_set_json
from core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from db.session import get_db
from models import User
from schemas.auth import (
    LoginRequest,
    RefreshTokenRequest,
    RegisterRequest,
    Token,
    UserRead,
    UserUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"])

BLACKLIST_TTL = settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60


def _issue_tokens(user: User) -> Token:
    """Create an access/refresh token pair for a user."""
    access_token = create_access_token(
        subject=str(user.id), extra_claims={"email": user.email}
    )
    refresh_token = create_refresh_token(subject=str(user.id))
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _authenticate(db: Session, email: str, password: str) -> User:
    """Validate credentials and return the matching active user."""
    user = db.scalar(select(User).where(User.email == email.lower().strip()))
    if user is None or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
        )
    return user


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new account",
    dependencies=[Depends(rate_limit_auth)],
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> UserRead:
    """Register a new user account."""
    email = payload.email.lower().strip()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    user = User(
        email=email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Registered new user %s", user.email)
    return UserRead.model_validate(user)


@router.post(
    "/token",
    response_model=Token,
    summary="OAuth2 password bearer login",
    dependencies=[Depends(rate_limit_auth)],
)
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    """Authenticate with the OAuth2 password flow and return JWTs.

    ``username`` accepts the account email address.
    """
    user = _authenticate(db, form_data.username, form_data.password)
    tokens = _issue_tokens(user)
    cache_set_json(
        f"session:{user.id}",
        {"email": user.email, "token_suffix": tokens.access_token[-16:]},
        ttl=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return tokens


@router.post(
    "/login",
    response_model=Token,
    summary="JSON login",
    dependencies=[Depends(rate_limit_auth)],
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> Token:
    """JSON-friendly alternative to ``/auth/token`` used by the SPA."""
    user = _authenticate(db, payload.email, payload.password)
    cache_set_json(
        f"session:{user.id}",
        {"email": user.email},
        ttl=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return _issue_tokens(user)


@router.post(
    "/refresh",
    response_model=Token,
    summary="Exchange a refresh token",
    dependencies=[Depends(rate_limit_auth)],
)
def refresh_access_token(
    payload: RefreshTokenRequest, db: Session = Depends(get_db)
) -> Token:
    """Issue a new token pair from a valid refresh token (with rotation)."""
    try:
        claims = decode_token(payload.refresh_token, token_type=REFRESH_TOKEN_TYPE)
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired, please sign in again",
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    token_id = claims.get("jti")
    if token_id and cache_get_json(f"revoked:{token_id}"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    user = db.scalar(select(User).where(User.id == int(claims["sub"])))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer active"
        )

    # Rotate: blacklist the used refresh token for its remaining lifetime.
    if token_id:
        cache_set_json(f"revoked:{token_id}", {"user_id": user.id}, ttl=BLACKLIST_TTL)

    return _issue_tokens(user)


@router.post(
    "/logout",
    summary="Revoke the current refresh token",
    dependencies=[Depends(rate_limit_auth)],
)
def logout(payload: RefreshTokenRequest) -> dict:
    """Blacklist a refresh token so it cannot be reused."""
    try:
        claims = decode_token(payload.refresh_token, token_type=REFRESH_TOKEN_TYPE)
        token_id = claims.get("jti")
        if token_id:
            cache_set_json(
                f"revoked:{token_id}", {"user_id": claims["sub"]}, ttl=BLACKLIST_TTL
            )
        cache_delete(f"session:{claims['sub']}")
    except (JWTError, KeyError):
        # Logging out an already invalid token is not an error for the client.
        pass
    return {"message": "Signed out"}


@router.get("/me", response_model=UserRead, summary="Current user profile")
def read_current_user(current_user: User = Depends(get_current_user)) -> UserRead:
    """Return the authenticated user's profile."""
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead, summary="Update the profile")
def update_current_user(
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserRead:
    """Update the display name and/or password of the authenticated user."""
    # ``get_current_user`` may return a detached Redis-cached snapshot; adding
    # that to the session would make SQLAlchemy treat it as a *new* row and
    # INSERT a duplicate primary key. Always mutate the persistent instance.
    user = db.get(User, current_user.id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.password:
        user.hashed_password = hash_password(payload.password)

    db.add(user)
    db.commit()
    db.refresh(user)
    cache_delete(f"user:{user.id}")
    return UserRead.model_validate(user)


@router.get(
    "/session",
    summary="Redis-backed session introspection",
    include_in_schema=False,
)
def session_info(current_user: User = Depends(get_current_user)) -> dict:
    """Report whether a cached session exists for the current user."""
    cached = cache_get_json(f"session:{current_user.id}")
    return {
        "user_id": current_user.id,
        "cached": cached is not None,
        "access_token_ttl_seconds": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "refresh_token_ttl_seconds": settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    }
