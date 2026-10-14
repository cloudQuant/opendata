"""Service-boundary identity validation for generated HTTP provider fetchers."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    ProviderEngineQueryError,
    build_query_model,
)
from opendata.data.providers._engine.row_expansion import (
    EMPTY_RESULT,
    MISSING_POINTER,
    NOT_A_RECORD_LIST,
    FieldMapping,
    NestedArrayExpansion,
    RecordRepointPlan,
    RowExpansionError,
    expand_nested_array,
    repoint_records,
)
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.intrinio.specs import CURRENCY_PAIRS
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import (
    ProviderModelQueryBudgetError,
    ProviderModelQueryIdentityError,
    ProviderModelQueryUpstreamError,
    ProviderModelQueryValidationError,
    query_provider_model,
)

INTRINIO_PAIRS_URL = "https://api-v2.intrinio.com/forex/pairs"
CBOE_INDICES_URL = "https://cdn.cboe.com/api/global/us_indices/definitions/all_indices.json"


class _JsonSession(requests.Session):
    """Requests-compatible local session driven only by the test's fixed documents."""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if url not in self.responses:
            raise AssertionError(f"unexpected governed HTTP request to {url}")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = "utf-8"
        response._content = json.dumps(self.responses[url], allow_nan=False).encode("utf-8")
        return response


def _context(source: str, model: str, host: str, *, attempts: int = 1) -> FetchContext:
    grant = RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:generated-engine-identity",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    budget = RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
    )
    return FetchContext(timeout=5.0, request_budget=budget)


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("intrinio", registry)
    register_provider("cboe", registry)
    return registry


def _query(
    registry: ProviderRegistry,
    *,
    source: str,
    model: str,
    query: dict[str, object],
    ctx: FetchContext,
) -> dict[str, object]:
    return asyncio.run(
        query_provider_model(
            registry=registry,
            source=source,
            model=model,
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=ctx,
        )
    )


def test_service_injected_identity_executes_two_generated_sources_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")
    session = _JsonSession(
        {
            INTRINIO_PAIRS_URL: {
                "pairs": [
                    {
                        "code": "EURUSD",
                        "name": "Euro / US Dollar",
                        "base_currency": "EUR",
                        "quote_currency": "USD",
                    }
                ]
            },
            CBOE_INDICES_URL: [{"index_symbol": "VIX", "index_name": "Cboe Volatility Index"}],
        }
    )
    governed = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr("opendata.data.http_client.get_shared_http_client", lambda: governed)
    registry = _registry()

    intrinio = registry.resolve_model("intrinio", "CurrencyPairs")
    intrinio_query: dict[str, object] = {}
    original_intrinio_transform = intrinio.transform_query

    def capture_intrinio_query(**kwargs: object) -> object:
        intrinio_query.update(kwargs)
        return original_intrinio_transform(**kwargs)

    monkeypatch.setattr(intrinio, "transform_query", capture_intrinio_query)
    intrinio_context = _context("intrinio", "CurrencyPairs", urlsplit(INTRINIO_PAIRS_URL).netloc)
    intrinio_result = _query(
        registry,
        source="intrinio",
        model="CurrencyPairs",
        query={"query": "EUR"},
        ctx=intrinio_context,
    )

    cboe = registry.resolve_model("cboe", "AvailableIndices")
    cboe_query: dict[str, object] = {}
    original_cboe_transform = cboe.transform_query

    def capture_cboe_query(**kwargs: object) -> object:
        cboe_query.update(kwargs)
        return original_cboe_transform(**kwargs)

    monkeypatch.setattr(cboe, "transform_query", capture_cboe_query)
    cboe_context = _context("cboe", "AvailableIndices", urlsplit(CBOE_INDICES_URL).netloc)
    cboe_result = _query(
        registry,
        source="cboe",
        model="AvailableIndices",
        query={},
        ctx=cboe_context,
    )

    assert intrinio_query == {"query": "EUR", "source": "intrinio", "market": "all"}
    assert cboe_query == {"source": "cboe", "market": "us"}
    assert intrinio_result["results"] == [
        {
            "symbol": "EURUSD",
            "name": "Euro / US Dollar",
            "base_currency": "EUR",
            "quote_currency": "USD",
        }
    ]
    assert cboe_result["results"] == [
        {
            "symbol": "VIX",
            "name": "Cboe Volatility Index",
            "exchange": None,
            "currency": None,
            "description": None,
            "data_delay": None,
            "open_time": None,
            "close_time": None,
            "time_zone": None,
            "tick_days": None,
            "tick_frequency": None,
            "tick_period": None,
        }
    ]
    assert session.calls[0]["params"] == {"api_key": "test-key"}
    assert session.calls[1]["params"] is None
    assert all(call["method"] == "GET" for call in session.calls)
    assert intrinio_context.request_budget.attempts_used == 1
    assert cboe_context.request_budget.attempts_used == 1

    # The generated factory carries the governed identities as defaults for older direct callers.
    direct_intrinio = registry.resolve_model("intrinio", "CurrencyPairs")
    direct_intrinio_query = direct_intrinio.transform_query(query="EUR")
    assert direct_intrinio_query.source == "intrinio"
    assert direct_intrinio_query.market == "all"
    direct_cboe = registry.resolve_model("cboe", "AvailableIndices")
    direct_cboe_query = direct_cboe.transform_query()
    assert direct_cboe_query.source == "cboe"
    assert direct_cboe_query.market == "us"

    direct_context = _context("intrinio", "CurrencyPairs", urlsplit(INTRINIO_PAIRS_URL).netloc)
    direct_rows = list(direct_intrinio.fetch(ctx=direct_context, query="EUR"))
    assert [row.model_dump(mode="json") for row in direct_rows] == intrinio_result["results"]
    assert session.calls[2]["params"] == {"api_key": "test-key"}
    assert direct_context.request_budget.attempts_used == 1


