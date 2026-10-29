"""Offline tests for bounded, value-redacted SSE instrument capture."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

os.environ["APP_ENV"] = "testing"
os.environ["TESTING"] = "true"
os.environ["REDIS_URL"] = ""
os.environ["SENTRY_DSN"] = ""

import pytest

from opendata.data.http_client import BoundedHttpResponse
from opendata.data.request_budget import (
    current_request_scopes,
    reserve_scoped_attempt,
)
from scripts.ops import sse_instrument_lifecycle_capture as capture

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _body(rows: Sequence[Mapping[str, object]] = (), *, total: int = 1) -> bytes:
    """Encode a synthetic SSE response envelope for a no-network test."""
    return json.dumps(
        {
            "result": [dict(row) for row in rows],
            "pageHelp": {"pageNo": 1, "pageSize": capture.PAGE_SIZE, "total": total},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _response(body: bytes, *, status: int = 200) -> BoundedHttpResponse:
    """Wrap synthetic bytes in the governed client's bounded result type."""
    return BoundedHttpResponse(
        body=body,
        status_code=status,
        retrieved_at=datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc),
    )


class _FakeClient:
    """A deterministic offline client that reserves every modeled physical send."""

    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.scope_limits: list[tuple[int, int, bool]] = []
        self.closed = False

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
        """Record fixed request facts and consume one finite local grant."""
        del overall_timeout, timeout
        scopes = current_request_scopes()
        assert len(scopes) == 1
        scope = scopes[0]
        self.scope_limits.append(
            (
                scope.budget.task_attempt_limit,
                scope.budget.source_attempt_limit,
                scope.deadline is not None,
            )
        )
        assert max_body_bytes == capture.MAX_RESPONSE_BYTES
        reserve_scoped_attempt(scopes, source, capture.HOST)
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast("BoundedHttpResponse", response)

    def close(self) -> None:
        """Record that the runner closed the injected transport."""
        self.closed = True


def _run(tmp_path: Path, fake: _FakeClient) -> tuple[dict[str, object], Path]:
    """Execute one offline capture beneath a new temporary private directory."""
    evidence_dir = tmp_path / "sse-evidence"
    receipt = capture.run_sse_instrument_capture(evidence_dir, client=fake)
    return receipt, evidence_dir


