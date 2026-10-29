"""Synthetic offline tests for the one-page BSE catalog capture."""

from __future__ import annotations

import io
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests
from requests.adapters import HTTPAdapter

from opendata.data.http_client import BoundedHttpResponse, FailureCategory, HttpFetchError
from opendata.data.request_budget import (
    RequestBudgetError,
    current_request_scopes,
    reserve_scoped_attempt,
)
from scripts.ops import bse_instrument_catalog_capture as capture

if TYPE_CHECKING:
    from collections.abc import Mapping


class _FakeBoundedPostClient:
    """Offline fake that records the sole request and consumes local budget."""

    def __init__(self, response: object, *, reserve_attempt: bool = True) -> None:
        """Store one response or exception for the one allowed call."""
        self.response = response
        self.reserve_attempt = reserve_attempt
        self.calls: list[dict[str, object]] = []

    def post_form_bounded(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        max_form_bytes: int,
        max_body_bytes: int,
        overall_timeout: float,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Record request policy and return only the preloaded synthetic value."""
        assert url == capture.ENDPOINT_URL
        assert dict(form) == {
            "page": "0",
            "typejb": "T",
            "xxfcbj[]": "2",
            "xxzqdm": "",
            "sortfield": "xxzqdm",
            "sorttype": "asc",
        }
        assert max_form_bytes == capture.MAX_FORM_BYTES
        assert max_body_bytes == capture.MAX_RESPONSE_BYTES
        assert overall_timeout == capture.REQUEST_TIMEOUT_SECONDS
        assert source == capture.SOURCE
        assert headers == {
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            "Referer": capture.REFERER,
        }
        scopes = current_request_scopes()
        if self.reserve_attempt:
            reserve_scoped_attempt(scopes, source, capture.HOST)
        self.calls.append(
            {
                "url": url,
                "form": dict(form),
                "timeout": timeout,
                "scope_count": len(scopes),
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return cast("BoundedHttpResponse", self.response)


def _enable_fake_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the explicit offline environment required by the fake seam."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")


def _response(body: bytes, status_code: int = 200) -> BoundedHttpResponse:
    """Construct one synthetic bounded response without contacting a source."""
    return BoundedHttpResponse(
        body=body,
        status_code=status_code,
        retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
    )


def test_capture_preserves_one_raw_page_without_parsing_or_publishing_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture exact bytes and keep JSONP/catalog values out of the receipt."""
    _enable_fake_environment(monkeypatch)
    body = b'callback([{"totalPages":91,"content":[{"securityCode":"920777"}]}]);'
    client = _FakeBoundedPostClient(_response(body))
    evidence_dir = tmp_path / "bse-canary"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    assert receipt["outcome"] == "raw_page_captured"
    assert receipt["request_attempted"] is True
    assert receipt["request_attempted_semantics"] == "bounded_client_invocation_only"
    assert receipt["client_invoked"] is True
    assert receipt["budget_reservations"] == 1
    assert receipt["transport_attempts"] == 1
    assert receipt["delivery_status"] == "response_received"
    assert receipt["http_response_observed"] is True
    assert receipt["failure_category"] is None
    assert receipt["schema_version"] == 2
    assert receipt["failure_diagnostic_schema_version"] == 1
    assert receipt["transport_send_ceiling"] == 1
    assert receipt["response_interpretation"] == "not_parsed"
    assert receipt["catalog_fields"] == "unknown"
    assert receipt["jsonp_envelope"] == "unknown"
    assert receipt["total_pages"] == "unknown"
    assert receipt["pagination"] == "unknown"
    assert receipt["source_identity"] == "unknown"
    assert receipt["market_completeness"] == "not_established"
    assert receipt["historical_coverage"] == "not_established"
    assert receipt["row_values_in_receipt"] is False
    assert len(client.calls) == 1
    assert client.calls[0]["scope_count"] == 1

    receipt_bytes = (evidence_dir / "receipt.json").read_bytes()
    assert b"920777" not in receipt_bytes
    assert b"totalPages" not in receipt_bytes
    assert b"callback" not in receipt_bytes
    raw_files = tuple((evidence_dir / "raw" / "objects").rglob("*.blob"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == body
    manifest_files = tuple((evidence_dir / "raw" / "manifests").rglob("*.json"))
    assert len(manifest_files) == 1
    manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    assert manifest["response_size_bytes"] == len(body)
    assert manifest["response_sha256"] == receipt["response_sha256"]
    assert stat.S_IMODE(raw_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_files[0].stat().st_mode) == 0o600


def test_failed_single_post_is_not_retried_or_reported_as_empty_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous send failure remains unknown and is never automatically replayed."""
    _enable_fake_environment(monkeypatch)
    client = _FakeBoundedPostClient(OSError("synthetic transport failure"))

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-failed",
        client_factory=lambda: client,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "request_failed_or_outcome_unknown"
    assert receipt["request_attempted"] is True
    assert receipt["client_invoked"] is True
    assert receipt["budget_reservations"] == 1
    assert receipt["transport_attempts"] is None
    assert receipt["delivery_status"] == "unknown"
    assert receipt["http_response_observed"] is False
    assert receipt["failure_category"] == "unknown"
    assert receipt["failure_category_source"] == "unknown"
    assert receipt["http_status"] is None
    assert receipt["total_pages"] == "unknown"
    assert receipt["pagination"] == "unknown"
    assert len(client.calls) == 1
    assert not tuple((tmp_path / "bse-failed" / "raw" / "objects").rglob("*.blob"))


def test_response_above_body_cap_is_rejected_without_raw_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an oversized fake response before content-addressed persistence."""
    _enable_fake_environment(monkeypatch)
    oversized = _response(b"x" * (capture.MAX_RESPONSE_BYTES + 1))
    client = _FakeBoundedPostClient(oversized)

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-oversized",
        client_factory=lambda: client,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "response_body_too_large"
    assert receipt["response_suppressed"] is True
    assert receipt["raw_response_persisted"] is False
    assert len(client.calls) == 1
    assert not tuple((tmp_path / "bse-oversized" / "raw" / "objects").rglob("*.blob"))


def test_non_success_status_is_not_saved_as_a_catalog_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx response is not decoded or persisted as successful page data."""
    _enable_fake_environment(monkeypatch)
    client = _FakeBoundedPostClient(_response(b"private-error-body", status_code=302))

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-redirect",
        client_factory=lambda: client,
    )

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "response_contract_invalid"
    assert receipt["http_status"] == 302
    assert receipt["failure_category"] == "unknown"
    assert receipt["delivery_status"] == "response_received"
    assert receipt["response_suppressed"] is True
    assert receipt["raw_response_persisted"] is False
    assert len(client.calls) == 1


def test_invalid_response_contract_is_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake transport returning a non-response value fails closed."""
    _enable_fake_environment(monkeypatch)
    client = _FakeBoundedPostClient(object())

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-invalid-response",
        client_factory=lambda: client,
    )

    assert receipt["failure_code"] == "response_contract_invalid"
    assert receipt["response_suppressed"] is True
    assert receipt["raw_response_persisted"] is False
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        (cast("bytes", "synthetic non-byte body"), 200),
        (b"synthetic body", cast("int", "200")),
    ],
)
def test_malformed_body_or_status_types_fail_closed(
    body: object,
    status_code: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime lookalikes cannot bypass the exact bounded response contract."""
    _enable_fake_environment(monkeypatch)
    malformed = cast(
        "BoundedHttpResponse",
        BoundedHttpResponse(
            body=cast("bytes", body),
            status_code=cast("int", status_code),
            retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        ),
    )
    client = _FakeBoundedPostClient(malformed)

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-malformed-types",
        client_factory=lambda: client,
    )

    assert receipt["failure_code"] == "response_contract_invalid"
    assert receipt["raw_response_persisted"] is False
    assert receipt["response_suppressed"] is True
    assert len(client.calls) == 1


def test_invalid_client_factory_fails_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client without the bounded form method cannot trigger transport."""
    _enable_fake_environment(monkeypatch)

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-invalid-client",
        client_factory=lambda: cast("capture._BoundedPostClient", object()),
    )

    assert receipt["failure_code"] == "client_contract_invalid"
    assert receipt["request_attempted"] is False
    assert receipt["client_invoked"] is False
    assert receipt["budget_reservations"] == 0
    assert receipt["transport_attempts"] is None
    assert receipt["delivery_status"] == "not_invoked"


