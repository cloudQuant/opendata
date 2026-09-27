"""P0 pipeline template and scheduler switch tests (A4.7, design §9.3).

Two verifiable pieces of A4.7 land here (the full-market dual-source
dry run stays blocked on the A3 THS credentials and the eastmoney
network):

* ``scheduler_decision`` - the explicit ``ENABLE_SCHEDULER`` switch.
  Production without an explicit value fails at startup (no more
  silent disabling through the ``workers > 1`` inference), Redis
  missing with multiple workers disables the scheduler with a warning
  instead of splitting the brain;
* the built-in schedule templates and the stock-daily pipeline
  factory that wires the A4.2/4.4/4.5/4.6 services into one runnable
  template.
"""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
)

from opendata.data.domains import dwd_table
from opendata.data.models import Instrument, TradingCalendar
from opendata.pipeline import templates
from opendata.pipeline.runner import Window
from opendata.pipeline.scheduling import (
    SchedulerConfigError,
    SchedulerDecision,
    scheduler_decision,
)
from opendata.pipeline.templates import (
    PIPELINE_TEMPLATES,
    TemplateKind,
    build_stock_daily_pipeline,
    incremental_window,
    normalize_ods_rows,
    refresh_metadata_backbone,
)

#: Column types the backbone dwd tables are built with in the test warehouse.
_BACKBONE_TYPES = {
    "symbol": String(32),
    "exchange": String(16),
    "name": String(64),
    "status": String(16),
    "currency": String(8),
    "board": String(32),
    "source": String(32),
    "date": Date(),
    "list_date": Date(),
    "delist_date": Date(),
    "prev_trade_date": Date(),
    "next_trade_date": Date(),
    "is_open": Boolean(),
    "_diff_flag": Integer(),
    "_merged_at": DateTime(),
    "_as_of": Date(),
}


def _warehouse_with(landed: dict[str, pd.DataFrame]) -> Engine:
    """A stand-in warehouse holding exactly the frames the backbone landed."""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    tables = {
        domain: Table(
            dwd_table(domain),
            metadata,
            *[Column(name, _BACKBONE_TYPES[name]) for name in frame.columns],
        )
        for domain, frame in landed.items()
    }
    metadata.create_all(engine)
    with engine.begin() as connection:
        for domain, frame in landed.items():
            records = frame.astype(object).where(frame.notna(), None).to_dict("records")
            connection.execute(tables[domain].insert(), records)
    return engine


class TestSchedulerDecision:
    def test_production_without_an_explicit_value_fails_startup(self):
        with pytest.raises(SchedulerConfigError, match="ENABLE_SCHEDULER"):
            scheduler_decision(enable_scheduler=None, is_production=True, redis_url=None, workers=1)

    def test_development_defaults_to_enabled(self):
        decision = scheduler_decision(
            enable_scheduler=None, is_production=False, redis_url=None, workers=1
        )

        assert decision == SchedulerDecision(
            True, "development default (explicit ENABLE_SCHEDULER unset)"
        )

    def test_explicit_disable_wins(self):
        decision = scheduler_decision(
            enable_scheduler=False, is_production=True, redis_url="redis://x", workers=1
        )

        assert decision.enabled is False
        assert "disabled" in decision.reason

    def test_multi_worker_without_redis_disables_with_a_warning_reason(self):
        decision = scheduler_decision(
            enable_scheduler=True, is_production=True, redis_url=None, workers=4
        )

        assert decision.enabled is False
        assert "Redis" in decision.reason

    def test_settings_default_is_unset_not_true(self):
        """ENABLE_SCHEDULER defaults to unset so production fails closed."""
        from opendata.core.config import Settings

        assert Settings.model_fields["enable_scheduler"].default is None

    def test_lifespan_uses_the_explicit_decision(self):
        """The startup path must consult scheduler_decision, not workers."""
        from pathlib import Path as PathLib

        source = PathLib("opendata/main.py").read_text(encoding="utf-8")

        assert "scheduler_decision(" in source
        assert "settings.workers > 1 and not settings.redis_url" not in source

    def test_multi_worker_with_redis_is_enabled(self):
        decision = scheduler_decision(
            enable_scheduler=True, is_production=True, redis_url="redis://x", workers=4
        )

        assert decision.enabled is True


class TestScheduleTemplates:
    def test_covers_the_built_in_jobs(self):
        kinds = {template.kind for template in PIPELINE_TEMPLATES}

        assert kinds == {
            TemplateKind.INCREMENTAL,
            TemplateKind.FULL_CHECK,
            TemplateKind.PARTITION_MAINTENANCE,
            TemplateKind.FRESHNESS,
        }

    def test_incremental_template_targets_the_p0_domain(self):
        template = next(
            item for item in PIPELINE_TEMPLATES if item.kind is TemplateKind.INCREMENTAL
        )

        assert template.payload["domain"] == "stock_daily"
        assert template.payload["source"] == "akshare"
        assert "17:00" in template.note or "18:00" in template.note  # 错峰窗口待真机校准

    def test_templates_carry_a_cron_and_a_name(self):
        for template in PIPELINE_TEMPLATES:
            assert template.name
            assert template.cron.count(" ") == 4  # 标准五段 cron


