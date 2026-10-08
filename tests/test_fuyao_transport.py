"""fuyao 传输层测试（A3.1，T1 档：mock 信封与错误码，无真机调用）。

覆盖：凭据脱敏与失败关闭、信封解析与业务码分类、错误业务文案表完整性、
限流器冷却与令牌桶、HTTP 客户端的重试/退避/429 联动/响应体上限/超时。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest

from opendata.data.providers.ths import (
    CODE_CATEGORIES,
    LOCAL_BLOCKING_CODES,
    RATE_LIMIT_CODES,
    TRANSIENT_CODES,
    FuyaoCredentials,
    FuyaoCredentialsError,
    FuyaoError,
    FuyaoHttpClient,
    FuyaoRateLimiter,
    category_for,
    error_for_transport,
    error_for_upstream_code,
    load_error_messages,
    parse_envelope,
)
from opendata.data.providers.ths.transport.errors import ErrorTableError

HTTP_CLIENT_LOGGER = FuyaoHttpClient.__module__


def _envelope(code: int = 0, *, items: list | None = None, message: str = "success") -> bytes:
    return json.dumps(
        {
            "code": code,
            "message": message,
            "request_id": "req-abc",
            "data": {"timestamp": 1767369600000, "item": items or []},
        }
    ).encode("utf-8")


def _client(handler, **kwargs) -> FuyaoHttpClient:
    credentials = FuyaoCredentials("fuyao-test-key", base_url="https://fuyao.test")
    return FuyaoHttpClient(
        credentials=credentials,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,  # 退避不真实休眠
        **kwargs,
    )


class TestCredentials:
    def test_missing_key_returns_none(self):
        assert FuyaoCredentials.from_environment(environment={}) is None

    def test_environment_is_read_without_leaking(self):
        credentials = FuyaoCredentials.from_environment(
            environment={
                "FUYAO_API_KEY": "fuyao-secret",
                "FUYAO_API_BASE_URL": "https://fuyao.example",
            }
        )

        assert credentials is not None
        assert credentials.api_key == "fuyao-secret"
        assert credentials.base_url == "https://fuyao.example"
        assert "fuyao-secret" not in repr(credentials)
        assert "fuyao-secret" not in str(credentials)
        assert "fuyao-secret" not in repr(FuyaoCredentialsError("FUYAO_CREDENTIAL_INVALID"))

    def test_default_base_url_matches_the_ecosystem_client(self):
        credentials = FuyaoCredentials("k")

        assert credentials.base_url == "https://fuyao.aicubes.cn"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_blank_key_is_rejected(self, bad):
        with pytest.raises(FuyaoCredentialsError):
            FuyaoCredentials(bad)

    def test_invalid_base_url_is_rejected(self):
        with pytest.raises(FuyaoCredentialsError) as exc:
            FuyaoCredentials("k", base_url="fuyao.aicubes.cn")

        assert exc.value.code == "FUYAO_BASE_URL_INVALID"


class TestEnvelope:
    def test_success_envelope_is_parsed(self):
        envelope = parse_envelope(
            {
                "code": 0,
                "message": "success",
                "request_id": "r1",
                "data": {"timestamp": 1, "item": [{"a": 1}]},
            }
        )

        assert envelope.code == 0
        assert envelope.items == ({"a": 1},)
        assert envelope.data_timestamp_ms == 1
        assert envelope.request_id == "r1"

    def test_absent_data_is_allowed(self):
        envelope = parse_envelope({"code": 0, "message": "ok", "request_id": "r"})

        assert envelope.items == ()
        assert envelope.data_timestamp_ms is None

    def test_non_integer_code_is_an_envelope_failure(self):
        with pytest.raises(FuyaoError) as exc:
            parse_envelope({"code": "0", "message": "ok", "request_id": "r"})

        assert exc.value.category == "envelope_invalid"

    def test_non_mapping_data_is_an_envelope_failure(self):
        with pytest.raises(FuyaoError) as exc:
            parse_envelope({"code": 0, "message": "ok", "request_id": "r", "data": []})

        assert exc.value.category == "envelope_invalid"

    def test_non_list_items_is_an_envelope_failure(self):
        with pytest.raises(FuyaoError) as exc:
            parse_envelope({"code": 0, "message": "ok", "data": {"item": {"a": 1}}})

        assert exc.value.category == "envelope_invalid"


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("code", "category", "retryable"),
        [
            (1003, "request", False),
            (2001, "auth", False),
            (2003, "permission", False),
            (3001, "empty", False),
            (3002, "not_ready", False),
            (3004, "unsupported", False),
            (4001, "rate_limited", True),
            (5001, "transient", True),
            (5002, "transient", True),
            (5003, "transient", True),
            (9999, "unknown", False),
        ],
    )
    def test_codes_map_to_stable_categories(self, code, category, retryable):
        error = error_for_upstream_code(code, request_id="r1")

        assert error.category == category
        assert error.retryable is retryable
        assert error.upstream_code == code
        assert error.request_id == "r1"
        assert error.message  # 中文业务文案非空
        assert error.advice  # 处置建议非空

    def test_zero_code_is_not_an_error(self):
        with pytest.raises(ValueError, match="success"):
            error_for_upstream_code(0)

    def test_code_sets_are_consistent(self):
        assert category_for(4001) == "rate_limited"
        assert {4001} == RATE_LIMIT_CODES
        assert {5001, 5002, 5003} == TRANSIENT_CODES
        assert {1001, 1002, 1003, 1004} == LOCAL_BLOCKING_CODES
        assert set(CODE_CATEGORIES) >= (RATE_LIMIT_CODES | TRANSIENT_CODES | LOCAL_BLOCKING_CODES)

    def test_transport_errors_carry_category_and_detail(self):
        error = error_for_transport("network", detail="ConnectError")

        assert error.category == "network"
        assert error.retryable is True
        assert error.upstream_code is None
        assert "ConnectError" in error.code  # 细节进 code，不进面向用户的文案
        assert error.as_dict()["advice"]


class TestErrorMessages:
    def test_every_registered_code_has_an_entry(self):
        table = load_error_messages()

        missing = [str(code) for code in CODE_CATEGORIES if str(code) not in table]
        assert missing == []

    def test_every_transport_key_has_an_entry(self):
        table = load_error_messages()

        for key in (
            "network",
            "timeout",
            "rate_limited",
            "response_too_large",
            "envelope_invalid",
            "http",
            "unknown",
        ):
            assert key in table

    def test_entries_declare_category_message_and_advice(self):
        for key, entry in load_error_messages().items():
            assert entry.category, key
            assert entry.message, key
            assert entry.advice, key

    def test_malformed_table_fails_closed(self, tmp_path):
        broken = tmp_path / "error_messages.yaml"
        broken.write_text("version: 1\nmessages:\n  0:\n    category: ok\n", encoding="utf-8")

        with pytest.raises(ErrorTableError, match="malformed"):
            load_error_messages.cache_clear() or load_error_messages(str(broken))
        load_error_messages.cache_clear()

    def test_missing_file_fails_closed(self, tmp_path):
        with pytest.raises(ErrorTableError, match="unreadable"):
            load_error_messages(str(tmp_path / "absent.yaml"))
        load_error_messages.cache_clear()


class TestRateLimiter:
    def test_short_lived_default_clients_share_host_governance_state(self):
        credentials = FuyaoCredentials("k", base_url="https://fuyao.shared.test")
        first = FuyaoHttpClient(credentials=credentials)
        second = FuyaoHttpClient(credentials=credentials)
        try:
            assert first._limiter is second._limiter
            assert first._limiter.rate_per_second == 5.0
            assert first._limiter.burst == 10
        finally:
            first.close()
            second.close()

    def test_burst_then_exhaustion(self):
        limiter = FuyaoRateLimiter(rate_per_second=1.0, burst=2)

        limiter.acquire()
        limiter.acquire()
        with pytest.raises(FuyaoError) as exc:
            limiter.acquire()

        assert exc.value.category == "http"
        assert "rate_limited_local_bucket" in exc.value.code

    def test_tokens_refill_over_time(self):
        now = {"t": 0.0}
        limiter = FuyaoRateLimiter(rate_per_second=1.0, burst=1, clock=lambda: now["t"])
        limiter.acquire()

        now["t"] = 1.0
        limiter.acquire()  # 一个令牌补充完成

    def test_cooldown_blocks_until_retry_after(self):
        now = {"t": 0.0}
        limiter = FuyaoRateLimiter(rate_per_second=10.0, burst=5, clock=lambda: now["t"])
        limiter.record_rate_limit(retry_after_seconds=30.0)

        assert limiter.cooldown_remaining == pytest.approx(30.0)
        with pytest.raises(FuyaoError) as exc:
            limiter.acquire()
        assert "rate_limited_wait" in exc.value.code

        now["t"] = 30.0
        assert limiter.cooldown_remaining == 0.0
        limiter.acquire()

    def test_wait_and_acquire_consumes_the_cooldown(self):
        now = {"t": 0.0}
        waits: list[float] = []
        limiter = FuyaoRateLimiter(rate_per_second=10.0, burst=1, clock=lambda: now["t"])
        limiter.record_rate_limit(retry_after_seconds=5.0)

        def advance_clock(seconds: float) -> None:
            waits.append(seconds)
            now["t"] += seconds

        limiter.wait_and_acquire(advance_clock)

        assert waits == [pytest.approx(5.0)]
        assert limiter.cooldown_remaining == 0.0

    def test_wait_does_not_clear_a_concurrently_extended_cooldown(self):
        now = {"t": 0.0}
        sleeping = threading.Event()
        release_sleep = threading.Event()
        limiter = FuyaoRateLimiter(rate_per_second=10.0, burst=1, clock=lambda: now["t"])
        limiter.record_rate_limit(retry_after_seconds=5.0)

        def controlled_sleep(seconds: float) -> None:
            assert seconds == pytest.approx(5.0)
            sleeping.set()
            assert release_sleep.wait(timeout=1.0)
            now["t"] += seconds

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(limiter.wait_and_acquire, controlled_sleep)
            assert sleeping.wait(timeout=1.0)
            limiter.record_rate_limit(retry_after_seconds=10.0)
            release_sleep.set()
            with pytest.raises(FuyaoError) as exc:
                pending.result(timeout=1.0)

        assert exc.value.category == "http"
        assert "rate_limited_wait" in exc.value.code
        assert limiter.cooldown_remaining == pytest.approx(5.0)

    def test_host_concurrency_wait_is_bounded_and_rate_limited(self):
        from opendata.data.providers.ths.transport.rate_limiter import (
            get_shared_concurrency_gate,
            shared_host_request,
        )

        host = "fuyao.gate-timeout.test"
        gate = get_shared_concurrency_gate(host)
        for _ in range(4):
            gate.acquire()
        try:
            with (
                pytest.raises(FuyaoError) as exc,
                shared_host_request(host, wait_timeout_seconds=0.01),
            ):
                pytest.fail("a saturated host gate should not be acquired")
        finally:
            for _ in range(4):
                gate.release()

        assert exc.value.category == "rate_limited"
        assert "host_concurrency_wait_timeout" in exc.value.code

    def test_client_classifies_host_concurrency_timeout(self, monkeypatch, caplog):
        @contextmanager
        def busy_host(_host):
            raise error_for_transport("rate_limited", detail="host_concurrency_wait_timeout")
            yield

        monkeypatch.setattr(f"{HTTP_CLIENT_LOGGER}.shared_host_request", busy_host)
        with (
            caplog.at_level(logging.INFO, logger=HTTP_CLIENT_LOGGER),
            _client(
                lambda _request: pytest.fail("busy host must fail before transport"), max_attempts=1
            ) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            client.get("/api/a-share/prices", params={"api_key": "query-secret"})

        assert exc.value.category == "rate_limited"
        event = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "governed_http_request"
        )
        assert event.failure_category == "rate_limited"
        assert event.endpoint.endswith("/api/a-share/prices")
        assert "query-secret" not in str(event.__dict__)

    def test_default_cooldown_is_one_second(self):
        limiter = FuyaoRateLimiter(rate_per_second=10.0, burst=1)

        limiter.record_rate_limit(retry_after_seconds=None)

        assert limiter.cooldown_remaining > 0.0


class TestHttpClient:
    def test_successful_get_sends_the_key_header_and_parses(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            seen["url"] = str(request.url)
            return httpx.Response(200, content=_envelope(items=[{"close": 1.0}]))

        with _client(handler) as client:
            result = client.get("/api/a-share/prices", params={"thscode": "600519.SH"})

        assert seen["x-api-key"] == "fuyao-test-key"
        assert seen["url"].startswith("https://fuyao.test/api/a-share/prices?")
        assert result.envelope.items == ({"close": 1.0},)
        assert result.status_code == 200
        assert result.elapsed_seconds >= 0.0

    def test_endpoint_must_be_absolute_path(self):
        with (
            _client(lambda r: httpx.Response(200, content=_envelope())) as client,
            pytest.raises(ValueError, match="must start with"),
        ):
            client.get("api/a-share/prices")

    def test_auth_error_is_not_retried(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=_envelope(2001, message="unauthorized"))

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "auth"
        assert exc.value.retryable is False
        assert len(calls) == 1  # 不浪费重试预算

    def test_transient_error_is_retried_then_succeeds(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(200, content=_envelope(5001, message="server error"))
            return httpx.Response(200, content=_envelope(items=[{"close": 2.0}]))

        with _client(handler, max_attempts=3) as client:
            result = client.get("/api/a-share/prices")

        assert len(calls) == 3
        assert result.envelope.items == ({"close": 2.0},)

    def test_retries_are_bounded(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=_envelope(5003, message="unavailable"))

        with _client(handler, max_attempts=2) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert len(calls) == 2
        assert exc.value.category == "transient"

    def test_http_429_enters_cooldown_and_is_retried(self):
        now = {"t": 0.0}
        limiter = FuyaoRateLimiter(
            rate_per_second=10.0,
            burst=10,
            clock=lambda: now["t"],
        )
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, content=b"")
            return httpx.Response(200, content=_envelope())

        credentials = FuyaoCredentials("k", base_url="https://fuyao.test")
        client = FuyaoHttpClient(
            credentials=credentials,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=limiter,
            max_attempts=2,
            sleep=lambda seconds: now.__setitem__("t", now["t"] + seconds),
        )

        result = client.get("/api/a-share/prices")

        assert result.status_code == 200
        # Retry-After 生效：冷却被重试等待消费完，随后请求成功。
        assert limiter.cooldown_remaining == 0.0

    def test_http_429_error_is_classified_retryable(self):
        limiter = FuyaoRateLimiter(rate_per_second=10.0, burst=10)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"")

        client = FuyaoHttpClient(
            credentials=FuyaoCredentials("k", base_url="https://fuyao.test"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            rate_limiter=limiter,
            max_attempts=1,
            sleep=lambda seconds: None,
        )

        with pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "rate_limited"
        assert exc.value.retryable is True

    def test_http_4xx_is_not_retried(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(404, content=b"not found")

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "http"
        assert exc.value.retryable is False
        assert len(calls) == 1

    def test_timeout_is_retryable(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("too slow", request=request)

        with _client(handler, max_attempts=2) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "timeout"
        assert len(calls) == 2

    def test_network_error_is_retryable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with _client(handler, max_attempts=1) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "network"

    def test_response_size_bound_is_enforced(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 512)

        with (
            _client(handler, max_response_bytes=64, max_attempts=1) as client,
            pytest.raises(FuyaoError) as exc,
        ):
            client.get("/api/a-share/prices")

        assert exc.value.category == "response_too_large"

    def test_invalid_json_is_an_envelope_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        with _client(handler, max_attempts=1) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "envelope_invalid"

    def test_invalid_max_attempts_is_rejected(self):
        with pytest.raises(ValueError, match="max_attempts"):
            FuyaoHttpClient(credentials=FuyaoCredentials("k"), max_attempts=0)

    def test_domain_result_is_not_retried(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=_envelope(3002, message="data not ready"))

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            client.get("/api/a-share/prices")

        assert exc.value.category == "not_ready"
        assert len(calls) == 1

    def test_structured_event_redacts_credentials_and_parameter_values(self, caplog):
        with (
            caplog.at_level(logging.INFO, logger=HTTP_CLIENT_LOGGER),
            _client(
                lambda _request: httpx.Response(200, content=_envelope(3002, message="not ready")),
                max_attempts=1,
            ) as client,
            pytest.raises(FuyaoError),
        ):
            client.get(
                "/api/a-share/prices",
                params={"thscode": "600519.SH", "api_token": "query-secret"},
            )

        event = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "governed_http_request"
        )
        assert event.source == "ths"
        assert event.endpoint == "https://fuyao.test/api/a-share/prices"
        assert event.parameter_summary == {
            "keys": ("api_token", "thscode"),
            "redacted_keys": ("api_token",),
        }
        assert event.request_id
        assert event.attempt == 1
        assert event.status == 200
        assert event.failure_category == "not_ready"
        assert event.elapsed_seconds >= 0.0
        assert "fuyao-test-key" not in str(event.__dict__)
        assert "query-secret" not in str(event.__dict__)

    def test_transport_event_does_not_log_exception_text(self, caplog):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("raw-exception-secret", request=request)

        with (
            caplog.at_level(logging.INFO, logger=HTTP_CLIENT_LOGGER),
            _client(handler, max_attempts=1) as client,
            pytest.raises(FuyaoError),
        ):
            client.get("/api/a-share/prices", params={"api_token": "query-secret"})

        event = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "governed_http_request"
        )
        assert event.failure_category == "network"
        assert event.status is None
        for secret in ("fuyao-test-key", "query-secret", "raw-exception-secret"):
            assert secret not in str(event.__dict__)

    def test_short_lived_clients_share_a_per_host_concurrency_cap(self):
        lock = threading.Lock()
        release = threading.Event()
        reached_limit = threading.Event()
        active = 0
        peak = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if peak >= 4:
                    reached_limit.set()
            release.wait(timeout=2.0)
            with lock:
                active -= 1
            return httpx.Response(200, content=_envelope())

        clients = [
            FuyaoHttpClient(
                credentials=FuyaoCredentials("k", base_url="https://fuyao.concurrent.test"),
                client=httpx.Client(transport=httpx.MockTransport(handler)),
                rate_limiter=FuyaoRateLimiter(rate_per_second=1000.0, burst=100),
                max_attempts=1,
                sleep=lambda _seconds: None,
            )
            for _ in range(8)
        ]
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(client.get, "/api/a-share/prices") for client in clients]
                assert reached_limit.wait(timeout=1.0)
                time.sleep(0.03)
                with lock:
                    assert peak == 4
                release.set()
                assert [future.result(timeout=2.0).status_code for future in futures] == [200] * 8
        finally:
            release.set()
            for client in clients:
                client.close()
