"""Offline tests for the governed canonical provider-model query route."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_query import (
    get_provider_model_query_context,
    get_provider_model_query_registry,
)
from opendata.api.provider_models import router
from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.domains import DomainSpec
from opendata.data.models.economic_series import SeriesCatalogItem, SeriesObservation
from opendata.data.models.equity_price import EquityHistorical, EquityQuote
from opendata.data.models.period_series import (
    BlsCatalogItem,
    BlsCatalogPage,
    BlsObservation,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.bls.models import search as bls_search
from opendata.data.providers.bls.models import series as bls_series
from opendata.data.providers.bls.models._client import BlsRawCatalogPage
from opendata.data.providers.fmp.models import equity_quote
from opendata.data.providers.fred.models import series as fred_series
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
)
from opendata.services.provider_model_query import query_provider_model


def _local_registry() -> ProviderRegistry:
    """Register the real six canonical models through provider bindings."""
    from opendata.data.providers.bls.provider import PROVIDER as BLS
    from opendata.data.providers.fmp.provider import PROVIDER as FMP
    from opendata.data.providers.fred.provider import PROVIDER as FRED

    registry = ProviderRegistry()
    for descriptor in (FRED, BLS, FMP):
        for binding in descriptor.fetcher_bindings:
            if binding.canonical_model_ids:
                registry.register_provider_models(
                    descriptor.source,
                    binding.canonical_model_ids,
                    binding.load(descriptor.source),
                )
    return registry


def _missing_provider_model_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model the pre-review state even after production specs are declared."""
    from opendata.data import domains

    specs = domains.load_domains()
    provider_model_domains = {
        "fred_search",
        "fred_series",
        "bls_search",
        "bls_series",
        "equity_historical",
        "equity_quote",
    }
    monkeypatch.setattr(
        domains,
        "load_domains",
        lambda: {
            domain: spec for domain, spec in specs.items() if domain not in provider_model_domains
        },
    )


def _complete_domain_specs(monkeypatch: pytest.MonkeyPatch, registry: ProviderRegistry) -> None:
    """Inject complete local semantics for test-only execution of real models."""
    from opendata.data import domains

    contracts: dict[str, type[Any]] = {
        "bls_search": BlsCatalogItem,
        "bls_series": BlsObservation,
        "fred_search": SeriesCatalogItem,
        "fred_series": SeriesObservation,
        "equity_historical": EquityHistorical,
        "equity_quote": EquityQuote,
    }
    semantic_fields: dict[str, tuple[str, str | None, tuple[str, ...], tuple[str, ...]]] = {
        "bls_search": ("snapshot", None, ("series_id",), ("survey",)),
        "bls_series": ("series", "year", ("series_id", "year", "period"), ("series_id",)),
        "fred_search": ("snapshot", None, ("series_id",), ("series_id",)),
        "fred_series": (
            "series",
            "date",
            ("series_id", "date", "realtime_start", "realtime_end"),
            ("series_id",),
        ),
        "equity_historical": ("series", "date", ("symbol", "date"), ("symbol",)),
        "equity_quote": ("snapshot", None, ("symbol",), ("symbol",)),
    }
    specs: dict[str, DomainSpec] = {}
    for domain, contract in contracts.items():
        temporal_kind, time_field, natural_key, filter_dims = semantic_fields[domain]
        specs[domain] = DomainSpec(
            display_name=domain,
            rest_path=f"provider/{domain}",
            contract=contract.__name__,
            temporal_kind=temporal_kind,
            time_field=time_field,
            natural_key=natural_key,
            filter_dims=filter_dims,
            storage_mode="transient",
            permissions=("query",),
        )

    monkeypatch.setattr(domains, "require_domain_semantics", lambda domain: specs[domain])
    monkeypatch.setattr(domains, "contract_model", lambda domain: contracts[domain])


