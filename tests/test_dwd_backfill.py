"""Bounded, explicit single-source ODS-to-DWD backfill tests."""

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import (
    URL,
    BigInteger,
    Column,
    Date,
    DateTime,
    Float,
    MetaData,
    SmallInteger,
    String,
    Table,
    create_engine,
    event,
)

from opendata.data.mapping import DomainMapping, require_domain_mapping
from opendata.pipeline.dwd_backfill import DwdBackfillError, backfill_dwd

STAMP = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)


class _RecordingWriter:
    """Writer test double that records each bounded DWD page."""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.frames: list[pd.DataFrame] = []
        self.tables: list[str] = []
        self.keys: list[tuple[str, ...]] = []
        self.fail_on_call = fail_on_call

    def write(self, frame: pd.DataFrame, *, table: str, key) -> int:
        if self.fail_on_call == len(self.frames) + 1:
            raise RuntimeError("injected writer failure")
        self.frames.append(frame.copy())
        self.tables.append(table)
        self.keys.append(tuple(key))
        return len(frame)


def _engine_with_tables(
    domain: str,
    source: str = "ths",
    *,
    snapshot_path: Path | None = None,
):
    """Create an isolated SQLite ODS/DWD pair matching one direct mapping."""
    mapping = require_domain_mapping(source, domain)
    metadata = MetaData()
    source_table = f"ods_{domain}_{source}"
    target_table = f"dwd_{domain}"
    time_field = next(
        field
        for field in mapping.key
        if field in {"trade_date", "ex_date", "as_of", "report_period"}
    )
    time_column = mapping.fields[time_field].source_column
    source_columns = list(dict.fromkeys(field.source_column for field in mapping.fields.values()))
    auxiliary_columns = list(
        dict.fromkeys(
            field.ms_column for field in mapping.fields.values() if field.ms_column is not None
        )
    )
    source_primary_key = list(mapping.source_key)
    if domain == "stock_action":
        auxiliary_columns.extend(("ex_date_ms", "event_key"))
        source_primary_key.append("event_key")

    ods_columns = []
    for name in (*source_primary_key, *source_columns, *auxiliary_columns):
        if any(column.name == name for column in ods_columns):
            continue
        if name == time_column:
            column_type = Date
        elif name in auxiliary_columns and name != "event_key":
            column_type = BigInteger
        elif name in {"thscode", "symbol", "股票代码", "指数代码", "成分券代码", "event_key"}:
            column_type = String(128)
        else:
            column_type = Float
        ods_columns.append(Column(name, column_type, primary_key=name in source_primary_key))
    ods = Table(source_table, metadata, *ods_columns)

    dwd_columns = []
    for name in (*mapping.key, *(field for field in mapping.fields if field not in mapping.key)):
        if name in mapping.key:
            column_type = Date if name == time_field else String(128)
        else:
            column_type = Float
        dwd_columns.append(Column(name, column_type, primary_key=name in mapping.key))
    dwd_columns.extend(
        [
            Column("source", String(32), nullable=False),
            Column("_merged_at", DateTime, nullable=False),
            Column("_diff_flag", SmallInteger, nullable=False),
            Column("_as_of", Date, nullable=False),
        ]
    )
    Table(target_table, metadata, *dwd_columns)

    if snapshot_path is None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
    else:
        sqlite_path = snapshot_path.resolve()
        engine = create_engine(URL.create("sqlite+pysqlite", database=str(sqlite_path)))

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, _connection_record):
            dbapi_connection.isolation_level = None
            dbapi_connection.execute("PRAGMA journal_mode=WAL")

        @event.listens_for(engine, "begin")
        def _begin_sqlite_transaction(connection):
            connection.exec_driver_sql("BEGIN")

    metadata.create_all(engine)
    return engine, ods, mapping, time_column, time_field


def _raw_row(
    mapping: DomainMapping,
    *,
    symbol: str,
    day: date,
    close: float = 10.0,
    timestamp: int = 0,
    event_key: str | None = None,
) -> dict[str, object]:
    """Build source-shaped rows from a direct mapping contract."""
    contract_values: dict[str, object] = {
        "symbol": symbol,
        "trade_date": day,
        "ex_date": day,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 100.0,
        "amount": 1_000.0,
        "cash_dividend": 0.2,
        "stock_dividend": 0.0,
        "rights_shares": 0.0,
        "rights_price": 0.0,
    }
    row = {
        field.source_column: contract_values[contract_field]
        for contract_field, field in mapping.fields.items()
    }
    for field in mapping.fields.values():
        if field.ms_column is not None:
            row[field.ms_column] = timestamp
    if event_key is not None:
        row["event_key"] = event_key
    return row


