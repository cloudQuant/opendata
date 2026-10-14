"""Offline contract and adapter checks for the fixed FRED macro slice."""

from __future__ import annotations

import asyncio
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_macro_iter2 import (
    FredRetailPrice,
    FredSeniorLoanOfficerSurvey,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models import iter2_macro
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._paged import FredQueryLimitError
from opendata.data.providers.fred.models.iter2_macro import (
    ConsumerPriceIndexFetcher,
    ManufacturingOutlookNYFetcher,
    ManufacturingOutlookTexasFetcher,
    MortgageIndicesFetcher,
    NonFarmPayrollsFetcher,
    PersonalConsumptionExpendituresFetcher,
    RetailPricesFetcher,
    SeniorLoanOfficerSurveyFetcher,
    SurveyOfEconomicConditionsChicagoFetcher,
    UniversityOfMichiganFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    reserve_scoped_attempt,
)


@pytest.fixture(autouse=True)
def _fred_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")


@pytest.fixture
def observation_fixture() -> list[dict[str, str]]:
    """Hand-authored FRED observations with real-time intervals and a dot value."""
    return [
        {
            "date": "2026-08-01",
            "value": "101.25",
            "realtime_start": "2026-09-01",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2026-09-01",
            "value": ".",
            "realtime_start": "2026-10-01",
            "realtime_end": "9999-12-31",
        },
    ]


def _context(fetcher: Any, *, task_attempts: int = 32, source_attempts: int = 24) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only synthetic FRED query grant",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={"api.stlouisfed.org", "www.federalreserve.gov"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        request_budget=RequestBudget(
            task_attempts=task_attempts,
            source_attempts=source_attempts,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
    )


def _response(observations: list[dict[str, str]], *, offset: int, page_size: int) -> str:
    return json.dumps(
        {
            "count": len(observations),
            "offset": offset,
            "limit": page_size,
            "observations": observations[offset : offset + page_size],
        }
    )


def _install_response(
    monkeypatch: pytest.MonkeyPatch,
    observations_by_series: dict[str, list[dict[str, str]]],
) -> list[tuple[str, dict[str, str]]]:
    requests: list[tuple[str, dict[str, str]]] = []

    def http_get(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        observations = observations_by_series[params["series_id"]]
        start = params.get("observation_start")
        end = params.get("observation_end")
        if start is not None:
            observations = [row for row in observations if row["date"] >= start]
        if end is not None:
            observations = [row for row in observations if row["date"] <= end]
        return 200, _response(
            observations,
            offset=int(params["offset"]),
            page_size=int(params["limit"]),
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)
    return requests


def _install_release_response(
    monkeypatch: pytest.MonkeyPatch,
    *,
    release_id: str,
    values_by_date: dict[str, str] | None = None,
) -> list[tuple[str, dict[str, str]]]:
    requests: list[tuple[str, dict[str, str]]] = []
    values_by_date = values_by_date or {"2026-09-01": "12.5"}

    def http_get(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        requested_date = params.get("observation_date")
        actual_date = requested_date or max(values_by_date)
        value = values_by_date.get(actual_date)
        if value is None:
            value = "."
        element_id = params["element_id"]
        table_element_id = int(element_id)
        series_element_id = table_element_id + 100_000
        child = {
            "element_id": series_element_id,
            "release_id": int(release_id),
            "series_id": f"SYNTH{series_element_id}",
            "parent_id": table_element_id,
            "line": "1",
            "type": "series",
            "name": f"Synthetic row for {element_id}",
            "level": "1",
            "date": actual_date,
            "value": value,
            "children": [],
        }
        payload = {
            "element_id": table_element_id,
            "release_id": int(release_id),
            "type": "table",
            "name": f"Synthetic release table {element_id}",
            "level": "0",
            "children": [child],
            "elements": {str(series_element_id): child},
        }
        return 200, json.dumps(payload)

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)
    return requests


def _series_rows(
    *, dates: tuple[str, ...] = ("2026-08-01", "2026-09-01"), base: float = 10.0
) -> list[dict[str, str]]:
    return [
        {
            "date": value_date,
            "value": str(base + index),
            "realtime_start": "2026-10-01",
            "realtime_end": "9999-12-31",
        }
        for index, value_date in enumerate(dates)
    ]


def _sloos_archive(series_specs: list[dict[str, Any]]) -> bytes:
    """Build a small hand-authored archive matching the official SLOOS XML shape."""
    message_ns = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/message"
    frb_ns = "http://www.federalreserve.gov/structure/compact/common"
    query_ns = "http://www.federalreserve.gov/structure/compact/SLOOS_SLOOS"
    common_ns = "http://www.SDMX.org/resources/SDMXML/schemas/v1_0/common"
    root = ET.Element(f"{{{message_ns}}}MessageGroup")
    header = ET.SubElement(root, f"{{{message_ns}}}Header")
    ET.SubElement(
        header, f"{{{message_ns}}}Name"
    ).text = "Senior Loan Officer Opinion Survey on Bank Lending Practices"
    dataset = ET.SubElement(root, f"{{{frb_ns}}}DataSet")
    for spec in series_specs:
        attributes = {
            "PANEL": "DOM",
            "BANKSIZE": "ALL",
            "LOANTYPE": "AUTO",
            "MEASURE": "DEMAND",
            "TERMS": "NA",
            "FREQ": "162",
            "CURRENCY": "NA",
            "UNIT": "Percentage",
            "UNIT_MULT": "1",
            "SERIES_NAME": spec["symbol"],
        }
        attributes.update(spec.get("attributes", {}))
        for removed in spec.get("remove_attributes", ()):
            attributes.pop(removed, None)
        series = ET.SubElement(dataset, f"{{{query_ns}}}Series", attributes)
        annotations = ET.SubElement(series, f"{{{frb_ns}}}Annotations")
        annotation = ET.SubElement(annotations, f"{{{common_ns}}}Annotation")
        ET.SubElement(annotation, f"{{{common_ns}}}AnnotationType").text = "Long Description"
        ET.SubElement(annotation, f"{{{common_ns}}}AnnotationText").text = spec.get(
            "title", "Net percentage of domestic banks reporting stronger demand for auto loans"
        )
        for day, value, status in spec.get("observations", (("2026-06-30", "10", "A"),)):
            ET.SubElement(
                series,
                f"{{{frb_ns}}}Obs",
                {"TIME_PERIOD": day, "OBS_VALUE": value, "OBS_STATUS": status},
            )
    document = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SLOOS_data.xml", document)
    return output.getvalue()


def test_cpi_maps_transform_frequency_and_serializes_contract(
    monkeypatch: pytest.MonkeyPatch, observation_fixture: list[dict[str, str]]
) -> None:
    fetcher = ConsumerPriceIndexFetcher()
    requests = _install_response(monkeypatch, {"CPIAUCSL": observation_fixture})

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        source="fred",
        market="us",
        transform="period",
        frequency="quarter",
        start_date="2026-08-01",
        end_date="2026-08-01",
    )

    assert len(rows) == 1
    assert rows[0].model_dump() == {
        "date": rows[0].date,
        "country": "united_states",
        "value": 101.25,
    }
    assert rows[0]._series_ids == ("CPIAUCSL",)
    assert rows[0]._realtime_by_series["CPIAUCSL"][0].isoformat() == "2026-09-01"
    assert list(rows[0].to_frame(rows).columns) == ["date", "country", "value"]
    assert requests[0][1]["series_id"] == "CPIAUCSL"
    assert requests[0][1]["units"] == "pch"
    assert requests[0][1]["frequency"] == "q"
    assert requests[0][0] == "https://api.stlouisfed.org/fred/series/observations"
    assert requests[0][1]["observation_start"] == "2026-08-01"
    assert requests[0][1]["observation_end"] == "2026-08-01"


