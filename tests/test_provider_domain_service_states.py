"""Offline contracts for provider-backed domain catalog and legacy routes."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, NoReturn, cast

import pytest
from fastapi import HTTPException

if TYPE_CHECKING:
    from sqlalchemy import Engine

from opendata.api import data_query
from opendata.api.dependencies import Principal
from opendata.data.domains import load_domains
from opendata.data.providers import register_providers
from opendata.data.registry import get_registry
from opendata.models.user import User

PROVIDER_MODELS = (
    ("fred_search", "fred", "FredSearch", "transient_model"),
    ("fred_series", "fred", "FredSeries", "warehouse_not_ready"),
    ("bls_search", "bls", "BlsSearch", "transient_model"),
    ("bls_series", "bls", "BlsSeries", "warehouse_not_ready"),
    ("equity_historical", "fmp", "EquityHistorical", "warehouse_not_ready"),
    ("equity_quote", "fmp", "EquityQuote", "transient_model"),
)


@pytest.fixture(autouse=True)
def registered_providers() -> None:
    """Populate the shared Registry without contacting provider sources."""
    register_providers()


@pytest.fixture
def catalog_without_warehouse_io(monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Serve the metadata catalog without a database or calendar read."""

    async def fixed_expected(_engine: object, *, on: date | None = None) -> date:
        del on
        return date(2026, 10, 8)

    async def no_diff_reports(_engine: object) -> None:
        return None

    monkeypatch.setattr(data_query, "_expected_data_date", fixed_expected)
    monkeypatch.setattr(data_query, "_diff_report_counts", no_diff_reports)
    return cast("Engine", object())


def _principal(domain: str) -> Principal:
    return Principal(
        user=User(id=1, username="provider-domain-state-probe"),
        api_key_id=None,
        scopes=(domain,),
    )


def _reject_warehouse(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("provider-query catalog row reached a warehouse helper")


@pytest.mark.parametrize(("domain", "source", "model", "reason"), PROVIDER_MODELS)
async def test_declared_provider_models_are_metadata_only_until_warehouse_ready(
    domain: str,
    source: str,
    model: str,
    reason: str,
    catalog_without_warehouse_io: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    spec = load_domains()[domain]
    capability = next(
        cap for cap in registry.capabilities() if cap.domain == domain and cap.source == source
    )
    fetcher = registry.resolve(
        capability.asset_class,
        capability.domain,
        period=capability.period,
        market=capability.market,
        source=capability.source,
    )
    extraction_calls: list[str] = []

    def reject_source(*_args: object, **_kwargs: object) -> object:
        extraction_calls.append(model)
        raise AssertionError("catalog must not execute provider fetchers")

    monkeypatch.setattr(fetcher, "fetch_async", reject_source)
    for helper in (
        "dwd_table",
        "ods_table",
        "_display_name",
        "_freshness",
        "_coverage_facts",
        "_source_leg",
        "_quality",
    ):
        monkeypatch.setattr(data_query, helper, _reject_warehouse)

    response = await data_query.data_catalog(
        principal=_principal(domain), engine=catalog_without_warehouse_io
    )

    assert response.data is not None
    data = response.data
    assert len(data["domains"]) == 1
    row = data["domains"][0]
    assert row["domain"] == domain
    assert row["display_name"] == spec.display_name
    assert row["domain_defined"] is True
    assert row["service_state"] == "provider_query"
    assert row["reason"] == reason
    assert row["temporal_kind"] == spec.temporal_kind
    assert row["time_field"] == spec.time_field
    assert row["natural_key"] == list(spec.natural_key)
    assert row["filter_dims"] == list(spec.filter_dims)
    assert row["storage_mode"] == spec.storage_mode
    assert row["permissions"] == list(spec.permissions)
    assert row["status"] == "unmapped"
    for field in ("layer", "table", "freshness_field", "latest", "lag_days", "coverage", "quality"):
        assert row[field] is None

    capability_row = next(item for item in row["capabilities"] if item["source"] == source)
    assert capability_row["endpoint"] is None
    assert capability_row["model_query_endpoint"] == {
        "model": model,
        "method": "POST",
        "path": f"/api/v1/providers/{source}/models/{model}/query",
    }
    assert row["sources"] == [
        {
            "source": source,
            "verified": capability.verified,
            "table": None,
            "status": "unmapped",
            "reason": reason,
            "latest": None,
            "lag_days": None,
        }
    ]
    assert extraction_calls == []


@pytest.mark.parametrize(("domain", "source", "model", "_reason"), PROVIDER_MODELS)
@pytest.mark.parametrize("legacy_route", ("query", "export", "freshness"))
async def test_legacy_date_routes_return_501_before_warehouse_access(
    domain: str,
    source: str,
    model: str,
    _reason: str,
    legacy_route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    capability = next(
        cap for cap in registry.capabilities() if cap.domain == domain and cap.source == source
    )
    principal = _principal(domain)
    engine = cast("Engine", object())
    for helper in (
        "dwd_table",
        "ods_table",
        "_table_columns",
        "_expected_data_date",
        "_freshness",
        "_fetch_rows",
        "_validated_query",
        "window_bounds",
    ):
        monkeypatch.setattr(data_query, helper, _reject_warehouse)

    with pytest.raises(HTTPException) as error:
        if legacy_route == "query":
            await data_query.query_domain_data(
                capability.asset_class, domain, principal, engine=engine
            )
        elif legacy_route == "export":
            await data_query.export_domain_data(
                capability.asset_class, domain, principal, engine=engine
            )
        else:
            await data_query.domain_freshness(
                domain, principal=principal, source=None, engine=engine
            )

    assert error.value.status_code == 501
    assert f"POST /api/v1/providers/{source}/models/{model}/query" in error.value.detail


async def test_legacy_provider_model_route_still_checks_principal_scope_first() -> None:
    capability = next(
        cap
        for cap in get_registry().capabilities()
        if cap.domain == "fred_search" and cap.source == "fred"
    )
    principal = Principal(
        user=User(id=1, username="out-of-scope"),
        api_key_id=None,
        scopes=("stock_daily",),
    )

    with pytest.raises(HTTPException) as error:
        await data_query.query_domain_data(
            capability.asset_class,
            capability.domain,
            principal,
            engine=cast("Engine", object()),
        )

    assert error.value.status_code == 403


def test_model_query_endpoint_requires_exact_reviewed_registry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    capability = next(
        cap
        for cap in registry.capabilities()
        if cap.domain == "fred_search" and cap.source == "fred"
    )
    fetcher = registry.resolve(
        capability.asset_class,
        capability.domain,
        period=capability.period,
        market=capability.market,
        source=capability.source,
    )
    spec = load_domains()[capability.domain]
    descriptors = registry.list_model_descriptors()

    assert data_query._provider_model_query_endpoint_for_capability(
        capability, fetcher, spec, descriptors
    ) == {
        "model": "FredSearch",
        "method": "POST",
        "path": "/api/v1/providers/fred/models/FredSearch/query",
    }

    monkeypatch.setattr(fetcher, "canonical_model", "legacy_module_alias")
    assert (
        data_query._provider_model_query_endpoint_for_capability(
            capability, fetcher, spec, descriptors
        )
        is None
    )

    monkeypatch.setattr(fetcher, "canonical_model", "FredSearch")
    unqueryable_spec = spec.model_copy(update={"permissions": ()})
    assert (
        data_query._provider_model_query_endpoint_for_capability(
            capability, fetcher, unqueryable_spec, descriptors
        )
        is None
    )
