"""Offline request-budget coverage for the THS Fuyao transport boundary."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any

import httpx
import pytest

from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.raw_response_cache import CachedRawResponse
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    RequestRedirectError,
    current_request_scopes,
    request_execution_scope,
    scoped_host_slot,
)

MODEL = "THSScopedBudgetFixture"
HOST = "fuyao.test"
_SUCCESS = json.dumps(
    {
        "code": 0,
        "message": "success",
        "request_id": "offline",
        "data": {"item": []},
    }
).encode()


def _grant(
    *,
    source: str = "ths",
    model: str = MODEL,
    operation: RequestOperation = RequestOperation.QUERY,
    host: str = HOST,
    attempts: int = 3,
    expires_at: datetime | None = None,
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-test-fixture:no-live-provider-rights",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={host},
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _policy_case_grants(case: str, *, now: datetime | None = None) -> tuple[RequestGrant, ...]:
    """Build policy-case grants when the test executes, not during collection."""
    current = now if now is not None else datetime.now(timezone.utc)
    expires_at = current + timedelta(minutes=5)

    if case == "no-grant":
        return ()
    if case == "zero-attempts":
        return (_grant(attempts=0, expires_at=expires_at),)
    if case == "wrong-source":
        return (_grant(source="fred", expires_at=expires_at),)
    if case == "wrong-model":
        return (_grant(model="DifferentModel", expires_at=expires_at),)
    if case == "wrong-host":
        return (_grant(host="different.test", expires_at=expires_at),)
    if case == "expired":
        return (_grant(expires_at=current - timedelta(seconds=1)),)
    if case == "wrong-operation":
        return (_grant(operation=RequestOperation.STORE, expires_at=expires_at),)
    raise AssertionError(f"unknown request-budget policy case: {case}")


def _budget(grants: tuple[RequestGrant, ...], *, attempts: int = 3) -> RequestBudget:
    return RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=grants,
    )


def _client(
    handler: Any,
    *,
    max_attempts: int = 3,
    sleep: Any = lambda _seconds: None,
    rate_limiter: FuyaoRateLimiter | None = None,
    raw_response_cache: Any = None,
) -> FuyaoHttpClient:
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        mock_transport=httpx.MockTransport(handler),
        max_attempts=max_attempts,
        backoff_base_seconds=0,
        backoff_jitter_seconds=0,
        sleep=sleep,
        rate_limiter=rate_limiter,
        raw_response_cache=raw_response_cache,
    )


def _scoped_get(
    client: FuyaoHttpClient,
    budget: RequestBudget,
    *,
    source: str = "ths",
    model: str | None = MODEL,
    deadline: float | None = None,
    cancellation: Any = None,
    params: dict[str, Any] | None = None,
) -> Any:
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        budget=budget,
        deadline=deadline,
        cancellation=cancellation,
    ):
        return client.get(
            "/v1/query",
            params=params if params is not None else {"fixture": "offline"},
        )


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("no-grant", RequestAuthorizationError),
        ("zero-attempts", RequestAttemptLimitError),
        ("wrong-source", RequestAuthorizationError),
        ("wrong-model", RequestAuthorizationError),
        ("wrong-host", RequestAuthorizationError),
        ("expired", RequestAuthorizationError),
        ("wrong-operation", RequestAuthorizationError),
    ],
    ids=(
        "no-grant",
        "zero-attempts",
        "wrong-source",
        "wrong-model",
        "wrong-host",
        "expired",
        "wrong-operation",
    ),
)
def test_scope_policy_rejections_happen_before_mock_transport_send(
    case: str, expected_error: type[Exception]
) -> None:
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(200, content=_SUCCESS)

    client = _client(handler)
    budget = _budget(_policy_case_grants(case))
    with pytest.raises(expected_error) as error:
        _scoped_get(client, budget)

    assert sends == []
    assert budget.attempts_used == 0
    assert budget.attempts_used_for("ths") == 0
    assert "offline-key" not in str(error.value)


def test_zero_attempt_grant_is_fresh_after_a_collection_delay() -> None:
    collection_time = datetime.now(timezone.utc)
    delayed_execution_time = collection_time + timedelta(minutes=6)

    grants = _policy_case_grants("zero-attempts", now=delayed_execution_time)
    assert len(grants) == 1
    assert grants[0].expires_at == delayed_execution_time + timedelta(minutes=5)
    assert grants[0].expires_at > collection_time + timedelta(minutes=5)

    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(200, content=_SUCCESS)

    client = _client(handler)
    budget = _budget(grants)

    with pytest.raises(RequestAttemptLimitError):
        _scoped_get(client, budget)

    assert sends == []
    assert budget.attempts_used == 0
    assert budget.attempts_used_for("ths") == 0


def test_query_preparation_failure_does_not_consume_an_attempt() -> None:
    class InvalidQueryValue:
        def __str__(self) -> str:
            raise ValueError("query preparation failed")

    sends: list[httpx.Request] = []
    client = _client(lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS))
    budget = _budget((_grant(),))

    with pytest.raises(ValueError, match="query preparation failed"):
        _scoped_get(client, budget, params={"bad": InvalidQueryValue()})

    assert sends == []
    assert budget.attempts_used == 0
    assert budget.attempts_used_for("ths") == 0


def test_scope_cache_hit_requires_grant_but_consumes_no_attempt() -> None:
    class Cache:
        def __init__(self) -> None:
            self.get_calls = 0

        def get(self, **_kwargs: Any) -> CachedRawResponse:
            self.get_calls += 1
            now = time.time()
            return CachedRawResponse(
                status_code=200,
                content=_SUCCESS,
                encoding="utf-8",
                created_at=now,
                expires_at=now + 60,
            )

        def put(self, **_kwargs: Any) -> None:
            raise AssertionError("cache hit must not write")

        def invalidate(self, **_kwargs: Any) -> None:
            raise AssertionError("valid cached response must not be invalidated")

    cache = Cache()
    sends: list[httpx.Request] = []
    client = _client(
        lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS),
        raw_response_cache=cache,
    )
    no_grant = _budget(())
    with pytest.raises(RequestAuthorizationError):
        _scoped_get(client, no_grant)
    assert cache.get_calls == 0
    assert sends == []

    zero_attempt_budget = RequestBudget(
        task_attempts=0,
        source_attempts=0,
        grants=(_grant(attempts=0),),
    )
    result = _scoped_get(client, zero_attempt_budget)
    assert result.status_code == 200
    assert cache.get_calls == 1
    assert sends == []
    assert zero_attempt_budget.attempts_used == 0


def test_each_429_retry_consumes_one_actual_mock_transport_attempt() -> None:
    now = {"value": 0.0}
    limiter = FuyaoRateLimiter(
        rate_per_second=10.0,
        burst=10,
        clock=lambda: now["value"],
    )
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        if len(sends) < 3:
            return httpx.Response(429, content=b"")
        return httpx.Response(200, content=_SUCCESS)

    client = _client(
        handler,
        max_attempts=3,
        sleep=lambda seconds: now.__setitem__("value", now["value"] + seconds + 1e-6),
        rate_limiter=limiter,
    )
    budget = _budget((_grant(attempts=3),), attempts=3)
    result = _scoped_get(client, budget)

    assert result.status_code == 200
    assert len(sends) == 3
    assert budget.attempts_used == 3
    assert budget.attempts_used_for("ths") == 3


def test_timeout_retries_each_consume_one_actual_mock_transport_attempt() -> None:
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        if len(sends) == 1:
            raise httpx.ReadTimeout("offline timeout", request=request)
        return httpx.Response(200, content=_SUCCESS)

    client = _client(handler, max_attempts=2)
    budget = _budget((_grant(attempts=2),), attempts=2)
    result = _scoped_get(client, budget)

    assert result.status_code == 200
    assert len(sends) == 2
    assert budget.attempts_used == 2
    assert budget.attempts_used_for("ths") == 2


def test_http_503_keeps_existing_single_attempt_policy_and_is_counted() -> None:
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(503, content=b"offline upstream unavailable")

    client = _client(handler, max_attempts=4)
    budget = _budget((_grant(attempts=3),), attempts=3)
    with pytest.raises(FuyaoError) as error:
        _scoped_get(client, budget)

    assert error.value.category == "http"
    assert error.value.retryable is False
    assert len(sends) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for("ths") == 1


def test_budget_exhaustion_after_three_retries_does_not_send_a_fourth_time() -> None:
    now = {"value": 0.0}
    limiter = FuyaoRateLimiter(
        rate_per_second=10.0,
        burst=10,
        clock=lambda: now["value"],
    )
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(429, content=b"")

    client = _client(
        handler,
        max_attempts=5,
        sleep=lambda seconds: now.__setitem__("value", now["value"] + seconds + 1e-6),
        rate_limiter=limiter,
    )
    budget = _budget((_grant(attempts=3),), attempts=3)
    with pytest.raises(RequestAttemptLimitError) as error:
        _scoped_get(client, budget)

    assert error.value.__class__ is RequestAttemptLimitError
    assert len(sends) == 3
    assert budget.attempts_used == 3
    assert budget.attempts_used_for("ths") == 3


def test_scoped_request_does_not_follow_redirects_and_preserves_error_type() -> None:
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(302, headers={"location": "https://other.test/"})

    client = _client(handler, max_attempts=4)
    budget = _budget((_grant(attempts=1),), attempts=1)
    with pytest.raises(RequestRedirectError) as error:
        _scoped_get(client, budget)

    assert error.value.__class__ is RequestRedirectError
    assert error.value.__cause__ is None
    assert len(sends) == 1
    assert budget.attempts_used == 1


def test_scoped_upstream_error_keeps_fuyao_classification_after_counting_send() -> None:
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        body = json.dumps({"code": 5001, "message": "temporary", "request_id": "offline"}).encode()
        return httpx.Response(200, content=body)

    client = _client(handler, max_attempts=1)
    budget = _budget((_grant(attempts=2),), attempts=2)
    with pytest.raises(FuyaoError) as error:
        _scoped_get(client, budget)

    assert error.value.__class__ is FuyaoError
    assert error.value.category == "transient"
    assert len(sends) == 1
    assert budget.attempts_used == 1


@pytest.mark.parametrize("mode", ["pre-cancelled", "cancel-after-retry"])
def test_cancellation_stops_future_sends_and_preserves_budget_error(mode: str) -> None:
    cancelled = Event()
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(429, content=b"")

    def cancel_during_backoff(_seconds: float) -> None:
        cancelled.set()

    client = _client(
        handler,
        max_attempts=3,
        sleep=cancel_during_backoff,
        rate_limiter=FuyaoRateLimiter(burst=10),
    )
    budget = _budget((_grant(attempts=3),), attempts=3)
    if mode == "pre-cancelled":
        cancelled.set()
    with pytest.raises(RequestExecutionCancelledError) as error:
        _scoped_get(client, budget, cancellation=cancelled)

    assert error.value.__class__ is RequestExecutionCancelledError
    assert len(sends) == (0 if mode == "pre-cancelled" else 1)
    assert budget.attempts_used == len(sends)


def test_cancellation_while_waiting_for_shared_budget_host_slot_releases_waiter() -> None:
    cancelled = Event()
    sends: list[httpx.Request] = []
    limiter = FuyaoRateLimiter(burst=100)
    client = _client(
        lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS),
        rate_limiter=limiter,
    )
    budget = _budget((_grant(),))
    waiting = Event()

    def call_while_slot_is_held() -> None:
        waiting.set()
        _scoped_get(client, budget, cancellation=cancelled)

    with (
        request_execution_scope(
            source="ths",
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        scoped_host_slot(current_request_scopes(), "ths", HOST),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(call_while_slot_is_held)
        assert waiting.wait(timeout=1)
        time.sleep(0.05)
        cancelled.set()
        with pytest.raises(RequestExecutionCancelledError):
            future.result(timeout=1)

    assert sends == []
    assert budget.attempts_used == 0
    result = _scoped_get(client, budget)
    assert result.status_code == 200
    assert len(sends) == 1
    assert budget.attempts_used == 1


def test_cancellation_while_waiting_for_instance_lock_is_checked_before_send() -> None:
    entered_handler = Event()
    release_handler = Event()
    second_started = Event()
    cancelled = Event()
    sends: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        if len(sends) == 1:
            entered_handler.set()
            assert release_handler.wait(timeout=1)
        return httpx.Response(200, content=_SUCCESS)

    client = _client(handler, rate_limiter=FuyaoRateLimiter(burst=100))
    budget = _budget((_grant(),), attempts=2)

    def first_call() -> Any:
        return _scoped_get(client, budget)

    def second_call() -> None:
        second_started.set()
        _scoped_get(client, budget, cancellation=cancelled)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(first_call)
        assert entered_handler.wait(timeout=1)
        second = executor.submit(second_call)
        assert second_started.wait(timeout=1)
        time.sleep(0.05)
        cancelled.set()
        with pytest.raises(RequestExecutionCancelledError):
            second.result(timeout=1)
        assert len(sends) == 1
        release_handler.set()
        assert first.result(timeout=1).status_code == 200

    assert budget.attempts_used == 1


def test_deadline_caps_transport_timeout_and_stops_retry_sends() -> None:
    sends: list[httpx.Request] = []
    captured_timeouts: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        captured_timeouts.append(request.extensions["timeout"])
        return httpx.Response(429, content=b"")

    client = _client(
        handler,
        max_attempts=3,
        sleep=lambda seconds: time.sleep(0.08),
        rate_limiter=FuyaoRateLimiter(burst=10),
    )
    budget = _budget((_grant(attempts=3),), attempts=3)
    deadline = time.monotonic() + 0.05
    with pytest.raises(RequestExecutionDeadlineError) as error:
        _scoped_get(client, budget, deadline=deadline)

    assert error.value.__class__ is RequestExecutionDeadlineError
    assert len(sends) == 1
    assert captured_timeouts[0]["read"] < client._timeout_seconds
    assert budget.attempts_used == 1


@pytest.mark.parametrize("configuration", ["auth", "event-hook", "opaque-client"])
def test_scoped_client_configuration_that_can_hide_sends_fails_closed(
    configuration: str,
) -> None:
    sends: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS)
    )
    if configuration == "opaque-client":
        injected = httpx.Client(
            transport=httpx.HTTPTransport(retries=1, trust_env=False),
            trust_env=False,
        )
        client = FuyaoHttpClient(
            credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
            client=injected,
        )
    else:
        client = FuyaoHttpClient(
            credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
            mock_transport=transport,
        )
        if configuration == "auth":
            client._client.auth = httpx.BasicAuth("test-user", "test-password")
        else:
            client._client.event_hooks["response"].append(lambda _response: None)
    budget = _budget((_grant(),))

    with pytest.raises(RequestAuthorizationError):
        _scoped_get(client, budget)

    assert sends == []
    assert budget.attempts_used == 0
    if configuration == "opaque-client":
        injected.close()


def test_closed_scoped_client_is_rejected_before_reserving_or_sending() -> None:
    sends: list[httpx.Request] = []
    client = _client(lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS))
    client.close()
    budget = _budget((_grant(),))

    with pytest.raises(RequestAuthorizationError, match="client is closed"):
        _scoped_get(client, budget)

    assert sends == []
    assert budget.attempts_used == 0


def test_default_http_transport_is_constructed_with_zero_retries_and_no_env_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = httpx.HTTPTransport
    observed: dict[str, Any] = {}

    def capture_transport(*args: Any, **kwargs: Any) -> httpx.HTTPTransport:
        observed.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", capture_transport)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}")
    )

    assert observed["retries"] == 0
    assert observed["trust_env"] is False
    assert client._client.auth is None
    assert client._client.event_hooks == {"request": [], "response": []}
    client.close()