class TestPipelineFactory:
    def test_wires_the_six_step_services(self):
        from opendata.pipeline.dwd_merge import DwdMergeService
        from opendata.pipeline.runner import DataPipeline

        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]  # not touched by the factory
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
        )

        assert isinstance(pipeline, DataPipeline)
        assert pipeline.spec.domain == "stock_daily"
        assert pipeline.spec.source == "akshare"
        assert pipeline.spec.table == "ods_stock_daily_akshare"
        # The hooks are bound methods of the step services.
        assert isinstance(pipeline.merge.__self__, DwdMergeService)
        # Cross-check needs a second source: the A3 THS feed.
        assert pipeline.cross_check is None

    def test_cross_check_is_wired_when_a_second_source_exists(self):
        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
            second_source="ths",
        )

        from opendata.pipeline.cross_check_service import CrossCheckService

        assert isinstance(pipeline.cross_check.__self__, CrossCheckService)

    def test_incremental_window_covers_the_requested_days(self):
        window = incremental_window(date(2024, 1, 8))

        assert window.start == window.end == date(2024, 1, 8)
        assert incremental_window(date(2024, 1, 8), lookback_days=3).start == date(2024, 1, 5)

    def test_a_calendar_decides_where_the_window_ends(self):
        """A4.7: the window ends on the date data is expected for, not on the clock."""
        from opendata.pipeline.trading_calendar import weekday_calendar

        calendar = weekday_calendar()

        assert incremental_window(date(2024, 1, 7), calendar=calendar).end == date(2024, 1, 5)
        assert incremental_window(
            date(2024, 1, 7), lookback_days=2, calendar=calendar
        ).start == date(2024, 1, 3)

    def test_a_calendar_with_no_trading_day_in_reach_fails_closed(self):
        """A month of dead days is a broken table, not a window four weeks wide."""
        from opendata.pipeline.trading_calendar import TIER_WAREHOUSE, CalendarView

        broken = CalendarView(
            tier=TIER_WAREHOUSE,
            open_days=frozenset({date(2023, 11, 24), date(2024, 1, 9)}),
        )

        with pytest.raises(ValueError, match="looks broken"):
            incremental_window(date(2024, 1, 8), calendar=broken)


