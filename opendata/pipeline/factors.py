"""Cumulative corporate-action adjustment factors (design D10).

Adjusted OHLC is synthesized at query time. Legacy rows (a missing
``adjustment_version`` or ``legacy-multiplicative-v1``) use
``qfq_factor`` and ``hfq_factor`` as multiplication ratios. Those fields
remain on ``affine-v1`` rows for historical lineage; affine queries use
the corresponding ``qfq_scale``/``qfq_offset`` or
``hfq_scale``/``hfq_offset`` to calculate ``scale * raw_price + offset``.
The query path refuses a selected series that mixes legacy and affine
rows for the same symbol.

For lineage, the legacy ratio of one event on its ex-date is the
value-equivalence of holding one share through the event (design §8.1)::

    reference = (prev_close - cash_dividend + allotment_ratio * allotment_price)
                / (1 + bonus + allotment_ratio)
    ratio     = prev_close / reference

An affine event maps its pre-event price to its post-event price as
``T(x) = a*x + b``, where ``a = 1 / (1 + bonus + allotment_ratio)`` and
``b`` is the event's net cash and rights-price amount divided by
``1 + bonus + allotment_ratio``. Composition follows function order:
``(a2, b2) o (a1, b1) = (a2*a1, a2*b1+b2)``.

* **qfq** composes forward event transforms strictly after a bar's date
  through that symbol's last available bar. The last bar is the identity
  anchor, with scale 1 and offset 0.
* **hfq** composes inverse event transforms after that symbol's first
  available bar through the bar's date. The first bar is the identity
  anchor, with scale 1 and offset 0.

``prev_close`` is the last close before an ex-date; without it the event
cannot be priced, so cumulation fails closed when a required previous
close is unknown.
"""

from __future__ import annotations

