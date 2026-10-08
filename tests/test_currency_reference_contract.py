"""Contract tests for ECB daily EUR-reference observations."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models.currency import CurrencyReferenceRate, SdmxGroupContext


def _row(**overrides: object) -> CurrencyReferenceRate:
    values: dict[str, object] = {
        "series_key": "D.ZXQ.EUR.SP00.A",
        "date": "2026-10-08",
        "frequency": "D",
        "quote_currency": "ZXQ",
        "base_currency": "EUR",
        "rate_type": "SP00",
        "rate_suffix": "A",
        "value": 1.2345,
        "source_value": "1.2345",
        "dataset_attributes": {"UNIT": "currency per EUR", "UNKNOWN": "raw"},
        "series_attributes": {"COLLECTION": "A"},
        "observation_attributes": {"OBS_STATUS": "A"},
        "group_context": (
            SdmxGroupContext(
                group_type="EXR_GROUP",
                key={"FREQ": "D", "CURRENCY": "ZXQ"},
                attributes={"COMMENT": "source group text"},
            ),
        ),
    }
    return CurrencyReferenceRate.model_validate({**values, **overrides})


def test_frame_and_json_roundtrips_preserve_daily_date_null_and_nested_context() -> None:
    rows = [
        _row(),
        _row(
            date=date(2026, 10, 9),
            value=None,
            source_value=None,
            dataset_attributes={"UNKNOWN": "kept"},
            series_attributes={},
            observation_attributes={},
            group_context=(),
        ),
    ]

    frame = CurrencyReferenceRate.to_frame(rows)
    assert frame["date"].tolist() == [date(2026, 10, 8), date(2026, 10, 9)]
    assert pd.isna(frame.loc[1, "value"])
    restored = CurrencyReferenceRate.from_frame(frame)
    assert restored == rows
    assert restored[1].value is None
    assert restored[0].group_context[0].attributes == {"COMMENT": "source group text"}

    for row in rows:
        encoded = row.model_dump_json()
        assert CurrencyReferenceRate.model_validate_json(encoded) == row
        assert '"date":"2026-10-' in encoded


def test_rate_value_may_be_zero_or_negative_without_source_unproven_positivity_rule() -> None:
    assert _row(value=0.0, source_value="0").value == 0.0
    assert _row(value=-1.0, source_value="-1.0").value == -1.0


def test_frame_input_does_not_coerce_timestamps_into_daily_dates() -> None:
    frame = CurrencyReferenceRate.to_frame([_row()])
    frame.at[0, "date"] = pd.Timestamp("2026-10-08T12:00:00")
    with pytest.raises(ValueError, match="must not include a time"):
        CurrencyReferenceRate.from_frame(frame)

    frame.at[0, "date"] = 20261008
    with pytest.raises(ValidationError):
        CurrencyReferenceRate.from_frame(frame)


@pytest.mark.parametrize(
    "overrides",
    [
        {"series_key": "D.ZXQ.EUR.SP00"},
        {"series_key": "D.ZXQ.USD.SP00.A"},
        {"frequency": "M"},
        {"quote_currency": ""},
        {"quote_currency": "ZX.Q"},
        {"quote_currency": "ZX Q"},
        {"base_currency": "USD"},
        {"rate_type": "SP01"},
        {"rate_suffix": "B"},
    ],
)
def test_reference_dimensions_must_match_the_exact_supported_key(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _row(**overrides)


@pytest.mark.parametrize(
    "invalid_date",
    [datetime(2026, 10, 8), "2026-1-08", "20261008", 20261008],
)
def test_daily_date_rejects_datetime_noncanonical_strings_and_numeric_coercion(
    invalid_date: object,
) -> None:
    with pytest.raises(ValidationError):
        _row(date=invalid_date)


@pytest.mark.parametrize("invalid_value", [True, False, "1.2", float("nan"), float("inf")])
def test_value_requires_a_finite_non_boolean_number(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        _row(value=invalid_value)


@pytest.mark.parametrize("raw", ["", "NaN", "Infinity", "1,2", "1e9999"])
def test_source_value_requires_finite_decimal_numeric_syntax(raw: str) -> None:
    with pytest.raises(ValidationError):
        _row(value=1.0, source_value=raw)


def test_value_and_source_value_must_be_present_or_missing_together_and_match() -> None:
    with pytest.raises(ValidationError):
        _row(value=None, source_value="1.0")
    with pytest.raises(ValidationError):
        _row(value=1.0, source_value=None)
    with pytest.raises(ValidationError):
        _row(value=2.0, source_value="1.0")

    required_fields = _row().model_dump()
    del required_fields["source_value"]
    with pytest.raises(ValidationError):
        CurrencyReferenceRate.model_validate(required_fields)


def test_group_and_attribute_maps_are_strict_string_maps_and_models_are_closed() -> None:
    with pytest.raises(ValidationError):
        _row(dataset_attributes={"UNIT": 1})
    with pytest.raises(ValidationError):
        _row(group_context=({"group_type": "G", "key": {"FREQ": 1}},))
    with pytest.raises(ValidationError):
        _row(unexpected="extra")
