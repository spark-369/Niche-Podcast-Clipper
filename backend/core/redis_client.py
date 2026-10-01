"""Redis client, caching helpers and a lightweight fixed-window rate limiter."""

import json
import logging
from typing import Any, Optional

import redis

from core.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[redis.ConnectionPool] = None


def get_redis() -> redis.Redis:
    """Return a lazily initialised Redis client backed by a shared pool."""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=32,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
    return redis.Redis(connection_pool=_pool)


def redis_available() -> bool:
    """Check whether Redis is reachable (used for health reporting)."""
    try:
        return bool(get_redis().ping())
    except redis.RedisError:
        return False


def cache_get_json(key: str) -> Optional[Any]:
    """Fetch and deserialize a JSON value; returns `None` on miss or failure."""
    try:
        raw = get_redis().get(key)
    except redis.RedisError as exc:  # pragma: no cover - resilience path
        logger.warning("Redis GET failed for %s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupted cache payload for %s", key)
        return None


def cache_set_json(key: str, value: Any, ttl: Optional[int] = None) -> bool:
    """Serialize and store a JSON value with an optional TTL."""
    try:
        get_redis().set(
            key,
            json.dumps(value, default=str),
            ex=ttl or settings.CACHE_TTL_SECONDS,
        )
        return True
    except (redis.RedisError, TypeError) as exc:  # pragma: no cover
        logger.warning("Redis SET failed for %s: %s", key, exc)
        return False


def cache_delete(*keys: str) -> int:
    """Delete one or more cache keys."""
    if not keys:
        return 0
    try:
        return int(get_redis().delete(*keys))
    except redis.RedisError as exc:  # pragma: no cover
        logger.warning("Redis DEL failed for %s: %s", keys, exc)
        return 0


def cache_delete_prefix(prefix: str) -> int:
    """Delete every key matching `prefix*` using a non blocking SCAN loop."""
    deleted = 0
    try:
        client = get_redis()
        for key in client.scan_iter(match=f"{prefix}*", count=500):
            client.delete(key)
            deleted += 1
    except redis.RedisError as exc:  # pragma: no cover
        logger.warning("Redis SCAN/DEL failed for %s: %s", prefix, exc)
    return deleted


def check_rate_limit(
    identifier: str, limit: Optional[int] = None, window: Optional[int] = None
) -> tuple[bool, int]:
    """Fixed window rate limiter.

    Returns:
        (allowed, remaining) where `allowed` is False when the caller exceeded
        the quota. Fails open when Redis is unavailable.
    """
    limit = limit or settings.RATE_LIMIT_REQUESTS
    window = window or settings.RATE_LIMIT_WINDOW_SECONDS
    key = f"ratelimit:{identifier}"
    try:
        client = get_redis()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if ttl is None or ttl < 0:
            client.expire(key, window)
            ttl = window
        return count <= limit, max(limit - int(count), 0)
    except redis.RedisError as exc:  # pragma: no cover - fail open
        logger.warning("Rate limiter unavailable: %s", exc)
        return True, limit
