"""Unit tests for the pipeline job trigger (AC-13, milestone B4).

The warehouse is off-limits here: these tests assert the *orchestration*
(universe, run order, which run carries the cross-check pair, scheduler
registration) with the pipeline builder and the DB readers stubbed. The
real end-to-end run of this module is the acceptance evidence in
``docs/evidence/B4/``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, insert

from opendata.data.models import Instrument
from opendata.pipeline import jobs
from opendata.pipeline.dump_import import shanghai_dates
from opendata.pipeline.runner import PipelineOutcome, Window
from opendata.pipeline.templates import ScheduleTemplate, TemplateKind
from opendata.pipeline.trading_calendar import (
    TIER_WAREHOUSE,
    TIER_WEEKDAY,
    calendar_from_days,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from sqlalchemy import Engine

WINDOW = Window(start=date(2026, 9, 24), end=date(2026, 9, 24))


class FakePipeline:
    """Stand-in for ``DataPipeline`` that records the window it ran."""

    def __init__(self, source: str, rows: int = 3) -> None:
        self.source = source
        self.rows = rows
        self.ran: list[tuple[Window, bool]] = []

    async def run(
        self,
        window: Window,
        *,
        resume: bool = True,
        source_windows: Mapping[str, Mapping[str, Window | None]] | None = None,
    ) -> PipelineOutcome:
        self.ran.append((window, resume))
        return PipelineOutcome(
            pipeline_id=f"stock_daily:{self.source}:{window.label()}",
            shards_total=1,
            shards_done=1,
            rows_written=self.rows,
            source_windows={
                source: dict(windows) for source, windows in (source_windows or {}).items()
            },
        )


class FakeScheduler:
    """Stand-in for ``SchedulerService`` capturing ``add_job`` calls."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    async def add_job(self, **kwargs: Any) -> dict[str, Any]:
        self.jobs[str(kwargs["job_id"])] = kwargs
        return {"job_id": kwargs["job_id"]}


