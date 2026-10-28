"""Behavior tests for the Fuyao capture bridge and SQL-only archive store."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from types import ModuleType

    from sqlalchemy.engine import Engine

from opendata.data.providers.ths.endpoints import (
    financial_statement_items,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    FinancialObservationFetchResult,
    ThsFinancialNativeUnitProfile,
    canonical_native_unit_mapping_sha256,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    decode_financial_observation_body,
)
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    TransportAttempt,
)
from opendata.pipeline.financial_observation_store import (
    FinancialObservationStoreError,
    FinancialObservationStoreTransactionError,
    persist_prepared_financial_observation_capture,
)
from opendata.pipeline.fuyao_observation_capture import (
    FuyaoCaptureProvenance,
    FuyaoSourceAttemptDeclaration,
    prepare_fuyao_observation_capture,
)
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.raw_evidence_reader import RawResponseArtifactReader

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = (
    _ROOT
    / "alembic_financial_archive"
    / "versions"
    / "20261027-0001_financial_observation_archive.py"
)
_RAW_SOURCE = RawResponseSource(
    provider_id="ths",
    dataset_id="ths_financial_statement",
    operation_id="income_annual",
)
_INSTRUMENT_ID = "600519.SH"
_OBSERVED_AT = datetime(2025, 4, 2, 4, 0, tzinfo=timezone.utc)
_REPORT_PERIOD = date(2024, 12, 31)
_DISCLOSURE_DATE = date(2025, 3, 31)
_NATIVE_VALUE = "12345678901234567890.123456789012345678"


def _load_migration() -> ModuleType:
    """Load the archive migration without loading application configuration."""
    spec = importlib.util.spec_from_file_location(
        "financial_observation_capture_service_test_migration", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def archive_engine(tmp_path: Path) -> Iterator[Engine]:
    """Create an isolated SQLite archive using the repository migration."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'test_financial_archive_capture.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()


