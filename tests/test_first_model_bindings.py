"""First canonical FRED, BLS, FMP, and ECB models bind through provider metadata."""

from __future__ import annotations

import builtins
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from opendata.data.models import (
    CurrencyReferenceRate,
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
    FredSofrObservation,
    FredSoniaObservation,
    SeriesCatalogItem,
    SeriesObservation,
)
from opendata.data.providers.bls.models._client import BlsRawCatalogPage
from opendata.data.providers.bls.models._contracts import BlsCatalogItem, BlsObservation
from opendata.data.providers.bls.models.search import BlsSearchQuery
from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.catalog import (
    get_provider,
    health_check,
    list_providers,
    register_providers,
)
from opendata.data.providers.ecb.models._reference_rates import RawReferenceRateRecord
from opendata.data.providers.ecb.models._series_query import (
    EcbBalanceOfPaymentsQuery,
    EcbYieldCurveQuery,
)
from opendata.data.providers.ecb.models.reference_rates import EcbCurrencyReferenceRatesQuery
from opendata.data.providers.fmp.models._contracts import EquityHistorical, EquityQuote
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fmp.models.equity_quote import EquityQuoteQuery
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery
from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.providers.fred.models.search import FredSearchQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.registry import ProviderRegistry

if TYPE_CHECKING:
    from opendata.data.capability import Capability


@dataclass(frozen=True)
class _ModelCase:
    source: str
    model_id: str
    module: str
    class_name: str
    domain: str
    query_type: type[Any]
    query_kwargs: dict[str, object]
    raw: object
    result_type: type[Any]