def test_cpi_limit_is_a_bounded_source_result_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = ConsumerPriceIndexFetcher()
    observations = _series_rows(dates=("2026-08-01", "2026-09-01", "2026-10-01"))
    requests = _install_response(monkeypatch, {"CPIAUCSL": observations})

    rows = fetcher.fetch(ctx=_context(fetcher), transform="index", limit=2)

    assert [row.date.isoformat() for row in rows] == ["2026-08-01", "2026-09-01"]
    assert requests[0][1]["limit"] == "2"
    assert requests[0][1]["offset"] == "0"


def test_nonfarm_categories_map_to_sa_and_nsa_and_date_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = NonFarmPayrollsFetcher()
    requests = _install_release_response(
        monkeypatch,
        release_id="50",
        values_by_date={"2026-08-01": "10.0", "2026-09-01": "11.0"},
    )

    nsa = fetcher.fetch(ctx=_context(fetcher), category="employees_nsa", date=["2026-08-01"])
    sa = fetcher.fetch(ctx=_context(fetcher), category="employees_sa", date="2026-09-01")

    assert [(row.symbol, row.value) for row in nsa] == [("SYNTH105645", 10.0)]
    assert [(row.symbol, row.value) for row in sa] == [("SYNTH104881", 11.0)]
    assert nsa[0].name == "Synthetic row for 5645"
    assert [params["element_id"] for _, params in requests] == ["5645", "4881"]
    assert requests[0][1]["observation_date"] == "2026-08-01"


def test_nonfarm_maps_all_documented_categories_to_release_elements() -> None:
    fetcher = NonFarmPayrollsFetcher()
    expected = {
        "employees_nsa": "5645",
        "employees_sa": "4881",
        "employees_production_and_nonsupervisory": "6411",
        "employees_women": "6304",
        "employees_women_percent": "6433",
        "avg_hours": "6462",
        "avg_hours_production_and_nonsupervisory": "5923",
        "avg_hours_overtime": "6466",
        "avg_hours_overtime_production_and_nonsupervisory": "6442",
        "avg_earnings_hourly": "6471",
        "avg_earnings_hourly_production_and_nonsupervisory": "5943",
        "avg_earnings_weekly": "6490",
        "avg_earnings_weekly_production_and_nonsupervisory": "6020",
        "index_weekly_hours": "6637",
        "index_weekly_hours_production_and_nonsupervisory": "6095",
        "index_weekly_payrolls": "6654",
        "index_weekly_payrolls_production_and_nonsupervisory": "6280",
    }

    assert set(fetcher.category_element_ids) == set(expected)
    for category, element_id in expected.items():
        query = fetcher.transform_query(category=category)
        assert fetcher.selected_series(query) == [(category, element_id)]


def test_pce_maps_all_documented_categories_and_exact_dates() -> None:
    fetcher = PersonalConsumptionExpendituresFetcher()
    expected = {
        "personal_income": "155443",
        "wages_by_industry": "3151",
        "real_pce_percent_change": "3160",
        "real_pce_quantity_index": "3196",
        "pce_price_index": "3208",
        "pce_dollars": "3220",
        "real_pce_chained_dollars": "3232",
        "pce_price_percent_change": "3172",
    }
    for category, series_id in expected.items():
        query = fetcher.transform_query(category=category)
        assert fetcher.selected_series(query) == [(category, series_id)]
    query = fetcher.transform_query(
        category="pce_price_index",
        date="2024-05-01,2024-04-01",
    )
    assert query.selected_dates() == (
        datetime(2024, 5, 1).date(),
        datetime(2024, 4, 1).date(),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(category="bad_category")


def test_pce_date_list_fetches_release_values_and_typed_hierarchy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = PersonalConsumptionExpendituresFetcher()
    requests = _install_release_response(
        monkeypatch,
        release_id="54",
        values_by_date={"2024-04-01": "5.5", "2024-05-01": "6.5"},
    )

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        category="pce_price_index",
        date=["2024-05-01", "2024-04-01"],
    )

    assert [(row.date.isoformat(), row.value, row.line) for row in rows] == [
        ("2024-05-01", 6.5, 1),
        ("2024-04-01", 5.5, 1),
    ]
    assert all(row.element_id == "103208" for row in rows)
    assert [params["observation_date"] for _, params in requests] == [
        "2024-05-01",
        "2024-04-01",
    ]


def test_michigan_series_merge_keeps_missing_sentinel_nullable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = UniversityOfMichiganFetcher()
    requests = _install_response(
        monkeypatch,
        {
            "UMCSENT": _series_rows(dates=("2026-08-01",), base=96.4),
            "MICH": [
                {
                    "date": "2026-08-01",
                    "value": ".",
                    "realtime_start": "2026-09-01",
                    "realtime_end": "9999-12-31",
                }
            ],
        },
    )

    rows = fetcher.fetch(ctx=_context(fetcher))

    assert len(rows) == 1
    assert rows[0].model_dump() == {
        "date": rows[0].date,
        "consumer_sentiment": 96.4,
        "inflation_expectation": None,
    }
    assert {params["series_id"] for _, params in requests} == {"UMCSENT", "MICH"}
    assert rows[0]._series_ids == ("MICH", "UMCSENT")


