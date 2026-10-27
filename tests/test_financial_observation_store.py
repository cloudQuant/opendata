"""Exercise SQL archive behavior with synthetic evidence and isolated databases."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

import opendata.pipeline.financial_observation_store as store
from opendata.data.providers.ths.endpoints import financial_statement_items
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeResult,
    FinancialObservationSourceReference,
    decode_financial_observation_body,
)
from opendata.pipeline.financial_observation_store import (
    FinancialObservationCaptureContext,
    FinancialObservationStoreConflictError,
    FinancialObservationStoreError,
    FinancialObservationStoreReceipt,
    FinancialObservationStoreTransactionError,
    PreparedCapture,
    _content_addressed_key,
    persist_prepared_financial_observation_capture,
    prepare_financial_observation_capture,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter, RawResponseSource
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunLeaseExpiredError,
    RunLedger,
    RunRecord,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT
    / "alembic_financial_archive"
    / "versions"
    / "20261027-0001_financial_observation_archive.py"
)
_CAPTURES = "od_financial_captures"
_OBSERVATIONS = "od_financial_observations"
_LINKS = "od_financial_capture_observation_links"
_QUARANTINE = "od_financial_observation_quarantine"
_SOURCE = RawResponseSource(
    provider_id="ths",
    dataset_id="ths_financial_statement",
    operation_id="income_annual",
)
_REQUEST_SHA256 = "b" * 64
_UNIT_SHA256 = "a" * 64
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MYSQL_ACCEPTANCE_GATE = "OD_FINANCIAL_ARCHIVE_ACCEPTANCE"
_MYSQL_ACCEPTANCE_SCHEMA = re.compile(r"^odfa_[0-9a-f]{16}$")
_MYSQL_ACCEPTANCE_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MYSQL_ACCEPTANCE_TABLES = frozenset(
    {_CAPTURES, _OBSERVATIONS, _LINKS, _QUARANTINE, "alembic_version_financial_archive"}
)
_MYSQL_CONNECT_TIMEOUT_SECONDS = 2
_MYSQL_SOCKET_IO_TIMEOUT_SECONDS = 6
_MYSQL_LOCK_WAIT_TIMEOUT_SECONDS = 5
_MYSQL_LOCK_OBSERVATION_TIMEOUT_SECONDS = 9
_MYSQL_WORKER_JOIN_TIMEOUT_SECONDS = 14
_MYSQL_WORKER_ABORT_JOIN_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class _Fixture:
    """One writer-backed response, decoded inputs, and an offline prepared DTO."""

    context: FinancialObservationCaptureContext
    reader: RawResponseArtifactReader
    manifest_sha256: str
    expected_http_status: int
    body: bytes
    result: FinancialObservationDecodeResult
    artifact_path: Path
    manifest_path: Path
    prepared: PreparedCapture


class _ReaderSubclass(RawResponseArtifactReader):
    """Untrusted subtype used to verify the store requires the exact reader."""


def _load_migration() -> ModuleType:
    """Load the isolated migration module without loading an application config."""
    spec = importlib.util.spec_from_file_location(
        "financial_archive_store_test_migration", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _engine(tmp_path: Path) -> Engine:
    """Create only a disposable SQLite database accepted by the archive migration."""
    database_path = tmp_path / "test_financial_archive_store.sqlite"
    engine = sa.create_engine(f"sqlite:///{database_path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    migration = _load_migration()
    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            migration.upgrade()
    return engine


def _fixture(
    artifact_root: Path,
    *,
    observed_at: datetime | None = None,
    request_id: str = "synthetic-request-001",
    changed_value: str = "12345678901234567890.123456789012345678",
    include_disclosure_date: bool = True,
    disclosure_timestamp_ms: int | None = None,
    include_quarantined_row: bool = False,
) -> _Fixture:
    """Create an offline synthetic response, verified raw artifacts, and decode result."""
    instant = observed_at or (datetime.now(timezone.utc) - timedelta(seconds=5))
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("fixture observed_at must be aware UTC")
    items = financial_statement_items("income")
    row: dict[str, object] = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": int(datetime(2024, 12, 31, tzinfo=_SHANGHAI).timestamp() * 1000),
        "currency": "CNY",
    }
    if include_disclosure_date:
        row["report_date_ms"] = (
            disclosure_timestamp_ms
            if disclosure_timestamp_ms is not None
            else int(datetime(2025, 3, 31, tzinfo=_SHANGHAI).timestamp() * 1000)
        )
    for item_code in items:
        row[item_code] = "0"
    row[items[0]] = changed_value
    if len(items) > 1:
        row[items[1]] = None
    rows = [row]
    if include_quarantined_row:
        quarantined_row = dict(row)
        quarantined_row.pop("report_date_ms", None)
        rows.append(quarantined_row)
    body = json.dumps(
        {
            "code": 0,
            "message": "ok",
            "request_id": request_id,
            "data": {"timestamp": row["period_end_ms"], "item": rows},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    decoded = decode_financial_observation_body(
        body,
        expected_instrument_id="600519.SH",
        statement_type="income",
        periodicity="annual",
        observed_at=instant,
        native_units=dict.fromkeys(items, "CNY"),
        native_unit_mapping_version_declared="operator-unit-v1",
        native_unit_mapping_sha256_declared=_UNIT_SHA256,
    )
    writer = RawResponseArtifactWriter(artifact_root)
    captured = writer.capture(
        [body],
        source=_SOURCE,
        request_summary_sha256=_REQUEST_SHA256,
        http_status=200,
        retention_class="restricted",
        source_history_limit="unknown",
    )
    manifest_sha256 = hashlib.sha256(captured.manifest_path.read_bytes()).hexdigest()
    reader = RawResponseArtifactReader(artifact_root)
    context = FinancialObservationCaptureContext(
        source_id="ths_fuyao",
        instrument_id="600519.SH",
        statement_type="income",
        periodicity="annual",
        observed_at_utc=instant,
        raw_source=_SOURCE,
        request_summary_sha256=_REQUEST_SHA256,
    )
    prepared = prepare_financial_observation_capture(
        context=context,
        reader=reader,
        manifest_sha256=manifest_sha256,
        expected_http_status=200,
        result=decoded,
    )
    return _Fixture(
        context=context,
        reader=reader,
        manifest_sha256=manifest_sha256,
        expected_http_status=200,
        body=body,
        result=decoded,
        artifact_path=captured.artifact_path,
        manifest_path=captured.manifest_path,
        prepared=prepared,
    )


def _prepare(
    fixture: _Fixture,
    *,
    context: FinancialObservationCaptureContext | None = None,
    reader: RawResponseArtifactReader | None = None,
    manifest_sha256: str | None = None,
    expected_http_status: int | None = None,
    result: FinancialObservationDecodeResult | None = None,
) -> PreparedCapture:
    """Prepare overridden test inputs outside any database transaction."""
    return prepare_financial_observation_capture(
        context=fixture.context if context is None else context,
        reader=fixture.reader if reader is None else reader,
        manifest_sha256=fixture.manifest_sha256 if manifest_sha256 is None else manifest_sha256,
        expected_http_status=(
            fixture.expected_http_status if expected_http_status is None else expected_http_status
        ),
        result=fixture.result if result is None else result,
    )


def _verified_evidence(fixture: _Fixture) -> RawEvidenceReadResult:
    """Reopen one fixture's real manifest and response object for validator tests."""
    return fixture.reader.read(
        fixture.manifest_sha256,
        expected_source=fixture.context.raw_source,
        expected_request_summary_sha256=fixture.context.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc) + timedelta(days=1),
        expected_http_status=fixture.expected_http_status,
    )


def _replace_sql_row_field(
    row: store._FrozenSqlRow,
    field: str,
    value: object,
) -> store._FrozenSqlRow:
    """Return a deliberately changed scalar row for defensive DTO tests."""
    return cast(
        "store._FrozenSqlRow",
        tuple((name, value if name == field else existing) for name, existing in row),
    )


def _unsafe_copy_dataclass(instance: object, **changes: object) -> object:
    """Build malformed frozen values to exercise the store's defensive boundary."""
    copied = object.__new__(type(instance))
    for item in fields(instance):
        object.__setattr__(copied, item.name, changes.get(item.name, getattr(instance, item.name)))
    return copied


@pytest.mark.parametrize(
    ("raw", "expected", "matches"),
    [
        (None, None, True),
        (None, Decimal("0"), False),
        (True, 1, False),
        (1.5, Decimal("1.5"), False),
        ("1.25", Decimal("1.25"), True),
        (4, Decimal("4"), True),
        (Decimal("3"), "3", True),
        ("not-a-number", 1, False),
        ("x" * 129, 1, False),
        (object(), 1, False),
        (Decimal("NaN"), Decimal("NaN"), False),
        (Decimal("2"), "x" * 129, False),
    ],
)
def test_raw_value_matcher_preserves_exact_numeric_and_null_semantics(
    raw: object,
    expected: Decimal | int | str | None,
    matches: bool,
) -> None:
    assert store._raw_value_matches(raw, expected) is matches


@pytest.mark.parametrize(
    "value",
    [
        "not-a-timestamp",
        10**30,
        int(datetime(2025, 3, 31, 12, tzinfo=_SHANGHAI).timestamp() * 1000),
    ],
)
def test_source_millisecond_date_rejects_non_midnight_and_invalid_inputs(value: object) -> None:
    assert store._source_millis_to_date(value) is None


def test_source_millisecond_date_accepts_shanghai_midnight() -> None:
    timestamp = int(datetime(2025, 3, 31, tzinfo=_SHANGHAI).timestamp() * 1000)
    assert store._source_millis_to_date(timestamp) == date(2025, 3, 31)


