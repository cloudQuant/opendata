"""Contract tests for JSON bodies through the governed HTTP transport."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest
import requests
from loguru import logger

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.raw_response_cache import RawResponseCache

if TYPE_CHECKING:
    from pathlib import Path


class FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"response") -> None:
        self.status_code = status_code
        self.content = content
        self.encoding = "utf-8"


class FakeSession:
    def __init__(self, script: list[int | Exception]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if not self.script:
            raise AssertionError("unexpected extra transport call")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class CacheProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, **_kwargs: Any) -> None:
        self.calls.append("get")
        raise AssertionError("POST must not read the raw-response cache")

    def put(self, **_kwargs: Any) -> None:
        self.calls.append("put")
        raise AssertionError("POST must not write the raw-response cache")


def _client(session: Any, **config_kwargs: Any) -> GovernedHttpClient:
    return GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, **config_kwargs),
        session=session,
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
    )


def test_post_sends_json_and_governance_metadata_without_logging_body() -> None:
    secret = "post-body-secret-sentinel"
    session = FakeSession([200])
    client = _client(session)
    events: list[dict[str, Any]] = []
    sink = logger.add(lambda message: events.append(message.record["extra"]), level="INFO")
    payload = {
        "api_key": secret,
        "seriesid": "CPIAUCSL",
        "observations": [None, True, {"value": 2.5, "labels": ["A", "B"]}],
    }
    try:
        response = client.request(
            "POST",
            "https://api.example.test/timeseries",
            json_body=payload,
            headers={"Referer": "https://client.example.test"},
            timeout=(1.25, 4.5),
            source="bls",
        )
    finally:
        logger.remove(sink)

    assert response.status_code == 200
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", "https://api.example.test/timeseries")
    assert kwargs["json"] == payload
    assert "data" not in kwargs
    assert kwargs["headers"]["Referer"] == "https://client.example.test"
    assert kwargs["headers"]["User-Agent"] == "opendata/0.1"
    assert kwargs["timeout"] == (1.25, 4.5)
    event = next(item for item in events if item.get("event") == "governed_http_request")
    assert event["source"] == "bls"
    assert event["endpoint"] == "https://api.example.test/timeseries"
    assert secret not in repr(event)


@pytest.mark.parametrize(
    ("result", "category"),
    [
        (429, FailureCategory.RATE_LIMITED),
        (503, FailureCategory.UPSTREAM_ERROR),
        (requests.Timeout("post-body-secret-sentinel"), FailureCategory.TIMEOUT),
    ],
)
def test_post_failure_statuses_and_timeout_make_one_attempt(
    result: int | Exception, category: FailureCategory
) -> None:
    session = FakeSession([result])
    client = _client(session, max_attempts=4)
    events: list[dict[str, Any]] = []
    sink = logger.add(lambda message: events.append(message.record["extra"]), level="INFO")

    try:
        with pytest.raises(HttpFetchError) as exc_info:
            client.request(
                "POST",
                "https://api.example.test/timeseries",
                json_body={"token": "post-body-secret-sentinel"},
                source="bls",
            )
    finally:
        logger.remove(sink)

    assert exc_info.value.category is category
    assert exc_info.value.attempts == 1
    assert len(session.calls) == 1
    assert "post-body-secret-sentinel" not in str(exc_info.value)
    assert "post-body-secret-sentinel" not in repr(events)


def test_post_does_not_touch_an_explicit_raw_response_cache() -> None:
    cache = CacheProbe()
    session = FakeSession([200])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=cache,  # type: ignore[arg-type]
    )

    client.request("POST", "https://api.example.test/timeseries", json_body={"x": 1})

    assert session.calls[0][2]["json"] == {"x": 1}
    assert cache.calls == []


def test_get_keeps_default_retry_and_raw_response_cache_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class CacheableSession(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.trust_env = False
            self.script = [500, 200]
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
            self.calls.append((method, url, kwargs))
            response = requests.Response()
            response.status_code = self.script.pop(0)
            response._content = b"cached response"
            response.encoding = "utf-8"
            return response

    monkeypatch.setattr(requests, "Session", CacheableSession)
    cache = RawResponseCache(tmp_path / "cache", ttl_seconds=60)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, backoff_base=0, backoff_jitter=0),
        raw_response_cache=cache,
        sleep=lambda _seconds: None,
    )

    assert client.get("https://api.example.test/timeseries", source="bls").status_code == 200
    assert len(client._get_session().calls) == 2
    assert all("json" not in call[2] for call in client._get_session().calls)
    assert client.get("https://api.example.test/timeseries", source="bls").status_code == 200
    assert len(client._get_session().calls) == 2


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_get_and_head_reject_json_body_before_governance_or_io(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession([200])
    cache = CacheProbe()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None),
        session=session,
        raw_response_cache=cache,  # type: ignore[arg-type]
    )
    limiter_calls: list[str] = []
    monkeypatch.setattr(client, "_acquire_token", lambda _host, _url: limiter_calls.append(method))

    with pytest.raises(ValueError, match="json_body"):
        client.request(method, "https://api.example.test/timeseries", json_body={"x": 1})

    assert session.calls == []
    assert limiter_calls == []
    assert cache.calls == []


@pytest.mark.parametrize("bad_kind", ["nan", "infinity", "unsupported", "key", "tuple", "cycle"])
def test_invalid_json_body_is_rejected_before_limiter_or_transport(
    bad_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "invalid-body-secret-sentinel"

    class SecretObject:
        def __repr__(self) -> str:
            return secret

    payload: dict[Any, Any] = {"value": 1}
    if bad_kind == "nan":
        payload["value"] = float("nan")
    elif bad_kind == "infinity":
        payload["value"] = float("inf")
    elif bad_kind == "unsupported":
        payload["value"] = SecretObject()
    elif bad_kind == "key":
        payload = {1: "value"}
    elif bad_kind == "tuple":
        payload["value"] = ("not", "a", "JSON", "array")
    else:
        nested: list[Any] = []
        nested.append(nested)
        payload["value"] = nested

    session = FakeSession([200])
    client = _client(session)
    limiter_calls: list[str] = []
    monkeypatch.setattr(
        client,
        "_acquire_token",
        lambda _host, _url: limiter_calls.append("called"),
    )

    with pytest.raises(ValueError) as exc_info:
        client.request("POST", "https://api.example.test/timeseries", json_body=payload)

    assert session.calls == []
    assert limiter_calls == []
    assert secret not in str(exc_info.value)


def test_nested_payload_is_snapshotted_before_transport() -> None:
    session = FakeSession([200])
    client = _client(session)
    payload = {"items": [{"value": 1}], "enabled": True, "optional": None}

    client.request("POST", "https://api.example.test/timeseries", json_body=payload)
    payload["items"][0]["value"] = 9
    payload["items"].append({"value": 10})

    assert session.calls[0][2]["json"] == {
        "items": [{"value": 1}],
        "enabled": True,
        "optional": None,
    }


def test_injected_session_governance_lock_serializes_different_hosts() -> None:
    first_entered = threading.Event()
    second_token_acquired = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    active = 0
    peak_active = 0

    class BlockingSession:
        def request(self, _method: str, url: str, **_kwargs: Any) -> FakeResponse:
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            if url.endswith("first"):
                first_entered.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("first request was not released")
            else:
                second_entered.set()
            with state_lock:
                active -= 1
            return FakeResponse(200)

    client = _client(BlockingSession(), max_concurrency_per_host=2)
    acquire_token = client._acquire_token

    def signal_second_token(host: str, url: str) -> None:
        acquire_token(host, url)
        if url.endswith("second"):
            second_token_acquired.set()

    client._acquire_token = signal_second_token  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.get, "https://first.example.test/first")
            assert first_entered.wait(timeout=1)
            second = pool.submit(client.get, "https://second.example.test/second")
            assert second_token_acquired.wait(timeout=1)
            assert not second_entered.wait(timeout=0.05)
            release_first.set()
            assert first.result(timeout=1).status_code == 200
            assert second.result(timeout=1).status_code == 200
    finally:
        release_first.set()

    assert peak_active == 1


def test_none_json_body_is_omitted_for_legacy_fake_sessions() -> None:
    session = FakeSession([200])

    _client(session).request("POST", "https://api.example.test/timeseries")

    assert "json" not in session.calls[0][2]