def _client(
    registry: ProviderRegistry,
    *,
    scopes: tuple[str, ...] | None = None,
    ctx: FetchContext | None = None,
    inject_context: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(allows_domain=lambda domain: scopes is None or domain in scopes)
    app.dependency_overrides[get_provider_model_query_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    if inject_context:
        app.dependency_overrides[get_provider_model_query_context] = lambda: ctx
    return TestClient(app)


def _path(source: str, model: str) -> str:
    return f"/providers/{source}/models/{model}/query"


def _fixture_context(source: str, model: str) -> FetchContext:
    from tests.provider_budget_fixtures import offline_fixture_context

    return offline_fixture_context(source, model, timeout=5)


def test_bls_search_keeps_central_page_and_pins_source_and_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: (
            calls.append(kwargs)
            or BlsRawCatalogPage(
                items=(
                    {
                        "series_id": "CES0000000001",
                        "survey": "CE",
                        "title": "Average hourly earnings",
                        "frequency": None,
                        "units": None,
                        "dimensions": {"industry_code": "000000"},
                        "source_metadata": {"source_file": "ce.series"},
                        "catalog_as_of": None,
                    },
                ),
                total=7,
                offset=2,
                limit=1,
            )
        ),
    )
    fetcher = registry.resolve_model("bls", "BlsSearch")
    original_transform = fetcher.transform_query
    captured: dict[str, object] = {}

    def capture_query(**kwargs: object) -> object:
        captured.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)

    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        response = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "offset": 2, "limit": 1, "source": "auto"}},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "bls"
    assert data["model"] == "BlsSearch"
    assert data["domain"] == "bls_search"
    assert data["verified"] is False
    assert data["completeness"] == "NOT_ASSESSED"
    assert datetime.fromisoformat(data["observed_at"]).utcoffset() == timedelta(0)
    assert data["pagination"] == {"total": 7, "offset": 2, "limit": 1}
    assert data["results"][0]["dimensions"] == {"industry_code": "000000"}
    assert data["results"][0]["source_metadata"] == {"source_file": "ce.series"}
    assert data["results"][0]["catalog_as_of"] is None
    assert captured["source"] == "bls"
    assert captured["market"] == "us"
    assert len(calls) == 1


def test_bls_series_retains_nested_footnotes_and_nullable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    monkeypatch.setattr(
        bls_series._client,
        "fetch_observations",
        lambda **_kwargs: [
            {
                "series_id": "SERIES_A",
                "year": "2024",
                "period": "M01",
                "period_name": "January",
                "value": "123.5",
                "footnotes": [{"code": "P", "text": "Preliminary."}],
                "latest": None,
                "api_version": "v1",
            }
        ],
    )

    with _client(registry, ctx=_fixture_context("bls", "BlsSeries")) as client:
        response = client.post(
            _path("bls", "BlsSeries"),
            json={
                "query": {
                    "series_ids": ["SERIES_A"],
                    "start_year": 2024,
                    "end_year": 2024,
                    "max_requests": 1,
                }
            },
        )

    assert response.status_code == 200
    row = response.json()["data"]["results"][0]
    assert row["footnotes"] == [{"code": "P", "text": "Preliminary."}]
    assert row["latest"] is None
    assert row["preliminary"] is True


def test_fred_series_preserves_revisions_request_context_and_null_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    monkeypatch.setattr(
        fred_series,
        "fetch_observation_pages",
        lambda **_kwargs: [
            {
                "date": "2024-01-01",
                "value": "1.25",
                "realtime_start": "2024-02-01",
                "realtime_end": "2024-02-28",
            },
            {
                "date": "2024-01-01",
                "value": ".",
                "realtime_start": "2024-03-01",
                "realtime_end": "9999-12-31",
            },
        ],
    )

    with _client(registry, ctx=_fixture_context("fred", "FredSeries")) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            json={"query": {"series_id": "GDP", "frequency": "q"}},
        )

    assert response.status_code == 200
    rows = response.json()["data"]["results"]
    assert len(rows) == 2
    assert rows[0]["realtime_start"] == date(2024, 2, 1).isoformat()
    assert rows[1]["realtime_start"] == date(2024, 3, 1).isoformat()
    assert rows[0]["requested_frequency"] == "q"
    assert rows[0]["requested_aggregation_method"] == "avg"
    assert rows[1]["value"] is None
    assert response.json()["data"]["pagination"] is None