def test_json_and_pointer_helpers_reject_malformed_source_shapes() -> None:
    for body in (b"", b"\xff", b"{", b'{"x":1,"x":2}', b'{"x":NaN}'):
        with pytest.raises(FinancialObservationStoreError):
            store._parse_json_body(body)
    with pytest.raises(FinancialObservationStoreError):
        store._validate_pointer("relative", "test pointer")
    with pytest.raises(FinancialObservationStoreError):
        store._validate_pointer("/bad~2escape", "test pointer")
    with pytest.raises(FinancialObservationStoreError):
        store._validate_pointer("/" + "x" * 512, "test pointer")
    assert store._validate_pointer("/a~1b/~0x", "test pointer") == ["a/b", "~x"]


def test_source_row_and_pointer_resolution_reject_non_objects_and_invalid_indices(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    payload = store._parse_json_body(fixture.body)
    row = json.loads(fixture.body)["data"]["item"][0]
    with pytest.raises(FinancialObservationStoreError):
        store._source_rows([])
    with pytest.raises(FinancialObservationStoreError):
        store._source_rows({"data": {"item": []}})
    with pytest.raises(FinancialObservationStoreError):
        store._source_rows({"data": {"item": [None]}})
    with pytest.raises(FinancialObservationStoreError):
        store._source_rows({"data": {"item": [dict(row) for _ in range(65)]}})
    for bad_payload, segments in (
        ([], ["data", "item", "0"]),
        ({"data": []}, ["data", "item", "0"]),
        ({"data": {"item": "bad"}}, ["data", "item", "0"]),
        ({"data": {"item": [row]}}, ["data", "item", "01"]),
        ({"data": {"item": [row]}}, ["data", "item", "1"]),
        ({"data": {"item": [[]]}}, ["data", "item", "0"]),
    ):
        with pytest.raises(FinancialObservationStoreError):
            store._resolve_body_row(bad_payload, segments)
    assert store._resolve_body_row(payload, ["data", "item", "0"]) == row


def test_request_id_and_source_row_scope_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "raw")
    payload = store._parse_json_body(fixture.body)
    row = json.loads(fixture.body)["data"]["item"][0]
    with pytest.raises(FinancialObservationStoreError):
        store._validate_request_id(payload, "different-request")
    for changes in (
        {"thscode": "000001.SZ"},
        {"ticker": "000001"},
        {"period": "quarterly"},
        {"currency": "USD"},
        {"fiscal_year": True},
        {"fiscal_period": "BOGUS"},
        {"fiscal_period": "Q1"},
        {"period_end_ms": 0},
    ):
        with pytest.raises(FinancialObservationStoreError):
            store._validate_raw_row_scope({**row, **changes}, fixture.context)
    with pytest.raises(FinancialObservationStoreError):
        store._validate_raw_row_scope(
            {**row, "fiscal_year": 9999, "fiscal_period": "FY"}, fixture.context
        )


