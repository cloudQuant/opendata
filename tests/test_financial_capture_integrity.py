"""Behavioral tests for the bounded read-only capture evidence audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, cast

import pytest
import sqlalchemy as sa
from sqlalchemy import event, insert

from opendata.pipeline.financial_capture_integrity import (
    FinancialCaptureIntegrityInputError,
    FinancialCaptureIntegrityQueryError,
    FinancialCaptureIntegrityReport,
    FinancialCaptureIntegrityScope,
    audit_financial_capture_integrity,
)
from opendata.pipeline.financial_observation_store import CAPTURES
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadError,
    RawEvidenceReadInputError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

_BODY = b'{"fixture":"DO_NOT_LEAK"}'
_SOURCE = RawResponseSource(
    provider_id="ths",
    dataset_id="ths_financial_statement",
    operation_id="income_annual",
)
_SCOPE = FinancialCaptureIntegrityScope(source_id="ths_fuyao", raw_source=_SOURCE)
_REQUEST_SHA256 = request_summary_sha256({"symbol": "600519"}, allowed_fields=("symbol",))


class _BrokenOffset(tzinfo):
    """Timezone object that simulates a broken caller-provided UTC offset."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ValueError("private timezone detail")

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "broken"


def _engine(tmp_path: Path) -> Engine:
    """Create an explicitly named SQLite database below the private test path."""
    return sa.create_engine(f"sqlite:///{tmp_path / 'test_financial_archive_integrity.db'}")


def _capture_row(
    tmp_path: Path,
    *,
    raw_body: bytes = _BODY,
    raw_source: RawResponseSource = _SOURCE,
    row_overrides: dict[str, object] | None = None,
) -> tuple[dict[str, object], Path, RawResponseArtifactReader]:
    """Write one synthetic content-addressed capture and return its SQL values."""
    artifact_root = tmp_path / "evidence"
    captured = RawResponseArtifactWriter(artifact_root).capture(
        [raw_body],
        source=raw_source,
        request_summary_sha256=_REQUEST_SHA256,
        http_status=200,
        retention_class="durable_candidate",
        source_history_limit="snapshot_only",
    )
    manifest = captured.manifest
    row: dict[str, object] = {
        "capture_id": manifest.capture_id,
        "provider_id": manifest.source.provider_id,
        "dataset_id": manifest.source.dataset_id,
        "operation_id": manifest.source.operation_id,
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "observed_at_utc": manifest.captured_at_utc.replace(tzinfo=None) - timedelta(seconds=1),
        "captured_at_utc": manifest.captured_at_utc.replace(tzinfo=None),
        "source_request_id": "synthetic-request-id",
        "http_status": manifest.http_status,
        "request_summary_sha256": manifest.request_summary_sha256,
        "raw_object_sha256": manifest.response_sha256,
        "raw_object_size_bytes": manifest.response_size_bytes,
        "raw_object_key": (f"{manifest.response_sha256[:2]}/{manifest.response_sha256}.blob"),
        "raw_manifest_sha256": "",
        "raw_manifest_size_bytes": len(captured.manifest_path.read_bytes()),
        "raw_manifest_key": "",
        "retention_class": manifest.retention_class,
        "source_history_limit": manifest.source_history_limit,
        "native_unit_mapping_version_declared": "synthetic-v1",
        "native_unit_mapping_sha256_declared": "f" * 64,
        "unit_mapping_declaration_status": "operator_declared",
        "decode_status": "decoded",
    }
    manifest_hash = captured.manifest_path.name.removesuffix(".json")
    row["raw_manifest_sha256"] = manifest_hash
    row["raw_manifest_key"] = f"{manifest_hash[:2]}/{manifest_hash}.json"
    if row_overrides:
        row.update(row_overrides)
    return (
        row,
        captured.artifact_path,
        RawResponseArtifactReader(artifact_root, max_response_bytes=8 * 1024 * 1024),
    )


def _insert_rows(engine: Engine, rows: list[dict[str, object]]) -> None:
    """Create only the capture table and insert synthetic setup rows."""
    with engine.begin() as connection:
        CAPTURES.create(connection)
        connection.execute(insert(CAPTURES), rows)


