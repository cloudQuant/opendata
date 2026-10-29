"""Exercise temporal identity runtime ownership through the real app lifespan."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing, suppress
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import event

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch
    from sqlalchemy.engine import URL, Engine

    from opendata.api.temporal_identity_runtime import (
        TemporalIdentityRuntime,
        TemporalIdentityRuntimeConfig,
    )
    from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry

_PIN = {
    "dataset_id": "instrument.identity",
    "source_id": "akshare",
    "authority_id": "reviewed-market-authority",
    "registry_id": "cn-a-share",
    "reference_version": "identity-reference-v1",
    "snapshot_sha256": "a" * 64,
}
_PIN_DOCUMENT = {"schema_version": 1, "pins": [_PIN]}
_HEADER_SQL = """
CREATE TABLE od_temporal_identity_snapshots (
    authority_id TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    reference_version TEXT NOT NULL,
    total_bytes INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    snapshot_sha256 BLOB NOT NULL,
    created_at_us INTEGER NOT NULL,
    PRIMARY KEY (authority_id, registry_id, reference_version)
);
"""
_CHUNKS_SQL = """
CREATE TABLE od_temporal_identity_snapshot_chunks (
    authority_id TEXT NOT NULL,
    registry_id TEXT NOT NULL,
    reference_version TEXT NOT NULL,
    chunk_no INTEGER NOT NULL,
    chunk_bytes BLOB NOT NULL,
    chunk_sha256 BLOB NOT NULL,
    PRIMARY KEY (authority_id, registry_id, reference_version, chunk_no)
);
"""


def _settings(**overrides: object) -> SimpleNamespace:
    """Make explicit isolated settings for the lifespan without loading .env."""
    values: dict[str, object] = {
        "app_name": "temporal-identity-test",
        "app_version": "test",
        "app_env": "testing",
        "secret_key": "isolated-test-secret",
        "is_production": False,
        "enable_scheduler": False,
        "redis_url": None,
        "workers": 1,
        "temporal_identity_enabled": False,
        "temporal_identity_pin_file": None,
        "temporal_identity_pin_sha256": None,
        "temporal_identity_warehouse_sqlite_path": None,
        "temporal_identity_warehouse_url": None,
        "temporal_identity_mysql_select_only_account_acknowledged": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _isolate_lifespan(monkeypatch: MonkeyPatch, *, testing: bool = True) -> object:
    """Replace pre-existing application services with no-I/O test seams."""
    from opendata import main
    from opendata.data import providers
    from opendata.pipeline import scheduling
    from opendata.services.data_acquisition import DataAcquisitionService

    monkeypatch.setattr(main, "settings", _settings())
    monkeypatch.setattr(main, "_testing_mode", lambda: testing)
    monkeypatch.setattr(providers, "register_providers", lambda: [])
    monkeypatch.setattr(
        scheduling,
        "scheduler_decision",
        lambda **_kwargs: SimpleNamespace(enabled=False, reason="isolated test"),
    )
    monkeypatch.setattr(main, "close_db", _async_noop)
    monkeypatch.setattr(main.task_scheduler, "start", _async_noop)
    monkeypatch.setattr(main.task_scheduler, "shutdown", _async_noop)
    monkeypatch.setattr(
        DataAcquisitionService,
        "_akshare_executor",
        SimpleNamespace(shutdown=lambda **_kwargs: None),
    )
    return main


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    """Stand in for async services that the isolated lifespan does not exercise."""


def _write_pin_file(path: Path) -> str:
    """Write one synthetic private pin document and return its exact digest."""
    raw = json.dumps(_PIN_DOCUMENT, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _create_existing_warehouse(path: Path) -> Path:
    """Create only the already-migrated schema in a disposable SQLite file."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(_HEADER_SQL + _CHUNKS_SQL)
    return path


