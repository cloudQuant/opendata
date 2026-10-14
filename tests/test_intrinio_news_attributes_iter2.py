"""Offline, per-model contract and governance checks for Intrinio iteration 2."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from requests import Response

from opendata.data.http_client import (
    GovernedHttpClient,
    HttpClientConfig,
)
from opendata.data.models.intrinio_news_attributes import (
    IntrinioCompanyFiling,
    IntrinioCompanyNews,
    IntrinioFredSeries,
    IntrinioHistoricalAttribute,
    IntrinioLatestAttribute,
    IntrinioSearchAttribute,
    IntrinioWorldNews,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.intrinio.models import iter2_news_attributes as intrinio
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "docs/evidence/C100/intrinio-news-attributes/fixtures"
TOKEN = "test-only-intrinio-token"


def _fixture(model: str, variant: str = "positive") -> object:
    return json.loads((FIXTURES / f"{model}.{variant}.json").read_text(encoding="utf-8"))


def _context(fetcher: object, *, attempts: int = 50) -> FetchContext:
    grant = RequestGrant(
        source="intrinio",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=frozenset({"api-v2.intrinio.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=3.0, request_budget=budget)


class _Response:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = json.dumps(body, allow_nan=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}


class _FakeClient:
    def __init__(self, *bodies: object, status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        body = self.bodies.pop(0) if self.bodies else {}
        return _Response(body, self.status)


_MODEL_CASES: tuple[tuple[type[Any], type[Any], dict[str, object], str], ...] = (
    (
        intrinio.IntrinioCompanyNewsFetcher,
        IntrinioCompanyNews,
        {"symbol": "AAPL", "start_date": "2025-01-01", "end_date": "2025-01-31"},
        "CompanyNews",
    ),
    (
        intrinio.IntrinioWorldNewsFetcher,
        IntrinioWorldNews,
        {"start_date": "2025-01-01", "end_date": "2025-02-28"},
        "WorldNews",
    ),
    (
        intrinio.IntrinioCompanyFilingsFetcher,
        IntrinioCompanyFiling,
        {"symbol": "AAPL", "start_date": "2025-01-01", "end_date": "2025-02-01"},
        "CompanyFilings",
    ),
    (
        intrinio.IntrinioSearchAttributesFetcher,
        IntrinioSearchAttribute,
        {"query": "revenue"},
        "SearchAttributes",
    ),
    (
        intrinio.IntrinioLatestAttributesFetcher,
        IntrinioLatestAttribute,
        {"symbol": "AAPL", "tag": "revenue"},
        "LatestAttributes",
    ),
    (
        intrinio.IntrinioHistoricalAttributesFetcher,
        IntrinioHistoricalAttribute,
        {"symbol": "AAPL", "tag": "revenue", "start_date": "2024-01-01"},
        "HistoricalAttributes",
    ),
    (
        intrinio.IntrinioFredSeriesFetcher,
        IntrinioFredSeries,
        {"symbol": "$GDP", "start_date": "2024-01-01"},
        "FredSeries",
    ),
)


def _direct_query_kwargs(model: str, values: dict[str, object]) -> dict[str, object]:
    return {"source": "intrinio", "market": "us", **values}


@pytest.mark.parametrize("fetcher_type,row_type,query,model", _MODEL_CASES)
def test_each_model_normalizes_its_positive_fixture_to_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    row_type: type[Any],
    query: dict[str, object],
    model: str,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    fetcher = fetcher_type()
    payload = _fixture(model)
    if model in {
        "CompanyNews",
        "WorldNews",
        "CompanyFilings",
        "HistoricalAttributes",
        "FredSeries",
    }:
        client = _FakeClient(payload)
        fetcher.http_client = client
        rows = fetcher.fetch(ctx=_context(fetcher), **_direct_query_kwargs(model, query))
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["source"] == "intrinio"
        assert call["headers"] == {"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"}
        assert "source" not in (call["params"] or {})
        assert "market" not in (call["params"] or {})
        assert call["timeout"] == 3.0
    elif model == "SearchAttributes":
        client = _FakeClient(payload)
        fetcher.http_client = client
        rows = fetcher.fetch(ctx=_context(fetcher), **_direct_query_kwargs(model, query))
        assert len(client.calls) == 1
        assert client.calls[0]["params"] == {"query": "revenue", "page_size": "1000"}
    else:
        fetcher.http_client = _FakeClient(*[payload for _ in range(4)])
        rows = fetcher.fetch(ctx=_context(fetcher), **_direct_query_kwargs(model, query))
        assert len(rows) == 1
    assert len(rows) == 1
    assert type(rows[0]) is row_type
    assert json.loads(rows[0].model_dump_json())


@pytest.mark.parametrize("fetcher_type,row_type,query,model", _MODEL_CASES)
def test_each_model_counterfact_fixture_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    row_type: type[Any],
    query: dict[str, object],
    model: str,
) -> None:
    del row_type
    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    fetcher = fetcher_type()
    fetcher.http_client = _FakeClient(_fixture(model, "counterfact"))
    with pytest.raises(intrinio.IntrinioShapeError):
        fetcher.fetch(ctx=_context(fetcher), **_direct_query_kwargs(model, query))


@pytest.mark.parametrize(
    "fetcher_type,query",
    [
        (
            intrinio.IntrinioCompanyNewsFetcher,
            {"symbol": "AAPL", "start_date": "2025-02-01", "end_date": "2025-01-01"},
        ),
        (intrinio.IntrinioWorldNewsFetcher, {"start_date": "2025-02-01", "end_date": "2025-01-01"}),
        (
            intrinio.IntrinioCompanyFilingsFetcher,
            {"start_date": "2025-02-01", "end_date": "2025-01-01"},
        ),
        (intrinio.IntrinioSearchAttributesFetcher, {"query": "  "}),
        (intrinio.IntrinioLatestAttributesFetcher, {"symbol": "", "tag": "revenue"}),
        (
            intrinio.IntrinioHistoricalAttributesFetcher,
            {
                "symbol": "AAPL",
                "tag": "revenue",
                "start_date": "2025-02-01",
                "end_date": "2025-01-01",
            },
        ),
        (
            intrinio.IntrinioFredSeriesFetcher,
            {"symbol": "$GDP", "start_date": "2025-02-01", "end_date": "2025-01-01"},
        ),
    ],
)
def test_invalid_model_inputs_are_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    query: dict[str, object],
) -> None:
    fetcher = fetcher_type()
    client = _FakeClient({})
    fetcher.http_client = client
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), **_direct_query_kwargs("unused", query))
    assert client.calls == []


@pytest.mark.parametrize("identity", [{"source": "not-intrinio"}, {"market": "all"}])
def test_mismatched_injected_provider_identity_is_validation_error_before_transport(
    identity: dict[str, str],
) -> None:
    fetcher = intrinio.IntrinioWorldNewsFetcher()
    client = _FakeClient({})
    fetcher.http_client = client
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), **identity)
    assert client.calls == []


def test_company_news_uses_documented_is_spam_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    fetcher = intrinio.IntrinioCompanyNewsFetcher()
    client = _FakeClient(_fixture("CompanyNews"))
    fetcher.http_client = client
    fetcher.fetch(
        ctx=_context(fetcher),
        source="intrinio",
        market="us",
        symbol="AAPL",
        is_spam=True,
    )
    assert client.calls[0]["params"]["is_spam"] == "true"
    assert "spam" not in client.calls[0]["params"]


def test_missing_key_and_rate_limit_are_distinct_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = intrinio.IntrinioWorldNewsFetcher()
    client = _FakeClient(_fixture("WorldNews"))
    fetcher.http_client = client
    monkeypatch.delenv("INTRINIO_API_KEY", raising=False)
    with pytest.raises(intrinio.IntrinioProviderError, match="CREDENTIAL_MISSING_OR_INVALID"):
        fetcher.fetch(ctx=_context(fetcher), source="intrinio", market="us")
    assert client.calls == []

    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    limited = intrinio.IntrinioWorldNewsFetcher()
    limited.http_client = _FakeClient(_fixture("WorldNews"), status=429)
    with pytest.raises(intrinio.IntrinioProviderError, match="INTRINIO_RATE_LIMITED"):
        limited.fetch(ctx=_context(limited), source="intrinio", market="us")


def test_governed_http_client_consumes_the_exact_intrinio_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        calls = 0

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            self.calls += 1
            assert method == "GET"
            assert url.startswith("https://api-v2.intrinio.com/companies/news")
            assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
            response = Response()
            response.status_code = 200
            response._content = json.dumps(_fixture("WorldNews")).encode("utf-8")
            response.headers["Content-Type"] = "application/json"
            response.url = url
            response.encoding = "utf-8"
            return response

    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    fetcher = intrinio.IntrinioWorldNewsFetcher()
    session = Session()
    fetcher.http_client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, backoff_base=0, backoff_jitter=0),
        session=session,  # type: ignore[arg-type]
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
    )
    ctx = _context(fetcher, attempts=1)
    rows = fetcher.fetch(ctx=ctx, source="intrinio", market="us")
    assert len(rows) == 1
    assert session.calls == 1
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 1


@pytest.mark.parametrize("fetcher_type,row_type,query,model", _MODEL_CASES)
def test_provider_model_query_injects_identity_and_serializes_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    row_type: type[Any],
    query: dict[str, object],
    model: str,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", TOKEN)
    fetcher = fetcher_type()
    capability = fetcher.capability
    received: dict[str, object] = {}
    original = fetcher.transform_query

    def record_query(**values: object) -> object:
        received.update(values)
        return original(**values)

    monkeypatch.setattr(fetcher, "transform_query", record_query)
    positive_payload = _fixture(model)
    # Keep this service-level seam offline; direct fetch tests above cover the real governed client.
    monkeypatch.setattr(
        fetcher, "extract_data", lambda _params, _ctx: _service_rows(model, positive_payload)
    )
    descriptor = SimpleNamespace(
        source="intrinio",
        model=model,
        domain=capability.domain,
        verified=False,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, fetcher)
    )
    monkeypatch.setattr(
        provider_model_query, "_reviewed_query_contract", lambda _domain: (None, row_type)
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    service_query = dict(query)
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="intrinio",
            model=model,
            query=service_query,
            principal=Principal(),
            ctx=_context(fetcher),
        )
    )
    assert received["source"] == "intrinio"
    assert received["market"] == capability.market
    assert response["source"] == "intrinio"
    assert response["model"] == model
    assert response["results"]
    assert isinstance(response["results"], list)
    assert json.dumps(response["results"], allow_nan=False)


def _service_rows(model: str, payload: object) -> list[dict[str, object]]:
    """Map one per-model positive wire fixture using each public row contract."""
    if model == "CompanyNews":
        return [
            intrinio._news_record(row, world=False, requested_symbols=("AAPL",))
            for row in intrinio._response_rows(payload, "news", max_rows=100)
        ]
    if model == "WorldNews":
        return [
            intrinio._news_record(row, world=True)
            for row in intrinio._response_rows(payload, "news", max_rows=100)
        ]
    if model == "CompanyFilings":
        query = intrinio._CompanyFilingsQuery(symbol="AAPL")
        return [
            intrinio.IntrinioCompanyFilingsFetcher._map_filing(row, query)
            for row in intrinio._response_rows(payload, "filings", max_rows=100)
        ]
    if model == "SearchAttributes":
        return [
            intrinio.IntrinioSearchAttributesFetcher._map_tag(row)
            for row in intrinio._response_rows(payload, "tags", max_rows=1000)
        ]
    if model == "LatestAttributes":
        return [{"symbol": "AAPL", "tag": "revenue", "value": payload["value"]}]
    if model == "HistoricalAttributes":
        return [
            {"symbol": "AAPL", "tag": "revenue", **row}
            for row in intrinio._response_rows(payload, "historical_data", max_rows=100)
        ]
    if model == "FredSeries":
        return list(intrinio._response_rows(payload, "historical_data", max_rows=100))
    raise AssertionError(f"unknown model fixture {model}")
