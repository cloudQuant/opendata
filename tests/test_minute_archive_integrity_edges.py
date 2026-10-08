"""Failure-path regressions for minute archive locking and integrity checks."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pyarrow as arrow
import pyarrow.parquet as parquet
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from opendata.data.minute_archive import (
    MinuteArchiveIndexError,
    MinuteArchiveIntegrityError,
    MinuteArchiveValidationError,
    ingest_minute_shard,
    purge_expired_minute_shards,
    query_minute_archive,
)
from opendata.models.minute_archive import MinuteArchiveShard

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from sqlalchemy import Engine


@pytest.fixture
def archive_engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a disposable SQLite metadata index for one test."""
    engine = create_engine(f"sqlite:///{tmp_path / 'minute-index.db'}")
    MinuteArchiveShard.__table__.create(engine)  # type: ignore[attr-defined]
    yield engine
    engine.dispose()


def _record(
    timestamp: object,
    *,
    symbol: str = "AAPL",
    close: float = 10.0,
) -> dict[str, object]:
    """Build one canonical minute bar."""
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 100,
        "amount": 1000,
    }


def _ingest(
    engine: Engine,
    root: Path,
    records: list[Mapping[str, object]],
) -> object:
    """Insert a shard through the public validation and publication path."""
    return ingest_minute_shard(
        engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=records,
    )


def _query(engine: Engine, root: Path, **overrides: object) -> object:
    """Query one date using valid defaults that an edge case can override."""
    current_utc_day = datetime.now(timezone.utc).date()
    options: dict[str, object] = {
        "asset_class": "equity",
        "domain": "stock_daily",
        "symbols": ["AAPL"],
        "source": "sample-feed",
        "period": "1m",
        "start": current_utc_day,
        "end": current_utc_day,
    }
    options.update(overrides)
    return query_minute_archive(engine, root, **options)  # type: ignore[arg-type]


def _indexed_record(engine: Engine) -> MinuteArchiveShard:
    """Return the one fixture shard row with its current indexed metadata."""
    with Session(engine) as session:
        record = session.scalars(select(MinuteArchiveShard)).one()
        session.expunge(record)
    return record


def _replace_indexed_parquet(
    engine: Engine,
    root: Path,
    rows: list[dict[str, object]],
    *,
    schema: arrow.Schema | None = None,
    row_count: int | None = None,
    min_timestamp: datetime | None = None,
    max_timestamp: datetime | None = None,
) -> None:
    """Publish a deliberately malformed but correctly hashed test shard."""
    record = _indexed_record(engine)
    directory = root / record.domain / record.symbol / f"{record.day.year:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    old_path = root / record.relative_path
    table = arrow.Table.from_pylist(rows, schema=schema)
    replacement = directory / "replacement.parquet"
    parquet.write_table(table, replacement)
    digest = hashlib.sha256(replacement.read_bytes()).hexdigest()
    new_path = directory / (
        f"{record.day.isoformat()}_{record.source}_{record.period}_{digest}.parquet"
    )
    replacement.replace(new_path)
    old_path.unlink()
    with Session(engine) as session, session.begin():
        indexed = session.get(
            MinuteArchiveShard,
            (record.domain, record.symbol, record.source, record.period, record.day),
        )
        assert indexed is not None
        indexed.relative_path = new_path.relative_to(root).as_posix()
        indexed.sha256 = digest
        indexed.row_count = len(rows) if row_count is None else row_count
        timestamps: list[datetime] = []
        for row in rows:
            value = row.get("timestamp")
            if isinstance(value, datetime):
                timestamps.append(value)
        if min_timestamp is not None:
            indexed.min_timestamp = min_timestamp
        elif timestamps:
            indexed.min_timestamp = min(timestamps)
        if max_timestamp is not None:
            indexed.max_timestamp = max_timestamp
        elif timestamps:
            indexed.max_timestamp = max(timestamps)


def _seed_current_shard(engine: Engine, root: Path) -> datetime:
    """Insert one current-date shard and return its UTC timestamp."""
    timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    _ingest(engine, root, [_record(timestamp)])
    return timestamp


