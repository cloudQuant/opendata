"""Synthetic tests for the opt-in one-shot Fuyao raw HTTP exchange."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from opendata.data.providers.ths import (
    FuyaoCredentials,
    FuyaoError,
    FuyaoHttpClient,
    FuyaoRateLimiter,
)
from opendata.data.providers.ths.transport import http_client as http_client_module
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
)

LOGGER_NAME = FuyaoHttpClient.__module__

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class _TrackingStream(httpx.SyncByteStream):
    """Synthetic stream that records reads and response cleanup."""

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        close_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._chunks = tuple(chunks)
        self._close_error = close_error
        self._read_error = read_error
        self.chunks_read = 0
        self.close_calls = 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk
        if self._read_error is not None:
            raise self._read_error

    def close(self) -> None:
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _NoHitCache:
    """An explicit, offline cache override for regression tests of ``get``."""

    def get(self, **_kwargs):
        return None

    def put(self, **_kwargs):
        pass

    def invalidate(self, **_kwargs):
        pass


def _client(
    handler,
    *,
    base_url: str = "https://fuyao.test",
    max_response_bytes: int = 1024,
    raw_response_cache=None,
    rate_limiter: FuyaoRateLimiter | None = None,
):
    """Build an owned, offline-only client for one synthetic exchange."""
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-api-key", base_url=base_url),
        mock_transport=httpx.MockTransport(handler),
        rate_limiter=rate_limiter or FuyaoRateLimiter(rate_per_second=100.0, burst=100),
        max_attempts=5,
        max_response_bytes=max_response_bytes,
        raw_response_cache=raw_response_cache,
    )


def _enable_synthetic_scope(monkeypatch, client: FuyaoHttpClient) -> None:
    """Supply a fake trusted scope without invoking real grants or storage."""
    scope = object()
    monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: (scope,))
    monkeypatch.setattr(http_client_module, "check_scope_liveness", lambda _scopes: None)
    monkeypatch.setattr(http_client_module, "authorize_request", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "scoped_host_slot", lambda *_args: nullcontext())
    monkeypatch.setattr(client, "_validate_source_attempt_mode", lambda _scopes: None)
    monkeypatch.setattr(client, "_scoped_timeout", lambda _scopes: client._timeout_seconds)
    monkeypatch.setattr(client, "_scoped_shared_host_request", lambda *_args: nullcontext())


class TestFuyaoRawExchange:
    def test_returns_complete_non_2xx_entity_without_exposing_headers(self, caplog, monkeypatch):
        body = b"synthetic response payload"
        observed_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            observed_headers.update(dict(request.headers))
            return httpx.Response(
                503,
                headers={"X-Private-Response": "header-secret"},
                stream=httpx.ByteStream(body),
            )

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler) as client,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange(
                "/v1/synthetic", params={"symbol": "TEST", "api_token": "query-secret"}
            )

        assert observed_headers["x-api-key"] == "synthetic-api-key"
        assert result.status_code == 503
        assert result.body == body
        assert result.elapsed_seconds >= 0.0
        assert not hasattr(result, "headers")
        assert body.decode() not in repr(result)
        log_text = "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )
        for secret in (
            "synthetic-api-key",
            "header-secret",
            "query-secret",
            body.decode(),
        ):
            assert secret not in log_text

    def test_429_returns_raw_body_records_cooldown_and_does_not_retry(self, monkeypatch):
        now = {"value": 10.0}
        limiter = FuyaoRateLimiter(
            rate_per_second=10.0,
            burst=10,
            clock=lambda: now["value"],
        )
        calls = 0
        body = b"synthetic rate limit body"

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                stream=httpx.ByteStream(body),
            )

        with _client(handler, rate_limiter=limiter) as client:
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/rate-limited")

        assert calls == 1
        assert (result.status_code, result.body) == (429, body)
        assert limiter.cooldown_remaining == pytest.approx(7.0)

    @pytest.mark.parametrize("retry_after", ["invalid", "0", "-3"])
    def test_429_invalid_retry_after_uses_default_cooldown(self, monkeypatch, retry_after):
        now = {"value": 10.0}
        limiter = FuyaoRateLimiter(
            rate_per_second=10.0,
            burst=10,
            clock=lambda: now["value"],
        )
        calls = 0
        body = b"synthetic rate limit body"

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                stream=httpx.ByteStream(body),
            )

        with _client(handler, rate_limiter=limiter) as client:
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/rate-limited")

        assert calls == 1
        assert (result.status_code, result.body) == (429, body)
        assert limiter.cooldown_remaining == pytest.approx(1.0)

    def test_preserves_reflected_api_key_without_logging_or_repr(self, caplog, monkeypatch):
        body = b'{"echo":"synthetic-api-key"}'

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(lambda _request: httpx.Response(200, stream=httpx.ByteStream(body))) as client,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/reflected-key")

        assert result.body == body
        assert "synthetic-api-key" not in repr(result)
        assert "synthetic-api-key" not in "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )

    def test_redirect_is_returned_without_following_its_location(self, monkeypatch):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "https://other-host.invalid/redirect-target"},
                stream=httpx.ByteStream(b"redirect body"),
            )

        with _client(handler) as client:
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/redirect")

        assert len(calls) == 1
        assert calls[0] == "https://fuyao.test/v1/redirect"
        assert (result.status_code, result.body) == (302, b"redirect body")

    def test_response_larger_than_cap_fails_without_reading_later_chunks(self, monkeypatch):
        calls = 0
        oversized = b"private-body-too-large"
        stream = _TrackingStream([oversized, b"must-not-read"])

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(404, stream=stream)

        with (
            _client(handler, max_response_bytes=8) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/oversized")

        assert calls == 1
        assert stream.chunks_read == 1
        assert stream.close_calls == 1
        assert getattr(exc.value, "category", None) == "response_too_large"
        assert oversized.decode() not in str(exc.value)
        assert oversized.decode() not in repr(exc.value)

    def test_exact_body_cap_is_returned_and_stream_is_closed(self, monkeypatch):
        body = b"12345678"
        stream = _TrackingStream([body[:4], body[4:]])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                headers={"Content-Length": str(len(body))},
                stream=stream,
            )

        with _client(handler, max_response_bytes=len(body)) as client:
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/exact-cap")

        assert (result.status_code, result.body) == (206, body)
        assert stream.chunks_read == 2
        assert stream.close_calls == 1

    @pytest.mark.parametrize(
        ("headers", "category"),
        [
            ({"Content-Length": "not-a-length"}, "envelope_invalid"),
            ({"Content-Length": "9"}, "response_too_large"),
            ({"Content-Encoding": "gzip"}, "envelope_invalid"),
            ({"Transfer-Encoding": "gzip"}, "envelope_invalid"),
            (
                {"Content-Length": "1", "Transfer-Encoding": "chunked"},
                "envelope_invalid",
            ),
        ],
    )
    def test_invalid_body_metadata_fails_before_reading_or_returning_body(
        self, monkeypatch, headers, category
    ):
        stream = _TrackingStream([b"synthetic body"])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, headers=headers, stream=stream)

        with (
            _client(handler, max_response_bytes=8) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/invalid-metadata")

        assert exc.value.category == category
        assert stream.chunks_read == 0
        assert stream.close_calls == 1

    def test_content_length_mismatch_fails_without_returning_body(self, monkeypatch):
        body = b"abc"
        stream = _TrackingStream([body])

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                headers={"Content-Length": "2"},
                stream=stream,
            )

        with (
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/content-length-mismatch")

        assert exc.value.category == "envelope_invalid"
        assert body.decode() not in str(exc.value)
        assert stream.chunks_read == 1
        assert stream.close_calls == 1

    def test_response_close_failure_is_sanitized_and_returns_no_body(self, monkeypatch):
        private_detail = "synthetic-api-key-and-body"
        stream = _TrackingStream([b"synthetic body"], close_error=RuntimeError(private_detail))

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=stream)

        with (
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/close-error")

        assert exc.value.category == "network"
        assert private_detail not in str(exc.value)
        assert private_detail not in repr(exc.value)
        assert stream.close_calls == 1

    def test_stream_and_close_failures_do_not_leak_or_return_partial_body(
        self, caplog, monkeypatch
    ):
        read_detail = "synthetic-key-and-partial-body"
        close_detail = "synthetic-key-and-close-body"
        stream = _TrackingStream(
            [b"partial response"],
            close_error=RuntimeError(close_detail),
            read_error=RuntimeError(read_detail),
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, stream=stream)

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/read-and-close-error")

        assert exc.value.category == "network"
        assert stream.chunks_read == 1
        assert stream.close_calls == 1
        log_text = "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )
        for private_detail in (read_detail, close_detail, "partial response"):
            assert private_detail not in str(exc.value)
            assert private_detail not in repr(exc.value)
            assert private_detail not in log_text

    def test_timeout_is_one_attempt_and_does_not_return_or_log_exception_body(
        self, caplog, monkeypatch
    ):
        calls = 0
        failure_detail = "synthetic-key and private-response-body"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout(failure_detail, request=request)

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/timeout")

        assert calls == 1
        assert getattr(exc.value, "category", None) == "timeout"
        assert failure_detail not in str(exc.value)
        assert failure_detail not in repr(exc.value)
        assert failure_detail not in "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )

    def test_raw_exchange_does_not_consult_or_write_the_optional_cache(self, monkeypatch):
        calls: list[str] = []

        class ForbiddenCache:
            def get(self, **_kwargs):
                calls.append("get")
                raise AssertionError("raw exchange must bypass cache")

            def put(self, **_kwargs):
                calls.append("put")
                raise AssertionError("raw exchange must bypass cache")

        with _client(
            lambda _request: httpx.Response(418, stream=httpx.ByteStream(b"teapot")),
            raw_response_cache=ForbiddenCache(),
        ) as client:
            _enable_synthetic_scope(monkeypatch, client)
            result = client.get_raw_exchange("/v1/cache-bypass")

        assert (result.status_code, result.body) == (418, b"teapot")
        assert calls == []

    def test_requires_scope_before_limiter_or_transport(self, monkeypatch):
        calls = {"limiter": 0, "transport": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, content=b"{}")

        with _client(handler) as client:
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )
            with pytest.raises(RequestAuthorizationError, match="active request scope"):
                client.get_raw_exchange("/v1/no-scope")

        assert calls == {"limiter": 0, "transport": 0}

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://fuyao.test",
            "https://fuyao.test:444",
            "https://user@fuyao.test",
            "https://fuyao.test:invalid",
        ],
    )
    def test_rejects_non_exact_https_host_before_limiter(self, monkeypatch, base_url):
        calls = {"limiter": 0, "transport": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with _client(handler, base_url=base_url) as client:
            _enable_synthetic_scope(monkeypatch, client)
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )

            with pytest.raises(RequestAuthorizationError):
                client.get_raw_exchange("/v1/unapproved-host")

        assert calls == {"limiter": 0, "transport": 0}

    @pytest.mark.parametrize(
        "invalid_client_state", ["closed", "auth", "redirect", "trust_env", "hook"]
    )
    def test_rejects_unverified_scoped_client_state_before_limiter(
        self, monkeypatch, invalid_client_state
    ):
        calls = {"limiter": 0, "transport": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with _client(handler) as client:
            _enable_synthetic_scope(monkeypatch, client)
            if invalid_client_state == "closed":
                client._client.close()
            elif invalid_client_state == "auth":
                client._client.auth = httpx.BasicAuth("synthetic-user", "synthetic-password")
            elif invalid_client_state == "redirect":
                client._client.follow_redirects = True
            elif invalid_client_state == "trust_env":
                # httpx exposes ``trust_env`` as a read-only property.
                client._client._trust_env = True
            else:
                client._client.event_hooks["response"].append(lambda _response: None)
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )

            with pytest.raises(RequestAuthorizationError):
                client.get_raw_exchange("/v1/unverified-client")

        assert calls == {"limiter": 0, "transport": 0}

    def test_raw_exchange_rejects_opaque_transport_before_limiter(self, monkeypatch):
        calls = {"limiter": 0, "transport": 0}

        class OpaqueTransport(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                calls["transport"] += 1
                raise AssertionError("unverified transport must not send")

            def close(self) -> None:
                pass

        with _client(lambda _request: pytest.fail("opaque transport must not send")) as client:
            _enable_synthetic_scope(monkeypatch, client)
            opaque_transport = OpaqueTransport()
            client._client._transport = opaque_transport
            client._scoped_transport = opaque_transport
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )

            with pytest.raises(RequestAuthorizationError, match="transport is not verified"):
                client.get_raw_exchange("/v1/unverified-transport")

        assert calls == {"limiter": 0, "transport": 0}

    @pytest.mark.parametrize("category", ["unknown", "auth"])
    def test_sanitizes_an_untrusted_fuyao_exception(self, caplog, monkeypatch, category):
        private_detail = "private-body-and-api-key"

        def handler(_request: httpx.Request) -> httpx.Response:
            raise FuyaoError(
                private_detail,
                category=category,
                message=private_detail,
                advice=private_detail,
            )

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/untrusted-error")

        assert exc.value.category == category
        assert private_detail not in str(exc.value)
        assert private_detail not in repr(exc.value)
        assert private_detail not in "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )

    @pytest.mark.parametrize("exception_type", [httpx.NetworkError, RuntimeError])
    def test_transport_and_unexpected_errors_are_sanitized_without_retry(
        self, caplog, monkeypatch, exception_type
    ):
        calls = 0
        private_detail = "synthetic-key-and-response-body"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise (
                exception_type(private_detail, request=request)
                if issubclass(exception_type, httpx.HTTPError)
                else exception_type(private_detail)
            )

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get_raw_exchange("/v1/transport-error")

        assert calls == 1
        assert exc.value.category == "network"
        assert private_detail not in str(exc.value)
        assert private_detail not in repr(exc.value)
        assert private_detail not in "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )

    def test_scoped_exchange_reserves_one_budget_and_durable_attempt(self, monkeypatch):
        counts = {"authorize": 0, "liveness": 0, "budget": 0, "gate": 0, "limiter": 0}
        scope = SimpleNamespace(
            source="ths",
            canonical_model="fuyao.synthetic",
            operation=RequestOperation.QUERY,
            source_attempt_binding=SourceAttemptBinding(
                grant_id="synthetic-grant",
                expected_revision=1,
                principal_scope="synthetic-principal",
                run_key="synthetic-run",
            ),
        )
        gate_attempts = []

        class RecordingGate:
            def reserve_attempt(self, attempt):
                counts["gate"] += 1
                gate_attempts.append(attempt)
                return AttemptReservation(
                    status=ReservationStatus.RESERVED_NEW,
                    reserved_at_us=123,
                )

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with _client(handler) as client:
            client._source_attempt_gate = RecordingGate()
            client._validate_source_attempt_mode = lambda _scopes: None
            client._scoped_timeout = lambda _scopes: 3.0
            client._scoped_shared_host_request = lambda *_args: nullcontext()

            def count_authorization(*_args) -> None:
                counts["authorize"] += 1

            def count_liveness(_scopes) -> None:
                counts["liveness"] += 1

            def count_budget(*_args) -> None:
                counts["budget"] += 1

            def acquire_limiter(_sleep) -> None:
                counts["limiter"] += 1

            monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: (scope,))
            monkeypatch.setattr(http_client_module, "authorize_request", count_authorization)
            monkeypatch.setattr(http_client_module, "check_scope_liveness", count_liveness)
            monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", count_budget)
            monkeypatch.setattr(
                http_client_module, "scoped_host_slot", lambda *_args: nullcontext()
            )
            monkeypatch.setattr(client._limiter, "wait_and_acquire", acquire_limiter)

            result = client.get_raw_exchange("/v1/scoped")

        assert counts["limiter"] == 1
        assert counts["budget"] == 1
        assert counts["gate"] == 1
        assert counts["authorize"] >= 1
        assert counts["liveness"] >= 1
        assert len(gate_attempts) == 1
        assert gate_attempts[0].source == "ths"
        assert gate_attempts[0].canonical_model == "fuyao.synthetic"
        assert gate_attempts[0].host == "fuyao.test"
        assert len(result.attempt_trace) == 1
        assert result.attempt_trace[0].attempt_no == 1
        assert result.attempt_trace[0].http_status == 200

    def test_scope_cancellation_while_waiting_for_client_lock_prevents_send(self, monkeypatch):
        calls = {"liveness": 0, "limiter": 0, "transport": 0}
        scope = object()

        class ContendedLock:
            def acquire(self, *, timeout: float) -> bool:
                assert timeout == pytest.approx(0.05)
                return False

            def release(self) -> None:
                pytest.fail("lock was not acquired and must not be released")

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with (
            _client(handler) as client,
            pytest.raises(RequestExecutionCancelledError),
        ):
            client._request_lock = ContendedLock()
            monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: (scope,))

            def cancel(_scopes) -> None:
                calls["liveness"] += 1
                raise RequestExecutionCancelledError()

            monkeypatch.setattr(http_client_module, "check_scope_liveness", cancel)
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )
            monkeypatch.setattr(client, "_validate_source_attempt_mode", lambda _scopes: None)
            client.get_raw_exchange("/v1/cancelled-lock-wait")

        assert calls == {"liveness": 1, "limiter": 0, "transport": 0}

    def test_legacy_get_scope_cancellation_while_waiting_for_lock(self, monkeypatch):
        calls = {"liveness": 0, "transport": 0}
        scope = SimpleNamespace(source_attempt_binding=None)

        class ContendedLock:
            def acquire(self, *, timeout: float) -> bool:
                assert timeout == pytest.approx(0.05)
                return False

            def release(self) -> None:
                pytest.fail("lock was not acquired and must not be released")

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with (
            _client(handler, raw_response_cache=_NoHitCache()) as client,
            pytest.raises(RequestExecutionCancelledError),
        ):
            client._request_lock = ContendedLock()
            monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: (scope,))

            def cancel(_scopes) -> None:
                calls["liveness"] += 1
                raise RequestExecutionCancelledError()

            monkeypatch.setattr(http_client_module, "check_scope_liveness", cancel)
            client.get("/v1/legacy-cancelled-lock-wait")

        assert calls == {"liveness": 1, "transport": 0}

    def test_request_budget_denial_is_propagated_without_transport(self, monkeypatch):
        calls = 0
        budget_error = RequestAttemptLimitError("synthetic budget exhausted")

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        def deny_budget(*_args) -> None:
            raise budget_error

        with (
            _client(handler) as client,
            pytest.raises(RequestAttemptLimitError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)
            monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", deny_budget)
            client.get_raw_exchange("/v1/budget-denied")

        assert exc.value is budget_error
        assert calls == 0

    @pytest.mark.parametrize("invalid_field", ["source", "model", "operation", "binding"])
    def test_durable_gate_rejects_incomplete_scope_before_limiter(self, monkeypatch, invalid_field):
        calls = {"limiter": 0, "transport": 0}
        binding = SourceAttemptBinding(
            grant_id="synthetic-grant",
            expected_revision=1,
            principal_scope="synthetic-principal",
            run_key="synthetic-run",
        )
        scope_fields = {
            "source": "ths",
            "canonical_model": "fuyao.synthetic",
            "operation": RequestOperation.QUERY,
            "source_attempt_binding": binding,
        }
        invalid_values = {
            "source": "other",
            "model": " ",
            "operation": "query",
            "binding": None,
        }
        if invalid_field == "model":
            scope_fields["canonical_model"] = invalid_values[invalid_field]
        elif invalid_field == "operation":
            scope_fields["operation"] = invalid_values[invalid_field]
        elif invalid_field == "binding":
            scope_fields["source_attempt_binding"] = invalid_values[invalid_field]
        else:
            scope_fields[invalid_field] = invalid_values[invalid_field]
        scope = SimpleNamespace(**scope_fields)

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with _client(handler) as client:
            client._source_attempt_gate = object()
            monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: (scope,))
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )

            with pytest.raises(
                RequestAuthorizationError, match="consistent trusted request scopes"
            ):
                client.get_raw_exchange("/v1/invalid-source-binding")

        assert calls == {"limiter": 0, "transport": 0}

    def test_durable_gate_rejects_mismatched_nested_scope_bindings(self, monkeypatch):
        calls = {"limiter": 0, "transport": 0}

        def make_scope(run_key: str):
            return SimpleNamespace(
                source="ths",
                canonical_model="fuyao.synthetic",
                operation=RequestOperation.QUERY,
                source_attempt_binding=SourceAttemptBinding(
                    grant_id="synthetic-grant",
                    expected_revision=1,
                    principal_scope="synthetic-principal",
                    run_key=run_key,
                ),
            )

        scopes = (make_scope("run-one"), make_scope("run-two"))

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["transport"] += 1
            return httpx.Response(200, stream=httpx.ByteStream(b"{}"))

        with _client(handler) as client:
            client._source_attempt_gate = object()
            monkeypatch.setattr(http_client_module, "current_request_scopes", lambda: scopes)
            monkeypatch.setattr(
                client._limiter,
                "wait_and_acquire",
                lambda _sleep: calls.__setitem__("limiter", 1),
            )

            with pytest.raises(
                RequestAuthorizationError, match="consistent trusted request scopes"
            ):
                client.get_raw_exchange("/v1/mismatched-source-bindings")

        assert calls == {"limiter": 0, "transport": 0}

    def test_source_attempt_reservation_requires_a_configured_gate(self):
        with (
            _client(lambda _request: pytest.fail("gate guard must prevent transport")) as client,
            pytest.raises(RequestAuthorizationError, match="gate is not configured"),
        ):
            client._reserve_source_attempt(
                (),
                host="fuyao.test",
                request_id="synthetic-request",
                attempt_no=1,
            )

    def test_scoped_shared_host_wait_checks_cancellation(self, monkeypatch):
        calls = {"liveness": 0, "release": 0}

        class ContendedGate:
            def acquire(self, *, timeout: float) -> bool:
                assert timeout == pytest.approx(0.05)
                return False

            def release(self) -> None:
                calls["release"] += 1

        monkeypatch.setattr(
            http_client_module,
            "get_shared_concurrency_gate",
            lambda _host: ContendedGate(),
        )

        def cancel(_scopes) -> None:
            calls["liveness"] += 1
            raise RequestExecutionCancelledError()

        monkeypatch.setattr(http_client_module, "check_scope_liveness", cancel)
        with (
            pytest.raises(RequestExecutionCancelledError),
            FuyaoHttpClient._scoped_shared_host_request((object(),), "fuyao.test"),
        ):
            pytest.fail("cancellation must prevent gate acquisition")

        assert calls == {"liveness": 1, "release": 0}

    def test_scoped_timeout_rejects_an_expired_deadline(self, monkeypatch):
        monkeypatch.setattr(http_client_module, "check_scope_liveness", lambda _scopes: None)
        with (
            _client(
                lambda _request: pytest.fail("deadline guard must prevent transport")
            ) as client,
            pytest.raises(RequestExecutionDeadlineError, match="deadline expired"),
        ):
            client._scoped_timeout((SimpleNamespace(deadline=0.0),))

    def test_existing_get_rethrows_limiter_error_and_logs_only_category(self, caplog, monkeypatch):
        private_detail = "synthetic-key-and-private-body"
        limiter_error = FuyaoError(
            private_detail,
            category="network",
            message="safe network failure",
            advice="safe advice",
        )

        def handler(_request: httpx.Request) -> httpx.Response:
            pytest.fail("limiter failure must happen before transport")

        with (
            caplog.at_level(logging.INFO, logger=LOGGER_NAME),
            _client(handler, raw_response_cache=_NoHitCache()) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            _enable_synthetic_scope(monkeypatch, client)

            def fail_limiter(_sleep) -> None:
                raise limiter_error

            monkeypatch.setattr(client._limiter, "wait_and_acquire", fail_limiter)
            client.get("/v1/legacy-limiter-error")

        assert exc.value is limiter_error
        assert private_detail not in "\n".join(
            record.getMessage() + repr(record.__dict__) for record in caplog.records
        )

    def test_existing_get_preserves_scoped_cached_redirect_rejection(self, monkeypatch):
        invalidations = 0

        class RedirectCache:
            def get(self, **_kwargs):
                return SimpleNamespace(status_code=302, content=b"redirect", encoding=None)

            def put(self, **_kwargs):
                pytest.fail("cached redirect must not be refreshed")

            def invalidate(self, **_kwargs):
                nonlocal invalidations
                invalidations += 1

        def handler(_request: httpx.Request) -> httpx.Response:
            pytest.fail("cache hit must prevent transport")

        with (
            _client(handler, raw_response_cache=RedirectCache()) as client,
            pytest.raises(RequestRedirectError),
        ):
            _enable_synthetic_scope(monkeypatch, client)
            client.get("/v1/legacy-cached-redirect")

        assert invalidations == 0

    def test_internal_raw_exchange_guard_rejects_empty_scope(self):
        with _client(lambda _request: pytest.fail("scope guard must prevent transport")) as client:
            client._validate_source_attempt_mode = lambda _scopes: None
            with pytest.raises(RequestAuthorizationError, match="active request scope"):
                client._raw_exchange_locked("/v1/no-internal-scope", params=None, scopes=())

    @pytest.mark.parametrize("endpoint", ["relative/path", "/path?token=secret", "/path#fragment"])
    def test_endpoint_must_be_a_path_without_query_or_fragment(self, endpoint):
        with (
            _client(lambda _request: pytest.fail("invalid endpoint must not send")) as client,
            pytest.raises(ValueError, match="endpoint must be a path"),
        ):
            client.get_raw_exchange(endpoint)
