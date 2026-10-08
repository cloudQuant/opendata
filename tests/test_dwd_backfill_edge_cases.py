"""Boundary contracts for the bounded ODS-to-DWD backfill."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine

from opendata.pipeline.dwd_backfill import backfill_dwd
from tests.test_dwd_backfill import STAMP, _engine_with_tables, _RecordingWriter


@pytest.mark.parametrize(
    ("start", "end", "page_size", "message"),
    [
        (datetime(2024, 1, 1), date(2024, 1, 2), 10, "date values"),
        (date(2024, 1, 3), date(2024, 1, 2), 10, "is after end"),
        (date(2024, 1, 1), date(2024, 1, 2), 0, "page_size must be positive"),
    ],
    ids=("datetime-is-not-date", "reversed-window", "zero-page-size"),
)
def test_invalid_selection_is_refused_before_engine_access(
    start: date | datetime,
    end: date,
    page_size: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        backfill_dwd(
            object(),
            domain="stock_daily",
            source="ths",
            start=start,
            end=end,
            page_size=page_size,
        )


def test_empty_selection_has_zero_progress_and_json_ready_dates() -> None:
    engine, _ods, _mapping, _time_column, _time_field = _engine_with_tables("stock_daily")
    writer = _RecordingWriter()
    start = date(2024, 1, 2)
    end = date(2024, 1, 3)
    try:
        stats = backfill_dwd(
            engine,
            domain="stock_daily",
            source="ths",
            start=start,
            end=end,
            page_size=1,
            writer=writer,
            merged_at=STAMP,
        )
    finally:
        engine.dispose()

    serialized = json.loads(json.dumps(stats.as_dict()))
    assert serialized["start"] == "2024-01-02"
    assert serialized["end"] == "2024-01-03"
    assert serialized["preflight_pages"] == 0
    assert serialized["rows_preflighted"] == 0
    assert serialized["pages_read"] == 0
    assert serialized["rows_read"] == 0
    assert serialized["rows_written"] == 0
    assert serialized["collision_keys"] == []
    assert writer.frames == []


def test_naive_merge_timestamp_fails_before_source_scan_or_write() -> None:
    engine, _ods, _mapping, _time_column, _time_field = _engine_with_tables("stock_daily")
    writer = _RecordingWriter()
    try:
        with pytest.raises(ValueError, match="merged_at must be timezone-aware"):
            backfill_dwd(
                engine,
                domain="stock_daily",
                source="ths",
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                writer=writer,
                merged_at=datetime(2026, 9, 30, 12, 30),
            )
    finally:
        engine.dispose()

    assert writer.frames == []


def test_missing_warehouse_tables_fail_closed_without_writes() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    writer = _RecordingWriter()
    try:
        with pytest.raises(LookupError, match="required warehouse tables are missing"):
            backfill_dwd(
                engine,
                domain="stock_daily",
                source="ths",
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                writer=writer,
                merged_at=STAMP,
            )
    finally:
        engine.dispose()

    assert writer.frames == []
