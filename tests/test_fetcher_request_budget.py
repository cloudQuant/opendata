"""Fetcher-template and governed-HTTP request budget integration tests."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.capability import Capability
from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.protocol import (
    FetchContext,
    Fetcher,
    FetchResult,
    NativeAsyncExtractionNotImplementedError,
    QueryParams,
    UnsupportedAsyncFetcherError,
)
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.fred.models import _client as fred_client
from opendata.data.raw_response_cache import CachedRawResponse
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAdapterRetryError,
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
)

HOST = "api.example.test"
URL = f"https://{HOST}/data"


def test_fetch_context_validates_budget_and_operation_without_timeout() -> None:
    """Context policy fields are checked even when no deadline is requested."""
    with pytest.raises(ValueError, match="request_budget must be a RequestBudget"):
        FetchContext(request_budget=cast("RequestBudget", object()))

    context = FetchContext(operation=cast("RequestOperation", "query"))
    assert context.operation is RequestOperation.QUERY


def make_grant(
    model: str = "TinyModel",
    *,
    source: str = "budget_test",
    operation: RequestOperation = RequestOperation.QUERY,
    task_attempts: int = 3,
    source_attempts: int = 3,
    host: str = HOST,
) -> RequestGrant:
    """Create explicit offline authority for a test-only endpoint."""
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-evidence:offline-fixture",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def make_budget(
    *,
    models: tuple[str, ...] = ("TinyModel",),
    source: str = "budget_test",
    task_attempts: int = 3,
    source_attempts: int = 3,
    host: str = HOST,
) -> RequestBudget:
    """Create a finite budget with one grant per canonical model."""
    return RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=tuple(
            make_grant(
                model,
                source=source,
                task_attempts=task_attempts,
                source_attempts=source_attempts,
                host=host,
            )
            for model in models
        ),
    )


class FakeResponse(requests.Response):
    """A local requests response with no socket or live endpoint."""

    def __init__(self, status_code: int = 200, content: bytes = b"{}") -> None:
        super().__init__()
        self.status_code = status_code
        self._content = content
        self.encoding = "utf-8"
        self.headers["Content-Type"] = "application/json"


class FakeSession:
    """Record the transport boundary and replay responses or exceptions."""

    def __init__(self, script: tuple[object, ...] = ()) -> None:
        self._script = deque(script)
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self._lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_first = False

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        with self._lock:
            self.calls.append((method, url, dict(kwargs)))
            outcome = self._script.popleft() if self._script else FakeResponse()
        self.started.set()
        if self.block_first and len(self.calls) == 1:
            self.release.wait(timeout=2)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, requests.Response):
            return outcome
        raise AssertionError("FakeSession scripts must contain Response or exception values")


class OfflineAdapter(requests.adapters.BaseAdapter):
    """Socket-free requests adapter that can replay an auth challenge."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(self, statuses: tuple[int, ...]) -> None:
        self.statuses = deque(statuses)
        self.calls: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        self.calls.append(request)
        status = self.statuses.popleft() if self.statuses else 200
        response = FakeResponse(status)
        response.url = request.url
        response.request = request
        response.connection = self
        if status == 401:
            response.headers["WWW-Authenticate"] = (
                'Digest realm="offline", nonce="offline-nonce", qop="auth"'
            )
        return response

    def close(self) -> None:
        """Release no resources; this adapter never opens a socket."""


def make_client(
    session: object,
    *,
    max_attempts: int = 3,
    raw_response_cache: object | None = None,
) -> GovernedHttpClient:
    """Build a no-delay client over a test-only session."""
    return GovernedHttpClient(
        HttpClientConfig(
            max_attempts=max_attempts,
            rate_limit_per_host=None,
            backoff_base=0,
            backoff_jitter=0,
        ),
        session=cast("requests.Session", session),
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=cast("Any", raw_response_cache),
    )


def make_breaker_client(
    session: requests.Session,
    time_fn: Callable[[], float],
) -> GovernedHttpClient:
    """Build an offline-test client with a one-response breaker threshold."""
    return GovernedHttpClient(
        HttpClientConfig(
            max_attempts=1,
            rate_limit_per_host=None,
            breaker_threshold=1,
            breaker_cooldown=5,
            backoff_base=0,
            backoff_jitter=0,
        ),
        session=session,
        sleep=lambda _seconds: None,
        time_fn=time_fn,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )


def open_rate_limit_breaker(client: GovernedHttpClient) -> None:
    """Trip the host breaker with one offline 429 response."""
    with pytest.raises(HttpFetchError) as rejected:
        client.get(URL, source="budget_test")
    assert rejected.value.category is FailureCategory.RATE_LIMITED


class TinyQuery(QueryParams):
    """Minimal local query used by test fetchers."""

    key: str = "value"


class TinyFetcher(Fetcher[TinyQuery, list[dict[str, str]]]):
    """A bounded template fetcher whose extract uses the real HTTP client."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "TinyModel"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="request_budget_test",
        period="snapshot",
        market="test",
        source="budget_test",
        verified=False,
    )

    def __init__(self, client: GovernedHttpClient) -> None:
        self.client = client
        self.validate_count = 0
        self.extract_count = 0
        self.normalize_count = 0
        self.finished = threading.Event()
        self.double_request = False

    def transform_query(self, **kwargs: object) -> TinyQuery:
        self.validate_count += 1
        return TinyQuery.model_validate(kwargs)

    def extract_data(self, params: TinyQuery, ctx: FetchContext) -> list[dict[str, str]]:
        del params, ctx
        self.extract_count += 1
        try:
            response = self.client.get(URL, source="budget_test")
            if self.double_request:
                self.client.get(URL, source="budget_test")
            return [{"status": str(response.status_code)}]
        finally:
            self.finished.set()

    def transform_data(
        self,
        raw: list[dict[str, str]],
        params: TinyQuery,
    ) -> FetchResult:
        del params
        self.normalize_count += 1
        return cast("FetchResult", tuple(raw))


class NativeTinyFetcher(TinyFetcher):
    """A native async implementation that checks scope before fake transport."""

    async_mode = "native_async"

    async def extract_data_async(
        self,
        params: TinyQuery,
        ctx: FetchContext,
    ) -> list[dict[str, str]]:
        del params, ctx
        assert current_request_scopes()
        response = await asyncio.to_thread(self.client.get, URL, source="budget_test")
        return [{"status": str(response.status_code)}]


class UnsupportedTinyFetcher(TinyFetcher):
    """A template whose async mode is explicitly unavailable."""

    async_mode = "unsupported"


class DefaultNativeTinyFetcher(TinyFetcher):
    """Claims native async without overriding the clear default error."""

    async_mode = "native_async"


class TinyDoubleRequestFetcher(TinyFetcher):
    """Issue a second GET to exercise cancellation/deadline between sends."""

    def extract_data(self, params: TinyQuery, ctx: FetchContext) -> list[dict[str, str]]:
        self.double_request = True
        try:
            return super().extract_data(params, ctx)
        finally:
            self.finished.set()


@pytest.fixture(autouse=True)
def disable_process_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test isolated from application cache initialization."""
    import opendata.data.http_client as http_client_module

    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)


def test_sync_and_bounded_async_share_template_scope_and_stage_counts() -> None:
    budget = make_budget(task_attempts=2, source_attempts=2)
    session = FakeSession((FakeResponse(), FakeResponse()))
    fetcher = TinyFetcher(make_client(session))

    sync_result = fetcher.fetch(ctx=FetchContext(request_budget=budget), key="sync")
    async_result = asyncio.run(
        fetcher.fetch_async(ctx=FetchContext(request_budget=budget), key="async")
    )

    assert sync_result == ({"status": "200"},)
    assert async_result == sync_result
    assert (fetcher.validate_count, fetcher.extract_count, fetcher.normalize_count) == (2, 2, 2)
    assert len(session.calls) == 2
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert budget.attempts_used == 2


def test_canonical_fetcher_without_budget_uses_zero_authority_before_session_request() -> None:
    session = FakeSession((FakeResponse(),))
    fetcher = TinyFetcher(make_client(session))

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(key="refused")

    assert session.calls == []


def test_unsupported_async_mode_fails_before_fetcher_io() -> None:
    session = FakeSession((FakeResponse(),))
    fetcher = UnsupportedTinyFetcher(make_client(session))

    with pytest.raises(UnsupportedAsyncFetcherError):
        asyncio.run(fetcher.fetch_async(key="not-started"))

    assert session.calls == []


