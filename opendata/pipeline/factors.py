"""Cumulative adjustment factors from corporate actions (design D10).

The platform never stores adjusted prices; ``adjust=qfq|hfq`` is
synthesized at query time from ``Bar x AdjustFactor``. This module is
the other half of that contract: it turns the event stream (cash
dividends, stock bonuses, rights issues) plus the unadjusted close
series into the per-day cumulative factor rows.

The ratio of one event on its ex-date is the value-equivalence of
holding one share through the event (design §8.1):

    reference = (prev_close - cash_dividend + allotment_ratio * allotment_price)
                / (1 + bonus + allotment_ratio)
    ratio     = prev_close / reference

* **hfq** anchors the earliest price at 1.0: the factor is the running
  product of the ratios of every event up to that date.
* **qfq** anchors the latest price at 1.0: the factor is the running
  product of the *inverse* ratios of every event after that date.

``prev_close`` is the last close before the ex-date; without it the
event cannot be priced (the ratio needs the price the event acts on),
so cumulation fails closed for events whose previous close is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from opendata.data.models import AdjustFactor

#: Sanity band of an event ratio; outside it the event is refused
#: rather than producing a nonsense factor (fail closed).
_MIN_RATIO = 0.1
_MAX_RATIO = 10.0
_ZERO_TOLERANCE = 1e-12


@dataclass(frozen=True)
class FactorEvent:
    """One corporate action as the factor math needs it.

    Attributes:
        symbol: Symbol identifier.
        ex_date: Ex-date the event is effective on.
        cash_dividend: Cash dividend per share (CNY).
        bonus: Stock dividend, shares per share held.
        allotment_ratio: Rights shares per share held.
        allotment_price: Price paid per rights share (CNY).
    """

    symbol: str
    ex_date: date
    cash_dividend: float = 0.0
    bonus: float = 0.0
    allotment_ratio: float = 0.0
    allotment_price: float = 0.0


def event_ratio(event: FactorEvent, prev_close: float) -> float:
    """Compute the price ratio of one event.

    Args:
        event: The corporate action.
        prev_close: Last close before the ex-date.

    Returns:
        The ratio to apply to prices after the event (hfq direction).

    Raises:
        ValueError: The previous close is not positive, or the ratio
            lands outside the sanity band (a dividend larger than the
            price or a nonsense rights price must fail closed).
    """
    if prev_close <= _ZERO_TOLERANCE:
        raise ValueError(f"{event.symbol}: non-positive prev_close {prev_close}")
    denominator = prev_close - event.cash_dividend + event.allotment_ratio * event.allotment_price
    if denominator <= _ZERO_TOLERANCE:
        raise ValueError(
            f"{event.symbol} @ {event.ex_date}: event payout exceeds prev_close "
            f"{prev_close}; cannot compute a factor"
        )
    ratio = prev_close * (1.0 + event.bonus + event.allotment_ratio) / denominator
    if not (_MIN_RATIO <= ratio <= _MAX_RATIO):
        raise ValueError(
            f"{event.symbol} @ {event.ex_date}: ratio {ratio:.6f} outside "
            f"[{_MIN_RATIO}, {_MAX_RATIO}]; refusing a nonsense factor"
        )
    return ratio


def cumulate_factors(
    events: Iterable[FactorEvent],
    closes: Sequence[tuple[str, date, float]],
    *,
    trading_days: Sequence[date] | None = None,
) -> list[AdjustFactor]:
    """Cumulate events into per-day qfq/hfq factors.

    Args:
        events: Corporate actions, any order.
        closes: ``(symbol, trade_date, close)`` rows of the unadjusted
            series (used to find each event's previous close).
        trading_days: Extra days to emit factor rows for (for example
            the ex-date itself when that day's bar comes from another
            source than ``closes``).

    Returns:
        One ``AdjustFactor`` per (symbol, trade_date), ordered by
        symbol then date.

    Raises:
        ValueError: An event has no previous close, or its ratio is
            outside the sanity band (fail closed).
    """
    close_by_symbol: dict[str, dict[date, float]] = {}
    symbols: set[str] = set()
    for symbol, day, close in closes:
        close_by_symbol.setdefault(symbol, {})[day] = close
        symbols.add(symbol)

    by_symbol: dict[str, list[FactorEvent]] = {}
    for event in events:
        by_symbol.setdefault(event.symbol, []).append(event)
        symbols.add(event.symbol)

    factors: list[AdjustFactor] = []
    for symbol in sorted(symbols):
        symbol_events = sorted(by_symbol.get(symbol, ()), key=lambda event: event.ex_date)
        symbol_closes = close_by_symbol.get(symbol, {})
        ratios: dict[date, float] = {}
        for event in symbol_events:
            previous = _previous_close(symbol_closes, event.ex_date)
            if previous is None:
                raise ValueError(
                    f"{symbol}: no close before ex-date {event.ex_date}; "
                    "cannot compute the event ratio"
                )
            ratios[event.ex_date] = event_ratio(event, previous)
        days = sorted(set(symbol_closes) | set(trading_days or ()))
        factors.extend(_factors_for_symbol(symbol, symbol_events, ratios, days))
    return factors


def _previous_close(closes: dict[date, float], ex_date: date) -> float | None:
    """Return the last close strictly before an ex-date, if any.

    Args:
        closes: The symbol's close series.
        ex_date: The ex-date.

    Returns:
        The close of the latest day before ``ex_date``, or None.
    """
    candidates = [day for day in closes if day < ex_date]
    if not candidates:
        return None
    return closes[max(candidates)]


def _factors_for_symbol(
    symbol: str,
    events: Sequence[FactorEvent],
    ratios: dict[date, float],
    days: Sequence[date],
) -> list[AdjustFactor]:
    """Emit one factor row per day for one symbol.

    ``qfq_factor`` is the product of the inverse ratios of every event
    strictly after the day (1.0 at the latest day); ``hfq_factor`` is
    the product of the ratios of every event on or before the day (1.0
    before the first event).

    Args:
        symbol: Symbol identifier.
        events: The symbol's events in ex-date order.
        ratios: Event ratio by ex-date.
        days: Days to emit rows for (sorted).

    Returns:
        The factor rows, one per day.
    """
    from opendata.data.models import AdjustFactor

    if not days:
        return []
    count = len(events)
    suffix = [1.0] * (count + 1)
    for index in range(count - 1, -1, -1):
        suffix[index] = suffix[index + 1] / ratios[events[index].ex_date]

    rows: list[AdjustFactor] = []
    hfq = 1.0
    next_event = 0
    for day in days:
        while next_event < count and events[next_event].ex_date <= day:
            hfq *= ratios[events[next_event].ex_date]
            next_event += 1
        rows.append(
            AdjustFactor(
                symbol=symbol,
                trade_date=day,
                qfq_factor=round(suffix[next_event], 12),
                hfq_factor=round(hfq, 12),
            )
        )
    return rows
