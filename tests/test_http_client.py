"""Governed HTTP client tests (A1.4 / §5.3).

All transport, clock, sleep and jitter seams are faked: no network
access and no real delays. Covers retry policy, failure
classification, rate limiting, the circuit breaker and their
interaction with success paths.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from loguru import logger

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
    TokenBucket,
)

URL = "http://example.com/data"
URL_B = "http://other.org/data"


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeSession:
    """Transport seam: replays a script of statuses / exceptions."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.script:
            raise AssertionError("unexpected extra transport call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class FakeClock:
    """Clock + sleep seam: records sleeps and advances time by them."""

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(script, *, config=None, start=1000.0):
    """Client wired to a fake session and clock, zero jitter."""
    clock = FakeClock(start)
    session = FakeSession(script)
    client = GovernedHttpClient(
        config, session=session, sleep=clock.sleep, time_fn=clock.time, jitter_fn=lambda: 0.0
    )
    return client, session, clock


class TestSuccessPath:
    def test_success_returns_response(self):
        client, session, clock = make_client([200])
        response = client.get(URL)
        assert response.status_code == 200
        assert len(session.calls) == 1
        assert clock.sleeps == []

    def test_governance_kwargs_passed_to_transport(self):
        client, session, _ = make_client([200])
        client.get(URL, params={"q": "1"}, headers={"Referer": "http://example.com"})
        _method, _url, kwargs = session.calls[0]
        assert kwargs["timeout"] == (10.0, 30.0)
        assert kwargs["params"] == {"q": "1"}
        assert kwargs["proxies"] is None
        assert kwargs["headers"]["User-Agent"] == "opendata/0.1"
        assert kwargs["headers"]["Referer"] == "http://example.com"

    def test_custom_config_reaches_transport(self):
        config = HttpClientConfig(
            connect_timeout=2.0,
            read_timeout=5.0,
            default_headers={"User-Agent": "custom/ua"},
            proxies={"https": "http://proxy:8080"},
        )
        client, session, _ = make_client([200], config=config)
        client.get(URL)
        _method, _url, kwargs = session.calls[0]
        assert kwargs["timeout"] == (2.0, 5.0)
        assert kwargs["headers"]["User-Agent"] == "custom/ua"
        assert kwargs["proxies"] == {"https": "http://proxy:8080"}

    def test_config_is_strict(self):
        with pytest.raises(Exception):
            HttpClientConfig(bogus=1)

    def test_rate_and_concurrency_limits_must_be_positive(self):
        for field, value in (
            ("max_attempts", 0),
            ("rate_limit_per_host", 0.0),
            ("rate_burst", 0),
            ("max_concurrency_per_host", 0),
        ):
            with pytest.raises(Exception):
                HttpClientConfig(**{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("connect_timeout", 0.0),
            ("connect_timeout", -1.0),
            ("read_timeout", 0.0),
            ("read_timeout", -1.0),
            ("backoff_base", -0.1),
            ("backoff_jitter", -0.1),
            ("rate_max_wait", -0.1),
            ("breaker_threshold", 0),
            ("breaker_cooldown", -0.1),
        ],
    )
    def test_governance_settings_reject_out_of_range_values(self, field, value):
        with pytest.raises(Exception):
            HttpClientConfig(**{field: value})

    @pytest.mark.parametrize(
        "field",
        [
            "connect_timeout",
            "read_timeout",
            "backoff_base",
            "backoff_jitter",
            "rate_max_wait",
            "breaker_cooldown",
        ],
    )
    def test_float_governance_settings_reject_nan(self, field):
        with pytest.raises(Exception):
            HttpClientConfig(**{field: float("nan")})

    def test_zero_is_allowed_for_optional_waits_and_cooldowns(self):
        config = HttpClientConfig(
            backoff_base=0.0,
            backoff_jitter=0.0,
            rate_max_wait=0.0,
            breaker_cooldown=0.0,
        )

        assert config.backoff_base == 0.0
        assert config.backoff_jitter == 0.0
        assert config.rate_max_wait == 0.0
        assert config.breaker_cooldown == 0.0

    def test_governance_defaults_are_bounded(self):
        config = HttpClientConfig()

        assert config.rate_limit_per_host == 5.0
        assert config.rate_burst == 10
        assert config.max_concurrency_per_host == 4


class TestRetries:
    def test_network_error_retried_with_backoff_then_raises(self):
        client, session, clock = make_client([requests.ConnectionError()] * 3)
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.NETWORK
        assert excinfo.value.attempts == 3
        assert len(session.calls) == 3
        assert clock.sleeps == [0.5, 1.0]

    def test_retry_then_success(self):
        client, session, clock = make_client([requests.ConnectionError(), 200])
        assert client.get(URL).status_code == 200
        assert len(session.calls) == 2
        assert clock.sleeps == [0.5]

    def test_timeout_classified_separately(self):
        client, _, _ = make_client([requests.Timeout()] * 3)
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.TIMEOUT

    def test_post_is_not_retried(self):
        client, session, _ = make_client([requests.ConnectionError()])
        with pytest.raises(HttpFetchError) as excinfo:
            client.request("POST", URL)
        assert excinfo.value.attempts == 1
        assert len(session.calls) == 1

    def test_5xx_retried(self):
        client, session, _ = make_client([500, 500, 500])
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.UPSTREAM_ERROR
        assert excinfo.value.status == 500
        assert len(session.calls) == 3

    def test_4xx_not_retried(self):
        client, session, clock = make_client([404])
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.UPSTREAM_ERROR
        assert excinfo.value.status == 404
        assert excinfo.value.attempts == 1
        assert clock.sleeps == []

    def test_jitter_added_to_backoff(self):
        clock = FakeClock()
        client = GovernedHttpClient(
            None,
            session=FakeSession([requests.ConnectionError(), 200]),
            sleep=clock.sleep,
            time_fn=clock.time,
            jitter_fn=lambda: 0.25,
        )
        client.get(URL)
        assert clock.sleeps == [0.5 + 0.25]

    def test_custom_max_attempts(self):
        config = HttpClientConfig(max_attempts=2)
        client, session, _ = make_client([requests.ConnectionError()] * 5, config=config)
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.attempts == 2
        assert len(session.calls) == 2


class TestCircuitBreaker:
    def test_429_retried_within_request(self):
        client, _, _ = make_client([429, 429, 429])
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.RATE_LIMITED
        assert excinfo.value.status == 429

    def test_403_blocked_not_retried(self):
        client, session, _ = make_client([403])
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.BLOCKED
        assert len(session.calls) == 1

    def test_breaker_fails_fast_after_threshold(self):
        config = HttpClientConfig(breaker_threshold=3)
        client, session, _ = make_client([429, 429, 429], config=config)
        with pytest.raises(HttpFetchError):
            client.get(URL)
        transport_calls = len(session.calls)
        # Breaker opened on the third consecutive 429; a fresh request
        # must fail fast without touching the transport.
        session.script = [200]
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.attempts == 0
        assert excinfo.value.category is FailureCategory.RATE_LIMITED
        assert len(session.calls) == transport_calls

    def test_success_resets_breaker(self):
        config = HttpClientConfig(breaker_threshold=3)
        client, _, _ = make_client([429, 200, 429, 200], config=config)
        assert client.get(URL).status_code == 200
        assert client.get(URL).status_code == 200

    def test_breaker_cooldown_allows_probe(self):
        config = HttpClientConfig(breaker_threshold=3, breaker_cooldown=60.0)
        client, session, clock = make_client([429, 429, 429], config=config)
        with pytest.raises(HttpFetchError):
            client.get(URL)
        # Advance past the cooldown (no transport call happens).
        clock.now += 61.0
        session.script = [200]
        assert client.get(URL).status_code == 200

    def test_breaker_cooldown_allows_only_one_concurrent_probe(self):
        config = HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=1,
            breaker_threshold=1,
            breaker_cooldown=5.0,
        )
        clock = FakeClock()
        probe_entered = threading.Event()
        release_probe = threading.Event()

        class BlockingProbeSession(FakeSession):
            block_next = False

            def request(self, method, url, **kwargs):
                if self.block_next:
                    self.calls.append((method, url, kwargs))
                    probe_entered.set()
                    if not release_probe.wait(timeout=1.0):
                        raise AssertionError("probe release timed out")
                    return FakeResponse(200)
                return super().request(method, url, **kwargs)

        session = BlockingProbeSession([429])
        client = GovernedHttpClient(
            config,
            session=session,
            sleep=clock.sleep,
            time_fn=clock.time,
            jitter_fn=lambda: 0.0,
        )
        with pytest.raises(HttpFetchError):
            client.get(URL)
        session.block_next = True
        clock.now += 6.0

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                probe = pool.submit(client.get, URL)
                assert probe_entered.wait(timeout=1.0)
                with pytest.raises(HttpFetchError) as blocked:
                    client.get(URL)
                assert blocked.value.attempts == 0
                assert len(session.calls) == 2
                release_probe.set()
                assert probe.result(timeout=1.0).status_code == 200
        finally:
            release_probe.set()

        assert len(session.calls) == 2

    @pytest.mark.parametrize(
        ("failure", "category"),
        [
            (requests.Timeout(), FailureCategory.TIMEOUT),
            (requests.ConnectionError(), FailureCategory.NETWORK),
        ],
    )
    def test_failed_half_open_probe_reopens_breaker(self, failure, category):
        config = HttpClientConfig(
            rate_limit_per_host=None,
            breaker_threshold=3,
            breaker_cooldown=5.0,
        )
        client, session, clock = make_client([429, 429, 429, failure, 200], config=config)
        with pytest.raises(HttpFetchError):
            client.get(URL)
        clock.now += 6.0

        with pytest.raises(HttpFetchError) as probe_error:
            client.get(URL)
        assert probe_error.value.category is category
        assert probe_error.value.attempts == 1
        assert not probe_error.value.retryable
        assert len(session.calls) == 4

        with pytest.raises(HttpFetchError) as blocked:
            client.get(URL)
        assert blocked.value.attempts == 0
        assert len(session.calls) == 4

        clock.now += 6.0
        assert client.get(URL).status_code == 200
        assert len(session.calls) == 5

    def test_breaker_isolated_per_host(self):
        config = HttpClientConfig(breaker_threshold=3)
        client, session, _ = make_client([429, 429, 429, 200], config=config)
        with pytest.raises(HttpFetchError):
            client.get(URL)
        assert client.get(URL_B).status_code == 200


