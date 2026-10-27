"""Offline transport boundary tests for the governed HTTP client."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
import requests

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.raw_response_cache import CachedRawResponse, RawResponseCache
from opendata.data.request_budget import (
    GrantDecision,
    RequestAdapterRetryError,
    RequestAuthorizationError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from tests.test_http_client_streaming import (
    FakeClock,
    GuardedResponse,
    QueuedResponseAdapter,
    SyntheticRaw,
    make_client,
    make_response,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import Any

URL = "https://api.example.test/archive"
SOURCE = "http_boundary_test"
MODEL = "ArchiveFetcher"


def make_budget() -> RequestBudget:
    """Grant one offline request to the test host."""
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-evidence:offline-fixture",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={"api.example.test"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))


def make_plain_response(status_code: int = 200, body: bytes = b"ok") -> requests.Response:
    """Return a fully consumed response for ordinary Requests behavior."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response._content_consumed = True
    response.headers["Content-Length"] = str(len(body))
    return response


def make_session(
    responses: list[requests.Response],
) -> tuple[requests.Session, QueuedResponseAdapter]:
    """Mount a socket-free response adapter on a standard Requests session."""
    session = requests.Session()
    session.trust_env = False
    adapter = QueuedResponseAdapter(responses)
    session.mount("https://", adapter)
    return session, adapter


class CountingRawResponseCache(RawResponseCache):
    """Count cache operations without touching the filesystem."""

    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0

    def get(
        self,
        *,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> CachedRawResponse | None:
        self.reads += 1
        return None

    def put(
        self,
        *,
        method: str,
        source: str,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        status_code: int,
        content: bytes,
        encoding: str | None = None,
    ) -> bool:
        self.writes += 1
        return True


@pytest.mark.parametrize(
    ("url", "host", "expected_url"),
    [
        (
            "https://user:syntheticpassword@[2001:db8::1]:443/archive"
            "?token=synthetic-query-secret#synthetic-fragment",
            "2001:db8::1",
            "https://[2001:db8::1]:443/archive",
        ),
        (
            "https://user:syntheticpassword@api.example.test:not-a-port/archive"
            "?token=synthetic-query-secret#synthetic-fragment",
            "api.example.test",
            "https://api.example.test/archive",
        ),
    ],
)
def test_http_fetch_error_uses_safe_endpoint_golden_vectors(
    url: str,
    host: str,
    expected_url: str,
) -> None:
    error = HttpFetchError(FailureCategory.NETWORK, url=url, host=host, attempts=1)

    assert error.url == expected_url
    assert str(error) == (f"[network] host={host} status=- attempts=1 url={expected_url}")
    for secret in ("syntheticpassword", "synthetic-query-secret", "synthetic-fragment"):
        assert secret not in str(error)
        assert secret not in repr(error)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test/archive",
        "https://user:secret@api.example.test/archive",
        "https://api.example.test:8443/archive",
        "https://api.example.test:invalid/archive",
        "https://other.example.test/archive",
    ],
)
def test_scoped_requests_reject_urls_outside_the_exact_https_origin_before_transport(
    url: str,
) -> None:
    session, adapter = make_session([make_plain_response()])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = make_budget()

    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError),
    ):
        client.get(url, source=SOURCE)

    assert adapter.calls == []
    assert budget.attempts_used == 0
    session.close()


def test_post_json_is_snapshotted_and_sent_as_standard_json() -> None:
    body = {"query": {"symbols": ["AAPL"], "limit": 2}}

    class CapturingAdapter(QueuedResponseAdapter):
        def __init__(self, responses: list[requests.Response]) -> None:
            super().__init__(responses)
            self.prepared_bodies: list[bytes | str | None] = []

        def send(
            self,
            request: requests.PreparedRequest,
            **kwargs: object,
        ) -> requests.Response:
            self.prepared_bodies.append(request.body)
            return super().send(request, **kwargs)

    class MutatingSession(requests.Session):
        def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
            body["query"]["symbols"].append("MSFT")
            return super().request(method, url, **kwargs)

    session = MutatingSession()
    session.trust_env = False
    adapter = CapturingAdapter([make_plain_response(status_code=201)])
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )

    response = client.request("POST", URL, json_body=body, use_cache=False)

    assert response.status_code == 201
    assert len(adapter.calls) == 1
    assert adapter.prepared_bodies == [b'{"query": {"symbols": ["AAPL"], "limit": 2}}']
    assert body["query"]["symbols"] == ["AAPL", "MSFT"]
    session.close()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_json_body_is_rejected_for_bodyless_methods_before_transport(method: str) -> None:
    session, adapter = make_session([make_plain_response()])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(ValueError, match="json_body is not allowed"):
        client.request(method, URL, json_body={"query": "archive"}, use_cache=False)

    assert adapter.calls == []
    session.close()


