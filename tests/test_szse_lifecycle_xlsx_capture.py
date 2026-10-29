"""Synthetic offline tests for the bounded SZSE XLSX capture command."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections import UserDict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from xml.sax.saxutils import escape

import pytest
import requests

from opendata.data.http_client import BoundedHttpResponse, GovernedHttpClient
from opendata.data.request_budget import (
    current_request_scopes,
    reserve_scoped_attempt,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter
from opendata.pipeline.szse_lifecycle_xlsx_adapter import SZSE_LIFECYCLE_SYNTHETIC_HEADERS
from scripts.ops import szse_lifecycle_xlsx_capture as capture

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DOC_REL_TYPE = f"{_DOC_REL_NS}/"


def _inline_cell(reference: str, value: str) -> str:
    """Render one synthetic inline string cell."""
    return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _synthetic_xlsx(
    *,
    header: tuple[str, ...] = SZSE_LIFECYCLE_SYNTHETIC_HEADERS,
    row_value: str = "SYNTHETIC-PRIVATE-ISSUER",
) -> bytes:
    """Build a minimal in-memory XLSX with a chosen synthetic header."""
    headers = "".join(
        _inline_cell(f"{column}1", value) for column, value in zip("ABCD", header, strict=False)
    )
    cells = "".join(
        _inline_cell(f"{column}2", value)
        for column, value in zip("ABCD", ("000123", row_value, "45292", "synthetic"), strict=False)
    )
    worksheet = (
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData><row r="1">{headers}</row>'
        f'<row r="2">{cells}</row></sheetData></worksheet>'
    ).encode()
    workbook = (
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_DOC_REL_NS}">'
        '<sheets><sheet name="Synthetic" sheetId="1" state="visible" r:id="rId1"/>'
        "</sheets></workbook>"
    ).encode()
    workbook_relationships = (
        f'<Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    ).encode()
    root_relationships = (
        f'<Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL_TYPE}officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    ).encode()
    content_types = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        b'relationships+xml"/>'
        b'<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        b'officedocument.spreadsheetml.sheet.main+xml"/>'
        b'<Override PartName="/xl/worksheets/sheet1.xml" '
        b'ContentType="application/vnd.openxmlformats-'
        b'officedocument.spreadsheetml.worksheet+xml"/>'
        b"</Types>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_relationships)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


class _FakeBoundedClient:
    """Offline fake that consumes the same local request budget as production."""

    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get_bounded(
        self,
        url: str,
        *,
        max_body_bytes: int,
        overall_timeout: float,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Record only the fixed request shape and return a synthetic response."""
        assert url == capture.ENDPOINT_URL
        assert max_body_bytes == capture.MAX_RESPONSE_BYTES
        assert overall_timeout == capture.REQUEST_TIMEOUT_SECONDS
        assert source == capture.SOURCE
        assert params is not None
        scopes = current_request_scopes()
        reserve_scoped_attempt(scopes, source, capture.HOST)
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if type(response) is BoundedHttpResponse:
            return response
        if type(response) is not bytes:
            return cast("BoundedHttpResponse", response)
        return BoundedHttpResponse(
            body=response,
            status_code=200,
            retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        )


def _bounded_response(body: object, status_code: object = 200) -> BoundedHttpResponse:
    """Construct deliberately malformed bounded responses for defensive tests."""
    return cast(
        "BoundedHttpResponse",
        BoundedHttpResponse(
            body=cast("bytes", body),
            status_code=cast("int", status_code),
            retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        ),
    )


def _enable_fake_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the exact environment required by the synthetic client seam."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")


def _selector_receipts(receipt: dict[str, object]) -> list[dict[str, object]]:
    """Narrow value-free selector receipt objects for static checking."""
    selector_receipts = receipt.get("selector_receipts")
    assert isinstance(selector_receipts, list)
    assert all(type(item) is dict for item in selector_receipts)
    return cast("list[dict[str, object]]", selector_receipts)