class TestMetadataBackbone:
    """AC-2|02: the step zero a full-market backfill starts with (§4.1)."""

    WINDOW = Window(start=date(2026, 9, 21), end=date(2026, 9, 25))

    @staticmethod
    def _instrument(**overrides) -> Instrument:
        fields = {
            "symbol": "600519.SH",
            "exchange": "SSE",
            "name": "贵州茅台",
            "status": "active",
            "currency": "CNY",
            "list_date": date(2001, 8, 1),
            "delist_date": None,
            "board": None,
        }
        fields.update(overrides)
        return Instrument.model_validate(fields)

    @staticmethod
    def _calendar(day: date, *, open_day: bool = True, prev: date | None = None) -> TradingCalendar:
        return TradingCalendar.model_validate(
            {
                "exchange": "CN-SSE",
                "date": day,
                "is_open": open_day,
                "prev_trade_date": prev,
                "next_trade_date": None,
            }
        )

    def _refresh(self, *, instruments=(), calendar=(), land=None):
        return refresh_metadata_backbone(
            object(),  # the engine only reaches the default writer
            source="ths",
            fetch_instruments=lambda: list(instruments),
            fetch_calendar=lambda: list(calendar),
            window=self.WINDOW,
            land=land if land is not None else lambda domain, frame, key: len(frame),
            merged_at=datetime(2026, 9, 26, 17, 0),
        )

    def test_the_landed_frame_is_the_contract_plus_the_trace_columns(self):
        written: dict[str, tuple[str, ...]] = {}

        def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
            written[domain] = tuple(frame.columns)
            assert frame["source"].unique().tolist() == ["ths"]
            assert frame["_diff_flag"].unique().tolist() == [0]
            assert frame["_as_of"].unique().tolist() == [self.WINDOW.end]
            assert frame["_merged_at"].unique().tolist() == [datetime(2026, 9, 26, 17, 0)]
            return len(frame)

        report = self._refresh(
            instruments=[self._instrument()],
            calendar=[self._calendar(date(2026, 9, 25))],
            land=land,
        )

        assert written["instrument"][:8] == (
            "symbol",
            "exchange",
            "name",
            "status",
            "currency",
            "list_date",
            "delist_date",
            "board",
        )
        assert written["trading_calendar"][:5] == (
            "exchange",
            "date",
            "is_open",
            "prev_trade_date",
            "next_trade_date",
        )
        assert report.landed == {"instrument": 1, "trading_calendar": 1}
        assert report.as_dict()["window"] == ["2026-09-21", "2026-09-25"]

    def test_a_row_the_contract_refuses_is_reported_not_landed(self):
        """A leg whose shape drifted is visible in the report, not half-landed."""
        good = self._instrument()
        hollow = {"symbol": "000001.SZ", "exchange": "SZSE"}  # no name/status/...

        report = self._refresh(instruments=[good, hollow], calendar=[])

        assert report.landed["instrument"] == 1
        assert report.rejected["instrument"] == ("row 1: name: Field required",)

    def test_a_row_carrying_a_field_the_contract_has_not_is_refused(self):
        """``extra='forbid'`` is the drift detector; a source column is not a field."""
        drift = {
            "symbol": "600519.SH",
            "exchange": "SSE",
            "name": "贵州茅台",
            "status": "active",
            "currency": "CNY",
            "list_date": None,
            "delist_date": None,
            "board": None,
            "board2": "main",
        }

        report = self._refresh(instruments=[drift])

        assert report.landed["instrument"] == 0
        assert "board2" in report.rejected["instrument"][0]

    def test_two_rows_on_one_key_are_refused_instead_of_the_last_one_winning(self):
        """A key the leg hands over twice is reported, and none of it is landed.

        The upsert rewrites a key it receives twice inside one statement, and
        ``DwdWriter.write`` reports ``len(frame)`` - so before this guard a
        second page naming a symbol the first had already named would land
        whichever row came last while the run claimed both rows. C42 measured
        the live face of it: nine catalog pages, 65,895 contract rows, zero
        collisions (``docs/evidence/C42/duplicate-symbol-scan.txt``), so this
        is a guard for an invariant nothing was holding rather than a repair
        of data lost today.
        """
        frames: dict[str, pd.DataFrame] = {}

        def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
            # the invariant the old code could not promise: one key, one row.
            assert len(frame) == frame[list(key)].drop_duplicates().shape[0]
            frames[domain] = frame
            return len(frame)

        stock = self._instrument(symbol="000001.SZ", name="平安银行")
        index = self._instrument(
            symbol="000001.SZ", name="平安银行指数", delist_date=date(2020, 1, 1)
        )

        report = self._refresh(
            instruments=[stock, index, self._instrument()], calendar=[], land=land
        )

        assert frames["instrument"]["symbol"].tolist() == ["600519.SH"]
        assert report.landed == {"instrument": 1, "trading_calendar": 0}
        (refusal,) = report.rejected["instrument"]
        assert "key symbol=000001.SZ" in refusal
        assert "published it on 2 rows" in refusal
        assert "disagrees on name, delist_date" in refusal

    def test_a_calendar_leg_that_publishes_one_day_twice_is_refused_too(self):
        """Identical duplicates are still a shape drift: nothing is guessed either way."""
        frames: dict[str, pd.DataFrame] = {}

        def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
            assert len(frame) == frame[list(key)].drop_duplicates().shape[0]
            frames[domain] = frame
            return len(frame)

        day = date(2026, 9, 25)
        report = self._refresh(
            instruments=[],
            calendar=[self._calendar(day), self._calendar(day)],
            land=land,
        )

        assert "trading_calendar" not in frames
        assert report.landed == {"instrument": 0, "trading_calendar": 0}
        (refusal,) = report.rejected["trading_calendar"]
        assert "key exchange=CN-SSE, date=2026-09-25" in refusal
        assert "with identical values" in refusal

    def test_an_empty_leg_lands_nothing(self):
        calls: list[str] = []

        def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
            calls.append(domain)
            return len(frame)

        report = self._refresh(land=land)

        assert report.landed == {"instrument": 0, "trading_calendar": 0}
        assert report.rejected == {"instrument": (), "trading_calendar": ()}
        assert calls == []

    def test_the_default_writer_upserts_each_backbone_table(self, monkeypatch):
        seen: list[tuple[str, tuple[str, ...]]] = []

        class _StubDwdWriter:
            def __init__(self, engine: object) -> None:
                assert engine is not None

            def write(self, frame, *, table, key):
                seen.append((table, key))
                return len(frame)

        monkeypatch.setattr("opendata.pipeline.dwd_merge.DwdWriter", _StubDwdWriter)

        report = refresh_metadata_backbone(
            object(),
            source="ths",
            fetch_instruments=lambda: [self._instrument()],
            fetch_calendar=lambda: [self._calendar(date(2026, 9, 25))],
            window=self.WINDOW,
        )

        assert seen == [
            ("dwd_instrument", ("symbol",)),
            ("dwd_trading_calendar", ("exchange", "date")),
        ]
        assert report.landed == {"instrument": 1, "trading_calendar": 1}

    def test_a_dry_run_still_needs_somewhere_to_put_the_rows(self):
        """``engine=None`` is only honest when a writer was handed in."""
        with pytest.raises(ValueError, match="needs an engine without a land override"):
            refresh_metadata_backbone(
                None,
                source="ths",
                fetch_instruments=lambda: [self._instrument()],
                fetch_calendar=lambda: [],
                window=self.WINDOW,
            )

    def test_a_row_that_is_neither_a_model_nor_a_mapping_is_refused(self):
        """The leg answering garbage is a shape drift, not a crash."""
        report = self._refresh(instruments=["600519.SH"])

        assert report.landed["instrument"] == 0
        assert report.rejected["instrument"][0].startswith("row 0:")

    def test_what_the_backbone_lands_is_what_its_consumers_read(self):
        """Producer and consumer close the loop through the two dwd tables.

        ``warehouse_calendar`` and ``landed_instruments`` are the two reads a
        batch runs on; both are pointed at the rows this step zero writes, on
        a warehouse that is only a stand-in for the tables' shape.
        """
        from opendata.pipeline.jobs import landed_instruments
        from opendata.pipeline.trading_calendar import TIER_WAREHOUSE, warehouse_calendar

        frames: dict[str, pd.DataFrame] = {}

        def capture(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
            frames[domain] = frame
            return len(frame)

        self._refresh(
            instruments=[self._instrument(), self._instrument(symbol="000001.SZ", exchange="SZSE")],
            calendar=[
                self._calendar(date(2026, 9, 24)),
                self._calendar(date(2026, 9, 25)),
                self._calendar(date(2026, 9, 26), open_day=False, prev=date(2026, 9, 25)),
            ],
            land=capture,
        )
        engine = _warehouse_with(frames)

        catalog = landed_instruments(engine)
        calendar = warehouse_calendar(engine)

        assert [row.symbol for row in catalog] == ["000001.SZ", "600519.SH"]
        assert calendar.tier == TIER_WAREHOUSE
        assert calendar.expected_data_date(date(2026, 9, 26)) == date(2026, 9, 25)
        engine.dispose()  # the stand-in warehouse is this test's own resource


def test_ods_rows_are_normalized_through_the_mapping():
    """The dwd reader sees contract-shaped frames, not raw ods columns."""
    raw_rows = [
        {
            "日期": date(2024, 1, 2),
            "股票代码": "600519.SH",
            "开盘": 1.0,
            "最高": 1.0,
            "最低": 1.0,
            "收盘": 1.0,
            "成交量": 100.0,
            "成交额": 100.0,
        }
    ]

    frame = normalize_ods_rows(raw_rows, domain="stock_daily", source="akshare")

    assert frame.iloc[0]["symbol"] == "600519"
    assert frame.iloc[0]["volume"] == 10_000.0  # 手 -> 股


def test_normalize_ods_rows_keeps_unknown_source_columns_out():
    """Extra ods columns never leak into the contract view."""
    raw_rows = [
        {
            "日期": date(2024, 1, 2),
            "股票代码": "600519",
            "开盘": 1.0,
            "最高": 1.0,
            "最低": 1.0,
            "收盘": 1.0,
            "成交量": 100.0,
            "成交额": 100.0,
            "涨跌幅": 1.5,
        }
    ]

    frame = normalize_ods_rows(raw_rows, domain="stock_daily", source="akshare")

    assert "涨跌幅" not in frame.columns


class TestOdsReadersUseTheSourceDateColumn:
    """The ods tables keep SOURCE column names, so the window filter must
    use the source's mapped date column (the akshare tables use 日期) -
    the contract field name only happens to match for the ths tables."""

    def _mapping(self):
        from opendata.data.mapping import DomainMapping, FieldMapping

        return DomainMapping(
            domain="stock_daily",
            key=("symbol", "trade_date"),
            fields={
                "symbol": FieldMapping("股票代码", normalize="plain"),
                "trade_date": FieldMapping("日期"),
                "close": FieldMapping("收盘"),
            },
        )

    def test_raw_reader_filters_on_the_mapped_column(self, monkeypatch):
        from opendata.pipeline import templates

        captured: dict[str, object] = {}

        def fake_load(engine, table, domain, start, end, keys, *, time_column=None):
            captured["time_column"] = time_column
            captured["table"] = table
            return []

        monkeypatch.setattr(templates, "_load_ods_rows", fake_load)
        monkeypatch.setattr(
            templates, "require_domain_mapping", lambda source, domain: self._mapping()
        )
        monkeypatch.setattr(
            "opendata.data.domains.ods_table", lambda domain, source: f"ods_{domain}_{source}"
        )

        reader = templates.ods_raw_reader(object(), "stock_daily", "akshare")
        reader(templates.Window(start=date(2026, 1, 5), end=date(2026, 1, 9)))

        assert captured["time_column"] == "日期"
        assert captured["table"] == "ods_stock_daily_akshare"

    def test_raw_reader_returns_source_columns_not_contract_ones(self, monkeypatch):
        from opendata.pipeline import templates

        monkeypatch.setattr(
            templates,
            "_load_ods_rows",
            lambda *a, **k: [{"股票代码": "000001", "日期": date(2026, 1, 5), "收盘": 11.14}],
        )
        monkeypatch.setattr(
            templates, "require_domain_mapping", lambda source, domain: self._mapping()
        )
        monkeypatch.setattr(
            "opendata.data.domains.ods_table", lambda domain, source: f"ods_{domain}_{source}"
        )

        frame = templates.ods_raw_reader(object(), "stock_daily", "akshare")(
            templates.Window(start=date(2026, 1, 5), end=date(2026, 1, 5))
        )

        # the cross-check owns normalization; handing it contract columns
        # would make it normalize twice and fail closed
        assert list(frame.columns) == ["股票代码", "日期", "收盘"]


class _Result:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows

    def one(self) -> tuple:
        assert len(self._rows) == 1, f"expected a single aggregate row, saw {self._rows}"
        return self._rows[0]


class _Connection:
    def __init__(self, engine: "_RecordingWarehouse") -> None:
        self._engine = engine

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: object, params: dict | None = None) -> _Result:
        sql = str(statement)
        self._engine.calls.append((sql, dict(params or {})))
        return _Result(self._engine.columns, self._engine.rows)


