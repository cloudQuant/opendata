"""Unit tests for the yfinance provider (C1 P0, D10 fix in C8).

The SDK is optional (FR-7): every test here works without it - upstream frames
are synthetic, the lazy import is blocked or replaced, and the restoration
numbers are the ones measured live in ``docs/evidence/C8/``.
"""

from __future__ import annotations

import datetime
import sys
import types
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models import OverseasBar
from opendata.data.protocol import FetchContext
from opendata.data.providers.yfinance import register
from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.providers.yfinance.models import _sdk
from opendata.data.providers.yfinance.models.stock_daily import (
    SPLITS_COLUMN,
    YfinanceHistory,
    YfinanceProviderError,
    YfinanceStockDailyFetcher,
    as_traded_bars,
    lookahead_factors,
    split_events,
)
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.registry import ProviderRegistry

#: The measured AAPL ex-date and its 4:1 ratio (2020-08-31, a Monday).
AAPL_SPLIT = pd.Series(
    [4.0],
    index=pd.DatetimeIndex([pd.Timestamp("2020-08-31", tz="America/New_York")]),
    name=SPLITS_COLUMN,
)


def _index(*days: str) -> pd.DatetimeIndex:
    """A trading index, keyed the way the SDK keys its frame (exchange tz)."""
    return pd.DatetimeIndex(
        [pd.Timestamp(day, tz="America/New_York") for day in days],
        name="Date",
    )


def _frame(
    days: tuple[str, ...] = ("2026-09-21", "2026-09-22"),
    **overrides: Any,
) -> pd.DataFrame:
    """Synthetic upstream frame: flat 228/230/227/229.5, 51M shares."""
    prices = {"Open": 228.0, "High": 230.0, "Low": 227.0, "Close": 229.5, "Volume": 51_000_000.0}
    prices.update(overrides)
    return pd.DataFrame(
        {name: [value] * len(days) for name, value in prices.items()},
        index=_index(*days),
    )


