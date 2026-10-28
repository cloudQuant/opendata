"""Offline tests for the restricted manual AkShare stock-daily pilot.

Every Eastmoney-shaped body here is synthetic; these tests are not source
recordings and cannot establish source availability or historical completeness.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.http_client import BoundedHttpResponse, FailureCategory, HttpFetchError
from opendata.data.request_budget import current_request_scopes, reserve_scoped_attempt
from scripts.ops import akshare_stock_daily_pilot as pilot

_NOW = datetime.now(timezone.utc).replace(microsecond=0)
_VALID_BODY = (
    b'{"rc":0,"data":{"code":"600519","market":1,"klines":['
    b'"2026-10-12,1685,1688,1690,1680,1.23,2076240,0,0,0,0"]}}'
)


class _FakeClient:
    """A no-network seam that still consumes the real scoped attempt budget."""

    def __init__(self, result: Any, *, close_error: Exception | None = None) -> None:
        """Keep one canned response or failure and record exact request arguments."""
        self.result = result
        self.close_error = close_error
        self.calls: list[dict[str, Any]] = []
        self.close_count = 0

    def get_bounded(self, url: str, **kwargs: Any) -> Any:
        """Record one request, reserve its scoped attempt, and return the canned result."""
        self.calls.append({"url": url, **kwargs})
        scopes = current_request_scopes()
        reserve_scoped_attempt(scopes, pilot.SOURCE, pilot.EASTMONEY_KLINE_HOST)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        """Track that the pilot closes its one-shot transport session."""
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def _private_directory(path: Path) -> Path:
    """Create a private directory for a local admission or capture test."""
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _write_authorization(directory: Path, *, nonce: str = "a" * 32) -> Path:
    """Write a synthetic short-lived local authorization document with mode 0600."""
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
        "nonce": nonce,
    }
    path = directory / "authorization.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, json.dumps(payload, sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _rewrite_authorization(path: Path, payload: dict[str, Any]) -> None:
    """Replace the test admission document without changing its private mode."""
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _response(
    body: Any = _VALID_BODY,
    *,
    status_code: Any = 200,
    retrieved_at: Any = _NOW + timedelta(seconds=1),
) -> BoundedHttpResponse:
    """Build a synthetic response; it is not an Eastmoney recording."""
    return BoundedHttpResponse(body, status_code, retrieved_at)


def _prepare_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
) -> tuple[Path, Path]:
    """Install the explicit local environment and isolated paths for one run."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(pilot, "_utcnow", lambda: datetime.now(timezone.utc))
    monkeypatch.setattr(pilot, "_create_pilot_client", lambda: client)
    authorization_dir = _private_directory(tmp_path / "authorization")
    output_parent = _private_directory(tmp_path / "captures")
    return _write_authorization(authorization_dir), output_parent / "capture"


def test_pilot_captures_one_fixed_bounded_sample_with_private_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind exact fixed parameters, one attempt, raw bytes, digest and retrieval time."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    manifest = pilot.run_pilot(authorization, evidence_dir)

    assert len(client.calls) == 1
    assert client.close_count == 1
    call = client.calls[0]
    assert call["url"] == pilot.EASTMONEY_KLINE_URL
    assert call["source"] == "akshare"
    assert call["params"] == {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "secid": "1.600519",
        "beg": "20261012",
        "end": "20261023",
    }
    assert call["max_body_bytes"] == pilot.PILOT_MAX_RESPONSE_BYTES
    assert call["overall_timeout"] == pilot.PILOT_OVERALL_TIMEOUT_SECONDS
    assert call["timeout"] == (3.0, 5.0)
    assert manifest["outcome"] == "bounded_sample_captured"
    assert manifest["attempts_used"] == 1
    assert manifest["row_count"] == 1
    assert manifest["observed_date_min"] == "2026-10-12"
    assert manifest["observed_date_max"] == "2026-10-12"
    assert manifest["retrieved_at_utc"] == pilot._format_utc(_NOW + timedelta(seconds=1))
    assert manifest["request"]["overall_timeout_wall_clock_limit"] == (
        "not_guaranteed; slow streaming reads may exceed the deadline substantially"
    )
    assert manifest["raw_sha256"] == hashlib.sha256(_VALID_BODY).hexdigest()
    assert manifest["completeness_assessment"] == "not_evaluated"
    assert "does not establish completeness" in str(manifest["completeness_note"])
    assert (evidence_dir / "raw-response.bin").read_bytes() == _VALID_BODY
    assert (
        json.loads((evidence_dir / "reservation.json").read_text())["state"]
        == "attempt_reserved_before_transport"
    )
    assert (evidence_dir.stat().st_mode & 0o777) == 0o700
    assert (evidence_dir / "raw-response.bin").stat().st_mode & 0o077 == 0
    assert (evidence_dir / "capture.json").stat().st_mode & 0o077 == 0