def test_mortgage_primary_expands_to_seven_official_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = MortgageIndicesFetcher()
    series = set(fetcher.series_by_index.values())
    rows_by_series = {series_id: _series_rows(dates=("2026-08-01",)) for series_id in series}
    requests = _install_response(monkeypatch, rows_by_series)

    rows = fetcher.fetch(ctx=_context(fetcher))

    assert len(rows) == 7
    assert {params["series_id"] for _, params in requests} == {
        fetcher.series_by_index[index] for index in fetcher.primary_indices
    }
    assert {row.symbol for row in rows} == set(fetcher.primary_indices)
    assert all(row.rate == 10.0 and row.name == row.symbol for row in rows)
    assert all(list(row.model_dump()) == ["date", "symbol", "name", "rate"] for row in rows)


def test_mortgage_ltv_group_expands_only_to_verified_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = MortgageIndicesFetcher()
    component_names = fetcher.group_indices["ltv_lte_80"]
    component_ids = {fetcher.series_by_index[name] for name in component_names}
    requests = _install_response(
        monkeypatch,
        {series_id: _series_rows(dates=("2026-08-01",)) for series_id in component_ids},
    )

    rows = fetcher.fetch(ctx=_context(fetcher), index="ltv_lte_80")

    assert {row.symbol for row in rows} == set(component_names)
    assert {params["series_id"] for _, params in requests} == component_ids


@pytest.mark.parametrize(
    ("fetcher_type", "expected_ids"),
    [
        (
            ManufacturingOutlookNYFetcher,
            ("NOCDINA066MNFRBNY", "NOCINA156MNFRBNY", "NOCDNA156MNFRBNY", "NOCNNA156MNFRBNY"),
        ),
        (
            ManufacturingOutlookTexasFetcher,
            ("VNWOUAMFRBDAL", "VNWOIUAMFRBDAL", "VNWODUAMFRBDAL", "VNWONUAMFRBDAL"),
        ),
    ],
)
def test_new_orders_manufacturing_rows_merge_from_four_series(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    expected_ids: tuple[str, ...],
) -> None:
    fetcher = fetcher_type()
    field_to_id = fetcher.series_by_field
    requests = _install_response(
        monkeypatch,
        {
            series_id: _series_rows(dates=("2026-08-01",), base=base)
            for base, series_id in enumerate(expected_ids, 1)
        },
    )

    rows = fetcher.fetch(ctx=_context(fetcher), topic="new_orders", frequency="m")

    assert len(rows) == 1
    assert rows[0].topic == "new_orders"
    assert rows[0].diffusion_index == 1.0
    assert rows[0].percent_reporting_increase == 2.0
    assert rows[0].percent_reporting_decrease == 3.0
    assert rows[0].percent_reporting_no_change == 4.0
    assert {params["series_id"] for _, params in requests} == set(expected_ids)
    assert all(params["frequency"] == "m" for _, params in requests)
    assert set(field_to_id.values()) == set(expected_ids)


@pytest.mark.parametrize(
    ("fetcher_type", "topic_csv", "topic_order"),
    [
        (
            ManufacturingOutlookNYFetcher,
            "prices_paid, new_orders,prices_paid",
            ("prices_paid", "new_orders"),
        ),
        (
            ManufacturingOutlookTexasFetcher,
            "new_orders_growth,business_outlook,new_orders_growth",
            ("new_orders_growth", "business_outlook"),
        ),
    ],
)
def test_manufacturing_csv_topics_merge_by_date_and_topic_without_crossing_fields(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    topic_csv: str,
    topic_order: tuple[str, str],
) -> None:
    fetcher = fetcher_type()
    query = fetcher.transform_query(topic=topic_csv, seasonally_adjusted=True)
    assert query.topic == ",".join(topic_order)
    with pytest.raises(ValueError, match="series_for only accepts one topic"):
        fetcher.series_for(query)
    topic_series = fetcher.topic_series_for(query)
    expected_request_ids = [
        series_id for topic in topic_order for series_id in topic_series[topic].values()
    ]
    observations_by_series: dict[str, list[dict[str, str]]] = {}
    for topic_index, topic in enumerate(topic_order):
        dates = ("2026-08-01", "2026-09-01") if topic_index == 0 else ("2026-08-01",)
        for field_index, series_id in enumerate(topic_series[topic].values(), 1):
            observations_by_series[series_id] = _series_rows(
                dates=dates,
                base=topic_index * 10 + field_index,
            )
    requests = _install_response(monkeypatch, observations_by_series)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        topic=topic_csv,
        seasonally_adjusted=True,
        frequency="quarter",
    )

    expected_keys = sorted(
        [
            ("2026-08-01", topic_order[0]),
            ("2026-08-01", topic_order[1]),
            ("2026-09-01", topic_order[0]),
        ]
    )
    assert [(row.date.isoformat(), row.topic) for row in rows] == expected_keys
    assert len({(row.date, row.topic) for row in rows}) == len(rows)
    assert [params["series_id"] for _, params in requests] == expected_request_ids
    assert len(requests) == 8
    assert all(params["frequency"] == "q" for _, params in requests)

    for row in rows:
        topic_index = topic_order.index(row.topic)
        date_offset = 1 if row.date.isoformat() == "2026-09-01" else 0
        expected_values = [
            topic_index * 10 + field_index + date_offset for field_index in range(1, 5)
        ]
        assert [
            row.diffusion_index,
            row.percent_reporting_increase,
            row.percent_reporting_decrease,
            row.percent_reporting_no_change,
        ] == expected_values
        serialized = json.loads(row.model_dump_json())
        assert serialized["topic"] == row.topic
        assert serialized["date"] == row.date.isoformat()
        assert "_series_ids" not in serialized


