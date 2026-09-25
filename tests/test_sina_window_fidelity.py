"""The sina-backed akshare legs must honor the window they were asked for (C24).

Four akshare fetchers wrap upstream pages that return a symbol's whole
history and take no date argument - sina's futures, option and
convertible-bond daily lines and the sina dividend/rights pages. They
inherited the same hole: ``start_date``/``end_date`` validated, then ignored.
Measured on 2026-09-25, a request for the 16 trading days ending 2026-09-25
came back with 210 bars on ``futures_daily``, 107 on ``option_daily`` and
1,415 rows spanning six years on ``bond_daily``. So an incremental job that
names a window would rewrite history it never asked for, and a degradation
to one of these legs would not be answering the same question.

The fix lives in the normalize stage (:func:`within_window`), so these tests
stub the extraction stage with a wide frame and assert on what ``fetch``
hands back: no row outside the window, and - the other half of the contract
- the full history still comes back when no window was requested.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from opendata.data.providers import register_providers
from opendata.data.providers.akshare.models._normalize import within_window
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest import MonkeyPatch

    from opendata.data.protocol import Fetcher

#: Inclusive bounds of the window every case asks for.
WINDOW_START = date(2026, 9, 7)
WINDOW_END = date(2026, 9, 18)

#: Every calendar day of September 2026 - the synthetic upstream's span.
FRAME_DAYS = tuple(date(2026, 9, day) for day in range(1, 31))

#: The days of that frame inside the window (the expected answer).
IN_WINDOW_DAYS = tuple(day for day in FRAME_DAYS if WINDOW_START <= day <= WINDOW_END)

#: Chinese spelling of a sina daily line (the option page).
_CN_COLUMNS = ("日期", "开盘", "最高", "最低", "收盘", "成交量")
#: English spelling of a sina daily line (the futures and bond pages).
_EN_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _daily_frame(columns: tuple[str, ...]) -> pd.DataFrame:
    """One daily-line row per September day under the given column names."""
    rows = []
    for index, day in enumerate(FRAME_DAYS):
        values = (
            day.isoformat(),
            float(index),
            float(index) + 1.0,
            float(index) - 1.0,
            float(index),
            float(index) * 10.0,
        )
        rows.append(dict(zip(columns, values, strict=True)))
    return pd.DataFrame(rows)


def _action_frame() -> pd.DataFrame:
    """A sina 分红 page: one dividend row per September day."""
    return pd.DataFrame(
        [
            {
                "除权除息日": day.isoformat(),
                "派息": 1.0,
                "送股": 0.0,
                "转增": 0.0,
                "indicator": "分红",
            }
            for day in FRAME_DAYS
        ]
    )


#: Legs that read a request window but whose upstream cannot: they must crop
#: in normalize. ``(domain, contract date field, frame factory)``; the set is
#: pinned by :func:`test_every_akshare_leg_is_classified_by_window_handling`.
WINDOW_BLIND_LEGS: tuple[tuple[str, str, Callable[[], pd.DataFrame]], ...] = (
    ("futures_daily", "trade_date", lambda: _daily_frame(_EN_COLUMNS)),
    ("option_daily", "trade_date", lambda: _daily_frame(_CN_COLUMNS)),
    ("bond_daily", "trade_date", lambda: _daily_frame(_EN_COLUMNS)),
    ("stock_action", "ex_date", _action_frame),
)

#: The three eastmoney kline routes push the window upstream (``beg``/``end``),
#: so normalize sees an already-cropped frame.
UPSTREAM_WINDOWED_LEGS = frozenset({"stock_daily", "index_daily", "fund_etf_daily"})

#: Report-period and snapshot tables: a request window is not how they are
#: keyed, so extraction naming no window is the expected shape, not a hole.
PERIOD_KEYED_LEGS = frozenset({"financial_indicator", "financial_statement", "index_constituent"})

LEG_CASES = pytest.mark.parametrize(
    ("domain", "date_attr", "frame_factory"),
    WINDOW_BLIND_LEGS,
    ids=[leg[0] for leg in WINDOW_BLIND_LEGS],
)


def _fetcher(domain: str) -> Fetcher[Any, Any]:
    """The akshare fetcher the live registry holds for one domain."""
    register_providers()
    registry = get_registry()
    capability = next(cap for cap in registry.capabilities() if cap.domain == domain)
    return registry.resolve(capability.asset_class, domain, source="akshare")


def _stubbed(monkeypatch: MonkeyPatch, domain: str, frame: pd.DataFrame) -> Fetcher[Any, Any]:
    """Point one fetcher's extraction at a recorded frame, for this test only."""
    fetcher = _fetcher(domain)
    monkeypatch.setattr(fetcher, "extract_data", lambda params, ctx: frame)
    return fetcher


