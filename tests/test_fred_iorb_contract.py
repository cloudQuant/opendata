"""Offline source-token and schema contract tests for FRED IORB."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from opendata.data.models.economic_series import SeriesObservation
from opendata.data.models.fred_iorb import FredIorbObservation

BASE: dict[str, object] = {
    "series_id": "IORB",
    "date": "2025-01-02",
    "value": 4.25,
    "realtime_start": "2025-01-02",
    "realtime_end": "9999-12-31",
    "transform_units": "lin",
    "output_type": 1,
    "requested_frequency": None,
    "requested_aggregation_method": "avg",
    "source_value": "4.25",
    "native_units": "Percent",
}


def observation(**updates: object) -> FredIorbObservation:
    values = dict(BASE)
    values.update(updates)
    return FredIorbObservation.model_validate(values)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("4.2500", 4.25),
        ("+4.25e0", 4.25),
        ("-0", -0.0),
        ("+0.000E-7", 0.0),
        ("1e-9999", 0.0),
        ("-1e-9999", -0.0),
    ],
)
def test_numeric_source_token_is_preserved_with_exact_float_pair(
    token: str, expected: float
) -> None:
    item = observation(source_value=token, value=expected)
    assert item.source_value == token
    assert item.value == expected
    if expected == 0.0:
        assert math.copysign(1.0, item.value) == math.copysign(1.0, expected)


def test_dot_is_the_only_missing_source_marker() -> None:
    item = observation(source_value=".", value=None)
    assert item.value is None
    assert item.source_value == "."


@pytest.mark.parametrize(
    "token",
    [
        "",
        " ",
        " 4.25",
        "4.25 ",
        "NaN",
        "nan",
        "Inf",
        "Infinity",
        "1e9999",
        "1_000",
        "1,000",
        "true",
        "null",
        ". ",
        "−1",
    ],
)
def test_noncanonical_or_nonfinite_source_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ValidationError):
        observation(source_value=token, value=4.25)


@pytest.mark.parametrize("value", [True, 4, "4.25", float("nan"), float("inf")])
def test_value_requires_a_finite_exact_float_or_none(value: object) -> None:
    with pytest.raises(ValidationError):
        observation(value=value)


@pytest.mark.parametrize(
    ("source_value", "value"),
    [
        (".", 0.0),
        ("4.25", None),
        ("4.26", 4.25),
        ("-0", 0.0),
        ("+0", -0.0),
    ],
)
def test_source_value_and_float_must_match_including_signed_zero(
    source_value: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        observation(source_value=source_value, value=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", datetime(2025, 1, 2)),
        ("date", 1735776000),
        ("date", 20250102),
        ("date", "2025-1-2"),
        ("date", "2025-02-30"),
        ("realtime_start", "2025-01-02T00:00:00Z"),
        ("realtime_end", "20250102"),
    ],
)
def test_date_fields_reject_timestamps_datetimes_and_noncanonical_text(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        observation(**{field: value})


def test_date_fields_accept_exact_date_and_canonical_string() -> None:
    item = observation(
        date=date(2025, 1, 2),
        realtime_start="2025-01-02",
        realtime_end=date(9999, 12, 31),
    )
    assert item.date == date(2025, 1, 2)
    assert type(item.date) is date


def test_date_subclasses_are_rejected() -> None:
    class DateChild(date):
        pass

    with pytest.raises(ValidationError):
        observation(date=DateChild(2025, 1, 2))


def test_realtime_range_order_is_inherited() -> None:
    with pytest.raises(ValidationError):
        observation(realtime_start="2025-01-03", realtime_end="2025-01-02")


@pytest.mark.parametrize("value", [True, 1.0, "1", 0, 5])
def test_output_type_requires_exact_supported_integer(value: object) -> None:
    with pytest.raises(ValidationError):
        observation(output_type=value)


@pytest.mark.parametrize("field,value", [("series_id", "SOFR"), ("native_units", "Percent ")])
def test_identity_and_unit_are_fixed_to_iorb_percent(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        observation(**{field: value})


def test_unknown_contract_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        observation(extra="not part of the contract")


def test_all_eleven_fields_round_trip_through_json_and_frame() -> None:
    item = observation(source_value="4.2500e0", value=4.25)
    decoded = FredIorbObservation.model_validate_json(item.model_dump_json())
    assert decoded == item
    frame = FredIorbObservation.to_frame([item])
    assert list(frame.columns) == list(FredIorbObservation.model_fields)
    assert len(frame.columns) == 11
    restored = FredIorbObservation.from_frame(frame)
    assert restored == [item]
    assert restored[0].source_value == "4.2500e0"


def test_dot_missing_value_survives_json_and_frame_round_trips() -> None:
    item = observation(source_value=".", value=None)
    assert FredIorbObservation.model_validate_json(item.model_dump_json()) == item
    restored = FredIorbObservation.from_frame(FredIorbObservation.to_frame([item]))
    assert restored == [item]
    assert restored[0].source_value == "."
    assert restored[0].value is None


def test_frame_bridge_rejects_datetime_before_stripping_time() -> None:
    record = dict(BASE)
    record["date"] = datetime(2025, 1, 2, tzinfo=timezone.utc)
    frame = FredIorbObservation.to_frame([observation()])
    frame.loc[0, "date"] = record["date"]
    with pytest.raises(ValueError, match="must not include a time"):
        FredIorbObservation.from_frame(frame)


def test_generic_series_observation_contract_remains_independent() -> None:
    generic = SeriesObservation.model_validate(
        {key: value for key, value in BASE.items() if key not in {"source_value", "native_units"}}
    )
    assert generic.series_id == "IORB"
    assert not isinstance(generic, FredIorbObservation)
