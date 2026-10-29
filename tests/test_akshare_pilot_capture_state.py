"""Offline behavior tests for explicit AkShare pilot capture states.

All responses and failures are synthetic. These tests establish local manifest
semantics and one-shot behavior; they do not contact Eastmoney or prove source
availability or historical completeness.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from opendata.data.http_client import BoundedHttpResponse, FailureCategory, HttpFetchError
from opendata.data.request_budget import current_request_scopes, reserve_scoped_attempt
from scripts.ops import akshare_stock_daily_pilot as pilot

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

_VALID_BODY = (
    b'{"rc":0,"data":{"code":"600519","market":1,"klines":['
    b'"2026-10-12,1685,1688,1690,1680,1.23,2076240,0,0,0,0"]}}'
)
_EMPTY_BODY = b'{"rc":0,"data":{"code":"600519","market":1,"klines":[]}}'
_RETRIEVED_AT = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)


class _FakeClient:
    """Return one synthetic result while recording physical client calls."""

    def __init__(self, result: Any, *, reserve_before_error: bool = False) -> None:
        self.result = result
        self.reserve_before_error = reserve_before_error
        self.calls = 0
        self.transport_attempts = 0
        self.close_calls = 0

    def get_bounded(self, _url: str, **_kwargs: Any) -> Any:
        self.calls += 1
        if isinstance(self.result, HttpFetchError):
            reserved_attempts = self.result.attempts
            if self.result.attempts == 0 and self.reserve_before_error:
                reserved_attempts = 1
            for _ in range(reserved_attempts):
                reserve_scoped_attempt(
                    current_request_scopes(), pilot.SOURCE, pilot.EASTMONEY_KLINE_HOST
                )
            self.transport_attempts += self.result.attempts
        else:
            reserve_scoped_attempt(
                current_request_scopes(), pilot.SOURCE, pilot.EASTMONEY_KLINE_HOST
            )
            self.transport_attempts += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.close_calls += 1


def _response(body: bytes = _VALID_BODY, *, status_code: int = 200) -> BoundedHttpResponse:
    return BoundedHttpResponse(body, status_code, _RETRIEVED_AT)


def _prepare_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
) -> tuple[Path, Path]:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(pilot, "_create_pilot_client", lambda: client)

    authorization_dir = tmp_path / "authorization"
    authorization_dir.mkdir(mode=0o700)
    authorization_dir.chmod(0o700)
    output_parent = tmp_path / "captures"
    output_parent.mkdir(mode=0o700)
    output_parent.chmod(0o700)

    issued_at = pilot._utcnow()
    payload = {
        "schema_version": 1,
        "purpose": pilot.PILOT_PURPOSE,
        "symbol": pilot.PILOT_SYMBOL,
        "start_date": pilot.PILOT_START_DATE.isoformat(),
        "end_date": pilot.PILOT_END_DATE.isoformat(),
        "adjustment_basis": pilot.PILOT_ADJUSTMENT_BASIS,
        "max_requests": pilot.PILOT_MAX_REQUESTS,
        "max_response_bytes": pilot.PILOT_MAX_RESPONSE_BYTES,
        "issued_at_utc": pilot._format_utc(issued_at),
        "expires_at_utc": pilot._format_utc(issued_at + timedelta(minutes=5)),
        "nonce": "b" * 32,
    }
    authorization = authorization_dir / "authorization.json"
    descriptor = os.open(authorization, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return authorization, output_parent / "capture"


def _manifest(evidence_dir: Path) -> dict[str, Any]:
    return json.loads((evidence_dir / "capture.json").read_text(encoding="utf-8"))


def test_nonempty_sample_records_sample_captured_and_preserves_reservation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    manifest = pilot.run_pilot(authorization, evidence_dir)

    assert manifest["capture_state"] == "sample_captured"
    assert manifest["outcome"] == "bounded_sample_captured"
    assert json.loads((evidence_dir / "reservation.json").read_text())["state"] == (
        "attempt_reserved_before_transport"
    )
    assert client.calls == 1
    assert client.close_calls == 1


def test_http_error_with_status_records_response_received(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = HttpFetchError(
        FailureCategory.RATE_LIMITED,
        url=pilot.EASTMONEY_KLINE_URL,
        host=pilot.EASTMONEY_KLINE_HOST,
        attempts=1,
        status=429,
        retryable=True,
    )
    client = _FakeClient(error)
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_http_request_failed"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "http_error_response_received"
    assert manifest["http_status"] == 429
    assert client.calls == 1


@pytest.mark.parametrize(
    ("status_code", "expected_state"),
    [
        (201, "response_rejected"),
        (204, "response_rejected"),
        (301, "http_error_response_received"),
        (404, "http_error_response_received"),
        (503, "http_error_response_received"),
    ],
)
def test_returned_http_status_has_precise_capture_state(
    status_code: int,
    expected_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_response(status_code=status_code))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == expected_state
    assert manifest["http_status"] == status_code
    assert client.calls == 1


@pytest.mark.parametrize(
    ("result", "expected_status"),
    [
        (
            HttpFetchError(
                FailureCategory.NETWORK,
                url=pilot.EASTMONEY_KLINE_URL,
                host=pilot.EASTMONEY_KLINE_HOST,
                attempts=1,
                retryable=False,
            ),
            None,
        ),
        (
            HttpFetchError(
                FailureCategory.TIMEOUT,
                url=pilot.EASTMONEY_KLINE_URL,
                host=pilot.EASTMONEY_KLINE_HOST,
                attempts=1,
                retryable=False,
            ),
            None,
        ),
        (
            HttpFetchError(
                FailureCategory.NETWORK,
                url=pilot.EASTMONEY_KLINE_URL,
                host=pilot.EASTMONEY_KLINE_HOST,
                attempts=1,
                status=200,
                retryable=False,
            ),
            200,
        ),
        (
            HttpFetchError(
                FailureCategory.TIMEOUT,
                url=pilot.EASTMONEY_KLINE_URL,
                host=pilot.EASTMONEY_KLINE_HOST,
                attempts=1,
                status=200,
                retryable=False,
            ),
            200,
        ),
        (RuntimeError("synthetic unexpected transport failure"), None),
    ],
    ids=[
        "network-without-status",
        "timeout-without-status",
        "network-after-status",
        "timeout-after-status",
        "unexpected-transport-exception",
    ],
)
def test_transport_failure_is_unknown_and_cannot_be_retried(
    result: Exception,
    expected_status: int | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(result)
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "transport_outcome_unknown"
    assert manifest["http_status"] == expected_status
    assert client.calls == 1
    with pytest.raises(pilot.PilotError, match="authorization_nonce_already_consumed"):
        pilot.run_pilot(authorization, evidence_dir.parent / "retry")
    assert client.calls == 1


@pytest.mark.parametrize(
    ("category", "reserve_before_error", "expected_budget_attempts"),
    [
        (FailureCategory.RATE_LIMITED, False, 0),
        (FailureCategory.TIMEOUT, True, 1),
    ],
    ids=["preflight-before-budget-reservation", "deadline-after-budget-reservation"],
)
def test_zero_transport_attempts_are_known_pre_send_failure(
    category: FailureCategory,
    reserve_before_error: bool,
    expected_budget_attempts: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = HttpFetchError(
        category,
        url=pilot.EASTMONEY_KLINE_URL,
        host=pilot.EASTMONEY_KLINE_HOST,
        attempts=0,
        status=None,
        retryable=False,
    )
    client = _FakeClient(error, reserve_before_error=reserve_before_error)
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_http_request_failed"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "failed_before_transport"
    assert manifest["attempts_used"] == expected_budget_attempts
    assert manifest["http_status"] is None
    assert client.calls == 1
    assert client.transport_attempts == 0
    assert client.close_calls == 1
    with pytest.raises(pilot.PilotError, match="authorization_nonce_already_consumed"):
        pilot.run_pilot(authorization, evidence_dir.parent / "retry")
    assert client.calls == 1
    assert client.transport_attempts == 0


def test_unexpected_request_result_without_status_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(object())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "transport_outcome_unknown"
    assert manifest["http_status"] is None
    assert client.calls == 1


def test_expired_admission_before_transport_records_failed_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    def expire_before_send(_authorization: object) -> None:
        raise pilot.PilotError("authorization_expired_before_transport")

    monkeypatch.setattr(pilot, "_require_authorization_live", expire_before_send)
    with pytest.raises(pilot.PilotError, match="authorization_expired_before_transport"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "failed_before_transport"
    assert manifest["attempts_used"] == 0
    assert client.calls == 0
    assert client.close_calls == 1


def test_received_empty_response_is_rejected_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(_response(_EMPTY_BODY))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = _manifest(evidence_dir)
    assert manifest["capture_state"] == "response_rejected"
    assert manifest["failure_category"] == "empty_sample"
    assert manifest["response_body_saved"] is True
    assert client.calls == 1
