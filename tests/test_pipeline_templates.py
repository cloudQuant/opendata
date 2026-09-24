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

from datetime import date

import pytest

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
)


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
