"""Password hashing and JWT creation / verification helpers."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from jose import JWTError, jwt
from passlib.context import CryptContext

from core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        return False


def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    secret_key: str,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret_key, algorithm=settings.ALGORITHM)


def create_access_token(
    subject: str,
    expires_delta: Optional[timedelta] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a short lived JWT access token."""
    return _create_token(
        subject=subject,
        token_type=ACCESS_TOKEN_TYPE,
        expires_delta=expires_delta
        or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        secret_key=settings.SECRET_KEY,
        extra_claims=extra_claims,
    )


def create_refresh_token(
    subject: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Create a long lived JWT refresh token."""
    return _create_token(
        subject=subject,
        token_type=REFRESH_TOKEN_TYPE,
        expires_delta=expires_delta
        or timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        secret_key=settings.REFRESH_SECRET_KEY,
    )


def decode_token(token: str, token_type: str = ACCESS_TOKEN_TYPE) -> Dict[str, Any]:
    """Decode a JWT and validate its type.

    Raises:
        JWTError: when the token is invalid, expired or of the wrong type.
    """
    secret_key = (
        settings.SECRET_KEY
        if token_type == ACCESS_TOKEN_TYPE
        else settings.REFRESH_SECRET_KEY
    )
    payload = jwt.decode(token, secret_key, algorithms=[settings.ALGORITHM])
    if payload.get("type") != token_type:
        raise JWTError(f"Invalid token type, expected '{token_type}'")
    if not payload.get("sub"):
        raise JWTError("Token is missing the 'sub' claim")
    return payload
