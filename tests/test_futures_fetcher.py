"""Akshare futures fetcher tests (B1.2 注册).

The akshare leg stays ``verified=False`` and is reachable by explicit source;
the ths leg that C6 verified owns ``auto``. These tests pin that routing
split and the normalize stage against a recorded Sina frame shape.
"""

from __future__ import annotations

import pandas as pd
import pytest

from opendata.data.models import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.akshare.models.futures_daily import (
    AkshareFuturesDailyFetcher,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-09-23",
                "open": 3110.0,
                "high": 3129.0,
                "low": 3107.0,
                "close": 3113.0,
                "volume": 522561,
                "hold": 1607164,
                "settle": 3117.0,
            },
            {
                "date": "2026-09-22",
                "open": 3117.0,
                "high": 3131.0,
                "low": 3107.0,
                "close": 3114.0,
                "volume": 567871,
                "hold": 1589847,
                "settle": 3121.0,
            },
        ]
    )


class TestFuturesDailyFetcher:
    def test_capability_is_futures_daily_and_unverified(self):
        fetcher = AkshareFuturesDailyFetcher()

        assert fetcher.capability.domain == "futures_daily"
        assert fetcher.capability.asset_class == "futures"
        assert fetcher.capability.source == "akshare"
        assert fetcher.capability.verified is False

    def test_normalize_maps_the_sina_frame_onto_bars(self):
        fetcher = AkshareFuturesDailyFetcher()
        params = fetcher.transform_query(symbol="RB0")

        result = fetcher.transform_data(_frame(), params)
        rows = list(result)

        assert len(rows) == 2
        assert all(isinstance(row, Bar) for row in rows)
        assert rows[0].symbol == "RB0"
        # trade-date ascending after the normalize
        assert [row.trade_date.isoformat() for row in rows] == [
            "2026-09-22",
            "2026-09-23",
        ]
        assert rows[1].close == 3113.0

    def test_normalize_uses_zero_amount_for_the_missing_turnover(self):
        fetcher = AkshareFuturesDailyFetcher()
        rows = list(fetcher.transform_data(_frame(), fetcher.transform_query(symbol="RB0")))

        assert all(row.amount == 0.0 for row in rows)

    def test_empty_frame_normalizes_to_nothing(self):
        fetcher = AkshareFuturesDailyFetcher()

        assert (
            tuple(fetcher.transform_data(pd.DataFrame(), fetcher.transform_query(symbol="RB0")))
            == ()
        )

    def test_bad_date_rows_are_skipped(self):
        fetcher = AkshareFuturesDailyFetcher()
        frame = _frame()
        frame.loc[0, "date"] = "not-a-date"

        rows = list(fetcher.transform_data(frame, fetcher.transform_query(symbol="RB0")))

        assert len(rows) == 1


@pytest.mark.e2e
class TestFuturesLive:
    """真机冒烟：sina 期货日线可达（RB0 螺纹钢主力）."""

    def test_live_fetch_returns_trading_day_bars(self):
        fetcher = AkshareFuturesDailyFetcher()
        query = fetcher.transform_query(symbol="RB0")
        raw = fetcher.extract_data(query, FetchContext(timeout=30.0))
        rows = list(fetcher.transform_data(raw, query))

        assert len(rows) >= 100  # years of daily bars
        assert [row.trade_date for row in rows] == sorted(row.trade_date for row in rows)
        assert all(row.close > 0 for row in rows)
        assert all(row.amount == 0.0 for row in rows)


class TestRouting:
    def test_explicit_source_resolves_after_registration(self):
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()

        fetcher = get_registry().resolve("futures", "futures_daily", source="akshare")

        assert isinstance(fetcher, AkshareFuturesDailyFetcher)

    def test_auto_routes_to_the_verified_ths_leg(self, monkeypatch):
        """C6 graduated ``futures_daily``, so auto now resolves instead of refusing."""
        from opendata.data import registry as registry_module
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        monkeypatch.setattr(
            registry_module, "_source_has_credentials", lambda source: source == "ths"
        )
        register_providers()

        resolved = get_registry().resolve("futures", "futures_daily", source="auto")

        assert resolved.capability.source == "ths"
