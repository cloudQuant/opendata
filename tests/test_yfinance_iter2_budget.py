"""Central-route budget boundaries for yfinance's direct Ticker fetchers."""

from __future__ import annotations

import asyncio
import json
import time
import types
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.yfinance.models import _sdk, _session
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAdapterRetryError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    RequestRedirectError,
    request_execution_scope,
)
from opendata.services import provider_model_query
from opendata.services.provider_model_query import ProviderModelQueryBudgetError

_EXPIRATION = (datetime.now(timezone.utc).date() + timedelta(days=90)).isoformat()

_DIRECT_TICKER_CASES = (
    ("BalanceSheet", {"symbol": "AAPL", "period": "annual"}, "get_balance_sheet"),
    ("CashFlowStatement", {"symbol": "AAPL", "period": "annual"}, "get_cashflow"),
    ("IncomeStatement", {"symbol": "AAPL", "period": "annual"}, "get_income_stmt"),
    ("HistoricalDividends", {"symbol": "AAPL"}, "dividends"),
    ("KeyExecutives", {"symbol": "AAPL"}, "get_info"),
    (
        "OptionsChains",
        {"symbol": "AAPL", "expiration_date": _EXPIRATION},
        "option_chain",
    ),
)


@pytest.mark.parametrize(("model", "query", "sdk_method"), _DIRECT_TICKER_CASES)
@pytest.mark.parametrize("allowed_attempts", [0, 1])
def test_direct_ticker_routes_reserve_before_sdk_access_and_serialize(
    model: str,
    query: dict[str, object],
    sdk_method: str,
    allowed_attempts: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yfinance as installed_yfinance

    events: list[tuple[str, str]] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def close(self) -> None:
            return None

    def send_once(
        _session_obj: object,
        _method: str,
        url: str,
        *_args: object,
        **kwargs: object,
    ) -> Response:
        assert kwargs["allow_redirects"] is False
        events.append(("send", url))
        return Response()

    monkeypatch.setattr(_session, "_SESSION", None)
    monkeypatch.setattr(_session, "_send_once", send_once)
    monkeypatch.setattr(installed_yfinance.data.YfData, "_instances", {})

    class OptionChain:
        @property
        def calls(self) -> pd.DataFrame:
            events.append(("local_property", "calls"))
            return pd.DataFrame({"contractSymbol": ["AAPL-CALL"], "strike": [100.0]})

        @property
        def puts(self) -> pd.DataFrame:
            events.append(("local_property", "puts"))
            return pd.DataFrame({"contractSymbol": ["AAPL-PUT"], "strike": [90.0]})

    class Ticker:
        def __init__(self, symbol: str, *, session: object) -> None:
            events.append(("Ticker", symbol))
            self.session = session

        def _call(self, method: str) -> None:
            events.append(("sdk_method", method))
            self.session.get("https://query1.finance.yahoo.com/v1/test", timeout=2)

        def get_balance_sheet(self, **_kwargs: object) -> pd.DataFrame:
            self._call("get_balance_sheet")
            return _statement_response()

        def get_cashflow(self, **_kwargs: object) -> pd.DataFrame:
            self._call("get_cashflow")
            return _statement_response()

        def get_income_stmt(self, **_kwargs: object) -> pd.DataFrame:
            self._call("get_income_stmt")
            return _statement_response()

        @property
        def dividends(self) -> pd.Series:
            self._call("dividends")
            return pd.Series([0.25], index=pd.DatetimeIndex(["2024-06-14"]), name="Dividends")

        def get_info(self) -> dict[str, object]:
            self._call("get_info")
            return {
                "companyOfficers": [{"name": "Example Officer", "title": "Chief Executive Officer"}]
            }

        def option_chain(self, expiration: str) -> OptionChain:
            self._call(f"option_chain:{expiration}")
            return OptionChain()

    fake_sdk = types.SimpleNamespace(
        __version__=installed_yfinance.__version__,
        config=installed_yfinance.config,
        data=installed_yfinance.data,
        Ticker=Ticker,
    )
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: fake_sdk)
    registry = ProviderRegistry()
    register_provider("yfinance", registry)
    fetcher = registry.resolve_model("yfinance", model)
    capability = fetcher.capability
    transformed_query: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        transformed_query.update(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    grant = RequestGrant(
        source="yfinance",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-yfinance-sdk",
        task_attempts=allowed_attempts,
        source_attempts=allowed_attempts,
        allowed_hosts=frozenset({"query1.finance.yahoo.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=allowed_attempts,
        source_attempts=allowed_attempts,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    context = FetchContext(timeout=2.0, request_budget=budget)

    if allowed_attempts == 0:
        with pytest.raises(ProviderModelQueryBudgetError):
            asyncio.run(
                provider_model_query.query_provider_model(
                    registry=registry,
                    source="yfinance",
                    model=model,
                    query=query,
                    principal=Principal(),
                    ctx=context,
                )
            )
        assert events[0] == ("Ticker", "AAPL")
        assert not any(event[0] == "send" for event in events)
        assert any(event[0] == "sdk_method" or event[0] == "sdk_property" for event in events)
        assert budget.attempts_used == 0
        assert budget.attempts_used_for("yfinance") == 0
        return

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="yfinance",
            model=model,
            query=query,
            principal=Principal(),
            ctx=context,
        )
    )
    assert transformed_query["source"] == "yfinance"
    assert transformed_query["market"] == "global"
    assert response["source"] == "yfinance"
    assert response["model"] == model
    assert response["results"]
    json.dumps(response, allow_nan=False)
    sends_after_first_query = [event for event in events if event[0] == "send"]
    assert len(sends_after_first_query) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for("yfinance") == 1
    assert events[0] == ("Ticker", "AAPL")
    assert sum(event[0] == "sdk_method" or event[0] == "sdk_property" for event in events) == 1
    if model == "OptionsChains":
        assert [event for event in events if event[0] == "local_property"] == [
            ("local_property", "calls"),
            ("local_property", "puts"),
        ]

    # A repeated/cached Ticker property or endpoint access is a new source attempt.
    calls_after_first_query = list(events)
    with pytest.raises(ProviderModelQueryBudgetError):
        asyncio.run(
            provider_model_query.query_provider_model(
                registry=registry,
                source="yfinance",
                model=model,
                query=query,
                principal=Principal(),
                ctx=context,
            )
        )
    assert events[: len(calls_after_first_query)] == calls_after_first_query
    assert sum(event[0] == "send" for event in events) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for("yfinance") == 1


def _statement_response() -> pd.DataFrame:
    return pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [100.0]},
        index=["Total Assets"],
    )


