"""Synthetic wire and authorization tests for the IORB page client."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opendata.data.providers.fred.models import _iorb_client as client_module
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._iorb_query import FredIorbQuery
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
)

_SOURCE = "fred"
_MODEL = "IORB"
_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_API_KEY = "synthetic-iorb-key"
_EARLIEST = "1776-07-04"
_LATEST = "9999-12-31"


class _Reply:
    def __init__(self, body: bytes, *, status: object = 200) -> None:
        self.content = body
        self.status_code = status


class _SyntheticGovHTTP:
    """Small offline implementation of the shared governed-client interface."""

    def __init__(self, replies: list[_Reply], after_get: Any | None = None) -> None:
        self.replies = list(replies)
        self.after_get = after_get
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Any, timeout: float | None, source: str) -> _Reply:
        reserve_scoped_attempt(current_request_scopes(), source, _HOST)
        self.calls.append(
            {"url": url, "params": dict(params), "timeout": timeout, "source": source}
        )
        if not self.replies:
            raise AssertionError("unexpected extra FRED request")
        reply = self.replies.pop(0)
        if self.after_get is not None:
            self.after_get(len(self.calls))
        return reply


def _encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _row(
    observation_date: str = "2025-01-02",
    value: str = "4.25",
    *,
    realtime_start: str = "2025-01-01",
    realtime_end: str = "2025-12-31",
) -> dict[str, str]:
    return {
        "date": observation_date,
        "value": value,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
    }


def _query(**updates: object) -> FredIorbQuery:
    return FredIorbQuery.model_validate(updates)


def _page(
    query: FredIorbQuery,
    rows: list[dict[str, str]],
    *,
    count: int,
    offset: int = 0,
    limit: int | None = None,
    **overrides: object,
) -> dict[str, Any]:
    realtime_start = query.as_of or query.realtime_start
    realtime_end = query.as_of or query.realtime_end
    page: dict[str, Any] = {
        "count": count,
        "offset": offset,
        "limit": min(query.page_size, query.max_records) if limit is None else limit,
        "units": query.transform_units,
        "output_type": query.output_type,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": query.sort_order,
        "realtime_start": realtime_start.isoformat() if realtime_start is not None else _EARLIEST,
        "realtime_end": realtime_end.isoformat() if realtime_end is not None else _LATEST,
        "observation_start": (
            query.start_date.isoformat() if query.start_date is not None else _EARLIEST
        ),
        "observation_end": query.end_date.isoformat() if query.end_date is not None else _LATEST,
        "observations": rows,
        "series_id": "IORB",
    }
    page.update(overrides)
    return page


def _grant(
    *,
    source: str = _SOURCE,
    model: str = _MODEL,
    operation: RequestOperation = RequestOperation.QUERY,
    decision: GrantDecision = GrantDecision.ALLOWED,
    host: str = _HOST,
    conditions: tuple[str, ...] = (),
    expires_in: timedelta = timedelta(minutes=5),
    task_attempts: int = 10,
    source_attempts: int = 3,
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="synthetic offline IORB grant",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + expires_in,
        conditions=conditions,
    )


@contextmanager
def _scope(
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    source: str = _SOURCE,
    model: str | None = _MODEL,
    grants: tuple[RequestGrant, ...] | None = None,
    include_grant: bool = True,
    cancellation: threading.Event | None = None,
    task_attempts: int = 10,
    source_attempts: int = 3,
):
    selected = (
        grants
        if grants is not None
        else (
            (
                _grant(
                    operation=operation,
                    task_attempts=task_attempts,
                    source_attempts=source_attempts,
                ),
            )
            if include_grant
            else ()
        )
    )
    budget = RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=selected,
    )
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=operation,
        budget=budget,
        cancellation=cancellation,
    ):
        yield


@pytest.fixture
def install_transport(monkeypatch: pytest.MonkeyPatch):
    def install(
        replies: list[_Reply],
        *,
        key: str = _API_KEY,
        after_get: Any | None = None,
    ) -> tuple[_SyntheticGovHTTP, list[int]]:
        fake = _SyntheticGovHTTP(replies, after_get)
        key_calls: list[int] = []

        def require_key() -> str:
            key_calls.append(1)
            return key

        monkeypatch.setattr(client_module, "get_shared_http_client", lambda: fake)
        monkeypatch.setattr(client_module._client, "require_api_key", require_key)
        return fake, key_calls

    return install


def test_fetch_retains_the_page_envelope_and_sends_exact_iorb_request(
    install_transport: Any,
) -> None:
    query = _query(
        transform_units="pch",
        output_type=3,
        frequency="w",
        aggregation_method="eop",
        sort_order="desc",
        start_date="2025-01-01",
        end_date="2025-01-31",
        realtime_start="2025-02-01",
        realtime_end="2025-03-01",
    )
    envelope = _page(query, [_row()], count=1, extra_metadata={"kept": True})
    fake, key_calls = install_transport([_Reply(_encode(envelope))])
    with _scope():
        pages = client_module.fetch_iorb_pages(query, timeout=7.25)

    assert pages == (envelope,)
    assert pages[0]["extra_metadata"] == {"kept": True}
    assert key_calls == [1]
    assert fake.calls == [
        {
            "url": _URL,
            "source": _SOURCE,
            "timeout": 7.25,
            "params": {
                "series_id": "IORB",
                "api_key": _API_KEY,
                "file_type": "json",
                "units": "pch",
                "output_type": "3",
                "order_by": "observation_date",
                "sort_order": "desc",
                "aggregation_method": "eop",
                "limit": "100000",
                "offset": "0",
                "frequency": "w",
                "observation_start": "2025-01-01",
                "observation_end": "2025-01-31",
                "realtime_start": "2025-02-01",
                "realtime_end": "2025-03-01",
            },
        }
    ]


def test_pagination_uses_finite_page_size_and_exact_offsets(install_transport: Any) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2025-01-02")], count=2, offset=0)
    second = _page(query, [_row("2025-01-03")], count=2, offset=1)
    fake, _ = install_transport([_Reply(_encode(first)), _Reply(_encode(second))])
    with _scope():
        result = client_module.fetch_iorb_pages(query)
    assert len(result) == 2
    assert [call["params"]["offset"] for call in fake.calls] == ["0", "1"]


def test_vintage_selector_is_serialized_in_input_order(install_transport: Any) -> None:
    query = _query(vintage_dates=["2025-01-02", "2025-01-01", "2025-01-02"])
    page = _page(query, [_row()], count=1)
    fake, _ = install_transport([_Reply(_encode(page))])
    with _scope():
        client_module.fetch_iorb_pages(query)
    assert fake.calls[0]["params"]["vintage_dates"] == "2025-01-02,2025-01-01,2025-01-02"


def test_pure_validator_preserves_original_source_tokens_without_io() -> None:
    query = _query()
    page = _page(query, [_row(value="+4.2500e0")], count=1)
    assert client_module.validate_iorb_pages((page,), query) == (page["observations"][0],)


@pytest.mark.parametrize(
    "bad_grant",
    [
        _grant(model="SOFR"),
        _grant(source="other"),
        _grant(operation=RequestOperation.STORE),
        _grant(decision=GrantDecision.DENIED),
        _grant(host="wrong.example"),
        _grant(conditions=("unresolved condition",)),
        _grant(expires_in=timedelta(seconds=-1)),
    ],
)
def test_wrong_or_unusable_grant_fails_before_key_or_http(
    bad_grant: RequestGrant,
    install_transport: Any,
) -> None:
    fake, key_calls = install_transport([])
    with _scope(grants=(bad_grant,)), pytest.raises(RequestAuthorizationError):
        client_module.fetch_iorb_pages(_query())
    assert key_calls == []
    assert fake.calls == []


def test_missing_scope_fails_before_key_or_transport(install_transport: Any) -> None:
    fake, key_calls = install_transport([])
    with pytest.raises(RequestAuthorizationError):
        client_module.fetch_iorb_pages(_query())
    assert key_calls == []
    assert fake.calls == []


@pytest.mark.parametrize("scope_model", [None, "SOFR"])
def test_wrong_scope_identity_fails_before_key_or_transport(
    scope_model: str | None,
    install_transport: Any,
) -> None:
    fake, key_calls = install_transport([])
    with _scope(model=scope_model), pytest.raises(RequestAuthorizationError):
        client_module.fetch_iorb_pages(_query())
    assert key_calls == []
    assert fake.calls == []


def test_constructed_copied_and_mutated_queries_reject_before_credentials(
    install_transport: Any,
) -> None:
    invalid = [
        FredIorbQuery.model_construct(series_id="SOFR"),
        _query().model_copy(update={"series_id": "SOFR"}),
    ]
    mutated = _query()
    mutated.series_id = "SOFR"
    invalid.append(mutated)
    fake, key_calls = install_transport([])
    with _scope():
        for query in invalid:
            with pytest.raises(RequestAuthorizationError):
                client_module.fetch_iorb_pages(query)
    assert key_calls == []
    assert fake.calls == []


def test_blank_api_key_fails_before_transport(install_transport: Any) -> None:
    fake, key_calls = install_transport([_Reply(b"{}")], key="  ")
    with _scope(), pytest.raises(FredProviderError) as error:
        client_module.fetch_iorb_pages(_query())
    assert error.value.code == "FRED_API_KEY_MISSING"
    assert key_calls == [1]
    assert fake.calls == []


@pytest.mark.parametrize("body", [b'{"count":0,"count":0}', b'{"count":NaN}', b"\xff"])
def test_duplicate_nonfinite_and_non_utf8_wire_json_reject_as_a_whole(
    body: bytes,
    install_transport: Any,
) -> None:
    fake, _ = install_transport([_Reply(body)])
    with _scope(), pytest.raises(FredProviderError) as error:
        client_module.fetch_iorb_pages(_query())
    assert error.value.code == "FRED_BAD_RESPONSE"
    assert len(fake.calls) == 1


def test_noninteger_status_and_http_error_fail_closed(install_transport: Any) -> None:
    query = _query()
    for reply, expected_code in (
        (_Reply(_encode(_page(query, [_row()], count=1)), status=200.0), "FRED_BAD_RESPONSE"),
        (_Reply(b"{}", status=503), "FRED_HTTP_ERROR"),
    ):
        fake, _ = install_transport([reply])
        with _scope(), pytest.raises(FredProviderError) as error:
            client_module.fetch_iorb_pages(query)
        assert error.value.code == expected_code
        assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("query_updates", "page_updates"),
    [
        ({}, {"units": "chg"}),
        ({}, {"output_type": True}),
        ({}, {"file_type": "csv"}),
        ({}, {"order_by": "series_id"}),
        ({}, {"sort_order": "desc"}),
        ({"realtime_start": "2025-01-01"}, {"realtime_start": "2025-01-02"}),
        ({"start_date": "2025-01-01"}, {"observation_start": "2025-01-02"}),
        ({}, {"series_id": "SOFR"}),
    ],
)
def test_page_header_echoes_are_bound_to_query(
    query_updates: dict[str, object],
    page_updates: dict[str, object],
) -> None:
    query = _query(**query_updates)
    page = _page(query, [_row()], count=1, **page_updates)
    with pytest.raises(FredProviderError) as error:
        client_module.validate_iorb_pages((page,), query)
    assert error.value.code in {"FRED_BAD_RESPONSE", "FRED_PAGINATION_ERROR"}


@pytest.mark.parametrize("output_type", [2, 3])
def test_unknown_output_type_wire_rows_fail_closed(output_type: int) -> None:
    query = _query(output_type=output_type)
    row = _row()
    row["release_date"] = "2025-01-03"  # type: ignore[typeddict-item]
    page = _page(query, [row], count=1)
    with pytest.raises(FredProviderError) as error:
        client_module.validate_iorb_pages((page,), query)
    assert error.value.code == "FRED_BAD_RESPONSE"


def test_bad_last_page_or_window_drift_returns_no_partial_chain(install_transport: Any) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2025-01-02")], count=2, offset=0)
    second = _page(
        query,
        [_row("2025-01-03")],
        count=2,
        offset=1,
        observation_end="2025-12-30",
    )
    fake, key_calls = install_transport([_Reply(_encode(first)), _Reply(_encode(second))])
    with _scope(), pytest.raises(FredProviderError) as error:
        client_module.fetch_iorb_pages(query)
    assert error.value.code == "FRED_BAD_RESPONSE"
    assert key_calls == [1]
    assert len(fake.calls) == 2


def test_query_caps_fail_without_returning_partial_pages(install_transport: Any) -> None:
    query = _query(page_size=1, max_records=1, max_pages=1)
    page = _page(query, [_row()], count=2)
    fake, _ = install_transport([_Reply(_encode(page))])
    with _scope(), pytest.raises(FredProviderError) as error:
        client_module.fetch_iorb_pages(query)
    assert error.value.code == "FRED_QUERY_LIMIT"
    assert len(fake.calls) == 1


def test_attempt_budget_stops_before_an_unfunded_second_send(install_transport: Any) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2025-01-02")], count=2, offset=0)
    second = _page(query, [_row("2025-01-03")], count=2, offset=1)
    fake, key_calls = install_transport([_Reply(_encode(first)), _Reply(_encode(second))])
    with _scope(task_attempts=1, source_attempts=1), pytest.raises(RequestAttemptLimitError):
        client_module.fetch_iorb_pages(query)
    assert key_calls == [1]
    assert len(fake.calls) == 1


@pytest.mark.parametrize("limit", ["page", "total", "depth"])
def test_fixed_page_body_total_and_json_depth_bounds(
    limit: str,
    install_transport: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    metadata: dict[str, object] = {}
    if limit == "page":
        monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", 128)
        monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", 10000)
        metadata = {"large": "x" * 300}
    elif limit == "total":
        monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", 10000)
        monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", 128)
    else:
        monkeypatch.setattr(client_module, "_MAX_JSON_DEPTH", 3)
        metadata = {"a": {"b": {"c": {"d": 1}}}}
    page = _page(query, [_row()], count=1, metadata=metadata)
    fake, _ = install_transport([_Reply(_encode(page))])
    with _scope(), pytest.raises(FredProviderError) as error:
        client_module.fetch_iorb_pages(query)
    assert (
        error.value.code == "FRED_RESPONSE_LIMIT"
        if limit in {"page", "total"}
        else "FRED_BAD_RESPONSE"
    )
    assert len(fake.calls) == 1


def test_cancel_after_transport_stops_before_page_return(install_transport: Any) -> None:
    query = _query()
    page = _page(query, [_row()], count=1)
    cancellation = threading.Event()
    fake, key_calls = install_transport(
        [_Reply(_encode(page))],
        after_get=lambda _: cancellation.set(),
    )
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_iorb_pages(query)
    assert key_calls == [1]
    assert len(fake.calls) == 1