def test_fixed_selectors_capture_only_content_addressed_private_raw_and_anonymous_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use exactly three audited selectors and keep source row values out of receipts."""
    _enable_fake_environment(monkeypatch)
    body = _synthetic_xlsx(row_value="SECRET-SYNTHETIC-ISSUER")
    fake = _FakeBoundedClient((body, body, body))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    assert receipt["outcome"] == "bounded_raw_capture_completed"
    assert receipt["synthetic_parser_contract_status"] == "all_synthetic_contracts_passed"
    assert receipt["selectors_attempted"] == 3
    assert receipt["transport_attempts"] == 3
    assert receipt["response_bytes_total"] == len(body) * 3
    assert len(fake.calls) == 3
    run_reservation = json.loads((tmp_path / "capture" / "run-reservation.json").read_text())
    assert run_reservation["max_transport_attempts_per_selector"] == 1
    assert run_reservation["max_transport_attempts_task"] == 3
    assert [call["params"] for call in fake.calls] == [
        {
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1110",
            "TABKEY": "tab1",
            "random": "0.6935816432433362",
        },
        {
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1793_ssgs",
            "TABKEY": "tab1",
            "random": "0.6935816432433362",
        },
        {
            "SHOWTYPE": "xlsx",
            "CATALOGID": "1793_ssgs",
            "TABKEY": "tab2",
            "random": "0.6935816432433362",
        },
    ]
    selector_receipts = _selector_receipts(receipt)
    assert [item["dataset_class"] for item in selector_receipts] == [
        "current_a",
        "suspended",
        "terminated",
    ]
    assert all(item["raw_response_persisted"] is True for item in selector_receipts)
    assert all(item["source_completeness_status"] == "unknown" for item in selector_receipts)
    assert receipt["market_completeness"] == "not_established"
    assert receipt["publication"] == "not_performed"
    assert receipt["scheduled_collection"] == "not_configured"

    serialized_receipt = json.dumps(receipt, sort_keys=True)
    assert "SECRET-SYNTHETIC-ISSUER" not in serialized_receipt
    assert "000123" not in serialized_receipt
    first_digest = selector_receipts[0]["response_sha256"]
    assert isinstance(first_digest, str)
    artifact = tmp_path / "capture" / "raw" / "objects" / first_digest[:2] / f"{first_digest}.blob"
    assert artifact.read_bytes() == body
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == first_digest
    assert (tmp_path / "capture").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "capture" / "receipt.json").stat().st_mode & 0o777 == 0o600


def test_unrecognized_realistic_header_is_rejected_after_raw_evidence_is_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the unrecognized workbook bytes while returning only a fixed mismatch code."""
    _enable_fake_environment(monkeypatch)
    private_header = "SECRET-UNRECOGNIZED-HEADER"
    body = _synthetic_xlsx(header=(private_header, *SZSE_LIFECYCLE_SYNTHETIC_HEADERS[1:]))
    valid = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, valid, valid))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    first = _selector_receipts(receipt)[0]
    assert first["failure_code"] == "header_contract_mismatch"
    assert first["header_contract_status"] == "synthetic_contract_mismatch"
    assert first["raw_response_persisted"] is True
    raw_digest = first["response_sha256"]
    assert isinstance(raw_digest, str)
    raw_path = tmp_path / "capture" / "raw" / "objects" / raw_digest[:2] / f"{raw_digest}.blob"
    assert raw_path.read_bytes() == body
    final_receipt = (tmp_path / "capture" / "receipt.json").read_text()
    assert private_header not in final_receipt


def test_transport_failure_is_redacted_and_still_receipted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not expose exception text when one selector has an unknown outcome."""
    _enable_fake_environment(monkeypatch)
    valid = _synthetic_xlsx()
    fake = _FakeBoundedClient((RuntimeError("SECRET-TRANSPORT-DETAIL"), valid, valid))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    first = _selector_receipts(receipt)[0]
    assert first["attempt_outcome"] == "unknown"
    assert first["failure_code"] == "request_failed_or_outcome_unknown"
    assert first["raw_response_persisted"] is False
    serialized = json.dumps(receipt, sort_keys=True)
    assert "SECRET-TRANSPORT-DETAIL" not in serialized
    assert receipt["selectors_attempted"] == 3
    assert receipt["transport_attempts"] == 3


def test_capture_rejects_non_private_existing_destination_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse an existing directory before the fake transport can be called."""
    _enable_fake_environment(monkeypatch)
    destination = tmp_path / "existing"
    destination.mkdir()
    fake = _FakeBoundedClient(())

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(destination, client_factory=lambda: fake)

    assert caught.value.code == "evidence_directory_must_be_new"
    assert fake.calls == []