def _inputs(
    root: Path,
) -> tuple[
    FinancialObservationFetchResult,
    ThsFinancialNativeUnitProfile,
    FuyaoCaptureProvenance,
    FuyaoSourceAttemptDeclaration,
    RawResponseArtifactWriter,
]:
    """Build one synthetic, source-shaped annual income statement response."""
    item_codes = financial_statement_items("income")
    unit_map = dict.fromkeys(item_codes, "CNY")
    profile_version = "synthetic-capture-service-v1"
    profile = ThsFinancialNativeUnitProfile(
        statement_type="income",
        version=profile_version,
        native_units=unit_map,
        mapping_sha256_declared=canonical_native_unit_mapping_sha256(
            statement_type="income",
            version=profile_version,
            native_units=unit_map,
        ),
        evidence_ref="synthetic:test-fixture-only",
    )
    row: dict[str, object] = {
        "thscode": _INSTRUMENT_ID,
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": shanghai_midnight_millis(_REPORT_PERIOD),
        "report_date_ms": shanghai_midnight_millis(_DISCLOSURE_DATE),
        "currency": "CNY",
    }
    row.update(dict.fromkeys(item_codes, "0"))
    row[item_codes[0]] = _NATIVE_VALUE
    row[item_codes[1]] = None
    body = json.dumps(
        {
            "code": 0,
            "message": "ok",
            "request_id": "synthetic-provider-request-01",
            "data": {
                "timestamp": row["period_end_ms"],
                "item": [row],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    decoded = decode_financial_observation_body(
        body,
        expected_instrument_id=_INSTRUMENT_ID,
        statement_type="income",
        periodicity="annual",
        observed_at=_OBSERVED_AT,
        native_units=profile.native_units,
        native_unit_mapping_version_declared=profile.version,
        native_unit_mapping_sha256_declared=profile.mapping_sha256_declared,
    )
    request_digest = request_summary_sha256(
        {"thscode": _INSTRUMENT_ID, "period": "annual"},
        allowed_fields={"thscode", "period", "start", "end", "limit"},
    )
    fetch_result = FinancialObservationFetchResult(
        instrument_id=_INSTRUMENT_ID,
        statement_type="income",
        periodicity="annual",
        source_request_id=decoded.source_request_id,
        body_sha256=hashlib.sha256(body).hexdigest(),
        observed_at=_OBSERVED_AT,
        status_code=200,
        unit_mapping_version_declared=profile.version,
        unit_mapping_sha256_declared=profile.mapping_sha256_declared,
        unit_mapping_evidence_kind=profile.evidence_kind,
        coverage_assessment="not_assessed",
        usability_status="USABLE_OBSERVATIONS_PRESENT",
        raw_body=body,
        observations=decoded.observations,
        quarantined_rows=decoded.quarantined_rows,
        request_summary_sha256=request_digest,
    )
    provider_host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert provider_host is not None
    attempt = TransportAttempt(
        attempt_id="synthetic-attempt-01",
        request_id="synthetic-transport-request-01",
        attempt_no=1,
        grant_id="synthetic-grant-01",
        expected_revision=1,
        source="ths",
        canonical_model="financial_statement",
        operation="query",
        principal_scope="synthetic-operator",
        host=provider_host,
    )
    declaration = FuyaoSourceAttemptDeclaration(
        attempt=attempt,
        reservation=AttemptReservation(
            status=ReservationStatus.RESERVED_NEW,
            reserved_at_us=int(_OBSERVED_AT.timestamp() * 1_000_000) - 1,
        ),
    )
    provenance = FuyaoCaptureProvenance(
        raw_source=_RAW_SOURCE,
        retention_class="restricted",
        source_history_limit="unknown",
    )
    return (
        fetch_result,
        profile,
        provenance,
        declaration,
        RawResponseArtifactWriter(root, max_response_bytes=1024 * 1024),
    )


def _begin_sqlite_transaction(connection: sa.Connection) -> None:
    """Open SQLite's physical transaction before archive callbacks run."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def test_bridge_and_store_persist_exact_raw_entity_and_source_dates_values_and_null(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """The committed SQL row preserves its source value, dates and explicit null."""
    raw_root = tmp_path / "raw-evidence"
    fetch_result, profile, provenance, declaration, writer = _inputs(raw_root)
    capture = prepare_fuyao_observation_capture(
        fetch_result,
        unit_profile=profile,
        provenance=provenance,
        source_attempt_declaration=declaration,
        writer=writer,
    )

    with archive_engine.connect() as connection:
        _begin_sqlite_transaction(connection)
        receipt = persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )
        assert receipt.inserted_capture is True
        assert receipt.inserted_observations == len(fetch_result.observations)
        connection.commit()

    reader = RawResponseArtifactReader(raw_root)
    evidence = reader.read(
        capture.manifest_sha256,
        expected_source=_RAW_SOURCE,
        expected_request_summary_sha256=fetch_result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
        expected_http_status=fetch_result.status_code,
    )
    assert evidence.body == fetch_result.raw_body

    with archive_engine.connect() as connection:
        stored = (
            connection.execute(
                sa.text(
                    "SELECT item_code, report_period, source_disclosure_date, observed_at_utc, "
                    "value_decimal_text, native_unit "
                    "FROM od_financial_observations "
                    "WHERE instrument_id = :instrument ORDER BY item_code"
                ).columns(
                    report_period=sa.Date(),
                    source_disclosure_date=sa.Date(),
                    observed_at_utc=sa.DateTime(),
                ),
                {"instrument": _INSTRUMENT_ID},
            )
            .mappings()
            .all()
        )
    by_item = {row["item_code"]: row for row in stored}
    first_code, second_code = financial_statement_items("income")[:2]
    assert by_item[first_code]["value_decimal_text"] == _NATIVE_VALUE
    assert by_item[second_code]["value_decimal_text"] is None
    for item_code in (first_code, second_code):
        assert by_item[item_code]["report_period"] == _REPORT_PERIOD
        assert by_item[item_code]["source_disclosure_date"] == _DISCLOSURE_DATE
        assert by_item[item_code]["observed_at_utc"] == _OBSERVED_AT.replace(tzinfo=None)
        assert by_item[item_code]["native_unit"] == "CNY"

    with archive_engine.connect() as connection:
        _begin_sqlite_transaction(connection)
        replay = persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )
        connection.commit()
    assert replay.capture_id == receipt.capture_id
    assert replay.inserted_capture is False
    assert replay.inserted_observations == 0
    assert replay.inserted_links == 0
    assert replay.inserted_quarantine_rows == 0


def test_sql_rollback_leaves_raw_evidence_and_prepared_capture_can_be_retried(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """A caller rollback leaves verified raw bytes and allows SQL-only retry."""
    raw_root = tmp_path / "raw-evidence-after-rollback"
    fetch_result, profile, provenance, declaration, writer = _inputs(raw_root)
    capture = prepare_fuyao_observation_capture(
        fetch_result,
        unit_profile=profile,
        provenance=provenance,
        source_attempt_declaration=declaration,
        writer=writer,
    )

    with archive_engine.connect() as connection:
        _begin_sqlite_transaction(connection)
        persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )
        connection.rollback()

    evidence = RawResponseArtifactReader(raw_root).read(
        capture.manifest_sha256,
        expected_source=_RAW_SOURCE,
        expected_request_summary_sha256=fetch_result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
        expected_http_status=fetch_result.status_code,
    )
    assert evidence.body == fetch_result.raw_body
    with archive_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_captures")) == 0

    with archive_engine.connect() as connection:
        _begin_sqlite_transaction(connection)
        retry = persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )
        connection.commit()
    assert retry.inserted_capture is True
    assert retry.inserted_observations == len(fetch_result.observations)
    with archive_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_captures")) == 1


def test_persist_rejects_missing_transaction_without_writing_sql(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """The SQL store rejects a missing transaction before writing archive rows."""
    raw_root = tmp_path / "raw-evidence-without-transaction"
    fetch_result, profile, provenance, declaration, writer = _inputs(raw_root)
    capture = prepare_fuyao_observation_capture(
        fetch_result,
        unit_profile=profile,
        provenance=provenance,
        source_attempt_declaration=declaration,
        writer=writer,
    )

    with (
        archive_engine.connect() as connection,
        pytest.raises(FinancialObservationStoreTransactionError),
    ):
        persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )

    assert raw_root.exists()
    with archive_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_captures")) == 0


def test_mid_insert_failure_rolls_back_savepoint_and_prepared_dto_can_be_retried(
    archive_engine: Engine,
    tmp_path: Path,
) -> None:
    """A failed later INSERT leaves no partial archive rows and can be retried."""
    raw_root = tmp_path / "raw-evidence-store-failure"
    fetch_result, profile, provenance, declaration, writer = _inputs(raw_root)
    capture = prepare_fuyao_observation_capture(
        fetch_result,
        unit_profile=profile,
        provenance=provenance,
        source_attempt_declaration=declaration,
        writer=writer,
    )
    expected_prepared = capture.prepared
    observation_insert_count = 0

    def fail_second_observation_insert(
        connection: sa.Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> tuple[str, object]:
        """Redirect one later INSERT to a missing SQLite table to trigger DB failure."""
        nonlocal observation_insert_count
        if statement.lstrip().upper().startswith("INSERT INTO OD_FINANCIAL_OBSERVATIONS"):
            observation_insert_count += 1
            if observation_insert_count == 2:
                return (
                    "INSERT INTO od_injected_failure_target (failure_marker) VALUES (?)",
                    ("injected",),
                )
        return statement, parameters

    successful_observation_inserts = 0

    def count_successful_observation_insert(
        connection: sa.Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """Count observation INSERT statements which reached SQLite successfully."""
        nonlocal successful_observation_inserts
        if statement.lstrip().upper().startswith("INSERT INTO OD_FINANCIAL_OBSERVATIONS"):
            successful_observation_inserts += 1

    with archive_engine.connect() as connection:
        _begin_sqlite_transaction(connection)
        event.listen(
            archive_engine,
            "before_cursor_execute",
            fail_second_observation_insert,
            retval=True,
        )
        event.listen(archive_engine, "after_cursor_execute", count_successful_observation_insert)
        try:
            with pytest.raises(
                FinancialObservationStoreError,
                match="financial archive database write failed",
            ):
                persist_prepared_financial_observation_capture(
                    connection,
                    prepared=capture.prepared,
                )
        finally:
            event.remove(archive_engine, "before_cursor_execute", fail_second_observation_insert)
            event.remove(
                archive_engine,
                "after_cursor_execute",
                count_successful_observation_insert,
            )

        assert observation_insert_count == 2
        assert successful_observation_inserts == 1
        assert connection.in_transaction()
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_captures")) == 0
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_observations")) == 0
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM od_financial_capture_observation_links")
            )
            == 0
        )
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM od_financial_observation_quarantine"))
            == 0
        )
        assert capture.prepared is expected_prepared
        retry = persist_prepared_financial_observation_capture(
            connection,
            prepared=capture.prepared,
        )
        assert retry.inserted_capture is True
        assert retry.inserted_observations == len(fetch_result.observations)
        connection.commit()

    evidence = RawResponseArtifactReader(raw_root).read(
        capture.manifest_sha256,
        expected_source=_RAW_SOURCE,
        expected_request_summary_sha256=fetch_result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
        expected_http_status=fetch_result.status_code,
    )
    assert evidence.body == fetch_result.raw_body
    with archive_engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM od_financial_captures")) == 1