def _audit(
    engine: Engine,
    reader: RawResponseArtifactReader,
    *,
    page_size: int = 100,
    after_capture_id: str | None = None,
) -> FinancialCaptureIntegrityReport:
    """Call the audit with a fixed trusted bound and the explicit test scope."""
    with engine.connect() as connection:
        return audit_financial_capture_integrity(
            connection,
            reader=reader,
            scopes=(_SCOPE,),
            captured_at_not_after=datetime.now(timezone.utc) + timedelta(seconds=5),
            page_size=page_size,
            after_capture_id=after_capture_id,
        )


def test_valid_capture_is_checked_with_select_only(tmp_path: Path) -> None:
    """A synthetic writer capture verifies and emits no raw response content."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    statements: list[str] = []

    def observe_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement.lstrip().split(maxsplit=1)[0].upper())

    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        report = _audit(engine, reader)
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)
        engine.dispose()

    assert report.checked_count == 1
    assert report.invalid_count == 0
    assert report.results[0].valid
    assert report.results[0].capture_id == row["capture_id"]
    assert report.next_cursor is None
    assert report.has_more is False
    assert report.pagination_blocked is False
    assert statements == ["SELECT"]
    assert "DO_NOT_LEAK" not in repr(report)
    assert _BODY.decode() not in repr(report)


def test_sql_manifest_size_mismatch_is_reported_without_detail(tmp_path: Path) -> None:
    """A valid manifest that disagrees with its SQL row fails closed."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    row["raw_manifest_size_bytes"] = int(row["raw_manifest_size_bytes"]) + 1
    _insert_rows(engine, [row])
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.invalid_count == 1
    assert report.results[0].error_code == "capture_manifest_mismatch"
    assert "DO_NOT_LEAK" not in repr(report)


def test_sql_manifest_key_mismatch_is_rejected(tmp_path: Path) -> None:
    """A SQL manifest key cannot redirect the capture reference."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(
        tmp_path,
        row_overrides={"raw_manifest_key": "../../outside.json"},
    )
    _insert_rows(engine, [row])
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "invalid_sql_capture"


def test_sql_capture_after_trusted_bound_is_rejected_when_ordered(tmp_path: Path) -> None:
    """A future SQL capture time is rejected even when observation order is valid."""
    engine = _engine(tmp_path)
    future_capture_time = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    row, _, reader = _capture_row(
        tmp_path,
        row_overrides={"captured_at_utc": future_capture_time},
    )
    assert row["observed_at_utc"] <= row["captured_at_utc"]
    _insert_rows(engine, [row])
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "invalid_sql_capture"


def test_changed_body_is_reported_as_integrity_failure(tmp_path: Path) -> None:
    """A same-length replacement is detected by the reader's body digest."""
    engine = _engine(tmp_path)
    row, body_path, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    body_path.write_bytes(_BODY.replace(b"DO_NOT_LEAK", b"TAMPERED___"))
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "evidence_integrity_failure"
    assert "TAMPERED___" not in repr(report)
    assert "DO_NOT_LEAK" not in repr(report)


