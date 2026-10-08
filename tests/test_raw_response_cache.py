"""Offline coverage for the opt-in raw HTTP response cache."""

from __future__ import annotations

import importlib
import json
import logging
import os
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests
from loguru import logger
from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
    pool,
)

from opendata.api.data_query import get_warehouse_engine
from opendata.core.config import Settings
from opendata.data.domains import dwd_table
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.providers import register_providers
from opendata.data.providers.ths import FuyaoCredentials, FuyaoError, FuyaoHttpClient
from opendata.data.raw_response_cache import RawResponseCache, get_configured_raw_response_cache
from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, contract_columns
from opendata.pipeline.watermark import latest_batches

HTTP_CLIENT_LOGGER = FuyaoHttpClient.__module__


def _put(
    cache: RawResponseCache,
    *,
    method: str = "GET",
    source: str = "fred",
    endpoint: str = "https://cache.example/series",
    params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    content: bytes = b"raw-body",
    status_code: int = 200,
) -> bool:
    return cache.put(
        method=method,
        source=source,
        endpoint=endpoint,
        params=params,
        headers=headers,
        context=context,
        status_code=status_code,
        content=content,
        encoding="utf-8",
    )


def _get(
    cache: RawResponseCache,
    *,
    source: str = "fred",
    endpoint: str = "https://cache.example/series",
    params: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
):
    return cache.get(
        source=source,
        endpoint=endpoint,
        params=params,
        headers=headers,
        context=context,
    )


class _CountingAdapter(requests.adapters.BaseAdapter):
    def __init__(self, *, body: bytes = b"provider-body") -> None:
        self.body = body
        self.calls: list[requests.PreparedRequest] = []
        self.lock = threading.Lock()

    def send(self, request, **kwargs):
        with self.lock:
            self.calls.append(request)
        response = requests.Response()
        response.status_code = 200
        response._content = self.body
        response._content_consumed = True
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self) -> None:
        return None


def _macro_client(cache: RawResponseCache, adapter: _CountingAdapter):
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", adapter)
    client = GovernedHttpClient(
        HttpClientConfig(rate_limit_per_host=None, max_attempts=1),
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=cache,
    )
    # Use the normal thread-local slot, matching the process-shared runtime.
    client._thread_local.session = session
    return client, session


def test_cache_is_disabled_by_default_and_supports_cache_dir_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CACHE_DIR", raising=False)
    monkeypatch.delenv("OPENDATA_CACHE_DIR", raising=False)
    assert Settings(_env_file=None).raw_response_cache_enabled is False

    alias_dir = tmp_path / "alias-cache"
    monkeypatch.setenv("OPENDATA_CACHE_DIR", str(alias_dir))
    assert Settings(_env_file=None).cache_dir == alias_dir

    preferred_dir = tmp_path / "preferred-cache"
    monkeypatch.setenv("CACHE_DIR", str(preferred_dir))
    assert Settings(_env_file=None).cache_dir == preferred_dir


def test_cache_key_normalizes_order_and_separates_response_dimensions(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60, schema_version="bars-v2")
    common = {
        "source": "fred",
        "endpoint": "https://cache.example/series?file_type=json",
        "params": {"series_id": "CPI", "api_key": "fred-secret"},
        "headers": {"X-api-key": "header-secret", "Accept": "application/json"},
        "context": {"adjust_basis": "split", "as_of": "2026-09-30"},
    }
    first_key = cache.key_for(**common)
    reversed_key = cache.key_for(
        **{
            **common,
            "params": dict(reversed(list(common["params"].items()))),
            "headers": dict(reversed(list(common["headers"].items()))),
            "context": dict(reversed(list(common["context"].items()))),
        }
    )

    assert first_key == reversed_key
    variations = [
        {**common, "source": "imf"},
        {**common, "endpoint": "https://cache.example/other"},
        {**common, "params": {**common["params"], "series_id": "PCE"}},
        {**common, "params": {**common["params"], "api_key": "other-fred-secret"}},
        {**common, "headers": {**common["headers"], "X-api-key": "other-header-secret"}},
        {**common, "context": {**common["context"], "adjust_basis": "none"}},
        {**common, "context": {**common["context"], "as_of": "2026-09-29"}},
    ]
    assert all(cache.key_for(**variation) != first_key for variation in variations)
    assert cache.key_for(**common) != RawResponseCache(
        tmp_path, ttl_seconds=60, schema_version="bars-v3"
    ).key_for(**common)


