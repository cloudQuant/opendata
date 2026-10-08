"""Offline integration contracts closing Iteration 01 acceptance gaps.

These checks use temporary SQLite files, mocked provider transport, and loopback
HTTP/WebSocket servers only. They do not connect to configured production
databases, provider services, Redis, or SMTP.

The ODS/WebSocket case keeps the production OdsWriter, DataPipeline runner,
BatchNotifier, subscription hub, and socket route. Its SQLite-only seams adapt
MySQL ODS upsert SQL to SQLite ON CONFLICT, convert pandas Timestamp values to
Python datetime for SQLite DBAPI binding, and adapt the MySQL watermark upsert
to SQLite SQL. The row-commit assertion runs before notification publication.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _serve(app):
    """Serve a test-only FastAPI app on a free loopback port with real sockets."""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("local test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.integration
def test_pipeline_commits_ods_then_notifies_a_real_websocket_subscriber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real runner/notifier/hub/socket chain against temporary stores."""
    from fastapi import FastAPI
    from websockets.sync.client import connect

    from opendata.api import data_subscribe
    from opendata.api.dependencies import Principal
    from opendata.core.database import Base
    from opendata.models.pipeline import PipelineProgress, ShardStatus
    from opendata.pipeline import notify as notify_module
    from opendata.pipeline import ods_writer as ods_writer_module
    from opendata.pipeline.notify import BatchNotifier
    from opendata.pipeline.ods_writer import OdsWriter
    from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window
    from opendata.pipeline.subscription import hub

    main_path = tmp_path / "main.sqlite"
    warehouse_path = tmp_path / "warehouse.sqlite"
    async_url = f"sqlite+aiosqlite:///{main_path}"
    async_engine = create_async_engine(async_url, poolclass=NullPool)

    async def create_main_tables() -> None:
        async with async_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_main_tables())
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    warehouse = create_engine(
        f"sqlite:///{warehouse_path}", connect_args={"check_same_thread": False}
    )
    batch_id = "1a5cb74c-9b07-45e4-b572-0987507af611"
    with warehouse.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE `ods_stock_daily_ths` ("
                "`symbol` TEXT NOT NULL, `trade_date` DATE NOT NULL, `close` REAL NOT NULL, "
                "`_source` TEXT NOT NULL, `_fetched_at` DATETIME NOT NULL, "
                "`_batch_id` TEXT NOT NULL, "
                "PRIMARY KEY (`symbol`, `trade_date`))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE `batch_watermark` ("
                "`seq` INTEGER PRIMARY KEY AUTOINCREMENT, `batch_id` TEXT NOT NULL UNIQUE, "
                "`domain` TEXT NOT NULL, `source` TEXT NOT NULL, `layer` TEXT NOT NULL, "
                "`window_start` DATE, `window_end` DATE, `rows_written` INTEGER NOT NULL, "
                "`created_at` DATETIME NOT NULL)"
            )
        )

    write_sequence: list[tuple[str, int]] = []
    date_in_window = date(2026, 9, 23)
    ods_writer = OdsWriter(warehouse, batch_size=100, mode="direct")

    def sqlite_upsert_sql(table: str, columns: list[str], key: tuple[str, ...]) -> str:
        quoted_table = ods_writer_module._quote(table)
        quoted_columns = ", ".join(ods_writer_module._quote(column) for column in columns)
        placeholders = ", ".join(f":{column}" for column in columns)
        conflict_key = ", ".join(ods_writer_module._quote(column) for column in key)
        updates = [column for column in columns if column not in key] or list(columns)
        assignments = ", ".join(
            f"{ods_writer_module._quote(column)} = excluded.{ods_writer_module._quote(column)}"
            for column in updates
        )
        return (
            f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({placeholders}) "  # noqa: S608
            f"ON CONFLICT ({conflict_key}) DO UPDATE SET {assignments}"
        )

    monkeypatch.setattr(ods_writer_module, "build_upsert_sql", sqlite_upsert_sql)
    original_records = ods_writer_module._records

    def sqlite_records(frame: pd.DataFrame) -> list[dict[str, object]]:
        """Convert pandas timestamps for sqlite3's narrower DBAPI bind support."""
        return [
            {
                name: value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
                for name, value in row.items()
            }
            for row in original_records(frame)
        ]

    monkeypatch.setattr(ods_writer_module, "_records", sqlite_records)

    def write_ods(frame: pd.DataFrame) -> int:
        """Use production ODS writer orchestration with local SQLite adapters."""
        result = ods_writer.write(
            frame,
            table="ods_stock_daily_ths",
            key=("symbol", "trade_date"),
            source="ths",
            batch_id=batch_id,
        )
        with warehouse.connect() as connection:
            committed = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM `ods_stock_daily_ths` WHERE `_batch_id` = :batch_id"
                    ),
                    {"batch_id": batch_id},
                ).scalar_one()
            )
        write_sequence.append(("ods_committed", committed))
        return result.rows

    def sqlite_record_batch(engine, watermark) -> None:
        """Adapt only the MySQL watermark upsert for this SQLite fixture."""
        with engine.connect() as connection:
            committed = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM `ods_stock_daily_ths` WHERE `_batch_id` = :batch_id"
                    ),
                    {"batch_id": watermark.batch_id},
                ).scalar_one()
            )
        write_sequence.append(("notify_after_commit", committed))
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO `batch_watermark` "
                    "(`batch_id`, `domain`, `source`, `layer`, `window_start`, `window_end`, "
                    "`rows_written`, `created_at`) "
                    "VALUES (:batch_id, :domain, :source, :layer, :window_start, :window_end, "
                    ":rows, :created_at) ON CONFLICT(`batch_id`) DO UPDATE SET "
                    "`rows_written` = excluded.`rows_written`"
                ),
                {
                    "batch_id": watermark.batch_id,
                    "domain": watermark.domain,
                    "source": watermark.source,
                    "layer": watermark.layer,
                    "window_start": watermark.window_start,
                    "window_end": watermark.window_end,
                    "rows": watermark.rows,
                    "created_at": watermark.created_at,
                },
            )

    async def resolve_test_principal(token: str):
        if token != "local-socket-test-token":
            return None
        return Principal(user=SimpleNamespace(username="iteration01-local-subscriber"), scopes=None)

    monkeypatch.setattr(notify_module, "record_batch", sqlite_record_batch)
    monkeypatch.setattr(data_subscribe, "principal_resolver", resolve_test_principal)
    monkeypatch.setattr(data_subscribe, "warehouse_engine_factory", lambda: warehouse)

    def fetch_symbol(symbol: str, _window: Window) -> pd.DataFrame:
        return pd.DataFrame([{"symbol": symbol, "trade_date": date_in_window, "close": 123.45}])

    pipeline = DataPipeline(
        PipelineSpec(
            domain="stock_daily",
            source="ths",
            key=("symbol", "trade_date"),
            symbols=("ITER01_SOCKET_PROBE",),
            variant="iteration01-real-socket",
        ),
        session_maker=sessions,
        write_ods=write_ods,
        fetch_symbol=fetch_symbol,
        notify=BatchNotifier(
            warehouse,
            batch_id=batch_id,
            layer="ods",
            table="ods_stock_daily_ths",
        ),
    )
    app = FastAPI()
    app.include_router(data_subscribe.router)
    pipeline_ids: list[str] = []

    @app.post("/__test/run-pipeline")
    async def run_pipeline() -> dict[str, object]:
        outcome = await pipeline.run(Window(start=date_in_window, end=date_in_window), resume=False)
        pipeline_ids.append(outcome.pipeline_id)
        return {
            "shards_done": outcome.shards_done,
            "rows_written": outcome.rows_written,
            "failures": [failure.error for failure in outcome.failures],
        }

    subscriber_baseline = hub.subscriber_count
    try:
        with (
            _serve(app) as base_url,
            connect(
                base_url.replace("http://", "ws://") + "/ws/data/subscribe", open_timeout=5
            ) as subscriber,
        ):
            subscriber.send(json.dumps({"action": "auth", "token": "local-socket-test-token"}))
            authenticated = json.loads(subscriber.recv(timeout=5))
            assert authenticated["type"] == "auth.ok"

            subscriber.send(
                json.dumps(
                    {
                        "action": "subscribe",
                        "domain": "stock_daily",
                        "layer": "ods",
                        "payload": "meta",
                    }
                )
            )
            acknowledgement = json.loads(subscriber.recv(timeout=5))
            assert acknowledgement["type"] == "subscribe.ok"
            assert acknowledgement["replayed"] == 0
            assert hub.subscriber_count == subscriber_baseline + 1

            response = httpx.post(base_url + "/__test/run-pipeline", timeout=10, trust_env=False)
            assert response.status_code == 200, response.text
            assert response.json() == {"shards_done": 1, "rows_written": 1, "failures": []}

            message = json.loads(subscriber.recv(timeout=10))

        assert message["type"] == "data.update"
        assert message["payload"] == "meta"
        assert message["domain"] == "stock_daily"
        assert message["source"] == "ths"
        assert message["layer"] == "ods"
        assert message["batch_id"] == batch_id
        assert message["rows"] == 1
        assert message["window"] == {
            "start": date_in_window.isoformat(),
            "end": date_in_window.isoformat(),
        }
        assert write_sequence == [("ods_committed", 1), ("notify_after_commit", 1)]

        unsubscribe_deadline = time.monotonic() + 5
        while (
            hub.subscriber_count != subscriber_baseline and time.monotonic() < unsubscribe_deadline
        ):
            time.sleep(0.01)
        assert hub.subscriber_count == subscriber_baseline

        assert len(pipeline_ids) == 1

        async def read_progress_rows() -> list[PipelineProgress]:
            async with sessions() as session:
                result = await session.execute(
                    select(PipelineProgress).where(
                        PipelineProgress.pipeline_id == pipeline_ids[0],
                        PipelineProgress.domain == "stock_daily",
                        PipelineProgress.source == "ths",
                        PipelineProgress.window_start == date_in_window,
                        PipelineProgress.window_end == date_in_window,
                    )
                )
                return list(result.scalars().all())

        progress_rows = asyncio.run(read_progress_rows())
        assert len(progress_rows) == 1
        assert progress_rows[0].shard == 0
        assert progress_rows[0].status is ShardStatus.DONE
        assert progress_rows[0].rows_written == 1
    finally:
        warehouse.dispose()
        asyncio.run(async_engine.dispose())