def test_client_factory_failure_is_sanitized_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed factory emits only the fixed local failure classification."""
    _enable_fake_environment(monkeypatch)

    def fail_factory() -> _FakeBoundedPostClient:
        raise OSError("synthetic private failure detail")

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-factory-failure",
        client_factory=fail_factory,
    )

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert receipt["request_attempted"] is False
    assert receipt["client_invoked"] is False
    assert receipt["budget_reservations"] == 0
    assert receipt["delivery_status"] == "not_invoked"
    assert receipt["failure_category"] == "unknown"
    assert (
        b"private failure detail"
        not in (tmp_path / "bse-factory-failure" / "receipt.json").read_bytes()
    )


def test_raw_evidence_persistence_failure_keeps_only_anonymous_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed writer does not publish or print synthetic response content."""
    _enable_fake_environment(monkeypatch)
    body = b'{"privateCatalogField":"SYNTHETIC-SECRET-CODE"}'
    client = _FakeBoundedPostClient(_response(body))

    class _FailingWriter:
        """Writer fake that simulates local evidence storage failure."""

        def __init__(self, root: object, *, max_response_bytes: int) -> None:
            """Accept the writer constructor contract without touching the path."""
            del root, max_response_bytes

        def capture(self, chunks: object, **kwargs: object) -> object:
            """Raise a fixed local persistence error."""
            del chunks, kwargs
            raise OSError("synthetic evidence failure")

    monkeypatch.setattr(capture, "RawResponseArtifactWriter", _FailingWriter)
    evidence_dir = tmp_path / "bse-writer-failure"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    assert receipt["failure_code"] == "raw_evidence_persistence_failed"
    assert receipt["response_sha256"] is not None
    assert receipt["delivery_status"] == "response_received"
    assert receipt["response_suppressed"] is False
    assert receipt["raw_response_persisted"] is False
    assert b"SYNTHETIC-SECRET-CODE" not in (evidence_dir / "receipt.json").read_bytes()
    assert len(client.calls) == 1


