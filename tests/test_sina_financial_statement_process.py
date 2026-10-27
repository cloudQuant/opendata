"""Offline tests for the fixed, default-deny Sina FZB child operation."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import requests
from urllib3.response import HTTPResponse

from opendata.data.http_client import BoundedHttpResponse, GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_FZB_HOST,
    SINA_FZB_URL,
    SINA_SOURCE,
    SinaFzbAdmissionError,
    SinaFzbTransportError,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_process import (
    SINA_FZB_PROCESS_OPERATION_ID,
    SINA_FZB_PROCESS_PROTOCOL,
    _capture_sina_fzb_response,
    _validated_retrieved_at,
    fetch_sina_fzb_capture,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.pipeline.process_executor import (
    MAX_RESULT_BYTES,
    MAX_RESULT_TEXT_BYTES,
    ProcessExecutionHandlerError,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

UTC = timezone.utc
VALID_RETRIEVED_AT = datetime(2026, 10, 27, 2, 0, 0, 123456, tzinfo=UTC)
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
FIXTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
SYMBOL = "600519.SH"


class _TrackedRaw(HTTPResponse):
    """Provide pinned bytes to Requests without opening a socket."""

    def __init__(self, body: bytes) -> None:
        self.body_stream = io.BytesIO(body)
        super().__init__(body=self.body_stream, preload_content=False)


class _ReplayAdapter(requests.adapters.BaseAdapter):
    """Return one in-memory status/body pair and record the prepared request."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(self, body: bytes, *, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code
        self.requests: list[requests.PreparedRequest] = []
        self.responses: list[requests.Response] = []

    def send(
        self,
        request: requests.PreparedRequest,
        **_kwargs: object,
    ) -> requests.Response:
        if self.requests:
            raise AssertionError("the child attempted more than one source request")
        self.requests.append(request)
        response = requests.Response()
        response.status_code = self.status_code
        response.url = request.url
        response.request = request
        response.connection = self
        response.encoding = "utf-8"
        response.headers["Content-Length"] = str(len(self.body))
        response.raw = _TrackedRaw(self.body)
        response._content = False
        response._content_consumed = False
        self.responses.append(response)
        return response

    def close(self) -> None:
        """Close no external resource because this adapter never opens a socket."""


def _recorded_body() -> bytes:
    """Read only the pinned offline response body used by W9/W12 fixtures."""
    archive = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == FIXTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    return base64.b64decode(call["content_b64"], validate=True)