def test_fmp_quote_preserves_large_timestamp_and_unknown_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    timestamp = 9_007_199_254_740_993
    monkeypatch.setattr(
        equity_quote,
        "fetch_array",
        lambda *_args, **_kwargs: [
            {"symbol": "AAPL", "name": None, "price": 101.25, "timestamp": timestamp}
        ],
    )

    with _client(registry, ctx=_fixture_context("fmp", "EquityQuote")) as client:
        response = client.post(_path("fmp", "EquityQuote"), json={"query": {"symbol": "AAPL"}})

    assert response.status_code == 200
    row = response.json()["data"]["results"][0]
    assert row["provider_timestamp"] == timestamp
    assert row["timestamp_unit"] is None
    assert row["timestamp_timezone"] is None
    assert row["name"] is None
    assert response.json()["data"]["pagination"] is None


def test_source_market_and_context_controls_fail_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    transport_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: transport_calls.append(kwargs),
    )

    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        source_conflict = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "source": "fred"}},
        )
        market_conflict = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "market": "ca"}},
        )
        context_in_query = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "request_budget": "secret-sentinel"}},
        )
        unknown_top_level = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce"}, "ctx": "secret-sentinel"},
        )

    assert [
        source_conflict.status_code,
        market_conflict.status_code,
        context_in_query.status_code,
        unknown_top_level.status_code,
    ] == [400, 400, 400, 400]
    assert "secret-sentinel" not in context_in_query.text
    assert "secret-sentinel" not in unknown_top_level.text
    assert transport_calls == []


def test_invalid_query_and_untrusted_context_are_rejected_before_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    transport_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: transport_calls.append(kwargs),
    )
    invalid_context = FetchContext(
        timeout=5,
        request_budget=RequestBudget(),
        operation=RequestOperation.STORE,
    )

    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        invalid_query = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "search_text": "bad\nvalue"}},
        )
    with _client(registry, ctx=invalid_context) as client:
        invalid_context_response = client.post(
            _path("bls", "BlsSearch"), json={"query": {"survey": "ce"}}
        )

    assert invalid_query.status_code == 400
    assert invalid_context_response.status_code == 400
    assert transport_calls == []


def test_zero_grant_context_rejects_at_fetcher_admission_without_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    transport_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: transport_calls.append(kwargs),
    )

    with _client(registry, inject_context=False) as client:
        response = client.post(_path("bls", "BlsSearch"), json={"query": {"survey": "ce"}})

    assert response.status_code == 403
    assert response.json()["detail"] == "Provider model query is not authorized"
    assert transport_calls == []


@pytest.mark.parametrize(
    ("source", "model", "domain", "query"),
    (
        ("bls", "BlsSearch", "bls_search", {"survey": "ce"}),
        (
            "bls",
            "BlsSeries",
            "bls_series",
            {"series_ids": ["SERIES_A"], "start_year": 2024, "end_year": 2024, "max_requests": 1},
        ),
        ("fred", "FredSearch", "fred_search", {"search_text": "GDP"}),
        ("fred", "FredSeries", "fred_series", {"series_id": "GDP"}),
        ("fmp", "EquityHistorical", "equity_historical", {"symbol": "AAPL"}),
        ("fmp", "EquityQuote", "equity_quote", {"symbol": "AAPL"}),
    ),
)
def test_production_specs_still_have_zero_grant_before_fetcher_extraction(
    source: str,
    model: str,
    domain: str,
    query: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.data.domains import load_domains

    spec = load_domains()[domain]
    assert spec.semantics_declared is True
    assert "query" in spec.permissions

    registry = _local_registry()
    fetcher = registry.resolve_model(source, model)
    extraction_calls: list[object] = []

    def reject_extraction(*_args: object, **_kwargs: object) -> object:
        extraction_calls.append(model)
        raise AssertionError("zero-grant request reached provider extraction")

    monkeypatch.setattr(fetcher, "extract_data", reject_extraction)
    with _client(registry, inject_context=False) as client:
        response = client.post(_path(source, model), json={"query": query})

    assert response.status_code == 403
    assert response.json()["detail"] == "Provider model query is not authorized"
    assert extraction_calls == []


def test_principal_authorization_precedes_domain_semantics_and_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    from opendata.data import domains

    def unreviewed(_domain: str) -> DomainSpec:
        raise AssertionError("domain semantics were inspected before authorization")

    monkeypatch.setattr(domains, "require_domain_semantics", unreviewed)
    io_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: io_calls.append(kwargs),
    )

    with _client(registry, scopes=("some_other_domain",)) as client:
        response = client.post(_path("bls", "BlsSearch"), json={"query": {"survey": "ce"}})

    assert response.status_code == 403
    assert io_calls == []