def test_cyclic_json_body_is_rejected_without_leaking_to_transport() -> None:
    session, adapter = make_session([make_plain_response()])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    body: dict[str, object] = {}
    body["self"] = body

    with pytest.raises(ValueError, match="finite JSON object"):
        client.request("POST", URL, json_body=body, use_cache=False)

    assert adapter.calls == []
    session.close()


def test_ordinary_duck_session_stays_compatible_with_bounded_session_guard() -> None:
    class DuckSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def request(self, method: str, url: str, **kwargs: object) -> requests.Response:
            self.calls.append((method, url, dict(kwargs)))
            return make_plain_response()

    session = DuckSession()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )

    response = client.get(URL, use_cache=False)
    assert response.content == b"ok"
    assert len(session.calls) == 1

    with pytest.raises(RequestAuthorizationError, match="standard requests.Session"):
        client.get_bounded(URL, max_body_bytes=8, overall_timeout=5.0)

    assert len(session.calls) == 1


def test_bounded_requests_reject_nested_adapter_retries_before_send() -> None:
    response, _raw = make_response([b"unused"])
    session, adapter = make_session([response])
    adapter.max_retries = requests.adapters.Retry(total=0, status=1)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(RequestAdapterRetryError, match="retries disabled"):
        client.get_bounded(URL, max_body_bytes=16, overall_timeout=5.0)

    assert adapter.calls == []
    session.close()


def test_bounded_request_deadline_that_cannot_fit_retry_stops_after_first_attempt() -> None:
    clock = FakeClock()
    response, raw = make_response(raw=SyntheticRaw([b"prefix", b"tail"], fail_after_reads=2))
    client, adapter, _clock = make_client(
        [response],
        clock=clock,
        config=HttpClientConfig(
            max_attempts=2,
            rate_limit_per_host=None,
            backoff_base=0.5,
            backoff_jitter=0.0,
        ),
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=32, overall_timeout=0.25)

    assert error.value.category is FailureCategory.NETWORK
    assert error.value.attempts == 1
    assert len(adapter.calls) == 1
    assert clock.sleeps == []
    assert raw.closed


def test_bounded_deadline_expiring_before_first_attempt_uses_zero_attempts() -> None:
    times = iter((100.0, 100.0, 101.0))
    session, adapter = make_session([make_plain_response()])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        time_fn=lambda: next(times, 101.0),
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=8, overall_timeout=1.0)

    assert error.value.category is FailureCategory.TIMEOUT
    assert error.value.attempts == 0
    assert adapter.calls == []
    session.close()


def test_bounded_finite_timeout_that_overflows_deadline_is_rejected_before_transport() -> None:
    session, adapter = make_session([make_plain_response()])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        time_fn=lambda: 1e308,
        raw_response_cache=None,
    )

    with pytest.raises(ValueError, match="invalid deadline"):
        client.get_bounded(URL, max_body_bytes=8, overall_timeout=1e308)

    assert adapter.calls == []
    session.close()


def test_stream_accepts_exact_byte_cap_and_ignores_non_ascii_content_length() -> None:
    response, raw = make_response(
        [b"abc"],
        headers={"Content-Length": "３", "Content-Encoding": "Identity"},
    )
    client, adapter, _clock = make_client([response])

    result = client.get_bounded(URL, max_body_bytes=3, overall_timeout=5.0)

    assert result.body == b"abc"
    assert result.status_code == 200
    assert len(adapter.calls) == 1
    assert raw.read_calls == 1
    assert raw.closed
    assert response.close_called


def test_non_byte_response_chunk_is_classified_and_response_is_closed() -> None:
    class TextStreamResponse(GuardedResponse):
        def iter_content(
            self,
            chunk_size: int = 1,
            decode_unicode: bool = False,
        ) -> Iterator[object]:
            del chunk_size, decode_unicode
            yield "not bytes"

    response = TextStreamResponse()
    raw = SyntheticRaw()
    response.raw = raw
    response.status_code = 200
    response.url = URL
    session, adapter = make_session([response])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        session=session,
        raw_response_cache=None,
    )

    with pytest.raises(HttpFetchError) as error:
        client.get_bounded(URL, max_body_bytes=16, overall_timeout=5.0)

    assert error.value.category is FailureCategory.NETWORK
    assert error.value.status == 200
    assert error.value.attempts == 1
    assert len(adapter.calls) == 1
    assert response.close_called
    assert raw.closed


def test_injected_session_bypasses_raw_cache_identity() -> None:
    response = make_plain_response(body=b"transport")
    session, adapter = make_session([response])
    cache = CountingRawResponseCache()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=cache,
    )

    fetched = client.get(URL)

    assert fetched.content == b"transport"
    assert len(adapter.calls) == 1
    assert cache.reads == 0
    assert cache.writes == 0
    session.close()
