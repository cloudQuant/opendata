"""Hand-authored All Access fixtures and provider-service serialization checks."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.models.cboe_index import CboeEquitySearchListing
from opendata.data.models.congress_cboe_iter2 import (
    CboeEquityHistorical,
    CboeEquityQuote,
    CboeEtfHistorical,
    CboeOptionsChains,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.cboe.models import _all_access_client as _client
from opendata.data.providers.cboe.models import all_access as _all_access
from opendata.data.providers.cboe.models.all_access import (
    CboeEquityHistoricalFetcher,
    CboeEquityQuoteFetcher,
    CboeEquitySearchFetcher,
    CboeEtfHistoricalFetcher,
    CboeOptionsChainsFetcher,
)
from opendata.data.providers.cboe.provider import PROVIDER
from opendata.data.providers.cboe.specs import NOT_DECLARABLE
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class _Response:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.content = json.dumps(payload).encode("utf-8")


class _Transport:
    def __init__(self, *responses: object) -> None:
        self.responses = [_Response(response) for response in responses]
        self.requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str,
        **_: object,
    ) -> _Response:
        self.requests.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
                "source": source,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        return self.responses.pop(0)


class _Principal:
    def allows_domain(self, domain: str) -> bool:
        return domain.startswith("cboe_")


def _fixture_context(model: str) -> FetchContext:
    grant = RequestGrant(
        source="cboe",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=24,
        source_attempts=24,
        allowed_hosts=frozenset({"api.livevol.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=24,
        source_attempts=24,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


def _descriptor(fetcher: object, model: str) -> SimpleNamespace:
    capability = fetcher.capability  # type: ignore[attr-defined]
    return SimpleNamespace(
        source=capability.source,
        model=model,
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )


_DAILY_QUOTE = {
    "symbol": "AAPL",
    "underlying_open": 100.0,
    "underlying_high": 103.0,
    "underlying_low": 99.5,
    "underlying_close": 102.25,
    "underlying_volume": 1_250_000,
}
_OPTION = {
    "root": "AAPL",
    "expiry": "2026-10-16",
    "strike": 100.0,
    "option_type": "C",
    "timestamp": "15:59:59.000",
    "option": "AAPL261016C00100000",
    "option_mid": 2.45,
    "option_bid": 2.4,
    "option_bid_size": 12,
    "option_ask": 2.5,
    "option_ask_size": 15,
    "option_open": 2.1,
    "option_high": 2.6,
    "option_low": 2.0,
    "option_close": 2.45,
    "option_last_trade_price": 2.44,
    "cboe_theo": 2.46,
    "option_prev_day_close": 2.3,
    "iv": 0.25,
    "open_interest": 1_200,
    "option_volume": 34,
    "delta": 0.52,
    "gamma": 0.03,
    "vega": 0.11,
    "theta": -0.02,
    "rho": 0.01,
}


_CASES: tuple[dict[str, Any], ...] = (
    {
        "model": "EquityHistorical",
        "fetcher": CboeEquityHistoricalFetcher,
        "contract": CboeEquityHistorical,
        "query": {
            "symbol": "aapl",
            "start_date": "2026-10-09",
            "end_date": "2026-10-09",
        },
        "responses": (["2026-10-09"], [_DAILY_QUOTE]),
        "expected": {
            "symbol": "AAPL",
            "date": "2026-10-09",
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.25,
            "volume": 1_250_000,
            "vwap": None,
            "calls_volume": None,
            "puts_volume": None,
            "total_options_volume": None,
        },
        "bad_responses": (["2026-10-09", "2026-10-09"],),
    },
    {
        "model": "EtfHistorical",
        "fetcher": CboeEtfHistoricalFetcher,
        "contract": CboeEtfHistorical,
        "query": {
            "symbol": "spy",
            "start_date": "2026-10-09",
            "end_date": "2026-10-09",
        },
        "responses": (
            ["2026-10-09"],
            [{**_DAILY_QUOTE, "symbol": "SPY"}],
        ),
        "expected": {
            "symbol": "SPY",
            "date": "2026-10-09",
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.25,
            "volume": 1_250_000,
            "vwap": None,
            "calls_volume": None,
            "puts_volume": None,
            "total_options_volume": None,
        },
        "bad_responses": (["2026-10-09"], [{**_DAILY_QUOTE, "symbol": "AAPL"}]),
    },
    {
        "model": "EquityQuote",
        "fetcher": CboeEquityQuoteFetcher,
        "contract": CboeEquityQuote,
        "query": {"symbol": "aapl", "date": "2026-10-12"},
        "responses": (
            [
                {
                    "symbol": "AAPL",
                    "underlying_bid": 102.2,
                    "underlying_bid_size": 10,
                    "underlying_ask": 102.3,
                    "underlying_ask_size": 12,
                    "underlying_last_trade_price": 102.25,
                    "underlying_last_trade_size": 25,
                    "underlying_open": 100.0,
                    "underlying_high": 103.0,
                    "underlying_low": 99.5,
                    "underlying_close": 102.25,
                    "underlying_volume": 1_250_000,
                    "underlying_prev_day_close": 101.0,
                    "iv30": 0.22,
                    "iv30_change": 0.01,
                    "iv30_change_percent": 0.04,
                    "seq_no": 17,
                }
            ],
        ),
        "expected": {
            "symbol": "AAPL",
            "bid": 102.2,
            "bid_size": 10,
            "ask": 102.3,
            "ask_size": 12,
            "last_price": 102.25,
            "last_size": 25,
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.25,
            "volume": 1_250_000,
            "prev_close": 101.0,
            "iv30": 0.22,
            "iv30_change": 0.01,
            "iv30_change_percent": 0.04,
            "sequence_number": 17,
        },
        "bad_responses": ([{"symbol": "AAPL", "underlying_bid": "102.2"}],),
    },
    {
        "model": "EquitySearch",
        "fetcher": CboeEquitySearchFetcher,
        "contract": CboeEquitySearchListing,
        "query": {"query": "apple", "is_symbol": False},
        "responses": (
            [
                {"name": "AAPL", "company_name": "Apple Inc."},
                {"name": "MSFT", "company_name": "Microsoft Corporation"},
            ],
        ),
        "expected_rows": (
            {"symbol": "AAPL", "name": "Apple Inc.", "dpm_name": None, "post_station": None},
        ),
        "bad_responses": ([{"name": "AAPL"}],),
    },
    {
        "model": "OptionsChains",
        "fetcher": CboeOptionsChainsFetcher,
        "contract": CboeOptionsChains,
        "query": {
            "symbol": "aapl",
            "date": "2026-10-12",
            "root": "AAPL",
            "option_type": "c",
            "min_expiry": "2026-10-16",
            "min_strike": 90,
        },
        "responses": (
            [
                {
                    "symbol": "AAPL",
                    "underlying_last_trade_price": 102.25,
                    "options": [_OPTION],
                }
            ],
        ),
        "expected_rows": None,
        "bad_responses": (
            [
                {
                    "symbol": "AAPL",
                    "options": [{**_OPTION, "option_type": "X"}],
                }
            ],
        ),
    },
)


def _expected_options_chain() -> dict[str, object]:
    """Build the hand-authored one-contract columnar output, including nullable fields."""
    populated: dict[str, object] = {
        "underlying_symbol": ["AAPL"],
        "underlying_price": [102.25],
        "contract_symbol": ["AAPL261016C00100000"],
        "eod_date": ["2026-10-12"],
        "expiration": ["2026-10-16"],
        "dte": [4],
        "strike": [100.0],
        "option_type": ["call"],
        "open_interest": [1_200],
        "volume": [34],
        "theoretical_price": [2.46],
        "last_trade_price": [2.44],
        "bid": [2.4],
        "bid_size": [12],
        "ask": [2.5],
        "ask_size": [15],
        "mark": [2.45],
        "open": [2.1],
        "high": [2.6],
        "low": [2.0],
        "close": [2.45],
        "prev_close": [2.3],
        "implied_volatility": [0.25],
        "delta": [0.52],
        "gamma": [0.03],
        "theta": [-0.02],
        "vega": [0.11],
        "rho": [0.01],
    }
    nullable_columns = (
        "contract_size",
        "last_trade_size",
        "last_trade_time",
        "tick",
        "bid_time",
        "bid_exchange",
        "ask_time",
        "ask_exchange",
        "open_bid",
        "open_ask",
        "bid_high",
        "ask_high",
        "bid_low",
        "ask_low",
        "close_size",
        "close_time",
        "close_bid",
        "close_bid_size",
        "close_bid_time",
        "close_ask",
        "close_ask_size",
        "close_ask_time",
        "change",
        "change_percent",
    )
    populated.update({field: [None] for field in nullable_columns})
    return CboeOptionsChains.model_validate(populated).model_dump(mode="json")


for _case in _CASES:
    if _case["model"] == "OptionsChains":
        _case["expected_rows"] = (_expected_options_chain(),)
    elif "expected_rows" not in _case:
        _case["expected_rows"] = (_case["expected"],)


@pytest.mark.parametrize("case", _CASES, ids=lambda case: str(case["model"]))
def test_each_all_access_model_runs_through_service_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
) -> None:
    """Each supported identity accepts injected source/market and serializes its output model."""
    model = str(case["model"])
    fetcher = case["fetcher"]()
    contract = case["contract"]
    monkeypatch.setattr(_all_access, "_market_today", lambda: date(2026, 10, 12))
    received_query: dict[str, object] = {}
    validated_queries: list[object] = []
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        received_query.update(kwargs)
        validated = original_transform_query(**kwargs)
        validated_queries.append(validated)
        return validated

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    transport = _Transport(*case["responses"])
    monkeypatch.setenv("CBOE_LIVEVOL_ACCESS_TOKEN", "fixture-access-token")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="cboe",
            model=model,
            query=case["query"],
            principal=_Principal(),
            ctx=_fixture_context(model),
        )
    )

    expected_rows = [
        contract.model_validate(row).model_dump(mode="json") for row in case["expected_rows"]
    ]
    assert response["source"] == "cboe"
    assert response["model"] == model
    assert response["domain"] == fetcher.capability.domain
    assert response["results"] == expected_rows
    assert received_query["source"] == "cboe"
    assert received_query["market"] == "us"
    assert all(request["source"] == "cboe" for request in transport.requests)
    assert all(
        request["headers"]["Authorization"] == "Bearer fixture-access-token"
        for request in transport.requests
    )
    assert all(
        request["url"].startswith("https://api.livevol.com/v1/delayed/allaccess/")
        for request in transport.requests
    )
    if model == "EquityHistorical":
        assert [request["url"].rsplit("/", 1)[-1] for request in transport.requests] == [
            "trading-days",
            "underlying-quotes",
        ]
        assert validated_queries[0].interval == "1d"
        assert transport.requests[0]["params"] == {
            "start_date": "2026-10-09",
            "end_date": "2026-10-09",
        }
        assert transport.requests[1]["params"] == {
            "symbols": "AAPL",
            "date": "2026-10-09",
            "seq_no": "0",
        }
    if model == "EquityQuote":
        assert transport.requests[0]["params"] == {
            "symbols": "AAPL",
            "date": "2026-10-12",
            "seq_no": "0",
        }
    if model == "EquitySearch":
        assert transport.requests[0]["params"] == {}
    if model == "OptionsChains":
        assert transport.requests[0]["params"] == {
            "symbol": "AAPL",
            "date": "2026-10-12",
            "root": "AAPL",
            "option_type": "C",
            "min_expiry": "2026-10-16",
            "min_strike": "90.0",
        }
    assert transport.responses == []


@pytest.mark.parametrize("case", _CASES, ids=lambda case: str(case["model"]))
def test_each_all_access_model_rejects_its_hand_authored_destructive_fixture(
    monkeypatch: pytest.MonkeyPatch,
    case: dict[str, Any],
) -> None:
    """A model-specific invalid source record fails closed before producing typed output."""
    model = str(case["model"])
    fetcher = case["fetcher"]()
    monkeypatch.setattr(_all_access, "_market_today", lambda: date(2026, 10, 12))
    transport = _Transport(*case["bad_responses"])
    monkeypatch.setenv("CBOE_LIVEVOL_ACCESS_TOKEN", "fixture-access-token")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)

    with pytest.raises(_client.CboeAllAccessError):
        fetcher.fetch(ctx=_fixture_context(model), **case["query"])

    assert len(transport.requests) == len(case["bad_responses"])
    assert transport.responses == []


def test_all_access_bindings_preserve_unverified_rights_and_the_fixed_eleven_model_count() -> None:
    assert set(PROVIDER.fetcher_dict) == {
        "AvailableIndices",
        "IndexConstituents",
        "IndexSearch",
        "IndexHistorical",
        "EquityHistorical",
        "EtfHistorical",
        "EquityQuote",
        "EquitySearch",
        "OptionsChains",
    }
    assert all(not fetcher.capability.verified for fetcher in PROVIDER.fetchers)
    assert PROVIDER.credentials[0].environment_variable == "CBOE_LIVEVOL_ACCESS_TOKEN"
    assert set(NOT_DECLARABLE) == {"FuturesCurve", "IndexSnapshots"}
    assert len(PROVIDER.fetcher_dict) + len(NOT_DECLARABLE) == 11


@pytest.mark.parametrize(
    "query",
    (
        {"symbol": "AAPL", "start_date": "2010-12-31", "end_date": "2026-10-09"},
        {"symbol": "AAPL", "start_date": "2026-09-10", "end_date": "2026-10-09"},
        {"symbol": "AAPL", "start_date": "2026-10-09", "end_date": "2026-10-12"},
        {"symbol": "AAPL", "start_date": "2026-10-10", "end_date": "2026-10-09"},
        {
            "symbol": "AAPL",
            "start_date": "2026-10-09",
            "end_date": "2026-10-09",
            "interval": "1m",
        },
    ),
    ids=(
        "before-source-history",
        "too-wide-window",
        "current-market-date",
        "reversed",
        "unsupported-intraday-interval",
    ),
)
def test_history_query_rejects_unbounded_or_unsupported_windows(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, str],
) -> None:
    monkeypatch.setattr(_all_access, "_market_today", lambda: date(2026, 10, 12))
    with pytest.raises(ValueError):
        CboeEquityHistoricalFetcher().transform_query(**query)


def test_options_chain_empty_source_collection_returns_no_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport([])
    monkeypatch.setattr(_all_access, "_market_today", lambda: date(2026, 10, 12))
    monkeypatch.setenv("CBOE_LIVEVOL_ACCESS_TOKEN", "fixture-access-token")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)

    result = CboeOptionsChainsFetcher().fetch(
        ctx=_fixture_context("OptionsChains"),
        symbol="AAPL",
        date="2026-10-12",
    )

    assert result == ()
    assert len(transport.requests) == 1