def test_context_validator_rejects_bad_scope_types_and_source_declarations(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    invalid_contexts = [
        object(),
        replace(fixture.context, source_id="other_source"),
        replace(fixture.context, instrument_id="bad"),
        replace(fixture.context, statement_type="other"),
        replace(fixture.context, periodicity="monthly"),
        replace(fixture.context, observed_at_utc=datetime(2026, 10, 27)),
        replace(fixture.context, raw_source=object()),
        replace(
            fixture.context,
            raw_source=_SOURCE.model_copy(update={"operation_id": "bad operation"}),
        ),
        replace(fixture.context, request_summary_sha256="not-a-digest"),
    ]
    for invalid in invalid_contexts:
        with pytest.raises(FinancialObservationStoreError):
            store._validate_context(invalid)
    assert store._validate_context(fixture.context).raw_source == _SOURCE


def test_evidence_validator_rechecks_reader_result_metadata_and_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    evidence = _verified_evidence(fixture)
    for invalid in (
        object(),
        replace(evidence, manifest=object()),
        replace(evidence, body=bytearray(evidence.body)),
        replace(evidence, body=b""),
        replace(evidence, manifest_sha256="0" * 64),
        replace(evidence, manifest_sha256=None),
        replace(evidence, body=evidence.body + b" "),
        replace(
            evidence,
            manifest=evidence.manifest.model_copy(update={"capture_id": "bad-capture"}),
        ),
        replace(
            evidence,
            manifest=evidence.manifest.model_copy(update={"response_size_bytes": 1}),
        ),
        replace(
            evidence,
            manifest=evidence.manifest.model_copy(
                update={"captured_at_utc": datetime(2026, 10, 27)}
            ),
        ),
        replace(
            evidence,
            manifest=evidence.manifest.model_copy(update={"http_status": 404}),
        ),
    ):
        with pytest.raises(FinancialObservationStoreError):
            store._validate_evidence(invalid, fixture.context, fixture.expected_http_status)
    with pytest.raises(FinancialObservationStoreError):
        store._validate_evidence(
            evidence,
            replace(fixture.context, request_summary_sha256="a" * 64),
            fixture.expected_http_status,
        )
    with pytest.raises(FinancialObservationStoreError, match="expected request"):
        store._validate_evidence(evidence, fixture.context, 201)
    with pytest.raises(FinancialObservationStoreError):
        store._validate_evidence(
            evidence,
            replace(
                fixture.context,
                raw_source=_SOURCE.model_copy(update={"operation_id": "other_operation"}),
            ),
            fixture.expected_http_status,
        )
    future_context = replace(
        fixture.context,
        observed_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(FinancialObservationStoreError, match="precedes"):
        store._validate_evidence(evidence, future_context, fixture.expected_http_status)
    monkeypatch.setattr(store, "_MAX_RAW_BODY_BYTES", 1)
    with pytest.raises(FinancialObservationStoreError, match="body exceeds"):
        store._validate_evidence(evidence, fixture.context, fixture.expected_http_status)
    monkeypatch.setattr(store, "_MAX_RAW_BODY_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(store, "_MAX_MANIFEST_BYTES", 1)
    with pytest.raises(FinancialObservationStoreError, match="manifest exceeds"):
        store._validate_evidence(evidence, fixture.context, fixture.expected_http_status)


def test_decoder_result_validator_binds_digest_request_id_units_and_collections(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    evidence = _verified_evidence(fixture)
    result = fixture.result
    invalid_results = [
        object(),
        replace(result, body_sha256="0" * 64),
        _unsafe_copy_dataclass(result, source_request_id=""),
        _unsafe_copy_dataclass(result, source_request_id=" padded "),
        replace(result, native_unit_mapping_version_declared=""),
        replace(result, native_unit_mapping_version_declared=" spaced "),
        replace(result, native_unit_mapping_version_declared="v" * 129),
        replace(result, native_unit_mapping_version_declared="v\x00bad"),
        replace(result, native_unit_mapping_version_declared="\ud800"),
        replace(result, native_unit_mapping_sha256_declared="bad-digest"),
    ]
    for invalid in invalid_results:
        with pytest.raises(FinancialObservationStoreError):
            store._validate_decode_result(invalid, evidence)
    assert store._validate_decode_result(result, evidence) is result
    with pytest.raises(FinancialObservationStoreError):
        store._validate_decode_result(object(), evidence)
    invalid_collections = [
        _unsafe_copy_dataclass(result, observations=[]),
        _unsafe_copy_dataclass(result, quarantined_rows=[None]),
        _unsafe_copy_dataclass(result, observations=(result.observations[0],) * 1025),
        _unsafe_copy_dataclass(result, quarantined_rows=(None,) * 65),
        _unsafe_copy_dataclass(result, observations=(), quarantined_rows=()),
    ]
    for invalid in invalid_collections:
        with pytest.raises(FinancialObservationStoreError):
            store._validate_decode_result(invalid, evidence)
    with pytest.raises(FinancialObservationStoreError, match="request id"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=replace(result, source_request_id="different-request"),
        )


def test_reader_argument_validation_rejects_type_digest_and_status_errors(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    cases = [
        (object(), fixture.manifest_sha256, 200, fixture.result),
        (fixture.reader, "../manifest", 200, fixture.result),
        (fixture.reader, fixture.manifest_sha256, True, fixture.result),
        (fixture.reader, fixture.manifest_sha256, 404, fixture.result),
        (fixture.reader, fixture.manifest_sha256, 200, object()),
    ]
    for reader, manifest_sha256, expected_http_status, result in cases:
        with pytest.raises(FinancialObservationStoreError):
            store._validate_reader_arguments(reader, manifest_sha256, expected_http_status, result)
    store._validate_reader_arguments(
        fixture.reader,
        fixture.manifest_sha256,
        fixture.expected_http_status,
        fixture.result,
    )


def test_row_scope_period_calendar_overflow_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    row = json.loads(fixture.body)["data"]["item"][0]
    monkeypatch.setitem(store.FINANCIAL_PERIOD_END, "FY", (13, 31))
    with pytest.raises(FinancialObservationStoreError, match="period is invalid"):
        store._validate_raw_row_scope(row, fixture.context)


def test_prepare_rows_enforces_mapping_and_reference_row_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    evidence = _verified_evidence(fixture)
    original_mapping = store.financial_statement_items
    monkeypatch.setattr(
        store,
        "financial_statement_items",
        lambda _statement: (_ for _ in ()).throw(KeyError("mapping unavailable")),
    )
    with pytest.raises(FinancialObservationStoreError, match="mapping is unavailable"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=fixture.result,
        )
    monkeypatch.setattr(store, "financial_statement_items", lambda _statement: ())
    with pytest.raises(FinancialObservationStoreError, match="mapping is empty"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=fixture.result,
        )
    monkeypatch.setattr(
        store,
        "financial_statement_items",
        lambda _statement: tuple(f"item_{index}" for index in range(17)),
    )
    with pytest.raises(FinancialObservationStoreError, match="item budget"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=fixture.result,
        )
    monkeypatch.setattr(store, "financial_statement_items", original_mapping)
    monkeypatch.setattr(store, "_MAX_OBSERVATION_REFERENCES", 1)
    bounded_result = replace(fixture.result, observations=(fixture.result.observations[0],))
    with pytest.raises(FinancialObservationStoreError, match="observation SQL budget"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=bounded_result,
        )


def test_prepare_rows_rejects_duplicate_source_and_quarantine_pointers(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw", include_quarantined_row=True)
    evidence = _verified_evidence(fixture)
    first_reference = fixture.result.observations[0]
    duplicate_observation_result = replace(
        fixture.result,
        observations=(first_reference, first_reference, *fixture.result.observations[1:]),
    )
    with pytest.raises(FinancialObservationStoreConflictError, match="duplicate source value"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=duplicate_observation_result,
        )
    quarantine = fixture.result.quarantined_rows[0]
    duplicate_quarantine_result = replace(
        fixture.result,
        quarantined_rows=(quarantine, quarantine),
    )
    with pytest.raises(FinancialObservationStoreConflictError, match="duplicate quarantined"):
        store._prepare_rows(
            context=fixture.context,
            evidence=evidence,
            expected_http_status=fixture.expected_http_status,
            result=duplicate_quarantine_result,
        )


def _table(connection: Connection, name: str) -> sa.Table:
    """Reflect one fixed migration-owned table for SQLAlchemy queries."""
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _counts(connection: Connection) -> dict[str, int]:
    """Return a compact row count for each archive table."""
    return {
        table_name: connection.scalar(
            sa.select(sa.func.count()).select_from(_table(connection, table_name))
        )
        or 0
        for table_name in (_CAPTURES, _OBSERVATIONS, _LINKS, _QUARANTINE)
    }


def _begin_immediate(connection: Connection) -> None:
    """Open SQLite's physical write transaction for caller-owned store tests."""
    driver_connection = connection.connection.driver_connection
    if not getattr(driver_connection, "in_transaction", False):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def test_exact_replay_is_idempotent_and_keeps_null_distinct_from_decimal_zero(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            first = persist_prepared_financial_observation_capture(
                connection, prepared=fixture.prepared
            )
            replay = persist_prepared_financial_observation_capture(
                connection, prepared=fixture.prepared
            )
            assert first.inserted_capture is True
            assert first.inserted_observations == len(fixture.result.observations)
            assert first.inserted_links == len(fixture.result.observations)
            assert replay.inserted_capture is False
            assert replay.inserted_observations == 0
            assert replay.inserted_links == 0
            assert _counts(connection) == {
                _CAPTURES: 1,
                _OBSERVATIONS: len(fixture.result.observations),
                _LINKS: len(fixture.result.observations),
                _QUARANTINE: 0,
            }
            observations_table = _table(connection, _OBSERVATIONS)
            rows = connection.execute(
                sa.select(
                    observations_table.c.item_code,
                    observations_table.c.value_decimal_text,
                ).order_by(observations_table.c.item_code)
            ).all()
            by_item: dict[str, str | None] = {}
            for row in rows:
                by_item[str(row[0])] = row[1]
            item_codes = financial_statement_items("income")
            assert by_item[item_codes[0]] == "12345678901234567890.123456789012345678"
            if len(item_codes) > 1:
                assert by_item[item_codes[1]] is None
            if len(item_codes) > 2:
                assert by_item[item_codes[2]] == "0"
            captures_table = _table(connection, _CAPTURES)
            keys = connection.execute(
                sa.select(
                    captures_table.c.raw_object_sha256,
                    captures_table.c.raw_object_key,
                    captures_table.c.raw_manifest_sha256,
                    captures_table.c.raw_manifest_key,
                )
            ).one()
            assert (
                keys.raw_object_key == f"{keys.raw_object_sha256[:2]}/{keys.raw_object_sha256}.blob"
            )
            assert keys.raw_manifest_key == (
                f"{keys.raw_manifest_sha256[:2]}/{keys.raw_manifest_sha256}.json"
            )
    finally:
        engine.dispose()


def test_reobservation_keeps_each_capture_time_without_rewriting_first_seen(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    first_seen = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=123456)
    later_seen = first_seen + timedelta(minutes=5)
    first = _fixture(tmp_path / "raw", observed_at=first_seen)
    second = _fixture(tmp_path / "raw", observed_at=later_seen)
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            persist_prepared_financial_observation_capture(connection, prepared=first.prepared)
            repeated = persist_prepared_financial_observation_capture(
                connection, prepared=second.prepared
            )
            assert repeated.inserted_capture is True
            assert repeated.inserted_observations == 0
            assert repeated.inserted_links == len(first.result.observations)
            captures_table = _table(connection, _CAPTURES)
            observations_table = _table(connection, _OBSERVATIONS)
            assert connection.scalar(sa.select(sa.func.count()).select_from(captures_table)) == 2
            assert connection.scalar(
                sa.select(sa.func.count()).select_from(observations_table)
            ) == len(first.result.observations)
            first_observed = connection.scalar(
                sa.select(observations_table.c.observed_at_utc).limit(1)
            )
            assert first_observed == first_seen.replace(tzinfo=None)
            capture_times = (
                connection.execute(
                    sa.select(captures_table.c.observed_at_utc).order_by(
                        captures_table.c.observed_at_utc
                    )
                )
                .scalars()
                .all()
            )
            assert capture_times == [
                first_seen.replace(tzinfo=None),
                later_seen.replace(tzinfo=None),
            ]
    finally:
        engine.dispose()


def test_changed_source_value_appends_a_restatement_under_same_natural_key(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    first = _fixture(
        tmp_path / "raw",
        observed_at=datetime.now(timezone.utc) - timedelta(days=1),
        changed_value="10",
    )
    second = _fixture(
        tmp_path / "raw",
        observed_at=first.context.observed_at_utc + timedelta(minutes=1),
        changed_value="11",
        request_id="synthetic-request-002",
    )
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            persist_prepared_financial_observation_capture(connection, prepared=first.prepared)
            receipt = persist_prepared_financial_observation_capture(
                connection, prepared=second.prepared
            )
            assert receipt.inserted_observations == 1
            observations_table = _table(connection, _OBSERVATIONS)
            observations = connection.execute(
                sa.select(
                    observations_table.c.natural_key_sha256,
                    observations_table.c.observation_id,
                    observations_table.c.value_decimal_text,
                ).order_by(observations_table.c.value_decimal_text)
            ).all()
            assert len(observations) == len(first.result.observations) + 1
            matching = [row for row in observations if row[2] in {"10", "11"}]
            assert len(matching) == 2
            assert matching[0][0] == matching[1][0]
            assert matching[0][1] != matching[1][1]
    finally:
        engine.dispose()


def test_same_capture_with_changed_immutable_metadata_conflicts_without_overwrite(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    changed = replace(
        fixture.result,
        native_unit_mapping_version_declared="operator-unit-v2",
        native_unit_mapping_sha256_declared="c" * 64,
    )
    changed_prepared = _prepare(fixture, result=changed)
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            persist_prepared_financial_observation_capture(connection, prepared=fixture.prepared)
            with pytest.raises(FinancialObservationStoreConflictError):
                persist_prepared_financial_observation_capture(
                    connection, prepared=changed_prepared
                )
            captures_table = _table(connection, _CAPTURES)
            assert connection.scalar(sa.select(sa.func.count()).select_from(captures_table)) == 1
            version = connection.scalar(
                sa.select(captures_table.c.native_unit_mapping_version_declared).limit(1)
            )
            assert version == "operator-unit-v1"
    finally:
        engine.dispose()


def test_quarantine_only_capture_keeps_explicit_source_and_pointer_provenance(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw", include_disclosure_date=False)
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            receipt = persist_prepared_financial_observation_capture(
                connection, prepared=fixture.prepared
            )
            assert receipt.inserted_capture is True
            assert receipt.inserted_observations == 0
            assert receipt.inserted_quarantine_rows == 1
            captures_table = _table(connection, _CAPTURES)
            quarantine_table = _table(connection, _QUARANTINE)
            links_table = _table(connection, _LINKS)
            assert connection.scalar(sa.select(captures_table.c.source_id).limit(1)) == "ths_fuyao"
            assert connection.scalar(sa.select(sa.func.count()).select_from(quarantine_table)) == 1
            assert connection.scalar(sa.select(sa.func.count()).select_from(links_table)) == 0
    finally:
        engine.dispose()


def test_quarantine_validator_covers_missing_invalid_future_and_mixed_rows(
    tmp_path: Path,
) -> None:
    missing = _fixture(tmp_path / "missing", include_disclosure_date=False)
    invalid_midnight = int(datetime(2024, 12, 30, tzinfo=_SHANGHAI).timestamp() * 1000)
    invalid = _fixture(
        tmp_path / "invalid",
        disclosure_timestamp_ms=invalid_midnight,
    )
    non_midnight = int(datetime(2025, 3, 31, 12, tzinfo=_SHANGHAI).timestamp() * 1000)
    invalid_timestamp = _fixture(
        tmp_path / "invalid-timestamp",
        disclosure_timestamp_ms=non_midnight,
    )
    before_disclosure = datetime(2025, 3, 29, 16, tzinfo=timezone.utc)
    future = _fixture(tmp_path / "future", observed_at=before_disclosure)
    mixed = _fixture(tmp_path / "mixed", include_quarantined_row=True)

    for fixture, reason in (
        (missing, "disclosure_date_missing"),
        (invalid, "disclosure_date_invalid"),
        (invalid_timestamp, "disclosure_date_invalid"),
        (future, "disclosure_date_after_observed_at"),
    ):
        assert len(fixture.result.quarantined_rows) == 1
        reference = fixture.result.quarantined_rows[0]
        assert reference.reason == reason
        store._validate_quarantine_reference(
            reference,
            context=fixture.context,
            body_sha256=fixture.result.body_sha256,
            body_payload=store._parse_json_body(fixture.body),
        )

    assert len(mixed.result.observations) == len(financial_statement_items("income"))
    assert len(mixed.result.quarantined_rows) == 1

    evidence = _verified_evidence(missing)
    reference = missing.result.quarantined_rows[0]
    payload = store._parse_json_body(missing.body)
    invalid_reference = replace(reference, reason="unknown_reason")
    bad_hash = replace(reference, body_sha256="0" * 64)
    bad_row_pointer = replace(reference, row_pointer="/not/data/0")
    bad_missing_pointer = replace(reference, disclosure_pointer="/data/item/0/report_date_ms")
    for candidate in (object(), invalid_reference, bad_hash, bad_row_pointer, bad_missing_pointer):
        with pytest.raises(FinancialObservationStoreError):
            store._validate_quarantine_reference(
                candidate,
                context=missing.context,
                body_sha256=evidence.manifest.response_sha256,
                body_payload=payload,
            )

    valid_fixture = _fixture(tmp_path / "valid-disclosure")
    valid_row_payload = store._parse_json_body(valid_fixture.body)
    invalid_reason = replace(
        reference,
        reason="disclosure_date_invalid",
        disclosure_pointer="/data/item/0/report_date_ms",
        body_sha256=valid_fixture.result.body_sha256,
    )
    future_reason = replace(
        invalid_reason,
        reason="disclosure_date_after_observed_at",
    )
    with pytest.raises(FinancialObservationStoreError):
        store._validate_quarantine_reference(
            invalid_reason,
            context=valid_fixture.context,
            body_sha256=valid_fixture.result.body_sha256,
            body_payload=valid_row_payload,
        )
    with pytest.raises(FinancialObservationStoreError):
        store._validate_quarantine_reference(
            future_reason,
            context=valid_fixture.context,
            body_sha256=valid_fixture.result.body_sha256,
            body_payload=valid_row_payload,
        )


def test_observation_reference_validator_rejects_scope_pointer_metadata_and_value_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    evidence = _verified_evidence(fixture)
    payload = store._parse_json_body(fixture.body)
    reference = fixture.result.observations[0]
    observation = reference.observation
    invalid_observations = [
        ("source_id", replace(observation, source_id="other_source")),
        ("instrument_id", replace(observation, instrument_id="000001.SH")),
        ("statement_type", replace(observation, statement_type="balance")),
        ("periodicity", replace(observation, periodicity="quarterly")),
        (
            "observed_at",
            replace(observation, observed_at=observation.observed_at - timedelta(hours=1)),
        ),
        ("report_period", replace(observation, report_period=date(2023, 12, 31))),
        (
            "source_disclosure_date",
            replace(observation, source_disclosure_date=date(2025, 3, 30)),
        ),
        ("source_metadata", replace(observation, source_metadata=(("unexpected", "metadata"),))),
        ("value", replace(observation, value=Decimal("999"))),
        ("item_code", replace(observation, item_code="unknown_item")),
    ]
    invalid_references: list[tuple[str, object]] = [
        ("reference_type", object()),
        ("body_sha256", replace(reference, body_sha256="0" * 64)),
        ("row_pointer", replace(reference, row_pointer="relative")),
        (
            "wrong_item_pointer",
            replace(reference, value_pointer=f"{reference.row_pointer}/wrong_item"),
        ),
        (
            "invalid_pointer_escape",
            replace(reference, value_pointer=f"{reference.row_pointer}/bad~2escape"),
        ),
        *[
            (f"observation_{case}", replace(reference, observation=invalid_observation))
            for case, invalid_observation in invalid_observations
        ],
        (
            "invalid_observation_source_id_type",
            replace(
                reference,
                observation=_unsafe_copy_dataclass(observation, source_id=None),
            ),
        ),
    ]
    # observation_id is a derived property, not a stored dataclass field. It
    # cannot be independently corrupted while preserving the observation type.
    changed_identity = replace(observation, value=Decimal("999"))
    assert changed_identity.observation_id != observation.observation_id
    for case, invalid_reference in invalid_references:
        try:
            store._validate_observation_reference(
                cast("FinancialObservationSourceReference", invalid_reference),
                context=fixture.context,
                evidence=evidence,
                body_payload=payload,
            )
        except FinancialObservationStoreError:
            continue
        pytest.fail(f"invalid observation-reference case was accepted: {case}")

    changed_payload = json.loads(fixture.body)
    changed_payload["data"]["item"][0]["extra_metadata"] = "changed"
    with pytest.raises(FinancialObservationStoreError, match="metadata"):
        store._validate_observation_reference(
            reference,
            context=fixture.context,
            evidence=evidence,
            body_payload=changed_payload,
        )
    changed_payload = json.loads(fixture.body)
    del changed_payload["data"]["item"][0][observation.item_code]
    with pytest.raises(FinancialObservationStoreError, match="value is missing"):
        store._validate_observation_reference(
            reference,
            context=fixture.context,
            evidence=evidence,
            body_payload=changed_payload,
        )
    changed_payload = json.loads(fixture.body)
    changed_payload["data"]["item"][0][observation.item_code] = "different"
    with pytest.raises(FinancialObservationStoreError, match="does not match its pointer"):
        store._validate_observation_reference(
            reference,
            context=fixture.context,
            evidence=evidence,
            body_payload=changed_payload,
        )
    monkeypatch.setattr(store, "financial_statement_items", lambda _statement: ())
    unknown_item_observation = replace(observation, item_code="unknown_item")
    unknown_item_reference = replace(
        reference,
        observation=unknown_item_observation,
        value_pointer=f"{reference.row_pointer}/unknown_item",
    )
    with pytest.raises(FinancialObservationStoreError, match="outside its statement mapping"):
        store._validate_observation_reference(
            unknown_item_reference,
            context=fixture.context,
            evidence=evidence,
            body_payload=payload,
        )
    monkeypatch.setattr(
        store,
        "financial_statement_items",
        lambda _statement: (_ for _ in ()).throw(KeyError("no mapping")),
    )
    with pytest.raises(FinancialObservationStoreError, match="mapping is unavailable"):
        store._validate_observation_reference(
            reference,
            context=fixture.context,
            evidence=evidence,
            body_payload=payload,
        )


def test_bad_raw_digest_or_pointer_fails_before_any_database_write(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    reference = fixture.result.observations[0]
    bad_reference = replace(reference, value_pointer=f"{reference.row_pointer}/missing_item")
    bad_result = replace(fixture.result, observations=(bad_reference,))
    incomplete_result = replace(
        fixture.result,
        observations=fixture.result.observations[:-1],
    )
    try:
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, manifest_sha256="0" * 64)
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, result=bad_result)
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, result=incomplete_result)
        with pytest.raises(FinancialObservationStoreError, match="reader rejected"):
            _prepare(fixture, expected_http_status=201)
        wrong_scope = replace(fixture.context, instrument_id="000001.SH")
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, context=wrong_scope)
        wrong_source_scope = replace(
            fixture.context,
            raw_source=_SOURCE.model_copy(update={"operation_id": "different_operation"}),
        )
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, context=wrong_source_scope)
        with pytest.raises(FinancialObservationStoreError):
            _fixture(
                tmp_path / "future-observation-raw",
                observed_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        missing_reader = RawResponseArtifactReader(tmp_path / "missing-raw-root")
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, reader=missing_reader)
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture, reader=_ReaderSubclass(tmp_path / "raw"))
        with pytest.raises(TypeError):
            prepare_financial_observation_capture(
                context=fixture.context,
                reader=fixture.reader,
                manifest_sha256=fixture.manifest_sha256,
                expected_http_status=fixture.expected_http_status,
                result=fixture.result,
                **{"evidence": object()},
            )
        with engine.connect() as connection:
            assert _counts(connection) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
    finally:
        engine.dispose()


def test_single_capture_row_limit_rejects_without_silent_truncation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "raw")
    payload = json.loads(fixture.body)
    row = payload["data"]["item"][0]
    payload["data"]["item"] = [dict(row) for _ in range(65)]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    captured = RawResponseArtifactWriter(tmp_path / "raw").capture(
        [body],
        source=_SOURCE,
        request_summary_sha256=_REQUEST_SHA256,
        http_status=200,
        retention_class="restricted",
        source_history_limit="unknown",
    )
    manifest_sha256 = hashlib.sha256(captured.manifest_path.read_bytes()).hexdigest()
    body_sha256 = hashlib.sha256(body).hexdigest()
    references = tuple(
        replace(reference, body_sha256=body_sha256) for reference in fixture.result.observations
    )
    oversized_result = replace(
        fixture.result,
        body_sha256=body_sha256,
        observations=references,
    )
    with pytest.raises(FinancialObservationStoreError, match="no rows were truncated"):
        prepare_financial_observation_capture(
            context=fixture.context,
            reader=fixture.reader,
            manifest_sha256=manifest_sha256,
            expected_http_status=fixture.expected_http_status,
            result=oversized_result,
        )


@pytest.mark.parametrize("artifact", ["body", "manifest"])
def test_tampered_on_disk_evidence_fails_before_any_database_write(
    tmp_path: Path,
    artifact: Literal["body", "manifest"],
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    target = fixture.artifact_path if artifact == "body" else fixture.manifest_path
    target.write_bytes(b"tampered evidence")
    try:
        with pytest.raises(FinancialObservationStoreError):
            _prepare(fixture)
        with engine.connect() as connection:
            assert _counts(connection) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
    finally:
        engine.dispose()


def test_prepared_capture_is_deeply_frozen_against_accidental_mutation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    prepared = fixture.prepared
    assert type(prepared._capture_values) is tuple
    assert all(type(entry) is tuple for entry in prepared._capture_values)
    assert type(prepared._observation_values) is tuple
    assert all(
        type(entry) is tuple
        and type(entry[1]) is tuple
        and all(type(value) is tuple for value in entry[1])
        for entry in prepared._observation_values
    )
    assert type(prepared._links_by_observation) is tuple
    assert all(
        type(group) is tuple
        and type(group[1]) is tuple
        and all(
            type(row) is tuple and all(type(value) is tuple for value in row) for row in group[1]
        )
        for group in prepared._links_by_observation
    )
    assert type(prepared._quarantine_values) is tuple
    with pytest.raises(AttributeError):
        prepared._capture_id = "0" * 32  # type: ignore[misc]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        pytest.param("capture", "provider_id", "p" * 129, id="varchar-character-limit"),
        pytest.param(
            "capture",
            "observed_at_utc",
            datetime(2026, 10, 27, tzinfo=timezone.utc),
            id="datetime-must-be-naive-utc",
        ),
        pytest.param(
            "observation",
            "source_metadata_json",
            "x" * 65_537,
            id="metadata-character-limit-before-encode",
        ),
        pytest.param(
            "observation",
            "source_metadata_json",
            "\u0800" * 21_846,
            id="metadata-utf8-byte-limit",
        ),
        pytest.param(
            "capture",
            "raw_object_size_bytes",
            2**63,
            id="bigint-upper-bound",
        ),
        pytest.param(
            "capture",
            "raw_object_size_bytes",
            -(2**63) - 1,
            id="bigint-lower-bound",
        ),
        pytest.param(
            "capture",
            "http_status",
            2**15,
            id="smallint-upper-bound",
        ),
        pytest.param(
            "capture",
            "http_status",
            -(2**15) - 1,
            id="smallint-lower-bound",
        ),
    ],
)
def test_persist_rejects_oversized_or_aware_dto_values_before_any_write(
    tmp_path: Path,
    section: Literal["capture", "observation"],
    field: str,
    value: object,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    prepared = fixture.prepared
    if section == "capture":
        prepared = replace(
            prepared,
            _capture_values=_replace_sql_row_field(prepared._capture_values, field, value),
        )
    else:
        observation_id, observation_values = prepared._observation_values[0]
        changed = _replace_sql_row_field(observation_values, field, value)
        prepared = replace(
            prepared,
            _observation_values=((observation_id, changed), *prepared._observation_values[1:]),
        )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(FinancialObservationStoreError):
                persist_prepared_financial_observation_capture(connection, prepared=prepared)
            assert _counts(connection) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
            transaction.rollback()
    finally:
        engine.dispose()


def test_materialize_rejects_malformed_prepared_dto_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    prepared = fixture.prepared
    observation_id, observation_row = prepared._observation_values[0]
    link_group = prepared._links_by_observation[0]
    link_id, link_rows = link_group
    link_row = link_rows[0]
    changed_observation_row = _replace_sql_row_field(observation_row, "observation_id", "f" * 64)
    changed_link_row = _replace_sql_row_field(link_row, "capture_id", "0" * 32)
    invalid = [
        object(),
        replace(prepared, _capture_id="bad"),
        replace(
            prepared,
            _capture_values=_replace_sql_row_field(
                prepared._capture_values, "capture_id", "1" * 32
            ),
        ),
        replace(prepared, _observation_values=[]),
        replace(
            prepared,
            _observation_values=((observation_id, observation_row),) * 1025,
        ),
        replace(prepared, _observation_values=((None,),)),
        replace(prepared, _observation_values=(("bad-id", observation_row),)),
        replace(prepared, _observation_values=((observation_id, changed_observation_row),)),
        replace(prepared, _observation_values=((observation_id, observation_row),) * 2),
        replace(prepared, _links_by_observation=[]),
        replace(prepared, _links_by_observation=((None,),)),
        replace(prepared, _links_by_observation=(("f" * 64, (link_row,)),)),
        replace(prepared, _links_by_observation=((link_id, ()),)),
        replace(prepared, _links_by_observation=((link_id, (changed_link_row,)),)),
        replace(prepared, _links_by_observation=()),
        replace(prepared, _quarantine_values=[]),
        replace(prepared, _sql_statement_ceiling=1),
    ]
    duplicated_pointer = _replace_sql_row_field(link_row, "link_id", "e" * 64)
    duplicated_pointer = _replace_sql_row_field(
        duplicated_pointer, "value_pointer", dict(link_row)["value_pointer"]
    )
    invalid.append(
        replace(
            prepared,
            _links_by_observation=((link_id, (link_row, duplicated_pointer)),),
        )
    )
    for candidate in invalid:
        with pytest.raises(FinancialObservationStoreError):
            store._materialize_prepared_capture(candidate)

    quarantine_fixture = _fixture(tmp_path / "quarantine", include_disclosure_date=False)
    quarantine_prepared = quarantine_fixture.prepared
    quarantine_row = quarantine_prepared._quarantine_values[0]
    changed_quarantine_row = _replace_sql_row_field(quarantine_row, "capture_id", "0" * 32)
    for candidate in (
        replace(quarantine_prepared, _quarantine_values=[]),
        replace(
            quarantine_prepared,
            _quarantine_values=(quarantine_row,) * 65,
        ),
        replace(
            quarantine_prepared,
            _quarantine_values=(changed_quarantine_row,),
        ),
        replace(
            quarantine_prepared,
            _quarantine_values=(quarantine_row, quarantine_row),
        ),
        replace(quarantine_prepared, _sql_statement_ceiling=None),
        replace(quarantine_prepared, _sql_statement_ceiling=1),
    ):
        with pytest.raises(FinancialObservationStoreError):
            store._materialize_prepared_capture(candidate)
    monkeypatch.setattr(store, "_MAX_SQL_STATEMENTS_PER_CAPTURE", 1)
    with pytest.raises(FinancialObservationStoreError, match="statement ceiling"):
        store._materialize_prepared_capture(quarantine_prepared)


def test_freeze_helpers_reject_mutable_rows_and_sql_budget_overruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "raw")
    capture_values = dict(fixture.prepared._capture_values)
    with pytest.raises(FinancialObservationStoreError):
        store._freeze_sql_row({}, store.CAPTURES)
    capture_values["provider_id"] = []
    with pytest.raises(FinancialObservationStoreError):
        store._freeze_sql_row(capture_values, store.CAPTURES)
    for malformed in (
        [],
        (("capture_id",),),
        (("capture_id", "a" * 32), ("capture_id", "b" * 32)),
        (("unknown_column", "x"),),
        (("capture_id", "a" * 32),),
    ):
        with pytest.raises(FinancialObservationStoreError):
            store._thaw_sql_row(malformed, store.CAPTURES)

    rows = store._materialize_prepared_capture(fixture.prepared)
    with pytest.raises(FinancialObservationStoreError, match="observation SQL budget"):
        store._freeze_prepared_rows(replace(rows, observations=(("x", {}),) * 1025))
    observation_id, original_group = next(iter(rows.links_by_observation.items()))
    original_link = original_group[0]
    oversized_group = tuple(
        {
            **original_link,
            "link_id": f"{index:064x}",
            "value_pointer": f"{original_link['value_pointer']}/{index}",
        }
        for index in range(1025)
    )
    with pytest.raises(FinancialObservationStoreError, match="SQL row budget"):
        store._freeze_prepared_rows(
            replace(rows, links_by_observation={observation_id: oversized_group})
        )
    quarantine_fixture = _fixture(tmp_path / "quarantine", include_disclosure_date=False)
    quarantine_rows = store._materialize_prepared_capture(quarantine_fixture.prepared)
    with pytest.raises(FinancialObservationStoreError, match="SQL row budget"):
        store._freeze_prepared_rows(
            replace(quarantine_rows, quarantine=quarantine_rows.quarantine * 65)
        )
    monkeypatch.setattr(store, "_MAX_SQL_STATEMENTS_PER_CAPTURE", 1)
    with pytest.raises(FinancialObservationStoreError, match="statement budget"):
        store._freeze_prepared_rows(rows)


def test_persistence_phase_does_not_read_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")

    def reject_file_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("persistence phase attempted filesystem access")

    monkeypatch.setattr(RawResponseArtifactReader, "read", reject_file_read)
    monkeypatch.setattr(Path, "read_bytes", reject_file_read)
    try:
        with engine.begin() as connection:
            _begin_immediate(connection)
            receipt = persist_prepared_financial_observation_capture(
                connection, prepared=fixture.prepared
            )
            assert receipt.inserted_capture is True
            assert _counts(connection) == {
                _CAPTURES: 1,
                _OBSERVATIONS: len(fixture.result.observations),
                _LINKS: len(fixture.result.observations),
                _QUARANTINE: 0,
            }
    finally:
        engine.dispose()


def test_store_savepoint_respects_caller_rollback_from_distinct_observer(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    try:
        with engine.connect() as writer:
            transaction = writer.begin()
            with engine.connect() as observer:
                writer_dbapi = writer.connection.driver_connection
                observer_dbapi = observer.connection.driver_connection
                assert observer_dbapi is not writer_dbapi

                # A logical SQLAlchemy transaction without a DBAPI BEGIN must fail closed.
                assert not getattr(writer_dbapi, "in_transaction", False)
                with pytest.raises(FinancialObservationStoreTransactionError):
                    persist_prepared_financial_observation_capture(
                        writer, prepared=fixture.prepared
                    )
                assert _counts(observer) == {
                    _CAPTURES: 0,
                    _OBSERVATIONS: 0,
                    _LINKS: 0,
                    _QUARANTINE: 0,
                }
                observer.rollback()

                writer.exec_driver_sql("BEGIN IMMEDIATE")
                persist_prepared_financial_observation_capture(writer, prepared=fixture.prepared)
                # A separate live connection cannot see the writer's uncommitted rows.
                assert _counts(observer) == {
                    _CAPTURES: 0,
                    _OBSERVATIONS: 0,
                    _LINKS: 0,
                    _QUARANTINE: 0,
                }
                # End the observer read before rollback so it cannot hide committed state
                # behind an old snapshot.
                observer.rollback()
                transaction.rollback()
                assert _counts(observer) == {
                    _CAPTURES: 0,
                    _OBSERVATIONS: 0,
                    _LINKS: 0,
                    _QUARANTINE: 0,
                }
                observer.rollback()
    finally:
        engine.dispose()


def test_sqlite_contending_begin_immediate_failure_is_fail_closed_without_partial_rows(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    try:
        with engine.connect() as owner, engine.connect() as contender:
            owner_transaction = owner.begin()
            contender_transaction = contender.begin()
            _begin_immediate(owner)
            contender.exec_driver_sql("PRAGMA busy_timeout=0")
            with pytest.raises(sa.exc.OperationalError):
                contender.exec_driver_sql("BEGIN IMMEDIATE")
            with pytest.raises(FinancialObservationStoreTransactionError):
                persist_prepared_financial_observation_capture(contender, prepared=fixture.prepared)

            with engine.connect() as observer:
                assert _counts(observer) == {
                    _CAPTURES: 0,
                    _OBSERVATIONS: 0,
                    _LINKS: 0,
                    _QUARANTINE: 0,
                }

            persist_prepared_financial_observation_capture(owner, prepared=fixture.prepared)
            owner_transaction.rollback()
            contender_transaction.rollback()

        with engine.connect() as observer:
            assert _counts(observer) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_digest", ["../outside", "/etc/passwd", "g" * 64])
def test_content_addressed_key_builder_rejects_path_input(bad_digest: str) -> None:
    with pytest.raises(FinancialObservationStoreError):
        _content_addressed_key(bad_digest, suffix=".blob")


@dataclass(slots=True)
class _MysqlPersistWorker:
    """Bounded state shared by one opt-in MySQL writer thread."""

    started: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    connection_id: int | None = None
    receipt: FinancialObservationStoreReceipt | None = None
    error: BaseException | None = None
    thread: threading.Thread | None = None
    driver_connection: object | None = None


@pytest.fixture(scope="session")
def _mysql_financial_archive_engine() -> Iterator[Engine]:
    """Connect only to the dedicated one-run socket target supplied by the audit script."""
    if os.environ.get(_MYSQL_ACCEPTANCE_GATE) != "1":
        pytest.skip("private MySQL acceptance requires OD_FINANCIAL_ARCHIVE_ACCEPTANCE=1")

    required_environment = (
        "OD_FINANCIAL_ARCHIVE_MYSQL_URL",
        "OD_FINANCIAL_ARCHIVE_MYSQL_SCHEMA",
        "OD_FINANCIAL_ARCHIVE_MYSQL_SOCKET",
        "OD_FINANCIAL_ARCHIVE_MYSQL_DATADIR",
        "OD_FINANCIAL_ARCHIVE_MYSQL_SERVER_UUID",
        "APP_ENV",
        "TESTING",
        "REDIS_URL",
        "SENTRY_DSN",
        "HOME",
        "TMPDIR",
    )
    missing = [name for name in required_environment if name not in os.environ]
    if missing:
        pytest.fail(f"private MySQL acceptance is missing required environment: {missing}")
    assert os.environ["APP_ENV"] == "testing"
    assert os.environ["TESTING"] == "true"
    assert os.environ["REDIS_URL"] == ""
    assert os.environ["SENTRY_DSN"] == ""
    if "PYTEST_XDIST_WORKER" in os.environ:
        pytest.fail("private MySQL acceptance must run without xdist workers")

    schema = os.environ["OD_FINANCIAL_ARCHIVE_MYSQL_SCHEMA"]
    socket_value = os.environ["OD_FINANCIAL_ARCHIVE_MYSQL_SOCKET"]
    datadir_value = os.environ["OD_FINANCIAL_ARCHIVE_MYSQL_DATADIR"]
    server_uuid = os.environ["OD_FINANCIAL_ARCHIVE_MYSQL_SERVER_UUID"]
    if _MYSQL_ACCEPTANCE_SCHEMA.fullmatch(schema) is None:
        pytest.fail("private MySQL acceptance schema is not a per-run random schema")
    if _MYSQL_ACCEPTANCE_UUID.fullmatch(server_uuid) is None:
        pytest.fail("private MySQL acceptance server UUID is malformed")

    socket_path = Path(socket_value)
    if (
        not socket_path.is_absolute()
        or socket_path.is_symlink()
        or socket_path.parent.is_symlink()
        or not socket_path.is_socket()
    ):
        pytest.fail("private MySQL acceptance requires its explicit Unix socket")
    run_dir = socket_path.parent.resolve(strict=True)
    lexical_datadir = run_dir / "data"
    datadir = Path(datadir_value)
    if (
        run_dir.parent != Path("/tmp")
        or not run_dir.name.startswith("od204-fa-mysql.")
        or socket_path.parent != run_dir
        or run_dir.is_symlink()
        or socket_path != run_dir / "mysql.sock"
        or lexical_datadir.is_symlink()
        or datadir != lexical_datadir
        or datadir.is_symlink()
    ):
        pytest.fail("private MySQL acceptance paths do not identify one disposable instance")
    expected_datadir = lexical_datadir.resolve(strict=True)
    for private_directory in (run_dir, datadir, run_dir / "home", run_dir / "tmp"):
        metadata = private_directory.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            pytest.fail("private MySQL acceptance directory ownership or mode is unsafe")
    socket_metadata = socket_path.stat()
    if socket_metadata.st_uid != os.geteuid():
        pytest.fail("private MySQL Unix socket is not owned by the test user")
    home_path = Path(os.environ["HOME"])
    tmp_path_value = Path(os.environ["TMPDIR"])
    if (
        home_path.resolve(strict=True) != run_dir / "home"
        or home_path.is_symlink()
        or tmp_path_value.resolve(strict=True) != run_dir / "tmp"
        or tmp_path_value.is_symlink()
    ):
        pytest.fail("private MySQL acceptance HOME/TMPDIR are outside the disposable run")

    try:
        url = make_url(os.environ["OD_FINANCIAL_ARCHIVE_MYSQL_URL"])
    except (TypeError, ValueError, sa.exc.ArgumentError):
        pytest.fail("private MySQL acceptance URL is malformed")
    socket_query = url.query.get("unix_socket")
    if (
        url.drivername != "mysql+pymysql"
        or url.username != "root"
        or url.password is not None
        or url.host != "localhost"
        or url.port is not None
        or url.database != schema
        or socket_query != socket_value
        or set(url.query) != {"unix_socket", "charset"}
        or url.query.get("charset") != "utf8mb4"
    ):
        pytest.fail("private MySQL URL must target only the supplied local Unix socket/schema")

    engine = sa.create_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": _MYSQL_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": _MYSQL_SOCKET_IO_TIMEOUT_SECONDS,
            "write_timeout": _MYSQL_SOCKET_IO_TIMEOUT_SECONDS,
        },
    )

    @event.listens_for(engine, "connect")
    def revalidate_private_mysql_identity(dbapi_connection: object, _: object) -> None:
        """Validate every NullPool DBAPI connection without recursively connecting."""
        current_socket = Path(socket_value)
        current_datadir = run_dir / "data"
        if (
            current_socket.is_symlink()
            or current_socket.parent.is_symlink()
            or current_socket.parent != run_dir
            or not current_socket.is_socket()
            or current_socket.stat().st_uid != os.geteuid()
            or current_datadir.is_symlink()
        ):
            raise RuntimeError("private MySQL socket or datadir changed during acceptance")
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(
                "SELECT DATABASE(), @@server_uuid, @@datadir, @@socket, @@skip_networking"
            )
            identity = cursor.fetchone()
            if not isinstance(identity, tuple) or len(identity) != 5:
                raise RuntimeError("private MySQL identity query returned an invalid result")
            db_name, current_uuid, current_datadir_value, current_socket_value, skip_networking = (
                identity
            )
            if (
                db_name != schema
                or current_uuid != server_uuid
                or Path(str(current_datadir_value)).resolve(strict=True) != expected_datadir
                or Path(str(current_socket_value)).resolve(strict=True) != current_socket.resolve()
                or int(skip_networking) != 1
            ):
                raise RuntimeError("new MySQL connection failed the private identity guard")
        finally:
            cursor.close()
            dbapi_connection.rollback()

    try:
        with engine.connect() as connection:
            if (
                connection.dialect.name != "mysql"
                or getattr(connection.dialect, "is_mariadb", False)
                or connection.dialect.server_version_info is None
                or len(connection.dialect.server_version_info) < 3
                or connection.dialect.server_version_info[0] != 8
                or connection.dialect.server_version_info[:3] < (8, 0, 16)
            ):
                pytest.fail("private financial archive acceptance requires MySQL 8")
            identity = (
                connection.execute(
                    sa.text(
                        "SELECT DATABASE() AS schema_name, @@server_uuid AS server_uuid, "
                        "@@datadir AS datadir, @@socket AS socket, "
                        "@@skip_networking AS skip_networking"
                    )
                )
                .mappings()
                .one()
            )
            if (
                identity["schema_name"] != schema
                or identity["server_uuid"] != server_uuid
                or Path(str(identity["datadir"])).resolve(strict=True) != expected_datadir
                or Path(str(identity["socket"])).resolve(strict=True) != socket_path.resolve()
                or int(identity["skip_networking"]) != 1
            ):
                pytest.fail("connected MySQL identity does not match the audited disposable target")
            engines = dict(
                connection.execute(
                    sa.text(
                        "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME IN "
                        "(:captures, :observations, :links, :quarantine, :version_table)"
                    ),
                    {
                        "schema": schema,
                        "captures": _CAPTURES,
                        "observations": _OBSERVATIONS,
                        "links": _LINKS,
                        "quarantine": _QUARANTINE,
                        "version_table": "alembic_version_financial_archive",
                    },
                ).all()
            )
            if engines != dict.fromkeys(_MYSQL_ACCEPTANCE_TABLES, "InnoDB"):
                pytest.fail(
                    "all archive and version tables must exist as InnoDB in the target schema"
                )
            # Fail closed before any write if the server cannot expose real InnoDB lock waits.
            connection.scalar(sa.text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))
    except BaseException:
        engine.dispose()
        raise

    try:
        yield engine
    finally:
        engine.dispose()


def _mysql_capture_state(connection: Connection, capture_id: str) -> dict[str, int]:
    """Count one capture and only the observations linked to that capture."""
    captures = _table(connection, _CAPTURES)
    links = _table(connection, _LINKS)
    quarantine = _table(connection, _QUARANTINE)
    return {
        "captures": connection.scalar(
            sa.select(sa.func.count())
            .select_from(captures)
            .where(captures.c.capture_id == capture_id)
        )
        or 0,
        "observations": connection.scalar(
            sa.select(sa.func.count(sa.distinct(links.c.observation_id))).where(
                links.c.capture_id == capture_id
            )
        )
        or 0,
        "links": connection.scalar(
            sa.select(sa.func.count()).select_from(links).where(links.c.capture_id == capture_id)
        )
        or 0,
        "quarantine": connection.scalar(
            sa.select(sa.func.count())
            .select_from(quarantine)
            .where(quarantine.c.capture_id == capture_id)
        )
        or 0,
    }


def _assert_mysql_fixture_root(tmp_path: Path) -> None:
    """Keep every opt-in raw artifact beneath the audited invocation's private tmpdir."""
    private_tmpdir = Path(os.environ["TMPDIR"]).resolve(strict=True)
    try:
        tmp_path.resolve(strict=True).relative_to(private_tmpdir)
    except ValueError:
        pytest.fail("MySQL acceptance raw fixtures must stay under its private TMPDIR")


def _mysql_values_for_capture(connection: Connection, capture_id: str) -> dict[str, str | None]:
    """Read persisted exact decimal text through one capture's immutable links."""
    observations = _table(connection, _OBSERVATIONS)
    links = _table(connection, _LINKS)
    rows = connection.execute(
        sa.select(observations.c.item_code, observations.c.value_decimal_text)
        .select_from(
            observations.join(links, observations.c.observation_id == links.c.observation_id)
        )
        .where(links.c.capture_id == capture_id)
    ).all()
    return {str(row.item_code): row.value_decimal_text for row in rows}


def _start_mysql_persist_worker(
    engine: Engine,
    prepared: PreparedCapture,
) -> _MysqlPersistWorker:
    """Start one isolated writer with a bounded InnoDB lock wait timeout."""
    state = _MysqlPersistWorker()

    def persist_in_thread() -> None:
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    state.driver_connection = connection.connection.driver_connection
                    connection.exec_driver_sql(
                        f"SET SESSION innodb_lock_wait_timeout = {_MYSQL_LOCK_WAIT_TIMEOUT_SECONDS}"
                    )
                    state.connection_id = int(connection.scalar(sa.text("SELECT CONNECTION_ID()")))
                    state.started.set()
                    state.receipt = persist_prepared_financial_observation_capture(
                        connection, prepared=prepared
                    )
                    transaction.commit()
                except BaseException as exc:
                    if transaction.is_active:
                        transaction.rollback()
                    state.error = exc
        except BaseException as exc:
            state.error = exc
        finally:
            state.done.set()

    worker = threading.Thread(
        target=persist_in_thread,
        name="financial-store-mysql-writer",
        daemon=True,
    )
    worker.start()
    state.thread = worker
    return state


def _await_mysql_lock_wait(engine: Engine, worker: _MysqlPersistWorker) -> None:
    """Require the server to report the worker's actual InnoDB lock wait."""
    if not worker.started.wait(timeout=5) or worker.connection_id is None:
        pytest.fail("MySQL writer did not start within its bounded setup timeout")
    deadline = time.monotonic() + _MYSQL_LOCK_OBSERVATION_TIMEOUT_SECONDS
    wait_query = sa.text(
        "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS waits "
        "JOIN performance_schema.threads AS requester "
        "ON requester.THREAD_ID = waits.REQUESTING_THREAD_ID "
        "WHERE requester.PROCESSLIST_ID = :connection_id"
    )
    while time.monotonic() < deadline:
        if worker.done.is_set():
            error_name = type(worker.error).__name__ if worker.error is not None else "no error"
            pytest.fail(f"MySQL writer completed before a lock wait was observed ({error_name})")
        with engine.connect() as observer:
            lock_waits = observer.scalar(wait_query, {"connection_id": worker.connection_id}) or 0
        if lock_waits > 0:
            return
        time.sleep(0.05)
    pytest.fail("server did not expose the competing writer in InnoDB LOCK WAIT")


def _join_mysql_worker(worker: _MysqlPersistWorker) -> None:
    """Join after bounded DB timeouts, abort its socket on timeout, then join once more."""
    thread = worker.thread
    assert thread is not None
    thread.join(timeout=_MYSQL_WORKER_JOIN_TIMEOUT_SECONDS)
    if thread.is_alive():
        close = getattr(worker.driver_connection, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        thread.join(timeout=_MYSQL_WORKER_ABORT_JOIN_TIMEOUT_SECONDS)
    if thread.is_alive():
        pytest.fail("MySQL writer remained active after bounded socket-abort cleanup")


def _mysql_receipt(worker: _MysqlPersistWorker) -> FinancialObservationStoreReceipt:
    """Return worker outcome while keeping captured exceptions free of source data."""
    if worker.error is not None:
        raise worker.error
    if worker.receipt is None:
        pytest.fail("MySQL writer exited without a receipt")
    return worker.receipt


@pytest.mark.skipif(
    os.environ.get(_MYSQL_ACCEPTANCE_GATE) != "1",
    reason="requires the audited private MySQL acceptance script",
)
def test_mysql_archive_acceptance_commit_rollback_and_exact_decimal_values(
    tmp_path: Path,
    _mysql_financial_archive_engine: Engine,
) -> None:
    engine = _mysql_financial_archive_engine
    _assert_mysql_fixture_root(tmp_path)
    rollback_fixture = _fixture(tmp_path / "rollback-raw")
    committed_fixture = _fixture(tmp_path / "commit-raw")
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            persist_prepared_financial_observation_capture(
                connection, prepared=rollback_fixture.prepared
            )
            values = _mysql_values_for_capture(connection, rollback_fixture.prepared._capture_id)
            item_codes = financial_statement_items("income")
            assert values[item_codes[0]] == "12345678901234567890.123456789012345678"
            if len(item_codes) > 1:
                assert item_codes[1] in values and values[item_codes[1]] is None
            if len(item_codes) > 2:
                assert values[item_codes[2]] == "0"
            transaction.rollback()
        with engine.connect() as observer:
            assert _mysql_capture_state(observer, rollback_fixture.prepared._capture_id) == {
                "captures": 0,
                "observations": 0,
                "links": 0,
                "quarantine": 0,
            }

        with engine.begin() as connection:
            persist_prepared_financial_observation_capture(
                connection, prepared=committed_fixture.prepared
            )
        with engine.connect() as observer:
            assert _mysql_capture_state(observer, committed_fixture.prepared._capture_id) == {
                "captures": 1,
                "observations": len(committed_fixture.result.observations),
                "links": len(committed_fixture.result.observations),
                "quarantine": 0,
            }
            committed_values = _mysql_values_for_capture(
                observer, committed_fixture.prepared._capture_id
            )
            assert committed_values[item_codes[0]] == ("12345678901234567890.123456789012345678")
            if len(item_codes) > 1:
                assert committed_values[item_codes[1]] is None
            if len(item_codes) > 2:
                assert committed_values[item_codes[2]] == "0"
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.environ.get(_MYSQL_ACCEPTANCE_GATE) != "1",
    reason="requires the audited private MySQL acceptance script",
)
def test_mysql_archive_acceptance_same_capture_replay_waits_for_lock(
    tmp_path: Path,
    _mysql_financial_archive_engine: Engine,
) -> None:
    engine = _mysql_financial_archive_engine
    _assert_mysql_fixture_root(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    worker: _MysqlPersistWorker | None = None
    try:
        with engine.connect() as owner:
            transaction = owner.begin()
            persist_prepared_financial_observation_capture(owner, prepared=fixture.prepared)
            worker = _start_mysql_persist_worker(engine, fixture.prepared)
            try:
                assert worker is not None
                _await_mysql_lock_wait(engine, worker)
                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()
                assert worker is not None
                _join_mysql_worker(worker)
        receipt = _mysql_receipt(worker)
        assert receipt.inserted_capture is False
        assert receipt.inserted_observations == 0
        assert receipt.inserted_links == 0
        assert receipt.inserted_quarantine_rows == 0
        with engine.connect() as observer:
            assert _mysql_capture_state(observer, fixture.prepared._capture_id) == {
                "captures": 1,
                "observations": len(fixture.result.observations),
                "links": len(fixture.result.observations),
                "quarantine": 0,
            }
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.environ.get(_MYSQL_ACCEPTANCE_GATE) != "1",
    reason="requires the audited private MySQL acceptance script",
)
def test_mysql_archive_acceptance_shared_observation_waits_for_lock(
    tmp_path: Path,
    _mysql_financial_archive_engine: Engine,
) -> None:
    engine = _mysql_financial_archive_engine
    _assert_mysql_fixture_root(tmp_path)
    first_seen = datetime.now(timezone.utc) - timedelta(days=1)
    first = _fixture(tmp_path / "first-raw", observed_at=first_seen)
    second = _fixture(
        tmp_path / "second-raw",
        observed_at=first_seen + timedelta(minutes=1),
    )
    assert first.prepared._capture_id != second.prepared._capture_id
    assert {
        observation.observation.observation_id for observation in first.result.observations
    } == {observation.observation.observation_id for observation in second.result.observations}
    worker: _MysqlPersistWorker | None = None
    try:
        with engine.connect() as owner:
            transaction = owner.begin()
            persist_prepared_financial_observation_capture(owner, prepared=first.prepared)
            worker = _start_mysql_persist_worker(engine, second.prepared)
            try:
                assert worker is not None
                _await_mysql_lock_wait(engine, worker)
                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()
                assert worker is not None
                _join_mysql_worker(worker)
        receipt = _mysql_receipt(worker)
        assert receipt.inserted_capture is True
        assert receipt.inserted_observations == 0
        assert receipt.inserted_links == len(second.result.observations)
        assert receipt.inserted_quarantine_rows == 0
        with engine.connect() as observer:
            assert _mysql_capture_state(observer, first.prepared._capture_id) == {
                "captures": 1,
                "observations": len(first.result.observations),
                "links": len(first.result.observations),
                "quarantine": 0,
            }
            assert _mysql_capture_state(observer, second.prepared._capture_id) == {
                "captures": 1,
                "observations": len(second.result.observations),
                "links": len(second.result.observations),
                "quarantine": 0,
            }
    finally:
        engine.dispose()


@pytest.mark.skipif(
    os.environ.get(_MYSQL_ACCEPTANCE_GATE) != "1",
    reason="requires the audited private MySQL acceptance script",
)
def test_mysql_archive_acceptance_same_capture_conflict_waits_for_lock(
    tmp_path: Path,
    _mysql_financial_archive_engine: Engine,
) -> None:
    engine = _mysql_financial_archive_engine
    _assert_mysql_fixture_root(tmp_path)
    fixture = _fixture(tmp_path / "raw")
    changed_result = replace(
        fixture.result,
        native_unit_mapping_version_declared="operator-unit-conflict-v1",
        native_unit_mapping_sha256_declared="c" * 64,
    )
    conflicting = _prepare(fixture, result=changed_result)
    worker: _MysqlPersistWorker | None = None
    try:
        with engine.connect() as owner:
            transaction = owner.begin()
            persist_prepared_financial_observation_capture(owner, prepared=fixture.prepared)
            worker = _start_mysql_persist_worker(engine, conflicting)
            try:
                assert worker is not None
                _await_mysql_lock_wait(engine, worker)
                transaction.commit()
            finally:
                if transaction.is_active:
                    transaction.rollback()
                assert worker is not None
                _join_mysql_worker(worker)
        assert worker is not None
        assert isinstance(worker.error, FinancialObservationStoreConflictError)
        with engine.connect() as observer:
            assert _mysql_capture_state(observer, fixture.prepared._capture_id) == {
                "captures": 1,
                "observations": len(fixture.result.observations),
                "links": len(fixture.result.observations),
                "quarantine": 0,
            }
    finally:
        engine.dispose()


def _claim_synthetic_archive_run(engine: Engine) -> tuple[RunLedger, RunRecord]:
    """Create a test-only ledger row with a live fenced lease on this temp DB."""
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    run_key = "synthetic-financial-archive-run"
    ledger.enqueue(run_key, '{"kind":"synthetic_archive_test"}')
    claimed_at = datetime.now(timezone.utc)
    claim = ledger.claim("synthetic-archive-worker", claimed_at, timedelta(seconds=60))
    assert claim is not None
    assert claim.lease_expires_at is not None
    return ledger, claim


def _archive_counts_with_quarantine(fixture: _Fixture) -> dict[str, int]:
    """Return expected row counts for the explicitly synthetic response fixture."""
    assert fixture.result.quarantined_rows
    return {
        _CAPTURES: 1,
        _OBSERVATIONS: len(fixture.result.observations),
        _LINKS: len(fixture.result.observations),
        _QUARANTINE: len(fixture.result.quarantined_rows),
    }


def _persist_synthetic_archive_callback(
    connection: Connection,
    fixture: _Fixture,
) -> FinancialObservationStoreReceipt:
    """Persist only pre-prepared synthetic fixture scalars through the supplied connection."""
    return persist_prepared_financial_observation_capture(
        connection,
        prepared=fixture.prepared,
    )


def test_run_ledger_commit_result_atomically_persists_synthetic_financial_capture(
    tmp_path: Path,
) -> None:
    """Commit synthetic archive tables and run success under one live lease.

    The input is locally generated Fuyao-shaped JSON. It is not an upstream
    response and does not establish Fuyao provider or data-quality acceptance.
    """
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "synthetic-raw", include_quarantined_row=True)
    expected_counts = _archive_counts_with_quarantine(fixture)
    ledger, claim = _claim_synthetic_archive_run(engine)
    try:
        assert claim.lease_expires_at is not None
        commit_result = ledger.commit_result(
            claim.run_key,
            worker="synthetic-archive-worker",
            fencing_token=claim.fencing_token,
            now=claim.updated_at,
            operation=lambda connection: _persist_synthetic_archive_callback(connection, fixture),
            clock=lambda: claim.updated_at + timedelta(seconds=1),
        )
        assert commit_result is not None
        committed, receipt = commit_result
        assert committed.status == "succeeded"
        assert committed.fencing_token == claim.fencing_token
        assert receipt.capture_id == fixture.prepared._capture_id
        assert receipt.inserted_capture is True
        assert receipt.inserted_observations == expected_counts[_OBSERVATIONS]
        assert receipt.inserted_links == expected_counts[_LINKS]
        assert receipt.inserted_quarantine_rows == expected_counts[_QUARANTINE]
        with engine.connect() as observer:
            assert _counts(observer) == expected_counts
        committed_state = ledger.get(claim.run_key)
        assert committed_state is not None
        assert committed_state.status == "succeeded"
    finally:
        engine.dispose()


def test_run_ledger_callback_error_rolls_back_synthetic_archive_and_run_success(
    tmp_path: Path,
) -> None:
    """Roll back archive rows when the SQL callback fails after inserting them."""
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "synthetic-raw", include_quarantined_row=True)
    ledger, claim = _claim_synthetic_archive_run(engine)

    def persist_then_fail(connection: Connection) -> None:
        _persist_synthetic_archive_callback(connection, fixture)
        raise RuntimeError("synthetic callback failure")

    try:
        with pytest.raises(RuntimeError, match="synthetic callback failure"):
            ledger.commit_result(
                claim.run_key,
                worker="synthetic-archive-worker",
                fencing_token=claim.fencing_token,
                now=claim.updated_at,
                operation=persist_then_fail,
                clock=lambda: claim.updated_at + timedelta(seconds=1),
            )
        with engine.connect() as observer:
            assert _counts(observer) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
        current = ledger.get(claim.run_key)
        assert current is not None
        assert current.status == "running"
        assert current.fencing_token == claim.fencing_token
        assert current.worker == "synthetic-archive-worker"
    finally:
        engine.dispose()