def test_capture_rejects_relative_evidence_path_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a relative path without relaxing the private-directory boundary."""
    _enable_fake_environment(monkeypatch)
    fake = _FakeBoundedClient(())

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(Path("relative-capture"), client_factory=lambda: fake)

    assert caught.value.code == "evidence_directory_invalid"
    assert fake.calls == []


def test_capture_rejects_non_path_input_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a string masquerading as a Path before inspecting its attributes."""
    _enable_fake_environment(monkeypatch)
    fake = _FakeBoundedClient(())
    path_as_string = cast("Path", str(tmp_path / "capture"))

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(path_as_string, client_factory=lambda: fake)

    assert caught.value.code == "evidence_directory_invalid"
    assert fake.calls == []


def test_injected_client_requires_an_isolated_testing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject test-only transport injection before creating evidence outside tests."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    fake = _FakeBoundedClient(())
    destination = tmp_path / "capture"

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(destination, client_factory=lambda: fake)

    assert caught.value.code == "local_capture_operation_failed"
    assert not destination.exists()
    assert fake.calls == []


def test_live_capture_requires_explicit_development_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a live invocation before it creates a directory outside development."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.delenv("TESTING", raising=False)
    destination = tmp_path / "capture"

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(destination)

    assert caught.value.code == "local_capture_operation_failed"
    assert not destination.exists()


def test_invalid_client_factory_gets_anonymous_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a fake without the bounded client method and do not send requests."""
    _enable_fake_environment(monkeypatch)

    def invalid_factory() -> capture._BoundedClient:
        return cast("capture._BoundedClient", object())

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=invalid_factory,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "client_contract_invalid"
    assert receipt["selectors_attempted"] == 0
    assert receipt["transport_attempts"] == 0


def test_client_factory_exception_gets_a_fixed_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redact a local factory exception and persist a final failure receipt."""
    _enable_fake_environment(monkeypatch)

    def fail_factory() -> capture._BoundedClient:
        raise RuntimeError("SECRET-FACTORY-DETAIL")

    destination = tmp_path / "capture"
    receipt = capture.run_szse_lifecycle_capture(destination, client_factory=fail_factory)

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert receipt["selectors_attempted"] == 0
    assert (destination / "receipt.json").is_file()
    assert "SECRET-FACTORY-DETAIL" not in (destination / "receipt.json").read_text()


def test_expired_task_deadline_receipts_selectors_without_sending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record all selectors as not attempted when the task deadline has expired."""
    _enable_fake_environment(monkeypatch)
    fake = _FakeBoundedClient(())
    monotonic_values = iter((100.0, 146.0, 147.0, 148.0))
    monkeypatch.setattr(
        capture,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    selectors = _selector_receipts(receipt)
    assert receipt["selectors_attempted"] == 0
    assert receipt["transport_attempts"] == 0
    assert all(item["attempt_outcome"] == "not_attempted" for item in selectors)
    assert all(item["request_invoked"] is False for item in selectors)
    assert fake.calls == []


def test_live_path_closes_its_session_while_using_only_a_fake_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the live environment branch without creating a network client."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("TESTING", raising=False)
    body = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, body, body))
    session = requests.Session()
    closed = False

    def mark_closed() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(session, "close", mark_closed)
    live_client = capture._LiveClient(
        client=cast("GovernedHttpClient", fake),
        session=session,
    )
    monkeypatch.setattr(capture, "_new_live_client", lambda: live_client)

    receipt = capture.run_szse_lifecycle_capture(tmp_path / "capture")

    assert receipt["outcome"] == "bounded_raw_capture_completed"
    assert len(fake.calls) == 3
    assert closed is True


@pytest.mark.parametrize(  # type: ignore[misc]
    ("limit_name", "expected_failure"),
    [
        ("MAX_TOTAL_RESPONSE_BYTES", "task_response_byte_cap_exceeded"),
        ("MAX_TASK_ROWS", "task_row_cap_exceeded"),
    ],
)
def test_task_caps_stop_after_the_first_over_budget_selector(
    limit_name: str,
    expected_failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop the run immediately when task-level byte or parsed-row budget is exceeded."""
    _enable_fake_environment(monkeypatch)
    monkeypatch.setattr(capture, limit_name, 0)
    body = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, body, body))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == expected_failure
    assert receipt["selectors_attempted"] == 1
    assert receipt["transport_attempts"] == 1
    assert len(fake.calls) == 1


