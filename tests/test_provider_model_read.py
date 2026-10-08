"""Offline native DWD reader tests using a recording MySQL boundary."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest
from sqlalchemy.dialects import mysql

from opendata.data import domains
from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.pipeline import ddl, provider_model_codec, provider_model_schema
from opendata.services import provider_model_store
from opendata.services.provider_model_read import (
    ProviderModelReadError,
    read_provider_model_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_STAMP = datetime(2026, 10, 8, 2, 30)


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fred", registry)
    register_provider("bls", registry)
    return registry


def _fmp_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fmp", registry)
    return registry


def _fred_row(
    *,
    value: float | None = 3.1,
    requested_frequency: str | None = None,
    date_value: date = date(2024, 2, 1),
    realtime_start: date = date(2024, 1, 1),
) -> SeriesObservation:
    return SeriesObservation(
        series_id="CPIAUCSL",
        date=date_value,
        value=value,
        realtime_start=realtime_start,
        realtime_end=date(2024, 12, 31),
        transform_units="lin",
        output_type=1,
        requested_frequency=requested_frequency,  # type: ignore[arg-type]
        requested_aggregation_method="avg",
    )


def _bls_row(
    *,
    series_id: str = "LNS14000000",
    period: str = "M13",
    year: int = 2025,
    latest: bool | None = None,
    preliminary: bool = True,
) -> BlsObservation:
    return BlsObservation(
        series_id=series_id,
        year=year,
        period=period,
        period_name="Annual average" if period == "M13" else period,
        value=4.1,
        footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
        latest=latest,
        preliminary=preliminary,
        api_version="v2",
    )


def _fmp_row(
    *,
    symbol: str = "AAPL",
    date_value: date = date(2024, 1, 2),
    volume: int | float | None = 2**53 + 1,
) -> EquityHistorical:
    return EquityHistorical(
        symbol=symbol,
        date=date_value,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=volume,
        change=0.5,
        change_percent=0.5,
        vwap=100.25,
        currency=None,
        currency_semantics="source_unverified",
        volume_unit=None,
        volume_unit_semantics="source_unverified",
        query_window_scope="explicit",
        window_boundary_semantics="source_unverified",
        provider_default_window_semantics="source_unverified",
        close_adjustment_semantics="split_adjusted_per_source_faq",
        adj_close_provided=False,
    )


def _grant(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    decision: GrantDecision = GrantDecision.ALLOWED,
    conditions: tuple[str, ...] = (),
    expires_at: datetime | None = None,
) -> RequestGrant:
    host = {
        "fred": "api.stlouisfed.org",
        "bls": "api.bls.gov",
        "fmp": "financialmodelingprep.com",
    }.get(source, "example.test")
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="offline-test-query-rights-record",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=(host,),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=1),
        conditions=conditions,
    )


def _context(
    source: str = "fred",
    model: str = "FredSeries",
    *,
    grants: Iterable[RequestGrant] | None = None,
    operation: RequestOperation = RequestOperation.QUERY,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
    timeout: float | None = None,
) -> FetchContext:
    effective_grants = tuple(grants) if grants is not None else (_grant(source, model),)
    budget = RequestBudget(task_attempts=3, source_attempts=3, grants=effective_grants)
    return FetchContext(
        timeout=timeout,
        _deadline_monotonic=deadline,
        _thread_cancel_event=cancel,
        request_budget=budget,
        operation=operation,
    )


def _table_columns(domain: str) -> tuple[ddl.Column, ...]:
    return (
        *provider_model_schema.model_storage_columns(domain),
        *ddl.DWD_TRACE_COLUMNS[:-1],
        ddl.Column("_as_of", "date", nullable=True),
    )


def _primary_index_rows(domain: str) -> list[dict[str, object]]:
    return [
        {
            "INDEX_NAME": "PRIMARY",
            "NON_UNIQUE": 0,
            "SEQ_IN_INDEX": position,
            "COLUMN_NAME": column,
            "SUB_PART": None,
            "EXPRESSION": None,
            "INDEX_TYPE": "BTREE",
        }
        for position, column in enumerate(provider_model_codec.physical_model_key(domain), start=1)
    ]


def _mysql_type(sql_type: str) -> Any:
    match = re.match(r"^varchar\((\d+)\)", sql_type, re.IGNORECASE)
    if match is not None:
        width = int(match.group(1))
        charset_match = re.search(r"CHARACTER\s+SET\s+(\w+)", sql_type, re.IGNORECASE)
        collation_match = re.search(r"COLLATE\s+(\w+)", sql_type, re.IGNORECASE)
        kwargs: dict[str, object] = {}
        if charset_match is not None:
            kwargs["charset"] = charset_match.group(1)
        if collation_match is not None:
            kwargs["collation"] = collation_match.group(1)
        return mysql.VARCHAR(width, **kwargs)
    normalized = sql_type.lower()
    if normalized == "date":
        return mysql.DATE()
    if normalized == "datetime":
        return mysql.DATETIME()
    if normalized == "double":
        return mysql.DOUBLE()
    if normalized == "bigint":
        return mysql.BIGINT()
    if normalized == "json":
        return mysql.JSON()
    if normalized == "tinyint(1)":
        return mysql.TINYINT(1)
    raise AssertionError(f"unexpected reviewed test type: {sql_type}")


class RecordingInspector:
    def __init__(self, domain: str) -> None:
        self.columns = [
            {
                "name": column.name,
                "type": _mysql_type(column.sql_type),
                "nullable": column.nullable,
                "default": column.sql_default,
            }
            for column in _table_columns(domain)
        ]
        self.index_rows = _primary_index_rows(domain)
        self.primary_key: dict[str, object] = {
            "name": "PRIMARY",
            "constrained_columns": list(provider_model_codec.physical_model_key(domain)),
        }
        self.unique_constraints: list[dict[str, object]] = []
        self.indexes: list[dict[str, object]] = []
        self.table_exists = True

    def has_table(self, _table: str) -> bool:
        return self.table_exists

    def get_columns(self, _table: str) -> list[dict[str, object]]:
        return self.columns

    def get_pk_constraint(self, _table: str) -> dict[str, object]:
        return self.primary_key

    def get_unique_constraints(self, _table: str) -> list[dict[str, object]]:
        return self.unique_constraints

    def get_indexes(self, _table: str) -> list[dict[str, object]]:
        return self.indexes


class RecordingResult:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]

    def mappings(self) -> RecordingResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        return self.rows[:size]


class RecordingConnection:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine
        self.dialect = mysql.dialect()

    def execute(self, statement: object, parameters: object) -> RecordingResult:
        sql = str(statement)
        if not self.engine.in_transaction:
            raise AssertionError("metadata and reads must share one read transaction")
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            self.engine.metadata_queries.append((sql, parameters))
            return RecordingResult(self.engine.index_rows)
        if not sql.lstrip().startswith("SELECT "):
            raise AssertionError("reader issued non-SELECT SQL")
        self.engine.selects.append((sql, parameters))
        if self.engine.cancel_after_select is not None:
            self.engine.cancel_after_select.set()
        return RecordingResult(self.engine.rows)


class _BeginTransaction:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine

    def __enter__(self) -> RecordingConnection:
        self.engine.begin_calls += 1
        self.engine.in_transaction = True
        return self.engine.connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> Literal[False]:
        self.engine.in_transaction = False
        if exc_type is None:
            self.engine.commit_calls += 1
        else:
            self.engine.rollback_calls += 1
        return False


class RecordingEngine:
    def __init__(
        self, dialect_name: str = "mysql", rows: Sequence[Mapping[str, object]] = ()
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.connection = RecordingConnection(self)
        self.rows = [dict(row) for row in rows]
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.in_transaction = False
        self.metadata_queries: list[tuple[str, object]] = []
        self.selects: list[tuple[str, object]] = []
        self.cancel_after_select: threading.Event | None = None

    def begin(self) -> _BeginTransaction:
        return _BeginTransaction(self)


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch,
    inspector: RecordingInspector,
) -> list[object]:
    observed: list[object] = []

    def fake_inspect(connection: object) -> RecordingInspector:
        observed.append(connection)
        assert isinstance(connection, RecordingConnection)
        assert connection.engine.in_transaction
        connection.engine.index_rows = [dict(row) for row in inspector.index_rows]
        return inspector

    monkeypatch.setattr(provider_model_store, "inspect", fake_inspect)
    return observed


def _storage_record(
    domain: str,
    row: SeriesObservation | BlsObservation | EquityHistorical,
    *,
    source: str | None = None,
    merged_at: datetime = _STAMP,
    as_of: date | None = None,
) -> dict[str, object]:
    record = provider_model_codec.encode_model_storage_row(domain, row)
    if domain == "bls_series":
        record["latest"] = None if row.latest is None else int(row.latest)
        record["preliminary"] = int(row.preliminary)
    elif domain == "equity_historical":
        record["adj_close_provided"] = 0
    record.update(
        {
            "source": source
            or {
                "fred_series": "fred",
                "bls_series": "bls",
                "equity_historical": "fmp",
            }[domain],
            "_merged_at": merged_at,
            "_diff_flag": 0,
            "_as_of": as_of,
        }
    )
    return record


def _call(
    engine: RecordingEngine,
    registry: ProviderRegistry,
    *,
    source: str = "fred",
    model: str = "FredSeries",
    filters: Mapping[str, object] | None = None,
    start: date | int | None = None,
    end: date | int | None = None,
    limit: int = 1_000,
    offset: int = 0,
    ctx: FetchContext | None = None,
) -> tuple[SeriesObservation | BlsObservation | EquityHistorical, ...]:
    return cast(
        "tuple[SeriesObservation | BlsObservation | EquityHistorical, ...]",
        read_provider_model_rows(
            engine=cast("Any", engine),
            registry=registry,
            source=source,
            model=model,
            filters=filters,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
            ctx=ctx,
        ),
    )


def _assert_no_db_touches(engine: RecordingEngine) -> None:
    assert engine.begin_calls == 0
    assert engine.metadata_queries == []
    assert engine.selects == []


def test_fred_reader_roundtrips_revisions_nullable_context_and_bound_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    records = [
        _storage_record(
            "fred_series",
            _fred_row(value=None, date_value=date(2024, 2, 1)),
        ),
        _storage_record(
            "fred_series",
            _fred_row(
                value=3.2,
                date_value=date(2024, 2, 1),
                realtime_start=date(2024, 3, 1),
            ),
        ),
    ]
    engine = RecordingEngine(rows=records)
    inspector = RecordingInspector("fred_series")
    inspected = _install_inspector(monkeypatch, inspector)
    context = _context()

    rows = _call(
        engine,
        registry,
        filters={
            "series_id": "CPIAUCSL' OR 1=1 --",
            "requested_frequency": None,
        },
        start=date(2024, 2, 1),
        end=date(2024, 2, 28),
        limit=2,
        offset=4,
        ctx=context,
    )

    assert len(rows) == 2
    assert all(type(row) is SeriesObservation for row in rows)
    assert rows[0].value is None and rows[1].value == 3.2
    sql, bound = engine.selects[0]
    assert "`requested_frequency` IS NULL" in sql
    assert "`series_id` = :filter_1" in sql
    assert "CPIAUCSL' OR 1=1 --" not in sql
    assert bound == {
        "limit": 2,
        "offset": 4,
        "filter_1": "CPIAUCSL' OR 1=1 --",
        "start": date(2024, 2, 1),
        "end": date(2024, 2, 28),
    }
    key = provider_model_codec.physical_model_key("fred_series")
    assert f"ORDER BY {', '.join(f'`{name}`' for name in key)} LIMIT :limit OFFSET :offset" in sql
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert len(engine.metadata_queries) == 1
    assert inspected == [engine.connection]
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0


def test_bls_reader_roundtrips_m13_footnotes_and_mysql_tinyints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    records = [
        _storage_record("bls_series", _bls_row(latest=None, preliminary=True)),
        _storage_record(
            "bls_series",
            _bls_row(year=2024, period="M01", latest=False, preliminary=False),
        ),
    ]
    engine = RecordingEngine(rows=records)
    inspected = _install_inspector(monkeypatch, RecordingInspector("bls_series"))
    context = _context("bls", "BlsSeries")

    rows = _call(
        engine,
        registry,
        source="bls",
        model="BlsSeries",
        filters={"period": "M13", "api_version": "v2"},
        start=2024,
        end=2025,
        limit=2,
        ctx=context,
    )

    assert all(type(row) is BlsObservation for row in rows)
    assert rows[0].period == "M13"
    assert rows[0].period_name == "Annual average"
    assert rows[0].footnotes == (BlsFootnote(code="P", text="Preliminary."), BlsFootnote())
    assert rows[0].latest is None and rows[0].preliminary is True
    assert rows[1].latest is False and rows[1].preliminary is False
    sql, bound = engine.selects[0]
    assert "`year` >= :start" in sql and "`year` <= :end" in sql
    assert bound == {
        "limit": 2,
        "offset": 0,
        "filter_0": "v2",
        "filter_1": "M13",
        "start": 2024,
        "end": 2025,
    }
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert inspected == [engine.connection]
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0


@pytest.mark.parametrize(
    "bad_grants",
    [
        (),
        (_grant("fred", "FredSeries", operation=RequestOperation.STORE),),
        (_grant("fred", "FredSeries", operation=RequestOperation.EXPORT),),
        (_grant("fred", "WrongFredModel"),),
        (_grant("fred", "FredSeries", decision=GrantDecision.UNKNOWN),),
        (_grant("fred", "FredSeries", conditions=("approval",)),),
        (
            _grant(
                "fred",
                "FredSeries",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            ),
        ),
    ],
)
def test_unusable_query_grants_precede_registry_filters_and_database(
    monkeypatch: pytest.MonkeyPatch,
    bad_grants: tuple[RequestGrant, ...],
) -> None:
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))

    class UnreadFilters(Mapping[str, object]):
        touches = 0

        def __getitem__(self, key: str) -> object:
            self.touches += 1
            raise KeyError(key)

        def __iter__(self) -> Any:
            self.touches += 1
            raise AssertionError("filters were read before QUERY authorization")

        def __len__(self) -> int:
            self.touches += 1
            raise AssertionError("filters were read before QUERY authorization")

    filters = UnreadFilters()
    monkeypatch.setattr(
        provider_model_store,
        "_registered_domain",
        lambda *_args: pytest.fail("registry was read before QUERY authorization"),
    )
    with pytest.raises(RequestAuthorizationError):
        read_provider_model_rows(
            engine=cast("Any", engine),
            registry=cast("Any", object()),
            source="fred",
            model="FredSeries",
            filters=filters,
            ctx=_context(grants=bad_grants),
        )
    assert filters.touches == 0
    _assert_no_db_touches(engine)


def test_default_wrong_operation_and_wrong_registered_identity_touch_no_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))

    with pytest.raises(RequestAuthorizationError):
        _call(engine, registry, ctx=None)
    _assert_no_db_touches(engine)

    with pytest.raises(ValueError, match="QUERY"):
        _call(engine, registry, ctx=_context(operation=RequestOperation.STORE))
    _assert_no_db_touches(engine)

    wrong_identity = _context(
        grants=(_grant("fred", "NotFredSeries"),),
    )
    with pytest.raises(ProviderModelReadError, match="identity"):
        _call(engine, registry, model="NotFredSeries", ctx=wrong_identity)
    _assert_no_db_touches(engine)


@pytest.mark.parametrize(
    ("call_kwargs", "source", "model"),
    [
        ({"filters": {"_request_context": "[]"}}, "fred", "FredSeries"),
        ({"filters": {"value": 1.0}}, "fred", "FredSeries"),
        ({"filters": {"output_type": True}}, "fred", "FredSeries"),
        ({"filters": {"output_type": 1.0}}, "fred", "FredSeries"),
        ({"filters": {"series_id": 42}}, "fred", "FredSeries"),
        ({"filters": {"series_id": ""}}, "fred", "FredSeries"),
        ({"filters": {"series_id": "   "}}, "fred", "FredSeries"),
        ({"start": datetime(2024, 1, 1)}, "fred", "FredSeries"),
        ({"start": date(2024, 2, 1), "end": date(2024, 1, 1)}, "fred", "FredSeries"),
        ({"filters": {"api_version": "v3"}}, "bls", "BlsSeries"),
        ({"filters": {"year": True}}, "bls", "BlsSeries"),
        ({"filters": {"series_id": "  "}}, "bls", "BlsSeries"),
        ({"filters": {"period": " \t "}}, "bls", "BlsSeries"),
        ({"start": True}, "bls", "BlsSeries"),
        ({"start": 999}, "bls", "BlsSeries"),
        ({"start": 2026, "end": 2025}, "bls", "BlsSeries"),
        ({"limit": True}, "bls", "BlsSeries"),
        ({"limit": 1_001}, "bls", "BlsSeries"),
        ({"offset": True}, "bls", "BlsSeries"),
        ({"offset": -1}, "bls", "BlsSeries"),
    ],
)
def test_invalid_filters_ranges_and_pagination_fail_before_database(
    monkeypatch: pytest.MonkeyPatch,
    call_kwargs: dict[str, object],
    source: str,
    model: str,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(
        monkeypatch,
        RecordingInspector("fred_series" if source == "fred" else "bls_series"),
    )
    with pytest.raises((TypeError, ValueError)):
        _call(
            engine,
            registry,
            source=source,
            model=model,
            ctx=_context(source, model),
            **call_kwargs,  # type: ignore[arg-type]
        )
    _assert_no_db_touches(engine)


def test_invalid_filter_float_and_bad_filter_mapping_fail_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    for filters in ({"value": float("inf")}, [], {1: "CPIAUCSL"}):
        with pytest.raises((TypeError, ValueError)):
            _call(engine, registry, filters=cast("Any", filters), ctx=_context())
        _assert_no_db_touches(engine)


@pytest.mark.parametrize(
    "schema_problem", ["missing_table", "wrong_columns", "prefix_key", "extra_unique"]
)
def test_unreviewed_schema_and_index_fail_before_data_select(
    monkeypatch: pytest.MonkeyPatch,
    schema_problem: str,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("bls_series")
    if schema_problem == "missing_table":
        inspector.table_exists = False
    elif schema_problem == "wrong_columns":
        inspector.columns.pop()
    elif schema_problem == "prefix_key":
        next(row for row in inspector.index_rows if row["COLUMN_NAME"] == "series_id")[
            "SUB_PART"
        ] = 8
    else:
        inspector.index_rows.append(
            {
                "INDEX_NAME": "uq_prefix",
                "NON_UNIQUE": 0,
                "SEQ_IN_INDEX": 1,
                "COLUMN_NAME": "series_id",
                "SUB_PART": 8,
                "EXPRESSION": None,
                "INDEX_TYPE": "BTREE",
            }
        )
    _install_inspector(monkeypatch, inspector)
    with pytest.raises(ProviderModelReadError):
        _call(engine, registry, source="bls", model="BlsSeries", ctx=_context("bls", "BlsSeries"))
    assert engine.begin_calls == 1
    assert engine.commit_calls == 0
    assert engine.rollback_calls == 1
    assert len(engine.metadata_queries) == (
        0 if schema_problem in {"missing_table", "wrong_columns"} else 1
    )
    assert engine.selects == []


def test_bad_last_row_or_wrong_source_fails_whole_page_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    valid = _storage_record("bls_series", _bls_row())
    invalid = dict(valid)
    invalid["footnotes"] = "not-json"
    engine = RecordingEngine(rows=(valid, invalid))
    _install_inspector(monkeypatch, RecordingInspector("bls_series"))
    with pytest.raises(ProviderModelReadError, match="decoding"):
        _call(engine, registry, source="bls", model="BlsSeries", ctx=_context("bls", "BlsSeries"))
    assert engine.selects and engine.rollback_calls == 1 and engine.commit_calls == 0

    wrong_source = dict(valid)
    wrong_source["source"] = "fred"
    engine = RecordingEngine(rows=(valid, wrong_source))
    _install_inspector(monkeypatch, RecordingInspector("bls_series"))
    with pytest.raises(ProviderModelReadError, match="source"):
        _call(engine, registry, source="bls", model="BlsSeries", ctx=_context("bls", "BlsSeries"))
    assert engine.selects and engine.rollback_calls == 1 and engine.commit_calls == 0


def test_cancel_and_deadline_after_select_abort_the_read_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    record = _storage_record("fred_series", _fred_row())
    engine = RecordingEngine(rows=(record,))
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    cancellation = threading.Event()
    engine.cancel_after_select = cancellation
    with pytest.raises(RequestExecutionCancelledError):
        _call(engine, registry, ctx=_context(cancel=cancellation))
    assert engine.selects and engine.rollback_calls == 1 and engine.commit_calls == 0

    engine = RecordingEngine(rows=(record,))
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    with pytest.raises(RequestExecutionDeadlineError):
        _call(engine, registry, ctx=_context(deadline=time.monotonic() - 1))
    _assert_no_db_touches(engine)


def test_admission_timeout_covers_registry_preparation_and_precedes_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    registered_domain = provider_model_store._registered_domain
    delayed = False

    def delayed_registry(*args: object) -> str:
        nonlocal delayed
        delayed = True
        time.sleep(0.05)
        return registered_domain(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(provider_model_store, "_registered_domain", delayed_registry)
    with pytest.raises(RequestExecutionDeadlineError):
        _call(engine, registry, ctx=_context(timeout=0.01))
    assert delayed
    _assert_no_db_touches(engine)


def test_inherited_query_scope_and_stricter_deadline_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    record = _storage_record("fred_series", _fred_row())
    engine = RecordingEngine(rows=(record,))
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(_grant("fred", "FredSeries"),),
    )
    with (
        request_execution_scope(
            source="fred",
            canonical_model="FredSeries",
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=time.monotonic() - 1,
        ),
        pytest.raises(RequestExecutionDeadlineError),
    ):
        _call(engine, registry, ctx=FetchContext(operation=RequestOperation.QUERY))
    _assert_no_db_touches(engine)


def test_non_mysql_is_rejected_without_opening_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine(dialect_name="sqlite")
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    with pytest.raises(ProviderModelReadError, match="MySQL"):
        _call(engine, registry, ctx=_context())
    _assert_no_db_touches(engine)


def test_domain_without_local_query_permission_is_rejected_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    query_removed = domains.require_domain_semantics("fred_series").model_copy(
        update={"permissions": ("store", "export")}
    )
    monkeypatch.setattr(
        domains,
        "require_domain_semantics",
        lambda _domain: query_removed,
    )

    with pytest.raises(ProviderModelReadError, match="QUERY permission"):
        _call(engine, registry, ctx=_context())
    _assert_no_db_touches(engine)


def test_fmp_reader_uses_exact_symbol_and_inclusive_date_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _fmp_registry()
    row = _fmp_row(symbol=" AAPL ", date_value=date(2024, 1, 2))
    engine = RecordingEngine(rows=(_storage_record("equity_historical", row),))
    inspected = _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
    context = _context(
        "fmp",
        "EquityHistorical",
        operation=RequestOperation.QUERY,
    )

    rows = _call(
        engine,
        registry,
        source="fmp",
        model="EquityHistorical",
        filters={
            "symbol": " AAPL ",
            "query_window_scope": "explicit",
            "close_adjustment_semantics": "split_adjusted_per_source_faq",
        },
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        limit=1,
        ctx=context,
    )

    assert len(rows) == 1 and type(rows[0]) is EquityHistorical
    assert rows[0].symbol == " AAPL "
    assert rows[0].date == date(2024, 1, 2)
    sql, bound = engine.selects[0]
    assert "`date` >= :start" in sql and "`date` <= :end" in sql
    assert "ORDER BY `symbol`, `date` LIMIT :limit OFFSET :offset" in sql
    assert bound == {
        "limit": 1,
        "offset": 0,
        "filter_0": "split_adjusted_per_source_faq",
        "filter_1": "explicit",
        "filter_2": " AAPL ",
        "start": date(2024, 1, 1),
        "end": date(2024, 1, 31),
    }
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert inspected == [engine.connection]
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0


@pytest.mark.parametrize(
    ("call_kwargs", "source", "model"),
    [
        ({"filters": {"symbol": ""}}, "fmp", "EquityHistorical"),
        ({"filters": {"symbol": "  "}}, "fmp", "EquityHistorical"),
        ({"filters": {"symbol": "AAPL\x00"}}, "fmp", "EquityHistorical"),
        ({"filters": {"symbol": "AAPL\x7f"}}, "fmp", "EquityHistorical"),
        ({"filters": {"query_window_scope": "unknown"}}, "fmp", "EquityHistorical"),
        (
            {"filters": {"close_adjustment_semantics": "unadjusted"}},
            "fmp",
            "EquityHistorical",
        ),
        ({"filters": {"date": datetime(2024, 1, 1)}}, "fmp", "EquityHistorical"),
        ({"start": datetime(2024, 1, 1)}, "fmp", "EquityHistorical"),
    ],
)
def test_invalid_fmp_symbol_date_and_metadata_filters_touch_no_database(
    monkeypatch: pytest.MonkeyPatch,
    call_kwargs: dict[str, object],
    source: str,
    model: str,
) -> None:
    registry = _fmp_registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
    with pytest.raises((TypeError, ValueError)):
        _call(
            engine,
            registry,
            source=source,
            model=model,
            ctx=_context(source, model, operation=RequestOperation.QUERY),
            **call_kwargs,  # type: ignore[arg-type]
        )
    _assert_no_db_touches(engine)


def test_fmp_quote_model_is_not_enabled_for_native_dwd_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _fmp_registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
    with pytest.raises(ProviderModelReadError, match="identity"):
        _call(
            engine,
            registry,
            source="fmp",
            model="EquityQuote",
            ctx=_context("fmp", "EquityQuote", operation=RequestOperation.QUERY),
        )
    _assert_no_db_touches(engine)
