"""Offline atomic ODS-capture plus native DWD batch-writer tests."""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from sqlalchemy.dialects import mysql

from opendata.data import domains
from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetError,
    RequestGrant,
    RequestOperation,
)
from opendata.pipeline import ddl, provider_model_codec, provider_model_raw, provider_model_schema
from opendata.services import provider_model_store
from opendata.services.provider_model_store import ProviderModelStoreError

_OBSERVED_AT = datetime(2026, 10, 8, 2, 30, tzinfo=timezone(timedelta(hours=8)))
_BATCH_ID = "00000000-0000-4000-8000-000000000001"
_ODS_NAMES = (
    "_batch_id",
    "_source",
    "_fetched_at",
    "_raw_scope",
    "_schema_version",
    "_raw_row_count",
    "_dwd_row_count",
    "_payload",
    "_query_context",
    "_payload_sha256",
    "_query_sha256",
)
_ODS_EXPECTED_COLUMNS = (
    ddl.Column(
        "_batch_id",
        "varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin",
        nullable=False,
    ),
    ddl.Column("_source", "varchar(32)", nullable=False),
    ddl.Column("_fetched_at", "datetime", nullable=False),
    ddl.Column("_raw_scope", "varchar(32)", nullable=False),
    ddl.Column("_schema_version", "smallint", nullable=False),
    ddl.Column("_raw_row_count", "int", nullable=False),
    ddl.Column("_dwd_row_count", "int", nullable=False),
    ddl.Column("_payload", "longtext", nullable=False),
    ddl.Column("_query_context", "longtext", nullable=False),
    ddl.Column("_payload_sha256", "varchar(64)", nullable=False),
    ddl.Column("_query_sha256", "varchar(64)", nullable=False),
)
_DWD_SHA256_BEFORE = {
    "fred_series": "ee1ef517c5fc827f9b05302bc5d3be02bcbd7485633be131822f441f58f1e860",
    "bls_series": "b3b9110f484376ae5dd05bcc36a39d491575169ce81cd69cc8e8c5f2112b06dd",
    "equity_historical": "cfa6870dd895114b726f2f97050b5352703ce93eab39327fa010e1274a5dd9b2",
}
_CASES = (
    ("fred_series", "fred", "FredSeries"),
    ("bls_series", "bls", "BlsSeries"),
    ("equity_historical", "fmp", "EquityHistorical"),
)
_DEFAULT_CONTEXT = object()


def _query(source: str) -> FredSeriesQuery | BlsSeriesQuery | EquityHistoricalQuery:
    if source == "fred":
        return FredSeriesQuery(
            source="fred",
            market="us",
            series_id="CPIAUCSL",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
        )
    if source == "bls":
        return BlsSeriesQuery(
            source="bls",
            market="us",
            series_ids=["LNS14000000"],
            start_year=2025,
            end_year=2025,
            max_requests=2,
        )
    return EquityHistoricalQuery(
        source="fmp",
        market="us",
        symbol="BRK.B",
    )


def _provider_case(
    source: str,
) -> tuple[str, str, str, object, Sequence[object]]:
    if source == "fred":
        model = "FredSeries"
        raw: object = [
            {
                "date": "2024-01-01",
                "value": ".",
                "realtime_start": "2024-01-01",
                "realtime_end": "9999-12-31",
            }
        ]
        rows: Sequence[object] = (
            SeriesObservation(
                series_id="CPIAUCSL",
                date=date(2024, 1, 1),
                value=None,
                realtime_start=date(2024, 1, 1),
                realtime_end=date(9999, 12, 31),
                transform_units="lin",
                output_type=1,
                requested_frequency=None,
                requested_aggregation_method="avg",
            ),
        )
        return "fred_series", source, model, raw, rows
    if source == "bls":
        model = "BlsSeries"
        raw = [
            {
                "series_id": "LNS14000000",
                "year": "2025",
                "period": "M13",
                "period_name": "Annual average",
                "value": "4.1",
                "footnotes": [{"code": "P", "text": "Preliminary."}],
                "latest": None,
                "api_version": "v2",
            }
        ]
        rows = (
            BlsObservation(
                series_id="LNS14000000",
                year=2025,
                period="M13",
                period_name="Annual average",
                value=4.1,
                footnotes=(BlsFootnote(code="P", text="Preliminary."),),
                latest=None,
                preliminary=True,
                api_version="v2",
            ),
        )
        return "bls_series", source, model, raw, rows

    model = "EquityHistorical"
    raw = [
        {
            "symbol": "BRK.B",
            "date": "2026-01-02",
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.0,
            "volume": 2**64 + 1,
            "change": -0.0,
            "changePercent": 0.25,
            "vwap": 101.0,
        }
    ]
    rows = (
        EquityHistorical(
            symbol="BRK.B",
            date=date(2026, 1, 2),
            open=100.0,
            high=103.0,
            low=99.5,
            close=102.0,
            volume=2**64 + 1,
            change=-0.0,
            change_percent=0.25,
            vwap=101.0,
            currency=None,
            currency_semantics="source_unverified",
            volume_unit=None,
            volume_unit_semantics="source_unverified",
            query_window_scope="provider_default_unknown",
            window_boundary_semantics="source_unverified",
            provider_default_window_semantics="source_unverified",
            close_adjustment_semantics="split_adjusted_per_source_faq",
            adj_close_provided=False,
        ),
    )
    return "equity_historical", source, model, raw, rows


