"""Offline Fetcher-stage tests for FRED SONIA.

The narrow client doubles exercise Fetcher wiring and normalization only;
they do not prove transport, page-header validation, pagination, or FRED
response behavior.
"""

from __future__ import annotations

import asyncio
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from pydantic import ValidationError

import opendata.data.providers.fred.models.sonia as sonia_module
from opendata.data.models.fred_sonia import FredSoniaObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sonia_client import (
    validate_sonia_pages as validate_actual_sonia_pages,
)
from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    reserve_scoped_attempt,
)

SELECTORS = (
    ("rate", "IUDSOIA", "Percent"),
    ("index", "IUDZOS2", "Index"),
    ("10th_percentile", "IUDZLS6", "Percent"),
    ("25th_percentile", "IUDZLS7", "Percent"),
    ("75th_percentile", "IUDZLS8", "Percent"),
    ("90th_percentile", "IUDZLS9", "Percent"),
    ("total_nominal_value", "IUDZLT2", "Millions of Pounds"),
)
TRANSFORMS = ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")
FREQUENCIES = (
    None,
    "d",
    "w",
    "bw",
    "m",
    "q",
    "sa",
    "a",
    "wef",
    "weth",
    "wew",
    "wetu",
    "wem",
    "wesu",
    "wesa",
    "bwew",
    "bwem",
)
AGGREGATIONS = ("avg", "sum", "eop")


def make_query(**overrides: object) -> FredSoniaQuery:
    values: dict[str, object] = {"parameter": "rate"}
    values.update(overrides)
    return FredSoniaQuery.model_validate(values)


def make_row(
    *,
    date: str = "2025-01-02",
    value: str = "4.25",
    realtime_start: str = "2025-01-02",
    realtime_end: str = "9999-12-31",
) -> dict[str, object]:
    return {
        "date": date,
        "value": value,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
    }


def make_page(
    *rows: dict[str, object],
    units: str = "lin",
    output_type: int = 1,
) -> dict[str, Any]:
    return {
        "realtime_start": "2025-01-02",
        "realtime_end": "9999-12-31",
        "observation_start": "2025-01-02",
        "observation_end": "2025-01-03",
        "units": units,
        "output_type": output_type,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(rows),
        "offset": 0,
        "limit": 100_000,
        "observations": list(rows),
        "test_header_marker": {"preserve": True},
    }


def make_validated_page(
    query: FredSoniaQuery,
    *rows: dict[str, str],
) -> dict[str, Any]:
    first_date = rows[0]["date"] if rows else "2025-01-02"
    last_date = rows[-1]["date"] if rows else first_date
    realtime_start = query.as_of or query.realtime_start or date(2025, 1, 2)
    realtime_end = query.as_of or query.realtime_end or date(9999, 12, 31)
    observation_start = query.start_date or date.fromisoformat(first_date)
    observation_end = query.end_date or date.fromisoformat(last_date)
    return {
        "count": len(rows),
        "offset": query.offset,
        "limit": min(query.page_size, query.max_records),
        "units": query.transform_units,
        "output_type": query.output_type,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": query.sort_order,
        "realtime_start": realtime_start.isoformat(),
        "realtime_end": realtime_end.isoformat(),
        "observation_start": observation_start.isoformat(),
        "observation_end": observation_end.isoformat(),
        "series_id": query.series_id,
        "observations": list(rows),
    }


def wire_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
    *,
    rows: tuple[dict[str, object], ...] | None = None,
    fetch: Any | None = None,
    validate: Any | None = None,
) -> dict[str, list[object]]:
    """Patch the two client seams without changing imported module identity."""
    calls: dict[str, list[object]] = {"fetch": [], "validate": [], "attempts": []}

    def fake_fetch(query: FredSoniaQuery, *, timeout: float | None = None) -> object:
        calls["fetch"].append((query, timeout))
        if current_request_scopes():
            reserve_scoped_attempt(
                current_request_scopes(),
                "fred",
                "api.stlouisfed.org",
            )
            calls["attempts"].append((query, timeout))
        if fetch is not None:
            return fetch(query, timeout=timeout)
        return pages

    def fake_validate(
        received_pages: tuple[dict[str, Any], ...],
        query: FredSoniaQuery,
    ) -> object:
        calls["validate"].append((received_pages, query))
        if validate is not None:
            return validate(received_pages, query)
        if rows is not None:
            return rows
        return tuple(row for page in received_pages for row in page["observations"])

    monkeypatch.setattr(sonia_module, "fetch_sonia_pages", fake_fetch)
    monkeypatch.setattr(sonia_module, "validate_sonia_pages", fake_validate)
    return calls