def _authorized_context() -> FetchContext:
    """Create a finite test-only grant; it has no production authority."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SINA_SOURCE,
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only:offline-fixture-transport",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset((SINA_FZB_HOST,)),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(
        timeout=20.0,
        request_budget=budget,
        operation=RequestOperation.QUERY,
    )


def _client(adapter: _ReplayAdapter) -> tuple[GovernedHttpClient, requests.Session]:
    """Build the real bounded client around an in-memory Requests adapter."""
    session = requests.Session()
    session.trust_env = False
    session.adapters.clear()
    session.mount("", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(
            connect_timeout=3.0,
            read_timeout=5.0,
            max_attempts=1,
            backoff_base=0.0,
            backoff_jitter=0.0,
            rate_limit_per_host=None,
        ),
        session=session,
        sleep=lambda _seconds: None,
        raw_response_cache=None,
    )
    return client, session


def test_private_capture_seam_preserves_exact_fixture_bytes_in_canonical_base64() -> None:
    body = _recorded_body()
    adapter = _ReplayAdapter(body)
    client, session = _client(adapter)
    context = _authorized_context()
    params: dict[str, JsonValue] = {
        "protocol": SINA_FZB_PROCESS_PROTOCOL,
        "symbol": SYMBOL,
    }

    envelope = _capture_sina_fzb_response(params, context=context, client=client)

    assert set(envelope) == {
        "protocol",
        "query",
        "retrieved_at_utc",
        "status_code",
        "body_b64",
    }
    assert envelope["protocol"] == SINA_FZB_PROCESS_PROTOCOL
    assert envelope["query"] == {"symbol": SYMBOL}
    assert type(envelope["status_code"]) is int and envelope["status_code"] == 200
    encoded = envelope["body_b64"]
    assert isinstance(encoded, str)
    decoded = base64.b64decode(encoded, validate=True)
    assert base64.b64encode(decoded).decode("ascii") == encoded
    assert decoded == body and len(decoded) <= MAX_RAW_RESPONSE_BYTES
    retrieved = envelope["retrieved_at_utc"]
    assert isinstance(retrieved, str) and retrieved.endswith("Z")
    assert len(retrieved) == 27 and retrieved[19] == "." and retrieved[20:26].isdigit()
    parsed = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
    assert parsed.tzinfo is UTC
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.method == "GET"
    assert request.url == SINA_FZB_URL + "?paperCode=sh600519&source=fzb&type=0&page=1&num=1000"
    assert request.headers["Accept-Encoding"] == "identity"
    assert session.trust_env is False
    assert client._config.max_attempts == 1
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 1
    assert MAX_RESULT_BYTES == 16 * 1024 * 1024
    assert MAX_RESULT_TEXT_BYTES == 8 * 1024 * 1024


@pytest.mark.parametrize(
    "parameters",
    [
        {},
        {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL, "url": SINA_FZB_URL},
        {"protocol": SINA_FZB_PROCESS_PROTOCOL, "unexpected": SYMBOL},
        {"protocol": "wrong-protocol", "symbol": SYMBOL},
        {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": [SYMBOL]},
        {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": "600519"},
    ],
)
def test_private_capture_seam_rejects_noncanonical_request_before_transport(
    parameters: dict[str, JsonValue],
) -> None:
    adapter = _ReplayAdapter(b"unused")
    client, _session = _client(adapter)

    with pytest.raises(SinaFzbWireError) as raised:
        _capture_sina_fzb_response(parameters, context=_authorized_context(), client=client)

    assert getattr(raised.value, "code", None) in {"invalid_envelope", "invalid_query"}
    assert adapter.requests == []


@pytest.mark.parametrize("invalid_dependency", ["context", "client"])
def test_private_capture_rejects_wrong_dependency_types_before_transport(
    invalid_dependency: str,
) -> None:
    adapter = _ReplayAdapter(b"unused")
    client, _session = _client(adapter)
    context: FetchContext = _authorized_context()

    if invalid_dependency == "context":
        context = cast("FetchContext", object())
    else:
        client = cast("GovernedHttpClient", object())

    with pytest.raises(SinaFzbAdmissionError) as raised:
        _capture_sina_fzb_response(
            {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL},
            context=context,
            client=client,
        )

    assert raised.value.code == "admission_denied"
    assert adapter.requests == []


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        pytest.param(object(), "transport_failed", id="wrong-response-type"),
        pytest.param(
            BoundedHttpResponse(body=b"x", status_code=True, retrieved_at=VALID_RETRIEVED_AT),
            "transport_failed",
            id="boolean-status",
        ),
        pytest.param(
            BoundedHttpResponse(body=b"x", status_code=503, retrieved_at=VALID_RETRIEVED_AT),
            "http_status",
            id="upstream-non-200",
        ),
        pytest.param(
            BoundedHttpResponse(body="not-bytes", status_code=200, retrieved_at=VALID_RETRIEVED_AT),
            "transport_failed",
            id="non-byte-body",
        ),
        pytest.param(
            BoundedHttpResponse(
                body=b"x" * (MAX_RAW_RESPONSE_BYTES + 1),
                status_code=200,
                retrieved_at=VALID_RETRIEVED_AT,
            ),
            "response_too_large",
            id="oversized-body",
        ),
    ],
)
def test_private_capture_rejects_invalid_bounded_helper_results(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    expected_code: str,
) -> None:
    adapter = _ReplayAdapter(b"unused")
    client, _session = _client(adapter)
    target = (
        "opendata.data.providers.sina.financial_statement_process._fetch_sina_fzb_bounded_response"
    )
    monkeypatch.setattr(target, lambda *_args, **_kwargs: response)

    with pytest.raises(SinaFzbTransportError) as raised:
        _capture_sina_fzb_response(
            {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL},
            context=_authorized_context(),
            client=client,
        )

    assert raised.value.code == expected_code
    assert adapter.requests == []


def test_retrieved_time_rejects_naive_offset_and_unusable_timezone_values() -> None:
    class BrokenTimezone(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta:
            raise RuntimeError("timezone details must not escape")

        def dst(self, _dt: datetime | None) -> timedelta | None:
            return None

    invalid_values: tuple[object, ...] = (
        None,
        datetime(2026, 10, 27, 2, 0, 0, 123456),
        datetime(2026, 10, 27, 2, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))),
        datetime(2026, 10, 27, 2, 0, 0, 123456, tzinfo=BrokenTimezone()),
    )

    for value in invalid_values:
        with pytest.raises(SinaFzbTransportError) as raised:
            _validated_retrieved_at(value)
        assert raised.value.code == "transport_failed"


def test_production_child_constructs_environment_closed_client_but_denies_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[requests.Session] = []

    class ObservedSession(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            sessions.append(self)

        def send(self, *_args: object, **_kwargs: object) -> requests.Response:
            raise AssertionError("default UNKNOWN grant must deny before HTTP send")

    monkeypatch.setattr(GovernedHttpClient, "_get_session", lambda _client: ObservedSession())

    with pytest.raises(SinaFzbAdmissionError) as raised:
        fetch_sina_fzb_capture({"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL})

    assert raised.value.code == "admission_denied"
    assert len(sessions) == 1
    assert sessions[0].trust_env is False
    assert sessions[0].auth is None
    assert sessions[0].proxies == {}
    assert "Authorization" not in sessions[0].headers


def test_production_child_closes_session_when_configuration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConfigurationFailureSession:
        closed = False

        @property
        def trust_env(self) -> bool:
            return True

        @trust_env.setter
        def trust_env(self, _value: bool) -> None:
            raise RuntimeError("session_configuration_failed")

        def close(self) -> None:
            self.closed = True

    session = ConfigurationFailureSession()
    monkeypatch.setattr(GovernedHttpClient, "_get_session", lambda _client: session)

    with pytest.raises(RuntimeError, match="session_configuration_failed"):
        fetch_sina_fzb_capture({"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL})

    assert session.closed


def test_fixed_production_operation_fails_safely_in_a_real_child_process() -> None:
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({SINA_FZB_PROCESS_OPERATION_ID: fetch_sina_fzb_capture}),
        max_result_bytes=MAX_RESULT_BYTES,
        max_result_text_bytes=MAX_RESULT_TEXT_BYTES,
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.05,
        shutdown_grace_seconds=0.1,
    )

    with pytest.raises(ProcessExecutionHandlerError, match="process_handler_failed") as raised:
        executor.run(
            SINA_FZB_PROCESS_OPERATION_ID,
            {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": SYMBOL},
            timeout_seconds=5.0,
        )

    assert "quotes.sina.cn" not in str(raised.value)
    assert "financial_statement" not in str(raised.value)