def test_selector_row_cap_rejects_oversized_parser_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply the selector row ceiling even if a parser result exceeds its contract."""
    _enable_fake_environment(monkeypatch)
    monkeypatch.setattr(capture, "MAX_ROWS_PER_SELECTOR", 0)
    body = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, body, body))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    assert receipt["failure_code"] == "task_row_cap_exceeded"
    assert receipt["selectors_attempted"] == 3
    assert all(item["raw_response_persisted"] is True for item in _selector_receipts(receipt))


@pytest.mark.parametrize(  # type: ignore[misc]
    ("response_kind", "expected_code", "suppressed"),
    [
        ("wrong_type", "response_contract_invalid", None),
        ("server_error", "response_contract_invalid", True),
        ("non_byte_body", "response_contract_invalid", True),
        ("non_integer_status", "response_contract_invalid", True),
    ],
)
def test_invalid_response_contracts_are_suppressed_and_receipted(
    response_kind: str,
    expected_code: str,
    suppressed: bool | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain only safe status metadata for malformed or unsuccessful responses."""
    _enable_fake_environment(monkeypatch)
    valid = _synthetic_xlsx()
    if response_kind == "wrong_type":
        first_response = object()
    elif response_kind == "server_error":
        first_response = _bounded_response(b"SECRET-HTTP-ERROR-BODY", 503)
    elif response_kind == "non_byte_body":
        first_response = _bounded_response(bytearray(b"SECRET-NON-BYTES"))
    else:
        first_response = _bounded_response(b"SECRET-STATUS", 200.0)
    fake = _FakeBoundedClient((first_response, valid, valid))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    first = _selector_receipts(receipt)[0]
    assert first["failure_code"] == expected_code
    assert first.get("response_suppressed") is suppressed
    assert first["raw_response_persisted"] is False
    assert "SECRET" not in json.dumps(receipt, sort_keys=True)
    if response_kind == "server_error":
        assert first["http_status"] == 503
    elif response_kind == "non_byte_body":
        assert first["http_status"] == 200
    else:
        assert first["http_status"] is None


def test_oversized_bounded_response_is_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensively reject a response larger than the selected per-request cap."""
    _enable_fake_environment(monkeypatch)
    monkeypatch.setattr(capture, "MAX_RESPONSE_BYTES", 4)
    oversized = _bounded_response(b"12345")
    fake = _FakeBoundedClient((oversized, oversized, oversized))

    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    first = _selector_receipts(receipt)[0]
    assert first["failure_code"] == "response_body_too_large"
    assert first["response_suppressed"] is True
    assert first["raw_response_persisted"] is False
    assert receipt["selectors_attempted"] == 3


def test_raw_evidence_writer_failure_is_redacted_and_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report a fixed persistence code when the private writer fails."""
    _enable_fake_environment(monkeypatch)
    body = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, body, body))

    def fail_capture(*args: object, **kwargs: object) -> object:
        raise OSError("SECRET-PERSISTENCE-PATH")

    monkeypatch.setattr(RawResponseArtifactWriter, "capture", fail_capture)
    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    first = _selector_receipts(receipt)[0]
    assert first["failure_code"] == "raw_evidence_persistence_failed"
    assert first["raw_persistence_state"] == "unknown_after_writer_failure"
    assert first["raw_response_persisted"] is False
    assert "SECRET-PERSISTENCE-PATH" not in json.dumps(receipt, sort_keys=True)


def test_unexpected_parser_failure_keeps_raw_response_but_not_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve content-addressed bytes if the parser raises an unexpected error."""
    _enable_fake_environment(monkeypatch)
    body = _synthetic_xlsx()
    fake = _FakeBoundedClient((body, body, body))

    def fail_parser(*args: object, **kwargs: object) -> object:
        raise RuntimeError("SECRET-PARSER-DETAIL")

    monkeypatch.setattr(capture, "parse_szse_lifecycle_xlsx", fail_parser)
    receipt = capture.run_szse_lifecycle_capture(
        tmp_path / "capture",
        client_factory=lambda: fake,
    )

    first = _selector_receipts(receipt)[0]
    assert first["failure_code"] == "synthetic_parser_rejected"
    assert first["raw_response_persisted"] is True
    assert first["header_contract_status"] == "synthetic_contract_rejected_or_unknown"
    assert "SECRET-PARSER-DETAIL" not in json.dumps(receipt, sort_keys=True)


def test_symlinked_evidence_parent_is_rejected_without_following_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep no-follow directory traversal and reject an ancestor symlink."""
    _enable_fake_environment(monkeypatch)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    fake = _FakeBoundedClient(())

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture.run_szse_lifecycle_capture(
            linked_parent / "capture",
            client_factory=lambda: fake,
        )

    assert caught.value.code == "evidence_directory_unavailable"
    assert not (real_parent / "capture").exists()
    assert fake.calls == []


