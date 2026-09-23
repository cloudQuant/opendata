"""Tests for the minimal REST client (A5 prerequisite, design §10.4).

Two layers: the request contract (path, parameters, auth header,
envelope unwrapping, pagination, error mapping) against an httpx mock
transport, and one real round trip over a socket against the running
ASGI app, which is what the consumer hand-off actually does.
"""

import json
import socket
import threading
import time

import httpx
import pytest

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
                ["600519", "000001"], start="2024-01-01", end="2024-01-31", adjust="qfq"
            )

        assert seen["path"] == "/api/v1/data/equity/stock_daily"
        params = seen["params"]
        assert params["symbols"] == "600519,000001"  # sequence -> csv
        assert params["adjust"] == "qfq"
        assert params["layer"] == "dwd"
        assert params["start"] == "2024-01-01"
        assert params["end"] == "2024-01-31"
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
