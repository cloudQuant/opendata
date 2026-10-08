"""Immutable local Parquet storage and indexed reads for minute bars."""

from __future__ import annotations

import fcntl
import hashlib
import heapq
import math
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, SupportsFloat, TypedDict, cast

import pandas as pd
import pyarrow as arrow
import pyarrow.dataset as arrow_dataset
import pyarrow.parquet as parquet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from opendata.models.minute_archive import MinuteArchiveShard

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


MINUTE_DOMAIN_ASSET_CLASSES: dict[str, str] = {
    "stock_daily": "equity",
    "index_daily": "index",
    "futures_daily": "futures",
    "option_daily": "option",
}
MINUTE_PERIODS = ("1m", "5m", "15m", "30m", "60m")
RECORD_COLUMNS = (
    "symbol",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
IDENTITY_COLUMNS = ("symbol", "timestamp", "period", "source")
VALUE_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
OUTPUT_COLUMNS = (*IDENTITY_COLUMNS, *VALUE_COLUMNS)
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PARQUET_NAME = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})_"
    r"(?P<source>[A-Za-z0-9][A-Za-z0-9._-]{0,63})_"
    r"(?P<period>1m|5m|15m|30m|60m)_(?P<digest>[0-9a-f]{64})\.parquet$"
)


class MinuteRecord(TypedDict):
    """One normalized archive row with UTC timestamp and numeric values."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


class MinuteArchiveValidationError(ValueError):
    """Raised when archive identity, query bounds, or rows are invalid."""


class MinuteArchiveIntegrityError(RuntimeError):
    """Raised when indexed archive data is missing, unsafe, or corrupted."""


class MinuteArchiveIndexError(RuntimeError):
    """Raised when the main-database metadata index is unavailable."""


@dataclass(frozen=True)
class MinuteShardResult:
    """Metadata returned after an immutable shard was published."""

    domain: str
    symbol: str
    source: str
    period: str
    day: date
    relative_path: str
    row_count: int
    min_timestamp: datetime
    max_timestamp: datetime
    sha256: str


@dataclass(frozen=True)
class MinuteQueryPage:
    """Deterministically paged minute rows and the effective request window."""

    start: date
    end: date
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]
    total_count: int
    page: int
    page_size: int

    def as_dict(self) -> dict[str, object]:
        """Render query metadata and records for the API envelope."""
        return {
            "layer": "file",
            "effective_window": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "columns": list(self.columns),
            "rows": list(self.rows),
            "count": self.total_count,
            "page": self.page,
            "page_size": self.page_size,
        }


@dataclass(frozen=True)
class MinuteArchivePurgeResult:
    """Counts and cleanup failures from one indexed retention pass."""

    candidates: int
    deleted: int
    files_deleted: int
    errors: tuple[str, ...]


_LOCKS_GUARD = threading.Lock()
_SHARD_LOCKS: dict[tuple[str, str, str, str, str, date], tuple[threading.RLock, int]] = {}


@contextmanager
def _shard_lock(
    root: Path, domain: str, symbol: str, source: str, period: str, day: date
) -> Iterator[None]:
    """Serialize shard operations under the archive snapshot/read-write lock."""
    key = (str(root), domain, symbol, source, period, day)
    with _LOCKS_GUARD:
        lock, users = _SHARD_LOCKS.get(key, (threading.RLock(), 0))
        _SHARD_LOCKS[key] = (lock, users + 1)
    lock.acquire()
    descriptor: int | None = None
    locked = False
    try:
        root.mkdir(parents=True, exist_ok=True)
        with _snapshot_file_lock(root, exclusive=False):
            try:
                try:
                    lock_dir = _archive_directory(root, (".locks",), create=True)
                except FileExistsError:
                    # Concurrent processes may both observe an absent lock
                    # directory; revalidate the winner without following links.
                    lock_dir = _archive_directory(root, (".locks",), create=False)
                filename = (
                    hashlib.sha256("\0".join(map(str, key[1:])).encode()).hexdigest() + ".lock"
                )
                lock_path = lock_dir / filename
                if lock_path.is_symlink():
                    raise MinuteArchiveIntegrityError(
                        "minute archive lock path must not be a symlink"
                    )
                flags = os.O_CREAT | os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(lock_path, flags, 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise MinuteArchiveIntegrityError(
                        "minute archive lock path is not a regular file"
                    )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            finally:
                if descriptor is not None:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
    finally:
        lock.release()
        with _LOCKS_GUARD:
            current_lock, users = _SHARD_LOCKS[key]
            if users == 1:
                del _SHARD_LOCKS[key]
            else:
                _SHARD_LOCKS[key] = (current_lock, users - 1)


@contextmanager
def _snapshot_file_lock(root: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the shared archive lock used by readers, writers, and snapshots.

    New lock files inherit the archive root owner and restrictive permissions.
    Existing lock permissions and ownership are never changed.
    """
    lock_path = root / ".snapshot.lock"
    if lock_path.is_symlink():
        raise MinuteArchiveIntegrityError("minute archive snapshot lock must not be a symlink")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise MinuteArchiveIntegrityError("minute archive snapshot lock must be a regular file")
        if created:
            root_stat = root.stat()
            descriptor_stat = os.fstat(descriptor)
            if (descriptor_stat.st_uid, descriptor_stat.st_gid) != (
                root_stat.st_uid,
                root_stat.st_gid,
            ):
                os.fchown(descriptor, root_stat.st_uid, root_stat.st_gid)
            os.fchmod(descriptor, 0o600)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise MinuteArchiveIntegrityError("minute archive snapshot lock is unavailable") from exc

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except OSError as exc:
        os.close(descriptor)
        raise MinuteArchiveIntegrityError("minute archive snapshot lock failed") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@lru_cache(maxsize=1)
