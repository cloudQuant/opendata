"""Offline, manually sourced FINRA API shape and mapping tests."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.models.finra import EquityShortInterest, OTCAggregate
from opendata.data.protocol import FetchContext
from opendata.data.providers.finra.models import _client
from opendata.data.providers.finra.models.equity_short_interest import (
    EquityShortInterestFetcher,
)
from opendata.data.providers.finra.models.otc_aggregate import OTCAggregateFetcher
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence/C100/gov-finra/fixtures"
CONTEXT = FetchContext(timeout=2)


def _fixture(name: str) -> Any:
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


class _Response:
    def __init__(self, body: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = 200
        self.content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.headers = headers or {"Content-Type": "application/json"}


class _Principal:
    def allows_domain(self, domain: str) -> bool:
        return domain.startswith("finra_")


class _FakeFinraHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.partitions = _fixture("finra_weekly_partitions.json")
        self.weekly = _fixture("finra_weekly_summary.json")
        self.short_interest = _fixture("finra_short_interest.json")
        self.omit_record_total = False

    def _response_headers(self, total: int) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self.omit_record_total:
            headers["Record-Total"] = str(total)
        return headers

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        if url == _client.FIP_URL:
            assert method == "POST"
            assert kwargs.get("params") == {"grant_type": "client_credentials"}
            assert kwargs.get("json_body") is None
            assert "Content-Type" not in headers
            assert not any(name in kwargs for name in ("data", "form", "content"))
            assert headers["Authorization"] == "Basic " + base64.b64encode(
                b"fixture-client:fixture-secret"
            ).decode("ascii")
            return _Response(
                {"access_token": "fixture.token_123", "token_type": "Bearer", "expires_in": "3600"}
            )
        assert headers.get("Authorization") == "Bearer fixture.token_123"
        # Request-capture oracle: FINRA Query API metadata/data use this header.
        # The generic Data-Version spelling must not be accepted by the fixture.
        assert headers.get("Data-API-Version") == "1"
        assert "Data-Version" not in headers
        if url.endswith("/partitions/group/otcmarket/name/weeklysummary"):
            assert method == "GET"
            return _Response(self.partitions)
        if url.endswith("/data/group/otcmarket/name/weeklysummary"):
            assert method == "POST"
            payload = kwargs.get("json_body")
            assert isinstance(payload, dict)
            assert payload["limit"] == _client.MAX_PAGE_SIZE and payload["offset"] == 0
            filters = payload["compareFilters"]
            assert isinstance(filters, list)
            return _Response(self.weekly, self._response_headers(1))
        if url.endswith("/data/group/otcmarket/name/consolidatedshortinterest"):
            assert method == "POST"
            payload = kwargs.get("json_body")
            assert isinstance(payload, dict)
            return _Response(self.short_interest, self._response_headers(2))
        raise AssertionError(f"unexpected FINRA endpoint: {url}")


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> _FakeFinraHttp:
    monkeypatch.setenv("FINRA_API_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("FINRA_API_CLIENT_SECRET", "fixture-secret")
    fake = _FakeFinraHttp()
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: fake)
    return fake


def _service_context(model: str) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    allowed_hosts = _client.FIP_HOSTS | _client.API_HOSTS
    grant = RequestGrant(
        source="finra",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=10,
        source_attempts=10,
        allowed_hosts=allowed_hosts,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=10,
        source_attempts=10,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=2, request_budget=budget)


def test_otc_aggregate_fetches_latest_partition_and_maps_finra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake(monkeypatch)
    fetcher = OTCAggregateFetcher()
    params = fetcher.transform_query(symbol="aapl", tier="T1", is_ats=True)
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert [call[0] for call in fake.calls] == ["POST", "GET", "POST"]
    payload = fake.calls[2][2]["json_body"]
    assert isinstance(payload, dict)
    assert payload["fields"] == [
        "issueSymbolIdentifier",
        "totalWeeklyShareQuantity",
        "totalWeeklyTradeCount",
        "lastUpdateDate",
    ]
    filters = payload["compareFilters"]
    assert isinstance(filters, list)
    assert {(item["fieldName"], item["fieldValue"]) for item in filters} == {
        ("summaryStartDate", "2019-09-16"),
        ("tierIdentifier", "T1"),
        ("summaryTypeCode", "ATS_W_SMBL"),
        ("issueSymbolIdentifier", "AAPL"),
    }
    assert result == (
        OTCAggregate(
            update_date=date(2019, 9, 23),
            symbol="AAPL",
            share_quantity=26209776,
            trade_quantity=123120,
        ),
    )
    malformed = [dict(raw[0])]
    malformed[0].pop("issueSymbolIdentifier")
    with pytest.raises(_client.FinraProviderError, match="FINRA_OTC_AGGREGATE_BAD_ROW"):
        fetcher.transform_data(malformed, params)
    wrong_symbol = [{**raw[0], "issueSymbolIdentifier": "MSFT"}]
    with pytest.raises(_client.FinraProviderError, match="FINRA_QUERY_SYMBOL_MISMATCH"):
        fetcher.transform_data(wrong_symbol, params)


def test_equity_short_interest_maps_manual_finra_rows_and_null_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake(monkeypatch)
    fetcher = EquityShortInterestFetcher()
    params = fetcher.transform_query(symbol="bdrbf")
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert [call[0] for call in fake.calls] == ["POST", "POST"]
    payload = fake.calls[1][2]["json_body"]
    assert isinstance(payload, dict)
    assert payload["compareFilters"] == [
        {"fieldName": "symbolCode", "fieldValue": "BDRBF", "compareType": "EQUAL"}
    ]
    assert result == (
        EquityShortInterest(
            settlement_date=date(2020, 3, 31),
            symbol="BDRBF",
            issue_name="Bombardier Inc. Class B Shares",
            market_class="OTC",
            current_short_position=50584440,
            previous_short_position=62437967,
            avg_daily_volume=0,
            days_to_cover=26.56,
            change=-11853527,
            change_pct=-18.98,
        ),
        EquityShortInterest(
            settlement_date=date(2020, 4, 15),
            symbol="BDRBF",
            issue_name="Bombardier Inc. Class B Shares",
            market_class="OTC",
            current_short_position=52552196,
            previous_short_position=50584440,
            avg_daily_volume=1279692,
            days_to_cover=41.07,
            change=1967756,
            change_pct=3.89,
        ),
    )
    malformed = [dict(raw[0])]
    malformed[0].pop("settlementDate")
    with pytest.raises(_client.FinraProviderError, match="FINRA_SHORT_INTEREST_BAD_ROW"):
        fetcher.transform_data(malformed, params)
    nonnumeric_cover = [{**raw[0], "daysToCoverQuantity": "N/A"}]
    with pytest.raises(_client.FinraProviderError, match="FINRA_SHORT_INTEREST_BAD_ROW"):
        fetcher.transform_data(nonnumeric_cover, params)
    wrong_symbol = [{**raw[0], "symbolCode": "MSFT"}]
    with pytest.raises(_client.FinraProviderError, match="FINRA_QUERY_SYMBOL_MISMATCH"):
        fetcher.transform_data(wrong_symbol, params)


def test_short_page_without_record_total_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake(monkeypatch)
    fake.omit_record_total = True

    with pytest.raises(_client.FinraProviderError, match="FINRA_RECORD_TOTAL_MISSING"):
        _client.post_rows(
            "https://api.finra.org/data/group/otcmarket/name/weeklysummary",
            token="fixture.token_123",
            fields=("issueSymbolIdentifier",),
            compare_filters=[],
            context=CONTEXT,
        )


@pytest.mark.parametrize("model", ["OTCAggregate", "EquityShortInterest"])
def test_finra_service_serializes_matching_symbol_and_rejects_foreign_rows(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    fake = _install_fake(monkeypatch)
    if model == "OTCAggregate":
        fetcher = OTCAggregateFetcher()
        contract = OTCAggregate
        query_args = {"symbol": "aapl"}
        wrong_symbol_field = "issueSymbolIdentifier"
        expected_rows = (
            OTCAggregate(
                update_date=date(2019, 9, 23),
                symbol="AAPL",
                share_quantity=26209776,
                trade_quantity=123120,
            ),
        )
        fake_rows = fake.weekly
    else:
        fetcher = EquityShortInterestFetcher()
        contract = EquityShortInterest
        query_args = {"symbol": "bdrbf"}
        wrong_symbol_field = "symbolCode"
        expected_rows = (
            EquityShortInterest(
                settlement_date=date(2020, 3, 31),
                symbol="BDRBF",
                issue_name="Bombardier Inc. Class B Shares",
                market_class="OTC",
                current_short_position=50584440,
                previous_short_position=62437967,
                avg_daily_volume=0,
                days_to_cover=26.56,
                change=-11853527,
                change_pct=-18.98,
            ),
            EquityShortInterest(
                settlement_date=date(2020, 4, 15),
                symbol="BDRBF",
                issue_name="Bombardier Inc. Class B Shares",
                market_class="OTC",
                current_short_position=52552196,
                previous_short_position=50584440,
                avg_daily_volume=1279692,
                days_to_cover=41.07,
                change=1967756,
                change_pct=3.89,
            ),
        )
        fake_rows = fake.short_interest

    capability = fetcher.capability
    descriptor = SimpleNamespace(
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
    seen_query: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def recording_transform_query(**kwargs: object) -> Any:
        seen_query.update(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", recording_transform_query)
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

    def query() -> dict[str, object]:
        return asyncio.run(
            provider_model_query.query_provider_model(
                registry=object(),
                source="finra",
                model=model,
                query=query_args,
                principal=_Principal(),
                ctx=_service_context(model),
            )
        )

    response = query()
    assert seen_query["source"] == "finra"
    assert seen_query["market"] == "us"
    assert response["source"] == "finra"
    assert response["model"] == model
    assert response["domain"] == capability.domain
    assert response["results"] == [row.model_dump(mode="json") for row in expected_rows]

    if model == "OTCAggregate":
        fake.weekly = [{**row, wrong_symbol_field: "MSFT"} for row in fake_rows]
    else:
        fake.short_interest = [{**row, wrong_symbol_field: "MSFT"} for row in fake_rows]
    with pytest.raises(provider_model_query.ProviderModelQueryUpstreamError):
        query()
