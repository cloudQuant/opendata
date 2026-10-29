"""Offline behavioral tests for bounded, identity-encoded Fuyao response reads."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import httpx
import pytest

import opendata.data.request_budget as request_budget_module
from opendata.data.providers.ths import FuyaoCredentials, FuyaoError, FuyaoHttpClient
from opendata.data.providers.ths.transport import http_client as http_client_module
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import Any

    from opendata.data.raw_response_cache import RawResponseCache

BASE_URL = "https://fuyao.test"
HOST = "fuyao.test"
ENVELOPE = b'{"code":0,"message":"ok","request_id":"synthetic","data":{"item":[]}}'


class ChunkStream(httpx.SyncByteStream):
    """Count raw chunks so overflow tests can prove the stream stops early."""

    def __init__(
        self,
        chunks: Sequence[bytes],
        *,
        fail_on: int | None = None,
        failure: type[httpx.RequestError] = httpx.ReadError,
        on_chunk: Callable[[int], None] | None = None,
        on_close: Callable[[], None] | None = None,
        close_failure: type[Exception] | None = None,
    ) -> None:
        self.chunks = tuple(chunks)
        self.fail_on = fail_on
        self.failure = failure
        self.on_chunk = on_chunk
        self.on_close = on_close
        self.close_failure = close_failure
        self.yielded = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self.chunks, start=1):
            if self.fail_on == index:
                request = httpx.Request("GET", f"{BASE_URL}/test")
                raise self.failure("synthetic stream failure", request=request)
            self.yielded += 1
            if self.on_chunk is not None:
                self.on_chunk(self.yielded)
            yield chunk

    def close(self) -> None:
        self.closed = True
        if self.on_close is not None:
            self.on_close()
        if self.close_failure is not None:
            raise self.close_failure("synthetic close failure")


class FakeClock:
    """Advance limiter cooldowns only when the client requests a sleep."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RecordingCache:
    """Keep cache interactions in memory and expose the bytes written."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.invalidations: list[dict[str, Any]] = []
        self.hit: Any = None

    def get(self, **kwargs: Any) -> Any:
        self.gets.append(dict(kwargs))
        return self.hit

    def put(self, **kwargs: Any) -> bool:
        self.puts.append(dict(kwargs))
        return True

    def invalidate(self, **kwargs: Any) -> None:
        self.invalidations.append(dict(kwargs))


class StubSourceAttemptGate:
    """Offline gate stub for the HTTP client's binding and reservation contract."""

    def __init__(self, result: object, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.attempts: list[TransportAttempt] = []

    def reserve_attempt(self, attempt: TransportAttempt) -> object:
        self.attempts.append(attempt)
        if self.error is not None:
            raise self.error
        return self.result


def _budget(*, task_attempts: int = 2, source_attempts: int = 2) -> RequestBudget:
    grant = RequestGrant(
        source="ths",
        canonical_model="fuyao.bounded-stream-fixture",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic test fixture only",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts={HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=(grant,),
    )


def _binding(*, principal_scope: str = "test-principal") -> SourceAttemptBinding:
    return SourceAttemptBinding(
        grant_id="test-grant",
        expected_revision=1,
        principal_scope=principal_scope,
        run_key="test-run",
    )


@contextmanager
def _maybe_scope(enabled: bool, budget: RequestBudget) -> Iterator[None]:
    if not enabled:
        yield
        return
    with request_execution_scope(
        source="ths",
        canonical_model="fuyao.bounded-stream-fixture",
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        yield


def _client(
    handler: Any,
    *,
    max_attempts: int = 2,
    max_response_bytes: int = 64,
    cache: Any | None = None,
    sleep_overshoot_seconds: float = 0.0,
) -> FuyaoHttpClient:
    clock = FakeClock()
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url=BASE_URL),
        mock_transport=httpx.MockTransport(handler),
        rate_limiter=FuyaoRateLimiter(
            rate_per_second=100.0,
            burst=20,
            clock=clock.monotonic,
        ),
        max_attempts=max_attempts,
        max_response_bytes=max_response_bytes,
        sleep=lambda seconds: clock.sleep(seconds + sleep_overshoot_seconds),
        raw_response_cache=cache,
    )


