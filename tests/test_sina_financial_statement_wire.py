"""Offline governed-transport tests for the bounded Sina FZB fetch seam."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from urllib3.response import HTTPResponse

from opendata.data.http_client import BoundedHttpResponse, GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina import financial_statement_wire as wire_module
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_FZB_HOST,
    SINA_FZB_URL,
    SinaFzbAdmissionError,
    SinaFzbFetchedSnapshot,
    SinaFzbQuery,
    SinaFzbTransportError,
)
from opendata.data.providers.sina.financial_statement_wire import fetch_sina_fzb_raw
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_CAPTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
_CAPTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
_SOURCE = "sina"
_MODEL = "financial_statement"


@lru_cache(maxsize=1)
def _recorded_body() -> bytes:
    """Return the original response bytes from the pinned offline recording."""
    archive = _CAPTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == _CAPTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    return base64.b64decode(call["content_b64"], validate=True)


class _ReplayAdapter(requests.adapters.BaseAdapter):
    """Return one in-memory response and observe the governed request scope."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(
        self,
        status_code: int,
        body: bytes,
        *,
        transport_error: requests.RequestException | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.transport_error = transport_error
        self.requests: list[requests.PreparedRequest] = []
        self.scopes: list[tuple[Any, ...]] = []
        self.responses: list[requests.Response] = []
        self.raw_streams: list[_TrackedRaw] = []

    def send(
        self,
        request: requests.PreparedRequest,
        **_kwargs: object,
    ) -> requests.Response:
        if self.requests:
            raise AssertionError("bounded Sina fetch attempted more than one HTTP request")
        self.requests.append(request)
        self.scopes.append(current_request_scopes())
        if self.transport_error is not None:
            raise self.transport_error
        response = requests.Response()
        response.status_code = self.status_code
        response.url = request.url
        response.request = request
        response.connection = self
        response.encoding = "utf-8"
        response.headers["Content-Length"] = str(len(self.body))
        if self.status_code in (301, 302, 303, 307, 308):
            response.headers["Location"] = "https://other.example.test/redirect"
        raw = _TrackedRaw(self.body)
        response.raw = raw
        response._content = False
        response._content_consumed = False
        self.responses.append(response)
        self.raw_streams.append(raw)
        return response

    def close(self) -> None:
        """Close no external resource because this adapter never opens a socket."""


class _TrackedRaw(HTTPResponse):
    """In-memory urllib3 response that records bounded-client cleanup."""

    def __init__(self, body: bytes) -> None:
        self.body_stream = io.BytesIO(body)
        super().__init__(body=self.body_stream, preload_content=False)
        self.closed_by_test = False

    def close(self) -> None:
        self.closed_by_test = True
        super().close()


@dataclass
class _OfflineTransport:
    """Pair an exact governed client with a scoped, test-only call recorder."""

    client: GovernedHttpClient
    adapter: _ReplayAdapter
    after_response: Callable[[], None] | None = None
    bounded_calls: list[dict[str, Any]] = field(default_factory=list)
    responses: list[BoundedHttpResponse] = field(default_factory=list)

    def fetch(self, context: FetchContext) -> SinaFzbFetchedSnapshot:
        """Record one call while restoring the client's original method on exit."""
        original_get_bounded = self.client.get_bounded

        def recording_get_bounded(
            url: str,
            *,
            max_body_bytes: int,
            overall_timeout: float,
            params: Mapping[str, str] | None = None,
            headers: Mapping[str, str] | None = None,
            timeout: float | tuple[float, float] | None = None,
            source: str = "unspecified",
        ) -> BoundedHttpResponse:
            self.bounded_calls.append(
                {
                    "url": url,
                    "max_body_bytes": max_body_bytes,
                    "overall_timeout": overall_timeout,
                    "params": dict(params or {}),
                    "headers": dict(headers or {}),
                    "timeout": timeout,
                    "source": source,
                }
            )
            response = original_get_bounded(
                url,
                max_body_bytes=max_body_bytes,
                overall_timeout=overall_timeout,
                params=params,
                headers=headers,
                timeout=timeout,
                source=source,
            )
            self.responses.append(response)
            if self.after_response is not None:
                self.after_response()
            return response

        with patch.object(self.client, "get_bounded", recording_get_bounded):
            return fetch_sina_fzb_raw(
                SinaFzbQuery("600519.SH"), context=context, client=self.client
            )