def _load_snapshot_migration() -> ModuleType:
    """Load revision 0021 without importing or executing the Alembic environment."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic_data"
        / "versions"
        / "20261029-0021_temporal_identity_snapshot.py"
    )
    spec = importlib.util.spec_from_file_location(
        "temporal_identity_snapshot_0021_e2e", migration_path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _migrate_and_seed_snapshot(path: Path, registry: TemporalIdentityRegistry) -> str:
    """Run migration 0021 and seed one canonical snapshot in a disposable SQLite file."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text

    from opendata.data.temporal_identity_store import TemporalIdentitySnapshotStore
    from opendata.data.temporal_instrument_identity_snapshot import (
        encode_temporal_identity_snapshot,
    )

    engine = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection: sqlite3.Connection, _record: object) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
            )
            connection.execute(
                text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
                {"marker": "0020_run_ledger_claim_policy"},
            )
            context = MigrationContext.configure(
                connection,
                opts={"version_table": "alembic_version_data"},
            )
            with Operations.context(context):
                _load_snapshot_migration().upgrade()
            connection.execute(
                text("UPDATE alembic_version_data SET version_num = :marker"),
                {"marker": "0021_temporal_identity_snapshot"},
            )

        raw = encode_temporal_identity_snapshot(registry)
        digest = hashlib.sha256(raw).hexdigest()
        store = TemporalIdentitySnapshotStore(engine)
        store.put(
            authority_id="reviewed-market-authority",
            registry_id="cn-a-share",
            registry=registry,
            expected_sha256=digest,
        )
        return digest
    finally:
        engine.dispose()


def _write_pinned_runtime_file(path: Path, snapshot_sha256: str) -> None:
    """Write a private synthetic pin for the canonical test snapshot digest."""
    document = {
        "schema_version": 1,
        "pins": [
            {
                "dataset_id": "instrument.identity",
                "source_id": "akshare",
                "authority_id": "reviewed-market-authority",
                "registry_id": "cn-a-share",
                "reference_version": "identity-reference-v1",
                "snapshot_sha256": snapshot_sha256,
            }
        ],
    }
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)


def _sqlite_schema(path: Path) -> tuple[tuple[str, str, str | None], ...]:
    """Read only the catalog names to prove app startup did not change the schema."""
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_schema "
            "WHERE type IN ('table', 'index', 'trigger', 'view') ORDER BY type, name"
        ).fetchall()
    return tuple((str(kind), str(name), sql) for kind, name, sql in rows)


def test_temporal_identity_settings_are_disabled_by_default_and_hide_the_mysql_url() -> None:
    """Settings require opt-in and never include the configured MySQL URL in repr."""
    from opendata.core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_identity_enabled is False
    assert settings.temporal_identity_pin_file is None
    assert settings.temporal_identity_pin_sha256 is None
    assert settings.temporal_identity_warehouse_sqlite_path is None
    assert settings.temporal_identity_warehouse_url is None
    assert settings.temporal_identity_mysql_select_only_account_acknowledged is False

    marker = "temporal-identity-secret-url-marker"
    configured = Settings(
        _env_file=None,  # type: ignore[call-arg]
        temporal_identity_warehouse_url=SecretStr(
            f"mysql+pymysql://reader:{marker}@warehouse.invalid/data"
        ),
    )
    assert marker not in repr(configured)
    assert "temporal_identity_warehouse_url" not in configured.model_dump()


def _enabled_settings(warehouse: Path, pin_file: Path, digest: str) -> SimpleNamespace:
    """Build settings for the read-only SQLite runtime fixture."""
    return _settings(
        temporal_identity_enabled=True,
        temporal_identity_pin_file=str(pin_file),
        temporal_identity_pin_sha256=digest,
        temporal_identity_warehouse_sqlite_path=str(warehouse),
    )


@pytest.mark.asyncio
async def test_default_disabled_lifespan_does_not_read_pin_or_build_warehouse_engine(
    monkeypatch: MonkeyPatch,
) -> None:
    """The real app lifespan keeps both runtime file and engine seams untouched."""
    from opendata import main
    from opendata.api import temporal_identity_runtime as runtime_module

    _isolate_lifespan(monkeypatch)

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("disabled lifespan attempted temporal identity I/O")

    monkeypatch.setattr(runtime_module, "_read_operator_pin_file", unexpected)
    monkeypatch.setattr(runtime_module, "_build_engine", unexpected)
    app = FastAPI(lifespan=main.lifespan)

    async with app.router.lifespan_context(app):
        assert app.state.temporal_identity_pin_registry is None
        assert app.state.temporal_identity_snapshot_store is None

    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
