"""Opt-in offline THS → MySQL ODS/DWD → v1 REST vertical acceptance.

Run only as an isolated acceptance, for example with
``pytest --noconftest -m e2e tests/test_ths_stock_mysql_vertical_acceptance.py``
and the ``OPENDATA_THS_VERTICAL_*`` variables below. Before opting in, create a
new, dedicated, temporary local MySQL 8 instance and use accounts whose grants
are limited exclusively to the two named test schemas. Empty schemas do not
prove that the MySQL instance is dedicated or that its accounts are least
privilege; the explicit isolation confirmation is an operator attestation, not
an automated proof. The test migrates both schemas and leaves them intact for
inspection; it never creates or drops databases. THS HTTP is served only by an
``httpx.MockTransport`` and the test never requests live provider credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from fastapi import FastAPI

    from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
    from opendata.data.request_budget import RequestBudget

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_MIGRATIONS = REPOSITORY_ROOT / "alembic_data"
CONTROL_MIGRATIONS = REPOSITORY_ROOT / "alembic"
MOCK_HOST = "fuyao.test"
MOCK_SYMBOL = "600519"
MOCK_THS_CODE = "600519.SH"
START_DATE = date(2026, 10, 29)
END_DATE = date(2026, 10, 30)
WAREHOUSE_URL_ENV = "OPENDATA_THS_VERTICAL_WAREHOUSE_URL"
WAREHOUSE_DATABASE_ENV = "OPENDATA_THS_VERTICAL_WAREHOUSE_DATABASE"
WAREHOUSE_CONFIRM_ENV = "OPENDATA_THS_VERTICAL_WAREHOUSE_CONFIRM_EMPTY"
CONTROL_URL_ENV = "OPENDATA_THS_VERTICAL_CONTROL_URL"
CONTROL_DATABASE_ENV = "OPENDATA_THS_VERTICAL_CONTROL_DATABASE"
CONTROL_CONFIRM_ENV = "OPENDATA_THS_VERTICAL_CONTROL_CONFIRM_EMPTY"
ISOLATED_INSTANCE_CONFIRM_ENV = "OPENDATA_THS_VERTICAL_CONFIRM_ISOLATED_INSTANCE"
WAREHOUSE_DATABASE_PREFIX = "opendata_ths_vertical_wh_"
CONTROL_DATABASE_PREFIX = "opendata_ths_vertical_ctl_"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_WAREHOUSE_DATABASE = re.compile(rf"^{re.escape(WAREHOUSE_DATABASE_PREFIX)}[0-9a-f]{{16,32}}$")
_CONTROL_DATABASE = re.compile(rf"^{re.escape(CONTROL_DATABASE_PREFIX)}[0-9a-f]{{16,32}}$")
_MIGRATION_CHILD = """
import os
from alembic import command
from alembic.config import Config

config = Config()
config.set_main_option("script_location", os.environ["OD204_ALEMBIC_SCRIPT_LOCATION"])
url = os.environ["OD204_ALEMBIC_URL"]
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
command.upgrade(config, "head")
"""


def _explicit_target(role: str) -> tuple[URL, str]:
    """Validate one explicit, local, empty-schema declaration without connecting."""
    if role == "warehouse":
        url_name, database_name, confirm_name = (
            WAREHOUSE_URL_ENV,
            WAREHOUSE_DATABASE_ENV,
            WAREHOUSE_CONFIRM_ENV,
        )
        database_pattern = _WAREHOUSE_DATABASE
    else:
        url_name, database_name, confirm_name = (
            CONTROL_URL_ENV,
            CONTROL_DATABASE_ENV,
            CONTROL_CONFIRM_ENV,
        )
        database_pattern = _CONTROL_DATABASE

    raw_url = os.environ.get(url_name, "")
    declared_database = os.environ.get(database_name, "")
    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail(f"{url_name} is not a valid SQLAlchemy URL", pytrace=False)

    database = url.database or ""
    if (
        url.drivername != "mysql+pymysql"
        or (url.host or "").casefold() not in LOCAL_HOSTS
        or url.port is None
        or not url.username
        or bool(url.query)
        or database_pattern.fullmatch(database) is None
        or declared_database != database
    ):
        pytest.fail(
            f"{role} target must be an explicit local mysql+pymysql URL with a matching "
            "randomly named disposable schema and no URL options",
            pytrace=False,
        )
    expected_confirmation = f"I_OWN_AND_ALLOW_MIGRATIONS:{database}"
    if os.environ.get(confirm_name) != expected_confirmation:
        pytest.fail(
            f"set {confirm_name} to the exact disposable schema name to permit migrations",
            pytrace=False,
        )
    return url, database


def _database_settings(url: URL, *, prefix: str) -> dict[str, str]:
    """Map a validated URL onto the application's explicit MySQL settings."""
    return {
        f"{prefix}MYSQL_HOST": url.host or "",
        f"{prefix}MYSQL_PORT": str(url.port),
        f"{prefix}MYSQL_USER": url.username or "",
        f"{prefix}MYSQL_PASSWORD": url.password or "",
        f"{prefix}MYSQL_DATABASE": url.database or "",
    }


