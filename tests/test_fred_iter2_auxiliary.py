"""Independent fixtures for the FRED calendar, GeoFRED, BOP and SEP adapters."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_iter2 import (
    FredBalanceOfPayments,
    FredEconomicCalendar,
    FredFederalFundsRate,
    FredOvernightBankFundingRate,
    FredPROJECTIONS,
    FredRegional,
    FredReleaseTable,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.iter2_auxiliary import (
    BalanceOfPaymentsFetcher,
    EconomicCalendarFetcher,
    FredRegionalFetcher,
    FredReleaseTableFetcher,
    ProjectionsFetcher,
)
from opendata.data.providers.fred.provider import PROVIDER as FRED_PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def test_fred_provider_loads_iter2_bindings_with_typed_contracts() -> None:
    expected = (
        (
            "FederalFundsRate",
            "FederalFundsRateFetcher",
            "fred_federal_funds_rate",
            FredFederalFundsRate,
        ),
        (
            "OvernightBankFundingRate",
            "OvernightBankFundingRateFetcher",
            "fred_overnight_bank_funding_rate",
            FredOvernightBankFundingRate,
        ),
        (
            "FredReleaseTable",
            "FredReleaseTableFetcher",
            "fred_release_table",
            FredReleaseTable,
        ),
        (
            "EconomicCalendar",
            "EconomicCalendarFetcher",
            "fred_economic_calendar",
            FredEconomicCalendar,
        ),
        ("FredRegional", "FredRegionalFetcher", "fred_regional", FredRegional),
        (
            "BalanceOfPayments",
            "BalanceOfPaymentsFetcher",
            "fred_balance_of_payments",
            FredBalanceOfPayments,
        ),
        ("PROJECTIONS", "ProjectionsFetcher", "fred_projections", FredPROJECTIONS),
    )
    for model_id, class_name, domain, contract in expected:
        binding = FRED_PROVIDER.fetcher_dict[model_id]
        fetcher = binding.load("fred")
        assert binding.class_name == class_name
        assert fetcher.canonical_model == model_id
        assert fetcher.capability.domain == domain
        assert fetcher.output_model is contract


@pytest.fixture(autouse=True)
def _fred_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")


def _context(fetcher: Any) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic query-only FRED fixture grant",
        task_attempts=32,
        source_attempts=32,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        timeout=5,
        request_budget=RequestBudget(
            task_attempts=32,
            source_attempts=32,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        ),
        operation=RequestOperation.QUERY,
    )


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    responder: Any,
) -> list[tuple[str, dict[str, str]]]:
    requests: list[tuple[str, dict[str, str]]] = []

    def http_get(
        url: str,
        params: dict[str, str],
        timeout: float | None,
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        status, body = responder(url, params)
        return status, json.dumps(body) if type(body) is dict else str(body)

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)
    return requests


def _install_series_http(
    monkeypatch: pytest.MonkeyPatch,
    rows_by_id: dict[str, list[dict[str, str]]],
) -> list[tuple[str, dict[str, str]]]:
    def responder(_url: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        rows = rows_by_id[params["series_id"]]
        offset = int(params["offset"])
        limit = int(params["limit"])
        return 200, {
            "count": len(rows),
            "offset": offset,
            "limit": limit,
            "observations": rows[offset : offset + limit],
        }

    return _install_http(monkeypatch, responder)


def _service_query(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Any,
    contract: type[Any],
    query: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str | None]]]:
    received: list[tuple[str, str | None]] = []
    original_transform = fetcher.transform_query

    def observe_query(**kwargs: object) -> Any:
        params = original_transform(**kwargs)
        received.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model=fetcher.canonical_model,
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
        lambda _domain: (None, contract),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model=fetcher.canonical_model,
            query=query,
            principal=Principal(),
            ctx=_context(fetcher),
        )
    )
    return response, received


def _obs(value: str, *, date: str = "2026-06-30") -> dict[str, str]:
    return {
        "date": date,
        "value": value,
        "realtime_start": "2026-07-01",
        "realtime_end": "9999-12-31",
    }


def test_release_table_flattens_official_element_tree_and_service_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = {
        "element_id": 205,
        "release_id": 53,
        "series_id": "GDPDEF",
        "parent_id": 201,
        "line": "2",
        "type": "series",
        "name": "Gross Domestic Product: Implicit Price Deflator",
        "level": "1",
        "children": [],
        "observation_date": "2026-06-30",
        "observation_value": "137.5",
    }
    table_fixture = {
        "release_id": 53,
        "element_id": 201,
        "parent_id": None,
        "line": "1",
        "type": "table",
        "name": "Gross Domestic Product",
        "level": "0",
        "children": [child],
        "elements": {"205": child},
    }
    requests = _install_http(
        monkeypatch,
        lambda _url, _params: (200, table_fixture),
    )
    fetcher = FredReleaseTableFetcher()
    response, received = _service_query(
        monkeypatch,
        fetcher,
        FredReleaseTable,
        {"release_id": "53", "date": "2026-06-30"},
    )

    assert received == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["model"] == "FredReleaseTable"
    assert response["results"] == [
        {
            "date": None,
            "level": 0,
            "element_type": "table",
            "line": 1,
            "element_id": "201",
            "parent_id": None,
            "children": "205",
            "symbol": None,
            "name": "Gross Domestic Product",
            "value": None,
        },
        {
            "date": "2026-06-30",
            "level": 1,
            "element_type": "series",
            "line": 2,
            "element_id": "205",
            "parent_id": "201",
            "children": None,
            "symbol": "GDPDEF",
            "name": "Gross Domestic Product: Implicit Price Deflator",
            "value": 137.5,
        },
    ]
    url, params = requests[0]
    assert url.endswith("/fred/release/tables")
    assert params["include_observation_values"] == "true"
    assert params["observation_date"] == "2026-06-30"
    assert params["api_key"] == "test-key"


def test_release_table_rejects_invalid_ids_before_io_and_caps_the_flat_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredReleaseTableFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args: pytest.fail("invalid IDs must fail before FRED I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(release_id="53;DROP")

    params = fetcher.transform_query(release_id="53", max_nodes=1)
    payload = {
        "release_id": 53,
        "elements": {
            "1": {
                "element_id": 1,
                "release_id": 53,
                "type": "series",
                "name": "one",
                "level": "0",
                "line": "1",
                "series_id": "AAA",
                "date": "2026-06-30",
                "value": "1.0",
                "children": [],
            },
            "2": {
                "element_id": 2,
                "release_id": 53,
                "type": "series",
                "name": "two",
                "level": "0",
                "line": "2",
                "series_id": "BBB",
                "date": "2026-06-30",
                "value": "2.0",
                "children": [],
            },
        },
    }
    with pytest.raises(FredProviderError, match="FRED_QUERY_LIMIT"):
        fetcher.transform_data(
            [{"requested_date": None, "payload": payload, "release_id": "53", "max_nodes": 1}],
            params,
        )


def test_economic_calendar_paginates_all_releases_and_handles_empty_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_rows = [
        {"release_id": 53, "release_name": "GDP", "date": "2026-10-01"},
        {"release_id": 9, "release_name": "Retail Sales", "date": "2026-10-02"},
        {"release_id": 53, "release_name": "GDP", "date": "2026-10-03"},
    ]
    requests = _install_http(
        monkeypatch,
        lambda _url, params: (
            200,
            {
                "realtime_start": "2026-10-12",
                "realtime_end": "2026-10-12",
                "count": len(source_rows),
                "offset": int(params["offset"]),
                "limit": int(params["limit"]),
                "release_dates": source_rows[
                    int(params["offset"]) : int(params["offset"]) + int(params["limit"])
                ],
            },
        ),
    )
    fetcher = EconomicCalendarFetcher()
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        start_date="2026-10-02",
        end_date="2026-10-03",
        limit=2,
        max_pages=2,
    )
    assert rows == (
        FredEconomicCalendar(date=datetime(2026, 10, 3).date(), release_id=53, event="GDP"),
        FredEconomicCalendar(date=datetime(2026, 10, 2).date(), release_id=9, event="Retail Sales"),
    )
    assert [params["offset"] for _, params in requests] == ["0", "2"]
    assert all(params["realtime_start"] == params["realtime_end"] for _, params in requests)
    assert all(params["include_release_dates_with_no_data"] == "true" for _, params in requests)

    empty_fetcher = EconomicCalendarFetcher()
    _install_http(
        monkeypatch,
        lambda _url, params: (
            200,
            {
                "realtime_start": "2026-10-12",
                "realtime_end": "2026-10-12",
                "count": 0,
                "offset": int(params["offset"]),
                "limit": int(params["limit"]),
                "release_dates": [],
            },
        ),
    )
    assert empty_fetcher.fetch(ctx=_context(empty_fetcher)) == ()

    limited_fetcher = EconomicCalendarFetcher()
    _install_http(
        monkeypatch,
        lambda _url, params: (
            200,
            {
                "realtime_start": "2026-10-12",
                "realtime_end": "2026-10-12",
                "count": 3,
                "offset": int(params["offset"]),
                "limit": int(params["limit"]),
                "release_dates": source_rows[:1],
            },
        ),
    )
    with pytest.raises(FredProviderError, match="FRED_QUERY_LIMIT"):
        limited_fetcher.fetch(ctx=_context(limited_fetcher), limit=1, max_pages=1)


def test_economic_calendar_single_release_joins_official_release_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def responder(url: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        if url.endswith("/fred/release"):
            return 200, {
                "releases": [
                    {"id": 53, "name": "Gross Domestic Product"},
                ]
            }
        return 200, {
            "realtime_start": params["realtime_start"],
            "realtime_end": params["realtime_end"],
            "count": 1,
            "offset": 0,
            "limit": 10_000,
            "release_dates": [{"release_id": 53, "date": "2026-10-29"}],
        }

    requests = _install_http(monkeypatch, responder)
    fetcher = EconomicCalendarFetcher()
    response, received = _service_query(
        monkeypatch,
        fetcher,
        FredEconomicCalendar,
        {"release_id": 53, "start_date": "2026-10-01", "end_date": "2026-10-31"},
    )
    assert received == [("fred", "us")]
    assert response["results"] == [
        {"date": "2026-10-29", "release_id": 53, "event": "Gross Domestic Product"}
    ]
    assert [url.rsplit("/", 1)[-1] for url, _ in requests] == ["release", "dates"]
    assert requests[0][1]["release_id"] == requests[1][1]["release_id"] == "53"


def test_economic_calendar_rejects_bad_query_malformed_page_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = EconomicCalendarFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args: pytest.fail("invalid calendar query must fail before FRED I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(start_date="2026-10-02", end_date="2026-10-01")
    with pytest.raises(ValidationError):
        fetcher.transform_query(include_release_dates_with_no_data="false")

    _install_http(
        monkeypatch,
        lambda _url, params: (
            200,
            {
                "realtime_start": "2026-10-12",
                "realtime_end": "2026-10-12",
                "count": 1,
                "offset": int(params["offset"]) + 1,
                "limit": int(params["limit"]),
                "release_dates": [],
            },
        ),
    )
    with pytest.raises(FredProviderError, match="FRED_PAGINATION_ERROR"):
        fetcher.fetch(ctx=_context(fetcher))

    _install_http(monkeypatch, lambda _url, _params: (429, "rate limited"))
    with pytest.raises(FredProviderError, match="FRED_HTTP_ERROR") as error:
        fetcher.fetch(ctx=_context(fetcher))
    assert error.value.status == 429
    assert "test-key" not in str(error.value)


def test_fred_regional_series_range_uses_typed_rows_and_provider_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_fixture = {
        "meta": {
            "date": "2026-09-01",
            "data": [
                {
                    "date": "2026-08-01",
                    "region": "Alabama",
                    "code": "01",
                    "value": "3.2",
                    "series_id": "ALPCPI",
                },
                {
                    "date": "2026-09-01",
                    "region": "California",
                    "code": "06",
                    "value": ".",
                    "series_id": "CAPCPI",
                },
            ],
        }
    }
    requests = _install_http(monkeypatch, lambda _url, _params: (200, series_fixture))
    fetcher = FredRegionalFetcher()
    response, received = _service_query(
        monkeypatch,
        fetcher,
        FredRegional,
        {"symbol": "WIPCPI", "start_date": "2026-08-01", "end_date": "2026-09-01"},
    )
    assert received == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["results"] == [
        {
            "date": "2026-08-01",
            "region": "Alabama",
            "code": "01",
            "value": 3.2,
            "series_id": "ALPCPI",
        },
        {
            "date": "2026-09-01",
            "region": "California",
            "code": "06",
            "value": None,
            "series_id": "CAPCPI",
        },
    ]
    assert requests[0][0].endswith("/geofred/series/data")
    assert requests[0][1]["date"] == "2026-09-01"
    assert requests[0][1]["start_date"] == "2026-08-01"


def test_fred_regional_group_parses_annual_period_keys_and_rejects_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_fixture = {
        "Personal Income by State": {
            "2013": [{"region": "Alabama", "code": "01", "value": "36014", "series_id": "ALPCPI"}],
            "2014": [{"region": "Alabama", "code": "01", "value": "37000", "series_id": "ALPCPI"}],
        }
    }
    requests = _install_http(monkeypatch, lambda _url, _params: (200, group_fixture))
    fetcher = FredRegionalFetcher()
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        symbol="882",
        is_series_group=True,
        region_type="state",
        units="Dollars",
        frequency="a",
        start_date="2013-01-01",
        end_date="2014-01-01",
    )
    assert rows == (
        FredRegional(
            date=datetime(2013, 1, 1).date(),
            region="Alabama",
            code="01",
            value=36014.0,
            series_id="ALPCPI",
        ),
        FredRegional(
            date=datetime(2014, 1, 1).date(),
            region="Alabama",
            code="01",
            value=37000.0,
            series_id="ALPCPI",
        ),
    )
    assert requests[0][0].endswith("/geofred/regional/data")
    assert requests[0][1]["series_group"] == "882"
    assert requests[0][1]["season"] == "NSA"
    assert requests[0][1]["units"] == "Dollars"
    assert requests[0][1]["date"] == "2014-01-01"

    overflow_fetcher = FredRegionalFetcher()
    _install_http(monkeypatch, lambda _url, _params: (200, group_fixture))
    with pytest.raises(FredProviderError, match="FRED_QUERY_LIMIT"):
        overflow_fetcher.fetch(
            ctx=_context(overflow_fetcher),
            symbol="882",
            is_series_group=True,
            region_type="state",
            units="Dollars",
            frequency="a",
            limit=1,
        )


def test_fred_regional_rejects_incomplete_group_and_bad_date_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredRegionalFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args: pytest.fail("invalid GeoFRED query must fail before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol="882", is_series_group=True, region_type="state")
    with pytest.raises(ValidationError):
        fetcher.transform_query(
            symbol="882", is_series_group="true", region_type="state", units="Dollars"
        )
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol="WIPCPI", start_date="2026-09-01", end_date="2026-08-01")

    _install_http(
        monkeypatch,
        lambda _url, _params: (
            200,
            {
                "meta": {
                    "data": [
                        {"region": "Alabama", "code": "01", "value": "1", "series_id": "ALPCPI"}
                    ]
                }
            },
        ),
    )
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.fetch(ctx=_context(fetcher), symbol="WIPCPI")


def test_fred_regional_rejects_numeric_region_codes_without_source_leading_zeroes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = FredRegionalFetcher()
    _install_http(
        monkeypatch,
        lambda _url, _params: (
            200,
            {
                "meta": {
                    "data": [
                        {"region": "California", "code": 6, "value": "1", "series_id": "CAPCPI"}
                    ]
                }
            },
        ),
    )
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.fetch(ctx=_context(fetcher), symbol="WIPCPI")


_BOP_FIXTURE = {
    "IEABC": [_obs("-220.5", date="2026-04-01")],
    "IEABCG": [_obs("-280.0", date="2026-04-01")],
    "IEABCS": [_obs("90.0", date="2026-04-01")],
    "IEABCPI": [_obs("-15.0", date="2026-04-01")],
    "IEABCSI": [_obs("-15.5", date="2026-04-01")],
}


def test_balance_of_payments_joins_only_official_us_quarterly_series_and_serializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install_series_http(monkeypatch, _BOP_FIXTURE)
    fetcher = BalanceOfPaymentsFetcher()
    response, received = _service_query(
        monkeypatch,
        fetcher,
        FredBalanceOfPayments,
        {"country": "united_states", "frequency": "quarterly", "start_date": "2026-04-01"},
    )
    assert received == [("fred", "us")]
    assert {params["series_id"] for _, params in requests} == set(_BOP_FIXTURE)
    assert all("frequency" not in params for _, params in requests)
    assert response["results"] == [
        {
            "period": "2026-04-01",
            "current_account": -220.5,
            "goods_balance": -280.0,
            "services_balance": 90.0,
            "primary_income_balance": -15.0,
            "secondary_income_balance": -15.5,
            "currency_code": "USD",
            "unit": "millions_of_us_dollars",
            "source_series_ids": {
                "current_account": "IEABC",
                "goods_balance": "IEABCG",
                "services_balance": "IEABCS",
                "primary_income_balance": "IEABCPI",
                "secondary_income_balance": "IEABCSI",
            },
        }
    ]


def test_balance_of_payments_pit_nullable_columns_and_parameter_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = BalanceOfPaymentsFetcher()
    requests = _install_series_http(monkeypatch, {**_BOP_FIXTURE, "IEABCPI": []})
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        as_of="2026-07-01",
        report_type="main",
        frequency="quarterly",
    )
    assert len(rows) == 1
    assert rows[0].primary_income_balance is None
    assert rows[0]._realtime_by_series["IEABC"] == (
        datetime(2026, 7, 1).date(),
        datetime(9999, 12, 31).date(),
    )
    assert {params["series_id"] for _, params in requests} == set(_BOP_FIXTURE)
    assert requests[3][1]["series_id"] == "IEABCPI"

    no_io_fetcher = BalanceOfPaymentsFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args: pytest.fail("unsupported BOP choices must fail before I/O"),
    )
    with pytest.raises(ValidationError):
        no_io_fetcher.transform_query(frequency="monthly")
    with pytest.raises(ValidationError):
        no_io_fetcher.transform_query(report_type="services")
    with pytest.raises(ValidationError):
        no_io_fetcher.transform_query(country="brazil")
        with pytest.raises(ValidationError):
            no_io_fetcher.transform_query(country=None)
        with pytest.raises(ValidationError):
            no_io_fetcher.transform_query(country="united_states")

    with pytest.raises(ValidationError):
        no_io_fetcher.transform_query(country="united_states", frequency=None)


def test_balance_of_payments_rejects_conflicting_vintages_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = BalanceOfPaymentsFetcher()
    params = fetcher.transform_query(frequency="quarterly")
    wrappers = [
        {
            "field_name": "current_account",
            "series_id": "IEABC",
            "observation": _obs("-220.5", date="2026-04-01"),
        },
        {
            "field_name": "current_account",
            "series_id": "IEABC",
            "observation": {
                **_obs("-221.0", date="2026-04-01"),
                "realtime_start": "2026-07-02",
            },
        },
    ]
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(wrappers, params)

    quarter_end = [
        {
            "field_name": "current_account",
            "series_id": "IEABC",
            "observation": _obs("-220.5", date="2026-03-31"),
        }
    ]
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(quarter_end, params)

    _install_http(monkeypatch, lambda _url, _params: (429, "rate limited"))
    with pytest.raises(FredProviderError, match="FRED_HTTP_ERROR") as error:
        fetcher.fetch(ctx=_context(fetcher), frequency="quarterly")
    assert error.value.status == 429
    assert "test-key" not in str(error.value)


_PROJECTION_IDS = {
    "range_high": "FEDTARRH",
    "central_tendency_high": "FEDTARCTH",
    "median": "FEDTARMD",
    "range_midpoint": "FEDTARRM",
    "central_tendency_midpoint": "FEDTARCTM",
    "range_low": "FEDTARRL",
    "central_tendency_low": "FEDTARCTL",
}


def test_fomc_projections_join_seven_verified_series_with_vintage_and_typed_service_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = {
        series_id: [_obs(str(3.0 + index / 10), date="2027-12-31")]
        for index, series_id in enumerate(_PROJECTION_IDS.values())
    }
    requests = _install_series_http(monkeypatch, fixtures)
    fetcher = ProjectionsFetcher()
    response, received = _service_query(
        monkeypatch,
        fetcher,
        FredPROJECTIONS,
        {"vintage_dates": ["2026-07-01"], "start_date": "2027-01-01"},
    )
    assert received == [("fred", "us")]
    assert {params["series_id"] for _, params in requests} == set(_PROJECTION_IDS.values())
    assert all(params["vintage_dates"] == "2026-07-01" for _, params in requests)
    assert response["results"] == [
        {
            "date": "2027-12-31",
            "range_high": 3.0,
            "central_tendency_high": 3.1,
            "median": 3.2,
            "range_midpoint": 3.3,
            "central_tendency_midpoint": 3.4,
            "range_low": 3.5,
            "central_tendency_low": 3.6,
        }
    ]


def test_fomc_long_run_uses_distinct_official_ids_and_missing_values_stay_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_run_ids = {
        "range_high": "FEDTARRHLR",
        "central_tendency_high": "FEDTARCTHLR",
        "median": "FEDTARMDLR",
        "range_midpoint": "FEDTARRMLR",
        "central_tendency_midpoint": "FEDTARCTMLR",
        "range_low": "FEDTARRLLR",
        "central_tendency_low": "FEDTARCTLLR",
    }
    fixtures = {
        series_id: [_obs("." if field_name == "median" else "2.5", date="9999-12-31")]
        for field_name, series_id in long_run_ids.items()
    }
    requests = _install_series_http(monkeypatch, fixtures)
    fetcher = ProjectionsFetcher()
    rows = fetcher.fetch(ctx=_context(fetcher), long_run=True, as_of="2026-07-01")
    assert {params["series_id"] for _, params in requests} == set(long_run_ids.values())
    assert rows[0].median is None
    assert rows[0].range_high == 2.5
    assert rows[0]._realtime_by_series["FEDTARRHLR"] == (
        datetime(2026, 7, 1).date(),
        datetime(9999, 12, 31).date(),
    )


def test_fomc_query_conflicts_and_pagination_limit_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = ProjectionsFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args: pytest.fail("invalid SEP query must fail before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(long_run="true")
    with pytest.raises(ValidationError):
        fetcher.transform_query(as_of="2026-07-01", realtime_start="2026-07-01")
    with pytest.raises(ValidationError):
        fetcher.transform_query(vintage_dates=["2026-07-01", "2026-07-02"])

    many = [_obs("3.0", date="2027-12-31"), _obs("3.1", date="2028-12-31")]
    _install_series_http(monkeypatch, dict.fromkeys(_PROJECTION_IDS.values(), many))
    with pytest.raises(FredProviderError, match="FRED_QUERY_LIMIT"):
        fetcher.fetch(ctx=_context(fetcher), page_size=1, max_records=2, max_pages=1)