def test_constructor_rejects_invalid_retry_and_response_limits() -> None:
    credentials = FuyaoCredentials("synthetic-key", base_url=BASE_URL)
    with pytest.raises(ValueError, match="max_attempts"):
        FuyaoHttpClient(credentials=credentials, max_attempts=0)
    for invalid_cap in (0, True):
        with pytest.raises(ValueError, match="max_response_bytes"):
            FuyaoHttpClient(credentials=credentials, max_response_bytes=invalid_cap)


def test_constructor_rejects_ambiguous_or_unverified_transport_injection() -> None:
    credentials = FuyaoCredentials("synthetic-key", base_url=BASE_URL)
    injected_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    mock_transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    try:
        with pytest.raises(ValueError, match="mutually exclusive"):
            FuyaoHttpClient(
                credentials=credentials,
                client=injected_client,
                mock_transport=mock_transport,
            )
    finally:
        injected_client.close()

    with pytest.raises(ValueError, match="exact httpx.MockTransport"):
        FuyaoHttpClient(
            credentials=credentials,
            mock_transport=cast("httpx.MockTransport", object()),
        )
    with pytest.raises(TypeError, match="SourceAttemptGate"):
        FuyaoHttpClient(
            credentials=credentials,
            source_attempt_gate=cast("SourceAttemptGate", object()),
        )


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_success_streams_with_identity_and_preserves_exact_raw_cache_bytes(
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
) -> None:
    stream = ChunkStream([ENVELOPE[:9], ENVELOPE[9:]])
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, stream=stream, headers={"Content-Length": str(len(ENVELOPE))})

    cache = RecordingCache()
    client = _client(handler, max_response_bytes=len(ENVELOPE) + 1, cache=cache)
    send_options: list[dict[str, Any]] = []
    original_send = client._client.send

    def record_send(request: httpx.Request, **kwargs: Any) -> httpx.Response:
        send_options.append(dict(kwargs))
        return original_send(request, **kwargs)

    monkeypatch.setattr(client._client, "send", record_send)
    budget = _budget()
    try:
        with _maybe_scope(scoped, budget):
            result = client.get("/api/test")
    finally:
        client.close()

    assert result.raw_content == ENVELOPE
    assert result.envelope.request_id == "synthetic"
    assert len(requests_seen) == 1
    assert requests_seen[0].headers["accept-encoding"] == "identity"
    assert send_options == [{"follow_redirects": False, "stream": True}]
    assert stream.closed
    assert len(cache.puts) == 1
    assert cache.puts[0]["content"] == ENVELOPE
    assert budget.attempts_used == (1 if scoped else 0)


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_entity_at_exact_configured_cap_is_accepted(scoped: bool) -> None:
    stream = ChunkStream([ENVELOPE])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Length": str(len(ENVELOPE))},
        )

    client = _client(handler, max_attempts=1, max_response_bytes=len(ENVELOPE))
    budget = _budget(task_attempts=1, source_attempts=1)
    try:
        with _maybe_scope(scoped, budget):
            result = client.get("/api/test")
    finally:
        client.close()

    assert result.raw_content == ENVELOPE
    assert stream.closed
    assert budget.attempts_used == (1 if scoped else 0)


def test_source_attempt_gate_binds_one_send_and_projects_a_local_trace() -> None:
    reservation = AttemptReservation(
        status=ReservationStatus.RESERVED_NEW,
        reserved_at_us=123_456,
    )
    gate = StubSourceAttemptGate(reservation)
    stream = ChunkStream([ENVELOPE])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=1, max_response_bytes=len(ENVELOPE))
    client._source_attempt_gate = cast("SourceAttemptGate", gate)
    budget = _budget(task_attempts=1, source_attempts=1)
    binding = _binding()
    try:
        with request_execution_scope(
            source="ths",
            canonical_model="fuyao.bounded-stream-fixture",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
        ):
            result = client.get("/api/test")
    finally:
        client.close()

    assert len(calls) == 1
    assert len(gate.attempts) == 1
    attempt = gate.attempts[0]
    assert attempt.request_id
    assert attempt.attempt_no == 1
    assert attempt.grant_id == binding.grant_id
    assert attempt.expected_revision == binding.expected_revision
    assert attempt.source == "ths"
    assert attempt.canonical_model == "fuyao.bounded-stream-fixture"
    assert attempt.operation == RequestOperation.QUERY.value
    assert attempt.principal_scope == binding.principal_scope
    assert attempt.host == HOST
    assert attempt.run_key == binding.run_key
    assert result.attempt_trace[0].attempt_id == attempt.attempt_id
    assert result.attempt_trace[0].gate_status == ReservationStatus.RESERVED_NEW.value
    assert result.attempt_trace[0].request_budget_reserved is True
    assert result.attempt_trace[0].http_status == 200
    assert result.attempt_trace[0].outcome == "response_observed"
    assert budget.attempts_used == 1
    assert stream.closed


