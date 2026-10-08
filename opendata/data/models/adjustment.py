"""Adjustment contract models (design §4.1)."""

from __future__ import annotations

import math
from datetime import date

from pydantic import model_validator

from opendata.data.models.base import ContractModel


class AdjustFactor(ContractModel):
    """Cumulative per-day adjustment factor.

    Legacy rows use ``raw_price * qfq_factor|hfq_factor``. ``affine-v1``
    rows use ``scale * raw_price + offset``; the old multiplication fields
    remain available for lineage and existing consumers. One row per
    (symbol, trade_date); event streams must be cumulated before emission.
    """

    symbol: str
    trade_date: date
    qfq_factor: float
    hfq_factor: float
    qfq_scale: float | None = None
    qfq_offset: float | None = None
    hfq_scale: float | None = None
    hfq_offset: float | None = None
    adjustment_version: str | None = None
    legacy_source: str | None = None

    @model_validator(mode="after")
    def validate_adjustment_coefficients(self) -> AdjustFactor:
        """Keep legacy and affine coefficient sets mutually exclusive."""
        for name, factor_value in (
            ("qfq_factor", self.qfq_factor),
            ("hfq_factor", self.hfq_factor),
        ):
            if not math.isfinite(factor_value) or factor_value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        coefficients = (self.qfq_scale, self.qfq_offset, self.hfq_scale, self.hfq_offset)
        if self.adjustment_version in (None, "legacy-multiplicative-v1"):
            if any(value is not None for value in coefficients):
                raise ValueError("legacy adjustment rows cannot contain affine coefficients")
            return self
        if self.adjustment_version != "affine-v1":
            raise ValueError(f"unknown adjustment version {self.adjustment_version!r}")
        if any(value is None for value in coefficients):
            raise ValueError("affine-v1 requires all four affine coefficients")
        qfq_scale = self.qfq_scale
        qfq_offset = self.qfq_offset
        hfq_scale = self.hfq_scale
        hfq_offset = self.hfq_offset
        if any(value is None for value in (qfq_scale, qfq_offset, hfq_scale, hfq_offset)):
            raise ValueError("affine-v1 requires all four affine coefficients")
        for name, coefficient in zip(
            ("qfq_scale", "qfq_offset", "hfq_scale", "hfq_offset"),
            (qfq_scale, qfq_offset, hfq_scale, hfq_offset),
            strict=True,
        ):
            if coefficient is None:
                raise ValueError("affine-v1 requires all four affine coefficients")
            if not math.isfinite(coefficient):
                raise ValueError(f"{name} must be finite")
        if qfq_scale is None or qfq_scale <= 0:
            raise ValueError("qfq_scale must be positive")
        if hfq_scale is None or hfq_scale <= 0:
            raise ValueError("hfq_scale must be positive")
        return self


class CorporateAction(ContractModel):
    """Corporate action event effective on the ex-date.

    All per-share quantities; cash amounts in CNY. A row with all zeros
    is invalid and must be filtered in ``normalize()``.
    """

    symbol: str
    ex_date: date
    cash_dividend: float = 0.0
    stock_dividend: float = 0.0
    rights_shares: float = 0.0
    rights_price: float = 0.0
