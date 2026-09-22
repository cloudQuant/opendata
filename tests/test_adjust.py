"""Adjusted-series synthesis tests (design D10, AC-2).

Math exactness against hand-computed series; the comparison against
akshare's official qfq series needs a recorded fixture and is tracked
as an A1 open item (evidence README).
"""

from datetime import date

import pytest

from opendata.data.adjust import apply_adjust
from opendata.data.models import AdjustFactor, Bar


def make_bars():
    """Three raw bars with distinguishable OHLC values."""
    return [
        Bar(
            symbol="600519.SH",
            trade_date=trade_date,
            open=100.0,
            high=110.0,
            low=90.0,
            close=100.0 + index,
            volume=1000.0,
            amount=100000.0,
        )
        for index, trade_date in enumerate(
            (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)), start=1
        )
    ]


def make_factors():
    """Factors anchored per the model contract (qfq latest = 1, hfq earliest = 1)."""
    return [
        AdjustFactor(
            symbol="600519.SH",
            trade_date=trade_date,
            qfq_factor=qfq,
            hfq_factor=hfq,
        )
        for trade_date, qfq, hfq in (
            (date(2024, 6, 3), 0.8, 1.0),
            (date(2024, 6, 4), 0.9, 1.125),
            (date(2024, 6, 5), 1.0, 1.25),
        )
    ]


class TestApplyAdjust:
    def test_qfq_math_is_exact(self):
        adjusted = apply_adjust(make_bars(), make_factors(), "qfq")
        assert [bar.close for bar in adjusted] == [
            101.0 * 0.8,
            102.0 * 0.9,
            103.0 * 1.0,
        ]
        assert adjusted[0].open == pytest.approx(80.0)
        assert adjusted[0].high == pytest.approx(88.0)
        assert adjusted[0].low == pytest.approx(72.0)

    def test_hfq_math_is_exact(self):
        adjusted = apply_adjust(make_bars(), make_factors(), "hfq")
        assert [bar.close for bar in adjusted] == [
            101.0 * 1.0,
            102.0 * 1.125,
            103.0 * 1.25,
        ]

    def test_volume_and_amount_never_adjusted(self):
        raw = make_bars()
        adjusted = apply_adjust(raw, make_factors(), "qfq")
        for before, after in zip(raw, adjusted, strict=True):
            assert after.volume == before.volume
            assert after.amount == before.amount

    def test_none_is_passthrough(self):
        raw = make_bars()
        assert apply_adjust(raw, make_factors(), "none") == raw

    def test_input_bars_not_mutated(self):
        raw = make_bars()
        apply_adjust(raw, make_factors(), "qfq")
        assert raw[0].close == 101.0

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError, match="unknown adjust method"):
            apply_adjust(make_bars(), make_factors(), "rqfq")

    def test_missing_factor_fails_closed(self):
        factors = make_factors()[:-1]
        with pytest.raises(ValueError, match="missing adjust factor"):
            apply_adjust(make_bars(), factors, "qfq")

    def test_duplicate_factor_fails_closed(self):
        factors = make_factors() + [make_factors()[0]]
        with pytest.raises(ValueError, match="duplicate adjust factor"):
            apply_adjust(make_bars(), factors, "qfq")

    def test_symbols_do_not_cross_contaminate(self):
        other = make_bars()[0].model_copy(update={"symbol": "000001.SZ"})
        with pytest.raises(ValueError, match="missing adjust factor"):
            apply_adjust([*make_bars(), other], make_factors(), "qfq")

    def test_order_preserved(self):
        raw = list(reversed(make_bars()))
        adjusted = apply_adjust(raw, make_factors(), "qfq")
        assert [bar.trade_date for bar in adjusted] == [bar.trade_date for bar in raw]
