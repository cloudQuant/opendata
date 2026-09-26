"""Unit tests for the pipeline job trigger (AC-13, milestone B4).

The warehouse is off-limits here: these tests assert the *orchestration*
(universe, run order, which run carries the cross-check pair, scheduler
registration) with the pipeline builder and the DB readers stubbed. The
real end-to-end run of this module is the acceptance evidence in
``docs/evidence/B4/``.
"""

from __future__ import annotations

from datetime import date
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

    async def run(self, window: Window, *, resume: bool = True) -> PipelineOutcome:
        self.ran.append((window, resume))
        return PipelineOutcome(
            pipeline_id=f"stock_daily:{self.source}:{window.label()}",
            shards_total=1,
            shards_done=1,
            rows_written=self.rows,
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
        jobs, "make_fetch_symbol", lambda domain, source: lambda s, w: pd.DataFrame()
    )
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

    async def test_incremental_templates_are_registered_and_others_skipped(self) -> None:
        scheduler = FakeScheduler()

        job_ids = await jobs.register_builtin_jobs(scheduler, templates=self.TEMPLATES)

        assert job_ids == ["pipeline_inc"]
        assert set(scheduler.jobs) == {"pipeline_inc"}
        entry = scheduler.jobs["pipeline_inc"]
        assert entry["trigger_type"] == "cron"
        assert entry["trigger_args"] == {"cron_expression": "30 17 * * 1-5"}

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

    async def test_shipped_templates_register_the_p0_incremental(self) -> None:
        scheduler = FakeScheduler()

        job_ids = await jobs.register_builtin_jobs(scheduler)

        assert job_ids == ["pipeline_p0-stock-daily-incremental"]


class TestExecuteTemplate:
    """The scheduler body: payload in, counters out, fail closed on gaps."""

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

        assert ids == ["pipeline_p0-stock-daily-incremental"]
        trigger = recorded[0]["trigger"]
        assert type(trigger).__name__ == "CronTrigger"
        assert "hour='17'" in str(trigger) and "day_of_week='1-5'" in str(trigger)
        assert recorded[0]["id"] == "pipeline_p0-stock-daily-incremental"
        assert "cron_expression" not in recorded[0]

    async def test_a_missing_scheduler_registers_nothing(self, monkeypatch):
        import opendata.services.scheduler_service as scheduler_service_module

        monkeypatch.setattr(scheduler_service_module, "get_scheduler_service", lambda: None)
        assert await jobs.attach_builtin_jobs() == []
