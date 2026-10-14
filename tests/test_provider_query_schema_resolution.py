"""Static query-schema resolution and catalog projection contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, create_model

from opendata.data.capability import Capability
from opendata.data.protocol import BaseQueryParams, MultiSymbolQueryParams, QueryParams
from opendata.services.provider_models import (
    ProviderModelSchemaError,
    query_schema_for_model,
    resolve_query_model_type,
)

_PROVIDER_CALLS: list[str] = []


class _LegacyQuery(QueryParams):
    search_text: str


class _GeneratedQuery(BaseModel):
    page: int = 1


class _ConcreteBaseQuery(BaseQueryParams):
    tenant: str = "default"


def _schema_tripwire() -> type[BaseModel]:
    _PROVIDER_CALLS.append("annotation")
    return _LegacyQuery


class _LegacyFetcher:
    def transform_query(self, **kwargs: object) -> _LegacyQuery:
        _PROVIDER_CALLS.append("transform_query")
        return _LegacyQuery.model_validate(kwargs)


class _PlainLegacyFetcher:
    def transform_query(self, **kwargs: object) -> QueryParams:
        _PROVIDER_CALLS.append("plain-transform_query")
        return QueryParams.model_validate(kwargs)


class _QueryTypeFetcher:
    def _query_type(self) -> type[_LegacyQuery]:
        _PROVIDER_CALLS.append("_query_type")
        return _LegacyQuery


class _ExplodingDescriptor:
    def __get__(self, _instance: object, _owner: type[object]) -> object:
        _PROVIDER_CALLS.append("descriptor")
        raise AssertionError("query declaration descriptor executed")


class _MaliciousGenericAnnotation:
    @property
    def __origin__(self) -> object:
        _PROVIDER_CALLS.append("annotation-origin")
        raise AssertionError("annotation descriptor executed")


class _DescriptorFetcher:
    query_model = _ExplodingDescriptor()

    def transform_query(self, **kwargs: object) -> _LegacyQuery:
        _PROVIDER_CALLS.append("descriptor-fallback")
        return _LegacyQuery.model_validate(kwargs)


class _PropertyFetcher:
    @property
    def query_model(self) -> type[_LegacyQuery]:
        _PROVIDER_CALLS.append("property")
        raise AssertionError("query-model property executed")

    def transform_query(self, **kwargs: object) -> _LegacyQuery:
        _PROVIDER_CALLS.append("property-fallback")
        return _LegacyQuery.model_validate(kwargs)


class _CatalogQuery(QueryParams):
    symbol: str
    limit: int = 100


class _CatalogFetcher:
    query_model = _CatalogQuery

    def transform_query(self, **kwargs: object) -> _CatalogQuery:
        _PROVIDER_CALLS.append("catalog-transform_query")
        return _CatalogQuery.model_validate(kwargs)


class _UnavailableCatalogFetcher:
    query_model = _ExplodingDescriptor()

    def transform_query(self, **kwargs: object) -> _CatalogQuery:
        _PROVIDER_CALLS.append("unavailable-catalog-transform_query")
        return _CatalogQuery.model_validate(kwargs)


def _catalog_capability(
    fetcher: object,
    *,
    endpoint: dict[str, str] | None = None,
) -> dict[str, Any]:
    from opendata.api.data_query import _catalog_capability as catalog_capability

    capability = Capability(
        asset_class="stock",
        domain="schema_test",
        period="daily",
        market="us",
        source="schema_test",
        verified=False,
        notes="offline schema test",
    )
    return catalog_capability(
        capability,
        fetcher,  # type: ignore[arg-type]
        domain_defined=True,
        model_query_endpoint=endpoint,
    )


def test_static_declarations_and_generated_pydantic_models_are_accepted() -> None:
    generated_model = create_model("GeneratedQueryFromFactory", limit=(int, 25))

    class GeneratedFetcher:
        query_model = generated_model

    assert resolve_query_model_type(GeneratedFetcher) is generated_model
    assert resolve_query_model_type(_CatalogFetcher) is _CatalogQuery


def test_legacy_queryparams_return_annotation_is_resolved_without_calling_fetcher() -> None:
    _PROVIDER_CALLS.clear()
    assert resolve_query_model_type(_LegacyFetcher) is _LegacyQuery
    assert resolve_query_model_type(_PlainLegacyFetcher) is QueryParams
    assert _PROVIDER_CALLS == []


def test_shared_query_bases_are_rejected_but_concrete_subclasses_are_accepted() -> None:
    _PROVIDER_CALLS.clear()

    def transform_query(**kwargs: object) -> BaseQueryParams:
        _PROVIDER_CALLS.append("transform_query")
        return BaseQueryParams.model_validate(kwargs)

    base_fetcher = type(
        "_BaseQueryParamsFetcher",
        (),
        {"query_model": BaseQueryParams, "transform_query": transform_query},
    )
    base_model_fetcher = type(
        "_BaseModelFetcher",
        (),
        {"query_model": BaseModel, "transform_query": transform_query},
    )
    multi_symbol_fetcher = type(
        "_MultiSymbolQueryParamsFetcher",
        (),
        {"query_model": MultiSymbolQueryParams, "transform_query": transform_query},
    )
    concrete_fetcher = type(
        "_ConcreteBaseQueryFetcher",
        (),
        {"query_model": _ConcreteBaseQuery, "transform_query": transform_query},
    )

    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(base_fetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(base_model_fetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(multi_symbol_fetcher)
    assert resolve_query_model_type(concrete_fetcher) is _ConcreteBaseQuery
    assert _PROVIDER_CALLS == []


def test_safe_query_type_return_annotation_is_resolved_without_calling_factory() -> None:
    _PROVIDER_CALLS.clear()
    assert resolve_query_model_type(_QueryTypeFetcher) is _LegacyQuery
    _QueryTypeFetcher._query_type.__annotations__["return"] = type[_LegacyQuery]
    assert resolve_query_model_type(_QueryTypeFetcher) is _LegacyQuery
    assert _PROVIDER_CALLS == []


def test_unknown_and_malicious_forward_annotations_fail_closed_without_execution() -> None:
    class UnsafeTransformFetcher:
        def transform_query(self, **_kwargs: object) -> BaseModel:
            _PROVIDER_CALLS.append("unsafe-transform")
            return _LegacyQuery()

    class UnsafeFactoryFetcher:
        def _query_type(self) -> type[BaseModel]:
            _PROVIDER_CALLS.append("unsafe-factory")
            return _LegacyQuery

    class UnknownForwardFetcher:
        def transform_query(self, **_kwargs: object) -> BaseModel:
            _PROVIDER_CALLS.append("unknown-forward")
            return _LegacyQuery()

    class BroadBaseFetcher:
        def transform_query(self, **_kwargs: object) -> BaseModel:
            _PROVIDER_CALLS.append("broad-base")
            return _LegacyQuery()

    class MaliciousGenericFetcher:
        def _query_type(self) -> type[BaseModel]:
            _PROVIDER_CALLS.append("malicious-generic")
            return _LegacyQuery

    UnsafeTransformFetcher.transform_query.__annotations__["return"] = "_schema_tripwire()"
    UnsafeFactoryFetcher._query_type.__annotations__["return"] = "type[_schema_tripwire()]"
    UnknownForwardFetcher.transform_query.__annotations__["return"] = "UnregisteredQueryType"
    BroadBaseFetcher.transform_query.__annotations__["return"] = BaseModel
    MaliciousGenericFetcher._query_type.__annotations__["return"] = _MaliciousGenericAnnotation()
    _PROVIDER_CALLS.clear()

    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(UnsafeTransformFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(UnsafeFactoryFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(UnknownForwardFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(BroadBaseFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(MaliciousGenericFetcher)
    assert _PROVIDER_CALLS == []


def test_invalid_explicit_descriptor_and_conflicting_declarations_do_not_fallback() -> None:
    class ConflictingFetcher:
        query_model = _LegacyQuery
        query_type = _GeneratedQuery

        def transform_query(self, **_kwargs: object) -> _LegacyQuery:
            _PROVIDER_CALLS.append("conflicting-transform")
            return _LegacyQuery(search_text="x")

    _PROVIDER_CALLS.clear()
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(_DescriptorFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(_PropertyFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(ConflictingFetcher)
    with pytest.raises(ProviderModelSchemaError):
        resolve_query_model_type(BaseModel)
    assert _PROVIDER_CALLS == []


def test_canonical_schema_service_preserves_safe_legacy_schema_and_error_mapping() -> None:
    fetcher = _LegacyFetcher()

    class Registry:
        def resolve_model(self, _source: str, _model: str) -> object:
            return fetcher

    descriptor = SimpleNamespace(source="schema_test", model="LegacyQuery")
    _PROVIDER_CALLS.clear()
    schema = query_schema_for_model(Registry(), descriptor)  # type: ignore[arg-type]
    assert schema == _LegacyQuery.model_json_schema(mode="validation")
    assert _PROVIDER_CALLS == []

    class InvalidFetcher:
        def transform_query(self, **_kwargs: object) -> str:
            return "not a model"

    class InvalidRegistry:
        def resolve_model(self, _source: str, _model: str) -> object:
            return InvalidFetcher()

    with pytest.raises(ProviderModelSchemaError) as caught:
        query_schema_for_model(InvalidRegistry(), descriptor)  # type: ignore[arg-type]
    assert str(caught.value) == "registered model query schema is invalid"


def test_catalog_available_shape_preserves_parameter_fields_and_endpoint() -> None:
    endpoint = {"method": "POST", "path": "/api/v1/providers/schema_test/models/Catalog"}
    _PROVIDER_CALLS.clear()
    result = _catalog_capability(_CatalogFetcher(), endpoint=endpoint)

    assert result == {
        "asset_class": "stock",
        "domain": "schema_test",
        "period": "daily",
        "market": "us",
        "source": "schema_test",
        "verified": False,
        "notes": "offline schema test",
        "callable": {
            "module": __name__,
            "name": "_CatalogFetcher.fetch",
        },
        "endpoint": {
            "name": "query_domain_data",
            "method": "GET",
            "path": "/api/v1/data/stock/schema_test",
            "query_filters": {"source": "schema_test", "period": "daily"},
        },
        "model_query_endpoint": endpoint,
        "parameters": [
            {"name": "source", "type": "str", "required": False, "description": None},
            {"name": "market", "type": "str | None", "required": False, "description": None},
            {"name": "symbol", "type": "str", "required": True, "description": None},
            {
                "name": "start_date",
                "type": "datetime.date | None",
                "required": False,
                "description": None,
            },
            {
                "name": "end_date",
                "type": "datetime.date | None",
                "required": False,
                "description": None,
            },
            {"name": "limit", "type": "int", "required": False, "description": None},
        ],
        "query_schema_status": "available",
        "query_schema_error": None,
    }
    assert _PROVIDER_CALLS == []


def test_catalog_unavailable_shape_is_sanitized_and_does_not_call_provider_code() -> None:
    endpoint = {"method": "POST", "path": "/api/v1/providers/schema_test/models/Unknown"}
    _PROVIDER_CALLS.clear()
    result = _catalog_capability(_UnavailableCatalogFetcher(), endpoint=endpoint)

    assert result == {
        "asset_class": "stock",
        "domain": "schema_test",
        "period": "daily",
        "market": "us",
        "source": "schema_test",
        "verified": False,
        "notes": "offline schema test",
        "callable": {
            "module": __name__,
            "name": "_UnavailableCatalogFetcher.fetch",
        },
        "endpoint": {
            "name": "query_domain_data",
            "method": "GET",
            "path": "/api/v1/data/stock/schema_test",
            "query_filters": {"source": "schema_test", "period": "daily"},
        },
        "model_query_endpoint": endpoint,
        "parameters": [],
        "query_schema_status": "unavailable",
        "query_schema_error": "PROVIDER_QUERY_SCHEMA_UNAVAILABLE",
    }
    assert "AssertionError" not in str(result)
    assert "descriptor" not in str(result)
    assert _PROVIDER_CALLS == []


def test_real_generic_and_leaf_query_declarations_resolve_to_exact_models() -> None:
    from opendata.data.providers.benzinga.models.price_target import (
        _QUERY_MODEL,
        BenzingaPriceTargetFetcher,
    )
    from opendata.data.providers.fmp.models.iter2_market import (
        EquityInfoFetcher,
        HistoricalQuery,
        IndexHistoricalFetcher,
        MultiSymbolQuery,
    )
    from opendata.data.providers.fred.models.iter2_rates_extra import (
        BondIndicesFetcher,
        CommercialPaperFetcher,
        CommoditySpotPricesFetcher,
        EuroShortTermRateFetcher,
        FredBondIndicesQuery,
        FredCommercialPaperQuery,
        FredCommoditySpotPricesQuery,
        FredEuroShortTermRateQuery,
        FredHighQualityMarketCorporateBondQuery,
        FredSpotRateQuery,
        FredYieldCurveQuery,
        HighQualityMarketCorporateBondFetcher,
        SpotRateFetcher,
        YieldCurveFetcher,
    )
    from opendata.data.providers.yfinance.models.market_iter2 import (
        YfinanceEquityHistoricalFetcher,
        YfinanceEquityHistoricalQuery,
    )

    cases = (
        (IndexHistoricalFetcher, HistoricalQuery),
        (EquityInfoFetcher, MultiSymbolQuery),
        (YfinanceEquityHistoricalFetcher, YfinanceEquityHistoricalQuery),
        (BenzingaPriceTargetFetcher, _QUERY_MODEL),
        (EuroShortTermRateFetcher, FredEuroShortTermRateQuery),
        (CommercialPaperFetcher, FredCommercialPaperQuery),
        (SpotRateFetcher, FredSpotRateQuery),
        (HighQualityMarketCorporateBondFetcher, FredHighQualityMarketCorporateBondQuery),
        (BondIndicesFetcher, FredBondIndicesQuery),
        (CommoditySpotPricesFetcher, FredCommoditySpotPricesQuery),
        (YieldCurveFetcher, FredYieldCurveQuery),
    )
    for fetcher_type, expected_model in cases:
        assert resolve_query_model_type(fetcher_type) is expected_model


def test_catalog_parameter_metadata_does_not_change_for_legacy_query_model() -> None:
    from opendata.api.data_query import _catalog_capability

    capability = Capability(
        asset_class="stock",
        domain="schema_test",
        period="daily",
        market="us",
        source="schema_test",
        verified=False,
    )
    result = _catalog_capability(
        capability,
        _PlainLegacyFetcher(),  # type: ignore[arg-type]
        domain_defined=False,
    )
    assert result["parameters"] == [
        {"name": "source", "type": "str", "required": False, "description": None},
        {"name": "market", "type": "str | None", "required": False, "description": None},
        {"name": "symbol", "type": "str | None", "required": False, "description": None},
        {
            "name": "start_date",
            "type": "datetime.date | None",
            "required": False,
            "description": None,
        },
        {
            "name": "end_date",
            "type": "datetime.date | None",
            "required": False,
            "description": None,
        },
    ]
    assert result["endpoint"] is None
    assert result["query_schema_status"] == "available"
    assert result["query_schema_error"] is None