def test_empty_klines_are_saved_as_rejected_not_reported_as_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record a valid empty wire payload without mislabeling it a captured sample."""
    empty_body = b'{"rc":0,"data":{"code":"600519","market":1,"klines":[]}}'
    client = _FakeClient(_response(empty_body))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert len(client.calls) == 1
    assert client.close_count == 1
    assert manifest["outcome"] == "response_saved_but_rejected"
    assert manifest["failure_category"] == "empty_sample"
    assert manifest["row_count"] == 0
    assert manifest["response_body_saved"] is True
    assert (evidence_dir / "raw-response.bin").read_bytes() == empty_body


def test_same_authorization_nonce_cannot_be_replayed_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep an exclusive consumed marker after success and block a second capture."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    pilot.run_pilot(authorization, evidence_dir)

    with pytest.raises(pilot.PilotError, match="authorization_nonce_already_consumed"):
        pilot.run_pilot(authorization, evidence_dir.parent / "second")
    assert len(client.calls) == 1
    assert not (evidence_dir.parent / "second").exists()


def test_explicit_nonproduction_environment_is_required_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an unset APP_ENV before consuming authorization or creating output."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    monkeypatch.delenv("APP_ENV")

    with pytest.raises(pilot.PilotError, match="explicit_development_environment_required"):
        pilot.run_pilot(authorization, evidence_dir)

    assert len(client.calls) == 0
    assert authorization.exists()
    assert not evidence_dir.exists()


def test_production_and_testing_modes_are_refused_without_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit production and test process markers fail closed."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(pilot.PilotError, match="explicit_development_environment_required"):
        pilot.run_pilot(authorization, evidence_dir)

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("TESTING", "true")
    with pytest.raises(pilot.PilotError, match="pilot_disabled_in_testing_environment"):
        pilot.run_pilot(authorization, evidence_dir)
    assert client.calls == []


def test_authorization_must_match_exact_fixed_request_before_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a wider authorization before making an evidence directory or request."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    payload = json.loads(authorization.read_text())
    payload["symbol"] = "000001.SZ"
    _rewrite_authorization(authorization, payload)

    with pytest.raises(pilot.PilotError, match="authorization_scope_invalid"):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b'{"schema_version":1,"schema_version":1}',
        b'{"value":NaN}',
        b"[" * 1100 + b"]" * 1100,
    ],
    ids=["malformed", "non-object", "duplicate-key", "non-finite", "too-deep"],
)
def test_invalid_authorization_json_fails_before_consuming_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    """Reject malformed, ambiguous and non-finite local authorization JSON."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    authorization.write_bytes(raw)
    authorization.chmod(0o600)

    with pytest.raises(pilot.PilotError, match="authorization_json_invalid"):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()
    assert list(authorization.parent.glob(".pilot-*.consumed")) == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("extra_key", "authorization_scope_invalid"),
        ("missing_key", "authorization_scope_invalid"),
        ("invalid_nonce", "authorization_nonce_invalid"),
    ],
)
def test_authorization_schema_keys_and_nonce_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str,
) -> None:
    """Reject wider scope, incomplete schema and malformed replay nonce."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    payload = json.loads(authorization.read_text())
    if mutation == "extra_key":
        payload["provider"] = "unexpected"
    elif mutation == "missing_key":
        del payload["max_requests"]
    else:
        payload["nonce"] = "NOT-32-HEX-CHARS"
    _rewrite_authorization(authorization, payload)

    with pytest.raises(pilot.PilotError, match=expected_code):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()


@pytest.mark.parametrize(
    ("timestamp_kind", "expected_code"),
    [
        ("missing_z", "authorization_timestamp_invalid"),
        ("invalid_syntax", "authorization_timestamp_invalid"),
        ("noncanonical", "authorization_timestamp_invalid"),
        ("non_string", "authorization_timestamp_invalid"),
    ],
)
def test_authorization_timestamp_encoding_must_be_canonical_utc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timestamp_kind: str,
    expected_code: str,
) -> None:
    """Refuse UTC timestamps with wrong type, syntax, suffix or precision."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    payload = json.loads(authorization.read_text())
    timestamp = payload["issued_at_utc"]
    if timestamp_kind == "missing_z":
        payload["issued_at_utc"] = timestamp[:-1] + "+00:00"
    elif timestamp_kind == "invalid_syntax":
        payload["issued_at_utc"] = "not-a-dateZ"
    elif timestamp_kind == "noncanonical":
        payload["issued_at_utc"] = timestamp[:-1] + "0Z"
    else:
        payload["issued_at_utc"] = 42
    _rewrite_authorization(authorization, payload)

    with pytest.raises(pilot.PilotError, match=expected_code):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()