@pytest.mark.parametrize(
    ("source", "model", "query", "expected"),
    [
        (
            "intrinio",
            "CurrencyPairs",
            {"query": "EUR", "source": "cboe"},
            ProviderModelQueryIdentityError,
        ),
        (
            "cboe",
            "AvailableIndices",
            {"market": "eu"},
            ProviderModelQueryIdentityError,
        ),
    ],
)
def test_service_rejects_wrong_source_or_market_before_any_attempt(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    query: dict[str, object],
    expected: type[Exception],
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")
    session = _JsonSession({})
    governed = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr("opendata.data.http_client.get_shared_http_client", lambda: governed)
    registry = _registry()
    url = INTRINIO_PAIRS_URL if source == "intrinio" else CBOE_INDICES_URL
    ctx = _context(source, model, urlsplit(url).netloc)

    with pytest.raises(expected):
        _query(registry, source=source, model=model, query=query, ctx=ctx)

    assert session.calls == []
    assert ctx.request_budget.attempts_used == 0
    assert ctx.request_budget.attempts_used_for(source) == 0


def test_generated_model_stays_strict_and_zero_budget_uses_no_transport_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")
    session = _JsonSession({})
    governed = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr("opendata.data.http_client.get_shared_http_client", lambda: governed)
    registry = _registry()
    fetcher = registry.resolve_model("intrinio", "CurrencyPairs")

    with pytest.raises(ProviderEngineError) as unknown:
        fetcher.transform_query(query="EUR", source="intrinio", market="all", unexpected="nope")
    assert isinstance(unknown.value, ProviderEngineQueryError)
    assert unknown.value.code == "INTRINIO_QUERY_INVALID"
    assert unknown.value.rejected == ("unexpected",)
    for identity_query, invalid_field in (
        ({"query": "EUR", "source": "cboe", "market": "all"}, "source"),
        ({"query": "EUR", "source": "intrinio", "market": "us"}, "market"),
    ):
        with pytest.raises(ProviderEngineError) as invalid_identity:
            fetcher.transform_query(**identity_query)
        assert invalid_identity.value.code == "INTRINIO_QUERY_INVALID"
        assert invalid_identity.value.invalid == (invalid_field,)

    dividends_fetcher = registry.resolve_model("intrinio", "HistoricalDividends")
    with pytest.raises(ProviderEngineQueryError) as missing:
        dividends_fetcher.transform_query(source="intrinio", market="us")
    assert missing.value.code == "INTRINIO_QUERY_INVALID"
    assert missing.value.missing == ("symbol",)

    legacy_query_model = build_query_model(CURRENCY_PAIRS)
    assert set(legacy_query_model.model_fields) == {"query"}
    with pytest.raises(ValidationError):
        legacy_query_model.model_validate({"query": "EUR", "source": "intrinio", "market": "all"})

    invalid_queries = (
        ("CurrencyPairs", {"query": "EUR", "unexpected": "nope"}),
        ("HistoricalDividends", {}),
        ("CurrencyPairs", {"query": ["EUR"]}),
    )
    for invalid_model, invalid_query in invalid_queries:
        invalid_service_context = _context(
            "intrinio", invalid_model, urlsplit(INTRINIO_PAIRS_URL).netloc
        )
        with pytest.raises(ProviderModelQueryValidationError):
            _query(
                registry,
                source="intrinio",
                model=invalid_model,
                query=invalid_query,
                ctx=invalid_service_context,
            )
        assert invalid_service_context.request_budget.attempts_used == 0
        assert invalid_service_context.request_budget.attempts_used_for("intrinio") == 0

    # Shape failures still represent the upstream response after one real transport attempt.
    malformed_session = _JsonSession({INTRINIO_PAIRS_URL: {"pairs": "not-an-array"}})
    malformed_client = GovernedHttpClient(session=malformed_session, raw_response_cache=None)
    monkeypatch.setattr(
        "opendata.data.http_client.get_shared_http_client", lambda: malformed_client
    )
    malformed_context = _context("intrinio", "CurrencyPairs", urlsplit(INTRINIO_PAIRS_URL).netloc)
    with pytest.raises(ProviderModelQueryUpstreamError):
        _query(
            registry,
            source="intrinio",
            model="CurrencyPairs",
            query={"query": "EUR"},
            ctx=malformed_context,
        )
    assert len(malformed_session.calls) == 1
    assert malformed_context.request_budget.attempts_used == 1

    zero_context = _context(
        "intrinio", "CurrencyPairs", urlsplit(INTRINIO_PAIRS_URL).netloc, attempts=0
    )
    with pytest.raises(ProviderModelQueryBudgetError):
        _query(
            registry,
            source="intrinio",
            model="CurrencyPairs",
            query={"query": "EUR"},
            ctx=zero_context,
        )
    assert session.calls == []
    assert zero_context.request_budget.attempts_used == 0
    assert zero_context.request_budget.attempts_used_for("intrinio") == 0


def test_empty_source_record_lists_are_valid_but_missing_and_wrong_shapes_still_refuse() -> None:
    repoint = RecordRepointPlan(
        records_pointer="records",
        fields=(FieldMapping("symbol", "ticker", required=False),),
    )
    nested = NestedArrayExpansion(
        records_pointer="records",
        child_array_pointer="transactions",
        child_fields=(FieldMapping("shares", "shares", required=False),),
    )

    assert repoint_records({"records": []}, repoint) == []
    assert expand_nested_array({"records": []}, nested) == []

    with pytest.raises(RowExpansionError) as missing_repoint:
        repoint_records({}, repoint)
    assert missing_repoint.value.reason == MISSING_POINTER
    with pytest.raises(RowExpansionError) as malformed_repoint:
        repoint_records({"records": {}}, repoint)
    assert malformed_repoint.value.reason == NOT_A_RECORD_LIST

    with pytest.raises(RowExpansionError) as missing_nested:
        expand_nested_array({}, nested)
    assert missing_nested.value.reason == MISSING_POINTER
    with pytest.raises(RowExpansionError) as malformed_nested:
        expand_nested_array({"records": {}}, nested)
    assert malformed_nested.value.reason == NOT_A_RECORD_LIST

    with pytest.raises(RowExpansionError) as no_children:
        expand_nested_array({"records": [{"transactions": []}]}, nested)
    assert no_children.value.reason == EMPTY_RESULT