@pytest.fixture
def stubbed_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route ``run_incremental_job`` through fake pipelines and no DB reads."""
    built: list[dict[str, Any]] = []

    def fake_build(**kwargs: Any) -> FakePipeline:
        pipeline = FakePipeline(str(kwargs["source"]))
        built.append({**kwargs, "pipeline": pipeline})
        return pipeline

    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", fake_build)
    monkeypatch.setattr(
        jobs, "make_fetch_symbol_async", lambda domain, source: lambda s, w: pd.DataFrame()
    )
    monkeypatch.setattr(jobs, "read_ods_watermarks", lambda *args, **kwargs: {})
    monkeypatch.setattr(jobs, "_count_in_window", lambda engine, domain, window: 7)
    monkeypatch.setattr(
        jobs, "_freshness", lambda engine, domain, sources, *, expected: {"dwd": None}
    )
    return built


@pytest.fixture
def logged() -> Iterator[list[tuple[str, str]]]:
    """Capture loguru records (level, message) for the duration of a test.

    The batch's provenance is half return value and half log line: what it
    *says* about a code it left alone is part of the answer, so a test that
    claims a run reports something has to read the line it printed.
    """
    from loguru import logger

    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield records
    finally:
        logger.remove(handler_id)


class TestRunIncrementalJob:
    """The batch orchestration: universe, run order, cross-check pairing."""

    async def test_ods_watermark_controls_reruns_after_raw_cache_invalidation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        from sqlalchemy import UniqueConstraint, select
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from opendata.core.database import Base
        from opendata.data.mapping import require_domain_mapping
        from opendata.data.raw_response_cache import RawResponseCache
        from opendata.models.pipeline_step import PipelineStepCheckpoint, PipelineSymbolWindow
        from opendata.pipeline.runner import DataPipeline, PipelineSpec

        first_day = date(2026, 9, 23)
        next_day = date(2026, 9, 24)
        symbols = ["000001", "600519"]
        warehouse = create_engine("sqlite://")
        metadata = MetaData()
        ods = Table(
            "ods_stock_daily_akshare",
            metadata,
            Column("股票代码", String, nullable=False),
            Column("日期", Date, nullable=False),
            Column("收盘", String, nullable=False),
            UniqueConstraint("股票代码", "日期"),
        )
        metadata.create_all(warehouse)
        assert (
            Base.metadata.tables[PipelineStepCheckpoint.__tablename__]
            is PipelineStepCheckpoint.__table__
        )
        assert (
            Base.metadata.tables[PipelineSymbolWindow.__tablename__]
            is PipelineSymbolWindow.__table__
        )
        control = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with control.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(control, expire_on_commit=False)
        calls: list[tuple[str, Window]] = []

        def fetch(symbol: str, window: Window) -> pd.DataFrame:
            calls.append((symbol, window))
            exchange = "SZ" if symbol.startswith("0") else "SH"
            return pd.DataFrame(
                {
                    "股票代码": [f"{symbol}.{exchange}"],
                    "日期": [window.end],
                    "收盘": ["10.0"],
                }
            )

        def write_ods(frame: pd.DataFrame) -> int:
            records = frame.to_dict("records")
            statement = sqlite_insert(ods).values(records)
            statement = statement.on_conflict_do_update(
                index_elements=["股票代码", "日期"],
                set_={"收盘": statement.excluded["收盘"]},
            )
            with warehouse.begin() as connection:
                connection.execute(statement)
            return len(frame)

        def build_pipeline(**kwargs: Any) -> DataPipeline:
            mapping = require_domain_mapping(str(kwargs["source"]), "stock_daily")
            spec = PipelineSpec(
                domain="stock_daily",
                source=str(kwargs["source"]),
                key=mapping.source_key,
                symbols=kwargs["symbols"],
                shard_size=int(kwargs["shard_size"]),
                variant=str(kwargs["run_variant"]),
            )
            return DataPipeline(
                spec,
                session_maker=maker,
                write_ods=write_ods,
                fetch_symbol=kwargs["fetch_symbol"],
            )

        monkeypatch.setattr(jobs, "build_stock_daily_pipeline", build_pipeline)
        monkeypatch.setattr(jobs, "make_fetch_symbol_async", lambda domain, source: fetch)
        monkeypatch.setattr(jobs, "_count_in_window", lambda engine, domain, window: 0)
        monkeypatch.setattr(
            jobs, "_freshness", lambda engine, domain, sources, *, expected: {"dwd": None}
        )
        cache = RawResponseCache(tmp_path, ttl_seconds=60)
        cache_identity = {
            "source": "akshare",
            "endpoint": "https://cache.example/stock-daily",
            "params": {"symbol": "600519", "as_of": first_day.isoformat()},
            "context": {"adjust_basis": "none"},
        }
        assert cache.put(
            method="GET",
            **cache_identity,
            status_code=200,
            content=b"cached fixture",
        )
        assert cache.get(**cache_identity) is not None

        calendar = calendar_from_days([first_day, next_day])
        first = await jobs.run_incremental_job(
            source="akshare",
            symbols=symbols,
            as_of=first_day,
            calendar=calendar,
            engine=warehouse,
            session_maker=maker,
            resume=False,
        )
        assert calls == [(symbol, Window(first_day, first_day)) for symbol in symbols]
        assert first.outcomes["akshare"].source_windows["akshare"] == {
            symbol: Window(first_day, first_day) for symbol in symbols
        }

        cache.invalidate(**cache_identity)
        assert cache.get(**cache_identity) is None
        calls.clear()
        repeated = await jobs.run_incremental_job(
            source="akshare",
            symbols=symbols,
            as_of=first_day,
            calendar=calendar,
            engine=warehouse,
            session_maker=maker,
            resume=False,
        )
        assert calls == []
        assert repeated.outcomes["akshare"].source_windows["akshare"] == dict.fromkeys(
            symbols, None
        )

        advanced = await jobs.run_incremental_job(
            source="akshare",
            symbols=symbols,
            as_of=next_day,
            calendar=calendar,
            engine=warehouse,
            session_maker=maker,
            resume=False,
        )
        assert calls == [(symbol, Window(next_day, next_day)) for symbol in symbols]
        assert advanced.outcomes["akshare"].source_windows["akshare"] == {
            symbol: Window(next_day, next_day) for symbol in symbols
        }
        with warehouse.connect() as connection:
            landed_days = (
                connection.execute(select(ods.c["日期"]).distinct().order_by(ods.c["日期"]))
                .scalars()
                .all()
            )
        assert landed_days == [first_day, next_day]

        warehouse.dispose()
        await control.dispose()

    async def test_rejects_a_domain_without_a_builder(self) -> None:
        with pytest.raises(ValueError, match="no pipeline builder"):
            await jobs.run_incremental_job(domain="index_daily", symbols=["600000"])

    async def test_rejects_an_empty_universe_instead_of_running_zero_symbols(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jobs, "symbol_universe", lambda engine, domain, *, limit=None: [])
        with pytest.raises(ValueError, match="empty symbol universe"):
            await jobs.run_incremental_job(source="akshare")

    async def test_dual_source_runs_the_secondary_feed_first(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        result = await jobs.run_incremental_job(
            source="ths", second_source="akshare", symbols=["000001", "600519"], engine=object()
        )

        assert [call["source"] for call in stubbed_run] == ["akshare", "ths"]
        assert [call["second_source"] for call in stubbed_run] == [None, "akshare"]
        assert list(result.outcomes) == ["akshare", "ths"]
        assert result.sources == ("akshare", "ths")

    async def test_single_source_run_carries_no_cross_check_pair(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        result = await jobs.run_incremental_job(
            source="akshare", symbols=["600519"], engine=object()
        )

        assert len(stubbed_run) == 1
        assert stubbed_run[0]["second_source"] is None
        assert result.sources == ("akshare",)

    async def test_result_counts_come_from_the_per_source_outcomes(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        """``as_of`` must be pinned: omitting it defaults to *today*, so the
        expected window below would only hold on 2026-09-24."""
        result = await jobs.run_incremental_job(
            source="ths",
            second_source="akshare",
            symbols=["600519"],
            as_of=WINDOW.start,
            engine=object(),
        )

        payload = result.as_dict()
        assert payload["symbols"] == 1
        assert payload["dwd_rows"] == 7
        assert payload["window"] == {"start": "2026-09-24", "end": "2026-09-24"}
        assert payload["per_source"]["ths"]["pipeline_id"] == f"stock_daily:ths:{WINDOW.label()}"
        assert result.failures == 0

    async def test_window_and_resume_flag_reach_every_pipeline(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        result = await jobs.run_incremental_job(
            source="ths",
            second_source="akshare",
            symbols=["600519"],
            as_of=date(2026, 9, 24),
            lookback_days=2,
            resume=False,
            engine=object(),
        )

        expected = Window(start=date(2026, 9, 22), end=date(2026, 9, 24))
        assert result.window == expected
        for call in stubbed_run:
            pipeline: FakePipeline = call["pipeline"]
            assert pipeline.ran == [(expected, False)]
        assert result.outcomes["ths"].shards_done == 1

    async def test_a_weekend_run_covers_friday_instead_of_an_empty_saturday(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        """A4.7: the window end is the calendar's expectation, not the clock."""
        result = await jobs.run_incremental_job(
            source="ths", symbols=["600519"], as_of=date(2026, 9, 26), engine=object()
        )

        expected = Window(start=date(2026, 9, 25), end=date(2026, 9, 25))
        assert result.window == expected
        assert stubbed_run[0]["pipeline"].ran == [(expected, True)]
        assert result.expectation == {
            "run_date": "2026-09-26",
            "expected_data_date": "2026-09-25",
            "calendar_tier": TIER_WEEKDAY,
            "decided_by": TIER_WEEKDAY,
        }

    async def test_a_holiday_calendar_pulls_the_window_back_to_the_last_open_day(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        closed_for_national_day = calendar_from_days([date(2025, 9, 30), date(2025, 10, 9)])

        result = await jobs.run_incremental_job(
            source="ths",
            symbols=["600519"],
            as_of=date(2025, 10, 5),
            engine=object(),
            calendar=closed_for_national_day,
        )

        assert result.window == Window(start=date(2025, 9, 30), end=date(2025, 9, 30))
        assert result.expectation["calendar_tier"] == TIER_WAREHOUSE
        assert result.expectation["decided_by"] == TIER_WAREHOUSE


class TestSymbolUniverse:
    """The universe reader is fail-soft: no table, no crash."""

    def test_missing_table_yields_an_empty_universe(self) -> None:
        class _Broken:
            def connect(self) -> Any:
                raise RuntimeError("no such table")

        assert jobs.symbol_universe(_Broken(), "stock_daily") == []

    def test_limit_is_bounded_to_a_positive_cap(self) -> None:
        captured: dict[str, Any] = {}

        class _Result:
            def all(self) -> list[tuple[str]]:
                return [("600519",)]

        class _Conn:
            def __enter__(self) -> _Conn:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def execute(self, statement: object, params: dict[str, int]) -> _Result:
                captured["cap"] = params["cap"]
                return _Result()

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        assert jobs.symbol_universe(_Engine(), "stock_daily", limit=0) == ["600519"]
        assert captured["cap"] == 1


class TestResolveFetcher:
    """The batch's routing is the registry's own answer, not a private table.

    ``resolve_fetcher`` is the only place ``jobs`` reaches the provider
    registry, and every other test in this file stubs it - so until C42 the
    function body had never run under the suite. These three readings are
    what the scheduled batch actually depends on: the pair it asks for
    answers, and a pair that does not exist fails closed.
    """

    def test_a_registered_pair_answers_with_its_fetcher(self) -> None:
        fetcher = jobs.resolve_fetcher("stock_daily", "ths")

        assert fetcher.capability.domain == "stock_daily"
        assert fetcher.capability.source == "ths"

    def test_an_unknown_source_fails_closed_rather_than_falling_back(self) -> None:
        """A batch must not end up on another feed under its own feet."""
        with pytest.raises(LookupError, match="no registered capability"):
            jobs.resolve_fetcher("stock_daily", "not_a_source")

    def test_an_unregistered_domain_is_a_lookup_error(self) -> None:
        with pytest.raises(LookupError, match="no capability registered for domain"):
            jobs.resolve_fetcher("not_a_domain", "ths")


def _daily_engine(days: Sequence[date]) -> Engine:
    """A landed ``dwd_stock_daily`` holding exactly the trade dates given."""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    daily = Table(
        jobs.dwd_table("stock_daily"),
        metadata,
        Column("symbol", String(32)),
        Column("trade_date", Date),
    )
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(daily), [{"symbol": "600519", "trade_date": day} for day in days])
    return engine


class TestWindowRowCount:
    """How many landed rows a window covers - and 0 is an answer, not a crash."""

    def test_only_the_rows_inside_the_window_are_counted(self) -> None:
        engine = _daily_engine(
            [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 24), date(2026, 9, 25)]
        )

        assert jobs._count_in_window(engine, "stock_daily", WINDOW) == 2

    def test_a_wider_window_counts_every_row(self) -> None:
        engine = _daily_engine(
            [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 24), date(2026, 9, 25)]
        )
        month = Window(start=date(2026, 9, 1), end=date(2026, 9, 30))

        assert jobs._count_in_window(engine, "stock_daily", month) == 4

    def test_an_unreadable_table_counts_zero_and_names_itself(
        self, logged: Sequence[tuple[str, str]]
    ) -> None:
        """No table is the normal state before the first backfill: report, never raise."""
        count = jobs._count_in_window(create_engine("sqlite://"), "stock_daily", WINDOW)

        assert count == 0
        assert [line for level, line in logged if "dwd_stock_daily" in line]