@pytest.mark.parametrize("case", ["expired", "future-issued", "too-old"])
def test_authorization_expiry_and_age_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Reject expired, future-issued and longer-than-policy authorization files."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    now = pilot._utcnow()
    payload = json.loads(authorization.read_text())
    if case == "expired":
        payload["issued_at_utc"] = pilot._format_utc(now - timedelta(minutes=2))
        payload["expires_at_utc"] = pilot._format_utc(now - timedelta(seconds=1))
    elif case == "future-issued":
        payload["issued_at_utc"] = pilot._format_utc(now + timedelta(minutes=1))
        payload["expires_at_utc"] = pilot._format_utc(now + timedelta(minutes=2))
    else:
        payload["issued_at_utc"] = pilot._format_utc(now - timedelta(minutes=16))
        payload["expires_at_utc"] = pilot._format_utc(now + timedelta(minutes=1))
    _rewrite_authorization(authorization, payload)

    with pytest.raises(
        pilot.PilotError,
        match="authorization_expired_or_not_yet_valid",
    ):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()


@pytest.mark.parametrize(
    ("content", "mode"),
    [
        (None, 0o644),
        (b"", 0o600),
        (b"x" * 4097, 0o600),
    ],
    ids=["public-mode", "empty", "oversized"],
)
def test_authorization_file_mode_and_size_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes | None,
    mode: int,
) -> None:
    """Reject public, empty and oversized admission files before parsing."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    if content is not None:
        authorization.write_bytes(content)
    authorization.chmod(mode)

    with pytest.raises(pilot.PilotError, match="authorization_file_permissions_invalid"):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []
    assert not evidence_dir.exists()


def test_unexpected_authorization_read_failure_is_sanitized_and_closes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convert a local read failure to a safe code and release its file descriptor."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    opened_fds: list[int] = []

    def fail_read(descriptor: int, _size: int) -> bytes:
        opened_fds.append(descriptor)
        raise OSError("sensitive local diagnostic")

    monkeypatch.setattr(pilot.os, "read", fail_read)
    with pytest.raises(pilot.PilotError, match="authorization_file_invalid") as raised:
        pilot.run_pilot(authorization, evidence_dir)

    assert "sensitive local diagnostic" not in str(raised.value)
    assert len(opened_fds) == 1
    with pytest.raises(OSError):
        os.fstat(opened_fds[0])
    assert client.calls == []
    assert not evidence_dir.exists()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([None], "returned date is not text"),
        (["not-a-date"], "returned date is invalid"),
        (["20261012"], "returned date is not canonical ISO format"),
        (
            ["2026-10-12", "2026-10-12"],
            "returned dates contain duplicate business keys",
        ),
    ],
    ids=["non-text", "unparseable", "noncanonical", "duplicate"],
)
def test_returned_date_validation_rejects_ambiguous_business_keys(
    values: list[object],
    message: str,
) -> None:
    """Enforce text, canonical ISO dates and unique source business keys."""
    if values == ["20261012"]:
        try:
            pilot.date.fromisoformat("20261012")
        except ValueError:
            pytest.skip("compact ISO dates are not accepted by this Python version")
    with pytest.raises(ValueError, match=message):
        pilot._validate_returned_dates(values)


def test_auth_paths_and_required_filesystem_primitives_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require absolute paths and safe open flags before touching local files."""
    with pytest.raises(pilot.PilotError, match="absolute_file_path_required"):
        pilot._open_private_parent(Path("relative/authorization.json"))
    with pytest.raises(pilot.PilotError, match="absolute_evidence_directory_required"):
        pilot._create_evidence_directory(Path("relative/capture"))

    private_parent = _private_directory(tmp_path / "private")
    with pytest.raises(pilot.PilotError, match="test_path_required"):
        pilot._open_private_directory(
            Path("relative"),
            unavailable_code="test_path_required",
            permissions_code="test_permissions_required",
        )

    monkeypatch.setattr(pilot, "_O_NONBLOCK", 0)
    with pytest.raises(pilot.PilotError, match="secure_local_file_primitives_unavailable"):
        pilot._open_private_directory(
            private_parent,
            unavailable_code="test_path_required",
            permissions_code="test_permissions_required",
        )