def _capture(source: str, raw: object | None = None) -> provider_model_raw.NativeRawCapture:
    domain, source_name, model, default_raw, _rows = _provider_case(source)
    selected_raw = default_raw if raw is None else raw
    return provider_model_raw.make_provider_model_capture(
        source=source_name,
        model=model,
        raw=cast("list[dict[str, object]]", selected_raw),
        params=_query(source_name),
    )


def _registry(source: str) -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider(source, registry)
    return registry


def _grant(source: str, model: str) -> RequestGrant:
    host = {
        "fred": "api.stlouisfed.org",
        "bls": "api.bls.gov",
        "fmp": "financialmodelingprep.com",
    }[source]
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.STORE,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-test-rights-record",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=(host,),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


def _context(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.STORE,
    cancel: threading.Event | None = None,
) -> FetchContext:
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(_grant(source, model),),
    )
    return FetchContext(
        request_budget=budget,
        operation=operation,
        _thread_cancel_event=cancel,
    )


def _mysql_type(sql_type: str) -> Any:
    match = re.match(r"^varchar\((\d+)\)", sql_type, re.IGNORECASE)
    if match is not None:
        kwargs: dict[str, object] = {}
        charset = re.search(r"CHARACTER\s+SET\s+(\w+)", sql_type, re.IGNORECASE)
        collation = re.search(r"COLLATE\s+(\w+)", sql_type, re.IGNORECASE)
        if charset is not None:
            kwargs["charset"] = charset.group(1)
        if collation is not None:
            kwargs["collation"] = collation.group(1)
        return mysql.VARCHAR(int(match.group(1)), **kwargs)
    normalized = sql_type.lower()
    types: dict[str, Any] = {
        "date": mysql.DATE(),
        "datetime": mysql.DATETIME(),
        "double": mysql.DOUBLE(),
        "bigint": mysql.BIGINT(),
        "json": mysql.JSON(),
        "tinyint(1)": mysql.TINYINT(1),
        "smallint": mysql.SMALLINT(),
        "int": mysql.INTEGER(),
        "longtext": mysql.LONGTEXT(),
    }
    if normalized not in types:
        raise AssertionError(f"unexpected test SQL type {sql_type!r}")
    return types[normalized]


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[ddl.Column, ...]
    primary_key: tuple[str, ...]


def _table_specs() -> dict[str, TableSpec]:
    specs: dict[str, TableSpec] = {}
    for domain, source, _model in _CASES:
        ods_table = domains.ods_table(domain, source)
        specs[ods_table] = TableSpec(
            provider_model_schema.model_ods_storage_columns(domain, source),
            ("_batch_id",),
        )
        dwd_table = domains.dwd_table(domain)
        dwd_columns = (
            *provider_model_schema.model_storage_columns(domain),
            *ddl.DWD_TRACE_COLUMNS[:-1],
            ddl.Column("_as_of", "date", nullable=True),
        )
        specs[dwd_table] = TableSpec(
            dwd_columns,
            provider_model_codec.physical_model_key(domain),
        )
    return specs


