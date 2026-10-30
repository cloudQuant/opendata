"""Offline behavior and safety tests for the one-request Sina lrb raw canary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from scripts.ops import sina_lrb_raw_canary as canary


def _response(body: bytes, *, status: int = 200) -> requests.Response:
    """Create a synthetic standard Requests response with exact raw bytes."""
    result = requests.Response()
    result.status_code = status
    result.headers["Content-Encoding"] = "identity"
    result.headers["Content-Length"] = str(len(body))
    result.raw = io.BytesIO(body)
    return result


def _offline_client() -> canary._LiveClient:
    """Return the production governed client shape without any network access."""
    live = canary._new_live_client()
    assert type(live.client) is GovernedHttpClient
    assert type(live.session) is requests.Session
    return live


def _testing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select the explicit test-only client factory environment."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")


def test_governed_canary_sends_exact_fixed_query_and_stores_only_raw_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real governed client pipeline with an in-memory adapter response."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    body = b'{"synthetic":"row-value-should-stay-in-private-blob"}'
    sends: list[dict[str, object]] = []

    def fake_send(
        adapter: HTTPAdapter,
        request: requests.PreparedRequest,
        **kwargs: Any,
    ) -> requests.Response:
        """Return a local response at the standard Requests adapter boundary."""
        del adapter
        parsed = urlsplit(request.url)
        assert request.method == "GET"
        assert parsed.scheme == "https"
        assert parsed.netloc == canary.HOST
        assert parsed.path == canary.ENDPOINT_PATH
        assert parse_qs(parsed.query) == {
            "paperCode": ["sh600519"],
            "source": ["lrb"],
            "type": ["0"],
            "page": ["1"],
            "num": ["1000"],
        }
        assert request.headers["Accept-Encoding"] == "identity"
        assert kwargs["stream"] is True
        assert kwargs["timeout"] == (canary.CONNECT_TIMEOUT_SECONDS, canary.READ_TIMEOUT_SECONDS)
        assert kwargs["proxies"] == {}
        response = _response(body)
        response.url = request.url
        response.request = request
        sends.append({"method": request.method, "host": parsed.netloc, "path": parsed.path})
        return response

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    evidence_dir = tmp_path / "sina-lrb-canary"
    receipt = canary.run_sina_lrb_raw_canary(evidence_dir)

    assert receipt["outcome"] == "raw_response_captured"
    assert receipt["source"] == "sina"
    assert receipt["canonical_model"] == "financial_statement"
    assert receipt["query"] == canary.QUERY_PARAMS
    assert receipt["budget_reservations"] == 1
    assert receipt["transport_attempts"] == 1
    assert receipt["automatic_retry"] is False
    assert receipt["redirects"] == "disabled"
    assert receipt["response_interpretation"] == "not_attempted"
    assert receipt["pagination"] == "unknown"
    assert receipt["historical_coverage"] == "not_established"
    assert receipt["row_values_in_receipt"] is False
    assert len(sends) == 1

    receipt_bytes = (evidence_dir / "receipt.json").read_bytes()
    assert b"row-value-should-stay" not in receipt_bytes
    assert b"/home/" not in receipt_bytes
    raw_files = tuple((evidence_dir / "raw" / "objects").rglob("*.blob"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == body
    assert hashlib.sha256(body).hexdigest() == receipt["response_sha256"]
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(raw_files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((evidence_dir / "receipt.json").stat().st_mode) == 0o600
    manifest_files = tuple((evidence_dir / "raw" / "manifests").rglob("*.json"))
    assert len(manifest_files) == 1
    manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    assert manifest["response_sha256"] == receipt["response_sha256"]
    assert manifest["response_size_bytes"] == len(body)
    assert manifest["source_history_limit"] == "unknown"


def test_explicit_offline_factory_accepts_governed_client_and_closes_its_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test seam accepts the actual governed client while transport remains local."""
    _testing_environment(monkeypatch)
    body = b"synthetic lrb fixture"
    sends = 0
    live = _offline_client()
    closed = False
    original_close = live.session.close

    def tracked_close() -> None:
        nonlocal closed
        closed = True
        original_close()

    def fake_send(
        _adapter: HTTPAdapter,
        request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        """Return an in-memory response at the governed transport boundary."""
        nonlocal sends
        sends += 1
        response = _response(body)
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(live.session, "close", tracked_close)
    monkeypatch.setattr(HTTPAdapter, "send", fake_send)

    receipt = canary.run_sina_lrb_raw_canary(
        tmp_path / "explicit-offline-factory",
        client_factory=lambda: live,
    )

    assert receipt["outcome"] == "raw_response_captured"
    assert receipt["transport_attempts"] == 1
    assert sends == 1
    assert closed is True


def test_budget_is_exact_to_sina_model_operation_and_host() -> None:
    """Pin the one-shot local grant and live transport safety configuration."""
    budget = canary._new_request_budget()
    grant = budget.grants[0]
    live = canary._new_live_client()
    try:
        assert budget.task_attempt_limit == 1
        assert budget.source_attempt_limit == 1
        assert grant.source == "sina"
        assert grant.canonical_model == "financial_statement"
        assert grant.operation is RequestOperation.QUERY
        assert grant.decision is GrantDecision.ALLOWED
        assert grant.allowed_hosts == frozenset({"quotes.sina.cn"})
        assert budget.profile is RequestBudgetProfile.BOUNDED_REQUEST
        assert live.session.trust_env is False
        assert live.session.auth is None
        assert not live.session.proxies
        assert not live.session.cookies
        assert live.client._config.max_attempts == 1
        assert live.client._config.rate_burst == 1
        assert live.client._config.rate_max_wait == 0.0
        assert live.client._config.proxies == {}
        assert live.client._raw_response_cache_override is None
        assert all(adapter.max_retries.total == 0 for adapter in live.session.adapters.values())
    finally:
        live.session.close()


def test_timeout_is_terminal_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network timeout yields one safe receipt and one transport send."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    sends = 0

    def timeout_send(
        _adapter: HTTPAdapter,
        _request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        nonlocal sends
        sends += 1
        raise requests.Timeout("synthetic private timeout detail")

    monkeypatch.setattr(HTTPAdapter, "send", timeout_send)
    evidence_dir = tmp_path / "timeout-run"
    receipt = canary.run_sina_lrb_raw_canary(evidence_dir)

    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["failure_code"] == "request_failed_or_outcome_unknown"
    assert receipt["failure_category"] == FailureCategory.TIMEOUT.value
    assert receipt["transport_attempts"] == 1
    assert receipt["budget_reservations"] == 1
    assert receipt["raw_response_persisted"] is False
    assert sends == 1
    receipt_text = (evidence_dir / "receipt.json").read_text(encoding="utf-8")
    assert "synthetic private timeout detail" not in receipt_text


def test_redirect_is_rejected_without_following_a_second_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 response never causes the governed request to contact its Location."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    sends: list[str] = []

    def redirect_send(
        _adapter: HTTPAdapter,
        request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        sends.append(request.url)
        result = _response(b"", status=302)
        result.headers["Location"] = "https://redirect.invalid/private"
        result.url = request.url
        result.request = request
        return result

    monkeypatch.setattr(HTTPAdapter, "send", redirect_send)
    receipt = canary.run_sina_lrb_raw_canary(tmp_path / "redirect-run")

    assert receipt["failure_category"] == FailureCategory.REDIRECT.value
    assert receipt["transport_attempts"] == 1
    assert len(sends) == 1
    assert "redirect.invalid" not in json.dumps(receipt)


def test_grant_rejection_is_before_transport_and_records_zero_reservations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact-host mismatch is denied before the adapter can send anything."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    wrong_grant = RequestGrant(
        source=canary.SOURCE,
        canonical_model=canary.CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic_wrong_host_test",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={"not-quotes.sina.cn"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        profile=profile,
    )
    monkeypatch.setattr(
        canary,
        "_new_request_budget",
        lambda: RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(wrong_grant,),
            profile=profile,
        ),
    )
    monkeypatch.setattr(
        HTTPAdapter,
        "send",
        lambda *_args, **_kwargs: pytest.fail("host-mismatched grant must block send"),
    )
    evidence_dir = tmp_path / "grant-denied"

    receipt = canary.run_sina_lrb_raw_canary(evidence_dir)

    assert receipt["failure_code"] == "governed_request_rejected"
    assert receipt["budget_reservations"] == 0
    assert receipt["transport_attempts"] == 0
    assert receipt["delivery_status"] == "not_sent"
    assert (evidence_dir / "run-reservation.json").is_file()
    assert (evidence_dir / "receipt.json").is_file()


def test_response_over_cap_is_rejected_without_persisting_source_bytes() -> None:
    """Defensive response validation rejects an oversized fake client contract."""
    live = _offline_client()
    body = b"x" * (canary.MAX_RESPONSE_BYTES + 1)
    live.client.get_bounded = lambda *_args, **_kwargs: BoundedHttpResponse(
        body=body,
        status_code=200,
        retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
    )
    writer = _NoWriteWriter()
    try:
        receipt = canary._capture_response(
            live.client,
            writer,
            canary._request_digest(),
        )
    finally:
        live.session.close()

    assert receipt["failure_code"] == "response_body_too_large"
    assert receipt["response_bytes"] is None
    assert receipt["response_sha256"] is None
    assert receipt["response_suppressed"] is True
    assert writer.called is False


class _NoWriteWriter:
    """Sentinel writer proving invalid responses stop before persistence."""

    def __init__(self) -> None:
        """Record whether invalid data reaches the writer."""
        self.called = False

    def capture(self, *_args: object, **_kwargs: object) -> None:
        """Record unexpected persistence without hiding it in the caller's catch."""
        self.called = True


def test_writer_failure_is_sanitized_and_never_triggers_a_second_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw writer failure is terminal and does not leak exception details."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    sends = 0

    def fake_send(
        _adapter: HTTPAdapter,
        request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        nonlocal sends
        sends += 1
        response = _response(b"synthetic body")
        response.url = request.url
        response.request = request
        return response

    class FailingWriter:
        """Replace only the local persistence boundary for this test."""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def capture(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("synthetic filesystem private detail")

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    monkeypatch.setattr(canary, "RawResponseArtifactWriter", FailingWriter)
    evidence_dir = tmp_path / "writer-failed"

    receipt = canary.run_sina_lrb_raw_canary(evidence_dir)

    assert receipt["failure_code"] == "raw_persistence_failed_or_unknown"
    assert receipt["raw_persistence_state"] == "unknown_after_writer_failure"
    assert receipt["raw_response_persisted"] is False
    assert receipt["transport_attempts"] == 1
    assert sends == 1
    assert "synthetic filesystem private detail" not in (evidence_dir / "receipt.json").read_text(
        encoding="utf-8"
    )


def test_offline_factory_requires_testing_mode_and_governed_client_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offline seam cannot return a transport-shaped object or run in dev mode."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "false")
    with pytest.raises(canary.SinaCanaryError, match="local_capture_operation_failed"):
        canary.run_sina_lrb_raw_canary(
            tmp_path / "wrong-env",
            client_factory=lambda: pytest.fail("factory called before environment gate"),
        )

    _testing_environment(monkeypatch)
    receipt = canary.run_sina_lrb_raw_canary(
        tmp_path / "wrong-client-shape",
        client_factory=lambda: None,  # type: ignore[arg-type]
    )
    assert receipt["failure_code"] == "client_contract_invalid"


def test_live_environment_gate_runs_before_evidence_path_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live request cannot start from a test or implicit runtime environment."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "false")
    evidence_dir = tmp_path / "live-rejected"

    with pytest.raises(canary.SinaCanaryError, match="local_capture_operation_failed"):
        canary.run_sina_lrb_raw_canary(evidence_dir)

    assert not evidence_dir.exists()


def test_private_directory_rejects_existing_relative_and_symlink_paths(tmp_path: Path) -> None:
    """The writer never reuses a path or follows a symlinked evidence ancestor."""
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(canary.SinaCanaryError, match="evidence_directory_must_be_new"):
        canary._create_private_evidence_directory(existing)
    with pytest.raises(canary.SinaCanaryError, match="evidence_directory_invalid"):
        canary._create_private_evidence_directory(Path("relative-run"))
    with pytest.raises(canary.SinaCanaryError, match="evidence_directory_invalid"):
        canary._create_private_evidence_directory(tmp_path / "parent" / ".." / "unsafe")

    if not canary._O_NOFOLLOW:
        pytest.skip("platform does not expose O_NOFOLLOW")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(canary.SinaCanaryError, match="evidence_directory_unavailable"):
        canary._create_private_evidence_directory(link / "new")


def test_receipt_is_no_clobber_and_only_whitelisted_filenames_are_writable(
    tmp_path: Path,
) -> None:
    """Atomic local receipt writes preserve the original metadata and safe names."""
    evidence_dir = tmp_path / "receipt-run"
    descriptor = canary._create_private_evidence_directory(evidence_dir)
    try:
        canary._write_json_exclusive(descriptor, "receipt.json", {"state": "first"})
        canary._write_json_if_missing(descriptor, "receipt.json", {"state": "second"})
        with pytest.raises(FileExistsError):
            canary._write_json_exclusive(descriptor, "receipt.json", {"state": "third"})
        with pytest.raises(canary.SinaCanaryError, match="local_capture_operation_failed"):
            canary._write_json_exclusive(descriptor, "unexpected.json", {})
    finally:
        os.close(descriptor)

    assert json.loads((evidence_dir / "receipt.json").read_text(encoding="utf-8")) == {
        "state": "first"
    }


def test_invalid_fake_response_contract_stops_before_raw_writer() -> None:
    """An untyped object or non-2xx value from a fake boundary is never persisted."""
    live = _offline_client()
    writer = _NoWriteWriter()
    try:
        live.client.get_bounded = lambda *_args, **_kwargs: object()  # type: ignore[method-assign]
        invalid = canary._capture_response(live.client, writer, canary._request_digest())
        assert invalid["failure_code"] == "response_contract_invalid"

        live.client.get_bounded = lambda *_args, **_kwargs: BoundedHttpResponse(
            body="not bytes",  # type: ignore[arg-type]
            status_code=200,
            retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        )
        malformed = canary._capture_response(live.client, writer, canary._request_digest())
        assert malformed["failure_code"] == "response_contract_invalid"

        live.client.get_bounded = lambda *_args, **_kwargs: BoundedHttpResponse(
            body=b"synthetic",
            status_code=503,
            retrieved_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
        )
        non_success = canary._capture_response(live.client, writer, canary._request_digest())
        assert non_success["failure_code"] == "response_contract_invalid"
        assert non_success["http_status"] == 503
    finally:
        live.session.close()
    assert writer.called is False


def test_failure_receipt_sanitizes_unrecognized_codes_and_status_values() -> None:
    """Receipt builders reduce arbitrary inputs to fixed, bounded metadata."""
    receipt = canary._failure_receipt(
        canary._request_digest(),
        failure_code="private exception message",
        request_attempted=True,
        http_status=999,
        response_bytes=canary.MAX_RESPONSE_BYTES + 1,
        failure_category="unregistered-category",
        transport_attempts=2,
    )

    assert receipt["failure_code"] == "local_capture_operation_failed"
    assert receipt["failure_category"] == "unknown"
    assert receipt["http_status"] is None
    assert receipt["response_bytes"] is None
    assert receipt["transport_attempts"] is None
    assert "private exception message" not in json.dumps(receipt)


def test_request_digest_is_stable_and_does_not_write_parameters_to_manifest() -> None:
    """The request contract is fixed and represented in raw evidence only by a digest."""
    first = canary._request_digest()
    second = canary._request_digest()
    reservation = canary._run_reservation(first)

    assert first == second
    assert reservation["request_summary_sha256"] == first
    assert "paperCode" not in json.dumps(reservation)
    assert "sh600519" not in json.dumps(reservation)
    assert canary.QUERY_PARAMS == {
        "paperCode": "sh600519",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }


@pytest.mark.parametrize(
    ("outcome", "expected_exit_code"),
    [("raw_response_captured", 0), ("partial_or_failed_capture", 1)],
)
def test_cli_prints_anonymous_receipt_and_maps_outcome_to_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
    expected_exit_code: int,
) -> None:
    """CLI success and failure both expose only the fixed receipt and correct status."""
    receipt = {"outcome": outcome, "row_values_in_receipt": False}
    seen_paths: list[Path] = []

    def fake_capture(evidence_dir: Path) -> dict[str, object]:
        """Record only the CLI argument and return a synthetic anonymous receipt."""
        seen_paths.append(evidence_dir)
        return receipt

    monkeypatch.setattr(canary, "run_sina_lrb_raw_canary", fake_capture)
    evidence_dir = tmp_path / "cli-evidence"

    exit_code = canary.main(["--evidence-dir", str(evidence_dir)])

    assert exit_code == expected_exit_code
    assert seen_paths == [evidence_dir]
    assert capsys.readouterr().out == json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n"
