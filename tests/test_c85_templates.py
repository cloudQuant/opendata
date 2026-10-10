"""P0 template rendering: schedule parsing, source lookup and ods key reads.

The warehouse seams here are the ``engine.connect()`` inside
:func:`opendata.pipeline.templates._load_ods_rows`, so a recording fake
engine shows exactly which scoped statement the templates would run.
No connection, no DDL.
"""

import ast
import importlib
import pathlib
from collections.abc import Awaitable, Sequence
from datetime import date
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy.ext.asyncio import async_sessionmaker

from opendata.data.mapping import DomainMapping, FieldMapping, load_mapping, mapping_sources
from opendata.pipeline import templates
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


def test_type_checking_only_imports_resolve() -> None:
    """Every name behind ``if TYPE_CHECKING:`` resolves where it points.

    ``templates`` hides its annotation imports behind the flag, and two of
    the names they bind - ``alerts.Notifier`` and ``runner.Hook`` - are
    typing aliases with no runtime value; ``from __future__ import
    annotations`` is the only thing that makes the module importable anyway.

    The probe this test began as re-executed the source with
    ``typing.TYPE_CHECKING`` forced true. That passed only while
    ``opendata.pipeline.alerts`` had not been imported yet: the exec then ran
    *that module's* guarded block too, and ``Notifier`` materialized by
    accident. Once another test module had imported it the normal way (
    ``tests/test_cross_check_service.py`` and ``tests/test_diff_alerts.py`` do
    so at import, as does every xdist worker's collection pass), the cached
    module carried no ``Notifier`` and line 49 raised ImportError. The verdict
    was a function of test order, not of the code under test.

    Resolution therefore goes through the real import machinery: a guarded
    name must be an attribute of the module it is imported from, or be bound
    inside that module's own ``if TYPE_CHECKING:`` block - exactly what a type
    checker sees, and independent of what is cached. Then the hazard this test
    is really a guard against gets checked head-on: a name behind the flag
    must never be evaluated when the module loads.
    """
    source = _module_source(templates)
    assert "from __future__ import annotations" in source, (
        "templates.py only gets away with annotation-only imports because "
        "PEP 563 keeps those annotations unevaluated at runtime"
    )

    guarded = _type_checking_imports(templates)
    assert len(guarded) >= 12, f"the guarded block shrank, so this probe is stale: {guarded}"

    resolved: dict[str, object] = {}
    typing_only: set[str] = set()
    for target, name, bound_as in guarded:
        value, is_alias = _resolve_guarded_name(target, name)
        resolved[bound_as] = value
        if is_alias:
            typing_only.add(bound_as)

    assert resolved["Sequence"] is Sequence
    assert resolved["Awaitable"] is Awaitable
    assert resolved["Engine"] is sqlalchemy.engine.Engine
    assert resolved["AsyncSession"].__name__ == "AsyncSession"
    assert resolved["async_sessionmaker"] is async_sessionmaker
    assert resolved["CrossCheckService"].__name__ == "CrossCheckService"
    assert resolved["DwdMergeService"].__name__ == "DwdMergeService"
    assert resolved["PipelineContext"] is PipelineContext
    assert resolved["CalendarView"] is CalendarView

    # Spelled out rather than papered over: these two have no runtime value,
    # so templates.py must never need them for anything but an annotation.
    assert sorted(typing_only) == ["Hook", "Notifier"]
    assert resolved["Notifier"] is None and resolved["Hook"] is None

    eager = _names_evaluated_at_runtime(templates, set(resolved))
    assert not eager, (
        f"{sorted(eager)} is referenced outside a lazy annotation, so it has "
        "to be a runtime import - templates.py would NameError on load"
    )


def _module_source(module: object) -> str:
    """The source ``module`` was compiled from."""
    return pathlib.Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]


def _is_type_checking(node: ast.expr) -> bool:
    """``TYPE_CHECKING`` / ``typing.TYPE_CHECKING``, however it is spelled."""
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"
    )


def _guarded_imports(tree: ast.AST, package: str) -> list[tuple[str, str, str]]:
    """``(target module, imported name, bound as)`` for every import behind the flag.

    An empty imported name means the bare ``import a.b`` form, where the
    binding *is* the module.
    """
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If) and _is_type_checking(node.test)):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.ImportFrom):
                target = stmt.module or ""
                if stmt.level:
                    target = f"{package}.{target}" if target else package
                if not target:
                    continue
                found.extend(
                    (target, alias.name, alias.asname or alias.name) for alias in stmt.names
                )
            elif isinstance(stmt, ast.Import):
                found.extend(
                    (alias.name, "", alias.asname or alias.name.split(".")[0])
                    for alias in stmt.names
                )
    return found


def _type_checking_imports(module: object) -> list[tuple[str, str, str]]:
    package = getattr(module, "__package__", "") or ""
    return _guarded_imports(ast.parse(_module_source(module)), package)


def _typing_alias_site(module: object, name: str) -> str | None:
    """``file:line`` where ``module`` binds ``name`` under its own flag block."""
    path = pathlib.Path(module.__file__)  # type: ignore[attr-defined]
    for _target, bound, stmt in _guarded_bindings(_module_source(module)):
        if bound == name:
            return f"{path}:{stmt.lineno}"
    return None