def test_evidence_directory_creation_failure_is_reported_without_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate mkdir errors to a stable code and leave the directory absent."""
    parent = _private_directory(tmp_path / "evidence-parent")

    def fail_mkdir(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("private path diagnostic")

    monkeypatch.setattr(pilot.os, "mkdir", fail_mkdir)
    with pytest.raises(pilot.PilotError, match="evidence_directory_create_failed"):
        pilot._create_evidence_directory(parent / "capture")

    assert not (parent / "capture").exists()


def test_evidence_directory_permissions_are_rechecked_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an opened evidence directory if its observed mode is not private."""
    parent = _private_directory(tmp_path / "evidence-parent")
    real_fstat = os.fstat
    calls = 0

    def report_insecure_capture_mode(descriptor: int) -> Any:
        nonlocal calls
        calls += 1
        actual = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFDIR | 0o600)
        return actual

    monkeypatch.setattr(pilot.os, "fstat", report_insecure_capture_mode)
    with pytest.raises(pilot.PilotError, match="evidence_directory_permissions_invalid"):
        pilot._create_evidence_directory(parent / "capture")

    assert calls == 2
    assert (parent / "capture").is_dir()


def test_evidence_file_permissions_are_checked_after_exclusive_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close and reject an output descriptor with an unexpected file type."""
    parent = _private_directory(tmp_path / "evidence-parent")
    directory_fd = os.open(parent, os.O_RDONLY | pilot._O_DIRECTORY)
    monkeypatch.setattr(
        pilot.os,
        "fstat",
        lambda _fd: SimpleNamespace(st_uid=os.getuid(), st_mode=stat.S_IFIFO | 0o600),
    )
    try:
        with pytest.raises(pilot.PilotError, match="evidence_file_permissions_invalid"):
            pilot._open_exclusive_file(directory_fd, "raw-response.bin")
    finally:
        os.close(directory_fd)

    assert (parent / "raw-response.bin").exists()


def test_evidence_short_write_is_reported_without_retrying_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a zero-byte write as an evidence failure, not a successful partial file."""
    monkeypatch.setattr(pilot.os, "write", lambda _fd, _content: 0)

    with pytest.raises(OSError, match="short evidence write"):
        pilot._write_all(123, b"bounded-evidence")


