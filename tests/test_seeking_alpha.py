"""The slice records documented UI capabilities without inventing wire contracts.

Wave 38 moved ``CalendarEarnings`` out of the refused set: the route, its required filter and its
page cursor were answered by the source itself in an unauthenticated round, and the model is now
bound. The assertions below therefore read both sides -- what is refused, what is bound, and that
the two together still account for all three inventory models, so no row can vanish quietly.
"""

from __future__ import annotations

import json
import types
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.catalog import get_provider
from opendata.data.providers.seeking_alpha.models import calendar_earnings_public as calendar
from opendata.data.providers.seeking_alpha.models import estimates_public as estimates
from opendata.data.providers.seeking_alpha.specs import DECLARED_MODELS, NOT_DECLARED
from opendata.data.request_budget import RequestAttemptLimitError
from scripts.quality.iter2_witnesses.seeking_alpha import _sa_eps_envelope, _sa_sales_envelope

INVENTORY_MODELS = frozenset({"CalendarEarnings", "ForwardEpsEstimates", "ForwardSalesEstimates"})


def test_every_inventory_model_is_now_bound_by_a_descriptor() -> None:
    """The refusal ledger is empty: both estimates rows landed in wave 55."""
    assert DECLARED_MODELS == ()
    assert NOT_DECLARED == {}


def test_the_bound_and_refused_sets_partition_the_inventory_models() -> None:
    """Every inventory row is either bound by a descriptor or named in the refusal ledger."""
    provider = get_provider("seeking_alpha")
    bound = {
        model_id
        for binding in provider.fetcher_bindings
        for model_id in binding.canonical_model_ids
    }
    assert bound & set(NOT_DECLARED) == set(), "bound *and* refused"
    assert bound | set(NOT_DECLARED) == INVENTORY_MODELS, "a silently dropped identity"
    assert bound == INVENTORY_MODELS
    assert provider.is_implemented
    assert provider.credentials == (), "all three routes were read without any credential"