def _guarded_bindings(source: str) -> list[tuple[str, str, ast.stmt]]:
    """Every name the flag block of ``source`` binds, imported or assigned."""
    out: list[tuple[str, str, ast.stmt]] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.If) and _is_type_checking(node.test)):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                module = getattr(stmt, "module", None) or ""
                out.extend((module, alias.asname or alias.name, stmt) for alias in stmt.names)
            elif isinstance(stmt, ast.Assign):
                out.extend(
                    ("", target.id, stmt) for target in stmt.targets if isinstance(target, ast.Name)
                )
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(stmt.target, ast.Name):
                    out.append(("", stmt.target.id, stmt))
            elif isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(("", stmt.name, stmt))
    return out


def _resolve_guarded_name(target: str, name: str) -> tuple[object | None, bool]:
    """Look ``name`` up in ``target`` the way the interpreter and mypy both do.

    Returns ``(value, is_typing_only_alias)``. The target has to import; the
    name has to be either a runtime attribute or a binding the target makes
    inside its own ``if TYPE_CHECKING:`` block. Anything else is a name
    templates.py annotates with but nobody declares.
    """
    module = importlib.import_module(target)
    if not name:
        return module, False
    try:
        return getattr(module, name), False
    except AttributeError:
        pass
    site = _typing_alias_site(module, name)
    if site is None:
        raise AssertionError(
            f"{target} binds no {name!r} at runtime and no TYPE_CHECKING {name!r} "
            f"either ({pathlib.Path(module.__file__)} is where it would live)"  # type: ignore[attr-defined]
        )
    return None, True


def _lazy_annotation_ids(tree: ast.AST) -> set[int]:
    """Node ids PEP 563 leaves as strings, so the interpreter never evaluates them.

    Class-body annotations count as eager unless the class is a ``dataclass``
    (which stores them) - a pydantic or attrs model would resolve them at
    class creation and need the real object.
    """
    eager_fields = {
        id(stmt)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and not _is_dataclass(node)
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign)
    }
    lazy: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and id(node) in eager_fields:
            continue
        if not isinstance(node, (ast.arg, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for field in ("annotation", "returns"):
            child = getattr(node, field, None)
            if child is not None:
                lazy.update(id(sub) for sub in ast.walk(child))
    return lazy


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        call = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(call, ast.Name) and call.id == "dataclass":
            return True
        if isinstance(call, ast.Attribute) and call.attr == "dataclass":
            return True
    return False


def _names_evaluated_at_runtime(module: object, names: set[str]) -> set[str]:
    """Which of ``names`` the module reads where the interpreter must resolve it.

    A read is legal when an enclosing scope binds the name at runtime - the
    deferred ``import`` inside the function is how templates.py reaches its
    services. It is a NameError when the only binding is the flag block,
    because that block never runs.
    """
    tree = ast.parse(_module_source(module))
    _link_parents(tree)
    lazy = _lazy_annotation_ids(tree)
    bound = _runtime_bindings(tree)
    eager: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Name) or node.id not in names or id(node) in lazy:
            continue
        if _inside_flag_block(node) or _binds(node, bound):
            continue
        eager.add(node.id)
    return eager


def _link_parents(tree: ast.AST) -> None:
    """Give every node a ``_parent`` so scope chains are walkable."""
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # type: ignore[attr-defined]


def _scope_chain(node: ast.AST) -> list[ast.AST]:
    """``node``'s enclosing scopes, innermost first, up to the module.

    A class body sees its own scope and the module; a method does not see the
    class scope, so class scopes are only kept when they bound ``node``
    directly.
    """
    chain: list[ast.AST] = []
    current = getattr(node, "_parent", None)
    direct = True
    while current is not None:
        is_scope = isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.Module, ast.ClassDef)
        )
        if is_scope and (direct or not isinstance(current, ast.ClassDef)):
            chain.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            direct = False
        current = getattr(current, "_parent", None)
    return chain


def _binds(node: ast.Name, bound: dict[int, set[str]]) -> bool:
    return any(node.id in bound.get(id(scope), ()) for scope in _scope_chain(node))


def _inside_flag_block(node: ast.AST) -> bool:
    current = getattr(node, "_parent", None)
    while current is not None:
        if isinstance(current, ast.If) and _is_type_checking(current.test):
            return True
        current = getattr(current, "_parent", None)
    return False


def _runtime_bindings(tree: ast.AST) -> dict[int, set[str]]:
    """Names each scope binds when the module actually executes, by scope id."""
    bound: dict[int, set[str]] = {}

    def record(scope_key: int, name: str) -> None:
        bound.setdefault(scope_key, set()).add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if _inside_flag_block(node):
                continue
            scope = _scope_chain(node)
            if scope:
                record(id(scope[0]), node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if _inside_flag_block(node):
                continue
            scope = _scope_chain(node)
            key = id(scope[0]) if scope else id(tree)
            for alias in node.names:
                record(key, alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _inside_flag_block(node):
                continue
            scope = _scope_chain(node)
            if scope:
                record(id(scope[0]), node.name)
    return bound