@pytest.mark.parametrize(
    ("row_overrides", "expected_error"),
    [
        (
            {
                "raw_object_sha256": "d" * 64,
                "raw_object_key": f"{'d' * 2}/{'d' * 64}.blob",
            },
            "capture_manifest_mismatch",
        ),
        ({"raw_object_key": "../../outside.blob"}, "invalid_sql_capture"),
        (
            {"captured_at_utc": datetime(2026, 1, 1, 0, 0, 0)},
            "invalid_sql_capture",
        ),
        (
            {"captured_at_utc": datetime.now(timezone.utc).replace(tzinfo=None)},
            "capture_manifest_mismatch",
        ),
        ({"request_summary_sha256": "e" * 64}, "evidence_context_mismatch"),
    ],
)
def test_sql_digest_key_time_and_request_mismatches_fail_closed(
    tmp_path: Path,
    row_overrides: dict[str, object],
    expected_error: str,
) -> None:
    """SQL body digests, keys, timestamps, and request hashes stay bound."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path, row_overrides=row_overrides)
    _insert_rows(engine, [row])
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == expected_error
    assert "DO_NOT_LEAK" not in repr(report)


@pytest.mark.parametrize(
    "row_overrides",
    [
        {"raw_manifest_key": b"../../outside.json"},
        {"raw_object_sha256": "not-a-lowercase-sha256"},
        {"raw_object_size_bytes": 0},
        {"http_status": 99},
        {"retention_class": "unapproved"},
        {"source_history_limit": "unbounded"},
    ],
)
def test_malformed_sql_metadata_is_rejected_before_read(
    tmp_path: Path,
    row_overrides: dict[str, object],
) -> None:
    """Malformed SQL scalar types and out-of-contract values fail closed."""
    engine = _engine(tmp_path)
    row, body_path, reader = _capture_row(tmp_path, row_overrides=row_overrides)
    _insert_rows(engine, [row])
    body_path.unlink()
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "invalid_sql_capture"


def test_missing_body_is_reported_as_unavailable_or_unsafe(tmp_path: Path) -> None:
    """A missing referenced body is reported without directory recovery scans."""
    engine = _engine(tmp_path)
    row, body_path, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    body_path.unlink()
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "evidence_unavailable_or_unsafe"
    assert "DO_NOT_LEAK" not in repr(report)


def test_scope_mismatch_does_not_read_raw_evidence(tmp_path: Path) -> None:
    """A SQL source identity outside the explicit allowlist is not opened."""
    engine = _engine(tmp_path)
    row, body_path, reader = _capture_row(tmp_path)
    row["dataset_id"] = "outside_authorized_scope"
    _insert_rows(engine, [row])
    body_path.unlink()
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "capture_scope_mismatch"


def test_manifest_source_mismatch_is_not_reclassified_as_allowed(tmp_path: Path) -> None:
    """The explicit allowlist is checked against the manifest as well as SQL."""
    other_source = RawResponseSource(
        provider_id="other",
        dataset_id="other_dataset",
        operation_id="other_operation",
    )
    row, _, reader = _capture_row(
        tmp_path,
        raw_source=other_source,
        row_overrides={
            "provider_id": _SOURCE.provider_id,
            "dataset_id": _SOURCE.dataset_id,
            "operation_id": _SOURCE.operation_id,
        },
    )
    engine = _engine(tmp_path)
    _insert_rows(engine, [row])
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == "evidence_context_mismatch"


def test_pages_use_capture_id_keyset_and_are_bounded(tmp_path: Path) -> None:
    """Pagination returns at most the requested count and advances by id."""
    engine = _engine(tmp_path)
    first, _, reader = _capture_row(tmp_path)
    second, _, _ = _capture_row(tmp_path)
    third, _, _ = _capture_row(tmp_path)
    _insert_rows(engine, [first, second, third])
    ordered_ids = sorted(str(row["capture_id"]) for row in (first, second, third))
    try:
        page_one = _audit(engine, reader, page_size=2)
        page_two = _audit(engine, reader, page_size=2, after_capture_id=page_one.next_cursor)
    finally:
        engine.dispose()

    assert page_one.checked_count == 2
    assert page_one.has_more is True
    assert page_one.next_cursor == ordered_ids[1]
    assert page_two.checked_count == 1
    assert page_two.has_more is False
    assert page_two.pagination_blocked is False
    assert [result.capture_id for result in page_two.results] == [ordered_ids[2]]


def test_bad_page_tail_reports_blocked_pagination(tmp_path: Path) -> None:
    """A malformed capture id cannot hide unvisited records behind a null cursor."""
    engine = _engine(tmp_path)
    first, _, reader = _capture_row(tmp_path)
    second, _, _ = _capture_row(tmp_path)
    third, _, _ = _capture_row(tmp_path)
    first["capture_id"] = "f" * 32
    second["capture_id"] = "g" * 32
    third["capture_id"] = "z" * 32
    _insert_rows(engine, [first, second, third])
    try:
        report = _audit(engine, reader, page_size=2)
    finally:
        engine.dispose()

    assert report.checked_count == 2
    assert report.has_more is True
    assert report.next_cursor is None
    assert report.pagination_blocked is True
    assert report.results[-1].error_code == "invalid_sql_capture"


def test_evidence_byte_budget_returns_resumable_cursor(tmp_path: Path) -> None:
    """The byte ceiling defers later captures and exposes a safe continuation."""
    engine = _engine(tmp_path)
    rows_and_readers = [_capture_row(tmp_path) for _ in range(3)]
    rows = [entry[0] for entry in rows_and_readers]
    reader = rows_and_readers[0][2]
    ordered_ids = sorted(str(row["capture_id"]) for row in rows)
    _insert_rows(engine, rows)
    try:
        page_one = _audit(engine, reader, page_size=10)
        page_two = _audit(engine, reader, after_capture_id=page_one.next_cursor)
    finally:
        engine.dispose()

    assert page_one.checked_count == 2
    assert page_one.byte_budget_exhausted is True
    assert page_one.has_more is True
    assert page_one.pagination_blocked is False
    assert page_one.next_cursor == ordered_ids[1]
    assert page_one.evidence_bytes_reserved_upper_bound == 2 * (8 * 1024 * 1024 + 16 * 1024)
    assert page_two.checked_count == 1
    assert page_two.has_more is False
    assert page_two.byte_budget_exhausted is False
    assert page_two.next_cursor is None


@pytest.mark.parametrize(
    ("page_size", "cursor", "bound"),
    [
        (0, None, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        (501, None, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        (True, None, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        (1, "../escape", datetime(2026, 1, 1, tzinfo=timezone.utc)),
        (1, None, datetime(2026, 1, 1)),
        (1, None, datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
    ],
)
def test_invalid_page_inputs_fail_before_query(
    tmp_path: Path,
    page_size: int,
    cursor: str | None,
    bound: datetime,
) -> None:
    """Malformed bounds, cursors, and page sizes are rejected up front."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    with (
        engine.connect() as connection,
        pytest.raises(FinancialCaptureIntegrityInputError),
    ):
        audit_financial_capture_integrity(
            connection,
            reader=reader,
            scopes=(_SCOPE,),
            captured_at_not_after=bound,
            page_size=page_size,
            after_capture_id=cursor,
        )
    engine.dispose()


