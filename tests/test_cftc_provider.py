"""Offline CFTC COT/COTSearch source fixtures and service-path checks."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.cftc import COT, COTSearch
from opendata.data.protocol import FetchContext
from opendata.data.providers.cftc import provider as cftc_provider
from opendata.data.providers.cftc.models import _client, cot_search
from opendata.data.providers.cftc.models.cot import COTFetcher
from opendata.data.providers.cftc.models.cot_search import COTSearchFetcher
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}


class _Transport:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        source: str,
        **kwargs: object,
    ) -> _Response:
        self.requests.append({"url": url, "params": dict(params or {}), "source": source})
        if not self.responses:
            raise AssertionError("unexpected extra CFTC request")
        return self.responses.pop(0)


class _Principal:
    def allows_domain(self, domain: str) -> bool:
        return domain.startswith("cftc_")


def _context(model: str) -> FetchContext:
    grant = RequestGrant(
        source="cftc",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=10,
        source_attempts=10,
        allowed_hosts=frozenset({"publicreporting.cftc.gov"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=10,
        source_attempts=10,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=2.0, request_budget=budget)


def _install_transport(monkeypatch: pytest.MonkeyPatch, *payloads: object) -> _Transport:
    transport = _Transport(*(_Response(payload) for payload in payloads))
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)
    return transport


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


def _cftc_row(code: str = "088691", *, value: str = "500000") -> dict[str, str]:
    return {
        "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000",
        "yyyy_report_week_ww": "2026-40",
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "cftc_contract_market_code": code,
        "cftc_market_code": "088",
        "cftc_region_code": "1",
        "cftc_commodity_code": "69",
        "commodity_name": "GOLD",
        "commodity_group_name": "NATURAL RESOURCES",
        "commodity_subgroup_name": "PRECIOUS METALS",
        "contract_market_name": "GOLD",
        "contract_units": "100 troy ounces",
        "open_interest_all": value,
        "noncomm_positions_long_all": "100000",
        "noncomm_positions_short_all": "80000",
        "noncomm_postions_spread_all": "12000",
        "comm_positions_long_all": "200000",
        "comm_positions_short_all": "220000",
        "change_in_oi_all": "2500",
        "change_in_noncomm_long_all": "-50",
        "pct_of_oi": "100.0",
    }


def _install_service_path(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: object,
    model: str,
    contract: type,
) -> None:
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )


@pytest.mark.parametrize(
    ("model", "fetcher_type", "contract", "query", "response_pages"),
    [
        (
            "COT",
            COTFetcher,
            COT,
            {"code": "CFTC_088691"},
            ([_cftc_row()], [_cftc_row()]),
        ),
        (
            "COTSearch",
            COTSearchFetcher,
            COTSearch,
            {"query": "gold", "category": "natural_resources"},
            ([_cftc_row()], [_cftc_row()]),
        ),
    ],
)
def test_provider_model_query_injects_identity_and_serializes_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    contract: type,
    query: dict[str, object],
    response_pages: tuple[list[dict[str, str]], ...],
) -> None:
    fetcher = fetcher_type()
    transport = _install_transport(monkeypatch, *response_pages)
    _install_service_path(monkeypatch, fetcher, model, contract)
    received_query: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_transform(**kwargs: object) -> object:
        received_query.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_transform)
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="cftc",
            model=model,
            query=query,
            principal=_Principal(),
            ctx=_context(model),
        )
    )

    assert received_query["source"] == "cftc"
    assert received_query["market"] == "us"
    assert response["source"] == "cftc"
    assert response["model"] == model
    assert len(response["results"]) == 1  # type: ignore[arg-type]
    assert all(request["source"] == "cftc" for request in transport.requests)
    if model == "COT":
        assert response["results"][0]["date"] == "2026-10-06"  # type: ignore[index]
        assert response["results"][0]["open_interest_all"] == 500000  # type: ignore[index]
        assert response["results"][0]["change_in_open_interest_all"] == 2500  # type: ignore[index]
        assert response["results"][0]["report_type"] == "legacy"  # type: ignore[index]
        assert response["results"][0]["futonly_or_combined"] == "combined"  # type: ignore[index]
        assert transport.requests[0]["url"].endswith("jun7-fc8e.json")
        assert transport.requests[0]["params"]["$where"] == "cftc_contract_market_code = '088691'"
    else:
        assert response["results"][0]["code"] == "088691"  # type: ignore[index]
        assert "date" not in response["results"][0]  # type: ignore[operator]
        assert transport.requests[0]["params"]["$where"].startswith(
            "upper(commodity_group_name) = 'NATURAL RESOURCES'"
        )


def test_cot_report_selectors_are_typed_identity_dimensions() -> None:
    fetcher = COTFetcher()
    selector_cases = (
        ({}, "legacy", "combined"),
        ({"report_type": "disaggregated"}, "disaggregated", "combined"),
        ({"futures_only": True}, "legacy", "futures_only"),
    )
    identities = []
    for selectors, expected_report, expected_product in selector_cases:
        query = fetcher.transform_query(
            code="088691", measure="positions", source="cftc", market="us", **selectors
        )
        row = fetcher.transform_data([_cftc_row()], query)[0]
        identity = (
            row.date,
            row.cftc_contract_market_code,
            row.report_type,
            row.futonly_or_combined,
        )
        identities.append(identity)
        assert row.report_type == expected_report
        assert row.futonly_or_combined == expected_product
    assert len(set(identities)) == len(selector_cases)


def test_cot_positions_projection_and_null_values_keep_source_units() -> None:
    row = _cftc_row()
    row["noncomm_positions_short_all"] = "null"
    fetcher = COTFetcher()
    query = fetcher.transform_query(
        code="088691",
        measure="positions",
        source="cftc",
        market="us",
    )
    rows = fetcher.transform_data([row], query)
    assert len(rows) == 1
    assert rows[0].non_commercial_positions_long_all == 100000
    assert rows[0].non_commercial_positions_short_all is None
    assert rows[0].contract_units == "100 troy ounces"


def test_cot_rejects_bad_query_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _install_transport(monkeypatch)
    with pytest.raises(ValidationError):
        COTFetcher().transform_query(code="bad' OR '1'='1")
    with pytest.raises(ValidationError):
        COTSearchFetcher().transform_query(code=123)
    assert transport.requests == []


def test_cot_rejects_corrupted_numeric_and_missing_identity_rows() -> None:
    fetcher = COTFetcher()
    query = fetcher.transform_query(code="088691", market="us", source="cftc")
    with pytest.raises(_client.CFTCProviderError, match="CFTC_BAD_NUMBER"):
        fetcher.transform_data([_cftc_row(value="500000.5")], query)
    nullable_public_field = _cftc_row()
    nullable_public_field.pop("market_and_exchange_names")
    assert (
        fetcher.transform_data([nullable_public_field], query)[0].market_and_exchange_names is None
    )

    missing_source_code = _cftc_row()
    missing_source_code.pop("cftc_contract_market_code")
    with pytest.raises(_client.CFTCProviderError, match="CFTC_BAD_ROW"):
        fetcher.transform_data([missing_source_code], query)

    wrong_source_code = _cftc_row("000001")
    with pytest.raises(_client.CFTCProviderError, match="CFTC_BAD_ROW"):
        fetcher.transform_data([wrong_source_code], query)


def test_cot_public_nullable_fields_and_legacy_crop_year_mappings() -> None:
    nullable = COT.model_validate({"date": "2026-10-06", "report_type": "legacy"})
    assert nullable.market_and_exchange_names is None
    assert nullable.cftc_contract_market_code is None
    assert nullable.futonly_or_combined is None
    assert nullable.cftc_contract_market_code_quotes is None
    assert nullable.cftc_subgroup_code is None

    raw = _cftc_row()
    raw.update(
        {
            "open_interest_old": "100",
            "open_interest_other": "200",
            "noncomm_positions_long_old": "10",
            "noncomm_postions_spread_old": "3",
            "noncomm_positions_short_other": "11",
            "noncomm_postions_spread_other": "4",
            "change_in_oi_all": "2500",
            "pct_of_oi_old": "14.5",
            "traders_noncomm_long_old": "8",
            "conc_gross_le_4_tdr_long_old": "31.25",
            "cftc_contract_market_code_quotes": "088691",
            "cftc_subgroup_code": "PRECIOUS_METALS",
        }
    )
    query = COTFetcher().transform_query(
        code="088691", source="cftc", market="us", report_type="legacy"
    )
    row = COTFetcher().transform_data([raw], query)[0]
    assert row.open_interest_old == 100
    assert row.open_interest_other == 200
    assert row.non_commercial_positions_long_old == 10
    assert row.non_commercial_positions_spread == 3
    assert row.non_commercial_positions_short_other == 11
    assert row.non_commercial_positions_spread_1 == 4
    assert row.change_in_open_interest_all == 2500
    assert row.open_interest_pct_old == 14.5
    assert row.traders_non_commercial_long_old == 8
    assert row.concentration_gross_top_4_traders_long_1 == 31.25
    assert row.cftc_contract_market_code_quotes == "088691"
    assert row.cftc_subgroup_code == "PRECIOUS_METALS"


def test_cot_supplemental_non_cit_and_cit_source_fields() -> None:
    raw = _cftc_row()
    raw.update(
        {
            "ncomm_positions_long_all_nocit": "30",
            "ncomm_postions_spread_all_nocit": "12",
            "comm_positions_short_all_nocit": "28",
            "cit_positions_long_all": "9",
            "change_open_interest_all": "5",
            "change_noncomm_spead_all_nocit": "-2",
            "pct_oi_noncomm_long_all_nocit": "25.0",
            "traders_cit_long_all": "4",
        }
    )
    query = COTFetcher().transform_query(
        code="088691", source="cftc", market="us", report_type="supplemental"
    )
    row = COTFetcher().transform_data([raw], query)[0]
    assert row.non_commercial_positions_long_all_non_cit == 30
    assert row.non_commercial_positions_spread_all_non_cit == 12
    assert row.commercial_positions_short_all_non_cit == 28
    assert row.cit_positions_long_all == 9
    assert row.change_open_interest_all == 5
    assert row.change_non_commercial_spread_all_non_cit == -2
    assert row.open_interest_pct_non_commercial_long_all_non_cit == 25.0
    assert row.traders_cit_long_all == 4


def test_cot_search_pages_current_directory_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = COTSearchFetcher()
    first_page = [_cftc_row("000001"), _cftc_row("000002")]
    latest_rows = [_cftc_row("000001"), _cftc_row("000002")]
    last_page = [_cftc_row("000003")]
    transport = _install_transport(monkeypatch, first_page, latest_rows, last_page)
    monkeypatch.setattr(cot_search, "PAGE_SIZE", 2)
    rows = fetcher.fetch(
        ctx=_context("COTSearch"),
        source="cftc",
        market="us",
        query="gold",
    )
    assert len(rows) == 3
    assert [row.code for row in rows] == ["000001", "000002", "000003"]
    assert transport.requests[1]["params"]["$offset"] == "0"
    assert transport.requests[2]["params"]["$offset"] == "2"


def test_cftc_rate_limit_is_sanitized_and_provider_is_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(_Response([], status_code=429))
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)
    with pytest.raises(_client.CFTCProviderError, match="CFTC_HTTP_ERROR status=429"):
        COTSearchFetcher().fetch(ctx=_context("COTSearch"), source="cftc", market="us")
    assert cftc_provider.PROVIDER.health_check().status == "ready"