@pytest.mark.parametrize(
    "fetcher_type",
    [
        ManufacturingOutlookNYFetcher,
        ManufacturingOutlookTexasFetcher,
    ],
)
@pytest.mark.parametrize(
    "invalid_topics",
    [
        "",
        "new_orders,",
        ",new_orders",
        "new_orders,,prices_paid",
        "new_orders,unknown_topic",
    ],
)
def test_manufacturing_csv_topic_validation_rejects_whole_query_before_io(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    invalid_topics: str,
) -> None:
    fetcher = fetcher_type()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid multi-topic query must fail before I/O"),
    )

    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), topic=invalid_topics)


@pytest.mark.parametrize(
    ("fetcher_type", "topics"),
    [
        (ManufacturingOutlookNYFetcher, "prices_paid,new_orders"),
        (ManufacturingOutlookTexasFetcher, "new_orders_growth,business_outlook"),
    ],
)
def test_manufacturing_multi_topic_request_budget_counts_each_http_attempt(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    topics: str,
) -> None:
    fetcher = fetcher_type()
    calls: list[dict[str, str]] = []

    class BudgetedSyntheticClient:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: float,
            source: str,
        ) -> SimpleNamespace:
            del url, timeout
            reserve_scoped_attempt(current_request_scopes(), source, "api.stlouisfed.org")
            calls.append(dict(params))
            return SimpleNamespace(
                status_code=200,
                text=_response([], offset=int(params["offset"]), page_size=int(params["limit"])),
            )

    client = BudgetedSyntheticClient()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client.get_shared_http_client",
        lambda: client,
    )
    context = _context(fetcher, task_attempts=7, source_attempts=7)

    with pytest.raises(RequestAttemptLimitError):
        fetcher.fetch(ctx=context, topic=topics, max_pages=1)

    assert len(calls) == 7
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 7


def test_manufacturing_topic_and_seasonal_maps_are_fixed_from_fred_releases() -> None:
    ny = ManufacturingOutlookNYFetcher()
    ny_query = ny.transform_query(topic="prices_paid")
    assert dict(ny.selected_series(ny_query)) == {
        "diffusion_index": "PPCDINA066MNEMFRBNY",
        "percent_reporting_increase": "PPCINA156MNEMFRBNY",
        "percent_reporting_decrease": "PPCDNA156MNEMFRBNY",
        "percent_reporting_no_change": "PPCNNA156MNEMFRBNY",
    }
    ny_sa = ny.transform_query(topic="business_outlook", seasonally_adjusted=True)
    assert dict(ny.selected_series(ny_sa)) == {
        "diffusion_index": "GAFDISA066MSFRBNY",
        "percent_reporting_increase": "GAFISA156MSFRBNY",
        "percent_reporting_decrease": "GAFDSA156MSFRBNY",
        "percent_reporting_no_change": "GAFNSA156MSFRBNY",
    }

    ny_prefixes = {
        "business_outlook": "GAF",
        "hours_worked": "AWC",
        "employment": "NEC",
        "inventories": "IVC",
        "prices_received": "PRC",
        "prices_paid": "PPC",
        "capex": "CEF",
        "unfilled_orders": "UOC",
        "new_orders": "NOC",
        "shipments": "SHC",
        "delivery_times": "DTC",
    }
    for topic, prefix in ny_prefixes.items():
        query = ny.transform_query(topic=topic)
        suffix = "NEMFRBNY" if topic in {"prices_received", "prices_paid"} else "NFRBNY"
        assert dict(ny.selected_series(query))["diffusion_index"] == (f"{prefix}DINA066M{suffix}")
        adjusted = ny.transform_query(topic=topic, seasonally_adjusted=True)
        assert dict(ny.selected_series(adjusted))["diffusion_index"] == (f"{prefix}DISA066MSFRBNY")

    texas = ManufacturingOutlookTexasFetcher()
    texas_sa = texas.transform_query(topic="new_orders_growth", seasonally_adjusted=True)
    assert dict(texas.selected_series(texas_sa)) == {
        "diffusion_index": "GROSAMFRBDAL",
        "percent_reporting_increase": "GROISAMFRBDAL",
        "percent_reporting_decrease": "GRODSAMFRBDAL",
        "percent_reporting_no_change": "GRONSAMFRBDAL",
    }

    texas_prefixes = {
        "business_activity": "BACT",
        "business_outlook": "FCOLK",
        "capex": "CEXP",
        "prices_paid": "PRM",
        "production": "PROD",
        "inventory": "FGI",
        "new_orders": "VNWO",
        "new_orders_growth": "GRO",
        "unfilled_orders": "UFIL",
        "shipments": "VSHP",
        "delivery_time": "DTM",
        "employment": "NEMP",
        "wages": "WGS",
        "hours_worked": "AVGWK",
    }
    for topic, prefix in texas_prefixes.items():
        query = texas.transform_query(topic=topic)
        assert dict(texas.selected_series(query))["diffusion_index"] == (f"{prefix}UAMFRBDAL")
        adjusted = texas.transform_query(topic=topic, seasonally_adjusted=True)
        assert dict(texas.selected_series(adjusted))["diffusion_index"] == (f"{prefix}SAMFRBDAL")


@pytest.mark.parametrize(
    ("fetcher_type", "query_fields"),
    [
        (UniversityOfMichiganFetcher, {}),
        (ManufacturingOutlookNYFetcher, {"topic": "prices_paid"}),
        (ManufacturingOutlookTexasFetcher, {"topic": "new_orders_growth"}),
        (SurveyOfEconomicConditionsChicagoFetcher, {}),
    ],
)
@pytest.mark.parametrize(
    ("public_frequency", "fred_frequency"),
    [("annual", "a"), ("quarter", "q"), (None, None)],
)
def test_public_survey_frequencies_map_to_fred_wire_tokens(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    query_fields: dict[str, str],
    public_frequency: str | None,
    fred_frequency: str | None,
) -> None:
    fetcher = fetcher_type()
    query = fetcher.transform_query(**query_fields, frequency=public_frequency)
    selected = dict(fetcher.selected_series(query))
    requests = _install_response(
        monkeypatch,
        {series_id: _series_rows(dates=("2026-08-01",)) for series_id in selected.values()},
    )

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        **query_fields,
        frequency=public_frequency,
        source="fred",
        market="us",
    )

    assert rows
    assert {params["series_id"] for _, params in requests} == set(selected.values())
    assert all(params.get("frequency") == fred_frequency for _, params in requests)