class TestFreshnessReadings:
    """The freshness half of a run report: one ISO date per table, None if unknown.

    C41 measured a shape asymmetry that decides how this face can be tested
    here: ``SELECT MAX(trade_date)`` hands back a ``date`` under MySQL but the
    ISO *text* under SQLite, and :func:`opendata.pipeline.freshness._as_date`
    reads only the first two shapes - so against a SQLite warehouse even a
    populated table reports ``missing``. No production path reaches that (the
    warehouse is MySQL; C42 measured no sqlite URL), but it means a test that
    wants a non-None reading has to hand the readers' *contract objects*
    back rather than land a table into a dialect that cannot answer.
    """

    def test_the_reports_are_flattened_to_iso_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from opendata.pipeline.freshness import FreshnessReport

        def fake_ods(
            engine: Engine, domain: str, source: str, *, table: str, expected: date
        ) -> FreshnessReport:
            if source != "ths":
                raise LookupError(f"unknown source {source!r}")
            return FreshnessReport(
                domain=domain,
                source=source,
                field="trade_date",
                latest=date(2026, 9, 24),
                expected=expected,
                lag_days=0,
                status="fresh",
            )

        def fake_dwd(engine: Engine, domain: str, *, expected: date) -> FreshnessReport:
            return FreshnessReport(
                domain=domain,
                source=None,
                field="trade_date",
                latest=None,
                expected=expected,
                lag_days=None,
                status="missing",
            )

        monkeypatch.setattr(jobs, "ods_freshness", fake_ods)
        monkeypatch.setattr(jobs, "dwd_freshness", fake_dwd)

        reports = jobs._freshness(
            create_engine("sqlite://"),
            "stock_daily",
            ["ths", "no_such_source"],
            expected=date(2026, 9, 26),
        )

        assert reports == {
            "ods:ths": "2026-09-24",
            "ods:no_such_source": None,
            "dwd": None,
        }

    def test_an_unmapped_source_is_reported_instead_of_raising(
        self, logged: Sequence[tuple[str, str]]
    ) -> None:
        """The real reader does raise for a source with no mapping; the batch turns it into None."""
        reports = jobs._freshness(
            create_engine("sqlite://"),
            "stock_daily",
            ["no_such_source"],
            expected=date(2026, 9, 26),
        )

        assert reports == {"ods:no_such_source": None, "dwd": None}
        assert [line for level, line in logged if "no_such_source" in line]


def _instrument(symbol: str, **overrides: object) -> Instrument:
    """One catalog row with the fields every consumer of it expects."""
    fields: dict[str, object] = {
        "symbol": symbol,
        "exchange": "SSE" if symbol.endswith(".SH") else "SZSE",
        "name": "测试标的",
        "status": "active",
        "currency": "CNY",
        "list_date": date(2001, 7, 31),
        "delist_date": None,
        "board": None,
    }
    fields.update(overrides)
    return Instrument.model_validate(fields)


def _catalog_engine(rows: Sequence[Mapping[str, object]], *, omit: tuple[str, ...] = ()) -> Engine:
    """A landed ``dwd_instrument``: contract columns, plus the dwd trace ones."""
    declared = {
        "symbol": String(32),
        "exchange": String(16),
        "name": String(64),
        "status": String(16),
        "currency": String(8),
        "list_date": Date,
        "delist_date": Date,
        "board": String(32),
        "source": String(16),
    }
    engine = create_engine("sqlite://")
    metadata = MetaData()
    catalog = Table(
        jobs.INSTRUMENT_TABLE,
        metadata,
        *[Column(name, type_) for name, type_ in declared.items() if name not in omit],
    )
    metadata.create_all(engine)
    present = [name for name in declared if name not in omit]
    with engine.begin() as conn:
        conn.execute(
            insert(catalog),
            [{name: row.get(name) for name in present} for row in rows],
        )
    return engine


class TestInstrumentCatalog:
    """The universe's second face: the landed ``Instrument`` catalog, as a contract.

    In-memory SQLite only - the point is that the rows are read back
    through ``Instrument``, and that what the catalog cannot answer (no
    table, wrong shape, hollow date columns) is reported rather than
    guessed at.
    """

    def test_landed_rows_come_back_as_contract_objects(self) -> None:
        engine = _catalog_engine(
            [
                {
                    "symbol": "600519.SH",
                    "exchange": "SSE",
                    "name": "贵州茅台",
                    "status": "active",
                    "currency": "CNY",
                    "list_date": date(2001, 7, 31),
                    "delist_date": None,
                    "board": None,
                }
            ]
        )

        rows = jobs.landed_instruments(engine)

        assert [row.symbol for row in rows] == ["600519.SH"]
        assert rows[0].list_date == date(2001, 7, 31)

    def test_missing_table_is_no_catalog(self) -> None:
        assert jobs.landed_instruments(create_engine("sqlite://")) == []

    def test_a_table_that_is_not_the_contract_is_no_catalog(self) -> None:
        """``currency`` is required by design §4.1; a table without it is not the catalog."""
        engine = _catalog_engine(
            [{"symbol": "600519.SH", "exchange": "SSE", "name": "x", "status": "active"}],
            omit=("currency",),
        )

        assert jobs.landed_instruments(engine) == []

    def test_delisted_before_the_window_is_dropped(self) -> None:
        catalog = [_instrument("000001.SZ", delist_date=date(2026, 8, 1))]

        kept, dropped, ambiguous = jobs.drop_inactive_symbols(
            ["000001", "600519"], catalog, window=WINDOW
        )

        assert kept == ["600519"]
        assert dropped == ["000001"]
        assert ambiguous == 0

    def test_listing_after_the_window_is_dropped(self) -> None:
        catalog = [_instrument("600519.SH", list_date=date(2026, 10, 9))]

        kept, dropped, _ = jobs.drop_inactive_symbols(["600519"], catalog, window=WINDOW)

        assert (kept, dropped) == ([], ["600519"])

    def test_a_hollow_date_column_keeps_the_symbol(self) -> None:
        """C19 measured ``list_date`` hollow on 22 of 36 reads: unknown is not unlisted."""
        catalog = [_instrument("600519.SH", list_date=None, delist_date=None)]

        kept, dropped, _ = jobs.drop_inactive_symbols(["600519"], catalog, window=WINDOW)

        assert (kept, dropped) == (["600519"], [])

    def test_a_code_the_catalog_holds_twice_is_left_alone(self) -> None:
        """000001 is a stock and an index; the delisted one must not delete the live one."""
        catalog = [_instrument("000001.SZ"), _instrument("000001.SH", delist_date=date(2020, 1, 1))]

        kept, dropped, ambiguous = jobs.drop_inactive_symbols(["000001"], catalog, window=WINDOW)

        assert (kept, dropped, ambiguous) == (["000001"], [], 1)

    def test_a_code_outside_the_catalog_stays(self) -> None:
        kept, dropped, _ = jobs.drop_inactive_symbols(
            ["600519"], [_instrument("000001.SZ")], window=WINDOW
        )

        assert (kept, dropped) == (["600519"], [])

    def test_derive_universe_uses_a_callers_list_as_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _never_reads(*args: object, **kwargs: object) -> list[Instrument]:
            raise AssertionError("the catalog must not be consulted for an explicit universe")

        monkeypatch.setattr(jobs, "landed_instruments", _never_reads)

        universe, provenance = jobs.derive_universe(
            object(), "stock_daily", symbols=["000001"], limit=None, window=WINDOW
        )

        assert universe == ["000001"]
        assert provenance == {"from": "caller", "requested": "1"}

    def test_derive_universe_says_when_no_catalog_is_landed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(jobs, "symbol_universe", lambda e, d, *, limit=None: ["600519"])
        monkeypatch.setattr(jobs, "landed_instruments", lambda engine, **kwargs: [])

        universe, provenance = jobs.derive_universe(
            object(), "stock_daily", symbols=None, limit=None, window=WINDOW
        )

        assert universe == ["600519"]
        assert provenance == {"from": "dwd_stock_daily", "catalog": "absent"}

    def test_derive_universe_applies_the_catalog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            jobs, "symbol_universe", lambda e, d, *, limit=None: ["000001", "600519"]
        )
        monkeypatch.setattr(
            jobs,
            "landed_instruments",
            lambda engine, **kwargs: [_instrument("000001.SZ", delist_date=date(2026, 1, 1))],
        )

        universe, provenance = jobs.derive_universe(
            object(), "stock_daily", symbols=None, limit=None, window=WINDOW
        )

        assert universe == ["600519"]
        assert provenance["catalog"] == jobs.INSTRUMENT_TABLE
        assert provenance["dropped_inactive"] == "1"
        assert provenance["ambiguous_codes"] == "0"

    def test_derive_universe_says_which_codes_it_refused_to_judge(
        self, monkeypatch: pytest.MonkeyPatch, logged: Sequence[tuple[str, str]]
    ) -> None:
        """Ambiguity is reported and the code stays; nothing was dropped here.

        The companion to the reading above: with no dropped code the run has
        one thing left to say, and it has to say it - the count of codes the
        catalog could not adjudicate is the part of the universe that stays
        unverified.
        """
        monkeypatch.setattr(jobs, "symbol_universe", lambda e, d, *, limit=None: ["000001"])
        monkeypatch.setattr(
            jobs,
            "landed_instruments",
            lambda engine, **kwargs: [
                _instrument("000001.SZ"),
                _instrument("000001.SH", delist_date=date(2020, 1, 1)),
            ],
        )

        universe, provenance = jobs.derive_universe(
            object(), "stock_daily", symbols=None, limit=None, window=WINDOW
        )

        assert universe == ["000001"]
        assert provenance["dropped_inactive"] == "0"
        assert provenance["ambiguous_codes"] == "1"
        assert [line for level, line in logged if "several catalog rows" in line]

    async def test_the_run_reports_the_universe_it_used(
        self, stubbed_run: list[dict[str, Any]]
    ) -> None:
        result = await jobs.run_incremental_job(
            source="ths", symbols=["600519"], as_of=WINDOW.start, engine=object()
        )

        assert result.universe == {"from": "caller", "requested": "1"}
        assert result.as_dict()["universe"] == result.universe


