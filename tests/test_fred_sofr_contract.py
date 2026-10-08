"""Synthetic contract tests for source-faithful FRED SOFR observations."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models.economic_series import SeriesObservation
from opendata.data.models.fred_sofr import FredSofrObservation

if TYPE_CHECKING:
    from collections.abc import Mapping


_PERCENT_SERIES = ("SOFR", "SOFR30DAYAVG", "SOFR90DAYAVG", "SOFR180DAYAVG")


def _row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "series_id": "SOFR",
        "date": "2026-01-02",
        "value": 4.25,
        "realtime_start": "2026-01-03",
        "realtime_end": "2026-01-04",
        "transform_units": "lin",
        "output_type": 1,
        "requested_frequency": "d",
        "requested_aggregation_method": "avg",
        "source_value": "4.25",
        "native_units": "Percent",
    }
    row.update(updates)
    return row


def _assert_rejected(row: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError):
        FredSofrObservation.model_validate(row)


@pytest.mark.parametrize("series_id", [*_PERCENT_SERIES, "SOFRINDEX"])
def test_supported_series_keep_their_exact_native_units(series_id: str) -> None:
    units = "Index Apr 2, 2018 = 1" if series_id == "SOFRINDEX" else "Percent"
    value = 1.0 if series_id == "SOFRINDEX" else 4.25
    row = FredSofrObservation.model_validate(
        _row(series_id=series_id, value=value, source_value=str(value), native_units=units)
    )

    assert row.series_id == series_id
    assert row.native_units == units


def test_native_units_must_match_series_and_unknown_series_is_rejected() -> None:
    _assert_rejected(
        _row(series_id="SOFRINDEX", native_units="Percent", value=1.0, source_value="1.0")
    )
    _assert_rejected(_row(series_id="SOFR", native_units="Index Apr 2, 2018 = 1", value=4.25))
    _assert_rejected(_row(series_id="SOFRX"))


@pytest.mark.parametrize(
    ("source_value", "value"),
    [
        (".", None),
        ("+1.2500e+1", 12.5),
        ("-0", -0.0),
        ("1e-9999", 0.0),
        ("-1e-9999", -0.0),
        ("1.7976931348623157e308", 1.7976931348623157e308),
    ],
)
def test_source_token_and_float_pair_are_preserved(source_value: str, value: float | None) -> None:
    row = FredSofrObservation.model_validate(_row(source_value=source_value, value=value))

    assert row.source_value == source_value
    assert row.value == value
    if value == 0.0 and source_value != ".":
        assert math.copysign(1.0, row.value) == math.copysign(1.0, float(source_value))


@pytest.mark.parametrize(
    "source_value",
    [
        "",
        " ",
        "NaN",
        "nan",
        "Inf",
        "Infinity",
        "True",
        "null",
        "1_000",
        "1,000",
        " 1",
        "1 ",
        "--1",
        ". ",
        "1e9999",
        "1.7976931348623159e308",
    ],
)
def test_invalid_or_nonfinite_source_tokens_are_rejected(source_value: str) -> None:
    _assert_rejected(_row(source_value=source_value))


@pytest.mark.parametrize("value", [1, True, "4.25", float("nan"), float("inf")])
def test_value_requires_a_finite_strict_float_or_none(value: object) -> None:
    _assert_rejected(_row(value=value))


@pytest.mark.parametrize(
    ("source_value", "value"),
    [
        (".", 0.0),
        ("4.25", None),
        ("4.25", 4.250000000000001),
        ("-0", 0.0),
        ("1e-9999", -0.0),
    ],
)
def test_missing_mismatch_and_signed_zero_mismatch_are_rejected(
    source_value: str, value: float | None
) -> None:
    _assert_rejected(_row(source_value=source_value, value=value))


@pytest.mark.parametrize(
    "field_name",
    ["date", "realtime_start", "realtime_end"],
)
@pytest.mark.parametrize(
    "bad_date",
    [
        datetime(2026, 1, 2),
        pd.Timestamp("2026-01-02T00:00:00"),
        "2026-1-2",
        "20260102",
        "2026-02-30",
        1767312000,
        20260102.0,
    ],
)
def test_dates_reject_datetime_timestamp_epoch_and_noncanonical_strings(
    field_name: str, bad_date: object
) -> None:
    _assert_rejected(_row(**{field_name: bad_date}))


def test_dates_accept_exact_date_objects_and_canonical_strings() -> None:
    row = FredSofrObservation.model_validate(
        _row(date=date(2026, 1, 2), realtime_start="2026-01-03", realtime_end=date(2026, 1, 4))
    )

    assert row.date == date(2026, 1, 2)
    assert row.realtime_start == date(2026, 1, 3)
    assert row.realtime_end == date(2026, 1, 4)


def test_realtime_interval_order_is_inherited() -> None:
    _assert_rejected(_row(realtime_start="2026-01-05", realtime_end="2026-01-04"))


@pytest.mark.parametrize("output_type", [True, False, 1.0, "1", "4", 0, 5])
def test_output_type_rejects_boolean_noninteger_and_out_of_range_values(
    output_type: object,
) -> None:
    _assert_rejected(_row(output_type=output_type))


@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_output_type_accepts_exact_supported_integers(output_type: int) -> None:
    assert (
        FredSofrObservation.model_validate(_row(output_type=output_type)).output_type == output_type
    )


def test_unknown_fields_are_forbidden() -> None:
    _assert_rejected(_row(unrecognized="extra"))


def test_all_eleven_fields_round_trip_through_json_and_contract_frame() -> None:
    row = FredSofrObservation.model_validate(
        _row(
            date="2026-02-03",
            realtime_start="2026-02-04",
            realtime_end="2026-02-05",
            source_value="+4.25000e+0",
        )
    )
    expected_fields = list(FredSofrObservation.model_fields)

    from_json = FredSofrObservation.model_validate_json(row.model_dump_json())
    frame = FredSofrObservation.to_frame([row])
    from_frame = FredSofrObservation.from_frame(frame)

    assert len(expected_fields) == 11
    assert list(row.model_dump()) == expected_fields
    assert list(frame.columns) == expected_fields
    assert from_json == row
    assert from_frame == [row]
    assert from_json.source_value == "+4.25000e+0"
    assert from_frame[0].source_value == "+4.25000e+0"


def test_missing_marker_round_trips_as_dot_and_none() -> None:
    row = FredSofrObservation.model_validate(_row(source_value=".", value=None))

    assert FredSofrObservation.model_validate_json(row.model_dump_json()) == row
    assert FredSofrObservation.from_frame(FredSofrObservation.to_frame([row])) == [row]


def test_contract_frame_does_not_silently_strip_datetime_from_dates() -> None:
    frame = FredSofrObservation.to_frame([FredSofrObservation.model_validate(_row())])
    frame.at[0, "date"] = pd.Timestamp("2026-01-02T01:30:00")

    with pytest.raises(ValueError, match="date must not include a time"):
        FredSofrObservation.from_frame(frame)


def test_original_series_observation_contract_remains_unchanged() -> None:
    generic = SeriesObservation.model_validate(
        {
            "series_id": "GDP",
            "date": "2026-01-02",
            "value": 4,
            "realtime_start": "2026-01-03",
            "realtime_end": "2026-01-04",
            "transform_units": "lin",
            "output_type": 1,
            "requested_frequency": "d",
            "requested_aggregation_method": "avg",
        }
    )

    assert generic.value == 4.0
    assert "source_value" not in SeriesObservation.model_fields
    assert SeriesObservation.from_frame(SeriesObservation.to_frame([generic])) == [generic]


class _DateSubclass(date):
    pass


def test_date_subclasses_are_rejected() -> None:
    _assert_rejected(_row(date=_DateSubclass(2026, 1, 2)))