def get_minute_archive_engine() -> Engine:
    """Create the synchronous engine for the main-database shard index."""
    from opendata.core.config import settings

    return create_engine(settings.database_url_sync, poolclass=NullPool)


def get_minute_archive_root() -> Path:
    """Return the configured local archive root."""
    from opendata.core.config import settings

    return Path(settings.data_dir) / "minute_archive"


def validate_minute_identity(
    domain: str, symbol: str, source: str, period: str
) -> tuple[str, str, str, str]:
    """Validate path-bearing identity fields without contacting providers."""
    if domain not in MINUTE_DOMAIN_ASSET_CLASSES:
        raise MinuteArchiveValidationError(f"minute data is not enabled for domain {domain!r}")
    if not isinstance(symbol, str) or not _COMPONENT.fullmatch(symbol):
        raise MinuteArchiveValidationError("symbol must be a plain path-safe identifier")
    if not isinstance(source, str) or not _SOURCE_COMPONENT.fullmatch(source):
        raise MinuteArchiveValidationError("source must be a plain path-safe identifier")
    if source.lower() == "auto":
        raise MinuteArchiveValidationError("minute archive queries require an explicit source")
    if period not in MINUTE_PERIODS:
        raise MinuteArchiveValidationError(f"period must be one of {', '.join(MINUTE_PERIODS)}")
    return domain, symbol, source, period


def validate_asset_domain(asset_class: str, domain: str) -> None:
    """Validate a Bar domain and its public asset-class route segment."""
    expected = MINUTE_DOMAIN_ASSET_CLASSES.get(domain)
    if expected is None or expected != asset_class:
        raise MinuteArchiveValidationError(
            "minute archive route does not match a supported Bar domain"
        )