def test_four_fixed_selectors_capture_private_raw_and_anonymous_receipts(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC-PRIVATE-ROW-MARKER"
    rows = [
        {
            "A_STOCK_CODE": "600001",
            "COMPANY_CODE": "company-identifier-not-security-id",
            "DELIST_DATE": "2026-10-29",
            "SEC_NAME_CN": marker,
        }
    ]
    fake = _FakeClient([_response(_body(rows)) for _ in capture._SELECTORS])

    receipt, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "bounded_unverified_sse_snapshot_captured"
    assert receipt["requests_attempted"] == 4
    assert receipt["requests_succeeded"] == 4
    assert receipt["pagination_status"] == "UNKNOWN"
    assert receipt["source_authenticity"] == "UNKNOWN"
    assert receipt["market_completeness"] == "UNKNOWN"
    assert receipt["historical_coverage"] == "UNKNOWN"
    assert receipt["identity_resolution"] == "not_performed"
    assert receipt["lifecycle_inference"] == "not_performed"
    assert marker not in json.dumps(receipt, ensure_ascii=False)
    assert fake.closed is True
    assert fake.scope_limits == [(8, 8, True)] * 4
    assert [call[1]["STOCK_TYPE"] for call in fake.calls] == ["1", "8", "1", "8"]
    assert [call[1]["COMPANY_STATUS"] for call in fake.calls] == [
        "2,4,5,7,8",
        "2,4,5,7,8",
        "3",
        "3",
    ]
    assert all(call[0] == capture.ENDPOINT for call in fake.calls)
    assert all(call[1]["pageHelp.pageSize"] == str(capture.PAGE_SIZE) for call in fake.calls)
    assert all(call[1]["pageHelp.pageNo"] == "1" for call in fake.calls)
    assert all("Authorization" not in call[2] for call in fake.calls)

    reservation_files = tuple(evidence_dir.glob("page-*-reservation.json"))
    page_receipt_files = tuple(evidence_dir.glob("page-*-receipt.json"))
    assert len(reservation_files) == len(page_receipt_files) == 4
    for path in (*reservation_files, *page_receipt_files):
        assert marker not in path.read_text(encoding="utf-8")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["automatic_retry"] is False
        for path in reservation_files
    )

    raw_files = tuple((evidence_dir / "raw").rglob("*"))
    body_files = [path for path in raw_files if path.suffix == ".blob"]
    manifest_files = [path for path in raw_files if path.suffix == ".json"]
    assert len(body_files) == 1
    assert len(manifest_files) == 4
    assert hashlib.sha256(body_files[0].read_bytes()).hexdigest() == body_files[0].stem
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in raw_files if path.is_dir())
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in raw_files if path.is_file())
    for path in (evidence_dir / "receipt.json", *evidence_dir.glob("page-*-receipt.json")):
        assert marker not in path.read_text(encoding="utf-8")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_full_first_page_requests_only_one_following_page_and_never_claims_completeness(
    tmp_path: Path,
) -> None:
    first_page = _response(
        _body(
            [{"source_field": f"row-{index:04d}"} for index in range(capture.PAGE_SIZE)],
            total=50_000,
        )
    )
    second_page = _response(_body([], total=50_000))
    fake = _FakeClient(
        [first_page, second_page, *(_response(_body()) for _ in capture._SELECTORS[1:])]
    )

    receipt, _evidence_dir = _run(tmp_path, fake)

    assert receipt["requests_attempted"] == 5
    assert fake.calls[0][1]["pageHelp.pageNo"] == "1"
    assert fake.calls[1][1]["pageHelp.pageNo"] == "2"
    selector_receipts = receipt["selectors"]
    assert isinstance(selector_receipts, list)
    assert selector_receipts[0]["row_count_observed"] == capture.PAGE_SIZE + 0
    assert selector_receipts[0]["pagination_observation"] == "short_page_observed"
    assert receipt["market_completeness"] == "UNKNOWN"
    assert "50000" not in json.dumps(receipt)


def test_transport_failure_is_not_retried_and_receipt_omits_exception_text(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC-TRANSPORT-DETAIL-MARKER"
    fake = _FakeClient([RuntimeError(marker)])

    receipt, evidence_dir = _run(tmp_path, fake)

    assert len(fake.calls) == 1
    assert fake.closed is True
    assert receipt["outcome"] == "partial_or_failed_capture"
    assert receipt["requests_failed"] == 1
    assert marker not in json.dumps(receipt)
    assert not (evidence_dir / "raw").exists()


def test_parser_rejection_keeps_only_raw_hash_and_sanitized_failure_receipt(
    tmp_path: Path,
) -> None:
    marker = "SYNTHETIC-MALFORMED-BODY-MARKER"
    fake = _FakeClient([_response(f"bad:{marker}".encode())])

    receipt, evidence_dir = _run(tmp_path, fake)

    assert receipt["requests_failed"] == 1
    assert marker not in json.dumps(receipt)
    page_receipts = receipt["page_receipts"]
    assert isinstance(page_receipts, list) and page_receipts
    page_receipt = page_receipts[0]
    assert isinstance(page_receipt, dict)
    assert page_receipt["raw_response_persisted"] is True
    assert page_receipt["failure_code"] == "response_json_invalid"
    raw_body = next((evidence_dir / "raw").rglob("*.blob"))
    assert raw_body.read_bytes() == f"bad:{marker}".encode()


def test_reusing_evidence_directory_fails_before_sending_again(tmp_path: Path) -> None:
    fake = _FakeClient([_response(_body()) for _ in capture._SELECTORS])
    evidence_dir = tmp_path / "sse-evidence"
    capture.run_sse_instrument_capture(evidence_dir, client=fake)
    calls_after_first_capture = len(fake.calls)
    assert calls_after_first_capture == len(capture._SELECTORS)

    with pytest.raises(capture.SSECaptureError) as raised:
        capture.run_sse_instrument_capture(evidence_dir, client=fake)

    assert raised.value.code == "evidence_directory_must_be_new"
    assert len(fake.calls) == calls_after_first_capture


def test_only_explicit_live_mode_can_create_a_real_transport(tmp_path: Path) -> None:
    with pytest.raises(capture.SSECaptureError) as raised:
        capture.run_sse_instrument_capture(tmp_path / "sse-evidence")

    assert raised.value.code == "explicit_live_mode_required"
    assert not (tmp_path / "sse-evidence").exists()


def test_live_transport_is_configured_for_one_send_without_environment_proxies() -> None:
    owned = capture._make_client(None)

    assert isinstance(owned, capture._OwnedGovernedClient)
    assert owned.client._config.max_attempts == 1
    assert owned.session.trust_env is False
    assert all(adapter.max_retries.total == 0 for adapter in owned.session.adapters.values())
    owned.close()


def test_atomic_receipt_write_cleans_partial_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | capture._O_DIRECTORY)
    original_write = os.write
    write_count = 0

    def partial_then_fail(fd: int, value: bytes) -> int:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            return original_write(fd, value[:3])
        raise OSError("synthetic write interruption")

    monkeypatch.setattr(capture.os, "write", partial_then_fail)
    with pytest.raises(capture.SSECaptureError) as raised:
        capture._write_json_exclusive(directory_fd, "receipt.json", {"state": "safe"})
    os.close(directory_fd)

    assert raised.value.code == "receipt_persistence_failed"
    assert tuple(directory.iterdir()) == ()