def test_url_userinfo_is_hashed_and_partitions_cache_keys(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    first_url = "https://alice:basic-secret@cache.example/series"
    second_url = "https://alice:other-secret@cache.example/series"

    first_key = cache.key_for(source="fred", endpoint=first_url)
    second_key = cache.key_for(source="fred", endpoint=second_url)

    assert first_key != second_key
    assert _put(cache, endpoint=first_url, content=b"alice-response")
    assert _get(cache, endpoint=first_url) is not None
    assert _get(cache, endpoint=second_url) is None
    entry_bytes = next((tmp_path / "raw_responses").glob("*.cache")).read_bytes()
    assert b"basic-secret" not in entry_bytes
    assert b"other-secret" not in entry_bytes


def test_ttl_and_only_successful_statuses_are_cached(tmp_path: Path) -> None:
    now = [100.0]
    cache = RawResponseCache(tmp_path, ttl_seconds=10, clock=lambda: now[0])

    assert _put(cache)
    assert _get(cache).content == b"raw-body"
    now[0] = 110.0
    assert _get(cache) is None
    assert not _put(cache, endpoint="https://cache.example/error", status_code=503)
    assert not _put(cache, endpoint="https://cache.example/post", method="POST")
    assert _get(cache, endpoint="https://cache.example/error") is None


def test_credentials_do_not_appear_in_entry_filename_or_metadata(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    params = {"series_id": "CPI", "api_key": "query-secret"}
    headers = {
        "Authorization": "Bearer authorization-secret",
        "Cookie": "session=cookie-secret",
        "X-api-key": "header-secret",
    }

    assert _put(
        cache,
        endpoint="https://user:basic-secret@cache.example/series?token=url-query-secret",
        params=params,
        headers=headers,
    )
    entry = next((tmp_path / "raw_responses").glob("*.cache"))
    assert len(entry.stem) == 64
    serialized = entry.name.encode() + entry.read_bytes()
    for secret in (
        b"query-secret",
        b"authorization-secret",
        b"cookie-secret",
        b"header-secret",
        b"basic-secret",
        b"url-query-secret",
    ):
        assert secret not in serialized


def test_corrupted_entry_is_a_warning_and_safe_miss(tmp_path: Path, caplog) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    assert _put(cache)
    entry = next((tmp_path / "raw_responses").glob("*.cache"))
    entry.write_bytes(entry.read_bytes()[:-1] + b"X")

    with caplog.at_level(logging.WARNING, logger="opendata.data.raw_response_cache"):
        assert _get(cache) is None

    assert any(
        getattr(record, "event", None) == "raw_response_cache_warning"
        and getattr(record, "reason", None) == "entry_invalid"
        for record in caplog.records
    )


def test_epoch_sized_expiry_metadata_tampering_is_rejected(tmp_path: Path) -> None:
    epoch = 1_800_000_000.0
    cache = RawResponseCache(tmp_path, ttl_seconds=60, clock=lambda: epoch)
    assert _put(cache)
    entry = next(cache.raw_dir.glob("*.cache"))
    record = entry.read_bytes()
    header_start = len(b"ODRAW1\n") + struct.calcsize(">I")
    header_length = struct.unpack_from(">I", record, len(b"ODRAW1\n"))[0]
    header_end = header_start + header_length
    metadata = json.loads(record[header_start:header_end].decode("utf-8"))
    metadata["expires_at"] += 1.0
    encoded_metadata = json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    entry.write_bytes(
        record[: len(b"ODRAW1\n")]
        + struct.pack(">I", len(encoded_metadata))
        + encoded_metadata
        + record[header_end:]
    )

    assert _get(cache) is None


def test_symlink_entries_and_cache_directories_are_refused(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path / "root", ttl_seconds=60)
    assert _put(cache)
    entry = next(cache.raw_dir.glob("*.cache"))
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-unchanged", encoding="utf-8")
    entry.unlink()
    try:
        entry.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    assert _get(cache) is None
    assert not _put(cache, content=b"must-not-replace-link")
    assert outside.read_text(encoding="utf-8") == "outside-unchanged"

    linked_root = tmp_path / "linked-root"
    linked_outside = tmp_path / "linked-outside"
    linked_outside.mkdir()
    linked_root.mkdir()
    raw_dir = linked_root / "raw_responses"
    raw_dir.symlink_to(linked_outside, target_is_directory=True)
    directory_cache = RawResponseCache(linked_root, ttl_seconds=60)
    assert not _put(directory_cache)
    assert list(linked_outside.iterdir()) == []


def test_concurrent_atomic_writes_never_leave_partial_entries(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    bodies = [f"body-{index}".encode() for index in range(16)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda body: _put(cache, content=body), bodies))

    cached = _get(cache)
    assert cached is not None
    assert cached.content in bodies
    assert list((tmp_path / "raw_responses").glob("*.tmp")) == []


def test_optional_settings_failure_disables_cache_safely(monkeypatch, caplog) -> None:
    def unavailable():
        raise ImportError("optional web config unavailable")

    monkeypatch.setattr("opendata.data.raw_response_cache._load_application_settings", unavailable)
    with caplog.at_level(logging.WARNING, logger="opendata.data.raw_response_cache"):
        assert get_configured_raw_response_cache() is None
    assert any(
        getattr(record, "reason", None) == "configuration_unavailable" for record in caplog.records
    )


def test_fuyao_sdk_import_does_not_require_optional_web_configuration() -> None:
    script = """
import builtins
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "opendata.core.config" or name == "pydantic_settings":
        raise ImportError("optional web dependency intentionally unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
assert FuyaoHttpClient is not None
"""
    result = subprocess.run(  # noqa: S603  # nosec B603: fixed local interpreter and literal script
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("source", "module_name", "has_params"),
    [
        ("ecb", "opendata.data.providers.ecb.models._client", True),
        ("fred", "opendata.data.providers.fred.models._client", True),
        ("imf", "opendata.data.providers.imf.models._client", False),
        ("oecd", "opendata.data.providers.oecd.models._client", True),
    ],
)
def test_macro_runtime_cache_hit_preserves_tuple_and_skips_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    module_name: str,
    has_params: bool,
) -> None:
    module = importlib.import_module(module_name)
    adapter = _CountingAdapter()
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    client, _session = _macro_client(cache, adapter)
    monkeypatch.setattr(module, "get_shared_http_client", lambda: client)
    url = f"https://{source}.cache.example/series"
    params = {"series_id": "CPI", "api_key": "macro-api-secret"}
    events: list[dict[str, Any]] = []
    sink = logger.add(lambda message: events.append(message.record["extra"]), level="INFO")
    try:
        if has_params:
            first = module._http_get(url, params, 3.5)
            second = module._http_get(url, params, 3.5)
        else:
            first = module._http_get(url, 3.5)
            second = module._http_get(url, 3.5)
    finally:
        logger.remove(sink)

    assert first == second == (200, "provider-body")
    assert len(adapter.calls) == 1
    hit = next(
        event
        for event in events
        if event.get("event") == "governed_http_request" and event.get("cache_hit")
    )
    assert hit["source"] == source
    assert hit["attempt"] == 0
    assert hit["request_id"]
    assert "macro-api-secret" not in str(hit)
    serialized = b"".join(entry.read_bytes() for entry in cache.raw_dir.glob("*.cache"))
    assert b"macro-api-secret" not in serialized


def test_macro_cache_identity_includes_session_credentials_and_request_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("opendata.data.providers.fred.models._client")
    adapter = _CountingAdapter()
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    client, session = _macro_client(cache, adapter)
    monkeypatch.setattr(module, "get_shared_http_client", lambda: client)
    session.headers["X-Tenant"] = "tenant-one"
    session.cookies.set("sid", "cookie-one")
    session.auth = requests.auth.HTTPBasicAuth("fred-user", "basic-one")

    url = "https://fred.cache.example/series"
    params = {"series_id": "CPI", "api_key": "key-one"}

    def module_get() -> tuple[int, str]:
        return module._http_get(url, params, None)

    assert module_get() == (200, "provider-body")
    assert module_get() == (200, "provider-body")
    assert len(adapter.calls) == 1

    session.headers["X-Tenant"] = "tenant-two"
    assert module_get() == (200, "provider-body")
    session.cookies.set("sid", "cookie-two")
    assert module_get() == (200, "provider-body")
    session.auth = requests.auth.HTTPBasicAuth("fred-user", "basic-two")
    assert module_get() == (200, "provider-body")
    assert module._http_get(url, {**params, "api_key": "key-two"}, None) == (
        200,
        "provider-body",
    )
    session.auth = None
    client.get("https://alice:basic-one@fred.cache.example/userinfo", source="fred")
    client.get("https://alice:basic-one@fred.cache.example/userinfo", source="fred")
    client.get("https://alice:basic-two@fred.cache.example/userinfo", source="fred")
    assert len(adapter.calls) == 7

    serialized = b"".join(entry.read_bytes() for entry in cache.raw_dir.glob("*.cache"))
    for secret in (
        b"cookie-one",
        b"cookie-two",
        b"basic-one",
        b"basic-two",
        b"key-one",
        b"key-two",
    ):
        assert secret not in serialized


def test_cache_failure_does_not_fail_successful_macro_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("opendata.data.providers.fred.models._client")
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "raw_responses").symlink_to(outside, target_is_directory=True)
    cache = RawResponseCache(root, ttl_seconds=60)
    adapter = _CountingAdapter()
    client, _session = _macro_client(cache, adapter)
    monkeypatch.setattr(module, "get_shared_http_client", lambda: client)
    result = module._http_get("https://fred.cache.example/series", {"series_id": "CPI"}, None)

    assert result == (200, "provider-body")
    assert len(adapter.calls) == 1
    assert list(outside.iterdir()) == []