def test_private_directory_and_receipts_do_not_change_ancestor_permissions(
    tmp_path: Path,
) -> None:
    """Only the new evidence leaf and its files receive private permissions."""
    parent = tmp_path / "evidence-parent"
    parent.mkdir(mode=0o750)
    ancestor_mode = stat.S_IMODE(parent.stat().st_mode)
    evidence_dir = parent / "new-run"

    descriptor = capture._create_private_evidence_directory(evidence_dir)
    try:
        capture._write_json_exclusive(descriptor, "receipt.json", {"value": "safe"})
    finally:
        os.close(descriptor)

    assert stat.S_IMODE(parent.stat().st_mode) == ancestor_mode
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((evidence_dir / "receipt.json").stat().st_mode) == 0o600


def test_preexisting_evidence_path_is_refused_without_overwrite(tmp_path: Path) -> None:
    """A prior directory cannot be reused for a second capture."""
    evidence_dir = tmp_path / "already-exists"
    evidence_dir.mkdir(mode=0o700)

    with pytest.raises(capture.BSECaptureError, match="evidence_directory_must_be_new"):
        capture._create_private_evidence_directory(evidence_dir)


def test_relative_or_symlink_ancestor_paths_fail_closed(tmp_path: Path) -> None:
    """Evidence paths must be absolute and no-follow traversal must reject links."""
    with pytest.raises(capture.BSECaptureError, match="evidence_directory_invalid"):
        capture._create_private_evidence_directory(Path("relative-evidence"))
    with pytest.raises(capture.BSECaptureError, match="evidence_directory_invalid"):
        capture._create_private_evidence_directory(tmp_path / "parent" / ".." / "unsafe")

    if not capture._O_NOFOLLOW:
        pytest.skip("platform does not expose O_NOFOLLOW")
    target = tmp_path / "real-parent"
    target.mkdir()
    link = tmp_path / "parent-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(capture.BSECaptureError, match="evidence_directory_unavailable"):
        capture._create_private_evidence_directory(link / "new-run")


