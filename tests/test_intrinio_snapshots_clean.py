"""Offline behavior tests for the clean Intrinio ETF and current market snapshot slice."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.intrinio_snapshots_clean import (
    SNAPSHOT_GAPS,
    IntrinioEtfPricePerformance,
    IntrinioMarketSnapshot,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.intrinio.models import iter2_market_clean
from opendata.data.providers.intrinio.models.iter2_snapshots_clean import (
    IntrinioEtfPricePerformanceFetcher,
    IntrinioMarketSnapshotsFetcher,
    IntrinioSnapshotQueryError,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)


def _daily(
    day: str,
    close: int | float,
    adjusted: int | float,
    volume: int = 1000,
    split_ratio: int | float | None = None,
) -> dict[str, object]:
    return {
        "date": day,
        "frequency": "daily",
        "close": close,
        "adj_close": adjusted,
        "volume": volume,
        "split_ratio": split_ratio,
    }


def _history() -> list[dict[str, object]]:
    return [
        _daily("2023-12-29", 100, 100),
        _daily("2024-01-02", 101, 102),
        _daily("2024-01-12", 102, 103),
        _daily("2024-01-31", 103, 104),
        _daily("2024-02-01", 104, 105),
        _daily("2024-04-12", 108, 109),
        _daily("2024-07-12", 110, 112),
        _daily("2024-10-11", 112, 114),
        _daily("2024-12-12", 114, 115),
        _daily("2024-12-31", 116, 116),
        _daily("2025-01-02", 120, 120),
        _daily("2025-01-09", 122, 122),
        _daily("2025-01-13", 123, 124),
    ]


def _snapshot_row(
    security_id: str,
    ticker: str,
    *,
    source: str = "delayed_sip",
    listing_venue: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": source,
        "security": {"id": security_id, "ticker": ticker},
        "last_price": 110.0,
        "last_time": "2026-10-12T14:30:00Z",
        "last_size": 20,
        "bid_price": 109.9,
        "bid_size": 30,
        "bid_time": "2026-10-12T14:29:59Z",
        "ask_price": 110.1,
        "ask_size": 40,
        "ask_time": "2026-10-12T14:29:58Z",
        "open_price": 108.0,
        "high_price": 111.0,
        "low_price": 107.0,
        "market_volume": 5000,
        "exchange_volume": 1200,
        "eod_close_price": 100.0,
        "updated_on": "2026-10-12T14:30:01Z",
    }
    if listing_venue is not None:
        row["listing_venue"] = listing_venue
    return row


def test_etf_price_history_computes_normalized_decimal_returns_and_variants() -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()
    trailing = fetcher.transform_query(symbol="spy")
    raw = [
        {
            "symbol": "SPY",
            "etf_ticker": "SPY",
            "security_ticker": "SPY",
            "prices": _history(),
        }
    ]

    row = fetcher.transform_data(raw, trailing)[0]
    assert isinstance(row, IntrinioEtfPricePerformance)
    assert row.one_day == pytest.approx(124 / 122 - 1)
    assert row.one_month == pytest.approx(124 / 115 - 1)
    assert row.one_year == pytest.approx(124 / 103 - 1)
    # New-year starts can be more than seven calendar days after the last trading close.
    assert row.ytd == pytest.approx(124 / 116 - 1)
    assert row.price == 123
    assert row.volume == 1000
    assert row.updated == date(2025, 1, 13)
    assert row.calculation_scale == "end_price_over_start_price_minus_one"
    assert row.calculation_method == "adjusted_close_price_ratio_fraction_v1"

    calendar = fetcher.transform_data(
        raw, fetcher.transform_query(symbol="SPY", return_type="calendar")
    )[0]
    assert calendar.one_year == pytest.approx(116 / 100 - 1)
    assert calendar.one_year != row.one_year
    assert calendar.max == row.max
    assert calendar.model_dump(mode="json")["updated"] == "2025-01-13"

    null_defaults = fetcher.transform_query(
        symbol="SPY", return_type=None, adjustment=None, source="intrinio", market="us"
    )
    assert null_defaults.return_type == "trailing"
    assert null_defaults.adjustment == "splits_and_dividends"


def test_etf_splits_only_is_rejected_before_io_when_ratio_direction_is_undocumented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()
    calls = 0

    def forbidden_io(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AssertionError("unsupported split adjustment must fail before source I/O")

    monkeypatch.setattr(fetcher, "_get_json", forbidden_io)
    with pytest.raises(IntrinioSnapshotQueryError, match="split_ratio direction"):
        fetcher.fetch(symbol="SPY", adjustment="splits_only")
    assert calls == 0
    assert "before I/O" in SNAPSHOT_GAPS["EtfPricePerformance.splits_only"]


def test_etf_query_rejects_unknown_or_unbounded_variants_before_io() -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol=["SPY", "SPY"])
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol=["SPY", "QQQ", "DIA", "IWM"])
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol="SPY", adjustment="total_return")
    with pytest.raises(IntrinioSnapshotQueryError, match="date window"):
        fetcher.transform_query(symbol="SPY", start_date=date(2024, 1, 1))


def test_etf_missing_key_stops_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTRINIO_API_KEY", raising=False)
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not be requested without the credential")

    monkeypatch.setattr(iter2_market_clean, "get_shared_http_client", forbidden_client)
    fetcher = IntrinioEtfPricePerformanceFetcher()
    params = fetcher.transform_query(symbol="SPY")
    with pytest.raises(iter2_market_clean.IntrinioMissingApiKeyError):
        fetcher.extract_data(params, FetchContext())
    assert calls == 0


def test_etf_source_requests_are_finite_and_wrong_identity_or_partial_history_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()
    params = fetcher.transform_query(symbol=["SPY", "QQQ"])
    calls: list[tuple[str, dict[str, str] | None]] = []
    history = _history()

    def fake_json(path: str, query: dict[str, str] | None, _ctx: FetchContext) -> dict[str, Any]:
        calls.append((path, query))
        if path == "/etfs/SPY":
            return {"ticker": "SPY"}
        if path == "/etfs/QQQ":
            return {"ticker": "QQQ"}
        if path.endswith("/SPY/prices"):
            return {"security": {"ticker": "SPY"}, "stock_prices": history, "next_page": None}
        if path.endswith("/QQQ/prices"):
            return {"security": {"ticker": "WRONG"}, "stock_prices": history, "next_page": None}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(fetcher, "_get_json", fake_json)
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="ticker mismatch"):
        fetcher.extract_data(params, FetchContext())
    assert [path for path, _ in calls] == [
        "/etfs/SPY",
        "/securities/SPY/prices",
        "/etfs/QQQ",
        "/securities/QQQ/prices",
    ]
    assert all(query is None or query["frequency"] == "daily" for _, query in calls)


def test_etf_fetch_runs_validated_pipeline_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()

    def fake_json(path: str, _query: dict[str, str] | None, _ctx: FetchContext) -> dict[str, Any]:
        if path == "/etfs/SPY":
            return {"ticker": "SPY"}
        if path == "/securities/SPY/prices":
            return {
                "security": {"ticker": "SPY"},
                "stock_prices": _history(),
                "next_page": None,
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(fetcher, "_get_json", fake_json)
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="intrinio",
        canonical_model="EtfPricePerformance",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture-only-test",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts={"api-v2.intrinio.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=2,
        source_attempts=2,
        grants=[grant],
        profile=profile,
    )
    result = fetcher.fetch(
        ctx=FetchContext(request_budget=budget),
        symbol="SPY",
        source="intrinio",
        market="us",
        return_type=None,
        adjustment=None,
    )
    assert len(result) == 1
    assert isinstance(result[0], IntrinioEtfPricePerformance)
    encoded = result[0].model_dump(mode="json")
    assert encoded["symbol"] == "SPY"
    assert encoded["return_type"] == "trailing"
    assert encoded["calculation_scale"] == "end_price_over_start_price_minus_one"


def test_etf_missing_adjusted_close_and_typed_contract_reject_bad_source_values() -> None:
    fetcher = IntrinioEtfPricePerformanceFetcher()
    params = fetcher.transform_query(symbol="SPY")
    row = _daily("2025-01-13", 123, 124)
    del row["adj_close"]
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="adj_close"):
        fetcher.transform_data(
            [
                {
                    "symbol": "SPY",
                    "etf_ticker": "SPY",
                    "security_ticker": "SPY",
                    "prices": [row],
                }
            ],
            params,
        )
    with pytest.raises(ValidationError):
        IntrinioEtfPricePerformance.model_validate(
            {
                "symbol": "SPY",
                "price": True,
                "updated": "2025-01-13",
                "return_type": "trailing",
                "adjustment": "splits_and_dividends",
                "calculation_scale": "end_price_over_start_price_minus_one",
                "calculation_method": "adjusted_close_price_ratio_fraction_v1",
            }
        )


def test_market_snapshot_query_supports_only_current_nasdaq() -> None:
    fetcher = IntrinioMarketSnapshotsFetcher()
    assert fetcher.transform_query().market == "nasdaq"
    assert fetcher.transform_query(market=None, source="intrinio").market == "nasdaq"
    with pytest.raises(ValidationError):
        fetcher.transform_query(market="nyse")
    with pytest.raises(IntrinioSnapshotQueryError, match="historical"):
        fetcher.transform_query(date="2024-06-01T12:00:00Z")
    with pytest.raises(IntrinioSnapshotQueryError, match="date window"):
        fetcher.transform_query(start_date=date(2024, 1, 1))


def test_market_snapshot_joins_primary_listing_by_security_id_not_trade_venue() -> None:
    fetcher = IntrinioMarketSnapshotsFetcher()
    params = fetcher.transform_query()
    raw = {
        "securities": [
            {
                "id": "sec-1",
                "ticker": "AAA",
                "name": "Alpha Corp",
                "exchange_mic": "XNAS",
                "primary_listing": True,
            },
            {
                "id": "sec-2",
                "ticker": "BBB",
                "name": "Beta Corp",
                "exchange_mic": "XNAS",
                "primary_listing": False,
            },
            {
                "id": "sec-3",
                "ticker": "CCC",
                "name": "Gamma Corp",
                "exchange_mic": "XNAS",
                "primary_listing": True,
            },
        ],
        "stock_prices": [
            _snapshot_row("sec-1", "AAA", listing_venue="N"),
            _snapshot_row("sec-2", "BBB"),
            _snapshot_row("sec-3", "CCC"),
            _snapshot_row("sec-other", "OTHER"),
        ],
    }

    rows = fetcher.transform_data(raw, params)
    assert [row.symbol for row in rows] == ["AAA", "CCC"]
    assert all(isinstance(row, IntrinioMarketSnapshot) for row in rows)
    assert rows[0].exchange == "XNAS"
    assert rows[0].name == "Alpha Corp"
    assert rows[0].last_price == 110
    assert rows[0].volume == 5000
    assert rows[0].prev_close == 100
    assert rows[0].change == 10
    assert rows[0].change_percent == pytest.approx(0.1)
    assert rows[0].last_trade_timestamp is not None
    assert rows[0].model_dump(mode="json")["source_listing_exchange_mic"] == "XNAS"


def test_market_snapshot_rejects_source_identity_mismatch_and_invalid_pages() -> None:
    fetcher = IntrinioMarketSnapshotsFetcher()
    params = fetcher.transform_query()
    directory = [
        {
            "id": "sec-1",
            "ticker": "AAA",
            "name": "Alpha Corp",
            "exchange_mic": "XNAS",
            "primary_listing": True,
        }
    ]
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="ticker"):
        fetcher.transform_data(
            {
                "securities": directory,
                "stock_prices": [_snapshot_row("sec-1", "WRONG")],
            },
            params,
        )
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="source"):
        fetcher.transform_data(
            {
                "securities": directory,
                "stock_prices": [_snapshot_row("sec-1", "AAA", source="iex")],
            },
            params,
        )

    def missing_cursor(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"securities": directory}

    fetcher._get_json = missing_cursor  # type: ignore[method-assign]
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="next_page"):
        fetcher._get_bounded_pages(
            "/stock_exchanges/XNAS/securities", {}, "securities", FetchContext()
        )

    page_responses = iter(
        [
            {"securities": [], "next_page": "cursor-1"},
            {"securities": [], "next_page": "cursor-1"},
        ]
    )
    fetcher._get_json = lambda *_args, **_kwargs: next(page_responses)  # type: ignore[method-assign]
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="repeated"):
        fetcher._get_bounded_pages(
            "/stock_exchanges/XNAS/securities", {}, "securities", FetchContext()
        )


def test_market_snapshot_extract_uses_documented_routes_and_bounded_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = IntrinioMarketSnapshotsFetcher()
    params = fetcher.transform_query()
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_json(path: str, query: dict[str, str] | None, _ctx: FetchContext) -> dict[str, Any]:
        calls.append((path, query))
        if path.endswith("/securities"):
            return {"securities": [], "next_page": None}
        if path.endswith("/prices/realtime"):
            return {"stock_prices": [], "next_page": None}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(fetcher, "_get_json", fake_json)
    raw = fetcher.extract_data(params, FetchContext())
    assert raw == {"securities": [], "stock_prices": []}
    assert calls == [
        (
            "/stock_exchanges/XNAS/securities",
            {"page_size": "1000"},
        ),
        (
            "/stock_exchanges/USCOMP/prices/realtime",
            {"source": "delayed_sip", "page_size": "1000"},
        ),
    ]


def test_snapshot_gaps_remain_explicit_and_no_fake_option_rows_are_declared() -> None:
    assert "OptionsSnapshots" in SNAPSHOT_GAPS
    assert "row schema" in SNAPSHOT_GAPS["OptionsSnapshots"]


def test_typed_contract_serializes_and_rejects_invalid_numeric_values() -> None:
    fetcher = IntrinioMarketSnapshotsFetcher()
    params = fetcher.transform_query(source="intrinio", market="nasdaq")
    result = fetcher.transform_data(
        {
            "securities": [
                {
                    "id": "sec-1",
                    "ticker": "AAA",
                    "name": "Alpha Corp",
                    "exchange_mic": "XNAS",
                    "primary_listing": True,
                }
            ],
            "stock_prices": [_snapshot_row("sec-1", "AAA")],
        },
        params,
    )
    assert result[0].model_dump(mode="json")["last_price"] == 110.0

    bad_row = _snapshot_row("sec-1", "AAA")
    bad_row["bid_size"] = -1
    with pytest.raises(iter2_market_clean.IntrinioResponseShapeError, match="bid_size"):
        fetcher.transform_data(
            {
                "securities": [
                    {
                        "id": "sec-1",
                        "ticker": "AAA",
                        "name": "Alpha Corp",
                        "exchange_mic": "XNAS",
                        "primary_listing": True,
                    }
                ],
                "stock_prices": [bad_row],
            },
            params,
        )