def _make_context(
    *,
    timeout: float | None = 5.0,
    deadline: float | None = None,
    cancellation: threading.Event | None = None,
    request_budget: RequestBudget | None = None,
    operation: RequestOperation = RequestOperation.QUERY,
    grant_source: str = _SOURCE,
    grant_model: str = _MODEL,
    grant_operation: RequestOperation = RequestOperation.QUERY,
    decision: GrantDecision = GrantDecision.ALLOWED,
    allowed_hosts: frozenset[str] | None = None,
) -> FetchContext:
    if request_budget is None and grant_source is not None:
        profile = RequestBudgetProfile.BOUNDED_REQUEST
        grant = RequestGrant(
            source=grant_source,
            canonical_model=grant_model,
            operation=grant_operation,
            decision=decision,
            rights_evidence="test-only:synthetic-offline-grant",
            task_attempts=1,
            source_attempts=1,
            allowed_hosts=allowed_hosts or frozenset({SINA_FZB_HOST}),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            profile=profile,
        )
        request_budget = RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(grant,),
            profile=profile,
        )
    return FetchContext(
        timeout=timeout,
        _deadline_monotonic=deadline,
        _thread_cancel_event=cancellation,
        request_budget=request_budget,
        operation=operation,
    )


def _make_transport(
    body: bytes | None = None,
    *,
    status_code: int = 200,
    after_response: Callable[[], None] | None = None,
    transport_error: requests.RequestException | None = None,
) -> _OfflineTransport:
    selected_body = _recorded_body() if body is None else body
    adapter = _ReplayAdapter(status_code, selected_body, transport_error=transport_error)
    session = requests.Session()
    session.trust_env = False
    session.adapters.clear()
    session.mount("", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(
            connect_timeout=3.0,
            read_timeout=5.0,
            max_attempts=1,
            rate_limit_per_host=None,
        ),
        session=session,
        sleep=lambda _seconds: None,
        raw_response_cache=None,
    )
    return _OfflineTransport(client, adapter, after_response=after_response)


def _fetch_error(
    transport: _OfflineTransport,
    context: FetchContext,
    code: str,
) -> SinaFzbTransportError | SinaFzbAdmissionError:
    with pytest.raises((SinaFzbTransportError, SinaFzbAdmissionError)) as raised:
        transport.fetch(context)
    assert raised.value.code == code
    return raised.value


