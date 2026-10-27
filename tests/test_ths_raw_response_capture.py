"""Offline tests for the successful Fuyao raw-response transport surface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.envelope import parse_envelope
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import (
    FuyaoHttpClient,
    FuyaoHttpResponse,
)
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.raw_response_cache import RawResponseCache
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestExecutionScope,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
)

_HOST = "fuyao.test"
_SUCCESS_BODY = (
    b'{"code":0,"message":"success","request_id":"fixture","data":{"item":[{"value":"001.2300"}]}}'
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    raw_response_cache: RawResponseCache | None = None,
    source_attempt_gate: SourceAttemptGate | None = None,
    max_response_bytes: int = 32 * 1024 * 1024,
) -> FuyaoHttpClient:
    """Build an offline client with deterministic rate and retry settings."""
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-fixture-key", base_url=f"https://{_HOST}"),
        mock_transport=httpx.MockTransport(handler),
        rate_limiter=FuyaoRateLimiter(rate_per_second=1_000.0, burst=1_000),
        raw_response_cache=raw_response_cache,
        source_attempt_gate=source_attempt_gate,
        max_attempts=1,
        max_response_bytes=max_response_bytes,
        backoff_base_seconds=0,
        backoff_jitter_seconds=0,
        sleep=lambda _seconds: None,
    )


def test_legacy_response_constructor_keeps_three_positional_arguments() -> None:
    """Old constructor and equality semantics ignore the new raw response field."""
    envelope = parse_envelope({"code": 0, "data": {"item": []}})

    result = FuyaoHttpResponse(200, envelope, 0.25)
    with_raw_body = FuyaoHttpResponse(200, envelope, 0.25, b"different raw body")

    assert result.status_code == 200
    assert result.envelope is envelope
    assert result.elapsed_seconds == 0.25
    assert result.raw_content == b""
    assert with_raw_body == result


def test_raw_response_body_is_excluded_from_repr() -> None:
    """Response representations never include sensitive source body values."""
    envelope = parse_envelope({"code": 0, "data": {"item": []}})
    sentinel = b"sensitive-raw-response-sentinel"

    result = FuyaoHttpResponse(200, envelope, 0.25, sentinel)

    assert "sensitive-raw-response-sentinel" not in repr(result)


def test_successful_response_preserves_raw_body_within_existing_limit() -> None:
    """A successful result retains the exact body already parsed by the transport."""
    client = _client(lambda _request: httpx.Response(200, content=_SUCCESS_BODY))
    try:
        result = client.get("/api/a-share/financials/income-statements")
    finally:
        client.close()

    assert result.raw_content == _SUCCESS_BODY
    assert result.envelope.items == ({"value": "001.2300"},)


def test_cache_hit_restores_the_same_raw_body(tmp_path: Path) -> None:
    """Both the network fill and cache hit return the originally captured bytes."""
    sends: list[httpx.Request] = []
    cache = RawResponseCache(tmp_path / "cache", ttl_seconds=60)
    client = _client(
        lambda request: sends.append(request) or httpx.Response(200, content=_SUCCESS_BODY),
        raw_response_cache=cache,
    )
    try:
        first = client.get("/api/a-share/financials/income-statements")
        second = client.get("/api/a-share/financials/income-statements")
    finally:
        client.close()

    assert first.raw_content == _SUCCESS_BODY
    assert second.raw_content == _SUCCESS_BODY
    assert second.envelope == first.envelope
    assert len(sends) == 1


def test_gate_path_captures_body_and_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gated transport captures bytes while leaving its raw cache untouched."""
    cache = RawResponseCache(tmp_path / "gated-cache", ttl_seconds=60)
    gate = SourceAttemptGate.__new__(SourceAttemptGate)
    monkeypatch.setattr(
        SourceAttemptGate,
        "reserve_attempt",
        lambda _gate, _attempt: AttemptReservation(ReservationStatus.RESERVED_NEW),
    )
    grant = RequestGrant(
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic-offline-test-fixture",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={_HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    budget = RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))
    binding = SourceAttemptBinding(
        grant_id="fixture-gate",
        expected_revision=1,
        principal_scope="fixture-worker",
        run_key="fixture-run",
    )
    client = _client(
        lambda _request: httpx.Response(200, content=_SUCCESS_BODY),
        raw_response_cache=cache,
        source_attempt_gate=gate,
    )
    try:
        with request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
        ) as scope:
            assert isinstance(scope, RequestExecutionScope)
            result = client.get("/api/a-share/financials/income-statements")
    finally:
        client.close()

    assert result.raw_content == _SUCCESS_BODY
    assert not cache.raw_dir.exists()


def test_empty_success_body_retains_existing_invalid_envelope_failure() -> None:
    """An empty HTTP 200 body remains an invalid envelope and returns no response."""
    client = _client(lambda _request: httpx.Response(200, content=b""))
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/a-share/financials/income-statements")
    finally:
        client.close()

    assert error.value.category == "envelope_invalid"


def test_oversized_body_is_rejected_before_raw_content_can_escape() -> None:
    """The existing response byte ceiling also bounds the new response field."""
    client = _client(
        lambda _request: httpx.Response(200, content=_SUCCESS_BODY),
        max_response_bytes=len(_SUCCESS_BODY) - 1,
    )
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/a-share/financials/income-statements")
    finally:
        client.close()

    assert error.value.category == "response_too_large"
