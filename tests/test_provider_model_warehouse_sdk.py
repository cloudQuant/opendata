"""Offline SDK tests for native provider-model warehouse reads."""

from __future__ import annotations

import copy
import json
import math
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata_client import (
    AuthenticationError,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    PermissionDeniedError,
    UnsupportedQueryError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_API_KEY = "od-warehouse-test-key"
_SECRET_SENTINEL = "WAREHOUSE_SDK_SECRET_SENTINEL"


def _envelope(data: Any, *, success: bool = True, message: str = "success") -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": success, "message": message, "data": data},
    )


def _warehouse_data(
    source: str,
    model: str,
    domain: str,
    results: list[dict[str, Any]],
    pagination: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": source,
        "model": model,
        "domain": domain,
        "verified": False,
        "read_at": "2026-10-08T02:00:00+00:00",
        "completeness": "NOT_ASSESSED",
        "snapshot_scope": "single_read_transaction",
        "results": results,
        "pagination": pagination,
        "native_metadata": {"retained": True, "provider": {"version": 2}},
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpendataClient:
    return OpendataClient(
        "http://api.test",
        api_key=_API_KEY,
        transport=httpx.MockTransport(handler),
    )


def _base_data() -> dict[str, Any]:
    return _warehouse_data(
        "fred",
        "FredSeries",
        "fred_series",
        [{"series_id": "GDP", "date": "2024-01-01"}],
        {"limit": 10, "offset": 0, "returned": 1, "total": None},
    )


def _invalid_page_cases() -> list[tuple[str, dict[str, Any]]]:
    base = _base_data()
    cases: list[tuple[str, dict[str, Any]]] = []

    def with_page(name: str, pagination: dict[str, Any]) -> None:
        cases.append((name, {**base, "pagination": pagination}))

    with_page("missing-limit", {"offset": 0, "returned": 1, "total": None})
    with_page("missing-offset", {"limit": 1, "returned": 1, "total": None})
    with_page("missing-returned", {"limit": 1, "offset": 0, "total": None})
    with_page("missing-total", {"limit": 1, "offset": 0, "returned": 1})
    with_page("bool-limit", {"limit": True, "offset": 0, "returned": 1, "total": None})
    with_page("bool-offset", {"limit": 1, "offset": False, "returned": 1, "total": None})
    with_page("bool-returned", {"limit": 1, "offset": 0, "returned": True, "total": None})
    with_page("non-null-total", {"limit": 1, "offset": 0, "returned": 1, "total": 1})
    with_page("limit-too-small", {"limit": 0, "offset": 0, "returned": 1, "total": None})
    with_page("limit-too-large", {"limit": 1001, "offset": 0, "returned": 1, "total": None})
    with_page("negative-offset", {"limit": 1, "offset": -1, "returned": 1, "total": None})
    with_page("wrong-returned", {"limit": 1, "offset": 0, "returned": 0, "total": None})
    with_page("too-many-results", {"limit": 1, "offset": 0, "returned": 1, "total": None})
    cases[-1] = (
        cases[-1][0],
        {**cases[-1][1], "results": [{"id": 1}, {"id": 2}]},
    )
    return cases


@pytest.mark.parametrize(
    ("source", "model", "domain", "query", "rows_and_pagination"),
    [
        (
            "fred",
            "FredSeries",
            "fred_series",
            {
                "filters": {"series_id": "CPIAUCSL", "requested_frequency": None},
                "start": "2024-02-01",
                "end": "2024-02-28",
                "limit": 2,
                "offset": 0,
            },
            {
                "rows": [
                    SeriesObservation(
                        series_id="CPIAUCSL",
                        date=date(2024, 2, 1),
                        value=None,
                        realtime_start=date(2024, 1, 1),
                        realtime_end=date(2024, 12, 31),
                        transform_units="lin",
                        output_type=1,
                        requested_frequency=None,
                        requested_aggregation_method="avg",
                    ).model_dump(mode="json"),
                    SeriesObservation(
                        series_id="CPIAUCSL",
                        date=date(2024, 2, 1),
                        value=3.2,
                        realtime_start=date(2024, 3, 1),
                        realtime_end=date(2024, 12, 31),
                        transform_units="lin",
                        output_type=1,
                        requested_frequency=None,
                        requested_aggregation_method="avg",
                    ).model_dump(mode="json"),
                ],
                "pagination": {"limit": 2, "offset": 0, "returned": 2, "total": None},
            },
        ),
        (
            "bls",
            "BlsSeries",
            "bls_series",
            {
                "filters": {"period": "M13", "api_version": "v2"},
                "start": 2025,
                "end": 2025,
                "limit": 1,
                "offset": 0,
            },
            {
                "rows": [
                    BlsObservation(
                        series_id="LNS14000000",
                        year=2025,
                        period="M13",
                        period_name="Annual average",
                        value=4.1,
                        footnotes=(
                            BlsFootnote(code="P", text="Preliminary."),
                            BlsFootnote(),
                        ),
                        latest=None,
                        preliminary=True,
                        api_version="v2",
                    ).model_dump(mode="json")
                ],
                "pagination": {"limit": 1, "offset": 0, "returned": 1, "total": None},
            },
        ),
        (
            "fmp",
            "EquityHistorical",
            "equity_historical",
            {
                "filters": {
                    "symbol": "AAPL",
                    "query_window_scope": "explicit",
                    "close_adjustment_semantics": "split_adjusted_per_source_faq",
                },
                "start": "2024-01-01",
                "end": "2024-01-31",
                "limit": 1,
                "offset": 0,
            },
            {
                "rows": [
                    EquityHistorical(
                        symbol="AAPL",
                        date=date(2024, 1, 2),
                        open=100.0,
                        high=101.0,
                        low=99.0,
                        close=-0.0,
                        volume=2**256 + 17,
                        change=0.5,
                        vwap=100.25,
                        currency=None,
                        volume_unit=None,
                        query_window_scope="explicit",
                    ).model_dump(mode="json")
                ],
                "pagination": {"limit": 1, "offset": 0, "returned": 1, "total": None},
            },
        ),
    ],
    ids=["fred-series", "bls-m13", "fmp-historical"],
)
def test_posts_one_exact_native_warehouse_query_and_preserves_payload(
    source: str,
    model: str,
    domain: str,
    query: dict[str, Any],
    rows_and_pagination: dict[str, Any],
) -> None:
    original_query = copy.deepcopy(query)
    rows = rows_and_pagination["rows"]
    pagination = rows_and_pagination["pagination"]
    assert isinstance(rows, list)
    assert isinstance(pagination, dict)
    data = _warehouse_data(source, model, domain, rows, pagination)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(data)

    with _client(handler) as client:
        result = client.query_provider_model_warehouse(source, model, query)

    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == (f"/api/v1/providers/{source}/models/{model}/warehouse/query")
    assert seen[0].headers["x-api-key"] == _API_KEY
    assert seen[0].headers["content-type"] == "application/json"
    body = json.loads(seen[0].content)
    assert body == {"query": query}
    assert set(body) == {"query"}
    assert "ctx" not in body and "budget" not in body and "grant" not in body
    assert query == original_query
    assert result == data
    assert result["verified"] is False
    assert result["snapshot_scope"] == "single_read_transaction"
    assert result["completeness"] == "NOT_ASSESSED"
    assert result["native_metadata"] == {"retained": True, "provider": {"version": 2}}
    if model == "FredSeries":
        assert len(result["results"]) == 2
        assert result["results"][0]["date"] == result["results"][1]["date"]
        assert result["results"][0]["realtime_start"] == "2024-01-01"
        assert result["results"][1]["realtime_start"] == "2024-03-01"
        assert result["results"][0]["value"] is None
        assert result["results"][1]["value"] == 3.2
        assert result["results"][0]["requested_frequency"] is None
    elif model == "BlsSeries":
        bls_result = result["results"][0]
        assert type(bls_result["year"]) is int
        assert bls_result["period"] == "M13"
        assert bls_result["period_name"] == "Annual average"
        assert bls_result["footnotes"][0] == {"code": "P", "text": "Preliminary."}
    else:
        fmp_result = result["results"][0]
        assert type(fmp_result["volume"]) is int
        assert fmp_result["volume"] == 2**256 + 17
        assert fmp_result["close"] == 0.0
        assert math.copysign(1.0, fmp_result["close"]) == -1.0
        assert fmp_result["currency"] is None
        assert fmp_result["volume_unit"] is None


@pytest.mark.parametrize(
    ("source", "model"),
    [
        ("auto", "FredSeries"),
        ("fred", "auto"),
        ("bad/source", "FredSeries"),
        ("fred", "bad?model=x"),
        (None, "FredSeries"),
        ("fred", 7),
    ],
)
def test_invalid_identity_fails_before_transport(source: Any, model: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(_base_data())

    with (
        _client(handler) as client,
        pytest.raises(ValueError, match="valid provider/model identifier") as error,
    ):
        client.query_provider_model_warehouse(source, model, {})

    assert seen == []
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_json_queries_fail_before_transport_without_mutation() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    deep: list[Any] = []
    cursor = deep
    for _ in range(300):
        child: list[Any] = []
        cursor.append(child)
        cursor = child
    queries: list[Any] = [
        None,
        [],
        {"metric": float("nan")},
        {"metric": float("inf")},
        {1: "first", "1": "second"},
        {"nested": [{None: "null key"}]},
        cyclic,
        {"deep": deep},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(_base_data())

    with _client(handler) as client:
        for query in queries:
            with pytest.raises(ValueError) as error:
                client.query_provider_model_warehouse("fred", "FredSeries", query)
            assert error.value.__cause__ is None
            assert error.value.__context__ is None

    assert seen == []
    assert cyclic["self"] is cyclic
    assert len(deep) == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, InvalidQueryError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (422, InvalidQueryError),
        (429, OpendataClientError),
        (501, UnsupportedQueryError),
        (503, OpendataClientError),
        (504, OpendataClientError),
    ],
)
def test_http_errors_use_existing_mapping_without_reflecting_secrets(
    status: int, error_type: type[OpendataClientError]
) -> None:
    query = {"series_id": _SECRET_SENTINEL}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"detail": f"{_API_KEY} {_SECRET_SENTINEL}"},
            request=request,
        )

    with (
        _client(handler) as client,
        pytest.raises(error_type) as error,
    ):
        client.query_provider_model_warehouse("fred", "FredSeries", query)

    assert type(error.value) is error_type
    assert f"HTTP {status}" in str(error.value)
    assert _API_KEY not in str(error.value)
    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_transport_exception_does_not_expose_query_or_cause() -> None:
    query = {"series_id": _SECRET_SENTINEL}

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection failed: {_API_KEY} {_SECRET_SENTINEL}",
            request=request,
        )

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.query_provider_model_warehouse("fred", "FredSeries", query)

    assert _API_KEY not in str(error.value)
    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_json_response_does_not_expose_body_or_cause() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=f"not-json {_SECRET_SENTINEL}".encode(),
            headers={"content-type": "application/json"},
            request=request,
        )

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.query_provider_model_warehouse("fred", "FredSeries", {"series_id": "GDP"})

    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "nonfinite",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_nonfinite_response_values_are_rejected_without_leaking_secrets(
    nonfinite: float,
) -> None:
    data = _base_data()
    data["native_metadata"] = {"secret": _SECRET_SENTINEL, "bad": nonfinite}

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {"success": True, "message": "success", "data": data},
            allow_nan=True,
        ).encode("utf-8")
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
            request=request,
        )

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.query_provider_model_warehouse("fred", "FredSeries", {"filters": {}})

    assert "invalid JSON values" in str(error.value)
    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"success": False, "message": _SECRET_SENTINEL, "data": _base_data()},
        {"success": 1, "data": _base_data()},
    ],
    ids=["null-envelope", "list-envelope", "failed-envelope", "nonboolean-success"],
)
def test_invalid_envelopes_are_sanitized(payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.query_provider_model_warehouse("fred", "FredSeries", {"series_id": "GDP"})

    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "data",
    [
        [],
        None,
        {**_base_data(), "source": "other"},
        {**_base_data(), "model": "OtherModel"},
        {**_base_data(), "domain": None},
        {**_base_data(), "verified": 1},
        {**_base_data(), "read_at": None},
        {**_base_data(), "completeness": "COMPLETE"},
        {**_base_data(), "snapshot_scope": "multiple_reads"},
        {**_base_data(), "results": "not-a-list"},
        {**_base_data(), "results": [["not", "a", "row"]]},
        {key: value for key, value in _base_data().items() if key != "pagination"},
        *[data for _, data in _invalid_page_cases()],
    ],
    ids=[
        "list-data",
        "null-data",
        "source-mismatch",
        "model-mismatch",
        "invalid-domain",
        "nonboolean-verified",
        "invalid-read-at",
        "wrong-completeness",
        "wrong-snapshot-scope",
        "nonlist-results",
        "nonobject-row",
        "missing-pagination",
        *[name for name, _ in _invalid_page_cases()],
    ],
)
def test_malformed_warehouse_response_shape_fails_safely(data: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _envelope(data)

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.query_provider_model_warehouse("fred", "FredSeries", {"series_id": _SECRET_SENTINEL})

    assert _SECRET_SENTINEL not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
