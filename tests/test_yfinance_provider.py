"""Unit tests for the yfinance provider (C1 P0).

The SDK is optional (FR-7): every test here works without it - upstream
frames are synthetic, and the fail-closed path is exercised by blocking the
lazy import. Live verification against Yahoo is deferred per R2 (the first
probe was rate limited; see docs/evidence/C1/yfinance-live-probe.txt).
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models import OverseasBar
from opendata.data.protocol import FetchContext
from opendata.data.providers.yfinance import register
from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.providers.yfinance.models.stock_daily import (
    YfinanceProviderError,
    YfinanceStockDailyFetcher,
)
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry


def _upstream_frame() -> pd.DataFrame:
    """Synthetic upstream frame: two days, SDK-style columns, tz-aware index."""
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-09-21", tz="America/New_York"),
            pd.Timestamp("2026-09-22", tz="America/New_York"),
        ],
        name="Date",
    )
    return pd.DataFrame(
        {
            "Open": [228.0, 229.5],
            "High": [230.0, 231.0],
            "Low": [227.0, 228.5],
            "Close": [229.5, 230.5],
            "Volume": [51_000_000.0, 42_000_000.0],
        },
        index=index,
    )


@pytest.fixture(autouse=True)
def registry() -> ProviderRegistry:
    """Registered fetchers for every test, isolated per test via the singleton."""
    register()
    return get_registry()


class TestRegistration:
    def test_capability_fields(self) -> None:
        capability = YfinanceStockDailyFetcher().capability
        assert capability.domain == "stock_daily_overseas"
        assert capability.market == "global"
        assert capability.source == SOURCE == "yfinance"
        assert capability.verified is False

    def test_provider_package_source_is_directory_name(self) -> None:
        assert SOURCE == "yfinance"

    def test_register_is_idempotent(self) -> None:
        first = register()
        assert first == []

    def test_unverified_capability_is_not_auto_routed(self) -> None:
        with pytest.raises(LookupError):
            get_registry().resolve_domain("stock_daily_overseas")


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            YfinanceStockDailyFetcher().transform_query(symbol="AAPL", bogus=1)


class TestTransform:
    def test_rows_sorted_amount_none_symbol_upper(self) -> None:
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="aapl")
        result = fetcher.transform_data(_upstream_frame(), query)
        assert [bar.trade_date for bar in result] == [
            datetime.date(2026, 9, 21),
            datetime.date(2026, 9, 22),
        ]
        assert all(bar.symbol == "AAPL" for bar in result)
        assert all(bar.amount is None for bar in result)
        assert result[1].close == pytest.approx(230.5)

    def test_missing_column_fails_closed(self) -> None:
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="AAPL")
        frame = _upstream_frame().drop(columns=["Volume"])
        with pytest.raises(YfinanceProviderError) as err:
            fetcher.transform_data(frame, query)
        assert err.value.code == "YFINANCE_COLUMNS_MISSING"

    def test_empty_response_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _empty(*args: object, **kwargs: object) -> pd.DataFrame:
            return pd.DataFrame()

        monkeypatch.setattr(
            "opendata.data.providers.yfinance.models._sdk.require_history_frame", _empty
        )
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="AAPL")
        with pytest.raises(YfinanceProviderError) as err:
            fetcher.extract_data(query, FetchContext())
        assert err.value.code == "YFINANCE_EMPTY_RESPONSE"

    def test_missing_sdk_fails_closed_with_stable_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def _blocked(name: str, *args: object, **kwargs: object) -> Any:
            if name == "yfinance":
                raise ImportError("no yfinance")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", _blocked)
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="AAPL")
        with pytest.raises(YfinanceProviderError) as err:
            fetcher.extract_data(query, FetchContext())
        assert err.value.code == "YFINANCE_SDK_MISSING"


class TestOverseasBarContract:
    def test_amount_is_nullable(self) -> None:
        bar = OverseasBar(
            symbol="AAPL",
            trade_date=datetime.date(2026, 9, 22),
            open=229.0,
            high=231.0,
            low=228.0,
            close=230.5,
            volume=42_000_000.0,
        )
        assert bar.amount is None

    def test_nan_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OverseasBar(
                symbol="AAPL",
                trade_date=datetime.date(2026, 9, 22),
                open=float("nan"),
                high=231.0,
                low=228.0,
                close=230.5,
                volume=42_000_000.0,
            )

    def test_from_frame_keeps_amount_none(self) -> None:
        frame = (
            _upstream_frame()
            .reset_index()
            .rename(
                columns={
                    "Date": "trade_date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
        )
        frame["symbol"] = "AAPL"
        rows = OverseasBar.from_frame(frame)
        assert len(rows) == 2
        assert all(row.amount is None for row in rows)