def _http_sequence(
    monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, replies: list[object]
) -> list[tuple[str, dict[str, Any]]]:
    """Replace only the module's transport; leave every query/parser/row validator live."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class Client:
        def get(self, url: str, **kwargs: Any) -> object:
            calls.append((url, deepcopy(kwargs)))
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, bytes):
                return types.SimpleNamespace(content=reply)
            if isinstance(reply, types.SimpleNamespace):
                return reply
            return types.SimpleNamespace(status_code=200, json=lambda: deepcopy(reply))

    monkeypatch.setattr(module, "get_shared_http_client", lambda: Client())
    return calls


def _run(fetcher: Any, **kwargs: object) -> Any:
    query = fetcher.transform_query(**kwargs)
    return fetcher.transform_data(fetcher.extract_data(query, FetchContext(timeout=2.0)), query)


def _replace(document: dict[str, Any], path: tuple[object, ...], value: object) -> None:
    parent: Any = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


@pytest.fixture
def calendar_captures() -> list[bytes]:
    """Replay the verified official captures unchanged, including the broken site-root cursor."""
    sources = (
        Path(__file__).resolve().parents[1]
        / "docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/sources"
    )
    return [
        (sources / "sa-calendar-second-date.json").read_bytes(),
        (sources / "sa-calendar-page2-api-form.json").read_bytes(),
    ]


def _calendar_page(calendar_captures: list[bytes]) -> dict[str, Any]:
    """Narrow a copied official resource to one page for corruption cases."""
    document = json.loads(calendar_captures[0])
    document["data"] = document["data"][:1]
    document["meta"] = {"size": 1, "total_count": 1, "total_pages": 1}
    document["links"]["next"] = None
    return document


def test_calendar_replays_all_50_plus_6_official_rows_and_builds_api_cursor(
    calendar_captures: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _http_sequence(monkeypatch, calendar, calendar_captures)
    rows = _run(
        calendar.SeekingAlphaCalendarEarningsFetcher(),
        start_date="2026-10-20",
        end_date="2026-10-20",
        use_cache=False,
    )
    documents = [json.loads(body) for body in calendar_captures]
    assert [len(document["data"]) for document in documents] == [50, 6]
    assert len(rows) == 56
    # Independent field-by-field expectation from the published resources, not adapter helpers.
    expected = [
        {
            "report_date": "2026-10-20",
            "symbol": record["attributes"]["slug"],
            "name": record["attributes"]["name"],
            "eps_previous": None,
            "eps_consensus": None,
            "market_cap": record["attributes"]["marketcap"],
            "reporting_time": record["attributes"]["release_time"],
            "exchange": record["attributes"]["exchange"],
            "sector_id": record["attributes"]["sector_id"],
        }
        for document in documents
        for record in document["data"]
    ]
    assert [row.model_dump(mode="json") for row in rows] == sorted(
        expected, key=lambda row: row["symbol"]
    )
    assert calls == [
        (
            "https://seekingalpha.com/api/v3/earnings_calendar/tickers",
            {
                "params": params,
                "headers": {"Accept": "application/json"},
                "timeout": (2.0, 2.0),
                "source": "seeking_alpha",
                "use_cache": False,
            },
        )
        for params in (
            {"filter[selected_date]": "2026-10-20"},
            {"filter[selected_date]": "2026-10-20", "page[number]": "2"},
        )
    ]
    assert next(row for row in rows if row.symbol == "LOR:CA").name == "L'Oréal S.A."
    assert next(row for row in rows if row.symbol == "FUN.P:CA").sector_id is None


@pytest.mark.parametrize(
    ("query", "symbols"),
    [
        ({"symbol": " fvcb "}, ["FVCB"]),
        ({"limit": 3}, ["ABB:CA", "ADC", "ALK"]),
        ({"symbol": " fvcb ", "limit": 1}, ["FVCB"]),
        ({"symbol": "ABSENT"}, []),
    ],
)
def test_calendar_symbol_and_limit_are_local_after_complete_sorted_pagination(
    query: dict[str, object],
    symbols: list[str],
    calendar_captures: list[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _http_sequence(monkeypatch, calendar, calendar_captures)
    rows = _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20", **query)
    assert [row.symbol for row in rows] == symbols
    assert [call[1]["params"] for call in calls] == [
        {"filter[selected_date]": "2026-10-20"},
        {"filter[selected_date]": "2026-10-20", "page[number]": "2"},
    ]
    assert all(call[1]["use_cache"] is True for call in calls)


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("meta", "total_pages"), True, "BAD_PAGINATION"),
        (("meta", "total_pages"), 0, "BAD_PAGINATION"),
        (("meta", "total_pages"), "2", "BAD_PAGINATION"),
        (("meta", "total_pages"), 2, "BAD_PAGINATION"),
        (("meta", "size"), 2, "SIZE_MISMATCH"),
        (("meta",), [], "BAD_ENVELOPE"),
        (("data",), {}, "BAD_ENVELOPE"),
        (("data", 0), [], "BAD_ROW"),
        (("data", 0, "type"), "ticker", "BAD_ROW_TYPE"),
        (("data", 0, "attributes"), None, "BAD_ATTRIBUTES"),
        (("data", 0, "attributes", "release_date"), None, "BAD_DATE_FIELD"),
        (("data", 0, "attributes", "release_date"), "short", "BAD_DATE_FIELD"),
        (("data", 0, "attributes", "release_date"), "2026-02-30", "BAD_DATE_FIELD"),
        (("data", 0, "attributes", "release_date"), "2026-10-21", "DATE_MISMATCH"),
        (("data", 0, "attributes", "slug"), True, "BAD_TEXT_FIELD"),
        (("data", 0, "attributes", "slug"), " ", "BAD_ROW"),
        (("data", 0, "attributes", "name"), 10, "BAD_TEXT_FIELD"),
        (("data", 0, "attributes", "marketcap"), "1.0", "BAD_NUMERIC_FIELD"),
        (("data", 0, "attributes", "marketcap"), False, "BAD_NUMERIC_FIELD"),
        (("data", 0, "attributes", "sector_id"), 40.5, "BAD_INTEGER_FIELD"),
        (("data", 0, "attributes", "sector_id"), True, "BAD_INTEGER_FIELD"),
    ],
)
def test_calendar_refuses_corrupt_pagination_dates_and_resource_fields(
    path: tuple[object, ...],
    value: object,
    code: str,
    calendar_captures: list[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _calendar_page(calendar_captures)
    _replace(document, path, value)
    calls = _http_sequence(monkeypatch, calendar, [json.dumps(document).encode()])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20")
    assert error.value.code == f"SEEKING_ALPHA_CALENDAR_{code}"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not-json", "BAD_JSON"),
        (b"\xff", "BAD_JSON"),
        (b"[]", "BAD_ENVELOPE"),
        (b" ", "BAD_JSON"),
        (b"x" * (4 * 1024 * 1024 + 1), "RESPONSE_TOO_LARGE"),
    ],
)
def test_calendar_refuses_invalid_or_oversized_wire_bodies(
    body: bytes, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _http_sequence(monkeypatch, calendar, [body])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20")
    assert error.value.code == f"SEEKING_ALPHA_CALENDAR_{code}"
    assert len(calls) == 1


@pytest.mark.parametrize("content", [None, "{}", b""])
def test_calendar_refuses_a_response_without_nonempty_bytes(
    content: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _http_sequence(monkeypatch, calendar, [types.SimpleNamespace(content=content)])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), end_date="2026-10-20")
    assert error.value.code == "SEEKING_ALPHA_CALENDAR_BAD_RESPONSE"


def test_calendar_nullable_cells_bom_and_inclusive_eight_day_window(
    calendar_captures: list[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _calendar_page(calendar_captures)
    document["data"][0]["attributes"].update(
        name=" ", exchange=None, release_time=None, sector_id=None, marketcap=None
    )
    replies = []
    for offset in range(8):
        day = date(2026, 10, 20) + timedelta(days=offset)
        page = deepcopy(document)
        page["data"][0]["attributes"]["release_date"] = f"{day}T23:59:00-04:00"
        replies.append(b"\xef\xbb\xbf" + json.dumps(page).encode())
    calls = _http_sequence(monkeypatch, calendar, replies)
    rows = _run(
        calendar.SeekingAlphaCalendarEarningsFetcher(),
        start_date="2026-10-20",
        end_date="2026-10-27",
        use_cache=None,
    )
    assert [row.report_date for row in rows] == [date(2026, 10, 20 + n) for n in range(8)]
    assert all(row.name is row.market_cap is row.sector_id is row.exchange is None for row in rows)
    assert [call[1]["params"] for call in calls] == [
        {"filter[selected_date]": f"2026-10-{20 + n}"} for n in range(8)
    ]
    assert all(call[1]["use_cache"] is True for call in calls)


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": True},
        {"symbol": " "},
        {"symbol": False},
        {"use_cache": "yes"},
        {"start_date": "2026-10-21", "end_date": "2026-10-20"},
    ],
)
def test_calendar_invalid_queries_refuse_before_transport(
    query: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _http_sequence(monkeypatch, calendar, [])
    with pytest.raises(ValidationError):
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), **query)
    assert calls == []


def test_calendar_window_over_eight_days_refuses_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _http_sequence(monkeypatch, calendar, [])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(
            calendar.SeekingAlphaCalendarEarningsFetcher(),
            start_date="2026-10-20",
            end_date="2026-10-28",
            limit=1,
        )
    assert error.value.code == "SEEKING_ALPHA_CALENDAR_WINDOW_TOO_LARGE_8D"
    assert calls == []


@pytest.mark.parametrize(
    ("bound", "expected_calls", "code"),
    [
        ("pages", 25, "TOO_MANY_PAGES"),
        ("rows", 1, "RESPONSE_TOO_LARGE"),
        ("bytes", 6, "RESPONSE_TOO_LARGE"),
    ],
)
def test_calendar_walk_is_bounded_even_when_local_limit_is_one(
    bound: str,
    expected_calls: int,
    code: str,
    calendar_captures: list[bytes],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _calendar_page(calendar_captures)
    query: dict[str, object] = {"start_date": "2026-10-20", "limit": 1}
    if bound == "pages":
        document["meta"]["total_pages"] = 26
        document["links"]["next"] = "/earnings_calendar/tickers?page[number]=2"
        replies = [json.dumps(document).encode()] * 25
    elif bound == "rows":
        document["data"] *= 5001
        document["meta"]["size"] = 5001
        replies = [json.dumps(document).encode()]
    else:
        query["end_date"] = "2026-10-27"
        empty = {"data": [], "meta": {"size": 0, "total_pages": 1}, "links": {"next": None}}
        body = json.dumps(empty).encode().ljust(3 * 1024 * 1024, b" ")
        replies = [body] * 8
    calls = _http_sequence(monkeypatch, calendar, replies)
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), **query)
    assert error.value.code == f"SEEKING_ALPHA_CALENDAR_{code}"
    assert len(calls) == expected_calls


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "UNAUTHORIZED"),
        (403, "FORBIDDEN"),
        (404, "NOT_FOUND"),
        (429, "RATE_LIMITED"),
        (503, "UPSTREAM_ERROR"),
        (400, "HTTP_ERROR"),
        (None, "HTTP_ERROR"),
    ],
)
def test_calendar_sanitizes_governed_http_failures(
    status: int | None, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = HttpFetchError(
        FailureCategory.NETWORK,
        url="https://seekingalpha.com/private",
        host="seekingalpha.com",
        attempts=1,
        status=status,
    )
    calls = _http_sequence(monkeypatch, calendar, [failure])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20")
    assert error.value.code == f"SEEKING_ALPHA_CALENDAR_{code}"
    assert error.value.status == status
    assert "private" not in str(error.value)
    assert len(calls) == 1


def test_calendar_transport_error_is_sanitized_but_budget_refusal_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http_sequence(monkeypatch, calendar, [RuntimeError("private detail")])
    with pytest.raises(calendar.SeekingAlphaCalendarEarningsError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20")
    assert str(error.value) == "SEEKING_ALPHA_CALENDAR_TRANSPORT_ERROR"
    failure = RequestAttemptLimitError("fixture budget exhausted")
    _http_sequence(monkeypatch, calendar, [failure])
    with pytest.raises(RequestAttemptLimitError) as error:
        _run(calendar.SeekingAlphaCalendarEarningsFetcher(), start_date="2026-10-20")
    assert error.value is failure


_ESTIMATE_CASES = [
    (estimates.SeekingAlphaForwardEpsEstimatesFetcher, _sa_eps_envelope),
    (estimates.SeekingAlphaForwardSalesEstimatesFetcher, _sa_sales_envelope),
]
_EPS_EXPECTED = [
    {
        "eps_gaap_actual": 1.05,
        "eps_gaap_consensus_low": 0.88,
        "eps_gaap_consensus_mean": 0.97,
        "eps_gaap_consensus_high": 1.12,
        "eps_normalized_actual": 1.15,
        "eps_normalized_consensus_low": 0.90,
        "eps_normalized_consensus_mean": 1.01,
        "eps_normalized_consensus_high": 1.20,
        "eps_normalized_num_of_estimates": 8,
    },
    {
        "eps_gaap_actual": 1.08,
        "eps_gaap_consensus_low": 0.92,
        "eps_gaap_consensus_mean": 1.02,
        "eps_gaap_consensus_high": 1.18,
        "eps_normalized_actual": 1.19,
        "eps_normalized_consensus_low": 0.95,
        "eps_normalized_consensus_mean": 1.06,
        "eps_normalized_consensus_high": 1.25,
        "eps_normalized_num_of_estimates": 7,
    },
]
_SALES_EXPECTED = [
    {
        "revenue_actual": 90753000000.0,
        "revenue_consensus_low": 88100000000.0,
        "revenue_consensus_mean": 92400000000.0,
        "revenue_consensus_high": 96800000000.0,
        "revenue_num_of_estimates": 9,
    },
    {
        "revenue_actual": 9534000000.0,
        "revenue_consensus_low": 91000000000.0,
        "revenue_consensus_mean": 97100000000.0,
        "revenue_consensus_high": 103400000000.0,
        "revenue_num_of_estimates": 8,
    },
]


def _estimate_replies(builder: Any) -> list[dict[str, Any]]:
    """Hand-authored witness values are synthetic, not official EPS/revenue captures."""
    return [response["body"] for response in builder()["responses"]]


def _estimate_record(replies: list[dict[str, Any]], item: str = "eps_gaap_actual") -> Any:
    return replies[1]["estimates"]["1150"][item]["0"][0]


@pytest.mark.parametrize(("fetcher_type", "builder"), _ESTIMATE_CASES)
@pytest.mark.parametrize("period", ["annual", "quarter"])
def test_estimates_resolve_search_id_map_params_and_transpose_every_synthetic_item(
    fetcher_type: type,
    builder: Any,
    period: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _estimate_replies(builder)
    # Exercise integer search ids, not only the numeric strings in the builder.
    replies[0]["data"][0]["id"] = 1150
    if period == "annual":
        for buckets in replies[1]["estimates"]["1150"].values():
            for records in buckets.values():
                records[0]["period"].update(
                    periodtypeid="annual", fiscalquarter=None, calendarquarter=None
                )
    calls = _http_sequence(monkeypatch, estimates, replies)
    rows = _run(fetcher_type(), symbol=" aapl ", period=period, relative_periods=2)
    values = (
        _EPS_EXPECTED
        if fetcher_type is estimates.SeekingAlphaForwardEpsEstimatesFetcher
        else _SALES_EXPECTED
    )
    expected = [
        {
            "symbol": "AAPL",
            "sa_ticker_id": "1150",
            "relative_period": str(index),
            "period_type": "annual" if period == "annual" else "quarterly",
            "fiscal_year": 2027,
            "fiscal_quarter": None if period == "annual" else index + 1,
            "calendar_year": 2026,
            "calendar_quarter": None if period == "annual" else index + 1,
            "period_end_date": f"2026-0{index + 4}-30",
            "as_of": "2024-06-19T00:06:03-04:00",
            **value,
        }
        for index, value in enumerate(values)
    ]
    assert [row.model_dump(mode="json") for row in rows] == expected
    item_names = list(values[0])
    model_id = "ForwardEpsEstimates" if builder is _sa_eps_envelope else "ForwardSalesEstimates"
    assert calls == [
        (
            "https://seekingalpha.com/api/v3/searches",
            {
                "params": {"filter[phrase]": "AAPL", "count": "1", "type": "ticker"},
                "timeout": 2.0,
                "source": "seeking_alpha",
                "headers": {"Accept": "application/json"},
                "cache_context": {"canonical_model": model_id},
            },
        ),
        (
            "https://seekingalpha.com/api/v3/symbol_data/estimates",
            {
                "params": {
                    "ticker_ids": "1150",
                    "period_type": "annual" if period == "annual" else "quarterly",
                    "relative_periods": "0,1,2",
                    "estimates_data_items": ",".join(item_names),
                },
                "timeout": 2.0,
                "source": "seeking_alpha",
                "headers": {"Accept": "application/json"},
                "cache_context": {"canonical_model": model_id},
            },
        ),
    ]
    assert type(rows[0].model_dump()[item_names[-1]]) is int


@pytest.mark.parametrize(("fetcher_type", "builder"), _ESTIMATE_CASES)
def test_estimates_multisymbol_requests_preserve_id_and_symbol_attribution(
    fetcher_type: type,
    builder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _estimate_replies(builder)
    second = deepcopy(first)
    second[0]["data"][0]["id"] = "42"
    series = second[1]["estimates"].pop("1150")
    actual = "eps_gaap_actual" if builder is _sa_eps_envelope else "revenue_actual"
    series[actual]["0"][0]["dataitemvalue"] = "123.5"
    second[1]["estimates"] = {"unrelated": {}, "42": series}
    calls = _http_sequence(monkeypatch, estimates, first + second)
    rows = _run(fetcher_type(), symbol=" aapl, , msft ", relative_periods=1)
    assert [(row.symbol, row.sa_ticker_id, row.relative_period) for row in rows] == [
        ("AAPL", "1150", "0"),
        ("AAPL", "1150", "1"),
        ("MSFT", "42", "0"),
        ("MSFT", "42", "1"),
    ]
    assert getattr(rows[2], actual) == 123.5
    assert [call[1]["params"].get("filter[phrase]") for call in calls[::2]] == ["AAPL", "MSFT"]
    assert [call[1]["params"]["ticker_ids"] for call in calls[1::2]] == ["1150", "42"]
    assert all(call[1]["params"]["relative_periods"] == "0,1" for call in calls[1::2])


def test_estimates_missing_items_and_optional_period_cells_stay_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _estimate_replies(_sa_eps_envelope)
    record = _estimate_record(replies)
    record["effectivedate"] = ""
    record["dataitemvalue"] = None
    record["period"] = {"periodenddate": "2026-04-30T23:59:00-04:00", "fiscalyear": "2027.0"}
    replies[1]["estimates"]["1150"] = {"eps_gaap_actual": {"0": [record]}}
    _http_sequence(monkeypatch, estimates, replies)
    row = _run(estimates.SeekingAlphaForwardEpsEstimatesFetcher(), symbol="AAPL")[0]
    assert row.model_dump() == {
        "symbol": "AAPL",
        "sa_ticker_id": "1150",
        "relative_period": "0",
        "period_type": "quarterly",
        "fiscal_year": 2027,
        "fiscal_quarter": None,
        "calendar_year": None,
        "calendar_quarter": None,
        "period_end_date": date(2026, 4, 30),
        "as_of": None,
        **dict.fromkeys(_EPS_EXPECTED[0]),
    }


def test_estimates_transposition_keys_both_relative_period_and_end_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _estimate_replies(_sa_eps_envelope)
    original = deepcopy(_estimate_record(replies))
    later = deepcopy(original)
    later["period"]["periodenddate"] = "2026-06-30T00:00:00Z"
    later["effectivedate"] = "2026-01-02T12:00:00Z"
    later["dataitemvalue"] = 3
    repeated = deepcopy(original)
    repeated["dataitemvalue"] = "2.5"
    mean = deepcopy(later)
    mean["dataitemvalue"] = "4.25"
    replies[1]["estimates"]["1150"] = {
        "eps_gaap_actual": {"1": [repeated], "0": [original, later]},
        "eps_gaap_consensus_mean": {"0": [mean]},
    }
    _http_sequence(monkeypatch, estimates, replies)
    rows = _run(estimates.SeekingAlphaForwardEpsEstimatesFetcher(), symbol="AAPL")
    assert [
        (row.relative_period, row.period_end_date, row.eps_gaap_actual, row.eps_gaap_consensus_mean)
        for row in rows
    ] == [
        ("0", date(2026, 4, 30), 1.05, None),
        ("0", date(2026, 6, 30), 3.0, 4.25),
        ("1", date(2026, 4, 30), 2.5, None),
    ]
    assert rows[1].as_of == datetime.fromisoformat("2026-01-02T12:00:00+00:00")


@pytest.mark.parametrize(
    "data",
    [None, {}, [], [None], [{"id": None}], [{"id": "ticker"}], [{"id": True}], [{"id": ""}]],
)
def test_estimates_unresolved_search_id_refuses_without_estimates_request(
    data: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _http_sequence(monkeypatch, estimates, [{"data": data}])
    with pytest.raises(ProviderEngineError) as error:
        _run(estimates.SeekingAlphaForwardEpsEstimatesFetcher(), symbol="AAPL")
    assert error.value.code == "SEEKING_ALPHA_ESTIMATES_ID_UNRESOLVED"
    assert [call[0] for call in calls] == ["https://seekingalpha.com/api/v3/searches"]


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("estimates",), [], "SHAPE_INVALID"),
        (("estimates", "1150"), [], "SHAPE_INVALID"),
        (("estimates", "1150", "eps_gaap_actual"), [], "SHAPE_INVALID"),
        (("estimates", "1150", "eps_gaap_actual", "0"), {}, "SHAPE_INVALID"),
        (("estimates", "1150", "eps_gaap_actual", "0", 0), [], "SHAPE_INVALID"),
        (("estimates", "1150", "eps_gaap_actual", "0", 0, "period"), None, "SHAPE_INVALID"),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "period", "periodenddate"),
            None,
            "SHAPE_INVALID",
        ),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "period", "periodenddate"),
            "not-date",
            "SHAPE_INVALID",
        ),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "period", "fiscalyear"),
            None,
            "SHAPE_INVALID",
        ),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "period", "fiscalyear"),
            "2027.5",
            "BAD_NUMERIC",
        ),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "period", "periodtypeid"),
            "monthly",
            "SHAPE_INVALID",
        ),
        (("estimates", "1150", "eps_gaap_actual", "0", 0, "effectivedate"), 123, "SHAPE_INVALID"),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "effectivedate"),
            "invalid",
            "SHAPE_INVALID",
        ),
        (("estimates", "1150", "eps_gaap_actual", "0", 0, "dataitemvalue"), True, "BAD_NUMERIC"),
        (("estimates", "1150", "eps_gaap_actual", "0", 0, "dataitemvalue"), {}, "BAD_NUMERIC"),
        (
            ("estimates", "1150", "eps_gaap_actual", "0", 0, "dataitemvalue"),
            "bad-number",
            "BAD_NUMERIC",
        ),
        (("estimates", "1150", "eps_gaap_actual", "0", 0, "dataitemvalue"), "NaN", "SHAPE_INVALID"),
        (
            ("estimates", "1150", "eps_normalized_num_of_estimates", "0", 0, "dataitemvalue"),
            "8.5",
            "BAD_NUMERIC",
        ),
        (
            ("estimates", "1150", "eps_normalized_num_of_estimates", "0", 0, "dataitemvalue"),
            "-1",
            "SHAPE_INVALID",
        ),
    ],
)
def test_estimates_refuse_malformed_series_periods_numbers_and_counts(
    path: tuple[object, ...],
    value: object,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _estimate_replies(_sa_eps_envelope)
    _replace(replies[1], path, value)
    calls = _http_sequence(monkeypatch, estimates, replies)
    with pytest.raises(ProviderEngineError) as error:
        _run(estimates.SeekingAlphaForwardEpsEstimatesFetcher(), symbol="AAPL")
    assert error.value.code == f"SEEKING_ALPHA_ESTIMATES_{code}"
    assert len(calls) == 2


@pytest.mark.parametrize(("fetcher_type", "builder"), _ESTIMATE_CASES)
@pytest.mark.parametrize("series", [None, {}, {"unknown_item": {}}, {"eps_gaap_actual": {"0": []}}])
def test_estimates_missing_or_empty_result_is_not_a_success(
    fetcher_type: type,
    builder: Any,
    series: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _estimate_replies(builder)
    replies[1]["estimates"] = {"1150": series}
    calls = _http_sequence(monkeypatch, estimates, replies)
    with pytest.raises(ProviderEngineError) as error:
        _run(fetcher_type(), symbol="AAPL")
    assert error.value.code == "SEEKING_ALPHA_ESTIMATES_EMPTY"
    assert len(calls) == 2


@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("failure_kind", ["http", "json", "envelope"])
def test_estimates_refuse_http_and_json_failures_at_either_transport_leg(
    stage: int,
    failure_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[object] = _estimate_replies(_sa_sales_envelope)
    if failure_kind == "http":
        replies[stage] = types.SimpleNamespace(status_code=403)
    elif failure_kind == "json":

        def bad_json() -> None:
            raise ValueError("malformed JSON")

        replies[stage] = types.SimpleNamespace(status_code=200, json=bad_json)
    else:
        replies[stage] = []
    calls = _http_sequence(monkeypatch, estimates, replies)
    with pytest.raises(ProviderEngineError) as error:
        _run(estimates.SeekingAlphaForwardSalesEstimatesFetcher(), symbol="AAPL")
    assert error.value.code == (
        "SEEKING_ALPHA_ESTIMATES_HTTP_ERROR"
        if failure_kind == "http"
        else "SEEKING_ALPHA_ESTIMATES_SHAPE_INVALID"
    )
    assert len(calls) == stage + 1
    if failure_kind == "http":
        assert error.value.status == 403
        assert error.value.url == calls[-1][0]


@pytest.mark.parametrize(
    ("query", "invalid"),
    [
        ({"symbol": " , "}, "symbol"),
        ({"symbol": ",".join(f"S{n}" for n in range(9))}, "symbol"),
        ({"symbol": "AAPL", "relative_periods": 0}, "relative_periods"),
        ({"symbol": "AAPL", "relative_periods": 9}, "relative_periods"),
    ],
)
def test_estimates_bounded_query_refusal_precedes_search(
    query: dict[str, object],
    invalid: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _http_sequence(monkeypatch, estimates, [])
    with pytest.raises(ProviderEngineError) as error:
        _run(estimates.SeekingAlphaForwardSalesEstimatesFetcher(), **query)
    assert error.value.code == "SEEKING_ALPHA_ESTIMATES_QUERY_INVALID"
    assert error.value.invalid == (invalid,)
    assert calls == []