def ingest_minute_shard(
    engine: Engine,
    root: Path | str,
    *,
    domain: str,
    symbol: str,
    source: str,
    period: str,
    records: Sequence[Mapping[str, object]],
) -> MinuteShardResult:
    """Merge and atomically publish one symbol/day/source/period shard.

    A process-local thread lock and per-shard POSIX lock serialize API, importer,
    and retention work; a shared root lock coordinates them with paired backup
    snapshots on one host. Multi-host shared-filesystem locking is not supported.
    The content-addressed file is
    immutable and remains available after an index switch so older database/file
    snapshots and in-flight reads keep working.

    Args:
        engine: Synchronous main-database engine for the metadata index.
        root: Injectable archive root; production uses ``DATA_DIR/minute_archive``.
        domain: One of the supported Bar domains.
        symbol: Path-safe symbol identifier.
        source: Explicit source label; ``auto`` is not accepted.
        period: Minute-bar period.
        records: Canonical ``symbol,timestamp,OHLCV,amount`` records from one UTC day.

    Returns:
        The committed shard metadata.

    Raises:
        MinuteArchiveValidationError: If identity or row content is invalid.
        MinuteArchiveIntegrityError: If an existing indexed shard fails verification.
        MinuteArchiveIndexError: If metadata cannot be committed.
    """
    domain, symbol, source, period = validate_minute_identity(domain, symbol, source, period)
    normalized = _normalize_records(records, symbol=symbol)
    if not normalized:
        raise MinuteArchiveValidationError("a minute shard must contain at least one row")
    day = normalized[0]["timestamp"].date()
    if any(row["timestamp"].date() != day for row in normalized):
        raise MinuteArchiveValidationError("one ingestion call may contain only one UTC day")
    _reject_expired_day(day)

    archive_root = _resolved_root(root, create=True)
    key = (domain, symbol, source, period, day)
    with _shard_lock(archive_root, *key):
        existing = _load_index_record(engine, key)
        old_rows: list[MinuteRecord] = []
        if existing is not None:
            old_frame = _read_indexed_frame(archive_root, existing)
            old_rows = cast("list[MinuteRecord]", old_frame.to_dict(orient="records"))

        merged_by_timestamp: dict[datetime, MinuteRecord] = {
            row["timestamp"]: row for row in old_rows
        }
        merged_by_timestamp.update({row["timestamp"]: row for row in normalized})
        merged = sorted(merged_by_timestamp.values(), key=lambda row: row["timestamp"])
        frame = pd.DataFrame(merged, columns=RECORD_COLUMNS)
        min_timestamp = merged[0]["timestamp"]
        max_timestamp = merged[-1]["timestamp"]
        shard_dir = _archive_directory(
            archive_root, (domain, symbol, f"{day.year:04d}"), create=True
        )
        file_hash, final_path = _write_immutable_file(frame, shard_dir, day, source, period)
        relative_path = final_path.relative_to(archive_root).as_posix()

        _publish_index(
            engine,
            key,
            relative_path=relative_path,
            row_count=len(merged),
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            sha256=file_hash,
        )

        return MinuteShardResult(
            domain=domain,
            symbol=symbol,
            source=source,
            period=period,
            day=day,
            relative_path=relative_path,
            row_count=len(merged),
            min_timestamp=min_timestamp,
            max_timestamp=max_timestamp,
            sha256=file_hash,
        )


