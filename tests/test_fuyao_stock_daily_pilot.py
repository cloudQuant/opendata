"""Offline behavior tests for the one-shot Fuyao stock-daily pilot."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import stat
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast
from urllib.parse import quote_from_bytes
from uuid import uuid4

import httpx
import pytest

from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.endpoints import PRICES_ENDPOINT, shanghai_midnight_millis
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.envelope import parse_envelope
from opendata.data.providers.ths.transport.errors import error_for_transport
from opendata.data.providers.ths.transport.http_client import (
    FuyaoHttpClient,
    FuyaoHttpResponse,
)
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudgetError,
    RequestRedirectError,
    current_request_scopes,
    reserve_scoped_attempt,
)
from scripts.ops import fuyao_stock_daily_pilot as pilot

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_SYNTHETIC_KEY = "offline-only-fuyao-key"


def _bypass_runtime_guard_for_offline_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass only the process guard in tests with synthetic clients/transports.

    The runner's TESTING and APP_ENV values remain untouched. A dedicated test
    exercises the real guard, including development with TESTING=true.
    """
    monkeypatch.setattr(pilot, "_require_local_development", lambda: None)


class _FakeClient:
    """Offline client double that reserves the real one-attempt request budget."""

    def __init__(self, body: bytes, *, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.failure: Exception | None = None

    def get(self, endpoint: str, *, params: Mapping[str, object]) -> FuyaoHttpResponse:
        """Record the call and exercise the caller's exact source grant."""
        self.calls.append((endpoint, dict(params)))
        scopes = current_request_scopes()
        if self.failure is not None:
            if isinstance(self.failure, RequestRedirectError) or not isinstance(
                self.failure, RequestBudgetError
            ):
                reserve_scoped_attempt(scopes, SOURCE, "fuyao.aicubes.cn")
            raise self.failure
        reserve_scoped_attempt(scopes, SOURCE, "fuyao.aicubes.cn")
        try:
            payload = json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The fake must return a transport response so the pilot, rather
            # than the test double, classifies malformed source bytes.
            payload = json.loads(_response_body())
        envelope = parse_envelope(payload)
        return FuyaoHttpResponse(
            status_code=self.status_code,
            envelope=envelope,
            elapsed_seconds=0.01,
            raw_content=self.body,
        )

    def close(self) -> None:
        """Record deterministic cleanup for the offline test."""
        self.closed = True


def _private_root(tmp_path: Path) -> Path:
    """Create a private child without changing any ancestor permissions."""
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    return root


def _authorization_document(**updates: object) -> dict[str, object]:
    """Build one fixed-scope synthetic operator admission document."""
    now = datetime.now(timezone.utc)
    document: dict[str, object] = {
        "schema_version": 1,
        "purpose": pilot.PILOT_PURPOSE,
        "symbol": pilot.PILOT_SYMBOL,
        "start_date": pilot.PILOT_START_DATE.isoformat(),
        "end_date": pilot.PILOT_END_DATE.isoformat(),
        "adjustment_basis": pilot.PILOT_ADJUSTMENT_BASIS,
        "max_requests": 1,
        "max_response_bytes": pilot.PILOT_MAX_RESPONSE_BYTES,
        "issued_at_utc": pilot._format_utc(now - timedelta(seconds=1)),
        "expires_at_utc": pilot._format_utc(now + timedelta(minutes=10)),
        "nonce": uuid4().hex,
    }
    document.update(updates)
    return document


def _write_authorization(root: Path, document: Mapping[str, object] | None = None) -> Path:
    """Create a synthetic mode-0600 authorization file in a private directory."""
    path = root / "authorization.json"
    content = json.dumps(
        dict(document or _authorization_document()),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        pilot._write_all(descriptor, content)
    finally:
        os.close(descriptor)
    return path


def _row(day: str, **updates: object) -> dict[str, object]:
    """Return one synthetic daily bar using the provider's documented keys."""
    from datetime import date

    item: dict[str, object] = {
        "date_ms": shanghai_midnight_millis(date.fromisoformat(day)),
        "open_price": 100.0,
        "high_price": 103.0,
        "low_price": 99.0,
        "close_price": 102.0,
        "volume": 10000.0,
        "turnover": 1020000.0,
    }
    item.update(updates)
    return item


def _response_body(
    days: tuple[str, ...] = (
        "2026-10-19",
        "2026-10-20",
        "2026-10-21",
        "2026-10-22",
        "2026-10-23",
    ),
    *,
    symbol: str = pilot.PILOT_SYMBOL,
    rows: list[dict[str, object]] | None = None,
    message: str = "success",
) -> bytes:
    """Encode a synthetic Fuyao envelope without external credentials or I/O."""
    selected_rows = rows if rows is not None else [_row(day) for day in days]
    payload = {
        "code": 0,
        "message": message,
        "request_id": "synthetic-request-id",
        "data": {
            "timestamp": shanghai_midnight_millis(pilot.PILOT_END_DATE),
            "thscode": symbol,
            "item": selected_rows,
        },
    }
    return json.dumps(payload, separators=(",", ":"), allow_nan=True).encode()


def _streamed_http_response(status_code: int, body: bytes) -> httpx.Response:
    """Build the unconsumed streaming response required by the bounded client."""
    return httpx.Response(
        status_code,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        stream=httpx.ByteStream(body),
    )


class _ResponseClient:
    """Return a prepared response while optionally consuming the real request grant."""

    def __init__(self, response: object, *, reserve: bool = True) -> None:
        self.response = response
        self.reserve = reserve
        self.closed = False

    def get(self, endpoint: str, *, params: Mapping[str, object]) -> FuyaoHttpResponse:
        del endpoint, params
        if self.reserve:
            reserve_scoped_attempt(current_request_scopes(), SOURCE, "fuyao.aicubes.cn")
        return cast("FuyaoHttpResponse", self.response)

    def close(self) -> None:
        self.closed = True


def _run(
    root: Path,
    client: _FakeClient,
    *,
    document: Mapping[str, object] | None = None,
    credentials: FuyaoCredentials | None = None,
    evidence_name: str = "capture",
) -> tuple[dict[str, object], Path]:
    """Run with an explicit synthetic client and credentials, never project config."""
    authorization_file = _write_authorization(root, document)
    evidence_dir = root / evidence_name
    metadata = pilot.run_pilot(
        authorization_file,
        evidence_dir,
        credentials=credentials or FuyaoCredentials(_SYNTHETIC_KEY),
        client_factory=lambda _credentials: client,
    )
    return metadata, evidence_dir


def test_success_records_only_private_validated_sample_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid sample writes only private whitelisted fields and a summary."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body()
    client = _FakeClient(body)

    metadata, evidence_dir = _run(root, client)

    expected_params = pilot.build_prices_request(
        symbol="600519.SH",
        start=pilot.PILOT_START_DATE,
        end=pilot.PILOT_REQUEST_END_EXCLUSIVE,
        adjust="unadjusted",
    )
    assert client.calls == [(PRICES_ENDPOINT, expected_params)]
    assert client.closed
    assert metadata["capture_state"] == "sample_captured"
    assert metadata["row_count"] == 5
    assert metadata["observed_date_min"] == "2026-10-19"
    assert metadata["observed_date_max"] == "2026-10-23"
    assert metadata["completeness_assessment"] == "not_established"
    assert metadata["response_body_saved"] is False
    assert metadata["validated_observations_persisted"] is True
    assert metadata["observations_artifact_state"] == "complete_published_sample"
    observations_bytes = (evidence_dir / "observations.json").read_bytes()
    observations = json.loads(observations_bytes)
    assert stat.S_IMODE((evidence_dir / "observations.json").stat().st_mode) == 0o600
    assert len(observations["rows"]) == 5
    assert observations["rows"][0]["trade_date"] == "2026-10-19"
    assert observations["rows"][0]["source_fields"] == _row("2026-10-19")
    assert observations["field_units"]["volume"].startswith("provider contract label: shares")
    assert stat.S_IMODE(evidence_dir.stat().st_mode) == 0o700
    capture = (evidence_dir / "capture.json").read_bytes()
    assert _SYNTHETIC_KEY.encode() not in capture
    assert b"synthetic-request-id" not in capture
    assert b"fuyao.aicubes.cn/api" not in capture
    assert _SYNTHETIC_KEY.encode() not in observations_bytes
    assert not (evidence_dir / "raw-response.bin").exists()


def test_request_scope_is_exactly_one_attempt_for_fixed_host_model_and_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fake client is allowed only the exact ths/stock_daily/query request."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())

    _, evidence_dir = _run(root, client)

    assert len(client.calls) == 1
    reservation = json.loads((evidence_dir / "reservation.json").read_bytes())
    assert reservation["source"] == "ths"
    assert reservation["endpoint_path"] == PRICES_ENDPOINT
    assert reservation["reservation_state"] == "authorized_scope_recorded_before_transport"
    assert "scope_recorded_at_utc" in reservation
    assert "reserved_at_utc" not in reservation
    assert reservation["local_budget_attempts_reserved"] == 0
    assert reservation["physical_send_count"] is None
    assert reservation["physical_send_count_semantics"] == ("not_observed_by_local_request_budget")
    assert reservation["request"]["max_requests"] == 1
    assert reservation["request"]["max_response_bytes"] == 65536
    assert reservation["request"]["response_limit_semantics"] == (
        "application_cumulative_entity_bytes_not_wire_or_rss"
    )


def test_real_bounded_client_authorizes_and_sends_exactly_one_mocked_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The actual HTTP client uses the fixed grant and one MockTransport send."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body()
    sent: list[dict[str, object]] = []
    clients: list[FuyaoHttpClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(
            {
                "method": request.method,
                "scheme": request.url.scheme,
                "host": request.url.host,
                "path": request.url.path,
                "query": dict(request.url.params),
            }
        )
        return _streamed_http_response(200, body)

    transport = httpx.MockTransport(handler)

    def factory(credentials: FuyaoCredentials) -> FuyaoHttpClient:
        client = FuyaoHttpClient(
            credentials=credentials,
            mock_transport=transport,
            timeout_seconds=pilot.PILOT_TIMEOUT_SECONDS,
            max_attempts=pilot.PILOT_MAX_REQUESTS,
            max_response_bytes=pilot.PILOT_MAX_RESPONSE_BYTES,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
            raw_response_cache=pilot._no_response_cache(),
        )
        clients.append(client)
        return client

    auth = _write_authorization(root)
    metadata = pilot.run_pilot(
        auth,
        root / "capture",
        credentials=FuyaoCredentials(_SYNTHETIC_KEY),
        client_factory=factory,
    )

    expected_query = {key: str(value) for key, value in pilot._REQUEST_PARAMS.items()}
    assert sent == [
        {
            "method": "GET",
            "scheme": "https",
            "host": "fuyao.aicubes.cn",
            "path": PRICES_ENDPOINT,
            "query": expected_query,
        }
    ]
    assert len(clients) == 1 and clients[0]._client.is_closed
    assert metadata["capture_state"] == "sample_captured"
    assert metadata["local_budget_attempts_reserved"] == 1
    assert metadata["physical_send_count"] is None
    assert metadata["physical_send_count_semantics"] == ("not_observed_by_local_request_budget")


def test_real_client_http_error_body_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real 503 response with the key in its body yields only a safe summary."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body(message=_SYNTHETIC_KEY)
    sends: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        sends.append(1)
        return _streamed_http_response(503, body)

    transport = httpx.MockTransport(handler)

    def factory(credentials: FuyaoCredentials) -> FuyaoHttpClient:
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=transport,
            timeout_seconds=pilot.PILOT_TIMEOUT_SECONDS,
            max_attempts=pilot.PILOT_MAX_REQUESTS,
            max_response_bytes=pilot.PILOT_MAX_RESPONSE_BYTES,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
            raw_response_cache=pilot._no_response_cache(),
        )

    with pytest.raises(pilot.PilotError, match="pilot_source_request_failed"):
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert sends == [1]
    assert capture["capture_state"] == "http_error_response_received"
    assert capture["http_status"] == 503
    assert capture["source_response_received"] is True
    assert capture["source_response_persisted"] is False
    assert capture["local_budget_attempts_reserved"] == 1
    assert capture["physical_send_count"] is None
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert not (evidence / "raw-response.bin").exists()
    assert not (evidence / "observations.json").exists()


def test_real_bounded_client_rejects_malformed_json_without_saving_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed source JSON is classified without retaining source bytes or text."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = b'{"code":0,"data":'
    sends: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        sends.append(1)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(body),
        )

    transport = httpx.MockTransport(handler)

    def factory(credentials: FuyaoCredentials) -> FuyaoHttpClient:
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=transport,
            timeout_seconds=pilot.PILOT_TIMEOUT_SECONDS,
            max_attempts=pilot.PILOT_MAX_REQUESTS,
            max_response_bytes=pilot.PILOT_MAX_RESPONSE_BYTES,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
            raw_response_cache=pilot._no_response_cache(),
        )

    with pytest.raises(pilot.PilotError, match="pilot_source_request_failed"):
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert sends == [1]
    assert capture["capture_state"] == "response_rejected"
    assert capture["failure_category"] == "envelope_invalid"
    assert capture["source_response_received"] is True
    assert capture["local_budget_attempts_reserved"] == 1
    assert body not in capture_bytes
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert not (evidence / "raw-response.bin").exists()
    assert not (evidence / "observations.json").exists()