class _RecordingWarehouse:
    """Answers one ods read and records the statement that asked it."""

    def __init__(self, *, columns: list[str], rows: list[tuple]) -> None:
        self.columns = columns
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def connect(self) -> _Connection:
        return _Connection(self)

    @property
    def sql(self) -> str:
        assert len(self.calls) == 1, f"expected one read, saw {len(self.calls)}"
        return self.calls[0][0]

    @property
    def params(self) -> dict:
        assert len(self.calls) == 1, f"expected one read, saw {len(self.calls)}"
        return self.calls[0][1]


_AK_COLUMNS = ["股票代码", "日期", "收盘"]


class TestLoadOdsRows:
    """The one statement every ods reader is built on."""

    def test_a_window_read_binds_both_bounds_on_the_mapped_column(self):
        engine = _RecordingWarehouse(columns=_AK_COLUMNS, rows=[])

        rows = templates._load_ods_rows(
            engine,
            "ods_stock_daily_akshare",
            "stock_daily",
            date(2026, 1, 5),
            date(2026, 1, 9),
            set(),
            time_column="日期",
        )

        assert engine.sql == (
            "SELECT * FROM `ods_stock_daily_akshare` WHERE `日期` >= :start AND `日期` <= :end"
        )
        assert engine.params == {"start": date(2026, 1, 5), "end": date(2026, 1, 9)}
        assert rows == []

    def test_the_contract_field_name_is_the_fallback_time_column(self):
        """Only sources that name their date column like the contract do are
        correct through this path - which is why the readers override it."""
        engine = _RecordingWarehouse(columns=["symbol", "trade_date", "close"], rows=[])

        templates._load_ods_rows(
            engine, "ods_stock_daily_ths", "stock_daily", date(2026, 1, 5), date(2026, 1, 5), set()
        )

        assert "`trade_date` >= :start" in engine.sql
        assert "日期" not in engine.sql

    def test_affected_keys_replace_the_window_with_one_predicate_per_key(self):
        engine = _RecordingWarehouse(columns=_AK_COLUMNS, rows=[])

        templates._load_ods_rows(
            engine,
            "ods_stock_daily_akshare",
            "stock_daily",
            date(2026, 1, 5),
            date(2026, 1, 9),
            {("000001", date(2026, 1, 6)), ("600519", date(2026, 1, 7))},
            time_column="日期",
        )

        assert ">= :start" not in engine.sql  # the key set is the filter now
        assert engine.sql == (
            "SELECT * FROM `ods_stock_daily_akshare` WHERE ("
            "(`symbol` = :symbol_0 AND `日期` = :day_0) OR "
            "(`symbol` = :symbol_1 AND `日期` = :day_1))"
        )
        assert engine.params == {
            "start": date(2026, 1, 5),
            "end": date(2026, 1, 9),
            "symbol_0": "000001",
            "day_0": date(2026, 1, 6),
            "symbol_1": "600519",
            "day_1": date(2026, 1, 7),
        }

    def test_the_key_symbol_column_follows_the_source(self):
        engine = _RecordingWarehouse(columns=_AK_COLUMNS, rows=[])

        templates._load_ods_rows(
            engine,
            "ods_stock_daily_akshare",
            "stock_daily",
            date(2026, 1, 5),
            date(2026, 1, 5),
            {("600519", date(2026, 1, 5))},
            time_column="日期",
            symbol_column="股票代码",
        )

        assert "(`股票代码` = :symbol_0 AND `日期` = :day_0)" in engine.sql

    def test_rows_are_rebuilt_as_dicts_of_the_returned_columns(self):
        engine = _RecordingWarehouse(
            columns=_AK_COLUMNS,
            rows=[("600519", date(2026, 1, 5), 1.0), ("000001", date(2026, 1, 6), 9.5)],
        )

        rows = templates._load_ods_rows(
            engine,
            "ods_stock_daily_akshare",
            "stock_daily",
            date(2026, 1, 5),
            date(2026, 1, 6),
            set(),
            time_column="日期",
        )

        assert rows == [
            {"股票代码": "600519", "日期": date(2026, 1, 5), "收盘": 1.0},
            {"股票代码": "000001", "日期": date(2026, 1, 6), "收盘": 9.5},
        ]