def test_source_attempt_binding_without_gate_fails_before_reserving_or_sending() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=ChunkStream([ENVELOPE]))

    client = _client(handler, max_attempts=1, max_response_bytes=len(ENVELOPE))
    budget = _budget(task_attempts=1, source_attempts=1)
    try:
        with (
            request_execution_scope(
                source="ths",
                canonical_model="fuyao.bounded-stream-fixture",
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=_binding(),
            ),
            pytest.raises(RequestAuthorizationError, match="configured durable gate"),
        ):
            client.get("/api/test")
    finally:
        client.close()

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("gate_result", "gate_error"),
    [
        (None, RuntimeError("synthetic gate detail")),
        (object(), None),
        (
            AttemptReservation(status=ReservationStatus.DENIED, denial_code="grant_not_allowed"),
            None,
        ),
    ],
)
def test_source_attempt_gate_denial_blocks_actual_send_without_leaking_details(
    gate_result: object,
    gate_error: Exception | None,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=ChunkStream([ENVELOPE]))

    client = _client(handler, max_attempts=2, max_response_bytes=len(ENVELOPE))
    gate = StubSourceAttemptGate(gate_result, error=gate_error)
    client._source_attempt_gate = cast("SourceAttemptGate", gate)
    budget = _budget(task_attempts=2, source_attempts=2)
    try:
        with (
            request_execution_scope(
                source="ths",
                canonical_model="fuyao.bounded-stream-fixture",
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=_binding(),
            ),
            pytest.raises(RequestAuthorizationError, match="did not authorize transport") as error,
        ):
            client.get("/api/test")
    finally:
        client.close()

    assert "synthetic gate detail" not in str(error.value)
    assert error.value.__cause__ is None
    assert calls == []
    assert budget.attempts_used == 1
    assert len(gate.attempts) == 1


@pytest.mark.parametrize("scope_failure", ["cancelled", "deadline"])
def test_scope_liveness_after_a_chunk_stops_before_the_next_chunk(
    monkeypatch: pytest.MonkeyPatch,
    scope_failure: str,
) -> None:
    cancellation = Event()
    original_monotonic = request_budget_module.time.monotonic
    current_time = {"value": original_monotonic()}
    deadline = current_time["value"] + 10.0

    if scope_failure == "deadline":
        monkeypatch.setattr(
            request_budget_module.time,
            "monotonic",
            lambda: current_time["value"],
        )

    def on_chunk(index: int) -> None:
        if index == 1:
            if scope_failure == "cancelled":
                cancellation.set()
            else:
                current_time["value"] = deadline

    stream = ChunkStream([b"first", b"must-not-be-read"], on_chunk=on_chunk)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=3, max_response_bytes=64)
    budget = _budget(task_attempts=3, source_attempts=3)
    expected_error = (
        RequestExecutionCancelledError
        if scope_failure == "cancelled"
        else RequestExecutionDeadlineError
    )
    try:
        with (
            request_execution_scope(
                source="ths",
                canonical_model="fuyao.bounded-stream-fixture",
                operation=RequestOperation.QUERY,
                budget=budget,
                deadline=deadline if scope_failure == "deadline" else None,
                cancellation=cancellation if scope_failure == "cancelled" else None,
            ),
            pytest.raises(expected_error),
        ):
            client.get("/api/test")
    finally:
        client.close()

    assert stream.yielded == 1
    assert stream.closed
    assert budget.attempts_used == 1


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
@pytest.mark.parametrize("encoding", ["utf-8", None], ids=["explicit-encoding", "default-encoding"])
def test_cache_hit_uses_identity_partition_and_skips_transport(
    scoped: bool,
    encoding: str | None,
) -> None:
    cache = RecordingCache()
    cache.hit = SimpleNamespace(status_code=200, content=ENVELOPE, encoding=encoding)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=ENVELOPE)

    client = _client(handler, max_response_bytes=len(ENVELOPE), cache=cache)
    budget = _budget()
    try:
        with _maybe_scope(scoped, budget):
            result = client.get("/api/test")
    finally:
        client.close()

    assert result.raw_content == ENVELOPE
    assert calls == []
    assert cache.gets[0]["headers"].get("accept-encoding") == "identity"
    assert cache.puts == []
    assert budget.attempts_used == 0


