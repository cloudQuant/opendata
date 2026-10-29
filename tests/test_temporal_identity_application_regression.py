"""Lock application configuration and startup behavior at their public seams.

These tests use explicit settings, a disposable log path, and mocked application
services. They never read a dotenv file, open a database connection, or start a
network listener.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from starlette.requests import Request


def _settings_without_dotenv(**values: object):
    """Build Settings using only explicit values and field defaults."""
    from opendata.core.config import Settings

    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fuyao_api_key_expires_at", date(2027, 2, 3)),
        ("fred_api_key_expires_at", datetime(2027, 2, 3, 23, tzinfo=timezone.utc)),
        ("fuyao_api_key_expires_at", "2027-02-03"),
        ("fred_api_key_expires_at", "2027-02-03T23:30:00Z"),
    ],
)
def test_issuer_expiry_settings_preserve_the_issuer_calendar_day(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    """Date and timestamp metadata normalize to a date without timezone drift."""
    monkeypatch.setenv("APP_ENV", "testing")

    settings = _settings_without_dotenv(**{field: value})

    assert getattr(settings, field) == date(2027, 2, 3)


@pytest.mark.parametrize("value", [None, ""])
def test_empty_issuer_expiry_disables_expiry_metadata(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    settings = _settings_without_dotenv(fuyao_api_key_expires_at=value)
    assert settings.fuyao_api_key_expires_at is None


@pytest.mark.parametrize("value", ["not-a-date", 20270203])
def test_invalid_issuer_expiry_is_rejected(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    with pytest.raises(ValidationError, match="ISO 8601"):
        _settings_without_dotenv(fred_api_key_expires_at=value)


def test_default_signing_key_is_rejected_only_when_process_environment_is_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.utils.constants import DEFAULT_SECRET_KEY

    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValidationError, match="Default secret key detected in production"):
        _settings_without_dotenv(secret_key=DEFAULT_SECRET_KEY)

    monkeypatch.setenv("APP_ENV", "testing")
    settings = _settings_without_dotenv(secret_key=DEFAULT_SECRET_KEY)
    assert settings.secret_key == DEFAULT_SECRET_KEY


@pytest.mark.parametrize("value", [Path("relative/policy.json"), "relative/policy.json", 7])
def test_provider_policy_path_rejects_non_absolute_inputs(
    monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    with pytest.raises(ValidationError, match="absolute path"):
        _settings_without_dotenv(provider_source_policy_file=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (Path("/tmp/reviewed-policy.json"), Path("/tmp/reviewed-policy.json")),
        ("/tmp/reviewed-policy.json", Path("/tmp/reviewed-policy.json")),
    ],
)
def test_provider_policy_path_accepts_only_explicit_absolute_paths(
    monkeypatch: pytest.MonkeyPatch,
    value: object,
    expected: Path | None,
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    settings = _settings_without_dotenv(provider_source_policy_file=value)
    assert settings.provider_source_policy_file == expected


@pytest.mark.parametrize(
    "field",
    ["cache_ttl_seconds", "retention_diff_report_days", "retention_minute_years"],
)
def test_retention_limits_reject_zero_before_they_can_purge_data(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    with pytest.raises(ValidationError, match="must be positive"):
        _settings_without_dotenv(**{field: 0})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            '["https://data.example", "https://ops.example"]',
            [
                "https://data.example",
                "https://ops.example",
            ],
        ),
        (
            "https://data.example, https://ops.example",
            [
                "https://data.example",
                "https://ops.example",
            ],
        ),
        (["https://data.example"], ["https://data.example"]),
    ],
)
def test_cors_origins_accept_json_comma_separated_and_list_configuration(
    monkeypatch: pytest.MonkeyPatch,
    value: str | list[str],
    expected: list[str],
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    assert _settings_without_dotenv(cors_origins=value).cors_origins == expected


def test_database_urls_quote_passwords_and_keep_sync_and_async_drivers_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    settings = _settings_without_dotenv(
        mysql_host="app-db.internal",
        mysql_port=3307,
        mysql_user="app-user",
        mysql_password="p a+ss/?",
        mysql_database="app_store",
        data_mysql_host="warehouse.internal",
        data_mysql_port=3308,
        data_mysql_user="reader",
        data_mysql_password="r e+a/d?",
        data_mysql_database="market_data",
    )

    assert (
        settings.database_url
        == "mysql+aiomysql://app-user:p+a%2Bss%2F%3F@app-db.internal:3307/app_store"
    )
    assert (
        settings.database_url_sync
        == "mysql+pymysql://app-user:p+a%2Bss%2F%3F@app-db.internal:3307/app_store"
    )
    assert (
        settings.data_database_url
        == "mysql+pymysql://reader:r+e%2Ba%2Fd%3F@warehouse.internal:3308/market_data"
    )
    assert (
        settings.data_database_url_async
        == "mysql+aiomysql://reader:r+e%2Ba%2Fd%3F@warehouse.internal:3308/market_data"
    )


def _request(path: str) -> Request:
    """Create a local ASGI request object without a socket or TestClient lifespan."""
    raw_path = path.encode("ascii")
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": raw_path,
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("TRUE", True), ("false", False)])
def test_testing_mode_reads_the_process_flag_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    from opendata import main

    monkeypatch.setenv("TESTING", value)
    assert main._testing_mode() is expected


def _load_main_variant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    testing: bool,
    frontend_present: bool,
    sentry_module: object,
) -> tuple[ModuleType, Mock, Mock, object, SimpleNamespace]:
    """Load main.py with explicit settings and mocked import-time side effects."""
    import loguru
    from fastapi import staticfiles

    from opendata import main as imported_main
    from opendata.api import rate_limit
    from opendata.core import config

    settings = SimpleNamespace(
        app_name="offline-regression",
        app_version="test-version",
        app_env="testing" if testing else "production",
        app_debug=True,
        secret_key="isolated-test-secret",
        is_production=not testing,
        cors_origins=["https://client.example"],
        cors_allow_credentials=False,
        log_json=True,
        log_level="INFO",
        log_file=str(tmp_path / "logs" / "app.log"),
        sentry_dsn="https://public@example.invalid/1",
        sentry_traces_sample_rate=0.25,
        temporal_identity_enabled=False,
    )
    sentinel_limiter = object()
    logger_add = Mock(return_value=1)
    logger_remove = Mock()
    logger_probe = SimpleNamespace(
        add=logger_add,
        remove=logger_remove,
        info=Mock(),
        warning=Mock(),
        debug=Mock(),
        error=Mock(),
    )
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(rate_limit, "get_limiter", lambda: sentinel_limiter)
    monkeypatch.setattr(loguru, "logger", logger_probe)
    monkeypatch.setenv("TESTING", "true" if testing else "false")
    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_module)

    frontend_path = Path(imported_main.__file__).resolve().parent.parent / "frontend" / "dist"
    original_is_dir = Path.is_dir

    def frontend_dir_only(path: Path) -> bool:
        if path == frontend_path:
            return frontend_present
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", frontend_dir_only)
    if frontend_present:

        class StaticFilesProbe:
            """Satisfy app.mount without opening a static directory."""

            def __init__(self, directory: str | Path, **_kwargs: object) -> None:
                self.directory = Path(directory)

            async def __call__(self, _scope: object, _receive: object, _send: object) -> None:
                return None

        monkeypatch.setattr(staticfiles, "StaticFiles", StaticFilesProbe)

    module_name = f"_od204_main_probe_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, imported_main.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, logger_add, logger_remove, sentinel_limiter, logger_probe


@pytest.mark.asyncio
async def test_non_test_import_configures_file_logging_sentry_and_operational_middleware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from slowapi.errors import RateLimitExceeded

    sentry_init = Mock()
    sentry = SimpleNamespace(init=sentry_init)
    module, logger_add, logger_remove, limiter, _logger = _load_main_variant(
        monkeypatch,
        tmp_path,
        testing=False,
        frontend_present=False,
        sentry_module=sentry,
    )

    assert logger_remove.call_count == 1
    assert logger_add.call_count == 2
    stderr_call, file_call = logger_add.call_args_list
    assert stderr_call.args[0] is sys.stderr
    assert stderr_call.kwargs == {"level": "INFO", "serialize": True}
    assert file_call.args[0] == str(tmp_path / "logs" / "app.log")
    assert file_call.kwargs["rotation"] == "10 MB"
    assert file_call.kwargs["retention"] == "30 days"
    assert (tmp_path / "logs").is_dir()
    assert sentry_init.call_args.kwargs == {
        "dsn": "https://public@example.invalid/1",
        "traces_sample_rate": 0.25,
        "environment": "production",
        "release": "test-version",
    }
    assert module.app.state.limiter is limiter
    assert any(
        item.cls.__name__ == "RequestLoggingMiddleware" for item in module.app.user_middleware
    )
    assert RateLimitExceeded in module.app.exception_handlers

    request = _request("/api/v1/example")
    rate_limited = await module.app.exception_handlers[RateLimitExceeded](request, object())
    invalid_value = await module.app.exception_handlers[ValueError](
        request, ValueError("bad input")
    )
    unexpected = await module.app.exception_handlers[Exception](
        request, RuntimeError("private detail")
    )
    assert rate_limited.status_code == 429
    assert json.loads(rate_limited.body) == {
        "detail": "Rate limit exceeded. Please try again later."
    }
    assert invalid_value.status_code == 400
    assert json.loads(invalid_value.body) == {
        "success": False,
        "message": "bad input",
        "error_code": "VALUE_ERROR",
    }
    assert unexpected.status_code == 500
    assert json.loads(unexpected.body) == {
        "success": False,
        "message": "Internal server error",
        "error_code": "INTERNAL_ERROR",
    }


def test_testing_import_uses_stderr_only_and_sentry_import_failure_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, logger_add, _logger_remove, _limiter, _logger_probe = _load_main_variant(
        monkeypatch,
        tmp_path,
        testing=True,
        frontend_present=False,
        sentry_module=None,
    )

    assert logger_add.call_count == 1
    assert logger_add.call_args.args[0] is sys.stderr
    assert "format" in logger_add.call_args.kwargs
    assert "serialize" not in logger_add.call_args.kwargs
    assert not (tmp_path / "logs").exists()
    assert not any(
        item.cls.__name__ == "RequestLoggingMiddleware" for item in module.app.user_middleware
    )


def test_missing_sentry_package_warns_without_aborting_non_test_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module, _add, _remove, _limiter, logger_probe = _load_main_variant(
        monkeypatch,
        tmp_path,
        testing=False,
        frontend_present=False,
        sentry_module=None,
    )

    logger_probe.warning.assert_called_once()
    assert "sentry-sdk not installed" in logger_probe.warning.call_args.args[0]
    assert module.app is not None


@pytest.mark.asyncio
async def test_frontend_routes_serve_spa_and_keep_api_404s_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.responses import FileResponse, JSONResponse

    module, _add, _remove, _limiter, _logger = _load_main_variant(
        monkeypatch,
        tmp_path,
        testing=True,
        frontend_present=True,
        sentry_module=None,
    )
    frontend = module.FRONTEND_DIR
    assert any(getattr(route, "path", None) == "/assets" for route in module.app.routes)
    root = next(route for route in module.app.routes if getattr(route, "path", None) == "/")
    root_response = await root.endpoint()
    assert isinstance(root_response, FileResponse)
    assert root_response.path == Path(frontend / "index.html")

    not_found = module.app.exception_handlers[404]
    api_response = await not_found(_request("/api/not-found"), Exception())
    health_response = await not_found(_request("/health/not-found"), Exception())
    assert isinstance(api_response, JSONResponse) and api_response.status_code == 404
    assert isinstance(health_response, JSONResponse) and health_response.status_code == 404

    spa_response = await not_found(_request("/portfolio/overview"), Exception())
    assert isinstance(spa_response, FileResponse)
    assert spa_response.path == Path(frontend / "index.html")

    original_is_file = Path.is_file

    def one_fixture_file(path: Path) -> bool:
        if path == frontend / "favicon.svg":
            return True
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", one_fixture_file)
    static_response = await not_found(_request("/favicon.svg"), Exception())
    assert isinstance(static_response, FileResponse)
    assert static_response.path == Path(frontend / "favicon.svg")


@pytest.mark.asyncio
async def test_without_frontend_root_describes_the_service_and_deprecation_is_path_scoped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from starlette.responses import Response

    module, _add, _remove, _limiter, _logger = _load_main_variant(
        monkeypatch,
        tmp_path,
        testing=True,
        frontend_present=False,
        sentry_module=None,
    )
    root = next(route for route in module.app.routes if getattr(route, "path", None) == "/")
    assert await root.endpoint() == {
        "app_name": "offline-regression",
        "version": "test-version",
        "description": "Web-based financial data management platform for akshare",
        "docs": "/docs",
    }

    async def next_response(_request: Request) -> Response:
        return Response("ok", status_code=200)

    legacy = await module.deprecation_header_middleware(_request("/api/data"), next_response)
    current = await module.deprecation_header_middleware(_request("/api/v1/data"), next_response)
    unrelated = await module.deprecation_header_middleware(_request("/health"), next_response)
    assert legacy.headers["Deprecation"] == "true"
    assert legacy.headers["Sunset"] == "2026-06-01"
    assert legacy.headers["Link"] == '</api/v1>; rel="successor-version"'
    assert "Deprecation" not in current.headers
    assert "Deprecation" not in unrelated.headers


@pytest.mark.asyncio
async def test_non_production_lifespan_keeps_bootstrap_and_scheduler_calls_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from opendata import main
    from opendata.core import database
    from opendata.data import providers
    from opendata.pipeline import scheduling
    from opendata.services.data_acquisition import DataAcquisitionService

    settings = SimpleNamespace(
        app_name="offline-lifespan",
        app_version="1",
        app_env="development",
        secret_key="non-default-test-secret",
        is_production=False,
        enable_scheduler=False,
        redis_url=None,
        workers=1,
        temporal_identity_enabled=False,
    )
    create_tables = AsyncMock()
    init_db = AsyncMock()
    close_db = AsyncMock()
    start = AsyncMock()
    shutdown = AsyncMock()
    registration = [object()]
    executor_shutdown = Mock()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_testing_mode", lambda: False)
    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main, "close_db", close_db)
    monkeypatch.setattr(main.task_scheduler, "start", start)
    monkeypatch.setattr(main.task_scheduler, "shutdown", shutdown)
    monkeypatch.setattr(database, "create_tables", create_tables)
    monkeypatch.setattr(providers, "register_providers", lambda: registration)
    monkeypatch.setattr(
        scheduling,
        "scheduler_decision",
        lambda **_kwargs: SimpleNamespace(enabled=False, reason="explicit test switch"),
    )
    monkeypatch.setattr(
        DataAcquisitionService,
        "_akshare_executor",
        SimpleNamespace(shutdown=executor_shutdown),
    )

    async with main.lifespan(FastAPI()):
        create_tables.assert_awaited_once()
        init_db.assert_awaited_once()
        start.assert_not_awaited()
        shutdown.assert_not_awaited()

    start.assert_not_awaited()
    shutdown.assert_awaited_once()
    close_db.assert_awaited_once()
    executor_shutdown.assert_called_once_with(wait=False)


@pytest.mark.asyncio
async def test_enabled_scheduler_starts_before_builtin_jobs_are_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from opendata import main
    from opendata.core import database
    from opendata.data import providers
    from opendata.pipeline import jobs, scheduling
    from opendata.services.data_acquisition import DataAcquisitionService

    settings = SimpleNamespace(
        app_name="scheduler-lifespan-test",
        app_version="1",
        app_env="development",
        secret_key="non-default-test-secret",
        is_production=False,
        enable_scheduler=True,
        redis_url="redis://isolated.invalid/0",
        workers=1,
        temporal_identity_enabled=False,
    )
    events: list[str] = []
    create_tables = AsyncMock()
    init_db = AsyncMock()
    close_db = AsyncMock()

    async def start() -> None:
        events.append("scheduler_started")

    async def attach_jobs() -> int:
        events.append("builtin_jobs_attached")
        return 4

    async def shutdown() -> None:
        events.append("scheduler_stopped")

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_testing_mode", lambda: False)
    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main, "close_db", close_db)
    monkeypatch.setattr(main.task_scheduler, "start", start)
    monkeypatch.setattr(main.task_scheduler, "shutdown", shutdown)
    monkeypatch.setattr(database, "create_tables", create_tables)
    monkeypatch.setattr(providers, "register_providers", lambda: [])
    monkeypatch.setattr(jobs, "attach_builtin_jobs", attach_jobs)
    monkeypatch.setattr(
        scheduling,
        "scheduler_decision",
        lambda **_kwargs: SimpleNamespace(enabled=True, reason="explicit test switch"),
    )
    monkeypatch.setattr(
        DataAcquisitionService,
        "_akshare_executor",
        SimpleNamespace(shutdown=Mock()),
    )

    async with main.lifespan(FastAPI()):
        assert events == ["scheduler_started", "builtin_jobs_attached"]
        create_tables.assert_awaited_once()
        init_db.assert_awaited_once()

    assert events == ["scheduler_started", "builtin_jobs_attached", "scheduler_stopped"]
    close_db.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_revision", "head_revision", "engine_failure"),
    [
        ("0020_previous", "0021_current", False),
        ("0021_current", "0021_current", False),
        (None, None, True),
    ],
)
async def test_production_lifespan_checks_migrations_without_bootstrapping_tables(
    monkeypatch: pytest.MonkeyPatch,
    current_revision: str | None,
    head_revision: str | None,
    engine_failure: bool,
) -> None:
    from alembic.runtime import migration
    from fastapi import FastAPI

    from alembic import config as alembic_config
    from alembic import script as alembic_script
    from opendata import main
    from opendata.core import database
    from opendata.data import providers
    from opendata.pipeline import scheduling
    from opendata.services.data_acquisition import DataAcquisitionService

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    sync_engine = SimpleNamespace(connect=lambda: Connection(), dispose=Mock())
    create_engine = (
        Mock(side_effect=RuntimeError("offline migration probe"))
        if engine_failure
        else Mock(return_value=sync_engine)
    )

    class ConfigStub:
        def __init__(self, _path: str) -> None:
            self.path = _path

    class ScriptDirectoryStub:
        @classmethod
        def from_config(cls, _config: object) -> ScriptDirectoryStub:
            return cls()

        def get_current_head(self) -> str | None:
            return head_revision

    def configure(_connection: object) -> SimpleNamespace:
        return SimpleNamespace(get_current_revision=lambda: current_revision)

    monkeypatch.setattr(alembic_config, "Config", ConfigStub)
    monkeypatch.setattr(alembic_script, "ScriptDirectory", ScriptDirectoryStub)
    monkeypatch.setattr(migration.MigrationContext, "configure", staticmethod(configure))
    monkeypatch.setattr("sqlalchemy.create_engine", create_engine)

    settings = SimpleNamespace(
        app_name="production-lifespan-test",
        app_version="1",
        app_env="production",
        secret_key="non-default-test-secret",
        is_production=True,
        database_url_sync="mysql+pymysql://test.invalid/offline",
        enable_scheduler=False,
        redis_url=None,
        workers=1,
        temporal_identity_enabled=False,
    )
    create_tables = AsyncMock()
    init_db = AsyncMock()
    close_db = AsyncMock()
    shutdown = AsyncMock()
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "_testing_mode", lambda: False)
    monkeypatch.setattr(main, "init_db", init_db)
    monkeypatch.setattr(main, "close_db", close_db)
    monkeypatch.setattr(main.task_scheduler, "shutdown", shutdown)
    monkeypatch.setattr(database, "create_tables", create_tables)
    monkeypatch.setattr(providers, "register_providers", lambda: [])
    monkeypatch.setattr(
        scheduling,
        "scheduler_decision",
        lambda **_kwargs: SimpleNamespace(enabled=False, reason="explicit test switch"),
    )
    monkeypatch.setattr(
        DataAcquisitionService,
        "_akshare_executor",
        SimpleNamespace(shutdown=Mock()),
    )

    async with main.lifespan(FastAPI()):
        create_tables.assert_not_awaited()
        init_db.assert_awaited_once()
        if not engine_failure:
            create_engine.assert_called_once_with(settings.database_url_sync)
    if not engine_failure:
        sync_engine.dispose.assert_called_once_with()
    shutdown.assert_awaited_once()
    close_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_incomplete_temporal_identity_runtime_fails_closed_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from opendata import main

    class IncompleteRuntime:
        enabled = True
        pin_registry = None
        snapshot_store = None

        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runtime = IncompleteRuntime()
    monkeypatch.setattr(main, "_create_temporal_identity_runtime_from_settings", lambda: runtime)
    app = FastAPI()
    app.state.temporal_identity_pin_registry = object()
    app.state.temporal_identity_snapshot_store = object()

    with pytest.raises(RuntimeError, match="temporal_identity_runtime_incomplete"):
        async with main.lifespan(app):
            pytest.fail("an incomplete runtime must never enter application startup")

    assert runtime.closed is True
    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_healthy", "scheduler_running", "expected"),
    [(True, True, "healthy"), (True, False, "degraded"), (False, False, "unhealthy")],
)
async def test_health_check_reports_database_scheduler_and_pool_state_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
    database_healthy: bool,
    scheduler_running: bool,
    expected: str,
) -> None:
    from opendata import main
    from opendata.core import database

    monkeypatch.setattr(
        database,
        "check_db_connection",
        AsyncMock(return_value=database_healthy),
    )
    monkeypatch.setattr(database, "engine", SimpleNamespace(pool=object()))
    monkeypatch.setattr(
        database,
        "data_engine",
        SimpleNamespace(
            pool=SimpleNamespace(
                size=lambda: 3,
                checkedin=lambda: 2,
                checkedout=lambda: 1,
                overflow=lambda: 0,
                status=lambda: "size=3 checked_in=2 checked_out=1",
            )
        ),
    )
    monkeypatch.setattr(main, "task_scheduler", SimpleNamespace(is_running=scheduler_running))
    monkeypatch.setattr(
        main,
        "settings",
        SimpleNamespace(app_name="health-test", app_version="v", app_env="testing"),
    )

    result = await main.health_check()

    assert result["status"] == expected
    assert result["checks"] == {
        "database": "connected" if database_healthy else "disconnected",
        "scheduler": "running" if scheduler_running else "stopped",
    }
    assert result["pool"]["main"] == {
        "size": 0,
        "checked_in": 0,
        "checked_out": 0,
        "overflow": 0,
        "invalid": "",
    }
    assert result["pool"]["data"]["checked_out"] == 1
    assert result["app_name"] == "health-test"


def test_settings_properties_classify_environments_and_hide_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    opaque_marker = f"test-marker:{uuid4()}"
    settings = _settings_without_dotenv(
        app_env="development",
        temporal_identity_warehouse_url=SecretStr(opaque_marker),
    )
    assert settings.is_development is True
    assert settings.is_production is False
    assert opaque_marker not in repr(settings)
    production = _settings_without_dotenv(app_env="production", secret_key="a unique secret")
    assert production.is_production is True
    assert production.is_development is False
