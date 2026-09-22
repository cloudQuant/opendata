"""
Simple in-memory TTL cache for high-frequency read endpoints.

Thread-safe, lightweight alternative to Redis for single-process deployments.
"""

import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe in-memory cache with per-key TTL."""

    def __init__(self, default_ttl: int = 60) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expiry)
        self._lock = threading.Lock()
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:  # noqa: ANN401
        """Get value if key exists and hasn't expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expiry = entry
            if time.time() > expiry:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:  # noqa: ANN401
        """Set value with TTL in seconds."""
        expiry = time.time() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._store[key] = (value, expiry)

    def invalidate(self, key: str) -> None:
        """Remove a specific key."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        """Return number of entries (including possibly expired)."""
        return len(self._store)


# Shared cache instance (60s default TTL)
api_cache = TTLCache(default_ttl=60)