class TestMakeFetchSymbol:
    """Step 1 keeps each source's own shape: raw frame or contract rows."""

    def test_raw_frame_passes_through_with_source_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frame = pd.DataFrame([{"日期": "2024-01-02", "收盘": 1.0}])

        class _RawFetcher:
            def transform_query(self, **kwargs: object) -> dict[str, object]:
                return kwargs

            def extract_data(self, query: object, ctx: object) -> pd.DataFrame:
                return frame

        monkeypatch.setattr(jobs, "resolve_fetcher", lambda domain, source: _RawFetcher())
        fetch = jobs.make_fetch_symbol("stock_daily", "akshare")

        assert fetch("600519", WINDOW) is frame

    def test_contract_rows_are_projected_back_onto_the_ods_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opendata.data.models import Bar

        bar = Bar(
            symbol="600519",
            trade_date=date(2024, 1, 2),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            amount=15.0,
        )

        class _ModelFetcher:
            def transform_query(self, **kwargs: object) -> dict[str, object]:
                return kwargs

            def extract_data(self, query: object, ctx: object) -> tuple[Bar, ...]:
                return (bar,)

        monkeypatch.setattr(jobs, "resolve_fetcher", lambda domain, source: _ModelFetcher())
        fetch = jobs.make_fetch_symbol("stock_daily", "ths")

        frame = fetch("600519", WINDOW)
        # ods keeps the source's own spelling (design §8.1), including the
        # dump's epoch-millisecond column derived back from the date.
        assert list(frame.columns) == [
            "thscode",
            "trade_date",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
            "date_ms",
        ]
        assert frame["close_price"][0] == 1.5
        assert list(shanghai_dates(frame["date_ms"])) == [bar.trade_date]


class TestRegisterBuiltinJobs:
    """Only executable kinds are registered, with their cron."""

    TEMPLATES = (
        ScheduleTemplate(
            name="inc",
            cron="30 17 * * 1-5",
            kind=TemplateKind.INCREMENTAL,
            payload={"domain": "stock_daily", "source": "akshare"},
        ),
        ScheduleTemplate(
            name="weekly",
            cron="0 2 * * 0",
            kind=TemplateKind.FULL_CHECK,
            payload={"domain": "stock_daily"},
        ),
    )

    async def test_a_kind_without_an_executor_is_skipped_not_half_wired(self, monkeypatch) -> None:
        """The skip branch still exists, and it is no longer about a shipped row.

        Since C57 every kind ``schedules.yaml`` declares has an executor, so
        the branch is exercised by narrowing the executable set. The reason it
        stays a skip-and-log rather than a registration: a row fired for a kind
        no body can run raises inside the scheduler, which is harder to find
        than the log line naming the row it skipped.
        """
        scheduler = FakeScheduler()
        monkeypatch.setattr(jobs, "EXECUTABLE_KINDS", frozenset({TemplateKind.INCREMENTAL}))

        job_ids = await jobs.register_builtin_jobs(scheduler, templates=self.TEMPLATES)

        assert job_ids == ["pipeline_inc"]
        assert set(scheduler.jobs) == {"pipeline_inc"}
        entry = scheduler.jobs["pipeline_inc"]
        assert entry["trigger_type"] == "cron"
        assert entry["trigger_args"] == {
            "cron_expression": "30 17 * * 1-5",
            "timezone": "Asia/Shanghai",
        }

    async def test_the_registered_callable_runs_the_injected_executor(self) -> None:
        scheduler = FakeScheduler()
        seen: list[str] = []

        async def executor(template: ScheduleTemplate) -> dict[str, Any]:
            seen.append(template.name)
            return {"ok": True}

        await jobs.register_builtin_jobs(scheduler, templates=self.TEMPLATES, run_job=executor)
        result = await scheduler.jobs["pipeline_inc"]["func"]()

        assert seen == ["inc"]
        assert result == {"ok": True}

    async def test_shipped_templates_register_the_executable_kinds(self) -> None:
        scheduler = FakeScheduler()

        job_ids = await jobs.register_builtin_jobs(scheduler)

        assert job_ids == [
            "pipeline_p0-stock-daily-incremental",
            "pipeline_p0-weekly-full-cross-check",
            "pipeline_partition-maintenance",
            "pipeline_freshness-check",
            "pipeline_retention-maintenance",
            "pipeline_key-health-notifications",
            "pipeline_p0-provider-patrol",
        ]
        # Nothing is skipped: each shipped kind has an executor.
        assert set(scheduler.jobs) == set(job_ids)

    async def test_the_weekly_row_carries_the_full_check_body(self) -> None:
        """The cron row must reach the executor, not just be registered.

        C48 measured the failure this guards: ``p0-weekly-full-cross-check``
        was declared for months while its kind had no executor, so the
        cross-check had no scheduled trigger at all.
        """
        scheduler = FakeScheduler()
        seen: list[str] = []

        async def executor(template: ScheduleTemplate) -> dict[str, Any]:
            seen.append(template.name)
            return {"ok": True}

        await jobs.register_builtin_jobs(scheduler, run_job=executor)
        result = await scheduler.jobs["pipeline_p0-weekly-full-cross-check"]["func"]()

        assert seen == ["p0-weekly-full-cross-check"]
        assert result == {"ok": True}
        assert scheduler.jobs["pipeline_p0-weekly-full-cross-check"]["trigger_args"] == {
            "cron_expression": "0 2 * * 0",
            "timezone": "Asia/Shanghai",
        }


