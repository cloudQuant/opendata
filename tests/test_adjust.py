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

    def test_affine_adjustment_applies_scale_and_offset_to_ohlc(self):
        affine = [
            AdjustFactor(
                symbol="600519.SH",
                trade_date=trade_date,
                qfq_factor=0.8,
                hfq_factor=1.25,
                qfq_scale=1.0,
                qfq_offset=-5.0,
                hfq_scale=1.0,
                hfq_offset=5.0,
                adjustment_version="affine-v1",
            )
            for trade_date in (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5))
        ]

        adjusted = apply_adjust(make_bars(), affine, "qfq")

        assert adjusted[0].open == pytest.approx(95.0)
        assert adjusted[0].high == pytest.approx(105.0)
        assert adjusted[0].low == pytest.approx(85.0)
        assert adjusted[0].close == pytest.approx(96.0)
        assert adjusted[0].volume == make_bars()[0].volume
        assert adjusted[0].amount == make_bars()[0].amount

    def test_model_copy_cannot_bypass_affine_runtime_validation(self):
        factor = make_factors()[0].model_copy(
            update={"adjustment_version": "affine-v1", "qfq_scale": float("inf")}
        )
        with pytest.raises(ValueError, match="invalid affine adjustment coefficients"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_model_copy_cannot_bypass_legacy_factor_validation(self):
        factor = make_factors()[0].model_copy(update={"qfq_factor": 0.0})

        with pytest.raises(ValueError, match="invalid legacy adjustment factors"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_model_copy_non_numeric_legacy_factor_fails_closed(self):
        factor = make_factors()[0].model_copy(update={"qfq_factor": "invalid"})

        with pytest.raises(ValueError, match="invalid legacy adjustment factors"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_finite_factor_that_overflows_price_arithmetic_is_rejected(self):
        factor = make_factors()[0].model_copy(update={"qfq_factor": 1e308})

        with pytest.raises(ValueError, match="adjusted OHLC.*must be finite"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_finite_affine_scale_that_overflows_price_arithmetic_is_rejected(self):
        factor = AdjustFactor(
            symbol="600519.SH",
            trade_date=date(2024, 6, 3),
            qfq_factor=1.0,
            hfq_factor=1.0,
            qfq_scale=1e308,
            qfq_offset=0.0,
            hfq_scale=1.0,
            hfq_offset=0.0,
            adjustment_version="affine-v1",
        )

        with pytest.raises(ValueError, match="adjusted OHLC.*must be finite"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_model_copy_non_numeric_affine_coefficient_fails_closed(self):
        factor = make_factors()[0].model_copy(
            update={
                "adjustment_version": "affine-v1",
                "qfq_scale": "invalid",
                "qfq_offset": 0.0,
                "hfq_scale": 1.0,
                "hfq_offset": 0.0,
            }
        )

        with pytest.raises(ValueError, match="invalid affine adjustment coefficients"):
            apply_adjust(make_bars()[:1], [factor], "qfq")

    def test_same_symbol_cannot_mix_legacy_and_affine_rows(self):
        affine = AdjustFactor(
            symbol="600519.SH",
            trade_date=date(2024, 6, 4),
            qfq_factor=1.0,
            hfq_factor=1.0,
            qfq_scale=1.0,
            qfq_offset=-5.0,
            hfq_scale=1.0,
            hfq_offset=5.0,
            adjustment_version="affine-v1",
        )

        with pytest.raises(ValueError, match="mixed adjustment versions for 600519.SH"):
            apply_adjust(make_bars()[:2], [make_factors()[0], affine], "qfq")

    def test_symbols_can_use_different_adjustment_families(self):
        legacy_bar = make_bars()[0]
        affine_bar = legacy_bar.model_copy(update={"symbol": "000001.SZ"})
        affine_factor = AdjustFactor(
            symbol="000001.SZ",
            trade_date=legacy_bar.trade_date,
            qfq_factor=1.0,
            hfq_factor=1.0,
            qfq_scale=1.0,
            qfq_offset=-5.0,
            hfq_scale=1.0,
            hfq_offset=5.0,
            adjustment_version="affine-v1",
        )

        adjusted = apply_adjust([legacy_bar, affine_bar], [make_factors()[0], affine_factor], "qfq")

        assert adjusted[0].close == pytest.approx(legacy_bar.close * 0.8)
        assert adjusted[1].close == pytest.approx(affine_bar.close - 5.0)

    def test_none_and_explicit_legacy_versions_share_a_family(self):
        second_factor = make_factors()[1].model_copy(
            update={"adjustment_version": "legacy-multiplicative-v1"}
        )

        adjusted = apply_adjust(make_bars()[:2], [make_factors()[0], second_factor], "qfq")

        assert [bar.close for bar in adjusted] == [101.0 * 0.8, 102.0 * 0.9]

    def test_unmatched_factor_versions_do_not_affect_selected_bars(self):
        affine_unused = AdjustFactor(
            symbol="600519.SH",
            trade_date=date(2024, 6, 4),
            qfq_factor=1.0,
            hfq_factor=1.0,
            qfq_scale=1.0,
            qfq_offset=0.0,
            hfq_scale=1.0,
            hfq_offset=0.0,
            adjustment_version="affine-v1",
        )

        adjusted = apply_adjust(make_bars()[:1], [make_factors()[0], affine_unused], "qfq")

        assert adjusted[0].close == pytest.approx(make_bars()[0].close * 0.8)

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