def test_real_bounded_client_stops_when_stream_exceeds_application_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A streamed body above the configured cumulative entity cap is rejected."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = b"x" * (pilot.PILOT_MAX_RESPONSE_BYTES + 1)
    sends: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        sends.append(1)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=httpx.ByteStream(body),
        )

    transport = httpx.MockTransport(handler)

    def factory(credentials: FuyaoCredentials) -> FuyaoHttpClient:
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=transport,
            timeout_seconds=pilot.PILOT_TIMEOUT_SECONDS,
            max_attempts=pilot.PILOT_MAX_REQUESTS,
            max_response_bytes=pilot.PILOT_MAX_RESPONSE_BYTES,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1.0, burst=1),
            raw_response_cache=pilot._no_response_cache(),
        )

    with pytest.raises(pilot.PilotError, match="pilot_source_request_failed"):
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert sends == [1]
    assert capture["capture_state"] == "response_rejected"
    assert capture["failure_category"] == "response_too_large"
    assert capture["local_budget_attempts_reserved"] == 1
    assert capture["source_response_persisted"] is False
    assert body not in capture_bytes
    assert not (evidence / "raw-response.bin").exists()
    assert not (evidence / "observations.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "000001.SZ"),
        ("start_date", "2026-10-18"),
        ("end_date", "2026-10-24"),
        ("adjustment_basis", "qfq"),
        ("max_requests", 2),
        ("max_response_bytes", 65537),
        ("purpose", "other"),
    ],
)
def test_authorization_rejects_any_scope_expansion_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """An altered local authorization cannot expand symbol, dates, or limits."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    document = _authorization_document(**{field: value})
    authorization_file = _write_authorization(root, document)
    created: list[bool] = []

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        created.append(True)
        return _FakeClient(_response_body())

    with pytest.raises(pilot.PilotError, match="authorization_scope_invalid"):
        pilot.run_pilot(
            authorization_file,
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    assert created == []
    assert not (root / "capture").exists()


def test_same_nonce_cannot_be_replayed_even_with_a_new_evidence_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exclusive nonce-marker creation blocks a second invocation in its directory."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    document = _authorization_document()
    authorization_file = _write_authorization(root, document)
    calls = 0

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        nonlocal calls
        calls += 1
        return _FakeClient(_response_body())

    pilot.run_pilot(
        authorization_file,
        root / "capture-one",
        credentials=FuyaoCredentials(_SYNTHETIC_KEY),
        client_factory=factory,
    )
    with pytest.raises(pilot.PilotError, match="authorization_nonce_already_consumed"):
        pilot.run_pilot(
            authorization_file,
            root / "capture-two",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    assert calls == 1
    assert not (root / "capture-two").exists()
    marker_files = list(root.glob(".fuyao-pilot-*.consumed"))
    assert len(marker_files) == 1
    nonce = document["nonce"]
    assert isinstance(nonce, str)
    assert nonce.encode() not in marker_files[0].read_bytes()


@pytest.mark.parametrize(
    ("environment", "expected_code"),
    [
        ("production", "explicit_development_environment_required"),
        ("development", "pilot_disabled_in_testing_environment"),
    ],
)
def test_runtime_guard_keeps_production_and_testing_calls_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: str,
    expected_code: str,
) -> None:
    """The real guard rejects production and TESTING=true before nonce consumption."""
    assert os.environ.get("TESTING", "").strip().casefold() in {"1", "true", "yes", "on"}
    monkeypatch.setenv("APP_ENV", environment)
    root = _private_root(tmp_path)
    authorization = _write_authorization(root)
    created: list[bool] = []

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        created.append(True)
        return _FakeClient(_response_body())

    with pytest.raises(pilot.PilotError, match=expected_code):
        pilot.run_pilot(
            authorization,
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    assert created == []
    assert not (root / "capture").exists()
    assert not list(root.glob(".fuyao-pilot-*.consumed"))


def test_altered_fuyao_origin_is_rejected_before_client_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Credential configuration cannot redirect the fixed-origin request."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    authorization_file = _write_authorization(root)
    created: list[bool] = []

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        created.append(True)
        return _FakeClient(_response_body())

    with pytest.raises(pilot.PilotError, match="provider_origin_not_allowed"):
        pilot.run_pilot(
            authorization_file,
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY, base_url="https://example.invalid"),
            client_factory=factory,
        )

    assert created == []
    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == "failed_before_transport"
    assert capture["failure_category"] == "provider_origin_not_allowed"
    assert capture["local_budget_attempts_reserved"] == 0
    assert capture["physical_send_count"] is None
    assert capture["http_status"] is None
    assert capture["source_response_received"] is False
    assert capture["response_body_saved"] is False
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert b"example.invalid" not in capture_bytes
    assert not (evidence / "raw-response.bin").exists()
    assert not (evidence / "observations.json").exists()
    assert len(list(root.glob(".fuyao-pilot-*.consumed"))) == 1


def test_client_factory_failure_writes_a_sanitized_pretransport_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Factory errors are summarized without logging their exception text."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        raise RuntimeError(f"{_SYNTHETIC_KEY} https://fuyao.aicubes.cn/?api_key=secret")

    with pytest.raises(pilot.PilotError, match="pilot_configuration_rejected") as caught:
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == "failed_before_transport"
    assert capture["failure_category"] == "local_configuration_rejected"
    assert capture["local_budget_attempts_reserved"] == 0
    assert capture["source_response_received"] is False
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert b"example.invalid" not in capture_bytes
    assert "secret" not in str(caught.value)
    assert len(list(root.glob(".fuyao-pilot-*.consumed"))) == 1


@pytest.mark.parametrize(
    ("failure", "expected_error", "expected_state", "expected_category", "reserved"),
    [
        (
            RequestRedirectError("redirect to synthetic.invalid"),
            "pilot_redirect_rejected",
            "response_rejected",
            "redirect_rejected",
            1,
        ),
        (
            RuntimeError(f"transport detail {_SYNTHETIC_KEY}"),
            "pilot_request_failed",
            "transport_outcome_unknown",
            "transport_or_response_failure",
            1,
        ),
    ],
)
def test_transport_failures_are_summarized_without_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
    expected_error: str,
    expected_state: str,
    expected_category: str,
    reserved: int,
) -> None:
    """Redirect and uncertain transport failures retain only safe classifications."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())
    client.failure = failure

    with pytest.raises(pilot.PilotError, match=expected_error) as caught:
        _run(root, client)

    capture_bytes = (root / "capture" / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == expected_state
    assert capture["failure_category"] == expected_category
    assert capture["local_budget_attempts_reserved"] == reserved
    assert capture["physical_send_count"] is None
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert "synthetic.invalid" not in capture_bytes.decode()
    assert str(failure) not in str(caught.value)
    assert client.closed


def test_returned_response_must_match_the_transport_response_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A client double cannot smuggle an untyped response into evidence writing."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _ResponseClient({"body": _SYNTHETIC_KEY})

    with pytest.raises(pilot.PilotError, match="pilot_response_contract_invalid"):
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=lambda _credentials: client,
        )

    capture_bytes = (root / "capture" / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["failure_category"] == "response_contract_invalid"
    assert capture["local_budget_attempts_reserved"] == 1
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert client.closed


def test_response_without_local_budget_reservation_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A response object alone is not proof that the request budget was reserved."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body()
    payload = json.loads(body)
    response = FuyaoHttpResponse(
        status_code=200,
        envelope=parse_envelope(payload),
        elapsed_seconds=0.01,
        raw_content=body,
    )
    client = _ResponseClient(response, reserve=False)

    with pytest.raises(pilot.PilotError, match="pilot_request_budget_contract_invalid"):
        pilot.run_pilot(
            _write_authorization(root),
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=lambda _credentials: client,
        )

    capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert capture["capture_state"] == "failed_before_transport"
    assert capture["failure_category"] == "request_budget_contract_invalid"
    assert capture["local_budget_attempts_reserved"] == 0
    assert capture["source_response_received"] is False
    assert client.closed


def test_http_201_is_rejected_without_persisting_source_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only exact HTTP 200 can become a captured sample."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body()
    client = _FakeClient(body, status_code=201)

    with pytest.raises(pilot.PilotError, match="pilot_http_status_rejected"):
        _run(root, client)

    capture_path = root / "capture" / "capture.json"
    capture = json.loads(capture_path.read_bytes())
    assert capture["capture_state"] == "response_rejected"
    assert capture["http_status"] == 201
    assert capture["response_body_saved"] is False
    assert capture["source_response_persisted"] is False
    assert not (root / "capture" / "raw-response.bin").exists()
    assert not (root / "capture" / "observations.json").exists()
    assert client.closed


def test_synthetic_response_above_application_cap_is_rejected_without_saving_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pilot independently checks the client contract before writing bytes."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(b"x" * (pilot.PILOT_MAX_RESPONSE_BYTES + 1))

    with pytest.raises(pilot.PilotError, match="pilot_response_body_rejected"):
        _run(root, client)

    capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert capture["capture_state"] == "response_rejected"
    assert capture["failure_category"] == "response_body_contract_invalid"
    assert not (root / "capture" / "raw-response.bin").exists()


@pytest.mark.parametrize("status_code", [200, 503])
def test_reflected_api_key_is_never_persisted_for_success_or_http_error_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status_code: int,
) -> None:
    """A response echoing the current key is suppressed before any body write."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body(message=_SYNTHETIC_KEY)
    client = _FakeClient(body, status_code=status_code)

    with pytest.raises(pilot.PilotError, match="pilot_credential_reflection_detected"):
        _run(root, client)

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == "response_rejected"
    assert capture["http_status"] == status_code
    assert capture["failure_category"] == "credential_reflection_detected"
    assert capture["response_body_saved"] is False
    assert capture["response_body_suppressed"] == "credential_reflection_detected"
    assert "raw_sha256" not in capture
    assert not (evidence / "raw-response.bin").exists()
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert client.closed


@pytest.mark.parametrize(
    "representation",
    [
        "percent",
        "percent_lower",
        "form",
        "base64",
        "base64_unpadded",
        "base64url",
        "base64url_unpadded",
        "hex",
        "hex_upper",
        "html",
    ],
)
def test_common_encoded_api_key_reflection_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    representation: str,
) -> None:
    """URL, base64, and hexadecimal forms of the credential are also detected."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    key = "offline /fuyao+key'"
    key_bytes = key.encode()
    percent_encoded = quote_from_bytes(key_bytes, safe="")
    standard_b64 = base64.b64encode(key_bytes).decode()
    urlsafe_b64 = base64.urlsafe_b64encode(key_bytes).decode()
    encoded = {
        "percent": percent_encoded,
        "percent_lower": re.sub(
            r"%[0-9A-F]{2}", lambda match: match.group().lower(), percent_encoded
        ),
        "form": percent_encoded.replace("%20", "+"),
        "base64": standard_b64,
        "base64_unpadded": standard_b64.rstrip("="),
        "base64url": urlsafe_b64,
        "base64url_unpadded": urlsafe_b64.rstrip("="),
        "hex": key_bytes.hex(),
        "hex_upper": key_bytes.hex().upper(),
        "html": "offline /fuyao+key&#x27;",
    }[representation]
    body = _response_body(message=encoded)
    client = _FakeClient(body)

    with pytest.raises(pilot.PilotError, match="pilot_credential_reflection_detected"):
        _run(root, client, credentials=FuyaoCredentials(key))

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["failure_category"] == "credential_reflection_detected"
    assert encoded.encode() not in capture_bytes
    assert not (evidence / "raw-response.bin").exists()