def test_bounded_file_reader_rejects_content_past_limit(tmp_path: Path) -> None:
    """Stop after limit plus a sentinel byte without consuming an unbounded file."""
    path = tmp_path / "bounded-input.bin"
    path.write_bytes(b"abc")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(pilot.PilotError, match="authorization_file_too_large"):
            pilot._read_bounded_file(descriptor, limit=2)
    finally:
        os.close(descriptor)


def test_response_outside_window_is_saved_as_rejected_evidence_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve a bounded but invalid response and distinguish it from a valid sample."""
    outside_window = (
        b'{"rc":0,"data":{"code":"600519","market":1,"klines":['
        b'"2026-10-09,1685,1688,1690,1680,1.23,2076240,0,0,0,0"]}}'
    )
    client = _FakeClient(_response(outside_window))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert len(client.calls) == 1
    assert manifest["outcome"] == "response_saved_but_rejected"
    assert manifest["response_body_saved"] is True
    assert manifest["raw_sha256"] == hashlib.sha256(outside_window).hexdigest()
    assert manifest["completeness_assessment"] == "not_evaluated"


@pytest.mark.parametrize(
    ("response", "failure_category"),
    [
        (object(), "bounded_response_type_invalid"),
        (_response(status_code=201), "bounded_response_contract_invalid"),
        (_response(status_code=True), "bounded_response_contract_invalid"),
        (_response(body=bytearray(_VALID_BODY)), "bounded_response_contract_invalid"),
        (
            _response(body=b"x" * (pilot.PILOT_MAX_RESPONSE_BYTES + 1)),
            "bounded_response_contract_invalid",
        ),
    ],
    ids=["wrong-type", "non-200", "bool-status", "non-bytes-body", "oversized-body"],
)
def test_invalid_bounded_response_is_rejected_without_saving_raw_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: Any,
    failure_category: str,
) -> None:
    """Refuse responses that violate the exact bounded-client contract."""
    client = _FakeClient(response)
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert len(client.calls) == 1
    assert client.close_count == 1
    assert manifest["outcome"] == "response_rejected"
    assert manifest["failure_category"] == failure_category
    assert manifest["response_body_saved"] is False
    assert manifest["response_body_note"] == "No response body was saved."
    assert not (evidence_dir / "raw-response.bin").exists()


@pytest.mark.parametrize(
    "retrieved_at",
    [
        None,
        datetime(2026, 10, 28, 12, 0),
        datetime.max.replace(tzinfo=timezone(timedelta(hours=-1))),
    ],
    ids=["missing", "naive", "utc-overflow"],
)
def test_invalid_retrieval_timestamp_keeps_raw_response_but_rejects_sample(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retrieved_at: Any,
) -> None:
    """Persist bounded bytes before rejecting invalid retrieval-time metadata."""
    client = _FakeClient(_response(retrieved_at=retrieved_at))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert client.close_count == 1
    assert manifest["outcome"] == "response_saved_but_rejected"
    assert manifest["failure_category"] == "retrieval_timestamp_invalid"
    assert manifest["retrieved_at_utc"] is None
    assert manifest["raw_sha256"] == hashlib.sha256(_VALID_BODY).hexdigest()
    assert (evidence_dir / "raw-response.bin").read_bytes() == _VALID_BODY


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b'{"rc":0,"data":{"code":"000001","market":1,"klines":[]}}',
    ],
    ids=["malformed-json", "wrong-provider-symbol"],
)
def test_wire_rejection_preserves_bounded_raw_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    """Retain bounded source bytes and record parser or identity rejection."""
    client = _FakeClient(_response(body))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert client.close_count == 1
    assert manifest["outcome"] == "response_saved_but_rejected"
    assert manifest["failure_category"] == "wire_or_window_validation_failed"
    assert manifest["raw_sha256"] == hashlib.sha256(body).hexdigest()
    assert (evidence_dir / "raw-response.bin").read_bytes() == body


def test_http_error_records_status_but_does_not_claim_to_save_unreturned_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reflect the bounded client's HTTP-error contract and retain no invented body."""
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

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert len(client.calls) == 1
    assert client.close_count == 1
    assert manifest["http_status"] == 429
    assert manifest["response_body_saved"] is False
    assert "did not return an error response body" in manifest["response_body_note"]
    assert not (evidence_dir / "raw-response.bin").exists()