class TestRateLimiting:
    def test_burst_allowed_then_wait(self):
        config = HttpClientConfig(rate_limit_per_host=2.0, rate_burst=2)
        client, session, clock = make_client([200, 200, 200], config=config)
        assert client.get(URL).status_code == 200
        assert client.get(URL).status_code == 200
        assert clock.sleeps == []
        assert client.get(URL).status_code == 200
        # Third token needs half a second to refill at 2 tokens/sec.
        assert clock.sleeps == [pytest.approx(0.5)]

    def test_max_wait_fails_without_transport(self):
        config = HttpClientConfig(rate_limit_per_host=0.001, rate_burst=1, rate_max_wait=1.0)
        client, session, _ = make_client([200, 200], config=config)
        assert client.get(URL).status_code == 200
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        assert excinfo.value.category is FailureCategory.RATE_LIMITED
        assert excinfo.value.attempts == 0
        # Only the first (successful) request reached the transport.
        assert len(session.calls) == 1

    def test_token_bucket_refill_over_time(self):
        clock = FakeClock()
        bucket = TokenBucket(rate=4.0, capacity=2, time_fn=clock.time)
        assert bucket.try_acquire(clock.time()) is True
        assert bucket.try_acquire(clock.time()) is True
        assert bucket.try_acquire(clock.time()) is False
        clock.now += 1.0
        assert bucket.try_acquire(clock.time()) is True

    def test_token_bucket_wait_seconds(self):
        clock = FakeClock()
        bucket = TokenBucket(rate=2.0, capacity=1, time_fn=clock.time)
        assert bucket.try_acquire(clock.time()) is True
        assert bucket.wait_seconds(clock.time()) == pytest.approx(0.5)

    def test_rate_limit_isolated_per_host(self):
        config = HttpClientConfig(rate_limit_per_host=2.0, rate_burst=1)
        client, session, clock = make_client([200, 200], config=config)
        assert client.get(URL).status_code == 200
        assert client.get(URL_B).status_code == 200
        assert clock.sleeps == []