@pytest.mark.integration
def test_missing_yfinance_sdk_does_not_block_ecb_registration_routing_or_fetch() -> None:
    """A clean child process blocks only yfinance and mocks ECB's HTTP transport."""
    child = r"""
import builtins
import json
import os
from datetime import date

blocked = []
original_import = builtins.__import__
def block_yfinance(name, *args, **kwargs):
    if name == "yfinance" or name.startswith("yfinance."):
        blocked.append(name)
        raise ImportError("intentional optional-SDK block")
    return original_import(name, *args, **kwargs)
builtins.__import__ = block_yfinance

from requests import Response, Session
from requests.adapters import BaseAdapter
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.registry import ProviderRegistry
from opendata.data.providers.yfinance import register as register_yfinance
from opendata.data.providers.ecb import register as register_ecb
from opendata.data.providers.ecb.models import _client as ecb_client
from opendata.data.providers.yfinance.models import _sdk
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError

registry = ProviderRegistry()
yfinance_registered = register_yfinance(registry)
ecb_registered = register_ecb(registry)
assert yfinance_registered and ecb_registered
yf_capability = next(cap for cap in registry.capabilities() if cap.source == "yfinance")
assert registry.resolve_domain(
    yf_capability.domain,
    source="yfinance",
    period=yf_capability.period,
    market=yf_capability.market,
).capability == yf_capability

try:
    _sdk.require_history_frame("AAPL", start=None, end=None, timeout=None)
except YfinanceProviderError as exc:
    assert exc.code == "YFINANCE_SDK_MISSING"
else:
    raise AssertionError("the optional yfinance import was not blocked")
assert blocked == ["yfinance"]

class FixtureAdapter(BaseAdapter):
    def __init__(self):
        self.urls = []
    def send(self, request, **kwargs):
        self.urls.append(request.url)
        response = Response()
        response.status_code = 200
        response._content = (
            b"KEY,TIME_PERIOD,OBS_VALUE\n"
            b"MNA.Q.N.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.N,2025-Q1,3222796.1\n"
        )
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response
    def close(self):
        pass

adapter = FixtureAdapter()
session = Session()
session.trust_env = False
session.mount("https://", adapter)
class NoCache:
    def get(self, **kwargs):
        return None
    def put(self, **kwargs):
        pass

client = GovernedHttpClient(
    HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
    session=session,
    raw_response_cache=NoCache(),
)
ecb_client.get_shared_http_client = lambda: client
fetcher = registry.resolve_domain(
    "economy_gdp", source="ecb", period="1Q", market="eu"
)
rows = fetcher.fetch(
    ctx=FetchContext(timeout=1.0),
    source="ecb",
    market="eu",
    series_id="MNA.Q.N.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.N",
    start_date=date(2025, 1, 1),
)
assert len(rows) == 1
assert rows[0].date.isoformat() == "2025-01-01"
assert rows[0].value == 3222796.1
assert len(adapter.urls) == 1
assert "ecb.fixture.invalid/service/data/" in adapter.urls[0]
assert not any(name.startswith("yfinance") for name in __import__("sys").modules)
print(json.dumps({
    "yfinance_registered": True,
    "ecb_registered": True,
    "route": "ecb",
    "rows": len(rows),
}))
"""
    env = os.environ.copy()
    env["ECB_API_BASE_URL"] = "https://ecb.fixture.invalid"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT), env.get("PYTHONPATH", "")) if part
    )
    completed = subprocess.run(  # noqa: S603  # fixed local interpreter and inline test code
        [sys.executable, "-c", child],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "yfinance_registered": True,
        "ecb_registered": True,
        "route": "ecb",
        "rows": 1,
    }