def test_snapshot_lock_rejects_symlink_without_touching_target(tmp_path: Path) -> None:
    """A lock symlink is rejected before opening or changing its target."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    root.mkdir()
    target = tmp_path / "operator-file"
    target.write_text("leave alone", encoding="utf-8")
    (root / ".snapshot.lock").symlink_to(target)

    with (
        pytest.raises(MinuteArchiveIntegrityError, match="must not be a symlink"),
        archive._snapshot_file_lock(root, exclusive=True),
    ):
        pytest.fail("symlink lock must not be entered")

    assert target.read_text(encoding="utf-8") == "leave alone"


def test_snapshot_lock_rejects_nonregular_descriptor_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-regular lock descriptor fails closed and is released."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    root.mkdir()
    lock_path = root / ".snapshot.lock"
    lock_path.touch()
    opened: list[int] = []
    real_open = os.open

    def open_device(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == lock_path:
            descriptor = real_open("/dev/null", os.O_RDONLY)
            opened.append(descriptor)
            return descriptor
        return real_open(path, flags, *args)

    monkeypatch.setattr(archive.os, "open", open_device)
    with (
        pytest.raises(MinuteArchiveIntegrityError, match="regular file"),
        archive._snapshot_file_lock(root, exclusive=False),
    ):
        pytest.fail("a character device is not a valid lock")

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_snapshot_lock_open_and_flock_failures_release_owned_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Open and lock errors become integrity failures without descriptor leaks."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    root.mkdir()
    lock_path = root / ".snapshot.lock"
    real_open = os.open

    def deny_open(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if Path(path) == lock_path:
            raise PermissionError("fixture denied lock open")
        return real_open(path, flags, *args)

    monkeypatch.setattr(archive.os, "open", deny_open)
    with (
        pytest.raises(MinuteArchiveIntegrityError, match="unavailable"),
        archive._snapshot_file_lock(root, exclusive=False),
    ):
        pytest.fail("an unavailable lock must not be entered")
    monkeypatch.setattr(archive.os, "open", real_open)

    opened: list[int] = []
    real_flock = archive.fcntl.flock

    def deny_flock(descriptor: int, operation: int) -> None:
        if operation != archive.fcntl.LOCK_UN:
            opened.append(descriptor)
            raise OSError("fixture denied flock")
        real_flock(descriptor, operation)

    monkeypatch.setattr(archive.fcntl, "flock", deny_flock)
    with (
        pytest.raises(MinuteArchiveIntegrityError, match="lock failed"),
        archive._snapshot_file_lock(root, exclusive=True),
    ):
        pytest.fail("a failed flock must not be entered")

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_existing_snapshot_lock_keeps_operator_permissions(tmp_path: Path) -> None:
    """Opening an existing regular lock does not normalize its mode."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    root.mkdir()
    lock_path = root / ".snapshot.lock"
    lock_path.touch(mode=0o640)
    lock_path.chmod(0o640)

    with archive._snapshot_file_lock(root, exclusive=False):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o640

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o640


def test_snapshot_lock_applies_archive_owner_to_new_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newly created snapshot lock inherits the existing archive owner."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    root.mkdir()
    real_fstat = os.fstat
    ownership_adjustments: list[tuple[int, int, int]] = []

    def report_different_owner(descriptor: int) -> SimpleNamespace:
        result = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_uid=result.st_uid + 1,
            st_gid=result.st_gid + 1,
        )

    def record_chown(descriptor: int, uid: int, gid: int) -> None:
        ownership_adjustments.append((descriptor, uid, gid))

    monkeypatch.setattr(archive.os, "fstat", report_different_owner)
    monkeypatch.setattr(archive.os, "fchown", record_chown)
    with archive._snapshot_file_lock(root, exclusive=True):
        assert (root / ".snapshot.lock").exists()

    root_stat = root.stat()
    assert len(ownership_adjustments) == 1
    _, owned_uid, owned_gid = ownership_adjustments[0]
    assert (owned_uid, owned_gid) == (root_stat.st_uid, root_stat.st_gid)
    assert stat.S_IMODE((root / ".snapshot.lock").stat().st_mode) == 0o600


