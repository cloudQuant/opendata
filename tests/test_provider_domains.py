"""Production-domain declarations bind to canonical local provider models."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from opendata.data.domains import contract_model, load_domains, require_domain_semantics
from opendata.data.providers.catalog import register_providers
from opendata.data.registry import ProviderRegistry
from opendata.services.api_key_service import key_allows_domain, normalize_scope_input

if TYPE_CHECKING:
    from opendata.data.capability import Capability


LEGACY_DOMAIN_IDS = {
    "stock_daily",
    "economy_cpi",
    "economy_gdp",
    "economy_rate",
    "economy_unemployment",
    "stock_daily_overseas",
    "stock_adjust",
    "stock_action",
    "financial_statement",
    "financial_indicator",
    "index_constituent",
    "futures_daily",
    "index_daily",
    "fund_etf_daily",
    "option_daily",
    "bond_daily",
    "futures_fundamentals",
    "instrument",
    "trading_calendar",
    "fund_action",
}

EXPECTED_DOMAINS = {
    "fred_search": (
        "fred",
        "FredSearch",
        "FredSearchFetcher",
        "SeriesCatalogItem",
        "snapshot",
        "last_updated",
        ("series_id",),
        ("series_id", "frequency", "units", "seasonal_adjustment"),
        "transient",
        ("query",),
    ),
    "fred_series": (
        "fred",
        "FredSeries",
        "FredSeriesFetcher",
        "SeriesObservation",
        "series",
        "date",
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "upsert",
        ("query", "store", "export"),
    ),
    "sofr": (
        "fred",
        "SOFR",
        "FredSofrFetcher",
        "FredSofrObservation",
        "series",
        "date",
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "transient",
        ("query",),
    ),
    "sonia": (
        "fred",
        "SONIA",
        "FredSoniaFetcher",
        "FredSoniaObservation",
        "series",
        "date",
        (
            "series_id",
            "parameter",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "parameter",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "transient",
        ("query",),
    ),
    "bls_search": (
        "bls",
        "BlsSearch",
        "BlsSearchFetcher",
        "BlsCatalogItem",
        "snapshot",
        "catalog_as_of",
        ("survey", "series_id"),
        ("survey", "series_id"),
        "transient",
        ("query",),
    ),
    "bls_series": (
        "bls",
        "BlsSeries",
        "BlsSeriesFetcher",
        "BlsObservation",
        "series",
        "year",
        ("series_id", "year", "period"),
        ("series_id", "year", "period", "api_version"),
        "upsert",
        ("query", "store", "export"),
    ),
    "equity_historical": (
        "fmp",
        "EquityHistorical",
        "EquityHistoricalFetcher",
        "EquityHistorical",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date", "query_window_scope", "close_adjustment_semantics"),
        "upsert",
        ("query", "store", "export"),
    ),
    "equity_quote": (
        "fmp",
        "EquityQuote",
        "EquityQuoteFetcher",
        "EquityQuote",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "exchange"),
        "transient",
        ("query",),
    ),
    "currency_reference_rates": (
        "ecb",
        "CurrencyReferenceRates",
        "EcbCurrencyReferenceRatesFetcher",
        "CurrencyReferenceRate",
        "series",
        "date",
        ("series_key", "date"),
        ("series_key", "date", "quote_currency", "base_currency"),
        "transient",
        ("query",),
    ),
    "yield_curve": (
        "ecb",
        "YieldCurve",
        "EcbYieldCurveFetcher",
        "EcbYieldCurveObservation",
        "series",
        "date",
        ("series_key", "date"),
        ("series_key", "date", "frequency", "ref_area", "currency"),
        "transient",
        ("query",),
    ),
    "balance_of_payments": (
        "ecb",
        "BalanceOfPayments",
        "EcbBalanceOfPaymentsFetcher",
        "EcbBalanceOfPaymentsObservation",
        "series",
        "period",
        ("series_key", "period"),
        ("series_key", "period", "frequency", "ref_area", "counterpart_area", "unit_measure"),
        "transient",
        ("query",),
    ),
}

EXPECTED_P0_DOMAINS = {
    "stock_daily",
    "stock_adjust",
    "stock_action",
    "financial_statement",
    "financial_indicator",
    "index_constituent",
    "futures_daily",
    "futures_fundamentals",
}


def _capability_identity(capability: Capability) -> tuple[str, str, str, str, str]:
    return (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )


def test_eleven_production_domains_preserve_v1_legacy_entries_and_explicit_semantics() -> None:
    registry_path = Path(__file__).parents[1] / "opendata/data/domains.yaml"
    raw_registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    specs = load_domains()

    assert raw_registry["version"] == 1
    assert set(specs) == LEGACY_DOMAIN_IDS | set(EXPECTED_DOMAINS)
    assert len(specs) == 31
    assert all(not specs[domain].semantics_declared for domain in LEGACY_DOMAIN_IDS)
    assert {domain for domain, spec in specs.items() if spec.priority == "P0"} == (
        EXPECTED_P0_DOMAINS
    )

    for domain, expected in EXPECTED_DOMAINS.items():
        (
            _source,
            _model_id,
            _fetcher_class,
            contract,
            temporal_kind,
            time_field,
            natural_key,
            filter_dims,
            storage_mode,
            permissions,
        ) = expected
        spec = require_domain_semantics(domain)
        assert spec.contract == contract
        assert contract_model(domain).__name__ == contract
        assert spec.temporal_kind == temporal_kind
        assert spec.time_field == time_field
        assert spec.natural_key == natural_key
        assert spec.filter_dims == filter_dims
        assert spec.storage_mode == storage_mode
        assert spec.permissions == permissions
        assert spec.priority is None


def test_eleven_domain_contracts_match_exact_registered_provider_models() -> None:
    registry = ProviderRegistry()
    register_providers(registry)
    descriptors = {
        (descriptor.source, descriptor.model): descriptor
        for descriptor in registry.list_model_descriptors()
    }
    expected_identities = {(source, model_id) for source, model_id, *_ in EXPECTED_DOMAINS.values()}

    assert len(descriptors) == 11
    assert set(descriptors) == expected_identities
    for domain, expected in EXPECTED_DOMAINS.items():
        source, model_id, fetcher_class, contract, *_ = expected
        fetcher = registry.resolve_model(source, model_id)
        descriptor = descriptors[(source, model_id)]
        assert type(fetcher).__name__ == fetcher_class
        assert fetcher.capability.source == source
        assert fetcher.capability.domain == domain
        assert descriptor.domain == domain
        assert descriptor.capability_identity == _capability_identity(fetcher.capability)
        assert descriptor.verified is False
        assert contract_model(domain).__name__ == contract


def test_domain_query_permission_does_not_grant_default_api_key_scope() -> None:
    default_scopes = normalize_scope_input(None)

    assert default_scopes == []
    for domain in EXPECTED_DOMAINS:
        assert "query" in load_domains()[domain].permissions
        assert not key_allows_domain(default_scopes, domain)
