"""Safe opt-in disk cache for successful raw HTTP GET responses.

Entries contain response bytes and a small integrity-checked metadata header.
Request identity values are hashed before they are written to disk; cache
filenames contain only the final SHA-256 fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
import struct
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger(__name__)

RAW_RESPONSE_SCHEMA_VERSION = 1
_MAGIC = b"ODRAW1\n"
_LENGTH = struct.Struct(">I")
_MAX_METADATA_BYTES = 16 * 1024
_MAX_BODY_BYTES = 64 * 1024 * 1024
_SENSITIVE_NAME_PARTS = (
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "cookie",
)
_SAFE_ENCODING = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


@dataclass(frozen=True)
class CachedRawResponse:
    """Minimum response state needed to reconstruct a transport response."""

    status_code: int
    content: bytes
    encoding: str | None
    created_at: float
    expires_at: float


class _CacheSettings(Protocol):
    """Small settings surface used by the lazy application configuration hook."""

    raw_response_cache_enabled: bool
    cache_dir: Path
    cache_ttl_seconds: int


class RawResponseCache:
    """Atomic TTL cache rooted at ``CACHE_DIR/raw_responses``.

    Args:
        cache_dir: Configured cache root (the ``raw_responses`` subdirectory
            is added here to match the retention cleaner).
        ttl_seconds: Positive lifetime for each entry.
        schema_version: Bump when response interpretation changes.
        clock: Injectable wall clock for deterministic TTL tests.
    """

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        ttl_seconds: int,
        schema_version: str | int = RAW_RESPONSE_SCHEMA_VERSION,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize a cache rooted under the configured application cache directory."""
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if isinstance(schema_version, bool) or not isinstance(schema_version, (str, int)):
            raise ValueError("schema_version must be a non-empty string or positive integer")
        if isinstance(schema_version, str) and not schema_version.strip():
            raise ValueError("schema_version must be a non-empty string or positive integer")
        if isinstance(schema_version, int) and schema_version <= 0:
            raise ValueError("schema_version must be a non-empty string or positive integer")
        self.cache_dir = Path(os.path.abspath(Path(cache_dir).expanduser()))
        self.raw_dir = self.cache_dir / "raw_responses"
        self.ttl_seconds = ttl_seconds
        self.schema_version = schema_version
        self._clock = clock

    def key_for(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a SHA-256 cache key without exposing request values."""
        normalized_params = _normalize_params(endpoint, params)
        param_lookup = {name.lower(): values for name, values in normalized_params}
        identity = {
            "schema_version": self.schema_version,
            "method": "GET",
            "source": source,
            "endpoint": _query_free_endpoint(endpoint),
            # URL userinfo is removed from the endpoint identity, but must still
            # partition responses by credential without writing it to disk.
            "url_credentials_sha256": _url_credentials_digest(endpoint),
            "params": normalized_params,
            "adjust_basis": param_lookup.get("adjust_basis"),
            "as_of": param_lookup.get("as_of"),
            "headers": _normalize_mapping(headers or {}),
            "context": _normalize_mapping(context or {}),
        }
        encoded = json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def get(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CachedRawResponse | None:
        """Read a fresh entry; unsafe, expired, or damaged entries miss."""
        try:
            key = self.key_for(
                source=source,
                endpoint=endpoint,
                params=params,
                headers=headers,
                context=context,
            )
            directory_fd = self._open_directory(create=False)
            if directory_fd is None:
                return None
            try:
                return self._read_entry(directory_fd, key)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            self._warn("read_failed", exc)
            return None

    def put(
        self,
        *,
        method: str,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        status_code: int,
        content: bytes,
        encoding: str | None = None,
    ) -> bool:
        """Atomically store a successful raw response; failures are non-fatal."""
        if (
            method.upper() != "GET"
            or not 200 <= status_code < 300
            or len(content) > _MAX_BODY_BYTES
        ):
            return False
        try:
            key = self.key_for(
                source=source,
                endpoint=endpoint,
                params=params,
                headers=headers,
                context=context,
            )
            now = self._clock()
            if not math.isfinite(now):
                raise ValueError("invalid_cache_clock")
            safe_encoding = encoding if encoding and _SAFE_ENCODING.fullmatch(encoding) else None
            metadata = {
                "version": RAW_RESPONSE_SCHEMA_VERSION,
                "key": key,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "status_code": status_code,
                "body_size": len(content),
                "body_sha256": hashlib.sha256(content).hexdigest(),
                "encoding": safe_encoding,
            }
            metadata_bytes = json.dumps(
                metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(metadata_bytes) > _MAX_METADATA_BYTES:
                raise ValueError("metadata_too_large")
            record = _MAGIC + _LENGTH.pack(len(metadata_bytes)) + metadata_bytes + content
            directory_fd = self._open_directory(create=True)
            if directory_fd is None:  # pragma: no cover - create=True always returns a dir
                raise OSError("cache_directory_unavailable")
            try:
                return self._write_entry(directory_fd, key, record)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            self._warn("write_failed", exc)
            return False

    def invalidate(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Remove one unsafe-to-use entry without making transport fail."""
        try:
            key = self.key_for(
                source=source,
                endpoint=endpoint,
                params=params,
                headers=headers,
                context=context,
            )
            directory_fd = self._open_directory(create=False)
            if directory_fd is None:
                return
            try:
                info = os.stat(f"{key}.cache", dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    os.unlink(f"{key}.cache", dir_fd=directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            self._warn("invalidate_failed", exc)

    def _open_directory(self, *, create: bool) -> int | None:
        """Open the cache directory without following its final symlink."""
        if self.cache_dir.is_symlink():
            raise OSError("cache_root_is_symlink")
        if create:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
        elif not self.raw_dir.exists() and not self.raw_dir.is_symlink():
            return None
        if self.raw_dir.is_symlink():
            raise OSError("raw_response_directory_is_symlink")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(self.raw_dir, flags)
        if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
            os.close(directory_fd)
            raise OSError("raw_response_path_not_directory")
        return directory_fd

    def _read_entry(self, directory_fd: int, key: str) -> CachedRawResponse | None:
        filename = f"{key}.cache"
        try:
            info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            self._warn("entry_not_regular_file")
            return None
        if info.st_size > _MAX_BODY_BYTES + _MAX_METADATA_BYTES + len(_MAGIC) + _LENGTH.size:
            self._warn("entry_too_large")
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(filename, flags, dir_fd=directory_fd)
        except OSError as exc:
            self._warn("entry_open_failed", exc)
            return None
        with os.fdopen(file_fd, "rb") as entry_file:
            if not stat.S_ISREG(os.fstat(entry_file.fileno()).st_mode):
                self._warn("entry_not_regular_file")
                return None
            record = entry_file.read(_MAX_BODY_BYTES + _MAX_METADATA_BYTES + 64)
        return self._decode(record, key)

    def _decode(self, record: bytes, expected_key: str) -> CachedRawResponse | None:
        try:
            if len(record) < len(_MAGIC) + _LENGTH.size or not record.startswith(_MAGIC):
                raise ValueError("bad_magic")
            header_length = _LENGTH.unpack_from(record, len(_MAGIC))[0]
            if header_length <= 0 or header_length > _MAX_METADATA_BYTES:
                raise ValueError("bad_metadata_length")
            header_start = len(_MAGIC) + _LENGTH.size
            body_start = header_start + header_length
            metadata = json.loads(record[header_start:body_start].decode("utf-8"))
            body = record[body_start:]
            if not isinstance(metadata, dict):
                raise ValueError("metadata_not_mapping")
            status_code = metadata.get("status_code")
            created_at = metadata.get("created_at")
            expires_at = metadata.get("expires_at")
            body_size = metadata.get("body_size")
            if (
                isinstance(metadata.get("version"), bool)
                or metadata.get("version") != RAW_RESPONSE_SCHEMA_VERSION
                or metadata.get("key") != expected_key
                or isinstance(status_code, bool)
                or not isinstance(status_code, int)
                or not 200 <= status_code < 300
                or isinstance(body_size, bool)
                or not isinstance(body_size, int)
                or body_size != len(body)
                or body_size > _MAX_BODY_BYTES
                or isinstance(created_at, bool)
                or not isinstance(created_at, (int, float))
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or not math.isfinite(created_at)
                or not math.isfinite(expires_at)
                or not math.isclose(
                    expires_at,
                    created_at + self.ttl_seconds,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                )
                or metadata.get("body_sha256") != hashlib.sha256(body).hexdigest()
            ):
                raise ValueError("metadata_or_body_mismatch")
            now = self._clock()
            if not math.isfinite(now):
                raise ValueError("invalid_cache_clock")
            if now < created_at:
                self._warn("entry_created_in_future")
                return None
            if now >= expires_at:
                return None
            encoding = metadata.get("encoding")
            if encoding is not None and (
                not isinstance(encoding, str) or not _SAFE_ENCODING.fullmatch(encoding)
            ):
                raise ValueError("invalid_encoding")
            return CachedRawResponse(
                status_code=status_code,
                content=body,
                encoding=encoding,
                created_at=float(created_at),
                expires_at=float(expires_at),
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            struct.error,
            TypeError,
            ValueError,
        ) as exc:
            self._warn("entry_invalid", exc)
            return None

    def _write_entry(self, directory_fd: int, key: str, record: bytes) -> bool:
        filename = f"{key}.cache"
        try:
            existing = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            self._warn("entry_not_regular_file")
            return False
        temporary = f".{key}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(file_fd, "wb") as entry_file:
                entry_file.write(record)
                entry_file.flush()
                os.fsync(entry_file.fileno())
            os.replace(
                temporary,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
            return True
        except Exception:
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory_fd)
            raise

    @staticmethod
    def _warn(reason: str, exc: Exception | None = None) -> None:
        """Warn without including URLs, params, credentials, or exception text."""
        logger.warning(
            "raw response cache safety miss",
            extra={
                "event": "raw_response_cache_warning",
                "reason": reason,
                "error_type": exc.__class__.__name__ if exc is not None else None,
            },
        )


_configured_lock = threading.Lock()
_configured_cache: RawResponseCache | None = None
_configured_cache_key: tuple[str, int] | None = None


def get_configured_raw_response_cache() -> RawResponseCache | None:
    """Return the process cache only when the explicit setting enables it."""
    global _configured_cache, _configured_cache_key
    try:
        settings = _load_application_settings()
        if not settings.raw_response_cache_enabled:
            return None
        cache_dir = str(settings.cache_dir)
        ttl_seconds = settings.cache_ttl_seconds
        cache_key = (cache_dir, ttl_seconds)
        with _configured_lock:
            if _configured_cache is None or _configured_cache_key != cache_key:
                _configured_cache = RawResponseCache(cache_dir, ttl_seconds=ttl_seconds)
                _configured_cache_key = cache_key
            return _configured_cache
    except Exception as exc:
        RawResponseCache._warn("configuration_unavailable", exc)
        return None


def _load_application_settings() -> _CacheSettings:
    """Load optional application config only when a transport actually asks."""
    from opendata.core.config import get_settings

    return get_settings()


def _query_free_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    host = parts.hostname or "unknown"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path or '/'}"


def _url_credentials_digest(endpoint: str) -> str | None:
    """Hash URL userinfo so direct cache users cannot share authenticated data."""
    parts = urlsplit(endpoint)
    if "@" not in parts.netloc:
        return None
    userinfo = parts.netloc.rsplit("@", 1)[0]
    return hashlib.sha256(userinfo.encode("utf-8")).hexdigest()


def _normalize_params(
    endpoint: str, params: Mapping[str, Any] | None
) -> list[tuple[str, list[Any]]]:
    pairs = parse_qsl(urlsplit(endpoint).query, keep_blank_values=True)
    for key, value in (params or {}).items():
        if isinstance(value, (list, tuple)):
            pairs.extend((str(key), item) for item in value)
        else:
            pairs.append((str(key), value))
    grouped: dict[str, list[Any]] = {}
    for raw_key, raw_value in pairs:
        key = str(raw_key)
        normalized = _normalize_value(raw_value)
        if _is_sensitive_name(key):
            normalized = {"sha256": _digest_value(normalized)}
        grouped.setdefault(key, []).append(normalized)
    return [(key, grouped[key]) for key in sorted(grouped)]


def _normalize_mapping(values: Mapping[str, Any]) -> list[tuple[str, Any]]:
    normalized: list[tuple[str, Any]] = []
    for raw_key, raw_value in values.items():
        key = str(raw_key)
        value = _normalize_value(raw_value)
        if _is_sensitive_name(key):
            value = {"sha256": _digest_value(value)}
        normalized.append((key.lower(), value))
    return sorted(normalized, key=lambda item: item[0])


def _normalize_value(value: object) -> object:
    if isinstance(value, Enum):
        return _normalize_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: str(item[0]))
        return {str(key): _normalize_value(item) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_sensitive_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_NAME_PARTS)


def _digest_value(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CachedRawResponse",
    "RAW_RESPONSE_SCHEMA_VERSION",
    "RawResponseCache",
    "get_configured_raw_response_cache",
]
