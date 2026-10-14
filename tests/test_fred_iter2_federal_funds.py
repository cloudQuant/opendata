"""Independent fixtures and adversarial tests for FRED's rate composites."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_iter2 import (
    FredFederalFundsRate,
    FredOvernightBankFundingRate,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.federal_funds import (
    FederalFundsRateFetcher,
    OvernightBankFundingRateFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_VALUES = {
    "EFFR": "3.88",
    "DFEDTARU": "4.00",
    "DFEDTARL": "3.75",
    "EFFR1": "3.85",
    "EFFR25": "3.87",
    "EFFR75": "3.88",
    "EFFR99": "3.92",
    "EFFRVOL": "117",
    "EFFRL": "3.75",
    "EFFRH": "4.00",
    "EFFRSD": "0.03",
}
_OBFR_VALUES = {
    "OBFR": "3.88",
    "OBFR1": "3.85",
    "OBFR25": "3.87",
    "OBFR75": "3.88",
    "OBFR99": "3.92",
    "OBFRVOL": "117",
}


@pytest.fixture(autouse=True)
def _fred_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")


@pytest.fixture
def federal_funds_fixture() -> dict[str, list[dict[str, str]]]:
    """One distinct sample for each documented FRED rate and companion ID."""
    return {
        series_id: [
            {
                "date": "2026-10-08",
                "value": value,
                "realtime_start": "2026-10-09",
                "realtime_end": "9999-12-31",
            }
        ]
        for series_id, value in _VALUES.items()
    }


@pytest.fixture
def overnight_bank_funding_fixture() -> dict[str, list[dict[str, str]]]:
    """One hand-authored observation for each official OBFR series ID."""
    return {
        series_id: [
            {
                "date": "2026-10-08",
                "value": value,
                "realtime_start": "2026-10-09",
                "realtime_end": "9999-12-31",
            }
        ]
        for series_id, value in _OBFR_VALUES.items()
    }


def _context(fetcher: Any) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic query-only grant for FRED test fixture",
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


def _page(observations: list[dict[str, str]], *, offset: int, limit: int) -> str:
    return json.dumps(
        {
            "count": len(observations),
            "offset": offset,
            "limit": limit,
            "observations": observations[offset : offset + limit],
        }
    )


def _install_response(
    monkeypatch: pytest.MonkeyPatch,
    fixture: dict[str, list[dict[str, str]]],
    requests: list[tuple[str, dict[str, str]]],
) -> None:
    def http_get(
        url: str,
        params: dict[str, str],
        timeout: float | None,
        **_kwargs,
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        return 200, _page(
            fixture[params["series_id"]],
            offset=int(params["offset"]),
            limit=int(params["limit"]),
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)


def test_federal_funds_maps_documented_fred_series_and_retains_private_vintages(
    monkeypatch: pytest.MonkeyPatch,
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, federal_funds_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        start_date="2026-10-08",
        end_date="2026-10-08",
        vintage_dates=["2026-10-09"],
    )

    assert {params["series_id"] for _, params in requests} == set(_VALUES)
    assert all("test-key" not in url for url, _ in requests)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, FredFederalFundsRate)
    assert row.model_dump(mode="json") == {
        "date": "2026-10-08",
        "rate": 3.88,
        "target_range_upper": 4.0,
        "target_range_lower": 3.75,
        "percentile_1": 3.85,
        "percentile_25": 3.87,
        "percentile_75": 3.88,
        "percentile_99": 3.92,
        "volume": 117.0,
        "intraday_low": 3.75,
        "intraday_high": 4.0,
        "standard_deviation": 0.03,
        "revision_indicator": None,
    }
    assert row._realtime_by_series["EFFR"] == (
        datetime(2026, 10, 9).date(),
        datetime(9999, 12, 31).date(),
    )


def test_effr_only_fetches_primary_series_and_leaves_companions_null(
    monkeypatch: pytest.MonkeyPatch,
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, federal_funds_fixture, requests)

    rows = fetcher.fetch(ctx=_context(fetcher), effr_only=True)

    assert [params["series_id"] for _, params in requests] == ["EFFR"]
    assert rows[0].rate == 3.88
    assert rows[0].percentile_25 is None
    assert rows[0].target_range_upper is None
    assert rows[0].volume is None


def test_invalid_query_is_rejected_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = FederalFundsRateFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args, **_kwargs: pytest.fail("invalid query must fail before FRED I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(start_date="2026-10-02", end_date="2026-10-01")
    with pytest.raises(ValidationError):
        fetcher.transform_query(frequency="d")
    with pytest.raises(ValidationError):
        fetcher.transform_query(as_of="2026-10-09", realtime_start="2026-10-09")
    with pytest.raises(ValidationError):
        fetcher.transform_query(vintage_dates=[])


def test_partial_companions_and_dot_values_are_nullable_but_primary_rate_is_required(
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    params = fetcher.transform_query()
    fixture = dict(federal_funds_fixture)
    fixture["EFFR25"] = [{**fixture["EFFR25"][0], "value": "."}]
    raw = [
        {
            "field_name": field_name,
            "series_id": series_id,
            "observation": observations[0],
        }
        for field_name, series_id in (
            ("rate", "EFFR"),
            ("percentile_25", "EFFR25"),
        )
        for observations in (fixture[series_id],)
    ]

    row = fetcher.transform_data(raw, params)[0]
    assert row.rate == 3.88
    assert row.percentile_25 is None
    assert row.percentile_1 is None

    raw[0]["observation"] = {**raw[0]["observation"], "value": "."}
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(raw, params)


def test_duplicate_revision_or_nonfinite_companion_fails_closed(
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    params = fetcher.transform_query()
    primary = {
        "field_name": "rate",
        "series_id": "EFFR",
        "observation": federal_funds_fixture["EFFR"][0],
    }
    revised = {
        **primary,
        "observation": {
            **federal_funds_fixture["EFFR"][0],
            "value": "3.89",
            "realtime_start": "2026-10-10",
        },
    }
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([primary, revised], params)

    bad_companion = {
        "field_name": "percentile_1",
        "series_id": "EFFR1",
        "observation": {
            **federal_funds_fixture["EFFR1"][0],
            "value": "nan",
        },
    }
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([primary, bad_companion], params)


def test_fred_pagination_limit_and_throttling_are_reported_without_key_leak(
    monkeypatch: pytest.MonkeyPatch,
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    first = federal_funds_fixture["EFFR"][0]
    second = {**first, "date": "2026-10-09"}
    fixture = {**federal_funds_fixture, "EFFR": [first, second]}
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, fixture, requests)
    with pytest.raises(FredProviderError, match="FRED_QUERY_LIMIT"):
        fetcher.fetch(ctx=_context(fetcher), page_size=1, max_records=2, max_pages=1)
    assert [params["offset"] for _, params in requests] == ["0"]

    def too_many_requests(
        url: str,
        params: dict[str, str],
        timeout: float | None,
        **_kwargs,
    ) -> tuple[int, str]:
        del timeout
        assert "api_key" not in url and "test-key" not in url
        return 429, "rate limited"

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", too_many_requests)
    with pytest.raises(FredProviderError, match="FRED_HTTP_ERROR") as error:
        fetcher.fetch(ctx=_context(fetcher), effr_only=True)
    assert error.value.status == 429
    assert "test-key" not in str(error.value)


def test_provider_model_query_injects_fred_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    federal_funds_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = FederalFundsRateFetcher()
    received: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object):
        params = original_transform_query(**kwargs)
        received.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, federal_funds_fixture, requests)
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model="FederalFundsRate",
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
        lambda _domain: (None, FredFederalFundsRate),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model="FederalFundsRate",
            query={"start_date": "2026-10-08", "effr_only": True},
            principal=Principal(),
            ctx=_context(fetcher),
        )
    )

    assert received == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["model"] == "FederalFundsRate"
    assert response["results"] == [
        {
            "date": "2026-10-08",
            "rate": 3.88,
            "target_range_upper": None,
            "target_range_lower": None,
            "percentile_1": None,
            "percentile_25": None,
            "percentile_75": None,
            "percentile_99": None,
            "volume": None,
            "intraday_low": None,
            "intraday_high": None,
            "standard_deviation": None,
            "revision_indicator": None,
        }
    ]


def test_fred_obfr_maps_six_official_series_and_aligns_nullable_companions(
    monkeypatch: pytest.MonkeyPatch,
    overnight_bank_funding_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = OvernightBankFundingRateFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, overnight_bank_funding_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        start_date="2026-10-08",
        end_date="2026-10-08",
        vintage_dates=["2026-10-09"],
    )

    assert {params["series_id"] for _, params in requests} == set(_OBFR_VALUES)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, FredOvernightBankFundingRate)
    assert row.model_dump(mode="json") == {
        "date": "2026-10-08",
        "rate": 3.88,
        "percentile_1": 3.85,
        "percentile_25": 3.87,
        "percentile_75": 3.88,
        "percentile_99": 3.92,
        "volume": 117.0,
        "revision_indicator": None,
    }
    assert row._realtime_by_series["OBFR"] == (
        datetime(2026, 10, 9).date(),
        datetime(9999, 12, 31).date(),
    )


def test_fred_obfr_rejects_bad_query_and_required_rate_missing(
    monkeypatch: pytest.MonkeyPatch,
    overnight_bank_funding_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = OvernightBankFundingRateFetcher()
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *_args, **_kwargs: pytest.fail("invalid query must fail before FRED I/O"),
    )
    with pytest.raises(ValidationError):
        fetcher.transform_query(start_date="2026-10-09", end_date="2026-10-08")
    with pytest.raises(ValidationError):
        fetcher.transform_query(transform="lin")

    primary = {
        "field_name": "rate",
        "series_id": "OBFR",
        "observation": {
            **overnight_bank_funding_fixture["OBFR"][0],
            "value": ".",
        },
    }
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([primary], fetcher.transform_query())


def test_fred_obfr_provider_query_injects_source_market_and_typed_output(
    monkeypatch: pytest.MonkeyPatch,
    overnight_bank_funding_fixture: dict[str, list[dict[str, str]]],
) -> None:
    fetcher = OvernightBankFundingRateFetcher()
    received: list[tuple[str, str | None]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object):
        params = original_transform_query(**kwargs)
        received.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    requests: list[tuple[str, dict[str, str]]] = []
    _install_response(monkeypatch, overnight_bank_funding_fixture, requests)
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fred",
        model="OvernightBankFundingRate",
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
        lambda _domain: (None, FredOvernightBankFundingRate),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fred",
            model="OvernightBankFundingRate",
            query={"frequency": "m"},
            principal=Principal(),
            ctx=_context(fetcher),
        )
    )

    assert received == [("fred", "us")]
    assert response["source"] == "fred"
    assert response["model"] == "OvernightBankFundingRate"
    assert response["results"] == [
        {
            "date": "2026-10-08",
            "rate": 3.88,
            "percentile_1": 3.85,
            "percentile_25": 3.87,
            "percentile_75": 3.88,
            "percentile_99": 3.92,
            "volume": 117.0,
            "revision_indicator": None,
        }
    ]
