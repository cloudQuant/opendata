"""Offline contract tests for the governed FRED SONIA page client."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest
import requests

from opendata.data.http_client import GovernedHttpClient
from opendata.data.providers.fred.models import _sonia_client as client_module
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


_SOURCE = "fred"
_MODEL = "SONIA"
_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_API_KEY = "synthetic-sonia-test-key"
_EARLIEST = "1776-07-04"
_LATEST = "9999-12-31"
_SELECTORS = (
    ("rate", "IUDSOIA"),
    ("index", "IUDZOS2"),
    ("10th_percentile", "IUDZLS6"),
    ("25th_percentile", "IUDZLS7"),
    ("75th_percentile", "IUDZLS8"),
    ("90th_percentile", "IUDZLS9"),
    ("total_nominal_value", "IUDZLT2"),
)


class _WireReply:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status


class _RecordingSession(requests.Session):
    """Serve synthetic FRED pages while exercising GovernedHttpClient."""

    def __init__(self, replies: Sequence[_WireReply]) -> None:
        super().__init__()
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "timeout": kwargs.get("timeout"),
            }
        )
        if not self.replies:
            raise AssertionError("unexpected additional FRED request")
        reply = self.replies.pop(0)
        response = requests.Response()
        response.status_code = reply.status
        response._content = reply.body
        response.headers["Content-Type"] = "application/json"
        response.url = url
        return response


def _compact_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _reply(value: object, *, status: int = 200) -> _WireReply:
    return _WireReply(_compact_json(value), status=status)


def _raw_reply(body: bytes, *, status: int = 200) -> _WireReply:
    return _WireReply(body, status=status)


def _row(
    observation_date: str = "2026-01-02",
    value: str = "4.25",
    *,
    realtime_start: str = "2026-01-01",
    realtime_end: str = "2026-12-31",
) -> dict[str, str]:
    return {
        "date": observation_date,
        "value": value,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
    }


def _query(**values: object) -> FredSoniaQuery:
    return FredSoniaQuery.model_validate(values)


def _page(
    query: FredSoniaQuery,
    rows: Sequence[dict[str, str]],
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
        "realtime_start": (realtime_start.isoformat() if realtime_start is not None else _EARLIEST),
        "realtime_end": realtime_end.isoformat() if realtime_end is not None else _LATEST,
        "observation_start": (
            query.start_date.isoformat() if query.start_date is not None else _EARLIEST
        ),
        "observation_end": (query.end_date.isoformat() if query.end_date is not None else _LATEST),
        "observations": list(rows),
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
    conditions: Sequence[str] = (),
    expires_in: timedelta = timedelta(minutes=5),
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="synthetic-test-only SONIA grant",
        task_attempts=10,
        source_attempts=3,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + expires_in,
        conditions=conditions,
    )


@contextmanager
def _scope(
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    scope_source: str = _SOURCE,
    scope_model: str | None = _MODEL,
    grant: RequestGrant | None = None,
    grants: Sequence[RequestGrant] | None = None,
    include_grant: bool = True,
    cancellation: threading.Event | None = None,
) -> Iterator[None]:
    selected_grants = (
        tuple(grants)
        if grants is not None
        else ((grant or _grant(operation=operation),) if include_grant else ())
    )
    budget = RequestBudget(task_attempts=10, source_attempts=3, grants=selected_grants)
    with request_execution_scope(
        source=scope_source,
        canonical_model=scope_model,
        operation=operation,
        budget=budget,
        cancellation=cancellation,
    ):
        yield


@pytest.fixture
def install_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[_RecordingSession, list[int]]]:
    def install(
        replies: Sequence[_WireReply],
        *,
        key: str = _API_KEY,
        after_request: Callable[[int], None] | None = None,
    ) -> tuple[_RecordingSession, list[int]]:
        import opendata.data.http_client as http_client_module

        session = _RecordingSession(replies)
        client = GovernedHttpClient(session=session, raw_response_cache=None)
        monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
        monkeypatch.setattr(client_module, "get_shared_http_client", lambda: client)
        key_calls: list[int] = []

        def require_key() -> str:
            key_calls.append(1)
            return key

        monkeypatch.setattr(client_module._client, "require_api_key", require_key)
        if after_request is not None:
            original_request = session.request

            def tracked_request(method: str, url: str, **kwargs: Any) -> requests.Response:
                response = original_request(method, url, **kwargs)
                after_request(len(session.calls))
                return response

            monkeypatch.setattr(session, "request", tracked_request)
        return session, key_calls

    return install


@pytest.mark.parametrize(("parameter", "series_id"), _SELECTORS)
def test_every_sonia_selector_uses_its_exact_fred_series(
    parameter: str,
    series_id: str,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(parameter=parameter)
    row = _row(value="+1.2500e+1")
    page = _page(query, [row], count=1, series_id=series_id, provider_marker={"retained": True})
    session, key_calls = install_transport([_reply(page)])

    with _scope():
        pages = client_module.fetch_sonia_pages(query, timeout=4.5)

    assert pages == (page,)
    assert client_module.validate_sonia_pages(pages, query) == (row,)
    assert pages[0]["provider_marker"] == {"retained": True}
    assert key_calls == [1]
    assert session.calls == [
        {
            "method": "GET",
            "url": _URL,
            "params": {
                "series_id": series_id,
                "api_key": _API_KEY,
                "file_type": "json",
                "units": "lin",
                "output_type": "1",
                "order_by": "observation_date",
                "sort_order": "asc",
                "aggregation_method": "avg",
                "limit": "100000",
                "offset": "0",
            },
            "timeout": 4.5,
        }
    ]


def test_complete_pages_keep_raw_envelopes_and_fixed_four_date_window(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(
        parameter="index",
        page_size=1,
        max_records=2,
        max_pages=2,
        offset=4,
        start_date="2026-01-01",
        end_date="2026-01-31",
        as_of="2026-02-01",
        sort_order="desc",
        transform_units="pca",
        frequency="m",
        aggregation_method="eop",
        output_type=3,
    )
    rows = [_row("2026-01-30", "1.0100"), _row("2026-01-29", "1.0050")]
    first = _page(query, rows[:1], count=6, offset=4, series_id="IUDZOS2", raw_meta="first")
    second = _page(query, rows[1:], count=6, offset=5, series_id="IUDZOS2", raw_meta="second")
    session, _ = install_transport([_reply(first), _reply(second)])

    with _scope():
        pages = client_module.fetch_sonia_pages(query)

    assert pages == (first, second)
    assert client_module.validate_sonia_pages(pages, query) == tuple(rows)
    assert [page["raw_meta"] for page in pages] == ["first", "second"]
    assert [call["params"]["offset"] for call in session.calls] == ["4", "5"]
    assert all(call["params"]["limit"] == "1" for call in session.calls)
    assert all(call["params"]["series_id"] == "IUDZOS2" for call in session.calls)
    assert all(call["params"]["output_type"] == "3" for call in session.calls)
    assert all(call["params"]["frequency"] == "m" for call in session.calls)
    assert all(call["params"]["aggregation_method"] == "eop" for call in session.calls)
    assert all(call["params"]["observation_start"] == "2026-01-01" for call in session.calls)
    assert all(call["params"]["observation_end"] == "2026-01-31" for call in session.calls)
    assert all(call["params"]["realtime_start"] == "2026-02-01" for call in session.calls)
    assert all(call["params"]["realtime_end"] == "2026-02-01" for call in session.calls)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("count", 3),
        ("realtime_start", "2026-02-01"),
        ("realtime_end", "2026-02-02"),
        ("observation_start", "2026-01-02"),
        ("observation_end", "2026-01-30"),
    ],
)
def test_paged_chain_requires_one_count_and_one_four_date_window(
    field: str,
    changed: object,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    second = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    second[field] = changed
    install_transport([_reply(first), _reply(second)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sonia_pages(query)


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("transform_units", ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")),
        (
            "frequency",
            (
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
            ),
        ),
        ("aggregation_method", ("avg", "sum", "eop")),
        ("output_type", (1, 2, 3, 4)),
    ],
)
def test_sonia_query_controls_are_sent_without_rewriting(
    field: str,
    values: tuple[object, ...],
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    for value in values:
        query = _query(**{field: value})
        session, _ = install_transport([_reply(_page(query, [], count=0))])
        with _scope():
            client_module.fetch_sonia_pages(query)
        wire_name = "units" if field == "transform_units" else field
        assert session.calls[0]["params"][wire_name] == str(value)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (
            {"as_of": "2026-04-05"},
            {"realtime_start": "2026-04-05", "realtime_end": "2026-04-05"},
        ),
        (
            {"vintage_dates": ["2026-04-05", "2026-04-06"]},
            {"vintage_dates": "2026-04-05,2026-04-06"},
        ),
    ],
)
def test_as_of_and_vintage_selectors_are_forwarded_exactly(
    selector: dict[str, object],
    expected: dict[str, str],
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(**selector)
    page = _page(query, [], count=0)
    session, _ = install_transport([_reply(page)])
    with _scope():
        assert client_module.fetch_sonia_pages(query) == (page,)
    for name, value in expected.items():
        assert session.calls[0]["params"][name] == value


def test_pure_validation_preserves_lexical_rows_without_scope_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    rows = [_row(value="+1.2500e+1"), _row("2026-01-03", ".")]
    page = _page(query, rows, count=2, provider_metadata=[1, None])

    def forbidden() -> None:
        raise AssertionError("pure SONIA page validation attempted HTTP access")

    monkeypatch.setattr(client_module, "get_shared_http_client", forbidden)
    assert client_module.validate_sonia_pages((page,), query) == tuple(rows)
    assert client_module.validate_sonia_pages((page,), query)[0]["value"] == "+1.2500e+1"


@pytest.mark.parametrize(
    "bad_query",
    [
        FredSeriesQuery(series_id="IUDSOIA"),
        object(),
        FredSoniaQuery.model_construct(parameter="index", series_id="IUDSOIA"),
        FredSoniaQuery.model_copy(FredSoniaQuery(), update={"series_id": "IUDZOS2"}),
    ],
    ids=("generic-query", "object", "constructed-selector-mismatch", "copied-selector-mismatch"),
)
def test_wrong_or_mutated_query_rejects_before_key_or_network(
    bad_query: object,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    session, key_calls = install_transport([])
    with _scope(), pytest.raises(RequestAuthorizationError):
        client_module.fetch_sonia_pages(bad_query)  # type: ignore[arg-type]
    assert key_calls == []
    assert session.calls == []


@pytest.mark.parametrize(
    ("scope_source", "scope_model", "grant"),
    [
        ("other", _MODEL, _grant(source="other")),
        (_SOURCE, "FredSonia", _grant(model="FredSonia")),
        (_SOURCE, _MODEL, _grant(operation=RequestOperation.STORE)),
        (_SOURCE, _MODEL, _grant(host="example.com")),
        (_SOURCE, _MODEL, _grant(decision=GrantDecision.DENIED)),
        (_SOURCE, _MODEL, _grant(decision=GrantDecision.UNKNOWN)),
        (_SOURCE, _MODEL, _grant(conditions=("pending-review",))),
        (_SOURCE, _MODEL, _grant(expires_in=timedelta(seconds=-1))),
    ],
    ids=(
        "wrong-source",
        "wrong-model",
        "wrong-operation-grant",
        "wrong-host",
        "denied",
        "unknown",
        "conditional",
        "expired",
    ),
)
def test_wrong_or_unusable_grant_rejects_before_key_or_network(
    scope_source: str,
    scope_model: str,
    grant: RequestGrant,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    session, key_calls = install_transport([])
    with (
        _scope(scope_source=scope_source, scope_model=scope_model, grant=grant),
        pytest.raises(RequestAuthorizationError),
    ):
        client_module.fetch_sonia_pages(_query())
    assert key_calls == []
    assert session.calls == []


def test_every_nested_scope_must_keep_exact_sonia_identity(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    session, key_calls = install_transport([])
    grant = _grant()
    budget = RequestBudget(
        task_attempts=10,
        source_attempts=3,
        grants=(grant, _grant(model="OTHER")),
    )

    with (
        request_execution_scope(
            source=_SOURCE,
            canonical_model=_MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        request_execution_scope(
            source=_SOURCE,
            canonical_model="OTHER",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError),
    ):
        client_module.fetch_sonia_pages(_query())
    assert key_calls == []
    assert session.calls == []


def test_matching_nonquery_operation_still_requires_its_own_grant(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query()
    operation = RequestOperation.STORE
    page = _page(query, [], count=0)
    session, _ = install_transport([_reply(page)])
    with _scope(operation=operation, grant=_grant(operation=operation)):
        assert client_module.fetch_sonia_pages(query) == (page,)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("units", "chg"),
        ("output_type", True),
        ("file_type", "xml"),
        ("order_by", "series_id"),
        ("sort_order", "desc"),
        ("count", True),
        ("offset", False),
        ("limit", True),
        ("realtime_start", "2026-02-30"),
        ("realtime_end", "0001-01-01"),
        ("observation_start", "2026-02-30"),
        ("observation_end", "0001-01-01"),
        ("series_id", "IUDSOIA"),
    ],
)
def test_mismatched_or_malformed_page_headers_fail_closed(
    field: str,
    value: object,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(parameter="index")
    page = _page(query, [], count=0)
    page[field] = value
    install_transport([_reply(page)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sonia_pages(query)


@pytest.mark.parametrize(
    "row",
    [
        {"date": "2026-01-02", "value": "1", "realtime_start": "2026-01-01"},
        {
            "date": "2026-01-02",
            "value": 1,
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
        },
        {
            "date": "2026-1-2",
            "value": "1",
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
        },
        {
            "date": "2026-01-02",
            "value": "1",
            "realtime_start": "2026-02-01",
            "realtime_end": "2026-01-02",
        },
        {
            "date": "2026-01-02",
            "value": "1",
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
            "revision": "unrecognized",
        },
    ],
    ids=(
        "missing-field",
        "nonstring-value",
        "noncanonical-date",
        "reversed-realtime",
        "unknown-field",
    ),
)
def test_unknown_or_malformed_observation_rows_fail_all_pages(
    row: dict[str, object],
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(output_type=2)
    page = _page(query, [row], count=1)  # type: ignore[list-item]
    install_transport([_reply(page)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sonia_pages(query)


@pytest.mark.parametrize("output_type", [2, 3])
def test_output_types_two_and_three_keep_known_wire_shape_and_fail_unknown_shape(
    output_type: int,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(output_type=output_type)
    page = _page(query, [_row(value="1.00500")], count=1)
    session, _ = install_transport([_reply(page)])
    with _scope():
        assert client_module.fetch_sonia_pages(query) == (page,)
    assert session.calls[0]["params"]["output_type"] == str(output_type)

    malformed = _page(query, [_row(value="1.00500")], count=1)
    malformed["observations"][0]["realtime_end"] = None
    install_transport([_reply(malformed)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sonia_pages(query)


def test_duplicates_are_preserved_but_conflicting_rows_and_bad_order_reject(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    query = _query(page_size=2, max_records=2, max_pages=1)
    first = _row(value="4.25")
    exact = _row(value="4.25")
    page = _page(query, [first, exact], count=2)
    install_transport([_reply(page)])
    with _scope():
        pages = client_module.fetch_sonia_pages(query)
    assert client_module.validate_sonia_pages(pages, query) == (first, exact)

    conflict_page = _page(query, [first, _row(value="4.250")], count=2)
    install_transport([_reply(conflict_page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(query)
    assert caught.value.code == "FRED_PAGINATION_CONFLICT"

    descending_query = _query(page_size=2, max_records=2, max_pages=1, sort_order="desc")
    unsorted = _page(
        descending_query,
        [_row("2026-01-02"), _row("2026-01-03")],
        count=2,
    )
    install_transport([_reply(unsorted)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sonia_pages(descending_query)


@pytest.mark.parametrize(
    "body",
    [
        b'{"count":0,"count":0}',
        b"\xff",
        b'{"count":NaN}',
    ],
    ids=("duplicate-key", "invalid-utf8", "nonfinite-json-constant"),
)
def test_unsafe_json_documents_reject_before_exposing_partial_pages(
    body: bytes,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    install_transport([_raw_reply(body)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(_query())
    assert caught.value.code == "FRED_BAD_RESPONSE"


def test_page_depth_body_and_total_response_limits_are_enforced(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    page = _page(query, [], count=0, metadata={"a": {"b": {"c": {}}}})
    monkeypatch.setattr(client_module, "_MAX_JSON_DEPTH", 3)
    install_transport([_reply(page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(query)
    assert caught.value.code == "FRED_BAD_RESPONSE"

    monkeypatch.setattr(client_module, "_MAX_JSON_DEPTH", 64)
    page = _page(query, [], count=0, metadata="x" * 200)
    body = _compact_json(page)
    monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", len(body) - 1)
    decoded: list[int] = []
    original_loads = client_module.json.loads

    def track_loads(*args: object, **kwargs: object) -> object:
        decoded.append(1)
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(client_module.json, "loads", track_loads)
    install_transport([_raw_reply(body)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(query)
    assert caught.value.code == "FRED_RESPONSE_LIMIT"
    assert decoded == []

    monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", 100_000)
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    second = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    bodies = [_compact_json(first), _compact_json(second)]
    monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", sum(map(len, bodies)) - 1)
    install_transport([_raw_reply(bodies[0]), _raw_reply(bodies[1])])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(query)
    assert caught.value.code == "FRED_RESPONSE_LIMIT"


@pytest.mark.parametrize(
    ("query", "count"),
    [
        (_query(page_size=1, max_records=1, max_pages=2), 2),
        (_query(page_size=1, max_records=2, max_pages=1), 2),
    ],
    ids=("record-cap", "page-cap"),
)
def test_query_caps_fail_instead_of_returning_partial_pages(
    query: FredSoniaQuery,
    count: int,
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    page = _page(query, [_row()], count=count, limit=1)
    session, _ = install_transport([_reply(page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sonia_pages(query)
    assert caught.value.code == "FRED_QUERY_LIMIT"
    assert len(session.calls) == 1


def test_cancellation_is_checked_during_paging_and_after_final_validation(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    second = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    cancellation = threading.Event()
    session, key_calls = install_transport(
        [_reply(first), _reply(second)],
        after_request=lambda index: cancellation.set() if index == 1 else None,
    )
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_sonia_pages(query)
    assert len(session.calls) == 1
    assert key_calls == [1]

    cancellation.clear()
    page = _page(_query(), [], count=0)
    install_transport([_reply(page)])
    original_validate = client_module.validate_sonia_pages

    def cancel_after_validation(
        pages: tuple[dict[str, Any], ...], validated_query: FredSoniaQuery
    ) -> Any:
        result = original_validate(pages, validated_query)
        cancellation.set()
        return result

    monkeypatch.setattr(client_module, "validate_sonia_pages", cancel_after_validation)
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_sonia_pages(_query())


def test_pure_validator_requires_a_complete_chain_and_rejects_bad_page_input() -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    last = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    with pytest.raises(FredProviderError) as caught:
        client_module.validate_sonia_pages((first,), query)
    assert caught.value.code == "FRED_PAGINATION_ERROR"
    assert client_module.validate_sonia_pages((first, last), query) == (
        _row("2026-01-02"),
        _row("2026-01-03"),
    )
    with pytest.raises(FredProviderError):
        client_module.validate_sonia_pages(([1],), query)  # type: ignore[arg-type]


def test_fetch_rejects_missing_scope_before_key_lookup_or_http(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    session, key_calls = install_transport([])
    with pytest.raises(RequestAuthorizationError):
        client_module.fetch_sonia_pages(_query())
    assert key_calls == []
    assert session.calls == []


def test_pre_cancelled_scope_rejects_before_key_lookup_or_http(
    install_transport: Callable[..., tuple[_RecordingSession, list[int]]],
) -> None:
    session, key_calls = install_transport([])
    cancellation = threading.Event()
    cancellation.set()
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_sonia_pages(_query())
    assert key_calls == []
    assert session.calls == []