def _insert(engine, table: Table, rows: list[dict[str, object]]) -> None:
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)


class TestDwdBackfill:
    def test_stock_daily_reads_in_bounded_pages_and_stamps_trace_fields(self):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables("stock_daily")
        _insert(
            engine,
            ods,
            [
                _raw_row(mapping, symbol="000001.SZ", day=date(2024, 1, 2)),
                _raw_row(mapping, symbol="600519.SH", day=date(2024, 1, 2), close=20.0),
                _raw_row(mapping, symbol="600519.SH", day=date(2024, 1, 3), close=21.0),
            ],
        )
        writer = _RecordingWriter()

        stats = backfill_dwd(
            engine,
            domain="stock_daily",
            source="ths",
            start=date(2024, 1, 2),
            end=date(2024, 1, 3),
            page_size=1,
            writer=writer,
            merged_at=STAMP,
        )

        assert stats.preflight_pages == stats.pages_read == 3
        assert stats.rows_preflighted == stats.rows_read == 3
        assert stats.rows_normalized == stats.rows_merged == stats.rows_written == 3
        assert stats.collision_keys == ()
        assert len(writer.frames) == 3
        all_rows = pd.concat(writer.frames, ignore_index=True)
        assert all_rows["symbol"].tolist() == ["000001", "600519", "600519"]
        assert set(all_rows["source"]) == {"ths"}
        assert set(all_rows["_as_of"]) == {STAMP.date()}
        assert set(all_rows["_merged_at"]) == {STAMP.replace(tzinfo=None)}
        assert set(all_rows["_diff_flag"]) == {0}
        assert writer.tables == ["dwd_stock_daily"] * 3
        assert writer.keys == [("symbol", "trade_date")] * 3

    @pytest.mark.parametrize(
        ("domain", "timestamp"),
        [("index_daily", "date_ms"), ("futures_daily", "timestamp"), ("option_daily", "timestamp")],
    )
    def test_date_window_uses_contract_date_not_numeric_epoch_column(self, domain, timestamp):
        engine, ods, mapping, _time_column, time_field = _engine_with_tables(domain)
        assert mapping.fields[time_field].ms_column == timestamp
        _insert(
            engine,
            ods,
            [
                _raw_row(
                    mapping,
                    symbol="IF2610.CFE" if domain != "index_daily" else "000300.SH",
                    day=date(2024, 1, 2),
                    timestamp=0,
                ),
                _raw_row(
                    mapping,
                    symbol="IF2612.CFE" if domain != "index_daily" else "000905.SH",
                    day=date(2024, 1, 10),
                    timestamp=1_704_124_800_000,
                ),
            ],
        )
        writer = _RecordingWriter()

        stats = backfill_dwd(
            engine,
            domain=domain,
            source="ths",
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
            page_size=1,
            writer=writer,
        )

        assert stats.rows_preflighted == stats.rows_read == stats.rows_written == 1
        assert writer.frames[0]["trade_date"].tolist() == [date(2024, 1, 2)]

    def test_stock_action_filters_on_ex_date_and_not_ex_date_millis(self):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables("stock_action")
        _insert(
            engine,
            ods,
            [
                {
                    **_raw_row(
                        mapping,
                        symbol="600519.SH",
                        day=date(2024, 1, 3),
                        timestamp=0,
                        event_key="inside",
                    ),
                    "ex_date_ms": 0,
                },
                {
                    **_raw_row(
                        mapping,
                        symbol="000001.SZ",
                        day=date(2024, 1, 10),
                        timestamp=1_704_124_800_000,
                        event_key="outside",
                    ),
                    "ex_date_ms": 1_704_124_800_000,
                },
            ],
        )
        writer = _RecordingWriter()

        stats = backfill_dwd(
            engine,
            domain="stock_action",
            source="ths",
            start=date(2024, 1, 1),
            end=date(2024, 1, 5),
            page_size=1,
            writer=writer,
        )

        assert stats.rows_preflighted == stats.rows_read == stats.rows_written == 1
        assert writer.frames[0]["symbol"].tolist() == ["600519"]
        assert writer.frames[0]["ex_date"].tolist() == [date(2024, 1, 3)]

    def test_stock_action_duplicate_normalized_key_across_pages_fails_before_writes(self):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables("stock_action")
        _insert(
            engine,
            ods,
            [
                _raw_row(
                    mapping,
                    symbol="600519.SH",
                    day=date(2024, 6, 27),
                    close=0.2,
                    event_key="cash",
                ),
                _raw_row(
                    mapping,
                    symbol="600519.SZ",
                    day=date(2024, 6, 27),
                    close=0.5,
                    event_key="cash-and-bonus",
                ),
            ],
        )
        writer = _RecordingWriter()

        with pytest.raises(DwdBackfillError, match="ambiguous normalized DWD key") as raised:
            backfill_dwd(
                engine,
                domain="stock_action",
                source="ths",
                start=date(2024, 6, 27),
                end=date(2024, 6, 27),
                page_size=1,
                writer=writer,
            )

        stats = raised.value.stats
        assert stats.preflight_pages == 2
        assert stats.rows_preflighted == 2
        assert stats.pages_read == stats.rows_read == stats.rows_written == 0
        assert stats.collision_keys == ("ths: ('600519', datetime.date(2024, 6, 27))",)
        assert writer.frames == []

    def test_nonadjacent_normalized_aliases_across_pages_fail_before_writes(self):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables("stock_action")
        first = date(2024, 6, 27)
        second = date(2024, 6, 28)
        _insert(
            engine,
            ods,
            [
                _raw_row(mapping, symbol="600519", day=first, event_key="raw-1"),
                _raw_row(mapping, symbol="600519", day=second, event_key="raw-2"),
                _raw_row(mapping, symbol="600519.SH", day=first, event_key="raw-3"),
                _raw_row(mapping, symbol="600519.SH", day=second, event_key="raw-4"),
            ],
        )
        writer = _RecordingWriter()

        with pytest.raises(DwdBackfillError, match="ambiguous normalized DWD key") as raised:
            backfill_dwd(
                engine,
                domain="stock_action",
                source="ths",
                start=first,
                end=second,
                page_size=1,
                writer=writer,
            )

        stats = raised.value.stats
        assert stats.preflight_pages == 3
        assert stats.rows_preflighted == 3
        assert stats.pages_read == stats.rows_read == stats.rows_written == 0
        assert stats.collision_keys == ("ths: ('600519', datetime.date(2024, 6, 27))",)
        assert writer.frames == []

    def test_preflight_and_execution_read_the_same_source_snapshot(self, tmp_path):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables(
            "stock_action",
            snapshot_path=tmp_path / "snapshot.db",
        )
        day = date(2024, 6, 27)
        symbol_column = mapping.fields["symbol"].source_column
        _insert(
            engine,
            ods,
            [
                _raw_row(mapping, symbol="000001.SZ", day=day, event_key="first"),
                _raw_row(mapping, symbol="600519.SH", day=day, event_key="second"),
            ],
        )

        class _MutatingWriter(_RecordingWriter):
            def write(self, frame, *, table, key):
                rows_written = super().write(frame, table=table, key=key)
                if len(self.frames) == 1:
                    with engine.begin() as connection:
                        connection.execute(
                            ods.update()
                            .where(ods.c[symbol_column] == "000001.SZ")
                            .values({symbol_column: "600519.SZ"})
                        )
                return rows_written

        writer = _MutatingWriter()
        stats = backfill_dwd(
            engine,
            domain="stock_action",
            source="ths",
            start=day,
            end=day,
            page_size=1,
            writer=writer,
        )

        assert stats.rows_preflighted == stats.rows_read == stats.rows_written == 2
        assert [frame["symbol"].iloc[0] for frame in writer.frames] == ["000001", "600519"]

    def test_explicit_source_is_required_before_engine_access(self):
        with pytest.raises(ValueError, match="explicit source"):
            backfill_dwd(
                object(),
                domain="stock_daily",
                source="auto",
                start=date(2024, 1, 1),
                end=date(2024, 1, 2),
            )

    def test_later_writer_failure_reports_committed_page_for_safe_rerun(self):
        engine, ods, mapping, _time_column, _time_field = _engine_with_tables("stock_daily")
        _insert(
            engine,
            ods,
            [
                _raw_row(mapping, symbol="000001.SZ", day=date(2024, 1, 2)),
                _raw_row(mapping, symbol="600519.SH", day=date(2024, 1, 2)),
            ],
        )
        writer = _RecordingWriter(fail_on_call=2)

        with pytest.raises(DwdBackfillError, match="key-level upserts") as raised:
            backfill_dwd(
                engine,
                domain="stock_daily",
                source="ths",
                start=date(2024, 1, 2),
                end=date(2024, 1, 2),
                page_size=1,
                writer=writer,
            )

        stats = raised.value.stats
        assert stats.rows_preflighted == 2
        assert stats.rows_read == 2
        assert stats.rows_normalized == stats.rows_merged == 2
        assert stats.rows_written == 1
        assert len(writer.frames) == 1
