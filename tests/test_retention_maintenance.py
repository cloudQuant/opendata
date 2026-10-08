"""Behavior tests for scheduled, bounded retention maintenance."""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, create_engine, insert, select
from sqlalchemy.orm import Session

from opendata.models.minute_archive import MinuteArchiveShard
from opendata.pipeline.maintenance import run_retention

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine


@pytest.fixture
def sqlite_warehouse() -> Iterator[Engine]:
    """Create an isolated SQLite warehouse with bounded and permanent rows."""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    diff_report = Table(
        "dq_diff_report",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("checked_at", DateTime(timezone=True), nullable=False),
    )
    permanent = Table(
        "dwd_daily_stock",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("checked_at", DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    cutoff_sample = datetime.now(timezone.utc) - timedelta(days=365)
    with engine.begin() as connection:
        connection.execute(insert(diff_report), [{"checked_at": cutoff_sample}])
        connection.execute(insert(diff_report), [{"checked_at": datetime.now(timezone.utc)}])
        connection.execute(insert(permanent), [{"checked_at": cutoff_sample}])
    yield engine
    engine.dispose()


@pytest.fixture
def sqlite_minute_index(tmp_path: Path) -> Iterator[Engine]:
    """Create a distinct isolated SQLite metadata index for minute files."""
    engine = create_engine(f"sqlite:///{tmp_path / 'main-metadata.db'}")
    MinuteArchiveShard.__table__.create(engine)
    yield engine
    engine.dispose()


def _seed_minute_file(root: Path, engine: Engine, day: date) -> Path:
    """Create an indexed test shard without relying on production data paths."""
    digest = f"{day.year % 10}" * 64
    relative_path = (
        f"stock_daily/600519/{day.year:04d}/{day.isoformat()}_sample-feed_1m_{digest}.parquet"
    )
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"isolated test shard")
    timestamp = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    with Session(engine) as session, session.begin():
        session.add(
            MinuteArchiveShard(
                domain="stock_daily",
                symbol="600519",
                source="sample-feed",
                period="1m",
                day=day,
                relative_path=relative_path,
                row_count=1,
                min_timestamp=timestamp,
                max_timestamp=timestamp,
                sha256=digest,
            )
        )
    return path


def test_retention_dry_run_reports_candidates_without_deleting(
    sqlite_warehouse: Engine,
    sqlite_minute_index: Engine,
    tmp_path: Path,
) -> None:
    """The default run reports old bounded data and preserves every file/row."""
    now = time.time()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    stale_cache = cache_root / "stale.response"
    fresh_cache = cache_root / "fresh.response"
    protected_env = cache_root / ".env"
    for path in (stale_cache, fresh_cache, protected_env):
        path.write_text("test-only", encoding="utf-8")
    os.utime(stale_cache, (now - 3600, now - 3600))
    os.utime(protected_env, (now - 3600, now - 3600))

    minute_root = tmp_path / "minute_archive"
    stale_day = date(2000, 1, 1)
    fresh_day = date.today().replace(month=1, day=1)
    stale_archive = _seed_minute_file(minute_root, sqlite_minute_index, stale_day)
    fresh_archive = _seed_minute_file(minute_root, sqlite_minute_index, fresh_day)

    result = run_retention(
        sqlite_warehouse,
        cache_root=cache_root,
        minute_root=minute_root,
        minute_metadata_engine=sqlite_minute_index,
    )

    actions = {action.target: action for action in result.actions}
    assert result.dry_run is True
    assert actions["dq_diff_report"].candidates == 1
    assert actions["minute-archive"].candidates == 1
    assert actions["raw-response-cache"].candidates == 1
    assert all(action.deleted == 0 for action in result.actions)
    assert stale_cache.exists() and protected_env.exists() and fresh_cache.exists()
    assert stale_archive.exists() and fresh_archive.exists()
    with sqlite_minute_index.connect() as connection:
        assert len(connection.execute(select(MinuteArchiveShard.symbol)).all()) == 2
    metadata = MetaData()
    diff_report = Table("dq_diff_report", metadata, autoload_with=sqlite_warehouse)
    permanent = Table("dwd_daily_stock", metadata, autoload_with=sqlite_warehouse)
    with sqlite_warehouse.connect() as connection:
        assert len(connection.execute(select(diff_report.c.id)).all()) == 2
        assert len(connection.execute(select(permanent.c.id)).all()) == 1


def test_enabled_retention_deletes_only_expired_bounded_targets(
    sqlite_warehouse: Engine,
    sqlite_minute_index: Engine,
    tmp_path: Path,
) -> None:
    """Explicit execution removes expired targets and leaves permanent data."""
    now = time.time()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    stale_cache = cache_root / "stale.response"
    fresh_cache = cache_root / "fresh.response"
    protected_env = cache_root / ".env"
    for path in (stale_cache, fresh_cache, protected_env):
        path.write_text("test-only", encoding="utf-8")
    os.utime(stale_cache, (now - 3600, now - 3600))
    os.utime(protected_env, (now - 3600, now - 3600))

    minute_root = tmp_path / "minute_archive"
    stale_archive = _seed_minute_file(minute_root, sqlite_minute_index, date(2000, 1, 1))
    fresh_archive = _seed_minute_file(
        minute_root,
        sqlite_minute_index,
        date.today().replace(month=1, day=1),
    )

    result = run_retention(
        sqlite_warehouse,
        dry_run=False,
        cache_root=cache_root,
        minute_root=minute_root,
        minute_metadata_engine=sqlite_minute_index,
    )

    actions = {action.target: action for action in result.actions}
    assert result.dry_run is False
    assert actions["dq_diff_report"].candidates == 1
    assert actions["dq_diff_report"].deleted == 1
    assert actions["minute-archive"].candidates == 1
    assert actions["minute-archive"].deleted == 1
    assert actions["raw-response-cache"].candidates == 1
    assert actions["raw-response-cache"].deleted == 1
    assert not stale_cache.exists()
    assert fresh_cache.exists() and protected_env.exists()
    assert not stale_archive.exists() and fresh_archive.exists()
    with sqlite_minute_index.connect() as connection:
        assert len(connection.execute(select(MinuteArchiveShard.symbol)).all()) == 1

    metadata = MetaData()
    permanent = Table("dwd_daily_stock", metadata, autoload_with=sqlite_warehouse)
    with sqlite_warehouse.connect() as connection:
        assert len(connection.execute(select(permanent.c.id)).all()) == 1
        diff_report = Table("dq_diff_report", metadata, autoload_with=sqlite_warehouse)
        assert len(connection.execute(select(diff_report.c.id)).all()) == 1


def test_default_cache_scope_preserves_sibling_application_caches(
    sqlite_warehouse: Engine,
    sqlite_minute_index: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The scheduler only deletes from CACHE_DIR/raw_responses."""
    from opendata.core.config import settings

    cache_root = tmp_path / "cache"
    unrelated_cache = cache_root / "api-cache.json"
    raw_response = cache_root / "raw_responses" / "stale.response"
    unrelated_cache.parent.mkdir(parents=True)
    raw_response.parent.mkdir()
    unrelated_cache.write_text("unrelated", encoding="utf-8")
    raw_response.write_text("raw response", encoding="utf-8")
    stale_time = time.time() - 3600
    os.utime(unrelated_cache, (stale_time, stale_time))
    os.utime(raw_response, (stale_time, stale_time))
    monkeypatch.setattr(settings, "cache_dir", cache_root)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    result = run_retention(
        sqlite_warehouse,
        dry_run=False,
        minute_metadata_engine=sqlite_minute_index,
    )

    cache_action = next(
        action for action in result.actions if action.target == "raw-response-cache"
    )
    assert cache_action.candidates == 1
    assert cache_action.deleted == 1
    assert unrelated_cache.exists()
    assert not raw_response.exists()
