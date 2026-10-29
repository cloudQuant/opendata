"""Behavior checks for the governed client's one-shot bounded form POST."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import TYPE_CHECKING, cast

import pytest
import requests
from loguru import logger
from requests.structures import CaseInsensitiveDict
from urllib3.util.retry import Retry

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAdapterRetryError,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

URL = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
SOURCE = "akshare"
MODEL = "BSEInstrumentCatalog"


class _FakeRaw:
    """Bounded response stream that records reads and closure."""

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        clock: _FakeClock | None = None,
        advance_per_chunk: float = 0.0,
    ) -> None:
        self.chunks = list(chunks)
        self.clock = clock
        self.advance_per_chunk = advance_per_chunk
        self.read_calls = 0
        self.closed = False

    def stream(self, _chunk_size: int, decode_content: bool = False) -> Iterable[bytes]:
        assert decode_content is True
        for chunk in self.chunks:
            self.read_calls += 1
            if self.clock is not None:
                self.clock.now += self.advance_per_chunk
            yield chunk

    def close(self) -> None:
        self.closed = True


class _FakeClock:
    """Monotonic clock advanced explicitly by response reads."""

    def __init__(self) -> None:
        self.now = 100.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _DuplicateForm(dict[str, str]):
    """Form mapping whose item stream contains the same key twice."""

    def items(self) -> list[tuple[str, str]]:
        return [("page", "0"), ("page", "1")]


class _DuplicateHeaders(dict[str, str]):
    """Header mapping whose item stream contains case-insensitive duplicates."""

    def items(self) -> list[tuple[str, str]]:
        return [("Referer", "first"), ("referer", "second")]


class _ExplodingItems(dict[str, str]):
    """Mapping that fails while yielding items, with an unsafe diagnostic."""

    def items(self) -> list[tuple[str, str]]:
        raise RuntimeError("private-form-value")


def _response(
    body: Iterable[bytes] = (b"ok",),
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    clock: _FakeClock | None = None,
    advance_per_chunk: float = 0.0,
) -> tuple[requests.Response, _FakeRaw]:
    raw = _FakeRaw(body, clock=clock, advance_per_chunk=advance_per_chunk)
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict(headers or {})
    response.raw = raw  # type: ignore[assignment]
    response._content = False
    response._content_consumed = False
    response.url = URL
    return response, raw


def _install_replay(
    monkeypatch: pytest.MonkeyPatch,
    responses: Iterable[requests.Response],
) -> list[tuple[requests.PreparedRequest, dict[str, object]]]:
    """Replay responses below an unmodified Requests session pipeline."""
    queued = list(responses)
    calls: list[tuple[requests.PreparedRequest, dict[str, object]]] = []

    def send(
        adapter: requests.adapters.HTTPAdapter,
        prepared: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        calls.append((prepared, dict(kwargs)))
        if not queued:
            raise AssertionError("unexpected physical transport attempt")
        response = queued.pop(0)
        response.request = prepared
        response.url = prepared.url or URL
        response.connection = adapter
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


def _new_client(*, clock: _FakeClock | None = None) -> GovernedHttpClient:
    session = _form_session()
    return GovernedHttpClient(
        HttpClientConfig(
            max_attempts=4,
            rate_limit_per_host=None,
            connect_timeout=3.0,
            read_timeout=9.0,
        ),
        session=session,
        sleep=clock.sleep if clock is not None else lambda _seconds: None,
        time_fn=clock.time if clock is not None else lambda: 100.0,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )


def _form_session() -> requests.Session:
    """Build an injected session with the bounded form transport defaults."""
    session = requests.Session()
    session.trust_env = False
    session.headers["Accept-Encoding"] = "identity"
    session.headers["Content-Type"] = "application/x-www-form-urlencoded"
    return session


def _budget(*, attempts: int = 1) -> RequestBudget:
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic bounded POST test grant",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"www.bse.cn"},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return RequestBudget(task_attempts=attempts, source_attempts=attempts, grants=(grant,))


def _post(
    client: GovernedHttpClient,
    budget: RequestBudget,
    form: object,
    *,
    max_form_bytes: object = 1024,
    max_body_bytes: object = 64,
    overall_timeout: object = 10.0,
    headers: object | None = None,
) -> BoundedHttpResponse:
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        return client.post_form_bounded(
            URL,
            cast("Mapping[str, str]", form),
            max_form_bytes=cast("int", max_form_bytes),
            max_body_bytes=cast("int", max_body_bytes),
            overall_timeout=cast("float", overall_timeout),
            headers=cast("Mapping[str, str] | None", headers),
            source=SOURCE,
        )


def test_form_post_sends_encoded_body_once_with_safe_bounded_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, _raw = _response([b"accepted"], headers={"Content-Length": "8"})
    calls = _install_replay(monkeypatch, [response])
    client = _new_client()
    budget = _budget()

    result = _post(
        client,
        budget,
        {"page": "0", "typejb": "T", "xxzqdm": "A B/中"},
        headers={"Referer": "https://www.bse.cn/"},
    )

    assert result.body == b"accepted"
    assert result.status_code == 200
    assert len(calls) == 1
    prepared, kwargs = calls[0]
    assert prepared.method == "POST"
    assert prepared.body == b"page=0&typejb=T&xxzqdm=A+B%2F%E4%B8%AD"
    assert prepared.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert prepared.headers["Accept-Encoding"] == "identity"
    assert prepared.headers["Referer"] == "https://www.bse.cn/"
    assert kwargs["stream"] is True
    assert budget.attempts_used == 1


def test_form_values_are_not_written_to_transport_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, _raw = _response([b"ok"], headers={"Content-Length": "2"})
    _install_replay(monkeypatch, [response])
    output = StringIO()
    sink_id = logger.add(output, format="{message}")
    try:
        _post(_new_client(), _budget(), {"query": "private-form-value"})
    finally:
        logger.remove(sink_id)

    assert "private-form-value" not in output.getvalue()
    assert "query=private-form-value" not in output.getvalue()


def test_encoded_form_limit_fails_before_budget_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = _new_client()
    budget = _budget()

    with pytest.raises(ValueError) as error:
        _post(client, budget, {"field": "too long"}, max_form_bytes=5)

    assert "too long" not in str(error.value)
    assert budget.attempts_used == 0
    assert calls == []


@pytest.mark.parametrize(
    ("limit_name", "value", "error_type", "message"),
    [
        ("max_form_bytes", True, TypeError, "exact int"),
        ("max_form_bytes", 0, ValueError, "must be positive"),
        ("max_form_bytes", 1024 * 1024 + 1, ValueError, "local safety limit"),
        ("max_body_bytes", True, TypeError, "exact int"),
        ("max_body_bytes", 0, ValueError, "must be positive"),
    ],
)
def test_post_rejects_invalid_explicit_byte_limits_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = _new_client()
    budget = _budget()
    limit = {limit_name: value}

    with pytest.raises(error_type, match=message):
        _post(client, budget, {"page": "0"}, **limit)

    assert calls == []
    assert budget.attempts_used == 0


def test_post_rejects_overflowed_overall_deadline_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=_form_session(),
        time_fn=lambda: 1e308,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(ValueError, match="invalid deadline"):
        _post(client, budget, {"page": "0"}, overall_timeout=1e308)

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("form", "max_form_bytes", "error_type", "message", "secret"),
    [
        (None, 1024, TypeError, "mapping of strings", None),
        ({"page": 1}, 1024, ValueError, "exact strings", None),
        ({1: "page"}, 1024, ValueError, "exact strings", None),
        (_DuplicateForm(), 1024, ValueError, "duplicate field names", None),
        ({"x": "\u4e2d"}, 2, ValueError, "field exceeds its byte limit", None),
        ({"x": "a b"}, 4, ValueError, "encoded form exceeds max_form_bytes", None),
        (_ExplodingItems(), 1024, ValueError, "could not be encoded safely", "private-form-value"),
    ],
)
def test_post_rejects_malformed_or_oversized_form_without_send(
    monkeypatch: pytest.MonkeyPatch,
    form: object,
    max_form_bytes: int,
    error_type: type[Exception],
    message: str,
    secret: str | None,
) -> None:
    calls = _install_replay(monkeypatch, [])
    budget = _budget()

    with pytest.raises(error_type, match=message) as error:
        _post(_new_client(), budget, form, max_form_bytes=max_form_bytes)

    if secret is not None:
        assert secret not in str(error.value)
    assert calls == []
    assert budget.attempts_used == 0


def test_post_rejects_form_with_more_than_the_field_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    form = {f"field-{index}": "x" for index in range(257)}
    budget = _budget()

    with pytest.raises(ValueError, match="too many fields"):
        _post(_new_client(), budget, form, max_form_bytes=1024 * 1024)

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("headers", "error_type", "message", "secret"),
    [
        ("not a mapping", TypeError, "mapping of strings", None),
        ({"Referer": 7}, ValueError, "exact strings", None),
        (_DuplicateHeaders(), ValueError, "duplicate headers", None),
        ({"X-Injected": "value"}, ValueError, "unsupported header", None),
        ({"": "value"}, ValueError, "malformed", None),
        ({"Réferer": "value"}, ValueError, "malformed", None),
        ({"Referer": "caf\u00e9"}, ValueError, "malformed", None),
        ({"Referer": "first\r\nsecond"}, ValueError, "malformed", None),
        (_ExplodingItems(), ValueError, "could not be copied safely", "private-form-value"),
    ],
)
def test_post_rejects_malformed_or_unsafe_caller_headers_without_send(
    monkeypatch: pytest.MonkeyPatch,
    headers: object,
    error_type: type[Exception],
    message: str,
    secret: str | None,
) -> None:
    calls = _install_replay(monkeypatch, [])
    budget = _budget()

    with pytest.raises(error_type, match=message) as error:
        _post(_new_client(), budget, {"page": "0"}, headers=headers)

    if secret is not None:
        assert secret not in str(error.value)
    assert calls == []
    assert budget.attempts_used == 0


def test_transport_headers_cannot_override_auth_framing_or_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = _new_client()
    budget = _budget()

    with pytest.raises(ValueError) as error:
        _post(
            client,
            budget,
            {"field": "private-header-value"},
            headers={"Authorization": "private-header-value"},
        )

    assert "private-header-value" not in str(error.value)
    assert budget.attempts_used == 0
    assert calls == []


def test_default_form_session_is_created_with_identity_and_form_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, _raw = _response([b"ok"], headers={"Content-Length": "2"})
    calls = _install_replay(monkeypatch, [response])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        raw_response_cache=None,
    )
    budget = _budget()

    result = _post(client, budget, {"page": "0"})

    assert result.body == b"ok"
    assert len(calls) == 1
    prepared, _kwargs = calls[0]
    assert prepared.headers["Accept-Encoding"] == "identity"
    assert prepared.headers["Content-Type"] == "application/x-www-form-urlencoded"
    form_session = client._thread_local.form_session
    assert form_session.trust_env is False
    assert form_session.headers["Accept-Encoding"] == "identity"
    assert form_session.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert budget.attempts_used == 1


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Accept-Encoding", "gzip"),
        ("Content-Type", "application/json"),
    ],
)
def test_conflicting_session_defaults_fail_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    value: str,
) -> None:
    calls = _install_replay(monkeypatch, [])
    session = _form_session()
    session.headers[header] = cast("str", value)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAuthorizationError):
        _post(client, budget, {"page": "0"})

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("header", "value", "message"),
    [
        ("Referer", 7, "malformed session headers"),
        ("X-Injected", "value", "unapproved session headers"),
        ("Authorization", "hidden", "authentication or framing headers"),
    ],
)
def test_form_post_rejects_unsafe_or_malformed_session_headers(
    monkeypatch: pytest.MonkeyPatch,
    header: str,
    value: object,
    message: str,
) -> None:
    calls = _install_replay(monkeypatch, [])
    session = _form_session()
    session.headers[header] = value
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAuthorizationError, match=message):
        _post(client, budget, {"page": "0"})

    assert calls == []
    assert budget.attempts_used == 0


def test_form_post_rejects_retry_subclass_on_standard_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])

    class RetrySubclass(Retry):
        """Retry subclass with a zero retry budget, rejected by exact-type policy."""

    session = _form_session()
    session.mount(
        "https://",
        requests.adapters.HTTPAdapter(max_retries=RetrySubclass(total=0)),
    )
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAdapterRetryError, match="retries disabled"):
        _post(client, budget, {"page": "0"})

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize("hidden_state", ["default_params", "custom_cookie_jar"])
def test_form_post_rejects_hidden_session_query_or_cookie_state(
    monkeypatch: pytest.MonkeyPatch,
    hidden_state: str,
) -> None:
    calls = _install_replay(monkeypatch, [])
    session = _form_session()
    if hidden_state == "default_params":
        session.params["token"] = "must-not-be-added-to-url"
    else:

        class LyingCookieJar(requests.cookies.RequestsCookieJar):
            """Cookie jar that can yield a cookie while reporting an empty length."""

            def __len__(self) -> int:
                return 0

            def __iter__(self):
                return iter([requests.cookies.create_cookie("token", "hidden")])

        session.cookies = LyingCookieJar()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAuthorizationError):
        _post(client, budget, {"page": "0"})

    assert calls == []
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (429, FailureCategory.RATE_LIMITED),
        (503, FailureCategory.UPSTREAM_ERROR),
    ],
)
def test_post_status_failure_is_single_attempt_even_when_gets_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    category: FailureCategory,
) -> None:
    response, raw = _response([b"not consumed"], status=status)
    calls = _install_replay(monkeypatch, [response])

    with pytest.raises(HttpFetchError) as error:
        _post(_new_client(), _budget(), {"page": "0"})

    assert error.value.category is category
    assert error.value.status == status
    assert error.value.attempts == 1
    assert error.value.retryable is False
    assert len(calls) == 1
    assert raw.read_calls == 0
    assert raw.closed


def test_post_timeout_after_send_is_not_signaled_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[requests.PreparedRequest] = []

    def timeout_after_send(
        _adapter: requests.adapters.HTTPAdapter,
        prepared: requests.PreparedRequest,
        **_kwargs: object,
    ) -> requests.Response:
        calls.append(prepared)
        raise requests.Timeout("synthetic ambiguous POST outcome")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", timeout_after_send)
    client = _new_client()
    budget = _budget()

    with pytest.raises(HttpFetchError) as error:
        _post(client, budget, {"page": "0"})

    assert error.value.category is FailureCategory.TIMEOUT
    assert error.value.attempts == 1
    assert error.value.retryable is False
    assert len(calls) == 1
    assert budget.attempts_used == 1


def test_redirect_is_rejected_without_following_or_reading_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, raw = _response(
        [b"redirect body"],
        status=302,
        headers={"Location": "https://other.invalid/"},
    )
    calls = _install_replay(monkeypatch, [response])

    with pytest.raises(HttpFetchError) as error:
        _post(_new_client(), _budget(), {"page": "0"})

    assert error.value.category is FailureCategory.REDIRECT
    assert error.value.status == 302
    assert len(calls) == 1
    assert raw.read_calls == 0
    assert raw.closed


def test_informational_response_is_not_returned_as_bounded_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, raw = _response([b"protocol switch"], status=101)
    calls = _install_replay(monkeypatch, [response])

    with pytest.raises(HttpFetchError) as error:
        _post(_new_client(), _budget(), {"page": "0"})

    assert error.value.category is FailureCategory.UPSTREAM_ERROR
    assert error.value.status == 101
    assert error.value.retryable is False
    assert len(calls) == 1
    assert raw.read_calls == 0
    assert raw.closed


def test_non_identity_response_encoding_fails_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, raw = _response(
        [b"compressed"],
        headers={"Content-Encoding": "gzip", "Content-Length": "10"},
    )
    calls = _install_replay(monkeypatch, [response])

    with pytest.raises(HttpFetchError) as error:
        _post(_new_client(), _budget(), {"page": "0"})

    assert error.value.category is FailureCategory.UNSUPPORTED_CONTENT_ENCODING
    assert len(calls) == 1
    assert raw.read_calls == 0
    assert raw.closed


def test_actual_stream_bytes_cannot_exceed_response_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, raw = _response([b"1234", b"56", b"unread"])
    calls = _install_replay(monkeypatch, [response])
    client = _new_client()
    budget = _budget()

    with pytest.raises(HttpFetchError) as error:
        _post(client, budget, {"page": "0"}, max_body_bytes=5)

    assert error.value.category is FailureCategory.BODY_TOO_LARGE
    assert error.value.attempts == 1
    assert len(calls) == 1
    assert raw.read_calls == 2
    assert raw.closed
    assert budget.attempts_used == 1


def test_overall_deadline_is_checked_between_response_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    response, raw = _response(
        [b"first", b"second"],
        clock=clock,
        advance_per_chunk=0.6,
    )
    calls = _install_replay(monkeypatch, [response])
    client = _new_client(clock=clock)
    budget = _budget()

    with (
        pytest.raises(HttpFetchError) as error,
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
    ):
        client.post_form_bounded(
            URL,
            {"page": "0"},
            max_form_bytes=1024,
            max_body_bytes=64,
            overall_timeout=0.5,
            source=SOURCE,
        )

    assert error.value.category is FailureCategory.TIMEOUT
    assert len(calls) == 1
    assert raw.read_calls == 1
    assert raw.closed
    assert calls[0][1]["timeout"] == (0.5, 0.5)


def test_post_requires_authorized_exact_host_and_query_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = _new_client()
    budget = _budget()

    with (
        pytest.raises(RequestAuthorizationError),
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
    ):
        client.post_form_bounded(
            "https://other.invalid/form",
            {"page": "0"},
            max_form_bytes=1024,
            max_body_bytes=64,
            overall_timeout=1.0,
            source=SOURCE,
        )

    assert calls == []
    assert budget.attempts_used == 0


def test_post_without_active_scope_does_not_create_transport_session() -> None:
    client = GovernedHttpClient(HttpClientConfig(rate_limit_per_host=None))

    with pytest.raises(RequestAuthorizationError):
        client.post_form_bounded(
            URL,
            {"page": "0"},
            max_form_bytes=1024,
            max_body_bytes=64,
            overall_timeout=1.0,
            source=SOURCE,
        )

    assert not hasattr(client._thread_local, "form_session")


def test_post_rejects_non_requests_form_session_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=cast("requests.Session", object()),
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAuthorizationError, match="standard Requests session"):
        _post(client, budget, {"page": "0"})

    assert calls == []
    assert budget.attempts_used == 0


def test_post_rejects_source_attempt_binding_without_durable_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_replay(monkeypatch, [])
    client = _new_client()
    budget = _budget()
    binding = SourceAttemptBinding(
        grant_id="grant-1",
        expected_revision=1,
        principal_scope="local-test",
    )

    with (
        pytest.raises(RequestAuthorizationError, match="requires a configured durable gate"),
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
        ),
    ):
        client.post_form_bounded(
            URL,
            {"page": "0"},
            max_form_bytes=1024,
            max_body_bytes=64,
            overall_timeout=1.0,
            source=SOURCE,
        )

    assert calls == []
    assert budget.attempts_used == 0
    assert not hasattr(client._thread_local, "form_session")


def test_request_budget_prevents_a_second_post_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_response, _raw = _response([b"ok"], headers={"Content-Length": "2"})
    calls = _install_replay(monkeypatch, [first_response])
    client = _new_client()
    budget = _budget(attempts=1)

    _post(client, budget, {"page": "0"})
    with pytest.raises(RequestAttemptLimitError, match="request attempt allowance exhausted"):
        _post(client, budget, {"page": "1"})

    assert len(calls) == 1
    assert budget.attempts_used == 1


@pytest.mark.parametrize("override_kind", ["adapter_subclass", "instance_send_override"])
def test_form_post_rejects_transport_adapters_that_can_hide_attempts(
    monkeypatch: pytest.MonkeyPatch,
    override_kind: str,
) -> None:
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter,
        "send",
        lambda *_args, **_kwargs: pytest.fail("rejected adapter must not send"),
    )
    session = _form_session()
    if override_kind == "adapter_subclass":

        class CustomAdapter(requests.adapters.HTTPAdapter):
            """Adapter subclass that the form transport must reject."""

        adapter = CustomAdapter(max_retries=0)
    else:
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        adapter.send = lambda *_args, **_kwargs: pytest.fail("must not send")  # type: ignore[method-assign]
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    budget = _budget()

    with pytest.raises(RequestAuthorizationError):
        _post(client, budget, {"page": "0"})

    assert budget.attempts_used == 0
