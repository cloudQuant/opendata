"""Adjusted-series synthesis from raw bars and factors (design D10).

The platform never stores adjusted prices: ``Bar`` holds unadjusted
OHLC, and adjusted series are synthesized here at query/merge time from
legacy multiplication factors or versioned affine coefficients. Volume
and amount are never adjusted.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from opendata.data.models import AdjustFactor, Bar

_METHODS = ("qfq", "hfq", "none")
_PRICE_FIELDS = ("open", "high", "low", "close")


def _is_finite_number(value: object) -> bool:
    """Check runtime values defensively, including ``model_copy`` updates."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


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
        ValueError: If the method is unknown, factor rows are duplicated,
            affine coefficients are invalid, or a bar has no factor row.
    """
    if method not in _METHODS:
        raise ValueError(f"unknown adjust method {method!r}; expected one of {_METHODS}")
    if method == "none":
        return list(bars)

    factor_map: dict[tuple[str, date], AdjustFactor] = {}
    for factor in factors:
        key = (factor.symbol, factor.trade_date)
        if key in factor_map:
            raise ValueError(f"duplicate adjust factor for {key}")
        factor_map[key] = factor

    adjusted: list[Bar] = []
    adjustment_family_by_symbol: dict[str, str] = {}
    for bar in bars:
        key = (bar.symbol, bar.trade_date)
        if key not in factor_map:
            raise ValueError(f"missing adjust factor for {key}")
        factor = factor_map[key]
        if factor.adjustment_version in (None, "legacy-multiplicative-v1"):
            family = "legacy-multiplicative-v1"
        elif factor.adjustment_version == "affine-v1":
            family = "affine-v1"
        else:
            raise ValueError(f"unknown adjustment version {factor.adjustment_version!r} for {key}")
        prior_family = adjustment_family_by_symbol.setdefault(bar.symbol, family)
        if prior_family != family:
            raise ValueError(
                f"mixed adjustment versions for {bar.symbol}: {prior_family} and {family}"
            )
        if (
            not _is_finite_number(factor.qfq_factor)
            or not _is_finite_number(factor.hfq_factor)
            or factor.qfq_factor <= 0
            or factor.hfq_factor <= 0
        ):
            raise ValueError(f"invalid legacy adjustment factors for {key}")
        if family == "legacy-multiplicative-v1":
            if any(
                value is not None
                for value in (
                    factor.qfq_scale,
                    factor.qfq_offset,
                    factor.hfq_scale,
                    factor.hfq_offset,
                )
            ):
                raise ValueError(f"legacy adjustment row for {key} contains affine coefficients")
            ratio = factor.qfq_factor if method == "qfq" else factor.hfq_factor
            updates = {field: getattr(bar, field) * ratio for field in _PRICE_FIELDS}
        else:
            scale = factor.qfq_scale if method == "qfq" else factor.hfq_scale
            offset = factor.qfq_offset if method == "qfq" else factor.hfq_offset
            if (
                scale is None
                or offset is None
                or factor.qfq_scale is None
                or factor.qfq_offset is None
                or factor.hfq_scale is None
                or factor.hfq_offset is None
                or not all(
                    _is_finite_number(value)
                    for value in (
                        factor.qfq_scale,
                        factor.qfq_offset,
                        factor.hfq_scale,
                        factor.hfq_offset,
                    )
                )
                or factor.qfq_scale <= 0
                or factor.hfq_scale <= 0
            ):
                raise ValueError(f"invalid affine adjustment coefficients for {key}")
            updates = {field: getattr(bar, field) * scale + offset for field in _PRICE_FIELDS}
        if not all(math.isfinite(value) for value in updates.values()):
            raise ValueError(f"adjusted OHLC for {key} must be finite")
        adjusted.append(bar.model_copy(update=updates))
    return adjusted