@pytest.fixture(autouse=True)
def install_fetcher_stage_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make each test offline and explicit about its Fetcher-stage boundary."""
    wire_fake_client(monkeypatch, (make_page(make_row()),))


def make_budget(
    *,
    model: str = "SONIA",
    task_attempts: int = 3,
    source_attempts: int = 3,
) -> RequestBudget:
    grant = RequestGrant(
        source="fred",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-evidence:offline-sonia-fetcher-stage",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=(grant,),
    )


def test_capability_is_unverified_variable_period_and_sonia_specific() -> None:
    assert sonia_module.FredSoniaFetcher.async_mode == "bounded_thread"
    assert sonia_module.FredSoniaFetcher.canonical_model == "SONIA"
    assert sonia_module.FredSoniaFetcher.capability.model_dump() == {
        "asset_class": "macro",
        "domain": "sonia",
        "period": "variable",
        "market": "gb",
        "source": "fred",
        "verified": False,
        "notes": "",
    }


@pytest.mark.parametrize(("parameter", "series_id", "native_units"), SELECTORS)
def test_all_seven_selectors_keep_their_typed_identity_and_native_unit(
    parameter: str,
    series_id: str,
    native_units: str,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(make_row(value="1.2500"))
    query = make_query(parameter=parameter)

    result = fetcher.transform_data((page,), query)

    assert len(result) == 1
    observation = cast("FredSoniaObservation", result[0])
    assert observation.parameter == parameter
    assert observation.series_id == series_id
    assert observation.native_units == native_units
    assert observation.source_value == "1.2500"
    assert observation.value == 1.25


@pytest.mark.parametrize(
    ("token", "expected", "sign"),
    (
        ("-0.000", -0.0, -1.0),
        ("-1e-10000", -0.0, -1.0),
        ("+1.2300e+2", 123.0, 1.0),
    ),
)
def test_source_token_precision_signed_zero_and_underflow_are_retained(
    token: str,
    expected: float,
    sign: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(make_row(value=token))
    wire_fake_client(monkeypatch, (page,))

    observation = cast("FredSoniaObservation", fetcher.transform_data((page,), make_query())[0])

    assert observation.source_value == token
    assert observation.value == expected
    assert observation.value is not None
    assert math.copysign(1.0, observation.value) == sign


def test_dot_missing_marker_is_retained_as_none_across_contract_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(make_row(value="."))
    wire_fake_client(monkeypatch, (page,))

    observation = cast("FredSoniaObservation", fetcher.transform_data((page,), make_query())[0])

    assert observation.source_value == "."
    assert observation.value is None
    assert FredSoniaObservation.model_validate_json(observation.model_dump_json()) == observation
    assert FredSoniaObservation.from_frame(FredSoniaObservation.to_frame([observation])) == [
        observation
    ]


@pytest.mark.parametrize("transform_units", TRANSFORMS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_transform_and_frequency_request_context_survives_normalization(
    transform_units: str,
    frequency: str | None,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(
        make_row(date="2025-01-02", value="2.00"),
        units=transform_units,
    )
    query = make_query(
        transform_units=transform_units,
        frequency=frequency,
        aggregation_method="sum",
        start_date="2025-01-02",
        end_date="2025-01-02",
    )

    observation = cast("FredSoniaObservation", fetcher.transform_data((page,), query)[0])

    assert observation.date.isoformat() == "2025-01-02"
    assert observation.source_value == "2.00"
    assert observation.transform_units == transform_units
    assert observation.requested_frequency == frequency
    assert observation.requested_aggregation_method == "sum"


@pytest.mark.parametrize("aggregation_method", AGGREGATIONS)
def test_aggregation_method_is_recorded_without_rewriting_the_source_value(
    aggregation_method: str,
) -> None:
    page = make_page(make_row(value="3.500"))
    query = make_query(aggregation_method=aggregation_method)

    observation = cast(
        "FredSoniaObservation",
        sonia_module.FredSoniaFetcher().transform_data((page,), query)[0],
    )

    assert observation.requested_aggregation_method == aggregation_method
    assert observation.source_value == "3.500"


@pytest.mark.parametrize("output_type", (1, 2, 3, 4))
def test_output_type_is_recorded_as_request_context(output_type: int) -> None:
    page = make_page(make_row(), output_type=output_type)
    query = make_query(output_type=output_type)

    observation = cast(
        "FredSoniaObservation",
        sonia_module.FredSoniaFetcher().transform_data((page,), query)[0],
    )

    assert observation.output_type == output_type
    # Output type 4 records a request; this Fetcher does not infer PIT rows.


def test_revision_rows_keep_distinct_source_intervals_without_deduplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = (
        make_row(
            date="2025-01-02",
            value="4.00",
            realtime_start="2025-01-03",
            realtime_end="2025-01-09",
        ),
        make_row(
            date="2025-01-02",
            value="4.25",
            realtime_start="2025-01-10",
            realtime_end="9999-12-31",
        ),
    )
    page = make_page(*rows, output_type=4)
    wire_fake_client(monkeypatch, (page,), rows=rows)

    observations = sonia_module.FredSoniaFetcher().transform_data(
        (page,), make_query(output_type=4)
    )

    assert len(observations) == 2
    assert [item.source_value for item in observations] == ["4.00", "4.25"]
    assert [item.realtime_start.isoformat() for item in observations] == [
        "2025-01-03",
        "2025-01-10",
    ]


def test_json_and_frame_preserve_all_twelve_sonia_contract_fields() -> None:
    page = make_page(
        make_row(
            value="-0.000",
            realtime_start="2025-01-03",
            realtime_end="2025-02-01",
        )
    )
    observation = cast(
        "FredSoniaObservation",
        sonia_module.FredSoniaFetcher().transform_data((page,), make_query())[0],
    )
    expected_fields = (
        "series_id",
        "date",
        "value",
        "realtime_start",
        "realtime_end",
        "transform_units",
        "output_type",
        "requested_frequency",
        "requested_aggregation_method",
        "parameter",
        "source_value",
        "native_units",
    )

    assert tuple(FredSoniaObservation.model_fields) == expected_fields
    assert FredSoniaObservation.model_validate_json(observation.model_dump_json()) == observation
    frame = FredSoniaObservation.to_frame([observation])
    assert list(frame.columns) == list(expected_fields)
    restored = FredSoniaObservation.from_frame(frame)
    assert restored == [observation]
    assert restored[0].source_value == "-0.000"
    assert restored[0].value is not None
    assert math.copysign(1.0, restored[0].value) == -1.0


def test_daily_window_rejects_late_row_without_returning_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    rows = (
        make_row(date="2025-01-02", value="1.0"),
        make_row(date="2025-01-04", value="2.0"),
    )
    pages = (make_page(*rows),)
    calls = wire_fake_client(monkeypatch, pages, rows=rows)
    query = make_query(start_date="2025-01-02", end_date="2025-01-03")

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data(pages, query)

    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert caught.value.__cause__ is None
    assert len(calls["validate"]) == 1


def test_low_frequency_keeps_source_period_label_without_daily_window_filter() -> None:
    query = make_query(frequency="m", start_date="2025-01-15", end_date="2025-01-31")
    page = make_validated_page(query, make_row(date="2024-12-01", value="4.0"))

    fetcher = sonia_module.FredSoniaFetcher()
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(sonia_module, "validate_sonia_pages", validate_actual_sonia_pages)
        observation = cast("FredSoniaObservation", fetcher.transform_data((page,), query)[0])

    assert observation.date.isoformat() == "2024-12-01"
    assert observation.requested_frequency == "m"
    # No aggregate window-label behavior is asserted here.


@pytest.mark.parametrize("invalid_query_kind", ("object", "constructed", "copied", "tampered"))
def test_direct_extract_rejects_bad_query_before_client_call(
    invalid_query_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    calls = wire_fake_client(monkeypatch, (make_page(make_row()),))
    valid = make_query()
    if invalid_query_kind == "object":
        invalid: object = object()
    elif invalid_query_kind == "constructed":
        invalid = FredSoniaQuery.model_construct(parameter="nonsense", series_id="IUDSOIA")
    elif invalid_query_kind == "copied":
        invalid = valid.model_copy(update={"output_type": True})
    else:
        invalid = valid
        object.__setattr__(invalid, "parameter", "index")

    with pytest.raises(FredProviderError) as caught:
        fetcher.extract_data(cast("FredSoniaQuery", invalid), FetchContext())

    assert caught.value.code == "FRED_BAD_QUERY"
    assert str(caught.value) == "FRED_BAD_QUERY"
    assert caught.value.__cause__ is None
    assert calls["fetch"] == []


def test_transform_query_preserves_pydantic_validation_for_client_400s() -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    with pytest.raises(ValidationError):
        fetcher.transform_query(parameter="not-a-sonia-selector")
    with pytest.raises(ValidationError):
        fetcher.transform_query(parameter="rate", output_type=True)
    with pytest.raises(ValidationError):
        fetcher.transform_query(parameter="rate", unexpected=True)


def test_direct_normalize_revalidates_query_before_client_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    pages = (make_page(make_row()),)
    calls = wire_fake_client(monkeypatch, pages)
    invalid = make_query().model_copy(update={"parameter": "index"})

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data(pages, invalid)

    assert caught.value.code == "FRED_BAD_QUERY"
    assert caught.value.__cause__ is None
    assert calls["validate"] == []


def test_invalid_raw_shapes_and_validator_rows_fail_with_stable_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(make_row(value="SECRET-TOKEN"))
    query = make_query()
    wire_fake_client(
        monkeypatch,
        (page,),
        validate=lambda _pages, _query: (make_row(value="1.0"), object()),
    )

    with pytest.raises(FredProviderError) as malformed_rows:
        fetcher.transform_data((page,), query)
    assert malformed_rows.value.code == "FRED_BAD_OBSERVATION"
    assert malformed_rows.value.__cause__ is None

    with pytest.raises(FredProviderError) as malformed_pages:
        fetcher.transform_data(cast("Any", [page]), query)
    assert malformed_pages.value.code == "FRED_BAD_OBSERVATION"
    assert malformed_pages.value.__cause__ is None


def test_bad_last_source_value_is_transactional_and_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    rows = (
        make_row(value="1.0"),
        make_row(value="PRIVATE-NaN-TOKEN"),
    )
    page = make_page(*rows)
    wire_fake_client(monkeypatch, (page,), rows=rows)

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data((page,), make_query())

    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert "PRIVATE-NaN-TOKEN" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_client_validator_exception_is_safely_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    page = make_page(make_row(value="PRIVATE-CLIENT-TOKEN"))
    wire_fake_client(
        monkeypatch,
        (page,),
        validate=lambda _pages, _query: (_ for _ in ()).throw(ValueError("PRIVATE-CLIENT-TOKEN")),
    )

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data((page,), make_query())

    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("output_type", (2, 3))
def test_unknown_alternate_output_shape_fails_closed_at_client_seam(
    output_type: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    query = make_query(output_type=output_type)
    good = make_row(value="1.0")
    malformed = {**make_row(date="2025-01-03", value="2.0"), "unknown": "not-supported"}
    page = make_validated_page(query, good, malformed)
    monkeypatch.setattr(sonia_module, "validate_sonia_pages", validate_actual_sonia_pages)

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data((page,), query)

    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert caught.value.__cause__ is None
    assert caught.value.__cause__ is None


def test_real_client_validation_feeds_fetcher_without_losing_source_token_or_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = make_query(
        parameter="total_nominal_value",
        start_date="2025-01-02",
        end_date="2025-01-02",
    )
    row = make_row(date="2025-01-02", value="-0.000")
    page = make_validated_page(query, row)
    monkeypatch.setattr(sonia_module, "validate_sonia_pages", validate_actual_sonia_pages)

    observation = cast(
        "FredSoniaObservation",
        sonia_module.FredSoniaFetcher().transform_data((page,), query)[0],
    )

    assert observation.parameter == "total_nominal_value"
    assert observation.series_id == "IUDZLT2"
    assert observation.native_units == "Millions of Pounds"
    assert observation.source_value == "-0.000"
    assert observation.value is not None
    assert math.copysign(1.0, observation.value) == -1.0


def test_extraction_keeps_complete_page_envelope_and_passes_context_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    pages = (make_page(make_row(value="1.000")),)
    calls = wire_fake_client(monkeypatch, pages)

    returned = fetcher.extract_data(make_query(), FetchContext(timeout=2.75))

    assert returned is pages
    assert returned[0]["test_header_marker"] == {"preserve": True}
    assert calls["fetch"] == [(make_query(), 2.75)]


def test_extraction_rejects_a_non_tuple_page_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    wire_fake_client(
        monkeypatch,
        (make_page(make_row()),),
        fetch=lambda _query, **_kwargs: [make_page(make_row())],
    )

    with pytest.raises(FredProviderError) as caught:
        fetcher.extract_data(make_query(), FetchContext())

    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert caught.value.__cause__ is None


def test_unexpected_transport_exception_is_mapped_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    wire_fake_client(
        monkeypatch,
        (make_page(make_row()),),
        fetch=lambda _query, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SECRET-TRANSPORT-TOKEN")
        ),
    )

    with pytest.raises(FredProviderError) as caught:
        fetcher.extract_data(make_query(), FetchContext(timeout=1.0))

    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert "SECRET-TRANSPORT-TOKEN" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_ungranted_and_wrong_model_grants_stop_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    calls = wire_fake_client(monkeypatch, (make_page(make_row()),))

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(parameter="rate")
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(
            ctx=FetchContext(request_budget=make_budget(model="SOFR")),
            parameter="rate",
        )

    assert calls["fetch"] == []


def test_sync_async_and_captured_paths_preserve_pages_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    pages = (make_page(make_row(value="1.00")),)
    calls = wire_fake_client(monkeypatch, pages)
    ctx = FetchContext(timeout=2.5, request_budget=make_budget())

    sync_result = fetcher.fetch(ctx=ctx, parameter="rate")

    async def run() -> tuple[Any, Any]:
        ordinary = await fetcher.fetch_async(ctx=ctx, parameter="rate")
        captured = await fetcher.fetch_captured_async(
            ctx=ctx,
            capture=lambda raw, _query: raw,
            parameter="rate",
        )
        return ordinary, captured

    async_result, captured = asyncio.run(run())

    assert sync_result == async_result == captured.results
    assert captured.capture == pages
    assert captured.capture[0]["test_header_marker"] == {"preserve": True}
    assert [call[1] for call in calls["fetch"]] == [2.5, 2.5, 2.5]
    assert [call[1].parameter for call in calls["validate"]] == ["rate", "rate", "rate"]
    assert len(calls["attempts"]) == 3


def test_cancelled_context_and_exhausted_budget_stop_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sonia_module.FredSoniaFetcher()
    calls = wire_fake_client(monkeypatch, (make_page(make_row()),))
    cancelled = threading.Event()
    cancelled.set()

    async def run() -> None:
        with pytest.raises(RequestExecutionCancelledError):
            await fetcher.fetch_async(
                ctx=FetchContext(
                    _thread_cancel_event=cancelled,
                    request_budget=make_budget(),
                ),
                parameter="rate",
            )
        with pytest.raises(RequestAttemptLimitError):
            await fetcher.fetch_async(
                ctx=FetchContext(
                    request_budget=make_budget(task_attempts=0, source_attempts=0),
                ),
                parameter="rate",
            )

    asyncio.run(run())

    assert len(calls["fetch"]) == 1
    assert calls["attempts"] == []
