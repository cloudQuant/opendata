"""Offline tests for the SOFR Fetcher stage with a narrow client test double.

The fake client in these tests exercises only Fetcher wiring and normalization;
it does not prove FRED transport, headers, pagination, or upstream behavior.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from pydantic import ValidationError

import opendata.data.providers.fred.models.sofr as sofr_module
from opendata.data.models.fred_sofr import FredSofrObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery
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

SERIES = ("SOFR", "SOFR30DAYAVG", "SOFR90DAYAVG", "SOFR180DAYAVG", "SOFRINDEX")
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


def make_query(**overrides: object) -> FredSofrQuery:
    values: dict[str, object] = {"series_id": "SOFR"}
    values.update(overrides)
    return FredSofrQuery.model_validate(values)


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


def make_page(*rows: dict[str, object]) -> dict[str, Any]:
    return {
        "realtime_start": "2025-01-02",
        "realtime_end": "9999-12-31",
        "observation_start": "2025-01-02",
        "observation_end": "2025-01-03",
        "units": "Percent",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(rows),
        "offset": 0,
        "limit": 100_000,
        "observations": list(rows),
        "test_header_marker": {"preserve": True},
    }


def wire_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
    *,
    rows: tuple[dict[str, object], ...] | None = None,
    fetch: Any | None = None,
    validate: Any | None = None,
) -> dict[str, list[object]]:
    """Install deterministic Fetcher-stage stubs, never actual HTTP logic."""
    calls: dict[str, list[object]] = {"fetch": [], "attempts": [], "validate": []}

    def fake_fetch(query: FredSofrQuery, *, timeout: float | None = None) -> object:
        calls["fetch"].append((query, timeout))
        reserve_scoped_attempt(current_request_scopes(), "fred", "api.stlouisfed.org")
        calls["attempts"].append((query, timeout))
        if fetch is not None:
            return fetch(query, timeout=timeout)
        return pages

    def fake_validate(received_pages: tuple[dict[str, Any], ...], query: FredSofrQuery) -> object:
        calls["validate"].append((received_pages, query))
        if validate is not None:
            return validate(received_pages, query)
        if rows is not None:
            return rows
        return tuple(row for page in received_pages for row in page["observations"])

    monkeypatch.setattr(sofr_module, "fetch_sofr_pages", fake_fetch)
    monkeypatch.setattr(sofr_module, "validate_sofr_pages", fake_validate)
    return calls


@pytest.fixture(autouse=True)
def install_fetcher_stage_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use a local page flattener; no HTTP, pagination, or upstream proof."""
    wire_fake_client(monkeypatch, (make_page(make_row()),))


def make_budget(
    *,
    model: str = "SOFR",
    task_attempts: int = 3,
    source_attempts: int = 3,
) -> RequestBudget:
    grant = RequestGrant(
        source="fred",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-evidence:offline-sofr-fetcher-stage",
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


def test_capability_is_unverified_variable_period_and_sofr_specific() -> None:
    assert sofr_module.FredSofrFetcher.async_mode == "bounded_thread"
    assert sofr_module.FredSofrFetcher.canonical_model == "SOFR"
    assert sofr_module.FredSofrFetcher.capability.model_dump() == {
        "asset_class": "macro",
        "domain": "sofr",
        "period": "variable",
        "market": "us",
        "source": "fred",
        "verified": False,
        "notes": "",
    }


@pytest.mark.parametrize("series_id", SERIES)
def test_each_of_five_series_gets_its_exact_native_unit(series_id: str) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(value="1.2500"))
    result = fetcher.transform_data((page,), make_query(series_id=series_id))

    assert len(result) == 1
    observation = cast("FredSofrObservation", result[0])
    assert observation.series_id == series_id
    assert observation.native_units == (
        "Index Apr 2, 2018 = 1" if series_id == "SOFRINDEX" else "Percent"
    )
    assert observation.source_value == "1.2500"
    assert observation.value == 1.25


@pytest.mark.parametrize(
    ("token", "expected", "sign"),
    (("-0.000", -0.0, -1.0), ("-1e-10000", -0.0, -1.0), ("+1.2300e+2", 123.0, 1.0)),
)
def test_source_token_precision_signed_zero_and_underflow_are_retained(
    token: str, expected: float, sign: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(value=token))
    wire_fake_client(monkeypatch, (page,))

    observation = cast("FredSofrObservation", fetcher.transform_data((page,), make_query())[0])

    assert observation.source_value == token
    assert observation.value == expected
    assert math_copysign(observation.value) == sign


