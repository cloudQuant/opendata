"""Behavior tests for the indexed local minute Parquet archive."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import ReferenceType, ref

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from opendata.data.minute_archive import (
    MinuteArchiveIntegrityError,
    MinuteArchiveValidationError,
    ingest_minute_shard,
    purge_expired_minute_shards,
    query_minute_archive,
)
from opendata.models.minute_archive import MinuteArchiveShard

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

#: A worker process is a cold CPython import of sqlalchemy and pandas. Under ``pytest -n 8`` that
#: start can take tens of seconds, and a short fixed deadline then fires while the worker is still
#: alive -- which the old wording reported as "never attempted the lock", a false claim about the
#: lock instead of the truth about the clock.
WORKER_STARTUP_BUDGET_S = 90.0


def _wait_for_worker_marker(process: subprocess.Popen[str], marker: Path, reason: str) -> None:
    """Block until the worker writes its own marker, or the worker is provably dead.

    Distinguishing the two outcomes is the point: a dead worker has an exit status and stderr, so
    those are reported instead of the lock claim. The worker writes the marker *before* it blocks on
    the lock, so a legitimately blocked worker still writes it and never consumes this budget.
    """
    deadline = time.monotonic() + WORKER_STARTUP_BUDGET_S
    while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.01)
    if marker.exists():
        return
    stdout, stderr = process.communicate(timeout=30)
    pytest.fail(
        f"{reason}; worker rc={process.returncode} after "
        f"{WORKER_STARTUP_BUDGET_S:.0f}s budget\n--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}",
        pytrace=False,
    )


@pytest.fixture
def archive_engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a distinct, file-backed SQLite main-database index."""
    engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}")
    MinuteArchiveShard.__table__.create(engine)
    yield engine
    engine.dispose()


def _record(timestamp: str, close: float = 10.0) -> dict[str, object]:
    """Return one canonical minute bar for a storage behavior test."""
    return {
        "symbol": "AAPL",
        "timestamp": timestamp,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 100,
        "amount": 1000,
    }


def test_content_addressed_upsert_is_idempotent_and_keeps_old_versions(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Retry and updates atomically move the index without deleting old blobs."""
    root = tmp_path / "minute_archive"
    first = _record("2025-06-01T09:30:00-04:00")
    second = _record("2025-06-01T09:31:00-04:00", close=11)
    original = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[first, second],
    )
    old_file = root / original.relative_path
    retry = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[first, second],
    )
    changed = _record("2025-06-01T09:30:00-04:00", close=12)
    updated = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[changed],
    )

    assert original.relative_path.startswith("stock_daily/AAPL/2025/")
    assert retry.row_count == 2 and retry.sha256 == original.sha256
    assert updated.row_count == 2 and updated.sha256 != original.sha256
    assert old_file.is_file()
    with archive_engine.connect() as connection:
        rows = connection.execute(select(MinuteArchiveShard)).all()
    assert len(rows) == 1
    assert rows[0].relative_path == updated.relative_path
    assert rows[0].row_count == 2


def test_microsecond_timestamp_survives_index_and_parquet_round_trip(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Metadata bounds preserve microseconds on MySQL and SQLite."""
    root = tmp_path / "minute_archive"
    timestamp = "2026-06-01T09:30:00.123456Z"
    ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record(timestamp)],
    )

    result = query_minute_archive(
        archive_engine,
        root,
        asset_class="equity",
        domain="stock_daily",
        symbols=["AAPL"],
        source="sample-feed",
        period="1m",
        start=date(2026, 6, 1),
        end=date(2026, 6, 1),
    )
    assert result.rows[0]["timestamp"] == "2026-06-01T09:30:00.123456+00:00"