def test_cache_bypasses_unverifiable_environment_routing() -> None:
    cache = RecordingCache()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=ChunkStream([ENVELOPE]))

    raw_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=True)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url=BASE_URL),
        client=raw_client,
        max_response_bytes=len(ENVELOPE),
        raw_response_cache=cast("RawResponseCache", cache),
    )
    try:
        result = client.get("/api/test")
    finally:
        raw_client.close()

    assert result.raw_content == ENVELOPE
    assert len(calls) == 1
    assert cache.gets == []
    assert cache.puts == []


def test_cache_identity_preparation_failure_does_not_prevent_real_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = RecordingCache()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=ChunkStream([ENVELOPE]))

    raw_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url=BASE_URL),
        client=raw_client,
        max_response_bytes=len(ENVELOPE),
        raw_response_cache=cast("RawResponseCache", cache),
    )
    original_build_request = raw_client.build_request
    preparation_attempts = 0

    def fail_cache_preparation_once(*args: Any, **kwargs: Any) -> httpx.Request:
        nonlocal preparation_attempts
        preparation_attempts += 1
        if preparation_attempts == 1:
            raise ValueError("synthetic cache identity preparation error")
        return original_build_request(*args, **kwargs)

    monkeypatch.setattr(raw_client, "build_request", fail_cache_preparation_once)
    try:
        result = client.get("/api/test")
    finally:
        raw_client.close()

    assert result.raw_content == ENVELOPE
    assert len(calls) == 1
    assert preparation_attempts == 2
    assert cache.gets == []
    assert cache.puts == []


def test_invalid_cache_entry_is_invalidated_before_a_bounded_transport_fetch() -> None:
    cache = RecordingCache()
    cache.hit = SimpleNamespace(status_code=200, content=b"not-json", encoding=None)
    calls: list[httpx.Request] = []
    stream = ChunkStream([ENVELOPE])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_response_bytes=len(ENVELOPE), cache=cache)
    try:
        result = client.get("/api/test")
    finally:
        client.close()

    assert result.raw_content == ENVELOPE
    assert len(cache.invalidations) == 1
    assert cache.invalidations[0]["endpoint"].endswith("/api/test")
    assert len(cache.puts) == 1
    assert cache.puts[0]["content"] == ENVELOPE
    assert len(calls) == 1
    assert stream.closed


def test_response_close_failure_is_sanitized_and_not_retried() -> None:
    stream = ChunkStream([ENVELOPE], close_failure=ValueError)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=3, max_response_bytes=len(ENVELOPE))
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "network"
    assert error.value.retryable is False
    assert "synthetic close failure" not in str(error.value)
    assert calls and len(calls) == 1
    assert stream.closed


def test_close_failure_does_not_replace_an_earlier_read_timeout() -> None:
    stream = ChunkStream(
        [b"prefix"],
        fail_on=1,
        failure=httpx.ReadTimeout,
        close_failure=ValueError,
    )
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=3, max_response_bytes=64)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "timeout"
    assert error.value.retryable is False
    assert "synthetic close failure" not in str(error.value)
    assert "synthetic stream failure" not in str(error.value)
    assert len(calls) == 1
    assert stream.closed