class RecordingResult:
    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self._rows = [dict(row) for row in rows]

    def mappings(self) -> RecordingResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self._rows


class RecordingInspector:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine

    def has_table(self, table: str) -> bool:
        self.engine.inspector_calls.append(("has_table", table))
        return table in self.engine.specs and table not in self.engine.missing_tables

    def get_columns(self, table: str) -> list[dict[str, object]]:
        self.engine.inspector_calls.append(("get_columns", table))
        spec = self.engine.specs[table]
        columns = [
            {
                "name": column.name,
                "type": _mysql_type(column.sql_type),
                "nullable": column.nullable,
                "default": column.sql_default,
            }
            for column in spec.columns
        ]
        columns.extend(dict(extra) for extra in self.engine.extra_columns.get(table, ()))
        for (mutated_table, name), value in self.engine.column_overrides.items():
            if mutated_table == table:
                next(column for column in columns if column["name"] == name).update(value)
        return columns

    def get_pk_constraint(self, table: str) -> dict[str, object]:
        self.engine.inspector_calls.append(("get_pk_constraint", table))
        return {
            "name": "PRIMARY",
            "constrained_columns": list(
                self.engine.primary_overrides.get(table, self.engine.specs[table].primary_key)
            ),
        }

    def get_unique_constraints(self, table: str) -> list[dict[str, object]]:
        self.engine.inspector_calls.append(("get_unique_constraints", table))
        return self.engine.unique_constraints.get(table, [])

    def get_indexes(self, table: str) -> list[dict[str, object]]:
        self.engine.inspector_calls.append(("get_indexes", table))
        return self.engine.indexes.get(table, [])


class RecordingConnection:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine
        self.dialect = mysql.dialect()

    def execute(self, statement: object, parameters: object = None) -> RecordingResult | None:
        sql = str(statement)
        self.engine.sql_statements.append(sql)
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            if not self.engine.in_transaction:
                raise AssertionError("index metadata must share the writer transaction")
            table = cast("Mapping[str, object]", parameters)["table"]
            self.engine.metadata_queries.append((sql, parameters))
            if self.engine.fail_metadata:
                raise RuntimeError("recording metadata failure")
            return RecordingResult(self.engine.index_rows[cast("str", table)])
        if "INFORMATION_SCHEMA.TABLES" in sql:
            if not self.engine.in_transaction:
                raise AssertionError("table engine checks must share the writer transaction")
            table = cast("Mapping[str, object]", parameters)["table"]
            self.engine.engine_queries.append((sql, parameters))
            engine_name = self.engine.table_engines.get(cast("str", table))
            if engine_name is None or cast("str", table) in self.engine.missing_tables:
                return RecordingResult([])
            return RecordingResult([{"TABLE_NAME": table, "ENGINE": engine_name}])
        if isinstance(parameters, Mapping) and "_payload" in parameters:
            self.engine.ods_insert_attempts += 1
            batch_id = cast("str", parameters["_batch_id"])
            if (
                batch_id in self.engine.persisted_batch_ids
                or batch_id in self.engine.pending_batch_ids
            ):
                raise RuntimeError("duplicate key")
            self.engine.pending_batch_ids.add(batch_id)
            self.engine.ods_rows.append(dict(parameters))
            self.engine.events.append("ods_insert")
            if self.engine.cancel_after_ods is not None:
                self.engine.cancel_after_ods.set()
            return None
        if "ON DUPLICATE KEY UPDATE" in sql:
            self.engine.dwd_attempts += 1
            self.engine.dwd_chunks.append(parameters)
            self.engine.events.append("dwd_upsert")
            if self.engine.fail_dwd_chunk == self.engine.dwd_attempts:
                raise RuntimeError("recording DWD chunk failure")
            return None
        raise AssertionError(f"unexpected SQL operation {sql!r}")


class _Transaction:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine

    def __enter__(self) -> RecordingConnection:
        self.engine.begin_calls += 1
        self.engine.in_transaction = True
        self.engine.pending_batch_ids = set()
        return self.engine.connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> Literal[False]:
        self.engine.in_transaction = False
        if exc_type is None:
            self.engine.commit_calls += 1
            self.engine.persisted_batch_ids.update(self.engine.pending_batch_ids)
        else:
            self.engine.rollback_calls += 1
            self.engine.pending_batch_ids.clear()
        return False