def test_non_get_request_never_creates_a_raw_response_cache_entry(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    adapter = _CountingAdapter()
    client, _session = _macro_client(cache, adapter)

    client.request("POST", "https://fred.cache.example/series")
    assert not cache.raw_dir.exists()
    client.get("https://fred.cache.example/series", source="fred")

    assert len(adapter.calls) == 2
    assert len(list(cache.raw_dir.glob("*.cache"))) == 1


def test_fuyao_runtime_cache_hit_preserves_envelope_and_credential_isolation(
    tmp_path: Path, caplog
) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    calls = {"same": 0, "different": 0, "params": 0}

    def handler_for(name: str):
        def handler(request: httpx.Request) -> httpx.Response:
            calls[name] += 1
            assert request.headers["X-api-key"] in {"fuyao-key-one", "fuyao-key-two"}
            expected_region = "us" if name == "params" else "cn"
            assert request.url.params.get("region") == expected_region
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "ok",
                    "request_id": "upstream-rid",
                    "data": {"timestamp": 10, "item": [{"value": 7}]},
                },
            )

        return handler

    def build(key: str, label: str, region: str = "cn") -> FuyaoHttpClient:
        transport = httpx.MockTransport(handler_for(label))
        http_client = httpx.Client(transport=transport, trust_env=False)
        http_client.headers["X-Tenant"] = "tenant-a"
        http_client.cookies.set("sid", "cookie-a")
        http_client.params = httpx.QueryParams({"region": region})
        return FuyaoHttpClient(
            credentials=FuyaoCredentials(key, base_url="https://fuyao.cache.example"),
            client=http_client,
            max_attempts=1,
            raw_response_cache=cache,
        )

    first_client = build("fuyao-key-one", "same")
    second_client = build("fuyao-key-one", "same")
    different_key_client = build("fuyao-key-two", "different")
    different_params_client = build("fuyao-key-one", "params", "us")
    try:
        with caplog.at_level(logging.INFO, logger=HTTP_CLIENT_LOGGER):
            first = first_client.get("/api/prices", params={"symbol": "000001"})
            second = second_client.get("/api/prices", params={"symbol": "000001"})
            separate = different_key_client.get("/api/prices", params={"symbol": "000001"})
            params_separate = different_params_client.get(
                "/api/prices", params={"symbol": "000001"}
            )
    finally:
        first_client.close()
        second_client.close()
        different_key_client.close()
        different_params_client.close()

    assert (
        first.status_code
        == second.status_code
        == separate.status_code
        == params_separate.status_code
        == 200
    )
    assert first.envelope == second.envelope == separate.envelope == params_separate.envelope
    assert first.envelope.items == ({"value": 7},)
    assert calls == {"same": 1, "different": 1, "params": 1}
    hit = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "governed_http_request"
        and getattr(record, "cache_hit", False)
    )
    assert hit.attempt == 0
    assert hit.source == "ths"
    assert hit.request_id
    for secret in ("fuyao-key-one", "fuyao-key-two", "cookie-a"):
        assert secret not in str(hit.__dict__)
    serialized = b"".join(entry.read_bytes() for entry in cache.raw_dir.glob("*.cache"))
    for secret in (b"fuyao-key-one", b"fuyao-key-two", b"cookie-a"):
        assert secret not in serialized