def query_minute_archive(
    engine: Engine,
    root: Path | str,
    *,
    asset_class: str,
    domain: str,
    symbols: Sequence[str],
    source: str,
    period: str,
    start: date,
    end: date,
    fields: Sequence[str] | None = None,
    page: int = 1,
    page_size: int = 100,
) -> MinuteQueryPage:
    """Read indexed Parquet shards for an explicit bounded query.

    Args:
        engine: Synchronous main-database engine.
        root: Injectable archive root.
        asset_class: Public route asset class, checked against the Bar domain.
        domain: One of the four supported Bar domains.
        symbols: One to twenty unique symbols.
        source: Explicit source label.
        period: Supported minute period.
        start: Inclusive UTC calendar date.
        end: Inclusive UTC calendar date.
        fields: Optional value-field allowlist; identity fields are always returned.
        page: One-based deterministic page number.
        page_size: Rows per page, at most 1000.

    Returns:
        A page with a total count and effective date window.

    Raises:
        MinuteArchiveValidationError: If any identity/filter/window is invalid.
        MinuteArchiveIntegrityError: If an indexed file is missing or corrupt.
        MinuteArchiveIndexError: If the main-database index cannot be read.
    """
    validate_asset_domain(asset_class, domain)
    if not symbols or len(symbols) > 20:
        raise MinuteArchiveValidationError("symbols must contain between 1 and 20 values")
    if len(set(symbols)) != len(symbols):
        raise MinuteArchiveValidationError("symbols must not contain duplicates")
    checked_symbols = tuple(
        validate_minute_identity(domain, symbol, source, period)[1] for symbol in symbols
    )
    _validate_window(start, end)
    if page < 1 or page_size < 1 or page_size > 1000:
        raise MinuteArchiveValidationError("page must be positive and page_size must be 1..1000")
    selected_fields = _selected_fields(fields)
    archive_root = _resolved_root(root, create=False)
    start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    offset = (page - 1) * page_size
    columns = tuple(column for column in OUTPUT_COLUMNS if column in selected_fields)
    total_count = 0
    page_rows: list[dict[str, object]] = []
    day = start
    while day <= end:
        # Drop the preceding day's materialized rows before opening the next
        # date. At most one UTC day of selected shard rows is retained.
        day_rows: list[list[dict[str, object]]] = []
        try:
            with Session(engine) as session:
                records = list(
                    session.scalars(
                        select(MinuteArchiveShard)
                        .where(
                            MinuteArchiveShard.domain == domain,
                            MinuteArchiveShard.symbol.in_(checked_symbols),
                            MinuteArchiveShard.source == source,
                            MinuteArchiveShard.period == period,
                            MinuteArchiveShard.day == day,
                        )
                        .order_by(MinuteArchiveShard.symbol)
                    ).all()
                )
        except SQLAlchemyError as exc:
            raise MinuteArchiveIndexError("minute archive metadata index is unavailable") from exc

        for record in records:
            key = (record.domain, record.symbol, record.source, record.period, record.day)
            with _shard_lock(archive_root, *key):
                current = _load_index_record(engine, key)
                if current is None:
                    # Retention completed while this read waited for the shard lock.
                    continue
                shard_rows = _read_query_rows(
                    archive_root,
                    current,
                    start_at=start_at,
                    end_at=end_at,
                    columns=columns,
                )
            if shard_rows:
                day_rows.append(shard_rows)
            del shard_rows

        if day_rows:
            for row in heapq.merge(
                *day_rows,
                key=lambda item: (item["timestamp"], item["symbol"]),
            ):
                if offset <= total_count < offset + page_size:
                    page_rows.append({column: _json_value(row[column]) for column in columns})
                total_count += 1
            # Do not retain the prior day's final merged row while the next
            # day's indexed files are being opened.
            del row
        del day_rows
        day += timedelta(days=1)

    return MinuteQueryPage(
        start=start,
        end=end,
        columns=columns,
        rows=tuple(page_rows),
        total_count=total_count,
        page=page,
        page_size=page_size,
    )


def _normalize_records(
    records: Iterable[Mapping[str, object]],
    *,
    symbol: str,
) -> list[MinuteRecord]:
    """Validate records, normalize timestamps to UTC, and reject duplicate submissions."""
    normalized: list[MinuteRecord] = []
    seen: set[datetime] = set()
    expected = set(RECORD_COLUMNS)
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise MinuteArchiveValidationError(f"row {index + 1} must be a mapping")
        if set(record) != expected:
            missing = sorted(expected - set(record))
            extra = sorted(set(record) - expected)
            raise MinuteArchiveValidationError(
                f"row {index + 1} has invalid columns (missing={missing}, extra={extra})"
            )
        row_symbol = record["symbol"]
        if not isinstance(row_symbol, str) or row_symbol != symbol:
            raise MinuteArchiveValidationError("every row symbol must match the shard symbol")
        timestamp = _timestamp_utc(record["timestamp"], index=index)
        if timestamp in seen:
            raise MinuteArchiveValidationError(
                f"duplicate timestamp in submission: {timestamp.isoformat()}"
            )
        seen.add(timestamp)
        values = {column: _finite_number(record[column], column, index) for column in VALUE_COLUMNS}
        if values["volume"] < 0 or values["amount"] < 0:
            raise MinuteArchiveValidationError(
                f"row {index + 1} volume and amount must be non-negative"
            )
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise MinuteArchiveValidationError(f"row {index + 1} high is below another OHLC value")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise MinuteArchiveValidationError(f"row {index + 1} low is above another OHLC value")
        normalized.append(
            {
                "symbol": symbol,
                "timestamp": timestamp,
                "open": values["open"],
                "high": values["high"],
                "low": values["low"],
                "close": values["close"],
                "volume": values["volume"],
                "amount": values["amount"],
            }
        )
    return normalized


