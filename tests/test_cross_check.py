"""Cross-check engine tests (A4.5, design §8.2).

Two sources for one domain are normalized through their mapping
tables to the contract shape and compared key by key. The design's
verification asks for two things this file pins down:

* an injected difference between two source samples is caught
  (deviation beyond the field tolerance);
* the counterexamples: a field-name mismatch must error (covered in
  ``test_data_mapping``) and an unconverted unit must produce a
  mismatch rather than a silent pass.

Missing semantics, the diff-rate denominator (union of keys) and
detail sampling follow the same section of the design.
"""

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from opendata.data.mapping import DomainMapping, FieldMapping, normalize_frame
from opendata.pipeline.cross_check import (
    DiffSummary,
    Verdict,
    compare_source_frames,
)

CHECKED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
BATCH_ID = "9e26f5a1-3f6c-4b7e-9c1d-2f5a8e3b7c41"


def _domain_mapping(suffix: str = "") -> DomainMapping:
    """A minimal stock-daily mapping for the test's 源B spelling."""
    return DomainMapping(
        domain="stock_daily",
        key=("symbol", "trade_date"),
        fields={
            "symbol": FieldMapping(f"symbol{suffix}", normalize="plain"),
            "trade_date": FieldMapping(f"trade_date{suffix}"),
            "name": FieldMapping(f"name{suffix}"),
            "close": FieldMapping(f"close{suffix}"),
            "volume": FieldMapping(f"volume{suffix}", scale=100),
        },
        tolerances={"close": 1e-4},
    )


def _frame(**columns: list) -> pd.DataFrame:
    base = {
        "symbol": ["600519", "000001"],
        "trade_date": [date(2024, 1, 2), date(2024, 1, 2)],
        "name": ["贵州茅台", "平安银行"],
        "close": [1688.0, 9.5],
        "volume": [30000.0, 20000.0],
    }
    base.update(columns)
    return pd.DataFrame(base)


def _compare(frame_a, frame_b, *, source_b="ths", sample_limit=100) -> DiffSummary:
    mapping = _domain_mapping()
    return compare_source_frames(
        "stock_daily",
        frame_a,
        mapping,
        frame_b,
        mapping,
        source_a="akshare",
        source_b=source_b,
        batch_id=BATCH_ID,
        checked_at=CHECKED_AT,
        sample_limit=sample_limit,
    )


class TestComparison:
    def test_identical_samples_are_consistent(self):
        summary = _compare(_frame(), _frame())

        assert summary.verdict is Verdict.CONSISTENT
        assert summary.deviation_count == 0
        assert summary.missing_count == 0
        assert summary.compared_keys == 2
        assert summary.samples == []
        assert summary.diff_rate == 0.0

    def test_injected_value_difference_is_caught(self):
        summary = _compare(_frame(), _frame(close=[1688.0, 9.9]))

        assert summary.verdict is Verdict.DEVIATION
        assert summary.deviation_count == 1
        sample = summary.samples[0]
        assert sample.field == "close"
        assert sample.biz_key == ("000001", date(2024, 1, 2))
        assert sample.value_a == 9.5
        assert sample.value_b == 9.9
        assert sample.deviation == pytest.approx((9.9 - 9.5) / 9.9)

    def test_difference_within_tolerance_is_consistent(self):
        # close tolerance is 1e-4 on the mapping.
        summary = _compare(_frame(), _frame(close=[1688.00001, 9.5]))

        assert summary.verdict is Verdict.CONSISTENT
        assert summary.deviation_count == 0

    def test_unconverted_unit_produces_a_mismatch(self):
        """Counterexample 2: a missing 手 -> 股 conversion must not pass."""
        summary = _compare(_frame(), _frame(volume=[30000.0, 2000000.0]))

        assert summary.verdict is Verdict.DEVIATION
        assert summary.samples[0].field == "volume"

    def test_date_keys_diverging_count_as_missing_on_both_sides(self):
        # trade_date is part of the key: shifting it makes each side
        # miss one key (union denominator stays 3).
        summary = _compare(_frame(), _frame(trade_date=[date(2024, 1, 2), date(2024, 1, 3)]))

        assert summary.missing_count == 2
        assert summary.compared_keys == 3
        assert all(sample.verdict is Verdict.MISSING for sample in summary.samples)

    def test_non_key_text_field_compares_exactly(self):
        summary = _compare(_frame(), _frame(name=["贵州茅台", "平安银行股份"]))

        assert summary.deviation_count == 1
        assert summary.samples[0].field == "name"
        assert summary.samples[0].deviation is None

    def test_single_side_missing_row_is_a_difference(self):
        summary = _compare(_frame().iloc[:1], _frame())

        assert summary.missing_count == 1
        assert summary.samples[0].verdict is Verdict.MISSING
        assert summary.samples[0].biz_key == ("000001", date(2024, 1, 2))

    def test_both_sides_missing_a_key_is_consistent(self):
        frame = _frame().iloc[:1]

        summary = _compare(frame, frame)

        assert summary.verdict is Verdict.CONSISTENT
        assert summary.compared_keys == 1

    def test_diff_rate_uses_the_union_of_keys(self):
        summary = _compare(_frame().iloc[:1], _frame())

        assert summary.compared_keys == 2  # union, not the smaller side
        assert summary.diff_rate == pytest.approx(0.5)

    def test_duplicate_keys_fail_closed(self):
        duplicated = pd.concat([_frame(), _frame().iloc[:1]], ignore_index=True)

        with pytest.raises(ValueError, match="duplicate"):
            _compare(_frame(), duplicated)

    def test_samples_are_capped_but_counts_stay_complete(self):
        frame_a = pd.DataFrame(
            {
                "symbol": [f"{index:06d}" for index in range(20)],
                "trade_date": [date(2024, 1, 2)] * 20,
                "name": ["样本"] * 20,
                "close": [1.0] * 20,
                "volume": [100.0] * 20,
            }
        )
        frame_b = frame_a.assign(close=[2.0] * 20)

        summary = _compare(frame_a, frame_b, sample_limit=5)

        assert summary.deviation_count == 20
        assert len(summary.samples) == 5

    def test_summary_carries_domain_sources_and_timestamp(self):
        summary = _compare(_frame(), _frame())

        assert summary.domain == "stock_daily"
        assert summary.source_a == "akshare"
        assert summary.source_b == "ths"
        assert summary.batch_id == BATCH_ID
        assert summary.checked_at == CHECKED_AT


