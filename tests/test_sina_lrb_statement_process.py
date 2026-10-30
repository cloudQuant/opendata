"""Offline behavior checks for the fixed Sina LRB process operation."""

from __future__ import annotations

import base64
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter

import opendata.data.providers.sina.lrb_statement_process as process_module
from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpFetchError,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina.lrb_statement_process import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_LRB_PROCESS_PROTOCOL,
    SINA_URL,
    SinaLrbProcessError,
    _authorized_request_context,
    _capture_sina_lrb_response,
    _format_retrieved_at,
    _http_config,
    _new_session,
    _transport_error_code,
    _validated_query,
    build_sina_lrb_query_parameters,
    sina_lrb_request_summary_sha256,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


_RETRIEVED_AT = datetime(2026, 10, 30, 1, 2, 3, 456789, tzinfo=timezone.utc)
_BODY = b'{"result":{"status":{"code":0},"data":{"report_count":"0"}}}'


class _RecordingAdapter(HTTPAdapter):
    """Return a fixed response while recording only the request envelope."""

    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = _BODY,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(max_retries=0)
        self.status = status
        self.body = body
        self.response_headers = headers or {}
        self.calls: list[tuple[str, dict[str, list[str]], dict[str, object]]] = []

    def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        parsed = urlsplit(request.url or "")
        self.calls.append((parsed.netloc, parse_qs(parsed.query), dict(kwargs)))
        response = requests.Response()
        response.status_code = self.status
        response.url = request.url
        response.request = request
        response._content = self.body
        response._content_consumed = True
        response.headers.update(self.response_headers)
        return response


def _client(
    adapter: HTTPAdapter,
    *,
    time_fn: Callable[[], float] | None = None,
) -> tuple[GovernedHttpClient, requests.Session]:
    session = _new_session()
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        _http_config(), session=session, time_fn=time_fn, raw_response_cache=None
    )
    return client, session


def _parameters(symbol: str = "600519.SH") -> dict[str, str]:
    return {"protocol": SINA_LRB_PROCESS_PROTOCOL, "symbol": symbol}


