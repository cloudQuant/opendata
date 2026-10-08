"""Cumulative factor tests (AC-2/AC-11, design D10).

The factor math is verified against hand-computed examples: a cash
dividend, a stock bonus, a rights issue, a multi-event chain, and the
fail-closed paths (missing previous close, degenerate event).
"""

from __future__ import annotations

from datetime import date

import pytest

from opendata.pipeline.factors import FactorEvent, cumulate_factors, event_ratio


def _d(day: int) -> date:
    return date(2026, 1, day)


def _events(*items) -> list[FactorEvent]:
    return [
        FactorEvent(
            symbol=symbol,
            ex_date=ex_date,
            cash_dividend=cash,
            bonus=bonus,
            allotment_ratio=allotment_ratio,
            allotment_price=allotment_price,
        )
        for symbol, ex_date, cash, bonus, allotment_ratio, allotment_price in items
    ]


def _closes(symbol: str, days: list[tuple[int, float]]) -> list[tuple[str, date, float]]:
    return [(symbol, _d(day), close) for day, close in days]


class TestEventRatio:
    def test_cash_dividend_only(self):
        # prev 10, dividend 0.5: reference = 9.5, ratio = 10/9.5
        event = FactorEvent("600519", _d(10), cash_dividend=0.5)

        assert event_ratio(event, 10.0) == pytest.approx(10.0 / 9.5)

    def test_stock_bonus_only(self):
        # prev 10, bonus 0.1 (10送1): reference = 10/1.1, ratio = 1.1
        event = FactorEvent("600519", _d(10), bonus=0.1)

        assert event_ratio(event, 10.0) == pytest.approx(1.1)

    def test_rights_issue(self):
        # prev 10, 10配3 @ 5: reference = (10 - 0 + 0.3*5)/1.3 = 11.5/1.3
        event = FactorEvent("600519", _d(10), allotment_ratio=0.3, allotment_price=5.0)

        assert event_ratio(event, 10.0) == pytest.approx(10.0 * 1.3 / 11.5)

    def test_non_positive_prev_close_fails_closed(self):
        event = FactorEvent("600519", _d(10), cash_dividend=0.5)

        with pytest.raises(ValueError, match="prev_close"):
            event_ratio(event, 0.0)

    def test_non_finite_or_negative_event_inputs_fail_closed(self):
        for event in (
            FactorEvent("600519", _d(10), cash_dividend=float("nan")),
            FactorEvent("600519", _d(10), bonus=-0.1),
            FactorEvent("600519", _d(10), allotment_ratio=0.1, allotment_price=-1.0),
        ):
            with pytest.raises(ValueError, match="finite and non-negative"):
                event_ratio(event, 10.0)

    def test_payout_beyond_the_price_fails_closed(self):
        event = FactorEvent("600519", _d(10), cash_dividend=20.0)

        with pytest.raises(ValueError, match="payout exceeds"):
            event_ratio(event, 10.0)

    def test_cash_above_close_fails_even_when_rights_funding_would_offset_it(self):
        event = FactorEvent(
            "600519", _d(10), cash_dividend=11.0, allotment_ratio=0.2, allotment_price=100.0
        )

        with pytest.raises(ValueError, match="payout exceeds"):
            event_ratio(event, 10.0)

    def test_nonsense_ratio_fails_closed(self):
        event = FactorEvent("600519", _d(10), bonus=100.0)

        with pytest.raises(ValueError, match="outside"):
            event_ratio(event, 10.0)