class TestExecuteTemplate:
    """The scheduler body: payload in, counters out, fail closed on gaps."""

    async def test_scheduled_patrol_dispatch_is_lazy_and_returns_its_report(self, monkeypatch):
        from opendata.pipeline import scheduled_patrol

        seen: list[ScheduleTemplate] = []

        async def report(template: ScheduleTemplate) -> dict[str, object]:
            seen.append(template)
            return {"enabled": False, "provider_calls": 0}

        monkeypatch.setattr(scheduled_patrol, "execute_scheduled_patrol", report)
        template = ScheduleTemplate(
            name="patrol-test",
            cron="0 18 * * *",
            kind=TemplateKind.SCHEDULED_PATROL,
            payload={"tier": "P0"},
            timezone="UTC",
        )

        result = await jobs._execute_template(template)

        assert seen == [template]
        assert result == {"enabled": False, "provider_calls": 0}

    async def test_retention_job_is_report_only_unless_explicitly_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opendata.core.config import settings
        from opendata.data import minute_archive
        from opendata.pipeline import maintenance

        modes: list[bool] = []
        metadata_engines: list[object] = []
        warehouse = object()
        metadata = object()

        class _Result:
            def as_dict(self) -> dict[str, object]:
                return {"dry_run": modes[-1], "actions": []}

        def run_retention(
            engine: object,
            *,
            dry_run: bool,
            minute_metadata_engine: object,
        ) -> _Result:
            assert engine is warehouse
            modes.append(dry_run)
            metadata_engines.append(minute_metadata_engine)
            return _Result()

        monkeypatch.setattr(jobs, "warehouse_engine", lambda: warehouse)
        monkeypatch.setattr(minute_archive, "get_minute_archive_engine", lambda: metadata)
        monkeypatch.setattr(maintenance, "run_retention", run_retention)
        template = ScheduleTemplate(
            name="retention-test",
            cron="0 4 * * *",
            kind=TemplateKind.RETENTION,
            payload={"targets": "bounded"},
        )

        monkeypatch.setattr(settings, "retention_execution_enabled", False)
        report = await jobs._execute_template(template)
        monkeypatch.setattr(settings, "retention_execution_enabled", True)
        executing = await jobs._execute_template(template)

        assert modes == [True, False]
        assert metadata_engines == [metadata, metadata]
        assert report["dry_run"] is True
        assert executing["dry_run"] is False

    async def test_key_health_job_calls_the_passive_notifier(self, monkeypatch) -> None:
        from opendata.pipeline import key_health_notifications
        from opendata.pipeline import patrol as patrol_module

        async def forbidden_patrol() -> None:
            pytest.fail("the scheduled key-health job must not probe providers")

        async def passive_report() -> dict[str, object]:
            return {"observations": 0, "sources": {}}

        monkeypatch.setattr(patrol_module, "patrol", forbidden_patrol)
        monkeypatch.setattr(
            key_health_notifications,
            "notify_scheduled_key_health",
            passive_report,
        )
        template = ScheduleTemplate(
            name="key-health-test",
            cron="0 9 * * *",
            kind=TemplateKind.KEY_HEALTH,
            payload={"observations": "recent-patrol"},
        )

        result = await jobs._execute_template(template)

        assert result == {"observations": 0, "sources": {}}

    async def test_payload_routes_to_the_named_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_job(**kwargs: Any) -> jobs.JobResult:
            captured.update(kwargs)
            return jobs.JobResult(
                domain="stock_daily",
                sources=("ths",),
                window=WINDOW,
                symbols=2,
            )

        monkeypatch.setattr(jobs, "run_incremental_job", fake_job)
        template = ScheduleTemplate(
            name="inc",
            cron="30 17 * * 1-5",
            kind=TemplateKind.INCREMENTAL,
            payload={"domain": "stock_daily", "source": "ths", "second_source": "akshare"},
        )

        payload = await jobs._execute_template(template)

        assert captured["source"] == "ths"
        assert captured["second_source"] == "akshare"
        assert payload["symbols"] == 2

    async def test_an_unknown_window_kind_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        template = ScheduleTemplate(
            name="inc",
            cron="30 17 * * 1-5",
            kind=TemplateKind.INCREMENTAL,
            payload={"domain": "stock_daily", "window": "last-week"},
        )

        with pytest.raises(ValueError, match="window"):
            await jobs._execute_template(template)

    async def test_a_domain_without_a_builder_is_refused(self) -> None:
        template = ScheduleTemplate(
            name="inc",
            cron="30 17 * * 1-5",
            kind=TemplateKind.INCREMENTAL,
            payload={"domain": "index_daily"},
        )

        with pytest.raises(ValueError, match="unsupported domain"):
            await jobs._execute_template(template)


class _FixedCalendar:
    """Calendar stub: the expected data date is a constant, no DB read."""

    def __init__(self, expected: date) -> None:
        self.expected = expected

    def expected_data_date(self, day: date) -> date:
        return self.expected