def _history(frame: pd.DataFrame | None = None, splits: pd.Series | None = None) -> YfinanceHistory:
    """The raw pair ``extract_data`` hands to ``transform_data``."""
    return YfinanceHistory(
        frame=frame if frame is not None else _frame(),
        splits=pd.Series(dtype="float64") if splits is None else splits,
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
        assert capability.verified is True

    def test_provider_package_source_is_directory_name(self) -> None:
        """``SOURCE`` 是从包目录名**派生**的标签：预计算字面量是它的黄金向量。

        （生产侧 ``SOURCE = Path(__file__).resolve().parent.name``，不是字符串常量，
        所以这句不是抄定义；抄定义的那种形态由 C44 的空壳普查点名。）
        """
        assert SOURCE == "yfinance"

    def test_source_label_routes_the_leg(self, registry: ProviderRegistry) -> None:
        """source 不是档案：按字面量点名必须取到这条腿，取不到就是标签写错了。

        断言 ``SOURCE == "yfinance"`` 只把定义抄一遍；这里断言的是注册表拿这个
        字符串做路由键的可观测行为。
        """
        fetcher = registry.resolve("equity", "stock_daily_overseas", source="yfinance")

        assert isinstance(fetcher, YfinanceStockDailyFetcher)
        assert fetcher.capability.source == "yfinance"

    def test_register_is_idempotent(self) -> None:
        first = register()
        assert first == []

    def test_verified_capability_is_auto_routed(self) -> None:
        fetcher = get_registry().resolve_domain("stock_daily_overseas")
        assert isinstance(fetcher, YfinanceStockDailyFetcher)


class TestQuery:
    def test_query_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            YfinanceStockDailyFetcher().transform_query(symbol="AAPL", bogus=1)


class TestSplitFactors:
    """The factor rule, on the tz-stripped series ``split_events`` produces."""

    @staticmethod
    def _events(splits: pd.Series) -> pd.Series:
        return split_events(_frame(), splits)

    def test_no_split_history_restores_everything_by_one(self) -> None:
        factors = lookahead_factors(_index("2020-08-28"), self._events(pd.Series(dtype="float64")))
        assert list(factors) == [1.0]

    def test_the_ex_date_row_is_already_post_split(self) -> None:
        """Measured: 2020-08-28 trades at 4x the 2020-08-31 basis."""
        factors = lookahead_factors(_index("2020-08-28", "2020-08-31"), self._events(AAPL_SPLIT))
        assert list(factors) == [4.0, 1.0]

    def test_two_later_splits_compound(self) -> None:
        splits = pd.Series(
            [4.0, 2.0],
            index=pd.DatetimeIndex([pd.Timestamp("2020-08-31"), pd.Timestamp("2022-01-03")]),
        )
        factors = lookahead_factors(
            _index("2020-08-28", "2021-01-04", "2022-06-01"), self._events(splits)
        )
        assert list(factors) == [8.0, 2.0, 1.0]

    def test_frame_split_rows_must_agree_with_the_history(self) -> None:
        frame = _frame(("2020-08-31",))
        frame[SPLITS_COLUMN] = 4.0
        assert list(split_events(frame, AAPL_SPLIT)) == [4.0]
        with pytest.raises(YfinanceProviderError) as err:
            split_events(frame, pd.Series(dtype="float64"))
        assert err.value.code == "YFINANCE_SPLITS_UNAVAILABLE"
        disagreeing = pd.Series([5.0], index=pd.DatetimeIndex([pd.Timestamp("2020-08-31")]))
        with pytest.raises(YfinanceProviderError) as err:
            split_events(frame, disagreeing)
        assert err.value.code == "YFINANCE_SPLITS_UNAVAILABLE"

    def test_window_with_no_split_row_inside_still_uses_the_history(self) -> None:
        """A pre-split window carries an all-zero column: the history still applies.

        This is the measured C8 defect - Yahoo 2020-06-01..07-14 shows no split
        row yet arrives divided by 4, so the full history must win here.
        """
        frame = _frame(("2020-06-01", "2020-07-14"))
        frame[SPLITS_COLUMN] = 0.0
        assert list(split_events(frame, AAPL_SPLIT)) == [4.0]
        restored = as_traded_bars(frame, AAPL_SPLIT)
        assert restored["Close"].tolist() == [918.0, 918.0]

    def test_negative_ratio_fails_closed(self) -> None:
        bogus = pd.Series([-4.0], index=pd.DatetimeIndex([pd.Timestamp("2020-08-31")]))
        with pytest.raises(YfinanceProviderError) as err:
            split_events(_frame(), bogus)
        assert err.value.code == "YFINANCE_BAD_SPLIT_RATIO"


class TestTransform:
    def test_rows_sorted_amount_none_symbol_upper(self) -> None:
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="aapl")
        result = fetcher.transform_data(_history(), query)
        assert [bar.trade_date for bar in result] == [
            datetime.date(2026, 9, 21),
            datetime.date(2026, 9, 22),
        ]
        assert all(bar.symbol == "AAPL" for bar in result)
        assert all(bar.amount is None for bar in result)
        assert result[1].close == pytest.approx(229.5)

    def test_pre_split_window_is_restored_with_the_out_of_window_ratio(self) -> None:
        """The defect C8 fixes: Yahoo de-visions by a split outside the window.

        Raw values as they arrive from the upstream for AAPL 2020-06-02, and
        the as-traded pair sina publishes for that session (320.75 / 21,910,704).
        """
        frame = _frame(
            ("2020-06-02",),
            Open=80.1875,
            High=80.860001,
            Low=79.867493,
            Close=80.835003,
            Volume=87_642_800.0,
        )
        query = YfinanceStockDailyFetcher().transform_query(symbol="AAPL")
        result = YfinanceStockDailyFetcher().transform_data(_history(frame, AAPL_SPLIT), query)
        assert result[0].open == pytest.approx(320.75, rel=1e-6)
        assert result[0].close == pytest.approx(323.34, rel=1e-6)
        assert result[0].volume == pytest.approx(21_910_700.0, rel=1e-6)

    def test_unsettled_session_is_dropped(self) -> None:
        """A live bar arrives with ``NaN`` prices; it is not a trading result."""
        frame = _frame(("2026-09-21", "2026-09-22"))
        live = pd.Timestamp("2026-09-22", tz="America/New_York")
        frame.loc[live, ["Open", "High", "Low", "Close"]] = float("nan")
        result = YfinanceStockDailyFetcher().transform_data(
            _history(frame),
            YfinanceStockDailyFetcher().transform_query(symbol="AAPL"),
        )
        assert [bar.trade_date for bar in result] == [datetime.date(2026, 9, 21)]

    def test_missing_column_fails_closed(self) -> None:
        query = YfinanceStockDailyFetcher().transform_query(symbol="AAPL")
        frame = _frame().drop(columns=["Volume"])
        with pytest.raises(YfinanceProviderError) as err:
            YfinanceStockDailyFetcher().transform_data(_history(frame), query)
        assert err.value.code == "YFINANCE_COLUMNS_MISSING"

    def test_as_traded_bars_leaves_the_input_alone(self) -> None:
        frame = _frame(("2020-08-28",), Close=124.807503, Volume=187_630_000.0)
        restored = as_traded_bars(frame, AAPL_SPLIT)
        assert frame["Close"].iloc[0] == pytest.approx(124.807503)
        assert restored["Close"].iloc[0] == pytest.approx(499.23, rel=1e-6)
        assert restored["Volume"].iloc[0] == pytest.approx(46_907_500.0, rel=1e-6)