def _ods_row(symbol: str, day: date) -> dict:
    """One complete akshare ods row (the mapping fails closed on gaps)."""
    return {
        "股票代码": symbol,
        "日期": day,
        "开盘": 1.0,
        "最高": 2.0,
        "最低": 0.5,
        "收盘": 1.5,
        "成交量": 100.0,
        "成交额": 150.0,
    }


class TestFrameReaderWindow:
    """The merge reader: a window read, widened so no affected key is dropped."""

    def _read(self, monkeypatch, *, rows, keys, start, end):
        captured: dict[str, object] = {}

        def fake_load(engine, table, domain, lower, upper, loaded_keys, **kwargs):
            captured["lower"] = lower
            captured["upper"] = upper
            captured["loaded_keys"] = loaded_keys
            captured["time_column"] = kwargs.get("time_column")
            return rows

        monkeypatch.setattr(templates, "_load_ods_rows", fake_load)
        reader = templates.ods_frame_reader(object(), "stock_daily", "akshare")  # ty: ignore[invalid-argument-type]
        return reader(start, end, keys), captured

    @staticmethod
    def _days(frame) -> list[str]:
        return sorted(str(value)[:10] for value in frame["trade_date"])

    def test_the_read_is_widened_to_cover_the_affected_dates(self, monkeypatch):
        rows = [_ods_row("600519", date(2025, 12, 20)), _ods_row("000001", date(2026, 2, 10))]

        _frame, captured = self._read(
            monkeypatch,
            rows=rows,
            keys={("600519.SH", date(2025, 12, 20)), ("000001.SZ", date(2026, 2, 10))},
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
        )

        assert captured["lower"] == date(2025, 12, 20)
        assert captured["upper"] == date(2026, 2, 10)
        # the keys are matched after normalization, so the SQL stays a window read
        assert captured["loaded_keys"] == set()
        assert captured["time_column"] == "日期"

    def test_affected_keys_outside_the_window_still_come_back(self, monkeypatch):
        """The merge must see the batch it just wrote even when the scheduler
        asked for a narrower window - dropping those rows is how a dwd layer
        silently stops converging."""
        rows = [
            _ods_row("600519", date(2025, 12, 20)),
            _ods_row("000001", date(2026, 1, 6)),
            _ods_row("300750", date(2026, 1, 7)),
        ]

        frame, _captured = self._read(
            monkeypatch,
            rows=rows,
            keys={("600519.SH", date(2025, 12, 20))},
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
        )

        assert self._days(frame) == ["2025-12-20", "2026-01-06", "2026-01-07"]

    def test_an_unaffected_row_inside_the_window_is_kept(self, monkeypatch):
        """The window is not turned into a key filter: the merge needs the
        whole range to cumulate factors over."""
        rows = [_ods_row("600100", date(2026, 1, 6))]

        frame, _captured = self._read(
            monkeypatch,
            rows=rows,
            keys={("600519.SH", date(2025, 12, 20))},
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
        )

        assert self._days(frame) == ["2026-01-06"]
        assert frame.iloc[0]["symbol"] == "600100"

    def test_no_affected_keys_leave_the_window_alone(self, monkeypatch):
        rows = [_ods_row("600519", date(2026, 1, 6))]

        frame, captured = self._read(
            monkeypatch,
            rows=rows,
            keys=set(),
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
        )

        assert (captured["lower"], captured["upper"]) == (date(2026, 1, 5), date(2026, 1, 9))
        assert len(frame) == 1

    def test_an_empty_ods_read_is_an_empty_frame_not_a_crash(self, monkeypatch):
        frame, _captured = self._read(
            monkeypatch,
            rows=[],
            keys={("600519.SH", date(2026, 1, 6))},
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
        )

        assert frame.empty


