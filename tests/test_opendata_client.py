"""Tests for the minimal REST client (A5 prerequisite, design §10.4).

Two layers: the request contract (path, parameters, auth header,
envelope unwrapping, pagination, error mapping) against an httpx mock
transport, and one real round trip over a socket against the running
ASGI app, which is what the consumer hand-off actually does.
"""

import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from opendata_client.client import SubscriptionSession

from opendata_client import (
    AuthenticationError,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    Page,
    PermissionDeniedError,
    UnsupportedQueryError,
)


def _envelope(data, success: bool = True, message: str = "success") -> httpx.Response:
    return httpx.Response(200, json={"success": success, "message": message, "data": data})


def _client(handler, **kwargs) -> OpendataClient:
    return OpendataClient(
        "http://api.test",
        api_key="od-test-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


class TestRequestContract:
    def test_stock_daily_sends_the_documented_parameters(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            seen["headers"] = dict(request.headers)
            return _envelope(
                {
                    "rows": [{"symbol": "600519", "trade_date": "2024-01-02", "close": 1.0}],
                    "columns": ["symbol", "trade_date", "close"],
                    "page": 1,
                    "page_size": 500,
                    "count": 1,
                }
            )

        with _client(handler) as client:
            page = client.stock_daily(
                ["600519", "000001"],
                start="2024-01-01",
                end="2024-01-31",
                adjust="qfq",
                fields=("open", "high", "low", "close"),
            )

        assert seen["path"] == "/api/v1/data/equity/stock_daily"
        params = seen["params"]
        assert params["symbols"] == "600519,000001"  # sequence -> csv
        assert params["adjust"] == "qfq"
        assert params["layer"] == "dwd"
        assert params["start"] == "2024-01-01"
        assert params["end"] == "2024-01-31"
        assert params["fields"] == "open,high,low,close"
        assert seen["headers"]["x-api-key"] == "od-test-key"
        assert isinstance(page, Page)
        assert page.symbols() == {"600519"}

    def test_a_single_symbol_stays_a_string(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return _envelope({"rows": [], "count": 0})

        with _client(handler) as client:
            client.stock_daily("600519")

        assert seen["symbols"] == "600519"

    def test_jwt_client_uses_the_bearer_header(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return _envelope({"rows": [], "count": 0})

        with OpendataClient(
            "http://api.test", token="jwt-token", transport=httpx.MockTransport(handler)
        ) as client:
            client.catalog()

        assert seen["authorization"] == "Bearer jwt-token"

    def test_credentials_are_required(self):
        with pytest.raises(OpendataClientError, match="pass an api_key or a token"):
            OpendataClient("http://api.test")

    def test_layers_and_catalog_freshness_diff_report(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(
                request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
            )
            if request.url.path.endswith("/catalog"):
                return _envelope({"domains": [{"domain": "stock_daily"}]})
            if "freshness" in request.url.path:
                return _envelope({"domain": "stock_daily", "lag_days": 0})
            return _envelope({"rows": [{"biz_key": "600519|2024-01-02", "field": "close"}]})

        with _client(handler) as client:
            assert client.catalog() == [{"domain": "stock_daily"}]
            assert client.freshness("stock_daily", source="akshare")["lag_days"] == 0
            assert client.diff_report("stock_daily", batch_id="b1", limit=5)[0]["field"] == "close"

        assert paths[0] == "/api/v1/data/catalog"
        assert paths[1] == "/api/v1/data/domains/stock_daily/freshness?source=akshare"
        assert paths[2] == "/api/v1/data/domains/stock_daily/diff-report?batch_id=b1&limit=5"


class TestPagination:
    def test_iter_rows_follows_a_short_page(self):
        pages = {
            1: [{"symbol": "a"}, {"symbol": "b"}],
            2: [{"symbol": "c"}],
        }
        seen_pages: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params)["page"])
            seen_pages.append(page)
            rows = pages[page]
            return _envelope({"rows": rows, "count": len(rows), "page": page, "page_size": 2})

        with _client(handler) as client:
            rows = list(client.iter_rows("equity", "stock_daily", symbols="600519", page_size=2))

        assert [row["symbol"] for row in rows] == ["a", "b", "c"]
        assert seen_pages == [1, 2]

    def test_iter_rows_stops_on_an_empty_page(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return _envelope({"rows": [], "count": 0, "page": 1, "page_size": 500})

        with _client(handler) as client:
            rows = list(client.iter_rows("equity", "stock_daily", symbols="600519"))

        assert rows == []
        assert len(calls) == 1

    def test_stock_daily_all_collects_every_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(dict(request.url.params)["page"])
            rows = [{"symbol": f"s{page}"}] * 2 if page == 1 else [{"symbol": "s2"}]
            return _envelope({"rows": rows, "count": len(rows), "page": page, "page_size": 2})

        with _client(handler) as client:
            rows = client.stock_daily_all("600519", page_size=2)

        assert [row["symbol"] for row in rows] == ["s1", "s1", "s2"]


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (400, InvalidQueryError),
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, NotFoundError),
            (422, InvalidQueryError),
            (501, UnsupportedQueryError),
            (500, OpendataClientError),
        ],
    )
    def test_status_codes_map_to_errors(self, status_code, expected):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, json={"detail": "boom"})

        with _client(handler) as client, pytest.raises(expected) as excinfo:
            client.stock_daily("600519")

        assert f"HTTP {status_code}" in str(excinfo.value)
        assert "boom" in str(excinfo.value)

    def test_failure_envelope_raises_even_with_200(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _envelope(None, success=False, message="domain is not registered")

        with _client(handler) as client, pytest.raises(OpendataClientError, match="not registered"):
            client.stock_daily("600519")

    def test_transport_failure_is_wrapped(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with _client(handler) as client, pytest.raises(OpendataClientError, match="failed"):
            client.stock_daily("600519")


@pytest.mark.e2e
class TestAgainstTheRunningApi:
    """The consumer hand-off: a real socket, a real key, real warehouse rows."""

    SYMBOL = "CLIENT_PROBE"

    @pytest.fixture
    def live_server(self):
        import uvicorn

        from opendata.main import app

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.1)
        if not server.started:
            server.should_exit = True
            pytest.skip("the API server did not start in time")
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(timeout=10)

    @pytest.fixture
    def api_key(self):
        """Issue a real key for the probe consumer, cleaning up afterwards."""
        import asyncio

        from sqlalchemy import text as sql_text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        from opendata.core.config import settings
        from opendata.models.user import User
        from opendata.services.api_key_service import ApiKeyService

        engine = create_async_engine(settings.database_url, poolclass=NullPool)

        async def issue() -> tuple[str, int]:
            session_maker = async_sessionmaker(engine, expire_on_commit=False)
            async with session_maker() as session:
                try:
                    await session.execute(sql_text("SELECT 1"))
                except Exception as exc:
                    pytest.skip(f"main database unreachable: {type(exc).__name__}")
                user = User(
                    username="a5-client",
                    email="a5-client@example.com",
                    hashed_password="x",
                    is_active=True,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
                issued = await ApiKeyService(session).issue(
                    owner=user, name="a5-client", scopes=["stock_daily"]
                )
                return issued.plaintext, user.id

        async def cleanup(owner_id: int) -> None:
            session_maker = async_sessionmaker(engine, expire_on_commit=False)
            async with session_maker() as session:
                await session.execute(
                    sql_text("DELETE FROM api_keys WHERE owner_user_id = :owner"),
                    {"owner": owner_id},
                )
                await session.execute(
                    sql_text("DELETE FROM users WHERE id = :owner"), {"owner": owner_id}
                )
                await session.commit()

        key, owner_id = asyncio.run(issue())
        yield key
        asyncio.run(cleanup(owner_id))
        asyncio.run(engine.dispose())

    @pytest.fixture
    def warehouse_rows(self):
        from sqlalchemy import create_engine
        from sqlalchemy import text as sql_text
        from sqlalchemy.pool import NullPool

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(sql_text("SELECT 1"))
        except Exception as exc:
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        with engine.begin() as connection:
            connection.execute(
                sql_text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
            connection.execute(
                sql_text(
                    "INSERT INTO `dwd_stock_daily` "
                    "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, "
                    "`amount`, `source`, `_merged_at`, `_diff_flag`, `_as_of`) "
                    "VALUES (:probe, '2024-01-02', 1.0, 2.0, 0.5, 1.5, 1000, 1500, "
                    "'akshare', NOW(), 0, NOW())"
                ),
                {"probe": self.SYMBOL},
            )
        yield
        with engine.begin() as connection:
            connection.execute(
                sql_text("DELETE FROM `dwd_stock_daily` WHERE `symbol` = :probe"),
                {"probe": self.SYMBOL},
            )
        engine.dispose()

    def test_consumer_fetches_daily_bars_over_http(self, live_server, api_key, warehouse_rows):
        client = OpendataClient(live_server, api_key=api_key)
        try:
            page = client.stock_daily([self.SYMBOL], start="2024-01-01", end="2024-01-31")
            catalog = client.catalog()
        finally:
            client.close()

        assert page.count == 1
        row = page.rows[0]
        assert row["symbol"] == self.SYMBOL
        assert float(row["close"]) == pytest.approx(1.5)
        assert "trade_date" in page.columns
        assert {entry["domain"] for entry in catalog} == {"stock_daily"}  # scope filter

    def test_every_catalog_reading_reaches_a_consumer(self, live_server, api_key, warehouse_rows):
        """AC-18|02 on the client plane: the five readings survive to a caller.

        A dashboard can render what an endpoint returns and still lose it on
        the way to a programmatic consumer, so this reads the catalog through
        ``opendata_client`` against the real warehouse and recomputes the lag
        from the baseline the freshness door ships.
        """
        client = OpendataClient(live_server, api_key=api_key)
        try:
            entry = next(e for e in client.catalog() if e["domain"] == "stock_daily")
            freshness = client.freshness("stock_daily")
        finally:
            client.close()

        assert entry["table"] == "dwd_stock_daily"
        assert entry["freshness_field"] == "trade_date"
        # 覆盖：行数与标的数来自这张表自己，窗口含本次探针插入的那一行
        coverage = entry["coverage"]
        assert coverage["rows"] >= 1
        assert coverage["symbols"] >= 1
        assert coverage["start"] <= "2024-01-02" <= coverage["end"]
        # 质量标记三态之一，未测量不等于一致
        assert entry["quality"]["flag"] in {"clean", "flagged", "unmeasured"}
        # 各源最近更新：一条腿一个读数，未映射要说明原因
        legs = {leg["source"]: leg for leg in entry["sources"]}
        assert {"akshare", "ths"} <= set(legs)
        for leg in legs.values():
            assert leg["status"] in {"fresh", "stale", "missing", "unmapped"}
            if leg["status"] == "unmapped":
                assert leg["reason"]

        # 新鲜度：滞后天数必须能用门面上给出的基准日复算，否则它是不可审计的
        expected = date.fromisoformat(freshness["expected_data_date"])
        latest = date.fromisoformat(freshness["latest"])
        assert (expected - latest).days == freshness["lag_days"]
        assert freshness["lag_days"] == entry["lag_days"]

    def test_scoped_consumer_is_denied_elsewhere(self, live_server, api_key, warehouse_rows):
        client = OpendataClient(live_server, api_key=api_key)
        try:
            with pytest.raises(PermissionDeniedError):
                client.query("index", "index_constituent", symbols="000300")
        finally:
            client.close()

    def test_bad_key_is_an_authentication_error(self, live_server, warehouse_rows):
        client = OpendataClient(live_server, api_key="od-not-a-real-key")
        try:
            with pytest.raises(AuthenticationError):
                client.stock_daily(self.SYMBOL)
        finally:
            client.close()

    def test_json_payload_matches_the_documented_envelope(
        self, live_server, api_key, warehouse_rows
    ):
        response = httpx.get(
            f"{live_server}/api/v1/data/equity/stock_daily",
            params={"symbols": self.SYMBOL},
            headers={"X-API-Key": api_key},
            timeout=10,
        )
        body = json.loads(response.text)

        assert response.status_code == 200
        assert body["success"] is True
        assert set(body["data"]) >= {"rows", "columns", "page", "page_size", "count"}

    def test_consumer_subscribes_over_a_real_socket(self, live_server, api_key, warehouse_rows):
        """The client's socket half, end to end (design §10.2/§10.4).

        Proves the documented sequence against a real server: first-frame
        auth with an API key, subscribe, acknowledge, unsubscribe. The
        live-push path (a batch landing → ``data.update``) is covered by
        ``tests/test_data_subscribe.py`` inside the app process.
        """
        from opendata_client import SubscriptionError

        client = OpendataClient(live_server, api_key=api_key)
        try:
            with client.subscribe("stock_daily", layer="dwd", timeout=10) as stream:
                assert stream.acknowledged is True
                assert stream.replayed >= 0
        except SubscriptionError as exc:  # pragma: no cover - only on a broken server
            pytest.fail(f"subscription failed: {exc}")
        finally:
            client.close()


class TestSubscriptionProtocol:
    """The socket half of the client, driven through a fake socket."""

    def _client(self) -> OpendataClient:
        return OpendataClient("http://api.test", api_key="od-test-key")

    def test_ws_url_follows_the_base_scheme(self):
        assert self._client()._ws_url() == "ws://api.test/ws/data/subscribe"
        secure = OpendataClient("https://api.test", api_key="od-test-key")
        try:
            assert secure._ws_url() == "wss://api.test/ws/data/subscribe"
        finally:
            secure.close()

    def test_api_key_is_the_credential_when_present(self):
        assert self._client()._credential() == "od-test-key"

    def test_jwt_is_the_credential_when_no_key(self):
        client = OpendataClient("http://api.test", token="jwt-value")
        try:
            assert client._credential() == "jwt-value"
        finally:
            client.close()

    def test_update_parses_a_meta_frame(self):
        from opendata_client import DataUpdate

        update = DataUpdate.from_message(
            {
                "type": "data.update",
                "payload": "meta",
                "domain": "stock_daily",
                "source": "ths",
                "layer": "ods",
                "batch_id": "b-1",
                "window": {"start": "2026-09-23", "end": "2026-09-23"},
                "rows": 12,
                "created_at": "2026-09-24T08:00:00+00:00",
                "replayed": True,
            }
        )

        assert update.domain == "stock_daily"
        assert update.batch_id == "b-1"
        assert update.rows == 12
        assert update.replayed is True
        assert update.data == ()

    def test_events_yield_updates_and_raise_on_error_frames(self):
        from opendata_client import DataUpdate, SubscriptionError

        class _Socket:
            def __init__(self, frames):
                self._frames = iter(frames)

            def __iter__(self):
                return self._frames

        client = self._client()
        try:
            good = _Socket(
                [
                    json.dumps({"type": "ping"}),
                    json.dumps({"type": "data.update", "domain": "stock_daily", "batch_id": "b-1"}),
                ]
            )
            session = SubscriptionSession(client, "stock_daily")
            session._socket = good
            updates = list(session)
            assert [item.batch_id for item in updates] == ["b-1"]
            assert isinstance(updates[0], DataUpdate)

            bad = _Socket(
                [
                    json.dumps(
                        {
                            "type": "error",
                            "code": "DOMAIN_FORBIDDEN",
                            "message": "无权读取",
                            "suggestion": "换 Key",
                        }
                    )
                ]
            )
            failing = SubscriptionSession(client, "stock_daily")
            failing._socket = bad
            with pytest.raises(SubscriptionError, match="DOMAIN_FORBIDDEN"):
                list(failing)
        finally:
            client.close()

    def test_expect_raises_when_the_server_reports_an_error(self):
        from opendata_client import SubscriptionError

        class _Socket:
            def __iter__(self):
                return iter([json.dumps({"type": "error", "code": "AUTH_FAILED", "message": "no"})])

        client = self._client()
        try:
            with pytest.raises(SubscriptionError, match="AUTH_FAILED"):
                client._expect(_Socket(), "auth.ok")
        finally:
            client.close()

    def test_expect_raises_when_the_socket_closes_first(self):
        from opendata_client import SubscriptionError

        class _Socket:
            def __iter__(self):
                return iter([])

        client = self._client()
        try:
            with pytest.raises(SubscriptionError, match="closed before auth.ok"):
                client._expect(_Socket(), "auth.ok")
        finally:
            client.close()

    def test_asset_class_is_looked_up_once_and_remembered(self):
        from opendata_client import NotFoundError

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200,
                json=[
                    {"domain": "stock_daily", "asset_class": "equity"},
                    {"domain": "economy_cpi", "asset_class": "economy"},
                ],
            )

        client = OpendataClient(
            "http://api.test", api_key="od-test-key", transport=httpx.MockTransport(handler)
        )
        try:
            assert client._asset_class_for("stock_daily") == "equity"
            assert client._asset_class_for("economy_cpi") == "economy"
            assert calls == ["/api/v1/data/capabilities"]  # cached after the first look
            with pytest.raises(NotFoundError):
                client._asset_class_for("not_a_domain")
        finally:
            client.close()

    def test_pull_attaches_the_batch_rows(self):
        from opendata_client import DataUpdate

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/capabilities"):
                return httpx.Response(
                    200, json=[{"domain": "stock_daily", "asset_class": "equity"}]
                )
            assert dict(request.url.params)["start"] == "2026-09-23"
            return _envelope(
                {
                    "rows": [{"symbol": "600519", "close": 1.0}],
                    "columns": ["symbol", "close"],
                    "page": 1,
                    "page_size": 500,
                    "count": 1,
                }
            )

        client = OpendataClient(
            "http://api.test", api_key="od-test-key", transport=httpx.MockTransport(handler)
        )
        try:
            update = client._with_rows(
                DataUpdate(
                    domain="stock_daily",
                    source="ths",
                    layer="ods",
                    batch_id="b-1",
                    window={"start": "2026-09-23", "end": "2026-09-23"},
                    rows=1,
                    created_at="2026-09-24T08:00:00+00:00",
                ),
                "stock_daily",
                None,
                500,
            )
        finally:
            client.close()

        assert update.data == ({"symbol": "600519", "close": 1.0},)

    def test_pull_leaves_a_full_payload_alone(self):
        from opendata_client import DataUpdate

        client = self._client()
        try:
            update = client._with_rows(
                DataUpdate(
                    domain="stock_daily",
                    source="ths",
                    layer="ods",
                    batch_id="b-1",
                    window={},
                    rows=1,
                    created_at="x",
                    payload="full",
                    data=({"symbol": "600519"},),
                ),
                "stock_daily",
                "equity",
                500,
            )
        finally:
            client.close()

        assert update.data == ({"symbol": "600519"},)


class TestProviderModelMetadata:
    def test_provider_models_filters_by_source_and_uses_api_key(self):
        descriptor = {"source": "akshare", "model": "stock_daily", "domain": "stock_daily"}
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope([descriptor])

        with _client(handler) as client:
            assert client.provider_models(source="akshare") == [descriptor]

        assert len(seen) == 1
        assert seen[0].url.path == "/api/v1/providers/models"
        assert seen[0].url.query == b"source=akshare"
        assert seen[0].headers["x-api-key"] == "od-test-key"

    def test_provider_models_without_filter_accepts_empty_directory(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope([])

        with _client(handler) as client:
            assert client.provider_models() == []

        assert seen[0].url.path == "/api/v1/providers/models"
        assert seen[0].url.query == b""
        assert seen[0].headers["x-api-key"] == "od-test-key"

    def test_provider_model_schema_preserves_complete_schema(self):
        payload = {
            "source": "akshare",
            "model": "stock_daily",
            "domain": "stock_daily",
            "capability_identity": "equity.stock_daily",
            "verified": True,
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$ref": "#/$defs/Query",
                "$defs": {
                    "Query": {
                        "type": "object",
                        "properties": {
                            "symbols": {"type": "array", "default": []},
                            "adjust": {"enum": ["none", "qfq", "hfq"], "nullable": True},
                        },
                    }
                },
            },
        }
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(payload)

        with _client(handler) as client:
            result = client.provider_model_schema("akshare", "stock_daily")

        assert result == payload
        assert result["schema"] == payload["schema"]
        assert len(seen) == 1
        assert seen[0].url.path == "/api/v1/providers/akshare/models/stock_daily/schema"
        assert seen[0].url.query == b""
        assert seen[0].headers["x-api-key"] == "od-test-key"

    def test_provider_model_schema_accepts_unicode_identifiers(self):
        payload = {"source": "数据源", "model": "日线_行情", "schema": {"type": "object"}}
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(payload)

        with _client(handler) as client:
            assert client.provider_model_schema("数据源", "日线_行情") == payload

        assert len(seen) == 1

    @pytest.mark.parametrize(
        ("method", "args", "kwargs"),
        [
            ("provider_models", (), {"source": "auto"}),
            ("provider_models", (), {"source": "AUTO"}),
            ("provider_models", (), {"source": "class"}),
            ("provider_models", (), {"source": "bad/name"}),
            ("provider_models", (), {"source": "bad?source=other"}),
            ("provider_models", (), {"source": 7}),
            ("provider_model_schema", ("auto", "model"), {}),
            ("provider_model_schema", ("source", "class"), {}),
            ("provider_model_schema", ("source", "bad/model"), {}),
            ("provider_model_schema", (None, "model"), {}),
        ],
    )
    def test_invalid_provider_model_identity_fails_before_transport(self, method, args, kwargs):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope([])

        with (
            _client(handler) as client,
            pytest.raises(ValueError, match="valid provider/model identifier"),
        ):
            getattr(client, method)(*args, **kwargs)

        assert seen == []

    @pytest.mark.parametrize("payload", [None, {}, ["not-a-descriptor"], [{}, "bad"]])
    def test_provider_models_rejects_malformed_payload(self, payload):
        with (
            _client(lambda request: _envelope(payload)) as client,
            pytest.raises(OpendataClientError, match="provider models response"),
        ):
            client.provider_models()

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            {"source": "akshare", "model": "stock_daily"},
            {"source": "akshare", "model": "stock_daily", "schema": []},
            {"source": "other", "model": "stock_daily", "schema": {}},
            {"source": "akshare", "model": "other", "schema": {}},
        ],
    )
    def test_provider_model_schema_rejects_bad_shape_or_identity(self, payload):
        with (
            _client(lambda request: _envelope(payload)) as client,
            pytest.raises(OpendataClientError, match="provider model schema response"),
        ):
            client.provider_model_schema("akshare", "stock_daily")

    @pytest.mark.parametrize(
        ("status", "error_type"),
        [
            (400, InvalidQueryError),
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, NotFoundError),
            (429, OpendataClientError),
            (500, OpendataClientError),
        ],
    )
    @pytest.mark.parametrize("method", ["models", "schema"])
    def test_provider_model_endpoints_reuse_http_error_mapping(self, method, status, error_type):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": "metadata request denied"})

        with (
            _client(handler) as client,
            pytest.raises(error_type, match=f"HTTP {status}: metadata request denied"),
        ):
            if method == "models":
                client.provider_models()
            else:
                client.provider_model_schema("akshare", "stock_daily")

    def test_sdk_cold_import_does_not_load_opendata_application(self, tmp_path):
        sdk_source = Path(__file__).resolve().parents[1] / "opendata_client"
        code = """
import sys
from pathlib import Path
import opendata_client
assert Path(opendata_client.__file__).resolve().parent == Path(sys.argv[1]) / "opendata_client"
assert not any(name == "opendata" or name.startswith("opendata.") for name in sys.modules)
"""
        env = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
            env.pop(name, None)
        env["PYTHONPATH"] = str(sdk_source)
        env["PYTHONNOUSERSITE"] = "1"

        result = subprocess.run(  # noqa: S603  # fixed interpreter and constant script
            [sys.executable, "-c", code, str(sdk_source)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=False,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr


class TestProviderModelQuery:
    def test_fred_revision_query_uses_exact_post_body_and_does_not_add_context(self):
        query = {
            "series_id": "GDP",
            "realtime_start": "2024-01-01",
            "realtime_end": "2024-12-31",
            "vintage_date": "2024-06-01",
            "optional_filter": None,
            "extension": {"preserve": [1, None, {"note": "unchanged"}]},
        }
        response_data = {
            "source": "fred",
            "model": "SeriesObservations",
            "domain": "fred_series",
            "verified": True,
            "observed_at": "2026-10-08T02:00:00+00:00",
            "completeness": "NOT_ASSESSED",
            "results": [
                {
                    "series_id": "GDP",
                    "date": "2024-01-01",
                    "value": "123.4",
                    "realtime_start": "2024-06-01",
                    "realtime_end": "2024-06-01",
                    "metadata": {"vintage": "2024-06-01", "footnotes": None},
                }
            ],
            "pagination": None,
        }
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(response_data)

        with _client(handler) as client:
            result = client.query_provider_model("fred", "SeriesObservations", query)

        assert result == response_data
        assert seen[0].method == "POST"
        assert seen[0].url.path == "/api/v1/providers/fred/models/SeriesObservations/query"
        assert seen[0].headers["x-api-key"] == "od-test-key"
        body = json.loads(seen[0].content)
        assert body == {"query": query}
        assert set(body) == {"query"}
        assert "ctx" not in body and "budget" not in body and "grant" not in body

    def test_bls_search_preserves_items_as_results_and_page_metadata(self):
        query = {"survey": "ce", "offset": 2, "limit": 1, "source": "auto"}
        response_data = {
            "source": "bls",
            "model": "BlsSearch",
            "domain": "bls_search",
            "verified": False,
            "observed_at": "2026-10-08T02:00:00+00:00",
            "completeness": "NOT_ASSESSED",
            "results": [
                {
                    "series_id": "CES0000000001",
                    "title": "Average hourly earnings",
                    "dimensions": {"industry_code": "000000"},
                    "source_metadata": {"source_file": "ce.series"},
                    "footnotes": [{"code": "P", "text": "Preliminary"}],
                    "catalog_as_of": None,
                }
            ],
            "pagination": {"total": 7, "offset": 2, "limit": 1},
        }
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(response_data)

        with _client(handler) as client:
            result = client.query_provider_model("bls", "BlsSearch", query)

        assert result == response_data
        assert result["results"][0]["footnotes"] == [{"code": "P", "text": "Preliminary"}]
        assert result["pagination"] == {"total": 7, "offset": 2, "limit": 1}
        assert seen[0].url.path == "/api/v1/providers/bls/models/BlsSearch/query"
        assert json.loads(seen[0].content) == {"query": query}

    def test_empty_page_beyond_total_is_a_valid_pagination_result(self):
        response_data = {
            "source": "bls",
            "model": "BlsSearch",
            "domain": "bls_search",
            "verified": False,
            "observed_at": "2026-10-08T02:00:00+00:00",
            "completeness": "NOT_ASSESSED",
            "results": [],
            "pagination": {"total": 0, "offset": 50, "limit": 10},
        }

        with _client(lambda request: _envelope(response_data)) as client:
            result = client.query_provider_model("bls", "BlsSearch", {"offset": 50, "limit": 10})

        assert result == response_data

    def test_large_fmp_timestamp_and_nullable_metadata_round_trip(self):
        timestamp = 2**53 + 123
        query = {
            "symbol": "AAPL",
            "timestamp": timestamp,
            "optional": None,
            "enabled": True,
            "ratio": 1.25,
            "filters": [{"kind": "raw", "optional": None}],
        }
        original_query = copy.deepcopy(query)
        response_data = {
            "source": "fmp",
            "model": "HistoricalPriceFull",
            "domain": "equity_price_history",
            "verified": True,
            "observed_at": "2026-10-08T02:00:00+00:00",
            "completeness": "NOT_ASSESSED",
            "results": [
                {
                    "symbol": "AAPL",
                    "timestamp": timestamp,
                    "price": None,
                    "metadata": {"provider_fields": ["timestamp", None]},
                }
            ],
            "pagination": None,
        }
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(response_data)

        with _client(handler) as client:
            result = client.query_provider_model("fmp", "HistoricalPriceFull", query)

        assert result == response_data
        assert result["results"][0]["timestamp"] == timestamp
        assert result["results"][0]["price"] is None
        assert json.loads(seen[0].content) == {"query": query}
        assert query == original_query

    def test_numeric_string_object_keys_are_retained_without_mutation(self):
        query = {"1": "first", "01": "second", "nested": [{"2": "third"}]}
        original_query = copy.deepcopy(query)
        response_data = {
            "source": "fred",
            "model": "SeriesObservations",
            "results": [],
            "pagination": None,
        }
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope(response_data)

        with _client(handler) as client:
            result = client.query_provider_model("fred", "SeriesObservations", query)

        assert result == response_data
        assert json.loads(seen[0].content) == {"query": query}
        assert query == original_query

    def test_non_string_object_keys_fail_before_post_without_mutation(self):
        queries = [
            {1: "first", "1": "second"},
            {"nested": {1: "numeric key"}},
            {"nested": [{None: "null key"}]},
            {"nested": [{"deeper": {False: "boolean key"}}]},
        ]
        snapshots = [copy.deepcopy(query) for query in queries]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope({})

        with _client(handler) as client:
            for query in queries:
                with pytest.raises(ValueError) as error:
                    client.query_provider_model("fred", "SeriesObservations", query)
                assert error.value.__cause__ is None
                assert error.value.__context__ is None

        assert seen == []
        assert queries == snapshots

    def test_cyclic_and_over_deep_queries_fail_before_post(self):
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic

        deep_value: list[object] = []
        cursor = deep_value
        for _ in range(300):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        queries = [cyclic, {"deep": deep_value}]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope({})

        with _client(handler) as client:
            for query in queries:
                with pytest.raises(ValueError) as error:
                    client.query_provider_model("fred", "SeriesObservations", query)
                assert error.value.__cause__ is None
                assert error.value.__context__ is None

        assert seen == []

    @pytest.mark.parametrize(
        ("source", "model"),
        [
            ("auto", "Model"),
            ("Source", "auto"),
            ("bad/source", "Model"),
            ("Source", "bad?model=x"),
            (None, "Model"),
            ("Source", 7),
        ],
    )
    def test_invalid_identity_fails_before_post(self, source, model):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope({})

        with (
            _client(handler) as client,
            pytest.raises(ValueError, match="valid provider/model identifier"),
        ):
            client.query_provider_model(source, model, {})

        assert seen == []

    @pytest.mark.parametrize(
        "query",
        [
            None,
            [],
            {"non_finite": float("nan")},
            {"non_finite": float("inf")},
            {"not_json": object()},
            {"unpaired_surrogate": "\ud800"},
        ],
    )
    def test_non_json_or_non_finite_query_fails_before_post(self, query):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _envelope({})

        with _client(handler) as client, pytest.raises(ValueError) as error:
            client.query_provider_model("fred", "SeriesObservations", query)

        assert seen == []
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    @pytest.mark.parametrize(
        ("status", "error_type"),
        [
            (400, InvalidQueryError),
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, NotFoundError),
            (429, OpendataClientError),
            (501, UnsupportedQueryError),
            (503, OpendataClientError),
            (504, OpendataClientError),
        ],
    )
    def test_http_errors_reuse_status_mapping(self, status, error_type):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"detail": "query rejected"})

        with (
            _client(handler) as client,
            pytest.raises(error_type, match=f"HTTP {status}: query rejected"),
        ):
            client.query_provider_model("fred", "SeriesObservations", {"series_id": "GDP"})

    def test_transport_error_does_not_expose_secret_exception_text_or_cause(self):
        secret = "FAKE_PROVIDER_SECRET_DO_NOT_LEAK"

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connection failed: {secret}", request=request)

        with _client(handler) as client, pytest.raises(OpendataClientError) as error:
            client.query_provider_model("fred", "SeriesObservations", {"series_id": secret})

        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    def test_invalid_json_does_not_expose_response_text_or_cause(self):
        secret = "FAKE_RESPONSE_SECRET_DO_NOT_LEAK"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=f"not-json {secret}".encode(),
                headers={"content-type": "application/json"},
            )

        with _client(handler) as client, pytest.raises(OpendataClientError) as error:
            client.query_provider_model("fred", "SeriesObservations", {"series_id": "GDP"})

        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    def test_failed_success_envelope_does_not_expose_message_or_cause(self):
        secret = "FAKE_ENVELOPE_SECRET_DO_NOT_LEAK"

        with (
            _client(lambda request: _envelope(None, success=False, message=secret)) as client,
            pytest.raises(OpendataClientError) as error,
        ):
            client.query_provider_model("fred", "SeriesObservations", {"series_id": "GDP"})

        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    @pytest.mark.parametrize(
        "data",
        [
            [],
            {"source": "other", "model": "SeriesObservations", "results": [], "pagination": None},
            {"source": "fred", "model": "other", "results": [], "pagination": None},
            {"source": "fred", "model": "SeriesObservations", "results": "bad", "pagination": None},
            {"source": "fred", "model": "SeriesObservations", "results": [[]], "pagination": None},
            {"source": "fred", "model": "SeriesObservations", "results": []},
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [],
                "pagination": [],
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [],
                "pagination": {"total": 1, "offset": 0},
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [],
                "pagination": {"total": True, "offset": 0, "limit": 1},
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [],
                "pagination": {"total": 1, "offset": -1, "limit": 1},
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [],
                "pagination": {"total": 1, "offset": 0, "limit": 0},
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [{"value": 1}, {"value": 2}],
                "pagination": {"total": 2, "offset": 0, "limit": 1},
            },
            {
                "source": "fred",
                "model": "SeriesObservations",
                "results": [{"value": 1}],
                "pagination": {"total": 1, "offset": 1, "limit": 1},
            },
        ],
    )
    def test_malformed_query_response_shape_fails_safely(self, data):
        secret = "FAKE_SHAPE_SECRET_DO_NOT_LEAK"

        def handler(request: httpx.Request) -> httpx.Response:
            return _envelope(data)

        with _client(handler) as client, pytest.raises(OpendataClientError) as error:
            client.query_provider_model("fred", "SeriesObservations", {"series_id": secret})

        assert secret not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