def test_run_ledger_expired_commit_clock_rolls_back_synthetic_archive_and_run_success(
    tmp_path: Path,
) -> None:
    """Roll back archive rows when the lease expires after callback SQL completes."""
    engine = _engine(tmp_path)
    fixture = _fixture(tmp_path / "synthetic-raw", include_quarantined_row=True)
    ledger, claim = _claim_synthetic_archive_run(engine)
    assert claim.lease_expires_at is not None
    receipts: list[FinancialObservationStoreReceipt] = []

    def persist_and_record_receipt(connection: Connection) -> FinancialObservationStoreReceipt:
        receipt = _persist_synthetic_archive_callback(connection, fixture)
        receipts.append(receipt)
        return receipt

    try:
        with pytest.raises(RunLeaseExpiredError, match="lease expired"):
            ledger.commit_result(
                claim.run_key,
                worker="synthetic-archive-worker",
                fencing_token=claim.fencing_token,
                now=claim.updated_at,
                operation=persist_and_record_receipt,
                clock=lambda: claim.lease_expires_at,
            )
        assert len(receipts) == 1
        assert receipts[0].capture_id == fixture.prepared._capture_id
        assert receipts[0].inserted_capture is True
        assert receipts[0].inserted_observations == len(fixture.result.observations)
        assert receipts[0].inserted_links == len(fixture.result.observations)
        assert receipts[0].inserted_quarantine_rows == len(fixture.result.quarantined_rows)
        with engine.connect() as observer:
            assert _counts(observer) == {
                _CAPTURES: 0,
                _OBSERVATIONS: 0,
                _LINKS: 0,
                _QUARANTINE: 0,
            }
        current = ledger.get(claim.run_key)
        assert current is not None
        assert current.status == "running"
        assert current.fencing_token == claim.fencing_token
        assert current.worker == "synthetic-archive-worker"
        assert current.lease_id == claim.lease_id
        assert current.lease_expires_at == claim.lease_expires_at
    finally:
        engine.dispose()