def test_bounded_fetch_uses_exact_query_and_propagates_the_execution_scope() -> None:
    deadline = time.monotonic() + 30.0
    cancellation = threading.Event()
    context = _make_context(timeout=1.25, deadline=deadline, cancellation=cancellation)
    budget = context.request_budget
    assert budget is not None
    transport = _make_transport()

    snapshot = transport.fetch(context)

    assert SINA_FZB_HOST == "quotes.sina.cn"
    assert SINA_FZB_URL == (
        "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    )
    assert len(transport.adapter.requests) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for(_SOURCE) == 1
    assert len(transport.bounded_calls) == 1
    bounded = transport.bounded_calls[0]
    assert bounded["url"] == SINA_FZB_URL
    assert bounded["source"] == _SOURCE
    assert bounded["params"] == {
        "paperCode": "sh600519",
        "source": "fzb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert bounded["max_body_bytes"] == 4_194_304 == MAX_RAW_RESPONSE_BYTES
    assert bounded["overall_timeout"] == 1.25
    assert bounded["timeout"] == (1.25, 1.25)

    request = transport.adapter.requests[0]
    assert request.method == "GET"
    assert request.headers["Accept-Encoding"] == "identity"
    parsed_url = urlsplit(request.url or "")
    assert parsed_url.scheme == "https"
    assert parsed_url.hostname == SINA_FZB_HOST
    assert parsed_url.path == urlsplit(SINA_FZB_URL).path
    assert parse_qs(parsed_url.query) == {
        "paperCode": ["sh600519"],
        "source": ["fzb"],
        "type": ["0"],
        "page": ["1"],
        "num": ["1000"],
    }

    scope_stack = transport.adapter.scopes[0]
    assert len(scope_stack) == 1
    scope = scope_stack[0]
    assert scope.source == _SOURCE
    assert scope.canonical_model == _MODEL
    assert scope.operation is RequestOperation.QUERY
    assert scope.budget is budget
    assert scope.deadline == deadline
    assert scope.cancellation is cancellation
    assert snapshot.query.symbol == "600519.SH"
    assert len(snapshot.reports) == 103
    assert snapshot.retrieved_at_utc == transport.responses[0].retrieved_at
    assert snapshot.retrieved_at_utc.tzinfo is timezone.utc


def test_context_timeout_is_tightened_against_the_fixed_socket_bounds() -> None:
    context = _make_context(timeout=12.0)
    transport = _make_transport()

    transport.fetch(context)

    assert len(transport.adapter.requests) == 1
    bounded = transport.bounded_calls[0]
    assert bounded["overall_timeout"] == 12.0
    assert bounded["timeout"] == (3.0, 5.0)


def test_non_200_success_status_is_rejected_before_parser_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _make_transport(b"{}", status_code=206)
    context = _make_context()

    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a non-200 response must not reach the JSON parser")

    monkeypatch.setattr(wire_module, "parse_sina_fzb_response", unexpected_parse)
    _fetch_error(transport, context, "http_status")
    assert len(transport.adapter.requests) == 1
    assert transport.responses[0].status_code == 206


def test_redirect_response_is_not_followed_to_another_host() -> None:
    transport = _make_transport(b"redirect body", status_code=302)

    _fetch_error(transport, _make_context(), "http_status")

    assert len(transport.adapter.requests) == 1
    assert transport.adapter.raw_streams[0].closed_by_test
    assert transport.adapter.requests[0].url is not None
    assert urlsplit(transport.adapter.requests[0].url).hostname == SINA_FZB_HOST


@pytest.mark.parametrize(
    ("transport_error", "code"),
    [
        (requests.ConnectionError("synthetic credential=do-not-echo"), "transport_failed"),
        (requests.Timeout("synthetic bounded socket timeout"), "deadline_exceeded"),
    ],
)
def test_bounded_transport_failures_are_sanitized_and_never_retried(
    transport_error: requests.RequestException, code: str
) -> None:
    transport = _make_transport(transport_error=transport_error)
    context = _make_context()

    mapped = _fetch_error(transport, context, code)

    assert len(transport.adapter.requests) == 1
    assert transport.responses == []
    assert len(transport.bounded_calls) == 1
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 1
    assert "do-not-echo" not in str(mapped)


def test_transport_response_over_the_byte_cap_fails_before_json_parsing() -> None:
    transport = _make_transport(b"x" * (MAX_RAW_RESPONSE_BYTES + 1))

    _fetch_error(transport, _make_context(), "response_too_large")

    assert len(transport.adapter.requests) == 1
    assert transport.responses == []
    assert transport.adapter.raw_streams[0].closed_by_test


@pytest.mark.parametrize(
    (
        "budget",
        "context_operation",
        "grant_source",
        "grant_model",
        "grant_operation",
        "decision",
        "hosts",
        "code",
    ),
    [
        pytest.param(
            "empty",
            RequestOperation.QUERY,
            "sina",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.ALLOWED,
            None,
            "admission_denied",
            id="empty-budget",
        ),
        pytest.param(
            "default",
            RequestOperation.STORE,
            "sina",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.ALLOWED,
            None,
            "admission_denied",
            id="non-query-context-operation",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "akshare",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.ALLOWED,
            None,
            "admission_denied",
            id="wrong-source",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "sina",
            "BalanceSheet",
            RequestOperation.QUERY,
            GrantDecision.ALLOWED,
            None,
            "admission_denied",
            id="wrong-model",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "sina",
            "financial_statement",
            RequestOperation.STORE,
            GrantDecision.ALLOWED,
            None,
            "admission_denied",
            id="wrong-grant-operation",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "sina",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.UNKNOWN,
            None,
            "admission_denied",
            id="unknown-grant",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "sina",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.DENIED,
            None,
            "admission_denied",
            id="denied-grant",
        ),
        pytest.param(
            "default",
            RequestOperation.QUERY,
            "sina",
            "financial_statement",
            RequestOperation.QUERY,
            GrantDecision.ALLOWED,
            frozenset({"other.example.test"}),
            "admission_denied",
            id="wrong-host",
        ),
    ],
)
def test_admission_denials_happen_before_any_transport_attempt(
    budget: str | None,
    context_operation: RequestOperation,
    grant_source: str,
    grant_model: str,
    grant_operation: RequestOperation,
    decision: GrantDecision,
    hosts: frozenset[str] | None,
    code: str,
) -> None:
    transport = _make_transport(b"unused")
    context = _make_context(
        request_budget=RequestBudget() if budget == "empty" else None,
        operation=context_operation,
        grant_source=grant_source,
        grant_model=grant_model,
        grant_operation=grant_operation,
        decision=decision,
        allowed_hosts=hosts,
    )

    _fetch_error(transport, context, code)

    assert transport.adapter.requests == []
    assert context.request_budget is None or context.request_budget.attempts_used == 0


def test_absent_request_budget_is_reported_before_the_client_is_called() -> None:
    transport = _make_transport(b"unused")
    context = FetchContext(timeout=2.0)

    _fetch_error(transport, context, "admission_required")

    assert transport.adapter.requests == []
    assert transport.bounded_calls == []


def test_expired_deadline_is_distinct_from_cancellation_and_sends_no_request() -> None:
    transport = _make_transport(b"unused")
    context = _make_context(deadline=time.monotonic() - 1.0)

    _fetch_error(transport, context, "deadline_exceeded")

    assert transport.adapter.requests == []
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0


def test_cancellation_before_transport_has_its_own_code_and_consumes_no_attempt() -> None:
    cancellation = threading.Event()
    cancellation.set()
    transport = _make_transport(b"unused")
    context = _make_context(cancellation=cancellation)

    _fetch_error(transport, context, "cancelled")

    assert transport.adapter.requests == []
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0


def test_cancellation_after_bounded_response_prevents_parser_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    transport = _make_transport(
        after_response=cancellation.set,
    )
    context = _make_context(cancellation=cancellation)

    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cancellation observed after transport must stop parsing")

    monkeypatch.setattr(wire_module, "parse_sina_fzb_response", unexpected_parse)
    _fetch_error(transport, context, "cancelled")

    assert len(transport.adapter.requests) == 1
    assert transport.responses
    assert transport.responses[0].status_code == 200
    assert transport.adapter.raw_streams[0].body_stream.closed


def test_cancellation_after_parsing_prevents_snapshot_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    transport = _make_transport()
    context = _make_context(cancellation=cancellation)
    original_parse = wire_module.parse_sina_fzb_response

    def parse_then_cancel(body: bytes, *, query: SinaFzbQuery) -> object:
        reports = original_parse(body, query=query)
        cancellation.set()
        return reports

    monkeypatch.setattr(wire_module, "parse_sina_fzb_response", parse_then_cancel)
    _fetch_error(transport, context, "cancelled")

    assert len(transport.adapter.requests) == 1
    assert transport.responses[0].status_code == 200
    assert transport.adapter.raw_streams[0].body_stream.closed