def test_non_success_close_failure_is_sanitized_and_not_retried() -> None:
    stream = ChunkStream([], close_failure=ValueError)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404, stream=stream)

    client = _client(handler, max_attempts=3)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "network"
    assert error.value.retryable is False
    assert "synthetic close failure" not in str(error.value)
    assert len(calls) == 1
    assert stream.closed


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
@pytest.mark.parametrize("outcome", ["success", "overflow", "read_error"])
def test_host_slots_cover_send_body_read_and_close(
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
    outcome: str,
) -> None:
    active: set[str] = set()
    events: list[str] = []
    expected_slots = {"shared", "scope"} if scoped else {"shared"}
    calls: list[httpx.Request] = []

    @contextmanager
    def watched_slot(name: str) -> Iterator[None]:
        active.add(name)
        events.append(f"enter-{name}")
        try:
            yield
        finally:
            events.append(f"exit-{name}")
            active.remove(name)

    def observe(stage: str) -> None:
        assert active == expected_slots
        events.append(stage)

    if outcome == "success":
        chunks = [ENVELOPE]
        fail_on = None
        cap = 128
    elif outcome == "overflow":
        chunks = [b"1234", b"56", b"unread"]
        fail_on = None
        cap = 5
    else:
        chunks = [b"prefix", b"unread"]
        fail_on = 2
        cap = 64
    stream = ChunkStream(
        chunks,
        fail_on=fail_on,
        on_chunk=lambda _index: observe("read"),
        on_close=lambda: observe("close"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        observe("send")
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=2, max_response_bytes=cap)
    if scoped:
        monkeypatch.setattr(
            client,
            "_scoped_shared_host_request",
            lambda *_args: watched_slot("shared"),
        )
        monkeypatch.setattr(
            http_client_module,
            "scoped_host_slot",
            lambda *_args: watched_slot("scope"),
        )
    else:
        monkeypatch.setattr(
            http_client_module,
            "shared_host_request",
            lambda _host: watched_slot("shared"),
        )

    budget = _budget(task_attempts=2, source_attempts=2)
    try:
        with _maybe_scope(scoped, budget):
            if outcome == "success":
                result = client.get("/api/test")
                assert result.raw_content == ENVELOPE
            else:
                with pytest.raises(FuyaoError) as error:
                    client.get("/api/test")
                assert error.value.category == (
                    "response_too_large" if outcome == "overflow" else "network"
                )
                assert error.value.retryable is False
    finally:
        client.close()

    assert len(calls) == 1
    assert stream.closed
    assert budget.attempts_used == (1 if scoped else 0)
    assert events.index("send") < events.index("read") < events.index("close")
    for slot in expected_slots:
        assert events.index("close") < events.index(f"exit-{slot}")
    assert not active


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_overflow_reads_only_to_the_first_excess_chunk_and_never_retries(scoped: bool) -> None:
    stream = ChunkStream([b"1234", b"56", b"must-not-be-read"])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=3, max_response_bytes=5)
    budget = _budget(task_attempts=3, source_attempts=3)
    try:
        with _maybe_scope(scoped, budget), pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "response_too_large"
    assert error.value.retryable is False
    assert len(calls) == 1
    assert stream.yielded == 2
    assert stream.closed
    assert budget.attempts_used == (1 if scoped else 0)


@pytest.mark.parametrize(
    ("headers", "expected_category"),
    [
        ([("Content-Length", "4"), ("content-length", "4")], "envelope_invalid"),
        ([("Content-Length", "4, 4")], "envelope_invalid"),
        ([("Content-Length", "-1")], "envelope_invalid"),
        ([("Content-Length", "1e2")], "envelope_invalid"),
        ([("Content-Length", "65")], "response_too_large"),
        ([("Content-Length", "4"), ("Transfer-Encoding", "chunked")], "envelope_invalid"),
        ([("Transfer-Encoding", "gzip, chunked")], "envelope_invalid"),
        ([("Transfer-Encoding", "identity")], "envelope_invalid"),
        (
            [("Transfer-Encoding", "chunked"), ("transfer-encoding", "chunked")],
            "envelope_invalid",
        ),
        ([("Content-Encoding", "gzip")], "envelope_invalid"),
        ([("Content-Encoding", "identity, gzip")], "envelope_invalid"),
        ([("Content-Encoding", "identity"), ("Content-Encoding", "identity")], "envelope_invalid"),
    ],
)
def test_invalid_response_headers_fail_closed_without_reading(
    headers: list[tuple[str, str]],
    expected_category: str,
) -> None:
    stream = ChunkStream([ENVELOPE])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers=headers)

    client = _client(handler, max_attempts=3, max_response_bytes=64)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == expected_category
    assert error.value.retryable is False
    assert stream.yielded == 0
    assert stream.closed


