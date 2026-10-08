"""Offline bounded snapshot-export tests with a recording MySQL boundary."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from sqlalchemy.dialects import mysql

from opendata.data import domains
from opendata.data import request_budget as request_budget_module
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
from opendata.services import provider_model_export, provider_model_store
from opendata.services.provider_model_export import (
    NativeModelExportArtifact,
    ProviderModelExportError,
    ProviderModelExportForbiddenError,
    ProviderModelExportLimitError,
    ProviderModelExportValidationError,
    export_provider_model_snapshot,
)

_NOW = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)
_CASES = {
    "fred": ("FredSeries", "fred_series"),
    "bls": ("BlsSeries", "bls_series"),
    "fmp": ("EquityHistorical", "equity_historical"),
}


def _grant(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.EXPORT,
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
        rights_evidence="offline-test-export-rights-record",
        task_attempts=0,
        source_attempts=0,
        allowed_hosts=(host,),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=1),
        conditions=conditions,
    )


def _context(
    source: str = "fred",
    model: str = "FredSeries",
    *,
    grants: Sequence[RequestGrant] | None = None,
    operation: RequestOperation = RequestOperation.EXPORT,
    timeout: float | None = 30.0,
    deadline: float | None = None,
    cancellation: threading.Event | None = None,
) -> FetchContext:
    selected_grants = tuple(grants) if grants is not None else (_grant(source, model),)
    return FetchContext(
        timeout=timeout,
        _deadline_monotonic=deadline,
        _thread_cancel_event=cancellation,
        request_budget=RequestBudget(
            task_attempts=0,
            source_attempts=0,
            grants=selected_grants,
        ),
        operation=operation,
    )


class Principal:
    def __init__(self, allowed_domains: Sequence[str] = ()) -> None:
        self.allowed_domains = frozenset(allowed_domains)
        self.calls: list[str] = []

    def allows_domain(self, domain: str) -> bool:
        self.calls.append(domain)
        return domain in self.allowed_domains


def _registry(source: str) -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider(source, registry)
    return registry


def _rows(source: str) -> tuple[Sequence[object], list[dict[str, object]]]:
    domain = _CASES[source][1]
    if source == "fred":
        rows: Sequence[object] = (
            SeriesObservation(
                series_id="CPIAUCSL",
                date=date(2024, 2, 1),
                value=None,
                realtime_start=date(2024, 1, 1),
                realtime_end=date(2024, 2, 1),
                transform_units="lin",
                output_type=1,
                requested_frequency=None,
                requested_aggregation_method="avg",
            ),
            SeriesObservation(
                series_id="CPIAUCSL",
                date=date(2024, 2, 1),
                value=3.25,
                realtime_start=date(2024, 2, 2),
                realtime_end=date(9999, 12, 31),
                transform_units="lin",
                output_type=1,
                requested_frequency="m",
                requested_aggregation_method="avg",
            ),
        )
    elif source == "bls":
        rows = (
            BlsObservation(
                series_id="LNS14000000",
                year=2025,
                period="M13",
                period_name="Annual average",
                value=4.1,
                footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
                latest=None,
                preliminary=True,
                api_version="v2",
            ),
        )
    else:
        rows = (
            EquityHistorical(
                symbol="BRK.B",
                date=date(2026, 1, 2),
                open=100.0,
                high=103.0,
                low=99.5,
                close=102.0,
                volume=2**256 + 17,
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

    records: list[dict[str, object]] = []
    for row in rows:
        record = provider_model_codec.encode_model_storage_row(domain, cast("Any", row))
        if domain == "bls_series":
            record["latest"] = None if row.latest is None else int(row.latest)  # type: ignore[attr-defined]
            record["preliminary"] = int(row.preliminary)  # type: ignore[attr-defined]
        elif domain == "equity_historical":
            record["adj_close_provided"] = 0
        record.update(
            {
                "source": source,
                "_merged_at": _NOW.replace(tzinfo=None),
                "_diff_flag": 0,
                "_as_of": None,
            }
        )
        records.append(record)
    return rows, records


def _dwd_columns(domain: str) -> tuple[ddl.Column, ...]:
    return (
        *provider_model_schema.model_storage_columns(domain),
        *ddl.DWD_TRACE_COLUMNS[:-1],
        ddl.Column("_as_of", "date", nullable=True),
    )


def _mysql_type(sql_type: str) -> Any:
    varchar = re.match(r"^varchar\((\d+)\)", sql_type, re.IGNORECASE)
    if varchar is not None:
        width = int(varchar.group(1))
        kwargs: dict[str, object] = {}
        charset = re.search(r"CHARACTER\s+SET\s+(\w+)", sql_type, re.IGNORECASE)
        collation = re.search(r"COLLATE\s+(\w+)", sql_type, re.IGNORECASE)
        if charset is not None:
            kwargs["charset"] = charset.group(1)
        if collation is not None:
            kwargs["collation"] = collation.group(1)
        return mysql.VARCHAR(width, **kwargs)
    normalized = sql_type.lower()
    types: dict[str, Any] = {
        "date": mysql.DATE(),
        "datetime": mysql.DATETIME(),
        "double": mysql.DOUBLE(),
        "bigint": mysql.BIGINT(),
        "json": mysql.JSON(),
        "tinyint(1)": mysql.TINYINT(1),
    }
    if normalized not in types:
        raise AssertionError(f"unexpected reviewed DWD type: {sql_type}")
    return types[normalized]


def _primary_index_rows(domain: str) -> list[dict[str, object]]:
    return [
        {
            "INDEX_NAME": "PRIMARY",
            "NON_UNIQUE": 0,
            "SEQ_IN_INDEX": position,
            "COLUMN_NAME": field_name,
            "SUB_PART": None,
            "EXPRESSION": None,
            "INDEX_TYPE": "BTREE",
        }
        for position, field_name in enumerate(
            provider_model_codec.physical_model_key(domain),
            start=1,
        )
    ]


class RecordingInspector:
    def __init__(self, domain: str) -> None:
        self.columns = [
            {
                "name": column.name,
                "type": _mysql_type(column.sql_type),
                "nullable": column.nullable,
                "default": column.sql_default,
            }
            for column in _dwd_columns(domain)
        ]
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
    def __init__(
        self,
        engine: RecordingEngine,
        rows: Sequence[Mapping[str, object]],
        *,
        is_select: bool = False,
    ) -> None:
        self.engine = engine
        self.rows = [dict(row) for row in rows]
        self.index = 0
        self.is_select = is_select
        self.closed = False

    def mappings(self) -> RecordingResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        self.engine.fetchmany_sizes.append(size)
        self.index += 1
        if self.engine.fetchmany_hook is not None:
            self.engine.fetchmany_hook(self.index)
        batch = self.rows[self.index - 1 : self.index - 1 + size]
        return batch

    def close(self) -> None:
        self.closed = True
        if self.is_select:
            self.engine.events.append("cursor_close")


class RecordingConnection:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine
        self.dialect = mysql.dialect()

    def execution_options(self, **options: object) -> RecordingConnection:
        self.engine.connection_options.append(dict(options))
        return self

    def get_isolation_level(self) -> str:
        self.engine.events.append("get_isolation_level")
        return self.engine.actual_isolation

    def begin(self) -> RecordingTransaction:
        return RecordingTransaction(self.engine)

    def execute(self, statement: object, parameters: object = None) -> RecordingResult:
        sql = str(statement)
        if not self.engine.in_transaction:
            raise AssertionError("export validation and SELECT must share one transaction")
        bound = dict(parameters) if isinstance(parameters, Mapping) else {}
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            self.engine.metadata_queries.append((sql, bound))
            return RecordingResult(self.engine, self.engine.index_rows)
        if "INFORMATION_SCHEMA.TABLES" in sql:
            self.engine.metadata_queries.append((sql, bound))
            table = bound["table"]
            return RecordingResult(
                self.engine,
                [{"TABLE_NAME": table, "ENGINE": self.engine.table_engines.get(table, "InnoDB")}],
            )
        if not sql.lstrip().startswith("SELECT "):
            raise AssertionError("export issued non-SELECT SQL")
        self.engine.selects.append((sql, bound))
        self.engine.select_options.append(dict(getattr(statement, "_execution_options", {})))
        if self.engine.fail_select:
            raise RuntimeError("database error containing a private DSN")
        result = RecordingResult(self.engine, self.engine.rows, is_select=True)
        self.engine.result = result
        return result


class RecordingTransaction:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine

    def __enter__(self) -> RecordingConnection:
        self.engine.transaction_calls += 1
        self.engine.in_transaction = True
        self.engine.events.append("transaction_begin")
        return self.engine.connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> Literal[False]:
        if (
            self.engine.result is not None
            and self.engine.result.is_select
            and not self.engine.result.closed
        ):
            raise AssertionError("streaming result must close before transaction exit")
        self.engine.in_transaction = False
        transaction_event = "transaction_commit" if exc_type is None else "transaction_rollback"
        self.engine.events.append(transaction_event)
        if exc_type is None:
            self.engine.commit_calls += 1
        else:
            self.engine.rollback_calls += 1
        return False


class RecordingConnectionContext:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine
        self.connection = engine.connection

    def __enter__(self) -> RecordingConnection:
        self.engine.connect_calls += 1
        return self.connection

    def __exit__(self, *_exc_info: object) -> None:
        self.engine.connection_close_calls += 1
        self.engine.events.append("connection_close")


class RecordingEngine:
    def __init__(
        self,
        domain: str,
        rows: Sequence[Mapping[str, object]] = (),
        *,
        dialect_name: str = "mysql",
        actual_isolation: str = "REPEATABLE READ",
    ) -> None:
        self.domain = domain
        self.dialect = SimpleNamespace(name=dialect_name)
        self.connection = RecordingConnection(self)
        self.rows = [dict(row) for row in rows]
        self.actual_isolation = actual_isolation
        self.index_rows = _primary_index_rows(domain)
        self.table_engines: dict[str, str] = {}
        self.inspector = RecordingInspector(domain)
        self.connect_calls = 0
        self.connection_close_calls = 0
        self.transaction_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.in_transaction = False
        self.connection_options: list[dict[str, object]] = []
        self.metadata_queries: list[tuple[str, dict[str, object]]] = []
        self.selects: list[tuple[str, dict[str, object]]] = []
        self.select_options: list[dict[str, object]] = []
        self.fetchmany_sizes: list[int] = []
        self.events: list[str] = []
        self.result: RecordingResult | None = None
        self.fetchmany_hook: Callable[[int], None] | None = None
        self.fail_select = False

    def connect(self) -> RecordingConnectionContext:
        return RecordingConnectionContext(self)


def _install_inspector(
    monkeypatch: pytest.MonkeyPatch,
    engine: RecordingEngine,
) -> None:
    def fake_inspect(connection: object) -> RecordingInspector:
        assert isinstance(connection, RecordingConnection)
        assert connection.engine is engine
        assert connection.engine.in_transaction
        return engine.inspector

    monkeypatch.setattr(provider_model_store, "inspect", fake_inspect)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    source: str = "fred",
    *,
    rows: Sequence[Mapping[str, object]] | None = None,
    engine: RecordingEngine | None = None,
) -> tuple[ProviderRegistry, RecordingEngine, Sequence[object], list[dict[str, object]]]:
    domain = _CASES[source][1]
    typed_rows, default_records = _rows(source)
    selected_engine = engine or RecordingEngine(
        domain,
        default_records if rows is None else rows,
    )
    _install_inspector(monkeypatch, selected_engine)
    return _registry(source), selected_engine, typed_rows, default_records


def _factory(engine: RecordingEngine, calls: list[int] | None = None) -> Callable[[], Any]:
    def create() -> RecordingEngine:
        if calls is not None:
            calls.append(1)
        return engine

    return create


def _tracked_tempdirs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Path]:
    created: list[Path] = []
    original = tempfile.TemporaryDirectory

    def create(*args: object, **kwargs: object) -> tempfile.TemporaryDirectory[str]:
        owner = original(*args, **kwargs)  # type: ignore[arg-type]
        created.append(Path(owner.name))
        return owner

    monkeypatch.setattr(provider_model_export.tempfile, "TemporaryDirectory", create)
    return created


def _call_export(
    engine: RecordingEngine,
    registry: ProviderRegistry,
    *,
    source: str = "fred",
    model: str | None = None,
    principal: Principal | None = None,
    ctx: FetchContext | None = None,
    **kwargs: object,
) -> NativeModelExportArtifact:
    chosen_model, domain = _CASES[source]
    return export_provider_model_snapshot(
        engine_factory=cast("Any", _factory(engine)),
        registry=registry,
        source=source,
        model=chosen_model if model is None else model,
        principal=principal or Principal((domain,)),
        ctx=ctx if ctx is not None else _context(source, chosen_model),
        **kwargs,
    )


@pytest.mark.parametrize("source", ["fred", "bls", "fmp"])
def test_all_native_profiles_export_complete_typed_snapshot_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    registry, engine, typed_rows, _records = _setup(monkeypatch, source)
    artifact = _call_export(engine, registry, source=source, max_records=20)
    try:
        with artifact.open_binary() as stream:
            payload = stream.read()
        assert artifact.byte_count == len(payload)
        assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
        assert artifact.row_count == len(typed_rows)
        assert artifact.snapshot_complete is True
        assert artifact.completeness == "NOT_ASSESSED"
        assert artifact.consistency == "single_repeatable_read_transaction"
        assert artifact.created_at.tzinfo is timezone.utc
        assert stat.S_IMODE(artifact._path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(artifact._path.stat().st_mode) == 0o600
        assert str(artifact._path) not in repr(artifact)

        lines = payload.splitlines()
        decoded = [json.loads(line) for line in lines]
        metadata = decoded[0]
        assert metadata == {
            "kind": "metadata",
            "schema_version": 1,
            "source": source,
            "model": _CASES[source][0],
            "domain": _CASES[source][1],
            "verified": artifact.verified,
            "created_at": artifact.created_at.isoformat().replace("+00:00", "Z"),
            "consistency": "single_repeatable_read_transaction",
            "completeness": "NOT_ASSESSED",
        }
        assert [item["kind"] for item in decoded] == [
            "metadata",
            *("row" for _ in typed_rows),
            "summary",
        ]
        assert [item["data"] for item in decoded[1:-1]] == [
            cast("Any", row).model_dump(mode="json", by_alias=False) for row in typed_rows
        ]
        assert decoded[-1] == {
            "kind": "summary",
            "rows": len(typed_rows),
            "snapshot_complete": True,
        }

        if source == "fred":
            assert decoded[1]["data"]["value"] is None
            assert decoded[1]["data"]["requested_frequency"] is None
            assert decoded[2]["data"]["realtime_start"] == "2024-02-02"
        elif source == "bls":
            assert decoded[1]["data"]["year"] == 2025
            assert decoded[1]["data"]["period"] == "M13"
            assert decoded[1]["data"]["footnotes"] == [
                {"code": "P", "text": "Preliminary."},
                {"code": None, "text": None},
            ]
        else:
            fmp_data = decoded[1]["data"]
            assert fmp_data["volume"] == 2**256 + 17
            assert type(fmp_data["volume"]) is int
            assert fmp_data["currency"] is None and fmp_data["volume_unit"] is None
            assert fmp_data["adj_close_provided"] is False
            assert math.copysign(1.0, fmp_data["change"]) == -1.0

        assert engine.connection_options == [{"isolation_level": "REPEATABLE READ"}]
        assert engine.transaction_calls == engine.commit_calls == 1
        assert engine.rollback_calls == 0
        assert engine.connect_calls == engine.connection_close_calls == 1
        assert len(engine.selects) == 1
        assert engine.fetchmany_sizes == [1] * (len(typed_rows) + 1)
        assert engine.select_options == [
            {"stream_results": True, "max_row_buffer": 1, "yield_per": 1}
        ]
        assert engine.events.index("cursor_close") < engine.events.index("transaction_commit")
        assert engine.selects[0][1]["limit"] == 21
        assert engine.selects[0][1]["offset"] == 0
        assert "ORDER BY" in engine.selects[0][0]
        assert "LIMIT :limit OFFSET :offset" in engine.selects[0][0]
    finally:
        artifact.cleanup()
        artifact.cleanup()
    assert not artifact._path.parent.exists()
    with pytest.raises(ProviderModelExportError, match="cleaned up"):
        artifact.open_binary()


def test_export_uses_one_bound_stable_select_without_user_offset_or_sql_literals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, engine, _rows_result, _records = _setup(monkeypatch)
    injection = "CPIAUCSL' OR 1=1 --"
    artifact = _call_export(
        engine,
        registry,
        filters={"series_id": injection, "requested_frequency": None},
        start=date(2024, 2, 1),
        end=date(2024, 2, 29),
        max_records=5,
    )
    sql, bound = engine.selects[0]
    key = provider_model_codec.physical_model_key("fred_series")
    assert f"ORDER BY {', '.join(f'`{name}`' for name in key)}" in sql
    assert "OFFSET :offset" in sql
    assert injection not in sql
    assert bound == {
        "limit": 6,
        "offset": 0,
        "filter_1": injection,
        "start": date(2024, 2, 1),
        "end": date(2024, 2, 29),
    }
    assert len(engine.selects) == 1
    artifact.cleanup()


def test_empty_snapshot_has_metadata_and_eof_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "bls"
    domain = _CASES[source][1]
    registry, engine, _typed, _records = _setup(monkeypatch, source, rows=())
    artifact = _call_export(engine, registry, source=source)
    try:
        with artifact.open_binary() as stream:
            lines = [json.loads(line) for line in stream.read().splitlines()]
        assert [line["kind"] for line in lines] == ["metadata", "summary"]
        assert lines[-1] == {"kind": "summary", "rows": 0, "snapshot_complete": True}
        assert artifact.domain == domain and artifact.row_count == 0
        assert engine.fetchmany_sizes == [1]
    finally:
        artifact.cleanup()


@pytest.mark.parametrize(
    ("ctx", "source", "model", "principal", "expected_error"),
    [
        (None, "fred", "FredSeries", Principal(("fred_series",)), RequestAuthorizationError),
        (
            _context(operation=RequestOperation.QUERY),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            ProviderModelExportValidationError,
        ),
        (
            _context(grants=(_grant("fred", "FredSeries", operation=RequestOperation.QUERY),)),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            RequestAuthorizationError,
        ),
        (
            _context(
                grants=(
                    _grant(
                        "fred",
                        "FredSeries",
                        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    ),
                ),
            ),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            RequestAuthorizationError,
        ),
        (
            _context(grants=(_grant("fred", "FredSeries", conditions=("review",)),)),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            RequestAuthorizationError,
        ),
        (
            _context(grants=(_grant("fred", "FredSeries", decision=GrantDecision.DENIED),)),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            RequestAuthorizationError,
        ),
        (
            _context("fmp", "EquityHistorical"),
            "fred",
            "FredSeries",
            Principal(("fred_series",)),
            RequestAuthorizationError,
        ),
        (
            _context("fmp", "EquityQuote"),
            "fmp",
            "EquityQuote",
            Principal(("equity_historical",)),
            ProviderModelExportValidationError,
        ),
        (
            _context(),
            "fred",
            "FredSeries",
            Principal(()),
            ProviderModelExportForbiddenError,
        ),
    ],
)
def test_authorization_identity_and_acl_fail_before_engine_or_spool(
    monkeypatch: pytest.MonkeyPatch,
    ctx: FetchContext | None,
    source: str,
    model: str,
    principal: Principal,
    expected_error: type[Exception],
) -> None:
    engine_calls: list[int] = []
    temp_paths = _tracked_tempdirs(monkeypatch)

    def forbidden_factory() -> RecordingEngine:
        engine_calls.append(1)
        raise AssertionError("engine factory ran before authorization")

    registry = _registry("fmp" if source == "fmp" else "fred")
    with pytest.raises(expected_error):
        export_provider_model_snapshot(
            engine_factory=cast("Any", forbidden_factory),
            registry=registry,
            source=source,
            model=model,
            principal=principal,
            ctx=ctx,
        )
    assert engine_calls == []
    assert temp_paths == []


def test_custom_context_without_timeout_gets_thirty_second_bound() -> None:
    context = _context(timeout=None)
    trusted = provider_model_export._trusted_export_context(context)
    assert trusted.operation is RequestOperation.EXPORT
    assert trusted.timeout == 30.0
    assert trusted.request_budget is context.request_budget
    assert provider_model_export._trusted_export_context(None).request_budget is None


@pytest.mark.parametrize(
    ("max_records", "max_bytes"),
    [
        (True, 1024),
        (0, 1024),
        (200_001, 1024),
        (10, 0),
        (10, 64 * 1024 * 1024 + 1),
    ],
)
def test_invalid_resource_limits_fail_before_engine_and_spool(
    monkeypatch: pytest.MonkeyPatch,
    max_records: object,
    max_bytes: object,
) -> None:
    registry = _registry("fred")
    calls: list[int] = []
    temp_paths = _tracked_tempdirs(monkeypatch)

    def factory() -> RecordingEngine:
        calls.append(1)
        raise AssertionError("invalid resource limits must fail before engine")

    with pytest.raises(ProviderModelExportValidationError):
        export_provider_model_snapshot(
            engine_factory=cast("Any", factory),
            registry=registry,
            source="fred",
            model="FredSeries",
            principal=Principal(("fred_series",)),
            ctx=_context(),
            max_records=cast("Any", max_records),
            max_bytes=cast("Any", max_bytes),
        )
    assert calls == [] and temp_paths == []


@pytest.mark.parametrize("failure", ["isolation", "schema", "unique", "prefix", "engine"])
def test_database_preflight_failure_creates_no_spool(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    registry, engine, _typed, _records = _setup(monkeypatch)
    temp_paths = _tracked_tempdirs(monkeypatch)
    if failure == "isolation":
        engine.actual_isolation = "READ COMMITTED"
    elif failure == "schema":
        engine.inspector.columns[0]["nullable"] = True
    elif failure == "unique":
        engine.inspector.unique_constraints.append(
            {"name": "unexpected", "column_names": ["value"]}
        )
    elif failure == "prefix":
        engine.index_rows[0]["SUB_PART"] = 12
    elif failure == "engine":
        table = domains.dwd_table("fred_series")
        engine.table_engines[table] = "MyISAM"

    with pytest.raises(ProviderModelExportError):
        _call_export(engine, registry)
    assert temp_paths == []
    assert engine.commit_calls == 0 and engine.rollback_calls == 1
    assert engine.selects == []


def test_max_records_fetches_one_extra_then_fails_without_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _typed_rows, records = _rows("fred")
    registry, engine, _typed, _records = _setup(monkeypatch, "fred", rows=records + records[:1])
    temp_paths = _tracked_tempdirs(monkeypatch)
    with pytest.raises(ProviderModelExportLimitError, match="row limit"):
        _call_export(engine, registry, max_records=2)
    assert engine.selects[0][1]["limit"] == 3
    assert engine.selects[0][1]["offset"] == 0
    assert engine.fetchmany_sizes == [1, 1, 1]
    assert engine.result is not None and engine.result.closed
    assert engine.events.index("cursor_close") < engine.events.index("transaction_rollback")
    assert engine.rollback_calls == 1 and engine.commit_calls == 0
    assert len(temp_paths) == 1 and not temp_paths[0].exists()


def test_late_bad_row_closes_cursor_rolls_back_and_removes_partial_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _typed, records = _rows("fred")
    bad_record = dict(records[1])
    bad_record["source"] = "bls"
    registry, engine, _typed_rows, _records = _setup(
        monkeypatch,
        "fred",
        rows=(records[0], bad_record),
    )
    temp_paths = _tracked_tempdirs(monkeypatch)
    with pytest.raises(ProviderModelExportError, match="source"):
        _call_export(engine, registry)
    assert engine.fetchmany_sizes == [1, 1]
    assert engine.result is not None and engine.result.closed
    assert engine.rollback_calls == 1 and engine.commit_calls == 0
    assert len(temp_paths) == 1 and not temp_paths[0].exists()


def test_total_byte_budget_includes_header_rows_and_summary_and_cleans_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, success_engine, _rows_result, _records = _setup(monkeypatch)
    complete = _call_export(success_engine, registry)
    with complete.open_binary() as stream:
        metadata_size = len(stream.readline())
    complete.cleanup()

    registry, engine, _typed, _records = _setup(monkeypatch)
    temp_paths = _tracked_tempdirs(monkeypatch)
    with pytest.raises(ProviderModelExportLimitError, match="byte limit"):
        _call_export(engine, registry, max_bytes=metadata_size + 1)
    assert engine.selects
    assert engine.result is not None and engine.result.closed
    assert engine.rollback_calls == 1
    assert len(temp_paths) == 1 and not temp_paths[0].exists()


def test_sql_failure_is_sanitized_and_private_file_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, engine, _typed, _records = _setup(monkeypatch)
    engine.fail_select = True
    temp_paths = _tracked_tempdirs(monkeypatch)
    with pytest.raises(ProviderModelExportError) as exc_info:
        _call_export(engine, registry)
    assert "private DSN" not in str(exc_info.value)
    assert "SELECT" in str(exc_info.value)
    assert engine.rollback_calls == 1 and engine.commit_calls == 0
    assert len(temp_paths) == 1 and not temp_paths[0].exists()


@pytest.mark.parametrize("expiry_or_cancel", ["cancel", "grant_expiry", "deadline"])
def test_cancellation_and_expiry_close_stream_and_remove_owned_file(
    monkeypatch: pytest.MonkeyPatch,
    expiry_or_cancel: str,
) -> None:
    _typed, records = _rows("fred")
    registry, engine, _typed_rows, _records = _setup(
        monkeypatch,
        "fred",
        rows=records,
    )
    temp_paths = _tracked_tempdirs(monkeypatch)
    cancellation = threading.Event()
    context = _context(cancellation=cancellation)
    if expiry_or_cancel == "cancel":
        engine.fetchmany_hook = lambda index: cancellation.set() if index == 2 else None
        expected_error: type[Exception] = RequestExecutionCancelledError
    elif expiry_or_cancel == "grant_expiry":
        real_datetime = datetime
        expired = {"value": False}

        class ControlledDateTime(real_datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                if expired["value"]:
                    return grant.expires_at + timedelta(seconds=1)
                return real_datetime.now(cast("Any", tz))

        grant = context.request_budget.grants[0]  # type: ignore[union-attr]
        monkeypatch.setattr(request_budget_module, "datetime", ControlledDateTime)
        engine.fetchmany_hook = lambda index: expired.update(value=True) if index == 2 else None
        expected_error = RequestAuthorizationError
    else:
        context = _context(timeout=30.0)
        budget = context.request_budget
        assert budget is not None
        clock_value = {"now": 1000.0}
        monkeypatch.setattr(time, "monotonic", lambda: clock_value["now"])
        engine.fetchmany_hook = lambda index: clock_value.update(now=1006.0) if index == 2 else None
        with (
            request_execution_scope(
                source="fred",
                canonical_model="FredSeries",
                operation=RequestOperation.EXPORT,
                budget=budget,
                deadline=1005.0,
            ),
            pytest.raises(RequestExecutionDeadlineError),
        ):
            _call_export(engine, registry, ctx=context)
        assert engine.result is not None and engine.result.closed
        assert engine.rollback_calls == 1
        assert len(temp_paths) == 1 and not temp_paths[0].exists()
        return

    with pytest.raises(expected_error):
        _call_export(engine, registry, ctx=context)
    assert engine.result is not None and engine.result.closed
    assert engine.events.index("cursor_close") < engine.events.index("transaction_rollback")
    assert engine.rollback_calls == 1 and engine.commit_calls == 0
    assert len(temp_paths) == 1 and not temp_paths[0].exists()


def test_domain_export_permission_is_required_before_engine_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry("fred")
    original = domains.require_domain_semantics
    monkeypatch.setattr(
        domains,
        "require_domain_semantics",
        lambda domain: original(domain).model_copy(update={"permissions": ("query", "store")}),
    )
    calls: list[int] = []
    temp_paths = _tracked_tempdirs(monkeypatch)

    def factory() -> RecordingEngine:
        calls.append(1)
        raise AssertionError("missing export permission must fail before engine")

    with pytest.raises(ProviderModelExportError, match="export contract"):
        export_provider_model_snapshot(
            engine_factory=cast("Any", factory),
            registry=registry,
            source="fred",
            model="FredSeries",
            principal=Principal(("fred_series",)),
            ctx=_context(),
        )
    assert calls == [] and temp_paths == []


def test_invalid_filters_and_ranges_fail_before_engine_or_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry("fred")
    calls: list[int] = []
    temp_paths = _tracked_tempdirs(monkeypatch)

    def factory() -> RecordingEngine:
        calls.append(1)
        raise AssertionError("invalid request must fail before engine")

    with pytest.raises(ValueError):
        export_provider_model_snapshot(
            engine_factory=cast("Any", factory),
            registry=registry,
            source="fred",
            model="FredSeries",
            principal=Principal(("fred_series",)),
            filters={"_request_context": "[]"},
            start=datetime(2024, 1, 1),  # type: ignore[arg-type]
            ctx=_context(),
        )
    assert calls == [] and temp_paths == []


def test_temp_file_and_directory_are_mode_0600_and_0700_then_cleanup_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, engine, _typed, _records = _setup(monkeypatch)
    artifact = _call_export(engine, registry)
    parent = artifact._path.parent
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact._path.stat().st_mode) == 0o600
    artifact.cleanup()
    artifact.cleanup()
    assert not parent.exists()


def test_artifact_roundtrip_hash_and_bytes_cover_the_exact_ndjson_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, engine, _typed, _records = _setup(monkeypatch)
    artifact = _call_export(engine, registry)
    try:
        with artifact.open_binary() as stream:
            content = stream.read()
        assert artifact.byte_count == os.path.getsize(artifact._path)
        assert artifact.sha256 == hashlib.sha256(content).hexdigest()
        assert content.endswith(b'"snapshot_complete":true}\n')
    finally:
        artifact.cleanup()