def test_json_escaped_api_key_reflection_is_suppressed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A quote/backslash in a key is detected in its JSON-escaped body form."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    key = 'offline "fuyao\\key'
    body = _response_body(message=key)

    with pytest.raises(pilot.PilotError, match="pilot_credential_reflection_detected"):
        _run(root, _FakeClient(body), credentials=FuyaoCredentials(key))

    capture_bytes = (root / "capture" / "capture.json").read_bytes()
    assert key.encode() not in capture_bytes
    assert not (root / "capture" / "raw-response.bin").exists()


@pytest.mark.parametrize(
    ("body", "expected_category"),
    [
        (_response_body(symbol="000001.SZ"), "response_symbol_mismatch"),
        (
            _response_body(rows=[_row("2026-10-19"), _row("2026-10-19")]),
            "response_duplicate_business_date",
        ),
        (_response_body(days=("2026-10-18",)), "response_date_outside_window"),
        (_response_body(days=()), "empty_sample"),
        (
            _response_body(
                rows=[
                    _row(
                        "2026-10-19",
                        open_price=0.0,
                        high_price=0.0,
                        low_price=0.0,
                        close_price=0.0,
                        volume=0.0,
                        turnover=0.0,
                    )
                ]
            ),
            "response_price_not_positive",
        ),
    ],
)
def test_invalid_sample_is_rejected_without_persisting_source_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
    expected_category: str,
) -> None:
    """Wire, identity, key, window, price, and empty failures stay unpersisted."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(body)

    with pytest.raises(pilot.PilotError):
        _run(root, client)

    evidence = root / "capture"
    capture = json.loads((evidence / "capture.json").read_bytes())
    assert capture["capture_state"] == "response_rejected"
    assert capture["completeness_assessment"] == "not_established"
    assert capture["failure_category"] == expected_category
    assert capture["response_body_saved"] is False
    assert capture["source_response_persisted"] is False
    assert not (evidence / "raw-response.bin").exists()
    assert not (evidence / "observations.json").exists()


def test_zero_volume_and_turnover_are_allowed_with_positive_prices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A no-trade observation remains valid when all four market prices are positive."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    rows = [_row("2026-10-19", volume=0.0, turnover=0.0)]

    metadata, evidence = _run(root, _FakeClient(_response_body(rows=rows)))

    assert metadata["capture_state"] == "sample_captured"
    observation = json.loads((evidence / "observations.json").read_bytes())
    assert observation["rows"][0]["source_fields"]["volume"] == 0.0
    assert observation["rows"][0]["source_fields"]["turnover"] == 0.0


def test_duplicate_json_keys_and_nonfinite_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Strict response parsing rejects ambiguous JSON object keys and NaN tokens."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    duplicate = b'{"code":0,"code":0,"data":{"thscode":"600519.SH","item":[]}}'
    with pytest.raises(pilot.PilotError, match="response_json_invalid"):
        _run(root, _FakeClient(duplicate))
    first_capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert first_capture["failure_category"] == "response_json_invalid"

    other_root = tmp_path / "nonfinite"
    other_root.mkdir(mode=0o700)
    nonfinite = _response_body(rows=[_row("2026-10-19", volume=float("nan"))])
    with pytest.raises(pilot.PilotError, match="response_json_invalid"):
        _run(other_root, _FakeClient(nonfinite))
    second_capture = json.loads((other_root / "capture" / "capture.json").read_bytes())
    assert second_capture["failure_category"] == "response_json_invalid"


def test_response_parse_exception_is_sanitized_and_never_persists_source_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unexpected parser exceptions become a fixed response failure category."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)

    def broken_normalizer(*_args: object, **_kwargs: object) -> object:
        raise ValueError(f"provider payload {_SYNTHETIC_KEY} must not be logged")

    monkeypatch.setattr(pilot, "normalize_bars", broken_normalizer)
    body = _response_body()
    client = _FakeClient(body)

    with pytest.raises(pilot.PilotError, match="pilot_response_rejected") as caught:
        _run(root, client)

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == "response_rejected"
    assert capture["failure_category"] == "wire_or_window_validation_failed"
    assert capture["source_response_received"] is True
    assert capture["source_response_persisted"] is False
    assert body not in capture_bytes
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert "provider payload" not in str(caught.value)
    assert not (evidence / "observations.json").exists()
    assert client.closed


def test_evidence_write_failure_falls_back_to_a_sanitized_failure_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A validated response whose local write fails is reported as not persisted."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    body = _response_body()
    client = _FakeClient(body)
    original_write_all = pilot._write_all

    def fail_sample_write(descriptor: int, content: bytes) -> None:
        if b'"rows":[' in content:
            raise OSError(f"disk failure {_SYNTHETIC_KEY} response=/secret")
        original_write_all(descriptor, content)

    monkeypatch.setattr(pilot, "_write_all", fail_sample_write)
    with pytest.raises(pilot.PilotError, match="pilot_evidence_persistence_failed") as caught:
        _run(root, client)

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["failure_category"] == "local_evidence_persistence_failure"
    assert capture["outcome"] == "evidence_persistence_failed"
    assert capture["capture_state"] == "response_observed_not_persisted"
    assert capture["local_budget_attempts_reserved"] == 1
    assert capture["http_status"] == 200
    assert capture["source_response_received"] is True
    assert "not an upstream processing receipt" in capture["source_response_received_semantics"]
    assert capture["source_response_persisted"] is False
    assert (
        capture["source_response_trust"]
        == "whitelisted_fields_validated_but_observation_write_failed"
    )
    assert capture["response_body_saved"] is False
    assert capture["validated_observations_persisted"] is False
    assert capture["observations_artifact_state"] == "write_failed_may_be_partial"
    assert capture["row_count"] == 5
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert b"/secret" not in capture_bytes
    assert "disk failure" not in str(caught.value)
    assert body not in capture_bytes
    assert not (evidence / "observations.json").exists()
    assert not (evidence / "raw-response.bin").exists()
    assert client.closed


def test_capture_manifest_failure_retains_unpublished_validated_observations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep the complete selected sample available when its success manifest fails."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())
    original_writer = pilot._write_json_exclusive

    def replace_before_capture_manifest(
        directory_fd: int,
        name: str,
        value: Mapping[str, object],
    ) -> None:
        if name == "capture.json" and value.get("outcome") == "bounded_sample_captured":
            raise OSError("synthetic capture manifest write failure")
        original_writer(directory_fd, name, value)

    monkeypatch.setattr(pilot, "_write_json_exclusive", replace_before_capture_manifest)
    with pytest.raises(pilot.PilotError, match="pilot_evidence_persistence_failed"):
        _run(root, client)

    evidence = root / "capture"
    capture_bytes = (evidence / "capture.json").read_bytes()
    capture = json.loads(capture_bytes)
    assert capture["capture_state"] == "validated_sample_unmanifested"
    assert capture["outcome"] == "evidence_persistence_failed"
    assert capture["failure_category"] == "local_evidence_persistence_failure"
    assert capture["source_response_received"] is True
    assert "not an upstream processing receipt" in capture["source_response_received_semantics"]
    assert capture["source_response_persisted"] is False
    assert capture["response_body_saved"] is False
    assert capture["sample_validation"] == "passed_fixed_scope_checks"
    assert capture["validated_observations_persisted"] is True
    assert capture["source_response_trust"] == "whitelisted_fields_validated"
    assert capture["observations_file"] == "observations.json"
    assert capture["observations_artifact_state"] == "complete_unpublished_sample"
    assert capture["physical_send_count"] is None
    observations = json.loads((evidence / "observations.json").read_bytes())
    assert len(observations["rows"]) == 5
    assert observations["request"]["symbol"] == pilot.PILOT_SYMBOL
    assert b"synthetic capture manifest write failure" not in capture_bytes
    assert _SYNTHETIC_KEY.encode() not in capture_bytes
    assert client.closed


def test_provider_business_error_is_classified_without_raw_error_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A source error after a reservation does not expose message or request ID."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())
    client.failure = error_for_transport("timeout")

    with pytest.raises(pilot.PilotError, match="pilot_source_request_failed"):
        _run(root, client)

    capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert capture["capture_state"] == "transport_outcome_unknown"
    assert capture["failure_category"] == "timeout"
    assert "offline-only" not in (root / "capture" / "capture.json").read_text()
    assert client.closed


def test_pretransport_budget_failure_is_recorded_without_claiming_a_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rejection before scoped reservation is clearly separated from ambiguity."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())
    client.failure = RequestAuthorizationError("synthetic denial")

    with pytest.raises(pilot.PilotError, match="pilot_request_not_authorized"):
        _run(root, client)

    capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert capture["capture_state"] == "failed_before_transport"
    assert capture["local_budget_attempts_reserved"] == 0


def test_http_status_failure_code_is_sanitized_and_carries_observed_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A known HTTP status is retained without the client's full exception string."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client = _FakeClient(_response_body())
    client.failure = error_for_transport("http", detail="503")

    with pytest.raises(pilot.PilotError, match="pilot_source_request_failed"):
        _run(root, client)

    capture = json.loads((root / "capture" / "capture.json").read_bytes())
    assert capture["capture_state"] == "http_error_response_received"
    assert capture["http_status"] == 503
    assert capture["failure_category"] == "http"
    assert client.closed


def test_authorization_file_must_be_private_regular_and_small(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unsafe authorization file metadata is refused before transport."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    path = _write_authorization(root)
    path.chmod(0o640)
    client = _FakeClient(_response_body())

    with pytest.raises(pilot.PilotError, match="authorization_file_permissions_invalid"):
        pilot.run_pilot(
            path,
            root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=lambda _credentials: client,
        )

    assert client.calls == []
    assert not (root / "capture").exists()


def test_authorization_symlink_and_non_object_json_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No-follow file opening and strict root-object parsing reject unsafe admission."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    factory_calls: list[bool] = []

    def factory(_credentials: FuyaoCredentials) -> _FakeClient:
        factory_calls.append(True)
        return _FakeClient(_response_body())

    symlink_case = root / "symlink"
    symlink_case.mkdir(mode=0o700)
    target = _write_authorization(symlink_case)
    linked_authorization = symlink_case / "linked-authorization.json"
    linked_authorization.symlink_to(target.name)
    with pytest.raises(pilot.PilotError, match="authorization_file_invalid"):
        pilot.run_pilot(
            linked_authorization,
            symlink_case / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )
    assert not (symlink_case / "capture").exists()
    assert not list(symlink_case.glob(".fuyao-pilot-*.consumed"))

    json_case = root / "non-object"
    json_case.mkdir(mode=0o700)
    authorization = json_case / "authorization.json"
    descriptor = os.open(authorization, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        pilot._write_all(descriptor, b"[]")
    finally:
        os.close(descriptor)
    with pytest.raises(pilot.PilotError, match="authorization_json_invalid"):
        pilot.run_pilot(
            authorization,
            json_case / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=factory,
        )
    assert not (json_case / "capture").exists()
    assert factory_calls == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        "2026-10-29T12:00:00Z",
        "2026-02-30T12:00:00.000000Z",
        "not-a-timestampZ",
    ],
)
def test_authorization_timestamp_requires_canonical_utc(value: object) -> None:
    """Timestamps must be parseable canonical UTC strings with exact microseconds."""
    with pytest.raises(pilot.PilotError, match="authorization_timestamp_invalid"):
        pilot._parse_utc_timestamp(value)


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_authorization_document_requires_an_exact_key_set(mutation: str) -> None:
    """Admission scope cannot be silently extended or partially specified."""
    now = datetime.now(timezone.utc)
    document = _authorization_document(
        issued_at_utc=pilot._format_utc(now - timedelta(seconds=1)),
        expires_at_utc=pilot._format_utc(now + timedelta(minutes=5)),
    )
    if mutation == "missing":
        document.pop("symbol")
    else:
        document["comment"] = "unreviewed field"
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(pilot.PilotError, match="authorization_scope_invalid"):
        pilot._validate_authorization(document, raw=raw, now=now)


def test_evidence_directory_must_be_new_and_parent_must_already_be_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The writer refuses unsafe output parents and never repairs permissions."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    auth = _write_authorization(root)
    existing = root / "existing"
    existing.mkdir(mode=0o700)
    client = _FakeClient(_response_body())
    with pytest.raises(pilot.PilotError, match="evidence_directory_must_be_new"):
        pilot.run_pilot(
            auth,
            existing,
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=lambda _credentials: client,
        )
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert client.calls == []


def test_authorization_timestamp_nonce_and_duplicate_fields_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed time, nonce, and duplicate authorization keys do not reach a client."""
    _bypass_runtime_guard_for_offline_test(monkeypatch)
    root = _private_root(tmp_path)
    client_factory_calls: list[bool] = []

    def client_factory(_credentials: FuyaoCredentials) -> _FakeClient:
        client_factory_calls.append(True)
        return _FakeClient(_response_body())

    now = datetime.now(timezone.utc)
    invalid_docs = (
        _authorization_document(nonce="not-a-nonce"),
        _authorization_document(issued_at_utc=pilot._format_utc(now + timedelta(hours=1))),
        _authorization_document(expires_at_utc=pilot._format_utc(now - timedelta(seconds=1))),
    )
    for index, document in enumerate(invalid_docs):
        subroot = root / f"case-{index}"
        subroot.mkdir(mode=0o700)
        path = _write_authorization(subroot, document)
        with pytest.raises(pilot.PilotError):
            pilot.run_pilot(
                path,
                subroot / "capture",
                credentials=FuyaoCredentials(_SYNTHETIC_KEY),
                client_factory=client_factory,
            )
        assert not (subroot / "capture").exists()

    duplicate_root = root / "duplicate"
    duplicate_root.mkdir(mode=0o700)
    valid = _authorization_document()
    raw = (
        json.dumps(valid, separators=(",", ":"))
        .replace('"schema_version":1', '"schema_version":1,"schema_version":1')
        .encode()
    )
    path = duplicate_root / "authorization.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
    finally:
        os.close(descriptor)
    with pytest.raises(pilot.PilotError, match="authorization_json_invalid"):
        pilot.run_pilot(
            path,
            duplicate_root / "capture",
            credentials=FuyaoCredentials(_SYNTHETIC_KEY),
            client_factory=client_factory,
        )
    assert client_factory_calls == []