def test_directory_sync_failure_removes_only_its_unpublished_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | capture._O_DIRECTORY)
    original_fsync = os.fsync
    sync_count = 0

    def fail_after_publish(fd: int) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == 2:
            raise OSError("synthetic directory sync interruption")
        original_fsync(fd)

    monkeypatch.setattr(capture.os, "fsync", fail_after_publish)
    with pytest.raises(capture.SSECaptureError) as raised:
        capture._write_json_exclusive(directory_fd, "receipt.json", {"state": "safe"})
    os.close(directory_fd)

    assert raised.value.code == "receipt_persistence_failed"
    assert tuple(directory.iterdir()) == ()


def test_atomic_receipt_publication_does_not_replace_existing_file(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    original = directory / "receipt.json"
    original.write_bytes(b"existing")
    directory_fd = os.open(directory, os.O_RDONLY | capture._O_DIRECTORY)

    with pytest.raises(capture.SSECaptureError) as raised:
        capture._write_json_exclusive(directory_fd, "receipt.json", {"state": "new"})
    os.close(directory_fd)

    assert raised.value.code == "receipt_persistence_failed"
    assert original.read_bytes() == b"existing"
    assert {path.name for path in directory.iterdir()} == {"receipt.json"}


def test_mode_contract_rejections_happen_before_creating_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeClient([])

    cases: tuple[tuple[dict[str, object], dict[str, str], str], ...] = (
        ({"live": 1}, {}, "live_flag_invalid"),
        (
            {"client": fake, "client_factory": lambda: fake},
            {},
            "offline_client_pair_invalid",
        ),
        ({"live": True, "client": fake}, {}, "live_and_offline_client_conflict"),
        (
            {"live": True},
            {"APP_ENV": "testing", "TESTING": "true"},
            "explicit_development_environment_required",
        ),
        (
            {"live": True},
            {"APP_ENV": "development", "TESTING": "true"},
            "live_capture_disabled_in_testing_environment",
        ),
        (
            {"client": fake},
            {"APP_ENV": "development", "TESTING": "true"},
            "offline_injection_requires_testing_environment",
        ),
        (
            {"client": fake},
            {"APP_ENV": "testing", "TESTING": "false"},
            "offline_injection_requires_testing_flag",
        ),
    )
    for index, (kwargs, environment, expected_code) in enumerate(cases):
        monkeypatch.setenv("APP_ENV", environment.get("APP_ENV", "testing"))
        monkeypatch.setenv("TESTING", environment.get("TESTING", "true"))
        evidence_dir = tmp_path / f"rejected-{index}"
        with pytest.raises(capture.SSECaptureError) as raised:
            capture.run_sse_instrument_capture(evidence_dir, **kwargs)  # type: ignore[arg-type]
        assert raised.value.code == expected_code
        assert not evidence_dir.exists()
    assert fake.calls == []


def test_factory_failures_are_sanitized_and_leave_a_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTHETIC-FACTORY-EXCEPTION-MARKER"

    def failing_factory() -> object:
        raise RuntimeError(marker)

    class MissingClose:
        def get_bounded(self, *_args: object, **_kwargs: object) -> BoundedHttpResponse:
            raise AssertionError("contract validation must happen before transport use")

    factories = (
        (failing_factory, "offline_client_factory_failed"),
        (lambda: object(), "client_contract_invalid"),
        (lambda: MissingClose(), "client_contract_invalid"),
    )
    for index, (factory, expected_code) in enumerate(factories):
        monkeypatch.setenv("APP_ENV", "testing")
        monkeypatch.setenv("TESTING", "true")
        evidence_dir = tmp_path / f"factory-{index}"
        receipt = capture.run_sse_instrument_capture(
            evidence_dir,
            client_factory=factory,  # type: ignore[arg-type]
        )
        assert receipt["stop_code"] == expected_code
        assert receipt["requests_attempted"] == 0
        assert marker not in json.dumps(receipt)
        assert json.loads((evidence_dir / "receipt.json").read_text()) == receipt


def test_client_factory_can_supply_an_offline_transport(tmp_path: Path) -> None:
    fake = _FakeClient([_response(_body()) for _ in capture._SELECTORS])
    receipt = capture.run_sse_instrument_capture(
        tmp_path / "factory-evidence", client_factory=lambda: fake
    )

    assert receipt["requests_succeeded"] == len(capture._SELECTORS)
    assert fake.closed is True


def test_governed_wrapper_delegates_exact_bounded_request_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _response(_body())
    observed: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_get_bounded(self: object, *args: object, **kwargs: object) -> BoundedHttpResponse:
        del self
        observed.append((args, kwargs))
        return expected

    monkeypatch.setattr(capture.GovernedHttpClient, "get_bounded", fake_get_bounded)
    owned = capture._make_client(None)
    actual = owned.get_bounded(
        capture.ENDPOINT,
        max_body_bytes=capture.MAX_RESPONSE_BYTES,
        overall_timeout=capture.REQUEST_TIMEOUT_SECONDS,
        params={"pageHelp.pageNo": "1"},
        headers={"Accept": "application/json"},
        timeout=(5.0, 10.0),
        source=capture.SOURCE,
    )
    owned.close()

    assert actual is expected
    assert len(observed) == 1
    assert observed[0][0] == (capture.ENDPOINT,)
    assert observed[0][1]["max_body_bytes"] == capture.MAX_RESPONSE_BYTES
    assert observed[0][1]["overall_timeout"] == capture.REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "response",
    [
        object(),
        BoundedHttpResponse(
            body=cast("bytes", "not-bytes"),
            status_code=200,
            retrieved_at=datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc),
        ),
        BoundedHttpResponse(
            body=b"",
            status_code=200,
            retrieved_at=datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc),
        ),
        BoundedHttpResponse(
            body=b"body",
            status_code=503,
            retrieved_at=datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc),
        ),
        BoundedHttpResponse(
            body=_body(),
            status_code=200,
            retrieved_at=datetime(2026, 10, 30, 4, 0),
        ),
    ],
)
def test_response_contract_failures_are_receipted_without_parsing(
    tmp_path: Path, response: object
) -> None:
    fake = _FakeClient([response])

    receipt, _evidence_dir = _run(tmp_path, fake)

    assert receipt["requests_failed"] == 1
    page_receipts = receipt["page_receipts"]
    assert isinstance(page_receipts, list) and page_receipts
    assert page_receipts[0]["failure_code"] == "response_contract_invalid"
    assert fake.closed is True


