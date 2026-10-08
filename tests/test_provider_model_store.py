"""Offline native DWD writer tests with a recording MySQL boundary."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence as RuntimeSequence
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast

import pymysql
import pytest
from pymysql.connections import MySQLResult
from pymysql.constants import FIELD_TYPE
from pymysql.converters import through
from sqlalchemy import text
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql.pymysql import MySQLDialect_pymysql

from opendata.data import domains
from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.pipeline import ddl, provider_model_codec, provider_model_schema
from opendata.services import provider_model_store
from opendata.services.provider_model_read import read_provider_model_rows
from opendata.services.provider_model_store import (
    ProviderModelStoreError,
    write_provider_model_rows,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_STAMP = datetime(2026, 10, 8, 2, 30, tzinfo=timezone(timedelta(hours=8)))


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fred", registry)
    register_provider("bls", registry)
    return registry


def _fmp_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fmp", registry)
    return registry


def _fred_row(*, value: float | None = 3.1) -> SeriesObservation:
    return SeriesObservation(
        series_id="CPIAUCSL",
        date=date(2024, 2, 1),
        value=value,
        realtime_start=date(2024, 1, 1),
        realtime_end=date(2024, 12, 31),
        transform_units="lin",
        output_type=1,
        requested_frequency=None,
        requested_aggregation_method="avg",
    )


def _bls_row(
    *,
    series_id: str = "LNS14000000",
    period: str = "M13",
    year: int = 2025,
) -> BlsObservation:
    return BlsObservation(
        series_id=series_id,
        year=year,
        period=period,
        period_name="Annual average" if period == "M13" else period,
        value=4.1,
        footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
        latest=None,
        preliminary=True,
        api_version="v2",
    )


def _fmp_row(
    *,
    symbol: str = "BRK.B",
    date_value: date = date(2024, 1, 2),
    volume: int | float | None = 2**53 + 1,
    close: float = 100.5,
) -> EquityHistorical:
    return EquityHistorical(
        symbol=symbol,
        date=date_value,
        open=100.0,
        high=101.0,
        low=99.0,
        close=close,
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
    operation: RequestOperation = RequestOperation.STORE,
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
        rights_evidence="offline-test-rights-record",
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
    grants: Sequence[RequestGrant] | None = None,
    operation: RequestOperation = RequestOperation.STORE,
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
        self.domain = domain
        self.index_rows = _primary_index_rows(domain)
        self.columns = [
            {
                "name": column.name,
                "type": _mysql_type(column.sql_type),
                "nullable": column.nullable,
                "default": column.sql_default,
            }
            for column in _table_columns(domain)
        ]
        self.primary_key: dict[str, object] = {
            "name": "PRIMARY",
            "constrained_columns": list(provider_model_codec.physical_model_key(domain)),
        }
        self.unique_constraints: list[dict[str, object]] = []
        self.indexes: list[dict[str, object]] = []
        self.seen_connection: object | None = None

    def has_table(self, _table: str) -> bool:
        return True

    def get_columns(self, _table: str) -> list[dict[str, object]]:
        return self.columns

    def get_pk_constraint(self, _table: str) -> dict[str, object]:
        return self.primary_key

    def get_unique_constraints(self, _table: str) -> list[dict[str, object]]:
        return self.unique_constraints

    def get_indexes(self, _table: str) -> list[dict[str, object]]:
        return self.indexes


class RecordingResult:
    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
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

    def execute(self, statement: object, parameters: object) -> object:
        sql = str(statement)
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            if not self.engine.in_transaction:
                raise AssertionError("index statistics must share the write transaction")
            self.engine.metadata_queries.append((sql, parameters))
            if self.engine.fail_metadata:
                raise RuntimeError("recording metadata failure")
            return RecordingResult(self.engine.index_rows)
        if sql.lstrip().startswith("SELECT "):
            self.engine.selects.append((sql, parameters))
            return RecordingResult(self.engine.rows)
        if self.engine.fail_execute:
            raise RuntimeError("recording execute failure")
        self.engine.executions.append((sql, parameters))
        if self.engine.cancel_after_execute is not None:
            self.engine.cancel_after_execute.set()
        return None


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
        self,
        dialect_name: str = "mysql",
        rows: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.connection = RecordingConnection(self)
        self.rows = [dict(row) for row in rows]
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.in_transaction = False
        self.index_rows: list[dict[str, object]] = []
        self.metadata_queries: list[tuple[str, object]] = []
        self.executions: list[tuple[str, object]] = []
        self.selects: list[tuple[str, object]] = []
        self.fail_execute = False
        self.fail_metadata = False
        self.cancel_after_execute: threading.Event | None = None

    def begin(self) -> _BeginTransaction:
        return _BeginTransaction(self)


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch,
    inspector: RecordingInspector,
) -> list[object]:
    observed: list[object] = []
    inspector_connection = inspector

    def capture_index_rows(connection: object) -> None:
        assert isinstance(connection, RecordingConnection)
        connection.engine.index_rows = [dict(row) for row in inspector_connection.index_rows]

    def fake_inspect(connection: object) -> RecordingInspector:
        observed.append(connection)
        inspector.seen_connection = connection
        assert isinstance(connection, RecordingConnection)
        assert connection.engine.in_transaction
        capture_index_rows(connection)
        return inspector

    monkeypatch.setattr(provider_model_store, "inspect", fake_inspect)
    return observed


def _call(
    engine: RecordingEngine,
    registry: ProviderRegistry,
    *,
    source: str = "fred",
    model: str = "FredSeries",
    rows: Sequence[SeriesObservation | BlsObservation | EquityHistorical] | None = None,
    ctx: FetchContext | None = None,
    observed_at: datetime = _STAMP,
) -> int:
    return write_provider_model_rows(
        engine=cast("Any", engine),
        registry=registry,
        source=source,
        model=model,
        rows=rows if rows is not None else (_fred_row(),),
        observed_at=observed_at,
        ctx=ctx,
    )


def _assert_no_db_touches(engine: RecordingEngine, inspector_calls: Sequence[object]) -> None:
    assert engine.begin_calls == 0
    assert engine.metadata_queries == []
    assert engine.executions == []
    assert inspector_calls == []


def _update_clause(sql: str) -> str:
    return sql.split("ON DUPLICATE KEY UPDATE ", maxsplit=1)[1]


class _DriverRowPacket:
    def __init__(self, values: Sequence[bytes | None]) -> None:
        self.values = iter(values)

    def read_length_coded_string(self) -> bytes | None:
        return next(self.values)


def _pymysql_driver_shape(
    record: dict[str, object],
    connection: pymysql.connections.Connection,
) -> dict[str, object]:
    raw_json: bytes | None = None
    raw_volume = record["volume"]
    if raw_volume is not None:
        parsed = json.loads(cast("str", raw_volume))
        mysql_json_text = json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))
        raw_json = mysql_json_text.encode(connection.encoding)

    field_types = (FIELD_TYPE.JSON, FIELD_TYPE.TINY, FIELD_TYPE.VARCHAR, FIELD_TYPE.VARCHAR)
    converters: list[tuple[str | None, Any]] = []
    for field_type in field_types:
        converter = connection.decoders.get(field_type)
        if converter is through:
            converter = None
        encoding = "ascii" if field_type == FIELD_TYPE.TINY else connection.encoding
        converters.append((encoding, converter))

    result = MySQLResult(connection)
    result.converters = converters
    tiny_wire_value = connection.escape(record["adj_close_provided"]).encode("ascii")
    raw_values = (
        raw_json,
        tiny_wire_value,
        None
        if record["currency"] is None
        else cast("str", record["currency"]).encode(connection.encoding),
        None
        if record["volume_unit"] is None
        else cast("str", record["volume_unit"]).encode(connection.encoding),
    )
    volume, adjusted_close, currency, volume_unit = result._read_row_from_packet(
        _DriverRowPacket(raw_values)
    )
    driver_record = dict(record)
    driver_record.update(
        {
            "volume": volume,
            "adj_close_provided": adjusted_close,
            "currency": currency,
            "volume_unit": volume_unit,
        }
    )
    return driver_record


def test_fred_writer_uses_real_registry_codec_and_revision_upsert_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("fred_series")
    inspected = _install_inspector(monkeypatch, inspector)
    context = _context()

    assert _call(engine, registry, rows=(_fred_row(value=3.1),), ctx=context) == 1
    assert _call(engine, registry, rows=(_fred_row(value=3.2),), ctx=context) == 1

    assert engine.begin_calls == 2
    assert engine.commit_calls == 2
    assert engine.rollback_calls == 0
    assert inspected == [engine.connection, engine.connection]
    assert inspector.seen_connection is engine.connection
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0
    assert len(engine.executions) == 2
    assert len(engine.metadata_queries) == 2
    stats_sql, stats_parameters = engine.metadata_queries[0]
    assert "TABLE_SCHEMA = DATABASE()" in stats_sql
    assert "TABLE_NAME = :table" in stats_sql
    assert all(
        field in stats_sql
        for field in (
            "INDEX_NAME",
            "NON_UNIQUE",
            "SEQ_IN_INDEX",
            "COLUMN_NAME",
            "SUB_PART",
            "EXPRESSION",
            "INDEX_TYPE",
        )
    )
    assert stats_parameters == {"table": "dwd_fred_series"}
    sql, bound = engine.executions[0]
    assert "INSERT INTO `dwd_fred_series`" in sql
    assert all(f"`{key}`" in sql for key in provider_model_codec.physical_model_key("fred_series"))
    update = _update_clause(sql)
    assert all(
        f"`{key}` = new.`{key}`" not in update
        for key in provider_model_codec.physical_model_key("fred_series")
    )
    assert isinstance(bound, list) and len(bound) == 1
    record = bound[0]
    assert record["value"] == 3.1
    assert record["requested_frequency"] is None
    assert record["_request_context"] == '["lin",1,null,"avg"]'
    assert record["source"] == "fred"
    assert record["_merged_at"] == datetime(2026, 10, 7, 18, 30)
    assert record["_diff_flag"] == 0
    assert record["_as_of"] is None


def test_bls_writer_preserves_m13_json_footnotes_and_unknown_pit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("bls_series"))
    context = _context("bls", "BlsSeries")

    assert (
        _call(
            engine,
            registry,
            source="bls",
            model="BlsSeries",
            rows=(_bls_row(),),
            ctx=context,
        )
        == 1
    )

    sql, bound = engine.executions[0]
    assert "INSERT INTO `dwd_bls_series`" in sql
    update = _update_clause(sql)
    assert all(
        f"`{key}` = new.`{key}`" not in update
        for key in provider_model_codec.physical_model_key("bls_series")
    )
    record = bound[0]  # type: ignore[index]
    assert record["period"] == "M13"
    assert record["period_name"] == "Annual average"
    assert isinstance(record["footnotes"], str)
    assert json.loads(record["footnotes"]) == [
        {"code": "P", "text": "Preliminary."},
        {"code": None, "text": None},
    ]
    assert record["latest"] is None
    assert record["preliminary"] is True
    assert record["api_version"] == "v2"
    assert record["_as_of"] is None
    assert record["_diff_flag"] == 0
    assert record["source"] == "bls"


def test_fmp_writer_and_reader_preserve_volume_through_pymysql_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _fmp_registry()
    symbol = " BRK.B "
    tiny_volume = float.fromhex("0x0.0000000000001p-1022")
    source_rows = (
        _fmp_row(symbol=symbol, date_value=date(2024, 1, 1), volume=2**53 + 1),
        _fmp_row(symbol=symbol, date_value=date(2024, 1, 2), volume=-0.0),
        _fmp_row(symbol=symbol, date_value=date(2024, 1, 3), volume=tiny_volume),
        _fmp_row(symbol=symbol, date_value=date(2024, 1, 4), volume=None),
    )
    writer_engine = RecordingEngine()
    writer_inspector = RecordingInspector("equity_historical")
    inspected_writer = _install_inspector(monkeypatch, writer_inspector)
    store_context = _context("fmp", "EquityHistorical")

    assert (
        _call(
            writer_engine,
            registry,
            source="fmp",
            model="EquityHistorical",
            rows=source_rows,
            ctx=store_context,
        )
        == 4
    )
    correction = _fmp_row(
        symbol=symbol,
        date_value=date(2024, 1, 1),
        volume=2**53 + 3,
        close=100.75,
    )
    assert (
        _call(
            writer_engine,
            registry,
            source="fmp",
            model="EquityHistorical",
            rows=(correction,),
            ctx=store_context,
        )
        == 1
    )

    assert writer_engine.begin_calls == writer_engine.commit_calls == 2
    assert writer_engine.rollback_calls == 0
    assert len(writer_engine.metadata_queries) == 2
    assert len(writer_engine.executions) == 2
    assert inspected_writer == [writer_engine.connection, writer_engine.connection]
    assert store_context.request_budget is not None
    assert store_context.request_budget.attempts_used == 0

    first_sql, first_bound = writer_engine.executions[0]
    correction_sql, correction_bound = writer_engine.executions[1]
    physical_key = provider_model_codec.physical_model_key("equity_historical")
    assert physical_key == ("symbol", "date")
    for sql in (first_sql, correction_sql):
        assert "INSERT INTO `dwd_equity_historical`" in sql
        assert "ON DUPLICATE KEY UPDATE" in sql
        update = _update_clause(sql)
        assert all(f"`{key}` = new.`{key}`" not in update for key in physical_key)
    assert isinstance(first_bound, list) and len(first_bound) == 4
    assert isinstance(correction_bound, list) and len(correction_bound) == 1
    first_records = cast("list[dict[str, object]]", first_bound)
    correction_records = cast("list[dict[str, object]]", correction_bound)
    assert json.loads(cast("str", first_records[0]["volume"])) == {
        "kind": "int",
        "value": str(2**53 + 1),
    }
    assert json.loads(cast("str", first_records[1]["volume"])) == {
        "kind": "float",
        "value": (-0.0).hex(),
    }
    assert json.loads(cast("str", first_records[2]["volume"])) == {
        "kind": "float",
        "value": tiny_volume.hex(),
    }
    assert first_records[3]["volume"] is None
    assert correction_records[0]["volume"] == json.dumps(
        {"kind": "int", "value": str(2**53 + 3)},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    for record in (*first_records, *correction_records):
        assert record["currency"] is None
        assert record["volume_unit"] is None
        assert record["adj_close_provided"] is False
        assert record["_as_of"] is None
        assert record["source"] == "fmp"

    # Exercise the installed PyMySQL bind conversion without opening a socket.
    driver_connection = pymysql.connections.Connection(
        defer_connect=True,
        charset="utf8mb4",
    )
    driver_connection.server_status = 0
    driver_cursor = driver_connection.cursor()
    compiled = text(first_sql).compile(dialect=MySQLDialect_pymysql())
    assert compiled.positiontup is not None
    rendered = [
        driver_cursor.mogrify(
            compiled.string,
            tuple(record[name] for name in compiled.positiontup),
        )
        for record in first_records
    ]
    assert driver_connection.escape(first_records[0]["volume"]) in rendered[0]
    assert driver_connection.escape(first_records[0]["adj_close_provided"]) == "0"
    assert driver_connection.escape(first_records[0]["currency"]) == "NULL"
    assert driver_connection.escape(first_records[0]["volume_unit"]) == "NULL"
    assert str(2**53 + 1) in rendered[0]
    assert driver_connection.decoders.get(FIELD_TYPE.JSON) is None

    persisted_records = [dict(record) for record in first_records]
    persisted_records[0] = dict(correction_records[0])
    driver_rows = [_pymysql_driver_shape(record, driver_connection) for record in persisted_records]
    assert driver_rows[0]["adj_close_provided"] == 0
    assert driver_rows[0]["currency"] is None and driver_rows[0]["volume_unit"] is None
    assert isinstance(driver_rows[0]["volume"], str)

    reader_engine = RecordingEngine(rows=driver_rows)
    reader_inspector = RecordingInspector("equity_historical")
    inspected_reader = _install_inspector(monkeypatch, reader_inspector)
    query_context = _context(
        "fmp",
        "EquityHistorical",
        grants=(
            _grant(
                "fmp",
                "EquityHistorical",
                operation=RequestOperation.QUERY,
            ),
        ),
        operation=RequestOperation.QUERY,
    )
    returned = read_provider_model_rows(
        engine=cast("Any", reader_engine),
        registry=registry,
        source="fmp",
        model="EquityHistorical",
        filters={"symbol": symbol},
        start=date(2024, 1, 1),
        end=date(2024, 1, 4),
        limit=10,
        ctx=query_context,
    )
    assert all(type(row) is EquityHistorical for row in returned)
    assert [row.date for row in returned] == [date(2024, 1, day) for day in range(1, 5)]
    assert [row.volume for row in returned] == [2**53 + 3, -0.0, tiny_volume, None]
    assert type(returned[0].volume) is int
    assert type(returned[1].volume) is float
    assert math.copysign(1.0, cast("float", returned[1].volume)) == -1.0
    assert cast("float", returned[2].volume).hex() == tiny_volume.hex()
    assert all(row.currency is None and row.volume_unit is None for row in returned)
    assert all(row.adj_close_provided is False for row in returned)
    assert reader_engine.begin_calls == reader_engine.commit_calls == 1
    assert reader_engine.rollback_calls == 0
    assert len(reader_engine.selects) == 1
    assert inspected_reader == [reader_engine.connection]
    assert query_context.request_budget is not None
    assert query_context.request_budget.attempts_used == 0
    driver_connection.close()


def test_fmp_writer_rejects_wrong_source_model_and_store_rights_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalFetcher

    registry = _fmp_registry()
    monkeypatch.setattr(
        EquityHistoricalFetcher,
        "extract_data",
        lambda *_args, **_kwargs: pytest.fail("native storage must not fetch provider data"),
    )

    calls = (
        (
            "fmp",
            "EquityHistorical",
            _context(grants=(_grant("fred", "FredSeries"),)),
            RequestAuthorizationError,
        ),
        (
            "fmp",
            "EquityHistorical",
            _context(
                "fmp",
                "EquityHistorical",
                grants=(_grant("fmp", "EquityHistorical", operation=RequestOperation.QUERY),),
            ),
            RequestAuthorizationError,
        ),
        (
            "fmp",
            "EquityQuote",
            _context("fmp", "EquityQuote"),
            ProviderModelStoreError,
        ),
    )
    for source, model, context, expected_error in calls:
        engine = RecordingEngine()
        inspected = _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
        with pytest.raises(expected_error):
            _call(
                engine,
                registry,
                source=source,
                model=model,
                rows=(_fmp_row(),),
                ctx=context,
            )
        _assert_no_db_touches(engine, inspected)
        assert context.request_budget is not None
        assert context.request_budget.attempts_used == 0


@pytest.mark.parametrize("bad_value", ["", "   ", "AAPL\x00", "AAPL\x7f"])
def test_fmp_invalid_symbol_contract_rows_fail_before_database(
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
) -> None:
    registry = _fmp_registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
    valid = _fmp_row()
    invalid = EquityHistorical.model_construct(
        **{**valid.model_dump(mode="python"), "symbol": bad_value}
    )

    with pytest.raises((ProviderModelStoreError, ValueError)):
        _call(
            engine,
            registry,
            source="fmp",
            model="EquityHistorical",
            rows=(invalid,),
            ctx=_context("fmp", "EquityHistorical"),
        )
    _assert_no_db_touches(engine, inspected)


def test_fmp_writer_requires_plain_date_rows_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _fmp_registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("equity_historical"))
    valid = _fmp_row()
    invalid = EquityHistorical.model_construct(
        **{**valid.model_dump(mode="python"), "date": datetime(2024, 1, 2)}
    )

    with pytest.raises(ValueError):
        _call(
            engine,
            registry,
            source="fmp",
            model="EquityHistorical",
            rows=(invalid,),
            ctx=_context("fmp", "EquityHistorical"),
        )
    _assert_no_db_touches(engine, inspected)


@pytest.mark.parametrize(
    ("grants", "expected_error"),
    [
        ((), RequestAuthorizationError),
        (("query",), RequestAuthorizationError),
        (("unknown",), RequestAuthorizationError),
        (("conditional",), RequestAuthorizationError),
        (("expired",), RequestAuthorizationError),
        (("wrong-model",), RequestAuthorizationError),
    ],
)
def test_missing_or_unusable_store_grants_touch_no_database(
    monkeypatch: pytest.MonkeyPatch,
    grants: tuple[str, ...],
    expected_error: type[Exception],
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    grant_map = {
        "query": _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
        "unknown": _grant("fred", "FredSeries", decision=GrantDecision.UNKNOWN),
        "conditional": _grant("fred", "FredSeries", conditions=("approval",)),
        "expired": _grant(
            "fred",
            "FredSeries",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
        "wrong-model": _grant("fred", "FredSearch"),
    }
    context = _context(grants=tuple(grant_map[name] for name in grants))

    with pytest.raises(expected_error):
        _call(engine, registry, ctx=context)
    _assert_no_db_touches(engine, inspected)


def test_default_context_and_non_store_context_touch_no_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))

    with pytest.raises(RequestAuthorizationError):
        _call(engine, registry, ctx=None)
    _assert_no_db_touches(engine, inspected)

    with pytest.raises(ValueError, match="STORE"):
        _call(engine, registry, ctx=_context(operation=RequestOperation.QUERY))
    _assert_no_db_touches(engine, inspected)


@pytest.mark.parametrize(
    ("cancel", "deadline", "timeout"),
    [
        (True, None, None),
        (False, -1.0, None),
        (False, None, 1.0),
    ],
)
def test_cancellation_and_expired_deadlines_fail_before_database(
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
    deadline: float | None,
    timeout: float | None,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    event = threading.Event() if cancel else None
    if event is not None:
        event.set()
    context = _context(cancel=event, deadline=deadline, timeout=timeout)
    if timeout is not None:
        ticks = iter((100.0, 102.0))
        monkeypatch.setattr(provider_model_store.time, "monotonic", lambda: next(ticks))

    with pytest.raises((RequestExecutionCancelledError, RequestExecutionDeadlineError)):
        _call(engine, registry, ctx=context)
    _assert_no_db_touches(engine, inspected)


def test_admission_timeout_includes_record_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    prepare_records = provider_model_store._prepare_records
    delayed = False

    def delayed_prepare(**kwargs: Any) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        nonlocal delayed
        delayed = True
        time.sleep(0.05)
        return prepare_records(**kwargs)

    monkeypatch.setattr(provider_model_store, "_prepare_records", delayed_prepare)

    with pytest.raises(RequestExecutionDeadlineError):
        _call(engine, registry, rows=(_fred_row(),), ctx=_context(timeout=0.01))

    assert delayed
    _assert_no_db_touches(engine, inspected)


def test_missing_store_grant_is_checked_before_registry_or_large_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadRows(RuntimeSequence[object]):
        reads = 0

        def __getitem__(self, index: int | slice) -> NoReturn:
            self.reads += 1
            raise AssertionError(f"unexpected row access at {index}")

        def __len__(self) -> int:
            self.reads += 1
            raise AssertionError("unexpected row count access")

    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    rows = UnreadRows()
    monkeypatch.setattr(
        provider_model_store,
        "_registered_domain",
        lambda *_args: pytest.fail("registry access preceded STORE authorization"),
    )

    with pytest.raises(RequestAuthorizationError):
        _call(engine, registry, rows=cast("Any", rows), ctx=_context(grants=()))

    assert rows.reads == 0
    _assert_no_db_touches(engine, inspected)


@pytest.mark.parametrize(
    "change",
    [
        "extra_column",
        "wrong_pk_order",
        "nullable_key",
        "wrong_collation",
        "wrong_width",
        "unsigned_type",
        "extra_unique",
        "nonnull_as_of",
    ],
)
def test_unreviewed_existing_dwd_schema_rolls_back_before_upsert(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("bls_series")
    if change == "extra_column":
        inspector.columns.append(
            {"name": "surprise", "type": mysql.VARCHAR(12), "nullable": True, "default": None}
        )
    elif change == "wrong_pk_order":
        inspector.primary_key["constrained_columns"] = ["year", "series_id", "period"]
    elif change == "nullable_key":
        next(column for column in inspector.columns if column["name"] == "period")["nullable"] = (
            True
        )
    elif change == "wrong_collation":
        next(column for column in inspector.columns if column["name"] == "series_id")["type"] = (
            mysql.VARCHAR(255, charset="utf8mb4", collation="utf8mb4_0900_ai_ci")
        )
    elif change == "wrong_width":
        next(column for column in inspector.columns if column["name"] == "period")["type"] = (
            mysql.VARCHAR(254, charset="utf8mb4", collation="utf8mb4_0900_bin")
        )
    elif change == "unsigned_type":
        next(column for column in inspector.columns if column["name"] == "year")["type"] = (
            mysql.BIGINT(unsigned=True)
        )
    elif change == "extra_unique":
        inspector.unique_constraints = [
            {"name": "uq_other_identity", "column_names": ["series_id"]}
        ]
    else:
        next(column for column in inspector.columns if column["name"] == "_as_of")["nullable"] = (
            False
        )
    inspected = _install_inspector(monkeypatch, inspector)

    with pytest.raises(ProviderModelStoreError):
        _call(
            engine,
            registry,
            source="bls",
            model="BlsSeries",
            rows=(_bls_row(),),
            ctx=_context("bls", "BlsSeries"),
        )

    assert engine.begin_calls == 1
    assert engine.rollback_calls == 1
    assert engine.commit_calls == 0
    assert engine.executions == []
    assert inspected == [engine.connection]


def test_mysql_public_inspector_hides_primary_key_prefix_length() -> None:
    generated_ddl = provider_model_schema.model_dwd_table_ddl("bls_series")
    prefixed_ddl = generated_ddl.replace(
        "PRIMARY KEY (`series_id`,",
        "PRIMARY KEY (`series_id`(8),",
        1,
    )
    assert prefixed_ddl != generated_ddl

    class DDLResult:
        def __init__(self, ddl_text: str) -> None:
            self.ddl_text = ddl_text

        def first(self) -> tuple[str, str]:
            return "dwd_bls_series", self.ddl_text

    class DDLConnection:
        def __init__(self, ddl_text: str) -> None:
            self.result = DDLResult(ddl_text)

        def execution_options(self, **_kwargs: object) -> DDLConnection:
            return self

        def exec_driver_sql(self, _statement: str) -> DDLResult:
            return self.result

    def reflected_pk_columns(ddl_text: str) -> list[str]:
        dialect = cast("Any", mysql.dialect())
        dialect._connection_charset = "utf8mb4"
        reflected = dialect.get_pk_constraint(
            cast("Any", DDLConnection(ddl_text)),
            "dwd_bls_series",
            info_cache={},
        )
        return cast("list[str]", reflected["constrained_columns"])

    expected = list(provider_model_codec.physical_model_key("bls_series"))
    assert reflected_pk_columns(generated_ddl) == expected
    assert reflected_pk_columns(prefixed_ddl) == expected


@pytest.mark.parametrize(
    "change",
    [
        "primary_prefix",
        "missing_primary_part",
        "duplicate_primary_position",
        "wrong_primary_order",
        "primary_expression",
        "primary_non_unique",
        "primary_non_btree",
        "missing_primary",
        "unknown_metadata",
        "same_key_prefix_unique",
        "different_key_unique",
        "metadata_query_failure",
    ],
)
def test_index_statistics_reject_ambiguous_primary_and_extra_unique_indexes(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("bls_series")
    inspected = _install_inspector(monkeypatch, inspector)
    rows = inspector.index_rows

    if change == "primary_prefix":
        next(row for row in rows if row["COLUMN_NAME"] == "series_id")["SUB_PART"] = 8
    elif change == "missing_primary_part":
        inspector.index_rows = [row for row in rows if row["COLUMN_NAME"] != "year"]
    elif change == "duplicate_primary_position":
        next(row for row in rows if row["COLUMN_NAME"] == "period")["SEQ_IN_INDEX"] = 1
    elif change == "wrong_primary_order":
        next(row for row in rows if row["COLUMN_NAME"] == "series_id")["SEQ_IN_INDEX"] = 2
        next(row for row in rows if row["COLUMN_NAME"] == "year")["SEQ_IN_INDEX"] = 1
    elif change == "primary_expression":
        next(row for row in rows if row["COLUMN_NAME"] == "series_id")["EXPRESSION"] = (
            "LEFT(`series_id`, 8)"
        )
    elif change == "primary_non_unique":
        rows[0]["NON_UNIQUE"] = 1
    elif change == "primary_non_btree":
        rows[0]["INDEX_TYPE"] = "HASH"
    elif change == "missing_primary":
        inspector.index_rows = []
    elif change == "unknown_metadata":
        rows[0]["UNREVIEWED"] = "unexpected"
    elif change in {"same_key_prefix_unique", "different_key_unique"}:
        extra_columns = (
            ("series_id", "year", "period")
            if change == "same_key_prefix_unique"
            else ("series_id",)
        )
        for position, column in enumerate(extra_columns, start=1):
            inspector.index_rows.append(
                {
                    "INDEX_NAME": "uq_extra_identity",
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": position,
                    "COLUMN_NAME": column,
                    "SUB_PART": 8 if change == "same_key_prefix_unique" and position == 1 else None,
                    "EXPRESSION": None,
                    "INDEX_TYPE": "BTREE",
                }
            )
    else:
        engine.fail_metadata = True

    with pytest.raises(ProviderModelStoreError):
        _call(
            engine,
            registry,
            source="bls",
            model="BlsSeries",
            rows=(_bls_row(),),
            ctx=_context("bls", "BlsSeries"),
        )

    assert engine.begin_calls == 1
    assert len(engine.metadata_queries) == 1
    assert engine.metadata_queries[0][1] == {"table": "dwd_bls_series"}
    assert engine.executions == []
    assert engine.rollback_calls == 1
    assert engine.commit_calls == 0
    assert inspected == [engine.connection]


def test_missing_table_and_non_mysql_dialect_never_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("fred_series")
    inspector.has_table = lambda _table: False  # type: ignore[method-assign]
    inspected = _install_inspector(monkeypatch, inspector)
    with pytest.raises(ProviderModelStoreError, match="does not exist"):
        _call(engine, registry, ctx=_context())
    assert engine.rollback_calls == 1
    assert engine.executions == []

    non_mysql = RecordingEngine(dialect_name="sqlite")
    with pytest.raises(ProviderModelStoreError, match="requires MySQL"):
        _call(non_mysql, registry, ctx=_context())
    assert non_mysql.begin_calls == 0
    assert non_mysql.executions == []
    assert inspected == [engine.connection]


def test_bad_last_row_duplicate_key_and_oversized_or_long_input_fail_pre_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    context = _context()

    with pytest.raises(ValueError):
        _call(engine, registry, rows=(_fred_row(), cast("Any", _bls_row())), ctx=context)
    with pytest.raises(ValueError, match="duplicate physical keys"):
        _call(engine, registry, rows=(_fred_row(), _fred_row()), ctx=context)
    with pytest.raises(ValueError, match="limited to"):
        _call(engine, registry, rows=(_fred_row(),) * 10_001, ctx=context)
    with pytest.raises(ValueError, match="column width"):
        _call(
            engine,
            registry,
            source="bls",
            model="BlsSeries",
            rows=(_bls_row(series_id="S" * 256),),
            ctx=_context("bls", "BlsSeries"),
        )
    with pytest.raises(TypeError, match="sequence"):
        _call(engine, registry, rows=cast("Any", "not-a-row-sequence"), ctx=context)
    with pytest.raises(ValueError, match="timezone-aware"):
        _call(engine, registry, rows=(_fred_row(),), ctx=context, observed_at=datetime(2026, 1, 1))

    _assert_no_db_touches(engine, inspected)


def test_scope_inheritance_requires_parent_and_store_grants_and_restores_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    parent_and_store = _context(
        grants=(
            _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
            _grant("fred", "FredSeries", operation=RequestOperation.STORE),
        )
    )
    assert parent_and_store.request_budget is not None

    with request_execution_scope(
        source="fred",
        canonical_model="FredSeries",
        operation=RequestOperation.QUERY,
        budget=parent_and_store.request_budget,
    ):
        assert len(provider_model_store.current_request_scopes()) == 1
        assert _call(engine, registry, ctx=FetchContext(operation=RequestOperation.STORE)) == 1
        assert len(provider_model_store.current_request_scopes()) == 1
    assert provider_model_store.current_request_scopes() == ()


def test_inherited_parent_cancellation_is_not_replaced_by_store_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    context = _context(
        grants=(
            _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
            _grant("fred", "FredSeries", operation=RequestOperation.STORE),
        )
    )
    assert context.request_budget is not None
    event = threading.Event()
    event.set()

    with (
        pytest.raises(RequestExecutionCancelledError),
        request_execution_scope(
            source="fred",
            canonical_model="FredSeries",
            operation=RequestOperation.QUERY,
            budget=context.request_budget,
            cancellation=event,
        ),
    ):
        _call(engine, registry, ctx=FetchContext(operation=RequestOperation.STORE))
    _assert_no_db_touches(engine, inspected)


def test_inherited_parent_deadline_is_checked_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    context = _context(
        grants=(
            _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
            _grant("fred", "FredSeries", operation=RequestOperation.STORE),
        )
    )
    assert context.request_budget is not None

    with (
        pytest.raises(RequestExecutionDeadlineError),
        request_execution_scope(
            source="fred",
            canonical_model="FredSeries",
            operation=RequestOperation.QUERY,
            budget=context.request_budget,
            deadline=-1.0,
        ),
    ):
        _call(engine, registry, ctx=FetchContext(operation=RequestOperation.STORE))
    _assert_no_db_touches(engine, inspected)


def test_inherited_parent_deadline_is_tighter_than_context_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    parent = _context(
        grants=(
            _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
            _grant("fred", "FredSeries", operation=RequestOperation.STORE),
        )
    )
    assert parent.request_budget is not None
    monkeypatch.setattr(provider_model_store.time, "monotonic", lambda: 100.0)
    registered_domain = provider_model_store._registered_domain
    seen_deadlines: list[float | None] = []

    def inspect_active_deadline(
        registry_arg: ProviderRegistry,
        source: str,
        model: str,
    ) -> str:
        seen_deadlines.append(provider_model_store.current_request_scopes()[-1].deadline)
        return registered_domain(registry_arg, source, model)

    monkeypatch.setattr(provider_model_store, "_registered_domain", inspect_active_deadline)
    context = FetchContext(
        timeout=20.0,
        _deadline_monotonic=120.0,
        request_budget=parent.request_budget,
        operation=RequestOperation.STORE,
    )

    with request_execution_scope(
        source="fred",
        canonical_model="FredSeries",
        operation=RequestOperation.QUERY,
        budget=parent.request_budget,
        deadline=110.0,
    ):
        assert _call(engine, registry, ctx=context) == 1

    assert seen_deadlines == [110.0]
    assert engine.commit_calls == 1


def test_store_context_cannot_replace_an_inherited_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    parent = _context(
        grants=(
            _grant("fred", "FredSeries", operation=RequestOperation.QUERY),
            _grant("fred", "FredSeries", operation=RequestOperation.STORE),
        )
    )
    replacement = _context("fred", "FredSeries")
    assert parent.request_budget is not None
    assert replacement.request_budget is not None
    assert replacement.request_budget is not parent.request_budget

    with (
        pytest.raises(RequestBudgetScopeError),
        request_execution_scope(
            source="fred",
            canonical_model="FredSeries",
            operation=RequestOperation.QUERY,
            budget=parent.request_budget,
        ),
    ):
        _call(
            engine,
            registry,
            ctx=FetchContext(
                request_budget=replacement.request_budget,
                operation=RequestOperation.STORE,
            ),
        )
    _assert_no_db_touches(engine, inspected)


def test_store_chunks_at_one_thousand_and_checks_cancellation_between_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("bls_series"))
    context = _context("bls", "BlsSeries")
    event = threading.Event()
    engine.cancel_after_execute = event
    context = FetchContext(
        request_budget=context.request_budget,
        operation=RequestOperation.STORE,
        _thread_cancel_event=event,
    )
    rows = tuple(
        _bls_row(series_id=f"SERIES{index:05d}", period=f"M{index % 13 + 1:02d}")
        for index in range(1_001)
    )

    with pytest.raises(RequestExecutionCancelledError):
        _call(
            engine,
            registry,
            source="bls",
            model="BlsSeries",
            rows=rows,
            ctx=context,
        )

    assert len(engine.executions) == 1
    assert len(engine.executions[0][1]) == 1_000  # type: ignore[arg-type]
    assert engine.rollback_calls == 1
    assert engine.commit_calls == 0


def test_scope_liveness_is_checked_before_commit_and_execute_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    context = _context(deadline=1.0)
    clock = [0.0]
    monkeypatch.setattr(provider_model_store.time, "monotonic", lambda: clock[0])
    execute = engine.connection.execute

    def expire_after_upsert(statement: object, parameters: object) -> object:
        result = execute(statement, parameters)
        if "INSERT INTO" in str(statement):
            clock[0] = 2.0
        return result

    monkeypatch.setattr(engine.connection, "execute", expire_after_upsert)

    with pytest.raises(RequestExecutionDeadlineError):
        _call(engine, registry, ctx=context)
    assert len(engine.executions) == 1
    assert engine.rollback_calls == 1
    assert engine.commit_calls == 0

    failing = RecordingEngine()
    failing.fail_execute = True
    _install_inspector(monkeypatch, RecordingInspector("fred_series"))
    with pytest.raises(ProviderModelStoreError, match="transaction failed"):
        _call(failing, registry, ctx=_context())
    assert failing.rollback_calls == 1
    assert failing.commit_calls == 0
    assert failing.begin_calls == 1


def test_unregistered_or_cross_identity_models_fail_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))

    with pytest.raises(ProviderModelStoreError):
        _call(engine, registry, model="FredSearch", ctx=_context(model="FredSearch"))
    with pytest.raises(ProviderModelStoreError):
        _call(engine, registry, source="bls", model="FredSeries", ctx=_context("bls", "FredSeries"))
    _assert_no_db_touches(engine, inspected)


def test_untyped_or_overlength_contract_rows_do_not_reach_the_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, RecordingInspector("fred_series"))

    class ChildSeriesObservation(SeriesObservation):
        pass

    bad_instance = ChildSeriesObservation.model_validate(_fred_row().model_dump())

    with pytest.raises(ValueError, match="exact reviewed central contract"):
        _call(engine, registry, rows=(cast("Any", bad_instance),), ctx=_context())
    _assert_no_db_touches(engine, inspected)


def test_only_actual_dwd_table_columns_are_reflected_for_the_selected_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine = RecordingEngine()
    inspector = RecordingInspector("fred_series")
    inspected = _install_inspector(monkeypatch, inspector)

    assert _call(engine, registry, ctx=_context()) == 1
    assert inspected == [engine.connection]
    assert domains.dwd_table("fred_series") == "dwd_fred_series"