def test_domain_without_query_permission_is_unavailable_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    from opendata.data import domains

    no_query_permission = DomainSpec(
        display_name="BLS search",
        rest_path="provider/bls-search",
        contract="BlsCatalogItem",
        temporal_kind="snapshot",
        time_field=None,
        natural_key=("series_id",),
        filter_dims=("survey",),
        storage_mode="transient",
        permissions=(),
    )
    monkeypatch.setattr(
        domains,
        "require_domain_semantics",
        lambda domain: no_query_permission,
    )
    io_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: io_calls.append(kwargs),
    )

    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        response = client.post(_path("bls", "BlsSearch"), json={"query": {"survey": "ce"}})

    assert response.status_code == 503
    assert io_calls == []


@pytest.mark.parametrize(
    ("source", "model"),
    (
        ("bls", "BlsSearch"),
        ("bls", "BlsSeries"),
        ("fred", "FredSearch"),
        ("fred", "FredSeries"),
        ("fmp", "EquityHistorical"),
        ("fmp", "EquityQuote"),
    ),
)
def test_production_six_models_remain_unavailable_without_reviewed_domains(
    source: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_provider_model_domains(monkeypatch)
    registry = _local_registry()
    calls: list[object] = []
    monkeypatch.setattr(bls_search._client, "fetch_catalog", lambda **kwargs: calls.append(kwargs))

    with _client(registry) as client:
        response = client.post(_path(source, model), json={"query": {}})

    assert response.status_code == 503
    assert calls == []


def test_exact_identity_conflicts_and_unknown_models_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    fetcher = registry.resolve_model("bls", "BlsSearch")
    monkeypatch.setattr(fetcher, "canonical_model", "AnotherModel")
    io_calls: list[object] = []
    monkeypatch.setattr(
        bls_search._client,
        "fetch_catalog",
        lambda **kwargs: io_calls.append(kwargs),
    )

    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        mismatched_binding = client.post(
            _path("bls", "BlsSearch"), json={"query": {"survey": "ce"}}
        )
        unknown_model = client.post(_path("bls", "MissingModel"), json={"query": {"survey": "ce"}})
        malformed_source = client.post(_path("auto", "BlsSearch"), json={"query": {"survey": "ce"}})

    assert mismatched_binding.status_code == 503
    assert unknown_model.status_code == 404
    assert malformed_source.status_code == 400
    assert io_calls == []


@pytest.mark.parametrize(
    ("error", "expected_status"),
    (
        (RequestAttemptLimitError("private attempt limit text"), 429),
        (RequestAuthorizationError("private authorization text"), 403),
        (RequestBudgetScopeError("private scope text"), 403),
        (RequestBudgetError("private unclassified budget text"), 503),
        (AsyncExecutionTimeoutError("private timeout text"), 504),
        (RequestExecutionDeadlineError("private deadline text"), 504),
        (RequestExecutionCancelledError("private cancellation text"), 503),
        (RuntimeError("upstream-secret-sentinel"), 502),
    ),
)
def test_execution_errors_are_classified_without_reflecting_causes(
    error: Exception,
    expected_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    fetcher = registry.resolve_model("bls", "BlsSearch")

    async def fail_fetch(*, ctx: FetchContext | None = None, **_kwargs: object) -> object:
        del ctx
        raise error

    monkeypatch.setattr(fetcher, "fetch_async", fail_fetch)
    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        response = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "search_text": "query-secret-sentinel"}},
        )

    assert response.status_code == expected_status
    assert "private" not in response.text
    assert "upstream-secret-sentinel" not in response.text
    assert "query-secret-sentinel" not in response.text