def test_failed_request_cannot_be_retried_with_same_authorization_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave the consumed marker after a failed request so the same grant cannot retry."""
    error = HttpFetchError(
        FailureCategory.NETWORK,
        url=pilot.EASTMONEY_KLINE_URL,
        host=pilot.EASTMONEY_KLINE_HOST,
        attempts=1,
        retryable=False,
    )
    client = _FakeClient(error)
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_http_request_failed"):
        pilot.run_pilot(authorization, evidence_dir)
    with pytest.raises(pilot.PilotError, match="authorization_nonce_already_consumed"):
        pilot.run_pilot(authorization, evidence_dir.parent / "retry")

    assert len(client.calls) == 1
    assert (evidence_dir / "capture.json").exists()
    assert not (evidence_dir.parent / "retry").exists()


def test_unexpected_transport_exception_is_sanitized_and_closes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep only a safe failure category when an unexpected transport error occurs."""
    client = _FakeClient(RuntimeError("secret transport diagnostic"))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    with pytest.raises(pilot.PilotError, match="pilot_request_failed") as raised:
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert "secret transport diagnostic" not in str(raised.value)
    assert "secret transport diagnostic" not in json.dumps(manifest)
    assert client.close_count == 1
    assert manifest["outcome"] == "request_failed"
    assert manifest["attempts_used"] == 1
    assert manifest["response_body_saved"] is False
    assert not (evidence_dir / "raw-response.bin").exists()


def test_session_close_failure_does_not_mask_durable_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain a verified bounded response even if best-effort session cleanup fails."""
    client = _FakeClient(_response(), close_error=OSError("cleanup detail"))
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    manifest = pilot.run_pilot(authorization, evidence_dir)

    assert client.close_count == 1
    assert manifest["outcome"] == "bounded_sample_captured"
    assert (evidence_dir / "raw-response.bin").read_bytes() == _VALID_BODY
    assert (evidence_dir / "capture.json").exists()


def test_evidence_manifest_write_failure_stops_before_client_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if the pre-request reservation cannot be durably recorded."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)

    def fail_manifest(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("evidence storage diagnostic")

    monkeypatch.setattr(pilot, "_write_json_exclusive", fail_manifest)
    with pytest.raises(pilot.PilotError, match="pilot_evidence_write_failed") as raised:
        pilot.run_pilot(authorization, evidence_dir)

    assert "evidence storage diagnostic" not in str(raised.value)
    assert client.calls == []
    assert client.close_count == 0
    assert evidence_dir.is_dir()
    assert list(evidence_dir.iterdir()) == []


def test_existing_evidence_path_fails_before_network_and_consumes_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing capture directory is never overwritten or reused."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    evidence_dir.mkdir(mode=0o700)
    evidence_dir.chmod(0o700)

    with pytest.raises(pilot.PilotError, match="evidence_directory_must_be_new"):
        pilot.run_pilot(authorization, evidence_dir)

    assert client.calls == []


def test_fifo_authorization_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open a named pipe nonblocking and reject it before reading or transport."""
    client = _FakeClient(_response())
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(pilot, "_create_pilot_client", lambda: client)
    authorization_dir = _private_directory(tmp_path / "authorization")
    output_parent = _private_directory(tmp_path / "captures")
    authorization = authorization_dir / "authorization.json"
    os.mkfifo(authorization, 0o600)

    with pytest.raises(pilot.PilotError, match="authorization_file_not_regular"):
        pilot.run_pilot(authorization, output_parent / "capture")

    assert client.calls == []
    assert not (output_parent / "capture").exists()


