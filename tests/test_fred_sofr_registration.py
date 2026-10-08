"""Offline registration and governed API contracts for canonical FRED SOFR."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_query import (
    get_provider_model_query_context,
    get_provider_model_query_registry,
)
from opendata.api.provider_models import router
from opendata.data.domains import contract_model, require_domain_semantics
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models import FredSofrObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.fred.models import _sofr_client
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_export, provider_model_store, provider_model_warehouse

_FIELDS = (
    "series_id",
    "date",
    "value",
    "realtime_start",
    "realtime_end",
    "transform_units",
    "output_type",
    "requested_frequency",
    "requested_aggregation_method",
    "source_value",
    "native_units",
)
_SERIES = "SOFR"
_HOST = "api.stlouisfed.org"
_SCENARIO = "保留SOFR原始数值、修订区间与请求变换上下文的融资成本研究"
_DOMAIN_FIELDS = (
    "series_id",
    "date",
    "realtime_start",
    "realtime_end",
    "transform_units",
    "output_type",
    "requested_frequency",
    "requested_aggregation_method",
)


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fred", registry)
    return registry


def _context(operation: RequestOperation = RequestOperation.QUERY) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=_SERIES,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only synthetic SOFR registration grant",
        task_attempts=10,
        source_attempts=3,
        allowed_hosts={_HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(task_attempts=10, source_attempts=3, grants=(grant,)),
        operation=operation,
    )


def _api_client(
    registry: ProviderRegistry,
    *,
    allowed: bool = True,
    ctx: FetchContext | None = None,
    inject_context: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(allows_domain=lambda domain: allowed and domain == "sofr")
    app.dependency_overrides[get_provider_model_query_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    if inject_context:
        app.dependency_overrides[get_provider_model_query_context] = lambda: ctx
    return TestClient(app)


def _query_path(source: str = "fred", model: str = "SOFR") -> str:
    return f"/providers/{source}/models/{model}/query"


def _query(**overrides: object) -> dict[str, object]:
    return {
        "series_id": "SOFR",
        "start_date": "2025-01-02",
        "end_date": "2025-01-03",
        "page_size": 1,
        "max_records": 2,
        "max_pages": 2,
        **overrides,
    }


def _row(day: str, value: str) -> dict[str, str]:
    return {
        "date": day,
        "value": value,
        "realtime_start": day,
        "realtime_end": "9999-12-31",
    }


def _page(offset: int, day: str, value: str) -> dict[str, Any]:
    return {
        "count": 2,
        "offset": offset,
        "limit": 1,
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "2025-01-02",
        "observation_end": "2025-01-03",
        "observations": [_row(day, value)],
        "fixture_page_marker": f"page-{offset}",
    }


class _RecordingSession(requests.Session):
    def __init__(self, pages: tuple[dict[str, Any], ...]) -> None:
        super().__init__()
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append({"method": method, "url": url, "params": params})
        offset = int(params["offset"])
        page = self.pages[offset]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(page, separators=(",", ":")).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.url = url
        return response


def _install_recording_transport(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
) -> _RecordingSession:
    import opendata.data.http_client as http_client_module

    session = _RecordingSession(pages)
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    monkeypatch.setattr(_sofr_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(_sofr_client._client, "require_api_key", lambda: "fixture-only-key")
    return session


def test_sofr_binding_contract_and_query_only_domain_are_exact() -> None:
    provider = get_provider("fred")
    binding = provider.fetcher_dict["SOFR"]
    registry = _registry()
    fetcher = registry.resolve_model("fred", "SOFR")
    descriptor = next(
        item
        for item in registry.list_model_descriptors()
        if (item.source, item.model) == ("fred", "SOFR")
    )
    spec = require_domain_semantics("sofr")

    assert binding.module == "opendata.data.providers.fred.models.sofr"
    assert binding.class_name == "FredSofrFetcher"
    assert binding.canonical_model_ids == ("SOFR",)
    assert binding.scenario == _SCENARIO
    assert type(fetcher).__name__ == "FredSofrFetcher"
    assert fetcher.canonical_model == "SOFR"
    assert fetcher.capability.model_dump() == {
        "asset_class": "macro",
        "domain": "sofr",
        "period": "variable",
        "market": "us",
        "source": "fred",
        "verified": False,
        "notes": "",
    }
    assert descriptor.verified is False
    assert contract_model("sofr") is FredSofrObservation
    assert spec.contract == "FredSofrObservation"
    assert spec.temporal_kind == "series"
    assert spec.time_field == "date"
    assert spec.natural_key == _DOMAIN_FIELDS
    assert spec.filter_dims == _DOMAIN_FIELDS
    assert spec.storage_mode == "transient"
    assert spec.permissions == ("query",)
    assert spec.priority is None
    assert tuple(FredSofrObservation.model_fields) == _FIELDS


@pytest.mark.parametrize(
    "query",
    [
        {"series_id": "NOT_SOFR"},
        {"series_id": "SOFR", "output_type": True},
        {"series_id": "SOFR", "start_date": "2025-01-02T00:00:00"},
        {"series_id": "SOFR", "unexpected": "value"},
    ],
    ids=("unknown-series", "bool-output-type", "datetime-date", "extra-query-field"),
)
def test_invalid_sofr_queries_are_api_400_before_any_send(
    query: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _install_recording_transport(monkeypatch, (_page(0, "2025-01-02", "4.25"),))
    registry = _registry()

    with _api_client(registry, ctx=_context()) as client:
        response = client.post(_query_path(), json={"query": query})

    assert response.status_code == 400
    assert session.calls == []


def test_no_grant_and_domain_acl_both_deny_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_recording_transport(monkeypatch, (_page(0, "2025-01-02", "4.25"),))
    registry = _registry()

    with _api_client(registry, inject_context=False) as client:
        no_grant = client.post(_query_path(), json={"query": {"series_id": "SOFR"}})
    with _api_client(registry, allowed=False, ctx=_context()) as client:
        acl_denied = client.post(_query_path(), json={"query": {"series_id": "SOFR"}})

    assert no_grant.status_code == acl_denied.status_code == 403
    assert session.calls == []


def test_exact_grant_uses_governed_pagination_and_retains_typed_raw_and_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = (_page(0, "2025-01-02", "4.2500"), _page(1, "2025-01-03", "."))
    session = _install_recording_transport(monkeypatch, pages)
    registry = _registry()

    with _api_client(registry, ctx=_context()) as client:
        response = client.post(_query_path(), json={"query": _query()})

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert (payload["source"], payload["model"], payload["domain"]) == ("fred", "SOFR", "sofr")
    assert payload["verified"] is False
    assert [tuple(row) for row in payload["results"]] == [_FIELDS, _FIELDS]
    assert payload["results"][0]["value"] == 4.25
    assert payload["results"][0]["source_value"] == "4.2500"
    assert payload["results"][0]["native_units"] == "Percent"
    assert payload["results"][1]["value"] is None
    assert payload["results"][1]["source_value"] == "."
    assert payload["pagination"] is None

    fetcher = registry.resolve_model("fred", "SOFR")
    raw = asyncio.run(fetcher.fetch_raw_async(ctx=_context(), **_query()))
    captured = asyncio.run(
        fetcher.fetch_captured_async(
            ctx=_context(),
            capture=lambda source_pages, _query_model: source_pages,
            **_query(),
        )
    )

    assert raw == pages
    assert captured.capture == pages
    assert captured.capture[0]["fixture_page_marker"] == "page-0"
    assert captured.capture[1]["fixture_page_marker"] == "page-1"
    assert [item.source_value for item in captured.results] == ["4.2500", "."]
    assert len(session.calls) == 6
    assert [call["params"]["offset"] for call in session.calls] == ["0", "1"] * 3
    assert all(call["method"] == "GET" for call in session.calls)
    assert all(call["url"].startswith("https://api.stlouisfed.org/") for call in session.calls)
    assert all(call["params"]["series_id"] == "SOFR" for call in session.calls)
    assert all(call["params"]["units"] == "lin" for call in session.calls)


def test_unregistered_case_or_source_does_not_fallback_to_sofr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_recording_transport(monkeypatch, (_page(0, "2025-01-02", "4.25"),))
    registry = _registry()

    with _api_client(registry, ctx=_context()) as client:
        lowercase_model = client.post(
            _query_path(model="sofr"), json={"query": {"series_id": "SOFR"}}
        )
        wrong_provider = client.post(
            _query_path(source="fmp"), json={"query": {"series_id": "SOFR"}}
        )
        query_source_mismatch = client.post(
            _query_path(), json={"query": {"series_id": "SOFR", "source": "fmp"}}
        )

    assert lowercase_model.status_code == 404
    assert wrong_provider.status_code == 404
    assert query_source_mismatch.status_code == 400
    assert session.calls == []


def test_transient_sofr_domain_is_rejected_by_store_warehouse_and_export_before_engine() -> None:
    registry = _registry()
    engine_calls: list[str] = []

    def engine_factory() -> object:
        engine_calls.append("called")
        raise AssertionError("transient SOFR reached a local database engine")

    with pytest.raises(provider_model_store.ProviderModelStoreError):
        provider_model_store.write_provider_model_rows(
            engine=object(),
            registry=registry,
            source="fred",
            model="SOFR",
            rows=(),
            observed_at=datetime.now(timezone.utc),
            ctx=_context(RequestOperation.STORE),
        )

    with pytest.raises(provider_model_warehouse.ProviderModelWarehouseNotFoundError):
        asyncio.run(
            provider_model_warehouse.query_provider_model_warehouse(
                registry=registry,
                source="fred",
                model="SOFR",
                query={},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=engine_factory,
                ctx=_context(RequestOperation.QUERY),
            )
        )

    with pytest.raises(provider_model_export.ProviderModelExportValidationError):
        provider_model_export.export_provider_model_snapshot(
            engine_factory=engine_factory,
            registry=registry,
            source="fred",
            model="SOFR",
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_context(RequestOperation.EXPORT),
        )

    assert engine_calls == []