class RecordingEngine:
    def __init__(self) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self.specs = _table_specs()
        self.connection = RecordingConnection(self)
        self.in_transaction = False
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.metadata_queries: list[tuple[str, object]] = []
        self.engine_queries: list[tuple[str, object]] = []
        self.sql_statements: list[str] = []
        self.inspector_calls: list[tuple[str, str]] = []
        self.table_engines = dict.fromkeys(self.specs, "InnoDB")
        self.index_rows = {
            table: [
                {
                    "INDEX_NAME": "PRIMARY",
                    "NON_UNIQUE": 0,
                    "SEQ_IN_INDEX": position,
                    "COLUMN_NAME": name,
                    "SUB_PART": None,
                    "EXPRESSION": None,
                    "INDEX_TYPE": "BTREE",
                }
                for position, name in enumerate(spec.primary_key, start=1)
            ]
            for table, spec in self.specs.items()
        }
        self.missing_tables: set[str] = set()
        self.extra_columns: dict[str, list[dict[str, object]]] = {}
        self.column_overrides: dict[tuple[str, str], dict[str, object]] = {}
        self.primary_overrides: dict[str, tuple[str, ...]] = {}
        self.unique_constraints: dict[str, list[dict[str, object]]] = {}
        self.indexes: dict[str, list[dict[str, object]]] = {}
        self.fail_metadata = False
        self.fail_dwd_chunk: int | None = None
        self.cancel_after_ods: threading.Event | None = None
        self.ods_rows: list[dict[str, object]] = []
        self.ods_insert_attempts = 0
        self.dwd_attempts = 0
        self.dwd_chunks: list[object] = []
        self.events: list[str] = []
        self.persisted_batch_ids: set[str] = set()
        self.pending_batch_ids: set[str] = set()

    def begin(self) -> _Transaction:
        return _Transaction(self)


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch,
    engine: RecordingEngine,
) -> list[tuple[str, str]]:
    def fake_inspect(connection: object) -> RecordingInspector:
        assert connection is engine.connection
        assert engine.in_transaction
        return RecordingInspector(engine)

    monkeypatch.setattr(provider_model_store, "inspect", fake_inspect)
    return engine.inspector_calls


def _call(
    engine: RecordingEngine,
    *,
    source: str,
    model: str,
    capture: provider_model_raw.NativeRawCapture,
    rows: Sequence[object],
    batch_id: str = _BATCH_ID,
    ctx: FetchContext | None | object = _DEFAULT_CONTEXT,
    registry: ProviderRegistry | None = None,
) -> int:
    selected_ctx = (
        _context(source, model) if ctx is _DEFAULT_CONTEXT else cast("FetchContext | None", ctx)
    )
    selected_registry = _registry(source) if registry is None else registry
    return provider_model_store.write_provider_model_batch(
        engine=cast("Any", engine),
        registry=selected_registry,
        source=source,
        model=model,
        capture=capture,
        rows=cast("Sequence[Any]", rows),
        observed_at=_OBSERVED_AT,
        batch_id=batch_id,
        ctx=selected_ctx,
    )


def _assert_no_insert(engine: RecordingEngine) -> None:
    assert engine.ods_rows == []
    assert engine.ods_insert_attempts == 0
    assert engine.dwd_attempts == 0


@pytest.mark.parametrize("domain,source,model", _CASES)
def test_native_ods_schema_is_exact_and_dwd_ddl_bytes_are_unchanged(
    domain: str,
    source: str,
    model: str,
) -> None:
    columns = provider_model_schema.model_ods_storage_columns(domain, source)
    table_ddl = provider_model_schema.model_ods_table_ddl(domain, source)
    assert tuple(column.name for column in columns) == _ODS_NAMES
    assert columns == _ODS_EXPECTED_COLUMNS
    assert all(column.nullable is False for column in columns)
    assert f"CREATE TABLE IF NOT EXISTS `{domains.ods_table(domain, source)}` (" in table_ddl
    assert "PRIMARY KEY (`_batch_id`)" in table_ddl
    assert "ENGINE=InnoDB ROW_FORMAT=DYNAMIC DEFAULT CHARSET=utf8mb4;" in table_ddl
    assert "PARTITION BY" not in table_ddl
    assert provider_model_schema.model_storage_columns(domain)
    assert model

    rendered_dwd = provider_model_schema.model_dwd_table_ddl(domain)
    import hashlib

    assert hashlib.sha256(rendered_dwd.encode("utf-8")).hexdigest() == _DWD_SHA256_BEFORE[domain]