def test_authorization_clock_accepts_exact_ttl_and_rejects_future_expired_or_long_ttl() -> None:
    """The UTC authorization boundary is exact, aware, and capped at 15 minutes."""
    now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
    valid = _authorization_document(
        issued_at_utc=pilot._format_utc(now),
        expires_at_utc=pilot._format_utc(now + pilot.PILOT_AUTHORIZATION_MAX_AGE),
    )
    valid_raw = json.dumps(valid, sort_keys=True, separators=(",", ":")).encode()
    authorization = pilot._validate_authorization(valid, raw=valid_raw, now=now)
    assert authorization.issued_at == now
    assert authorization.expires_at - authorization.issued_at == pilot.PILOT_AUTHORIZATION_MAX_AGE

    invalid_documents = (
        _authorization_document(
            issued_at_utc=pilot._format_utc(now + timedelta(microseconds=1)),
            expires_at_utc=pilot._format_utc(now + timedelta(minutes=10)),
        ),
        _authorization_document(
            issued_at_utc=pilot._format_utc(now - timedelta(minutes=1)),
            expires_at_utc=pilot._format_utc(now),
        ),
        _authorization_document(
            issued_at_utc=pilot._format_utc(now),
            expires_at_utc=pilot._format_utc(
                now + pilot.PILOT_AUTHORIZATION_MAX_AGE + timedelta(microseconds=1)
            ),
        ),
    )
    for document in invalid_documents:
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        with pytest.raises(pilot.PilotError, match="authorization_expired_or_not_yet_valid"):
            pilot._validate_authorization(document, raw=raw, now=now)


