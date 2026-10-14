"""Offline source-byte and service checks for Cboe's linked daily-history CSVs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.cboe_index_historical import CboeIndexHistorical
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.cboe.models import index_historical_public as history
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CAPTURE_PATH = (
    _REPOSITORY_ROOT / "docs/evidence/C100/congress-cboe/captures/vix-history-20261012.csv"
)
_HANDOFF_PATH = _REPOSITORY_ROOT / "docs/evidence/C100/congress-cboe/cboe-integration.json"
_STRUCTURE_MANIFEST_PATH = (
    _REPOSITORY_ROOT / "docs/evidence/C100/congress-cboe/non-vix-csv-structure-investigation.json"
)
_CAPTURE_SHA256 = "51ea1e59bfeaf400f6816300c39145df5e3fb437e7c60bdb8ca2178f8c96f436"
_OFFICIAL_HISTORY_URLS = {
    "VIX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VVIX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "VIX9D": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "OVX": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/OVX_History.csv",
    "GVZ": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/GVZ_History.csv",
    "VXAPL": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXAPL_History.csv",
    "VXAZN": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXAZN_History.csv",
    "VXEEM": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXEEM_History.csv",
}
_VIX_URL = history._HISTORY_URLS["VIX"]
_FIRST_TWO_DAYS = (
    b"DATE,OPEN,HIGH,LOW,CLOSE\n"
    b"01/02/1990,17.240000,17.240000,17.240000,17.240000\n"
    b"01/03/1990,18.190000,18.190000,18.190000,18.190000\n"
)
_CAPTURED_HISTORY_CASES = (
    (
        "VVIX",
        "vvix-history-investigation-20261012.csv",
        "0c8b79bf3aaefa48a9f37cb410ba980e39bf975379f9a68e9fd6a4d1c4128bbe",
        date(2006, 3, 6),
        {"open": None, "high": None, "low": None, "close": 71.73},
    ),
    (
        "VIX9D",
        "vix9d-history-investigation-20261012.csv",
        "9c2476680030afa10a7008764b70865094ec97f2fa64e5113f0f0216506bb89a",
        date(2011, 1, 4),
        {"open": 16.06, "high": 16.06, "low": 16.06, "close": 16.06},
    ),
    (
        "OVX",
        "ovx-history-investigation-20261012.csv",
        "70d756e1300ee7d2f4cc375fe6b435b1049dc8440c9005bce49bee71fea8b7b2",
        date(2009, 9, 18),
        {"open": None, "high": None, "low": None, "close": 41.67},
    ),
    (
        "GVZ",
        "gvz-history-investigation-20261012.csv",
        "f19436d8165084197c2abb1c60a723c5645f780d5cf17123d36973fc5042d3ae",
        date(2009, 9, 18),
        {"open": None, "high": None, "low": None, "close": 22.62},
    ),
    (
        "VXAPL",
        "vxapl-history-investigation-20261012.csv",
        "ef6fa254af0471ca3d7bb3b337809c7c9665d3a6c753abb0bb89e704d9d3d9b5",
        date(2011, 1, 7),
        {"open": 31.86, "high": 32.41, "low": 31.33, "close": 31.57},
    ),
    (
        "VXAZN",
        "vxazn-history-investigation-20261012.csv",
        "67df3c078a1bf7475d45c3cbfcbce6825a9e25b5551b00382d7bb9a0db556009",
        date(2011, 1, 7),
        {"open": 38.46, "high": 39.38, "low": 37.17, "close": 37.29},
    ),
    (
        "VXEEM",
        "vxeem-history-investigation-20261012.csv",
        "29140018e7af4303cc098726c4cd02d39e4f48993efb83e5de64f3bc65374c5f",
        date(2011, 3, 16),
        {"open": 29.18, "high": 34.32, "low": 28.53, "close": 32.86},
    ),
)


class _ReplaySession(requests.Session):
    """Return fixed CSV bytes while preserving the real governed HTTP path."""

    def __init__(self, replies: dict[str, tuple[int, bytes]]) -> None:
        super().__init__()
        self.replies = dict(replies)
        self.calls: list[dict[str, object]] = []
        self.governed_get_calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        try:
            status, body = self.replies[url]
        except KeyError as exc:
            raise AssertionError(f"unexpected source URL: {url}") from exc
        response = requests.Response()
        response.status_code = status
        response._content = body
        response.url = url
        response.headers["Content-Type"] = "text/csv"
        response.encoding = "utf-8"
        return response


def _request_budget(*, attempts: int = 8) -> RequestBudget:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="cboe",
        canonical_model="IndexHistorical",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline Cboe-linked CSV replay fixture; no storage/export rights",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"cdn-api.cboe.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
        profile=profile,
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    replies: dict[str, tuple[int, bytes]],
) -> _ReplaySession:
    session = _ReplaySession(replies)
    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=1,
            rate_limit_per_host=None,
            breaker_threshold=10,
            default_headers={},
        ),
        session=session,
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )
    original_get = client.get

    def observed_get(url: str, **kwargs: object) -> requests.Response:
        session.governed_get_calls.append({"url": url, **kwargs})
        return original_get(url, **kwargs)

    client.get = observed_get  # type: ignore[method-assign]
    monkeypatch.setattr(history, "get_shared_http_client", lambda: client)
    return session


def _context(*, attempts: int = 8) -> FetchContext:
    return FetchContext(timeout=5.0, request_budget=_request_budget(attempts=attempts))


def test_handwritten_csv_maps_daily_ohlc_and_keeps_unpublished_volume_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})
    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol=" vix ",
        start_date="1990-01-02",
        end_date="1990-01-02",
        interval="1d",
        source="cboe",
        market="us",
    )

    assert rows == [
        CboeIndexHistorical(
            symbol="VIX",
            date=date(1990, 1, 2),
            open=17.24,
            high=17.24,
            low=17.24,
            close=17.24,
            volume=None,
            calls_volume=None,
            puts_volume=None,
            total_options_volume=None,
        )
    ]
    assert rows[0].model_dump(mode="json") == {
        "symbol": "VIX",
        "date": "1990-01-02",
        "open": 17.24,
        "high": 17.24,
        "low": 17.24,
        "close": 17.24,
        "volume": None,
        "calls_volume": None,
        "puts_volume": None,
        "total_options_volume": None,
    }
    assert session.calls[0]["url"] == _VIX_URL
    assert session.calls[0]["params"] is None
    assert "Range" not in session.calls[0]["headers"]
    assert session.governed_get_calls[0]["use_cache"] is True


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ({}, True),
        ({"use_cache": None}, True),
        ({"use_cache": True}, True),
        ({"use_cache": False}, False),
    ),
)
def test_use_cache_defaults_strictly_and_reaches_governed_get(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
    expected: bool,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol="VIX",
        **query,
    )

    assert len(rows) == 2
    assert session.governed_get_calls[0]["use_cache"] is expected
    assert len(session.calls) == 1


def test_official_capture_replays_with_local_date_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _CAPTURE_PATH.read_bytes()
    assert len(body) == 473_584
    assert hashlib.sha256(body).hexdigest() == _CAPTURE_SHA256
    session = _client(monkeypatch, {_VIX_URL: (200, body)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol="VIX",
        start_date=date(2026, 10, 9),
        end_date=date(2026, 10, 9),
    )

    assert len(rows) == 1
    assert rows[0].symbol == "VIX"
    assert rows[0].date == date(2026, 10, 9)
    assert rows[0].volume is None
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == _VIX_URL


def test_only_officially_linked_symbols_are_requested_for_a_multi_symbol_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert history._HISTORY_URLS == _OFFICIAL_HISTORY_URLS
    expected_urls = list(_OFFICIAL_HISTORY_URLS.values())
    responses = {
        url: (
            200,
            (
                f"DATE,{symbol}\n01/02/1990,17.24\n".encode()
                if symbol in {"VVIX", "OVX", "GVZ"}
                else _FIRST_TWO_DAYS
            ),
        )
        for symbol, url in _OFFICIAL_HISTORY_URLS.items()
    }
    session = _client(monkeypatch, responses)

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=len(expected_urls)),
        symbol=list(_OFFICIAL_HISTORY_URLS),
        start_date="1990-01-02",
        end_date="1990-01-02",
    )

    assert [row.symbol for row in rows] == list(_OFFICIAL_HISTORY_URLS)
    for row in rows:
        if row.symbol in {"VVIX", "OVX", "GVZ"}:
            assert (row.open, row.high, row.low, row.close) == (None, None, None, 17.24)
        else:
            assert (row.open, row.high, row.low, row.close) == (17.24, 17.24, 17.24, 17.24)
    assert [call["url"] for call in session.calls] == expected_urls
    assert len(session.calls) == len(expected_urls)
    assert all(call["params"] is None for call in session.calls)
    assert all("Range" not in call["headers"] for call in session.calls)


def test_local_date_bounds_are_inclusive_and_filter_outside_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol="VIX",
        start_date="1990-01-03",
        end_date=date(1990, 1, 3),
    )

    assert len(rows) == 1
    assert rows[0].date == date(1990, 1, 3)
    assert rows[0].open == 18.19
    assert rows[0].close == 18.19
    assert rows[0].volume is None
    assert session.calls[0]["params"] is None


def test_nullable_ohlc_cell_remains_typed_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"DATE,OPEN,HIGH,LOW,CLOSE\n01/04/1990,17.2,,17.1,17.18\n"
    _client(monkeypatch, {_VIX_URL: (200, body)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol="VIX",
        start_date="1990-01-04",
        end_date="1990-01-04",
    )

    assert rows[0].open == 17.2
    assert rows[0].high is None
    assert rows[0].low == 17.1
    assert rows[0].close == 17.18
    assert rows[0].volume is None
    assert rows[0].calls_volume is None
    assert rows[0].puts_volume is None
    assert rows[0].total_options_volume is None
    assert rows[0].model_dump(mode="json")["high"] is None


@pytest.mark.parametrize("symbol", ("VVIX", "OVX", "GVZ"))
def test_close_only_source_rows_do_not_fabricate_daily_ohlc(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
) -> None:
    url = _OFFICIAL_HISTORY_URLS[symbol]
    body = f"DATE,{symbol}\n01/02/1990,17.25\n".encode()
    _client(monkeypatch, {url: (200, body)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol=symbol,
        start_date="1990-01-02",
        end_date="1990-01-02",
    )

    assert len(rows) == 1
    assert rows[0].model_dump(mode="json") == {
        "symbol": symbol,
        "date": "1990-01-02",
        "open": None,
        "high": None,
        "low": None,
        "close": 17.25,
        "volume": None,
        "calls_volume": None,
        "puts_volume": None,
        "total_options_volume": None,
    }


@pytest.mark.parametrize(
    ("symbol", "body"),
    (
        ("VVIX", b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17,17,17,17\n"),
        ("OVX", b"DATE,VVIX\n01/02/1990,17\n"),
        ("GVZ", b"DATE,VALUE\n01/02/1990,17\n"),
    ),
)
def test_unobserved_or_cross_symbol_headers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    body: bytes,
) -> None:
    _client(monkeypatch, {_OFFICIAL_HISTORY_URLS[symbol]: (200, body)})

    with pytest.raises(history.CboeIndexHistoricalError) as caught:
        history.CboeIndexHistoricalFetcher().fetch(
            ctx=_context(attempts=1),
            symbol=symbol,
        )

    assert caught.value.code == "CBOE_INDEX_HISTORICAL_BAD_CSV"


@pytest.mark.parametrize(
    ("symbol", "capture_name", "expected_sha256", "expected_date", "expected_values"),
    _CAPTURED_HISTORY_CASES,
)
def test_captured_official_symbols_replay_their_observed_schema(
    monkeypatch: pytest.MonkeyPatch,
    symbol: str,
    capture_name: str,
    expected_sha256: str,
    expected_date: date,
    expected_values: dict[str, float | None],
) -> None:
    capture = _CAPTURE_PATH.parent / capture_name
    body = capture.read_bytes()
    assert hashlib.sha256(body).hexdigest() == expected_sha256
    manifest = json.loads(_STRUCTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    source_entry = next(item for item in manifest["requests"] if item["symbol"] == symbol)
    assert source_entry["response"]["status"] == 200
    assert source_entry["response"]["body_sha256"] == expected_sha256
    assert source_entry["response"]["capture_file"].endswith(capture_name)
    assert source_entry["response"]["body_truncated_at_limit"] is False
    assert source_entry["observed_last_data_date"] == "10/09/2026"
    assert source_entry["observed_first_data_date"] == expected_date.strftime("%m/%d/%Y")
    request_time = datetime.fromisoformat(source_entry["request"]["started_at_utc"])
    assert request_time.utcoffset() == timedelta(0)
    url = _OFFICIAL_HISTORY_URLS[symbol]
    _client(monkeypatch, {url: (200, body)})

    rows = history.CboeIndexHistoricalFetcher().fetch(
        ctx=_context(attempts=1),
        symbol=symbol,
        start_date=expected_date,
        end_date=expected_date,
    )

    assert len(rows) == 1
    assert rows[0].symbol == symbol
    assert rows[0].date == expected_date
    assert {field: getattr(rows[0], field) for field in expected_values} == expected_values
    assert rows[0].volume is None
    assert rows[0].calls_volume is None
    assert rows[0].puts_volume is None
    assert rows[0].total_options_volume is None


@pytest.mark.parametrize(
    "query",
    (
        {"symbol": "SPX"},
        {"symbol": "VIX", "interval": "1m"},
        {"symbol": "VIX", "page": 2},
        {"symbol": "VIX", "offset": 100},
        {"symbol": "VIX", "use_cache": 0},
        {"symbol": "VIX", "use_cache": "false"},
        {"symbol": "VIX", "start_date": "2026-10-02", "end_date": "2026-10-01"},
        {"symbol": ["VIX", "vix"]},
        {"symbol": "VIX", "market": "eu"},
    ),
)
def test_unsupported_or_invalid_query_is_refused_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    session = _client(monkeypatch, {})

    with pytest.raises(ValidationError):
        history.CboeIndexHistoricalFetcher().fetch(ctx=_context(), **query)

    assert session.calls == []


@pytest.mark.parametrize(
    "body",
    (
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17,17,17,17\n01/02/1990,18,18,18,18\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/32/1990,17,17,17,17\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,NaN,17,17,17\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17,not-a-number,17,17\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17%,17,17,17\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE,VOLUME\n01/02/1990,17,17,17,17,100\n",
        b"DATE,OPEN,HIGH,LOW,CLOSE\n01/02/1990,17,17,17\n",
    ),
)
def test_malformed_and_ambiguous_source_rows_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    _client(monkeypatch, {_VIX_URL: (200, body)})

    with pytest.raises(history.CboeIndexHistoricalError):
        history.CboeIndexHistoricalFetcher().fetch(
            ctx=_context(attempts=1),
            symbol="VIX",
        )


def test_empty_collection_is_not_null_and_service_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})
    registry = ProviderRegistry()
    register_provider("cboe", registry)
    fetcher = registry.resolve_model("cboe", "IndexHistorical")
    received: list[tuple[object, object]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object) -> history.CboeIndexHistoricalQuery:
        params = original_transform_query(**kwargs)
        assert type(params) is history.CboeIndexHistoricalQuery
        received.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, CboeIndexHistorical),
    )
    principal = SimpleNamespace(allows_domain=lambda domain: domain == "cboe_index_historical")
    result = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="cboe",
            model="IndexHistorical",
            query={
                "symbol": "VIX",
                "start_date": "1989-12-31",
                "end_date": "1990-01-01",
            },
            principal=principal,
            ctx=FetchContext(timeout=5.0, request_budget=_request_budget(attempts=1)),
        )
    )

    assert received == [("cboe", "us")]
    assert result["source"] == "cboe"
    assert result["model"] == "IndexHistorical"
    assert result["results"] == []
    assert json.dumps(result["results"], allow_nan=False) == "[]"
    assert len(session.calls) == 1


def test_provider_service_serializes_daily_rows_with_injected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})
    registry = ProviderRegistry()
    register_provider("cboe", registry)
    fetcher = registry.resolve_model("cboe", "IndexHistorical")
    received: list[tuple[object, object]] = []
    original_transform_query = fetcher.transform_query

    def observe_query(**kwargs: object) -> history.CboeIndexHistoricalQuery:
        params = original_transform_query(**kwargs)
        received.append((params.source, params.market))
        return params

    fetcher.transform_query = observe_query  # type: ignore[method-assign]
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, CboeIndexHistorical),
    )
    principal = SimpleNamespace(allows_domain=lambda domain: domain == "cboe_index_historical")
    result = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="cboe",
            model="IndexHistorical",
            query={
                "symbol": "vix",
                "start_date": "1990-01-02",
                "end_date": "1990-01-02",
                "use_cache": False,
            },
            principal=principal,
            ctx=FetchContext(timeout=5.0, request_budget=_request_budget(attempts=1)),
        )
    )

    assert received == [("cboe", "us")]
    assert result["results"] == [
        {
            "symbol": "VIX",
            "date": "1990-01-02",
            "open": 17.24,
            "high": 17.24,
            "low": 17.24,
            "close": 17.24,
            "volume": None,
            "calls_volume": None,
            "puts_volume": None,
            "total_options_volume": None,
        }
    ]
    assert json.dumps(result["results"], allow_nan=False)
    assert session.calls[0]["url"] == _VIX_URL
    assert session.governed_get_calls[0]["use_cache"] is False


def test_request_budget_denial_happens_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (200, _FIRST_TWO_DAYS)})

    with pytest.raises(RequestAuthorizationError):
        history.CboeIndexHistoricalFetcher().fetch(
            ctx=FetchContext(timeout=5.0),
            symbol="VIX",
        )

    assert session.calls == []


@pytest.mark.parametrize(
    ("status", "expected_code"),
    (
        (403, "CBOE_INDEX_HISTORICAL_FORBIDDEN"),
        (404, "CBOE_INDEX_HISTORICAL_NOT_FOUND"),
        (429, "CBOE_INDEX_HISTORICAL_RATE_LIMITED"),
        (503, "CBOE_INDEX_HISTORICAL_UPSTREAM_ERROR"),
    ),
)
def test_http_failures_remain_sanitized_and_classified(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected_code: str,
) -> None:
    session = _client(monkeypatch, {_VIX_URL: (status, b"restricted")})

    with pytest.raises(history.CboeIndexHistoricalError) as caught:
        history.CboeIndexHistoricalFetcher().fetch(
            ctx=_context(attempts=1),
            symbol="VIX",
        )

    assert caught.value.code == expected_code
    assert caught.value.status == status
    assert str(caught.value) == f"{expected_code} status={status}"
    assert len(session.calls) == 1


def test_integration_handoff_declares_only_the_limited_candidate_domain() -> None:
    handoff = json.loads(_HANDOFF_PATH.read_text(encoding="utf-8"))
    binding = next(
        item for item in handoff["bindings"] if item["canonical_model_id"] == "IndexHistorical"
    )

    assert binding == {
        "module": "opendata.data.providers.cboe.models.index_historical_public",
        "class_name": "CboeIndexHistoricalFetcher",
        "canonical_model_id": "IndexHistorical",
        "domain": "cboe_index_historical",
        "contract": "CboeIndexHistorical",
    }
    assert handoff["domains"]["cboe_index_historical"] == {
        "display_name": "Cboe volatility index daily history",
        "rest_path": "index/cboe-index-historical",
        "contract": "CboeIndexHistorical",
        "priority": None,
        "temporal_kind": "series",
        "time_field": "date",
        "natural_key": ["symbol", "date"],
        "filter_dims": ["symbol", "date"],
        "storage_mode": "transient",
        "permissions": ["query"],
    }
    assert set(handoff["remaining_fixed_identities"]) == {"FuturesCurve", "IndexSnapshots"}