def _test_environment(
    warehouse_url: URL,
    control_url: URL,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the fixed environment for test imports and migration children."""
    values = {
        "APP_ENV": "testing",
        "TESTING": "true",
        "REDIS_URL": "",
        "SENTRY_DSN": "",
        "PYTHON_DOTENV_DISABLED": "1",
        **_database_settings(control_url, prefix=""),
        **_database_settings(warehouse_url, prefix="DATA_"),
    }
    if extra is not None:
        values.update(extra)
    return values


def _verify_empty_local_schema(engine: Engine, url: URL, database: str, *, role: str) -> None:
    """Bind the explicit schema to a local MySQL 8 instance and require emptiness."""
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text("SELECT DATABASE(), @@port, @@read_only, VERSION(), @@version_comment")
            ).one()
            tables = (
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            routines = (
                connection.execute(
                    text(
                        "SELECT routine_name FROM information_schema.routines "
                        "WHERE routine_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            triggers = (
                connection.execute(
                    text(
                        "SELECT trigger_name FROM information_schema.triggers "
                        "WHERE trigger_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
            events = (
                connection.execute(
                    text(
                        "SELECT event_name FROM information_schema.events "
                        "WHERE event_schema = DATABASE()"
                    )
                )
                .scalars()
                .all()
            )
    except Exception as exc:
        pytest.fail(
            f"could not verify the explicit {role} MySQL target ({type(exc).__name__})",
            pytrace=False,
        )

    assert identity[0] == database
    assert int(identity[1]) == url.port
    assert int(identity[2]) == 0
    version = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(identity[3]))
    assert version is not None, f"{role} target did not report a standard MySQL version"
    assert int(version.group(1)) == 8, "the acceptance requires MySQL 8"
    assert tuple(int(part) for part in version.groups()) >= (8, 0, 16)
    assert "mariadb" not in str(identity[4]).casefold()
    assert not tables and not routines and not triggers and not events, (
        f"the explicitly named {role} schema is not empty; no migration was run"
    )


def _apply_migrations(
    *,
    script_location: Path,
    url: URL,
    warehouse_url: URL,
    control_url: URL,
    cwd: Path,
) -> None:
    """Upgrade one already-verified empty schema in a credential-isolated child."""
    cwd.mkdir(mode=0o700)
    if (cwd / ".env").exists():
        pytest.fail("the isolated migration working directory contains a .env file")

    child_environment = {
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PYTHONHOME": sys.base_prefix,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(REPOSITORY_ROOT),
        **_test_environment(
            warehouse_url,
            control_url,
            extra={
                "OD204_ALEMBIC_SCRIPT_LOCATION": str(script_location),
                "OD204_ALEMBIC_URL": url.render_as_string(hide_password=False),
            },
        ),
    }
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter and constant child source
            [sys.executable, "-c", _MIGRATION_CHILD],
            cwd=cwd,
            env=child_environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("migration child timed out; preserve both disposable schemas for inspection")
    if result.returncode != 0:
        pytest.fail(
            f"{script_location.name} migration failed; child output is suppressed to avoid "
            "credential disclosure; preserve the schema for inspection",
            pytrace=False,
        )


def _create_mock_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., Iterator[FuyaoHttpClient]]:
    """Create the real THS transport with a fixed in-process MockTransport."""
    from opendata.data.providers.ths import FuyaoCredentials, FuyaoHttpClient
    from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter

    @contextmanager
    def make_client(*, timeout_seconds: float | None = None) -> Iterator[FuyaoHttpClient]:
        client = FuyaoHttpClient(
            credentials=FuyaoCredentials(
                "ths-test-fixture-only-key",
                base_url=f"https://{MOCK_HOST}",
            ),
            mock_transport=httpx.MockTransport(handler),
            rate_limiter=FuyaoRateLimiter(rate_per_second=1000.0, burst=2),
            timeout_seconds=timeout_seconds or 30.0,
            max_attempts=1,
            backoff_base_seconds=0.0,
            backoff_jitter_seconds=0.0,
            sleep=lambda _seconds: None,
        )
        try:
            yield client
        finally:
            client.close()

    return make_client


def _fixture_budget() -> RequestBudget:
    """Authorize one synthetic request to the mock host for this job only."""
    from opendata.data.request_budget import (
        GrantDecision,
        RequestBudget,
        RequestBudgetProfile,
        RequestGrant,
        RequestOperation,
    )

    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="ths",
        canonical_model="stock_daily",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:mock-transport",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts=frozenset({MOCK_HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=2,
        source_attempts=2,
        grants=(grant,),
        profile=profile,
    )


def _shanghai_midnight_millis(day: date) -> int:
    """Return the exact synthetic source timestamp for Shanghai midnight."""
    shanghai = timezone(timedelta(hours=8))
    return int(datetime.combine(day, time.min, tzinfo=shanghai).timestamp() * 1000)


def _synthetic_envelope(revision: int) -> dict[str, object]:
    """Build two fixed business-date bars with an explicit correction revision."""
    items: list[dict[str, object]] = []
    for offset, day in enumerate((START_DATE, END_DATE)):
        close = 10.2 + offset + (revision * 100.0)
        items.append(
            {
                "date_ms": _shanghai_midnight_millis(day),
                "volume": 1000.0 + offset,
                "turnover": 10200.0 + offset,
                "open_price": close - 0.2,
                "high_price": close + 0.3,
                "low_price": close - 0.5,
                "close_price": close,
            }
        )
    return {
        "code": 0,
        "message": "success",
        "request_id": "od204-offline-stock-daily",
        "data": {"timestamp": None, "item": items},
    }


def _synthetic_search_envelope() -> dict[str, object]:
    """Return one fixed ticker-search result for resolving the plain code."""
    return {
        "code": 0,
        "message": "success",
        "request_id": "od204-offline-symbol-search",
        "data": {
            "timestamp": None,
            "item": [
                {
                    "thscode": MOCK_THS_CODE,
                    "ticker": MOCK_SYMBOL,
                    "name": "合成测试标的",
                }
            ],
        },
    }


async def _read_v1_rows(app: FastAPI, *, warehouse_engine: Engine) -> list[dict[str, object]]:
    """Read the committed DWD rows through the existing v1 REST route."""
    from httpx import ASGITransport, AsyncClient

    from opendata.api.data_query import get_warehouse_engine, router
    from opendata.api.dependencies import Principal, get_current_principal
    from opendata.models.user import User, UserRole

    app.include_router(router, prefix="/api/v1/data")
    principal = Principal(
        user=User(
            id=204,
            username="od204-vertical-fixture",
            email="od204-vertical-fixture@example.test",
            hashed_password="not-a-real-password-hash",
            role=UserRole.USER,
            is_active=True,
        )
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_warehouse_engine] = lambda: warehouse_engine
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://opendata-test.local",
    ) as client:
        response = await client.get(
            "/api/v1/data/equity/stock_daily",
            params={
                "symbols": "600519",
                "start": START_DATE.isoformat(),
                "end": END_DATE.isoformat(),
                "source": "auto",
                "layer": "dwd",
                "fields": "symbol,trade_date,close,volume,amount,source",
                "page": 1,
                "page_size": 10,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    rows = body["data"]["rows"]
    assert isinstance(rows, list)
    return rows


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ths_single_stock_reaches_mysql_ods_dwd_batch_and_v1_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise real THS fetch, request budget, both stores, idempotence, and v1 read."""
    environment_names = (
        WAREHOUSE_URL_ENV,
        WAREHOUSE_DATABASE_ENV,
        WAREHOUSE_CONFIRM_ENV,
        CONTROL_URL_ENV,
        CONTROL_DATABASE_ENV,
        CONTROL_CONFIRM_ENV,
        ISOLATED_INSTANCE_CONFIRM_ENV,
    )
    if not any(os.environ.get(name) for name in environment_names):
        pytest.skip(
            "set both explicit local MySQL targets and the isolated-instance attestation to opt in"
        )
    if "opendata.core.config" in sys.modules:
        pytest.fail(
            "run this acceptance with --noconftest before application settings are imported"
        )

    warehouse_url, warehouse_database = _explicit_target("warehouse")
    control_url, control_database = _explicit_target("control")
    if warehouse_database == control_database:
        pytest.fail("warehouse and control schemas must be distinct", pytrace=False)
    if warehouse_url.host != control_url.host or warehouse_url.port != control_url.port:
        pytest.fail(
            "warehouse and control URLs must point to the same dedicated local MySQL instance",
            pytrace=False,
        )
    expected_isolation_confirmation = (
        "I_CREATED_A_TEMPORARY_LOCAL_MYSQL8_INSTANCE_AND_LIMITED_SUPPLIED_ACCOUNTS_TO_ONLY:"
        f"{warehouse_database},{control_database}"
    )
    if os.environ.get(ISOLATED_INSTANCE_CONFIRM_ENV) != expected_isolation_confirmation:
        pytest.fail(
            f"set {ISOLATED_INSTANCE_CONFIRM_ENV} only after creating a dedicated temporary "
            "local MySQL 8 instance and limiting the supplied database accounts exclusively "
            f"to schemas {warehouse_database!r} and {control_database!r}; empty schemas alone "
            "do not establish instance isolation or account privileges",
            pytrace=False,
        )

    monkeypatch.chdir(tmp_path)
    assert not (Path.cwd() / ".env").exists(), (
        "the isolated test working directory must not have .env"
    )
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    monkeypatch.delenv("FUYAO_API_BASE_URL", raising=False)
    for name, value in _test_environment(warehouse_url, control_url).items():
        monkeypatch.setenv(name, value)

    warehouse_engine = create_engine(
        warehouse_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    control_engine = create_engine(
        control_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    async_engine = None
    try:
        _verify_empty_local_schema(
            warehouse_engine,
            warehouse_url,
            warehouse_database,
            role="warehouse",
        )
        _verify_empty_local_schema(
            control_engine,
            control_url,
            control_database,
            role="control",
        )

        _apply_migrations(
            script_location=WAREHOUSE_MIGRATIONS,
            url=warehouse_url,
            warehouse_url=warehouse_url,
            control_url=control_url,
            cwd=tmp_path / "warehouse-migration-cwd",
        )
        _apply_migrations(
            script_location=CONTROL_MIGRATIONS,
            url=control_url,
            warehouse_url=warehouse_url,
            control_url=control_url,
            cwd=tmp_path / "control-migration-cwd",
        )

        warehouse_tables = set(inspect(warehouse_engine).get_table_names())
        control_tables = set(inspect(control_engine).get_table_names())
        assert {
            "alembic_version_data",
            "ods_stock_daily_ths",
            "dwd_stock_daily",
            "batch_watermark",
        } <= warehouse_tables
        assert {
            "alembic_version",
            "data_tables",
            "pipeline_progress",
            "pipeline_step_checkpoints",
            "pipeline_symbol_windows",
        } <= control_tables

        from fastapi import FastAPI
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from opendata.data.providers.ths.endpoints import (
            PRICES_ENDPOINT,
            TICKERS_SEARCH_ENDPOINT,
        )
        from opendata.data.providers.ths.models import stock_daily as stock_daily_module
        from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
        from opendata.pipeline import jobs
        from opendata.pipeline.trading_calendar import weekday_calendar

        async_url = control_url.set(drivername="mysql+aiomysql")
        async_engine = create_async_engine(
            async_url,
            poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )
        session_maker = async_sessionmaker(
            async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

        active_revision = [0]
        expected_source_start = str(_shanghai_midnight_millis(START_DATE))
        exclusive_source_end = date.fromordinal(END_DATE.toordinal() + 1)
        expected_source_end = str(_shanghai_midnight_millis(exclusive_source_end) - 1)
        expected_request_pair = [
            (
                "GET",
                TICKERS_SEARCH_ENDPOINT,
                {"q": MOCK_SYMBOL, "limit": "10"},
            ),
            (
                "GET",
                PRICES_ENDPOINT,
                {
                    "thscode": MOCK_THS_CODE,
                    "interval": "1d",
                    "start": expected_source_start,
                    "end": expected_source_end,
                    "adjust": "none",
                },
            ),
        ]
        mock_requests: list[tuple[str, str, dict[str, str]]] = []

        def source_handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert request.url.host == MOCK_HOST
            request_record = (request.method, request.url.path, dict(request.url.params))
            if request.url.path == TICKERS_SEARCH_ENDPOINT:
                assert request_record == expected_request_pair[0]
                mock_requests.append(request_record)
                return httpx.Response(200, json=_synthetic_search_envelope())
            assert request.url.path == PRICES_ENDPOINT
            assert request_record == expected_request_pair[1]
            mock_requests.append(request_record)
            return httpx.Response(200, json=_synthetic_envelope(active_revision[0]))

        monkeypatch.setattr(
            stock_daily_module,
            "client",
            _create_mock_client_factory(source_handler),
        )
        assert ThsStockDailyFetcher.canonical_model == "stock_daily"

        from opendata.pipeline.subscription import hub

        async def no_external_subscribers(_event: object, *, rows: object = None) -> int:
            return 0

        monkeypatch.setattr(hub, "wants_full", lambda _event: False)
        monkeypatch.setattr(hub, "publish_batch", no_external_subscribers)

        first_budget = _fixture_budget()
        first = await jobs.run_incremental_job(
            domain="stock_daily",
            source="ths",
            symbols=[MOCK_SYMBOL],
            limit=1,
            as_of=END_DATE,
            lookback_days=1,
            shard_size=1,
            engine=warehouse_engine,
            session_maker=session_maker,
            resume=False,
            calendar=weekday_calendar(),
            request_budget=first_budget,
        )
        assert first.failures == 0
        assert first.dwd_rows == 2
        assert first_budget.attempts_used == 2
        assert mock_requests == expected_request_pair

        with warehouse_engine.connect() as connection:
            ods_rows = connection.execute(
                text(
                    "SELECT thscode, trade_date, close_price, volume, turnover, _source "
                    "FROM `ods_stock_daily_ths` WHERE thscode = :symbol ORDER BY trade_date"
                ),
                {"symbol": MOCK_THS_CODE},
            ).all()
            dwd_rows = connection.execute(
                text(
                    "SELECT symbol, trade_date, close, volume, amount, source, _diff_flag "
                    "FROM `dwd_stock_daily` WHERE symbol = :symbol ORDER BY trade_date"
                ),
                {"symbol": "600519"},
            ).all()
            first_batches = connection.execute(
                text(
                    "SELECT domain, source, layer, rows_written, window_start, window_end "
                    "FROM `batch_watermark` WHERE domain = 'stock_daily' AND source = 'ths' "
                    "ORDER BY seq"
                )
            ).all()
        assert len(ods_rows) == len(dwd_rows) == 2
        assert [row[1] for row in ods_rows] == [START_DATE, END_DATE]
        assert [row[2] for row in ods_rows] == pytest.approx([10.2, 11.2])
        assert all(row[0] == MOCK_THS_CODE and row[5] == "ths" for row in ods_rows)
        assert [row[1] for row in dwd_rows] == [START_DATE, END_DATE]
        assert [row[2] for row in dwd_rows] == pytest.approx([10.2, 11.2])
        assert all(row[0] == "600519" for row in dwd_rows)
        assert all(row[5] == "ths" for row in dwd_rows)
        assert all(row[6] == 0 for row in dwd_rows)
        assert len(first_batches) == 1
        assert first_batches[0][0:4] == ("stock_daily", "ths", "ods", 2)
        assert first_batches[0][4:] == (START_DATE, END_DATE)

        active_revision[0] = 1
        second_budget = _fixture_budget()
        second = await jobs.run_incremental_job(
            domain="stock_daily",
            source="ths",
            symbols=[MOCK_SYMBOL],
            limit=1,
            as_of=END_DATE,
            lookback_days=1,
            shard_size=1,
            engine=warehouse_engine,
            session_maker=session_maker,
            resume=False,
            calendar=weekday_calendar(),
            request_budget=second_budget,
        )
        assert second.failures == 0
        assert second.dwd_rows == 2
        assert second_budget.attempts_used == 2
        assert mock_requests == expected_request_pair * 2

        with warehouse_engine.connect() as connection:
            ods_after_replay = connection.execute(
                text(
                    "SELECT thscode, trade_date, close_price, _source "
                    "FROM `ods_stock_daily_ths` WHERE thscode = :symbol ORDER BY trade_date"
                ),
                {"symbol": MOCK_THS_CODE},
            ).all()
            dwd_after_replay = connection.execute(
                text(
                    "SELECT symbol, trade_date, close, source, _diff_flag "
                    "FROM `dwd_stock_daily` WHERE symbol = :symbol ORDER BY trade_date"
                ),
                {"symbol": "600519"},
            ).all()
            batches_after_replay = connection.execute(
                text(
                    "SELECT batch_id, rows_written FROM `batch_watermark` "
                    "WHERE domain = 'stock_daily' AND source = 'ths' AND layer = 'ods' "
                    "ORDER BY seq"
                )
            ).all()
        assert len(ods_after_replay) == len(dwd_after_replay) == 2
        assert [row[2] for row in ods_after_replay] == pytest.approx([110.2, 111.2])
        assert [row[2] for row in dwd_after_replay] == pytest.approx([110.2, 111.2])
        assert all(row[0] == MOCK_THS_CODE and row[3] == "ths" for row in ods_after_replay)
        assert all(
            row[0] == "600519" and row[3] == "ths" and row[4] == 0 for row in dwd_after_replay
        )
        assert len(batches_after_replay) == 2
        assert all(rows_written == 2 for _batch_id, rows_written in batches_after_replay)
        assert batches_after_replay[0][0] != batches_after_replay[1][0]

        third_budget = _fixture_budget()
        third = await jobs.run_incremental_job(
            domain="stock_daily",
            source="ths",
            symbols=[MOCK_SYMBOL],
            limit=1,
            as_of=END_DATE,
            lookback_days=1,
            shard_size=1,
            engine=warehouse_engine,
            session_maker=session_maker,
            resume=False,
            calendar=weekday_calendar(),
            request_budget=third_budget,
        )
        assert third.failures == 0
        assert third.dwd_rows == 2
        assert third_budget.attempts_used == 2
        assert mock_requests == expected_request_pair * 3

        with warehouse_engine.connect() as connection:
            ods_after_same_revision = connection.execute(
                text(
                    "SELECT thscode, trade_date, close_price, _source "
                    "FROM `ods_stock_daily_ths` WHERE thscode = :symbol ORDER BY trade_date"
                ),
                {"symbol": MOCK_THS_CODE},
            ).all()
            dwd_after_same_revision = connection.execute(
                text(
                    "SELECT symbol, trade_date, close, source, _diff_flag "
                    "FROM `dwd_stock_daily` WHERE symbol = :symbol ORDER BY trade_date"
                ),
                {"symbol": MOCK_SYMBOL},
            ).all()
            batches_after_same_revision = connection.execute(
                text(
                    "SELECT batch_id, rows_written FROM `batch_watermark` "
                    "WHERE domain = 'stock_daily' AND source = 'ths' AND layer = 'ods' "
                    "ORDER BY seq"
                )
            ).all()
        assert len(ods_after_same_revision) == len(dwd_after_same_revision) == 2
        assert [row[2] for row in ods_after_same_revision] == pytest.approx([110.2, 111.2])
        assert [row[2] for row in dwd_after_same_revision] == pytest.approx([110.2, 111.2])
        assert all(row[0] == MOCK_THS_CODE and row[3] == "ths" for row in ods_after_same_revision)
        assert all(
            row[0] == MOCK_SYMBOL and row[3] == "ths" and row[4] == 0
            for row in dwd_after_same_revision
        )
        assert len(batches_after_same_revision) == 3
        assert all(rows_written == 2 for _batch_id, rows_written in batches_after_same_revision)
        assert len({batch_id for batch_id, _rows_written in batches_after_same_revision}) == 3

        with control_engine.connect() as connection:
            metadata_rows = connection.execute(
                text(
                    "SELECT table_name, row_count, data_start_date, data_end_date, "
                    "last_update_status FROM `data_tables` WHERE table_name IN "
                    "('ods_stock_daily_ths', 'dwd_stock_daily') ORDER BY table_name"
                )
            ).all()
            progress_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM `pipeline_progress` WHERE domain = 'stock_daily' "
                    "AND source = 'ths' AND status = 'DONE'"
                )
            )
        assert len(metadata_rows) == 2
        assert all(row[1] == 2 for row in metadata_rows)
        assert all(row[2] == START_DATE and row[3] == END_DATE for row in metadata_rows)
        assert all(row[4] == "success" for row in metadata_rows)
        assert progress_count == 1

        app = FastAPI()
        api_rows = await _read_v1_rows(app, warehouse_engine=warehouse_engine)
        assert len(api_rows) == 2
        assert [row["trade_date"] for row in api_rows] == [
            START_DATE.isoformat(),
            END_DATE.isoformat(),
        ]
        assert [row["close"] for row in api_rows] == pytest.approx([110.2, 111.2])
        assert all(row["symbol"] == "600519" and row["source"] == "ths" for row in api_rows)
        assert all(row["volume"] > 0 and row["amount"] > 0 for row in api_rows)
    finally:
        if async_engine is not None:
            await async_engine.dispose()
        warehouse_engine.dispose()
        control_engine.dispose()