def test_native_async_uses_the_same_scope_and_default_implementation_fails_clearly() -> None:
    budget = make_budget(task_attempts=1, source_attempts=1)
    session = FakeSession((FakeResponse(),))
    fetcher = NativeTinyFetcher(make_client(session))

    result = asyncio.run(fetcher.fetch_async(ctx=FetchContext(request_budget=budget)))

    assert result == ({"status": "200"},)
    assert budget.attempts_used == 1
    assert session.calls[0][2]["allow_redirects"] is False

    default_native = DefaultNativeTinyFetcher(make_client(FakeSession()))
    with pytest.raises(NativeAsyncExtractionNotImplementedError):
        asyncio.run(default_native.fetch_async())


def test_same_budget_attempt_count_survives_calls_on_different_event_loops() -> None:
    budget = make_budget(task_attempts=1, source_attempts=1)
    session = FakeSession((FakeResponse(), FakeResponse()))
    fetcher = TinyFetcher(make_client(session))

    assert asyncio.run(fetcher.fetch_async(ctx=FetchContext(request_budget=budget))) == (
        {"status": "200"},
    )
    with pytest.raises(RequestAttemptLimitError):
        asyncio.run(fetcher.fetch_async(ctx=FetchContext(request_budget=budget)))

    assert budget.attempts_used == 1
    assert len(session.calls) == 1


def test_failed_transport_attempt_is_counted_and_cannot_be_retried_past_budget() -> None:
    budget = make_budget(task_attempts=1, source_attempts=1)
    session = FakeSession((requests.Timeout("offline timeout"), FakeResponse()))
    client = make_client(session, max_attempts=3)

    with (
        request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAttemptLimitError),
    ):
        client.get(URL, source="budget_test")

    assert len(session.calls) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for("budget_test") == 1


def test_pagination_style_second_request_stops_after_caller_cancellation() -> None:
    budget = make_budget(task_attempts=2, source_attempts=2)
    session = FakeSession((FakeResponse(), FakeResponse()))
    session.block_first = True
    fetcher = TinyDoubleRequestFetcher(make_client(session))

    async def run_and_cancel() -> None:
        call = asyncio.create_task(
            fetcher.fetch_async(ctx=FetchContext(timeout=1, request_budget=budget))
        )
        assert await asyncio.to_thread(session.started.wait, 1)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    try:
        asyncio.run(run_and_cancel())
    finally:
        session.release.set()
    assert fetcher.finished.wait(timeout=1)
    assert len(session.calls) == 1
    assert budget.attempts_used == 1


def test_deadline_prevents_a_second_page_after_first_transport_finishes() -> None:
    budget = make_budget(task_attempts=2, source_attempts=2)
    session = FakeSession((FakeResponse(), FakeResponse()))
    session.block_first = True
    fetcher = TinyDoubleRequestFetcher(make_client(session))

    async def run_timeout() -> None:
        call = asyncio.create_task(
            fetcher.fetch_async(ctx=FetchContext(timeout=0.04, request_budget=budget))
        )
        assert await asyncio.to_thread(session.started.wait, 1)
        with pytest.raises(AsyncExecutionTimeoutError):
            await call

    asyncio.run(run_timeout())
    session.release.set()
    assert fetcher.finished.wait(timeout=1)
    assert len(session.calls) == 1
    assert budget.attempts_used == 1