@pytest.mark.parametrize("shape", ("mapping", "constructed_row"))
def test_unapproved_output_shapes_and_construct_bypass_are_rejected(
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    fetcher = registry.resolve_model("fred", "FredSeries")

    if shape == "mapping":
        bad_output: object = {"series_id": "GDP"}
    else:
        bad_output = (
            SeriesObservation.model_construct(
                series_id="GDP",
                date="not-a-date",
                value="not-a-number",
                realtime_start="bad",
                realtime_end="bad",
                transform_units="invalid",
                output_type=99,
                requested_frequency=None,
                requested_aggregation_method="unknown",
            ),
        )

    async def return_bad_output(*, ctx: FetchContext | None = None, **_kwargs: object) -> object:
        del ctx
        return bad_output

    monkeypatch.setattr(fetcher, "fetch_async", return_bad_output)
    with _client(registry, ctx=_fixture_context("fred", "FredSeries")) as client:
        response = client.post(_path("fred", "FredSeries"), json={"query": {"series_id": "GDP"}})

    assert response.status_code == 502
    assert response.json()["detail"] == "Provider model query failed"


@pytest.mark.parametrize(
    ("total", "offset"),
    ((0, 0), (1, 1), (True, 0)),
)
def test_bls_page_rejects_items_beyond_reported_total_without_dropping_them(
    total: int,
    offset: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    fetcher = registry.resolve_model("bls", "BlsSearch")
    item = BlsCatalogItem.model_validate(
        {"series_id": "SERIES_A", "survey": "CE", "title": "Employment"}
    )
    invalid_page = BlsCatalogPage(items=(item,), total=total, offset=offset, limit=1)

    async def return_invalid_page(*, ctx: FetchContext | None = None, **_kwargs: object) -> object:
        del ctx
        return invalid_page

    monkeypatch.setattr(fetcher, "fetch_async", return_invalid_page)
    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        response = client.post(_path("bls", "BlsSearch"), json={"query": {"survey": "ce"}})

    assert response.status_code == 502
    assert response.json()["detail"] == "Provider model query failed"


def test_fetcher_query_exception_does_not_leak_body_or_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)

    def raise_transport_secret(**_kwargs: object) -> object:
        raise RuntimeError("upstream-secret-sentinel")

    monkeypatch.setattr(bls_search._client, "fetch_catalog", raise_transport_secret)
    with _client(registry, ctx=_fixture_context("bls", "BlsSearch")) as client:
        response = client.post(
            _path("bls", "BlsSearch"),
            json={"query": {"survey": "ce", "search_text": "query-secret-sentinel"}},
        )

    assert response.status_code == 502
    assert "upstream-secret-sentinel" not in response.text
    assert "query-secret-sentinel" not in response.text


@pytest.mark.asyncio
async def test_task_cancel_propagates_as_cancelled_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _local_registry()
    _complete_domain_specs(monkeypatch, registry)
    fetcher = registry.resolve_model("bls", "BlsSearch")

    started = asyncio.Event()

    async def wait_for_cancellation(
        *, ctx: FetchContext | None = None, **_kwargs: object
    ) -> object:
        del ctx
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(fetcher, "fetch_async", wait_for_cancellation)
    principal = SimpleNamespace(allows_domain=lambda _domain: True)
    task = asyncio.create_task(
        query_provider_model(
            registry=registry,
            source="bls",
            model="BlsSearch",
            query={"survey": "ce"},
            principal=principal,
            ctx=_fixture_context("bls", "BlsSearch"),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel("private cancellation text")
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