def test_query_parameter_builder_is_the_golden_summary_source() -> None:
    assert build_sina_lrb_query_parameters("600519.SH") == {
        "paperCode": "sh600519",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert build_sina_lrb_query_parameters("000001.SZ") == {
        "paperCode": "sz000001",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert (
        sina_lrb_request_summary_sha256("600519.SH")
        == "3efc9094f5d1bcb908eb943d40dae109f4e571710766edc8bdadeccb64a36b70"
    )
    for invalid in ("600519", "600519.BJ", object()):
        with pytest.raises(SinaLrbProcessError, match="invalid_query"):
            build_sina_lrb_query_parameters(invalid)


def test_fixed_query_accepts_only_canonical_shenzhen_or_shanghai_symbols() -> None:
    assert _validated_query(_parameters()).symbol == "600519.SH"
    assert _validated_query(_parameters("000001.SZ")).symbol == "000001.SZ"

    for invalid in ("600519", "600519.sh", "600519.BJ", " 600519.SH", "Ａ００００１.SZ"):
        with pytest.raises(SinaLrbProcessError) as captured:
            _validated_query(_parameters(invalid))
        assert captured.value.code == "invalid_query"


def test_request_uses_fixed_endpoint_query_and_returns_body_without_parsing() -> None:
    adapter = _RecordingAdapter()
    client, session = _client(adapter)
    call_started = datetime.now(timezone.utc)
    try:
        result = _capture_sina_lrb_response(
            _parameters(), context=_authorized_request_context(), client=client
        )
    finally:
        session.close()
    call_finished = datetime.now(timezone.utc)

    assert len(adapter.calls) == 1
    host, query, kwargs = adapter.calls[0]
    assert host == "quotes.sina.cn"
    assert SINA_URL.startswith("https://quotes.sina.cn/")
    assert query == {
        "paperCode": ["sh600519"],
        "source": ["lrb"],
        "type": ["0"],
        "page": ["1"],
        "num": ["1000"],
    }
    # Requests consumes allow_redirects before calling the mounted adapter.
    # A single adapter call plus the bounded 302 test below proves it was not followed.
    assert "allow_redirects" not in kwargs
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == (3.0, 5.0)
    assert result["query"] == {"symbol": "600519.SH"}
    assert result["status_code"] == 200
    retrieved_at_text = result["retrieved_at_utc"]
    assert isinstance(retrieved_at_text, str)
    retrieved_at = datetime.fromisoformat(retrieved_at_text.replace("Z", "+00:00"))
    assert retrieved_at.tzinfo is timezone.utc
    assert retrieved_at.isoformat(timespec="microseconds").replace("+00:00", "Z") == (
        retrieved_at_text
    )
    assert call_started <= retrieved_at <= call_finished
    assert base64.b64decode(result["body_b64"], validate=True) == _BODY


def test_local_authorization_is_exact_and_limited_to_one_request_in_this_invocation() -> None:
    context = _authorized_request_context()
    budget = context.request_budget
    assert budget is not None
    assert budget.profile is RequestBudgetProfile.BOUNDED_REQUEST
    assert budget.task_attempt_limit == 1
    assert budget.source_attempt_limit == 1
    assert budget.attempts_used == 0
    assert budget.attempts_used_for("sina") == 0
    grant = budget.grants[0]
    assert grant.decision is GrantDecision.ALLOWED
    assert grant.allowed_hosts == frozenset({"quotes.sina.cn"})
    assert grant.rights_evidence == "user_authorized_akshare_data_use"
    assert grant.conditions == ()

    adapter = _RecordingAdapter()
    client, session = _client(adapter)
    try:
        _capture_sina_lrb_response(_parameters(), context=context, client=client)
    finally:
        session.close()

    second_adapter = _RecordingAdapter()
    second_client, second_session = _client(second_adapter)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(_parameters(), context=context, client=second_client)
    finally:
        second_session.close()

    assert captured.value.code == "admission_denied"
    assert len(adapter.calls) == 1
    assert second_adapter.calls == []
    assert budget.attempts_used == 1
    assert budget.attempts_used_for("sina") == 1


def test_local_rate_limiter_denies_an_immediate_second_attempt() -> None:
    adapter = _RecordingAdapter()
    client, session = _client(adapter, time_fn=lambda: 1.0)
    try:
        _capture_sina_lrb_response(
            _parameters(), context=_authorized_request_context(), client=client
        )
        with pytest.raises(SinaLrbProcessError, match="admission_denied"):
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert len(adapter.calls) == 1


def test_unknown_or_wrong_host_grant_rejects_before_transport() -> None:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="sina",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.UNKNOWN,
        rights_evidence="unreviewed_source_rights",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={"example.invalid"},
        expires_at=datetime.max.replace(tzinfo=timezone.utc),
        conditions=("deny until explicitly reviewed",),
        profile=profile,
    )
    context = FetchContext(
        timeout=20.0,
        request_budget=RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(grant,),
            profile=profile,
        ),
        operation=RequestOperation.QUERY,
    )
    adapter = _RecordingAdapter()
    client, session = _client(adapter)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(_parameters(), context=context, client=client)
    finally:
        session.close()

    assert captured.value.code == "admission_denied"
    assert adapter.calls == []


def test_redirect_status_is_rejected_without_following_a_second_request() -> None:
    adapter = _RecordingAdapter(
        status=302,
        body=b"",
        headers={"Location": "https://other-host.invalid/redirected"},
    )
    client, session = _client(adapter)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert captured.value.code == "http_status"
    assert len(adapter.calls) == 1
    assert adapter.response_headers["Location"] == "https://other-host.invalid/redirected"
    assert [call[0] for call in adapter.calls] == ["quotes.sina.cn"]
    assert adapter.calls[0][1]["source"] == ["lrb"]
    assert "allow_redirects" not in adapter.calls[0][2]


def test_response_byte_limit_is_enforced_by_the_bounded_transport() -> None:
    adapter = _RecordingAdapter(body=b"x" * (MAX_RAW_RESPONSE_BYTES + 1))
    client, session = _client(adapter)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert captured.value.code == "response_too_large"
    assert len(adapter.calls) == 1


def test_bounded_capture_rejects_compressed_response_before_reading_body() -> None:
    adapter = _RecordingAdapter(headers={"Content-Encoding": "gzip"})
    client, session = _client(adapter)
    try:
        with pytest.raises(SinaLrbProcessError, match="transport_failed"):
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert len(adapter.calls) == 1