def _consumer_app_and_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the real consumer query route on isolated SQLite stores."""
    from fastapi import FastAPI

    from opendata.api import data_query
    from opendata.core.api_key_rate_limit import APIKeyRateLimiter, LocalSlidingWindowBackend
    from opendata.core.database import Base, get_db
    from opendata.data.providers.ths import register as register_ths
    from opendata.data.registry import ProviderRegistry
    from opendata.models import ApiKey, User
    from opendata.services.api_key_service import ApiKeyService

    # Import the models so their tables are registered in Base.metadata.
    assert ApiKey.__tablename__ == "api_keys"
    main_path = tmp_path / "consumer-main.sqlite"
    warehouse_path = tmp_path / "consumer-warehouse.sqlite"
    async_url = f"sqlite+aiosqlite:///{main_path}"
    async_engine = create_async_engine(async_url, poolclass=NullPool)

    async def initialize() -> str:
        async with async_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(async_engine, expire_on_commit=False)
        async with maker() as session:
            owner = User(
                username="iteration01-consumer",
                email="iteration01-consumer@example.invalid",
                hashed_password="fixture-only",
                is_active=True,
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)
            issued = await ApiKeyService(session).issue(
                owner=owner,
                name="iteration01 temporary consumer",
                scopes=["stock_daily"],
                rate_limit=100,
            )
            return issued.plaintext

    api_key = asyncio.run(initialize())
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def get_temp_db():
        async with sessions() as session:
            yield session

    warehouse = create_engine(
        f"sqlite:///{warehouse_path}", connect_args={"check_same_thread": False}
    )
    with warehouse.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE `dwd_stock_daily` ("
                "`symbol` TEXT NOT NULL, `trade_date` DATE NOT NULL, `open` REAL NOT NULL, "
                "`high` REAL NOT NULL, `low` REAL NOT NULL, `close` REAL NOT NULL, "
                "`volume` REAL NOT NULL, `amount` REAL, `source` TEXT NOT NULL, "
                "`_merged_at` DATETIME NOT NULL, `_diff_flag` INTEGER NOT NULL, "
                "`_as_of` DATE NOT NULL, PRIMARY KEY (`symbol`, `trade_date`))"
            )
        )
        dates = []
        current = date(2024, 1, 2)
        while len(dates) < 40:
            if current.weekday() < 5:
                dates.append(current)
            current += timedelta(days=1)
        connection.execute(
            text(
                "INSERT INTO `dwd_stock_daily` "
                "(`symbol`, `trade_date`, `open`, `high`, `low`, `close`, `volume`, `amount`, "
                "`source`, `_merged_at`, `_diff_flag`, `_as_of`) "
                "VALUES (:symbol, :trade_date, :open, :high, :low, :close, :volume, :amount, "
                ":source, :merged_at, 0, :as_of)"
            ),
            [
                {
                    "symbol": "ITER01_CONSUMER_PROBE",
                    "trade_date": day.isoformat(),
                    "open": 10.0 + index,
                    "high": 10.5 + index,
                    "low": 9.5 + index,
                    "close": 10.1 + index + (0.4 if index % 2 else 0.0),
                    "volume": 1_000 + index,
                    "amount": (10.1 + index) * (1_000 + index),
                    "source": "ths",
                    "merged_at": "2024-03-01T00:00:00",
                    "as_of": day.isoformat(),
                }
                for index, day in enumerate(dates)
            ],
        )

    registry = ProviderRegistry()
    register_ths(registry)
    monkeypatch.setattr(data_query, "get_registry", lambda: registry)
    monkeypatch.setattr(
        "opendata.api.dependencies.api_key_rate_limiter",
        APIKeyRateLimiter(backend=LocalSlidingWindowBackend()),
    )
    app = FastAPI()
    app.include_router(data_query.router, prefix="/api/v1/data")
    app.dependency_overrides[get_db] = get_temp_db
    app.dependency_overrides[data_query.get_warehouse_engine] = lambda: warehouse
    api_calls: list[str] = []

    @app.middleware("http")
    async def count_queries(request, call_next):
        if request.url.path == "/api/v1/data/equity/stock_daily":
            api_calls.append(request.url.path)
        return await call_next(request)

    first_date = dates[0].isoformat()
    last_date = dates[-1].isoformat()
    return app, warehouse, async_engine, api_key, api_calls, first_date, last_date


def _run_consumer_example(
    base_url: str,
    api_key: str,
    *,
    symbol: str,
    start: str,
    end: str,
    backtest: bool,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["OPENDATA_BASE_URL"] = base_url
    env["OPENDATA_API_KEY"] = api_key
    env["NO_PROXY"] = ",".join(filter(None, [env.get("NO_PROXY", ""), "127.0.0.1", "localhost"]))
    env["no_proxy"] = env["NO_PROXY"]
    env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(ROOT), str(ROOT / "opendata_client"), env.get("PYTHONPATH", ""))
        if part
    )
    command = [
        sys.executable,
        str(ROOT / "examples" / "consumer_handoff.py"),
        "--symbol",
        symbol,
        "--start",
        start,
        "--end",
        end,
    ]
    if backtest:
        command.extend(["--fast", "2", "--slow", "5"])
    else:
        command.append("--no-backtest")
    return subprocess.run(  # noqa: S603  # fixed local example script and explicit arguments
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.integration
def test_consumer_handoff_example_reads_seeded_rows_from_local_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the documented client example as a child against a temp SQLite API."""
    app, warehouse, async_engine, api_key, api_calls, first_date, last_date = (
        _consumer_app_and_fixtures(tmp_path, monkeypatch)
    )
    symbol = "ITER01_CONSUMER_PROBE"
    try:
        with _serve(app) as base_url:
            completed = _run_consumer_example(
                base_url,
                api_key,
                symbol=symbol,
                start=first_date,
                end=last_date,
                backtest=False,
            )
        assert completed.returncode == 0, completed.stderr
        assert f"symbol      : {symbol}" in completed.stdout
        assert "calls       : 1" in completed.stdout
        assert "rows        : 40" in completed.stdout
        assert f"date range  : {first_date} .. {last_date}" in completed.stdout
        elapsed_line = next(
            line for line in completed.stdout.splitlines() if line.startswith("elapsed")
        )
        elapsed_seconds = float(elapsed_line.split(":", 1)[1].strip().removesuffix("s"))
        assert elapsed_seconds >= 0
        assert api_calls == ["/api/v1/data/equity/stock_daily"]
        assert "--api-key" not in completed.args
    finally:
        warehouse.dispose()
        asyncio.run(async_engine.dispose())


