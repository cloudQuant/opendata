"""Build an explicitly enabled, read-only temporal identity API runtime.

The runtime is disabled by default and reads no settings or environment. Enabling
it requires an operator-owned versioned pin file and a SHA-256 digest delivered
through an independent trusted configuration channel. The digest binds exact
bytes but is not a signature or proof of source authority or completeness.
Warehouse tables must already exist; startup validates their columns with
SELECT statements only.

SQLite connections use both ``mode=ro`` and ``PRAGMA query_only``. MySQL URLs
require ``ssl_ca``, ``ssl_verify_cert=true``, and ``ssl_verify_identity=true``.
The factory removes these reviewed options from the URL and binds them directly
as PyMySQL arguments so the CA and both verification flags reach the driver.
Other URL query options are rejected because some run SQL before checkout
guards.
Connections are put in read-only transaction mode, but this code cannot inspect
or prove account grants. Production MySQL credentials must belong to a dedicated
account whose database grants are SELECT-only.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

from opendata.api.temporal_identity import (
    MAX_TEMPORAL_IDENTITY_PINS,
    TemporalIdentityPin,
    TemporalIdentityPinRegistry,
)
from opendata.data.temporal_identity_store import TemporalIdentitySnapshotStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry


class _DBAPICursor(Protocol):
    """Describe the DB-API cursor operations used by read-only checkout hooks."""

    def execute(self, statement: str) -> object:
        """Execute one fixed session statement."""

    def fetchone(self) -> tuple[object, ...] | None:
        """Return a single synthetic or driver row."""

    def close(self) -> None:
        """Release cursor resources."""


class _DBAPIConnection(Protocol):
    """Describe the cursor factory needed by read-only checkout hooks."""

    def cursor(self) -> _DBAPICursor:
        """Create a cursor for fixed read-only session configuration."""


MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 40_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PIN_FIELDS = frozenset(
    {
        "dataset_id",
        "source_id",
        "authority_id",
        "registry_id",
        "reference_version",
        "snapshot_sha256",
    }
)
_ROOT_FIELDS = frozenset({"schema_version", "pins"})


class TemporalIdentityRuntimeError(RuntimeError):
    """Report a fixed, content-free runtime configuration failure."""

    def __init__(self, code: str) -> None:
        """Create an error with a stable public code and no configuration details."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TemporalIdentityRuntimeConfig:
    """Explicit inputs for enabling the temporal identity reader.

    Supply exactly one warehouse target: ``warehouse_engine`` for an injected
    synchronous SQLAlchemy engine, ``warehouse_sqlite_path`` for a SQLite file,
    or ``warehouse_url`` for a MySQL URL. An injected engine is a trusted
    capability: it must be dedicated to this reader and configured without
    write-capable credentials, startup SQL, or code that shares or changes its
    sessions. This runtime cannot audit an injected engine's creator,
    ``connect_args``, credentials, TLS, or external references. Injected SQLite
    engines must at least declare ``mode=ro`` and ``uri=true`` in their URL.
    MySQL URLs require verified TLS and an operator acknowledgement that their
    account has only SELECT grants; this runtime does not verify those grants.
    """

    enabled: bool = False
    pin_file: str | Path | None = field(default=None, repr=False)
    pin_sha256: str | None = field(default=None, repr=False)
    warehouse_engine: Engine | None = field(default=None, repr=False, compare=False)
    warehouse_sqlite_path: str | Path | None = None
    warehouse_url: str | None = field(default=None, repr=False, compare=False)
    mysql_select_only_account_acknowledged: bool = False


@dataclass(slots=True)
class TemporalIdentityPinLookup:
    """Expose only exact ``get`` lookup to consumers that do not need pin lists."""

    _registry: TemporalIdentityPinRegistry = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def get(self, dataset_id: str) -> TemporalIdentityPin | None:
        """Return the one exact reviewed pin for a dataset, if present."""
        if self._closed:
            raise TemporalIdentityRuntimeError("runtime_closed")
        return self._registry.get(dataset_id)

    def close(self) -> None:
        """Revoke a saved facade reference when its runtime shuts down."""
        self._closed = True


