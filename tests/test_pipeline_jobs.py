"""Unit tests for the pipeline job trigger (AC-13, milestone B4).

The warehouse is off-limits here: these tests assert the *orchestration*
(universe, run order, which run carries the cross-check pair, scheduler
registration) with the pipeline builder and the DB readers stubbed. The
real end-to-end run of this module is the acceptance evidence in
``docs/evidence/B4/``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from opendata.pipeline import jobs
from opendata.pipeline.dump_import import shanghai_dates
from opendata.pipeline.runner import PipelineOutcome, Window
from opendata.pipeline.templates import ScheduleTemplate, TemplateKind

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
        result = await jobs.run_incremental_job(
            source="ths", second_source="akshare", symbols=["600519"], engine=object()
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