class TestNormalizationInTheComparison:
    def test_frames_are_normalized_before_comparison(self):
        """Source spellings differ (600519.SH vs 600519) but must match."""
        raw_a = pd.DataFrame(
            {
                "股票代码": ["600519.SH"],
                "日期": [date(2024, 1, 2)],
                "开盘": [1685.0],
                "最高": [1690.0],
                "最低": [1680.0],
                "收盘": [1688.0],
                "成交量": [30000.0],
                "成交额": [5.06e9],
            }
        )
        raw_b = pd.DataFrame(
            {
                "symbol": ["600519"],
                "trade_date": [date(2024, 1, 2)],
                "open": [1685.0],
                "high": [1690.0],
                "low": [1680.0],
                "close": [1688.0],
                "volume": [3000000.0],  # already in shares
                "amount": [5.06e9],
            }
        )
        mapping_b = DomainMapping(
            domain="stock_daily",
            key=("symbol", "trade_date"),
            fields={
                "symbol": FieldMapping("symbol", normalize="plain"),
                "trade_date": FieldMapping("trade_date"),
                "open": FieldMapping("open"),
                "high": FieldMapping("high"),
                "low": FieldMapping("low"),
                "close": FieldMapping("close"),
                "volume": FieldMapping("volume"),
                "amount": FieldMapping("amount"),
            },
            tolerances={"close": 1e-4},
        )
        raw_a = raw_a.assign(开盘=[1685.0], 最高=[1690.0], 最低=[1680.0], 成交额=[5.06e9])

        summary = compare_source_frames(
            "stock_daily",
            raw_a,
            require_akshare_mapping(),
            raw_b,
            mapping_b,
            source_a="akshare",
            source_b="ths",
            batch_id=BATCH_ID,
            checked_at=CHECKED_AT,
        )

        assert summary.verdict is Verdict.CONSISTENT

    def test_structural_field_mismatch_fails_closed(self):
        mapping = _domain_mapping()
        narrower = DomainMapping(
            domain="stock_daily",
            key=("symbol", "trade_date"),
            fields={
                "symbol": FieldMapping("symbol"),
                "trade_date": FieldMapping("trade_date"),
                "close": FieldMapping("close"),
            },
        )

        with pytest.raises(ValueError, match="different contract fields"):
            compare_source_frames(
                "stock_daily",
                _frame(),
                mapping,
                _frame().drop(columns=["name", "volume"]),
                narrower,
                source_a="akshare",
                source_b="ths",
                batch_id=BATCH_ID,
                checked_at=CHECKED_AT,
            )


def require_akshare_mapping():
    """The shipped akshare mapping (helper for readability)."""
    from opendata.data.mapping import require_domain_mapping

    return require_domain_mapping("akshare", "stock_daily")


def test_normalize_keeps_contract_columns_only():
    mapping = require_akshare_mapping()
    raw = pd.DataFrame(
        {
            "日期": [date(2024, 1, 2)],
            "股票代码": ["600519"],
            "开盘": [1.0],
            "最高": [1.2],
            "最低": [0.9],
            "收盘": [1.1],
            "成交量": [100.0],
            "成交额": [1000.0],
            "换手率": [0.5],  # not mapped: dropped from the contract view
        }
    )

    normalized = normalize_frame(raw, mapping)

    assert "换手率" not in normalized.columns
