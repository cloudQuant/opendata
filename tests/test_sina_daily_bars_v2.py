"""Offline contract tests for the additive source-native Sina daily-bar v2."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from opendata.data.providers.sina.daily_bars_v2 import (
    SinaBondDailyV2Adapter,
    SinaFuturesDailyV2Adapter,
    SinaOptionDailyV2Adapter,
    SourceNativeBarV2,
)
from opendata.data.providers.sina.daily_bars_v2._base import SinaDailyBarV2Normalizer

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "upstream"

RECORDED_CASES = (
    (SinaFuturesDailyV2Adapter, "futures_daily_sina", "RB0", "RB0"),
    (SinaOptionDailyV2Adapter, "option_daily_sina", "10003889", "10003889"),
    (SinaBondDailyV2Adapter, "bond_daily_sina", "sh010107", "sh010107"),
)


@pytest.mark.parametrize(
    ("adapter_type", "fixture_name", "symbol", "expected_output_symbol"), RECORDED_CASES
)
def test_recorded_sina_fixture_projects_native_ohlcv_and_null_amount(
    adapter_type: type[SinaDailyBarV2Normalizer],
    fixture_name: str,
    symbol: str,
    expected_output_symbol: str,
) -> None:
    """Recorded frames project exact OHLCV and explicit absent turnover."""
    fixture_dir = FIXTURE_ROOT / fixture_name
    metadata = json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))
    raw = pd.read_csv(fixture_dir / "reference.csv.gz")
    adapter = adapter_type()

    assert metadata["status"] == "recorded"
    assert metadata["function"] == adapter.profile.function
    assert metadata["upstream_commit"] == adapter.profile.upstream_commit
    assert metadata["kwargs"]["symbol"] == symbol
    assert tuple(raw.columns) == adapter.profile.expected_columns
    assert adapter.profile.amount_column is None
    assert adapter.profile.amount_absence_reason
    assert adapter.profile.price_unit_status == "unverified_source_native"
    assert adapter.profile.volume_unit_status == "unverified_source_native"
    assert adapter.profile.excluded_source_columns == (
        ("hold", "settle") if fixture_name == "futures_daily_sina" else ()
    )

    bars = adapter.normalize(raw, symbol=symbol)

    assert len(bars) == len(raw) == metadata["rows"]
    assert all(isinstance(bar, SourceNativeBarV2) for bar in bars)
    assert [bar.trade_date for bar in bars] == sorted(bar.trade_date for bar in bars)
    assert all(bar.amount is None for bar in bars)
    source_by_date = {
        date.fromisoformat(str(record[adapter.profile.date_column])): record
        for record in raw.to_dict("records")
    }
    for bar in bars:
        record = source_by_date[bar.trade_date]
        assert bar.source == "sina"
        assert bar.source_profile == adapter.profile.name
        assert bar.asset_class == adapter.profile.asset_class
        assert bar.domain == adapter.profile.domain
        assert bar.symbol == expected_output_symbol
        assert bar.projection == "ohlcv_projection"
        assert bar.excluded_source_columns == adapter.profile.excluded_source_columns
        assert bar.source_frame_retention_required is True
        for field in ("open", "high", "low", "close", "volume"):
            source_column = getattr(adapter.profile, f"{field}_column")
            assert getattr(bar, field) == float(record[source_column])
        assert bar.amount is None
        serialized = bar.model_dump()
        assert "amount" in serialized and serialized["amount"] is None
        assert serialized["source_profile"] == adapter.profile.name
        assert serialized["price_unit_status"] == "unverified_source_native"
        assert serialized["volume_unit_status"] == "unverified_source_native"
        assert serialized["amount_unit_status"] == "not_provided"


def test_declared_amount_column_preserves_real_zero_and_explicit_null() -> None:
    """An explicitly mapped source amount keeps zero distinct from missing."""
    profile = replace(
        SinaOptionDailyV2Adapter.profile,
        expected_columns=(*SinaOptionDailyV2Adapter.profile.expected_columns, "成交额"),
        amount_column="成交额",
        amount_absence_reason=None,
        amount_unit_status="unverified_source_native",
    )
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": 1.0,
                "最高": 1.0,
                "最低": 1.0,
                "收盘": 1.0,
                "成交量": 10,
                "成交额": 0.0,
            },
            {
                "日期": "2026-09-23",
                "开盘": 1.0,
                "最高": 1.0,
                "最低": 1.0,
                "收盘": 1.0,
                "成交量": 11,
                "成交额": pd.NA,
            },
        ]
    )

    bars = SinaDailyBarV2Normalizer(profile).normalize(raw, symbol="10003889")

    assert [bar.amount for bar in bars] == [0.0, None]
    assert "amount" in bars[0].model_dump()
    assert bars[1].model_dump()["amount"] is None
    assert bars[0].amount_unit_status == "unverified_source_native"


def test_missing_or_unexpected_source_columns_fail_closed() -> None:
    adapter = SinaOptionDailyV2Adapter()
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": 1,
                "最高": 1,
                "最低": 1,
                "收盘": 1,
                "成交量": 1,
            }
        ]
    )

    with pytest.raises(ValueError, match="missing columns=.*成交量"):
        adapter.normalize(raw.drop(columns=["成交量"]), symbol="10003889")
    with pytest.raises(ValueError, match="unexpected columns=.*成交额"):
        adapter.normalize(raw.assign(成交额=0.0), symbol="10003889")


@pytest.mark.parametrize(
    ("adapter_type", "symbol"),
    [
        (SinaFuturesDailyV2Adapter, "RB0"),
        (SinaOptionDailyV2Adapter, "10003889"),
        (SinaBondDailyV2Adapter, "sh010107"),
    ],
)
def test_negative_volume_is_rejected_for_each_sina_profile(
    adapter_type: type[SinaDailyBarV2Normalizer], symbol: str
) -> None:
    adapter = adapter_type()
    profile = adapter.profile
    record: dict[str, object] = dict.fromkeys(profile.expected_columns, 1.0)
    record[profile.date_column] = "2026-09-22"
    record[profile.open_column] = 10.0
    record[profile.high_column] = 11.0
    record[profile.low_column] = 9.0
    record[profile.close_column] = 10.5
    record[profile.volume_column] = -1.0
    if profile.amount_column is not None:
        record[profile.amount_column] = 0.0
    raw = pd.DataFrame([record], columns=profile.expected_columns)

    with pytest.raises(ValueError, match="volume must be non-negative"):
        adapter.normalize(raw, symbol=symbol)


@pytest.mark.parametrize("bad_amount", ["0", float("inf"), -float("inf")])
def test_declared_amount_column_rejects_non_numeric_or_non_finite_values(
    bad_amount: object,
) -> None:
    profile = replace(
        SinaOptionDailyV2Adapter.profile,
        expected_columns=(*SinaOptionDailyV2Adapter.profile.expected_columns, "成交额"),
        amount_column="成交额",
        amount_absence_reason=None,
        amount_unit_status="unverified_source_native",
    )
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": 1.0,
                "最高": 1.0,
                "最低": 1.0,
                "收盘": 1.0,
                "成交量": 1,
                "成交额": bad_amount,
            }
        ]
    )

    with pytest.raises(ValueError, match="成交额"):
        SinaDailyBarV2Normalizer(profile).normalize(raw, symbol="10003889")


@pytest.mark.parametrize("bad_value", ["1.25", float("nan"), float("inf")])
def test_required_market_values_reject_non_numeric_or_non_finite_values(
    bad_value: object,
) -> None:
    adapter = SinaOptionDailyV2Adapter()
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": bad_value,
                "最高": 1.0,
                "最低": 1.0,
                "收盘": 1.0,
                "成交量": 1,
            }
        ]
    )

    with pytest.raises(ValueError, match="open"):
        adapter.normalize(raw, symbol="10003889")


@pytest.mark.parametrize(
    "date_value",
    [
        datetime(2026, 9, 22, 15, 30),
        datetime(2026, 9, 22, tzinfo=timezone.utc),
        "2026-09-22T15:30:00",
        "2026-09-22T00:00:00+00:00",
    ],
)
def test_intraday_or_timezone_aware_timestamps_are_rejected(date_value: object) -> None:
    adapter = SinaOptionDailyV2Adapter()
    raw = pd.DataFrame(
        [{"日期": date_value, "开盘": 1.0, "最高": 1.0, "最低": 1.0, "收盘": 1.0, "成交量": 1}]
    )

    with pytest.raises(ValueError, match="trade date"):
        adapter.normalize(raw, symbol="10003889")


@pytest.mark.parametrize(
    ("date_value", "expected_date"),
    [
        (date(2026, 9, 22), date(2026, 9, 22)),
        (datetime(2026, 9, 22), date(2026, 9, 22)),
        ("2026-09-22", date(2026, 9, 22)),
        ("2026-09-22T00:00:00", date(2026, 9, 22)),
    ],
)
def test_date_only_and_naive_midnight_values_are_accepted(
    date_value: object, expected_date: date
) -> None:
    adapter = SinaOptionDailyV2Adapter()
    raw = pd.DataFrame(
        [{"日期": date_value, "开盘": 1.0, "最高": 1.0, "最低": 1.0, "收盘": 1.0, "成交量": 1}]
    )

    bars = adapter.normalize(raw, symbol="10003889")

    assert bars[0].trade_date == expected_date


def test_bond_numeric_symbols_use_known_exchange_prefixes() -> None:
    adapter = SinaBondDailyV2Adapter()

    assert adapter.normalize_symbol("110059") == "sh110059"
    assert adapter.normalize_symbol("120001") == "sz120001"
    assert adapter.normalize_symbol("SH010107") == "sh010107"
    assert adapter.normalize_symbol("999999") == "999999"


@pytest.mark.parametrize(
    ("open_value", "high_value", "low_value", "close_value"),
    [
        (1.1, 1.0, 0.9, 1.0),  # high below open
        (1.0, 1.1, 0.9, 1.2),  # high below close
        (1.0, 0.8, 0.9, 1.0),  # high below low
        (0.9, 1.1, 1.0, 1.0),  # low above open
        (1.0, 1.1, 0.9, 0.8),  # low above close
    ],
)
def test_inconsistent_ohlc_geometry_fails_closed(
    open_value: float,
    high_value: float,
    low_value: float,
    close_value: float,
) -> None:
    adapter = SinaOptionDailyV2Adapter()
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": open_value,
                "最高": high_value,
                "最低": low_value,
                "收盘": close_value,
                "成交量": 1,
            }
        ]
    )

    with pytest.raises(ValueError, match="inconsistent OHLC"):
        adapter.normalize(raw, symbol="10003889")


def test_normalizer_requires_a_bound_source_profile() -> None:
    with pytest.raises(TypeError, match="SinaSourceProfile is required"):
        SinaDailyBarV2Normalizer()


@pytest.mark.parametrize(
    ("raw", "symbol", "message"),
    [
        ([], "10003889", "raw must be a pandas DataFrame"),
        (pd.DataFrame(), "  ", "symbol must be a non-empty string"),
        (pd.DataFrame(), None, "symbol must be a non-empty string"),
    ],
)
def test_normalizer_rejects_invalid_frame_and_instrument_identity(
    raw: object, symbol: object, message: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        SinaOptionDailyV2Adapter().normalize(raw, symbol=symbol)  # type: ignore[arg-type]


@pytest.mark.parametrize("bound", ["start_date", "end_date"])
def test_date_range_bounds_must_be_calendar_dates_not_datetimes(bound: str) -> None:
    kwargs = {bound: datetime(2026, 9, 22)}
    with pytest.raises(TypeError, match=f"{bound} must be a date or None"):
        SinaOptionDailyV2Adapter().normalize(
            pd.DataFrame(columns=SinaOptionDailyV2Adapter.profile.expected_columns),
            symbol="10003889",
            **kwargs,
        )


def test_date_range_is_inclusive_and_discards_rows_outside_requested_window() -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-24",
                "开盘": 1.0,
                "最高": 1.2,
                "最低": 0.9,
                "收盘": 1.1,
                "成交量": 30,
            },
            {
                "日期": "2026-09-21",
                "开盘": 1.0,
                "最高": 1.1,
                "最低": 0.9,
                "收盘": 1.0,
                "成交量": 10,
            },
            {
                "日期": "2026-09-23",
                "开盘": 1.0,
                "最高": 1.2,
                "最低": 0.8,
                "收盘": 1.1,
                "成交量": 20,
            },
        ]
    )

    bars = SinaOptionDailyV2Adapter().normalize(
        raw,
        symbol="10003889",
        start_date=date(2026, 9, 23),
        end_date=date(2026, 9, 24),
    )

    assert [(bar.trade_date, bar.volume) for bar in bars] == [
        (date(2026, 9, 23), 20.0),
        (date(2026, 9, 24), 30.0),
    ]


def test_reversed_date_range_is_rejected_before_reading_rows() -> None:
    raw = pd.DataFrame(columns=SinaOptionDailyV2Adapter.profile.expected_columns)

    with pytest.raises(ValueError, match="start_date must be on or before end_date"):
        SinaOptionDailyV2Adapter().normalize(
            raw,
            symbol="10003889",
            start_date=date(2026, 9, 24),
            end_date=date(2026, 9, 23),
        )


def test_duplicate_source_column_labels_are_rejected_before_projection() -> None:
    columns = (*SinaOptionDailyV2Adapter.profile.expected_columns[:-1], "成交量", "成交量")
    raw = pd.DataFrame([["2026-09-22", 1.0, 1.0, 1.0, 1.0, 10, 10]], columns=columns)

    with pytest.raises(ValueError, match="duplicate column names"):
        SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")


@pytest.mark.parametrize(
    ("trade_date", "message"),
    [
        ("2026-02-30", "has an invalid trade date"),
        ("2026-9-2", "trade date must be date-only"),
        (20260922, "has an invalid trade date"),
    ],
)
def test_malformed_calendar_dates_fail_with_row_context(trade_date: object, message: str) -> None:
    raw = pd.DataFrame(
        [{"日期": trade_date, "开盘": 1.0, "最高": 1.0, "最低": 1.0, "收盘": 1.0, "成交量": 1}]
    )

    with pytest.raises(ValueError, match=message):
        SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")


@pytest.mark.parametrize("bad_value", [True, np.bool_(True), None, pd.NA])
def test_required_market_values_reject_boolean_and_missing_values(bad_value: object) -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": bad_value,
                "最高": 1.0,
                "最低": 1.0,
                "收盘": 1.0,
                "成交量": 1,
            }
        ]
    )

    with pytest.raises(ValueError, match="open"):
        SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")


def test_decimal_prices_and_zero_volume_keep_their_financial_values() -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": Decimal("2.35"),
                "最高": Decimal("2.42"),
                "最低": Decimal("2.31"),
                "收盘": Decimal("2.40"),
                "成交量": Decimal("0"),
            }
        ]
    )

    bars = SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")

    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (
        2.35,
        2.42,
        2.31,
        2.40,
    )
    assert bars[0].volume == 0.0


def test_nested_source_cell_is_rejected_instead_of_treated_as_missing() -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": [1.0, 2.0],
                "最高": 2.0,
                "最低": 1.0,
                "收盘": 1.5,
                "成交量": 1,
            }
        ]
    )

    with pytest.raises(ValueError, match="source cells must contain scalar values"):
        SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")


def test_duplicate_instrument_date_key_is_rejected_even_when_prices_differ() -> None:
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": 1.0,
                "最高": 1.2,
                "最低": 0.9,
                "收盘": 1.1,
                "成交量": 10,
            },
            {
                "日期": "2026-09-22",
                "开盘": 1.1,
                "最高": 1.3,
                "最低": 1.0,
                "收盘": 1.2,
                "成交量": 12,
            },
        ]
    )

    with pytest.raises(ValueError, match="duplicate trade date 2026-09-22"):
        SinaOptionDailyV2Adapter().normalize(raw, symbol="10003889")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"expected_columns": (*SinaOptionDailyV2Adapter.profile.expected_columns, "日期")},
            "expected_columns contains duplicates",
        ),
        (
            {"excluded_source_columns": ("成交量", "成交量")},
            "excluded_source_columns contains duplicates",
        ),
        ({"open_column": "不存在"}, "mapped columns must be declared"),
        (
            {"amount_absence_reason": ""},
            "absent amount column needs an explicit source declaration",
        ),
        (
            {"amount_unit_status": "unverified_source_native"},
            "absent amount cannot have a unit status",
        ),
        ({"amount_column": "成交额"}, "amount_column must be declared in expected_columns"),
        (
            {
                "expected_columns": (
                    *SinaFuturesDailyV2Adapter.profile.expected_columns,
                    "成交额",
                ),
                "amount_column": "成交额",
            },
            "amount_absence_reason conflicts with a declared amount_column",
        ),
        (
            {
                "expected_columns": (
                    *SinaFuturesDailyV2Adapter.profile.expected_columns,
                    "成交额",
                ),
                "amount_column": "成交额",
                "amount_absence_reason": None,
                "amount_unit_status": "not_provided",
            },
            "present source-native amount unit must be marked unverified",
        ),
        (
            {"excluded_source_columns": ("hold",)},
            "every unprojected source column must be explicitly listed",
        ),
    ],
)
def test_source_profile_rejects_conflicting_financial_schema_metadata(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        replace(SinaFuturesDailyV2Adapter.profile, **changes)


def test_declared_amount_metadata_round_trips_with_the_projection() -> None:
    profile = replace(
        SinaOptionDailyV2Adapter.profile,
        expected_columns=(*SinaOptionDailyV2Adapter.profile.expected_columns, "成交额"),
        amount_column="成交额",
        amount_absence_reason=None,
        amount_unit_status="unverified_source_native",
    )
    raw = pd.DataFrame(
        [
            {
                "日期": "2026-09-22",
                "开盘": 2.35,
                "最高": 2.42,
                "最低": 2.31,
                "收盘": 2.40,
                "成交量": 100,
                "成交额": 240.0,
            }
        ]
    )
    bar = SinaDailyBarV2Normalizer(profile).normalize(raw, symbol="10003889")[0]

    restored = SourceNativeBarV2.from_frame(SourceNativeBarV2.to_frame([bar]))

    assert restored == [bar]
    assert restored[0].amount == 240.0
    assert restored[0].amount_unit_status == "unverified_source_native"
    assert restored[0].source_profile == "option_daily_sina"
    assert restored[0].source_frame_retention_required is True


def test_projection_contract_requires_nullable_amount_key_and_rejects_bad_source_tag() -> None:
    bar = SinaOptionDailyV2Adapter().normalize(
        pd.DataFrame(
            [
                {
                    "日期": "2026-09-22",
                    "开盘": 2.35,
                    "最高": 2.42,
                    "最低": 2.31,
                    "收盘": 2.40,
                    "成交量": 100,
                }
            ]
        ),
        symbol="10003889",
    )[0]
    payload = bar.model_dump()

    missing_amount = dict(payload)
    del missing_amount["amount"]
    with pytest.raises(ValueError, match="amount"):
        SourceNativeBarV2.model_validate(missing_amount)

    wrong_source = dict(payload)
    wrong_source["source"] = "akshare"
    with pytest.raises(ValueError, match="source"):
        SourceNativeBarV2.model_validate(wrong_source)

    assert payload["amount"] is None
    assert payload["source"] == "sina"
    assert payload["projection"] == "ohlcv_projection"
