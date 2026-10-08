"""Request-context contracts for generic FRED observation rows."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.economic_series import FredFrequency, SeriesObservation
from opendata.data.providers.fred import models as fred_models
from opendata.data.providers.fred.models.series import FredSeriesFetcher


def _raw_observation(**updates: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "date": "2026-01-01",
        "value": "123.5",
        "realtime_start": "2026-01-01",
        "realtime_end": "9999-12-31",
    }
    raw.update(updates)
    return raw


def _normalize(
    raw: dict[str, object], *, frequency: FredFrequency | None, aggregation_method: str
) -> SeriesObservation:
    fetcher = FredSeriesFetcher()
    query = fetcher.transform_query(
        series_id="GDP",
        frequency=frequency,
        aggregation_method=aggregation_method,
    )
    row = fetcher.transform_data([raw], query)[0]
    assert isinstance(row, SeriesObservation)
    return row


def _contract_values(**updates: object) -> dict[str, Any]:
    values: dict[str, Any] = {
        "series_id": "GDP",
        "date": date(2026, 1, 1),
        "value": 123.5,
        "realtime_start": date(2026, 1, 1),
        "realtime_end": date(9999, 12, 31),
        "transform_units": "lin",
        "output_type": 1,
        "requested_frequency": None,
        "requested_aggregation_method": "avg",
    }
    values.update(updates)
    return values


def test_identical_raw_observation_keeps_frequency_and_aggregation_request_context() -> None:
    raw = _raw_observation()
    quarterly_average = _normalize(raw, frequency="q", aggregation_method="avg")
    quarterly_sum = _normalize(raw, frequency="q", aggregation_method="sum")
    native_average = _normalize(raw, frequency=None, aggregation_method="avg")

    assert quarterly_average.date == quarterly_sum.date == native_average.date
    assert quarterly_average.value == quarterly_sum.value == native_average.value
    assert (
        quarterly_average.requested_frequency,
        quarterly_average.requested_aggregation_method,
    ) == (
        "q",
        "avg",
    )
    assert (quarterly_sum.requested_frequency, quarterly_sum.requested_aggregation_method) == (
        "q",
        "sum",
    )
    assert (native_average.requested_frequency, native_average.requested_aggregation_method) == (
        None,
        "avg",
    )


def test_raw_context_like_keys_cannot_override_validated_query_context() -> None:
    raw = _raw_observation(
        requested_frequency="m",
        requested_aggregation_method="sum",
    )

    row = _normalize(raw, frequency="q", aggregation_method="avg")

    assert row.requested_frequency == "q"
    assert row.requested_aggregation_method == "avg"


def test_distinct_realtime_revisions_are_preserved_with_request_context() -> None:
    fetcher = FredSeriesFetcher()
    query = fetcher.transform_query(series_id="GDP", frequency="q", aggregation_method="sum")
    raw_rows = [
        _raw_observation(
            value="123.5",
            realtime_start="2026-02-01",
            realtime_end="2026-02-14",
            requested_frequency="m",
        ),
        _raw_observation(
            value="124.0",
            realtime_start="2026-02-15",
            requested_aggregation_method="avg",
        ),
    ]

    result = fetcher.transform_data(raw_rows, query)

    assert len(result) == 2
    assert all(isinstance(row, SeriesObservation) for row in result)
    assert [row.value for row in result] == [123.5, 124.0]
    assert [row.realtime_start for row in result] == [date(2026, 2, 1), date(2026, 2, 15)]
    assert all(row.requested_frequency == "q" for row in result)
    assert all(row.requested_aggregation_method == "sum" for row in result)


@pytest.mark.parametrize("missing_field", ["requested_frequency", "requested_aggregation_method"])
def test_request_context_fields_are_required(missing_field: str) -> None:
    values = _contract_values()
    values.pop(missing_field)

    with pytest.raises(ValidationError):
        SeriesObservation.model_validate(values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"requested_frequency": "yearly"},
        {"requested_aggregation_method": "median"},
    ],
)
def test_contract_rejects_unknown_request_context_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SeriesObservation.model_validate(_contract_values(**overrides))


@pytest.mark.parametrize(
    "query",
    [
        {"frequency": "yearly"},
        {"aggregation_method": "median"},
    ],
)
def test_query_rejects_unknown_request_context_values(query: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FredSeriesFetcher().transform_query(series_id="GDP", **query)


def test_frame_roundtrip_preserves_null_and_nonnull_request_context() -> None:
    rows = [
        _normalize(_raw_observation(), frequency=None, aggregation_method="avg"),
        _normalize(_raw_observation(), frequency="q", aggregation_method="sum"),
    ]

    restored = SeriesObservation.from_frame(SeriesObservation.to_frame(rows))

    assert restored == rows
    assert restored[0].requested_frequency is None
    assert restored[0].requested_aggregation_method == "avg"
    assert restored[1].requested_frequency == "q"
    assert restored[1].requested_aggregation_method == "sum"


def test_json_schema_requires_request_context_and_frequency_alias_is_shared() -> None:
    schema = SeriesObservation.model_json_schema()
    required = set(schema["required"])
    series_frequency = fred_models.series.FredFrequency
    frequency_annotation = SeriesObservation.model_fields["requested_frequency"].annotation

    assert {"requested_frequency", "requested_aggregation_method"} <= required
    assert series_frequency is FredFrequency
    assert FredFrequency in getattr(frequency_annotation, "__args__", ())