def test_fuyao_nonzero_envelope_is_never_cached(tmp_path: Path) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 3002, "message": "not ready"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("fuyao-key", base_url="https://fuyao.cache.example"),
        client=http_client,
        max_attempts=1,
        raw_response_cache=cache,
    )
    try:
        for _ in range(2):
            with pytest.raises(FuyaoError) as exc_info:
                client.get("/api/prices")
            assert exc_info.value.category == "not_ready"
    finally:
        client.close()

    assert calls == 2
    assert list((tmp_path / "raw_responses").glob("*.cache")) == []


def _sqlalchemy_type(sql_type: str):
    """SQLite stand-in for the DWD contract DDL's SQL types."""
    if sql_type.startswith(("varchar", "char")):
        return String
    if sql_type == "double":
        return Float
    if sql_type == "date":
        return Date
    if sql_type == "datetime":
        return DateTime
    return Integer


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ac13_05_actual_dwd_adjustment_route_never_caches_synthetic_series(
    tmp_path: Path, test_client, test_user_token
) -> None:
    """AC-13|05: raw HTTP bytes stay separate from SQLite qfq/hfq query results."""
    from opendata.main import app

    register_providers()
    engine = create_engine(
        "sqlite://", poolclass=pool.StaticPool, connect_args={"check_same_thread": False}
    )
    metadata = MetaData()
    stock_table = Table(
        dwd_table("stock_daily"),
        metadata,
        *[
            Column(
                column.name,
                _sqlalchemy_type(column.sql_type),
                nullable=column.nullable,
                primary_key=column.name in {"symbol", "trade_date"},
            )
            for column in [*contract_columns("stock_daily"), *DWD_TRACE_COLUMNS]
        ],
    )
    factor_table = Table(
        "dwd_stock_adjust",
        metadata,
        Column("symbol", String(64), primary_key=True),
        Column("trade_date", Date, primary_key=True),
        Column("qfq_factor", Float, nullable=False),
        Column("hfq_factor", Float, nullable=False),
    )
    watermark_table = Table(
        "batch_watermark",
        metadata,
        Column("seq", Integer, primary_key=True, autoincrement=True),
        Column("batch_id", String(36), nullable=False, unique=True),
        Column("domain", String(64), nullable=False),
        Column("source", String(32), nullable=False),
        Column("layer", String(8), nullable=False),
        Column("window_start", Date),
        Column("window_end", Date),
        Column("rows_written", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
    )
    metadata.create_all(engine)

    today = date.today()
    day1 = today - timedelta(days=2)
    day2 = today - timedelta(days=1)
    merged_at = datetime.now()
    with engine.begin() as connection:
        connection.execute(
            insert(stock_table),
            [
                {
                    "symbol": "CACHE_ADJUST_PROBE",
                    "trade_date": day,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 100.0,
                    "amount": 1000.0,
                    "source": "ths",
                    "_merged_at": merged_at,
                    "_diff_flag": 0,
                    "_as_of": day,
                }
                for day, close in ((day1, 10.0), (day2, 12.0))
            ],
        )
        connection.execute(
            insert(factor_table),
            [
                {
                    "symbol": "CACHE_ADJUST_PROBE",
                    "trade_date": day1,
                    "qfq_factor": 0.5,
                    "hfq_factor": 2.0,
                },
                {
                    "symbol": "CACHE_ADJUST_PROBE",
                    "trade_date": day2,
                    "qfq_factor": 1.0,
                    "hfq_factor": 3.0,
                },
            ],
        )
        connection.execute(
            insert(watermark_table).values(
                batch_id="cache-ods-watermark-0001",
                domain="stock_daily",
                source="ths",
                layer="ods",
                window_start=day1,
                window_end=day2,
                rows_written=2,
                created_at=merged_at,
            )
        )

    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    adapter = _CountingAdapter(body=b'{"raw":"unadjusted HTTP bytes"}')
    raw_client, _session = _macro_client(cache, adapter)
    raw_response = raw_client.get(
        "https://fred.cache.example/raw-bars",
        params={"series_id": "CACHE_ADJUST_PROBE", "api_key": "fixture-key"},
        source="fred",
    )
    assert raw_response.content == b'{"raw":"unadjusted HTTP bytes"}'
    cache_files_before = {entry.name: entry.read_bytes() for entry in cache.raw_dir.glob("*.cache")}
    assert len(cache_files_before) == 1

    previous_override = app.dependency_overrides.get(get_warehouse_engine)
    app.dependency_overrides[get_warehouse_engine] = lambda: engine
    headers = {"Authorization": f"Bearer {test_user_token}"}
    base_params = {
        "symbols": "CACHE_ADJUST_PROBE",
        "start": day1.isoformat(),
        "end": day2.isoformat(),
    }
    try:
        watermark_before = latest_batches(engine, domain="stock_daily")
        assert len(watermark_before) == 1
        assert watermark_before[0].layer == "ods"

        qfq = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={**base_params, "adjust": "qfq"},
            headers=headers,
        )
        hfq = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={**base_params, "adjust": "hfq"},
            headers=headers,
        )
        assert qfq.status_code == hfq.status_code == 200
        qfq_rows = qfq.json()["data"]["rows"]
        hfq_rows = hfq.json()["data"]["rows"]
        assert [row["close"] for row in qfq_rows] == [5.0, 12.0]
        assert [row["close"] for row in hfq_rows] == [20.0, 36.0]
        assert {
            entry.name: entry.read_bytes() for entry in cache.raw_dir.glob("*.cache")
        } == cache_files_before

        for entry in cache.raw_dir.glob("*.cache"):
            entry.unlink()
        assert list(cache.raw_dir.glob("*.cache")) == []
        qfq_after_clear = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={**base_params, "adjust": "qfq"},
            headers=headers,
        )
        hfq_after_clear = await test_client.get(
            "/api/v1/data/equity/stock_daily",
            params={**base_params, "adjust": "hfq"},
            headers=headers,
        )
        assert [row["close"] for row in qfq_after_clear.json()["data"]["rows"]] == [
            5.0,
            12.0,
        ]
        assert [row["close"] for row in hfq_after_clear.json()["data"]["rows"]] == [
            20.0,
            36.0,
        ]
        assert latest_batches(engine, domain="stock_daily") == watermark_before
        assert list(cache.raw_dir.glob("*.cache")) == []
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_warehouse_engine, None)
        else:
            app.dependency_overrides[get_warehouse_engine] = previous_override
        engine.dispose()
