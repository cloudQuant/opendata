"""Offline public-contract tests for the FRED rates-extra slice."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.models.fred_rates_extra import (
    FredBondIndices,
    FredCommercialPaper,
    FredCommoditySpotPrices,
    FredEuroShortTermRate,
    FredHighQualityMarketCorporateBond,
    FredSpotRate,
    FredYieldCurve,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.iter2_rates_extra import (
    BondIndicesFetcher,
    CommercialPaperFetcher,
    CommoditySpotPricesFetcher,
    EuroShortTermRateFetcher,
    HighQualityMarketCorporateBondFetcher,
    SpotRateFetcher,
    YieldCurveFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    reserve_scoped_attempt,
)
from opendata.services import provider_model_query

_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_RELEASE_URL = f"https://{_HOST}/fred/v2/release/observations"
_DATE = "2026-10-01"
_REALTIME = "2026-10-02"
_REALTIME_END = "9999-12-31"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")


class _Reply:
    def __init__(self, body: object, *, status: object = 200) -> None:
        self.content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.status_code = status


def _observation(value: str, day: str = _DATE) -> dict[str, str]:
    return {
        "date": day,
        "value": value,
        "realtime_start": _REALTIME,
        "realtime_end": _REALTIME_END,
    }


class _SyntheticGovHTTP:
    """Offline Governed-HTTP seam that still charges request-budget grants."""

    def __init__(
        self,
        fixtures: dict[str, list[dict[str, str]]],
        *,
        cancel_after: threading.Event | None = None,
        wrong_header: str | None = None,
        rate_limited: bool = False,
        release_pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self.fixtures = fixtures
        self.cancel_after = cancel_after
        self.wrong_header = wrong_header
        self.rate_limited = rate_limited
        self.release_pages = release_pages
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Any,
        timeout: float | None,
        source: str,
        headers: Any = None,
    ) -> _Reply:
        del timeout
        reserve_scoped_attempt(current_request_scopes(), source, _HOST)
        request = {"url": url, "params": dict(params), "source": source, "headers": headers}
        self.calls.append(request)
        if self.rate_limited:
            raise HttpFetchError(
                FailureCategory.RATE_LIMITED,
                url=url,
                host=_HOST,
                attempts=1,
                status=429,
                retryable=True,
            )
        if url == _RELEASE_URL:
            if self.release_pages is None:
                raise AssertionError("no offline release fixture configured")
            cursor = params.get("next_cursor")
            if cursor is None:
                page_index = 0
            else:
                cursor_to_page = {
                    page["next_cursor"]: index + 1
                    for index, page in enumerate(self.release_pages[:-1])
                }
                if cursor not in cursor_to_page:
                    raise AssertionError(f"unexpected release cursor {cursor}")
                page_index = cursor_to_page[cursor]
            return _Reply(self.release_pages[page_index])
        series_id = params["series_id"]
        if series_id not in self.fixtures:
            raise AssertionError(f"no offline fixture for {series_id}")
        rows = self.fixtures[series_id]
        offset = int(params["offset"])
        limit = int(params["limit"])
        page: dict[str, Any] = {
            "count": len(rows),
            "offset": offset,
            "limit": limit,
            "units": params["units"],
            "output_type": int(params["output_type"]),
            "file_type": "json",
            "order_by": "observation_date",
            "sort_order": params["sort_order"],
            "realtime_start": params.get("realtime_start", "1776-07-04"),
            "realtime_end": params.get("realtime_end", _REALTIME_END),
            "observation_start": params.get("observation_start", "1776-07-04"),
            "observation_end": params.get("observation_end", "9999-12-31"),
            "observations": rows[offset : offset + limit],
        }
        if self.wrong_header == "units":
            page["units"] = "pch"
        elif self.wrong_header == "output_type":
            page["output_type"] = 4 if page["output_type"] != 4 else 1
        elif self.wrong_header == "missing_row_key" and page["observations"]:
            page["observations"][0] = {
                key: value
                for key, value in page["observations"][0].items()
                if key != "realtime_end"
            }
        if self.cancel_after is not None:
            self.cancel_after.set()
        return _Reply(page)


def _hqm_release_pages(
    *,
    day: str = _DATE,
    missing_spot_maturity: str | None = None,
    split_at: int | None = None,
) -> list[dict[str, Any]]:
    spot_series = []
    for index in range(1, 201):
        tenor = Decimal(index) / Decimal(2)
        key = format(tenor.normalize(), "f")
        if "." in key:
            key = key.rstrip("0").rstrip(".")
        label = f"{key}-Year"
        title_tenor = "6 -Month" if index == 1 else label
        spot_series.append(
            {
                "series_id": f"HQMTEST{index:03d}",
                "title": f"{title_tenor} High Quality Market (HQM) Corporate Bond Spot Rate",
                "frequency": "Monthly",
                "units": "Percent",
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "last_updated": "2026-10-06T19:34:00Z",
                "copyright_id": "public domain: citation requested",
                "notes": "",
                "observations": [
                    {
                        "date": day,
                        "value": "." if label == missing_spot_maturity else f"{index / 100:.2f}",
                    }
                ],
            }
        )
    par_series = []
    for index, years in enumerate((2, 5, 10, 30), start=1):
        label = f"{years}-Year"
        par_series.append(
            {
                "series_id": f"HQMPAR{index:02d}",
                "title": f"{label} High Quality Market (HQM) Corporate Bond Par Yield",
                "frequency": "Monthly",
                "units": "Percent",
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "last_updated": "2026-10-06T19:34:00Z",
                "copyright_id": "public domain: citation requested",
                "notes": "",
                "observations": [{"date": day, "value": f"{4 + index / 10:.2f}"}],
            }
        )
    all_series = spot_series + par_series
    release = {
        "release_id": 402,
        "name": "Corporate Bond Yield Curve",
        "url": "https://home.treasury.gov/",
        "sources": [
            {"name": "U.S. Department of the Treasury", "url": "https://home.treasury.gov/"}
        ],
    }
    if split_at is None:
        return [{"has_more": "false", "release": release, "series": all_series}]
    last = all_series[split_at - 1]
    cursor = f"{last['series_id']},{day}"
    return [
        {
            "has_more": "true",
            "next_cursor": cursor,
            "release": release,
            "series": all_series[:split_at],
        },
        {"has_more": "false", "release": release, "series": all_series[split_at:]},
    ]


def _context(
    fetcher: Any,
    *,
    decision: GrantDecision = GrantDecision.ALLOWED,
    cancel_event: threading.Event | None = None,
) -> tuple[FetchContext, RequestBudget]:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=decision,
        rights_evidence="synthetic offline FRED query rights fixture",
        task_attempts=256 if decision is GrantDecision.ALLOWED else 0,
        source_attempts=64 if decision is GrantDecision.ALLOWED else 0,
        allowed_hosts={_HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=256 if decision is GrantDecision.ALLOWED else 0,
        source_attempts=64 if decision is GrantDecision.ALLOWED else 0,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    context = FetchContext(
        timeout=5,
        request_budget=budget,
        operation=RequestOperation.QUERY,
        _thread_cancel_event=cancel_event,
    )
    return context, budget


def _install_client(monkeypatch: pytest.MonkeyPatch, client: _SyntheticGovHTTP) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models.iter2_rates_extra.get_shared_http_client",
        lambda: client,
    )


def _matrix() -> list[
    tuple[
        Any,
        type[Any],
        dict[str, object],
        dict[str, list[dict[str, str]]],
        list[dict[str, object]],
        str,
    ]
]:
    estr_values = {
        "ECBESTRVOLWGTTRMDMNRT": "3.65",
        "ECBESTRRT25THPCTVOL": "3.64",
        "ECBESTRRT75THPCTVOL": "3.66",
        "ECBESTRTOTVOL": "40234.5",
        "ECBESTRNUMTRANS": "999",
        "ECBESTRNUMACTBANKS": "25",
        "ECBESTRSHRVOL5LRGACTBNK": "42.7",
    }
    estr_expected = {
        "date": _DATE,
        "rate": 3.65,
        "percentile_25": 3.64,
        "percentile_75": 3.66,
        "volume": 40234.5,
        "transactions": 999,
        "number_of_banks": 25,
        "large_bank_share_of_volume": 42.7,
    }
    cp_expected = {
        "date": _DATE,
        "symbol": "RIFSPPAAAD01NB",
        "maturity": "overnight",
        "rate": 5.03,
        "title": None,
        "asset_type": "asset_backed",
    }
    bond_facts = {
        "BAMLC1A0C13YEY": (
            4.01,
            "1-3 Year",
            "ICE BofA 1-3 Year US Corporate Index Effective Yield",
        ),
        "BAMLC2A0C35YEY": (
            4.25,
            "3-5 Year",
            "ICE BofA 3-5 Year US Corporate Index Effective Yield",
        ),
        "BAMLC3A0C57YEY": (
            4.40,
            "5-7 Year",
            "ICE BofA 5-7 Year US Corporate Index Effective Yield",
        ),
        "BAMLC4A0C710YEY": (
            4.62,
            "7-10 Year",
            "ICE BofA 7-10 Year US Corporate Index Effective Yield",
        ),
        "BAMLC7A0C1015YEY": (
            4.75,
            "10-15 Year",
            "ICE BofA 10-15 Year US Corporate Index Effective Yield",
        ),
        "BAMLC8A0C15PYEY": (
            4.80,
            "15+ Year",
            "ICE BofA 15+ Year US Corporate Index Effective Yield",
        ),
    }
    bond_fixture = {
        series_id: [_observation(str(value))]
        for series_id, (value, _maturity, _title) in bond_facts.items()
    }
    bond_expected = [
        {
            "date": _DATE,
            "symbol": series_id,
            "value": value,
            "maturity": maturity,
            "title": title,
        }
        for series_id, (value, maturity, title) in bond_facts.items()
    ]
    yield_ids = {
        "DGS1MO": "1-Month",
        "DGS2MO": "2-Month",
        "DGS3MO": "3-Month",
        "DGS4MO": "4-Month",
        "DGS6MO": "6-Month",
        "DGS1": "1-Year",
        "DGS2": "2-Year",
        "DGS3": "3-Year",
        "DGS5": "5-Year",
        "DGS7": "7-Year",
        "DGS10": "10-Year",
        "DGS20": "20-Year",
        "DGS30": "30-Year",
    }
    yield_fixture = {
        series_id: [_observation(str(index + 1.0))] for index, series_id in enumerate(yield_ids)
    }
    yield_expected = [
        {
            "date": _DATE,
            "yield_curve_type": "nominal",
            "maturity": maturity,
            "rate": float(index + 1),
        }
        for index, maturity in enumerate(yield_ids.values())
    ]
    cases = [
        (
            EuroShortTermRateFetcher,
            FredEuroShortTermRate,
            {"start_date": _DATE, "as_of": _REALTIME},
            {series_id: [_observation(value)] for series_id, value in estr_values.items()},
            [estr_expected],
            "eu",
        ),
        (
            CommercialPaperFetcher,
            FredCommercialPaper,
            {"start_date": _DATE, "category": "asset_backed", "maturity": "overnight"},
            {"RIFSPPAAAD01NB": [_observation("5.03")]},
            [cp_expected],
            "us",
        ),
        (
            SpotRateFetcher,
            FredSpotRate,
            {"start_date": _DATE, "maturity": ["1", "5"]},
            {},
            [
                {
                    "date": _DATE,
                    "category": "spot_rate",
                    "maturity": "1-Year",
                    "rate": 0.02,
                },
                {
                    "date": _DATE,
                    "category": "spot_rate",
                    "maturity": "5-Year",
                    "rate": 0.1,
                },
            ],
            "us",
        ),
        (
            HighQualityMarketCorporateBondFetcher,
            FredHighQualityMarketCorporateBond,
            {"date": _DATE},
            {},
            [
                {
                    "date": _DATE,
                    "yield_curve": "spot",
                    "rate": index / 100,
                    "maturity": f"{index / 2:g}-Year",
                }
                for index in range(1, 201)
            ],
            "us",
        ),
        (
            BondIndicesFetcher,
            FredBondIndices,
            {"start_date": _DATE},
            bond_fixture,
            bond_expected,
            "us",
        ),
        (
            CommoditySpotPricesFetcher,
            FredCommoditySpotPrices,
            {"start_date": _DATE, "commodity": "wti"},
            {"DCOILWTICO": [_observation("74.25")]},
            [
                {
                    "date": _DATE,
                    "symbol": "wti",
                    "commodity": "wti",
                    "price": 74.25,
                    "unit": "Dollars per Barrel",
                }
            ],
            "us",
        ),
        (
            YieldCurveFetcher,
            FredYieldCurve,
            {"date": _DATE, "yield_curve_type": "nominal"},
            yield_fixture,
            yield_expected,
            "us",
        ),
    ]
    return cases


@pytest.mark.parametrize(
    "fetcher_type,contract,query,fixtures,expected,market",
    _matrix(),
    ids=(
        "estr",
        "commercial-paper",
        "spot-rate",
        "hqm",
        "bond-indices",
        "commodity",
        "yield-curve",
    ),
)
def test_service_injects_identity_and_serializes_exact_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: Any,
    contract: type[Any],
    query: dict[str, object],
    fixtures: dict[str, list[dict[str, str]]],
    expected: list[dict[str, object]],
    market: str,
) -> None:
    fetcher = fetcher_type()
    observed: list[tuple[str, str | None]] = []
    original_transform = fetcher.transform_query

    def observe_query(**kwargs: object) -> Any:
        params = original_transform(**kwargs)
        observed.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    release_pages = (
        _hqm_release_pages()
        if fetcher_type in {SpotRateFetcher, HighQualityMarketCorporateBondFetcher}
        else None
    )
    client = _SyntheticGovHTTP(fixtures, release_pages=release_pages)
    _install_client(monkeypatch, client)
    capability = fetcher.capability
    if fetcher_type is YieldCurveFetcher:
        assert capability.domain == "fred_yield_curve"
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

    context, _budget = _context(fetcher)
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model=fetcher.canonical_model,
            query=query,
            principal=Principal(),
            ctx=context,
        )
    )
    assert observed == [("fred", market)]
    assert response["source"] == "fred"
    assert response["model"] == fetcher.canonical_model
    assert response["results"] == expected
    assert json.loads(json.dumps(response["results"], allow_nan=False)) == expected
    assert client.calls
    expected_url = _RELEASE_URL if release_pages is not None else _URL
    assert all(call["url"] == expected_url and call["source"] == "fred" for call in client.calls)
    if release_pages is not None:
        assert all(
            call["headers"] == {"Authorization": "Bearer offline-test-key"} for call in client.calls
        )
        assert all(call["params"]["release_id"] == "402" for call in client.calls)
        assert all("api_key" not in call["params"] for call in client.calls)
    else:
        assert all(call["params"]["api_key"] == "offline-test-key" for call in client.calls)
        assert all(call["params"]["units"] == "lin" for call in client.calls)
    if "as_of" in query:
        assert all(
            call["params"]["realtime_start"] == call["params"]["realtime_end"] == _REALTIME
            for call in client.calls
        )


def test_rate_query_validation_rejects_bad_selectors_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases: tuple[tuple[Any, dict[str, object]], ...] = (
        (CommercialPaperFetcher(), {"category": "unknown"}),
        (SpotRateFetcher(), {"maturity": "100.5"}),
        (SpotRateFetcher(), {"category": "par_yield", "maturity": "1"}),
        (HighQualityMarketCorporateBondFetcher(), {"maturities": [0.25]}),
        (BondIndicesFetcher(), {"index_type": "oas"}),
        (BondIndicesFetcher(), {"category": "high_yield"}),
        (BondIndicesFetcher(), {"index": "corporate"}),
        (CommoditySpotPricesFetcher(), {"commodity": "copper"}),
        (YieldCurveFetcher(), {"yield_curve_type": "unknown"}),
        (YieldCurveFetcher(), {"yield_curve_type": "corporate_spot", "as_of": _REALTIME}),
        (EuroShortTermRateFetcher(), {"market": "us"}),
    )
    for fetcher, query in cases:
        context, _budget = _context(fetcher)
        client = _SyntheticGovHTTP({})
        _install_client(monkeypatch, client)
        with pytest.raises((ValidationError, ValueError)):
            fetcher.fetch(ctx=context, **query)
        assert client.calls == []


@pytest.mark.parametrize("bad_header", ("units", "output_type", "missing_row_key"))
def test_source_header_field_and_unit_drift_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    bad_header: str,
) -> None:
    fetcher = CommoditySpotPricesFetcher()
    client = _SyntheticGovHTTP(
        {"DCOILWTICO": [_observation("74.25")]},
        wrong_header=bad_header,
    )
    _install_client(monkeypatch, client)
    context, _budget = _context(fetcher)
    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.fetch(ctx=context, commodity="wti")
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "fetcher_type, query, fixture",
    (
        (
            CommercialPaperFetcher,
            {"category": "asset_backed", "maturity": "overnight"},
            {"RIFSPPAAAD01NB": [_observation("5.03")]},
        ),
        (
            BondIndicesFetcher,
            {"start_date": _DATE},
            {"BAMLC1A0C13YEY": [_observation("4.01")]},
        ),
    ),
    ids=("commercial-paper-percent", "bond-index-percent"),
)
def test_long_output_models_reject_transform_metadata_drift(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: Any,
    query: dict[str, object],
    fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = fetcher_type()
    client = _SyntheticGovHTTP(fixture, wrong_header="units")
    _install_client(monkeypatch, client)
    context, _budget = _context(fetcher)

    with pytest.raises(FredProviderError, match="FRED_BAD_RESPONSE"):
        fetcher.fetch(ctx=context, **query)

    assert len(client.calls) == 1


def test_commercial_paper_missing_value_is_omitted_and_empty_source_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CommercialPaperFetcher()
    query = {"category": "asset_backed", "maturity": "overnight"}
    context, _budget = _context(fetcher)

    null_client = _SyntheticGovHTTP({"RIFSPPAAAD01NB": [_observation(".")]})
    _install_client(monkeypatch, null_client)
    null_rows = fetcher.fetch(ctx=context, **query)
    assert null_rows == ()

    empty_client = _SyntheticGovHTTP({"RIFSPPAAAD01NB": []})
    _install_client(monkeypatch, empty_client)
    empty_rows = fetcher.fetch(ctx=context, **query)
    assert empty_rows == ()


def test_commercial_paper_long_rows_preserve_series_category_and_maturity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CommercialPaperFetcher()
    fixtures = {
        "RIFSPPAAAD01NB": [_observation("5.01")],
        "RIFSPPAAAD07NB": [_observation("5.02")],
        "RIFSPPFAAD01NB": [_observation("5.03")],
        "RIFSPPFAAD07NB": [_observation("5.04")],
    }
    client = _SyntheticGovHTTP(fixtures)
    _install_client(monkeypatch, client)
    context, _budget = _context(fetcher)

    rows = fetcher.fetch(
        ctx=context,
        category=["asset_backed", "financial"],
        maturity=["overnight", "7d"],
    )

    assert [row.model_dump(mode="json") for row in rows] == [
        {
            "date": _DATE,
            "symbol": "RIFSPPAAAD01NB",
            "maturity": "overnight",
            "rate": 5.01,
            "title": None,
            "asset_type": "asset_backed",
        },
        {
            "date": _DATE,
            "symbol": "RIFSPPAAAD07NB",
            "maturity": "7d",
            "rate": 5.02,
            "title": None,
            "asset_type": "asset_backed",
        },
        {
            "date": _DATE,
            "symbol": "RIFSPPFAAD01NB",
            "maturity": "overnight",
            "rate": 5.03,
            "title": None,
            "asset_type": "financial",
        },
        {
            "date": _DATE,
            "symbol": "RIFSPPFAAD07NB",
            "maturity": "7d",
            "rate": 5.04,
            "title": None,
            "asset_type": "financial",
        },
    ]
    assert len(client.calls) == 4


def test_commercial_paper_max_records_bounds_long_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CommercialPaperFetcher()
    fixtures = {
        "RIFSPPAAAD01NB": [
            _observation("5.01", "2026-10-01"),
            _observation("5.02", "2026-10-02"),
        ],
        "RIFSPPAAAD07NB": [
            _observation("5.03", "2026-10-01"),
            _observation("5.04", "2026-10-02"),
        ],
    }
    client = _SyntheticGovHTTP(fixtures)
    _install_client(monkeypatch, client)
    context, _budget = _context(fetcher)

    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.fetch(
            ctx=context,
            category="asset_backed",
            maturity=["overnight", "7d"],
            max_records=3,
        )

    assert len(client.calls) == 2


def test_pages_are_bounded_and_offsets_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = CommercialPaperFetcher()
    fixture = {
        "RIFSPPAAAD01NB": [
            _observation("5.01", "2026-10-01"),
            _observation("5.02", "2026-10-02"),
            _observation("5.03", "2026-10-03"),
        ]
    }
    client = _SyntheticGovHTTP(fixture)
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)
    rows = fetcher.fetch(
        ctx=context,
        category="asset_backed",
        maturity="overnight",
        page_size=1,
        max_pages=3,
    )
    assert [row.rate for row in rows] == [5.01, 5.02, 5.03]
    assert [call["params"]["offset"] for call in client.calls] == ["0", "1", "2"]
    assert budget.attempts_used_for("fred") == 3


def test_denied_grant_and_missing_api_key_fail_before_network_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CommoditySpotPricesFetcher()
    denied_context, denied_budget = _context(fetcher, decision=GrantDecision.DENIED)
    denied_client = _SyntheticGovHTTP({})
    _install_client(monkeypatch, denied_client)
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(ctx=denied_context, commodity="wti")
    assert denied_client.calls == []
    assert denied_budget.attempts_used_for("fred") == 0

    context, missing_key_budget = _context(fetcher)
    missing_key_client = _SyntheticGovHTTP({})
    _install_client(monkeypatch, missing_key_client)

    def missing_key() -> str:
        raise FredProviderError("FRED_API_KEY_MISSING")

    monkeypatch.setattr(_client, "require_api_key", missing_key)
    with pytest.raises(FredProviderError, match="FRED_API_KEY_MISSING"):
        fetcher.fetch(ctx=context, commodity="wti")
    assert missing_key_client.calls == []
    assert missing_key_budget.attempts_used_for("fred") == 0


def test_rate_limit_from_governed_http_is_attributed_without_retrying_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CommoditySpotPricesFetcher()
    client = _SyntheticGovHTTP({"DCOILWTICO": [_observation("74.25")]}, rate_limited=True)
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)
    with pytest.raises(FredProviderError, match="FRED_HTTP_ERROR") as error:
        fetcher.fetch(ctx=context, commodity="wti")
    assert error.value.status == 429
    assert len(client.calls) == 1
    assert budget.attempts_used_for("fred") == 1


def test_cancellation_between_composite_series_stops_next_source_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = EuroShortTermRateFetcher()
    cancelled = threading.Event()
    context, budget = _context(fetcher, cancel_event=cancelled)
    fixtures = {
        "ECBESTRVOLWGTTRMDMNRT": [_observation("3.65")],
        "ECBESTRRT25THPCTVOL": [_observation("3.64")],
        "ECBESTRRT75THPCTVOL": [_observation("3.66")],
        "ECBESTRTOTVOL": [_observation("40234.5")],
        "ECBESTRNUMTRANS": [_observation("999")],
        "ECBESTRNUMACTBANKS": [_observation("25")],
        "ECBESTRSHRVOL5LRGACTBNK": [_observation("42.7")],
    }
    client = _SyntheticGovHTTP(fixtures, cancel_after=cancelled)
    _install_client(monkeypatch, client)
    with pytest.raises(RequestExecutionCancelledError):
        fetcher.fetch(ctx=context)
    assert len(client.calls) == 1
    assert budget.attempts_used_for("fred") == 1


def test_hqm_reads_the_complete_release_curve_and_follows_official_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = HighQualityMarketCorporateBondFetcher()
    client = _SyntheticGovHTTP({}, release_pages=_hqm_release_pages(split_at=97))
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)

    rows = fetcher.fetch(ctx=context, date=_DATE)

    assert len(rows) == 200
    assert rows[0].maturity == "0.5-Year"
    assert rows[0].rate == 0.01
    assert rows[-1].maturity == "100-Year"
    assert rows[-1].rate == 2.0
    assert len(client.calls) == 2
    assert client.calls[0]["params"] == {"release_id": "402", "format": "json", "limit": "500000"}
    assert client.calls[1]["params"]["next_cursor"] == f"HQMTEST097,{_DATE}"
    assert all(
        call["headers"] == {"Authorization": "Bearer offline-test-key"} for call in client.calls
    )
    assert budget.attempts_used_for("fred") == 2


def test_spot_rate_reads_requested_categories_from_the_full_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SpotRateFetcher()
    client = _SyntheticGovHTTP({}, release_pages=_hqm_release_pages(split_at=97))
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)

    rows = fetcher.fetch(
        ctx=context,
        start_date=_DATE,
        end_date=_DATE,
        category=["spot_rate", "par_yield"],
        maturity=["2", "5"],
    )

    assert [(row.category, row.maturity, row.rate) for row in rows] == [
        ("par_yield", "2-Year", 4.1),
        ("par_yield", "5-Year", 4.2),
        ("spot_rate", "2-Year", 0.04),
        ("spot_rate", "5-Year", 0.1),
    ]
    assert len(client.calls) == 2
    assert all(call["url"] == _RELEASE_URL for call in client.calls)
    assert budget.attempts_used_for("fred") == 2


def test_spot_rate_missing_values_remain_null_and_empty_release_series_remain_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SpotRateFetcher()
    context, _budget = _context(fetcher)
    query = {"category": "spot_rate", "maturity": "2"}

    missing_pages = _hqm_release_pages()
    missing_series = next(
        item
        for item in missing_pages[0]["series"]
        if item["title"].startswith("2-Year") and item["title"].endswith("Spot Rate")
    )
    missing_series["observations"][0]["value"] = "."
    missing_client = _SyntheticGovHTTP({}, release_pages=missing_pages)
    _install_client(monkeypatch, missing_client)
    missing = fetcher.fetch(ctx=context, **query)
    assert len(missing) == 1
    assert missing[0].rate is None

    empty_pages = _hqm_release_pages()
    empty_series = next(
        item
        for item in empty_pages[0]["series"]
        if item["title"].startswith("2-Year") and item["title"].endswith("Spot Rate")
    )
    empty_series["observations"] = []
    empty_client = _SyntheticGovHTTP({}, release_pages=empty_pages)
    _install_client(monkeypatch, empty_client)
    assert fetcher.fetch(ctx=context, **query) == ()


def test_spot_rate_denied_grant_stops_before_credentials_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = SpotRateFetcher()
    client = _SyntheticGovHTTP({}, release_pages=_hqm_release_pages())
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher, decision=GrantDecision.DENIED)

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(ctx=context, maturity="1")

    assert client.calls == []
    assert budget.attempts_used_for("fred") == 0


def test_yield_curve_uses_injected_today_and_nearest_complete_prior_curve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = YieldCurveFetcher(today_provider=lambda: date_type(2026, 10, 12))
    ids = (
        "DGS1MO",
        "DGS2MO",
        "DGS3MO",
        "DGS4MO",
        "DGS6MO",
        "DGS1",
        "DGS2",
        "DGS3",
        "DGS5",
        "DGS7",
        "DGS10",
        "DGS20",
        "DGS30",
    )
    fixtures = {
        series_id: [
            _observation(f"{index + 1}.0", "2026-10-02"),
            _observation(".", "2026-10-09")
            if index == 0
            else _observation(f"{index + 11}.0", "2026-10-09"),
        ]
        for index, series_id in enumerate(ids)
    }
    client = _SyntheticGovHTTP(fixtures)
    _install_client(monkeypatch, client)
    context, _budget = _context(fetcher)

    # The requested default is the injected current day. The newest day is
    # incomplete, so the adapter chooses one older complete date for every tenor.
    rows = fetcher.fetch(ctx=context)

    assert len(rows) == len(ids)
    assert {row.date.isoformat() for row in rows} == {"2026-10-02"}
    assert [row.rate for row in rows] == [float(index + 1) for index in range(len(ids))]
    assert all(call["params"]["observation_start"] == "2025-09-07" for call in client.calls)
    assert all(call["params"]["observation_end"] == "2026-10-12" for call in client.calls)

    # Weekend and future targets both resolve to the last complete observation
    # not later than the target; no values are combined across dates.
    rows = fetcher.fetch(ctx=context, date=["2026-10-04", "2026-11-01"])
    assert {row.date.isoformat() for row in rows} == {"2026-10-02"}


def test_yield_curve_corporate_spot_uses_the_complete_official_curve_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = YieldCurveFetcher(today_provider=lambda: date_type(2026, 10, 12))
    client = _SyntheticGovHTTP({}, release_pages=_hqm_release_pages(split_at=97))
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)

    rows = fetcher.fetch(ctx=context, date="2026-10-02", yield_curve_type="corporate_spot")

    assert len(rows) == 200
    assert rows[0].maturity == "0.5-Year"
    assert rows[-1].maturity == "100-Year"
    assert {row.date.isoformat() for row in rows} == {_DATE}
    assert len(client.calls) == 2
    assert budget.attempts_used_for("fred") == 2


def test_yield_curve_treasury_minus_fed_funds_uses_official_series_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = YieldCurveFetcher(today_provider=lambda: date_type(2026, 10, 12))
    values = {
        "T3MFF": ("3-Month", "0.31"),
        "T6MFF": ("6-Month", "0.35"),
        "T1YFF": ("1-Year", "0.65"),
        "T5YFF": ("5-Year", "0.72"),
        "T10YFF": ("10-Year", "0.78"),
    }
    client = _SyntheticGovHTTP(
        {series_id: [_observation(value)] for series_id, (_maturity, value) in values.items()}
    )
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher)

    rows = fetcher.fetch(
        ctx=context,
        date=_DATE,
        yield_curve_type="treasury_minus_fed_funds",
    )

    assert [(row.maturity, row.rate) for row in rows] == [
        ("3-Month", 0.31),
        ("6-Month", 0.35),
        ("1-Year", 0.65),
        ("5-Year", 0.72),
        ("10-Year", 0.78),
    ]
    assert {call["params"]["series_id"] for call in client.calls} == set(values)
    assert budget.attempts_used_for("fred") == len(values)


def test_hqm_release_rejects_unit_drift_and_incomplete_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for mutate in (
        lambda page: page["series"][0].update(units="Basis Points"),
        lambda page: page["series"].pop(),
    ):
        pages = _hqm_release_pages()
        mutate(pages[0])
        client = _SyntheticGovHTTP({}, release_pages=pages)
        _install_client(monkeypatch, client)
        fetcher = HighQualityMarketCorporateBondFetcher()
        context, _budget = _context(fetcher)
        with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
            fetcher.fetch(ctx=context, date=_DATE)
        assert len(client.calls) == 1


def test_hqm_release_denied_grant_stops_before_credentials_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = HighQualityMarketCorporateBondFetcher()
    client = _SyntheticGovHTTP({}, release_pages=_hqm_release_pages())
    _install_client(monkeypatch, client)
    context, budget = _context(fetcher, decision=GrantDecision.DENIED)

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(ctx=context, date=_DATE)

    assert client.calls == []
    assert budget.attempts_used_for("fred") == 0


def test_rate_model_contracts_are_strict_and_keep_native_percentage_points() -> None:
    assert (
        FredEuroShortTermRate.model_validate({"date": _DATE, "rate": 3.65, "volume": 40234.5}).rate
        == 3.65
    )
    # A small percent value is still a valid source-native percentage point;
    # the adapter must not silently multiply it by 100.
    assert FredEuroShortTermRate.model_validate({"date": _DATE, "rate": 0.0365}).rate == 0.0365
    with pytest.raises(ValidationError):
        FredSpotRate.model_validate({"date": _DATE, "maturity": "1-Year", "rate": "4.2"})
    with pytest.raises(ValidationError):
        FredYieldCurve.model_validate({"date": _DATE, "maturity": "1-Year", "rate": float("nan")})
    with pytest.raises(ValidationError):
        FredCommercialPaper.model_validate(
            {
                "date": _DATE,
                "symbol": "RIFSPPAAAD01NB",
                "maturity": "overnight",
                "rate": None,
                "title": None,
                "asset_type": "asset_backed",
            }
        )
    with pytest.raises(ValidationError):
        FredBondIndices.model_validate(
            {
                "date": _DATE,
                "symbol": "BAMLC1A0C13YEY",
                "value": "4.01",
                "maturity": "1-3 Year",
                "title": "ICE BofA 1-3 Year US Corporate Index Effective Yield",
            }
        )