def test_selector_receipt_sanitizer_drops_malformed_and_private_values() -> None:
    """Normalize all unknown receipt fields and omit private attacker values."""
    malformed = {
        "sequence": "SECRET-SEQUENCE",
        "dataset_class": "SECRET-DATASET",
        "request_invoked": "yes",
        "attempt_outcome": "SECRET-OUTCOME",
        "failure_code": "SECRET-ERROR",
        "http_status": "503",
        "response_bytes": -1,
        "response_sha256": "SECRET-HASH",
        "raw_manifest_sha256": "SECRET-MANIFEST",
        "raw_response_persisted": "true",
        "raw_persistence_state": "SECRET-STATE",
        "response_suppressed": True,
        "request_summary_sha256": "SECRET-REQUEST",
        "parsed_row_count": -1,
        "header_contract_status": "SECRET-HEADER",
        "source_authenticity": "SECRET-AUTHENTICITY",
        "unknown_private_field": "SECRET-ROW-VALUE",
    }

    sanitized = capture._sanitize_selector_receipt(malformed)
    serialized = json.dumps(sanitized, sort_keys=True)

    assert sanitized["sequence"] is None
    assert sanitized["dataset_class"] is None
    assert sanitized["attempt_outcome"] == "unknown"
    assert sanitized["failure_code"] == "local_capture_operation_failed"
    assert sanitized["response_suppressed"] is True
    assert "unknown_private_field" not in sanitized
    assert "SECRET" not in serialized
    assert capture._sanitize_selector_receipt(UserDict(malformed)) == {}


def test_selector_request_digest_fails_closed_when_hash_helper_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse to issue a request if the fixed request summary cannot be hashed."""
    monkeypatch.setattr(capture, "request_summary_sha256", lambda *args, **kwargs: None)
    selector = capture._Selector("current_a", "1110", "tab1")

    with pytest.raises(capture.SzseCaptureError) as caught:
        capture._selector_request_digest(selector)

    assert caught.value.code == "local_capture_operation_failed"
    assert capture._transport_attempts(None) == 0


def test_receipt_writer_rejects_unapproved_filename() -> None:
    """Restrict the capture receipt writer to its fixed private filenames."""
    with pytest.raises(capture.SzseCaptureError) as caught:
        capture._write_json_exclusive(-1, "unapproved.json", {})

    assert caught.value.code == "local_capture_operation_failed"


def test_atomic_receipt_short_write_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove a partially written private receipt when the filesystem write fails."""
    directory = tmp_path / "receipt-root"
    directory_fd = capture._create_private_evidence_directory(directory)
    original_write = os.write
    write_calls = 0

    def write_one_byte_then_fail(
        descriptor: int,
        data: bytes | bytearray | memoryview,
    ) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return original_write(descriptor, memoryview(data)[:1])
        raise OSError("SECRET-WRITE-FAILURE")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "write", write_one_byte_then_fail)
            with pytest.raises(OSError):
                capture._write_json(directory_fd, "receipt.json", {"message": "private"})
        assert list(directory.iterdir()) == []
    finally:
        os.close(directory_fd)


def test_atomic_receipt_does_not_overwrite_and_removes_pending_link(
    tmp_path: Path,
) -> None:
    """Preserve a pre-existing receipt when no-clobber publication fails."""
    directory = tmp_path / "receipt-root"
    directory_fd = capture._create_private_evidence_directory(directory)
    existing = directory / "receipt.json"
    existing.write_text("original", encoding="utf-8")

    try:
        with pytest.raises(FileExistsError):
            capture._write_json(directory_fd, "receipt.json", {"message": "replacement"})
        assert existing.read_text(encoding="utf-8") == "original"
        assert sorted(item.name for item in directory.iterdir()) == ["receipt.json"]
    finally:
        os.close(directory_fd)


def test_atomic_receipt_size_limit_cleans_temporary_file(tmp_path: Path) -> None:
    """Do not leave an oversized serialized receipt in the evidence directory."""
    directory = tmp_path / "receipt-root"
    directory_fd = capture._create_private_evidence_directory(directory)

    try:
        with pytest.raises(capture.SzseCaptureError) as caught:
            capture._write_json(
                directory_fd,
                "receipt.json",
                {"payload": "x" * (128 * 1024)},
            )
        assert caught.value.code == "local_capture_operation_failed"
        assert list(directory.iterdir()) == []
    finally:
        os.close(directory_fd)