@pytest.mark.parametrize(
    "fetcher_type",
    [
        UniversityOfMichiganFetcher,
        ManufacturingOutlookNYFetcher,
        ManufacturingOutlookTexasFetcher,
        SurveyOfEconomicConditionsChicagoFetcher,
    ],
)
def test_invalid_public_survey_frequency_fails_before_io(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
) -> None:
    fetcher = fetcher_type()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid frequency must fail before I/O"),
    )

    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), frequency=0)


@pytest.mark.parametrize(
    "fetcher_type",
    [
        UniversityOfMichiganFetcher,
        ManufacturingOutlookNYFetcher,
        ManufacturingOutlookTexasFetcher,
        SurveyOfEconomicConditionsChicagoFetcher,
    ],
)
@pytest.mark.parametrize(
    ("legacy_frequency", "fred_frequency"), [("m", "m"), ("q", "q"), ("a", "a")]
)
def test_legacy_frequency_shorthands_are_preserved(
    fetcher_type: type[Any], legacy_frequency: str, fred_frequency: str
) -> None:
    fetcher = fetcher_type()
    query = fetcher.transform_query(frequency=legacy_frequency)

    assert fetcher.source_frequency(query) == fred_frequency


def test_chicago_nine_series_merge_supports_bounded_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SurveyOfEconomicConditionsChicagoFetcher()
    series = set(fetcher.series_by_field.values())
    requests = _install_response(
        monkeypatch,
        {
            series_id: _series_rows(base=float(index))
            for index, series_id in enumerate(sorted(series))
        },
    )

    rows = fetcher.fetch(ctx=_context(fetcher), page_size=1, max_pages=2, max_records=2)

    assert len(rows) == 2
    assert len(requests) == len(series) * 2
    assert {params["series_id"] for _, params in requests} == series
    assert {params["offset"] for _, params in requests} == {"0", "1"}
    assert rows[0].activity_index is not None
    assert list(rows[0].model_dump()) == [
        "date",
        "activity_index",
        "one_year_outlook",
        "manufacturing_activity",
        "non_manufacturing_activity",
        "capital_expenditures_expectations",
        "hiring_expectations",
        "current_hiring",
        "labor_costs",
        "non_labor_costs",
    ]


def test_total_page_budget_fails_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = MortgageIndicesFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("aggregate page budget must fail before I/O"),
    )
    with pytest.raises(FredProviderError, match="FRED_BAD_QUERY"):
        fetcher.fetch(
            ctx=_context(fetcher),
            index=list(fetcher.series_by_index),
            max_pages=2,
        )


def test_invalid_queries_are_rejected_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid queries must fail before I/O"),
    )
    with pytest.raises(ValidationError):
        ConsumerPriceIndexFetcher().fetch(
            ctx=_context(ConsumerPriceIndexFetcher()), harmonized=True
        )
    with pytest.raises(ValidationError):
        NonFarmPayrollsFetcher().fetch(
            ctx=_context(NonFarmPayrollsFetcher()), category="not_a_public_category"
        )
    with pytest.raises(ValidationError):
        NonFarmPayrollsFetcher().fetch(ctx=_context(NonFarmPayrollsFetcher()), date=["2026-02-30"])
    with pytest.raises(ValidationError):
        PersonalConsumptionExpendituresFetcher().fetch(
            ctx=_context(PersonalConsumptionExpendituresFetcher()),
            date=["2026-01-01", "2026-01-01"],
        )
    with pytest.raises(ValidationError):
        NonFarmPayrollsFetcher().fetch(
            ctx=_context(NonFarmPayrollsFetcher()), date=["2026-01-01"] * 11
        )
    with pytest.raises(ValidationError):
        MortgageIndicesFetcher().fetch(
            ctx=_context(MortgageIndicesFetcher()),
            index=["ltv_lte_80", "ltv_lte80_fico_ge740"],
        )


def test_bad_source_shape_bad_value_and_as_of_mismatch_fail_closed(
    observation_fixture: list[dict[str, str]],
) -> None:
    fetcher = ConsumerPriceIndexFetcher()
    params = fetcher.transform_query(transform="index")
    wrapped = [
        {
            "_selection": "total",
            "_series_id": "CPIAUCSL",
            "observation": observation_fixture[0],
        }
    ]
    assert fetcher.transform_data(wrapped, params)[0].value == 101.25
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([None], params)
    bad_value = [{**wrapped[0], "observation": {**observation_fixture[0], "value": "NaN"}}]
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(bad_value, params)
    as_of_params = fetcher.transform_query(transform="index", as_of="2026-08-15")
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(wrapped, as_of_params)


def test_missing_key_and_rate_limit_are_explicit_errors(
    monkeypatch: pytest.MonkeyPatch, observation_fixture: list[dict[str, str]]
) -> None:
    fetcher = ConsumerPriceIndexFetcher()
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(
        "opendata.core.config.get_settings",
        lambda: SimpleNamespace(fred_api_key=None, fred_api_base_url=None),
    )
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("missing API key must fail before HTTP"),
    )
    with pytest.raises(FredProviderError, match="FRED_API_KEY_MISSING"):
        fetcher.fetch(ctx=_context(fetcher))

    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")

    def rate_limited(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del url, params, timeout
        return 429, "rate limited"

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", rate_limited)
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=_context(fetcher))
    assert error.value.code == "FRED_HTTP_ERROR"
    assert error.value.status == 429


def test_source_page_record_limit_is_not_silently_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = ConsumerPriceIndexFetcher()
    _install_response(monkeypatch, {"CPIAUCSL": _series_rows()})

    with pytest.raises(FredQueryLimitError, match="FRED_QUERY_LIMIT"):
        fetcher.fetch(ctx=_context(fetcher), page_size=1, max_pages=1, max_records=1)