def test_shard_lock_symlink_failure_releases_process_lock(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """A symlinked shard lock is refused and its in-process lock entry is removed."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "archive"
    (root / ".locks").mkdir(parents=True)
    day = date.today()
    key = (str(root), "stock_daily", "AAPL", "sample-feed", "1m", day)
    lock_name = hashlib.sha256("\0".join(map(str, key[1:])).encode()).hexdigest() + ".lock"
    outside = tmp_path / "outside.lock"
    outside.write_text("do not open", encoding="utf-8")
    (root / ".locks" / lock_name).symlink_to(outside)

    with (
        pytest.raises(MinuteArchiveIntegrityError, match="symlink"),
        archive._shard_lock(root, *key[1:]),
    ):
        pytest.fail("a symlinked per-shard lock must not be entered")

    assert outside.read_text(encoding="utf-8") == "do not open"
    assert key not in archive._SHARD_LOCKS


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (object(), "must be a mapping"),
        ({**_record("2026-06-01T09:30:00Z"), "unexpected": 1}, "invalid columns"),
        (
            {
                key: value
                for key, value in _record("2026-06-01T09:30:00Z").items()
                if key != "amount"
            },
            "invalid columns",
        ),
        (_record("2026-06-01T09:30:00Z", symbol="MSFT"), "symbol must match"),
        (_record("not-an-iso-date"), "not ISO 8601"),
        (_record("2026-06-01T09:30:00"), "timezone-aware"),
        (_record(30), "must include a timezone"),
        ({**_record("2026-06-01T09:30:00Z"), "open": True}, "open must be numeric"),
        ({**_record("2026-06-01T09:30:00Z"), "open": None}, "open must be numeric"),
        ({**_record("2026-06-01T09:30:00Z"), "open": float("nan")}, "open must be finite"),
        ({**_record("2026-06-01T09:30:00Z"), "volume": -1}, "non-negative"),
        ({**_record("2026-06-01T09:30:00Z"), "amount": -1}, "non-negative"),
        ({**_record("2026-06-01T09:30:00Z"), "high": 8}, "high is below"),
        ({**_record("2026-06-01T09:30:00Z"), "low": 11}, "low is above"),
    ],
)
def test_ingest_rejects_invalid_rows_before_creating_shards(
    archive_engine: Engine,
    tmp_path: Path,
    record: object,
    message: str,
) -> None:
    """Invalid shape, timestamps, numbers, and OHLC bounds never publish files."""
    root = tmp_path / "archive"
    with pytest.raises(MinuteArchiveValidationError, match=message):
        _ingest(archive_engine, root, [record])  # type: ignore[list-item]

    if root.exists():
        assert not list(root.rglob("*.parquet"))
    with Session(archive_engine) as session:
        assert session.scalars(select(MinuteArchiveShard)).all() == []


def test_ingest_rejects_duplicate_timestamps_and_empty_shards(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Duplicate input rows and empty batches are explicitly rejected."""
    root = tmp_path / "archive"
    row = _record("2026-06-01T09:30:00Z")
    with pytest.raises(MinuteArchiveValidationError, match="duplicate timestamp"):
        _ingest(archive_engine, root, [row, row.copy()])
    with pytest.raises(MinuteArchiveValidationError, match="at least one row"):
        _ingest(archive_engine, root, [])
    assert not list(root.rglob("*.parquet")) if root.exists() else True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"asset_class": "index"}, "route does not match"),
        ({"domain": "unknown"}, "route does not match"),
        ({"symbols": []}, "between 1 and 20"),
        ({"symbols": [f"S{i}" for i in range(21)]}, "between 1 and 20"),
        ({"symbols": ["AAPL", "AAPL"]}, "must not contain duplicates"),
        ({"symbols": ["../AAPL"]}, "path-safe"),
        ({"source": "auto"}, "explicit source"),
        ({"period": "2m"}, "period must be one of"),
        ({"start": date(2026, 6, 2), "end": date(2026, 6, 1)}, "start must not be after end"),
        ({"start": date(2026, 6, 1), "end": date(2026, 7, 2)}, "cannot exceed 31"),
        ({"page": 0}, "page must be positive"),
        ({"page_size": 0}, "page must be positive"),
        ({"page_size": 1001}, "page must be positive"),
        ({"fields": ["not_a_bar_field"]}, "unknown minute fields"),
    ],
)
def test_query_rejects_invalid_route_filter_and_projection(
    archive_engine: Engine,
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    """Invalid route, selection, date, page, and projection inputs fail closed."""
    with pytest.raises(MinuteArchiveValidationError, match=message):
        _query(archive_engine, tmp_path / "archive", **overrides)


def test_query_and_ingest_reject_symlink_and_non_directory_roots(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Configured roots cannot redirect writes or reads through filesystem links."""
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(MinuteArchiveValidationError, match="root must not be a symlink"):
        _ingest(archive_engine, linked_root, [_record(datetime.now(timezone.utc))])

    file_root = tmp_path / "not-a-directory"
    file_root.write_text("no", encoding="utf-8")
    with pytest.raises(MinuteArchiveValidationError, match="not a directory"):
        _query(archive_engine, file_root)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("schema", "unexpected schema"),
        ("timezone", "not stored in UTC"),
        ("row-count", "row count does not match"),
        ("wrong-day", "outside its indexed day"),
        ("wrong-symbol", "indexed symbol"),
        ("duplicate", "not unique and ordered"),
        ("bounds", "time bounds do not match"),
        ("missing-timestamp-field", "cannot be verified"),
    ],
)
def test_query_rejects_rehashed_parquet_that_disagrees_with_index(
    archive_engine: Engine,
    tmp_path: Path,
    case: str,
    message: str,
) -> None:
    """A valid content hash cannot hide malformed schema or index disagreement."""
    root = tmp_path / "archive"
    timestamp = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    day = timestamp.date()
    _ingest(archive_engine, root, [_record(timestamp)])
    base = _record(timestamp)
    rows = [base]
    schema: arrow.Schema | None = None
    row_count: int | None = None
    min_timestamp: datetime | None = None
    max_timestamp: datetime | None = None

    if case == "schema":
        rows = [{"symbol": "AAPL", "timestamp": timestamp}]
    elif case == "missing-timestamp-field":
        rows = [{"symbol": "AAPL", "close": 10.0}]
    elif case == "timezone":
        schema = arrow.schema(
            [
                arrow.field("symbol", arrow.string()),
                arrow.field("timestamp", arrow.timestamp("us", tz="America/New_York")),
                *(
                    arrow.field(name, arrow.float64())
                    for name in ("open", "high", "low", "close", "volume", "amount")
                ),
            ]
        )
    elif case == "row-count":
        row_count = 2
    elif case == "wrong-day":
        next_day = timestamp + timedelta(days=1)
        rows = [_record(next_day)]
    elif case == "wrong-symbol":
        rows = [_record(timestamp, symbol="MSFT")]
    elif case == "duplicate":
        rows = [base, base.copy()]
        row_count = 2
    elif case == "bounds":
        min_timestamp = timestamp + timedelta(minutes=1)
        max_timestamp = timestamp + timedelta(minutes=1)

    _replace_indexed_parquet(
        archive_engine,
        root,
        rows,
        schema=schema,
        row_count=row_count,
        min_timestamp=min_timestamp,
        max_timestamp=max_timestamp,
    )

    with pytest.raises(MinuteArchiveIntegrityError, match=message):
        _query(archive_engine, root, start=day, end=day)


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("../outside.parquet", "unsafe relative path"),
        ("other/AAPL/2026/file.parquet", "does not match its shard key"),
        ("stock_daily/AAPL/2026/not-a-content-addressed-file.parquet", "does not match its digest"),
    ],
)
def test_query_rejects_untrusted_index_paths(
    archive_engine: Engine,
    tmp_path: Path,
    relative_path: str,
    message: str,
) -> None:
    """Corrupt index paths cannot escape or redirect a query to another shard."""
    root = tmp_path / "archive"
    _seed_current_shard(archive_engine, root)
    record = _indexed_record(archive_engine)
    with Session(archive_engine) as session, session.begin():
        indexed = session.get(
            MinuteArchiveShard,
            (record.domain, record.symbol, record.source, record.period, record.day),
        )
        assert indexed is not None
        indexed.relative_path = relative_path

    with pytest.raises(MinuteArchiveIntegrityError, match=message):
        _query(archive_engine, root, start=record.day, end=record.day)


def test_query_rejects_symlinked_index_target(archive_engine: Engine, tmp_path: Path) -> None:
    """A path-correct index cannot make the reader follow a Parquet symlink."""
    root = tmp_path / "archive"
    _seed_current_shard(archive_engine, root)
    record = _indexed_record(archive_engine)
    indexed_path = root / record.relative_path
    indexed_path.unlink()
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"outside data")
    indexed_path.symlink_to(outside)

    with pytest.raises(MinuteArchiveIntegrityError, match="must not be a symlink"):
        _query(archive_engine, root, start=record.day, end=record.day)
    assert outside.read_bytes() == b"outside data"


def test_query_reports_a_missing_metadata_table_as_index_error(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """An unavailable metadata table is not represented as an empty result."""
    MinuteArchiveShard.__table__.drop(archive_engine)  # type: ignore[attr-defined]
    with pytest.raises(MinuteArchiveIndexError, match="index is unavailable"):
        _query(archive_engine, tmp_path / "archive")


def test_retention_reports_a_missing_indexed_file_after_retiring_metadata(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing expired bytes are reported instead of being counted as removed."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    indexed_path = root / _indexed_record(archive_engine).relative_path
    indexed_path.unlink()

    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert (result.candidates, result.deleted, result.files_deleted) == (1, 1, 0)
    assert any("already missing" in error for error in result.errors)
    with Session(archive_engine) as session:
        assert session.scalars(select(MinuteArchiveShard)).all() == []


def test_retention_reports_file_unlink_failure_without_claiming_file_deletion(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A filesystem permission failure remains visible in retention counts."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    indexed_path = root / _indexed_record(archive_engine).relative_path
    real_unlink = Path.unlink

    def reject_archive_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path == indexed_path:
            raise PermissionError("fixture refused archive cleanup")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_archive_unlink)
    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert (result.candidates, result.deleted, result.files_deleted) == (1, 1, 0)
    assert indexed_path.is_file()
    assert any("PermissionError" in error for error in result.errors)


def test_retention_refuses_unsafe_index_path_and_keeps_metadata(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expired but unsafe row is retained for operator repair."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    record = _indexed_record(archive_engine)
    with Session(archive_engine) as session, session.begin():
        indexed = session.get(
            MinuteArchiveShard,
            (record.domain, record.symbol, record.source, record.period, record.day),
        )
        assert indexed is not None
        indexed.relative_path = "../../outside.parquet"

    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert result.candidates == 1 and result.deleted == 0 and result.files_deleted == 0
    assert any("unsafe relative path" in error for error in result.errors)
    with Session(archive_engine) as session:
        assert (
            session.scalars(select(MinuteArchiveShard)).one().relative_path
            == "../../outside.parquet"
        )


def test_retention_delete_trigger_failure_is_counted_as_metadata_error(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed metadata transaction is reported and leaves both index and file."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    indexed_path = root / _indexed_record(archive_engine).relative_path
    with archive_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER deny_minute_archive_delete BEFORE DELETE ON minute_archive_shards "
                "BEGIN SELECT RAISE(FAIL, 'fixture denied metadata delete'); END"
            )
        )

    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert result.candidates == 1 and result.deleted == 0 and result.files_deleted == 0
    assert indexed_path.is_file()
    assert any("metadata deletion failed" in error for error in result.errors)
    with Session(archive_engine) as session:
        assert session.scalars(select(MinuteArchiveShard)).one()


def test_retention_skips_a_row_removed_while_waiting_for_its_shard_lock(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A candidate retired by another owner is not counted twice."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    record = _indexed_record(archive_engine)
    key = (record.domain, record.symbol, record.source, record.period, record.day)

    @contextmanager
    def remove_before_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
        with Session(archive_engine) as session, session.begin():
            current = session.get(MinuteArchiveShard, key)
            if current is not None:
                session.delete(current)
        yield

    monkeypatch.setattr(archive, "_shard_lock", remove_before_lock)
    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert result.candidates == 1 and result.deleted == 0 and result.files_deleted == 0
    assert not result.errors


def test_retention_rejects_symlinked_day_directory_without_following_it(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retention does not traverse an expired shard path replaced by a symlink."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "archive"
    expired_day = date.today().replace(year=date.today().year - 20, month=6, day=1)
    _ingest(
        archive_engine,
        root,
        [_record(datetime.combine(expired_day, datetime.min.time(), timezone.utc))],
    )
    symbol_dir = root / "stock_daily" / "AAPL"
    year_dir = symbol_dir / f"{expired_day.year:04d}"
    outside = tmp_path / "outside"
    outside.mkdir()
    for parquet_file in year_dir.glob("*.parquet"):
        parquet_file.unlink()
    year_dir.rmdir()
    year_dir.symlink_to(outside, target_is_directory=True)

    result = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date.today().replace(month=1, day=1),
    )

    assert result.candidates == 1 and result.deleted == 0 and result.files_deleted == 0
    assert result.errors and "symlink" in result.errors[0]
    assert list(outside.iterdir()) == []
    with Session(archive_engine) as session:
        assert session.scalars(select(MinuteArchiveShard)).one()


def test_purge_requires_positive_retention_and_existing_index_table(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Retention rejects zero years and reports an absent metadata index."""
    with pytest.raises(MinuteArchiveValidationError, match="must be positive"):
        purge_expired_minute_shards(archive_engine, tmp_path / "archive", keep_years=0)
    MinuteArchiveShard.__table__.drop(archive_engine)  # type: ignore[attr-defined]
    with pytest.raises(MinuteArchiveIndexError, match="index is unavailable"):
        purge_expired_minute_shards(archive_engine, tmp_path / "archive", keep_years=1)
