"""Offline proof that the macro runtime seams use governed transport."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from loguru import logger

from opendata.data.http_client import (
    GovernedHttpClient,
    HttpClientConfig,
)


class _Response:
    def __init__(self, status_code: int = 200, text: str = "body") -> None:
        self.status_code = status_code
        self.text = text


class _Session:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return _Response(self.statuses.pop(0))


_PROVIDERS = (
    ("ecb", "opendata.data.providers.ecb.models._client", True),
    ("fred", "opendata.data.providers.fred.models._client", True),
    ("imf", "opendata.data.providers.imf.models._client", False),
    ("oecd", "opendata.data.providers.oecd.models._client", True),
)


def test_macro_providers_resolve_to_one_shared_governance_client() -> None:
    clients = [
        importlib.import_module(module_name).get_shared_http_client()
        for _source, module_name, _has_params in _PROVIDERS
    ]

    assert all(client is clients[0] for client in clients)


@pytest.mark.parametrize(("source", "module_name", "has_params"), _PROVIDERS)
def test_macro_runtime_seams_use_governed_client(
    monkeypatch: pytest.MonkeyPatch, source: str, module_name: str, has_params: bool
) -> None:
    module = importlib.import_module(module_name)
    session = _Session()
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1), session=session
    )
    monkeypatch.setattr(module, "get_shared_http_client", lambda: client)
    url = f"https://{source}.example/data?url_secret=never-log-this"
    params = {"api_key": "query-secret", "series_id": "cpi"}

    if has_params:
        status, text = module._http_get(url, params, 4.25)
    else:
        status, text = module._http_get(url, 4.25)

    assert (status, text) == (200, "body")
    method, called_url, kwargs = session.calls[0]
    assert (method, called_url) == ("GET", url)
    assert kwargs["timeout"] == 4.25
    assert kwargs["params"] == (params if has_params else None)


@pytest.mark.parametrize(("source", "module_name", "has_params"), _PROVIDERS)
def test_macro_http_errors_keep_provider_code_and_safe_attribution(
    monkeypatch: pytest.MonkeyPatch, source: str, module_name: str, has_params: bool
) -> None:
    module = importlib.import_module(module_name)
    session = _Session([503])
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1), session=session
    )
    monkeypatch.setattr(module, "get_shared_http_client", lambda: client)
    url = f"https://{source}.example/data?api_key=query-secret"
    params = {"api_key": "query-secret"}

    error_type = getattr(module, f"{source.title()}ProviderError")
    with pytest.raises(error_type) as exc_info:
        if has_params:
            module._http_get(url, params, None)
        else:
            module._http_get(url, None)

    assert exc_info.value.code == f"{source.upper()}_HTTP_ERROR"
    assert exc_info.value.status == 503
    assert exc_info.value.url == f"https://{source}.example/data"
    assert "query-secret" not in str(exc_info.value)


def test_structured_events_correlate_retries_and_redact_credentials() -> None:
    session = _Session([500, 200])
    client = GovernedHttpClient(
        HttpClientConfig(
            rate_limit_per_host=None,
            max_attempts=2,
            backoff_base=0.0,
            backoff_jitter=0.0,
        ),
        session=session,
        sleep=lambda _seconds: None,
        time_fn=lambda: 10.0,
        jitter_fn=lambda: 0.0,
    )
    events: list[dict[str, Any]] = []
    sink = logger.add(lambda message: events.append(message.record["extra"]), level="INFO")
    try:
        client.get(
            "https://example.test/observations?token=url-secret",
            params={"api_key": "query-secret", "series_id": "CPI"},
            source="fred",
        )
    finally:
        logger.remove(sink)

    request_events = [event for event in events if event.get("event") == "governed_http_request"]
    assert [event["attempt"] for event in request_events] == [1, 2]
    assert [event["failure_category"] for event in request_events] == ["upstream_error", None]
    assert request_events[0]["request_id"] == request_events[1]["request_id"]
    assert request_events[0]["source"] == "fred"
    assert request_events[0]["endpoint"] == "https://example.test/observations"
    assert request_events[0]["parameter_summary"] == {
        "keys": ("api_key", "series_id"),
        "redacted_keys": ("api_key",),
    }
    assert request_events[0]["elapsed_seconds"] >= 0.0
    assert "url-secret" not in repr(request_events)
    assert "query-secret" not in repr(request_events)
