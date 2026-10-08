"""Request-budget errors retain their type across provider HTTP boundaries."""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests

import opendata.data.http_client as http_client_module
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.bls.models import _client as bls_client
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
)

FAKE_FMP_KEY = "OFFLINE_FAKE_FMP_KEY"
BLS_SEARCH_HOST = "download.bls.gov"
BLS_SERIES_HOST = "api.bls.gov"
FMP_HOST = "financialmodelingprep.com"
URL = "https://api.example.test/data"


class SocketFreeAdapter(requests.adapters.BaseAdapter):
    """Replay a response or request exception without opening a socket."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(
        self,
        statuses: tuple[int, ...] = (200,),
        *,
        failure: requests.RequestException | None = None,
    ) -> None:
        self._statuses = deque(statuses)
        self.failure = failure
        self.calls: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        status = self._statuses.popleft() if self._statuses else 200
        response = requests.Response()
        response.status_code = status
        response._content = b"{}"
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "application/json"
        response.url = request.url
        response.request = request
        response.connection = self
        return response

    def close(self) -> None:
        """This adapter owns no resources."""


@pytest.fixture(autouse=True)
def disable_external_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep provider tests offline and independent from process cache state."""
    monkeypatch.setenv("BLS_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", FAKE_FMP_KEY)
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)


def resolve_fetcher(source: str, model: str) -> Fetcher[Any, Any]:
    """Resolve one actual provider model through its registered metadata."""
    registry = ProviderRegistry()
    register_provider(source, registry)
    return registry.resolve_model(source, model)


def make_grant(source: str, model: str, host: str, *, attempts: int = 1) -> RequestGrant:
    """Construct explicit, short-lived offline fixture authority."""
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:provider-budget-error-boundary",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def make_budget(
    source: str,
    model: str,
    host: str,
    *,
    limit: int,
    grant_host: str | None = None,
) -> RequestBudget:
    """Return a bounded budget whose grant can be scoped to a different host."""
    return RequestBudget(
        task_attempts=limit,
        source_attempts=limit,
        grants=(make_grant(source, model, grant_host or host, attempts=max(limit, 1)),),
    )


