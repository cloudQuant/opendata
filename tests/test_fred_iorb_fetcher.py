"""Fetcher-stage tests for IORB normalization and raw-page preservation."""

from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

import opendata.data.providers.fred.models.iorb as iorb_module
from opendata.data.models.fred_iorb import FredIorbObservation
from opendata.data.protocol import CapturedFetch, FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._iorb_query import FredIorbQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    reserve_scoped_attempt,
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


def query(**updates: object) -> FredIorbQuery:
    return FredIorbQuery.model_validate(updates)


def row(
    *,
    date_text: str = "2025-01-02",
    value: str = "4.2500",
    realtime_start: str = "2025-01-01",
    realtime_end: str = "2025-12-31",
) -> dict[str, str]:
    return {
        "date": date_text,
        "value": value,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
    }


def page(query_value: FredIorbQuery, *rows: dict[str, str]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "offset": query_value.offset,
        "limit": min(query_value.page_size, query_value.max_records),
        "units": query_value.transform_units,
        "output_type": query_value.output_type,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": query_value.sort_order,
        "realtime_start": query_value.as_of.isoformat()
        if query_value.as_of is not None
        else (
            query_value.realtime_start.isoformat() if query_value.realtime_start else "1776-07-04"
        ),
        "realtime_end": query_value.as_of.isoformat()
        if query_value.as_of is not None
        else (query_value.realtime_end.isoformat() if query_value.realtime_end else "9999-12-31"),
        "observation_start": query_value.start_date.isoformat()
        if query_value.start_date is not None
        else "1776-07-04",
        "observation_end": query_value.end_date.isoformat()
        if query_value.end_date is not None
        else "9999-12-31",
        "series_id": "IORB",
        "observations": list(rows),
    }


def fake_client(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
    rows: tuple[dict[str, Any], ...] | None = None,
    *,
    fetch_error: BaseException | None = None,
    validate_error: BaseException | None = None,
) -> dict[str, list[object]]:
    calls: dict[str, list[object]] = {"fetch": [], "validate": [], "attempts": []}

    def fake_fetch(query_value: FredIorbQuery, *, timeout: float | None = None) -> object:
        calls["fetch"].append((query_value, timeout))
        scopes = current_request_scopes()
        if scopes:
            reserve_scoped_attempt(scopes, "fred", "api.stlouisfed.org")
            calls["attempts"].append((query_value, timeout))
        if fetch_error is not None:
            raise fetch_error
        return pages

    def fake_validate(
        received_pages: tuple[dict[str, Any], ...],
        query_value: FredIorbQuery,
    ) -> object:
        calls["validate"].append((received_pages, query_value))
        if validate_error is not None:
            raise validate_error
        if rows is not None:
            return rows
        return tuple(
            row_item for source_page in received_pages for row_item in source_page["observations"]
        )

    monkeypatch.setattr(iorb_module, "fetch_iorb_pages", fake_fetch)
    monkeypatch.setattr(iorb_module, "validate_iorb_pages", fake_validate)
    return calls