async def test_enabled_lifespan_rejects_incomplete_configuration(
    monkeypatch: MonkeyPatch,
) -> None:
    """The opt-in switch cannot silently enable a partially configured reader."""
    from opendata import main
    from opendata.api.temporal_identity_runtime import TemporalIdentityRuntimeError

    _isolate_lifespan(monkeypatch)
    monkeypatch.setattr(main, "settings", _settings(temporal_identity_enabled=True))
    app = FastAPI(lifespan=main.lifespan)

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_digest_invalid"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan entered with incomplete temporal identity settings")

    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
async def test_enabled_lifespan_injects_runtime_and_revokes_it_on_shutdown(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A valid runtime is available during requests and closed after app shutdown."""
    from opendata import main
    from opendata.api import temporal_identity_runtime as runtime_module
    from opendata.api.temporal_identity_runtime import TemporalIdentityRuntimeError

    _isolate_lifespan(monkeypatch)
    warehouse = _create_existing_warehouse(tmp_path / "warehouse.sqlite")
    pin_file = tmp_path / "temporal-identity-pins.json"
    digest = _write_pin_file(pin_file)
    monkeypatch.setattr(main, "settings", _enabled_settings(warehouse, pin_file, digest))

    disposed: list[bool] = []
    original_create_engine = runtime_module._create_configured_engine

    def create_monitored_engine(
        url: URL, *, connect_args: dict[str, object] | None = None
    ) -> Engine:
        engine = original_create_engine(url, connect_args=connect_args)
        event.listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        return engine

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    app = FastAPI(lifespan=main.lifespan)

    async with app.router.lifespan_context(app):
        pin_registry = app.state.temporal_identity_pin_registry
        snapshot_store = app.state.temporal_identity_snapshot_store
        assert pin_registry is not None
        assert pin_registry.get("instrument.identity") is not None
        assert snapshot_store is not None
        assert callable(snapshot_store.get)
        assert not hasattr(snapshot_store, "put")

    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None
    assert disposed == [True]
    with pytest.raises(TemporalIdentityRuntimeError, match="runtime_closed"):
        snapshot_store.get(
            authority_id="reviewed-market-authority",
            registry_id="cn-a-share",
            reference_version="identity-reference-v1",
            expected_sha256="a" * 64,
        )


@pytest.mark.asyncio
async def test_enabled_lifespan_pin_failure_is_closed_and_does_not_echo_configuration(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A missing pin file fails startup with a fixed error and no configured DSN."""
    from opendata import main
    from opendata.api.temporal_identity_runtime import TemporalIdentityRuntimeError

    _isolate_lifespan(monkeypatch)
    marker = "sensitive-temporal-warehouse-dsn"
    monkeypatch.setattr(
        main,
        "settings",
        _settings(
            temporal_identity_enabled=True,
            temporal_identity_pin_file=str(tmp_path / "missing-pins.json"),
            temporal_identity_pin_sha256="a" * 64,
            temporal_identity_warehouse_url=SecretStr(
                f"mysql+pymysql://operator:{marker}@warehouse.invalid/data"
            ),
            temporal_identity_mysql_select_only_account_acknowledged=True,
        ),
    )
    app = FastAPI(lifespan=main.lifespan)

    with pytest.raises(TemporalIdentityRuntimeError) as captured:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan entered despite a missing pin document")

    assert captured.value.code == "pin_file_unavailable"
    assert marker not in str(captured.value)
    assert str(tmp_path) not in repr(captured.value)
    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
async def test_enabled_lifespan_database_schema_failure_disposes_owned_engine(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A database lacking pre-existing tables fails closed and releases its engine."""
    from opendata import main
    from opendata.api import temporal_identity_runtime as runtime_module
    from opendata.api.temporal_identity_runtime import TemporalIdentityRuntimeError

    _isolate_lifespan(monkeypatch)
    warehouse = tmp_path / "warehouse-without-migrations.sqlite"
    with closing(sqlite3.connect(warehouse)):
        pass
    pin_file = tmp_path / "temporal-identity-pins.json"
    digest = _write_pin_file(pin_file)
    monkeypatch.setattr(main, "settings", _enabled_settings(warehouse, pin_file, digest))

    disposed: list[bool] = []
    original_create_engine = runtime_module._create_configured_engine

    def create_monitored_engine(
        url: URL, *, connect_args: dict[str, object] | None = None
    ) -> Engine:
        engine = original_create_engine(url, connect_args=connect_args)
        event.listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        return engine

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    app = FastAPI(lifespan=main.lifespan)

    with pytest.raises(TemporalIdentityRuntimeError) as captured:
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan entered despite missing migrated tables")

    assert captured.value.code == "warehouse_unavailable"
    assert str(warehouse) not in repr(captured.value)
    assert disposed == [True]
    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
async def test_startup_failure_after_injection_closes_runtime_and_clears_state(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    """A later startup exception still revokes facades and disposes the runtime."""
    from opendata import main
    from opendata.api import temporal_identity_runtime as runtime_module
    from opendata.data import providers

    _isolate_lifespan(monkeypatch)
    warehouse = _create_existing_warehouse(tmp_path / "warehouse.sqlite")
    pin_file = tmp_path / "temporal-identity-pins.json"
    digest = _write_pin_file(pin_file)
    monkeypatch.setattr(main, "settings", _enabled_settings(warehouse, pin_file, digest))

    disposed: list[bool] = []
    created: list[TemporalIdentityRuntime] = []
    original_create_engine = runtime_module._create_configured_engine
    original_factory = main.create_temporal_identity_runtime

    def create_monitored_engine(
        url: URL, *, connect_args: dict[str, object] | None = None
    ) -> Engine:
        engine = original_create_engine(url, connect_args=connect_args)
        event.listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        return engine

    def capture_runtime(
        config: TemporalIdentityRuntimeConfig | None = None,
    ) -> TemporalIdentityRuntime:
        runtime = original_factory(config)
        created.append(runtime)
        return runtime

    def fail_provider_startup() -> list[object]:
        raise RuntimeError("controlled provider bootstrap failure")

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    monkeypatch.setattr(main, "create_temporal_identity_runtime", capture_runtime)
    monkeypatch.setattr(providers, "register_providers", fail_provider_startup)
    app = FastAPI(lifespan=main.lifespan)

    with pytest.raises(RuntimeError, match="controlled provider bootstrap failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan entered despite provider bootstrap failure")

    assert len(created) == 1
    runtime = created[0]
    assert runtime.pin_registry is None
    assert runtime.snapshot_store is None
    assert disposed == [True]
    assert app.state.temporal_identity_pin_registry is None
    assert app.state.temporal_identity_snapshot_store is None


@pytest.mark.asyncio
async def test_existing_scheduler_enabled_lifecycle_order_is_preserved(
    monkeypatch: MonkeyPatch,
) -> None:
    """The wrapper keeps the existing scheduler-enabled startup and shutdown sequence."""
    from opendata import main
    from opendata.core import database
    from opendata.data import providers
    from opendata.pipeline import jobs, scheduling
    from opendata.services.data_acquisition import DataAcquisitionService

    events: list[str] = []
    monkeypatch.setattr(main, "settings", _settings(enable_scheduler=True))
    monkeypatch.setattr(main, "_testing_mode", lambda: False)
    monkeypatch.setattr(database, "create_tables", lambda: _record_async(events, "create_tables"))
    monkeypatch.setattr(main, "init_db", lambda: _record_async(events, "init_db"))
    monkeypatch.setattr(main, "close_db", lambda: _record_async(events, "close_db"))

    def register_providers() -> list[object]:
        events.append("providers")
        return []

    monkeypatch.setattr(
        providers,
        "register_providers",
        register_providers,
    )

    def decide(**_kwargs: object) -> object:
        events.append("decision")
        return SimpleNamespace(enabled=True, reason="enabled for test")

    monkeypatch.setattr(scheduling, "scheduler_decision", decide)
    monkeypatch.setattr(main.task_scheduler, "start", lambda: _record_async(events, "start"))
    monkeypatch.setattr(main.task_scheduler, "shutdown", lambda: _record_async(events, "shutdown"))
    monkeypatch.setattr(
        jobs,
        "attach_builtin_jobs",
        lambda: _record_async_value(events, "attach_jobs", ["fixture-job"]),
    )
    monkeypatch.setattr(
        DataAcquisitionService,
        "_akshare_executor",
        SimpleNamespace(shutdown=lambda **_kwargs: None),
    )
    app = FastAPI(lifespan=main.lifespan)

    async with app.router.lifespan_context(app):
        events.append("entered")

    assert events == [
        "create_tables",
        "init_db",
        "providers",
        "decision",
        "start",
        "attach_jobs",
        "entered",
        "shutdown",
        "close_db",
    ]


@pytest.mark.asyncio
async def test_main_app_lifespan_migrated_snapshot_and_asgi_resolution_end_to_end(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Serve a canonical migrated SQLite snapshot through the real app lifespan."""
    from httpx import ASGITransport, AsyncClient

    from opendata import main
    from opendata.api import temporal_identity_runtime as runtime_module
    from opendata.api.dependencies import Principal, get_current_principal
    from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
    from opendata.api.published_data import get_dataset_grants
    from opendata.api.temporal_identity_runtime import TemporalIdentityRuntimeError
    from opendata.data.temporal_instrument_identity import (
        TemporalIdentityAssignment,
        TemporalIdentityRegistry,
    )
    from opendata.models.user import User, UserRole

    _isolate_lifespan(monkeypatch)
    registry = TemporalIdentityRegistry(
        reference_version="identity-reference-v1",
        evidence_ref="tests/fixtures/synthetic-identity-registry.json",
        assignments=(
            TemporalIdentityAssignment(
                assignment_id="assignment-600000-v1",
                instrument_id="instrument-600000",
                exchange="XSHG",
                symbol="600000",
                valid_from=date(2020, 1, 1),
                valid_to=date(2020, 1, 2),
                source_id="akshare",
                source_version="synthetic-source-v1",
                evidence_ref="tests/fixtures/synthetic-identity-row.json",
                source_sha256="b" * 64,
            ),
        ),
    )
    warehouse_path = tmp_path / "migrated-identity-warehouse.sqlite"
    digest = _migrate_and_seed_snapshot(warehouse_path, registry)
    pin_path = tmp_path / "temporal-identity-pins.json"
    _write_pinned_runtime_file(pin_path, digest)
    schema_before_startup = _sqlite_schema(warehouse_path)
    monkeypatch.setattr(
        main,
        "settings",
        _settings(
            temporal_identity_enabled=True,
            temporal_identity_pin_file=str(pin_path),
            temporal_identity_pin_sha256=hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            temporal_identity_warehouse_sqlite_path=str(warehouse_path),
        ),
    )

    user = User(
        id=41,
        username="temporal-lifespan-test",
        email="temporal-lifespan@example.test",
        hashed_password="synthetic-test-only",
        role=UserRole.USER,
        is_active=True,
    )
    principal = Principal(user=user, api_key_id=91, scopes=("identity.resolve", "equity"))
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id="instrument.identity",
                domain="equity",
                source_id="akshare",
                permitted_operations=("identity.resolve",),
                purposes=("research",),
                valid_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
                valid_until=datetime(2100, 1, 1, tzinfo=timezone.utc),
                review_evidence="OD204-SYNTHETIC-TEST",
            ),
        )
    )

    app = main.app
    dependency_overrides_before = dict(app.dependency_overrides)
    state_sentinel = object()
    state_before = {
        name: getattr(app.state, name, state_sentinel)
        for name in (
            "temporal_identity_pin_registry",
            "temporal_identity_snapshot_store",
        )
    }
    monkeypatch.setitem(app.dependency_overrides, get_current_principal, lambda: principal)
    monkeypatch.setitem(app.dependency_overrides, get_dataset_grants, lambda: grants)

    engines: list[Engine] = []
    statements: list[str] = []
    disposed: list[bool] = []
    disposed_engine_ids: set[int] = set()
    original_create_engine = runtime_module._create_configured_engine

    def create_monitored_engine(
        url: URL, *, connect_args: dict[str, object] | None = None
    ) -> Engine:
        engine = original_create_engine(url, connect_args=connect_args)
        engines.append(engine)
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _connection, _cursor, statement, _params, _context, _many: statements.append(
                " ".join(statement.split())
            ),
        )

        def note_disposed(disposed_engine: Engine) -> None:
            disposed.append(True)
            disposed_engine_ids.add(id(disposed_engine))

        event.listen(engine, "engine_disposed", note_disposed)
        return engine

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    facade = None
    try:
        async with app.router.lifespan_context(app):
            pin_registry = app.state.temporal_identity_pin_registry
            facade = app.state.temporal_identity_snapshot_store
            assert pin_registry is not None
            assert facade is not None
            assert pin_registry.get("instrument.identity").snapshot_sha256 == digest

            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as client:
                known_and_unknown = await client.post(
                    "/api/v2/identity/resolve",
                    json={
                        "dataset_id": "instrument.identity",
                        "authority_id": "reviewed-market-authority",
                        "registry_id": "cn-a-share",
                        "reference_version": "identity-reference-v1",
                        "snapshot_sha256": digest,
                        "window_start": "2020-01-01",
                        "window_end_exclusive": "2020-01-05",
                        "exchange": "XSHG",
                        "symbol": "600000",
                    },
                )
                unknown_pair = await client.post(
                    "/api/v2/identity/resolve",
                    json={
                        "dataset_id": "instrument.identity",
                        "authority_id": "reviewed-market-authority",
                        "registry_id": "cn-a-share",
                        "reference_version": "identity-reference-v1",
                        "snapshot_sha256": digest,
                        "window_start": "2020-01-01",
                        "window_end_exclusive": "2020-01-05",
                        "exchange": "XSHG",
                        "symbol": "999999",
                    },
                )

            assert known_and_unknown.status_code == 200
            body = known_and_unknown.json()
            assert body["segments"][0]["status"] == "known"
            assert body["segments"][0]["instrument_id"] == "instrument-600000"
            assert body["segments"][1]["status"] == "unknown"
            assert body["segments"][1]["start"] == "2020-01-02"
            assert body["source_integrity"] == (
                "snapshot_bytes_pinned_source_evidence_not_authenticated"
            )
            assert unknown_pair.status_code == 200
            assert unknown_pair.json()["segments"]
            assert all(
                segment["status"] == "unknown" for segment in unknown_pair.json()["segments"]
            )

        assert app.state.temporal_identity_pin_registry is None
        assert app.state.temporal_identity_snapshot_store is None
        assert facade is not None
        with pytest.raises(TemporalIdentityRuntimeError, match="runtime_closed"):
            facade.get(
                authority_id="reviewed-market-authority",
                registry_id="cn-a-share",
                reference_version="identity-reference-v1",
                expected_sha256=digest,
            )
        assert _sqlite_schema(warehouse_path) == schema_before_startup
        assert statements
        statement_types = {statement.split(maxsplit=1)[0].upper() for statement in statements}
        assert statement_types <= {"BEGIN", "PRAGMA", "SELECT"}
        assert any(
            statement.upper().startswith("SELECT ")
            and "OD_TEMPORAL_IDENTITY_SNAPSHOTS" in statement.upper()
            and "OD_TEMPORAL_IDENTITY_SNAPSHOT_CHUNKS" in statement.upper()
            for statement in statements
        )
        assert disposed == [True]
    finally:
        for engine in engines:
            if id(engine) not in disposed_engine_ids:
                engine.dispose()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(dependency_overrides_before)
        for name, previous in state_before.items():
            if previous is state_sentinel:
                with suppress(AttributeError):
                    delattr(app.state, name)
            else:
                setattr(app.state, name, previous)


async def _record_async(events: list[str], name: str) -> None:
    """Append one event from an async lifecycle seam."""
    events.append(name)


async def _record_async_value(events: list[str], name: str, value: object) -> object:
    """Append one event and return its fixture result from an async seam."""
    events.append(name)
    return value
