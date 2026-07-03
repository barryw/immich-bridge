"""Small cache facade backed by Redis when configured."""

import json
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from immich_bridge.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL = 300


class CacheBackend(Protocol):
    """Protocol for cache backends."""

    def get_json(self, key: str) -> dict[str, Any] | None: ...
    def set_json(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL) -> None: ...
    def get_int(self, key: str) -> int | None: ...
    def incr_with_ttl(self, key: str, ttl: int = DEFAULT_TTL) -> int: ...
    def clear(self) -> None: ...


@dataclass
class CacheEntry:
    """A cached item with expiration."""

    value: dict[str, Any]
    expires_at: float


class InMemoryCache:
    """Thread-safe in-memory cache for single-instance deployments."""

    def __init__(self) -> None:
        self._items: dict[str, CacheEntry] = {}
        self._lock = Lock()

    def get_json(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if time.time() > entry.expires_at:
                del self._items[key]
                return None
            logger.debug("cache_hit", key=key, backend="memory")
            return entry.value

    def set_json(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL) -> None:
        with self._lock:
            self._items[key] = CacheEntry(value=value, expires_at=time.time() + ttl)
        logger.debug("cache_set", key=key, backend="memory")

    def get_int(self, key: str) -> int | None:
        """Return an integer counter value."""
        cached = self.get_json(key)
        raw_count = cached.get("count") if cached else None
        return raw_count if isinstance(raw_count, int) else None

    def incr_with_ttl(self, key: str, ttl: int = DEFAULT_TTL) -> int:
        """Increment a small counter and expire it after ttl seconds."""
        with self._lock:
            now = time.time()
            entry = self._items.get(key)
            if entry is None or now > entry.expires_at:
                value = 1
                expires_at = now + ttl
            else:
                raw_count = entry.value.get("count")
                value = int(raw_count) + 1 if isinstance(raw_count, int) else 1
                expires_at = entry.expires_at
            self._items[key] = CacheEntry(value={"count": value}, expires_at=expires_at)
            return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
        logger.info("cache_cleared", backend="memory")


class RedisCache:
    """Redis-backed cache for shared state across app instances."""

    def __init__(
        self,
        host: str,
        port: int = 6379,
        db: int = 0,
        password: str | None = None,
    ) -> None:
        import redis

        self._redis = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password or None,
            decode_responses=True,
        )
        self._prefix = "immich-bridge:cache:"
        logger.info("redis_cache_initialized", host=host, port=port, db=db)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get_json(self, key: str) -> dict[str, Any] | None:
        try:
            value = self._redis.get(self._key(key))
            if value is None:
                return None
            logger.debug("cache_hit", key=key, backend="redis")
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except Exception as e:
            logger.warning("redis_cache_error", operation="get_json", error=str(e))
            return None

    def set_json(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL) -> None:
        try:
            self._redis.setex(self._key(key), ttl, json.dumps(value))
            logger.debug("cache_set", key=key, backend="redis")
        except Exception as e:
            logger.warning("redis_cache_error", operation="set_json", error=str(e))

    def get_int(self, key: str) -> int | None:
        """Return an integer counter value."""
        try:
            value = self._redis.get(self._key(key))
            if value is None:
                return None
            return int(value)
        except Exception as e:
            logger.warning("redis_cache_error", operation="get_int", error=str(e))
            return None

    def incr_with_ttl(self, key: str, ttl: int = DEFAULT_TTL) -> int:
        """Increment a small counter and expire it after ttl seconds."""
        try:
            redis_key = self._key(key)
            pipe = self._redis.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            count, current_ttl = pipe.execute()
            if int(current_ttl) < 0:
                self._redis.expire(redis_key, ttl)
            return int(count)
        except Exception as e:
            logger.warning("redis_cache_error", operation="incr_with_ttl", error=str(e))
            return 1

    def clear(self) -> None:
        try:
            cursor = 0
            while True:
                cursor, keys = self._redis.scan(cursor, match=f"{self._prefix}*", count=100)
                if keys:
                    self._redis.delete(*keys)
                if cursor == 0:
                    break
            logger.info("cache_cleared", backend="redis")
        except Exception as e:
            logger.warning("redis_cache_error", operation="clear", error=str(e))


_cache: CacheBackend | None = None
_cache_lock = Lock()


def init_cache(
    redis_host: str | None = None,
    redis_port: int = 6379,
    redis_db: int = 0,
    redis_password: str | None = None,
) -> None:
    """Initialize the global cache."""
    global _cache
    with _cache_lock:
        if redis_host:
            _cache = RedisCache(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                password=redis_password,
            )
        else:
            _cache = InMemoryCache()
            logger.info("in_memory_cache_initialized")


def get_cache() -> CacheBackend:
    """Get the global cache instance."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = InMemoryCache()
                logger.info("in_memory_cache_initialized_default")
    return _cache
