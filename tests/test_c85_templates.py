"""P0 template rendering: schedule parsing, source lookup and ods key reads.

The warehouse seams here are the ``engine.connect()`` inside
:func:`opendata.pipeline.templates._load_ods_rows`, so a recording fake
engine shows exactly which scoped statement the templates would run.
No connection, no DDL.
"""

import pathlib
import sys
import types
import typing
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import async_sessionmaker

from opendata.data.mapping import DomainMapping, FieldMapping, load_mapping, mapping_sources
from opendata.pipeline import runner, templates
from opendata.pipeline.runner import PipelineContext, Window
from opendata.pipeline.templates import (
    _load_ods_rows,
    _ods_affected_keys,
    _parse_template,
    default_source,
    load_schedule_templates,
    ods_frame_reader,
    ods_raw_reader,
)
from opendata.pipeline.trading_calendar import CalendarView

END_OF_WINDOW = date(2026, 1, 9)
THS_COLUMNS = (
    "thscode",
    "trade_date",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)


def _ths_row(symbol: str, day: date) -> tuple[object, ...]:
    """One full ths stock-daily row in the source's own column order."""
    return (symbol, day, 1.0, 2.0, 0.5, 1.5, 100.0, 150.0)


class RecordingResult:
    """The executed-statement shape ``_load_ods_rows`` consumes."""

    def __init__(self, columns: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
        """Carry the projection and its rows."""
        self._columns = columns
        self._rows = rows

    def keys(self) -> tuple[str, ...]:
        """Return the column names the driver would report."""
        return self._columns

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the rows the driver would hand back."""
        return list(self._rows)

    def all(self) -> list[tuple[object, ...]]:
        """Return the same rows through the Core spelling."""
        return list(self._rows)


class RecordingConnection:
    """A connection that records statements instead of opening a socket."""

    def __init__(self, engine: "RecordingEngine") -> None:
        """Bind the recording engine."""
        self._engine = engine

    def execute(self, statement: Any, params: dict[str, object] | None = None) -> RecordingResult:
        """Record one execution and answer with the canned rows."""
        self._engine.calls.append((getattr(statement, "text", str(statement)), dict(params or {})))
        return RecordingResult(self._engine.columns, self._engine.rows)

    def close(self) -> None:
        """Mark the connection closed."""
        self._engine.closed += 1

    def __enter__(self) -> "RecordingConnection":
        """Enter the context the templates use."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Leave the context; nothing to release."""
        return None


class RecordingEngine:
    """A fake warehouse engine: records every statement, returns fixed rows."""

    def __init__(
        self,
        rows: list[tuple[object, ...]] | None = None,
        columns: tuple[str, ...] = THS_COLUMNS,
    ) -> None:
        """Start with the given canned projection."""
        self.rows = rows or []
        self.columns = columns
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = 0

    def connect(self) -> RecordingConnection:
        """Hand back a recording connection."""
        return RecordingConnection(self)

    @property
    def statement(self) -> str:
        """The single SQL text recorded so far."""
        assert self.calls, "expected exactly one recorded statement"
        return self.calls[0][0]

    @property
    def bind_params(self) -> dict[str, object]:
        """The parameters of that statement."""
        assert self.calls, "expected exactly one recorded statement"
        return self.calls[0][1]


def _context(**overrides: object) -> PipelineContext:
    """A generic (unpartitioned) pipeline context for the key loader."""
    values: dict[str, object] = {
        "domain": "stock_daily",
        "source": "ths",
        "window": Window(start=date(2026, 1, 5), end=END_OF_WINDOW),
        "affected_keys": [],
        "symbols": ("600519", "000001"),
        "comparison_windows": {"600519": Window(start=date(2026, 1, 5), end=END_OF_WINDOW)},
    }
    values.update(overrides)
    return PipelineContext(**values)  # type: ignore[arg-type]


def test_payload_that_is_not_a_mapping_is_malformed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "schedules.yaml"
    path.write_text(
        "templates:\n"
        "  - name: daily\n"
        "    cron: '0 17 * * *'\n"
        "    kind: incremental\n"
        "    payload: not-a-mapping\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="payload must be a mapping"):
        load_schedule_templates(path)


def test_entry_that_is_not_a_mapping_names_its_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="is not a mapping"):
        load_schedule_templates(_write_templates(tmp_path, "- just-a-string"))


def test_template_entry_round_trips_its_declared_fields() -> None:
    template = _parse_template(
        {"name": "daily", "cron": "0 17 * * *", "kind": "incremental", "note": "staggered"},
        pathlib.Path("schedules.yaml"),
    )

    assert template.name == "daily"
    assert template.kind is templates.TemplateKind.INCREMENTAL
    assert template.payload == {}
    assert template.timezone == "Asia/Shanghai"


def test_invalid_timezone_is_reported(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RuntimeError, match="IANA zone"):
        load_schedule_templates(
            _write_templates(
                tmp_path,
                "- name: daily\n  cron: '0 17 * * *'\n  kind: incremental\n  timezone: Not/AZone\n",
            )
        )


def _write_templates(tmp_path: pathlib.Path, entries: str) -> pathlib.Path:
    """Write a schedules file with the given raw entries block."""
    path = tmp_path / "schedules.yaml"
    path.write_text(f"templates:\n{entries}\n", encoding="utf-8")
    return path


def test_default_source_returns_the_source_that_maps_the_domain() -> None:
    source = default_source("stock_daily")

    assert source in mapping_sources()
    assert "stock_daily" in load_mapping(source).domains


def test_unmapped_domain_has_no_default_source() -> None:
    with pytest.raises(LookupError, match="no source mapping covers domain 'not_a_domain'"):
        default_source("not_a_domain")


def test_ods_affected_keys_materializes_the_generic_context() -> None:
    engine = RecordingEngine(
        rows=[
            _ths_row("600519.SH", date(2026, 1, 5)),
            # A repeated landing must collapse to one key.
            _ths_row("600519.SH", date(2026, 1, 5)),
            _ths_row("000001.SZ", date(2026, 1, 6)),
        ]
    )

    keys = _ods_affected_keys(engine, "stock_daily", ["ths"])(_context())

    assert isinstance(keys, list)
    assert keys == [("600519", date(2026, 1, 5)), ("000001", date(2026, 1, 6))]
    assert "(`thscode` = :symbol_0 OR `thscode` LIKE :symbol_prefix_0 ESCAPE '!')" in (
        engine.statement
    )
    assert engine.bind_params["symbol_0"] == "600519"


def test_ods_affected_keys_of_an_empty_leg_is_empty() -> None:
    engine = RecordingEngine(rows=[])

    assert _ods_affected_keys(engine, "stock_daily", ["ths"])(_context()) == []


def test_short_and_non_date_affected_keys_are_never_used_as_window_bounds() -> None:
    engine = RecordingEngine(rows=[])
    reader = ods_frame_reader(engine, "stock_daily", "ths")

    frame = reader(
        date(2026, 1, 5),
        END_OF_WINDOW,
        {
            # Truncated key: no date slot to read at all.
            ("600519",),
            # A driver that kept the date as text is not a bound either.
            ("600519.SH", "2026-01-20"),
            # The one usable out-of-window date.
            ("600519.SH", date(2026, 1, 2)),
        },
        symbols=["600519"],
        symbol_windows={"600519": Window(start=date(2026, 1, 5), end=END_OF_WINDOW)},
    )

    assert frame.empty
    assert "`trade_date` IN (:affected_day_0_0)" in engine.statement
    assert engine.bind_params == {
        "symbol_0": "600519",
        "symbol_prefix_0": "600519.%",
        "start_0": date(2026, 1, 5),
        "end_0": END_OF_WINDOW,
        "affected_day_0_0": date(2026, 1, 2),
    }


def test_raw_reader_scopes_the_select_to_the_requested_symbols() -> None:
    engine = RecordingEngine(rows=[_ths_row("600519.SH", date(2026, 1, 5))])
    read = ods_raw_reader(engine, "stock_daily", "ths")

    frame = read(
        Window(start=date(2026, 1, 5), end=END_OF_WINDOW),
        symbols=["600519"],
        symbol_windows={"600519": Window(start=date(2026, 1, 4), end=date(2026, 1, 8))},
    )

    assert list(frame["thscode"]) == ["600519.SH"]
    assert engine.statement.startswith("SELECT * FROM `ods_stock_daily_ths` WHERE ")
    assert "(`thscode` = :symbol_0 OR `thscode` LIKE :symbol_prefix_0 ESCAPE '!')" in (
        engine.statement
    )
    assert "`trade_date` >= :start_0 AND `trade_date` <= :end_0" in engine.statement
    assert engine.bind_params["start_0"] == date(2026, 1, 4)
    assert engine.bind_params["end_0"] == date(2026, 1, 8)
    assert "start" not in engine.bind_params


def test_raw_reader_skips_symbols_with_no_window_without_reading() -> None:
    engine = RecordingEngine(rows=[_ths_row("600519.SH", date(2026, 1, 5))])

    frame = ods_raw_reader(engine, "stock_daily", "ths")(
        Window(start=date(2026, 1, 5), end=END_OF_WINDOW),
        symbols=["600519"],
        symbol_windows={"600519": None},
    )

    assert frame.empty
    assert engine.calls == []


def test_bounded_read_of_a_symbol_list_without_windows_falls_back_to_the_range() -> None:
    engine = RecordingEngine(rows=[])
    rows = _load_ods_rows(
        engine,
        "ods_stock_daily_ths",
        "stock_daily",
        date(2026, 1, 5),
        END_OF_WINDOW,
        {("600519.SH", date(2026, 1, 5)), ("000001.SZ", date(2026, 1, 6))},
        time_column="trade_date",
        symbol_column="thscode",
    )

    assert rows == []
    assert "SELECT * FROM `ods_stock_daily_ths` WHERE (" in engine.statement
    # Exact key pairs, all values bound, ordered so the statement is stable.
    assert engine.statement.endswith(
        "(`thscode` = :symbol_0 AND `trade_date` = :day_0) "
        "OR (`thscode` = :symbol_1 AND `trade_date` = :day_1))"
    )
    assert engine.bind_params["symbol_0"] == "000001.SZ"
    assert engine.bind_params["day_1"] == date(2026, 1, 5)


def test_a_key_without_a_date_column_never_widens_the_read_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot domains key on the symbol alone: no affected date to read.

    ``date_index`` is then ``None``, so the reader must keep the caller's
    window instead of mining the affected keys for bounds.
    """
    snapshot = DomainMapping(
        domain="stock_daily",
        key=("symbol",),
        fields={
            "symbol": FieldMapping("thscode", normalize="plain"),
            "trade_date": FieldMapping("trade_date"),
        },
        adjust="unadjusted",
        suspension="absent_row",
        denominator="key_union",
    )
    monkeypatch.setattr(templates, "require_domain_mapping", lambda source, domain: snapshot)
    engine = RecordingEngine(rows=[], columns=("thscode", "trade_date"))

    frame = ods_frame_reader(engine, "stock_daily", "ths")(
        date(2026, 1, 5),
        END_OF_WINDOW,
        {("600519.SH", date(2026, 1, 2))},
    )

    assert frame.empty
    assert engine.bind_params == {"start": date(2026, 1, 5), "end": END_OF_WINDOW}
    assert "`trade_date` >= :start AND `trade_date` <= :end" in engine.statement


def test_type_checking_only_imports_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute the annotation-only block with the flag forced true.

    ``typing.TYPE_CHECKING`` is ``False`` at runtime, so the guarded
    imports never run; re-executing the source with the flag on proves
    every name the module's annotations reference is importable. One of
    them (``runner.Hook``) is itself a typing-only alias with no runtime
    value, so the probe materializes it for the duration of the exec and
    hands the module back afterwards.
    """
    monkeypatch.setattr(runner, "Hook", Callable[[object], object], raising=False)

    namespace = _exec_with_type_checking(templates)

    assert namespace["Sequence"] is Sequence
    assert namespace["Awaitable"] is Awaitable
    assert namespace["Engine"] is sqlalchemy.engine.Engine
    assert namespace["AsyncSession"].__name__ == "AsyncSession"
    assert namespace["async_sessionmaker"] is async_sessionmaker
    assert namespace["CrossCheckService"].__name__ == "CrossCheckService"
    assert namespace["DwdMergeService"].__name__ == "DwdMergeService"
    assert namespace["PipelineContext"] is PipelineContext
    assert namespace["CalendarView"] is CalendarView


def _exec_with_type_checking(module: object) -> dict[str, object]:
    """Re-execute ``module``'s source with ``TYPE_CHECKING`` true.

    The probe runs under its own throwaway name: ``dataclass`` resolution
    looks the defining module up in ``sys.modules``, so the namespace has
    to be registered while the source executes (and removed after).
    """
    path = pathlib.Path(module.__file__)
    source = path.read_text(encoding="utf-8")
    name = f"opendata_c85_probe_{path.stem}"
    probe = types.ModuleType(name)
    probe.__dict__["__file__"] = str(path)
    original = typing.TYPE_CHECKING
    typing.TYPE_CHECKING = True
    sys.modules[name] = probe
    try:
        exec(compile(source, str(path), "exec"), probe.__dict__)
    finally:
        typing.TYPE_CHECKING = original
        sys.modules.pop(name, None)
    return dict(probe.__dict__)