def test_query_filters_window_projects_identity_and_pages_deterministically(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Real Parquet reads honor UTC day windows, field projection, and paging."""
    root = tmp_path / "minute_archive"
    ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[
            _record("2025-06-01T09:30:00-04:00"),
            _record("2025-06-01T09:31:00-04:00", close=11),
        ],
    )
    ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record("2026-06-01T09:30:00-04:00", close=12)],
    )

    page_one = query_minute_archive(
        archive_engine,
        root,
        asset_class="equity",
        domain="stock_daily",
        symbols=["AAPL"],
        source="sample-feed",
        period="1m",
        start=date(2025, 6, 1),
        end=date(2025, 6, 1),
        fields=["close"],
        page=1,
        page_size=1,
    )
    page_two = query_minute_archive(
        archive_engine,
        root,
        asset_class="equity",
        domain="stock_daily",
        symbols=["AAPL"],
        source="sample-feed",
        period="1m",
        start=date(2025, 6, 1),
        end=date(2025, 6, 1),
        fields=["close"],
        page=2,
        page_size=1,
    )

    assert page_one.total_count == 2
    assert page_one.columns == ("symbol", "timestamp", "period", "source", "close")
    assert page_one.rows[0]["close"] == 10.0
    assert page_two.rows[0]["close"] == 11.0
    assert page_one.as_dict()["layer"] == "file"
    assert page_one.as_dict()["effective_window"] == {
        "start": "2025-06-01",
        "end": "2025-06-01",
    }


def test_multi_day_multi_symbol_pages_are_sorted_without_duplicates(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Day-at-a-time shard reads page a multi-symbol range without row loss."""
    import opendata.data.minute_archive as archive

    root = tmp_path / "minute_archive"
    rows_read: list[date] = []
    live_rows: dict[date, list[ReferenceType[dict[str, object]]]] = {}
    original_read = archive._read_query_rows

    class TrackedRow(dict[str, object]):
        """Weak-referenceable row used to check day-batch release behavior."""

    def record_read(
        root_arg: Path,
        record: MinuteArchiveShard,
        *,
        start_at: datetime,
        end_at: datetime,
        columns: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if rows_read and record.day != rows_read[-1]:
            gc.collect()
            assert all(row_ref() is None for row_ref in live_rows[rows_read[-1]]), (
                "query retained a prior UTC day's shard rows"
            )
        rows_read.append(record.day)
        rows = original_read(
            root_arg,
            record,
            start_at=start_at,
            end_at=end_at,
            columns=columns,
        )
        tracked = [TrackedRow(row) for row in rows]
        live_rows.setdefault(record.day, []).extend(ref(row) for row in tracked)
        return tracked

    monkeypatch.setattr(archive, "_read_query_rows", record_read)
    expected: list[tuple[str, str]] = []
    for day, time_text in ((date(2025, 6, 1), "09:30"), (date(2025, 6, 2), "09:30")):
        timestamp = f"{day.isoformat()}T{time_text}:00Z"
        for symbol, close in (("AAPL", 10), ("MSFT", 20)):
            record = _record(timestamp, close=float(close))
            record["symbol"] = symbol
            ingest_minute_shard(
                archive_engine,
                root,
                domain="stock_daily",
                symbol=symbol,
                source="sample-feed",
                period="1m",
                records=[record],
            )
            expected.append(
                (datetime.fromisoformat(timestamp.replace("Z", "+00:00")).isoformat(), symbol)
            )

    pages = [
        query_minute_archive(
            archive_engine,
            root,
            asset_class="equity",
            domain="stock_daily",
            symbols=["MSFT", "AAPL"],
            source="sample-feed",
            period="1m",
            start=date(2025, 6, 1),
            end=date(2025, 6, 2),
            page=page,
            page_size=2,
        )
        for page in (1, 2)
    ]
    actual = [(row["timestamp"], row["symbol"]) for result in pages for row in result.rows]

    assert actual == sorted(expected)
    assert all(result.total_count == 4 and len(result.rows) == 2 for result in pages)
    assert rows_read == [date(2025, 6, 1), date(2025, 6, 1), date(2025, 6, 2), date(2025, 6, 2)] * 2


def test_concurrent_threads_merge_into_one_shard(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """The shard lock prevents same-process concurrent writers from losing rows."""
    root = tmp_path / "minute_archive"

    def write(minute: int) -> None:
        ingest_minute_shard(
            archive_engine,
            root,
            domain="stock_daily",
            symbol="AAPL",
            source="sample-feed",
            period="1m",
            records=[_record(f"2026-06-01T09:{minute:02d}:00Z", close=float(minute))],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(30, 38)))

    result = query_minute_archive(
        archive_engine,
        root,
        asset_class="equity",
        domain="stock_daily",
        symbols=["AAPL"],
        source="sample-feed",
        period="1m",
        start=date(2026, 6, 1),
        end=date(2026, 6, 1),
    )
    assert result.total_count == 8


def test_separate_process_writers_merge_without_lost_updates(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Independent importer processes share the on-disk lock for one shard."""
    root = tmp_path / "minute_archive"
    code = """
import sys
from sqlalchemy import create_engine
from opendata.data.minute_archive import ingest_minute_shard
engine = create_engine(f\"sqlite:///{sys.argv[1]}\")
minute = int(sys.argv[3])
ingest_minute_shard(
    engine,
    sys.argv[2],
    domain=\"stock_daily\",
    symbol=\"AAPL\",
    source=\"sample-feed\",
    period=\"1m\",
    records=[{
        \"symbol\": \"AAPL\",
        \"timestamp\": f\"2026-06-01T09:{minute:02d}:00Z\",
        \"open\": 10,
        \"high\": 40,
        \"low\": 8,
        \"close\": minute,
        \"volume\": 100,
        \"amount\": 1000,
    }],
)
engine.dispose()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    processes = [
        subprocess.Popen(  # noqa: S603  # nosec B603
            [sys.executable, "-c", code, str(tmp_path / "main.db"), str(root), str(minute)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for minute in (30, 31)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], outputs
    result = query_minute_archive(
        archive_engine,
        root,
        asset_class="equity",
        domain="stock_daily",
        symbols=["AAPL"],
        source="sample-feed",
        period="1m",
        start=date(2026, 6, 1),
        end=date(2026, 6, 1),
    )
    assert result.total_count == 2


def test_missing_and_corrupt_indexed_files_fail_closed(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Indexed absence or byte corruption raises an integrity error, never empty data."""
    root = tmp_path / "minute_archive"
    shard = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record("2026-06-01T09:30:00Z")],
    )
    file_path = root / shard.relative_path
    query_options = {
        "asset_class": "equity",
        "domain": "stock_daily",
        "symbols": ["AAPL"],
        "source": "sample-feed",
        "period": "1m",
        "start": date(2026, 6, 1),
        "end": date(2026, 6, 1),
    }

    file_path.write_bytes(file_path.read_bytes() + b"corrupt")
    with pytest.raises(MinuteArchiveIntegrityError, match="checksum"):
        query_minute_archive(archive_engine, root, **query_options)
    file_path.unlink()
    with pytest.raises(MinuteArchiveIntegrityError, match="missing"):
        query_minute_archive(archive_engine, root, **query_options)


def test_path_components_source_and_timezone_are_validated(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Unsafe paths, the auto source token, and naive timestamps are rejected."""
    with pytest.raises(MinuteArchiveValidationError, match="path-safe"):
        ingest_minute_shard(
            archive_engine,
            tmp_path,
            domain="stock_daily",
            symbol="../secret",
            source="sample-feed",
            period="1m",
            records=[_record("2026-06-01T09:30:00Z")],
        )
    with pytest.raises(MinuteArchiveValidationError, match="explicit source"):
        ingest_minute_shard(
            archive_engine,
            tmp_path,
            domain="stock_daily",
            symbol="AAPL",
            source="auto",
            period="1m",
            records=[_record("2026-06-01T09:30:00Z")],
        )
    naive = _record("2026-06-01T09:30:00")
    with pytest.raises(MinuteArchiveValidationError, match="timezone-aware"):
        ingest_minute_shard(
            archive_engine,
            tmp_path,
            domain="stock_daily",
            symbol="AAPL",
            source="sample-feed",
            period="1m",
            records=[naive],
        )


def test_maximum_calendar_end_is_rejected_before_exclusive_bound_overflow(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """The inclusive date API refuses a date whose next-day bound cannot exist."""
    with pytest.raises(MinuteArchiveValidationError, match="supported query range"):
        query_minute_archive(
            archive_engine,
            tmp_path / "minute_archive",
            asset_class="equity",
            domain="stock_daily",
            symbols=["AAPL"],
            source="sample-feed",
            period="1m",
            start=date.max,
            end=date.max,
        )


def test_csv_importer_streams_symbol_day_batches_and_help_needs_no_database(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """The local CLI imports sorted batches and advertises options without connecting."""
    from scripts.ops.import_minute_archive import import_csv

    csv_path = tmp_path / "minute.csv"
    csv_path.write_text(
        "symbol,timestamp,open,high,low,close,volume,amount\n"
        "AAPL,2025-06-01T09:30:00Z,9,11,8,10,100,1000\n"
        "AAPL,2025-06-02T09:30:00Z,10,12,9,11,100,1100\n"
        "MSFT,2025-06-01T09:30:00Z,19,21,18,20,100,2000\n",
        encoding="utf-8",
    )
    shards, rows = import_csv(
        csv_path,
        engine=archive_engine,
        root=tmp_path / "minute_archive",
        domain="stock_daily",
        source="local-import",
        period="1m",
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    help_result = subprocess.run(
        [sys.executable, "scripts/ops/import_minute_archive.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (shards, rows) == (3, 3)
    assert help_result.returncode == 0
    assert "--domain" in help_result.stdout and "--archive-root" in help_result.stdout


def test_symlinked_archive_subdirectory_is_refused(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """Archive reads never follow a symbol symlink outside the configured root."""
    root = tmp_path / "minute_archive"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "stock_daily").symlink_to(outside, target_is_directory=True)

    with pytest.raises(MinuteArchiveIntegrityError, match="symlink"):
        ingest_minute_shard(
            archive_engine,
            root,
            domain="stock_daily",
            symbol="AAPL",
            source="sample-feed",
            period="1m",
            records=[_record("2026-06-01T09:30:00Z")],
        )
    assert list(outside.iterdir()) == []


def test_index_publication_failure_leaves_no_visible_partial_shard(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A complete immutable orphan is harmless when its index transaction fails."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(
        archive,
        "_publish_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index unavailable")),
    )
    root = tmp_path / "minute_archive"
    with pytest.raises(RuntimeError, match="index unavailable"):
        ingest_minute_shard(
            archive_engine,
            root,
            domain="stock_daily",
            symbol="AAPL",
            source="sample-feed",
            period="1m",
            records=[_record("2026-06-01T09:30:00Z")],
        )

    with archive_engine.connect() as connection:
        assert connection.execute(select(MinuteArchiveShard)).all() == []
    files = list(root.rglob("*.parquet"))
    assert len(files) == 1
    assert pd.read_parquet(files[0]).shape == (1, 8)


def test_retention_deletes_expired_index_and_all_immutable_versions(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dry-run preserves an expired shard; execution retires its pointer and blobs."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "minute_archive"
    expired_day = date(datetime.now(timezone.utc).year - 20, 6, 1)
    original = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record(f"{expired_day.isoformat()}T09:30:00Z")],
    )
    ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record(f"{expired_day.isoformat()}T09:30:00Z", close=12)],
    )
    files = list((root / "stock_daily" / "AAPL" / str(expired_day.year)).glob("*.parquet"))
    assert len(files) == 2

    dry = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date(datetime.now(timezone.utc).year, 1, 1),
        dry_run=True,
    )
    assert dry.candidates == 1 and dry.deleted == 0 and dry.files_deleted == 0
    assert all(path.exists() for path in files)
    purged = purge_expired_minute_shards(
        archive_engine,
        root,
        keep_years=10,
        today=date(datetime.now(timezone.utc).year, 1, 1),
    )

    assert purged.candidates == 1 and purged.deleted == 1 and purged.files_deleted == 2
    assert purged.errors == ()
    assert all(not path.exists() for path in files)
    with archive_engine.connect() as connection:
        assert connection.execute(select(MinuteArchiveShard)).all() == []
    assert original.relative_path.startswith("stock_daily/AAPL/")


def test_retention_waits_for_a_reader_in_another_process(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared per-shard flock keeps retention from unlinking an active read."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "minute_archive"
    expired_day = date(datetime.now(timezone.utc).year - 20, 6, 1)
    ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[_record(f"{expired_day.isoformat()}T09:30:00Z")],
    )
    marker = tmp_path / "reader-entered-parquet"
    release = tmp_path / "release-reader"
    code = """
import sys
import time
from datetime import date
from pathlib import Path
from sqlalchemy import create_engine
import opendata.data.minute_archive as archive
original = archive._read_query_rows
def pause_read(*args, **kwargs):
    Path(sys.argv[4]).write_text(\"reading\")
    while not Path(sys.argv[5]).exists():
        time.sleep(0.01)
    return original(*args, **kwargs)
archive._read_query_rows = pause_read
engine = create_engine(f\"sqlite:///{sys.argv[1]}\")
page = archive.query_minute_archive(
    engine,
    sys.argv[2],
    asset_class=\"equity\",
    domain=\"stock_daily\",
    symbols=[\"AAPL\"],
    source=\"sample-feed\",
    period=\"1m\",
    start=date.fromisoformat(sys.argv[3]),
    end=date.fromisoformat(sys.argv[3]),
)
assert page.total_count == 1
engine.dispose()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    reader = subprocess.Popen(  # noqa: S603  # nosec B603
        [
            sys.executable,
            "-c",
            code,
            str(tmp_path / "main.db"),
            str(root),
            expired_day.isoformat(),
            str(marker),
            str(release),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_worker_marker(
        reader, marker, "reader never reached Parquet while holding its shard lock"
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        purge = executor.submit(
            purge_expired_minute_shards,
            archive_engine,
            root,
            keep_years=10,
            today=date(datetime.now(timezone.utc).year, 1, 1),
        )
        time.sleep(0.2)
        assert not purge.done()
        release.touch()
        stdout, stderr = reader.communicate(timeout=30)
        result = purge.result(timeout=30)

    assert reader.returncode == 0, (stdout, stderr)
    assert result.deleted == 1 and result.files_deleted == 1


def test_global_snapshot_lock_blocks_writer_and_expired_shard_purge_processes(
    archive_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The exclusive paired-snapshot lock blocks and then releases real workers."""
    import opendata.data.minute_archive as archive

    monkeypatch.setattr(archive, "_reject_expired_day", lambda _day: None)
    root = tmp_path / "minute_archive"
    today = date(datetime.now(timezone.utc).year, 1, 1)
    expired_day = date(today.year - 20, 6, 1)
    expired_record = _record(f"{expired_day.isoformat()}T09:30:00Z")
    expired_record["symbol"] = "MSFT"
    old_shard = ingest_minute_shard(
        archive_engine,
        root,
        domain="stock_daily",
        symbol="MSFT",
        source="sample-feed",
        period="1m",
        records=[expired_record],
    )
    expired_file = root / old_shard.relative_path

    worker_code = r"""
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from sqlalchemy import create_engine
import opendata.data.minute_archive as archive
original_lock = archive._snapshot_file_lock
@contextmanager
def observed_lock(root, *, exclusive):
    if not exclusive:
        Path(sys.argv[4]).write_text("waiting", encoding="utf-8")
    with original_lock(root, exclusive=exclusive):
        yield
archive._snapshot_file_lock = observed_lock
engine = create_engine(f"sqlite:///{sys.argv[1]}")
operation = sys.argv[3]
if operation == "writer":
    archive.ingest_minute_shard(
        engine,
        sys.argv[2],
        domain="stock_daily",
        symbol="AAPL",
        source="sample-feed",
        period="1m",
        records=[{
            "symbol": "AAPL",
            "timestamp": "2026-06-01T09:30:00Z",
            "open": 9,
            "high": 11,
            "low": 8,
            "close": 10,
            "volume": 100,
            "amount": 1000,
        }],
    )
elif operation == "purge":
    result = archive.purge_expired_minute_shards(
        engine,
        sys.argv[2],
        keep_years=10,
        today=date.fromisoformat(sys.argv[5]),
    )
    assert result.deleted == 1 and result.files_deleted == 1, result
else:
    raise AssertionError(operation)
Path(sys.argv[6]).write_text("done", encoding="utf-8")
engine.dispose()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    for operation in ("writer", "purge"):
        marker = tmp_path / f"{operation}-waiting"
        done = tmp_path / f"{operation}-done"
        with archive._snapshot_file_lock(root, exclusive=True):
            worker = subprocess.Popen(  # noqa: S603  # nosec B603
                [
                    sys.executable,
                    "-c",
                    worker_code,
                    str(tmp_path / "main.db"),
                    str(root),
                    operation,
                    str(marker),
                    today.isoformat(),
                    str(done),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_for_worker_marker(
                worker, marker, f"{operation} never attempted the global shared lock"
            )
            time.sleep(0.2)
            assert not done.exists(), f"{operation} passed an exclusive snapshot lock"
            if operation == "purge":
                assert expired_file is not None and expired_file.exists()

        stdout, stderr = worker.communicate(timeout=30)
        assert worker.returncode == 0, (operation, stdout, stderr)
        assert done.is_file()
        if operation == "purge":
            assert expired_file is not None and not expired_file.exists()


def test_a_dead_worker_is_reported_dead_not_as_a_lock_miss(tmp_path: Path) -> None:
    """Counterfact for the startup budget: a worker that died must fail fast, not wait 90 s.

    Without this arm the larger budget would read like a relaxation -- a genuinely broken worker
    would simply take 90 s longer before someone called it a lock miss. The message carries the
    exit status, so a crash is never again reported as a lock-protocol finding.
    """
    started = time.monotonic()
    worker = subprocess.Popen(  # noqa: S603  # nosec B603
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); raise SystemExit(3)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with pytest.raises(pytest.fail.Exception, match="rc=3") as caught:
        _wait_for_worker_marker(worker, tmp_path / "never-written", "never attempted the lock")
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"dead worker took {elapsed:.1f}s -- the budget is not the exit path"
    assert "boom" in str(caught.value), "the worker's stderr was dropped from the report"
