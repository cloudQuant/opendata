"""Bounded affine query and official-check coverage on temporary SQLite stores."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    insert,
    update,
)

from opendata.api.data_query import _factor_rows
from opendata.pipeline.query import apply_adjust_to_rows

DAY = date(2026, 9, 24)


def _factor_table(metadata: MetaData, *, affine: bool, lineage: bool = False) -> Table:
    columns = [
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("qfq_factor", Float, nullable=False),
        Column("hfq_factor", Float, nullable=False),
    ]
    if affine:
        columns.extend(
            [
                Column("qfq_scale", Float),
                Column("qfq_offset", Float),
                Column("hfq_scale", Float),
                Column("hfq_offset", Float),
                Column("adjustment_version", String(32)),
                Column("legacy_source", String(64)),
            ]
        )
    if lineage:
        columns.extend(
            [
                Column("source", String(64)),
                Column("_merged_at", DateTime),
                Column("_diff_flag", Integer),
                Column("_as_of", Date),
            ]
        )
    return Table("dwd_stock_adjust", metadata, *columns)


def test_factor_rows_reads_legacy_schema_without_affine_columns():
    metadata = MetaData()
    factors = _factor_table(metadata, affine=False)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(factors),
            {
                "symbol": "600519",
                "trade_date": DAY,
                "qfq_factor": 0.9,
                "hfq_factor": 1.1,
            },
        )

    try:
        rows = _factor_rows(engine, ["600519"])
    finally:
        engine.dispose()

    assert rows == [
        {
            "symbol": "600519",
            "trade_date": DAY.isoformat(),
            "qfq_factor": 0.9,
            "hfq_factor": 1.1,
        }
    ]


def test_factor_rows_keeps_affine_contract_fields_and_filters_dwd_lineage():
    metadata = MetaData()
    factors = _factor_table(metadata, affine=True, lineage=True)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(factors),
            {
                "symbol": "600519",
                "trade_date": DAY,
                "qfq_factor": 0.9,
                "hfq_factor": 1.1,
                "qfq_scale": 1.0,
                "qfq_offset": -5.0,
                "hfq_scale": 1.0,
                "hfq_offset": 5.0,
                "adjustment_version": "affine-v1",
                "legacy_source": "corporate-action-ratio-v1",
                "source": "corporate-action-affine-v1",
                "_merged_at": datetime(2026, 9, 24, 12, 0),
                "_diff_flag": 0,
                "_as_of": DAY,
            },
        )

    try:
        rows = _factor_rows(engine, ["600519"])
    finally:
        engine.dispose()

    assert rows == [
        {
            "symbol": "600519",
            "trade_date": DAY.isoformat(),
            "qfq_factor": 0.9,
            "hfq_factor": 1.1,
            "qfq_scale": 1.0,
            "qfq_offset": -5.0,
            "hfq_scale": 1.0,
            "hfq_offset": 5.0,
            "adjustment_version": "affine-v1",
            "legacy_source": "corporate-action-ratio-v1",
        }
    ]


def test_factor_rows_binds_actual_bar_keys_and_bounds_sql_batches():
    metadata = MetaData()
    factors = _factor_table(metadata, affine=True)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    orphan_day = DAY - timedelta(days=1)
    selected_days = [DAY + timedelta(days=index) for index in range(401)]
    with engine.begin() as connection:
        connection.execute(
            insert(factors),
            [
                {
                    "symbol": "600519",
                    "trade_date": trade_day,
                    "qfq_factor": 0.9,
                    "hfq_factor": 1.1,
                    "qfq_scale": 1.0,
                    "qfq_offset": 0.0,
                    "hfq_scale": 1.0,
                    "hfq_offset": 0.0,
                    "adjustment_version": "affine-v1",
                }
                for trade_day in [*selected_days, orphan_day]
            ],
        )

    statements: list[tuple[str, object]] = []

    def record_factor_select(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT * FROM `dwd_stock_adjust`"):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record_factor_select)
    try:
        rows = _factor_rows(
            engine,
            ["600519"],
            bar_keys=[("600519", trade_day) for trade_day in selected_days],
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_factor_select)
        engine.dispose()

    assert rows is not None
    assert len(rows) == len(selected_days)
    assert len(statements) == 2
    assert [statement.count("`trade_date` =") for statement, _ in statements] == [400, 1]
    assert all(len(parameters) <= 800 for _, parameters in statements)
    assert all(orphan_day.isoformat() not in repr(parameters) for _, parameters in statements)


def test_affine_adjust_uses_offset_and_rejects_duplicate_factors():
    row = {
        "symbol": "600519",
        "trade_date": DAY,
        "open": 100.0,
        "high": 120.0,
        "low": 90.0,
        "close": 110.0,
        "volume": 300.0,
        "amount": 33_000.0,
    }
    factor = {
        "symbol": "600519",
        "trade_date": DAY,
        # Deliberately differs from the affine result to prove that the new version
        # does not silently keep multiplying by the preserved legacy ratio.
        "qfq_factor": 0.5,
        "hfq_factor": 2.0,
        "qfq_scale": 1.0,
        "qfq_offset": -5.0,
        "hfq_scale": 1.0,
        "hfq_offset": 5.0,
        "adjustment_version": "affine-v1",
        "legacy_source": "corporate-action-ratio-v1",
    }

    adjusted = apply_adjust_to_rows("stock_daily", [row], method="qfq", factors=[factor])

    assert (adjusted[0]["open"], adjusted[0]["high"]) == (95.0, 115.0)
    assert (adjusted[0]["low"], adjusted[0]["close"]) == (85.0, 105.0)
    assert (adjusted[0]["volume"], adjusted[0]["amount"]) == (300.0, 33_000.0)
    assert set(adjusted[0]) == set(row)
    with pytest.raises(ValueError, match="duplicate adjust factor"):
        apply_adjust_to_rows("stock_daily", [row], method="qfq", factors=[factor, factor])


def test_official_checker_reads_and_applies_affine_coefficients_from_factor_table():
    from scripts.ops.qfq_official_check import _compare, _server_side_series

    metadata = MetaData()
    bars = Table(
        "dwd_stock_daily",
        metadata,
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("open", Float, nullable=False),
        Column("high", Float, nullable=False),
        Column("low", Float, nullable=False),
        Column("close", Float, nullable=False),
        Column("volume", Float, nullable=False),
        Column("amount", Float, nullable=False),
        Column("source", String(32), nullable=False),
        Column("_merged_at", DateTime, nullable=False),
        Column("_diff_flag", Integer, nullable=False),
        Column("_as_of", Date),
    )
    factors = _factor_table(metadata, affine=True, lineage=True)
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    next_day = DAY + timedelta(days=1)
    with engine.begin() as connection:
        connection.execute(
            insert(bars),
            [
                {
                    "symbol": "600519",
                    "trade_date": DAY,
                    "open": 100.0,
                    "high": 120.0,
                    "low": 90.0,
                    "close": 110.0,
                    "volume": 300.0,
                    "amount": 33_000.0,
                    "source": "ths",
                    "_merged_at": datetime(2026, 9, 24, 12, 0),
                    "_diff_flag": 0,
                    "_as_of": DAY,
                },
                {
                    "symbol": "600519",
                    "trade_date": next_day,
                    "open": 110.0,
                    "high": 130.0,
                    "low": 100.0,
                    "close": 120.0,
                    "volume": 400.0,
                    "amount": 48_000.0,
                    "source": "ths",
                    "_merged_at": datetime(2026, 9, 25, 12, 0),
                    "_diff_flag": 0,
                    "_as_of": next_day,
                },
            ],
        )
        connection.execute(
            insert(factors),
            [
                {
                    "symbol": "600519",
                    "trade_date": day,
                    "qfq_factor": 0.5,
                    "hfq_factor": 2.0,
                    "qfq_scale": 1.0,
                    "qfq_offset": -5.0,
                    "hfq_scale": 1.0,
                    "hfq_offset": 5.0,
                    "adjustment_version": "affine-v1",
                    "legacy_source": "corporate-action-ratio-v1",
                    "source": "corporate-action-affine-v1",
                    "_merged_at": datetime(2026, 9, 24, 12, 0),
                    "_diff_flag": 0,
                    "_as_of": day,
                }
                for day in (DAY, next_day)
            ],
        )

    try:
        adjusted = _server_side_series(engine, "600519", DAY, next_day, "qfq")
        with engine.begin() as connection:
            connection.execute(
                update(factors)
                .where(factors.c.symbol == "600519", factors.c.trade_date == next_day)
                .values(qfq_offset=0.0)
            )
        wrong = _server_side_series(engine, "600519", DAY, next_day, "qfq")
    finally:
        engine.dispose()

    assert (adjusted[0]["open"], adjusted[0]["high"]) == (95.0, 115.0)
    assert (adjusted[0]["low"], adjusted[0]["close"]) == (85.0, 105.0)
    assert adjusted[1]["close"] == 115.0
    assert wrong[1]["close"] == 120.0
    official = {DAY.isoformat(): 105.0, next_day.isoformat(): 115.0}
    assert _compare(adjusted, official, tolerance=2e-3)["failed"] is False
    assert _compare(wrong, official, tolerance=2e-3)["failed"] is True