class TestScheduleTemplateFailClosed:
    """A deployment without its schedule definitions must not start empty."""

    def _load(self, text: str, tmp_path):
        path = tmp_path / "schedules.yaml"
        path.write_text(text, encoding="utf-8")
        return templates.load_schedule_templates(path)

    def test_a_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="unreadable"):
            templates.load_schedule_templates(tmp_path / "absent.yaml")

    def test_malformed_yaml_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="unreadable"):
            self._load("templates: [\n  name: broken\n", tmp_path)

    def test_a_document_without_a_templates_list_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="must declare a templates list"):
            self._load("jobs:\n  - name: x\n", tmp_path)

    def test_a_scalar_entry_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="is not a mapping"):
            self._load("templates:\n  - just-a-string\n", tmp_path)

    def test_a_missing_required_field_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="is malformed"):
            self._load("templates:\n  - name: only-a-name\n", tmp_path)

    def test_an_unknown_kind_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="is malformed"):
            self._load(
                "templates:\n  - name: n\n    cron: '0 1 * * *'\n    kind: time_travel\n", tmp_path
            )

    def test_a_well_formed_file_loads_with_its_defaults(self, tmp_path):
        entries = self._load(
            "templates:\n"
            "  - name: nightly\n"
            "    cron: '30 20 * * *'\n"
            "    kind: incremental\n"
            "    payload:\n"
            "      domain: stock_daily\n"
            "  - name: weekly-check\n"
            "    cron: '0 3 * * 1'\n"
            "    kind: full_check\n",
            tmp_path,
        )

        assert [entry.name for entry in entries] == ["nightly", "weekly-check"]
        assert entries[0].payload == {"domain": "stock_daily"}
        assert entries[0].note == ""
        assert entries[1].payload == {}
        assert entries[1].kind is TemplateKind.FULL_CHECK


def test_a_negative_lookback_is_refused_not_silently_ignored():
    with pytest.raises(ValueError, match="lookback_days must be >= 0"):
        templates.incremental_window(date(2026, 1, 8), lookback_days=-1)