@dataclass(slots=True)
class TemporalIdentitySnapshotLookup:
    """Expose snapshot reads without forwarding the store's write methods."""

    _store: TemporalIdentitySnapshotStore = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> TemporalIdentityRegistry:
        """Read exactly one caller-pinned snapshot through the immutable store."""
        if self._closed:
            raise TemporalIdentityRuntimeError("runtime_closed")
        return self._store.get(
            authority_id=authority_id,
            registry_id=registry_id,
            reference_version=reference_version,
            expected_sha256=expected_sha256,
        )

    def close(self) -> None:
        """Revoke a saved facade reference when its runtime shuts down."""
        self._closed = True


@dataclass(slots=True)
class TemporalIdentityRuntime:
    """Hold the read-only store and release an owned engine on shutdown."""

    enabled: bool
    pin_registry: TemporalIdentityPinRegistry | None = field(default=None, repr=False)
    pin_lookup: TemporalIdentityPinLookup | None = None
    snapshot_store: TemporalIdentitySnapshotLookup | None = field(default=None, repr=False)
    _engine: Engine | None = field(default=None, repr=False)
    _owns_engine: bool = False
    _checkout_guard: Callable[..., None] | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Detach the guard, revoke runtime lookups, and dispose only owned engines."""
        if self._closed:
            return
        self._closed = True
        if self.pin_lookup is not None:
            self.pin_lookup.close()
        if self.snapshot_store is not None:
            self.snapshot_store.close()
        if self._engine is not None and self._checkout_guard is not None:
            with suppress(Exception):
                event.remove(self._engine.pool, "checkout", self._checkout_guard)
        if self._engine is not None and self._owns_engine:
            with suppress(Exception):
                self._engine.dispose()
        self.pin_registry = None
        self.pin_lookup = None
        self.snapshot_store = None
        self._engine = None
        self._owns_engine = False
        self._checkout_guard = None

    def __enter__(self) -> TemporalIdentityRuntime:
        """Return this runtime for a synchronous lifespan context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Release owned database resources when leaving a lifespan context."""
        self.close()


def create_temporal_identity_runtime(
    config: TemporalIdentityRuntimeConfig | None = None,
) -> TemporalIdentityRuntime:
    """Create a disabled runtime or validate and bind an enabled read-only runtime.

    The disabled branch does not inspect the pin path, read environment, build
    an engine, or connect to a database. The enabled branch loads and verifies
    the complete pin file before constructing the warehouse engine.

    Args:
        config: Explicit deployment-owned settings. ``None`` means disabled.

    Returns:
        A runtime whose ``pin_registry`` is the exact registry type accepted by
        the existing route, while ``pin_lookup`` offers a restricted ``get`` API.

    Raises:
        TemporalIdentityRuntimeError: If paths, pins, connection settings, or
            pre-existing warehouse schema fail their fixed checks.
    """
    if config is None:
        return TemporalIdentityRuntime(enabled=False)
    if type(config.enabled) is not bool:
        raise TemporalIdentityRuntimeError("runtime_config_invalid")
    if not config.enabled:
        return TemporalIdentityRuntime(enabled=False)

    pin_path, expected_sha256 = _validate_pin_config(config)
    raw_pins = _read_operator_pin_file(pin_path)
    pin_registry = _decode_pin_registry(raw_pins, expected_sha256)
    engine, owns_engine = _build_engine(config)
    checkout_guard = _readonly_checkout_guard(engine.dialect.name)
    try:
        event.listen(engine.pool, "checkout", checkout_guard)
        _validate_existing_schema(engine)
        store = TemporalIdentitySnapshotLookup(TemporalIdentitySnapshotStore(engine))
    except Exception:
        with suppress(Exception):
            event.remove(engine.pool, "checkout", checkout_guard)
        if owns_engine:
            with suppress(Exception):
                engine.dispose()
        raise TemporalIdentityRuntimeError("warehouse_unavailable") from None

    return TemporalIdentityRuntime(
        enabled=True,
        pin_registry=pin_registry,
        pin_lookup=TemporalIdentityPinLookup(pin_registry),
        snapshot_store=store,
        _engine=engine,
        _owns_engine=owns_engine,
        _checkout_guard=checkout_guard,
    )


def _validate_pin_config(config: TemporalIdentityRuntimeConfig) -> tuple[Path, str]:
    """Validate required independent pin inputs without including them in errors."""
    if type(config.pin_sha256) is not str or _SHA256.fullmatch(config.pin_sha256) is None:
        raise TemporalIdentityRuntimeError("pin_digest_invalid")
    if not isinstance(config.pin_file, (str, Path)) or isinstance(config.pin_file, bool):
        raise TemporalIdentityRuntimeError("pin_path_invalid")
    try:
        path = _validated_absolute_path(config.pin_file)
    except (TypeError, ValueError, OSError):
        raise TemporalIdentityRuntimeError("pin_path_invalid") from None
    return path, config.pin_sha256


def _validated_absolute_path(raw_path: str | Path) -> Path:
    """Require an unambiguous absolute POSIX path without traversal components."""
    raw = str(raw_path)
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or not raw.startswith("/")
        or raw.startswith("//")
        or raw.endswith("/")
    ):
        raise ValueError("unsafe path")
    components = raw[1:].split("/")
    if not components or any(not part or part in {".", ".."} for part in components):
        raise ValueError("unsafe path")
    return Path(raw)


def _read_operator_pin_file(path: Path) -> bytes:
    """Read one bounded private regular file through no-follow directory FDs."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_CLOEXEC", "geteuid")
    if any(not hasattr(os, name) for name in required):
        raise TemporalIdentityRuntimeError("pin_platform_unsupported")

    directory_fds: list[int] = []
    file_fd: int | None = None
    snapshots: list[os.stat_result] = []
    try:
        directory_fd = os.open(
            os.sep,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        directory_fds.append(directory_fd)
        snapshots.append(os.fstat(directory_fd))
        _check_directory(snapshots[-1], is_final=False)

        components = path.parts[1:]
        for index, name in enumerate(components[:-1]):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fds[-1],
            )
            directory_fds.append(child_fd)
            snapshots.append(os.fstat(child_fd))
            _check_directory(snapshots[-1], is_final=index == len(components) - 2)

        parent_fd = directory_fds[-1]
        filename = components[-1]
        before_named = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        _check_pin_file(before_named, expected_size=None)
        file_fd = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        before = os.fstat(file_fd)
        _check_pin_file(before, expected_size=before_named.st_size)
        if not _same_file(before_named, before):
            raise TemporalIdentityRuntimeError("pin_file_changed")
        if before.st_size <= 0 or before.st_size > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
            raise TemporalIdentityRuntimeError("pin_file_size_invalid")

        content = bytearray()
        while len(content) <= MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
            chunk = os.read(
                file_fd,
                min(64 * 1024, MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(file_fd)
        current_named = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if (
            len(content) != before.st_size
            or not _same_file(before, after)
            or not _same_file(after, current_named)
        ):
            raise TemporalIdentityRuntimeError("pin_file_changed")

        _verify_directory_bindings(directory_fds, snapshots, components[:-1])
        return bytes(content)
    except TemporalIdentityRuntimeError:
        raise
    except OSError as exc:
        code = "pin_symlink_rejected" if exc.errno == errno.ELOOP else "pin_file_unavailable"
        raise TemporalIdentityRuntimeError(code) from None
    finally:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
        for descriptor in reversed(directory_fds):
            with suppress(OSError):
                os.close(descriptor)


def _check_directory(info: os.stat_result, *, is_final: bool) -> None:
    """Reject untrusted or writable parent directories, allowing root sticky ancestors."""
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
        raise TemporalIdentityRuntimeError("pin_path_unsafe")
    writable = bool(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    root_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if writable and not root_sticky:
        raise TemporalIdentityRuntimeError("pin_path_unsafe")
    if is_final and (info.st_uid != os.geteuid() or writable):
        raise TemporalIdentityRuntimeError("pin_path_unsafe")


def _check_pin_file(info: os.stat_result, *, expected_size: int | None) -> None:
    """Require one owner-only, singly linked regular pin file."""
    if stat.S_ISLNK(info.st_mode):
        raise TemporalIdentityRuntimeError("pin_symlink_rejected")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TemporalIdentityRuntimeError("pin_file_not_regular")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise TemporalIdentityRuntimeError("pin_file_permissions_unsafe")
    if expected_size is not None and info.st_size != expected_size:
        raise TemporalIdentityRuntimeError("pin_file_changed")
    if info.st_size <= 0 or info.st_size > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
        raise TemporalIdentityRuntimeError("pin_file_size_invalid")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identity, access mode and mutation timestamps for an opened file."""
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_uid",
        "st_gid",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(left, name) == getattr(right, name) for name in fields)


def _verify_directory_bindings(
    descriptors: list[int], snapshots: list[os.stat_result], names: tuple[str, ...]
) -> None:
    """Ensure every path component still identifies its originally opened directory."""
    for index, descriptor in enumerate(descriptors):
        before = snapshots[index]
        after = os.fstat(descriptor)
        if not _same_directory(before, after):
            raise TemporalIdentityRuntimeError("pin_path_changed")
        if index == 0:
            continue
        named = os.stat(names[index - 1], dir_fd=descriptors[index - 1], follow_symlinks=False)
        if not _same_directory(after, named):
            raise TemporalIdentityRuntimeError("pin_path_changed")


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare stable owner and inode metadata for one path directory."""
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid")
    return stat.S_ISDIR(right.st_mode) and all(
        getattr(left, name) == getattr(right, name) for name in fields
    )


def _decode_pin_registry(raw: bytes, expected_sha256: str) -> TemporalIdentityPinRegistry:
    """Check an independent digest and decode bounded version-one pin JSON."""
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise TemporalIdentityRuntimeError("pin_digest_mismatch")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        _check_json_budget(document)
        _check_json_unicode(document)
        root = _require_object(document, _ROOT_FIELDS)
        if type(root["schema_version"]) is not int or root["schema_version"] != 1:
            raise TemporalIdentityRuntimeError("pin_document_invalid")
        pins_value = root["pins"]
        if type(pins_value) is not list or not 1 <= len(pins_value) <= MAX_TEMPORAL_IDENTITY_PINS:
            raise TemporalIdentityRuntimeError("pin_count_invalid")
        pins: list[TemporalIdentityPin] = []
        for value in pins_value:
            item = _require_object(value, _PIN_FIELDS)
            pins.append(TemporalIdentityPin.model_validate(item, strict=True))
        return TemporalIdentityPinRegistry(tuple(pins))
    except TemporalIdentityRuntimeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise TemporalIdentityRuntimeError("pin_document_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at every JSON object depth."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Reject non-standard NaN and Infinity JSON values."""
    raise ValueError("non-standard JSON constant")


def _check_json_budget(document: object) -> None:
    """Bound JSON nesting and node count before schema validation."""
    stack: list[tuple[object, int]] = [(document, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > _MAX_JSON_DEPTH or nodes > _MAX_JSON_NODES:
            raise TemporalIdentityRuntimeError("pin_document_bounds_invalid")
        if type(value) is dict:
            stack.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            stack.extend((child, depth + 1) for child in value)


def _check_json_unicode(document: object) -> None:
    """Reject escaped lone surrogate scalars before model validation."""
    stack: list[object] = [document]
    while stack:
        value = stack.pop()
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise TemporalIdentityRuntimeError("pin_document_invalid") from None
        elif type(value) is dict:
            stack.extend(value.keys())
            stack.extend(value.values())
        elif type(value) is list:
            stack.extend(value)


def _require_object(value: object, fields: frozenset[str]) -> dict[str, object]:
    """Require an exact object field set without echoing operator content."""
    if type(value) is not dict or set(value) != fields:
        raise TemporalIdentityRuntimeError("pin_document_invalid")
    return value


def _build_engine(config: TemporalIdentityRuntimeConfig) -> tuple[Engine, bool]:
    """Adopt an explicit engine or create one from a bounded SQLite/MySQL target."""
    choices = sum(
        value is not None
        for value in (
            config.warehouse_engine,
            config.warehouse_sqlite_path,
            config.warehouse_url,
        )
    )
    if choices != 1:
        raise TemporalIdentityRuntimeError("warehouse_config_invalid")
    if config.warehouse_engine is not None:
        engine = config.warehouse_engine
        if not isinstance(engine, Engine):
            raise TemporalIdentityRuntimeError("warehouse_config_invalid")
        if not _supported_dialect(engine):
            raise TemporalIdentityRuntimeError("warehouse_dialect_unsupported")
        if engine.dialect.name == "sqlite" and not _sqlite_engine_declares_read_only_uri(engine):
            raise TemporalIdentityRuntimeError("sqlite_read_only_engine_required")
        if engine.dialect.name == "mysql" and engine.url.query:
            raise TemporalIdentityRuntimeError("warehouse_url_invalid")
        if engine.dialect.name == "mysql" and (
            type(config.mysql_select_only_account_acknowledged) is not bool
            or not config.mysql_select_only_account_acknowledged
        ):
            raise TemporalIdentityRuntimeError("mysql_select_only_account_required")
        return engine, False

    if config.warehouse_sqlite_path is not None:
        try:
            path = _validated_absolute_path(config.warehouse_sqlite_path)
        except (TypeError, ValueError, OSError):
            raise TemporalIdentityRuntimeError("warehouse_path_invalid") from None
        if any(character in path.as_posix() for character in "?#%"):
            raise TemporalIdentityRuntimeError("warehouse_path_invalid")
        _validate_sqlite_file(path)
        url = URL.create(
            "sqlite+pysqlite",
            database=f"file:{path.as_posix()}",
            query={"mode": "ro", "uri": "true"},
        )
        return _create_configured_engine(url), True

    if (
        type(config.warehouse_url) is not str
        or not config.warehouse_url.strip()
        or config.warehouse_url != config.warehouse_url.strip()
    ):
        raise TemporalIdentityRuntimeError("warehouse_config_invalid")
    try:
        url = make_url(config.warehouse_url)
    except Exception:
        raise TemporalIdentityRuntimeError("warehouse_url_invalid") from None
    if url.drivername != "mysql+pymysql" or not url.host or not url.database:
        raise TemporalIdentityRuntimeError("warehouse_url_invalid")
    mysql_connect_args = _mysql_tls_connect_args(url)
    if type(config.mysql_select_only_account_acknowledged) is not bool or not (
        config.mysql_select_only_account_acknowledged
    ):
        raise TemporalIdentityRuntimeError("mysql_select_only_account_required")
    return _create_configured_engine(url.set(query={}), connect_args=mysql_connect_args), True


def _sqlite_engine_declares_read_only_uri(engine: Engine) -> bool:
    """Require injected SQLite URLs to declare URI-level read-only access."""
    database = engine.url.database
    return (
        isinstance(database, str)
        and database.startswith("file:/")
        and engine.url.query.get("mode") == "ro"
        and engine.url.query.get("uri") == "true"
    )


def _mysql_tls_connect_args(url: URL) -> dict[str, object]:
    """Validate three explicit TLS URL values and bind them as PyMySQL args.

    SQLAlchemy passes unrecognized URL query keys directly to PyMySQL. Options
    such as ``init_command`` execute before the pool's checkout read-only hook,
    so the URL deliberately has a three-key allowlist and rejects duplicate or
    non-string values as well. The factory strips these values from the SQLAlchemy
    URL and passes them directly to PyMySQL; this preserves the explicit CA when
    certificate and hostname verification are enabled together.
    """
    query = url.query
    ca_path = query.get("ssl_ca")
    verify_cert = query.get("ssl_verify_cert")
    verify_identity = query.get("ssl_verify_identity")
    if (
        set(query) != {"ssl_ca", "ssl_verify_cert", "ssl_verify_identity"}
        or type(ca_path) is not str
        or not ca_path.startswith("/")
        or any(ord(character) < 32 for character in ca_path)
        or type(verify_cert) is not str
        or verify_cert != "true"
        or type(verify_identity) is not str
        or verify_identity != "true"
    ):
        raise TemporalIdentityRuntimeError("warehouse_url_invalid")
    return {
        "ssl_ca": ca_path,
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }


def _supported_dialect(engine: Engine) -> bool:
    """Allow synchronous SQLite and non-MariaDB MySQL engines only."""
    return engine.dialect.name in {"sqlite", "mysql"} and not getattr(
        engine.dialect, "is_mariadb", False
    )


def _validate_sqlite_file(path: Path) -> None:
    """Open an existing SQLite database without following path symlinks."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required):
        raise TemporalIdentityRuntimeError("warehouse_path_platform_unsupported")
    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        descriptor = os.open(
            os.sep,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        directory_fds.append(descriptor)
        _check_warehouse_directory(os.fstat(descriptor), is_parent=False)
        parent_names = path.parts[1:-1]
        for index, name in enumerate(parent_names):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fds[-1],
            )
            directory_fds.append(descriptor)
            _check_warehouse_directory(
                os.fstat(descriptor), is_parent=index == len(parent_names) - 1
            )
        before_named = os.stat(path.name, dir_fd=directory_fds[-1], follow_symlinks=False)
        if (
            not stat.S_ISREG(before_named.st_mode)
            or before_named.st_nlink != 1
            or before_named.st_uid not in {0, os.geteuid()}
            or before_named.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise TemporalIdentityRuntimeError("warehouse_path_unsafe")
        file_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory_fds[-1],
        )
        opened = os.fstat(file_fd)
        if not _same_file(before_named, opened):
            raise TemporalIdentityRuntimeError("warehouse_path_changed")
    except TemporalIdentityRuntimeError:
        raise
    except OSError:
        raise TemporalIdentityRuntimeError("warehouse_path_unavailable") from None
    finally:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
        for descriptor in reversed(directory_fds):
            with suppress(OSError):
                os.close(descriptor)


def _create_configured_engine(url: URL, *, connect_args: dict[str, object] | None = None) -> Engine:
    """Create an owned no-pool engine while keeping driver details out of errors."""
    try:
        if connect_args is None:
            return create_engine(url, poolclass=NullPool, pool_pre_ping=False)
        return create_engine(
            url,
            poolclass=NullPool,
            pool_pre_ping=False,
            connect_args=connect_args,
        )
    except Exception:
        raise TemporalIdentityRuntimeError("warehouse_engine_unavailable") from None


def _readonly_checkout_guard(dialect: str) -> Callable[..., None]:
    """Build a pool hook that enforces per-connection read-only state."""
    if dialect == "sqlite":
        return _set_sqlite_query_only
    if dialect == "mysql":
        return _set_mysql_transaction_read_only
    raise TemporalIdentityRuntimeError("warehouse_dialect_unsupported")


def _check_warehouse_directory(info: os.stat_result, *, is_parent: bool) -> None:
    """Reject unsafe SQLite ancestors while allowing root sticky directories."""
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
        raise TemporalIdentityRuntimeError("warehouse_path_unsafe")
    writable = bool(info.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    root_sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
    if writable and not root_sticky:
        raise TemporalIdentityRuntimeError("warehouse_path_unsafe")
    if is_parent and (info.st_uid != os.geteuid() or writable):
        raise TemporalIdentityRuntimeError("warehouse_path_unsafe")


def _set_sqlite_query_only(
    dbapi_connection: _DBAPIConnection, _record: object, _proxy: object
) -> None:
    """Enable and verify SQLite query-only mode on every checked-out connection."""
    cursor = None
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only = ON")
        cursor.execute("PRAGMA query_only")
        row = cursor.fetchone()
        if not row or row[0] != 1:
            raise TemporalIdentityRuntimeError("sqlite_read_only_unavailable")
    except TemporalIdentityRuntimeError:
        raise
    except Exception:
        raise TemporalIdentityRuntimeError("sqlite_read_only_unavailable") from None
    finally:
        if cursor is not None:
            with suppress(Exception):
                cursor.close()


def _set_mysql_transaction_read_only(
    dbapi_connection: _DBAPIConnection, _record: object, _proxy: object
) -> None:
    """Set and verify the default transaction mode on each MySQL checkout.

    This does not inspect account grants. The deployment must use a SELECT-only
    database account for a durable least-privilege boundary.
    """
    cursor = None
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute("SELECT @@session.transaction_read_only")
        row = cursor.fetchone()
        if not row or not _mysql_flag_is_true(row[0]):
            raise TemporalIdentityRuntimeError("mysql_read_only_unavailable")
    except TemporalIdentityRuntimeError:
        raise
    except Exception:
        raise TemporalIdentityRuntimeError("mysql_read_only_unavailable") from None
    finally:
        if cursor is not None:
            with suppress(Exception):
                cursor.close()


def _mysql_flag_is_true(value: object) -> bool:
    """Normalize only documented true-like driver values for MySQL session state."""
    if value is True or (type(value) is int and value == 1):
        return True
    return isinstance(value, str) and value.strip().casefold() in {"1", "on", "yes"}


def _validate_existing_schema(engine: Engine) -> None:
    """Require both migrated tables and their columns through read-only SELECTs."""
    header_sql = text(
        "SELECT authority_id, registry_id, reference_version, total_bytes, chunk_count, "
        "snapshot_sha256, created_at_us FROM od_temporal_identity_snapshots LIMIT 0"
    )
    chunks_sql = text(
        "SELECT authority_id, registry_id, reference_version, chunk_no, chunk_bytes, "
        "chunk_sha256 FROM od_temporal_identity_snapshot_chunks LIMIT 0"
    )
    try:
        with engine.connect() as connection:
            connection.execute(header_sql)
            connection.execute(chunks_sql)
    except Exception:
        raise TemporalIdentityRuntimeError("warehouse_schema_unavailable") from None


__all__ = [
    "MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES",
    "TemporalIdentityPinLookup",
    "TemporalIdentityRuntime",
    "TemporalIdentityRuntimeConfig",
    "TemporalIdentityRuntimeError",
    "TemporalIdentitySnapshotLookup",
    "create_temporal_identity_runtime",
]