def math_copysign(value: float | None) -> float:
    import math

    assert value is not None
    return math.copysign(1.0, value)


def test_dot_missing_marker_roundtrips_with_none_source_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(value="."))
    wire_fake_client(monkeypatch, (page,))
    observation = cast("FredSofrObservation", fetcher.transform_data((page,), make_query())[0])

    assert observation.source_value == "."
    assert observation.value is None
    assert FredSofrObservation.model_validate_json(observation.model_dump_json()) == observation


@pytest.mark.parametrize("transform_units", TRANSFORMS)
@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_transform_and_frequency_contexts_are_recorded_without_rewriting_source_label(
    transform_units: str, frequency: str | None
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(date="2025-01-02", value="2.00"))
    query = make_query(
        transform_units=transform_units,
        frequency=frequency,
        aggregation_method="sum",
        start_date="2025-01-02",
        end_date="2025-01-02",
    )

    observation = cast("FredSofrObservation", fetcher.transform_data((page,), query)[0])

    assert observation.date.isoformat() == "2025-01-02"
    assert observation.source_value == "2.00"
    assert observation.transform_units == transform_units
    assert observation.requested_frequency == frequency
    assert observation.requested_aggregation_method == "sum"


def test_output_types_are_recorded_as_request_context_only() -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row())
    for output_type in (1, 2, 3, 4):
        observation = cast(
            "FredSofrObservation",
            fetcher.transform_data((page,), make_query(output_type=output_type))[0],
        )
        assert observation.output_type == output_type
    # Type 4 is context only: this Fetcher does not infer first publication.


def test_model_json_and_frame_preserve_all_eleven_sofr_fields() -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(
        make_row(value="-0.000", realtime_start="2025-01-03", realtime_end="2025-02-01")
    )
    observation = cast("FredSofrObservation", fetcher.transform_data((page,), make_query())[0])
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
        "source_value",
        "native_units",
    )

    assert tuple(FredSofrObservation.model_fields) == expected_fields
    assert FredSofrObservation.model_validate_json(observation.model_dump_json()) == observation
    frame = FredSofrObservation.to_frame([observation])
    assert list(frame.columns) == list(expected_fields)
    assert FredSofrObservation.from_frame(frame) == [observation]
    restored = FredSofrObservation.from_frame(frame)[0]
    assert restored.source_value == "-0.000"
    assert math_copysign(restored.value) == -1.0


def test_daily_native_window_rejects_any_bad_row_without_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
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
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(date="2024-12-01", value="4.0"))
    query = make_query(frequency="m", start_date="2025-01-15", end_date="2025-01-31")

    observation = cast("FredSofrObservation", fetcher.transform_data((page,), query)[0])

    assert observation.date.isoformat() == "2024-12-01"
    assert observation.requested_frequency == "m"
    # The row label is retained; this test does not assert FRED's aggregate window rule.


@pytest.mark.parametrize("invalid_query_kind", ("object", "constructed", "copied", "tampered"))
def test_direct_extract_rejects_noncanonical_or_invalid_query_before_client(
    invalid_query_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row()),)
    calls = wire_fake_client(monkeypatch, pages)
    valid = make_query()
    if invalid_query_kind == "object":
        invalid: object = object()
    elif invalid_query_kind == "constructed":
        invalid = FredSofrQuery.model_construct(series_id="NOPE")
    elif invalid_query_kind == "copied":
        invalid = valid.model_copy(update={"series_id": "NOPE"})
    else:
        invalid = valid
        object.__setattr__(invalid, "series_id", "NOPE")

    with pytest.raises(FredProviderError) as caught:
        fetcher.extract_data(cast("FredSofrQuery", invalid), FetchContext())

    assert caught.value.code == "FRED_BAD_QUERY"
    assert str(caught.value) == "FRED_BAD_QUERY"
    assert caught.value.__cause__ is None
    assert calls["fetch"] == []


def test_transform_query_keeps_pydantic_validation_for_client_400_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    calls = wire_fake_client(monkeypatch, (make_page(make_row()),))

    with pytest.raises(ValidationError):
        fetcher.transform_query(series_id="NOT_SOFR")

    assert calls["fetch"] == []


def test_direct_normalize_revalidates_query_before_client_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row()),)
    calls = wire_fake_client(monkeypatch, pages)
    invalid = make_query().model_copy(update={"output_type": True})

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data(pages, invalid)

    assert caught.value.code == "FRED_BAD_QUERY"
    assert calls["validate"] == []