class TestFreshnessExecutor:
    """The ``freshness`` row's executor - the matrix's only production caller (AC-18|01)."""

    EXPECTED = date(2026, 9, 25)

    @staticmethod
    def _template(payload: Mapping[str, Any] | None = None) -> ScheduleTemplate:
        return ScheduleTemplate(
            name="freshness-check",
            cron="30 8 * * *",
            kind=TemplateKind.FRESHNESS,
            payload=payload if payload is not None else {"domains": "all"},
        )

    def _stub_matrix(self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
        """Replace the matrix with a recorder that delivers through its channel."""
        from opendata.pipeline.alert_matrix import MatrixRun, MatrixScope

        async def fake_matrix(engine: Any, **kwargs: Any) -> MatrixRun:
            captured["engine"] = engine
            captured.update(kwargs)
            broadcast = kwargs["broadcast"]
            await broadcast({"type": "data.freshness_alert", "domain": "stock_daily"})
            return MatrixRun(
                expected=self.EXPECTED,
                reports=(),
                alerts=(),
                delivered=1,
                scope=MatrixScope(domains=20, source_legs=28, unmapped_legs=5),
            )

        monkeypatch.setattr(jobs, "run_alert_matrix", fake_matrix)

    async def test_it_measures_against_the_calendar_not_the_wall_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        engine = object()
        seen: list[Any] = []
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: engine)
        monkeypatch.setattr(
            jobs, "resolve_calendar", lambda e: seen.append(e) or _FixedCalendar(self.EXPECTED)
        )

        result = await jobs._execute_template(self._template())

        # A Saturday run must measure Friday: the lag is a trading-day lag.
        assert seen == [engine]
        assert captured["expected"] == self.EXPECTED
        assert captured["domains"] is None
        assert captured["engine"] is engine
        assert result["expected"] == self.EXPECTED.isoformat()
        assert result["scope"] == {
            "domains": 20,
            "source_legs": 28,
            "unmapped_legs": 5,
            "deferred_domains": [],
            "deferred_legs": 0,
            "failure_legs": None,
            "partitioned_tables": 0,
            "disk_path": None,
        }

    async def test_it_feeds_the_three_faces_freshness_cannot_reach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-13|07: one scheduled run must measure all four kinds.

        The other three read places the warehouse engine cannot: the shard
        checkpoint lives in the control database, the partition horizon needs
        a year, and the water level is a filesystem path. Passing ``None`` for
        any of them would leave that row of the matrix permanently green, so
        the wiring itself is the thing under test.
        """
        from datetime import date
        from pathlib import Path

        from opendata.core.config import settings

        captured: dict[str, Any] = {}
        control = object()
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(jobs, "control_engine", lambda: control)
        monkeypatch.setattr(jobs, "resolve_calendar", lambda engine: _FixedCalendar(self.EXPECTED))

        await jobs._execute_template(self._template({"domains": "all", "years_ahead": 3}))

        assert captured["control_engine"] is control
        assert captured["current_year"] == date.today().year
        assert captured["years_ahead"] == 3
        # settings.data_dir is the warehouse's configured data directory, so
        # the reading names a path the deployment declares rather than one
        # this code invented.
        assert captured["disk_path"] == Path(settings.data_dir)

    async def test_the_horizon_years_default_to_the_design_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(jobs, "control_engine", lambda: object())
        monkeypatch.setattr(jobs, "resolve_calendar", lambda engine: _FixedCalendar(self.EXPECTED))

        await jobs._execute_template(self._template())

        assert captured["years_ahead"] == 2

    async def test_it_delivers_on_the_websocket_channel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import opendata.api.websocket as websocket_module

        captured: dict[str, Any] = {}
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(jobs, "resolve_calendar", lambda engine: _FixedCalendar(self.EXPECTED))
        sent: list[dict[str, Any]] = []

        async def record(message: dict[str, Any]) -> None:
            sent.append(message)

        monkeypatch.setattr(websocket_module.ws_manager, "broadcast", record)

        result = await jobs._execute_template(self._template())

        assert sent == [{"type": "data.freshness_alert", "domain": "stock_daily"}]
        assert result["delivered"] == 1

    async def test_a_domain_list_restricted_the_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(jobs, "resolve_calendar", lambda engine: _FixedCalendar(self.EXPECTED))

        await jobs._execute_template(self._template({"domains": "stock_daily, index_daily"}))

        assert captured["domains"] == ("stock_daily", "index_daily")

    async def test_an_unmeasurable_domains_token_is_refused_before_any_db_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        self._stub_matrix(monkeypatch, captured)

        def no_engine() -> Any:
            raise AssertionError("the payload must be validated before the warehouse opens")

        monkeypatch.setattr(jobs, "warehouse_engine", no_engine)

        with pytest.raises(ValueError, match="domains"):
            await jobs._execute_template(self._template({"domains": ""}))

    async def test_the_p0_token_is_not_a_domain_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shipped template watched "p0" while no P0 tier exists in code.

        ``'all'`` replaced it in ``schedules.yaml``; the guard here is that
        a token cannot silently stand for an invented set - an unknown
        domain reaches :func:`~opendata.pipeline.alert_matrix.registered_legs`
        and raises there.
        """
        from opendata.pipeline.alert_matrix import registered_legs

        captured: dict[str, Any] = {}
        self._stub_matrix(monkeypatch, captured)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(jobs, "resolve_calendar", lambda engine: _FixedCalendar(self.EXPECTED))

        await jobs._execute_template(self._template({"domains": "p0"}))

        assert captured["domains"] == ("p0",)
        with pytest.raises(ValueError, match="not registered"):
            registered_legs(("p0",))


@pytest.mark.integration
class TestFullCheckExecutor:
    """The ``full_check`` row's executor: the cross-check's scheduled trigger.

    The warehouse stays out of it (the ods readers and the report writer are
    stubbed), but everything between the cron payload and the channels is the
    production code: :func:`~opendata.pipeline.templates.build_cross_check`,
    ``CrossCheckService.run``, the real field mappings of both legs, the
    process-scoped :class:`~opendata.pipeline.alerts.AlertPolicy` and
    :class:`~opendata.pipeline.diff_alerts.DiffAlertDispatcher`.
    """

    TEMPLATE = ScheduleTemplate(
        name="p0-weekly-full-cross-check",
        cron="0 2 * * 0",
        kind=TemplateKind.FULL_CHECK,
        payload={"domain": "stock_daily", "window": "full"},
    )

    @pytest.fixture(autouse=True)
    def _fresh_policy(self) -> Iterator[None]:
        """Start each run with an unset dedupe set and rate baseline."""
        from opendata.pipeline.diff_alerts import reset_policy

        reset_policy()
        yield
        reset_policy()

    @staticmethod
    def _frames(*, close_ths: float = 17.0, close_ak: float = 16.0) -> dict[str, pd.DataFrame]:
        """Both legs as their own ods tables keep them (source spelling).

        Every mapped column has to be there: ``normalize_frame`` fails closed
        on a missing one rather than comparing the subset that survives. The
        volume pair is spelled in each feed's own unit (ths shares, akshare
        lots) and agrees only after the mapping's ``scale: 100``.
        """
        return {
            "ths": pd.DataFrame(
                {
                    "thscode": ["600519.SH"],
                    "trade_date": [WINDOW.start],
                    "open_price": [16.5],
                    "high_price": [17.5],
                    "low_price": [16.0],
                    "close_price": [close_ths],
                    "volume": [1200.0],
                    "turnover": [1.0e8],
                }
            ),
            "akshare": pd.DataFrame(
                {
                    "股票代码": ["600519"],
                    "日期": [WINDOW.start],
                    "开盘": [16.5],
                    "最高": [17.5],
                    "最低": [16.0],
                    "收盘": [close_ak],
                    "成交量": [12.0],
                    "成交额": [1.0e8],
                }
            ),
        }

    def _stub_readers(
        self,
        monkeypatch: pytest.MonkeyPatch,
        written: list[Any],
        channels: dict[str, Any],
        frames: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        """Swap the two ods reads, the report write and the WS/hub sinks."""
        import opendata.pipeline.diff_alerts as diff_alerts_module
        import opendata.pipeline.diff_report as diff_report_module
        import opendata.pipeline.templates as templates_module

        legs = self._frames() if frames is None else frames

        class _Writer:
            def __init__(self, engine: object) -> None:
                pass

            def write(self, summary: Any) -> int:
                written.append(summary)
                return len(summary.samples)

        class _Hub:
            async def publish_diff_alert(self, message: dict[str, Any]) -> int:
                channels["hub"].append(message)
                return 2

        async def broadcast(message: dict[str, Any]) -> None:
            channels["ws"].append(message)

        dispatcher = diff_alerts_module.DiffAlertDispatcher(
            broadcast=broadcast, hub=_Hub(), mail_send=None, recipients=()
        )
        monkeypatch.setattr(diff_report_module, "DiffReportWriter", _Writer)
        monkeypatch.setattr(
            templates_module,
            "ods_raw_reader",
            lambda engine, domain, source: lambda window: legs[source].copy(),
        )
        monkeypatch.setattr(templates_module, "cross_check_window", lambda *a, **k: WINDOW)
        monkeypatch.setattr(diff_alerts_module, "production_dispatcher", lambda **k: dispatcher)
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())

    async def test_the_scheduled_run_compares_reports_and_alerts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opendata.pipeline.cross_check import Verdict

        written: list[Any] = []
        channels: dict[str, Any] = {"ws": [], "hub": []}
        self._stub_readers(monkeypatch, written, channels)

        result = await jobs._execute_template(self.TEMPLATE)

        assert result["verdict"] == Verdict.DEVIATION.value
        assert result["compared_keys"] == 1, "the two spellings of the key must still join"
        assert result["deviations"] == 1
        assert result["missing"] == 0
        assert result["per_field"] == {"close": 1}
        # The batch id is the hook's shape: a report row must name which run
        # produced it, and a scheduled check that invented its own prefix
        # would leave the pipeline's key space empty again.
        assert result["batch_id"] == f"xcheck:stock_daily:{WINDOW.label()}"
        assert written and written[0].batch_id == result["batch_id"]
        assert len(channels["ws"]) == 1 and channels["ws"][0]["level"] == "warning"
        assert len(channels["hub"]) == 1
        assert result["deliveries"] == [
            {
                "batch_id": result["batch_id"],
                "alerted": True,
                "level": "warning",
                "reason": channels["ws"][0]["reason"],
                "ws_sent": True,
                "ws_error": None,
                "hub_delivered": 2,
                "hub_error": None,
                "mail_sent": 0,
                "mail_errors": [],
                "mail_skipped": "SMTP not configured (no sender)",
            }
        ]

    async def test_a_second_run_of_the_same_difference_is_not_re_alerted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-9|04 across triggers: the dedupe state outlives one comparison."""
        written: list[Any] = []
        channels: dict[str, Any] = {"ws": [], "hub": []}
        self._stub_readers(monkeypatch, written, channels)

        first = await jobs._execute_template(self.TEMPLATE)
        second = await jobs._execute_template(self.TEMPLATE)

        assert first["deliveries"][0]["alerted"] is True
        assert second["deliveries"][0]["alerted"] is False
        assert "already alerted" in second["deliveries"][0]["reason"]
        # Suppressed means the channel sees nothing, not that the comparison
        # stopped happening: the report still carries the difference.
        assert len(channels["ws"]) == 1
        assert len(written) == 2

    async def test_consistent_legs_produce_no_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written: list[Any] = []
        channels: dict[str, Any] = {"ws": [], "hub": []}
        self._stub_readers(
            monkeypatch, written, channels, frames=self._frames(close_ths=17.0, close_ak=17.0)
        )

        result = await jobs._execute_template(self.TEMPLATE)

        assert result["verdict"] == "consistent"
        assert result["deliveries"][0]["alerted"] is False
        assert channels["ws"] == []

    async def test_a_payload_window_other_than_full_is_refused(self) -> None:
        template = ScheduleTemplate(
            name="weekly",
            cron="0 2 * * 0",
            kind=TemplateKind.FULL_CHECK,
            payload={"domain": "stock_daily", "window": "last-trading-day"},
        )

        with pytest.raises(ValueError, match="derives its"):
            await jobs._execute_template(template)

    async def test_a_domain_without_a_comparable_pair_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed rather than compare a source with itself."""
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: object())
        monkeypatch.setattr(
            jobs, "SUPPORTED_DOMAINS", frozenset({"trading_calendar", "index_daily"})
        )

        def template(domain: str) -> ScheduleTemplate:
            return ScheduleTemplate(
                name="weekly",
                cron="0 2 * * 0",
                kind=TemplateKind.FULL_CHECK,
                payload={"domain": domain, "window": "full"},
            )

        # authority.json ranks one feed for the calendar: nothing to check it
        # against, and a check that silently compared it with itself would be
        # green forever.
        with pytest.raises(LookupError, match="no second feed"):
            await jobs._execute_template(template("trading_calendar"))
        # index_daily ranks two, but only one of them has a field mapping,
        # which is the same hole measured from the other side (C48 §4).
        with pytest.raises(LookupError, match="unknown domain 'index_daily'"):
            await jobs._execute_template(template("index_daily"))