def test_transport_failure_is_translated_to_a_fixed_code() -> None:
    error = HttpFetchError(
        FailureCategory.TIMEOUT,
        url=SINA_URL,
        host="quotes.sina.cn",
        attempts=1,
    )
    assert error.category is FailureCategory.TIMEOUT
    assert _transport_error_code(error) == "deadline_exceeded"
    assert (
        _transport_error_code(
            HttpFetchError(
                FailureCategory.BODY_TOO_LARGE,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=1,
            )
        )
        == "response_too_large"
    )
    assert (
        _transport_error_code(
            HttpFetchError(
                FailureCategory.NETWORK,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=1,
            )
        )
        == "transport_failed"
    )
    assert (
        _transport_error_code(
            HttpFetchError(
                FailureCategory.UNSUPPORTED_CONTENT_ENCODING,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=1,
                status=200,
            )
        )
        == "transport_failed"
    )
    assert _transport_error_code(object()) == "transport_failed"  # type: ignore[arg-type]
    assert (
        _transport_error_code(
            HttpFetchError(
                FailureCategory.RATE_LIMITED,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=0,
            )
        )
        == "admission_denied"
    )
    assert (
        _transport_error_code(
            HttpFetchError(
                FailureCategory.RATE_LIMITED,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=1,
                status=429,
            )
        )
        == "http_status"
    )


def test_fixed_http_config_is_one_attempt_and_conservative_for_this_invocation() -> None:
    config = _http_config()

    assert config.max_attempts == 1
    assert config.rate_limit_per_host == 1.0
    assert config.rate_burst == 1
    assert config.max_concurrency_per_host == 1
    assert config.rate_max_wait == 0.0
    assert config.proxies == {}
    assert config.default_headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize(
    "replacement,expected_code",
    [
        (object(), "transport_failed"),
        (
            BoundedHttpResponse(body=b"ok", status_code=201, retrieved_at=_RETRIEVED_AT),
            "http_status",
        ),
        (
            BoundedHttpResponse(body="not bytes", status_code=200, retrieved_at=_RETRIEVED_AT),
            "transport_failed",
        ),  # type: ignore[arg-type]
        (
            BoundedHttpResponse(
                body=b"x" * (MAX_RAW_RESPONSE_BYTES + 1),
                status_code=200,
                retrieved_at=_RETRIEVED_AT,
            ),
            "response_too_large",
        ),
    ],
)
def test_capture_validates_the_transport_response_envelope(
    replacement: object, expected_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _RecordingAdapter()
    client, session = _client(adapter)
    monkeypatch.setattr(client, "get_bounded", lambda *args, **kwargs: replacement)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert captured.value.code == expected_code
    assert adapter.calls == []


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (RuntimeError("source response data must not be exposed"), "transport_failed"),
        (
            HttpFetchError(
                FailureCategory.NETWORK,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=1,
            ),
            "transport_failed",
        ),
        (
            HttpFetchError(
                FailureCategory.TIMEOUT,
                url=SINA_URL,
                host="quotes.sina.cn",
                attempts=0,
            ),
            "deadline_exceeded",
        ),
    ],
)
def test_capture_translates_transport_errors_without_retaining_details(
    failure: Exception, expected_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _RecordingAdapter()
    client, session = _client(adapter)

    def fail(*args: object, **kwargs: object) -> BoundedHttpResponse:
        raise failure

    monkeypatch.setattr(client, "get_bounded", fail)
    try:
        with pytest.raises(SinaLrbProcessError) as captured:
            _capture_sina_lrb_response(
                _parameters(), context=_authorized_request_context(), client=client
            )
    finally:
        session.close()

    assert captured.value.code == expected_code
    assert str(captured.value) == expected_code
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    "context",
    [
        object(),
        FetchContext(timeout=20.0),
        (
            FetchContext(
                timeout=20.0,
                request_budget=_authorized_request_context().request_budget,
                operation=RequestOperation.STORE,
            )
        ),
    ],
)
def test_capture_rejects_invalid_context_before_transport(
    context: object,
) -> None:
    adapter = _RecordingAdapter()
    client, session = _client(adapter)
    try:
        with pytest.raises(SinaLrbProcessError, match="admission_denied"):
            _capture_sina_lrb_response(
                _parameters(),
                context=context,
                client=client,  # type: ignore[arg-type]
            )
    finally:
        session.close()
    assert adapter.calls == []