def test_response_size_guard_rejects_over_limit_body_before_raw_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "MAX_RESPONSE_BYTES", 4)
    fake = _FakeClient([_response(b"12345")])

    receipt, evidence_dir = _run(tmp_path, fake)

    assert receipt["requests_failed"] == 1
    page_receipts = receipt["page_receipts"]
    assert isinstance(page_receipts, list) and page_receipts
    assert page_receipts[0]["failure_code"] == "response_contract_invalid"
    assert page_receipts[0]["raw_response_persisted"] is False
    assert not (evidence_dir / "raw").exists()


def test_raw_evidence_failure_is_anonymous_and_stops_after_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTHETIC-RAW-STORE-DETAIL-MARKER"

    def fail_capture(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError(marker)

    monkeypatch.setattr(capture.RawResponseArtifactWriter, "capture", fail_capture)
    fake = _FakeClient([_response(_body())])

    receipt, _evidence_dir = _run(tmp_path, fake)

    assert len(fake.calls) == 1
    assert receipt["requests_failed"] == 1
    assert marker not in json.dumps(receipt)
    page_receipts = receipt["page_receipts"]
    assert isinstance(page_receipts, list) and page_receipts
    assert page_receipts[0]["failure_code"] == "raw_evidence_persistence_failed"
    assert page_receipts[0]["raw_response_persisted"] is None


def test_reservation_and_page_receipt_failures_stop_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = capture._write_json_exclusive
    fake_without_send = _FakeClient([_response(_body())])

    def fail_reservation(directory_fd: int, name: str, value: dict[str, object]) -> None:
        if name.endswith("-reservation.json"):
            raise capture.SSECaptureError("synthetic_reservation_failure")
        original_write(directory_fd, name, value)

    monkeypatch.setattr(capture, "_write_json_exclusive", fail_reservation)
    reservation_receipt, reservation_dir = _run(tmp_path, fake_without_send)
    assert reservation_receipt["stop_code"] == "page_reservation_persistence_failed"
    assert reservation_receipt["requests_attempted"] == 0
    assert fake_without_send.calls == []
    assert not tuple(reservation_dir.glob("page-*-reservation.json"))

    monkeypatch.setattr(capture, "_write_json_exclusive", original_write)
    fake_with_send = _FakeClient([_response(_body())])
    page_case_parent = tmp_path / "page-receipt-case"
    page_case_parent.mkdir(mode=0o700)

    def fail_first_page_receipt(directory_fd: int, name: str, value: dict[str, object]) -> None:
        if name == "page-01-receipt.json":
            raise capture.SSECaptureError("synthetic_page_receipt_failure")
        original_write(directory_fd, name, value)

    monkeypatch.setattr(capture, "_write_json_exclusive", fail_first_page_receipt)
    page_receipt, page_dir = _run(page_case_parent, fake_with_send)
    assert page_receipt["stop_code"] == "page_receipt_persistence_failed"
    assert page_receipt["requests_attempted"] == 1
    assert fake_with_send.closed is True
    assert not (page_dir / "page-01-receipt.json").exists()
    rows = page_receipt["page_receipts"]
    assert isinstance(rows, list) and rows
    assert rows[0]["failure_code"] == "page_receipt_persistence_failed"


def test_terminal_receipt_failure_is_not_reported_as_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = capture._write_json_exclusive

    def fail_terminal(directory_fd: int, name: str, value: dict[str, object]) -> None:
        if name == "receipt.json":
            raise capture.SSECaptureError("receipt_persistence_failed")
        original_write(directory_fd, name, value)

    monkeypatch.setattr(capture, "_write_json_exclusive", fail_terminal)
    fake = _FakeClient([RuntimeError("synthetic transport failure")])

    with pytest.raises(capture.SSECaptureError) as raised:
        capture.run_sse_instrument_capture(tmp_path / "terminal-failure", client=fake)

    assert raised.value.code == "receipt_persistence_failed"
    assert fake.closed is True


def test_request_cap_and_deadline_are_enforced_as_unknown_partial_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture, "MAX_REQUESTS", 3)
    full_page = _response(
        _body([{"source_field": f"row-{index:04d}"} for index in range(capture.PAGE_SIZE)])
    )
    capped_fake = _FakeClient([full_page, full_page, full_page])
    capped, _capped_dir = _run(tmp_path, capped_fake)

    assert capped["requests_attempted"] == 3
    assert capped["stop_code"] == "request_cap_reached"
    assert capped["market_completeness"] == "UNKNOWN"
    assert capped_fake.scope_limits == [(3, 3, True)] * 3

    monkeypatch.setattr(capture, "MAX_REQUESTS", 8)
    readings = iter((100.0, 200.0))
    monkeypatch.setattr(capture, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    deadline_parent = tmp_path / "deadline-case"
    deadline_parent.mkdir(mode=0o700)
    deadline_fake = _FakeClient([])
    deadline, _deadline_dir = _run(deadline_parent, deadline_fake)

    assert deadline["requests_attempted"] == 0
    assert deadline["stop_code"] == "task_deadline_reached"
    assert deadline["outcome"] == "partial_or_failed_capture"
    assert deadline_fake.closed is True


def test_total_response_cap_fails_closed_before_accepting_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _body()
    monkeypatch.setattr(capture, "MAX_TOTAL_RESPONSE_BYTES", len(body) - 1)
    fake = _FakeClient([_response(body)])

    receipt, _evidence_dir = _run(tmp_path, fake)

    assert receipt["stop_code"] == "task_response_byte_cap_exceeded"
    assert receipt["requests_failed"] == 1
    page_receipts = receipt["page_receipts"]
    assert isinstance(page_receipts, list) and page_receipts
    assert page_receipts[0]["failure_code"] == "task_response_byte_cap_exceeded"
    assert page_receipts[0]["parsed_row_count"] is None


def test_local_capture_exception_is_sanitized_into_a_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTHETIC-LOCAL-DETAIL-MARKER"

    def fail_budget() -> object:
        raise RuntimeError(marker)

    monkeypatch.setattr(capture, "_new_request_budget", fail_budget)
    fake = _FakeClient([])

    receipt, evidence_dir = _run(tmp_path, fake)

    assert receipt["stop_code"] == "local_capture_operation_failed"
    assert receipt["requests_attempted"] == 0
    assert marker not in json.dumps(receipt)
    assert json.loads((evidence_dir / "receipt.json").read_text()) == receipt
    assert fake.closed is True


def test_transport_close_exception_does_not_discard_a_persisted_receipt(
    tmp_path: Path,
) -> None:
    class CloseFailureClient(_FakeClient):
        def close(self) -> None:
            self.closed = True
            raise RuntimeError("synthetic close failure")

    fake = CloseFailureClient([_response(_body()) for _ in capture._SELECTORS])
    receipt, evidence_dir = _run(tmp_path, fake)

    assert receipt["outcome"] == "bounded_unverified_sse_snapshot_captured"
    assert json.loads((evidence_dir / "receipt.json").read_text()) == receipt
    assert fake.closed is True


def test_pagination_and_parser_code_helpers_cover_conservative_states() -> None:
    def page_attempt(
        *, outcome: str = "succeeded", row_count: int | None = capture.PAGE_SIZE
    ) -> capture._PageAttempt:
        return capture._PageAttempt(
            selector=capture._SELECTORS[0],
            sequence_number=1,
            page_number=1,
            request_summary_sha256="0" * 64,
            outcome=outcome,
            row_count=row_count,
        )

    assert capture._pagination_observation(()) == "not_attempted"
    assert capture._pagination_observation([page_attempt(row_count=None)]) == "bounded_sample_only"
    assert (
        capture._pagination_observation([page_attempt(), page_attempt(row_count=capture.PAGE_SIZE)])
        == "page_cap_reached"
    )
    assert capture._pagination_observation([page_attempt(outcome="failed")]) == (
        "unknown_after_failed_request"
    )
    assert capture._safe_parser_code("response_json_invalid") == "response_json_invalid"
    assert capture._safe_parser_code("SOURCE detail") == "offline_parser_rejected"
    assert capture._safe_parser_code("x" * 97) == "offline_parser_rejected"


def test_datetime_gate_rejects_naive_and_broken_offsets() -> None:
    class BrokenTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> None:
            del dt
            raise ValueError("synthetic timezone failure")

        def dst(self, dt: datetime | None) -> None:
            del dt
            return None

        def tzname(self, dt: datetime | None) -> str:
            del dt
            return "BROKEN"

    assert capture._is_aware_datetime(None) is False
    assert capture._is_aware_datetime(datetime(2026, 10, 30, 4, 0)) is False
    assert capture._is_aware_datetime(datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)) is True
    assert (
        capture._is_aware_datetime(datetime(2026, 10, 30, 4, 0, tzinfo=BrokenTimezone())) is False
    )