@pytest.mark.parametrize(
    ("domain", "source"),
    [("fred_series", "bls"), ("bls_series", "fred"), ("equity_quote", "fmp"), ("auto", "fmp")],
)
def test_native_ods_schema_fails_closed_for_unreviewed_pairs(domain: str, source: str) -> None:
    with pytest.raises(ValueError):
        provider_model_schema.model_ods_storage_columns(domain, source)
    with pytest.raises(ValueError):
        provider_model_schema.model_ods_table_ddl(domain, source)


@pytest.mark.parametrize("source", ["fred", "bls", "fmp"])
def test_batch_persists_exact_raw_capture_and_typed_rows_for_each_model(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    domain, source, model, raw, rows = _provider_case(source)
    capture = _capture(source, raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)

    assert _call(
        engine,
        source=source,
        model=model,
        capture=capture,
        rows=rows,
    ) == len(rows)

    ods_table = domains.ods_table(domain, source)
    dwd_table = domains.dwd_table(domain)
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert len(engine.engine_queries) == 2
    assert [parameters for _sql, parameters in engine.engine_queries] == [
        {"table": ods_table},
        {"table": dwd_table},
    ]
    assert all(":table" in sql and "TABLE_NAME = :table" in sql for sql, _ in engine.engine_queries)
    assert engine.events == ["ods_insert", "dwd_upsert"]
    ods = engine.ods_rows[0]
    assert tuple(ods) == _ODS_NAMES
    assert ods["_source"] == source
    assert ods["_raw_scope"] == "extract_data_output"
    assert ods["_schema_version"] == 1
    assert ods["_raw_row_count"] == len(raw)  # type: ignore[arg-type]
    assert ods["_dwd_row_count"] == len(rows)
    assert ods["_fetched_at"] == _OBSERVED_AT.astimezone(timezone.utc).replace(tzinfo=None)
    assert ods["_payload"] == capture.raw_json
    assert ods["_query_context"] == capture.query_json
    assert ods["_payload_sha256"] == capture.raw_sha256
    assert ods["_query_sha256"] == capture.query_sha256
    assert all("CREATE TABLE" not in sql and "COUNT(" not in sql for sql in engine.sql_statements)

    dwd_sql = engine.sql_statements[-1]
    assert f"INSERT INTO `{dwd_table}`" in dwd_sql
    if source == "bls":
        dwd_bound = cast("list[dict[str, object]]", engine.dwd_chunks[0])[0]
        assert dwd_bound["year"] == 2025
        assert dwd_bound["period"] == "M13"
        assert dwd_bound["footnotes"] == '[{"code":"P","text":"Preliminary."}]'
        assert 'year":"2025"' in cast("str", ods["_payload"])
    if source == "fmp":
        dwd_bound = cast("list[dict[str, object]]", engine.dwd_chunks[0])[0]
        assert dwd_bound["volume"] == '{"kind":"int","value":"18446744073709551617"}'
        raw_json = json.loads(cast("str", ods["_payload"]))
        assert type(raw_json[0]["volume"]) is int and raw_json[0]["volume"] == 2**64 + 1
        assert math.copysign(1.0, raw_json[0]["change"]) == -1.0


def test_empty_result_still_writes_an_ods_audit_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _capture("fred", [])
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)
    assert (
        _call(
            engine,
            source="fred",
            model="FredSeries",
            capture=capture,
            rows=(),
        )
        == 0
    )
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert len(engine.ods_rows) == 1
    assert engine.ods_rows[0]["_raw_row_count"] == 0
    assert engine.ods_rows[0]["_dwd_row_count"] == 0
    assert engine.dwd_attempts == 0