def make_governed_client(
    monkeypatch: pytest.MonkeyPatch,
    adapter: SocketFreeAdapter,
) -> tuple[GovernedHttpClient, requests.Session]:
    """Install a real governed client over a socket-free requests session."""
    session = requests.Session()
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=1,
            rate_limit_per_host=None,
            breaker_threshold=3,
            backoff_base=0,
            backoff_jitter=0,
        ),
        session=session,
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )
    monkeypatch.setattr(bls_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    return client, session


PROVIDER_CASES: tuple[tuple[str, str, dict[str, object], str], ...] = (
    (
        "bls",
        "BlsSearch",
        {"survey": "ce", "search_text": "consumer prices"},
        BLS_SEARCH_HOST,
    ),
    (
        "bls",
        "BlsSeries",
        {
            "series_ids": ["CUUR0000SA0"],
            "start_year": 2023,
            "end_year": 2023,
            "max_requests": 1,
        },
        BLS_SERIES_HOST,
    ),
    (
        "fmp",
        "EquityQuote",
        {"symbol": "AAPL"},
        FMP_HOST,
    ),
    (
        "fmp",
        "EquityHistorical",
        {
            "symbol": "AAPL",
            "start_date": "2025-01-02",
            "end_date": "2025-01-03",
            "max_requests": 1,
            "max_records": 10,
        },
        FMP_HOST,
    ),
)


@pytest.mark.parametrize(
    ("source", "model", "query", "host"),
    PROVIDER_CASES,
    ids=("bls-search-get", "bls-series-post", "fmp-quote", "fmp-historical"),
)
def test_actual_provider_models_propagate_zero_attempt_budget(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    query: dict[str, object],
    host: str,
) -> None:
    adapter = SocketFreeAdapter()
    _, session = make_governed_client(monkeypatch, adapter)
    budget = make_budget(source, model, host, limit=0)
    fetcher = resolve_fetcher(source, model)
    try:
        with pytest.raises(RequestAttemptLimitError):
            fetcher.fetch(ctx=FetchContext(request_budget=budget), **query)
    finally:
        session.close()

    assert adapter.calls == []
    assert budget.attempts_used == 0
    assert budget.attempts_used_for(source) == 0


def test_actual_provider_client_propagates_host_authorization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SocketFreeAdapter()
    _, session = make_governed_client(monkeypatch, adapter)
    budget = make_budget("fmp", "EquityQuote", FMP_HOST, limit=1, grant_host="other.example")
    fetcher = resolve_fetcher("fmp", "EquityQuote")
    try:
        with pytest.raises(RequestAuthorizationError):
            fetcher.fetch(ctx=FetchContext(request_budget=budget), symbol="AAPL")
    finally:
        session.close()

    assert adapter.calls == []
    assert budget.attempts_used == 0


def test_actual_provider_client_propagates_deadline_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SocketFreeAdapter()
    client, session = make_governed_client(monkeypatch, adapter)
    budget = make_budget("bls", "BlsSearch", BLS_SEARCH_HOST, limit=1)

    def delay_until_deadline(_host: str, _url: str) -> None:
        time.sleep(0.08)

    monkeypatch.setattr(client, "_acquire_token", delay_until_deadline)
    fetcher = resolve_fetcher("bls", "BlsSearch")
    try:
        with pytest.raises(RequestExecutionDeadlineError):
            fetcher.fetch(
                ctx=FetchContext(timeout=0.04, request_budget=budget),
                survey="ce",
                search_text="consumer prices",
            )
    finally:
        session.close()

    assert adapter.calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("source", "model", "query", "host"),
    PROVIDER_CASES[:3],
    ids=("bls-get-429", "bls-post-429", "fmp-get-429"),
)
def test_real_governed_http_errors_keep_provider_classification(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    query: dict[str, object],
    host: str,
) -> None:
    adapter = SocketFreeAdapter((429,))
    _, session = make_governed_client(monkeypatch, adapter)
    budget = make_budget(source, model, host, limit=1)
    fetcher = resolve_fetcher(source, model)
    try:
        if source == "bls":
            from opendata.data.providers.bls.models._client import BlsProviderError

            with pytest.raises(BlsProviderError) as rejected:
                fetcher.fetch(ctx=FetchContext(request_budget=budget), **query)
            assert rejected.value.code == "BLS_HTTP_ERROR"
        else:
            from opendata.data.providers.fmp.models._client import FMPProviderError

            with pytest.raises(FMPProviderError) as rejected:
                fetcher.fetch(ctx=FetchContext(request_budget=budget), **query)
            assert rejected.value.code == "FMP_RATE_LIMITED"
            assert FAKE_FMP_KEY not in str(rejected.value)
    finally:
        session.close()

    assert rejected.value.status == 429
    assert len(adapter.calls) == 1
    assert budget.attempts_used == 1


def test_fmp_generic_exception_containing_fake_key_stays_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaisingClient:
        def get(self, *_args: object, **_kwargs: object) -> requests.Response:
            raise RuntimeError(f"offline transport URL contained apikey={FAKE_FMP_KEY}")

    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: RaisingClient())
    fetcher = resolve_fetcher("fmp", "EquityQuote")
    budget = make_budget("fmp", "EquityQuote", FMP_HOST, limit=1)
    with pytest.raises(fmp_client.FMPProviderError) as rejected:
        fetcher.fetch(ctx=FetchContext(request_budget=budget), symbol="AAPL")

    assert rejected.value.code == "FMP_TRANSPORT_ERROR"
    assert FAKE_FMP_KEY not in str(rejected.value)
    assert rejected.value.__cause__ is None
    assert rejected.value.__context__ is None
    assert budget.attempts_used == 0