class TestFailures:
    def test_host_less_url_raises(self):
        client, _, _ = make_client([])
        with pytest.raises(ValueError, match="no host"):
            client.get("not-a-url")

    def test_diagnostic_string(self):
        client, _, _ = make_client([404])
        with pytest.raises(HttpFetchError) as excinfo:
            client.get(URL)
        text = str(excinfo.value)
        assert "[upstream_error]" in text
        assert "host=example.com" in text
        assert "status=404" in text
        assert "attempts=1" in text
        assert URL in text

    def test_host_is_lowercased(self):
        client, _, _ = make_client([200])
        assert client.get("http://EXAMPLE.com/x").status_code == 200


def test_host_concurrency_limit_is_shared_across_thread_local_sessions(monkeypatch):
    lock = threading.Lock()
    release = threading.Event()
    reached_limit = threading.Event()
    active = 0
    peak = 0

    class BlockingSession:
        def request(self, method, url, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if peak >= 2:
                    reached_limit.set()
            release.wait(timeout=2.0)
            with lock:
                active -= 1
            return FakeResponse(200)

    monkeypatch.setattr(requests, "Session", BlockingSession)
    client = GovernedHttpClient(
        HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=1,
            max_concurrency_per_host=2,
        )
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(client.get, URL) for _ in range(8)]
        assert reached_limit.wait(timeout=1.0)
        time.sleep(0.03)
        with lock:
            assert peak == 2
        release.set()
        assert [future.result(timeout=2.0).status_code for future in futures] == [200] * 8


def test_transport_events_redact_url_credentials_parameters_and_exception_text():
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=FakeSession([requests.ConnectionError("raw-exception-secret")]),
    )
    events = []
    sink = logger.add(lambda message: events.append(message.record["extra"]), level="INFO")
    try:
        with pytest.raises(HttpFetchError) as exc:
            client.get(
                "https://user:password@example.test/data?token=url-secret",
                params={"access_token": "parameter-secret"},
                source="test",
            )
    finally:
        logger.remove(sink)

    assert exc.value.url == "https://example.test/data"
    event = next(item for item in events if item.get("event") == "governed_http_request")
    assert event["endpoint"] == "https://example.test/data"
    assert event["parameter_summary"] == {
        "keys": ("access_token",),
        "redacted_keys": ("access_token",),
    }
    for secret in ("password", "url-secret", "parameter-secret", "raw-exception-secret"):
        assert secret not in str(exc.value)
        assert secret not in repr(event)