def test_receipt_creation_is_no_clobber_and_failure_recorder_preserves_first_copy(
    tmp_path: Path,
) -> None:
    """The first receipt remains immutable when a second write is attempted."""
    evidence_dir = tmp_path / "receipt-run"
    descriptor = capture._create_private_evidence_directory(evidence_dir)
    try:
        capture._write_json_exclusive(descriptor, "receipt.json", {"state": "first"})
        capture._write_json_if_missing(descriptor, "receipt.json", {"state": "second"})
        with pytest.raises(FileExistsError):
            capture._write_json_exclusive(descriptor, "receipt.json", {"state": "third"})
        with pytest.raises(capture.BSECaptureError, match="local_capture_operation_failed"):
            capture._write_json_exclusive(descriptor, "unapproved.json", {})
    finally:
        os.close(descriptor)

    assert json.loads((evidence_dir / "receipt.json").read_text(encoding="utf-8")) == {
        "state": "first"
    }


def test_budget_and_live_transport_are_scoped_to_one_non_retrying_host() -> None:
    """The live client can issue at most one non-proxied retry-free BSE POST."""
    budget = capture._new_request_budget()
    grant = budget.grants[0]
    live = capture._new_live_client()
    try:
        assert budget.task_attempt_limit == 1
        assert budget.source_attempt_limit == 1
        assert grant.source == capture.SOURCE
        assert grant.allowed_hosts == frozenset({capture.HOST})
        assert grant.operation.value == "query"
        assert live.session.trust_env is False
        assert live.session.headers["Accept-Encoding"] == "identity"
        assert live.client._config.max_attempts == 1
        assert live.client._config.proxies == {}
        assert all(adapter.max_retries.total == 0 for adapter in live.session.adapters.values())
        live.client._validate_bounded_form_session(live.session)
    finally:
        live.session.close()


