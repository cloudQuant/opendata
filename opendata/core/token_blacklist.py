"""
Token blacklist for logout support.

Supports two backends:
- **Redis** (recommended for production): works across multiple workers/processes
  and survives restarts.  Enabled automatically when REDIS_URL is set.
- **In-memory** (fallback): thread-safe dict with TTL-based auto-cleanup.
  Does NOT persist across restarts or share state between workers.
"""

import abc
import hashlib
import threading
import time

from loguru import logger

# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class _TokenBlacklistBackend(abc.ABC):
    """Common interface for token blacklist backends."""

    @abc.abstractmethod
    def revoke(self, token: str, expires_at: float | None = None) -> None: ...

    @abc.abstractmethod
    def is_revoked(self, token: str) -> bool: ...

    @abc.abstractmethod
    def cleanup(self) -> int: ...

    @abc.abstractmethod
    def size(self) -> int: ...

    @abc.abstractmethod
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# In-memory backend (original implementation)
# ---------------------------------------------------------------------------


class _InMemoryBlacklist(_TokenBlacklistBackend):
    """Thread-safe in-memory token blacklist with TTL-based auto-cleanup."""

    _CLEANUP_INTERVAL = 300  # 5 minutes

    def __init__(self) -> None:
        self._blacklist: dict[str, float] = {}  # token_hash -> expiry ts
        self._lock = threading.Lock()
        self._cleanup_timer: threading.Timer | None = None
        self._start_cleanup_loop()

    # -- helpers --

    @staticmethod
    def _hash(token: str) -> str:
        """Store a SHA-256 hash instead of the raw token for safety."""
        return hashlib.sha256(token.encode()).hexdigest()

    def _start_cleanup_loop(self) -> None:
        self._cleanup_timer = threading.Timer(self._CLEANUP_INTERVAL, self._periodic_cleanup)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _periodic_cleanup(self) -> None:
        try:
            removed = self.cleanup()
            if removed:
                logger.debug(f"Token blacklist cleanup: removed {removed} expired entries")
        except Exception as e:
            logger.error(f"Token blacklist cleanup error: {e}")
        finally:
            self._start_cleanup_loop()

    # -- public API --

    def revoke(self, token: str, expires_at: float | None = None) -> None:
        if expires_at is None:
            expires_at = time.time() + 86400
        with self._lock:
            self._blacklist[self._hash(token)] = expires_at

    def is_revoked(self, token: str) -> bool:
        h = self._hash(token)
        with self._lock:
            exp = self._blacklist.get(h)
            if exp is None:
                return False
            if exp < time.time():
                del self._blacklist[h]
                return False
            return True

    def cleanup(self) -> int:
        now = time.time()
        with self._lock:
            expired = [t for t, exp in self._blacklist.items() if exp < now]
            for t in expired:
                del self._blacklist[t]
            return len(expired)

    def size(self) -> int:
        return len(self._blacklist)

    def stop(self) -> None:
        if self._cleanup_timer is not None:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


class _RedisBlacklist(_TokenBlacklistBackend):
    """Redis-backed token blacklist.  Uses SET + TTL for auto-expiry."""

    _KEY_PREFIX = "tkbl:"  # token-blacklist namespace

    def __init__(self, redis_url: str) -> None:
        import redis

        self._redis = redis.from_url(redis_url, decode_responses=True)
        # Verify connectivity
        self._redis.ping()
        logger.info("Token blacklist using Redis backend")

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _key(self, token: str) -> str:
        return f"{self._KEY_PREFIX}{self._hash(token)}"

    def revoke(self, token: str, expires_at: float | None = None) -> None:
        ttl = int((expires_at or (time.time() + 86400)) - time.time())
        if ttl <= 0:
            return  # Already expired, no point blacklisting
        self._redis.setex(self._key(token), ttl, "1")

    def is_revoked(self, token: str) -> bool:
        return bool(self._redis.exists(self._key(token)) > 0)

    def cleanup(self) -> int:
        # Redis handles expiry automatically via TTL; nothing to do.
        return 0

    def size(self) -> int:
        # Count keys with our prefix (approximate).
        cursor, keys = self._redis.scan(match=f"{self._KEY_PREFIX}*", count=1000)
        count = len(keys)
        while cursor:
            cursor, batch = self._redis.scan(
                cursor=cursor, match=f"{self._KEY_PREFIX}*", count=1000
            )
            count += len(batch)
        return count

    def stop(self) -> None:
        try:
            self._redis.close()
        except Exception as e:
            logger.debug("Redis close failed (may already be closed): %s", e)


# ---------------------------------------------------------------------------
# Factory - pick the best available backend
# ---------------------------------------------------------------------------


def _create_blacklist() -> _TokenBlacklistBackend:
    """Create the best available token blacklist backend."""
    from opendata.core.config import settings

    if settings.redis_url:
        try:
            return _RedisBlacklist(settings.redis_url)
        except Exception as e:
            logger.warning(f"Redis unavailable ({e}), falling back to in-memory token blacklist")

    logger.info("Token blacklist using in-memory backend (set REDIS_URL for multi-worker support)")

    # Warn if running multiple workers without Redis
    if settings.workers > 1 and settings.is_production:
        logger.warning(
            f"⚠️  Running {settings.workers} workers WITHOUT Redis! "
            "In-memory token blacklist does NOT share state across workers. "
            "Logout tokens will only be revoked in the worker that processed the logout. "
            "Set REDIS_URL to fix this."
        )

    return _InMemoryBlacklist()


# Global singleton
token_blacklist: _TokenBlacklistBackend = _create_blacklist()
