"""Offline typed contracts for the fixed OECD macro-model slice."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.models.macro_iter2 import (
    OecdCompositeLeadingIndicator,
    OecdConsumerPriceIndex,
    OecdCountryInterestRate,
    OecdGdpForecast,
    OecdGdpNominal,
    OecdGdpReal,
    OecdHousePriceIndex,
    OecdSharePriceIndex,
    OecdUnemployment,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.oecd.models import _client
from opendata.data.providers.oecd.models._client import OecdProviderError
from opendata.data.providers.oecd.models.iter2 import (
    OecdCompositeLeadingIndicatorFetcher,
    OecdConsumerPriceIndexFetcher,
    OecdCountryInterestRateFetcher,
    OecdGdpForecastFetcher,
    OecdGdpNominalFetcher,
    OecdGdpRealFetcher,
    OecdHousePriceIndexFetcher,
    OecdSharePriceIndexFetcher,
    OecdUnemploymentFetcher,
)
from opendata.data.providers.oecd.provider import PROVIDER as OECD_PROVIDER
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def _context(model: str) -> FetchContext:
    grant = RequestGrant(
        source="oecd",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=frozenset({"sdmx.oecd.org"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=2.0,
        request_budget=RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,)),
    )


def _descriptor(fetcher: object, model: str) -> SimpleNamespace:
    capability = fetcher.capability  # type: ignore[attr-defined]
    return SimpleNamespace(
        source=capability.source,
        model=model,
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


def _fixture(
    ref_area: str,
    frequency: str,
    measure: str,
    value: str = "101.5",
    period: str = "2024",
    **dimensions: str,
) -> dict[str, str]:
    return {
        "REF_AREA": ref_area,
        "FREQ": frequency,
        "MEASURE": measure,
        "TIME_PERIOD": period,
        "OBS_VALUE": value,
        **dimensions,
    }


def _csv(*rows: dict[str, str]) -> str:
    names: list[str] = []
    for row in rows:
        for name in row:
            if name not in names:
                names.append(name)
    return (
        ",".join(names)
        + "\n"
        + "\n".join(",".join(row.get(name, "") for name in names) for row in rows)
    )


CASES = (
    (
        "CompositeLeadingIndicator",
        OecdCompositeLeadingIndicatorFetcher,
        OecdCompositeLeadingIndicator,
        {"country": "united_states"},
        _fixture(
            "USA",
            "M",
            "LI",
            period="2024-01",
            UNIT_MEASURE="IX",
            ACTIVITY="_Z",
            ADJUSTMENT="AA",
            TRANSFORMATION="IX",
            TIME_HORIZ="_Z",
            METHODOLOGY="H",
        ),
        ("country", "USA"),
    ),
    (
        "ConsumerPriceIndex",
        OecdConsumerPriceIndexFetcher,
        OecdConsumerPriceIndex,
        {"country": "united_states"},
        _fixture(
            "USA",
            "M",
            "CPI",
            period="2024-01",
            METHODOLOGY="N",
            UNIT_MEASURE="PA",
            EXPENDITURE="_T",
            ADJUSTMENT="N",
            TRANSFORMATION="GY",
        ),
        ("expenditure", "_T"),
    ),
    (
        "CountryInterestRates",
        OecdCountryInterestRateFetcher,
        OecdCountryInterestRate,
        {"country": "united_states"},
        _fixture(
            "USA",
            "M",
            "IR3TIB",
            period="2024-01",
            UNIT_MEASURE="PA",
            ACTIVITY="",
            ADJUSTMENT="",
            TRANSFORMATION="",
        ),
        ("country", "USA"),
    ),
    (
        "GdpNominal",
        OecdGdpNominalFetcher,
        OecdGdpNominal,
        {"country": "united_states"},
        _fixture(
            "USA",
            "Q",
            "B1GQ",
            period="2024-Q1",
            ADJUSTMENT="Y",
            SECTOR="S1",
            COUNTERPART_SECTOR="S1",
            TRANSACTION="B1GQ",
            INSTR_ASSET="_Z",
            ACTIVITY="_Z",
            EXPENDITURE="_Z",
            UNIT_MEASURE="USD_PPP",
            PRICE_BASE="V",
            TRANSFORMATION="LA",
            TABLE_IDENTIFIER="T0102",
            UNIT_MULT="6",
        ),
        ("value", 101_500_000.0),
    ),
    (
        "GdpReal",
        OecdGdpRealFetcher,
        OecdGdpReal,
        {"country": "united_states"},
        _fixture(
            "USA",
            "Q",
            "B1GQ",
            period="2024-Q1",
            ADJUSTMENT="Y",
            SECTOR="S1",
            COUNTERPART_SECTOR="S1",
            TRANSACTION="B1GQ",
            INSTR_ASSET="_Z",
            ACTIVITY="_Z",
            EXPENDITURE="_Z",
            UNIT_MEASURE="USD_PPP",
            PRICE_BASE="LR",
            TRANSFORMATION="LA",
            TABLE_IDENTIFIER="T0102",
            UNIT_MULT="6",
        ),
        ("country", "USA"),
    ),
    (
        "GdpForecast",
        OecdGdpForecastFetcher,
        OecdGdpForecast,
        {"country": "united_states"},
        _fixture("USA", "A", "GDPV_USD", period="2024", UNIT_MEASURE="USD_EXC", UNIT_MULT="0"),
        ("value", 101.5),
    ),
    (
        "HousePriceIndex",
        OecdHousePriceIndexFetcher,
        OecdHousePriceIndex,
        {"country": "united_states"},
        _fixture(
            "USA",
            "Q",
            "RHP",
            period="2024-Q1",
            UNIT_MEASURE="IX",
            ADJUSTMENT="S",
            BASE_PER="2015",
            UNIT_MULT="0",
        ),
        ("country", "USA"),
    ),
    (
        "SharePriceIndex",
        OecdSharePriceIndexFetcher,
        OecdSharePriceIndex,
        {"country": "united_states"},
        _fixture(
            "USA",
            "M",
            "SHARE",
            period="2024-01",
            UNIT_MEASURE="IX",
            ADJUSTMENT="",
            TRANSFORMATION="",
            ACTIVITY="",
            TIME_HORIZ="",
            METHODOLOGY="",
        ),
        ("value", 101.5),
    ),
    (
        "Unemployment",
        OecdUnemploymentFetcher,
        OecdUnemployment,
        {"country": "united_states"},
        _fixture(
            "USA",
            "M",
            "UNE_LF_M",
            period="2024-01",
            UNIT_MEASURE="PT_LF_SUB",
            TRANSFORMATION="_Z",
            ADJUSTMENT="N",
            SEX="_T",
            AGE="Y_GE15",
            ACTIVITY="_Z",
        ),
        ("country", "USA"),
    ),
)


@pytest.mark.parametrize("model,fetcher_type,contract,query,fixture,identity", CASES)
def test_each_oecd_model_returns_typed_rows_and_service_injects_identity(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    contract: type,
    query: dict[str, object],
    fixture: dict[str, str],
    identity: tuple[str, object],
) -> None:
    fetcher = fetcher_type()
    calls: list[tuple[str, dict[str, str]]] = []

    def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        del timeout
        calls.append((url, dict(params)))
        return 200, _csv(fixture)

    monkeypatch.setattr(_client, "_http_get", _http_get)
    result = fetcher.fetch(ctx=_context(model), **query)
    assert len(result) == 1
    assert isinstance(result[0], contract)
    assert calls[0][1]["format"] == "csvfile"
    if model == "GdpNominal":
        assert calls[0][0].endswith("/Q.Y.USA.S1.S1.B1GQ._Z._Z._Z.USD_PPP.V.LA.T0102")
        assert result[0].value == pytest.approx(101_500_000.0)
    if model == "GdpReal":
        assert calls[0][0].endswith("/Q.Y.USA.S1.S1.B1GQ._Z._Z._Z.USD_PPP.LR.LA.T0102")
        assert result[0].value == pytest.approx(101_500_000.0)
    if model == "CompositeLeadingIndicator":
        assert calls[0][0].endswith("/USA.M.LI...AA.IX..H")
    if model == "Unemployment":
        assert ",DSD_LFS@DF_IALFS_INDIC,1.0/" in calls[0][0]
        assert calls[0][0].endswith("/USA.UNE_LF_M.PT_LF_SUB._Z.N._T.Y_GE15._Z.M")
    if model == "HousePriceIndex":
        assert calls[0][0].endswith("/USA.Q.RHP.IX")

    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query, "_reviewed_query_contract", lambda _domain: (None, contract)
    )
    transformed_kwargs: dict[str, object] = {}
    transform_query = fetcher.transform_query

    def _capture_transform_query(**kwargs: object) -> object:
        transformed_kwargs.update(kwargs)
        return transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", _capture_transform_query)
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="oecd",
            model=model,
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_context(model),
        )
    )
    assert response["source"] == "oecd"
    assert response["model"] == model
    assert response["domain"] == fetcher.capability.domain
    assert isinstance(response["results"][0], dict)
    assert response["results"][0][identity[0]] == identity[1]
    assert transformed_kwargs["source"] == "oecd"
    assert transformed_kwargs["market"] == "global"


@pytest.mark.parametrize(
    "fetcher_type,query",
    (
        (OecdCompositeLeadingIndicatorFetcher, {"adjustment": "bad"}),
        (OecdCompositeLeadingIndicatorFetcher, {"country": "oecd_non_euro_area"}),
        (OecdConsumerPriceIndexFetcher, {"transform": "bad"}),
        (OecdCountryInterestRateFetcher, {"duration": "bad"}),
        (OecdGdpNominalFetcher, {"units": "bad"}),
        (OecdGdpRealFetcher, {"units": "bad"}),
        (OecdGdpForecastFetcher, {"units": "bad"}),
        (OecdHousePriceIndexFetcher, {"frequency": "monthly"}),
        (OecdSharePriceIndexFetcher, {"frequency": "weekly"}),
        (OecdUnemploymentFetcher, {"age": "unknown"}),
        (OecdConsumerPriceIndexFetcher, {"expenditure": ["total", "not-a-category"]}),
    ),
)
def test_invalid_query_is_rejected_before_upstream_io(
    monkeypatch: pytest.MonkeyPatch, fetcher_type: type, query: dict[str, object]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(_client, "_http_get", lambda *args: calls.append(args))
    with pytest.raises((ValidationError, ValueError)):
        fetcher_type().fetch(**query)
    assert calls == []


def test_invalid_empty_csv_shape_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "_http_get", lambda *_args: (200, "not,a,series\n"))
    with pytest.raises(OecdProviderError) as exc:
        _client.fetch_observations("USA.A.SHARE.IX", start=None, end=None, timeout=1.0, flow="test")
    assert exc.value.code == "OECD_BAD_RESPONSE"


def test_rate_limit_status_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_client, "_http_get", lambda *_args: (429, "limited"))
    with pytest.raises(OecdProviderError) as exc:
        OecdSharePriceIndexFetcher().fetch(ctx=_context("SharePriceIndex"), country="USA")
    assert exc.value.code == "OECD_HTTP_ERROR"
    assert exc.value.status == 429


def test_wrong_source_unit_is_rejected() -> None:
    fetcher = OecdCountryInterestRateFetcher()
    params = fetcher.transform_query(country="USA")
    raw = [_fixture("USA", "M", "IR3TIB", period="2024-01", UNIT_MEASURE="USD")]
    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data(raw, params)
    assert exc.value.code == "OECD_BAD_RESPONSE"


def test_house_price_yoy_uses_calendar_period_and_handles_gaps() -> None:
    fetcher = OecdHousePriceIndexFetcher()
    params = fetcher.transform_query(country="USA", transform="yoy", start_date="2024-01-01")
    rows = [
        _fixture(
            "USA",
            "Q",
            "RHP",
            period="2023-Q2",
            UNIT_MEASURE="IX",
            ADJUSTMENT="S",
            BASE_PER="2015",
            UNIT_MULT="0",
            OBS_VALUE="105",
        ),
        _fixture(
            "USA",
            "Q",
            "RHP",
            period="2023-Q1",
            UNIT_MEASURE="IX",
            ADJUSTMENT="S",
            BASE_PER="2015",
            UNIT_MULT="0",
            OBS_VALUE="100",
        ),
        _fixture(
            "USA",
            "Q",
            "RHP",
            period="2024-Q1",
            UNIT_MEASURE="IX",
            ADJUSTMENT="S",
            BASE_PER="2015",
            UNIT_MULT="0",
            OBS_VALUE="110",
        ),
    ]
    transformed = fetcher.transform_data(rows, params)
    assert len(transformed) == 1
    assert transformed[0].date.isoformat() == "2024-01-01"
    assert transformed[0].value == pytest.approx(10.0)


@pytest.mark.parametrize(
    "dimensions",
    (
        {"UNIT_MEASURE": "PT_AVG_L_TERM", "ADJUSTMENT": "S", "BASE_PER": "2015", "UNIT_MULT": "0"},
        {"ADJUSTMENT": "S", "BASE_PER": "2015", "UNIT_MULT": "0"},
        {"UNIT_MEASURE": "IX", "ADJUSTMENT": "N", "BASE_PER": "2015", "UNIT_MULT": "0"},
        {"UNIT_MEASURE": "IX", "ADJUSTMENT": "S", "BASE_PER": "2010", "UNIT_MULT": "0"},
        {"UNIT_MEASURE": "IX", "ADJUSTMENT": "S", "BASE_PER": "2015", "UNIT_MULT": "3"},
    ),
)
def test_house_price_rejects_ambiguous_or_nonmatching_unit_metadata(
    dimensions: dict[str, str],
) -> None:
    fetcher = OecdHousePriceIndexFetcher()
    params = fetcher.transform_query(country="USA")
    row = _fixture("USA", "Q", "RHP", period="2024-Q1", **dimensions)

    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data([row], params)

    assert exc.value.code == "OECD_BAD_RESPONSE"


@pytest.mark.parametrize(
    "model_id",
    (
        "CompositeLeadingIndicator",
        "ConsumerPriceIndex",
        "CountryInterestRates",
        "GdpNominal",
        "GdpReal",
        "GdpForecast",
        "HousePriceIndex",
        "SharePriceIndex",
        "Unemployment",
    ),
)
def test_new_oecd_capabilities_are_explicit_only(model_id: str) -> None:
    fetcher = OECD_PROVIDER.fetcher_dict[model_id].load("oecd")
    capability = fetcher.capability
    assert capability.verified is False
    assert capability.notes == "offline candidate; cross-source validation not completed"

    registry = ProviderRegistry()
    registry.register(fetcher)
    with pytest.raises(LookupError, match="no verified capability"):
        registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
        )
    assert (
        registry.resolve(
            capability.asset_class,
            capability.domain,
            period=capability.period,
            market=capability.market,
            source="oecd",
        )
        is fetcher
    )


def test_missing_observation_is_kept_nullable() -> None:
    fetcher = OecdSharePriceIndexFetcher()
    params = fetcher.transform_query(country="USA")
    row = _fixture("USA", "M", "SHARE", period="2024-01", UNIT_MEASURE="IX", OBS_VALUE="..")
    result = fetcher.transform_data([row], params)
    assert result[0].value is None


def test_annual_gdp_uses_annual_national_accounts_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    row = _fixture(
        "USA",
        "A",
        "B1GQ",
        period="2023",
        SECTOR="S1",
        COUNTERPART_SECTOR="S1",
        TRANSACTION="B1GQ",
        INSTR_ASSET="_Z",
        ACTIVITY="_Z",
        EXPENDITURE="_Z",
        UNIT_MEASURE="USD_PPP",
        PRICE_BASE="LR",
        TRANSFORMATION="N",
        TABLE_IDENTIFIER="T0102",
        UNIT_MULT="6",
    )
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda url, params, _timeout: calls.append((url, dict(params))) or (200, _csv(row)),
    )
    result = OecdGdpRealFetcher().fetch(ctx=_context("GdpReal"), country="USA", frequency="annual")
    assert result[0].date.isoformat() == "2023-01-01"
    assert result[0].value == pytest.approx(101_500_000.0)
    assert ",DSD_NAMAIN10@DF_TABLE1_EXPENDITURE,2.0/" in calls[0][0]
    assert calls[0][0].endswith("/A.USA.S1.S1.B1GQ._Z._Z._Z.USD_PPP.LR.N.T0102")


def test_unsupported_nominal_gdp_variant_is_rejected_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(_client, "_http_get", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="per capita"):
        OecdGdpNominalFetcher().fetch(
            ctx=_context("GdpNominal"),
            country="USA",
            units="capita",
            frequency="quarter",
        )
    assert calls == []


@pytest.mark.parametrize(
    ("price_base", "price_code", "flow_id"),
    (
        (
            "current_prices",
            "V",
            "DF_TABLE1_EXPENDITURE_HCPC",
        ),
        (
            "volume",
            "LR",
            "DF_TABLE1_EXPENDITURE_HVPVOB",
        ),
    ),
)
def test_annual_gdp_per_capita_uses_official_flow_and_serializes_typed_row(
    monkeypatch: pytest.MonkeyPatch,
    price_base: str,
    price_code: str,
    flow_id: str,
) -> None:
    row = _fixture(
        "USA",
        "A",
        "B1GQ_POP",
        period="2023",
        SECTOR="S1",
        COUNTERPART_SECTOR="S1",
        TRANSACTION="B1GQ_POP",
        INSTR_ASSET="_Z",
        ACTIVITY="_Z",
        EXPENDITURE="_Z",
        UNIT_MEASURE="USD_PPP_PS",
        PRICE_BASE=price_code,
        TRANSFORMATION="N",
        TABLE_IDENTIFIER="T0102",
        UNIT_MULT="0",
    )
    calls: list[tuple[str, dict[str, str]]] = []

    def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        del timeout
        calls.append((url, dict(params)))
        return 200, _csv(row)

    monkeypatch.setattr(_client, "_http_get", _http_get)
    fetcher = OecdGdpNominalFetcher()
    query = {"country": "USA", "frequency": "annual", "units": "capita", "price_base": price_base}
    result = fetcher.fetch(ctx=_context("GdpNominal"), **query)
    assert len(result) == 1 and isinstance(result[0], OecdGdpNominal)
    assert result[0].date.isoformat() == "2023-01-01"
    assert result[0].value == pytest.approx(101.5)
    assert f"DSD_NAMAIN10@{flow_id},2.0/" in calls[0][0]
    assert calls[0][0].endswith(f"/A.USA.S1.S1.B1GQ_POP._Z._Z._Z.USD_PPP_PS.{price_code}.N.T0102")

    transformed_kwargs: dict[str, object] = {}
    transform_query = fetcher.transform_query

    def _capture_transform_query(**kwargs: object) -> object:
        transformed_kwargs.update(kwargs)
        return transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", _capture_transform_query)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, "GdpNominal"), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, OecdGdpNominal),
    )
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="oecd",
            model="GdpNominal",
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_context("GdpNominal"),
        )
    )
    assert response["source"] == "oecd"
    assert response["model"] == "GdpNominal"
    assert isinstance(response["results"][0], dict)
    assert response["results"][0]["value"] == pytest.approx(101.5)
    assert transformed_kwargs["source"] == "oecd"
    assert transformed_kwargs["market"] == "global"


@pytest.mark.parametrize(
    "unit,unit_multiplier,error_code",
    (
        ("USD_PPP", "0", "OECD_BAD_RESPONSE"),
        ("USD_PPP_PS", None, "OECD_BAD_OBSERVATION"),
    ),
)
def test_annual_gdp_per_capita_rejects_bad_units_or_missing_multiplier(
    unit: str, unit_multiplier: str | None, error_code: str
) -> None:
    dimensions = {
        "SECTOR": "S1",
        "COUNTERPART_SECTOR": "S1",
        "TRANSACTION": "B1GQ_POP",
        "INSTR_ASSET": "_Z",
        "ACTIVITY": "_Z",
        "EXPENDITURE": "_Z",
        "UNIT_MEASURE": unit,
        "PRICE_BASE": "V",
        "TRANSFORMATION": "N",
        "TABLE_IDENTIFIER": "T0102",
    }
    if unit_multiplier is not None:
        dimensions["UNIT_MULT"] = unit_multiplier
    with pytest.raises(OecdProviderError) as exc:
        OecdGdpNominalFetcher().transform_data(
            [_fixture("USA", "A", "B1GQ_POP", period="2023", **dimensions)],
            OecdGdpNominalFetcher().transform_query(
                country="USA", frequency="annual", units="capita"
            ),
        )
    assert exc.value.code == error_code


@pytest.mark.parametrize("multiplier", (None, "not-an-exponent", "13"))
def test_qna_requires_a_valid_source_unit_multiplier(multiplier: str | None) -> None:
    fetcher = OecdGdpRealFetcher()
    params = fetcher.transform_query(country="USA")
    dimensions = {
        "ADJUSTMENT": "Y",
        "SECTOR": "S1",
        "COUNTERPART_SECTOR": "S1",
        "TRANSACTION": "B1GQ",
        "INSTR_ASSET": "_Z",
        "ACTIVITY": "_Z",
        "EXPENDITURE": "_Z",
        "UNIT_MEASURE": "USD_PPP",
        "PRICE_BASE": "LR",
        "TRANSFORMATION": "LA",
        "TABLE_IDENTIFIER": "T0102",
    }
    if multiplier is not None:
        dimensions["UNIT_MULT"] = multiplier
    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data(
            [_fixture("USA", "Q", "B1GQ", period="2024-Q1", **dimensions)],
            params,
        )
    assert exc.value.code == "OECD_BAD_OBSERVATION"


def test_unemployment_wrong_period_frequency_is_rejected() -> None:
    fetcher = OecdUnemploymentFetcher()
    params = fetcher.transform_query(country="USA", frequency="monthly")
    row = _fixture(
        "USA",
        "M",
        "UNE_LF_M",
        period="2024-Q1",
        UNIT_MEASURE="PT_LF_SUB",
        TRANSFORMATION="_Z",
        ADJUSTMENT="N",
        SEX="_T",
        AGE="Y_GE15",
        ACTIVITY="_Z",
    )
    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data([row], params)
    assert exc.value.code == "OECD_BAD_RESPONSE"


def test_unemployment_validates_official_flat_csv_dimension_names() -> None:
    fetcher = OecdUnemploymentFetcher()
    params = fetcher.transform_query(country="USA", frequency="monthly", seasonal_adjustment=True)
    # The official flat CSV exposes TRANSFORMATION and ACTIVITY.  The two
    # `_Z` slots in the SDMX key are distinct dimensions, not ACTIVITY/SECTOR.
    row = _fixture(
        "USA",
        "M",
        "UNE_LF_M",
        value="3.9",
        period="2024-03",
        UNIT_MEASURE="PT_LF_SUB",
        TRANSFORMATION="_Z",
        ADJUSTMENT="Y",
        SEX="_T",
        AGE="Y_GE15",
        ACTIVITY="_Z",
    )

    result = fetcher.transform_data([row], params)

    assert len(result) == 1
    assert isinstance(result[0], OecdUnemployment)
    assert result[0].value == pytest.approx(3.9)


def test_unemployment_rejects_flat_csv_without_transformation_dimension() -> None:
    fetcher = OecdUnemploymentFetcher()
    params = fetcher.transform_query(country="USA", frequency="monthly")
    # This shape has ACTIVITY and SECTOR but omits the source's
    # TRANSFORMATION column, so it is not accepted as an OECD flat CSV row.
    row = _fixture(
        "USA",
        "M",
        "UNE_LF_M",
        period="2024-03",
        UNIT_MEASURE="PT_LF_SUB",
        ADJUSTMENT="Y",
        SEX="_T",
        AGE="Y_GE15",
        ACTIVITY="_Z",
        SECTOR="_Z",
    )

    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data([row], params)

    assert exc.value.code == "OECD_BAD_RESPONSE"


def test_cpi_expenditure_list_is_mapped_and_parsed_as_source_codes() -> None:
    fetcher = OecdConsumerPriceIndexFetcher()
    params = fetcher.transform_query(
        country="USA", expenditure=["total", "food_non_alcoholic_beverages"]
    )
    rows = [
        _fixture(
            "USA",
            "M",
            "CPI",
            period="2024-01",
            METHODOLOGY="N",
            UNIT_MEASURE="PA",
            EXPENDITURE="_T",
            ADJUSTMENT="N",
            TRANSFORMATION="GY",
        ),
        _fixture(
            "USA",
            "M",
            "CPI",
            period="2024-01",
            METHODOLOGY="N",
            UNIT_MEASURE="PA",
            EXPENDITURE="CP01",
            ADJUSTMENT="N",
            TRANSFORMATION="GY",
        ),
    ]

    result = fetcher.transform_data(rows, params)

    assert {row.expenditure for row in result} == {"_T", "CP01"}


def test_cpi_expenditure_aliases_render_sdmx_categories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = OecdConsumerPriceIndexFetcher()
    calls: list[tuple[str, dict[str, str]]] = []
    row = _fixture(
        "USA",
        "M",
        "CPI",
        period="2024-01",
        METHODOLOGY="N",
        UNIT_MEASURE="PA",
        EXPENDITURE="CP045_0722",
        ADJUSTMENT="N",
        TRANSFORMATION="GY",
    )
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda url, params, _timeout: calls.append((url, dict(params))) or (200, _csv(row)),
    )

    fetcher.fetch(
        ctx=_context("ConsumerPriceIndex"),
        country="USA",
        expenditure=["energy", "services_less_housing"],
    )

    assert calls[0][0].endswith("/USA.M.N.CPI.PA.CP045_0722+SERVXCP041_042_0432.N.GY")


@pytest.mark.parametrize(
    "units,measure,unit_measure",
    (
        ("current_prices", "GDP_USD", "USD_EXC"),
        ("volume", "GDPV_USD", "USD_EXC"),
        ("capita", "GDPVD_CAP", "USD_PPP_PS"),
        ("growth", "GDPV_ANNPCT", "PA"),
        ("deflator", "PGDP", "IX"),
    ),
)
def test_gdp_forecast_validates_units_and_applies_multiplier(
    units: str, measure: str, unit_measure: str
) -> None:
    fetcher = OecdGdpForecastFetcher()
    params = fetcher.transform_query(country="USA", units=units)
    row = _fixture(
        "USA",
        "A",
        measure,
        value="1.25",
        period="2024",
        UNIT_MEASURE=unit_measure,
        UNIT_MULT="6",
    )

    result = fetcher.transform_data([row], params)

    assert result[0].value == pytest.approx(1_250_000.0)


def test_gdp_forecast_rejects_wrong_unit_metadata() -> None:
    fetcher = OecdGdpForecastFetcher()
    params = fetcher.transform_query(country="USA", units="volume")
    row = _fixture("USA", "A", "GDPV_USD", period="2024", UNIT_MEASURE="IX", UNIT_MULT="0")

    with pytest.raises(OecdProviderError) as exc:
        fetcher.transform_data([row], params)

    assert exc.value.code == "OECD_BAD_RESPONSE"


@pytest.mark.parametrize(
    "country,area_code",
    (
        ("eu22_oecd", "EU22OECD"),
        ("euro_area_17_countries", "EA17"),
        ("former_ussr", "SUN_F"),
        ("non_oecd_economies", "WXOECD"),
        ("oecd_asia_oceania", "OECDSO"),
        ("other_countries", "W_O"),
        ("rest_of_world", "WXD"),
        ("world", "W"),
    ),
)
def test_forecast_aggregate_aliases_use_official_sdmx_areas(
    monkeypatch: pytest.MonkeyPatch, country: str, area_code: str
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    row = _fixture(area_code, "A", "GDPV_USD", period="2024", UNIT_MEASURE="USD_EXC", UNIT_MULT="0")
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda url, params, _timeout: calls.append((url, dict(params))) or (200, _csv(row)),
    )

    OecdGdpForecastFetcher().fetch(ctx=_context("GdpForecast"), country=country, units="volume")

    assert calls[0][0].endswith(f"/{area_code}.GDPV_USD.A")


@pytest.mark.parametrize(
    "query,expected_key,measure,frequency,age,sex,adjustment",
    (
        (
            {"frequency": "annual", "age": "total"},
            "/USA.UNE_LF.PT_LF_SUB._Z.N._T.Y_GE15._Z.A",
            "UNE_LF",
            "A",
            "Y_GE15",
            "_T",
            "N",
        ),
        (
            {"frequency": "quarter", "age": "15-24", "sex": "male", "seasonal_adjustment": True},
            "/USA.UNE_LF.PT_LF_SUB._Z.Y.M.Y15T24._Z.Q",
            "UNE_LF",
            "Q",
            "Y15T24",
            "M",
            "Y",
        ),
        (
            {"frequency": "monthly", "age": "25+", "sex": "female", "seasonal_adjustment": True},
            "/USA.UNE_LF_M.PT_LF_SUB._Z.Y.F.Y_GE25._Z.M",
            "UNE_LF_M",
            "M",
            "Y_GE25",
            "F",
            "Y",
        ),
    ),
)
def test_unemployment_query_maps_all_documented_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
    expected_key: str,
    measure: str,
    frequency: str,
    age: str,
    sex: str,
    adjustment: str,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    period = {"A": "2024", "Q": "2024-Q1", "M": "2024-01"}[frequency]
    row = _fixture(
        "USA",
        frequency,
        measure,
        period=period,
        UNIT_MEASURE="PT_LF_SUB",
        TRANSFORMATION="_Z",
        ADJUSTMENT=adjustment,
        SEX=sex,
        AGE=age,
        ACTIVITY="_Z",
    )
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda url, params, _timeout: calls.append((url, dict(params))) or (200, _csv(row)),
    )
    result = OecdUnemploymentFetcher().fetch(ctx=_context("Unemployment"), country="USA", **query)
    assert len(result) == 1
    assert isinstance(result[0], OecdUnemployment)
    assert calls[0][0].endswith(expected_key)


def test_public_query_defaults_match_documented_oecd_defaults() -> None:
    cli = OecdCompositeLeadingIndicatorFetcher().transform_query()
    share = OecdSharePriceIndexFetcher().transform_query()
    forecast = OecdGdpForecastFetcher().transform_query()
    unemployment = OecdUnemploymentFetcher().transform_query()

    assert cli.country == "g20"
    assert share.frequency == "monthly"
    assert forecast.country == "all"
    assert forecast.units == "volume"
    assert unemployment.frequency == "monthly"
    assert unemployment.age == "total"


def test_cli_growth_rate_uses_source_index_and_computes_year_over_year() -> None:
    fetcher = OecdCompositeLeadingIndicatorFetcher()
    params = fetcher.transform_query(country="USA", growth_rate=True, start_date="2024-01-01")
    rows = [
        _fixture(
            "USA",
            "M",
            "LI",
            value="105",
            period="2024-01",
            UNIT_MEASURE="IX",
            ACTIVITY="_Z",
            ADJUSTMENT="AA",
            TRANSFORMATION="IX",
            TIME_HORIZ="_Z",
            METHODOLOGY="H",
        ),
        _fixture(
            "USA",
            "M",
            "LI",
            value="100",
            period="2023-01",
            UNIT_MEASURE="IX",
            ACTIVITY="_Z",
            ADJUSTMENT="AA",
            TRANSFORMATION="IX",
            TIME_HORIZ="_Z",
            METHODOLOGY="H",
        ),
        _fixture(
            "USA",
            "M",
            "LI",
            value="108",
            period="2024-02",
            UNIT_MEASURE="IX",
            ACTIVITY="_Z",
            ADJUSTMENT="AA",
            TRANSFORMATION="IX",
            TIME_HORIZ="_Z",
            METHODOLOGY="H",
        ),
    ]

    result = fetcher.transform_data(rows, params)

    assert [row.date.isoformat() for row in result] == ["2024-01-01", "2024-02-01"]
    assert result[0].value == pytest.approx(5.0)
    assert result[1].value is None


def test_cli_growth_request_fetches_one_year_of_base_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    row = _fixture(
        "USA",
        "M",
        "LI",
        value="105",
        period="2024-01",
        UNIT_MEASURE="IX",
        ACTIVITY="_Z",
        ADJUSTMENT="NOR",
        TRANSFORMATION="IX",
        TIME_HORIZ="_Z",
        METHODOLOGY="H",
    )
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda url, params, _timeout: calls.append((url, dict(params))) or (200, _csv(row)),
    )

    OecdCompositeLeadingIndicatorFetcher().fetch(
        ctx=_context("CompositeLeadingIndicator"),
        country="USA",
        adjustment="normalized",
        growth_rate=True,
        start_date="2024-01-01",
    )

    assert calls[0][0].endswith("/USA.M.LI...NOR.IX..H")
    assert calls[0][1]["startPeriod"] == "2023-01-01"