def make_budget(
    *, model: str = "IORB", task_attempts: int = 3, source_attempts: int = 3
) -> RequestBudget:
    grant = RequestGrant(
        source="fred",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic offline IORB fetcher test",
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


def test_fetcher_declares_only_unverified_variable_iorb_capability() -> None:
    # The consumer of the declared canonical id is ``ProviderRegistry.resolve_model``: the fred
    # descriptor (opendata/data/providers/fred/provider.py) binds no IORB row yet, so the binding
    # is made here with the same operation ``catalog.register_provider`` uses for a real one.
    registry = ProviderRegistry()
    fetcher = iorb_module.FredIorbFetcher()
    registry.register_provider_models("fred", (fetcher.canonical_model,), fetcher)
    assert registry.resolve_model("fred", "IORB") is fetcher
    assert iorb_module.FredIorbFetcher.capability.model_dump() == {
        "asset_class": "macro",
        "domain": "iorb",
        "period": "variable",
        "market": "us",
        "source": "fred",
        "verified": False,
        "notes": "",
    }


def test_query_transform_preserves_typed_validation_error_for_client400() -> None:
    with pytest.raises(ValidationError):
        iorb_module.FredIorbFetcher().transform_query(series_id="SOFR")
    with pytest.raises(ValidationError):
        iorb_module.FredIorbFetcher().transform_query(output_type=True)


@pytest.mark.parametrize("source_token", ["+4.2500e0", "-0", "1e-9999", "."])
def test_normalization_preserves_source_token_unit_and_all_request_context(
    source_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query(
        transform_units="pch",
        frequency="m",
        aggregation_method="sum",
        output_type=4,
        start_date="2025-01-10",
        end_date="2025-01-20",
    )
    source_row = row(date_text="2025-01-02", value=source_token)
    pages = (page(query_value, source_row),)
    fake_client(monkeypatch, pages)
    result = iorb_module.FredIorbFetcher().transform_data(pages, query_value)

    assert len(result) == 1
    item = result[0]
    assert isinstance(item, FredIorbObservation)
    assert item.series_id == "IORB"
    assert item.native_units == "Percent"
    assert item.source_value == source_token
    assert item.transform_units == "pch"
    assert item.requested_frequency == "m"
    assert item.requested_aggregation_method == "sum"
    assert item.output_type == 4
    assert item.realtime_start == date(2025, 1, 1)
    assert item.value is None if source_token == "." else item.value == float(source_token)


def test_transform_and_frequency_contexts_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    source_row = row()
    for transform in TRANSFORMS:
        for frequency in FREQUENCIES:
            query_value = query(transform_units=transform, frequency=frequency)
            pages = (page(query_value, source_row),)
            fake_client(monkeypatch, pages)
            item = iorb_module.FredIorbFetcher().transform_data(pages, query_value)[0]
            assert item.transform_units == transform
            assert item.requested_frequency == frequency


def test_all_output_types_are_recorded_without_pit_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_row = row()
    for output_type in (1, 2, 3, 4):
        query_value = query(output_type=output_type)
        pages = (page(query_value, source_row),)
        fake_client(monkeypatch, pages)
        item = iorb_module.FredIorbFetcher().transform_data(pages, query_value)[0]
        assert item.output_type == output_type


def test_revisions_keep_each_realtime_interval_and_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    first = row(realtime_start="2025-01-01", realtime_end="2025-01-31")
    revised = row(value="4.30", realtime_start="2025-02-01", realtime_end="9999-12-31")
    pages = (page(query_value, first, revised),)
    fake_client(monkeypatch, pages)
    result = iorb_module.FredIorbFetcher().transform_data(pages, query_value)
    assert len(result) == 2
    assert result[0].realtime_start == date(2025, 1, 1)
    assert result[1].realtime_start == date(2025, 2, 1)


def test_daily_date_window_rejects_out_of_range_row_without_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query(start_date="2025-01-10", end_date="2025-01-20")
    rows = (row(date_text="2025-01-15"), row(date_text="2025-01-21"))
    pages = (page(query_value, *rows),)
    fake_client(monkeypatch, pages)
    with pytest.raises(FredProviderError) as error:
        iorb_module.FredIorbFetcher().transform_data(pages, query_value)
    assert error.value.code == "FRED_BAD_OBSERVATION"
    assert error.value.__cause__ is None


def test_low_frequency_preserves_source_period_label_without_daily_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query(frequency="m", start_date="2025-01-15", end_date="2025-01-20")
    pages = (page(query_value, row(date_text="2025-01-01")),)
    fake_client(monkeypatch, pages)
    result = iorb_module.FredIorbFetcher().transform_data(pages, query_value)
    assert result[0].date == date(2025, 1, 1)
    assert result[0].requested_frequency == "m"


def test_bad_tail_row_fails_whole_normalization_without_value_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    pages = (page(query_value, row(), row(value="secret-token")),)
    fake_client(monkeypatch, pages)
    with pytest.raises(FredProviderError) as error:
        iorb_module.FredIorbFetcher().transform_data(pages, query_value)
    assert error.value.code == "FRED_BAD_OBSERVATION"
    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None


def test_bad_raw_shape_and_client_exception_map_to_safe_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    fetcher = iorb_module.FredIorbFetcher()
    fake_client(monkeypatch, ())
    with pytest.raises(FredProviderError) as error:
        fetcher.transform_data([], query_value)  # type: ignore[arg-type]
    assert error.value.code == "FRED_BAD_OBSERVATION"

    fake_client(monkeypatch, (), fetch_error=RuntimeError("private body token"))
    with pytest.raises(FredProviderError) as error:
        fetcher.extract_data(query_value, FetchContext())
    assert error.value.code == "FRED_BAD_OBSERVATION"
    assert "private body token" not in str(error.value)


def test_direct_stage_revalidation_rejects_constructed_copied_mutated_and_wrong_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    pages = (page(query_value, row()),)
    calls = fake_client(monkeypatch, pages)
    malformed = [
        FredIorbQuery.model_construct(series_id="SOFR"),
        query_value.model_copy(update={"series_id": "SOFR"}),
    ]
    mutated = query()
    mutated.series_id = "SOFR"
    malformed.append(mutated)
    malformed.append(FredSeriesQuery.model_validate({"series_id": "IORB"}))  # type: ignore[arg-type]
    fetcher = iorb_module.FredIorbFetcher()
    for invalid in malformed:
        with pytest.raises(FredProviderError) as error:
            fetcher.extract_data(invalid, FetchContext())  # type: ignore[arg-type]
        assert error.value.code == "FRED_BAD_QUERY"
        with pytest.raises(FredProviderError) as error:
            fetcher.transform_data(pages, invalid)  # type: ignore[arg-type]
        assert error.value.code == "FRED_BAD_QUERY"
    assert calls["fetch"] == []
    assert calls["validate"] == []


def test_extraction_passes_timeout_and_retains_full_page_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    pages = (page(query_value, row()),)
    calls = fake_client(monkeypatch, pages)
    result = iorb_module.FredIorbFetcher().extract_data(
        query_value,
        FetchContext(timeout=8.5),
    )
    assert result is pages
    assert calls["fetch"] == [(query_value, 8.5)]


def test_raw_async_and_capture_paths_keep_envelopes_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query_value = query()
    pages = (page(query_value, row(value="4.2500")),)
    calls = fake_client(monkeypatch, pages)
    fetcher = iorb_module.FredIorbFetcher()
    ctx = FetchContext(timeout=6.75, request_budget=make_budget())
    raw = asyncio.run(fetcher.fetch_raw_async(ctx=ctx))
    assert raw == pages
    assert calls["fetch"][-1][1] == 6.75

    captured = asyncio.run(
        fetcher.fetch_captured_async(
            capture=lambda raw_pages, params: (raw_pages, params.model_dump(mode="python")),
            ctx=ctx,
        )
    )
    assert isinstance(captured, CapturedFetch)
    assert captured.capture[0] == pages
    assert captured.capture[1]["series_id"] == "IORB"
    assert captured.results[0].source_value == "4.2500"
    assert calls["fetch"][-1][1] == 6.75


def test_pre_cancelled_async_context_stops_before_client(monkeypatch: pytest.MonkeyPatch) -> None:
    query_value = query()
    pages = (page(query_value, row()),)
    calls = fake_client(monkeypatch, pages)
    cancelled = threading.Event()
    cancelled.set()
    ctx = FetchContext(request_budget=make_budget(), _thread_cancel_event=cancelled)
    with pytest.raises(RequestExecutionCancelledError):
        asyncio.run(iorb_module.FredIorbFetcher().fetch_async(ctx=ctx))
    assert calls["fetch"] == []