def test_duplicate_raw_rows_can_be_kept_with_one_deduplicated_typed_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain, source, model, raw, rows = _provider_case("fmp")
    duplicated_raw = [*cast("list[dict[str, object]]", raw), *cast("list[dict[str, object]]", raw)]
    capture = _capture(source, duplicated_raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)
    assert (
        _call(
            engine,
            source=source,
            model=model,
            capture=capture,
            rows=rows,
        )
        == 1
    )
    assert engine.ods_rows[0]["_raw_row_count"] == 2
    assert engine.ods_rows[0]["_dwd_row_count"] == 1
    assert len(json.loads(cast("str", engine.ods_rows[0]["_payload"]))) == 2
    assert len(cast("list[dict[str, object]]", engine.dwd_chunks[0])) == 1
    assert any(f'"{domains.ods_table(domain, source)}"' in sql for sql in engine.sql_statements)


@pytest.mark.parametrize("source", ["fred", "bls", "fmp"])
def test_invalid_or_forged_inputs_fail_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    _domain, source, model, raw, rows = _provider_case(source)
    capture = _capture(source, raw)
    engine = RecordingEngine()
    inspected = _install_inspector(monkeypatch, engine)

    with pytest.raises(ValueError):
        _call(engine, source=source, model=model, capture=capture, rows=rows, batch_id="bad")
    assert engine.begin_calls == 0

    forged = replace(capture, raw_sha256="0" * 64)
    with pytest.raises(ValueError):
        _call(engine, source=source, model=model, capture=forged, rows=rows)
    assert engine.begin_calls == 0

    with pytest.raises(ValueError):
        _call(
            engine,
            source=source,
            model=model,
            capture=capture,
            rows=rows,
            ctx=_context(source, model, operation=RequestOperation.QUERY),
        )
    assert engine.begin_calls == 0

    with pytest.raises((RequestAuthorizationError, RequestBudgetError)):
        _call(engine, source=source, model=model, capture=capture, rows=rows, ctx=None)
    assert engine.begin_calls == 0
    assert inspected == []
    _assert_no_insert(engine)