class TestPipelineWriterWiring:
    """What the factory hands the ods writer and the step-5 notifier."""

    def _spy_writer(self, monkeypatch):
        seen: dict[str, object] = {}

        class _StubWriter:
            def __init__(self, engine: object) -> None:
                seen["engine"] = engine

            def write(self, frame, **kwargs):
                seen["columns"] = list(frame.columns)
                seen.update(kwargs)
                return _Written(rows=len(frame))

        monkeypatch.setattr("opendata.pipeline.ods_writer.OdsWriter", _StubWriter)
        return seen

    def test_the_writer_gets_the_source_spelling_of_the_key(self, monkeypatch):
        seen = self._spy_writer(monkeypatch)
        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
            batch_id="a" * 36,
        )

        assert pipeline.write_ods(pd.DataFrame({"x": [1, 2, 3]})) == 3
        assert seen["table"] == "ods_stock_daily_akshare"
        assert seen["key"] == ("股票代码", "日期")  # ods keeps the source columns
        assert seen["source"] == "akshare"
        assert seen["batch_id"] == "a" * 36

    def test_the_notifier_shares_the_batch_id_the_writer_stamped(self, monkeypatch):
        """A ``full`` payload is read back by ``_batch_id``: a notifier built
        on another id would announce the batch and return no rows."""
        seen = self._spy_writer(monkeypatch)
        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
        )

        assert pipeline.notify is not None
        generated = pipeline.notify.batch_id
        assert generated  # one id generated by the factory, not None
        pipeline.write_ods(pd.DataFrame({"x": [1, 2]}))

        assert seen["batch_id"] == generated
        assert pipeline.notify.layer == "ods"
        assert pipeline.notify.table == "ods_stock_daily_akshare"

    def test_a_supplied_notify_hook_wins(self, monkeypatch):
        self._spy_writer(monkeypatch)

        async def my_hook(context):
            return None

        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
            notify=my_hook,
        )

        assert pipeline.notify is my_hook


def test_write_dwd_targets_the_stock_daily_dwd_table(monkeypatch):
    seen: dict[str, object] = {}

    class _StubDwdWriter:
        def __init__(self, engine: object) -> None:
            seen["engine"] = engine

        def write(self, frame, *, table, key):
            seen.update({"rows": len(frame), "table": table, "key": key})
            return len(frame)

    monkeypatch.setattr("opendata.pipeline.dwd_merge.DwdWriter", _StubDwdWriter)

    rows = templates._write_dwd(object(), pd.DataFrame({"symbol": ["600519"]}))  # ty: ignore[invalid-argument-type]

    assert rows == 1
    assert seen["table"] == "dwd_stock_daily"
    assert seen["key"] == ("symbol", "trade_date")


class _Written:
    """The rows/batches answer the writer closure reads."""

    def __init__(self, *, rows: int) -> None:
        self.rows = rows
        self.batches = 1


def _akshare_style_mapping():
    """A mapping whose ods columns are the source's own (日期, not trade_date)."""
    from opendata.data.mapping import DomainMapping, FieldMapping

    return DomainMapping(
        domain="stock_daily",
        key=("symbol", "trade_date"),
        fields={
            "symbol": FieldMapping("股票代码", normalize="plain"),
            "trade_date": FieldMapping("日期"),
            "close": FieldMapping("收盘"),
        },
    )


class TestOdsLegSpan:
    """The probe that bounds a scheduled full check.

    A ten-year partitioned leg is not scanned for its minimum: the read is a
    bounded ``MIN``/``MAX``, and a leg with nothing inside that bound is an
    error rather than an empty half of a comparison.
    """

    def _engine(self, rows):
        return _RecordingWarehouse(columns=["oldest", "newest"], rows=rows)

    def _patch(self, monkeypatch):
        monkeypatch.setattr(
            templates, "require_domain_mapping", lambda source, domain: _akshare_style_mapping()
        )
        monkeypatch.setattr(
            "opendata.data.domains.ods_table", lambda domain, source: f"ods_{domain}_{source}"
        )

    def test_the_probe_asks_the_source_date_column_for_min_and_max(self, monkeypatch):
        self._patch(monkeypatch)
        engine = self._engine([(date(2026, 5, 6), "2026-09-24")])

        span = templates.ods_leg_span(engine, "stock_daily", "akshare", probe_days=400)

        assert engine.sql == (
            "SELECT MIN(`日期`), MAX(`日期`) FROM `ods_stock_daily_akshare` WHERE `日期` >= :floor"
        )
        assert engine.params == {"floor": date.today() - timedelta(days=400)}
        # A driver that answers with text is read back as dates.
        assert span == (date(2026, 5, 6), date(2026, 9, 24))

    def test_an_empty_leg_is_refused_rather_than_compared_with_nothing(self, monkeypatch):
        self._patch(monkeypatch)
        engine = self._engine([(None, None)])

        with pytest.raises(ValueError, match="has no rows since"):
            templates.ods_leg_span(engine, "stock_daily", "akshare")