def test_single_chunked_transfer_encoding_without_content_length_is_accepted() -> None:
    stream = ChunkStream([ENVELOPE])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"Transfer-Encoding": "chunked"},
        )

    client = _client(handler, max_response_bytes=len(ENVELOPE) + 1)
    try:
        result = client.get("/api/test")
    finally:
        client.close()

    assert result.raw_content == ENVELOPE
    assert stream.closed


def test_content_length_larger_than_actual_entity_is_rejected_after_eof() -> None:
    stream = ChunkStream([b"short"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"Content-Length": "6"})

    client = _client(handler, max_response_bytes=64)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "envelope_invalid"
    assert error.value.retryable is False
    assert stream.closed


def test_content_length_shorter_than_actual_entity_is_rejected_after_eof() -> None:
    stream = ChunkStream([ENVELOPE])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Length": str(len(ENVELOPE) - 1)},
        )

    client = _client(handler, max_response_bytes=len(ENVELOPE) + 1)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "envelope_invalid"
    assert error.value.retryable is False
    assert stream.closed


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (httpx.ReadError, "network"),
        (httpx.ReadTimeout, "timeout"),
    ],
)
def test_stream_read_failures_are_outcome_unknown_and_not_retried(
    failure: type[httpx.RequestError],
    category: str,
) -> None:
    stream = ChunkStream([b"prefix", b"tail"], fail_on=2, failure=failure)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    client = _client(handler, max_attempts=3, max_response_bytes=64)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == category
    assert error.value.retryable is False
    assert len(calls) == 1
    assert stream.closed


def test_429_retry_after_is_preserved_and_each_scoped_send_uses_budget() -> None:
    budget = _budget(task_attempts=2, source_attempts=2)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, stream=ChunkStream([]))
        return httpx.Response(200, stream=ChunkStream([ENVELOPE]))

    client = _client(
        handler,
        max_attempts=2,
        max_response_bytes=len(ENVELOPE),
        sleep_overshoot_seconds=1e-6,
    )
    try:
        with _maybe_scope(True, budget):
            result = client.get("/api/test")
    finally:
        client.close()

    assert result.envelope.request_id == "synthetic"
    assert len(calls) == 2
    assert all(request.headers["accept-encoding"] == "identity" for request in calls)
    assert budget.attempts_used == 2


def test_unscoped_response_hooks_are_rejected_before_any_send() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=ENVELOPE)

    client = _client(handler)

    def eager_read(response: httpx.Response) -> httpx.Response:
        response.read()
        return response

    client._client.event_hooks["response"].append(eager_read)
    try:
        with pytest.raises(RequestAuthorizationError, match="response hooks"):
            client.get("/api/test")
    finally:
        client.close()

    assert calls == []


@pytest.mark.parametrize("feature", ["auth", "mounts"])
def test_unscoped_auth_and_mounts_are_rejected_before_any_send(feature: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=ENVELOPE)

    raw_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        mounts=(
            {"https://fuyao.test": httpx.MockTransport(handler)} if feature == "mounts" else None
        ),
        auth=httpx.BasicAuth("synthetic-user", "synthetic-password") if feature == "auth" else None,
        trust_env=False,
    )
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url=BASE_URL),
        client=raw_client,
        max_attempts=3,
    )
    try:
        with pytest.raises(RequestAuthorizationError):
            client.get("/api/test")
    finally:
        raw_client.close()

    assert calls == []


def test_redirect_body_is_not_read_or_followed_and_is_closed() -> None:
    stream = ChunkStream([b"must-not-be-read"])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://other.example.test/target"},
            stream=stream,
        )

    client = _client(handler, max_attempts=3)
    try:
        with pytest.raises(FuyaoError) as error:
            client.get("/api/test")
    finally:
        client.close()

    assert error.value.category == "http"
    assert error.value.retryable is False
    assert len(calls) == 1
    assert stream.yielded == 0
    assert stream.closed