def test_noop_cache_never_reads_or_writes_response_bytes() -> None:
    """The injected cache capability is a permanent miss and never persists data."""
    cache = pilot._NoResponseCache()
    cache.get(source="ths", endpoint=PRICES_ENDPOINT)
    assert (
        cache.put(
            method="GET",
            source="ths",
            endpoint=PRICES_ENDPOINT,
            status_code=200,
            content=b"private sample",
        )
        is False
    )
    cache.invalidate(source="ths", endpoint=PRICES_ENDPOINT)


def test_default_client_factory_pins_single_attempt_and_no_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client construction pins the source ceiling, byte cap, timeout, and cache bypass."""
    captured: dict[str, object] = {}

    class _ConstructorFake:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def get(self, endpoint: str, *, params: Mapping[str, object]) -> FuyaoHttpResponse:
            raise AssertionError("constructor fake must not send")

        def close(self) -> None:
            return None

    monkeypatch.setattr(pilot, "FuyaoHttpClient", _ConstructorFake)
    credentials = FuyaoCredentials(_SYNTHETIC_KEY)

    pilot._make_client(credentials, None)

    assert captured["credentials"] is credentials
    assert captured["timeout_seconds"] == 12.0
    assert captured["max_attempts"] == 1
    assert captured["max_response_bytes"] == 65536
    assert isinstance(captured["raw_response_cache"], pilot._NoResponseCache)
    rate_limiter = captured["rate_limiter"]
    assert isinstance(rate_limiter, FuyaoRateLimiter)
    assert rate_limiter.burst == 1
    assert DEFAULT_FUYAO_API_BASE_URL == "https://fuyao.aicubes.cn"


@pytest.mark.parametrize(
    ("behavior", "expected_error"),
    [
        ("valid", None),
        ("wrong_type", "provider_credentials_invalid"),
        ("raises", "provider_credentials_unavailable"),
    ],
)
def test_lazy_credential_loader_uses_only_an_injected_synthetic_provider_module(
    monkeypatch: pytest.MonkeyPatch,
    behavior: str,
    expected_error: str | None,
) -> None:
    """Credential loader behavior is tested without importing project config or .env."""
    module_name = "opendata.data.providers.ths.models._client"
    synthetic_module = types.ModuleType(module_name)
    credentials = FuyaoCredentials(_SYNTHETIC_KEY)

    def synthetic_lookup() -> object:
        if behavior == "raises":
            raise RuntimeError("synthetic configuration unavailable")
        if behavior == "wrong_type":
            return object()
        return credentials

    setattr(synthetic_module, "credentials", synthetic_lookup)  # noqa: B010
    monkeypatch.setitem(sys.modules, module_name, synthetic_module)

    if expected_error is None:
        assert pilot._load_project_credentials() is credentials
    else:
        with pytest.raises(pilot.PilotError, match=expected_error):
            pilot._load_project_credentials()


def test_cli_prints_only_safe_summary_or_fixed_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI surface never serializes full response or exception content."""
    output = io.StringIO()
    success_metadata = {
        "outcome": "bounded_sample_captured",
        "request_sha256": "a" * 64,
        "row_count": 5,
        "retrieved_at_utc": "2026-10-29T12:00:00.000000Z",
        "secret": _SYNTHETIC_KEY,
        "sample": {"close": 123.45},
    }
    monkeypatch.setattr(pilot, "run_pilot", lambda *_args, **_kwargs: success_metadata)
    with contextlib.redirect_stdout(output):
        assert (
            pilot.main(
                [
                    "--authorization-file",
                    "/private/auth.json",
                    "--evidence-dir",
                    "/private/evidence",
                ]
            )
            == 0
        )
    success_output = output.getvalue()
    success_document = json.loads(success_output)
    assert set(success_document) == {
        "outcome",
        "request_sha256",
        "row_count",
        "retrieved_at_utc",
    }
    assert _SYNTHETIC_KEY not in success_output
    assert "123.45" not in success_output

    output = io.StringIO()

    def failed_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise pilot.PilotError("pilot_request_failed")

    monkeypatch.setattr(pilot, "run_pilot", failed_run)
    with contextlib.redirect_stdout(output):
        assert (
            pilot.main(
                [
                    "--authorization-file",
                    "/private/auth.json",
                    "--evidence-dir",
                    "/private/evidence",
                ]
            )
            == 1
        )
    assert json.loads(output.getvalue()) == {
        "outcome": "failed",
        "code": "pilot_request_failed",
    }
    assert _SYNTHETIC_KEY not in output.getvalue()
