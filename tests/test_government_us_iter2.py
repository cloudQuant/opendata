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

from opendata.data.models.government_us import (
    CommodityPsdData,
    TreasuryAuction,
    WeatherBulletin,
    WeatherBulletinDownload,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.government_us.models import _client
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