_MODEL_CASES = (
    _ModelCase(
        source="fred",
        model_id="FredSearch",
        module="opendata.data.providers.fred.models.search",
        class_name="FredSearchFetcher",
        domain="fred_search",
        query_type=FredSearchQuery,
        query_kwargs={"search_text": "gross domestic product"},
        raw=[
            {
                "id": "GDP",
                "title": "Gross Domestic Product",
                "frequency": "Quarterly",
                "units": "Billions of Dollars",
                "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
                "observation_start": "1947-01-01",
                "observation_end": "2026-04-01",
                "last_updated": "2026-09-25 07:30:00-05",
                "notes": "Official FRED catalog note",
                "popularity": 95,
            }
        ],
        result_type=SeriesCatalogItem,
    ),
    _ModelCase(
        source="fred",
        model_id="FredSeries",
        module="opendata.data.providers.fred.models.series",
        class_name="FredSeriesFetcher",
        domain="fred_series",
        query_type=FredSeriesQuery,
        query_kwargs={"series_id": "GDP"},
        raw=[
            {
                "date": "2026-01-01",
                "value": "123.5",
                "realtime_start": "2026-01-01",
                "realtime_end": "9999-12-31",
            }
        ],
        result_type=SeriesObservation,
    ),
    _ModelCase(
        source="fred",
        model_id="SOFR",
        module="opendata.data.providers.fred.models.sofr",
        class_name="FredSofrFetcher",
        domain="sofr",
        query_type=FredSofrQuery,
        query_kwargs={"series_id": "SOFR"},
        raw=(
            {
                "count": 1,
                "offset": 0,
                "limit": 100_000,
                "units": "lin",
                "output_type": 1,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
                "observation_start": "1776-07-04",
                "observation_end": "9999-12-31",
                "observations": [
                    {
                        "date": "2025-01-02",
                        "value": "4.2500",
                        "realtime_start": "2025-01-02",
                        "realtime_end": "9999-12-31",
                    }
                ],
            },
        ),
        result_type=FredSofrObservation,
    ),
    _ModelCase(
        source="fred",
        model_id="SONIA",
        module="opendata.data.providers.fred.models.sonia",
        class_name="FredSoniaFetcher",
        domain="sonia",
        query_type=FredSoniaQuery,
        query_kwargs={"parameter": "rate"},
        raw=(
            {
                "count": 1,
                "offset": 0,
                "limit": 100_000,
                "units": "lin",
                "output_type": 1,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "realtime_start": "1776-07-04",
                "realtime_end": "9999-12-31",
                "observation_start": "1776-07-04",
                "observation_end": "9999-12-31",
                "observations": [
                    {
                        "date": "2025-01-02",
                        "value": "4.2500",
                        "realtime_start": "2025-01-02",
                        "realtime_end": "9999-12-31",
                    }
                ],
            },
        ),
        result_type=FredSoniaObservation,
    ),
    _ModelCase(
        source="bls",
        model_id="BlsSearch",
        module="opendata.data.providers.bls.models.search",
        class_name="BlsSearchFetcher",
        domain="bls_search",
        query_type=BlsSearchQuery,
        query_kwargs={"survey": "ce", "search_text": "prices"},
        raw=BlsRawCatalogPage(
            items=({"series_id": "SERIES_A", "survey": "CE", "title": "Prices"},),
            total=1,
            offset=0,
            limit=100,
        ),
        result_type=BlsCatalogItem,
    ),
    _ModelCase(
        source="bls",
        model_id="BlsSeries",
        module="opendata.data.providers.bls.models.series",
        class_name="BlsSeriesFetcher",
        domain="bls_series",
        query_type=BlsSeriesQuery,
        query_kwargs={
            "series_ids": ["SERIES_A"],
            "start_year": 2024,
            "end_year": 2024,
            "max_requests": 1,
        },
        raw=[
            {
                "series_id": "SERIES_A",
                "year": "2024",
                "period": "M01",
                "period_name": "January",
                "value": "123.5",
                "footnotes": [{"code": "P", "text": "Preliminary."}],
                "latest": True,
                "api_version": "v1",
            }
        ],
        result_type=BlsObservation,
    ),
    _ModelCase(
        source="fmp",
        model_id="EquityHistorical",
        module="opendata.data.providers.fmp.models.equity_historical",
        class_name="EquityHistoricalFetcher",
        domain="equity_historical",
        query_type=EquityHistoricalQuery,
        query_kwargs={
            "symbol": "AAPL",
            "start_date": "2026-01-02",
            "end_date": "2026-01-02",
        },
        raw=[
            {
                "symbol": "AAPL",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 100,
            }
        ],
        result_type=EquityHistorical,
    ),
    _ModelCase(
        source="fmp",
        model_id="EquityQuote",
        module="opendata.data.providers.fmp.models.equity_quote",
        class_name="EquityQuoteFetcher",
        domain="equity_quote",
        query_type=EquityQuoteQuery,
        query_kwargs={"symbol": "AAPL"},
        raw=[{"symbol": "AAPL", "price": 100.5}],
        result_type=EquityQuote,
    ),
    _ModelCase(
        source="ecb",
        model_id="CurrencyReferenceRates",
        module="opendata.data.providers.ecb.models.reference_rates",
        class_name="EcbCurrencyReferenceRatesFetcher",
        domain="currency_reference_rates",
        query_type=EcbCurrencyReferenceRatesQuery,
        query_kwargs={"quote_currencies": ["USD"]},
        raw=(
            RawReferenceRateRecord(
                series_key="D.USD.EUR.SP00.A",
                date="2026-10-08",
                frequency="D",
                quote_currency="USD",
                base_currency="EUR",
                rate_type="SP00",
                rate_suffix="A",
                value="1.234500",
                dataset_attributes={},
                series_attributes={},
                observation_attributes={},
                group_context=[],
            ),
        ),
        result_type=CurrencyReferenceRate,
    ),
    _ModelCase(
        source="ecb",
        model_id="YieldCurve",
        module="opendata.data.providers.ecb.models.yield_curve",
        class_name="EcbYieldCurveFetcher",
        domain="yield_curve",
        query_type=EcbYieldCurveQuery,
        query_kwargs={"series_key": "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"},
        raw=(
            {
                "series_key": "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M",
                "period": "2026-10-07",
                "dimensions": {
                    "FREQ": "B",
                    "REF_AREA": "U2",
                    "CURRENCY": "EUR",
                    "PROVIDER_FM": "4F",
                    "INSTRUMENT_FM": "G_N_A",
                    "PROVIDER_FM_ID": "SV_C_YM",
                    "DATA_TYPE_FM": "SR_3M",
                },
                "value": "-0.000",
                "dataset_attributes": {"UNKNOWN_DATASET": "preserved"},
                "series_attributes": {"UNKNOWN_SERIES": "preserved"},
                "observation_attributes": {"OBS_STATUS": "A"},
                "group_context": [],
            },
        ),
        result_type=EcbYieldCurveObservation,
    ),
    _ModelCase(
        source="ecb",
        model_id="BalanceOfPayments",
        module="opendata.data.providers.ecb.models.balance_of_payments",
        class_name="EcbBalanceOfPaymentsFetcher",
        domain="balance_of_payments",
        query_type=EcbBalanceOfPaymentsQuery,
        query_kwargs={
            "series_key": "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL",
            "start_period": "2026-06",
            "end_period": "2026-06",
        },
        raw=(
            {
                "series_key": "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL",
                "period": "2026-06",
                "dimensions": {
                    "FREQ": "M",
                    "ADJUSTMENT": "N",
                    "REF_AREA": "I10",
                    "COUNTERPART_AREA": "W1",
                    "REF_SECTOR": "S121",
                    "COUNTERPART_SECTOR": "S1",
                    "FLOW_STOCK_ENTRY": "T",
                    "ACCOUNTING_ENTRY": "A",
                    "INT_ACC_ITEM": "FA",
                    "FUNCTIONAL_CAT": "R",
                    "INSTR_ASSET": "F",
                    "MATURITY": "_Z",
                    "UNIT_MEASURE": "EUR",
                    "CURRENCY_DENOM": "X1",
                    "VALUATION": "_X",
                    "COMP_METHOD": "N",
                    "TYPE_ENTITY": "ALL",
                },
                "value": "123.4500",
                "dataset_attributes": {"UNKNOWN_DATASET": "preserved"},
                "series_attributes": {"UNKNOWN_SERIES": "preserved"},
                "observation_attributes": {"OBS_STATUS": "A"},
                "group_context": [],
            },
        ),
        result_type=EcbBalanceOfPaymentsObservation,
    ),
)


