"""Offline tests for Sina symbol spelling and shared AkShare normalization.

The accepted Sina stock-code families below are a verified subset, not a claim
of complete A-share coverage. In particular, the 689/CDR family remains
unclassified and is expected to fail closed until its source mapping is verified.
"""

from datetime import date

import pandas as pd
import pytest

import opendata.data.providers.akshare._vendor as akshare_vendor
from opendata.data.providers.akshare.models import AkshareFinancialStatementFetcher
from opendata.data.providers.akshare.models._normalize import (
    as_date,
    em_symbol,
    plain_symbol,
    sina_symbol,
    within_window,
)


def test_plain_symbol_removes_supported_exchange_suffix_and_outer_whitespace() -> None:
    assert plain_symbol(" 600519.SH ") == "600519"
    assert plain_symbol("000001.sz") == "000001"
    assert plain_symbol("830799.BJ") == "830799"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519", "600519.SH"),
        ("688981.SH", "688981.SH"),
        ("000001", "000001.SZ"),
        ("300750.SZ", "300750.SZ"),
        ("430047", "430047.BJ"),
        ("830799.BJ", "830799.BJ"),
    ],
)
def test_em_symbol_preserves_known_sh_sz_and_bj_prefix_rules(symbol: str, expected: str) -> None:
    assert em_symbol(symbol) == expected


def test_within_window_includes_both_bounds_and_rejects_outside_dates() -> None:
    start = date(2026, 1, 2)
    end = date(2026, 1, 4)

    assert within_window(start, start, end)
    assert within_window(end, start, end)
    assert not within_window(date(2026, 1, 1), start, end)
    assert not within_window(date(2026, 1, 5), start, end)


def test_within_window_supports_open_ended_bounds() -> None:
    assert within_window(date(2026, 1, 4), date(2026, 1, 2), None)
    assert not within_window(date(2026, 1, 1), date(2026, 1, 2), None)
    assert within_window(date(2026, 1, 2), None, date(2026, 1, 4))
    assert not within_window(date(2026, 1, 5), None, date(2026, 1, 4))
    assert within_window(date(2026, 1, 3), None, None)


def test_as_date_accepts_date_and_iso_string_and_returns_none_for_missing_or_invalid() -> None:
    expected = date(2026, 10, 27)

    assert as_date(expected) == expected
    assert as_date("2026-10-27") == expected
    assert as_date(None) is None
    assert as_date(float("nan")) is None
    assert as_date("not a date") is None


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519", "sh600519"),
        ("600519.SH", "sh600519"),
        ("600519.sh", "sh600519"),
        ("601398.SH", "sh601398"),
        ("603986", "sh603986"),
        ("605499", "sh605499"),
        ("688981.SH", "sh688981"),
        ("000001", "sz000001"),
        ("000001.sz", "sz000001"),
        ("001979.SZ", "sz001979"),
        ("002594", "sz002594"),
        ("003816.SZ", "sz003816"),
        ("300750", "sz300750"),
        ("301308.SZ", "sz301308"),
    ],
)
def test_sh_and_sz_symbols_keep_their_existing_sina_spellings(symbol: str, expected: str) -> None:
    assert sina_symbol(symbol) == expected


@pytest.mark.parametrize(
    "symbol",
    [
        "430047",
        "430047.SH",
        "430047.SZ",
        "830799",
        "830799.SH",
        "830799.SZ",
        "920000",
        "920000.SH",
        "920000.SZ",
        "920000.BJ",
        "920010",
        "920010.SH",
        "920010.SZ",
        "920992.BJ",
    ],
)
def test_beijing_symbols_are_rejected_without_guessing_a_sina_prefix(symbol: str) -> None:
    with pytest.raises(ValueError, match="cannot derive the sina symbol for Beijing"):
        sina_symbol(symbol)


@pytest.mark.parametrize("symbol", ["000001.SH", "300750.SH", "600519.SZ", "688981.SZ"])
def test_explicit_exchange_suffix_conflicts_are_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="Sina exchange suffix conflicts with code prefix"):
        sina_symbol(symbol)


@pytest.mark.parametrize(
    "symbol",
    [
        "689009",
        "999001",
        "510300",
        "159915",
        "60051",
        "600519.SH.extra",
        "600519.BJ",
        "600519.XYZ",
    ],
)
def test_unknown_families_and_malformed_spellings_fail_closed(symbol: str) -> None:
    with pytest.raises(ValueError, match="cannot derive the sina symbol"):
        sina_symbol(symbol)


@pytest.mark.parametrize(
    ("symbol", "error_message"),
    [
        ("430047", "cannot derive the sina symbol for Beijing"),
        ("830799", "cannot derive the sina symbol for Beijing"),
        ("920000", "cannot derive the sina symbol for Beijing"),
        ("920010", "cannot derive the sina symbol for Beijing"),
        ("999001", "unsupported A-share code"),
        ("000001.SH", "Sina exchange suffix conflicts with code prefix"),
        ("600519.SZ", "Sina exchange suffix conflicts with code prefix"),
    ],
)
def test_financial_fetcher_rejects_unsupported_symbols_before_calling_sina(
    monkeypatch: pytest.MonkeyPatch, symbol: str, error_message: str
) -> None:
    calls: list[dict[str, str]] = []

    def fake_report(*, stock: str, symbol: str) -> pd.DataFrame:
        calls.append({"stock": stock, "symbol": symbol})
        return pd.DataFrame()

    monkeypatch.setattr(akshare_vendor, "stock_financial_report_sina", fake_report)

    with pytest.raises(ValueError, match=error_message):
        AkshareFinancialStatementFetcher().fetch(symbol=symbol)

    assert calls == []