class TestCumulate:
    def test_no_events_produces_unit_factors(self):
        factors = cumulate_factors([], _closes("600519", [(1, 10.0), (2, 10.0)]))

        assert len(factors) == 2
        assert all(f.qfq_factor == 1.0 and f.hfq_factor == 1.0 for f in factors)

    def test_cash_dividend_anchors_both_ends(self):
        # event on day 10 (dividend 1.0); the previous close is day 5's
        # 11.0 -> ratio 11/10
        # hfq: 1.0 before, 1.1 from the ex-date on
        # qfq: 1/1.1 before, 1.0 from the ex-date on
        factors = cumulate_factors(
            _events(("600519", _d(10), 1.0, 0.0, 0.0, 0.0)),
            _closes("600519", [(1, 10.0), (5, 11.0), (10, 12.0), (11, 13.0)]),
        )

        by_day = {f.trade_date: f for f in factors}
        assert by_day[_d(1)].hfq_factor == pytest.approx(1.0)
        assert by_day[_d(5)].hfq_factor == pytest.approx(1.0)
        assert by_day[_d(10)].hfq_factor == pytest.approx(1.1)
        assert by_day[_d(11)].hfq_factor == pytest.approx(1.1)
        assert by_day[_d(1)].qfq_factor == pytest.approx(1.0 / 1.1)
        assert by_day[_d(5)].qfq_factor == pytest.approx(1.0 / 1.1)
        assert by_day[_d(10)].qfq_factor == pytest.approx(1.0)
        assert by_day[_d(11)].qfq_factor == pytest.approx(1.0)

    def test_chain_of_two_events_multiplies(self):
        # event A on day 5: ratio 1.1; event B on day 12: ratio 1.2
        # hfq: 1.0, 1.1, 1.1*1.2 = 1.32
        # qfq: 1/1.32, 1/1.2, 1.0
        factors = cumulate_factors(
            _events(
                ("600519", _d(5), 0.0, 0.1, 0.0, 0.0),
                ("600519", _d(12), 0.0, 0.2, 0.0, 0.0),
            ),
            _closes(
                "600519",
                [(1, 10.0), (5, 11.0), (12, 13.2), (13, 14.0)],
            ),
        )

        by_day = {f.trade_date: f for f in factors}
        assert by_day[_d(1)].hfq_factor == pytest.approx(1.0)
        assert by_day[_d(5)].hfq_factor == pytest.approx(1.1)
        assert by_day[_d(12)].hfq_factor == pytest.approx(1.32)
        assert by_day[_d(13)].hfq_factor == pytest.approx(1.32)
        assert by_day[_d(1)].qfq_factor == pytest.approx(1.0 / 1.32)
        assert by_day[_d(5)].qfq_factor == pytest.approx(1.0 / 1.2)
        assert by_day[_d(13)].qfq_factor == pytest.approx(1.0)

    def test_adjusted_series_keep_a_constant_scale_ratio(self):
        """qfq and hfq are two scales, not one: their ratio per day is
        constant (the total event product), which is what makes both
        series visually identical after a change of units."""
        factors = cumulate_factors(
            _events(("600519", _d(10), 1.0, 0.0, 0.0, 0.0)),
            _closes("600519", [(1, 10.0), (10, 12.0)]),
        )
        by_day = {f.trade_date: f for f in factors}

        ratio_day1 = by_day[_d(1)].qfq_factor / by_day[_d(1)].hfq_factor
        ratio_day10 = by_day[_d(10)].qfq_factor / by_day[_d(10)].hfq_factor

        assert ratio_day1 == pytest.approx(ratio_day10)

    def test_anchors_are_exact(self):
        """qfq is 1.0 on the latest day, hfq is 1.0 before the first
        event - the two anchors the contract promises."""
        factors = cumulate_factors(
            _events(("600519", _d(10), 1.0, 0.0, 0.0, 0.0)),
            _closes("600519", [(1, 10.0), (10, 12.0)]),
        )
        by_day = {f.trade_date: f for f in factors}

        assert by_day[_d(10)].qfq_factor == pytest.approx(1.0)
        assert by_day[_d(1)].hfq_factor == pytest.approx(1.0)

    def test_symbols_are_independent(self):
        factors = cumulate_factors(
            _events(
                ("600519", _d(10), 1.0, 0.0, 0.0, 0.0),
                ("000001", _d(10), 0.0, 0.2, 0.0, 0.0),
            ),
            _closes("600519", [(1, 10.0), (10, 12.0)]) + _closes("000001", [(1, 5.0), (10, 6.0)]),
        )

        by_key = {(f.symbol, f.trade_date): f for f in factors}
        assert {day for symbol, day in by_key if symbol == "600519"} == {_d(1), _d(10)}
        assert by_key[("000001", _d(10))].hfq_factor == pytest.approx(1.2)

    def test_missing_previous_close_fails_closed(self):
        with pytest.raises(ValueError, match="no close before"):
            cumulate_factors(
                _events(("600519", _d(10), 1.0, 0.0, 0.0, 0.0)),
                _closes("600519", [(10, 12.0)]),  # no day before the ex-date
            )

    def test_ex_date_without_a_bar_gets_a_row_via_trading_days(self):
        factors = cumulate_factors(
            _events(("600519", _d(10), 1.0, 0.0, 0.0, 0.0)),
            _closes("600519", [(1, 10.0), (11, 12.0)]),
            trading_days=[_d(10)],
        )

        by_day = {f.trade_date: f for f in factors}
        assert _d(10) in by_day
        assert by_day[_d(10)].hfq_factor == pytest.approx(10.0 / 9.0)

    def test_affine_cash_transform_preserves_the_cash_offset(self):
        event = FactorEvent("600519", _d(3), cash_dividend=5.0)
        factors = cumulate_factors(
            [event],
            _closes("600519", [(1, 100.0), (2, 120.0), (3, 125.0)]),
        )

        by_day = {factor.trade_date: factor for factor in factors}
        assert by_day[_d(1)].qfq_scale == pytest.approx(1.0)
        assert by_day[_d(1)].qfq_offset == pytest.approx(-5.0)
        assert 100.0 * by_day[_d(1)].qfq_scale + by_day[_d(1)].qfq_offset == pytest.approx(95.0)
        assert 120.0 * by_day[_d(2)].qfq_scale + by_day[_d(2)].qfq_offset == pytest.approx(115.0)
        assert by_day[_d(3)].qfq_scale == pytest.approx(1.0)
        assert by_day[_d(3)].qfq_offset == pytest.approx(0.0)
        assert 125.0 * by_day[_d(3)].hfq_scale + by_day[_d(3)].hfq_offset == pytest.approx(130.0)
        assert all(factor.adjustment_version == "affine-v1" for factor in factors)

    def test_affine_coefficients_cover_bonus_rights_and_combined_actions(self):
        cases = [
            (FactorEvent("600519", _d(3), bonus=0.25), (0.8, 0.0)),
            (
                FactorEvent("600519", _d(3), allotment_ratio=0.2, allotment_price=10.0),
                (1 / 1.2, 2.0 / 1.2),
            ),
            (
                FactorEvent(
                    "600519",
                    _d(3),
                    cash_dividend=5.0,
                    bonus=0.2,
                    allotment_ratio=0.1,
                    allotment_price=20.0,
                ),
                (1 / 1.3, -3.0 / 1.3),
            ),
        ]
        for event, expected in cases:
            factors = cumulate_factors(
                [event], _closes("600519", [(1, 100.0), (2, 120.0), (3, 130.0)])
            )
            prior = next(factor for factor in factors if factor.trade_date == _d(1))
            assert (prior.qfq_scale, prior.qfq_offset) == pytest.approx(expected)

    def test_affine_cash_offset_matches_c56_cash_dividend_evidence(self):
        factors = cumulate_factors(
            [FactorEvent("600519", _d(3), cash_dividend=28.02)],
            _closes("600519", [(1, 1268.0), (2, 1300.0), (3, 1290.0)]),
        )

        prior = factors[0]
        assert 1268.0 * prior.qfq_scale + prior.qfq_offset == pytest.approx(1239.98)

    def test_affine_event_composition_keeps_non_commuting_order(self):
        factors = cumulate_factors(
            [
                FactorEvent("600519", _d(2), cash_dividend=5.0),
                FactorEvent("600519", _d(3), bonus=0.25),
            ],
            _closes("600519", [(1, 100.0), (2, 110.0), (3, 115.0)]),
        )

        first = next(factor for factor in factors if factor.trade_date == _d(1))
        last = next(factor for factor in factors if factor.trade_date == _d(3))
        assert (first.qfq_scale, first.qfq_offset) == pytest.approx((0.8, -4.0))
        assert (last.hfq_scale, last.hfq_offset) == pytest.approx((1.25, 5.0))

    def test_affine_events_after_latest_bar_do_not_move_latest_anchor(self):
        factors = cumulate_factors(
            [FactorEvent("600519", _d(3), cash_dividend=5.0)],
            _closes("600519", [(1, 100.0), (2, 120.0)]),
        )

        latest = factors[-1]
        assert (latest.qfq_scale, latest.qfq_offset) == (1.0, 0.0)

    def test_multiple_same_day_events_are_ambiguous(self):
        with pytest.raises(ValueError, match="ambiguous multiple corporate actions"):
            cumulate_factors(
                [
                    FactorEvent("600519", _d(10), cash_dividend=1.0, event_key="cash"),
                    FactorEvent("600519", _d(10), bonus=0.1, event_key="bonus"),
                ],
                _closes("600519", [(1, 10.0), (10, 12.0)]),
            )
