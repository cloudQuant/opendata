"""Hermetic tests for the governed FRED SOFR page client."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.providers.fred.models import _sofr_client as client_module
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery
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
    from collections.abc import Callable, Iterator, Mapping, Sequence


_SOURCE = "fred"
_MODEL = "SOFR"
_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_API_KEY = "synthetic-test-key"
_EARLIEST = "1776-07-04"
_LATEST = "9999-12-31"


class _Response:
    def __init__(self, body: bytes, *, status: object = 200) -> None:
        self.content = body
        self.status_code = status


class _RecordingGovHTTP:
    def __init__(
        self,
        responses: Sequence[_Response | BaseException],
        *,
        after_get: Callable[[int], None] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.after_get = after_get
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout: float | None,
        source: str,
    ) -> _Response:
        self.calls.append(
            {"url": url, "params": dict(params), "timeout": timeout, "source": source}
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        response = self.responses.pop(0)
        if self.after_get is not None:
            self.after_get(len(self.calls))
        if isinstance(response, BaseException):
            raise response
        return response


def _compact_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _response(value: object, *, status: object = 200) -> _Response:
    return _Response(_compact_json(value), status=status)


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


def _page(
    query: FredSofrQuery,
    rows: Sequence[dict[str, str]],
    *,
    count: int,
    offset: int = 0,
    limit: int | None = None,
    **overrides: object,
) -> dict[str, Any]:
    realtime_start = query.as_of or query.realtime_start
    realtime_end = query.as_of or query.realtime_end
    observation_start = query.start_date
    observation_end = query.end_date
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
            observation_start.isoformat() if observation_start is not None else _EARLIEST
        ),
        "observation_end": (
            observation_end.isoformat() if observation_end is not None else _LATEST
        ),
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
        rights_evidence="synthetic-test-grant",
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
) -> Callable[..., tuple[_RecordingGovHTTP, list[int]]]:
    def install(
        responses: Sequence[_Response | BaseException],
        *,
        after_get: Callable[[int], None] | None = None,
        key: str = _API_KEY,
    ) -> tuple[_RecordingGovHTTP, list[int]]:
        fake = _RecordingGovHTTP(responses, after_get=after_get)
        key_calls: list[int] = []

        def require_key() -> str:
            key_calls.append(1)
            return key

        monkeypatch.setattr(client_module._client, "require_api_key", require_key)
        monkeypatch.setattr(client_module, "get_shared_http_client", lambda: fake)
        return fake, key_calls

    return install


def _query(**values: object) -> FredSofrQuery:
    return FredSofrQuery.model_validate({"series_id": "SOFR", **values})


def test_fetch_uses_fixed_governed_http_and_returns_complete_envelope(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=3, max_records=3, start_date="2026-01-01", end_date="2026-01-31")
    envelope = _page(query, [_row()], count=1, metadata={"kept": [1, None]})
    fake, key_calls = install_transport([_response(envelope)])

    with _scope():
        pages = client_module.fetch_sofr_pages(query, timeout=7.5)

    assert pages == (envelope,)
    assert pages[0]["metadata"] == {"kept": [1, None]}
    assert key_calls == [1]
    assert fake.calls == [
        {
            "url": _URL,
            "params": {
                "series_id": "SOFR",
                "api_key": _API_KEY,
                "file_type": "json",
                "units": "lin",
                "output_type": "1",
                "order_by": "observation_date",
                "sort_order": "asc",
                "aggregation_method": "avg",
                "limit": "3",
                "offset": "0",
                "observation_start": "2026-01-01",
                "observation_end": "2026-01-31",
            },
            "timeout": 7.5,
            "source": _SOURCE,
        }
    ]


@pytest.mark.parametrize("units", ["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"])
def test_all_transform_units_are_mapped_exactly(
    units: str,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(transform_units=units)
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope():
        client_module.fetch_sofr_pages(query)

    assert fake.calls[0]["params"]["units"] == units


@pytest.mark.parametrize(
    "frequency",
    [
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
    ],
)
def test_all_sixteen_frequencies_are_mapped_exactly(
    frequency: str,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(frequency=frequency)
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope():
        client_module.fetch_sofr_pages(query)

    assert fake.calls[0]["params"]["frequency"] == frequency


@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_all_output_types_are_mapped_as_wire_strings(
    output_type: int,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(output_type=output_type)
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope():
        client_module.fetch_sofr_pages(query)

    assert fake.calls[0]["params"]["output_type"] == str(output_type)


def test_aggregation_sort_and_selector_parameters_are_exact(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(
        series_id="SOFR90DAYAVG",
        transform_units="pca",
        output_type=4,
        frequency="weth",
        aggregation_method="eop",
        sort_order="desc",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 28),
        realtime_start="2026-03-01",
        realtime_end="2026-03-05",
    )
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope():
        client_module.fetch_sofr_pages(query)

    assert fake.calls[0]["params"] == {
        "series_id": "SOFR90DAYAVG",
        "api_key": _API_KEY,
        "file_type": "json",
        "units": "pca",
        "output_type": "4",
        "order_by": "observation_date",
        "sort_order": "desc",
        "aggregation_method": "eop",
        "limit": "100000",
        "offset": "0",
        "frequency": "weth",
        "observation_start": "2026-02-01",
        "observation_end": "2026-02-28",
        "realtime_start": "2026-03-01",
        "realtime_end": "2026-03-05",
    }


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
def test_as_of_and_vintage_selectors_map_without_inference(
    selector: dict[str, object],
    expected: dict[str, str],
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(**selector)
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope():
        client_module.fetch_sofr_pages(query)

    params = fake.calls[0]["params"]
    assert {name: params[name] for name in expected} == expected


def test_page_limit_is_bounded_by_max_records_and_offset_is_paged(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    bounded_query = _query(page_size=3, max_records=2, max_pages=1)
    bounded = _page(
        bounded_query,
        [_row("2026-01-02"), _row("2026-01-03")],
        count=2,
        limit=2,
    )
    bounded_fake, _ = install_transport([_response(bounded)])

    with _scope():
        assert client_module.fetch_sofr_pages(bounded_query) == (bounded,)

    assert bounded_fake.calls[0]["params"]["limit"] == "2"

    query = _query(page_size=3, offset=2, max_records=4, max_pages=2)
    first = _page(
        query,
        [_row("2026-01-02"), _row("2026-01-03"), _row("2026-01-04")],
        count=6,
        offset=2,
        limit=3,
    )
    second = _page(query, [_row("2026-01-05")], count=6, offset=5, limit=3)
    fake, _ = install_transport([_response(first), _response(second)])

    with _scope():
        pages = client_module.fetch_sofr_pages(query)

    assert [call["params"]["limit"] for call in fake.calls] == ["3", "3"]
    assert [call["params"]["offset"] for call in fake.calls] == ["2", "5"]
    assert pages == (first, second)


def test_offset_beyond_count_returns_one_empty_page(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=2, offset=9, max_records=2)
    page = _page(query, [], count=4, offset=9, limit=2)
    fake, _ = install_transport([_response(page)])

    with _scope():
        result = client_module.fetch_sofr_pages(query)

    assert result == (page,)
    assert len(fake.calls) == 1
    assert client_module.validate_sofr_pages(result, query) == ()


def test_valid_complete_pages_flatten_without_losing_lexical_values(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=2, max_records=2, max_pages=1)
    rows = [_row("2026-01-02", "+1.2500e+1"), _row("2026-01-03", ".")]
    page = _page(query, rows, count=2)
    fake, _ = install_transport([_response(page)])

    with _scope():
        pages = client_module.fetch_sofr_pages(query)

    assert client_module.validate_sofr_pages(pages, query) == tuple(rows)
    assert [row["value"] for row in client_module.validate_sofr_pages(pages, query)] == [
        "+1.2500e+1",
        ".",
    ]
    assert fake.calls[0]["params"]["limit"] == "2"


def test_pure_validation_does_not_need_scope_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    page = _page(query, [_row()], count=1)

    def forbidden() -> None:
        raise AssertionError("pure validation tried to get an HTTP client")

    monkeypatch.setattr(client_module, "get_shared_http_client", forbidden)
    assert client_module.validate_sofr_pages((page,), query) == (_row(),)


def test_pure_validation_requires_the_full_chain_and_respects_query_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, offset=0, limit=1)
    last = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    with pytest.raises(FredProviderError) as caught:
        client_module.validate_sofr_pages((first,), query)
    assert caught.value.code == "FRED_PAGINATION_ERROR"
    assert client_module.validate_sofr_pages((first, last), query) == (
        _row("2026-01-02"),
        _row("2026-01-03"),
    )

    capped_query = _query(page_size=1, max_records=1, max_pages=2)
    capped_page = _page(capped_query, [_row()], count=2, limit=1)
    with pytest.raises(FredProviderError) as caught:
        client_module.validate_sofr_pages((capped_page,), capped_query)
    assert caught.value.code == "FRED_QUERY_LIMIT"

    metadata_page = _page(query, [_row()], count=1, metadata={"large": "x" * 128})
    monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", 128)
    with pytest.raises(FredProviderError) as caught:
        client_module.validate_sofr_pages((metadata_page,), query)
    assert caught.value.code == "FRED_RESPONSE_LIMIT"


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
        ("realtime_end", _EARLIEST.replace("07-04", "01-01")),
        ("observation_start", "2026-02-30"),
        ("observation_end", _EARLIEST.replace("07-04", "01-01")),
        ("series_id", "SOFRX"),
    ],
)
def test_invalid_echo_headers_reject_the_entire_page(
    field: str,
    value: object,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    page = _page(query, [], count=0)
    page[field] = value
    install_transport([_response(page)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


@pytest.mark.parametrize(
    ("query_values", "field", "value"),
    [
        ({"start_date": "2026-01-01"}, "observation_start", "2025-12-31"),
        ({"end_date": "2026-01-31"}, "observation_end", "2026-02-01"),
        ({"as_of": "2026-02-01"}, "realtime_end", "2026-02-02"),
        ({"realtime_start": "2026-02-01"}, "realtime_start", "2026-01-31"),
        ({"realtime_end": "2026-02-01"}, "realtime_end", "2026-02-02"),
    ],
)
def test_explicit_query_dates_must_match_echo_headers(
    query_values: dict[str, object],
    field: str,
    value: str,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(**query_values)
    page = _page(query, [], count=0, **{field: value})
    install_transport([_response(page)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_vintage_mode_does_not_infer_unverified_realtime_echo_dates(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(vintage_dates=["2026-02-01"])
    page = _page(
        query,
        [],
        count=0,
        realtime_start="2026-02-01",
        realtime_end="2026-02-02",
    )
    install_transport([_response(page)])

    with _scope():
        assert client_module.fetch_sofr_pages(query) == (page,)


@pytest.mark.parametrize(
    "rows",
    [
        [{"date": "2026-01-02", "value": "1", "realtime_start": "2026-01-01"}],
        [
            {
                "date": "2026-01-02",
                "value": 1,
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
            }
        ],
        [
            {
                "date": "2026-1-2",
                "value": "1",
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
            }
        ],
        [
            {
                "date": "2026-01-02",
                "value": "1",
                "realtime_start": "2026-02-01",
                "realtime_end": "2026-01-02",
            }
        ],
        [
            {
                "date": "2026-01-02",
                "value": "1",
                "realtime_start": "2026-01-01",
                "realtime_end": "2026-01-02",
                "extra": "x",
            }
        ],
    ],
)
def test_unknown_or_malformed_observation_shape_fails_whole_result(
    rows: list[dict[str, object]],
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    page = _page(query, rows, count=len(rows))
    install_transport([_response(page)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_conflicting_same_identity_rows_fail_but_exact_duplicates_are_preserved(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=2, max_records=2, max_pages=1)
    first = _row(value="4.25")
    duplicate = _row(value="4.25")
    conflict = _row(value="4.250")
    ok_page = _page(query, [first, duplicate], count=2)
    fake, _ = install_transport([_response(ok_page)])

    with _scope():
        pages = client_module.fetch_sofr_pages(query)
    assert client_module.validate_sofr_pages(pages, query) == (first, duplicate)

    bad_page = _page(query, [first, conflict], count=2)
    install_transport([_response(bad_page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sofr_pages(query)
    assert caught.value.code == "FRED_PAGINATION_CONFLICT"
    assert len(fake.calls) == 1


def test_same_observation_date_can_keep_distinct_realtime_revisions(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=2, max_records=2, max_pages=1)
    rows = [
        _row("2026-01-02", "4.25", realtime_start="2026-01-03", realtime_end="2026-01-04"),
        _row("2026-01-02", "4.5", realtime_start="2026-02-03", realtime_end="2026-02-04"),
    ]
    install_transport([_response(_page(query, rows, count=2))])

    with _scope():
        pages = client_module.fetch_sofr_pages(query)

    assert client_module.validate_sofr_pages(pages, query) == tuple(rows)


@pytest.mark.parametrize(
    ("sort_order", "rows"),
    [
        ("asc", [_row("2026-01-03"), _row("2026-01-02")]),
        ("desc", [_row("2026-01-02"), _row("2026-01-03")]),
    ],
)
def test_observation_date_order_is_checked_across_complete_result(
    sort_order: str,
    rows: list[dict[str, str]],
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(sort_order=sort_order, page_size=2, max_records=2, max_pages=1)
    install_transport([_response(_page(query, rows, count=2))])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_bad_tail_page_count_or_length_fails_without_partial_return(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    wrong_count = _page(query, [_row("2026-01-03")], count=3, offset=1, limit=1)
    install_transport([_response(first), _response(wrong_count)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)

    too_short = _page(query, [], count=2, offset=1, limit=1)
    install_transport([_response(first), _response(too_short)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


@pytest.mark.parametrize(
    ("field", "drifted_value"),
    [
        ("realtime_start", "2025-09-01"),
        ("realtime_end", "9998-12-31"),
        ("observation_start", "2025-09-01"),
        ("observation_end", "9998-12-31"),
    ],
)
def test_fetch_and_pure_validation_reject_cross_page_window_drift_early(
    field: str,
    drifted_value: str,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=1, max_records=3, max_pages=3)
    first = _page(query, [_row("2026-01-02")], count=3, offset=0, limit=1)
    drifted = _page(query, [_row("2026-01-03")], count=3, offset=1, limit=1)
    drifted[field] = drifted_value
    third = _page(query, [_row("2026-01-04")], count=3, offset=2, limit=1)
    fake, _ = install_transport([_response(first), _response(drifted), _response(third)])

    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)

    assert len(fake.calls) == 2
    with pytest.raises(FredProviderError):
        client_module.validate_sofr_pages((first, drifted, third), query)


def test_cross_page_window_can_keep_distinct_harmless_metadata(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(
        query,
        [_row("2026-01-02")],
        count=2,
        offset=0,
        limit=1,
        metadata={"page_note": "first"},
    )
    second = _page(
        query,
        [_row("2026-01-03")],
        count=2,
        offset=1,
        limit=1,
        metadata={"page_note": "second"},
    )
    install_transport([_response(first), _response(second)])

    with _scope():
        pages = client_module.fetch_sofr_pages(query)

    assert pages == (first, second)
    assert pages[0]["metadata"] != pages[1]["metadata"]
    assert client_module.validate_sofr_pages(pages, query) == (
        _row("2026-01-02"),
        _row("2026-01-03"),
    )


def test_wrong_optional_series_id_and_unknown_top_metadata_are_handled_strictly(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    valid = _page(query, [], count=0, source_meta={"source": ["fred", 1]})
    valid["series_id"] = "SOFR"
    install_transport([_response(valid)])
    with _scope():
        assert client_module.fetch_sofr_pages(query) == (valid,)

    invalid = _page(query, [], count=0, series_id=True)
    install_transport([_response(invalid)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_duplicate_json_keys_non_utf8_nonfinite_and_excessive_depth_reject(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    valid = _page(query, [], count=0)
    bodies = [
        b'{"count":0,"count":0}',
        b"\xff",
        _compact_json(valid).replace(b'"count":0', b'"count":NaN', 1),
    ]
    for body in bodies:
        install_transport([_Response(body)])
        with _scope(), pytest.raises(FredProviderError):
            client_module.fetch_sofr_pages(query)

    monkeypatch.setattr(client_module, "_MAX_JSON_DEPTH", 3)
    too_deep = dict(valid)
    too_deep["metadata"] = {"a": {"b": {"c": {}}}}
    install_transport([_response(too_deep)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_body_limit_is_checked_before_json_decode_and_total_is_bounded(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query()
    page = _page(query, [], count=0)
    body = _compact_json(page)
    monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", len(body))
    monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", len(body))
    fake, _ = install_transport([_Response(body)])
    with _scope():
        assert client_module.fetch_sofr_pages(query) == (page,)
    assert len(fake.calls) == 1

    decoded: list[int] = []
    original_loads = client_module.json.loads

    def track_loads(*args: object, **kwargs: object) -> object:
        decoded.append(1)
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(client_module.json, "loads", track_loads)
    install_transport([_Response(body + b" ")])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)
    assert decoded == []

    monkeypatch.setattr(client_module, "_MAX_PAGE_BODY_BYTES", 100_000)
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    second = _page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)
    bodies = [_compact_json(first), _compact_json(second)]
    monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", sum(map(len, bodies)))
    install_transport([_Response(bodies[0]), _Response(bodies[1])])
    with _scope():
        assert len(client_module.fetch_sofr_pages(query)) == 2

    monkeypatch.setattr(client_module, "_MAX_TOTAL_BODY_BYTES", sum(map(len, bodies)) - 1)
    install_transport([_Response(bodies[0]), _Response(bodies[1])])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)


def test_query_max_records_and_pages_fail_before_returning_partial_data(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query(page_size=1, max_records=1, max_pages=2)
    page = _page(query, [_row()], count=2, limit=1)
    fake, _ = install_transport([_response(page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sofr_pages(query)
    assert caught.value.code == "FRED_QUERY_LIMIT"
    assert len(fake.calls) == 1

    query = _query(page_size=1, max_records=2, max_pages=1)
    page = _page(query, [_row()], count=2, limit=1)
    fake, _ = install_transport([_response(page)])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sofr_pages(query)
    assert caught.value.code == "FRED_QUERY_LIMIT"
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "wrong-source",
        "wrong-model",
        "wrong-operation",
        "wrong-host",
        "conditional",
        "expired",
        "denied",
        "unknown",
    ],
)
def test_unauthorized_scope_fails_before_key_lookup_or_http(
    case: str,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, key_calls = install_transport([_response(_page(query, [], count=0))])
    scope_options: dict[str, object] = {}
    if case == "missing":
        scope_options["include_grant"] = False
    elif case == "wrong-source":
        scope_options["scope_source"] = "other"
    elif case == "wrong-model":
        scope_options["scope_model"] = "FredSofr"
    elif case == "wrong-operation":
        scope_options["grant"] = _grant(operation=RequestOperation.STORE)
    elif case == "wrong-host":
        scope_options["grant"] = _grant(host="example.com")
    elif case == "conditional":
        scope_options["grant"] = _grant(conditions=("pending-review",))
    elif case == "expired":
        scope_options["grant"] = _grant(expires_in=timedelta(seconds=-1))
    elif case == "denied":
        scope_options["grant"] = _grant(decision=GrantDecision.DENIED)
    elif case == "unknown":
        scope_options["grant"] = _grant(decision=GrantDecision.UNKNOWN)
    else:  # pragma: no cover - parametrization is fixed above
        raise AssertionError(f"unknown authorization case: {case}")

    with _scope(**scope_options), pytest.raises(RequestAuthorizationError):
        client_module.fetch_sofr_pages(query)

    assert key_calls == []
    assert fake.calls == []


def test_every_active_scope_must_match_exact_source_and_model(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, key_calls = install_transport([])
    budget = RequestBudget(
        task_attempts=10,
        source_attempts=3,
        grants=[
            _grant(),
            _grant(model="OTHER"),
        ],
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
        client_module.fetch_sofr_pages(query)

    assert key_calls == []
    assert fake.calls == []


@pytest.mark.parametrize(
    "operation",
    [RequestOperation.QUERY, RequestOperation.STORE, RequestOperation.EXPORT],
)
def test_each_real_operation_must_match_its_own_grant(
    operation: RequestOperation,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, _ = install_transport([_response(_page(query, [], count=0))])

    with _scope(operation=operation):
        client_module.fetch_sofr_pages(query)

    assert len(fake.calls) == 1


def test_missing_scope_rejects_before_key_lookup_or_http(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, key_calls = install_transport([_response(_page(query, [], count=0))])

    with pytest.raises(RequestAuthorizationError):
        client_module.fetch_sofr_pages(query)

    assert key_calls == []
    assert fake.calls == []


@pytest.mark.parametrize("bad_query", [FredSeriesQuery(series_id="SOFR"), object()])
def test_nonexact_query_class_rejects_before_key_lookup_or_http(
    bad_query: object,
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    fake, key_calls = install_transport([])

    with _scope(), pytest.raises(RequestAuthorizationError):
        client_module.fetch_sofr_pages(bad_query)  # type: ignore[arg-type]

    assert key_calls == []
    assert fake.calls == []


def test_constructed_or_copied_invalid_query_is_revalidated_before_io(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    constructed = FredSofrQuery.model_construct(series_id="SOFR", output_type=True)
    copied = _query().model_copy(update={"output_type": True})
    fake, key_calls = install_transport([])

    for invalid_query in (constructed, copied):
        with _scope(), pytest.raises(RequestAuthorizationError):
            client_module.fetch_sofr_pages(invalid_query)

    assert key_calls == []
    assert fake.calls == []


def test_scope_cancellation_is_checked_during_paging_and_before_return(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _query(page_size=1, max_records=2, max_pages=2)
    first = _page(query, [_row("2026-01-02")], count=2, limit=1)
    cancellation = threading.Event()
    fake, key_calls = install_transport(
        [
            _response(first),
            _response(_page(query, [_row("2026-01-03")], count=2, offset=1, limit=1)),
        ],
        after_get=lambda index: cancellation.set() if index == 1 else None,
    )
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_sofr_pages(query)
    assert len(fake.calls) == 1
    assert key_calls == [1]

    cancellation.clear()
    page = _page(_query(), [], count=0)
    install_transport([_response(page)])
    original_validate = client_module._validate_pages

    def cancel_after_validation(
        pages: tuple[dict[str, Any], ...], validated_query: FredSofrQuery
    ) -> Any:
        result = original_validate(pages, validated_query)
        cancellation.set()
        return result

    monkeypatch.setattr(client_module, "_validate_pages", cancel_after_validation)
    with _scope(cancellation=cancellation), pytest.raises(RequestExecutionCancelledError):
        client_module.fetch_sofr_pages(_query())


def test_http_failures_are_stable_and_do_not_expose_credentials_or_body(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, _ = install_transport([_Response(b"private-body", status=503)])

    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sofr_pages(query)

    assert caught.value.code == "FRED_HTTP_ERROR"
    assert caught.value.status == 503
    assert _API_KEY not in str(caught.value)
    assert "private-body" not in str(caught.value)
    assert fake.calls[0]["url"] == _URL

    transport_error = HttpFetchError(
        FailureCategory.NETWORK,
        url=_URL,
        host=_HOST,
        attempts=1,
        status=502,
    )
    install_transport([transport_error])
    with _scope(), pytest.raises(FredProviderError) as caught:
        client_module.fetch_sofr_pages(query)
    assert caught.value.code == "FRED_HTTP_ERROR"
    assert caught.value.status == 502
    assert _API_KEY not in str(caught.value)


def test_malformed_page_input_to_pure_validator_fails_cleanly() -> None:
    query = _query()
    with pytest.raises(FredProviderError):
        client_module.validate_sofr_pages(([1],), query)  # type: ignore[arg-type]


def test_request_page_response_requires_exact_int_status_and_bytes_content(
    install_transport: Callable[..., tuple[_RecordingGovHTTP, list[int]]],
) -> None:
    query = _query()
    fake, _ = install_transport([_Response(_compact_json(_page(query, [], count=0)), status=True)])
    with _scope(), pytest.raises(FredProviderError):
        client_module.fetch_sofr_pages(query)
    assert len(fake.calls) == 1