def test_same_source_budget_aggregates_two_model_grants_and_two_clients() -> None:
    budget = make_budget(
        models=("TinyModel", "OtherModel"),
        task_attempts=2,
        source_attempts=1,
    )
    first_session = FakeSession((FakeResponse(),))
    second_session = FakeSession((FakeResponse(),))
    first = make_client(first_session)
    second = make_client(second_session)

    with request_execution_scope(
        source="budget_test",
        canonical_model="TinyModel",
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        first.get(URL, source="budget_test")
    with (
        request_execution_scope(
            source="budget_test",
            canonical_model="OtherModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAttemptLimitError),
    ):
        second.get(URL, source="budget_test")

    assert len(first_session.calls) == 1
    assert second_session.calls == []
    assert budget.attempts_used_for("budget_test") == 1


def test_two_clients_sharing_one_budget_serialize_same_host_transport() -> None:
    budget = make_budget(
        models=("TinyModel", "OtherModel"),
        task_attempts=2,
        source_attempts=2,
    )
    gate_started = threading.Event()
    release = threading.Event()
    active_lock = threading.Lock()
    active = 0
    maximum_active = 0

    class ObservedSession(FakeSession):
        def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                gate_started.set()
            if len(self.calls) == 0:
                release.wait(timeout=2)
            try:
                return super().request(method, url, **kwargs)
            finally:
                with active_lock:
                    active -= 1

    clients = (
        make_client(ObservedSession((FakeResponse(),))),
        make_client(ObservedSession((FakeResponse(),))),
    )

    def issue(client: GovernedHttpClient, model: str) -> None:
        with request_execution_scope(
            source="budget_test",
            canonical_model=model,
            operation=RequestOperation.QUERY,
            budget=budget,
        ):
            client.get(URL, source="budget_test")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(issue, clients[0], "TinyModel")
        assert gate_started.wait(timeout=1)
        second = pool.submit(issue, clients[1], "OtherModel")
        time.sleep(0.05)
        assert maximum_active == 1
        release.set()
        first.result(timeout=1)
        second.result(timeout=1)

    assert maximum_active == 1
    assert budget.attempts_used == 2


def test_cache_requires_grant_before_read_and_does_not_consume_an_attempt() -> None:
    class SpyCache:
        def __init__(self) -> None:
            self.reads = 0

        def get(self, **_kwargs: object) -> CachedRawResponse:
            self.reads += 1
            now = time.time()
            return CachedRawResponse(200, b"cached", "utf-8", now, now + 10)

    cache = SpyCache()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        sleep=lambda _seconds: None,
        raw_response_cache=cast("Any", cache),
    )
    no_grant = RequestBudget(task_attempts=1, source_attempts=1)

    with (
        request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=no_grant,
        ),
        pytest.raises(RequestAuthorizationError),
    ):
        client.get(URL, source="budget_test")
    assert cache.reads == 0

    grant = make_budget(task_attempts=1, source_attempts=1)
    with request_execution_scope(
        source="budget_test",
        canonical_model="TinyModel",
        operation=RequestOperation.QUERY,
        budget=grant,
    ):
        response = client.get(URL, source="budget_test")
    assert response.content == b"cached"
    assert cache.reads == 1
    assert grant.attempts_used == 0


def test_redirects_are_disabled_and_rejected_without_cache_or_followup() -> None:
    budget = make_budget(task_attempts=2, source_attempts=2)
    session = FakeSession((FakeResponse(302, b"redirect"), FakeResponse()))
    client = make_client(session)

    with (
        request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestRedirectError),
    ):
        client.get(URL, source="budget_test")

    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False
    assert budget.attempts_used == 1


