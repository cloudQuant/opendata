"""Mapped ODS watermarks and their finite incremental windows."""

from datetime import date

from sqlalchemy import Column, Date, Integer, MetaData, String, Table, create_engine, insert

from opendata.pipeline.ods_watermark import (
    effective_symbol_windows,
    read_ods_watermarks,
)
from opendata.pipeline.runner import Window


def test_reads_latest_mapped_ods_date_for_each_requested_symbol() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table(
        "ods_stock_daily_akshare",
        metadata,
        Column("股票代码", String, nullable=False),
        Column("日期", Date, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(table),
            [
                {"股票代码": "600519.SH", "日期": date(2026, 9, 20)},
                {"股票代码": "600519.SH", "日期": date(2026, 9, 24)},
                # A future row must not push the watermark past the request.
                {"股票代码": "600519.SH", "日期": date(2026, 9, 30)},
                {"股票代码": "000001.SZ", "日期": date(2026, 9, 18)},
            ],
        )

    watermarks = read_ods_watermarks(
        engine,
        "stock_daily",
        "akshare",
        ["600519", "000001", "300001"],
        end=date(2026, 9, 24),
    )

    assert watermarks == {
        "600519": date(2026, 9, 24),
        "000001": date(2026, 9, 18),
    }


def test_absent_ods_table_is_an_empty_watermark_set() -> None:
    engine = create_engine("sqlite://")

    assert (
        read_ods_watermarks(
            engine,
            "stock_daily",
            "akshare",
            ["600519"],
            end=date(2026, 9, 24),
        )
        == {}
    )


def test_futures_watermark_uses_the_mapped_trade_date_not_epoch_ms() -> None:
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table(
        "ods_futures_daily_ths",
        metadata,
        Column("thscode", String, nullable=False),
        Column("trade_date", Date, nullable=False),
        Column("timestamp", Integer, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(table),
            [
                {
                    "thscode": "IF2610.CFE",
                    "trade_date": date(2026, 9, 23),
                    "timestamp": 1_790_121_600_000,
                }
            ],
        )

    assert read_ods_watermarks(
        engine,
        "futures_daily",
        "ths",
        ["IF2610.CFE"],
        end=date(2026, 9, 24),
    ) == {"IF2610.CFE": date(2026, 9, 23)}


def test_per_symbol_windows_catch_up_and_preserve_lookback_without_future_growth() -> None:
    base = Window(start=date(2026, 9, 22), end=date(2026, 9, 24))

    windows = effective_symbol_windows(
        ["fresh", "stale", "current", "future"],
        base,
        {
            "stale": date(2026, 9, 18),
            "current": base.end,
            "future": date(2026, 9, 30),
        },
        lookback_days=2,
    )

    assert windows == {
        "fresh": base,
        "stale": Window(start=date(2026, 9, 17), end=base.end),
        "current": Window(start=date(2026, 9, 22), end=base.end),
        "future": Window(start=date(2026, 9, 22), end=base.end),
    }
    no_lookback = effective_symbol_windows(
        ["current", "future"],
        base,
        {"current": base.end, "future": date(2026, 9, 30)},
    )
    assert no_lookback == {"current": None, "future": None}


def test_saturated_dates_do_not_overflow_or_extend_requested_end() -> None:
    latest = date.max
    at_max = Window(start=latest, end=latest)

    assert effective_symbol_windows(["max"], at_max, {"max": latest}, lookback_days=3) == {
        "max": Window(start=date(9999, 12, 28), end=latest)
    }
    at_min = Window(start=date.min, end=date.min)
    assert effective_symbol_windows(["min"], at_min, {"min": date.min}, lookback_days=3) == {
        "min": at_min
    }
