"""Offline checks for the evidence-backed remaining FRED macro slice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fred_macro_iter2 import FredRetailPrice
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.iter2_macro import FredSeriesFetcher
from opendata.data.providers.fred.models.iter2_macro_remaining import (
    RetailPricesRemainingFetcher,
    RetailPricesRemainingQuery,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)


def _context(fetcher: RetailPricesRemainingFetcher) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic source-structure test grant",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        request_budget=RequestBudget(
            task_attempts=2,
            source_attempts=2,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
    )


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
def test_gas_selects_the_official_piped_gas_series(region: str, area_code: str) -> None:
    fetcher = RetailPricesRemainingFetcher()
    params = fetcher.transform_query(item="gas", region=region)

    assert isinstance(params, RetailPricesRemainingQuery)
    assert fetcher.selected_series(params) == [("gas", f"APU{area_code}72620")]


def test_existing_exact_item_mapping_remains_available() -> None:
    fetcher = RetailPricesRemainingFetcher()
    params = fetcher.transform_query(item="bacon", region="west")

    assert fetcher.selected_series(params) == [("bacon", "APU0400704111")]


def test_gas_fetch_uses_fred_transport_and_serializes_typed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = RetailPricesRemainingFetcher()
    requests: list[dict[str, Any]] = []

    def fake_extract(
        _series_fetcher: FredSeriesFetcher, query: Any, _ctx: FetchContext
    ) -> list[dict[str, str]]:
        requests.append(query.model_dump(mode="python"))
        return [
            {
                "date": "2026-08-01",
                "value": "2.35",
                "realtime_start": "2026-09-01",
                "realtime_end": "9999-12-31",
            }
        ]

    monkeypatch.setattr(FredSeriesFetcher, "extract_data", fake_extract)
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        source="fred",
        market="us",
        item="gas",
        region="midwest",
        frequency="quarter",
        transform="lin",
    )

    assert len(requests) == 1
    assert requests[0]["series_id"] == "APU020072620"
    assert requests[0]["frequency"] == "q"
    assert requests[0]["transform_units"] == "lin"
    assert len(rows) == 1 and isinstance(rows[0], FredRetailPrice)
    assert rows[0].model_dump(mode="json") == {
        "date": "2026-08-01",
        "symbol": "APU020072620",
        "country": "united_states",
        "description": (
            "Utility (piped) gas per therm — All piped gas in Midwest, "
            "average price, not seasonally adjusted"
        ),
        "value": 2.35,
    }
    assert rows[0]._series_ids == ("APU020072620",)


@pytest.mark.parametrize("item", [None, "fuel", "oil", "not_a_selector"])
def test_unsupported_retail_items_fail_before_source_io(
    item: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = RetailPricesRemainingFetcher()
    monkeypatch.setattr(
        FredSeriesFetcher,
        "extract_data",
        lambda *_args: pytest.fail("invalid selectors must fail before I/O"),
    )

    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), item=item)


def test_bad_retail_observation_shape_fails_closed() -> None:
    fetcher = RetailPricesRemainingFetcher()
    params = fetcher.transform_query(item="gas")

    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([None], params)  # type: ignore[list-item]