def test_raw_shape_and_normalizer_failure_are_safe_and_transactional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(value="1.0"))
    query = make_query()
    wire_fake_client(monkeypatch, (page,), validate=lambda _pages, _query: (make_row(), object()))

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data((page,), query)
    assert caught.value.code == "FRED_BAD_OBSERVATION"
    assert caught.value.__cause__ is None

    with pytest.raises(FredProviderError) as wrong_raw:
        fetcher.transform_data(cast("Any", [page]), query)
    assert wrong_raw.value.code == "FRED_BAD_OBSERVATION"


def test_validator_exceptions_do_not_leak_source_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    page = make_page(make_row(value="PRIVATE-TOKEN"))
    wire_fake_client(
        monkeypatch,
        (page,),
        validate=lambda _pages, _query: (_ for _ in ()).throw(ValueError("PRIVATE-TOKEN")),
    )

    with pytest.raises(FredProviderError) as caught:
        fetcher.transform_data((page,), make_query())

    assert str(caught.value) == "FRED_BAD_OBSERVATION"
    assert "PRIVATE-TOKEN" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_ungranted_and_wrong_model_grants_do_not_extract(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row()),)
    calls = wire_fake_client(monkeypatch, pages)

    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(series_id="SOFR")
    with pytest.raises(RequestAuthorizationError):
        fetcher.fetch(
            ctx=FetchContext(request_budget=make_budget(model="FRED_SERIES")),
            series_id="SOFR",
        )

    assert calls["fetch"] == []


def test_sync_async_and_captured_paths_use_runtime_grant_and_keep_page_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row(value="1.00")),)
    calls = wire_fake_client(monkeypatch, pages)
    ctx = FetchContext(timeout=2.5, request_budget=make_budget())

    sync_result = fetcher.fetch(ctx=ctx, series_id="SOFR")

    async def run() -> tuple[Any, Any]:
        ordinary = await fetcher.fetch_async(ctx=ctx, series_id="SOFR")
        captured = await fetcher.fetch_captured_async(
            ctx=ctx,
            capture=lambda raw, _query: raw,
            series_id="SOFR",
        )
        return ordinary, captured

    async_result, captured = asyncio.run(run())

    assert sync_result == async_result == captured.results
    assert captured.capture == pages
    assert captured.capture[0]["test_header_marker"] == {"preserve": True}
    assert [call[1] for call in calls["fetch"]] == [2.5, 2.5, 2.5]
    assert len(calls["validate"]) == 3


def test_cancelled_context_and_exhausted_budget_stop_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row()),)
    calls = wire_fake_client(monkeypatch, pages)
    cancelled = threading.Event()
    cancelled.set()

    async def run() -> None:
        with pytest.raises(RequestExecutionCancelledError):
            await fetcher.fetch_async(
                ctx=FetchContext(_thread_cancel_event=cancelled, request_budget=make_budget()),
                series_id="SOFR",
            )
        with pytest.raises(RequestAttemptLimitError):
            await fetcher.fetch_async(
                ctx=FetchContext(request_budget=make_budget(task_attempts=0, source_attempts=0)),
                series_id="SOFR",
            )

    asyncio.run(run())
    assert len(calls["fetch"]) == 1
    assert calls["attempts"] == []


@pytest.mark.asyncio
async def test_same_fetcher_instance_serializes_async_client_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = sofr_module.FredSofrFetcher()
    pages = (make_page(make_row()),)
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    calls = 0

    def blocking_fetch(_query: FredSofrQuery, *, timeout: float | None = None) -> object:
        nonlocal active, max_active, calls
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
            current = calls
        if current == 1:
            entered.set()
            release.wait(timeout=2)
        with lock:
            active -= 1
        return pages

    wire_fake_client(monkeypatch, pages, fetch=blocking_fetch)
    budget = make_budget(task_attempts=2, source_attempts=2)
    ctx = FetchContext(request_budget=budget)
    first = asyncio.create_task(fetcher.fetch_async(ctx=ctx, series_id="SOFR"))

    try:
        assert await asyncio.to_thread(entered.wait, 1.0)
        second = asyncio.create_task(fetcher.fetch_async(ctx=ctx, series_id="SOFR"))
        await asyncio.sleep(0.05)
        with lock:
            assert calls == 1
            assert max_active == 1
        release.set()
        await asyncio.gather(first, second)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)

    assert calls == 2
    assert max_active == 1
