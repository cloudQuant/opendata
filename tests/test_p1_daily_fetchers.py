"""Akshare P1 daily-bar fetcher tests (B1.2 注册: index / ETF / option / bond).

All four capabilities carry ``verified=False`` until the AC-6 P1 fidelity
sampling covers them, so the tests pin the routing semantics (explicit
source resolves, ``auto`` refuses - same contract as the futures daily
chain) and the normalize stage against recorded upstream frame shapes.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from opendata.data.models import Bar
from opendata.data.providers.akshare.models.bond_daily import AkshareBondDailyFetcher
from opendata.data.providers.akshare.models.fund_etf_daily import AkshareFundEtfDailyFetcher
from opendata.data.providers.akshare.models.index_daily import AkshareIndexDailyFetcher
from opendata.data.providers.akshare.models.option_daily import AkshareOptionDailyFetcher

_EM_ROW_23 = ("2026-09-23", 3.92, 3.95, 3.97, 3.91, 1_283_405, 5_064_112.5)
_EM_ROW_22 = ("2026-09-22", 3.89, 3.92, 3.93, 3.88, 1_024_880, 4_002_731.0)
_EM_COLUMNS = ("日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额")


def _em_frame() -> pd.DataFrame:
    """Two eastmoney kline rows (volume in lots, 手)."""
    return pd.DataFrame(
        [
            dict(zip(_EM_COLUMNS, _EM_ROW_23, strict=True)),
            dict(zip(_EM_COLUMNS, _EM_ROW_22, strict=True)),
        ]
    )


def _sina_option_frame() -> pd.DataFrame:
    """Two sina SSE stock-option rows (no turnover column)."""
    columns = ("日期", "开盘", "最高", "最低", "收盘", "成交量")
    rows = [
        ("2026-09-23", 45.0, 48.5, 44.0, 47.0, 1288.0),
        ("2026-09-22", 43.0, 46.0, 42.5, 45.5, 964.0),
    ]
    return pd.DataFrame([dict(zip(columns, row, strict=True)) for row in rows])


def _sina_bond_frame() -> pd.DataFrame:
    """Two sina convertible-bond rows (English columns, no turnover)."""
    columns = ("date", "open", "high", "low", "close", "volume")
    rows = [
        ("2026-09-23", 128.5, 129.2, 128.1, 129.0, 35_410),
        ("2026-09-22", 127.9, 128.7, 127.6, 128.5, 28_977),
    ]
    return pd.DataFrame([dict(zip(columns, row, strict=True)) for row in rows])


def _case_id(case: tuple[Any, ...]) -> str:
    return str(case[1])


#: (fetcher, domain, asset_class, kwargs, frame, date column,
#:    expected symbol, OHLC of the latest row, volume, amount)
CASES: tuple[tuple[Any, ...], ...] = (
    (
        AkshareIndexDailyFetcher(),
        "index_daily",
        "index",
        {"symbol": "000300"},
        _em_frame,
        "日期",
        "000300",
        (3.92, 3.97, 3.91, 3.95),
        128_340_500.0,
        5_064_112.5,
    ),
    (
        AkshareFundEtfDailyFetcher(),
        "fund_etf_daily",
        "fund",
        {"symbol": "510300.SH"},
        _em_frame,
        "日期",
        "510300",
        (3.92, 3.97, 3.91, 3.95),
        128_340_500.0,
        5_064_112.5,
    ),
    (
        AkshareOptionDailyFetcher(),
        "option_daily",
        "option",
        {"symbol": "10003889"},
        _sina_option_frame,
        "日期",
        "10003889",
        (45.0, 48.5, 44.0, 47.0),
        1288.0,
        0.0,
    ),
    (
        AkshareBondDailyFetcher(),
        "bond_daily",
        "bond",
        {"symbol": "SH010107"},
        _sina_bond_frame,
        "date",
        "sh010107",
        (128.5, 129.2, 128.1, 129.0),
        35_410.0,
        0.0,
    ),
)


def _latest(fetcher: Any, case: tuple[Any, ...]) -> Bar:
    """Normalize the case frame and return its newest bar."""
    raw = fetcher.transform_data(case[4](), fetcher.transform_query(**case[3]))
    rows = list(raw)
    assert all(isinstance(row, Bar) for row in rows)
    bars: list[Bar] = list(rows)
    return max(bars, key=lambda bar: bar.trade_date)


class TestP1DailyRegistration:
    """One unverified cn/1D capability per new domain, wired into routing."""

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_capability_shape(self, case: tuple[Any, ...]) -> None:
        fetcher: Any = case[0]

        assert fetcher.capability.domain == case[1]
        assert fetcher.capability.asset_class == case[2]
        assert fetcher.capability.source == "akshare"
        assert fetcher.capability.market == "cn"
        assert fetcher.capability.period == "1D"
        assert fetcher.capability.verified is False
        assert fetcher.capability.participates_in_auto() is False

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_domains_yaml_declares_the_domain(self, case: tuple[Any, ...]) -> None:
        from opendata.data.domains import contract_model, require_domain

        assert require_domain(case[1]).contract == "Bar"
        assert contract_model(case[1]) is Bar

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_explicit_source_resolves_and_auto_refuses(self, case: tuple[Any, ...]) -> None:
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        registry = get_registry()
        fetcher: Any = case[0]

        assert registry.resolve(case[2], case[1], source="akshare") is fetcher or isinstance(
            registry.resolve(case[2], case[1], source="akshare"), type(fetcher)
        )
        with pytest.raises(LookupError):
            registry.resolve(case[2], case[1], source="auto")


class TestP1DailyNormalize:
    """``normalize()`` owns field mapping and unit conversion per source."""

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_latest_bar_matches_the_source_row(self, case: tuple[Any, ...]) -> None:
        fetcher: Any = case[0]

        bar = _latest(fetcher, case)

        assert bar.symbol == case[6]
        assert bar.trade_date.isoformat() == "2026-09-23"
        assert (bar.open, bar.high, bar.low, bar.close) == case[7]
        assert bar.volume == case[8]
        assert bar.amount == case[9]

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_both_rows_survive(self, case: tuple[Any, ...]) -> None:
        fetcher: Any = case[0]

        rows = list(fetcher.transform_data(case[4](), fetcher.transform_query(**case[3])))

        assert len(rows) == 2

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_empty_frame_normalizes_to_nothing(self, case: tuple[Any, ...]) -> None:
        fetcher: Any = case[0]

        assert (
            tuple(fetcher.transform_data(pd.DataFrame(), fetcher.transform_query(**case[3]))) == ()
        )

    @pytest.mark.parametrize("case", CASES, ids=_case_id)
    def test_bad_date_rows_are_skipped(self, case: tuple[Any, ...]) -> None:
        fetcher: Any = case[0]
        frame = case[4]()
        frame.loc[0, case[5]] = "not-a-date"

        rows = list(fetcher.transform_data(frame, fetcher.transform_query(**case[3])))

        assert [row.trade_date.isoformat() for row in rows if isinstance(row, Bar)] == [
            "2026-09-22"
        ]


class TestBondSinaCode:
    """The bond symbol spelling never guesses an exchange."""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("SH010107", "sh010107"),
            ("110059", "sh110059"),
            ("123456", "sz123456"),
            ("999999", "999999"),
        ],
    )
    def test_prefix_is_only_added_for_the_convertible_blocks(
        self, symbol: str, expected: str
    ) -> None:
        assert AkshareBondDailyFetcher.sina_code(symbol) == expected