@pytest.mark.parametrize(
    "mutation",
    [
        "ods_extra_column",
        "dwd_type",
        "dwd_missing",
        "ods_myisam",
        "dwd_myisam",
        "ods_unique",
        "ods_prefix",
    ],
)
def test_schema_or_engine_drift_rolls_back_before_any_insert(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    domain, source, model, raw, rows = _provider_case("fred")
    capture = _capture(source, raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)
    ods_table = domains.ods_table(domain, source)
    dwd_table = domains.dwd_table(domain)
    if mutation == "ods_extra_column":
        engine.extra_columns[ods_table] = [
            {"name": "unreviewed", "type": mysql.VARCHAR(12), "nullable": True, "default": None}
        ]
    elif mutation == "dwd_type":
        engine.column_overrides[(dwd_table, "value")] = {"type": mysql.BIGINT()}
    elif mutation == "dwd_missing":
        engine.missing_tables.add(dwd_table)
    elif mutation == "ods_myisam":
        engine.table_engines[ods_table] = "MyISAM"
    elif mutation == "dwd_myisam":
        engine.table_engines[dwd_table] = "MyISAM"
    elif mutation == "ods_unique":
        engine.unique_constraints[ods_table] = [
            {"name": "unreviewed_unique", "column_names": ["_source"]}
        ]
    else:
        next(row for row in engine.index_rows[ods_table] if row["INDEX_NAME"] == "PRIMARY")[
            "SUB_PART"
        ] = 12

    with pytest.raises(ProviderModelStoreError):
        _call(engine, source=source, model=model, capture=capture, rows=rows)
    assert engine.begin_calls == engine.rollback_calls == 1
    assert engine.commit_calls == 0
    _assert_no_insert(engine)


def test_reused_batch_id_is_append_only_and_duplicate_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    domain, source, model, raw, rows = _provider_case("fred")
    capture = _capture(source, raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)

    assert _call(engine, source=source, model=model, capture=capture, rows=rows) == 1
    assert engine.commit_calls == 1
    assert engine.persisted_batch_ids == {_BATCH_ID}

    with pytest.raises(ProviderModelStoreError, match="transaction failed"):
        _call(engine, source=source, model=model, capture=capture, rows=rows)
    assert engine.begin_calls == 2
    assert engine.commit_calls == 1
    assert engine.rollback_calls == 1
    assert engine.ods_insert_attempts == 2
    assert len(engine.ods_rows) == 1
    assert engine.dwd_attempts == 1
    assert any(f'"{domains.ods_table(domain, source)}"' in sql for sql in engine.sql_statements)


def test_second_dwd_chunk_failure_rolls_back_the_ods_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _domain, source, model, raw, _rows = _provider_case("fmp")
    capture = _capture(source, raw)
    base = date(2020, 1, 1)
    rows = tuple(
        EquityHistorical(
            symbol="BRK.B",
            date=base + timedelta(days=index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=index,
            change=None,
            change_percent=None,
            vwap=None,
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
        for index in range(1_001)
    )
    engine = RecordingEngine()
    engine.fail_dwd_chunk = 2
    _install_inspector(monkeypatch, engine)

    with pytest.raises(ProviderModelStoreError, match="transaction failed"):
        _call(engine, source=source, model=model, capture=capture, rows=rows)
    assert engine.begin_calls == engine.rollback_calls == 1
    assert engine.commit_calls == 0
    assert engine.ods_insert_attempts == 1
    assert len(engine.ods_rows) == 1
    assert engine.dwd_attempts == 2
    assert [len(cast("list[object]", chunk)) for chunk in engine.dwd_chunks] == [1_000, 1]
    assert engine.events == ["ods_insert", "dwd_upsert", "dwd_upsert"]


def test_cancellation_after_ods_insert_rolls_back_without_dwd_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _domain, source, model, raw, rows = _provider_case("fred")
    capture = _capture(source, raw)
    cancelled = threading.Event()
    engine = RecordingEngine()
    engine.cancel_after_ods = cancelled
    _install_inspector(monkeypatch, engine)

    with pytest.raises(RequestBudgetError):
        _call(
            engine,
            source=source,
            model=model,
            capture=capture,
            rows=rows,
            ctx=_context(source, model, cancel=cancelled),
        )
    assert engine.begin_calls == engine.rollback_calls == 1
    assert engine.commit_calls == 0
    assert engine.ods_insert_attempts == 1
    assert engine.dwd_attempts == 0


def test_batch_writer_checks_both_tables_before_insert_and_uses_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _domain, source, model, raw, rows = _provider_case("fred")
    capture = _capture(source, raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)
    assert _call(engine, source=source, model=model, capture=capture, rows=rows) == 1
    assert engine.begin_calls == engine.commit_calls == 1
    assert len(engine.metadata_queries) == 2
    assert len(engine.engine_queries) == 2
    tables_checked = [parameters["table"] for _sql, parameters in engine.metadata_queries]
    assert tables_checked == [domains.ods_table("fred_series", "fred"), "dwd_fred_series"]
    assert engine.events == ["ods_insert", "dwd_upsert"]
    insert_positions = [
        index for index, sql in enumerate(engine.sql_statements) if sql.startswith("INSERT")
    ]
    assert len(insert_positions) == 2
    assert max(
        index
        for index, sql in enumerate(engine.sql_statements)
        if "INFORMATION_SCHEMA.STATISTICS" in sql or "INFORMATION_SCHEMA.TABLES" in sql
    ) < min(insert_positions)


def test_invalid_registered_pair_and_naive_timestamp_fail_without_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _domain, source, model, raw, rows = _provider_case("fred")
    capture = _capture(source, raw)
    engine = RecordingEngine()
    _install_inspector(monkeypatch, engine)
    registry = _registry("fred")
    with pytest.raises(ProviderModelStoreError):
        _call(
            engine,
            source="bls",
            model=model,
            capture=capture,
            rows=rows,
            registry=registry,
        )
    assert engine.begin_calls == 0

    with pytest.raises(ValueError, match="timezone-aware"):
        provider_model_store.write_provider_model_batch(
            engine=cast("Any", engine),
            registry=registry,
            source=source,
            model=model,
            capture=capture,
            rows=cast("Sequence[Any]", rows),
            observed_at=datetime(2026, 10, 8),
            batch_id=_BATCH_ID,
            ctx=_context(source, model),
        )
    assert engine.begin_calls == 0