class TestAttachBuiltinJobs:
    """Startup registers on the live scheduler, not the A1 wrapper."""

    async def test_registers_the_cron_jobs_on_the_raw_scheduler(self, monkeypatch):
        import opendata.services.scheduler_service as scheduler_service_module

        recorded: list[dict[str, object]] = []

        class _RawScheduler:
            def add_job(self, func: object, **kwargs: object) -> None:
                recorded.append(kwargs)

        class _Service:
            def get_scheduler(self) -> object:
                return _RawScheduler()

        monkeypatch.setattr(scheduler_service_module, "get_scheduler_service", lambda: _Service())
        ids = await jobs.attach_builtin_jobs()

        assert ids == [
            "pipeline_p0-stock-daily-incremental",
            "pipeline_p0-weekly-full-cross-check",
            "pipeline_partition-maintenance",
            "pipeline_freshness-check",
            "pipeline_retention-maintenance",
            "pipeline_key-health-notifications",
            "pipeline_p0-provider-patrol",
        ]
        trigger = recorded[0]["trigger"]
        assert type(trigger).__name__ == "CronTrigger"
        assert "hour='17'" in str(trigger) and "day_of_week='1-5'" in str(trigger)
        weekly = recorded[1]["trigger"]
        assert type(weekly).__name__ == "CronTrigger"
        assert "hour='2'" in str(weekly) and "day_of_week='0'" in str(weekly)
        parts_trigger = recorded[2]["trigger"]
        assert type(parts_trigger).__name__ == "CronTrigger"
        assert "hour='3'" in str(parts_trigger) and "day_of_week='*'" in str(parts_trigger)
        freshness_trigger = recorded[3]["trigger"]
        assert type(freshness_trigger).__name__ == "CronTrigger"
        assert "hour='8'" in str(freshness_trigger) and "minute='30'" in str(freshness_trigger)
        assert "hour='4'" in str(recorded[4]["trigger"])
        assert "hour='9'" in str(recorded[5]["trigger"])
        patrol = recorded[6]["trigger"]
        assert str(patrol.timezone) == "UTC"
        next_fire = patrol.get_next_fire_time(
            None, datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        )
        assert next_fire == datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc)
        assert recorded[0]["id"] == "pipeline_p0-stock-daily-incremental"
        assert "cron_expression" not in recorded[0]

    async def test_a_missing_scheduler_registers_nothing(self, monkeypatch):
        import opendata.services.scheduler_service as scheduler_service_module

        monkeypatch.setattr(scheduler_service_module, "get_scheduler_service", lambda: None)
        assert await jobs.attach_builtin_jobs() == []


