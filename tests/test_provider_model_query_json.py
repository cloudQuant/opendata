"""Strict JSON and bounded-stream tests for provider model queries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi import HTTPException, Request

from opendata.api import provider_model_query
from opendata.api.schemas import APIResponse

_BODY_LIMIT = 1024 * 1024
_SECRET_SENTINEL = "QUERY_JSON_SECRET_SENTINEL"

RouteInvoker = Callable[
    [Sequence[bytes], Sequence[tuple[bytes, bytes]]],
    tuple[APIResponse, list[bytes]],
]


def _make_request(
    chunks: Sequence[bytes], headers: Sequence[tuple[bytes, bytes]]
) -> tuple[Request, list[bytes]]:
    delivered: list[bytes] = []
    index = 0

    async def receive() -> dict[str, Any]:
        nonlocal index
        if index >= len(chunks):
            return {"type": "http.disconnect"}
        body = chunks[index]
        index += 1
        delivered.append(body)
        return {
            "type": "http.request",
            "body": body,
            "more_body": index < len(chunks),
        }

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/providers/fred/models/FredSeries/query",
        "raw_path": b"/providers/fred/models/FredSeries/query",
        "query_string": b"",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    return Request(scope, receive), delivered


@pytest.fixture
def route_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RouteInvoker, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def fake_query_provider_model(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"accepted": True}

    monkeypatch.setattr(
        provider_model_query,
        "query_provider_model",
        fake_query_provider_model,
    )

    def invoke(
        chunks: Sequence[bytes],
        headers: Sequence[tuple[bytes, bytes]] = (),
    ) -> tuple[APIResponse, list[bytes]]:
        request, delivered = _make_request(chunks, headers)
        response = asyncio.run(
            provider_model_query.query_registered_provider_model(
                "fred",
                "FredSeries",
                request,
                current_principal=object(),
                registry=object(),
                ctx=None,
            )
        )
        return response, delivered

    return invoke, calls


def _assert_rejected(
    invoke: RouteInvoker,
    calls: list[dict[str, Any]],
    chunks: Sequence[bytes],
    *,
    status_code: int = 400,
) -> HTTPException:
    with pytest.raises(HTTPException) as captured:
        invoke(chunks, ())
    error = captured.value
    assert error.status_code == status_code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET_SENTINEL not in str(error.detail)
    assert calls == []
    return error


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":{"series_id":"GDP"},"query":{"series_id":"CPI"}}',
        b'{"query":{"series_id":"GDP","series_id":"CPI"}}',
        b'{"query":{"nested":{"field":"first","field":"second"}}}',
    ],
    ids=["duplicate-query-key", "duplicate-field-key", "duplicate-nested-key"],
)
def test_duplicate_keys_at_any_depth_are_rejected(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]], body: bytes
) -> None:
    invoke, calls = route_harness

    error = _assert_rejected(invoke, calls, [body])

    assert error.detail == "Invalid provider model query request"


@pytest.mark.parametrize("number", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_json_numbers_are_rejected_without_leaking_parser_text(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]], number: bytes
) -> None:
    invoke, calls = route_harness
    body = b'{"query":{"value":' + number + b',"marker":"' + _SECRET_SENTINEL.encode() + b'"}}'

    error = _assert_rejected(invoke, calls, [body])

    assert error.detail == "Invalid provider model query request"


def test_invalid_utf8_is_rejected_without_parser_cause(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    invoke, calls = route_harness

    _assert_rejected(
        invoke,
        calls,
        [b'{"query":{"marker":"' + _SECRET_SENTINEL.encode() + b'\xff"}}'],
    )


def _nested_query_body(levels: int) -> bytes:
    value = b"0"
    for _ in range(levels):
        value = b"[" + value + b"]"
    return b'{"query":{"deep":' + value + b"}}"


def test_query_depth_over_256_is_rejected_iteratively(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    invoke, calls = route_harness

    _assert_rejected(invoke, calls, [_nested_query_body(600)])


def test_query_at_depth_256_is_accepted(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    invoke, calls = route_harness

    response, _ = invoke([_nested_query_body(255)], ())

    assert response.success is True
    assert len(calls) == 1
    nested = calls[0]["query"]["deep"]
    for _ in range(255):
        assert isinstance(nested, list)
        nested = nested[0]
    assert nested == 0


def test_decoder_recursion_error_becomes_generic_bad_request(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    invoke, calls = route_harness

    _assert_rejected(invoke, calls, [_nested_query_body(1500)])


def test_oversized_multichunk_body_stops_reading_without_content_length(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    _, calls = route_harness
    chunks = [b"x" * _BODY_LIMIT, b"y", _SECRET_SENTINEL.encode()]
    request, delivered = _make_request(chunks, ())

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            provider_model_query.query_registered_provider_model(
                "fred",
                "FredSeries",
                request,
                current_principal=object(),
                registry=object(),
                ctx=None,
            )
        )

    error = captured.value
    assert error.status_code == 413
    assert error.detail == "Provider model query request is too large"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET_SENTINEL not in str(error.detail)
    assert calls == []
    assert delivered == chunks[:2]


def test_exact_body_limit_preserves_finite_json_values_and_large_integer(
    route_harness: tuple[RouteInvoker, list[dict[str, Any]]],
) -> None:
    invoke, calls = route_harness
    prefix = (
        b'{"query":{"series_id":"GDP","ratio":1.25,"nullable":null,'
        b'"enabled":true,"big":9007199254740993,"padding":"'
    )
    suffix = b'"}}'
    body = prefix + b"x" * (_BODY_LIMIT - len(prefix) - len(suffix)) + suffix

    response, delivered = invoke([body], ())

    assert len(body) == _BODY_LIMIT
    assert delivered == [body]
    assert response.success is True
    assert len(calls) == 1
    assert calls[0]["query"] == {
        "series_id": "GDP",
        "ratio": 1.25,
        "nullable": None,
        "enabled": True,
        "big": 9007199254740993,
        "padding": "x" * (_BODY_LIMIT - len(prefix) - len(suffix)),
    }


def test_cancellation_during_request_stream_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def receive() -> dict[str, Any]:
        raise asyncio.CancelledError

    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/providers/fred/models/FredSeries/query",
        "raw_path": b"/providers/fred/models/FredSeries/query",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    request = Request(scope, receive)

    async def should_not_run(**_kwargs: Any) -> dict[str, Any]:
        pytest.fail("cancelled request must not reach provider query execution")

    monkeypatch.setattr(provider_model_query, "query_provider_model", should_not_run)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            provider_model_query.query_registered_provider_model(
                "fred",
                "FredSeries",
                request,
                current_principal=object(),
                registry=object(),
                ctx=None,
            )
        )
