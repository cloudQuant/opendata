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

import pandas as pd
import pytest

from opendata.pipeline import templates
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


class _Result:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns = columns
        self._rows = rows

    def keys(self) -> list[str]:
        return self._columns

    def fetchall(self) -> list[tuple]:
        return self._rows


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