@pytest.mark.integration
def test_consumer_handoff_example_runs_tiny_backtrader_case_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The optional CLI backtest gets its own run; this test never installs it."""
    import importlib.util

    if importlib.util.find_spec("backtrader") is None:
        pytest.skip("backtrader is not installed in the active Anaconda base environment")

    app, warehouse, async_engine, api_key, api_calls, first_date, last_date = (
        _consumer_app_and_fixtures(tmp_path, monkeypatch)
    )
    symbol = "ITER01_CONSUMER_PROBE"
    try:
        with _serve(app) as base_url:
            completed = _run_consumer_example(
                base_url,
                api_key,
                symbol=symbol,
                start=first_date,
                end=last_date,
                backtest=True,
            )
        assert completed.returncode == 0, completed.stderr
        assert "rows        : 40" in completed.stdout
        assert f"date range  : {first_date} .. {last_date}" in completed.stdout
        assert "backtest    : final value " in completed.stdout
        final_value = float(
            completed.stdout.split("backtest    : final value ", maxsplit=1)[1]
            .splitlines()[0]
            .replace(",", "")
        )
        assert final_value > 0
        assert api_calls == ["/api/v1/data/equity/stock_daily"]
        assert "--api-key" not in completed.args
    finally:
        warehouse.dispose()
        asyncio.run(async_engine.dispose())


def _patch_production_lifespan(
    monkeypatch: pytest.MonkeyPatch, *, enable_scheduler: bool | None
) -> dict[str, list[str]]:
    """Replace only external startup/shutdown seams with no-I/O sentinels."""
    import importlib

    import sqlalchemy
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    main = importlib.import_module("opendata.main")
    state: dict[str, list[str]] = {"init": [], "start": [], "shutdown": [], "close": []}
    main.settings = SimpleNamespace(
        app_name="iteration01-test",
        app_version="test",
        app_env="production",
        secret_key="temporary-test-secret",
        is_production=True,
        database_url_sync="sqlite://not-opened",
        enable_scheduler=enable_scheduler,
        redis_url=None,
        workers=1,
    )
    monkeypatch.setattr(main, "_testing_mode", lambda: False)

    async def init_db() -> None:
        state["init"].append("called")

    async def close_db() -> None:
        state["close"].append("called")

    class NoIoConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class NoIoEngine:
        def connect(self):
            return NoIoConnection()

        def dispose(self) -> None:
            pass

    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main, "close_db", close_db)
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda _url: NoIoEngine())
    monkeypatch.setattr(
        MigrationContext,
        "configure",
        staticmethod(lambda _connection: SimpleNamespace(get_current_revision=lambda: "head")),
    )
    monkeypatch.setattr(
        ScriptDirectory,
        "from_config",
        classmethod(lambda _cls, _config: SimpleNamespace(get_current_head=lambda: "head")),
    )

    from opendata.data import providers

    monkeypatch.setattr(providers, "register_providers", lambda: [])

    async def start_scheduler() -> None:
        state["start"].append("called")

    async def shutdown_scheduler() -> None:
        state["shutdown"].append("called")

    monkeypatch.setattr(main.task_scheduler, "start", start_scheduler)
    monkeypatch.setattr(main.task_scheduler, "shutdown", shutdown_scheduler)
    monkeypatch.setattr(
        "opendata.services.data_acquisition.DataAcquisitionService._akshare_executor",
        SimpleNamespace(shutdown=lambda **_kwargs: None),
    )
    return state


@pytest.mark.integration
async def test_production_lifespan_rejects_unset_scheduler_before_scheduler_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from opendata.pipeline.scheduling import SchedulerConfigError

    state = _patch_production_lifespan(monkeypatch, enable_scheduler=None)
    main = __import__("opendata.main", fromlist=["lifespan"])
    with pytest.raises(SchedulerConfigError, match="ENABLE_SCHEDULER must be set explicitly"):
        async with main.lifespan(FastAPI()):
            pytest.fail("production lifespan entered with an unset scheduler switch")
    assert state["init"] == ["called"]
    assert state["start"] == []
    assert state["shutdown"] == []
    assert state["close"] == []


@pytest.mark.integration
async def test_production_lifespan_starts_and_shuts_down_with_scheduler_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    state = _patch_production_lifespan(monkeypatch, enable_scheduler=False)
    main = __import__("opendata.main", fromlist=["lifespan"])
    entered = False
    async with main.lifespan(FastAPI()):
        entered = True
    assert entered
    assert state["init"] == ["called"]
    assert state["start"] == []
    assert state["shutdown"] == ["called"]
    assert state["close"] == ["called"]