class TestCrossCheckWindow:
    """The weekly check's window is what both legs hold, not the calendar."""

    def _patch(self, monkeypatch, spans: dict[str, tuple[date, date]]):
        asked: list[str] = []

        def fake_span(engine, domain, source, *, probe_days=templates.FULL_CHECK_PROBE_DAYS):
            asked.append(source)
            return spans[source]

        monkeypatch.setattr(templates, "ods_leg_span", fake_span)
        return asked

    def test_the_window_is_the_intersection_of_the_two_legs(self, monkeypatch):
        # ths runs years deep; the ported akshare leg starts 2026-08-20.
        asked = self._patch(
            monkeypatch,
            {
                "ths": (date(2019, 1, 2), date(2026, 9, 24)),
                "akshare": (date(2026, 8, 20), date(2026, 9, 10)),
            },
        )

        window = templates.cross_check_window(object(), "stock_daily", ["ths", "akshare"])

        assert asked == ["ths", "akshare"]
        assert (window.start, window.end) == (date(2026, 8, 20), date(2026, 9, 10))

    def test_a_shared_span_wider_than_the_cap_is_walked_back_from_the_newest_day(self, monkeypatch):
        self._patch(
            monkeypatch,
            {
                "ths": (date(2019, 1, 2), date(2026, 9, 24)),
                "akshare": (date(2020, 3, 5), date(2026, 9, 24)),
            },
        )

        window = templates.cross_check_window(
            object(), "stock_daily", ["ths", "akshare"], max_days=31
        )

        assert window.end == date(2026, 9, 24)
        assert (window.end - window.start).days + 1 == 31

    def test_legs_that_do_not_overlap_are_refused(self, monkeypatch):
        self._patch(
            monkeypatch,
            {
                "ths": (date(2026, 9, 1), date(2026, 9, 24)),
                "akshare": (date(2026, 5, 1), date(2026, 5, 31)),
            },
        )

        with pytest.raises(ValueError, match="do not overlap"):
            templates.cross_check_window(object(), "stock_daily", ["ths", "akshare"])

    def test_an_empty_leg_stops_the_window_from_being_named(self, monkeypatch):
        def empty(engine, domain, source, **kwargs):
            raise ValueError(
                f"ods leg ods_stock_daily_{source} has no rows since 2025; nothing to compare"
            )

        monkeypatch.setattr(templates, "ods_leg_span", empty)

        with pytest.raises(ValueError, match="nothing to compare"):
            templates.cross_check_window(object(), "stock_daily", ["ths", "akshare"])


class TestBuildCrossCheck:
    """The factory that replaced "compute the decision and drop it"."""

    def test_the_pair_comes_from_the_authority_baseline(self) -> None:
        service = templates.build_cross_check(object())  # ty: ignore[invalid-argument-type]

        assert service.domain == "stock_daily"
        assert service.sources == ("ths", "akshare")
        assert set(service.mappings) == {"ths", "akshare"}
        assert set(service.readers) == {"ths", "akshare"}

    def test_a_domain_without_a_second_feed_is_refused(self) -> None:
        with pytest.raises(LookupError, match="has no second feed"):
            templates.build_cross_check(object(), domain="trading_calendar")  # ty: ignore[invalid-argument-type]

    def test_the_production_defaults_reach_the_channels(self) -> None:
        from opendata.pipeline.diff_alerts import shared_policy

        service = templates.build_cross_check(object(), sources=("ths", "akshare"))  # ty: ignore[invalid-argument-type]

        assert service.notifier is not None
        # The dedupe set and the rate baseline only mean something if they
        # are the process's, not this comparison's.
        assert service.policy is shared_policy()

    def test_an_injected_notifier_and_policy_win(self) -> None:
        from opendata.pipeline.alerts import AlertPolicy

        async def notify(summary, decision):
            return None

        policy = AlertPolicy()
        service = templates.build_cross_check(
            object(),  # ty: ignore[invalid-argument-type]
            sources=("ths", "akshare"),
            notifier=notify,
            policy=policy,
        )

        assert service.notifier is notify
        assert service.policy is policy

    def test_the_pipeline_hook_shares_that_wiring(self, monkeypatch) -> None:
        """Step 3 of the P0 template is the same factory, not a second build."""
        self._spy_writer(monkeypatch)
        pipeline = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
            source="ths",
            second_source="akshare",
        )

        assert pipeline.cross_check is not None
        service = pipeline.cross_check.__self__
        assert service.sources == ("ths", "akshare")
        assert service.notifier is not None
        # A single-source build keeps the step off rather than comparing
        # a source with itself.
        solo = build_stock_daily_pipeline(
            engine=object(),  # ty: ignore[invalid-argument-type]
            session_maker=object(),  # ty: ignore[invalid-argument-type]
            fetch_symbol=lambda symbol, window: None,
            symbols=("600519",),
            source="ths",
        )
        assert solo.cross_check is None

    def _spy_writer(self, monkeypatch):
        class _StubWriter:
            def __init__(self, engine: object) -> None:
                pass

            def write(self, frame, **kwargs):
                return _Written(rows=len(frame))

        monkeypatch.setattr("opendata.pipeline.ods_writer.OdsWriter", _StubWriter)
