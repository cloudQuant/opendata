"""Mapped ODS watermark reads and the windows derived from them.

Every test here drives :mod:`opendata.pipeline.ods_watermark` through a
recording fake engine: the module's only warehouse seams are
``inspect(engine).has_table(...)`` and ``engine.connect()``, so the
emitted SQL and its bind parameters can be asserted without a database.
"""

import pathlib
import sys
import types
import typing
from collections.abc import Mapping, Sequence
from datetime import date, datetime

import pytest
import sqlalchemy

from opendata.data.mapping import DomainMapping, FieldMapping
from opendata.pipeline import ods_watermark
from opendata.pipeline.ods_watermark import (
    effective_symbol_windows,
    enclosing_window,
    read_ods_watermarks,
    union_symbol_windows,
)
from opendata.pipeline.runner import Window

END = date(2026, 9, 24)


class RecordingResult:
    """The executed-statement shape ``ods_watermark`` consumes (``.all()``)."""

    def __init__(self, rows: Sequence[tuple[object, ...]]) -> None:
        """Carry the rows a real cursor would have produced."""
        self._rows = list(rows)

    def all(self) -> list[tuple[object, ...]]:
        """Return the batched result rows."""
        return list(self._rows)

    def keys(self) -> tuple[str, ...]:
        """Return the projection names (``symbol``, ``latest_date``)."""
        return ("symbol", "latest_date")

    def fetchall(self) -> list[tuple[object, ...]]:
        """Return the same rows through the DBAPI spelling."""
        return list(self._rows)


class RecordingConnection:
    """A connection that records statements instead of opening a socket."""

    def __init__(self, engine: "RecordingEngine") -> None:
        """Bind the recording engine."""
        self._engine = engine

    def execute(self, statement: object, params: dict[str, object] | None = None):
        """Record one execution and answer with the canned rows."""
        text = getattr(statement, "text", str(statement))
        self._engine.calls.append((text, dict(params or {})))
        return RecordingResult(self._engine.rows)

    def close(self) -> None:
        """Mark the connection closed (the module never closes explicitly)."""
        self._engine.closed += 1

    def __enter__(self) -> "RecordingConnection":
        """Enter the context the module's ``with`` expects."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Leave the context; nothing to release."""
        return None


class RecordingEngine:
    """A fake engine: records every statement, connects on demand."""

    def __init__(self, rows: Sequence[tuple[object, ...]] = ()) -> None:
        """Start with no recorded calls and the given canned rows."""
        self.rows = list(rows)
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = 0

    def connect(self) -> RecordingConnection:
        """Hand back a recording connection."""
        return RecordingConnection(self)

    def sql(self) -> str:
        """The single statement emitted so far."""
        assert self.calls, "expected the read to emit one statement"
        return self.calls[0][0]

    def params(self) -> dict[str, object]:
        """The parameters of that single statement."""
        assert self.calls, "expected the read to emit one statement"
        return self.calls[0][1]


class FakeInspector:
    """Stands in for ``sqlalchemy.inspect(engine)``."""

    def __init__(self, *, has_table: bool = True) -> None:
        """Decide whether the warehouse reports the ods table."""
        self.has_table_flag = has_table
        self.checked: list[str] = []

    def has_table(self, name: str, **_: object) -> bool:
        """Record the probe and answer from the flag."""
        self.checked.append(name)
        return self.has_table_flag


def _mapping(
    fields: dict[str, FieldMapping], key: tuple[str, ...] = ("symbol", "trade_date")
) -> DomainMapping:
    """Build a real ``DomainMapping`` with the given field spelling."""
    return DomainMapping(
        domain="stock_daily",
        key=key,
        fields=fields,
        adjust="unadjusted",
        suspension="absent_row",
        denominator="key_union",
    )


def _ths_mapping() -> DomainMapping:
    """The mapping the happy path expects: ``thscode`` plus ``trade_date``."""
    return _mapping(
        {
            "symbol": FieldMapping("thscode", normalize="plain"),
            "trade_date": FieldMapping("trade_date"),
            "close": FieldMapping("close_price"),
        }
    )


@pytest.fixture
def engine_with_table(monkeypatch) -> RecordingEngine:
    """A recording engine whose table always exists."""
    engine = RecordingEngine()
    monkeypatch.setattr(ods_watermark, "inspect", lambda _: FakeInspector())
    monkeypatch.setattr(
        ods_watermark, "require_domain_mapping", lambda source, domain: _ths_mapping()
    )
    return engine