@pytest.mark.parametrize(
    ("item", "series_id"),
    [
        ("bacon", "APU0000704111"),
        ("bananas", "APU0000711211"),
        ("butter", "APU0000FS1101"),
        ("chops", "APU0000FD3101"),
        ("cookies", "APU0000702421"),
        ("diesel", "APU000074717"),
        ("electricity", "APU000072610"),
        ("flour", "APU0000701111"),
        ("gasoline", "APU00007471A"),
        ("grapefruit", "APU0000711411"),
        ("ground_beef", "APU0000FC1101"),
        ("ham", "APU0000FD2101"),
        ("ice_cream", "APU0000710411"),
        ("lemons", "APU0000711412"),
        ("malt_beverages", "APU0000720111"),
        ("orange_juice", "APU0000713111"),
        ("potato_chips", "APU0000718311"),
        ("spaghetti", "APU0000701322"),
        ("strawberries", "APU0000711415"),
        ("sugar", "APU0000715211"),
        ("unleaded", "APU00007471A"),
        ("vodka", "APU0000720222"),
        ("wine", "APU0000720311"),
        ("yogurt", "APU0000FJ4101"),
    ],
)
def test_retail_prices_exact_item_series_mapping(item: str, series_id: str) -> None:
    fetcher = RetailPricesFetcher()
    params = fetcher.transform_query(item=item, region="all_city")
    assert fetcher.selected_series(params) == [(item, series_id)]


@pytest.mark.parametrize(
    ("region", "area_code"),
    [
        ("all_city", "0000"),
        ("northeast", "0100"),
        ("midwest", "0200"),
        ("south", "0300"),
        ("west", "0400"),
    ],
)
def test_retail_prices_uses_official_area_code(region: str, area_code: str) -> None:
    fetcher = RetailPricesFetcher()
    params = fetcher.transform_query(item="bacon", region=region)
    assert fetcher.selected_series(params) == [("bacon", f"APU{area_code}704111")]


def test_retail_prices_rejects_unmapped_public_selectors_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RetailPricesFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail(
            "unsupported RetailPrices selectors must fail before I/O"
        ),
    )
    assert len(iter2_macro._RETAIL_ITEM_CHOICES) == 51
    assert set(iter2_macro._RETAIL_ITEM_CHOICES) == (
        set(iter2_macro._RETAIL_ITEM_CODES) | set(iter2_macro._RETAIL_SELECTOR_GAPS)
    )
    with pytest.raises(ValidationError, match="default full fuel basket"):
        fetcher.transform_query()
    for item, reason in iter2_macro._RETAIL_SELECTOR_GAPS.items():
        with pytest.raises(ValidationError) as error:
            fetcher.transform_query(item=item)
        assert reason in str(error.value)
    with pytest.raises(ValidationError, match="not_a_selector"):
        fetcher.transform_query(item="not_a_selector")
    with pytest.raises(ValidationError, match="only United States"):
        fetcher.transform_query(item="bacon", country=None)


def test_retail_prices_fetches_and_serializes_a_typed_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RetailPricesFetcher()
    requests = _install_response(
        monkeypatch,
        {"APU0200704111": _series_rows(dates=("2026-08-01",), base=4.25)},
    )
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        item="bacon",
        region="midwest",
        frequency="quarter",
        transform="pc1",
    )

    assert len(requests) == 1
    _, params = requests[0]
    assert params["series_id"] == "APU0200704111"
    assert params["frequency"] == "q"
    assert params["units"] == "pc1"
    assert len(rows) == 1 and isinstance(rows[0], FredRetailPrice)
    assert rows[0].model_dump(mode="json") == {
        "date": "2026-08-01",
        "symbol": "APU0200704111",
        "country": "united_states",
        "description": (
            "Bacon, sliced, per lb. (453.6 gm) in Midwest, average price, not seasonally adjusted"
        ),
        "value": 4.25,
    }
    assert rows[0]._series_ids == ("APU0200704111",)


def test_retail_prices_rejects_bad_source_shape_and_nonfinite_value() -> None:
    fetcher = RetailPricesFetcher()
    params = fetcher.transform_query(item="bacon")
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([None], params)
    wrapped = {
        "_selection": "bacon",
        "_series_id": "APU0000704111",
        "observation": {
            "date": "2026-08-01",
            "value": "NaN",
            "realtime_start": "2026-09-01",
            "realtime_end": "9999-12-31",
        },
    }
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([wrapped], params)


def test_retail_prices_rate_limit_and_page_budget_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RetailPricesFetcher()
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(
        "opendata.core.config.get_settings",
        lambda: SimpleNamespace(fred_api_key=None, fred_api_base_url=None),
    )
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("missing key must fail before HTTP"),
    )
    with pytest.raises(FredProviderError, match="FRED_API_KEY_MISSING"):
        fetcher.fetch(ctx=_context(fetcher), item="bacon")

    monkeypatch.setenv("FRED_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: (429, "rate limited"),
    )
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=_context(fetcher), item="bacon")
    assert error.value.code == "FRED_HTTP_ERROR"
    assert error.value.status == 429

    _install_response(
        monkeypatch,
        {"APU0000704111": _series_rows(dates=("2026-08-01", "2026-09-01"))},
    )
    with pytest.raises(FredQueryLimitError, match="FRED_QUERY_LIMIT"):
        fetcher.fetch(
            ctx=_context(fetcher),
            item="bacon",
            page_size=1,
            max_pages=1,
            max_records=2,
        )


def test_provider_model_query_injects_identity_and_serializes_retail_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.services import provider_model_query

    fetcher = RetailPricesFetcher()
    _install_response(
        monkeypatch,
        {"APU0200704111": _series_rows(dates=("2026-08-01",), base=5.5)},
    )
    observed: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object):
        params = original_transform_query(**kwargs)
        observed.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model="RetailPrices",
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
        lambda _domain: (None, FredRetailPrice),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model="RetailPrices",
            query={"item": "bacon", "region": "midwest", "source": "auto"},
            principal=Principal(),
            ctx=FetchContext(timeout=2.0, request_budget=_context(fetcher).request_budget),
        )
    )

    assert observed == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["model"] == "RetailPrices"
    assert response["results"] == [
        {
            "date": "2026-08-01",
            "symbol": "APU0200704111",
            "country": "united_states",
            "description": (
                "Bacon, sliced, per lb. (453.6 gm) in Midwest, average price, "
                "not seasonally adjusted"
            ),
            "value": 5.5,
        }
    ]