def test_evidence_parent_must_be_private_and_have_no_symlinked_components(
    tmp_path: Path,
) -> None:
    """Refuse public or symlinked parents before creating an evidence directory."""
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o700)
    public_parent.chmod(0o755)
    with pytest.raises(pilot.PilotError, match="evidence_parent_directory_required"):
        pilot._create_evidence_directory(public_parent / "capture")
    assert not (public_parent / "capture").exists()

    private_parent = _private_directory(tmp_path / "private")
    symlink_parent = tmp_path / "private-link"
    symlink_parent.symlink_to(private_parent, target_is_directory=True)
    with pytest.raises(pilot.PilotError, match="evidence_parent_directory_unavailable"):
        pilot._create_evidence_directory(symlink_parent / "capture")
    assert not (private_parent / "capture").exists()


def test_authorization_expiring_after_consumption_is_denied_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recheck expiry at the final pre-scope gate after reservation and setup."""
    client = _FakeClient(_response())
    authorization, evidence_dir = _prepare_run(tmp_path, monkeypatch, client)
    payload = json.loads(authorization.read_text())
    issued_at = datetime.fromisoformat(payload["issued_at_utc"].replace("Z", "+00:00"))
    clock_values = iter((issued_at, issued_at, issued_at, issued_at + timedelta(minutes=6)))
    monkeypatch.setattr(pilot, "_utcnow", lambda: next(clock_values))

    with pytest.raises(pilot.PilotError, match="authorization_expired_before_transport"):
        pilot.run_pilot(authorization, evidence_dir)

    manifest = json.loads((evidence_dir / "capture.json").read_text())
    assert client.calls == []
    assert client.close_count == 1
    assert manifest["outcome"] == "authorization_expired_before_transport"
    assert manifest["attempts_used"] == 0


def test_pilot_grant_expiry_never_outlives_local_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clamp the isolated ALLOWED grant to the shorter local admission expiry."""
    authorization = pilot._Authorization(
        nonce="b" * 32,
        digest="c" * 64,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(seconds=30),
    )
    monkeypatch.setattr(pilot, "_utcnow", lambda: _NOW)

    budget = pilot._new_pilot_budget(authorization)

    assert budget.grants[0].expires_at == authorization.expires_at


def test_client_isolated_from_environment_and_configured_for_one_attempt() -> None:
    """Pin the no-proxy Requests session and the single-attempt GET configuration."""
    transport = pilot._create_pilot_client()
    try:
        client = transport.client
        session = transport.session
        assert client._config.max_attempts == 1
        assert client._config.rate_burst == 1
        assert session.trust_env is False
        assert session.verify is True
        assert session.auth is None
        assert session.proxies == {}
        assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    finally:
        transport.close()


def test_cli_prints_only_safe_capture_metadata_and_safe_failure_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose bounded identifiers on success and only a fixed code on failure."""
    metadata = {
        "outcome": "bounded_sample_captured",
        "request_sha256": "a" * 64,
        "raw_sha256": "b" * 64,
        "row_count": 1,
        "retrieved_at_utc": "2026-10-28T12:00:00.000000Z",
    }
    monkeypatch.setattr(pilot, "run_pilot", lambda *_args: metadata)
    arguments = [
        "--authorization-file",
        "/private/authorization.json",
        "--evidence-dir",
        "/private/capture",
    ]

    assert pilot.main(arguments) == 0
    success_output = json.loads(capsys.readouterr().out)
    assert success_output == metadata
    assert "raw-response" not in json.dumps(success_output)

    def fail_run(*_args: Any) -> dict[str, object]:
        raise pilot.PilotError("fixed_safe_code")

    monkeypatch.setattr(pilot, "run_pilot", fail_run)
    assert pilot.main(arguments) == 1
    failure_output = json.loads(capsys.readouterr().out)
    assert failure_output == {"outcome": "failed", "code": "fixed_safe_code"}


def test_module_entrypoint_returns_safe_environment_failure_without_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise the executable entrypoint with the production gate closed."""
    authorization = tmp_path / "missing-authorization.json"
    evidence_dir = tmp_path / "capture"
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(pilot.__file__),
            "--authorization-file",
            str(authorization),
            "--evidence-dir",
            str(evidence_dir),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(pilot.__file__), run_name="__main__")

    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "failed",
        "code": "explicit_development_environment_required",
    }
    assert not evidence_dir.exists()
