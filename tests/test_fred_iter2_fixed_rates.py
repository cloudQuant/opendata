"""Hand-authored contracts and adversarial checks for fixed FRED rates."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_iter2 import (
    FredAmeribor,
    FredDiscountWindowPrimaryCreditRate,
    FredEuropeanCentralBankInterestRate,
    FredTipsYield,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.fixed_rates import (
    AmeriborFetcher,
    DiscountWindowPrimaryCreditRateFetcher,
    EuropeanCentralBankInterestRatesFetcher,
    TipsYieldsFetcher,
)
from opendata.data.providers.fred.provider import PROVIDER as FRED_PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


@pytest.fixture(autouse=True)
def _fred_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")


@pytest.fixture
def ameribor_fixture() -> dict[str, list[dict[str, str]]]:
    """Independent samples for each of the five active official FRED series."""
    return {
        "AMERIBOR": [
            {
                "date": "2026-10-01",
                "value": "3.67438",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "AMBOR30": [
            {
                "date": "2026-10-01",
                "value": "3.80236",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "AMBOR90": [
            {
                "date": "2026-10-01",
                "value": "3.71958",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "AMBOR30T": [
            {
                "date": "2026-10-01",
                "value": "4.04608",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "AMBOR90T": [
            {
                "date": "2026-10-01",
                "value": "4.02322",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
    }


@pytest.fixture
def ecb_fixture() -> dict[str, list[dict[str, str]]]:
    """Independent daily samples for ECB deposit, lending and refinancing rates."""
    return {
        "ECBDFR": [
            {
                "date": "2026-10-01",
                "value": "2.00",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            },
            {
                "date": "2026-10-02",
                "value": "2.00",
                "realtime_start": "2026-10-03",
                "realtime_end": "9999-12-31",
            },
        ],
        "ECBMLFR": [
            {
                "date": "2026-10-01",
                "value": "2.40",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            },
            {
                "date": "2026-10-02",
                "value": "2.40",
                "realtime_start": "2026-10-03",
                "realtime_end": "9999-12-31",
            },
        ],
        "ECBMRRFR": [
            {
                "date": "2026-10-01",
                "value": "2.15",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            },
            {
                "date": "2026-10-02",
                "value": "2.15",
                "realtime_start": "2026-10-03",
                "realtime_end": "9999-12-31",
            },
        ],
    }


@pytest.fixture
def discount_window_fixture() -> list[dict[str, str]]:
    """Independent daily DPCREDIT samples for frequency-aggregation checks."""
    return [
        {
            "date": "2026-10-01",
            "value": "4.00",
            "realtime_start": "2026-10-02",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2026-10-02",
            "value": "4.00",
            "realtime_start": "2026-10-03",
            "realtime_end": "9999-12-31",
        },
    ]


@pytest.fixture
def tips_fixture() -> dict[str, list[dict[str, str]]]:
    """Independent daily constant-maturity inflation-indexed yield samples."""
    return {
        "DFII5": [
            {
                "date": "2026-10-01",
                "value": "1.52",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "DFII10": [
            {
                "date": "2026-10-01",
                "value": "1.81",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "DFII20": [
            {
                "date": "2026-10-01",
                "value": "2.05",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
        "DFII30": [
            {
                "date": "2026-10-01",
                "value": "2.16",
                "realtime_start": "2026-10-02",
                "realtime_end": "9999-12-31",
            }
        ],
    }


def _context(fetcher: Any) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only synthetic FRED query grant",
        task_attempts=10,
        source_attempts=3,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=5,
        request_budget=RequestBudget(task_attempts=10, source_attempts=3, grants=(grant,)),
        operation=RequestOperation.QUERY,
    )


def _page(observations: list[dict[str, str]], *, offset: int, limit: int) -> str:
    return json.dumps(
        {
            "count": len(observations),
            "offset": offset,
            "limit": limit,
            "observations": observations[offset : offset + limit],
        }
    )


def _install_series_response(
    monkeypatch: pytest.MonkeyPatch,
    observations_by_id: dict[str, list[dict[str, str]]],
    requests: list[tuple[str, dict[str, str]]],
) -> None:
    def http_get(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        observations = observations_by_id[params["series_id"]]
        return 200, _page(
            observations,
            offset=int(params["offset"]),
            limit=int(params["limit"]),
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)


def test_ameribor_maps_five_series_pages_and_output_shape(
    monkeypatch: pytest.MonkeyPatch,
    ameribor_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = AmeriborFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_series_response(monkeypatch, ameribor_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        maturity="all",
        start_date="2026-10-01",
        end_date="2026-10-01",
        vintage_dates=["2026-10-02"],
        frequency="m",
        aggregation_method="eop",
        transform="lin",
    )

    assert [row.symbol for row in rows] == [
        "AMBOR30",
        "AMBOR30T",
        "AMBOR90",
        "AMBOR90T",
        "AMERIBOR",
    ]
    assert all(isinstance(row, FredAmeribor) for row in rows)
    assert [row.maturity for row in rows] == ["30d", "30d", "90d", "90d", "overnight"]
    assert all(row.date.isoformat() == "2026-10-01" for row in rows)
    assert all(row._realtime_start.isoformat() == "2026-10-02" for row in rows)
    assert list(rows[0].model_dump()) == ["date", "symbol", "maturity", "rate", "title"]
    assert all(row.rate > 0 for row in rows)
    assert {params["series_id"] for _, params in requests} == set(ameribor_fixture)
    assert all(params["frequency"] == "m" for _, params in requests)
    assert all(params["aggregation_method"] == "eop" for _, params in requests)
    assert all(params["vintage_dates"] == "2026-10-02" for _, params in requests)


def test_ameribor_rejects_invalid_choice_and_bad_nonnullable_rates_before_or_after_io(
    monkeypatch: pytest.MonkeyPatch,
    ameribor_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = AmeriborFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid maturity must be rejected before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), maturity="1m")

    params = fetcher.transform_query(maturity="overnight")
    bad_row = {**ameribor_fixture["AMERIBOR"][0], "value": "."}
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(
            [{"_selection": "overnight", "_series_id": "AMERIBOR", "observation": bad_row}],
            params,
        )


def test_ecb_rate_series_mapping_pit_paging_and_dot_missing(
    monkeypatch: pytest.MonkeyPatch,
    ecb_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = EuropeanCentralBankInterestRatesFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_series_response(monkeypatch, ecb_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        interest_rate_type="refinancing",
        start_date="2026-10-01",
        end_date="2026-10-02",
        as_of="2026-10-04",
        page_size=1,
        max_pages=2,
    )

    assert [row.rate for row in rows] == [2.15, 2.15]
    assert all(isinstance(row, FredEuropeanCentralBankInterestRate) for row in rows)
    assert [row._series_id for row in rows] == ["ECBMRRFR", "ECBMRRFR"]
    assert [params["offset"] for _, params in requests] == ["0", "1"]
    assert all(params["series_id"] == "ECBMRRFR" for _, params in requests)
    assert all(
        params["realtime_start"] == params["realtime_end"] == "2026-10-04" for _, params in requests
    )
    assert list(rows[0].model_dump()) == ["date", "rate"]

    missing = {**ecb_fixture["ECBDFR"][0], "value": "."}
    missing_rows = fetcher.transform_data(
        [
            {
                "_selection": "deposit",
                "_series_id": "ECBDFR",
                "observation": missing,
            }
        ],
        fetcher.transform_query(interest_rate_type="deposit"),
    )
    assert missing_rows[0].rate is None


def test_ecb_invalid_type_rejected_before_io_and_429_redacts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = EuropeanCentralBankInterestRatesFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid ECB rate type must be rejected before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), interest_rate_type="borrowing")

    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda url, params, timeout, **_kwargs: (429, "rate limit"),
    )
    with pytest.raises(FredProviderError) as raised:
        fetcher.fetch(ctx=_context(fetcher), interest_rate_type="deposit")
    assert raised.value.code == "FRED_HTTP_ERROR"
    assert raised.value.status == 429
    assert "test-key" not in str(raised.value)


@pytest.mark.parametrize(
    ("parameter", "frequency"),
    [
        ("daily_excl_weekend", None),
        ("daily", None),
        ("weekly", "w"),
        ("monthly", "m"),
        ("annual", "a"),
    ],
)
def test_discount_window_frequency_is_an_explicit_fred_aggregation(
    monkeypatch: pytest.MonkeyPatch,
    discount_window_fixture: list[dict[str, str]],
    parameter: str,
    frequency: str | None,
) -> None:
    fetcher = DiscountWindowPrimaryCreditRateFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_series_response(monkeypatch, {"DPCREDIT": discount_window_fixture}, requests)

    rows = fetcher.fetch(ctx=_context(fetcher), parameter=parameter, page_size=1, max_pages=2)

    assert [row.rate for row in rows] == [4.0, 4.0]
    assert all(isinstance(row, FredDiscountWindowPrimaryCreditRate) for row in rows)
    assert [row._series_id for row in rows] == ["DPCREDIT", "DPCREDIT"]
    assert [params["offset"] for _, params in requests] == ["0", "1"]
    assert all(params["series_id"] == "DPCREDIT" for _, params in requests)
    if frequency is None:
        assert all("frequency" not in params for _, params in requests)
    else:
        assert all(params["frequency"] == frequency for _, params in requests)


def test_discount_window_rejects_unknown_frequency_and_bad_values(
    monkeypatch: pytest.MonkeyPatch,
    discount_window_fixture: list[dict[str, str]],
) -> None:
    fetcher = DiscountWindowPrimaryCreditRateFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid parameter must be rejected before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), parameter="quarterly")

    params = fetcher.transform_query()
    for bad_value in ("", "NaN", "inf", "not-a-rate"):
        observation = {**discount_window_fixture[0], "value": bad_value}
        with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
            fetcher.transform_data(
                [
                    {
                        "_selection": "daily_excl_weekend",
                        "_series_id": "DPCREDIT",
                        "observation": observation,
                    }
                ],
                params,
            )


def test_tips_yields_maps_all_four_curve_tenors_and_preserves_contract(
    monkeypatch: pytest.MonkeyPatch,
    tips_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = TipsYieldsFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_series_response(monkeypatch, tips_fixture, requests)

    rows = fetcher.fetch(ctx=_context(fetcher), maturity=None, vintage_dates=["2026-10-02"])

    assert {row.symbol for row in rows} == set(tips_fixture)
    assert all(isinstance(row, FredTipsYield) for row in rows)
    assert {row.value for row in rows} == {1.52, 1.81, 2.05, 2.16}
    assert all(row.due is None for row in rows)
    assert all(row.name and "Inflation-Indexed" in row.name for row in rows)
    assert list(rows[0].model_dump()) == ["date", "symbol", "due", "name", "value"]
    assert {params["series_id"] for _, params in requests} == set(tips_fixture)
    assert all(params["vintage_dates"] == "2026-10-02" for _, params in requests)


def test_tips_yields_rejects_unknown_tenor_and_bad_dates_and_duplicate_revisions(
    monkeypatch: pytest.MonkeyPatch,
    tips_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = TipsYieldsFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("unsupported maturity must be rejected before I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), maturity="7")

    params = fetcher.transform_query(maturity="10")
    sample = tips_fixture["DFII10"][0]
    invalid_date = {**sample, "date": "2026-10-01T00:00:00"}
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(
            [{"_selection": "10", "_series_id": "DFII10", "observation": invalid_date}],
            params,
        )
    duplicate_revision = [
        {"_selection": "10", "_series_id": "DFII10", "observation": sample},
        {
            "_selection": "10",
            "_series_id": "DFII10",
            "observation": {**sample, "value": "1.82", "realtime_start": "2026-10-03"},
        },
    ]
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(duplicate_revision, params)


def test_provider_model_query_injects_identity_and_serializes_ecb_rows(
    monkeypatch: pytest.MonkeyPatch,
    ecb_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = EuropeanCentralBankInterestRatesFetcher()
    observed: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object):
        params = original_transform_query(**kwargs)
        observed.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    requests: list[tuple[str, dict[str, str]]] = []
    _install_series_response(monkeypatch, ecb_fixture, requests)
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model="EuropeanCentralBankInterestRates",
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
        lambda _domain: (None, FredEuropeanCentralBankInterestRate),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model="EuropeanCentralBankInterestRates",
            query={"interest_rate_type": "deposit", "start_date": "2026-10-01"},
            principal=Principal(),
            ctx=_context(fetcher),
        )
    )

    assert observed == [("fred", "eu")]
    assert response["source"] == "fred"
    assert response["model"] == "EuropeanCentralBankInterestRates"
    assert response["results"] == [
        {"date": "2026-10-01", "rate": 2.0},
        {"date": "2026-10-02", "rate": 2.0},
    ]


def test_provider_has_lazy_bindings_for_all_fixed_rate_fetchers() -> None:
    fetchers = {
        fetcher.canonical_model: fetcher
        for fetcher in FRED_PROVIDER.fetchers
        if fetcher.canonical_model
        in {
            "Ameribor",
            "DiscountWindowPrimaryCreditRate",
            "EuropeanCentralBankInterestRates",
            "TipsYields",
        }
    }

    assert set(fetchers) == {
        "Ameribor",
        "DiscountWindowPrimaryCreditRate",
        "EuropeanCentralBankInterestRates",
        "TipsYields",
    }
    assert isinstance(fetchers["Ameribor"], AmeriborFetcher)
    assert isinstance(
        fetchers["DiscountWindowPrimaryCreditRate"], DiscountWindowPrimaryCreditRateFetcher
    )
    assert isinstance(
        fetchers["EuropeanCentralBankInterestRates"], EuropeanCentralBankInterestRatesFetcher
    )
    assert isinstance(fetchers["TipsYields"], TipsYieldsFetcher)