def test_sloos_category_filters_use_the_official_dimension_values() -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    specs = [
        {
            "symbol": "SLOOS_SPREAD.Q",
            "attributes": {"LOANTYPE": "CILG", "MEASURE": "TERMS", "TERMS": "SPREAD"},
        },
        {"symbol": "SLOOS_CONSUMER.Q", "attributes": {"LOANTYPE": "NOCCAU"}},
        {"symbol": "SLOOS_HELOC.Q", "attributes": {"LOANTYPE": "HELOC"}},
        {"symbol": "SLOOS_AUTO.Q", "attributes": {"LOANTYPE": "AUTO"}},
        {"symbol": "SLOOS_CARD.Q", "attributes": {"LOANTYPE": "CC"}},
        {"symbol": "SLOOS_FIRMS.Q", "attributes": {"LOANTYPE": "CI"}},
        {"symbol": "SLOOS_MORTGAGE.Q", "attributes": {"LOANTYPE": "GSE"}},
        {"symbol": "SLOOS_CRE.Q", "attributes": {"LOANTYPE": "CREMFR"}},
        {"symbol": "SLOOS_STANDARDS.Q", "attributes": {"LOANTYPE": "INST", "MEASURE": "STND"}},
        {"symbol": "SLOOS_DEMAND.Q", "attributes": {"LOANTYPE": "MORT", "MEASURE": "DEMAND"}},
        {"symbol": "SLOOS_FOREIGN.Q", "attributes": {"PANEL": "FGN", "LOANTYPE": "CI"}},
        {"symbol": "SLOOS_NOT_SPREAD.Q", "attributes": {"MEASURE": "STND", "TERMS": "SPREAD"}},
    ]
    archive = _sloos_archive(specs)
    expected = {
        "spreads": {"SLOOS_SPREAD.Q"},
        "consumer": {
            "SLOOS_CONSUMER.Q",
            "SLOOS_AUTO.Q",
            "SLOOS_CARD.Q",
            "SLOOS_STANDARDS.Q",
            "SLOOS_NOT_SPREAD.Q",
        },
        "auto": {"SLOOS_AUTO.Q", "SLOOS_NOT_SPREAD.Q"},
        "credit_card": {"SLOOS_CARD.Q"},
        "firms": {"SLOOS_SPREAD.Q", "SLOOS_FIRMS.Q", "SLOOS_FOREIGN.Q"},
        "mortgage": {"SLOOS_MORTGAGE.Q", "SLOOS_DEMAND.Q"},
        "commercial_real_estate": {"SLOOS_CRE.Q"},
        "standards": {"SLOOS_STANDARDS.Q", "SLOOS_NOT_SPREAD.Q"},
        "demand": {
            "SLOOS_CONSUMER.Q",
            "SLOOS_AUTO.Q",
            "SLOOS_CARD.Q",
            "SLOOS_FIRMS.Q",
            "SLOOS_HELOC.Q",
            "SLOOS_MORTGAGE.Q",
            "SLOOS_CRE.Q",
            "SLOOS_DEMAND.Q",
            "SLOOS_FOREIGN.Q",
        },
        "foreign_banks": {"SLOOS_FOREIGN.Q"},
    }
    assert set(iter2_macro._SLOOS_SELECTOR_CHOICES) == set(iter2_macro._SLOOS_SELECTOR_FILTERS)
    for category, expected_symbols in expected.items():
        params = fetcher.transform_query(category=category)
        rows = fetcher.transform_data(archive, params)
        assert {row.symbol for row in rows} == expected_symbols


def test_sloos_unit_multiplier_uses_the_official_factor_values() -> None:
    assert iter2_macro._parse_sloos_multiplier("1") == 1.0
    assert iter2_macro._parse_sloos_multiplier(".1") == 0.1
    assert iter2_macro._parse_sloos_multiplier("1000") == 1000.0
    for unsupported in (None, "-1", "10", "nan", "1e308"):
        with pytest.raises(FredProviderError):
            iter2_macro._parse_sloos_multiplier(unsupported)


def test_sloos_bad_category_and_vintage_options_fail_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    client = SimpleNamespace(
        get=lambda *args, **kwargs: pytest.fail("invalid SLOOS query must fail before I/O")
    )
    monkeypatch.setattr(
        "opendata.data.providers.fred.models.iter2_macro.get_shared_http_client",
        lambda: client,
    )
    with pytest.raises(ValidationError, match="category"):
        fetcher.transform_query(category="not_a_category")
    with pytest.raises(ValidationError, match="no historical real-time vintages"):
        fetcher.transform_query(category="auto", as_of="2026-01-01")
    with pytest.raises(ValidationError, match="canonical YYYY-MM-DD"):
        fetcher.transform_query(start_date="2026-2-01")


def test_sloos_download_uses_official_public_archive_and_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    archive = _sloos_archive([{"symbol": "SLOOS_AUTO.Q"}])
    calls: list[tuple[str, dict[str, str], float, str]] = []

    class FakeClient:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: float,
            source: str,
        ) -> SimpleNamespace:
            calls.append((url, params, timeout, source))
            return SimpleNamespace(status_code=200, content=archive)

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(
        "opendata.data.providers.fred.models.iter2_macro.get_shared_http_client",
        FakeClient,
    )
    rows = fetcher.fetch(
        ctx=FetchContext(timeout=1.25, request_budget=_context(fetcher).request_budget),
        category="auto",
    )
    assert calls == [
        (
            iter2_macro._SLOOS_DOWNLOAD_URL,
            {"rel": "SLOOS", "filetype": "zip"},
            1.25,
            "fred",
        )
    ]
    assert len(rows) == 1 and isinstance(rows[0], FredSeniorLoanOfficerSurvey)
    assert rows[0].model_dump(mode="json") == {
        "date": "2026-06-30",
        "symbol": "SLOOS_AUTO.Q",
        "value": 10.0,
        "title": "Net percentage of domestic banks reporting stronger demand for auto loans",
    }


