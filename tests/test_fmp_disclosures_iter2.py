"""Offline contract and bounded-transport tests for FMP disclosures Iteration 2."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp.models import _client
from opendata.data.providers.fmp.models import iter2_disclosures as fmp
from opendata.data.providers.fmp.models._client import FMPProviderError, FMPQueryLimitError
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "evidence"
    / "C100"
    / "fmp-disclosures"
    / "fixtures"
    / "rows.json"
)
FIXTURES: dict[str, dict[str, dict[str, Any]]] = json.loads(_FIXTURE_PATH.read_text())

CASES: dict[str, tuple[type[Any], dict[str, object]]] = {
    "CompanyNews": (fmp.CompanyNewsFetcher, {"symbol": "AAPL"}),
    "WorldNews": (fmp.WorldNewsFetcher, {}),
    "CompanyFilings": (
        fmp.CompanyFilingsFetcher,
        {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"},
    ),
    "DiscoveryFilings": (
        fmp.DiscoveryFilingsFetcher,
        {"start_date": "2024-01-01", "end_date": "2024-01-31"},
    ),
    "HistoricalDividends": (fmp.HistoricalDividendsFetcher, {"symbol": "AAPL"}),
    "HistoricalSplits": (fmp.HistoricalSplitsFetcher, {"symbol": "AAPL"}),
    "HistoricalEps": (fmp.HistoricalEpsFetcher, {"symbol": "AAPL"}),
    "HistoricalEmployees": (fmp.HistoricalEmployeesFetcher, {"symbol": "AAPL"}),
    "HistoricalMarketCap": (fmp.HistoricalMarketCapFetcher, {"symbol": "AAPL"}),
    "EarningsCallTranscript": (
        fmp.EarningsCallTranscriptFetcher,
        {"symbol": "AAPL", "year": 2020, "quarter": 3},
    ),
    "PriceTarget": (fmp.PriceTargetFetcher, {"symbol": "AAPL"}),
    "PriceTargetConsensus": (fmp.PriceTargetConsensusFetcher, {"symbol": "AAPL"}),
    "KeyExecutives": (fmp.KeyExecutivesFetcher, {"symbol": "AAPL"}),
    "ExecutiveCompensation": (fmp.ExecutiveCompensationFetcher, {"symbol": "AAPL"}),
    "GovernmentTrades": (fmp.GovernmentTradesFetcher, {"symbol": "AAPL"}),
    "InsiderTrading": (fmp.InsiderTradingFetcher, {"symbol": "TRMK"}),
    "InstitutionalOwnership": (
        fmp.InstitutionalOwnershipFetcher,
        {"symbol": "AAPL", "year": 2023, "quarter": 3},
    ),
    "EquityOwnership": (fmp.EquityOwnershipFetcher, {"symbol": "AAPL"}),
}


def _context(fetcher_class: type[Any], timeout: float = 7) -> FetchContext:
    """Provide one bounded, exact-model grant for offline fetcher tests."""
    canonical_model = fetcher_class.canonical_model
    assert canonical_model is not None
    grant = RequestGrant(
        source="fmp",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline FMP disclosure fixture grant",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    budget = RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,))
    return FetchContext(timeout=timeout, request_budget=budget)


@pytest.mark.parametrize("identity", tuple(CASES))
def test_each_disclosure_has_handwritten_valid_and_invalid_source_rows(identity: str) -> None:
    fetcher_class, _ = CASES[identity]
    model = fetcher_class.record_model
    assert model.model_validate(FIXTURES[identity]["positive"])
    with pytest.raises(ValidationError):
        model.model_validate(FIXTURES[identity]["negative"])


@pytest.mark.parametrize("identity", tuple(CASES))
def test_each_fetcher_returns_its_typed_contract(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher_class, query = CASES[identity]
    positive = dict(FIXTURES[identity]["positive"])
    if identity == "KeyExecutives":
        positive.pop("symbol")  # FMP's key-executives rows omit the queried symbol.
    if identity == "GovernmentTrades":
        chamber = positive.pop("chamber")
        endpoint_for_row = "senate-trades" if chamber == "senate" else "house-trades"

        def fake_fetch_array(
            endpoint: str, _params: object, _timeout: float | None
        ) -> list[dict[str, Any]]:
            return [dict(positive)] if endpoint == endpoint_for_row else []

    else:

        def fake_fetch_array(
            _endpoint: str, _params: object, _timeout: float | None
        ) -> list[dict[str, Any]]:
            return [dict(positive)]

    monkeypatch.setattr(fmp, "fetch_array", fake_fetch_array)
    records = fetcher_class().fetch(ctx=_context(fetcher_class), **query)
    assert len(records) == 1
    assert type(records[0]) is fetcher_class.record_model


@pytest.mark.parametrize("identity", tuple(CASES))
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher_class, query = CASES[identity]
    fetcher = fetcher_class()
    capability = fetcher.capability
    raw = dict(FIXTURES[identity]["positive"])
    if identity == "KeyExecutives":
        raw.pop("symbol")
    if identity == "GovernmentTrades":
        chamber = raw.pop("chamber")
        row_endpoint = "senate-trades" if chamber == "senate" else "house-trades"
    else:
        row_endpoint = ""

    observed_query: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        observed_query.update(kwargs)
        return original_transform_query(**kwargs)

    def fake_fetch_array(
        endpoint: str, _params: object, _timeout: float | None
    ) -> list[dict[str, Any]]:
        if identity == "GovernmentTrades":
            return [dict(raw)] if endpoint == row_endpoint else []
        return [dict(raw)]

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    monkeypatch.setattr(fmp, "fetch_array", fake_fetch_array)
    descriptor = SimpleNamespace(
        source="fmp",
        model=fetcher_class.canonical_model,
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
        lambda _domain: (None, fetcher_class.record_model),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model=fetcher_class.canonical_model,
            query=query,
            principal=Principal(),
            ctx=_context(fetcher_class),
        )
    )

    assert observed_query["source"] == "fmp"
    assert observed_query["market"] == capability.market
    assert response["source"] == "fmp"
    assert response["model"] == fetcher_class.canonical_model
    assert response["domain"] == capability.domain
    expected_raw = dict(raw)
    if identity == "KeyExecutives":
        expected_raw["symbol"] = query["symbol"]
    if identity == "GovernmentTrades":
        expected_raw["chamber"] = "senate" if row_endpoint == "senate-trades" else "house"
    expected = fetcher_class.record_model.model_validate(expected_raw).model_dump(
        mode="json", by_alias=False
    )
    assert response["results"] == [expected]


@pytest.mark.parametrize(
    ("fetcher_class", "query"),
    [
        (fmp.CompanyNewsFetcher, {"symbol": "AAPL", "limit": 251}),
        (fmp.CompanyNewsFetcher, {"symbol": "AAPL", "start_date": "2026-01-01"}),
        (
            fmp.CompanyFilingsFetcher,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-04-02"},
        ),
        (fmp.GovernmentTradesFetcher, {"symbol": "AAPL", "max_requests": 1}),
        (fmp.EarningsCallTranscriptFetcher, {"symbol": "AAPL", "year": 2020, "quarter": 5}),
        (fmp.InstitutionalOwnershipFetcher, {"symbol": "AAPL", "year": 2023, "quarter": 0}),
        (fmp.EquityOwnershipFetcher, {"symbol": "AAPL", "limit": 2_001}),
    ],
)
def test_invalid_parameters_are_rejected_before_transport(
    fetcher_class: type[Any], query: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_io(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        pytest.fail("invalid query reached transport")

    monkeypatch.setattr(fmp, "fetch_array", no_io)
    with pytest.raises(ValidationError):
        fetcher_class().fetch(**query)


@pytest.mark.parametrize("identity", tuple(CASES))
def test_empty_provider_response_is_a_typed_empty_result(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher_class, query = CASES[identity]

    def empty_array(endpoint: str, _params: object, _timeout: float | None) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(fmp, "fetch_array", empty_array)
    assert fetcher_class().fetch(ctx=_context(fetcher_class), **query) == ()


def test_news_page_budget_fails_instead_of_returning_a_truncated_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str], float | None]] = []
    row = dict(FIXTURES["CompanyNews"]["positive"])

    def full_page(
        endpoint: str, params: dict[str, str], timeout: float | None
    ) -> list[dict[str, Any]]:
        calls.append((endpoint, params, timeout))
        return [dict(row)]

    monkeypatch.setattr(fmp, "fetch_array", full_page)
    with pytest.raises(FMPQueryLimitError) as caught:
        fmp.CompanyNewsFetcher().fetch(
            ctx=_context(fmp.CompanyNewsFetcher), symbol="AAPL", limit=1, max_requests=1
        )
    assert caught.value.code == "FMP_INCOMPLETE_PAGE_BUDGET"
    assert len(calls) == 1
    assert calls[0][0] == "news/stock"
    assert calls[0][1] == {"symbols": "AAPL", "page": "0", "limit": "1"}
    assert calls[0][2] == 7


def test_wrong_symbol_and_unknown_source_fields_fail_closed() -> None:
    query = fmp.CompanyNewsQuery.model_validate({"symbol": "AAPL"})
    wrong_symbol = dict(FIXTURES["CompanyNews"]["positive"], symbol="MSFT")
    with pytest.raises(FMPProviderError, match="FMP_WRONG_SYMBOL"):
        fmp.CompanyNewsFetcher().transform_data([wrong_symbol], query)
    unknown_field = dict(FIXTURES["CompanyNews"]["positive"], sourceTypo="raw")
    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        fmp.CompanyNewsFetcher().transform_data([unknown_field], query)


def test_missing_api_key_fails_before_the_shared_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setattr(
        _client,
        "get_shared_http_client",
        lambda: pytest.fail("missing key reached shared HTTP client"),
    )
    with pytest.raises(FMPProviderError, match="FMP_API_KEY_MISSING"):
        fmp.CompanyNewsFetcher().fetch(ctx=_context(fmp.CompanyNewsFetcher), symbol="AAPL")


def test_company_filings_json_uses_governed_client_and_paginates_documented_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    row = dict(FIXTURES["CompanyFilings"]["positive"])

    def session_request(
        _session: requests.Session,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response:
        calls.append({"method": method, "url": url, **kwargs})
        page = kwargs["params"]["page"]
        document = [row] if page == "0" else []
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(document).encode("utf-8")
        response.url = url
        return response

    config = HttpClientConfig(
        max_attempts=1,
        rate_limit_per_host=None,
        backoff_base=0,
        backoff_jitter=0,
    )
    governed_client = GovernedHttpClient(
        config,
        sleep=lambda _seconds: None,
        time_fn=lambda: 0.0,
        jitter_fn=lambda: 0.0,
    )
    monkeypatch.setenv("FMP_API_KEY", "fixture-only-key")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: governed_client)
    monkeypatch.setattr(requests.Session, "request", session_request)

    records = fmp.CompanyFilingsFetcher().fetch(
        ctx=_context(fmp.CompanyFilingsFetcher),
        symbol="AAPL",
        start_date="2024-03-01",
        end_date="2024-03-01",
        limit=1,
        max_requests=2,
    )

    assert len(records) == 1
    assert records[0].has_financials is True
    assert records[0].symbol == "AAPL"
    assert len(calls) == 2
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == "https://financialmodelingprep.com/stable/sec-filings-search/symbol"
    assert calls[0]["params"] == {
        "symbol": "AAPL",
        "from": "2024-03-01",
        "to": "2024-03-01",
        "page": "0",
        "limit": "1",
        "apikey": "fixture-only-key",
    }
    assert calls[0]["timeout"] == 7
    assert calls[1]["params"]["page"] == "1"
