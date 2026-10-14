"""Offline contract and query tests for the IMF SDMX macro identities."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.models.macro_iter2 import (
    ImfAvailableIndicator,
    ImfConsumerPriceIndex,
    ImfDirectionOfTrade,
    ImfEconomicIndicator,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.imf.models import _sdmx
from opendata.data.providers.imf.models._sdmx import ImfSdmxError
from opendata.data.providers.imf.models.iter2 import (
    ImfAvailableIndicatorsFetcher,
    ImfConsumerPriceIndexFetcher,
    ImfDirectionOfTradeFetcher,
    ImfEconomicIndicatorFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def _context(model: str) -> FetchContext:
    grant = RequestGrant(
        source="imf",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=frozenset({"api.imf.org"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=2.0,
        request_budget=RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,)),
    )


def _code_list(list_id: str, *codes: tuple[str, str]) -> dict[str, object]:
    return {
        "id": list_id,
        "codes": [{"id": code, "names": {"en": label}} for code, label in codes],
    }


def _structure(
    flow: str,
    agency: str,
    version: str,
    structure_id: str,
    dimension_ids: tuple[str, ...],
    codelists: list[dict[str, object]],
) -> str:
    dimensions = [
        {
            "id": dimension_id,
            "position": position,
            "conceptIdentity": (
                f"urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=IMF:CS_{flow}(1.0)."
                f"{dimension_id}"
            ),
        }
        for position, dimension_id in enumerate(dimension_ids)
    ]
    for dimension in dimensions:
        if dimension["id"] == "FREQUENCY":
            dimension["conceptIdentity"] = (
                "urn:sdmx:org.sdmx.infomodel.conceptscheme.Concept=IMF:CS_MASTER_SYSTEM(1.0).FREQ"
            )
    data = {
        "dataflows": [{"id": flow, "agencyID": agency, "version": version, "name": flow}],
        "dataStructures": [
            {
                "id": structure_id,
                "dataStructureComponents": {"dimensionList": {"dimensions": dimensions}},
            }
        ],
        "codelists": codelists,
    }
    return json.dumps({"data": data})


def _cpi_metadata() -> str:
    return _structure(
        "CPI",
        "IMF.STA",
        "5.0.0",
        "DSD_CPI",
        ("COUNTRY", "INDEX_TYPE", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY"),
        [
            _code_list("CL_COUNTRY", ("USA", "United States")),
            _code_list("CL_INDEX_TYPE", ("CPI", "Consumer price index (CPI)")),
            _code_list(
                "CL_COICOP_1999", ("_T", "All Items"), ("CP01", "Food and non-alcoholic beverages")
            ),
            _code_list(
                "CL_CPI_TYPE_OF_TRANSFORMATION", ("YOY_PCH_PA_PT", "Year-over-year percent change")
            ),
            _code_list("CL_FREQ", ("M", "Monthly")),
        ],
    )


def _imts_metadata() -> str:
    return _structure(
        "IMTS",
        "IMF.STA",
        "1.0.0",
        "DSD_IMTS",
        ("COUNTRY", "INDICATOR", "COUNTERPART_COUNTRY", "FREQUENCY"),
        [
            _code_list("CL_IMTS_COUNTRY", ("USA", "United States"), ("CHN", "China")),
            _code_list(
                "CL_IMTS_INDICATOR",
                ("XG_FOB_USD", "Exports of goods, Free on board (FOB), US dollar"),
            ),
            _code_list("CL_FREQ", ("M", "Monthly")),
        ],
    )


def _weo_catalog() -> str:
    return json.dumps(
        {
            "data": {
                "dataflows": [
                    {
                        "id": "WEO",
                        "agencyID": "IMF.RES",
                        "version": "9.0.0",
                        "name": "World Economic Outlook (WEO)",
                    }
                ]
            }
        }
    )


def _all_catalog() -> str:
    """Return a small manual fixture with two indicator and one other flow."""
    return json.dumps(
        {
            "data": {
                "dataflows": [
                    {
                        "id": "WEO",
                        "agencyID": "IMF.RES",
                        "version": "9.0.0",
                        "name": "World Economic Outlook (WEO)",
                    },
                    {
                        "id": "CPI",
                        "agencyID": "IMF.STA",
                        "version": "5.0.0",
                        "name": "Consumer Price Index",
                    },
                    {
                        "id": "OTHER",
                        "agencyID": "IMF.STA",
                        "version": "1.0.0",
                        "name": "Other statistical table",
                    },
                ]
            }
        }
    )


def _weo_metadata() -> str:
    return _structure(
        "WEO",
        "IMF.RES",
        "9.0.0",
        "DSD_WEO",
        ("COUNTRY", "INDICATOR", "FREQUENCY"),
        [
            _code_list("CL_WEO_COUNTRY", ("USA", "United States")),
            _code_list("CL_WEO_INDICATOR", ("NGDP_RPCH", "GDP, constant prices, percent change")),
            _code_list("CL_FREQ", ("A", "Annual")),
        ],
    )


def _csv(*rows: dict[str, str]) -> str:
    columns: list[str] = []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)
    return (
        ",".join(columns)
        + "\n"
        + "\n".join(",".join(row.get(column, "") for column in columns) for row in rows)
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str]]
) -> list[tuple[str, dict[str, str]]]:
    calls: list[tuple[str, dict[str, str]]] = []

    def _get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        del timeout
        calls.append((url, dict(params)))
        if not responses:
            raise AssertionError("unexpected IMF SDMX request")
        return responses.pop(0)

    monkeypatch.setattr(_sdmx, "_get", _get)
    return calls


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


def _service(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: object,
    model: str,
    contract: type,
    query: dict[str, object],
) -> dict[str, object]:
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query, "_reviewed_query_contract", lambda _domain: (None, contract)
    )
    return asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="imf",
            model=model,
            query=query,
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_context(model),
        )
    )


def test_cpi_typed_transform_window_and_service_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _csv(
        {
            "COUNTRY": "USA",
            "INDEX_TYPE": "CPI",
            "COICOP_1999": "_T",
            "TYPE_OF_TRANSFORMATION": "YOY_PCH_PA_PT",
            "FREQUENCY": "M",
            "TIME_PERIOD": "2024-03",
            "OBS_VALUE": "3.2",
            "UNIT": "Percent",
            "UNIT_MULT": "0",
        }
    )
    data = (200, observation)
    responses = [(200, _cpi_metadata()), data]
    calls = _install(monkeypatch, responses)
    fetcher = ImfConsumerPriceIndexFetcher()
    query = {"country": "United States", "start_date": "2024-01-01", "end_date": "2024-12-31"}
    result = fetcher.fetch(ctx=_context("ConsumerPriceIndex"), **query)
    assert len(result) == 1 and isinstance(result[0], ImfConsumerPriceIndex)
    assert result[0].country == "United States"
    assert result[0].value == 3.2
    assert result[0].unit_multiplier == 1
    assert calls[1][1]["startPeriod"] == "2024-01-01"
    assert calls[1][1]["endPeriod"] == "2024-12-31"
    assert calls[1][0].endswith("/USA.CPI._T.YOY_PCH_PA_PT.M")

    _install(monkeypatch, [(200, _cpi_metadata()), data])
    response = _service(monkeypatch, fetcher, "ConsumerPriceIndex", ImfConsumerPriceIndex, query)
    assert response["source"] == "imf" and response["model"] == "ConsumerPriceIndex"
    assert response["results"][0]["country_code"] == "USA"
    assert (
        response["results"][0]["title"]
        == "Consumer price index (CPI), All Items, Year-over-year percent change"
    )


def test_direction_of_trade_uses_imts_current_successor_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = (
        200,
        _csv(
            {
                "COUNTRY": "USA",
                "INDICATOR": "XG_FOB_USD",
                "COUNTERPART_COUNTRY": "CHN",
                "FREQUENCY": "M",
                "TIME_PERIOD": "2024-02",
                "OBS_VALUE": "1234.5",
                "UNIT": "USD",
                "UNIT_MULT": "3",
                "SCALE": "3",
            }
        ),
    )
    calls = _install(monkeypatch, [(200, _imts_metadata()), data])
    fetcher = ImfDirectionOfTradeFetcher()
    query = {"country": "United States", "counterpart": "China", "direction": "exports"}
    result = fetcher.fetch(ctx=_context("DirectionOfTrade"), **query)
    assert len(result) == 1 and isinstance(result[0], ImfDirectionOfTrade)
    row = result[0]
    assert row.country == "United States" and row.counterpart == "China"
    assert row.title == "Exports of goods, Free on board (FOB), US dollar"
    assert row.unit_multiplier == 1000
    assert calls[1][0].endswith("/USA.XG_FOB_USD.CHN.M")

    _install(monkeypatch, [(200, _imts_metadata()), data])
    response = _service(monkeypatch, fetcher, "DirectionOfTrade", ImfDirectionOfTrade, query)
    assert response["results"][0]["symbol"] == "XG_FOB_USD.USA.CHN.M"


def test_economic_indicator_resolves_official_flow_and_typed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _csv(
        {
            "COUNTRY": "USA",
            "INDICATOR": "NGDP_RPCH",
            "FREQUENCY": "A",
            "TIME_PERIOD": "2024",
            "OBS_VALUE": "2.75",
            "UNIT": "PT",
            "SCALE": "0",
            "ORDER": "1",
            "LEVEL": "0",
        }
    )
    responses = [(200, _weo_catalog()), (200, _weo_metadata()), (200, observation)]
    calls = _install(monkeypatch, responses)
    fetcher = ImfEconomicIndicatorFetcher()
    query = {"symbol": "WEO::NGDP_RPCH", "country": "United States", "frequency": "annual"}
    result = fetcher.fetch(ctx=_context("EconomicIndicators"), **query)
    assert len(result) == 1 and isinstance(result[0], ImfEconomicIndicator)
    assert result[0].symbol_root == "WEO::NGDP_RPCH"
    assert result[0].country == "United States"
    assert result[0].country_code == "USA"
    assert result[0].value == 2.75
    assert len(calls) == 3
    assert calls[2][0].endswith("/USA.NGDP_RPCH.A")

    _install(monkeypatch, [(200, _weo_catalog()), (200, _weo_metadata()), (200, observation)])
    response = _service(monkeypatch, fetcher, "EconomicIndicators", ImfEconomicIndicator, query)
    assert response["results"][0]["unit"] == "PT"
    assert response["results"][0]["title"] == "GDP, constant prices, percent change"


def test_available_indicators_returns_flow_indicator_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [(200, _weo_catalog()), (200, _weo_metadata())]
    calls = _install(monkeypatch, responses)
    fetcher = ImfAvailableIndicatorsFetcher()
    query = {"dataflows": "WEO", "query": "constant prices"}
    result = fetcher.fetch(ctx=_context("AvailableIndicators"), **query)
    assert len(result) == 1 and isinstance(result[0], ImfAvailableIndicator)
    assert result[0].symbol == "WEO::NGDP_RPCH"
    assert result[0].agency_id == "IMF.RES"
    assert result[0].dataflow_name == "World Economic Outlook (WEO)"
    assert result[0].dimension_id == "INDICATOR"
    assert len(calls) == 2

    _install(monkeypatch, [(200, _weo_catalog()), (200, _weo_metadata())])
    response = _service(monkeypatch, fetcher, "AvailableIndicators", ImfAvailableIndicator, query)
    assert response["results"][0]["description"] == "GDP, constant prices, percent change"


def test_available_indicators_defaults_to_full_catalog_and_skips_non_indicator_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_metadata = _structure(
        "OTHER",
        "IMF.STA",
        "1.0.0",
        "DSD_OTHER",
        ("COUNTRY", "FREQUENCY"),
        [
            _code_list("CL_COUNTRY", ("USA", "United States")),
            _code_list("CL_FREQ", ("A", "Annual")),
        ],
    )
    responses = [
        (200, _all_catalog()),
        (200, _weo_metadata()),
        (200, _cpi_metadata()),
        (200, other_metadata),
    ]
    calls = _install(monkeypatch, responses)
    fetcher = ImfAvailableIndicatorsFetcher()
    result = fetcher.fetch(ctx=_context("AvailableIndicators"))
    assert [row.dataflow_id for row in result] == ["CPI", "WEO"]
    assert all(isinstance(row, ImfAvailableIndicator) for row in result)
    assert [row.dimension_id for row in result] == ["INDEX_TYPE", "INDICATOR"]
    assert len(calls) == 4

    _install(
        monkeypatch,
        [
            (200, _all_catalog()),
            (200, _weo_metadata()),
            (200, _cpi_metadata()),
            (200, other_metadata),
        ],
    )
    transformed_kwargs: dict[str, object] = {}
    transform_query = fetcher.transform_query

    def _capture_transform_query(**kwargs: object) -> object:
        transformed_kwargs.update(kwargs)
        return transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", _capture_transform_query)
    response = _service(
        monkeypatch,
        fetcher,
        "AvailableIndicators",
        ImfAvailableIndicator,
        {"max_requests": 4},
    )
    assert response["results"][0]["symbol"] == "CPI::CPI"
    assert response["results"][1]["symbol"] == "WEO::NGDP_RPCH"
    assert transformed_kwargs["source"] == "imf"
    assert transformed_kwargs["market"] == "global"


def test_available_indicators_paginates_catalog_without_silent_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_metadata = _structure(
        "OTHER",
        "IMF.STA",
        "1.0.0",
        "DSD_OTHER",
        ("COUNTRY", "FREQUENCY"),
        [
            _code_list("CL_COUNTRY", ("USA", "United States")),
            _code_list("CL_FREQ", ("A", "Annual")),
        ],
    )
    calls = _install(
        monkeypatch,
        [(200, _all_catalog()), (200, _cpi_metadata()), (200, other_metadata)],
    )
    result = ImfAvailableIndicatorsFetcher().fetch(
        ctx=_context("AvailableIndicators"), flow_offset=1, flow_limit=2, max_requests=3
    )
    assert [row.dataflow_id for row in result] == ["CPI"]
    assert len(calls) == 3


def test_available_indicators_rejects_catalog_over_budget_without_partial_metadata_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, [(200, _all_catalog())])
    with pytest.raises(ImfSdmxError) as exc:
        ImfAvailableIndicatorsFetcher().fetch(ctx=_context("AvailableIndicators"), max_requests=2)
    assert exc.value.code == "IMF_REQUEST_BUDGET_EXCEEDED"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fetcher_type,query",
    (
        (ImfConsumerPriceIndexFetcher, {"transform": "bad"}),
        (ImfConsumerPriceIndexFetcher, {"country": "bad country!"}),
        (ImfConsumerPriceIndexFetcher, {"start_date": "2025-01-01", "end_date": "2024-01-01"}),
        (ImfDirectionOfTradeFetcher, {"direction": "bad"}),
        (ImfDirectionOfTradeFetcher, {}),
        (ImfEconomicIndicatorFetcher, {"symbol": "not-an-imf-symbol"}),
        (ImfEconomicIndicatorFetcher, {"symbol": ["WEO::NGDP_RPCH", "CPI::CPI"]}),
        (
            ImfAvailableIndicatorsFetcher,
            {"dataflows": ["WEO", "CPI", "IMTS"], "max_requests": 3},
        ),
        (ImfAvailableIndicatorsFetcher, {"flow_offset": 0}),
        (ImfAvailableIndicatorsFetcher, {"dataflows": "WEO", "flow_offset": 0, "flow_limit": 1}),
    ),
)
def test_invalid_params_are_rejected_before_source_io(
    monkeypatch: pytest.MonkeyPatch, fetcher_type: type, query: dict[str, object]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(_sdmx, "_get", lambda *args: calls.append(args))
    with pytest.raises((ValidationError, ValueError)):
        fetcher_type().fetch(**query)
    assert calls == []


def test_bad_source_shape_and_rate_limit_preserve_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sdmx, "_get", lambda *_args: (200, "wrong,header\n"))
    with pytest.raises(ImfSdmxError) as bad_shape:
        _sdmx.fetch_observations(
            "IMF.STA", "IMTS", "1.0.0", "USA.XG_FOB_USD.CHN.M", start=None, end=None, timeout=1.0
        )
    assert bad_shape.value.code == "IMF_BAD_RESPONSE"

    monkeypatch.setattr(_sdmx, "_get", lambda *_args: (429, "rate limited"))
    with pytest.raises(ImfSdmxError) as limited:
        _sdmx.fetch_dataflows(timeout=1.0)
    assert limited.value.code == "IMF_HTTP_ERROR" and limited.value.status == 429


def test_bad_cpi_dimension_and_missing_value_are_not_coerced_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_unit = _csv(
        {
            "COUNTRY": "USA",
            "INDEX_TYPE": "CPI",
            "COICOP_1999": "_T",
            "TYPE_OF_TRANSFORMATION": "IX",
            "FREQUENCY": "M",
            "TIME_PERIOD": "2024-03",
            "OBS_VALUE": "1.0",
        }
    )
    _install(monkeypatch, [(200, _cpi_metadata()), (200, wrong_unit)])
    with pytest.raises(ImfSdmxError) as exc:
        ImfConsumerPriceIndexFetcher().fetch(ctx=_context("ConsumerPriceIndex"), country="USA")
    assert exc.value.code == "IMF_BAD_RESPONSE"

    missing = _csv(
        {
            "COUNTRY": "USA",
            "INDEX_TYPE": "CPI",
            "COICOP_1999": "_T",
            "TYPE_OF_TRANSFORMATION": "YOY_PCH_PA_PT",
            "FREQUENCY": "M",
            "TIME_PERIOD": "2024-03",
            "OBS_VALUE": "..",
        }
    )
    _install(monkeypatch, [(200, _cpi_metadata()), (200, missing)])
    assert (
        ImfConsumerPriceIndexFetcher().fetch(ctx=_context("ConsumerPriceIndex"), country="USA")
        == ()
    )