def test_sloos_rate_limit_is_reported_without_retrying_unboundedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.data.http_client import FailureCategory, HttpFetchError

    fetcher = SeniorLoanOfficerSurveyFetcher()
    failure = HttpFetchError(
        FailureCategory.RATE_LIMITED,
        url=iter2_macro._SLOOS_DOWNLOAD_URL,
        host="www.federalreserve.gov",
        attempts=1,
        status=429,
        retryable=True,
    )
    attempts = 0

    class FakeClient:
        def get(self, *args: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            raise failure

    monkeypatch.setattr(
        "opendata.data.providers.fred.models.iter2_macro.get_shared_http_client",
        FakeClient,
    )
    with pytest.raises(FredProviderError) as error:
        fetcher.fetch(ctx=_context(fetcher), category="auto")
    assert error.value.code == "FRED_HTTP_ERROR"
    assert error.value.status == 429
    assert attempts == 1


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("lin", 16.0),
        ("chg", 8.0),
        ("ch1", 15.0),
        ("pch", 100.0),
        ("pc1", 1500.0),
        ("pca", 1500.0),
        ("cch", 100.0 * math.log(2.0)),
        ("cca", 400.0 * math.log(2.0)),
        ("log", math.log(16.0)),
    ],
)
def test_sloos_supports_fred_transform_contract(transform: str, expected: float) -> None:
    values = [(1.0, 2.0, 4.0, 8.0, 16.0)]
    observations = [
        (date, value)
        for date, value in zip(
            [
                datetime(2020, 3, 31).date(),
                datetime(2020, 6, 30).date(),
                datetime(2020, 9, 30).date(),
                datetime(2020, 12, 31).date(),
                datetime(2021, 3, 31).date(),
            ],
            values[0],
            strict=True,
        )
    ]
    transformed = iter2_macro._transform_sloos_observations(observations, transform)
    assert transformed[-1][1] is not None
    assert transformed[-1][1] == pytest.approx(expected)


def test_sloos_transform_uses_history_before_date_filter_and_omits_unavailable_values() -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    archive = _sloos_archive(
        [
            {
                "symbol": "SLOOS_AUTO.Q",
                "observations": [
                    ("2020-03-31", "10", "A"),
                    ("2020-06-30", "12", "A"),
                    ("2020-09-30", "15", "A"),
                    ("2020-12-31", "20", "A"),
                    ("2021-03-31", "25", "A"),
                    ("2021-06-30", ".", "NA"),
                ],
            }
        ]
    )
    rows = fetcher.transform_data(
        archive,
        fetcher.transform_query(
            category="auto",
            start_date="2021-03-31",
            end_date="2021-03-31",
            transform="pc1",
        ),
    )
    assert len(rows) == 1 and rows[0].value == pytest.approx(150.0)
    missing = iter2_macro._transform_sloos_observations(
        [(datetime(2024, 3, 31).date(), -2.0)], "log"
    )
    assert missing[0][1] is None
    missing_rows = fetcher.transform_data(
        _sloos_archive(
            [
                {
                    "symbol": "SLOOS_AUTO.Q",
                    "observations": [
                        ("2026-03-31", "10", "A"),
                        ("2026-06-30", ".", "NA"),
                    ],
                }
            ]
        ),
        fetcher.transform_query(category="auto"),
    )
    assert [row.value for row in missing_rows] == [10.0]


def test_sloos_rejects_bad_shape_unknown_enum_duplicate_time_and_bad_numbers() -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    params = fetcher.transform_query(category="auto")
    prefixed_symbol_rows = fetcher.transform_data(
        _sloos_archive([{"symbol": "H0.SUBLPDHMD_N.Q"}]), params
    )
    assert prefixed_symbol_rows[0].symbol == "H0.SUBLPDHMD_N.Q"
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(b"not a zip", params)
    hostile_xml = io.BytesIO()
    with zipfile.ZipFile(hostile_xml, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SLOOS_data.xml",
            b'<!DOCTYPE x [<!ENTITY entity "blocked">]><x>&entity;</x>',
        )
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(hostile_xml.getvalue(), params)
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(_sloos_archive([]), params)
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(
            _sloos_archive([{"symbol": "SLOOS_BAD.Q", "attributes": {"LOANTYPE": "UNKNOWN"}}]),
            params,
        )
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(_sloos_archive([{"symbol": "invalid/name"}]), params)
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.transform_data(
            _sloos_archive([{"symbol": "SLOOS_BAD.Q", "remove_attributes": ("MEASURE",)}]),
            params,
        )
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(
            _sloos_archive(
                [
                    {
                        "symbol": "SLOOS_BAD.Q",
                        "observations": [
                            ("2026-06-30", "10", "A"),
                            ("2026-06-30", "11", "A"),
                        ],
                    }
                ]
            ),
            params,
        )
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(
            _sloos_archive(
                [{"symbol": "SLOOS_BAD.Q", "observations": [("2026-06-30", "NaN", "A")]}]
            ),
            params,
        )
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(
            _sloos_archive(
                [{"symbol": "SLOOS_BAD.Q", "observations": [("2026-06-30", "10", "ZZ")]}]
            ),
            params,
        )


def test_sloos_date_filter_empty_result_and_page_limit_are_explicit() -> None:
    fetcher = SeniorLoanOfficerSurveyFetcher()
    archive = _sloos_archive(
        [
            {
                "symbol": "SLOOS_AUTO.Q",
                "observations": [("2026-03-31", "10", "A"), ("2026-06-30", "11", "A")],
            }
        ]
    )
    empty = fetcher.transform_data(
        archive,
        fetcher.transform_query(category="auto", start_date="2027-01-01"),
    )
    assert empty == ()
    with pytest.raises(FredQueryLimitError, match="FRED_QUERY_LIMIT"):
        fetcher.transform_data(
            archive,
            fetcher.transform_query(category="auto", page_size=1, max_pages=1),
        )


def test_provider_model_query_injects_identity_and_serializes_sloos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.services import provider_model_query

    fetcher = SeniorLoanOfficerSurveyFetcher()
    archive = _sloos_archive([{"symbol": "SLOOS_AUTO.Q"}])

    class FakeClient:
        def get(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(status_code=200, content=archive)

    monkeypatch.setattr(
        "opendata.data.providers.fred.models.iter2_macro.get_shared_http_client",
        FakeClient,
    )
    observed: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object):
        params = original_transform_query(**kwargs)
        observed.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model="SeniorLoanOfficerSurvey",
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
        lambda _domain: (None, FredSeniorLoanOfficerSurvey),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model="SeniorLoanOfficerSurvey",
            query={"category": "auto", "source": "auto"},
            principal=Principal(),
            ctx=FetchContext(timeout=2.0, request_budget=_context(fetcher).request_budget),
        )
    )
    assert observed == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["model"] == "SeniorLoanOfficerSurvey"
    assert response["results"] == [
        {
            "date": "2026-06-30",
            "symbol": "SLOOS_AUTO.Q",
            "value": 10.0,
            "title": "Net percentage of domestic banks reporting stronger demand for auto loans",
        }
    ]