def test_private_evidence_path_rejects_relative_traversal_and_symlink_parents(
    tmp_path: Path,
) -> None:
    invalid_paths = (
        Path("relative-evidence"),
        Path("/tmp/opendata/../evidence"),
        Path("/"),
        "/tmp/evidence-as-a-string",
    )
    for path in invalid_paths:
        with pytest.raises(capture.SSECaptureError) as raised:
            capture._create_private_evidence_directory(cast("Path", path))
        assert raised.value.code == "absolute_evidence_directory_required"

    target = tmp_path / "real-parent"
    target.mkdir(mode=0o700)
    alias = tmp_path / "parent-alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(capture.SSECaptureError) as raised:
        capture._create_private_evidence_directory(alias / "evidence")
    assert raised.value.code == "private_directory_unavailable"
    assert not (target / "evidence").exists()


def test_private_storage_primitives_and_mkdir_failures_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_dir = tmp_path / "evidence"
    for flag_name in ("_O_DIRECTORY", "_O_NOFOLLOW"):
        directory_flag = getattr(capture, flag_name)
        monkeypatch.setattr(capture, flag_name, 0)
        with pytest.raises(capture.SSECaptureError) as unsupported:
            capture._create_private_evidence_directory(evidence_dir)
        assert unsupported.value.code == "secure_local_file_primitives_unavailable"
        assert not evidence_dir.exists()
        monkeypatch.setattr(capture, flag_name, directory_flag)

    original_mkdir = os.mkdir

    def fail_mkdir(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("synthetic permission failure")

    monkeypatch.setattr(capture.os, "mkdir", fail_mkdir)
    with pytest.raises(capture.SSECaptureError) as denied:
        capture._create_private_evidence_directory(evidence_dir)
    monkeypatch.setattr(capture.os, "mkdir", original_mkdir)
    assert denied.value.code == "evidence_directory_create_failed"
    assert not evidence_dir.exists()


def test_private_directory_chain_rejects_relative_input() -> None:
    with pytest.raises(capture.SSECaptureError) as raised:
        capture._open_private_directory_chain(Path("relative-parent"))
    assert raised.value.code == "absolute_private_directory_required"


def test_private_directory_chain_rejects_shared_parent_and_output_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir(mode=0o700)
    original_parent_mode = stat.S_IMODE(shared_parent.stat().st_mode)
    assert original_parent_mode & 0o077 == 0
    os.chmod(shared_parent, original_parent_mode | 0o077)
    try:
        with pytest.raises(capture.SSECaptureError) as parent_rejected:
            capture._open_private_directory_chain(shared_parent)
    finally:
        os.chmod(shared_parent, original_parent_mode)
    assert parent_rejected.value.code == "private_directory_permissions_required"

    evidence_dir = tmp_path / "world-readable-evidence"
    original_mkdir = os.mkdir

    def create_shared_directory(name: str, mode: int = 0o777, *, dir_fd: int) -> None:
        original_mkdir(name, mode, dir_fd=dir_fd)
        os.chmod(name, 0o755, dir_fd=dir_fd, follow_symlinks=False)

    monkeypatch.setattr(capture.os, "mkdir", create_shared_directory)
    try:
        with pytest.raises(capture.SSECaptureError) as evidence_rejected:
            capture._create_private_evidence_directory(evidence_dir)
    finally:
        monkeypatch.setattr(capture.os, "mkdir", original_mkdir)
        if evidence_dir.exists():
            os.chmod(evidence_dir, 0o700)
    assert evidence_rejected.value.code == "evidence_directory_permissions_invalid"


def test_live_environment_gate_allows_development_only_when_testing_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "off")

    capture._require_live_environment()