class _TransportResponse:
    def __init__(self, status_code: int = 200, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"Location": location} if location is not None else {}
        self.url = "https://query1.finance.yahoo.com/v1/test"
        self.text = "fixture"
        self.content = b"fixture"

    def close(self) -> None:
        return None


def _transport_budget(
    *,
    attempts: int,
    hosts: tuple[str, ...] = ("query1.finance.yahoo.com",),
    model: str = "EquityHistorical",
) -> RequestBudget:
    grant = RequestGrant(
        source="yfinance",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:yfinance-transport",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=hosts,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )


def _isolated_session(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    import yfinance

    monkeypatch.setattr(_session, "_SESSION", None)
    monkeypatch.setattr(yfinance.data.YfData, "_instances", {})
    with _session.governed_yfinance_session(yfinance) as session:
        assert session is not None
        return yfinance, session


def test_governed_sdk_call_rejects_sdk_without_a_supported_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_calls: list[bool] = []
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: types.SimpleNamespace())

    @_sdk._governed_sdk_call
    def sdk_body() -> None:
        body_calls.append(True)

    with pytest.raises(YfinanceProviderError, match="YFINANCE_UNSUPPORTED_SDK_VERSION"):
        sdk_body()
    assert body_calls == []


@pytest.mark.parametrize(
    "third_url",
    [
        "https://query2.finance.yahoo.com/v1/third",
        "https://query1.finance.yahoo.com/v1/third",
    ],
    ids=["same-redirect-host", "return-to-original-host"],
)
def test_redirect_chain_never_restores_source_credentials_before_completion(
    third_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    responses = [
        _TransportResponse(302, "https://query2.finance.yahoo.com/v1/second"),
        _TransportResponse(302, third_url),
        _TransportResponse(),
    ]
    sent: list[dict[str, object]] = []

    def send_once(
        session_obj: Any,
        _method: str,
        url: str,
        *_args: object,
        **kwargs: object,
    ) -> _TransportResponse:
        sent.append(
            {
                "url": url,
                "session_headers": dict(session_obj.headers),
                "session_cookie_count": len(session_obj.cookies),
                "session_auth": session_obj.auth,
                "session_proxy_auth": session_obj.proxy_auth,
                "headers": kwargs.get("headers"),
                "cookies": kwargs.get("cookies"),
                "auth": kwargs.get("auth"),
                "proxy_auth": kwargs.get("proxy_auth"),
            }
        )
        return responses.pop(0)

    monkeypatch.setattr(_session, "_send_once", send_once)
    session.headers.update(
        {
            "Authorization": "session-secret",
            "Host": "query1.finance.yahoo.com",
            "X-API-Key": "session-key",
        }
    )
    session.cookies.set("session-secret", "secret-value", domain=".yahoo.com", secure=True)
    session.auth = ("session-user", "session-password")
    session.proxy_auth = ("proxy-user", "proxy-password")
    original_headers = dict(session.headers)
    original_auth = session.auth
    original_proxy_auth = session.proxy_auth
    budget = _transport_budget(
        attempts=3,
        hosts=("query1.finance.yahoo.com", "query2.finance.yahoo.com"),
    )

    with request_execution_scope(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        response = session.get(
            "https://query1.finance.yahoo.com/v1/start",
            headers={
                "Authorization": "request-secret",
                "Cookie": "request=secret",
                "Host": "query1.finance.yahoo.com",
                "X-API-Key": "request-key",
            },
            cookies={"request-cookie": "request-value"},
            auth=("request-user", "request-password"),
            proxy_auth=("request-proxy-user", "request-proxy-password"),
            timeout=5,
        )

    assert response.status_code == 200
    assert [entry["url"] for entry in sent] == [
        "https://query1.finance.yahoo.com/v1/start",
        "https://query2.finance.yahoo.com/v1/second",
        third_url,
    ]
    initial_session_headers = {
        str(key).lower(): value for key, value in sent[0]["session_headers"].items()
    }
    assert initial_session_headers["authorization"] == "session-secret"
    for entry in sent[1:]:
        session_headers = entry["session_headers"]
        request_headers = entry["headers"]
        assert isinstance(session_headers, Mapping)
        assert isinstance(request_headers, Mapping)
        assert not {str(key).lower() for key in session_headers}.intersection(
            {"authorization", "cookie", "proxy-authorization", "host", "x-api-key"}
        )
        assert not {str(key).lower() for key in request_headers}.intersection(
            {"authorization", "cookie", "proxy-authorization", "host", "x-api-key"}
        )
        assert entry["session_cookie_count"] == 0
        assert entry["session_auth"] is None
        assert entry["session_proxy_auth"] is None
        assert entry["cookies"] is None
        assert entry["auth"] is None
        assert entry["proxy_auth"] is None
    assert dict(session.headers) == original_headers
    assert len(session.cookies) == 1
    assert session.auth == original_auth
    assert session.proxy_auth == original_proxy_auth
    assert budget.attempts_used == 3


def test_redirect_chain_restores_session_credentials_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    sent: list[str] = []

    def send_once(
        _session_obj: Any,
        _method: str,
        url: str,
        *_args: object,
        **_kwargs: object,
    ) -> _TransportResponse:
        sent.append(url)
        if len(sent) == 1:
            return _TransportResponse(302, "https://query2.finance.yahoo.com/v1/second")
        raise OSError("offline transport fixture")

    monkeypatch.setattr(_session, "_send_once", send_once)
    session.headers.update({"Authorization": "session-secret", "Host": "query1.finance.yahoo.com"})
    session.cookies.set("session-secret", "secret-value", domain=".yahoo.com", secure=True)
    session.auth = ("session-user", "session-password")
    session.proxy_auth = ("proxy-user", "proxy-password")
    original_headers = dict(session.headers)
    original_auth = session.auth
    original_proxy_auth = session.proxy_auth
    budget = _transport_budget(
        attempts=2,
        hosts=("query1.finance.yahoo.com", "query2.finance.yahoo.com"),
    )
    with (
        request_execution_scope(
            source="yfinance",
            canonical_model="EquityHistorical",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(OSError, match="offline transport fixture"),
    ):
        session.get("https://query1.finance.yahoo.com/v1/start")

    assert sent == [
        "https://query1.finance.yahoo.com/v1/start",
        "https://query2.finance.yahoo.com/v1/second",
    ]
    assert dict(session.headers) == original_headers
    assert len(session.cookies) == 1
    assert session.auth == original_auth
    assert session.proxy_auth == original_proxy_auth
    assert budget.attempts_used == 2


def test_transport_counts_each_real_send_and_reauthorizes_redirect_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    sends: list[tuple[str, object, int]] = []
    responses = [
        _TransportResponse(302, "https://query2.finance.yahoo.com/v1/next"),
        _TransportResponse(),
    ]

    def send_once(
        session_obj: Any, method: str, url: str, *_args: object, **kwargs: object
    ) -> _TransportResponse:
        sends.append((url, kwargs.get("headers"), len(session_obj.cookies)))
        assert kwargs["allow_redirects"] is False
        return responses.pop(0)

    monkeypatch.setattr(_session, "_send_once", send_once)
    session.headers["Authorization"] = "session-secret"
    session.cookies.set("session-secret", "secret-value", domain=".yahoo.com", secure=True)
    budget = _transport_budget(
        attempts=2,
        hosts=("query1.finance.yahoo.com", "query2.finance.yahoo.com"),
    )
    with request_execution_scope(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        response = session.get(
            "https://query1.finance.yahoo.com/v1/start",
            headers={"Authorization": "request-secret", "Cookie": "request=secret"},
            timeout=5,
        )
    assert response.status_code == 200
    assert [url for url, _headers, _cookies in sends] == [
        "https://query1.finance.yahoo.com/v1/start",
        "https://query2.finance.yahoo.com/v1/next",
    ]
    assert sends[0][2] == 1
    assert sends[1][2] == 0
    redirected_headers = sends[1][1]
    assert isinstance(redirected_headers, Mapping)
    assert not {str(key).lower() for key in redirected_headers}.intersection(
        {"authorization", "cookie", "proxy-authorization"}
    )
    assert session.headers["Authorization"] == "session-secret"
    assert len(session.cookies) == 1
    assert budget.attempts_used == 2


def test_transport_denies_ungranted_consent_redirect_before_second_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    sends: list[str] = []

    def send_once(
        _session_obj: Any, _method: str, url: str, *_args: object, **_kwargs: object
    ) -> _TransportResponse:
        sends.append(url)
        return _TransportResponse(302, "https://consent.yahoo.com/collect")

    monkeypatch.setattr(_session, "_send_once", send_once)
    budget = _transport_budget(attempts=3)
    with (
        request_execution_scope(
            source="yfinance",
            canonical_model="EquityHistorical",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError),
    ):
        session.get("https://query1.finance.yahoo.com/v1/start")
    assert sends == ["https://query1.finance.yahoo.com/v1/start"]
    assert budget.attempts_used == 1


def test_transport_rejects_retry_replay_and_clamps_timeout_to_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    sends: list[tuple[str, object]] = []

    def send_once(
        _session_obj: Any, method: str, url: str, *_args: object, **kwargs: object
    ) -> _TransportResponse:
        sends.append((method, kwargs.get("timeout")))
        return _TransportResponse(307, "https://query1.finance.yahoo.com/v1/replay")

    monkeypatch.setattr(_session, "_send_once", send_once)
    retry_strategy = type(session.retry)(count=1)
    monkeypatch.setattr(session, "retry", retry_strategy)
    retry_budget = _transport_budget(attempts=1)
    with (
        request_execution_scope(
            source="yfinance",
            canonical_model="EquityHistorical",
            operation=RequestOperation.QUERY,
            budget=retry_budget,
        ),
        pytest.raises(RequestAdapterRetryError),
    ):
        session.get("https://query1.finance.yahoo.com/v1/retry")
    assert sends == []
    assert retry_budget.attempts_used == 0
    monkeypatch.setattr(session, "retry", type(session.retry)(count=0))

    budget = _transport_budget(attempts=2)
    deadline = time.monotonic() + 1.0
    with (
        request_execution_scope(
            source="yfinance",
            canonical_model="EquityHistorical",
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
        ),
        pytest.raises(RequestRedirectError, match="unsafe method"),
    ):
        session.post(
            "https://query1.finance.yahoo.com/v1/start",
            json={"payload": "must not replay"},
            timeout=(10.0, 20.0),
        )
    assert len(sends) == 1
    assert sends[0][0] == "POST"
    assert isinstance(sends[0][1], tuple)
    assert all(0 < value <= 1.0 for value in sends[0][1])
    assert budget.attempts_used == 1


def test_transport_detects_yfdata_session_replacement_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yfinance, session = _isolated_session(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        _session,
        "_send_once",
        lambda _session_obj, _method, url, **_kwargs: sent.append(url) or _TransportResponse(),
    )
    budget = _transport_budget(attempts=1)
    data = yfinance.data.YfData._instances[yfinance.data.YfData]
    original = data._session
    data._session = object()
    try:
        with (
            request_execution_scope(
                source="yfinance",
                canonical_model="EquityHistorical",
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(RequestAuthorizationError, match="replaced"),
        ):
            session.get("https://query1.finance.yahoo.com/v1/test")
    finally:
        data._session = original
    assert sent == []
    assert budget.attempts_used == 0


def test_concurrent_request_scopes_keep_attempts_in_their_own_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _yfinance, session = _isolated_session(monkeypatch)
    sent: list[str] = []

    def send_once(
        _session_obj: Any, _method: str, url: str, *_args: object, **_kwargs: object
    ) -> _TransportResponse:
        time.sleep(0.01)
        sent.append(url)
        return _TransportResponse()

    monkeypatch.setattr(_session, "_send_once", send_once)
    budgets = [_transport_budget(attempts=1), _transport_budget(attempts=1)]

    def fetch(budget: RequestBudget, suffix: str) -> None:
        with request_execution_scope(
            source="yfinance",
            canonical_model="EquityHistorical",
            operation=RequestOperation.QUERY,
            budget=budget,
        ):
            session.get(f"https://query1.finance.yahoo.com/v1/{suffix}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(fetch, budget, str(index)) for index, budget in enumerate(budgets)
        ]
        for future in futures:
            future.result()
    assert len(sent) == 2
    assert [budget.attempts_used for budget in budgets] == [1, 1]


def test_yfinance_cache_get_hit_consumes_zero_transport_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yfinance, session = _isolated_session(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(
        _session,
        "_send_once",
        lambda _session_obj, _method, url, **_kwargs: sent.append(url) or _TransportResponse(),
    )
    data = yfinance.data.YfData._instances[yfinance.data.YfData]
    data._crumb = "fixture-crumb"
    data._cookie = True
    data.cache_get.cache_clear()
    first_budget = _transport_budget(attempts=1)
    with request_execution_scope(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        budget=first_budget,
    ):
        first = data.cache_get("https://query1.finance.yahoo.com/v1/cache-test", timeout=2)
    assert first_budget.attempts_used == 1
    zero_budget = _transport_budget(attempts=0)
    with request_execution_scope(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        budget=zero_budget,
    ):
        cached = data.cache_get("https://query1.finance.yahoo.com/v1/cache-test", timeout=2)
    assert cached is first
    assert sent == ["https://query1.finance.yahoo.com/v1/cache-test"]
    assert zero_budget.attempts_used == 0