def test_capture_rejects_untrusted_http_client_before_transport() -> None:
    with pytest.raises(SinaLrbProcessError, match="admission_denied"):
        _capture_sina_lrb_response(
            _parameters(),
            context=_authorized_request_context(),
            client=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "value",
    [
        None,
        datetime(2026, 10, 30, 1, 2, 3, 456789),
        datetime(2026, 10, 30, 1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_retrieval_timestamp_must_be_an_aware_utc_datetime(value: object) -> None:
    with pytest.raises(SinaLrbProcessError, match="transport_failed"):
        _format_retrieved_at(value)


def test_retrieval_timestamp_hides_timezone_implementation_errors() -> None:
    class _FailingTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            raise RuntimeError("must not escape")

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

    with pytest.raises(SinaLrbProcessError, match="transport_failed"):
        _format_retrieved_at(datetime(2026, 10, 30, tzinfo=_FailingTimezone()))


def test_fetch_wrapper_closes_its_session_after_success_and_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for status, expected_code in ((200, None), (302, "http_status")):
        adapter = _RecordingAdapter(status=status, body=_BODY if status == 200 else b"")
        session = _new_session()
        session.mount("https://", adapter)
        closed = False
        original_close = session.close

        def close(original_close=original_close) -> None:
            nonlocal closed
            closed = True
            original_close()

        monkeypatch.setattr(process_module, "_new_session", lambda session=session: session)
        monkeypatch.setattr(session, "close", close)
        if expected_code is None:
            result = process_module.fetch_sina_lrb_capture(_parameters())
            assert result["status_code"] == 200
        else:
            with pytest.raises(SinaLrbProcessError, match=expected_code):
                process_module.fetch_sina_lrb_capture(_parameters())
        assert closed is True
        assert len(adapter.calls) == 1


def test_expired_deadline_and_cancellation_fail_before_transport() -> None:
    for error_type, context in (
        (
            RequestExecutionDeadlineError,
            FetchContext(
                timeout=20.0,
                _deadline_monotonic=time.monotonic() - 1.0,
                request_budget=_authorized_request_context().request_budget,
                operation=RequestOperation.QUERY,
            ),
        ),
        (
            RequestExecutionCancelledError,
            _cancelled_context(),
        ),
    ):
        adapter = _RecordingAdapter()
        client, session = _client(adapter)
        try:
            with pytest.raises(SinaLrbProcessError) as captured:
                _capture_sina_lrb_response(_parameters(), context=context, client=client)
        finally:
            session.close()

        assert captured.value.code == (
            "deadline_exceeded" if error_type is RequestExecutionDeadlineError else "cancelled"
        )
        assert adapter.calls == []


def _cancelled_context() -> FetchContext:
    cancellation = threading.Event()
    cancellation.set()
    return FetchContext(
        timeout=20.0,
        _thread_cancel_event=cancellation,
        request_budget=_authorized_request_context().request_budget,
        operation=RequestOperation.QUERY,
    )


def test_session_is_direct_unauthenticated_and_retry_free() -> None:
    session = _new_session()
    try:
        assert session.trust_env is False
        assert session.auth is None
        assert session.proxies == {}
        assert "Authorization" not in session.headers
        assert session.headers["Accept-Encoding"] == "identity"
        assert session.cookies.get_dict() == {}
        assert all(adapter.max_retries.total == 0 for adapter in session.adapters.values())
    finally:
        session.close()


def test_request_protocol_rejects_untrusted_transport_controls() -> None:
    invalid_inputs: tuple[Mapping[str, object], ...] = (
        {"protocol": SINA_LRB_PROCESS_PROTOCOL, "symbol": "600519.SH", "url": SINA_URL},
        {"protocol": "other", "symbol": "600519.SH"},
        {"protocol": SINA_LRB_PROCESS_PROTOCOL, "symbol": ["600519.SH"]},
    )
    for invalid in invalid_inputs:
        with pytest.raises(SinaLrbProcessError):
            _validated_query(invalid)  # type: ignore[arg-type]