def test_receipt_validation_rejects_unsafe_names_and_oversize_payloads(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | capture._O_DIRECTORY)
    try:
        with pytest.raises(capture.SSECaptureError) as invalid_name:
            capture._write_json_exclusive(directory_fd, "../receipt.json", {"state": "safe"})
        with pytest.raises(capture.SSECaptureError) as too_large:
            capture._write_json_exclusive(
                directory_fd, "large.json", {"payload": "x" * (256 * 1024)}
            )
    finally:
        os.close(directory_fd)
    assert invalid_name.value.code == "receipt_name_invalid"
    assert too_large.value.code == "receipt_too_large"
    assert tuple(directory.iterdir()) == ()


def test_zero_length_receipt_write_is_rejected_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    directory_fd = os.open(directory, os.O_RDONLY | capture._O_DIRECTORY)
    monkeypatch.setattr(capture.os, "write", lambda _fd, _value: 0)
    try:
        with pytest.raises(capture.SSECaptureError) as raised:
            capture._write_json_exclusive(directory_fd, "receipt.json", {"state": "safe"})
    finally:
        os.close(directory_fd)
    assert raised.value.code == "receipt_persistence_failed"
    assert tuple(directory.iterdir()) == ()


def test_main_refuses_live_capture_without_flag_and_rejects_live_testing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_dir = tmp_path / "cli-evidence"
    assert capture.main(["--evidence-dir", str(evidence_dir)]) == 2
    no_live = json.loads(capsys.readouterr().out)
    assert no_live["stop_code"] == "explicit_live_mode_required"
    assert not evidence_dir.exists()

    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")
    assert capture.main(["--evidence-dir", str(evidence_dir), "--live"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["stop_code"] == "explicit_development_environment_required"
    assert not evidence_dir.exists()


def test_main_exit_status_follows_receipt_outcome_not_only_request_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        capture,
        "run_sse_instrument_capture",
        lambda _path, *, live: {
            "outcome": "partial_or_failed_capture",
            "requests_failed": 0,
            "live": live,
        },
    )
    assert capture.main(["--evidence-dir", str(tmp_path / "partial"), "--live"]) == 1
    assert json.loads(capsys.readouterr().out)["requests_failed"] == 0

    monkeypatch.setattr(
        capture,
        "run_sse_instrument_capture",
        lambda _path, *, live: {
            "outcome": "bounded_unverified_sse_snapshot_captured",
            "requests_failed": 0,
            "live": live,
        },
    )
    assert capture.main(["--evidence-dir", str(tmp_path / "bounded"), "--live"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == (
        "bounded_unverified_sse_snapshot_captured"
    )

    monkeypatch.setattr(
        capture,
        "run_sse_instrument_capture",
        lambda _path, *, live: {
            "outcome": "bounded_unverified_sse_snapshot_captured",
            "requests_failed": 1,
            "live": live,
        },
    )
    assert capture.main(["--evidence-dir", str(tmp_path / "inconsistent"), "--live"]) == 1
    assert json.loads(capsys.readouterr().out)["requests_failed"] == 1


def test_python_module_entrypoint_exits_before_any_live_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence_dir = tmp_path / "module-evidence"
    monkeypatch.setattr(sys, "argv", [str(capture.__file__), "--evidence-dir", str(evidence_dir)])

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(capture.__file__), run_name="__main__")

    assert raised.value.code == 2
    assert json.loads(capsys.readouterr().out)["stop_code"] == "explicit_live_mode_required"
    assert not evidence_dir.exists()