def test_atomic_receipt_fsync_failure_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean private partial files if the atomic receipt cannot be flushed."""
    directory = tmp_path / "receipt-root"
    directory_fd = capture._create_private_evidence_directory(directory)

    def fail_fsync(descriptor: int) -> None:
        raise OSError("SECRET-FSYNC-FAILURE")

    try:
        with monkeypatch.context() as patcher:
            patcher.setattr(os, "fsync", fail_fsync)
            with pytest.raises(OSError):
                capture._write_json(directory_fd, "receipt.json", {"message": "private"})
        assert list(directory.iterdir()) == []
    finally:
        os.close(directory_fd)


@pytest.mark.parametrize("failure", [FileExistsError, OSError])  # type: ignore[misc]
def test_failure_receipt_fallback_does_not_mask_existing_or_unwritable_file(
    failure: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat fallback receipt write errors as best-effort without exposing details."""

    def raise_write_error(*args: object, **kwargs: object) -> None:
        raise failure("SECRET-RECEIPT-FAILURE")

    monkeypatch.setattr(capture, "_write_json", raise_write_error)
    capture._write_json_if_missing(-1, "receipt.json", {})


@pytest.mark.parametrize(  # type: ignore[misc]
    ("outcome", "expected_status"),
    [
        ("bounded_raw_capture_completed", 0),
        ("partial_or_failed_capture", 1),
    ],
)
def test_cli_prints_the_value_free_receipt_and_maps_outcome_to_exit_code(
    outcome: str,
    expected_status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Map an already-sanitized run receipt to stdout and its documented status."""
    expected_receipt = {"outcome": outcome, "row_values_in_receipt": False}
    monkeypatch.setattr(capture, "run_szse_lifecycle_capture", lambda path: expected_receipt)

    status = capture.main(
        ["--evidence-dir", str(tmp_path / "unused"), "--live"],
    )

    assert status == expected_status
    assert json.loads(capsys.readouterr().out) == expected_receipt


def test_cli_reports_only_fixed_code_when_live_capture_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep CLI rejection output anonymous even when the internal exception has detail."""

    def reject_capture(path: Path) -> dict[str, object]:
        raise capture.SzseCaptureError("local_capture_operation_failed")

    monkeypatch.setattr(capture, "run_szse_lifecycle_capture", reject_capture)
    status = capture.main(["--evidence-dir", str(tmp_path / "unused"), "--live"])

    assert status == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "outcome": "rejected",
        "failure_code": "local_capture_operation_failed",
    }


def test_cli_live_guard_returns_without_running_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the explicit --live gate effective if parser arguments are malformed."""
    parser = SimpleNamespace(
        parse_args=lambda argv: SimpleNamespace(
            evidence_dir=tmp_path / "unused",
            live=False,
        ),
    )
    monkeypatch.setattr(capture, "_build_cli_parser", lambda: parser)
    monkeypatch.setattr(
        capture,
        "run_szse_lifecycle_capture",
        lambda evidence_dir: pytest.fail("capture must not run without --live"),
    )

    assert capture.main([]) == 2


def test_live_client_configuration_has_one_attempt_and_no_environment_proxy() -> None:
    """Pin the actual Requests adapter and governed client against hidden retries."""
    live = capture._new_live_client()
    try:
        assert live.session.trust_env is False
        assert live.client._config.max_attempts == 1
        assert live.client._config.rate_burst == 1
        assert live.client._config.max_concurrency_per_host == 1
        assert live.client._config.rate_max_wait == capture.RATE_LIMIT_MAX_WAIT_SECONDS
        assert all(adapter.max_retries.total == 0 for adapter in live.session.adapters.values())
    finally:
        live.session.close()


def test_live_client_rate_gate_admits_three_selectors_with_bounded_waits() -> None:
    """Allow two refill waits while keeping the three-token run within 45 seconds."""
    live = capture._new_live_client()
    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    live.client._time = monotonic
    live.client._sleep = sleep
    try:
        for _selector in capture._REQUEST_PARAMS:
            live.client._acquire_token(
                capture.HOST,
                capture.ENDPOINT_URL,
                deadline=capture.MAX_TASK_TIMEOUT_SECONDS,
            )

        assert sleeps == [pytest.approx(1.0), pytest.approx(1.0)]
        assert now == pytest.approx(2.0)
        assert now < capture.MAX_TASK_TIMEOUT_SECONDS
        assert live.client._config.max_attempts == 1
    finally:
        live.session.close()