def _timestamp_utc(value: object, *, index: int) -> datetime:
    """Parse an aware timestamp and normalize it to UTC."""
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MinuteArchiveValidationError(
                f"row {index + 1} timestamp is not ISO 8601"
            ) from exc
    else:
        raise MinuteArchiveValidationError(f"row {index + 1} timestamp must include a timezone")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MinuteArchiveValidationError(f"row {index + 1} timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: object, field: str, index: int) -> float:
    """Read one finite numeric market field."""
    if isinstance(value, bool):
        raise MinuteArchiveValidationError(f"row {index + 1} {field} must be numeric")
    try:
        parsed = float(cast("str | SupportsFloat", value))
    except (TypeError, ValueError) as exc:
        raise MinuteArchiveValidationError(f"row {index + 1} {field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise MinuteArchiveValidationError(f"row {index + 1} {field} must be finite")
    return parsed


def _validate_window(start: date, end: date) -> None:
    """Reject inverted or overlong inclusive date windows."""
    if start > end:
        raise MinuteArchiveValidationError("start must not be after end")
    if end == date.max:
        raise MinuteArchiveValidationError("end date is outside the supported query range")
    if (end - start).days >= 31:
        raise MinuteArchiveValidationError("minute archive window cannot exceed 31 inclusive days")


def _selected_fields(fields: Sequence[str] | None) -> frozenset[str]:
    """Validate optional projected fields and always retain row identity."""
    if fields is None:
        return frozenset(OUTPUT_COLUMNS)
    requested = set(fields)
    invalid = requested - set(OUTPUT_COLUMNS)
    if invalid:
        raise MinuteArchiveValidationError(f"unknown minute fields: {', '.join(sorted(invalid))}")
    return frozenset((*IDENTITY_COLUMNS, *requested))


def _resolved_root(root: Path | str, *, create: bool) -> Path:
    """Resolve the configured root and refuse a symlink at the root itself."""
    configured = Path(root).expanduser()
    if configured.is_symlink():
        raise MinuteArchiveValidationError("minute archive root must not be a symlink")
    if create:
        configured.mkdir(parents=True, exist_ok=True)
    elif configured.exists() and not configured.is_dir():
        raise MinuteArchiveValidationError("minute archive root is not a directory")
    resolved = configured.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise MinuteArchiveValidationError("minute archive root is not a directory")
    return resolved


def _archive_directory(root: Path, components: Sequence[str], *, create: bool) -> Path:
    """Walk/create controlled path components without following child symlinks."""
    current = root
    for component in components:
        if component in {"", ".", ".."} or Path(component).name != component:
            raise MinuteArchiveValidationError("unsafe minute archive path component")
        current = current / component
        if current.is_symlink():
            raise MinuteArchiveIntegrityError("minute archive path contains a symlink")
        if not current.exists():
            if not create:
                return current
            current.mkdir()
        elif not current.is_dir():
            raise MinuteArchiveIntegrityError("minute archive path component is not a directory")
        if not current.resolve().is_relative_to(root):
            raise MinuteArchiveIntegrityError("minute archive path escapes its configured root")
    return current


def _indexed_path(root: Path, record: MinuteArchiveShard) -> Path:
    """Resolve and validate a relative index path without following symlinks."""
    relative = PurePosixPath(record.relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise MinuteArchiveIntegrityError("minute archive index contains an unsafe relative path")
    expected_dir = (record.domain, record.symbol, f"{record.day.year:04d}")
    if tuple(relative.parts[:3]) != expected_dir:
        raise MinuteArchiveIntegrityError("minute archive index path does not match its shard key")
    current = _archive_directory(root, relative.parts[:-1], create=False)
    target = current / relative.parts[-1]
    if target.is_symlink():
        raise MinuteArchiveIntegrityError("minute archive file must not be a symlink")
    match = _PARQUET_NAME.fullmatch(target.name)
    if (
        match is None
        or match.group("day") != record.day.isoformat()
        or match.group("source") != record.source
        or match.group("period") != record.period
        or match.group("digest") != record.sha256
    ):
        raise MinuteArchiveIntegrityError("minute archive index filename does not match its digest")
    if not target.resolve().is_relative_to(root):
        raise MinuteArchiveIntegrityError("minute archive file escapes its configured root")
    return target


def _write_immutable_file(
    frame: pd.DataFrame,
    directory: Path,
    day: date,
    source: str,
    period: str,
) -> tuple[str, Path]:
    """Write a Parquet temp file and atomically publish its content-addressed name."""
    descriptor, temp_name = tempfile.mkstemp(prefix=".minute-", suffix=".tmp", dir=directory)
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        parquet.write_table(
            arrow.Table.from_pandas(frame, preserve_index=False),
            temp_path,
            compression="zstd",
        )
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        file_hash = _sha256_file(temp_path)
        final_path = directory / f"{day.isoformat()}_{source}_{period}_{file_hash}.parquet"
        if final_path.is_symlink():
            raise MinuteArchiveIntegrityError("minute archive target must not be a symlink")
        if final_path.exists():
            if _sha256_file(final_path) != file_hash:
                raise MinuteArchiveIntegrityError("content-addressed minute file has a bad digest")
        else:
            os.replace(temp_path, final_path)
            _fsync_directory(directory)
        return file_hash, final_path
    finally:
        temp_path.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Persist a file rename on platforms that support directory fsync."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    """Hash one archive file without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_index_record(
    engine: Engine, key: tuple[str, str, str, str, date]
) -> MinuteArchiveShard | None:
    """Read one metadata row from mainDB; fail closed if the index is absent."""
    try:
        with Session(engine) as session:
            return session.get(MinuteArchiveShard, key)
    except SQLAlchemyError as exc:
        raise MinuteArchiveIndexError("minute archive metadata index is unavailable") from exc


def _publish_index(
    engine: Engine,
    key: tuple[str, str, str, str, date],
    *,
    relative_path: str,
    row_count: int,
    min_timestamp: datetime,
    max_timestamp: datetime,
    sha256: str,
) -> None:
    """Commit the new immutable file pointer in one metadata transaction."""
    try:
        with Session(engine) as session, session.begin():
            record = session.get(MinuteArchiveShard, key)
            if record is None:
                record = MinuteArchiveShard(
                    domain=key[0],
                    symbol=key[1],
                    source=key[2],
                    period=key[3],
                    day=key[4],
                    relative_path=relative_path,
                    row_count=row_count,
                    min_timestamp=min_timestamp,
                    max_timestamp=max_timestamp,
                    sha256=sha256,
                )
                session.add(record)
            else:
                record.relative_path = relative_path
                record.row_count = row_count
                record.min_timestamp = min_timestamp
                record.max_timestamp = max_timestamp
                record.sha256 = sha256
                record.updated_at = datetime.now(timezone.utc)
    except SQLAlchemyError as exc:
        raise MinuteArchiveIndexError("minute archive metadata publication failed") from exc


def _verify_indexed_file(root: Path, record: MinuteArchiveShard) -> Path:
    """Verify immutable file bytes and bounded metadata without loading bar values."""
    path = _indexed_path(root, record)
    if not path.is_file():
        raise MinuteArchiveIntegrityError("indexed minute archive file is missing")
    if _sha256_file(path) != record.sha256:
        raise MinuteArchiveIntegrityError("indexed minute archive checksum does not match")
    try:
        parquet_file = parquet.ParquetFile(path)
        schema = parquet_file.schema_arrow
        timestamp_type = schema.field("timestamp").type
        if tuple(schema.names) != RECORD_COLUMNS:
            raise MinuteArchiveIntegrityError("minute archive file has an unexpected schema")
        if (
            not arrow.types.is_timestamp(timestamp_type)
            or timestamp_type.unit not in {"ns", "us"}
            or timestamp_type.tz != "UTC"
        ):
            raise MinuteArchiveIntegrityError("minute archive timestamps are not stored in UTC")
        if parquet_file.metadata.num_rows != record.row_count or record.row_count < 1:
            raise MinuteArchiveIntegrityError("minute archive row count does not match its index")
        minimum: datetime | None = None
        maximum: datetime | None = None
        previous: datetime | None = None
        for batch in parquet_file.iter_batches(
            columns=["symbol", "timestamp"],
            batch_size=4096,
        ):
            for row in batch.to_pylist():
                timestamp = row["timestamp"]
                if not isinstance(timestamp, datetime):
                    raise MinuteArchiveIntegrityError("minute archive timestamp has invalid type")
                timestamp = timestamp.astimezone(timezone.utc)
                if timestamp.date() != record.day:
                    raise MinuteArchiveIntegrityError(
                        "minute archive row is outside its indexed day"
                    )
                if row["symbol"] != record.symbol:
                    raise MinuteArchiveIntegrityError(
                        "minute archive rows do not match their indexed symbol"
                    )
                if previous is not None and timestamp <= previous:
                    raise MinuteArchiveIntegrityError(
                        "minute archive timestamps are not unique and ordered"
                    )
                previous = timestamp
                minimum = timestamp if minimum is None else min(minimum, timestamp)
                maximum = timestamp if maximum is None else max(maximum, timestamp)
        if minimum != _as_utc(record.min_timestamp) or maximum != _as_utc(record.max_timestamp):
            raise MinuteArchiveIntegrityError("minute archive time bounds do not match its index")
    except MinuteArchiveIntegrityError:
        raise
    except Exception as exc:
        raise MinuteArchiveIntegrityError("minute archive Parquet file cannot be verified") from exc
    return path


def _read_query_rows(
    root: Path,
    record: MinuteArchiveShard,
    *,
    start_at: datetime,
    end_at: datetime,
    columns: Sequence[str],
) -> list[dict[str, object]]:
    """Read one integrity-checked shard with Arrow time predicates and projection."""
    path = _verify_indexed_file(root, record)
    try:
        time_field = arrow_dataset.field("timestamp")
        stored_columns = [column for column in columns if column not in {"period", "source"}]
        table = arrow_dataset.dataset(path, format="parquet").to_table(
            columns=stored_columns,
            filter=(time_field >= start_at) & (time_field < end_at),
        )
        rows = cast("list[dict[str, object]]", table.to_pylist())
    except Exception as exc:
        raise MinuteArchiveIntegrityError("minute archive Parquet query failed") from exc
    for row in rows:
        timestamp = row.get("timestamp")
        if (
            row.get("symbol") != record.symbol
            or not isinstance(timestamp, datetime)
            or timestamp.astimezone(timezone.utc) < start_at
            or timestamp.astimezone(timezone.utc) >= end_at
        ):
            raise MinuteArchiveIntegrityError("minute archive query returned an invalid row")
        row["period"] = record.period
        row["source"] = record.source
    return rows


def _read_indexed_frame(root: Path, record: MinuteArchiveShard) -> pd.DataFrame:
    """Verify path, digest, schema, count, and timestamp bounds before returning rows."""
    path = _verify_indexed_file(root, record)
    try:
        frame = cast("pd.DataFrame", parquet.read_table(path).to_pandas())
    except MinuteArchiveIntegrityError:
        raise
    except Exception as exc:
        raise MinuteArchiveIntegrityError("minute archive Parquet file cannot be read") from exc
    return frame


def _as_utc(value: datetime) -> datetime:
    """Read a metadata timestamp, treating SQLite's naive DATETIME as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_value(value: object) -> object:
    """Convert pandas scalar values into stable JSON-compatible values."""
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().astimezone(timezone.utc).isoformat()
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _reject_expired_day(day: date) -> None:
    """Refuse backfills older than the configured minute retention horizon."""
    from opendata.core.config import settings

    oldest_year = datetime.now(timezone.utc).year - settings.retention_minute_years
    if day.year < oldest_year:
        raise MinuteArchiveValidationError(
            "minute shard is outside the configured retention window"
        )


def purge_expired_minute_shards(
    engine: Engine,
    root: Path | str,
    *,
    keep_years: int | None = None,
    today: date | None = None,
    dry_run: bool = False,
) -> MinuteArchivePurgeResult:
    """Retire expired metadata and canonical file versions under shard locks.

    Args:
        engine: Main-database engine, distinct from the data warehouse engine.
        root: Verified archive root.
        keep_years: Override for the positive year retention limit.
        today: Calendar override for deterministic tests.
        dry_run: Count indexed shard candidates without deleting.

    Returns:
        Candidate metadata count, deleted metadata count, removed file count,
        and any cleanup errors.

    Raises:
        MinuteArchiveValidationError: If the root or retention window is invalid.
        MinuteArchiveIndexError: If the main metadata table cannot be read.
    """
    from opendata.core.config import settings

    years = settings.retention_minute_years if keep_years is None else keep_years
    if years <= 0:
        raise MinuteArchiveValidationError("minute archive retention must be positive")
    archive_root = _resolved_root(root, create=False)
    current_day = today or datetime.now(timezone.utc).date()
    cutoff = date(current_day.year - years, 1, 1)
    try:
        with Session(engine) as session:
            candidates = list(
                session.scalars(
                    select(MinuteArchiveShard).where(MinuteArchiveShard.day < cutoff)
                ).all()
            )
    except SQLAlchemyError as exc:
        raise MinuteArchiveIndexError("minute archive metadata index is unavailable") from exc
    candidate_keys = {
        (row.domain, row.symbol, row.source, row.period, row.day) for row in candidates
    }
    candidate_count = len(candidate_keys)
    if dry_run or not candidate_keys:
        return MinuteArchivePurgeResult(candidate_count, 0, 0, ())

    deleted = 0
    files_deleted = 0
    errors: list[str] = []
    for key in sorted(candidate_keys):
        domain, symbol, source, period, shard_day = key
        with _shard_lock(archive_root, *key):
            try:
                with Session(engine) as session, session.begin():
                    record = session.get(MinuteArchiveShard, key)
                    if record is None or record.day >= cutoff:
                        continue
                    # Keep the path for safety validation, then remove the index
                    # transactionally before physical cleanup.
                    indexed_path = _indexed_path(archive_root, record)
                    index_file_missing = not indexed_path.is_file()
                    session.delete(record)
                deleted += 1
                if index_file_missing:
                    errors.append(f"{indexed_path.name}: indexed Parquet file was already missing")
                day_dir = _archive_directory(
                    archive_root, (domain, symbol, f"{shard_day.year:04d}"), create=False
                )
                matching_files = _files_for_key(day_dir, shard_day, source, period)
                if indexed_path not in matching_files and indexed_path.exists():
                    errors.append(f"{indexed_path.name}: index path is not a canonical shard")
                    continue
                for file_path in matching_files:
                    if file_path.is_symlink():
                        errors.append(f"{file_path.name}: refusing to remove a symlink")
                        continue
                    file_path.unlink(missing_ok=True)
                    files_deleted += 1
                _remove_empty_archive_dirs(archive_root, day_dir)
            except (MinuteArchiveValidationError, MinuteArchiveIntegrityError, OSError) as exc:
                errors.append(f"{domain}/{symbol}/{shard_day}: {type(exc).__name__}: {exc}")
            except SQLAlchemyError:
                errors.append(f"{domain}/{symbol}/{shard_day}: metadata deletion failed")
                continue
    return MinuteArchivePurgeResult(candidate_count, deleted, files_deleted, tuple(errors))


def _files_for_key(day_dir: Path, day: date, source: str, period: str) -> list[Path]:
    """List only canonical immutable Parquet versions for one shard key."""
    if day_dir.is_symlink():
        raise MinuteArchiveIntegrityError("minute archive retention path contains a symlink")
    if not day_dir.is_dir():
        return []
    files: list[Path] = []
    for item in day_dir.iterdir():
        if item.is_symlink():
            if item.suffix == ".parquet":
                raise MinuteArchiveIntegrityError("minute archive retention refuses a symlink file")
            continue
        match = _PARQUET_NAME.fullmatch(item.name)
        if (
            item.is_file()
            and match is not None
            and match.group("day") == day.isoformat()
            and match.group("source") == source
            and match.group("period") == period
        ):
            files.append(item)
    return files


def _remove_empty_archive_dirs(root: Path, day_dir: Path) -> None:
    """Remove empty year/symbol/domain directories without recursive deletion."""
    for directory in (day_dir, day_dir.parent, day_dir.parent.parent):
        if directory == root or directory.is_symlink():
            break
        try:
            directory.rmdir()
        except OSError:
            break


__all__ = [
    "IDENTITY_COLUMNS",
    "MINUTE_DOMAIN_ASSET_CLASSES",
    "MINUTE_PERIODS",
    "MinuteArchiveIndexError",
    "MinuteArchiveIntegrityError",
    "MinuteArchivePurgeResult",
    "MinuteArchiveValidationError",
    "MinuteQueryPage",
    "MinuteShardResult",
    "get_minute_archive_engine",
    "get_minute_archive_root",
    "ingest_minute_shard",
    "purge_expired_minute_shards",
    "query_minute_archive",
    "validate_asset_domain",
    "validate_minute_identity",
]
