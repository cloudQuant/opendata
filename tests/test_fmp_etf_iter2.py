"""Offline tests for the bounded FMP ETF model slice."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.fmp_etf import (
    FmpEtfCountry,
    FmpEtfEquityExposure,
    FmpEtfHistorical,
    FmpEtfHolding,
    FmpEtfInfo,
    FmpEtfPricePerformance,
    FmpEtfSearch,
    FmpEtfSector,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models import iter2_etf
from opendata.data.providers.fmp.models.iter2_etf import (
    EtfCountriesFetcher,
    EtfEquityExposureFetcher,
    EtfHistoricalFetcher,
    EtfHoldingsFetcher,
    EtfInfoFetcher,
    EtfPricePerformanceFetcher,
    EtfSearchFetcher,
    EtfSectorsFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_FIXTURE_PATH = (
    Path(__file__).parents[1] / "docs" / "evidence" / "C100" / "fmp-etf" / "fixtures" / "rows.json"
)


class _FixtureSession(requests.Session):
    """Requests-compatible fixture transport kept inside GovernedHttpClient."""

    def __init__(self, responses: list[object]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        if method != "GET":
            raise AssertionError("ETF providers may only make GET requests")
        params = kwargs.get("params")
        self.calls.append(
            {
                "url": url,
                "path": urlsplit(url).path,
                "params": dict(params) if isinstance(params, dict) else {},
                "timeout": kwargs.get("timeout"),
                "allow_redirects": kwargs.get("allow_redirects"),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected ETF request")
        payload = self.responses.pop(0)
        if isinstance(payload, tuple):
            status, body, content_type = payload
        else:
            status, body, content_type = 200, payload, "application/json"
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.headers["Content-Type"] = content_type
        response._content = (
            body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        )
        response.request = requests.Request(method, url).prepare()
        return response


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> _FixtureSession:
    monkeypatch.setenv("FMP_API_KEY", "offline-fixture-key")
    session = _FixtureSession([])
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
        session=session,
    )
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(iter2_etf, "get_shared_http_client", lambda: client)
    return session


def _context(model: str, *, attempts: int = 16, timeout: float = 1.0) -> FetchContext:
    grant = RequestGrant(
        source="fmp",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline fixture based on public FMP response documentation",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        timeout=timeout,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        ),
    )


def _fixtures() -> dict[str, dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _directory() -> list[dict[str, str]]:
    return [
        {"symbol": "SPY", "name": "State Street SPDR S&P 500 ETF"},
        {"symbol": "ZWT-T.TO", "name": "BMO Equal Weight Technology Index ETF"},
        {"symbol": "CDL", "name": "VictoryShares US Large Cap High Div Volatility Wtd ETF"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
    ]


_CASES: dict[str, dict[str, object]] = {
    "EtfSearch": {
        "fetcher": EtfSearchFetcher,
        "contract": FmpEtfSearch,
        "query": {"query": "SPY"},
        "model": "EtfSearch",
        "responses": lambda fixtures: [_directory()],
        "expected": {"symbol": "SPY", "name": "State Street SPDR S&P 500 ETF"},
        "calls": 1,
    },
    "EtfInfo": {
        "fetcher": EtfInfoFetcher,
        "contract": FmpEtfInfo,
        "query": {"symbol": "spy"},
        "model": "EtfInfo",
        "responses": lambda fixtures: [_directory(), [fixtures["EtfInfo"]["positive"]]],
        "expected": {"symbol": "SPY", "asset_class": "Equity", "expense_ratio": 0.09},
        "calls": 2,
    },
    "EtfHoldings": {
        "fetcher": EtfHoldingsFetcher,
        "contract": FmpEtfHolding,
        "query": {"symbol": "SPY", "date": "2026-07-30"},
        "model": "EtfHoldings",
        "responses": lambda fixtures: [_directory(), [fixtures["EtfHoldings"]["positive"]]],
        "expected": {"symbol": "SPY", "asset": "AAPL", "weight_percentage": 7.79997012},
        "calls": 2,
    },
    "EtfCountries": {
        "fetcher": EtfCountriesFetcher,
        "contract": FmpEtfCountry,
        "query": {"symbol": "SPY"},
        "model": "EtfCountries",
        "responses": lambda fixtures: [_directory(), [fixtures["EtfCountries"]["positive"]]],
        "expected": {"symbol": "SPY", "country": "United States", "weight": 97.26},
        "calls": 2,
    },
    "EtfEquityExposure": {
        "fetcher": EtfEquityExposureFetcher,
        "contract": FmpEtfEquityExposure,
        "query": {"symbol": "aapl"},
        "model": "EtfEquityExposure",
        "responses": lambda fixtures: [_directory(), [fixtures["EtfEquityExposure"]["positive"]]],
        "expected": {
            "equity_symbol": "AAPL",
            "etf_symbol": "ZWT-T.TO",
            "weight": 10.100000000000001,
        },
        "calls": 2,
    },
    "EtfPricePerformance": {
        "fetcher": EtfPricePerformanceFetcher,
        "contract": FmpEtfPricePerformance,
        "query": {"symbol": "SPY", "max_records": 1},
        "model": "EtfPricePerformance",
        "responses": lambda fixtures: [
            _directory(),
            [
                fixtures["EtfPricePerformance"]["positive"],
                {
                    "symbol": "QQQ",
                    "changePercentage": 0.25,
                    "price": 600.0,
                    "volume": 1,
                },
            ],
        ],
        "expected": {"symbol": "SPY", "one_day": -0.2221355},
        "calls": 2,
    },
    "EtfSectors": {
        "fetcher": EtfSectorsFetcher,
        "contract": FmpEtfSector,
        "query": {"symbol": "SPY"},
        "model": "EtfSectors",
        "responses": lambda fixtures: [_directory(), [fixtures["EtfSectors"]["positive"]]],
        "expected": {
            "symbol": "SPY",
            "sector": "Basic Materials",
            "weight": 1.6916311902850854,
        },
        "calls": 2,
    },
    "EtfHistorical": {
        "fetcher": EtfHistoricalFetcher,
        "contract": FmpEtfHistorical,
        "query": {
            "symbol": "CDL",
            "start_date": "2020-01-01",
            "end_date": "2020-01-31",
        },
        "model": "EtfHistorical",
        "responses": lambda fixtures: [
            _directory(),
            {
                "symbol": "CDL",
                "historical": [fixtures["EtfHistorical"]["positive"]],
            },
        ],
        "expected": {
            "symbol": "CDL",
            "date": "2020-01-09",
            "close": 50.179001,
            "volume": 76600,
        },
        "calls": 2,
    },
}


@pytest.mark.parametrize("model_id", tuple(_CASES))
def test_etf_models_use_governed_transport_and_serialize_source_facts(
    model_id: str,
    transport: _FixtureSession,
) -> None:
    case = _CASES[model_id]
    transport.responses = case["responses"](_fixtures())  # type: ignore[operator]
    fetcher_type = case["fetcher"]
    fetcher = fetcher_type()  # type: ignore[operator]
    output = fetcher.fetch(ctx=_context(model_id), **case["query"])  # type: ignore[arg-type]
    assert len(output) == 1
    row = output[0]
    assert type(row) is case["contract"]
    dumped = row.model_dump(mode="json", by_alias=False)
    assert all(dumped[key] == value for key, value in case["expected"].items())
    assert len(transport.calls) == case["calls"]
    assert all(call["allow_redirects"] is False for call in transport.calls)
    assert all(call["timeout"] == 1.0 for call in transport.calls)
    assert all(call["params"].get("apikey") == "offline-fixture-key" for call in transport.calls)
    assert all(
        "source" not in call["params"] and "market" not in call["params"]
        for call in transport.calls
    )

    if model_id == "EtfHoldings":
        assert transport.calls[1]["params"].get("date") == "2026-07-30"
    if model_id == "EtfSearch":
        assert transport.calls[0]["params"] == {"apikey": "offline-fixture-key"}
    if model_id == "EtfHistorical":
        assert transport.calls[1]["path"].endswith("/api/v3/historical-price-full/CDL")
        assert transport.calls[1]["params"].get("from") == "2020-01-01"
        assert transport.calls[1]["params"].get("to") == "2020-01-31"


def _counterfactual_responses(model_id: str) -> list[object]:
    fixtures = _fixtures()
    bad = fixtures[model_id]["negative"]
    if model_id == "EtfSearch":
        return [[bad]]
    if model_id == "EtfHistorical":
        return [_directory(), {"symbol": "CDL", "historical": [bad]}]
    if model_id == "EtfPricePerformance":
        return [_directory(), [bad]]
    if model_id == "EtfEquityExposure":
        return [_directory(), [bad]]
    return [_directory(), [bad]]


@pytest.mark.parametrize("model_id", tuple(_CASES))
def test_etf_models_reject_counterfactual_rows(
    model_id: str,
    transport: _FixtureSession,
) -> None:
    case = _CASES[model_id]
    transport.responses = _counterfactual_responses(model_id)
    fetcher = case["fetcher"]()  # type: ignore[operator]
    with pytest.raises(fmp_client.FMPProviderError):
        fetcher.fetch(ctx=_context(model_id), **case["query"])  # type: ignore[arg-type]


@pytest.mark.parametrize("model_id", tuple(_CASES))
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    model_id: str,
    transport: _FixtureSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _CASES[model_id]
    transport.responses = case["responses"](_fixtures())  # type: ignore[operator]
    fetcher = case["fetcher"]()  # type: ignore[operator]
    capability = fetcher.capability
    original_transform_query = fetcher.transform_query
    observed: dict[str, object] = {}

    def capture_query(**kwargs: object) -> object:
        observed.update(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    descriptor = SimpleNamespace(
        source="fmp",
        model=model_id,
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
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, case["contract"]),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model=model_id,
            query=case["query"],  # type: ignore[arg-type]
            principal=Principal(),
            ctx=_context(model_id),
        )
    )
    assert response["source"] == "fmp"
    assert response["model"] == model_id
    assert response["domain"] == capability.domain
    assert observed["source"] == "fmp"
    assert observed["market"] == "global"
    expected = dict(case["expected"])
    assert len(response["results"]) == 1  # type: ignore[arg-type]
    result = response["results"][0]  # type: ignore[index]
    assert all(result[key] == value for key, value in expected.items())  # type: ignore[index]
    assert len(transport.calls) == case["calls"]


def test_etf_directory_has_no_guessed_pagination_and_is_not_silently_truncated(
    transport: _FixtureSession,
) -> None:
    transport.responses = [_directory()]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        EtfSearchFetcher().fetch(ctx=_context("EtfSearch"), query="SPY", page=2)
    assert error.value.code == "FMP_BAD_QUERY"
    assert transport.calls == []

    transport.responses = [_directory()]
    with pytest.raises(fmp_client.FMPQueryLimitError) as limit_error:
        EtfSearchFetcher().fetch(ctx=_context("EtfSearch"), query="SPY", max_records=1)
    assert limit_error.value.code == "FMP_INCOMPLETE_ETF_DIRECTORY"
    assert len(transport.calls) == 1


def test_fmp_etf_rejects_unlisted_symbols_and_html_before_typed_output(
    transport: _FixtureSession,
) -> None:
    transport.responses = [_directory()]
    with pytest.raises(fmp_client.FMPProviderError) as missing_etf:
        EtfInfoFetcher().fetch(ctx=_context("EtfInfo"), symbol="MSFT")
    assert missing_etf.value.code == "FMP_SYMBOL_NOT_ETF"
    assert len(transport.calls) == 1

    transport.responses = [(200, "<html>upgrade</html>", "text/html")]
    with pytest.raises(fmp_client.FMPProviderError) as html_error:
        EtfSearchFetcher().fetch(ctx=_context("EtfSearch"))
    assert html_error.value.code == "FMP_HTML_RESPONSE"


def test_equity_exposure_rejects_a_returned_symbol_outside_etf_directory(
    transport: _FixtureSession,
) -> None:
    transport.responses = [
        _directory(),
        [
            {
                "symbol": "MSFT",
                "asset": "AAPL",
                "sharesNumber": 1,
                "weightPercentage": 1.0,
                "marketValue": 2.0,
            }
        ],
    ]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        EtfEquityExposureFetcher().fetch(ctx=_context("EtfEquityExposure"), symbol="AAPL")
    assert error.value.code == "FMP_SYMBOL_NOT_ETF"
