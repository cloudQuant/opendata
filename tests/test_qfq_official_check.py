"""Offline tests for the AC-11 cross-source adjust checker (pure logic)."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "qfq_official_check.py"
_spec = importlib.util.spec_from_file_location("qfq_official_check", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

sina_symbol = _module.sina_symbol
_compare = _module._compare


class TestSinaSymbol:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("600519", "sh600519"),
            ("688981", "sh688981"),
            ("000001", "sz000001"),
            ("300750", "sz300750"),
            ("510300", "sh510300"),
            ("159915", "sz159915"),
            ("920992", "bj920992"),
        ],
    )
    def test_exchange_prefix_rules(self, code: str, expected: str) -> None:
        assert sina_symbol(code) == expected

    def test_unknown_prefix_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="cannot derive the sina symbol"):
            sina_symbol("123456")


class TestCompare:
    def test_matching_series_passes(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 19)]
        rows = [{"trade_date": day, "close": 10.0 * index} for index, day in enumerate(days, 1)]
        official = {day.isoformat(): 10.0 * index for index, day in enumerate(days, 1)}

        report = _compare(rows, official, tolerance=2e-3)

        assert report["failed"] is False
        assert report["compared"] == 5
        assert report["max_shape_dev"] == pytest.approx(0.0)

    def test_constant_anchor_offset_is_a_level_difference_not_a_failure(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 19)]
        rows = [{"trade_date": day, "close": 2.0 * index} for index, day in enumerate(days, 1)]
        official = {day.isoformat(): 1.0 * index for index, day in enumerate(days, 1)}

        report = _compare(rows, official, tolerance=2e-3)

        assert report["anchor"] == pytest.approx(2.0)
        assert report["max_dev"] > 0.5  # levels differ by the anchoring constant
        assert report["failed"] is False

    def test_shape_divergence_fails(self) -> None:
        days = [date(2026, 1, day) for day in range(14, 17)]
        rows = [
            {"trade_date": day, "close": close}
            for day, close in zip(days, (10.0, 11.0, 10.0), strict=True)
        ]
        official = dict(zip((day.isoformat() for day in days), (10.0, 11.0, 12.0), strict=True))

        report = _compare(rows, official, tolerance=2e-3)

        assert report["failed"] is True
        assert report["diffs"]

    def test_no_common_dates_is_a_failure(self) -> None:
        report = _compare([{"trade_date": date(2026, 1, 1), "close": 10.0}], {}, tolerance=2e-3)

        assert report["failed"] is True
        assert report["compared"] == 0