@pytest.mark.parametrize(
    "scopes",
    [
        (),
        (
            FinancialCaptureIntegrityScope(
                source_id="bad source id",
                raw_source=_SOURCE,
            ),
        ),
        (_SCOPE, _SCOPE),
    ],
)
def test_invalid_allowlist_is_rejected(
    tmp_path: Path,
    scopes: tuple[FinancialCaptureIntegrityScope, ...],
) -> None:
    """Empty, malformed, and duplicated source allowlists are rejected."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    try:
        with engine.connect() as connection, pytest.raises(FinancialCaptureIntegrityInputError):
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=scopes,
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()


def test_scope_with_unsupported_value_is_rejected(tmp_path: Path) -> None:
    """Every allowlist entry must be the explicit scope value type."""
    engine = _engine(tmp_path)
    _, _, reader = _capture_row(tmp_path)
    invalid_scopes = cast("tuple[FinancialCaptureIntegrityScope, ...]", (object(),))
    try:
        with engine.connect() as connection, pytest.raises(FinancialCaptureIntegrityInputError):
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=invalid_scopes,
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()


def test_scope_dump_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexpected scope serialization errors become a fixed input error."""
    engine = _engine(tmp_path)
    _, _, reader = _capture_row(tmp_path)

    def fail_dump(self: RawResponseSource, *args: object, **kwargs: object) -> object:
        raise ValueError("private source details")

    monkeypatch.setattr(RawResponseSource, "model_dump", fail_dump)
    try:
        with (
            engine.connect() as connection,
            pytest.raises(
                FinancialCaptureIntegrityInputError,
                match="scope identity is invalid",
            ) as error,
        ):
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=(_SCOPE,),
                captured_at_not_after=datetime.now(timezone.utc),
            )
        assert "private source details" not in str(error.value)
    finally:
        engine.dispose()