class TestExtract:
    def test_empty_response_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_sdk, "require_history_frame", lambda *a, **k: pd.DataFrame())
        fetcher = YfinanceStockDailyFetcher()
        query = fetcher.transform_query(symbol="AAPL")
        with pytest.raises(YfinanceProviderError) as err:
            fetcher.extract_data(query, FetchContext())
        assert err.value.code == "YFINANCE_EMPTY_RESPONSE"

    def test_window_and_split_history_travel_together(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_sdk, "require_history_frame", lambda *a, **k: _frame())
        monkeypatch.setattr(_sdk, "require_split_history", lambda symbol: AAPL_SPLIT)
        history = YfinanceStockDailyFetcher().extract_data(
            YfinanceStockDailyFetcher().transform_query(symbol="AAPL"), FetchContext(timeout=5.0)
        )
        assert list(history.splits) == [4.0]
        assert history.frame is not None

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
        query = YfinanceStockDailyFetcher().transform_query(symbol="AAPL")
        for call in (
            lambda: YfinanceStockDailyFetcher().extract_data(query, FetchContext()),
            lambda: _sdk.require_split_history("AAPL"),
        ):
            with pytest.raises(YfinanceProviderError) as err:
                call()
            assert err.value.code == "YFINANCE_SDK_MISSING"


class TestSdkSeam:
    """The two lazy SDK calls, with a stand-in module so no network is touched."""

    @pytest.fixture
    def fake_sdk(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Install a fake ``yfinance`` and hand back its recording box."""
        calls: dict[str, Any] = {}

        class _Ticker:
            def __init__(self, symbol: str) -> None:
                calls["symbol"] = symbol

            def history(self, **kwargs: object) -> pd.DataFrame:
                calls["history"] = kwargs
                return _frame()

            @property
            def splits(self) -> object:
                if isinstance(calls.get("splits"), Exception):
                    raise calls["splits"]
                return calls.get("splits_value", pd.Series(dtype="float64"))

        module = types.ModuleType("yfinance")
        module.Ticker = _Ticker  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "yfinance", module)
        return calls

    def test_history_asks_the_unadjusted_mode(self, fake_sdk: dict[str, Any]) -> None:
        _sdk.require_history_frame("aapl ", start=None, end=None, timeout=None)
        assert fake_sdk["history"]["auto_adjust"] is False
        assert fake_sdk["history"]["interval"] == "1d"
        assert fake_sdk["symbol"] == "aapl"

    def test_inclusive_end_becomes_the_upstreams_exclusive_one(
        self, fake_sdk: dict[str, Any]
    ) -> None:
        _sdk.require_history_frame(
            "AAPL", start=datetime.date(2020, 6, 1), end=datetime.date(2020, 7, 14), timeout=2.5
        )
        assert fake_sdk["history"]["start"] == "2020-06-01"
        assert fake_sdk["history"]["end"] == "2020-07-15"
        assert fake_sdk["history"]["timeout"] == 2.5

    def test_split_history_is_flattened_to_floats(self, fake_sdk: dict[str, Any]) -> None:
        fake_sdk["splits_value"] = pd.DataFrame({SPLITS_COLUMN: [4.0]}, index=AAPL_SPLIT.index)
        splits = _sdk.require_split_history("AAPL")
        assert list(splits) == [4.0]

    def test_absent_split_history_is_not_a_failure(self, fake_sdk: dict[str, Any]) -> None:
        fake_sdk["splits_value"] = None
        assert _sdk.require_split_history("AAPL").empty

    def test_a_failing_split_request_fails_closed(self, fake_sdk: dict[str, Any]) -> None:
        """An empty result would read as "no splits", so a raise must be a raise."""
        fake_sdk["splits"] = RuntimeError("upstream 429")
        with pytest.raises(YfinanceProviderError) as err:
            _sdk.require_split_history("AAPL")
        assert err.value.code == "YFINANCE_SPLITS_UNAVAILABLE"


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
            _frame()
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