class TestPartitionMaintenanceJob:
    """AC-8 items 5/6: the daily row must split ``pmax``, not only report it.

    The ``information_schema`` reads and the ``REORGANIZE`` are MySQL-only, so
    the maintainer's three warehouse touches are stood in for here. What these
    tests own is what jobs.py adds on top of them: which tables get asked, that
    the plan is applied rather than returned, and that the pass re-reads the
    horizon instead of trusting its own statement. The real ``ALTER`` - and a
    new-year row landing in its own partition - are the e2e class of
    ``tests/test_partition_maintenance.py``.
    """

    @staticmethod
    def _patch_warehouse(
        monkeypatch: pytest.MonkeyPatch,
        states: dict[str, list],
        *,
        advance: bool = True,
    ) -> dict[str, list[str]]:
        """Stand in for ``information_schema.PARTITIONS`` and the ``ALTER``.

        Args:
            states: Table to the partitions that exist before the pass; a table
                left out of the dict is an unpartitioned one.
            advance: When True, ``ensure`` appends the partitions it planned,
                the way a successful ``REORGANIZE`` changes what the next read
                sees. When False the state stays behind, which is how a pass
                that sent its statement without closing the horizon reads.

        Returns:
            The partition names each table was asked to apply, in call order.
        """
        from opendata.pipeline.partitions import (
            PartitionMaintainer,
            PartitionState,
            plan_yearly_partitions,
        )

        asked: dict[str, list[str]] = {}

        def is_partitioned(self: object, table: str) -> bool:
            return table in states

        def state(self: object, table: str) -> list:
            return list(states[table])

        def ensure(self: object, table: str, *, current_year: int, years_ahead: int) -> list[str]:
            plan = plan_yearly_partitions(
                list(states[table]), current_year=current_year, years_ahead=years_ahead
            )
            asked[table] = [name for name, _ in plan]
            if advance:
                states[table].extend(PartitionState(name, bound) for name, bound in plan)
            return asked[table]

        monkeypatch.setattr(PartitionMaintainer, "is_partitioned", is_partitioned)
        monkeypatch.setattr(PartitionMaintainer, "state", state)
        monkeypatch.setattr(PartitionMaintainer, "ensure", ensure)
        return asked

    @staticmethod
    def _through(bound: date) -> list:
        """Yearly partitions up to ``bound``, plus the ``pmax`` fallback."""
        from opendata.pipeline.partitions import PartitionState

        years = range(2024, bound.year)
        return [PartitionState(f"p{year}", date(year + 1, 1, 1)) for year in years] + [
            PartitionState("pmax", None)
        ]

    def test_the_pass_applies_the_plan_rather_than_returning_it(self, monkeypatch):
        """The matrix half reports a gap; this half has to close it."""
        states = {"dwd_stock_daily": self._through(date(2025, 1, 1))}
        asked = self._patch_warehouse(monkeypatch, states)

        run = jobs.maintain_partition_horizon(
            create_engine("sqlite://"),
            current_year=2026,
            years_ahead=2,
            tables=("dwd_stock_daily",),
        )

        assert asked == {"dwd_stock_daily": ["p2025", "p2026", "p2027"]}
        assert run.applied == {"dwd_stock_daily": ["p2025", "p2026", "p2027"]}
        assert run.applied_total == 3
        assert run.remaining == {}
        assert (run.measured, run.partitioned) == (("dwd_stock_daily",), 1)

    def test_a_pass_that_applied_but_left_the_horizon_short_says_so(self, monkeypatch):
        states = {"dwd_stock_daily": self._through(date(2027, 1, 1))}
        self._patch_warehouse(monkeypatch, states, advance=False)

        run = jobs.maintain_partition_horizon(
            create_engine("sqlite://"),
            current_year=2026,
            years_ahead=2,
            tables=("dwd_stock_daily",),
        )

        assert run.applied == {"dwd_stock_daily": ["p2027"]}
        # The statement went out and the gap is still there: counting the
        # applied names alone would read this pass as a closed horizon.
        assert run.remaining == {"dwd_stock_daily": ["p2027"]}

    def test_an_unpartitioned_table_is_neither_asked_nor_counted(self, monkeypatch):
        asked = self._patch_warehouse(monkeypatch, {})

        run = jobs.maintain_partition_horizon(
            create_engine("sqlite://"), current_year=2026, years_ahead=2, tables=("ods_x",)
        )

        assert run.measured == ("ods_x",)
        # 0 is the reading that says "this pass saw no partitions"; an empty
        # applied dict with a positive count would say "all years exist".
        assert run.partitioned == 0
        assert asked == {}

    def test_a_second_pass_is_asked_and_answers_nothing(self, monkeypatch):
        states = {"dwd_stock_daily": self._through(date(2029, 1, 1))}
        asked = self._patch_warehouse(monkeypatch, states)

        run = jobs.maintain_partition_horizon(
            create_engine("sqlite://"),
            current_year=2026,
            years_ahead=2,
            tables=("dwd_stock_daily",),
        )

        assert asked == {"dwd_stock_daily": []}
        assert run.applied == {} and run.remaining == {}
        assert run.partitioned == 1

    def test_the_default_table_set_is_the_registered_warehouse(self, monkeypatch):
        """No ``tables=`` means every registered dwd/ods table, not a name kept
        in the job body: a new domain table has to be maintained by the same
        cron run without editing this module."""
        monkeypatch.setattr(
            jobs,
            "warehouse_tables",
            lambda domains=None: ("dwd_stock_daily", "ods_stock_daily_ths"),
        )
        states = {"dwd_stock_daily": self._through(date(2029, 1, 1))}
        asked = self._patch_warehouse(monkeypatch, states)

        run = jobs.maintain_partition_horizon(
            create_engine("sqlite://"), current_year=2026, years_ahead=2, domains=("stock_daily",)
        )

        assert run.measured == ("dwd_stock_daily", "ods_stock_daily_ths")
        assert asked == {"dwd_stock_daily": []}
        assert run.partitioned == 1

    def test_the_default_census_is_read_after_the_providers_are_registered(self, monkeypatch):
        """A fake ``warehouse_tables`` hid the real trap (C58).

        The registry fills lazily, so in a process that has not resolved a
        fetcher yet it knows zero capabilities and the census is the 20
        ``dwd_*`` names alone -- every ``ods_*`` leg, which is the layer the
        patrol writes into, would fall outside the pass in silence.
        """
        from opendata.data import providers as providers_module

        order: list[str] = []
        monkeypatch.setattr(
            providers_module, "register_providers", lambda: order.append("register") or []
        )
        monkeypatch.setattr(
            jobs, "warehouse_tables", lambda domains=None: order.append("read") or ()
        )
        self._patch_warehouse(monkeypatch, {})

        jobs.maintain_partition_horizon(
            create_engine("sqlite://"), current_year=2026, years_ahead=2
        )

        assert order == ["register", "read"]

    def test_the_registered_census_reaches_the_ods_layer(self):
        """Pin the registry-derived legacy table plan without claiming live table existence."""
        from opendata.data.domains import dwd_table, load_domains, ods_table
        from opendata.data.providers import register_providers
        from opendata.pipeline.alert_matrix import registered_legs, warehouse_tables

        deferred_domains = {
            "bls_search",
            "bls_series",
            "cboe_available_indices",
            "cboe_index_constituent_quotes",
            "cboe_index_search",
            "currency_reference_rates",
            "equity_historical",
            "equity_quote",
            "federal_reserve_money_measures",
            "federal_reserve_treasury_rates",
            "fred_search",
            "fred_series",
            "sofr",
            "sonia",
            "balance_of_payments",
            "yield_curve",
        }

        register_providers()
        tables = warehouse_tables()
        known = set(load_domains())
        legs = registered_legs()
        measured_domains = known - deferred_domains
        ods = [table for table in tables if table.startswith("ods_")]
        expected_ods = {
            ods_table(domain, source) for domain in measured_domains for source in legs[domain]
        }
        expected_dwd = {dwd_table(domain) for domain in measured_domains}
        deferred_legs = sum(len(legs[domain]) for domain in deferred_domains)

        assert ods, "census 里没有 ods 表：注册表读空了，维护面覆盖不到落库层"
        assert len(known) == 36
        assert deferred_domains <= known
        assert set(legs) == known
        assert sum(len(sources) for sources in legs.values()) == 49
        assert len(measured_domains) == 20
        assert len(deferred_domains) == 16
        assert len(measured_domains) + len(deferred_domains) == len(known)
        assert len(expected_ods) == 33
        assert len(expected_dwd) == 20
        assert deferred_legs == 16
        assert len(expected_ods) + deferred_legs == 49
        assert set(ods) == expected_ods
        for table in ods:
            tail = table[len("ods_") :].rsplit("_", 1)
            assert len(tail) == 2 and tail[0] in known, f"{table} 不是 ods_<domain>_<source>"
        assert set(tables) - set(ods) == expected_dwd

    async def test_the_shipped_row_dispatches_to_the_partition_body(self, monkeypatch):
        """Registering a cron row is not the same as it reaching a body (C48)."""
        called: dict[str, object] = {}
        monkeypatch.setattr(jobs, "warehouse_engine", lambda: create_engine("sqlite://"))

        def fake_maintain(
            engine: Engine,
            *,
            current_year: int,
            years_ahead: int,
            tables: Sequence[str] | None = None,
            domains: Sequence[str] | None = None,
        ) -> jobs.PartitionRun:
            called["current_year"] = current_year
            called["years_ahead"] = years_ahead
            called["tables"] = tables
            return jobs.PartitionRun(
                current_year=current_year,
                years_ahead=years_ahead,
                measured=("dwd_stock_daily",),
                partitioned=1,
            )

        monkeypatch.setattr(jobs, "maintain_partition_horizon", fake_maintain)
        template = next(
            row for row in jobs.PIPELINE_TEMPLATES if row.kind is TemplateKind.PARTITION_MAINTENANCE
        )

        result = await jobs._execute_template(template)

        assert called["years_ahead"] == 2  # schedules.yaml spells it "2"
        assert called["current_year"] == date.today().year
        assert called["tables"] is None  # the whole registered warehouse
        assert result["applied_total"] == 0
        assert result["remaining"] == {}