import math
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
        event_key: Source payload digest retained only for traceability. It
            is not an economic event identity.
        source: Source name when supplied by the version store.
        event_identity: Stable source-scoped identity; never inferred from a
            payload digest.
        economic_event_id: Reviewed cross-source identity used to recognize
            mirrors of the same event.
        pre_event_share_basis: Explicit identifier for the shared per-share
            basis used by same-day event components.
    """

    symbol: str
    ex_date: date
    cash_dividend: float = 0.0
    bonus: float = 0.0
    allotment_ratio: float = 0.0
    allotment_price: float = 0.0
    event_key: str | None = None
    source: str | None = None
    event_identity: str | None = None
    economic_event_id: str | None = None
    pre_event_share_basis: str | None = None
    source_revision: str | None = None
    content_digest: str | None = None


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
    validate_event(event)
    if not math.isfinite(prev_close) or prev_close <= _ZERO_TOLERANCE:
        raise ValueError(f"{event.symbol}: non-finite or non-positive prev_close {prev_close}")
    if event.cash_dividend > prev_close:
        raise ValueError(
            f"{event.symbol} @ {event.ex_date}: event payout exceeds prev_close "
            f"{prev_close}; cannot compute a factor"
        )
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


def validate_event(event: FactorEvent) -> None:
    """Reject malformed event quantities before either adjustment math."""
    values = {
        "cash_dividend": event.cash_dividend,
        "bonus": event.bonus,
        "allotment_ratio": event.allotment_ratio,
        "allotment_price": event.allotment_price,
    }
    for name, value in values.items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(
                f"{event.symbol} @ {event.ex_date}: {name} must be finite and non-negative"
            )


def coalesce_same_day_events(events: Iterable[FactorEvent]) -> list[FactorEvent]:
    """Combine explicitly identified same-day actions on one share basis.

    One event per date remains the legacy path. Multiple actions are
    combined only when each row has a stable identity and the same explicit
    pre-event share basis. Repeated source rows and reviewed cross-source
    mirrors are deduplicated; ambiguous mirrors and unreviewed legacy rows
    fail closed. Cash, shares, and rights subscription amounts are summed
    independently, then an equivalent rights price is derived from the
    aggregate rights amount.
    """
    grouped: dict[tuple[str, date], list[FactorEvent]] = {}
    for event in events:
        grouped.setdefault((event.symbol, event.ex_date), []).append(event)

    combined: list[FactorEvent] = []
    for (symbol, ex_date), same_day in sorted(grouped.items()):
        for event in same_day:
            validate_event(event)
        if len(same_day) == 1:
            combined.append(same_day[0])
            continue

        if any(not event.event_identity or not event.event_identity.strip() for event in same_day):
            raise ValueError(f"{symbol} @ {ex_date}: ambiguous multiple corporate actions")
        bases = {event.pre_event_share_basis for event in same_day}
        if len(bases) != 1 or None in bases or "" in bases:
            raise ValueError(
                f"{symbol} @ {ex_date}: same-day actions need one explicit pre-event share basis"
            )

        unique: list[FactorEvent] = []
        for candidate in same_day:
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(unique)
                    if _same_event_identity(existing, candidate)
                ),
                None,
            )
            if duplicate_index is None:
                if any(
                    _cross_source_without_reviewed_mirror(existing, candidate)
                    for existing in unique
                ):
                    raise ValueError(
                        f"{symbol} @ {ex_date}: cross-source same-day action identity is unresolved"
                    )
                unique.append(candidate)
                continue
            existing = unique[duplicate_index]
            if not _same_event_facts(existing, candidate):
                raise ValueError(
                    f"{symbol} @ {ex_date}: one corporate action identity has conflicting payloads"
                )

        if len(unique) == 1:
            combined.append(unique[0])
            continue

        rights_shares = sum(event.allotment_ratio for event in unique)
        rights_amount = sum(event.allotment_ratio * event.allotment_price for event in unique)
        combined.append(
            FactorEvent(
                symbol=symbol,
                ex_date=ex_date,
                cash_dividend=sum(event.cash_dividend for event in unique),
                bonus=sum(event.bonus for event in unique),
                allotment_ratio=rights_shares,
                allotment_price=(rights_amount / rights_shares if rights_shares else 0.0),
                pre_event_share_basis=next(iter(bases)),
            )
        )
    return combined


def _same_event_identity(left: FactorEvent, right: FactorEvent) -> bool:
    """Whether two observations carry an explicitly equivalent identity."""
    if left.source == right.source and left.event_identity == right.event_identity:
        return True
    return bool(
        left.source != right.source
        and left.economic_event_id
        and left.economic_event_id == right.economic_event_id
    )


def _same_event_facts(left: FactorEvent, right: FactorEvent) -> bool:
    """Compare values that affect event math and the shared basis."""
    return (
        left.symbol,
        left.ex_date,
        left.cash_dividend,
        left.bonus,
        left.allotment_ratio,
        left.allotment_price,
        left.pre_event_share_basis,
    ) == (
        right.symbol,
        right.ex_date,
        right.cash_dividend,
        right.bonus,
        right.allotment_ratio,
        right.allotment_price,
        right.pre_event_share_basis,
    )


def _cross_source_without_reviewed_mirror(left: FactorEvent, right: FactorEvent) -> bool:
    """Refuse cross-source components unless their relationship was reviewed."""
    if left.source is None or right.source is None or left.source == right.source:
        return False
    return not left.economic_event_id or not right.economic_event_id


def _event_transform(event: FactorEvent) -> tuple[float, float]:
    """Return the affine transform from pre-event to post-event prices."""
    share_count = 1.0 + event.bonus + event.allotment_ratio
    if share_count <= _ZERO_TOLERANCE:
        raise ValueError(f"{event.symbol} @ {event.ex_date}: invalid share count")
    scale = 1.0 / share_count
    offset = (-event.cash_dividend + event.allotment_ratio * event.allotment_price) / share_count
    if not math.isfinite(scale) or not math.isfinite(offset) or scale <= 0:
        raise ValueError(f"{event.symbol} @ {event.ex_date}: invalid affine event transform")
    return scale, offset


def _compose(outer: tuple[float, float], inner: tuple[float, float]) -> tuple[float, float]:
    """Compose affine transforms, returning ``outer(inner(x))``."""
    outer_scale, outer_offset = outer
    inner_scale, inner_offset = inner
    return (
        outer_scale * inner_scale,
        outer_scale * inner_offset + outer_offset,
    )


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
        symbol_closes = close_by_symbol.setdefault(symbol, {})
        if day in symbol_closes:
            raise ValueError(f"duplicate close for {(symbol, day)}")
        if not math.isfinite(close) or close <= 0:
            raise ValueError(f"{symbol} @ {day}: close must be finite and positive")
        symbol_closes[day] = close
        symbols.add(symbol)

    by_symbol: dict[str, list[FactorEvent]] = {}
    for event in coalesce_same_day_events(events):
        symbol_events = by_symbol.setdefault(event.symbol, [])
        symbol_events.append(event)
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
        factors.extend(_factors_for_symbol(symbol, symbol_events, ratios, days, symbol_closes))
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
    closes: dict[date, float],
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
        closes: Available raw closes defining the affine anchor range.

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
    first_day = min(closes) if closes else days[0]
    last_day = max(closes) if closes else days[-1]
    affine_events = [
        (event, _event_transform(event))
        for event in events
        if first_day < event.ex_date <= last_day
    ]
    affine_suffix = [(1.0, 0.0)] * (len(affine_events) + 1)
    for index in range(len(affine_events) - 1, -1, -1):
        affine_suffix[index] = _compose(affine_suffix[index + 1], affine_events[index][1])
    hfq = 1.0
    next_event = 0
    affine_hfq = (1.0, 0.0)
    next_affine_event = 0
    for day in days:
        while next_event < count and events[next_event].ex_date <= day:
            hfq *= ratios[events[next_event].ex_date]
            next_event += 1
        while (
            next_affine_event < len(affine_events)
            and affine_events[next_affine_event][0].ex_date <= day
        ):
            event_scale, event_offset = affine_events[next_affine_event][1]
            inverse = (1.0 / event_scale, -event_offset / event_scale)
            affine_hfq = _compose(affine_hfq, inverse)
            next_affine_event += 1
        qfq_transform = affine_suffix[next_affine_event]

        rows.append(
            AdjustFactor(
                symbol=symbol,
                trade_date=day,
                qfq_factor=round(suffix[next_event], 12),
                hfq_factor=round(hfq, 12),
                qfq_scale=qfq_transform[0],
                qfq_offset=qfq_transform[1],
                hfq_scale=affine_hfq[0],
                hfq_offset=affine_hfq[1],
                adjustment_version="affine-v1",
                legacy_source="corporate-action-ratio-v1",
            )
        )
    return rows
