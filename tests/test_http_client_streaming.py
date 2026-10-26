"""Behavior tests for the governed client's explicitly bounded GET API."""

from __future__ import annotations

from datetime import timezone
from typing import TYPE_CHECKING

import pytest
import requests
from requests.structures import CaseInsensitiveDict
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.request_budget import RequestAuthorizationError

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Iterator

URL = "https://example.invalid/archive"
TEST_POOL = HTTPConnectionPool("example.invalid")


class FakeClock:
    """Monotonic test clock advanced only by explicit test actions."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class SyntheticRaw(HTTPResponse):
    """Small urllib3-shaped stream that records reads and early closure."""

    def __init__(
        self,
        chunks: Iterable[bytes] = (),
        *,
        fail_after_reads: int | None = None,
        read_timeout_at: int | None = None,
        clock: FakeClock | None = None,
        advance_per_read: float = 0.0,
        advance_on_eof: float = 0.0,
    ) -> None:
        super().__init__(body=b"", preload_content=False)
        self._chunks = list(chunks)
        self._fail_after_reads = fail_after_reads
        self._read_timeout_at = read_timeout_at
        self._clock = clock
        self._advance_per_read = advance_per_read
        self._advance_on_eof = advance_on_eof
        self.read_calls = 0
        self.stream_calls = 0
        self.requested_sizes: list[int] = []
        self.decode_content_values: list[bool | None] = []
        self._synthetic_closed = False

    @property
    def closed(self) -> bool:
        return self._synthetic_closed

    def stream(
        self,
        amt: int | None = 65536,
        decode_content: bool | None = None,
    ) -> Generator[bytes, None, None]:
        self.stream_calls += 1
        chunk_size = 65536 if amt is None else amt
        self.requested_sizes.append(chunk_size)
        self.decode_content_values.append(decode_content)
        for source_chunk in self._chunks:
            for offset in range(0, len(source_chunk), chunk_size):
                self.read_calls += 1
                if self._fail_after_reads == self.read_calls:
                    raise requests.ConnectionError("synthetic stream interruption")
                if self._read_timeout_at == self.read_calls:
                    raise ReadTimeoutError(TEST_POOL, URL, "synthetic read timeout")
                if self._clock is not None:
                    self._clock.now += self._advance_per_read
                yield source_chunk[offset : offset + chunk_size]
        if self._clock is not None:
            self._clock.now += self._advance_on_eof
        self.close()

    def close(self) -> None:
        self._synthetic_closed = True


class GuardedResponse(requests.Response):
    """Response that makes accidental eager content or text access fail."""

    def __init__(self) -> None:
        super().__init__()
        self.close_called = False

    @property
    def content(self) -> bytes:
        raise AssertionError("bounded client must not access Response.content")

    @property
    def text(self) -> str:
        raise AssertionError("bounded client must not access Response.text")

    def close(self) -> None:
        self.close_called = True
        super().close()


def make_response(
    chunks: Iterable[bytes] = (),
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    raw: SyntheticRaw | None = None,
) -> tuple[GuardedResponse, SyntheticRaw]:
    stream = raw if raw is not None else SyntheticRaw(chunks)
    response = GuardedResponse()
    response.status_code = status_code
    response.headers = CaseInsensitiveDict(headers or {})
    response.raw = stream
    response._content = False
    response._content_consumed = False
    response.url = URL
    return response, stream


class ScriptedSession(requests.Session):
    """Session that returns only synthetic responses and records request options."""

    def __init__(self, responses: Iterable[requests.Response]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected transport attempt")
        return self.responses.pop(0)


class SingleResponseAdapter(requests.adapters.BaseAdapter):
    """Replay one response through Requests' real redirect handling without a socket."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(self, response: requests.Response) -> None:
        self.response = response
        self.calls: list[tuple[requests.PreparedRequest, dict[str, object]]] = []

    def send(
        self,
        request: requests.PreparedRequest,
        **_kwargs: object,
    ) -> requests.Response:
        self.calls.append((request, _kwargs))
        self.response.url = request.url
        self.response.request = request
        self.response.connection = self
        return self.response

    def close(self) -> None:
        """Close no resources because this adapter never opens a socket."""


