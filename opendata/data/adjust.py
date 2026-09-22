"""Adjusted-series synthesis from raw bars and factors (design D10).

The platform never stores adjusted prices: ``Bar`` holds unadjusted
OHLC, and adjusted series are synthesized here at query/merge time as
``raw price x factor``. Volume and amount are never adjusted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from opendata.data.models import AdjustFactor, Bar

_METHODS = ("qfq", "hfq", "none")
_PRICE_FIELDS = ("open", "high", "low", "close")


def apply_adjust(
    bars: Sequence[Bar],
    factors: Sequence[AdjustFactor],
    method: str,
) -> list[Bar]:
    """Synthesize an adjusted bar series from raw bars and factors.

    Fail-closed by design: every bar must have a matching factor row for
    its (symbol, trade_date), and duplicate factor rows are rejected.
    Callers with sparse (event-only) factor series must cumulate them
    before calling (see :class:`opendata.data.models.AdjustFactor`).

    Args:
        bars: Unadjusted bars; order is preserved.
        factors: One cumulative factor row per (symbol, trade_date).
        method: ``"qfq"`` (forward-adjusted), ``"hfq"`` (back-adjusted)
            or ``"none"`` (passthrough, D10 default).

    Returns:
        New ``Bar`` instances with adjusted OHLC; volume and amount are
        copied unchanged.

    Raises:
        ValueError: If the method is unknown, factor rows are duplicated
            or a bar has no matching factor row.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown adjust method {method!r}; expected one of {_METHODS}")
    if method == "none":
        return list(bars)

    factor_map: dict[tuple[str, date], float] = {}
    for factor in factors:
        key = (factor.symbol, factor.trade_date)
        if key in factor_map:
            raise ValueError(f"duplicate adjust factor for {key}")
        factor_map[key] = factor.qfq_factor if method == "qfq" else factor.hfq_factor

    adjusted: list[Bar] = []
    for bar in bars:
        key = (bar.symbol, bar.trade_date)
        if key not in factor_map:
            raise ValueError(f"missing adjust factor for {key}")
        ratio = factor_map[key]
        updates = {field: getattr(bar, field) * ratio for field in _PRICE_FIELDS}
        adjusted.append(bar.model_copy(update=updates))
    return adjusted