def _read(engine: RecordingEngine, symbols: Sequence[str], **kwargs: object) -> dict[str, date]:
    """Call the module under test with the fixed end date."""
    return read_ods_watermarks(engine, "stock_daily", "ths", symbols, end=END, **kwargs)


def test_non_positive_batch_size_fails_before_touching_the_warehouse(
    engine_with_table: RecordingEngine,
) -> None:
    with pytest.raises(ValueError, match="batch_size must be positive, got 0"):
        _read(engine_with_table, ["600519"], batch_size=0)

    assert engine_with_table.calls == []


def test_empty_symbol_request_reads_nothing_without_resolving_anything(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    def fail(*_: object) -> object:
        raise AssertionError("the mapping must not be resolved for an empty request")

    monkeypatch.setattr(ods_watermark, "require_domain_mapping", fail)

    assert _read(engine_with_table, []) == {}
    assert engine_with_table.calls == []


def test_mapping_without_a_symbol_field_fails_closed(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    monkeypatch.setattr(
        ods_watermark,
        "require_domain_mapping",
        lambda source, domain: _mapping({"trade_date": FieldMapping("trade_date")}),
    )

    with pytest.raises(
        LookupError, match=r"mapping 'ths'/'stock_daily' has no symbol field"
    ) as raised:
        _read(engine_with_table, ["600519"])

    assert str(raised.value) == "mapping 'ths'/'stock_daily' has no symbol field"
    assert engine_with_table.calls == []


def test_mapping_without_the_contract_date_field_fails_closed(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    monkeypatch.setattr(
        ods_watermark,
        "require_domain_mapping",
        lambda source, domain: _mapping({"symbol": FieldMapping("thscode", normalize="plain")}),
    )

    with pytest.raises(LookupError, match=r"has no 'trade_date' date field"):
        _read(engine_with_table, ["600519"])

    assert engine_with_table.calls == []


def test_mapped_identifier_that_cannot_be_quoted_is_refused(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    monkeypatch.setattr(
        ods_watermark,
        "require_domain_mapping",
        lambda source, domain: _mapping(
            {
                "symbol": FieldMapping("thscode; DROP TABLE x", normalize="plain"),
                "trade_date": FieldMapping("trade_date"),
            }
        ),
    )

    with pytest.raises(ValueError, match="mapped ODS identifier is invalid"):
        _read(engine_with_table, ["600519"])

    assert engine_with_table.calls == []


def test_mapping_whose_key_drops_the_symbol_fails_closed(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    mapping = _mapping(
        {
            "symbol": FieldMapping("thscode", normalize="plain"),
            "trade_date": FieldMapping("trade_date"),
        },
        key=("trade_date",),
    )
    monkeypatch.setattr(ods_watermark, "require_domain_mapping", lambda source, domain: mapping)

    with pytest.raises(LookupError, match=r"has no symbol key"):
        _read(engine_with_table, ["600519"])

    assert engine_with_table.calls == []


def test_missing_ods_table_short_circuits_before_any_statement(
    engine_with_table: RecordingEngine, monkeypatch
) -> None:
    inspector = FakeInspector(has_table=False)
    monkeypatch.setattr(ods_watermark, "inspect", lambda _: inspector)

    assert _read(engine_with_table, ["600519"]) == {}
    assert inspector.checked == ["ods_stock_daily_ths"]
    assert engine_with_table.calls == []


def test_select_binds_each_symbol_as_exact_and_escaped_prefix(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [("600519.SH", date(2026, 9, 20))]

    watermarks = _read(engine_with_table, ["600519", "600519%"])

    sql = engine_with_table.sql()
    assert "SELECT `thscode`, MAX(`trade_date`) AS `latest_date`" in sql
    assert "FROM `ods_stock_daily_ths` WHERE `trade_date` <= :end_date" in sql
    assert "(`thscode` = :symbol_0 OR `thscode` LIKE :symbol_prefix_0 ESCAPE '!')" in sql
    assert sql.count("GROUP BY `thscode`") == 1
    assert engine_with_table.params() == {
        "end_date": END,
        "symbol_0": "600519",
        "symbol_prefix_0": "600519.%",
        "symbol_1": "600519%",
        # ``%`` is the LIKE wildcard, so it travels escaped in the prefix.
        "symbol_prefix_1": "600519!%.%",
    }
    assert watermarks == {"600519": date(2026, 9, 20)}


def test_symbols_are_read_in_batches_of_the_requested_size(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [("600519.SH", date(2026, 9, 22))]

    _read(engine_with_table, ["600519", "000001", "300001"], batch_size=2)

    assert len(engine_with_table.calls) == 2
    first_sql, first_params = engine_with_table.calls[0]
    second_sql, second_params = engine_with_table.calls[1]
    assert ":symbol_1" in first_sql and ":symbol_1" not in second_sql
    assert sorted(first_params) == [
        "end_date",
        "symbol_0",
        "symbol_1",
        "symbol_prefix_0",
        "symbol_prefix_1",
    ]
    assert sorted(second_params) == ["end_date", "symbol_0", "symbol_prefix_0"]


def test_rows_with_a_null_symbol_or_date_are_dropped(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [
        (None, date(2026, 9, 23)),
        ("000001.SZ", None),
        ("600519.SH", date(2026, 9, 23)),
    ]

    assert _read(engine_with_table, ["600519", "000001"]) == {"600519": date(2026, 9, 23)}


def test_rows_outside_the_requested_universe_or_of_a_foreign_type_are_ignored(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [
        # A landed symbol nobody asked for: the caller must not see it.
        ("000002.SZ", date(2026, 9, 23)),
        # A driver that hands back a non-string spelling cannot be matched.
        (7, date(2026, 9, 23)),
        ("600519.SH", date(2026, 9, 21)),
    ]

    assert _read(engine_with_table, ["600519"]) == {"600519": date(2026, 9, 21)}


def test_watermarks_never_move_backwards_or_past_the_requested_end(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [
        ("600519.SH", date(2026, 9, 10)),
        # A stale second row for the same symbol must not win.
        ("600519.SH", date(2026, 9, 4)),
        # A row after ``end`` is not a watermark for this run.
        ("000001.SZ", date(2026, 9, 30)),
    ]

    assert _read(engine_with_table, ["600519", "000001"]) == {"600519": date(2026, 9, 10)}


def test_driver_date_scalars_are_normalized_to_dates(
    engine_with_table: RecordingEngine,
) -> None:
    engine_with_table.rows = [
        ("600519.SH", datetime(2026, 9, 24, 15, 30)),
        ("000001.SZ", date(2026, 9, 23)),
        ("000002.SZ", "20260922"),
        ("300001.SZ", "2026-09-21 00:00:00"),
    ]

    assert _read(engine_with_table, ["600519", "000001", "000002", "300001"]) == {
        "600519": date(2026, 9, 24),
        "000001": date(2026, 9, 23),
        "000002": date(2026, 9, 22),
        "300001": date(2026, 9, 21),
    }


def test_negative_lookback_days_is_rejected() -> None:
    base = Window(start=date(2026, 9, 22), end=END)

    with pytest.raises(ValueError, match="lookback_days must be >= 0, got -1"):
        effective_symbol_windows(["600519"], base, {}, lookback_days=-1)


def test_union_symbol_windows_spans_every_source_leg() -> None:
    windows = union_symbol_windows(
        ["600519", "000001", "300001", "300001"],
        {
            "akshare": {
                "600519": Window(start=date(2026, 9, 2), end=date(2026, 9, 20)),
                "000001": None,
                "300001": None,
            },
            "ths": {
                "600519": Window(start=date(2026, 9, 10), end=date(2026, 9, 24)),
                "000001": Window(start=date(2026, 9, 18), end=date(2026, 9, 19)),
            },
        },
        fallback=Window(start=date(2026, 1, 1), end=END),
    )

    assert windows == {
        "600519": Window(start=date(2026, 9, 2), end=date(2026, 9, 24)),
        "000001": Window(start=date(2026, 9, 18), end=date(2026, 9, 19)),
        # Landed on no source: there is nothing to compare for this symbol.
        "300001": None,
    }


def test_enclosing_window_takes_the_outer_bounds_or_the_fallback() -> None:
    fallback = Window(start=date(2026, 9, 1), end=END)

    assert enclosing_window({"600519": None, "000001": None}, fallback=fallback) == fallback
    assert enclosing_window(
        {
            "600519": Window(start=date(2026, 9, 12), end=date(2026, 9, 20)),
            "000001": Window(start=date(2026, 9, 5), end=date(2026, 9, 23)),
            "300001": None,
        },
        fallback=fallback,
    ) == Window(start=date(2026, 9, 5), end=date(2026, 9, 23))


def test_type_checking_only_imports_resolve() -> None:
    """Execute the annotation-only block with the flag forced true.

    ``typing.TYPE_CHECKING`` is ``False`` at runtime, so the guarded
    imports never run; re-executing the source with the flag on proves
    every name the module's annotations reference is importable.
    """
    namespace = _exec_with_type_checking(ods_watermark)

    assert namespace["Mapping"] is Mapping
    assert namespace["Sequence"] is Sequence
    assert namespace["Engine"] is sqlalchemy.engine.Engine


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