@pytest.mark.parametrize("case", _MODEL_CASES, ids=lambda case: case.model_id)
def test_canonical_model_resolves_to_its_source_class_query_and_result(
    case: _ModelCase,
) -> None:
    provider = get_provider(case.source)
    binding = provider.fetcher_dict[case.model_id]
    assert binding.canonical_model_ids == (case.model_id,)
    assert binding.module == case.module
    assert binding.class_name == case.class_name

    fetchers_by_binding = dict(zip(provider.fetcher_bindings, provider.fetchers, strict=True))
    fetcher = fetchers_by_binding[binding]
    assert type(fetcher).__name__ == case.class_name
    assert fetcher.capability.source == case.source
    assert fetcher.capability.domain == case.domain
    assert fetcher.capability.verified is False

    query = fetcher.transform_query(**case.query_kwargs)
    assert type(query) is case.query_type
    result = fetcher.transform_data(case.raw, query)
    record = result.items[0] if hasattr(result, "items") else result[0]
    assert isinstance(record, case.result_type)

    if isinstance(record, (EquityHistorical, EquityQuote)):
        assert record.currency is None
        assert record.currency_semantics == "source_unverified"
        assert record.volume_unit is None
        assert record.volume_unit_semantics == "source_unverified"


def _capability_identity(capability: Capability) -> tuple[str, str, str, str, str]:
    return (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )


def test_registration_preserves_legacy_order_and_binds_eleven_models_exactly() -> None:
    registry = ProviderRegistry()
    registered = register_providers(registry)

    assert len(list_providers()) == 34
    assert len(registered) == len(registry.capabilities()) == 44
    assert len({capability.source for capability in registry.capabilities()}) == 9
    assert sum(not provider.is_implemented for provider in list_providers()) == 25

    expected_legacy_sources = (
        ["akshare"] * 10
        + ["ecb"] * 3
        + ["fred"] * 3
        + ["imf"] * 3
        + ["oecd"] * 2
        + ["ths"] * 11
        + ["yfinance"]
    )
    new_models = {(case.source, case.model_id) for case in _MODEL_CASES}
    new_capability_identities = {
        _capability_identity(registry.resolve_model(source, model_id).capability)
        for source, model_id in new_models
    }
    legacy_capabilities = [
        capability
        for capability in registered
        if _capability_identity(capability) not in new_capability_identities
    ]
    assert [capability.source for capability in legacy_capabilities] == expected_legacy_sources
    assert [capability.source for capability in registered[-4:]] == ["bls", "bls", "fmp", "fmp"]

    descriptors = {
        (descriptor.source, descriptor.model): descriptor
        for descriptor in registry.list_model_descriptors()
    }
    assert set(descriptors) == new_models
    for case in _MODEL_CASES:
        provider = get_provider(case.source)
        binding = provider.fetcher_dict[case.model_id]
        index = provider.fetcher_bindings.index(binding)
        fetcher = registry.resolve_model(case.source, case.model_id)
        assert fetcher is provider.fetchers[index]
        assert fetcher.capability.verified is False
        assert descriptors[(case.source, case.model_id)].verified is False

    assert sum(capability.participates_in_auto() for capability in registered) == 23
    with pytest.raises(LookupError, match="unknown provider model"):
        registry.resolve_model("fmp", "UnknownModel")
    assert register_providers(registry) == []


def test_fmp_and_bls_health_checks_are_local_and_do_not_overstate_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "opendata.core.config" or name.startswith("opendata.core.config."):
            raise AssertionError("provider health read settings")
        return real_import(name, globals, locals, fromlist, level)

    def reject_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("provider health attempted network access")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket.socket, "connect", reject_network)

    fmp_provider = get_provider("fmp")
    fmp_credential = fmp_provider.credentials[0]
    assert fmp_credential.environment_variable == "FMP_API_KEY"
    assert fmp_credential.required_for_health is True
    assert fmp_credential.settings_attribute is None
    missing_fmp = health_check("fmp")["fmp"]
    assert missing_fmp.status == "missing_key"
    assert missing_fmp.missing_credentials == ("api_key",)

    monkeypatch.setenv("FMP_API_KEY", "fixture-only-fmp-key")
    ready_fmp = health_check("fmp")["fmp"]
    assert ready_fmp.status == "ready"
    assert "fixture-only-fmp-key" not in repr(ready_fmp)

    bls_provider = get_provider("bls")
    bls_credential = bls_provider.credentials[0]
    assert bls_credential.environment_variable == "BLS_API_KEY"
    assert bls_credential.required_for_health is False
    assert bls_provider.health_check().status == "ready"
    assert "v1" in bls_provider.description and "v2" in bls_provider.description
    assert "permission" in bls_provider.description
    assert "approved data use" in bls_provider.description