class QueuedResponseAdapter(requests.adapters.BaseAdapter):
    """Replay synthetic responses through a standard Requests session pipeline."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(self, responses: Iterable[requests.Response]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def send(
        self,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        if not self.responses:
            raise AssertionError("unexpected transport attempt")
        call_kwargs = dict(kwargs)
        call_kwargs["headers"] = dict(request.headers)
        method = request.method or ""
        url = request.url or ""
        self.calls.append((method, url, call_kwargs))
        response = self.responses.pop(0)
        response.url = url
        response.request = request
        response.connection = self
        return response

    def close(self) -> None:
        """Close no resources because this adapter never opens a socket."""


class RedirectOverrideSession(requests.Session):
    """Session with a redirect method override, rejected by bounded requests."""

    def resolve_redirects(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> Iterator[requests.Response]:
        raise AssertionError("bounded validation must reject overridden redirect handling")


def make_client(
    responses: Iterable[requests.Response],
    *,
    config: HttpClientConfig | None = None,
    clock: FakeClock | None = None,
) -> tuple[GovernedHttpClient, QueuedResponseAdapter, FakeClock]:
    selected_clock = clock if clock is not None else FakeClock()
    session = requests.Session()
    adapter = QueuedResponseAdapter(responses)
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        config
        or HttpClientConfig(
            rate_limit_per_host=None,
            connect_timeout=2.0,
            read_timeout=7.0,
        ),
        session=session,
        sleep=selected_clock.sleep,
        time_fn=selected_clock.time,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )
    return client, adapter, selected_clock


def test_bounded_success_streams_and_preserves_http_metadata() -> None:
    response, raw = make_response(
        [b"abc", b"def"],
        status_code=206,
        headers={"Content-Length": "6"},
    )
    client, session, _clock = make_client([response])

    result = client.get_bounded(
        URL,
        max_body_bytes=8,
        overall_timeout=12.0,
        headers={"accept-encoding": "gzip", "X-Request": "test"},
        timeout=(3.0, 5.0),
        source="test_archive",
    )

    assert isinstance(result, BoundedHttpResponse)
    assert result.body == b"abcdef"
    assert result.status_code == 206
    assert result.retrieved_at.tzinfo is timezone.utc
    assert response._content is False
    assert raw.read_calls == 2
    assert response.close_called
    assert raw.decode_content_values == [True]
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("GET", URL)
    assert kwargs["stream"] is True
    request_headers = kwargs["headers"]
    assert isinstance(request_headers, dict)
    assert request_headers["Accept-Encoding"] == "identity"
    assert kwargs["timeout"] == (3.0, 5.0)


@pytest.mark.parametrize(
    "headers",
    [None, {"Content-Length": "1"}, {"Content-Length": "invalid"}],
)
def test_unknown_or_unreliable_length_is_enforced_by_streamed_bytes(
    headers: dict[str, str] | None,
) -> None:
    response, raw = make_response([b"abcd", b"ef", b"never-read"], headers=headers)
    client, _session, _clock = make_client([response])

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=5, overall_timeout=10.0)

    assert error.value.category is FailureCategory.BODY_TOO_LARGE
    assert error.value.status == 200
    assert error.value.attempts == 1
    assert raw.read_calls == 2
    assert raw.closed


def test_declared_length_over_limit_closes_before_reading() -> None:
    response, raw = make_response([b"unread"], headers={"Content-Length": "6"})
    client, _session, _clock = make_client([response])

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=5, overall_timeout=10.0)

    assert error.value.category is FailureCategory.BODY_TOO_LARGE
    assert raw.stream_calls == 0
    assert raw.read_calls == 0
    assert raw.closed


def test_compressed_response_is_rejected_before_requests_can_expand_it() -> None:
    response, raw = make_response(
        [b"compressed bytes"],
        headers={"Content-Encoding": "gzip", "Content-Length": "16"},
    )
    client, session, _clock = make_client([response])

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(
            URL,
            max_body_bytes=32,
            overall_timeout=10.0,
            headers={"Accept-Encoding": "br"},
        )

    assert error.value.category is FailureCategory.UNSUPPORTED_CONTENT_ENCODING
    assert raw.stream_calls == 0
    assert raw.closed
    request_headers = session.calls[0][2]["headers"]
    assert isinstance(request_headers, dict)
    assert request_headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "headers",
    [
        {"Location": "https://other.example.invalid/redirect-target"},
        {},
    ],
)
def test_redirect_status_is_rejected_without_following_or_reading_body(
    headers: dict[str, str],
) -> None:
    response, raw = make_response(
        [b"oversized redirect body"],
        status_code=302,
        headers=headers,
    )
    adapter = SingleResponseAdapter(response)
    session = requests.Session()
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=1, overall_timeout=10.0)

    assert error.value.category is FailureCategory.REDIRECT
    assert error.value.status == 302
    assert error.value.attempts == 1
    assert len(adapter.calls) == 1
    assert raw.read_calls == 0
    assert raw.closed
    assert response.close_called


def test_bounded_request_rejects_preconfigured_session_response_hooks() -> None:
    response, raw = make_response([b"unread"], status_code=302)
    adapter = QueuedResponseAdapter([response])
    session = requests.Session()
    session.mount("https://", adapter)

    def unexpected_hook(
        _response: requests.Response,
        **_kwargs: object,
    ) -> requests.Response:
        raise AssertionError("preconfigured response hook must not run")

    session.hooks["response"] = [unexpected_hook]
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(RequestAuthorizationError):
        client.get_bounded(URL, max_body_bytes=1, overall_timeout=10.0)

    assert not adapter.calls
    assert raw.read_calls == 0
    assert not response.close_called


def test_bounded_request_rejects_overridden_session_redirect_handler() -> None:
    response, raw = make_response([b"unread"], status_code=302)
    adapter = QueuedResponseAdapter([response])
    session = RedirectOverrideSession()
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(RequestAuthorizationError):
        client.get_bounded(URL, max_body_bytes=1, overall_timeout=10.0)

    assert not adapter.calls
    assert raw.read_calls == 0
    assert not response.close_called


def test_wrapped_urllib3_read_timeout_is_classified_as_timeout() -> None:
    response, raw = make_response(
        [b"prefix", b"tail"],
        raw=SyntheticRaw([b"prefix", b"tail"], read_timeout_at=2),
    )
    client, session, _clock = make_client(
        [response],
        config=HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=1,
            connect_timeout=2.0,
            read_timeout=7.0,
        ),
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)

    assert error.value.category is FailureCategory.TIMEOUT
    assert error.value.status == 200
    assert error.value.attempts == 1
    assert len(session.calls) == 1
    assert raw.closed


def test_stream_read_timeout_reopens_half_open_breaker_as_timeout() -> None:
    rate_limited, _rate_raw = make_response(status_code=429)
    timed_out, timeout_raw = make_response(
        [b"prefix", b"tail"],
        raw=SyntheticRaw([b"prefix", b"tail"], read_timeout_at=2),
    )
    client, session, clock = make_client(
        [rate_limited, timed_out],
        config=HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=1,
            breaker_threshold=1,
            breaker_cooldown=1.0,
            connect_timeout=2.0,
            read_timeout=7.0,
        ),
    )

    with pytest.raises(HttpFetchError) as limited:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)
    assert limited.value.category is FailureCategory.RATE_LIMITED

    clock.now += 1.1
    with pytest.raises(HttpFetchError) as probe:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)
    assert probe.value.category is FailureCategory.TIMEOUT
    assert probe.value.status == 200
    assert probe.value.attempts == 1
    assert timeout_raw.closed

    with pytest.raises(HttpFetchError) as reopened:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)
    assert reopened.value.category is FailureCategory.TIMEOUT
    assert reopened.value.attempts == 0
    assert len(session.calls) == 2


def test_midstream_network_error_is_retried_with_finite_governed_attempts() -> None:
    first, first_raw = make_response(
        [b"prefix", b"tail"], raw=SyntheticRaw([b"prefix", b"tail"], fail_after_reads=2)
    )
    second, second_raw = make_response([b"complete"])
    client, session, clock = make_client(
        [first, second],
        config=HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=2,
            connect_timeout=2.0,
            read_timeout=7.0,
        ),
    )

    result = client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)

    assert result.body == b"complete"
    assert result.status_code == 200
    assert first_raw.closed
    assert second.close_called
    assert len(session.calls) == 2
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (429, FailureCategory.RATE_LIMITED),
        (503, FailureCategory.UPSTREAM_ERROR),
    ],
)
def test_rate_and_upstream_statuses_remain_visible_and_close_stream(
    status_code: int,
    category: FailureCategory,
) -> None:
    response, raw = make_response([b"error body"], status_code=status_code)
    client, session, _clock = make_client(
        [response],
        config=HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=10.0)

    assert error.value.category is category
    assert error.value.status == status_code
    assert error.value.attempts == 1
    assert len(session.calls) == 1
    assert raw.stream_calls == 0
    assert raw.closed


def test_total_budget_stops_before_another_stream_read() -> None:
    clock = FakeClock()
    raw = SyntheticRaw([b"one", b"two"], clock=clock, advance_per_read=0.6)
    response, _raw = make_response(raw=raw)
    client, session, _clock = make_client([response], clock=clock)

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=0.5)

    assert error.value.category is FailureCategory.TIMEOUT
    assert error.value.attempts == 1
    assert len(session.calls) == 1
    assert raw.read_calls == 1
    assert raw.closed
    assert session.calls[0][2]["timeout"] == (0.5, 0.5)


def test_total_budget_is_checked_after_the_final_read_reaches_eof() -> None:
    clock = FakeClock()
    raw = SyntheticRaw([b"body"], clock=clock, advance_on_eof=0.6)
    response, _raw = make_response(raw=raw)
    client, _session, _clock = make_client([response], clock=clock)

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=0.5)

    assert error.value.category is FailureCategory.TIMEOUT
    assert error.value.attempts == 1
    assert raw.read_calls == 1
    assert raw.closed


@pytest.mark.parametrize(
    ("max_body_bytes", "overall_timeout", "timeout", "error_type"),
    [
        (True, 1.0, None, TypeError),
        (0, 1.0, None, ValueError),
        (1, True, None, TypeError),
        (1, float("nan"), None, ValueError),
        (1, float("inf"), None, ValueError),
        (1, 10**1000, None, ValueError),
        (1, 1.0, True, TypeError),
        (1, 1.0, (1.0, float("nan")), ValueError),
    ],
)
def test_bounded_request_inputs_reject_nonfinite_and_bool_values(
    max_body_bytes: int,
    overall_timeout: int | float,
    timeout: float | tuple[float, float] | None,
    error_type: type[Exception],
) -> None:
    response, raw = make_response([b"body"])
    client, session, _clock = make_client([response])

    with pytest.raises(error_type):
        client.get_bounded(
            URL,
            max_body_bytes=max_body_bytes,
            overall_timeout=overall_timeout,
            timeout=timeout,
        )

    assert not session.calls
    assert not raw.closed


def test_default_get_keeps_nonstream_transport_behavior() -> None:
    response, raw = make_response([b"not eagerly read"])
    session = ScriptedSession([response])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )

    result = client.get(URL, use_cache=False)

    assert result is response
    assert "stream" not in session.calls[0][2]
    assert raw.read_calls == 0
    assert not raw.closed