@LEG_CASES
def test_a_named_window_crops_the_whole_history(
    monkeypatch: MonkeyPatch,
    domain: str,
    date_attr: str,
    frame_factory: Callable[[], pd.DataFrame],
) -> None:
    """A windowed request returns exactly its window - sina handed over the rest."""
    fetcher = _stubbed(monkeypatch, domain, frame_factory())

    rows = list(fetcher.fetch(symbol="10011425", start_date=WINDOW_START, end_date=WINDOW_END))

    days = [getattr(row, date_attr) for row in rows]
    assert days == sorted(days), f"{domain} must stay date-ascending"
    assert set(days) == set(IN_WINDOW_DAYS), (
        f"{domain} answered {len(days)} rows for a {len(IN_WINDOW_DAYS)}-day window"
    )


@LEG_CASES
def test_an_open_request_still_returns_the_full_history(
    monkeypatch: MonkeyPatch,
    domain: str,
    date_attr: str,
    frame_factory: Callable[[], pd.DataFrame],
) -> None:
    """With no window given, the crop must not invent a range of its own."""
    fetcher = _stubbed(monkeypatch, domain, frame_factory())

    rows = list(fetcher.fetch(symbol="10011425"))

    assert len(rows) == len(FRAME_DAYS)


def test_one_sided_windows_bound_only_the_side_given(monkeypatch: MonkeyPatch) -> None:
    """Each bound is independent: naming one must not close the other."""
    fetcher = _stubbed(monkeypatch, "futures_daily", _daily_frame(_EN_COLUMNS))

    tail = [bar.trade_date for bar in fetcher.fetch(symbol="CU2611", start_date=WINDOW_END)]
    head = [bar.trade_date for bar in fetcher.fetch(symbol="CU2611", end_date=WINDOW_START)]

    assert tail == [day for day in FRAME_DAYS if day >= WINDOW_END]
    assert head == [day for day in FRAME_DAYS if day <= WINDOW_START]


def test_within_window_bounds_are_inclusive() -> None:
    """Both ends are inside the window; a None side leaves it open."""
    assert within_window(date(2026, 9, 7), date(2026, 9, 7), date(2026, 9, 18))
    assert within_window(date(2026, 9, 18), date(2026, 9, 7), date(2026, 9, 18))
    assert not within_window(date(2026, 9, 6), date(2026, 9, 7), date(2026, 9, 18))
    assert not within_window(date(2026, 9, 19), date(2026, 9, 7), date(2026, 9, 18))
    assert within_window(date(2026, 9, 19), None, None)


def test_every_akshare_leg_is_classified_by_window_handling() -> None:
    """A new akshare leg must be triaged here, not born silently window-blind.

    The sets are computed from the live registry, which is what keeps the
    cases above from going stale: registering another akshare capability
    whose extraction never names the window fails until it is either filtered
    (add it to :data:`WINDOW_BLIND_LEGS`), pushed upstream, or shown to be a
    period-keyed table that has no request window to honor.
    """
    import inspect

    register_providers()
    registry = get_registry()
    akshare_domains = {cap.domain for cap in registry.capabilities() if cap.source == "akshare"}
    window_blind = {
        domain
        for domain in akshare_domains
        if "start_date" not in inspect.getsource(_fetcher(domain).extract_data)
    }

    assert window_blind == {leg[0] for leg in WINDOW_BLIND_LEGS} | PERIOD_KEYED_LEGS
    assert akshare_domains == window_blind | UPSTREAM_WINDOWED_LEGS