def test_scoped_requests_reject_adapter_retries_before_transport() -> None:
    budget = make_budget(task_attempts=1, source_attempts=1)
    session = requests.Session()
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=1))
    client = make_client(session)
    try:
        with (
            request_execution_scope(
                source="budget_test",
                canonical_model="TinyModel",
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(RequestAdapterRetryError),
        ):
            client.get(URL, source="budget_test")
    finally:
        session.close()
    assert budget.attempts_used == 0


def test_scoped_requests_reject_digest_auth_before_offline_adapter_send() -> None:
    probe_session = requests.Session()
    probe_adapter = OfflineAdapter((401, 200))
    probe_session.mount("https://", probe_adapter)
    probe_session.auth = requests.auth.HTTPDigestAuth("offline-user", "offline-password")
    try:
        assert probe_session.get(URL).status_code == 200
        assert len(probe_adapter.calls) == 2
    finally:
        probe_session.close()

    budget = make_budget(task_attempts=1, source_attempts=1)
    session = requests.Session()
    adapter = OfflineAdapter((401, 200))
    session.mount("https://", adapter)
    session.auth = requests.auth.HTTPDigestAuth("offline-user", "offline-password")
    client = make_client(session)
    try:
        with (
            request_execution_scope(
                source="budget_test",
                canonical_model="TinyModel",
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(
                RequestAuthorizationError,
                match="unsupported session authentication",
            ) as rejected,
        ):
            client.get(URL, source="budget_test")
    finally:
        session.close()

    assert adapter.calls == []
    assert budget.attempts_used == 0
    assert "offline-user" not in str(rejected.value)
    assert "offline-password" not in str(rejected.value)


def test_scoped_requests_reject_response_hook_that_would_reissue() -> None:
    def install_reissue_hook(session: requests.Session) -> None:
        already_reissued = False

        def reissue_once(
            response: requests.Response,
            **_kwargs: object,
        ) -> requests.Response:
            nonlocal already_reissued
            if not already_reissued:
                already_reissued = True
                session.get(URL)
            return response

        session.hooks["response"].append(reissue_once)

    probe_session = requests.Session()
    probe_adapter = OfflineAdapter((200, 200))
    probe_session.mount("https://", probe_adapter)
    install_reissue_hook(probe_session)
    try:
        assert probe_session.get(URL).status_code == 200
        assert len(probe_adapter.calls) == 2
    finally:
        probe_session.close()

    budget = make_budget(task_attempts=1, source_attempts=1)
    session = requests.Session()
    adapter = OfflineAdapter((200, 200))
    session.mount("https://", adapter)
    install_reissue_hook(session)
    client = make_client(session)
    try:
        with (
            request_execution_scope(
                source="budget_test",
                canonical_model="TinyModel",
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(RequestAuthorizationError, match="session response hooks"),
        ):
            client.get(URL, source="budget_test")
    finally:
        session.close()

    assert adapter.calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize("auth_kind", ["none", "basic-auth", "basic-tuple"])
def test_scoped_requests_allow_default_and_pure_basic_session_auth(auth_kind: str) -> None:
    budget = make_budget(task_attempts=1, source_attempts=1)
    session = requests.Session()
    adapter = OfflineAdapter((200,))
    session.mount("https://", adapter)
    if auth_kind == "basic-auth":
        session.auth = requests.auth.HTTPBasicAuth("offline-user", "offline-password")
    elif auth_kind == "basic-tuple":
        session.auth = ("offline-user", "offline-password")
    client = make_client(session)
    try:
        with request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ):
            response = client.get(URL, source="budget_test")
    finally:
        session.close()

    assert response.status_code == 200
    assert len(adapter.calls) == 1
    assert budget.attempts_used == 1


def test_half_open_budget_rejection_releases_slot_and_allows_later_probe() -> None:
    clock = [100.0]
    adapter = OfflineAdapter((429, 200))
    session = requests.Session()
    session.mount("https://", adapter)
    client = make_breaker_client(session, lambda: clock[0])
    try:
        open_rate_limit_breaker(client)
        breaker = client._breakers[HOST]
        opened_at = breaker.opened_at
        category = breaker.category
        assert opened_at == 100.0
        assert category is FailureCategory.RATE_LIMITED
        assert len(adapter.calls) == 1

        clock[0] += 6.0
        grant = make_grant(task_attempts=1, source_attempts=1)
        zero_budget = RequestBudget(
            task_attempts=0,
            source_attempts=0,
            grants=(grant,),
        )
        with (
            request_execution_scope(
                source="budget_test",
                canonical_model="TinyModel",
                operation=RequestOperation.QUERY,
                budget=zero_budget,
            ),
            pytest.raises(RequestAttemptLimitError),
        ):
            client.get(URL, source="budget_test")

        assert zero_budget.attempts_used == 0
        assert len(adapter.calls) == 1
        assert breaker.opened_at == opened_at
        assert breaker.category is category
        assert breaker.probe_in_flight is False

        later_budget = make_budget(task_attempts=1, source_attempts=1)
        with request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=later_budget,
        ):
            response = client.get(URL, source="budget_test")
        assert response.status_code == 200
        assert len(adapter.calls) == 2
        assert later_budget.attempts_used == 1
        assert HOST not in client._breakers
    finally:
        session.close()


@pytest.mark.parametrize(
    ("stop_kind", "expected_error"),
    [
        ("cancel", RequestExecutionCancelledError),
        ("deadline", RequestExecutionDeadlineError),
    ],
)
def test_half_open_liveness_rejection_releases_slot_for_next_probe(
    monkeypatch: pytest.MonkeyPatch,
    stop_kind: str,
    expected_error: type[Exception],
) -> None:
    clock = [200.0]
    adapter = OfflineAdapter((429, 200))
    session = requests.Session()
    session.mount("https://", adapter)
    client = make_breaker_client(session, lambda: clock[0])
    try:
        open_rate_limit_breaker(client)
        breaker = client._breakers[HOST]
        opened_at = breaker.opened_at
        category = breaker.category
        clock[0] += 6.0

        cancellation = threading.Event()

        @contextmanager
        def stop_after_claimed_slot(_host: str, _scopes: object) -> Iterator[None]:
            if stop_kind == "cancel":
                cancellation.set()
            else:
                time.sleep(0.06)
            yield

        grant = make_grant(task_attempts=1, source_attempts=1)
        budget = RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))
        deadline = time.monotonic() + 0.03 if stop_kind == "deadline" else None
        with monkeypatch.context() as patcher:
            patcher.setattr(client, "_scoped_semaphore", stop_after_claimed_slot)
            with (
                request_execution_scope(
                    source="budget_test",
                    canonical_model="TinyModel",
                    operation=RequestOperation.QUERY,
                    budget=budget,
                    deadline=deadline,
                    cancellation=cancellation if stop_kind == "cancel" else None,
                ),
                pytest.raises(expected_error),
            ):
                client.get(URL, source="budget_test")

        assert budget.attempts_used == 0
        assert len(adapter.calls) == 1
        assert breaker.opened_at == opened_at
        assert breaker.category is category
        assert breaker.probe_in_flight is False

        later_budget = make_budget(task_attempts=1, source_attempts=1)
        with request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=later_budget,
        ):
            response = client.get(URL, source="budget_test")
        assert response.status_code == 200
        assert len(adapter.calls) == 2
        assert later_budget.attempts_used == 1
        assert HOST not in client._breakers
    finally:
        session.close()