def test_live_capture_uses_identity_encoding_through_fake_adapter_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The governed live POST preconditions pass while adapter send stays offline."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    body = b'{"synthetic":"bounded-live-path-response"}'
    sends: list[dict[str, object]] = []

    def fake_send(
        adapter: HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        """Return an in-memory response at the last Requests adapter boundary."""
        del adapter
        assert request.method == "POST"
        assert request.url == capture.ENDPOINT_URL
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        assert request.body == (
            b"page=0&typejb=T&xxfcbj%5B%5D=2&xxzqdm=&sortfield=xxzqdm&sorttype=asc"
        )
        assert kwargs["stream"] is True
        sends.append({"url": request.url, "method": request.method, "body": request.body})

        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Encoding"] = "identity"
        response.headers["Content-Length"] = str(len(body))
        response.url = request.url
        response.request = request
        response.raw = io.BytesIO(body)
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    receipt = capture.run_bse_instrument_catalog_capture(tmp_path / "live-session-canary")

    assert receipt["outcome"] == "raw_page_captured"
    assert receipt["budget_reservations"] == 1
    assert receipt["client_invoked"] is True
    assert receipt["response_interpretation"] == "not_parsed"
    assert receipt["raw_response_persisted"] is True
    assert len(sends) == 1


def test_fake_transport_requires_explicit_offline_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected client cannot bypass the explicit test-mode gate."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    client = _FakeBoundedPostClient(_response(b"{}"))

    with pytest.raises(capture.BSECaptureError, match="local_capture_operation_failed"):
        capture.run_bse_instrument_catalog_capture(
            tmp_path / "bse-rejected-environment",
            client_factory=lambda: client,
        )
    assert client.calls == []


def test_live_capture_requires_development_environment_before_path_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live transport cannot start from test mode or implicitly load settings."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "false")
    evidence_dir = tmp_path / "bse-live-rejected"

    with pytest.raises(capture.BSECaptureError, match="local_capture_operation_failed"):
        capture.run_bse_instrument_catalog_capture(evidence_dir)
    assert not evidence_dir.exists()


def test_cli_prints_only_the_sanitized_receipt_and_uses_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI output contains only the returned receipt and signals its outcome."""
    success = {"outcome": "raw_page_captured", "row_values_in_receipt": False}
    monkeypatch.setattr(capture, "run_bse_instrument_catalog_capture", lambda _path: success)

    assert capture.main(["--evidence-dir", str(tmp_path / "unused-success")]) == 0
    assert json.loads(capsys.readouterr().out) == success

    failure = {"outcome": "partial_or_failed_capture", "failure_code": "safe"}
    monkeypatch.setattr(capture, "run_bse_instrument_catalog_capture", lambda _path: failure)
    assert capture.main(["--evidence-dir", str(tmp_path / "unused-failure")]) == 1
    assert json.loads(capsys.readouterr().out) == failure


def test_failure_receipt_never_echoes_unrecognized_failure_text() -> None:
    """Failure and status values remain on the fixed safe vocabulary."""
    receipt = capture._failure_receipt(
        "a" * 64,
        failure_code="synthetic response payload detail",
        request_attempted=True,
        http_status=999,
        response_bytes=capture.MAX_RESPONSE_BYTES + 1,
    )

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert receipt["failure_category"] == "unknown"
    assert receipt["http_status"] is None
    assert receipt["response_bytes"] is None
    assert receipt["response_interpretation"] == "not_parsed"
    assert receipt["total_pages"] == "unknown"

    malformed_code = capture._failure_receipt(
        "a" * 64,
        failure_code=cast("str", ["synthetic", "private", "detail"]),
        request_attempted=True,
    )
    assert malformed_code["failure_code"] == "local_capture_operation_failed"


@pytest.mark.parametrize(
    ("category", "status", "expected_delivery"),
    [
        (FailureCategory.NETWORK, None, "unknown"),
        (FailureCategory.RATE_LIMITED, 429, "response_received"),
        (FailureCategory.UPSTREAM_ERROR, 503, "response_received"),
    ],
)
def test_exact_http_fetch_error_emits_only_fixed_category_and_valid_status(
    category: FailureCategory,
    status: int | None,
    expected_delivery: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typed failures preserve only enum/status diagnostics and the one-send scope."""
    _enable_fake_environment(monkeypatch)
    error = HttpFetchError(
        category,
        url="https://bse.example/private?token=DO_NOT_ECHO",
        host="bse.example",
        attempts=1,
        status=status,
        retryable=True,
    )
    client = _FakeBoundedPostClient(error)
    evidence_dir = tmp_path / "bse-typed-failure"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    encoded = (evidence_dir / "receipt.json").read_bytes()
    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "request_failed_or_outcome_unknown"
    assert receipt["failure_category"] == category.value
    assert receipt["failure_category_source"] == "http_fetch_error"
    assert receipt["http_status"] == status
    assert receipt["transport_attempts"] == 1
    assert receipt["delivery_status"] == expected_delivery
    assert receipt["http_response_observed"] is (status is not None)
    assert receipt["client_invoked"] is True
    assert receipt["budget_reservations"] == 1
    assert receipt["transport_send_ceiling"] == 1
    assert receipt["retry_policy"] == "none"
    assert receipt["raw_response_persisted"] is False
    assert len(client.calls) == 1
    assert b"DO_NOT_ECHO" not in encoded
    assert b"bse.example" not in encoded


def test_spoofed_or_malformed_http_failure_attributes_are_not_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subclasses, malformed categories, and exception text cannot supply diagnostics."""
    _enable_fake_environment(monkeypatch)

    class _SpoofedHttpFetchError(HttpFetchError):
        """A subclass must not impersonate the exact governed transport error."""

    subclass_error = _SpoofedHttpFetchError(
        FailureCategory.NETWORK,
        url="https://bse.example/private?secret=DO_NOT_ECHO",
        host="bse.example",
        attempts=1,
        status=503,
    )
    subclass_error.args = ("SUBCLASS_EXCEPTION_TEXT_DO_NOT_ECHO",)
    client = _FakeBoundedPostClient(subclass_error)
    evidence_dir = tmp_path / "bse-spoofed-error"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    encoded = (evidence_dir / "receipt.json").read_bytes()
    assert receipt["failure_category"] == "unknown"
    assert receipt["failure_category_source"] == "unknown"
    assert receipt["http_status"] is None
    assert receipt["delivery_status"] == "unknown"
    assert receipt["transport_attempts"] is None
    assert receipt["http_response_observed"] is False
    assert receipt["budget_reservations"] == 1
    assert len(client.calls) == 1
    assert b"DO_NOT_ECHO" not in encoded
    assert b"SUBCLASS_EXCEPTION_TEXT_DO_NOT_ECHO" not in encoded


def test_exact_http_fetch_error_with_mutated_category_and_out_of_range_status_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an actual enum member and a 100..599 integer are accepted."""
    _enable_fake_environment(monkeypatch)
    error = HttpFetchError(
        FailureCategory.NETWORK,
        url="https://bse.example/",
        host="bse.example",
        attempts=1,
        status=503,
    )
    error.category = "category-secret-sentinel"  # type: ignore[assignment]
    error.status = 999
    client = _FakeBoundedPostClient(error)
    evidence_dir = tmp_path / "bse-malformed-error-fields"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    encoded = (evidence_dir / "receipt.json").read_bytes()
    assert receipt["failure_category"] == "unknown"
    assert receipt["failure_category_source"] == "unknown"
    assert receipt["http_status"] is None
    assert receipt["delivery_status"] == "unknown"
    assert receipt["transport_attempts"] == 1
    assert receipt["http_response_observed"] is False
    assert b"category-secret-sentinel" not in encoded


def test_http_fetch_error_zero_attempts_means_not_sent_and_rejects_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-send transport error cannot simultaneously claim an HTTP response."""
    _enable_fake_environment(monkeypatch)
    error = HttpFetchError(
        FailureCategory.NETWORK,
        url="https://bse.example/",
        host="bse.example",
        attempts=0,
        status=503,
    )
    client = _FakeBoundedPostClient(error)

    receipt = capture.run_bse_instrument_catalog_capture(
        tmp_path / "bse-zero-transport-attempts",
        client_factory=lambda: client,
    )

    assert receipt["failure_category"] == "network"
    assert receipt["failure_category_source"] == "http_fetch_error"
    assert receipt["http_status"] is None
    assert receipt["transport_attempts"] == 0
    assert receipt["budget_reservations"] == 1
    assert receipt["client_invoked"] is True
    assert receipt["http_response_observed"] is False
    assert receipt["delivery_status"] == "not_sent"
    assert len(client.calls) == 1


def test_request_budget_rejection_is_local_and_never_claims_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local request authorization failures stay separate from uncertain transport."""
    _enable_fake_environment(monkeypatch)
    client = _FakeBoundedPostClient(
        RequestBudgetError("PRIVATE_REQUEST_BUDGET_DETAIL"),
        reserve_attempt=False,
    )
    evidence_dir = tmp_path / "bse-local-budget-rejection"

    receipt = capture.run_bse_instrument_catalog_capture(
        evidence_dir,
        client_factory=lambda: client,
    )

    encoded = (evidence_dir / "receipt.json").read_bytes()
    assert receipt["failure_code"] == "governed_request_rejected"
    assert receipt["failure_category"] == "unknown"
    assert receipt["failure_category_source"] == "local"
    assert receipt["budget_reservations"] == 0
    assert receipt["transport_attempts"] is None
    assert receipt["client_invoked"] is True
    assert receipt["http_response_observed"] is False
    assert receipt["delivery_status"] == "unknown"
    assert len(client.calls) == 1
    assert b"PRIVATE_REQUEST_BUDGET_DETAIL" not in encoded