def test_invalid_bound_timezone_errors_are_sanitized(tmp_path: Path) -> None:
    """A broken tzinfo implementation cannot leak its exception details."""
    engine = _engine(tmp_path)
    _, _, reader = _capture_row(tmp_path)
    bound = datetime(2026, 1, 1, tzinfo=_BrokenOffset())
    try:
        with (
            engine.connect() as connection,
            pytest.raises(
                FinancialCaptureIntegrityInputError,
                match="timezone-aware UTC datetime",
            ) as error,
        ):
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=(_SCOPE,),
                captured_at_not_after=bound,
            )
        assert "private timezone detail" not in str(error.value)
    finally:
        engine.dispose()


def test_reader_limit_must_respect_page_byte_contract(tmp_path: Path) -> None:
    """A mutated reader limit above the page bound is rejected before querying."""
    engine = _engine(tmp_path)
    _, _, reader = _capture_row(tmp_path)
    reader._max_response_bytes = 8 * 1024 * 1024 + 1
    try:
        with (
            engine.connect() as connection,
            pytest.raises(
                FinancialCaptureIntegrityInputError,
                match="at most eight MiB",
            ),
        ):
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=(_SCOPE,),
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()


def test_non_connection_and_non_reader_inputs_are_rejected(tmp_path: Path) -> None:
    """The audit requires an existing connection and exact reviewed reader."""
    engine = _engine(tmp_path)
    _, _, reader = _capture_row(tmp_path)
    with pytest.raises(TypeError, match="existing SQLAlchemy Connection"):
        audit_financial_capture_integrity(
            cast("Connection", object()),
            reader=reader,
            scopes=(_SCOPE,),
            captured_at_not_after=datetime.now(timezone.utc),
        )
    try:
        with engine.connect() as connection, pytest.raises(TypeError, match="reviewed"):
            audit_financial_capture_integrity(
                connection,
                reader=cast("RawResponseArtifactReader", object()),
                scopes=(_SCOPE,),
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("reader_error", "expected_error"),
    [
        (RawEvidenceReadInputError, "invalid_evidence_reference"),
        (RawEvidenceReadError, "evidence_integrity_failure"),
    ],
)
def test_reader_exceptions_map_to_sanitized_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader_error: type[RawEvidenceReadError],
    expected_error: str,
) -> None:
    """Reader input and unexpected read errors are reduced to stable codes."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])

    def fail_read(
        self: RawResponseArtifactReader,
        manifest_sha256: str,
        *,
        expected_source: RawResponseSource,
        expected_request_summary_sha256: str,
        captured_at_not_after: datetime,
        expected_http_status: int = 200,
    ) -> RawEvidenceReadResult:
        raise reader_error("private reader details")

    monkeypatch.setattr(RawResponseArtifactReader, "read", fail_read)
    try:
        report = _audit(engine, reader)
    finally:
        engine.dispose()

    assert report.results[0].error_code == expected_error
    assert "private reader details" not in repr(report)


def test_unlisted_source_scope_returns_no_captures(tmp_path: Path) -> None:
    """The allowlist bounds SQL selection without traversing raw directories."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    unrelated = FinancialCaptureIntegrityScope(
        source_id="other_source",
        raw_source=RawResponseSource(
            provider_id="other",
            dataset_id="other_dataset",
            operation_id="other_operation",
        ),
    )
    try:
        with engine.connect() as connection:
            report = audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=(unrelated,),
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()

    assert report.results == ()
    assert report.checked_count == 0
    assert report.has_more is False


def test_database_error_is_sanitized(tmp_path: Path) -> None:
    """A failed SQL read returns a fixed message without driver details."""
    engine = _engine(tmp_path)
    row, _, reader = _capture_row(tmp_path)
    _insert_rows(engine, [row])
    connection = engine.connect()
    connection.close()
    try:
        with pytest.raises(FinancialCaptureIntegrityQueryError) as error:
            audit_financial_capture_integrity(
                connection,
                reader=reader,
                scopes=(_SCOPE,),
                captured_at_not_after=datetime.now(timezone.utc),
            )
    finally:
        engine.dispose()

    assert str(error.value) == "read-only financial capture query failed"
    assert "DO_NOT_LEAK" not in str(error.value)