def test_registry_provider_model_uses_real_fred_client_and_injected_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_body = {
        "count": 1,
        "offset": 0,
        "seriess": [
            {
                "id": "GDP",
                "title": "Gross Domestic Product",
                "frequency": "Quarterly",
                "units": "Billions of Dollars",
                "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
                "observation_start": "1947-01-01",
                "observation_end": "2025-01-01",
                "last_updated": "2025-01-01 00:00:00-0500",
            }
        ],
    }
    session = FakeSession((FakeResponse(content=json.dumps(response_body).encode()),))
    client = make_client(session)
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    monkeypatch.setattr(fred_client, "get_shared_http_client", lambda: client)
    registry = ProviderRegistry()
    register_provider("fred", registry)
    fetcher = registry.resolve_model("fred", "FredSearch")
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(
            RequestGrant(
                source="fred",
                canonical_model="FredSearch",
                operation=RequestOperation.QUERY,
                decision=GrantDecision.ALLOWED,
                rights_evidence="test-evidence:fred-search-offline",
                task_attempts=1,
                source_attempts=1,
                allowed_hosts={"api.stlouisfed.org"},
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            ),
        ),
    )

    result = asyncio.run(
        fetcher.fetch_async(ctx=FetchContext(timeout=2, request_budget=budget), search_text="GDP")
    )

    assert len(result) == 1
    assert result[0].series_id == "GDP"
    assert len(session.calls) == 1
    assert session.calls[0][1].startswith("https://api.stlouisfed.org/")
    assert session.calls[0][2]["allow_redirects"] is False
    assert budget.attempts_used == 1


def test_explicit_budget_without_canonical_identity_fails_closed() -> None:
    budget = make_budget()
    session = FakeSession((FakeResponse(),))
    client = make_client(session)

    with (
        request_execution_scope(
            source="budget_test",
            canonical_model=None,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError, match="canonical model"),
    ):
        client.get(URL, source="budget_test")

    assert session.calls == []


def test_scope_isolated_across_async_tasks() -> None:
    first = make_budget(task_attempts=1, source_attempts=1)
    second = make_budget(task_attempts=1, source_attempts=1)

    async def read_budget(budget: RequestBudget) -> RequestBudget:
        with request_execution_scope(
            source="budget_test",
            canonical_model="TinyModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ):
            await asyncio.sleep(0)
            return current_request_scopes()[-1].budget

    async def run() -> tuple[RequestBudget, RequestBudget]:
        return await asyncio.gather(read_budget(first), read_budget(second))

    results = asyncio.run(run())
    assert results == [first, second]
