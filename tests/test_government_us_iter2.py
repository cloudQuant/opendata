"""Offline, manually sourced source-shape tests for government_us iteration 2."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.government_us import (
    CommodityPsdData,
    TreasuryAuction,
    WeatherBulletin,
    WeatherBulletinDownload,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.government_us.models import _client, treasury_prices
from opendata.data.providers.government_us.models.commodity_psd_data import (
    CommodityPsdDataFetcher,
)
from opendata.data.providers.government_us.models.commodity_psd_report import (
    CommodityPsdReportFetcher,
)
from opendata.data.providers.government_us.models.treasury_auctions import (
    TreasuryAuctionsFetcher,
)
from opendata.data.providers.government_us.models.weather_bulletin import (
    WeatherBulletinFetcher,
)
from opendata.data.providers.government_us.models.weather_bulletin_download import (
    WeatherBulletinDownloadFetcher,
    WeatherBulletinDownloadQuery,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence/C100/gov-finra/fixtures"
CONTEXT = FetchContext(timeout=2)


class _Principal:
    def allows_domain(self, domain: str) -> bool:
        return domain.startswith("government_us_")


def _fixture(name: str) -> Any:
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def _service_context(
    model: str = "CommodityPsdData",
    allowed_hosts: frozenset[str] = frozenset({"api.fas.usda.gov"}),
) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="government_us",
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


def test_commodity_psd_data_maps_manual_official_api_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture("usda_psd.json")
    monkeypatch.setenv("USDA_FAS_API_KEY", "fixture-key")
    calls: list[tuple[str, dict[str, str] | None, dict[str, str] | None]] = []

    def fake_get_json(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        allowed_hosts: frozenset[str],
    ) -> tuple[object, dict[str, str]]:
        calls.append((url, params, headers))
        assert context is CONTEXT
        assert allowed_hosts == frozenset({"api.fas.usda.gov"})
        if url.endswith("/commodities"):
            return fixture["commodities"], {}
        if url.endswith("/countries"):
            return fixture["countries"], {}
        if url.endswith("/regions"):
            return fixture["regions"], {}
        if url.endswith("/commodityAttributes"):
            return fixture["commodityAttributes"], {}
        assert url.endswith("/commodity/1/country/all/year/2024")
        return fixture["commodity_year"], {}

    monkeypatch.setattr(_client, "get_json", fake_get_json)
    fetcher = CommodityPsdDataFetcher()
    params = fetcher.transform_query(commodity="corn", start_year=2024, end_year=2024)
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert len(calls) == 5
    assert all(headers == {"X-Api-Key": "fixture-key"} for _, _, headers in calls)
    assert result == (
        CommodityPsdData(
            region="South America",
            country="Brazil",
            commodity="Corn",
            attribute="Production",
            marketing_year="2024",
            value=132000,
            unit="1000 MT",
        ),
        CommodityPsdData(
            region="North America",
            country="United States",
            commodity="Corn",
            attribute="Production",
            marketing_year="2024",
            value=377460,
            unit="1000 MT",
        ),
    )
    us_params = fetcher.transform_query(
        commodity="corn", market="us", start_year=2024, end_year=2024
    )
    us_result = fetcher.transform_data(fetcher.extract_data(us_params, CONTEXT), us_params)
    assert us_result == (result[1],)
    with pytest.raises(_client.GovernmentUSProviderError, match="USDA_BAD_COMMODITY_ROW"):
        fetcher.transform_data([{**raw[0], "value": "not-numeric"}], params)


def test_provider_model_service_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture("usda_psd.json")
    monkeypatch.setenv("USDA_FAS_API_KEY", "fixture-key")
    fetcher = CommodityPsdDataFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source=capability.source,
        model="CommodityPsdData",
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

    def fake_get_json(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        allowed_hosts: frozenset[str],
    ) -> tuple[object, dict[str, str]]:
        assert params is None
        assert headers == {"X-Api-Key": "fixture-key"}
        assert context.request_budget is not None
        assert allowed_hosts == frozenset({"api.fas.usda.gov"})
        for endpoint in ("commodities", "countries", "regions", "commodityAttributes"):
            if url.endswith(f"/{endpoint}"):
                return fixture[endpoint], {}
        assert url.endswith("/commodity/1/country/all/year/2024")
        return fixture["commodity_year"], {}

    monkeypatch.setattr(_client, "get_json", fake_get_json)
    monkeypatch.setattr(fetcher, "transform_query", recording_transform_query)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, CommodityPsdData),
    )

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="government_us",
            model="CommodityPsdData",
            query={"commodity": "corn", "start_year": 2024, "end_year": 2024},
            principal=_Principal(),
            ctx=_service_context(),
        )
    )

    assert seen_query["source"] == "government_us"
    assert seen_query["market"] == "global"
    assert response["source"] == "government_us"
    assert response["model"] == "CommodityPsdData"
    assert response["domain"] == capability.domain
    assert response["results"] == [
        CommodityPsdData(
            region="South America",
            country="Brazil",
            commodity="Corn",
            attribute="Production",
            marketing_year="2024",
            value=132000,
            unit="1000 MT",
        ).model_dump(mode="json"),
        CommodityPsdData(
            region="North America",
            country="United States",
            commodity="Corn",
            attribute="Production",
            marketing_year="2024",
            value=377460,
            unit="1000 MT",
        ).model_dump(mode="json"),
    ]


def test_commodity_psd_report_preserves_pdf_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = (EVIDENCE / "usda_report.pdf").read_bytes()
    seen: list[tuple[str, str | None]] = []

    def fake_get_bytes(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        source: str,
        allowed_hosts: frozenset[str],
        maximum_bytes: int,
    ) -> tuple[bytes, str, dict[str, str]]:
        seen.append((url, headers.get("Accept") if headers else None))
        assert params is None and source == "government_us"
        assert context is CONTEXT and maximum_bytes >= len(body)
        return body, "application/pdf", {}

    monkeypatch.setattr(_client, "get_bytes", fake_get_bytes)
    fetcher = CommodityPsdReportFetcher()
    params = fetcher.transform_query(commodity="grain", year=2022, month=5)
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)
    world_params = fetcher.transform_query(commodity="world_production", year=2022, month=5)
    world_raw = fetcher.extract_data(world_params, CONTEXT)
    world_result = fetcher.transform_data(world_raw, world_params)

    assert seen == [
        ("https://apps.fas.usda.gov/PSDOnline/Circulars/2022/05/Grain.pdf", "application/pdf"),
        (
            "https://apps.fas.usda.gov/PSDOnline/Circulars/2022/05/production.pdf",
            "application/pdf",
        ),
    ]
    assert base64.b64decode(result[0].content) == body
    assert base64.b64decode(world_result[0].content) == body
    assert result[0].data_format == {
        "media_type": "application/pdf",
        "encoding": "base64",
        "source_url": seen[0][0],
    }
    with pytest.raises(_client.GovernmentUSProviderError, match="USDA_REPORT_NOT_PDF"):
        monkeypatch.setattr(
            _client,
            "get_bytes",
            lambda *args, **kwargs: (b"not a document", "text/plain", {}),
        )
        fetcher.extract_data(params, CONTEXT)


def test_unverified_usda_report_categories_keep_selectors_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def unexpected_get_bytes(url: str, **_kwargs: object) -> tuple[bytes, str, dict[str, str]]:
        seen.append(url)
        raise AssertionError("unverified report category must fail before HTTP")

    monkeypatch.setattr(_client, "get_bytes", unexpected_get_bytes)
    fetcher = CommodityPsdReportFetcher()
    unsupported = (
        "citrus",
        "coffee",
        "cotton",
        "dairy",
        "fruit",
        "livestock",
        "oilseeds",
        "stone_fruit",
        "sugar",
        "tree_nuts",
    )
    for commodity in unsupported:
        params = fetcher.transform_query(commodity=commodity, year=2022, month=5)
        assert params.commodity == commodity
        with pytest.raises(
            _client.GovernmentUSProviderError,
            match="USDA_REPORT_CATEGORY_UNVERIFIED",
        ):
            fetcher.extract_data(params, CONTEXT)
    assert seen == []


def test_treasury_auctions_maps_manual_fiscaldata_page(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture("treasury_auctions.json")
    calls: list[dict[str, str] | None] = []

    def fake_get_json(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        allowed_hosts: frozenset[str],
    ) -> tuple[object, dict[str, str]]:
        assert url.endswith("/v1/accounting/od/auctions_query")
        assert context is CONTEXT and allowed_hosts == frozenset({"api.fiscaldata.treasury.gov"})
        calls.append(params)
        return fixture, {}

    monkeypatch.setattr(_client, "get_json", fake_get_json)
    fetcher = TreasuryAuctionsFetcher()
    params = fetcher.transform_query(
        start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), cusip="91282cjl6"
    )
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert len(calls) == 1
    assert "cusip:eq:91282CJL6" in calls[0]["filter"]
    assert result == (
        TreasuryAuction(
            cusip="91282CJL6",
            issue_date=date(2025, 2, 18),
            security_type="Note",
            security_term="10-Year",
            maturity_date=date(2035, 2, 15),
            auction_date=date(2025, 2, 12),
            interest_rate=4.5,
            high_yield=4.63,
            bid_to_cover_ratio=2.54,
            reopening="No",
            updated_timestamp=datetime(2025, 2, 12, 13, tzinfo=timezone.utc),
        ),
    )
    malformed = [dict(raw[0])]
    malformed[0].pop("cusip")
    with pytest.raises(_client.GovernmentUSProviderError, match="TREASURY_AUCTION_BAD_ROW"):
        fetcher.transform_data(malformed, params)

    foreign_params = fetcher.transform_query(
        start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), cusip="91282CJL5"
    )
    with pytest.raises(_client.GovernmentUSProviderError, match="TREASURY_AUCTION_CUSIP_MISMATCH"):
        fetcher.transform_data(raw, foreign_params)


def test_treasury_auction_service_serializes_match_and_rejects_foreign_cusip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture("treasury_auctions.json")
    fetcher = TreasuryAuctionsFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source=capability.source,
        model="TreasuryAuctions",
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

    def fake_get_json(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        allowed_hosts: frozenset[str],
    ) -> tuple[object, dict[str, str]]:
        assert url.endswith("/v1/accounting/od/auctions_query")
        assert context.request_budget is not None
        assert allowed_hosts == frozenset({"api.fiscaldata.treasury.gov"})
        assert params is not None
        assert f"cusip:eq:{seen_query['cusip']}" in params["filter"]
        return fixture, {}

    monkeypatch.setattr(_client, "get_json", fake_get_json)
    monkeypatch.setattr(fetcher, "transform_query", recording_transform_query)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, TreasuryAuction),
    )

    def query(cusip: str) -> dict[str, object]:
        return asyncio.run(
            provider_model_query.query_provider_model(
                registry=object(),
                source="government_us",
                model="TreasuryAuctions",
                query={
                    "start_date": date(2025, 2, 1),
                    "end_date": date(2025, 2, 28),
                    "cusip": cusip,
                },
                principal=_Principal(),
                ctx=_service_context(
                    "TreasuryAuctions", frozenset({"api.fiscaldata.treasury.gov"})
                ),
            )
        )

    response = query("91282CJL6")
    assert seen_query["source"] == "government_us"
    assert seen_query["market"] == "us"
    assert response["results"] == [
        TreasuryAuction.model_validate(fixture["data"][0]).model_dump(mode="json")
    ]
    with pytest.raises(provider_model_query.ProviderModelQueryUpstreamError):
        query("91282CJL5")


def test_weather_bulletin_maps_noaa_product_and_rejects_missing_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture("nws_bulletin.json")

    def fake_get_json(
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        allowed_hosts: frozenset[str],
    ) -> tuple[object, dict[str, str]]:
        assert url == "https://api.weather.gov/products/types/TUV/locations/KOUN/latest"
        assert headers is not None and "application/ld+json" in headers["Accept"]
        assert context is CONTEXT and allowed_hosts == frozenset({"api.weather.gov"})
        return fixture, {}

    monkeypatch.setattr(_client, "get_json", fake_get_json)
    fetcher = WeatherBulletinFetcher()
    params = fetcher.transform_query(product_type="tuv", location="koun")
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert result == (
        WeatherBulletin(
            id="TEST-20261012T1200Z",
            wmo_collective_id="ABCD01",
            issuing_office="KOUN",
            issuance_time=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
            product_code="TUV",
            product_name="Weather Bulletin",
            product_text="NOUS43 KOUN 121200\nTEST BULLETIN\n",
        ),
    )
    malformed = dict(raw)
    malformed.pop("productText")
    with pytest.raises(_client.GovernmentUSProviderError, match="NWS_BAD_PRODUCT_ROW"):
        fetcher.transform_data(malformed, params)


def test_weather_bulletin_download_preserves_media_type_and_rejects_foreign_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = (EVIDENCE / "nws_document.txt").read_bytes()
    url = "https://api.weather.gov/products/abc"

    def fake_get_bytes(
        requested_url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        context: FetchContext,
        source: str,
        allowed_hosts: frozenset[str],
        maximum_bytes: int,
    ) -> tuple[bytes, str, dict[str, str]]:
        assert requested_url == url and params is None
        assert source == "government_us" and context is CONTEXT
        assert allowed_hosts == frozenset(
            {"api.weather.gov", "weather.gov", "www.weather.gov", "forecast.weather.gov"}
        )
        assert len(document) <= maximum_bytes
        return document, "text/plain", {}

    monkeypatch.setattr(_client, "get_bytes", fake_get_bytes)
    fetcher = WeatherBulletinDownloadFetcher()
    params = fetcher.transform_query(urls={"url": url})
    raw = fetcher.extract_data(params, CONTEXT)
    result = fetcher.transform_data(raw, params)

    assert result == (
        WeatherBulletinDownload(
            content="Tk9VUzQzIEtPVU4gMTIxMjAwClRFU1QgQlVMTEVUSU4K",
            data_format={"media_type": "text/plain", "encoding": "base64", "source_url": url},
        ),
    )
    with pytest.raises(ValueError, match="plain official HTTPS URL"):
        WeatherBulletinDownloadQuery.model_validate({"urls": "https://example.com/report.txt"})


_TREASURY_URL = "https://treasurydirect.gov/GA-FI/FedInvest/securityPriceDetail"
_TREASURY_HEADER = b"cusip,security_type,rate,maturity_date,call_date,bid,offer,eod_price\n"


@pytest.fixture
def treasury_clock(monkeypatch):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(2024, 6, 29)

    monkeypatch.setattr(treasury_prices, "date", FrozenDate)


def _transcribed_treasury_listing():
    from scripts.quality.iter2_witnesses._parts.government_us.rows_w33_treasury_prices import (
        _document,
    )

    return ("\n".join(_document()["responses"][0]["body"]) + "\n").encode()


class _TreasuryReplaySession(requests.Session):
    def __init__(self, body, content_type, failure):
        super().__init__()
        self.body = body
        self.content_type = content_type
        self.failure = failure
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        assert method == "POST" and url == _TREASURY_URL
        if self.failure is not None:
            raise self.failure
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response._content = self.body
        response.headers["Content-Type"] = self.content_type
        return response


def _treasury_replay(monkeypatch, body, *, content_type="text/csv", failure=None):
    session = _TreasuryReplaySession(body, content_type, failure)
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None, default_headers={}),
        session=session,
        raw_response_cache=None,
        sleep=lambda _: None,
    )
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: client)
    return session


def _treasury_context():
    return _service_context("TreasuryPrices", frozenset({"treasurydirect.gov"}))


@pytest.mark.parametrize("header", [b"", _TREASURY_HEADER])
def test_treasury_transcribed_2024_prices_retain_first_headerless_row(
    monkeypatch, treasury_clock, header
):
    session = _treasury_replay(monkeypatch, header + _transcribed_treasury_listing())
    rows = treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-06-26")
    assert [row.cusip for row in rows] == ["912796Y45", "912797KH9", "912796Y52"]
    assert rows[0].model_dump(mode="json") == {
        "cusip": "912796Y45",
        "security_type": "MARKET BASED BILL",
        "rate": 0.0,
        "maturity_date": "2024-06-27",
        "call_date": None,
        "bid": 0.0,
        "offer": 99.9855,
        "eod_price": 0.0,
        "issuer_name": None,
        "isin": None,
        "issue_date": None,
        "last_traded_date": None,
        "total_trades": None,
        "last_price": None,
        "highest_price": None,
        "lowest_price": None,
        "ytm": None,
        "date": "2024-06-26",
    }
    assert rows[2].bid == 99.869125 and rows[2].offer == 99.86875
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["params"] == {
        "priceDateDay": "26",
        "priceDateMonth": "6",
        "priceDateYear": "2024",
        "fileType": "csv",
        "csv": "CSV FORMAT",
    }
    assert "json" not in call and "data" not in call
    assert call["timeout"] == 2
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"cusip": " 912796y45 "}, ["912796Y45"]),
        ({"security_type": "bill"}, ["912796Y45", "912797KH9", "912796Y52"]),
        ({"security_type": "bill", "cusip": "912796Y52"}, ["912796Y52"]),
    ],
)
def test_treasury_local_cusip_and_security_type_selectors(
    monkeypatch, treasury_clock, query, expected
):
    session = _treasury_replay(monkeypatch, _transcribed_treasury_listing())
    rows = treasury_prices.TreasuryPricesFetcher().fetch(
        ctx=_treasury_context(), date="2024-06-26", **query
    )
    assert [row.cusip for row in rows] == expected
    assert len(session.calls) == 1
    assert "cusip" not in session.calls[0]["params"]
    assert "security_type" not in session.calls[0]["params"]


@pytest.mark.parametrize("query", [{"cusip": "912796Y46"}, {"security_type": "note"}])
def test_treasury_no_matching_security_refuses(monkeypatch, treasury_clock, query):
    session = _treasury_replay(monkeypatch, _transcribed_treasury_listing())
    with pytest.raises(_client.GovernmentUSProviderError) as caught:
        treasury_prices.TreasuryPricesFetcher().fetch(
            ctx=_treasury_context(), date="2024-06-26", **query
        )
    assert caught.value.code == "TREASURY_PRICE_NO_MATCHING_ROW"
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2024, 6, 28), date(2024, 6, 28)),
        (date(2024, 6, 29), date(2024, 6, 28)),
        (date(2024, 6, 30), date(2024, 6, 28)),
        (date(2024, 7, 1), date(2024, 7, 1)),
    ],
)
def test_treasury_business_day_default(today, expected):
    assert treasury_prices.last_business_day(today) == expected


def test_treasury_default_date_uses_frozen_weekday_not_current_quotes(monkeypatch, treasury_clock):
    session = _treasury_replay(monkeypatch, _transcribed_treasury_listing())
    rows = treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), cusip="912796Y45")
    assert rows[0].date == date(2024, 6, 28)
    assert session.calls[0]["params"]["priceDateDay"] == "28"


@pytest.mark.parametrize(
    "query",
    [
        {"date": "2024-06-30"},
        {"symbol": "912796Y45"},
        {"start_date": "2024-06-26"},
        {"end_date": "2024-06-26"},
        {"cusip": "bad"},
        {"cusip": "912796Y4!"},
        {"security_type": "stock"},
    ],
)
def test_treasury_invalid_queries_refuse_before_http(monkeypatch, treasury_clock, query):
    session = _treasury_replay(monkeypatch, b"")
    with pytest.raises(ValidationError):
        treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), **query)
    assert session.calls == []


def test_treasury_old_date_refuses_before_http(monkeypatch, treasury_clock):
    session = _treasury_replay(monkeypatch, b"")
    with pytest.raises(_client.GovernmentUSProviderError) as caught:
        treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-05-29")
    assert caught.value.code == "TREASURY_PRICE_DATE_TOO_OLD"
    assert session.calls == []


@pytest.mark.parametrize(
    "body,content_type,code",
    [
        (b"", "text/csv", "EMPTY_DOCUMENT"),
        (b" \n", "text/csv", "EMPTY_DOCUMENT"),
        (_TREASURY_HEADER, "text/csv", "EMPTY_DOCUMENT"),
        (b"<html>not prices</html>", "text/csv", "HTML_RESPONSE"),
        (_transcribed_treasury_listing(), "text/html; charset=utf-8", "HTML_RESPONSE"),
        (b"912796Y45,BILL,0,06/27/2024,,1,2\n", "text/csv", "BAD_COLUMN_COUNT"),
        (b"912796Y45,BILL,0,06/27/2024,,1,2,3\n" * 5001, "text/csv", "TOO_MANY_ROWS"),
    ],
    ids=lambda value: f"bytes-{len(value)}" if isinstance(value, bytes) else str(value),
)
def test_treasury_document_guards(monkeypatch, treasury_clock, body, content_type, code):
    session = _treasury_replay(monkeypatch, body, content_type=content_type)
    with pytest.raises(_client.GovernmentUSProviderError) as caught:
        treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-06-26")
    assert caught.value.code == "TREASURY_PRICE_" + code
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "column,value,code",
    [
        (2, "not-numeric", "BAD_VALUE"),
        (5, "NaN", "BAD_VALUE"),
        (6, "inf", "BAD_VALUE"),
        (7, "-inf", "BAD_VALUE"),
        (3, "02/30/2024", "BAD_DATE"),
        (4, "not-a-date", "BAD_DATE"),
    ],
)
def test_treasury_bad_price_and_date_cells(monkeypatch, treasury_clock, column, value, code):
    cells = _transcribed_treasury_listing().decode().splitlines()[0].split(",")
    cells[column] = value
    session = _treasury_replay(monkeypatch, (",".join(cells) + "\n").encode())
    with pytest.raises(_client.GovernmentUSProviderError) as caught:
        treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-06-26")
    assert caught.value.code == "TREASURY_PRICE_" + code
    assert len(session.calls) == 1


def test_treasury_blank_numerics_call_date_and_latin1_text(monkeypatch, treasury_clock):
    session = _treasury_replay(
        monkeypatch, b"912796Y45,MARKET BASED NOTE \xe9,,06/27/2024,06/26/2024,,99.5,\n"
    )
    row = treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-06-26")[
        0
    ]
    assert row.security_type == "MARKET BASED NOTE é"
    assert row.rate is None and row.bid is None and row.eod_price is None
    assert row.offer == 99.5
    assert row.call_date == date(2024, 6, 26)
    assert len(session.calls) == 1


def test_treasury_governed_timeout_is_sanitized(monkeypatch, treasury_clock):
    session = _treasury_replay(monkeypatch, b"", failure=requests.Timeout("private detail"))
    with pytest.raises(_client.GovernmentUSProviderError) as caught:
        treasury_prices.TreasuryPricesFetcher().fetch(ctx=_treasury_context(), date="2024-06-26")
    assert caught.value.code == "GOV_US_HTTP_ERROR"
    assert caught.value.status is None
    assert str(caught.value) == "GOV_US_HTTP_ERROR"
    assert len(session.calls) == 1


def test_treasury_missing_grant_never_sends(monkeypatch, treasury_clock):
    session = _treasury_replay(monkeypatch, _transcribed_treasury_listing())
    with pytest.raises(RequestAuthorizationError):
        treasury_prices.TreasuryPricesFetcher().fetch(date="2024-06-26")
    assert session.calls == []
