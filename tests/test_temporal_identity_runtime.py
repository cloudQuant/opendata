"""Isolated lifecycle and configuration tests for temporal identity runtime setup."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError

import opendata.api.temporal_identity_runtime as runtime_module
from opendata.api.temporal_identity import TemporalIdentityPinRegistry
from opendata.api.temporal_identity_runtime import (
    TemporalIdentityRuntimeConfig,
    TemporalIdentityRuntimeError,
    create_temporal_identity_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import URL, Engine

_PIN: dict[str, str] = {
    "dataset_id": "instrument.identity",
    "source_id": "akshare",
    "authority_id": "reviewed-market-authority",
    "registry_id": "cn-a-share",
    "reference_version": "identity-reference-v1",
    "snapshot_sha256": "a" * 64,
}
_PINS: dict[str, object] = {"schema_version": 1, "pins": [_PIN]}
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


def _write_pin_file(path: Path, document: object = _PINS) -> tuple[Path, str]:
    """Write one synthetic operator pin file with the required private mode."""
    raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, hashlib.sha256(raw).hexdigest()


def _create_existing_warehouse(path: Path, *, schema: bool = True) -> Path:
    """Create a disposable SQLite file with only the already-migrated fixture schema."""
    with closing(sqlite3.connect(path)) as connection:
        if schema:
            connection.executescript(_HEADER_SQL + _CHUNKS_SQL)
    return path


def _config(path: Path, pin_file: Path, digest: str) -> TemporalIdentityRuntimeConfig:
    """Return a complete enabled configuration for one synthetic SQLite file."""
    return TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=path,
    )


def test_disabled_runtime_does_not_read_files_or_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disabled branch has no path, credential, or database side effects."""

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("disabled runtime attempted I/O")

    monkeypatch.setattr(runtime_module, "_read_operator_pin_file", unexpected)
    monkeypatch.setattr(runtime_module, "_build_engine", unexpected)
    runtime = create_temporal_identity_runtime(
        TemporalIdentityRuntimeConfig(
            enabled=False,
            pin_file="/path/that/does/not/exist",
            pin_sha256="invalid",
            warehouse_url="invalid",
        )
    )

    assert runtime.enabled is False
    assert runtime.pin_registry is None
    assert runtime.snapshot_store is None
    runtime.close()


def test_default_runtime_is_disabled_and_context_manager_closes_idempotently() -> None:
    """Omitted configuration stays inert; entering and closing twice is safe."""
    runtime = create_temporal_identity_runtime()
    assert runtime.enabled is False

    with runtime as active_runtime:
        assert active_runtime is runtime
        assert active_runtime.pin_registry is None
        assert active_runtime.snapshot_store is None

    runtime.close()


def test_snapshot_lookup_forwards_only_pinned_read_arguments() -> None:
    """The public snapshot facade forwards get arguments without a write API."""

    class SnapshotReader:
        def get(self, **kwargs: object) -> dict[str, object]:
            return kwargs

    lookup = runtime_module.TemporalIdentitySnapshotLookup(SnapshotReader())
    result = lookup.get(
        authority_id="authority",
        registry_id="registry",
        reference_version="version",
        expected_sha256="a" * 64,
    )

    assert result == {
        "authority_id": "authority",
        "registry_id": "registry",
        "reference_version": "version",
        "expected_sha256": "a" * 64,
    }
    assert not hasattr(lookup, "put")


def test_non_boolean_enable_flag_fails_before_configuration_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truthy non-bools cannot enable the runtime by accident."""

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("invalid enable flag attempted I/O")

    monkeypatch.setattr(runtime_module, "_validate_pin_config", unexpected)
    config = TemporalIdentityRuntimeConfig(enabled=1)  # type: ignore[arg-type]

    with pytest.raises(TemporalIdentityRuntimeError, match="runtime_config_invalid"):
        create_temporal_identity_runtime(config)


def test_enabled_runtime_reads_existing_schema_and_keeps_sqlite_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid pin loads while both the SQLite URI and connection forbid writes."""
    warehouse = _create_existing_warehouse(tmp_path / "warehouse.sqlite")
    pin_file, digest = _write_pin_file(tmp_path / "temporal-identity-pins.json")
    statements: list[str] = []
    disposed: list[bool] = []
    original_create_engine = runtime_module._create_configured_engine

    def create_monitored_engine(url: URL) -> Engine:
        engine = original_create_engine(url)
        event.listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _connection, _cursor, statement, _parameters, _context, _many: statements.append(
                statement.lstrip().split(maxsplit=1)[0].upper()
            ),
        )
        return engine

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    runtime = create_temporal_identity_runtime(_config(warehouse, pin_file, digest))
    assert runtime.enabled is True
    assert type(runtime.pin_registry) is TemporalIdentityPinRegistry
    assert runtime.pin_lookup is not None
    assert runtime.snapshot_store is not None
    assert callable(runtime.snapshot_store.get)
    assert not hasattr(runtime.snapshot_store, "put")
    assert runtime.pin_lookup.get("instrument.identity") == runtime.pin_registry.get(
        "instrument.identity"
    )
    assert runtime.pin_lookup.get("unknown.dataset") is None
    assert statements == ["SELECT", "SELECT"]

    try:
        assert runtime._engine is not None
        assert runtime._engine.url.query["mode"] == "ro"
        assert runtime._engine.url.query["uri"] == "true"
        with runtime._engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
            with pytest.raises(SQLAlchemyError):
                connection.exec_driver_sql("CREATE TABLE forbidden_write (id INTEGER)")
    finally:
        runtime.close()
    assert disposed == [True]


def test_injected_sqlite_engine_is_guarded_but_not_disposed(tmp_path: Path) -> None:
    """Injected engines receive query-only checkout protection and remain caller-owned."""
    warehouse = _create_existing_warehouse(tmp_path / "injected.sqlite")
    engine = create_engine(
        runtime_module.URL.create(
            "sqlite+pysqlite",
            database=f"file:{warehouse}",
            query={"mode": "ro", "uri": "true"},
        )
    )
    pin_file, digest = _write_pin_file(tmp_path / "pins.json")
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_engine=engine,
    )
    runtime = create_temporal_identity_runtime(config)
    assert runtime.pin_lookup is not None
    assert runtime.snapshot_store is not None
    checkout_guard = runtime._checkout_guard
    assert checkout_guard is not None
    assert event.contains(engine.pool, "checkout", checkout_guard)
    pin_lookup = runtime.pin_lookup
    snapshot_lookup = runtime.snapshot_store

    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA query_only").scalar_one() == 1
    finally:
        runtime.close()

    assert runtime.pin_registry is None
    assert runtime.pin_lookup is None
    assert runtime.snapshot_store is None
    assert not event.contains(engine.pool, "checkout", checkout_guard)
    with pytest.raises(TemporalIdentityRuntimeError, match="runtime_closed"):
        pin_lookup.get("instrument.identity")
    with pytest.raises(TemporalIdentityRuntimeError, match="runtime_closed"):
        snapshot_lookup.get(
            authority_id="reviewed-market-authority",
            registry_id="cn-a-share",
            reference_version="identity-reference-v1",
            expected_sha256="a" * 64,
        )
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        with pytest.raises(SQLAlchemyError):
            connection.exec_driver_sql("CREATE TABLE forbidden_after_close (id INTEGER)")
    engine.dispose()


def test_injected_sqlite_engine_must_declare_read_only_uri(tmp_path: Path) -> None:
    """A checkout PRAGMA alone is not accepted as SQLite file-level protection."""
    warehouse = _create_existing_warehouse(tmp_path / "injected-writable.sqlite")
    engine = create_engine(f"sqlite:///{warehouse}")
    config = TemporalIdentityRuntimeConfig(enabled=True, warehouse_engine=engine)

    try:
        with pytest.raises(TemporalIdentityRuntimeError, match="sqlite_read_only_engine_required"):
            runtime_module._build_engine(config)
    finally:
        engine.dispose()


def test_engine_builder_requires_one_valid_target() -> None:
    """Missing or conflicting warehouse targets fail before any connection work."""
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_config_invalid"):
        runtime_module._build_engine(TemporalIdentityRuntimeConfig(enabled=True))

    conflicting = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_sqlite_path="/var/lib/opendata/warehouse.sqlite",
        warehouse_url="mysql+pymysql://reader:placeholder@db.example/warehouse",
    )
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_config_invalid"):
        runtime_module._build_engine(conflicting)


def test_engine_builder_rejects_non_engine_and_unsupported_injections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injected objects must be real supported SQLAlchemy engines."""
    invalid_injection = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_engine=object(),  # type: ignore[arg-type]
    )
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_config_invalid"):
        runtime_module._build_engine(invalid_injection)

    engine = create_engine("sqlite://")
    monkeypatch.setattr(runtime_module, "_supported_dialect", lambda _engine: False)
    try:
        with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_dialect_unsupported"):
            runtime_module._build_engine(
                TemporalIdentityRuntimeConfig(enabled=True, warehouse_engine=engine)
            )
    finally:
        engine.dispose()


def test_engine_builder_requires_acknowledgement_for_mysql_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secure URL still requires the explicit least-privilege operator ack."""
    monkeypatch.setattr(
        runtime_module,
        "_create_configured_engine",
        lambda *_args, **_kwargs: pytest.fail("missing acknowledgement reached engine creation"),
    )
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_url=(
            "mysql+pymysql://reader:placeholder@db.example/warehouse"
            "?ssl_ca=%2Fetc%2Fssl%2Fca.pem&ssl_verify_cert=true&ssl_verify_identity=true"
        ),
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="mysql_select_only_account_required"):
        runtime_module._build_engine(config)


@pytest.mark.parametrize(
    ("warehouse_url", "error_code"),
    [
        (" ", "warehouse_config_invalid"),
        ("mysql+pymysql://[invalid", "warehouse_url_invalid"),
        ("postgresql://db.example/warehouse", "warehouse_url_invalid"),
        ("mysql+pymysql:///warehouse", "warehouse_url_invalid"),
        ("mysql+pymysql://db.example", "warehouse_url_invalid"),
    ],
)
def test_mysql_url_parser_rejects_malformed_and_wrong_targets_before_engine_creation(
    warehouse_url: str,
    error_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed URL syntax, wrong drivers, and incomplete targets fail closed."""
    monkeypatch.setattr(
        runtime_module,
        "_create_configured_engine",
        lambda *_args, **_kwargs: pytest.fail("invalid URL reached engine creation"),
    )
    config = TemporalIdentityRuntimeConfig(enabled=True, warehouse_url=warehouse_url)

    with pytest.raises(TemporalIdentityRuntimeError, match=error_code):
        runtime_module._build_engine(config)


def test_mysql_injection_rejects_url_options_and_requires_select_only_acknowledgement() -> None:
    """Injected MySQL engines cannot hide startup SQL in URL options."""
    with_query = create_engine(
        "mysql+pymysql://reader:placeholder@db.example/warehouse?init_command=SELECT%201"
    )
    try:
        with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_url_invalid"):
            runtime_module._build_engine(
                TemporalIdentityRuntimeConfig(
                    enabled=True,
                    warehouse_engine=with_query,
                    mysql_select_only_account_acknowledged=True,
                )
            )
    finally:
        with_query.dispose()

    without_ack = create_engine("mysql+pymysql://reader:placeholder@db.example/warehouse")
    try:
        with pytest.raises(
            TemporalIdentityRuntimeError, match="mysql_select_only_account_required"
        ):
            runtime_module._build_engine(
                TemporalIdentityRuntimeConfig(enabled=True, warehouse_engine=without_ack)
            )
    finally:
        without_ack.dispose()


def test_injected_mysql_engine_requires_acknowledgement_and_remains_caller_owned() -> None:
    """A trusted, unadorned injected MySQL engine is adopted without ownership."""
    engine = create_engine("mysql+pymysql://reader:placeholder@db.example/warehouse")
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_engine=engine,
        mysql_select_only_account_acknowledged=True,
    )

    try:
        adopted, owns_engine = runtime_module._build_engine(config)
        assert adopted is engine
        assert owns_engine is False
    finally:
        engine.dispose()


def test_sqlite_warehouse_path_validation_failures(tmp_path: Path) -> None:
    """Unsafe, absent, and query-like SQLite paths are rejected before engine creation."""
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_invalid"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(enabled=True, warehouse_sqlite_path="relative.sqlite")
        )

    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_invalid"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(
                enabled=True,
                warehouse_sqlite_path=tmp_path / "warehouse?mode=rw.sqlite",
            )
        )

    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_unavailable"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(
                enabled=True, warehouse_sqlite_path=tmp_path / "missing.sqlite"
            )
        )


def test_sqlite_warehouse_rejects_symlink_and_world_writable_files(tmp_path: Path) -> None:
    """SQLite targets must be private regular files without symlink traversal."""
    target = tmp_path / "target.sqlite"
    target.write_bytes(b"")
    link = tmp_path / "linked.sqlite"
    link.symlink_to(target)
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_unsafe"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(enabled=True, warehouse_sqlite_path=link)
        )

    permissive = tmp_path / "permissive.sqlite"
    permissive.write_bytes(b"")
    permissive.chmod(0o666)
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_unsafe"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(enabled=True, warehouse_sqlite_path=permissive)
        )


def test_warehouse_directory_guard_distinguishes_sticky_ancestors() -> None:
    """Root-owned sticky ancestors are allowed, while writable parent dirs fail."""
    unsafe_directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o770,
        st_uid=os.geteuid(),
    )
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_unsafe"):
        runtime_module._check_warehouse_directory(unsafe_directory, is_parent=False)

    sticky_ancestor = SimpleNamespace(
        st_mode=stat.S_IFDIR | stat.S_ISVTX | 0o1777,
        st_uid=0,
    )
    runtime_module._check_warehouse_directory(sticky_ancestor, is_parent=False)
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_unsafe"):
        runtime_module._check_warehouse_directory(sticky_ancestor, is_parent=True)


def test_sqlite_warehouse_requires_safe_platform_flags_and_stable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File descriptor protections fail closed if unsupported or if the file changes."""
    warehouse = tmp_path / "warehouse.sqlite"
    warehouse.write_bytes(b"")
    warehouse.chmod(0o600)

    no_follow_flag = os.O_NOFOLLOW
    monkeypatch.delattr(runtime_module.os, "O_NOFOLLOW")
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_platform_unsupported"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(enabled=True, warehouse_sqlite_path=warehouse)
        )
    monkeypatch.setattr(runtime_module.os, "O_NOFOLLOW", no_follow_flag, raising=False)

    monkeypatch.setattr(runtime_module, "_same_file", lambda _left, _right: False)
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_path_changed"):
        runtime_module._build_engine(
            TemporalIdentityRuntimeConfig(enabled=True, warehouse_sqlite_path=warehouse)
        )


def test_configured_engine_creation_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Database-library exceptions cannot leak driver details into runtime errors."""

    def fail_create_engine(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError("sensitive connection text")

    monkeypatch.setattr(runtime_module, "create_engine", fail_create_engine)
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_engine_unavailable") as exc:
        runtime_module._create_configured_engine(runtime_module.URL.create("sqlite", database="x"))
    assert "sensitive connection text" not in str(exc.value)


def test_configured_engine_passes_reviewed_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory forwards only its already-validated driver arguments."""
    received: dict[str, object] = {}
    engine = object()

    def capture_create_engine(url: URL, **kwargs: object) -> Any:
        received["url"] = url
        received.update(kwargs)
        return engine

    monkeypatch.setattr(runtime_module, "create_engine", capture_create_engine)
    connect_args = {
        "ssl_ca": "/etc/ssl/ca.pem",
        "ssl_verify_cert": True,
        "ssl_verify_identity": True,
    }

    result = runtime_module._create_configured_engine(
        runtime_module.URL.create("mysql+pymysql", host="db.example", database="warehouse"),
        connect_args=connect_args,
    )

    assert result is engine
    assert received["connect_args"] == connect_args
    assert received["poolclass"] is runtime_module.NullPool
    assert received["pool_pre_ping"] is False


def test_unsupported_dialect_has_no_read_only_hook() -> None:
    """Only the explicitly implemented SQL dialects receive checkout guards."""
    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_dialect_unsupported"):
        runtime_module._readonly_checkout_guard("postgresql")


def test_supported_dialects_select_their_read_only_checkout_guard() -> None:
    """SQLite and MySQL share a fixed guard selection with no caller SQL input."""
    assert (
        runtime_module._readonly_checkout_guard("sqlite") is runtime_module._set_sqlite_query_only
    )
    assert (
        runtime_module._readonly_checkout_guard("mysql")
        is runtime_module._set_mysql_transaction_read_only
    )


def test_startup_fails_closed_when_migration_schema_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime rejects a missing schema and does not create migration tables."""
    warehouse = _create_existing_warehouse(tmp_path / "unmigrated.sqlite", schema=False)
    pin_file, digest = _write_pin_file(tmp_path / "pins.json")
    disposed: list[bool] = []
    original_create_engine = runtime_module._create_configured_engine
    original_event_listen = runtime_module.event.listen
    original_guard_factory = runtime_module._readonly_checkout_guard
    created_pools: list[object] = []
    installed_guards: list[Any] = []

    def create_monitored_engine(url: URL) -> Engine:
        engine = original_create_engine(url)
        created_pools.append(engine.pool)
        original_event_listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        return engine

    def record_checkout_guard(dialect: str) -> Any:
        guard = original_guard_factory(dialect)
        installed_guards.append(guard)
        return guard

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    monkeypatch.setattr(runtime_module, "_readonly_checkout_guard", record_checkout_guard)

    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_unavailable"):
        create_temporal_identity_runtime(_config(warehouse, pin_file, digest))
    assert disposed == [True]
    assert len(created_pools) == len(installed_guards) == 1
    assert not event.contains(created_pools[0], "checkout", installed_guards[0])

    with closing(sqlite3.connect(warehouse)) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert tables == []


def test_startup_listener_failure_releases_owned_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed startup hook does not leak the newly created SQLite engine."""
    warehouse = _create_existing_warehouse(tmp_path / "listener-failure.sqlite")
    pin_file, digest = _write_pin_file(tmp_path / "pins.json")
    disposed: list[bool] = []
    original_create_engine = runtime_module._create_configured_engine
    original_event_listen = runtime_module.event.listen
    original_event_remove = runtime_module.event.remove

    def create_monitored_engine(url: URL) -> Engine:
        engine = original_create_engine(url)
        original_event_listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
        return engine

    def fail_listener(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic listener failure")

    monkeypatch.setattr(runtime_module, "_create_configured_engine", create_monitored_engine)
    monkeypatch.setattr(
        runtime_module,
        "event",
        SimpleNamespace(listen=fail_listener, remove=original_event_remove),
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_unavailable") as exc:
        create_temporal_identity_runtime(_config(warehouse, pin_file, digest))

    assert str(exc.value) == "warehouse_unavailable"
    assert disposed == [True]


@pytest.mark.parametrize(
    ("raw", "error_code"),
    [
        (
            b'{"schema_version":1,"schema_version":1,"pins":[]}',
            "pin_document_invalid",
        ),
        (
            b'{"schema_version":1,"pins":[{"dataset_id":"x","dataset_id":"y"}]}',
            "pin_document_invalid",
        ),
        (
            b'{"schema_version":1,"pins":[],"unexpected":true}',
            "pin_document_invalid",
        ),
        (b'{"schema_version":2,"pins":[]}', "pin_document_invalid"),
        (b'{"schema_version":1,"pins":[]}', "pin_count_invalid"),
        (b'{"schema_version":1,"pins":[{"dataset_id":"x"}]}', "pin_document_invalid"),
    ],
)
def test_pin_document_rejects_duplicates_unknown_fields_and_invalid_shapes(
    tmp_path: Path, raw: bytes, error_code: str
) -> None:
    """Versioning and exact field rules fail closed before a database is opened."""
    pin_file = tmp_path / "invalid-pins.json"
    pin_file.write_bytes(raw)
    pin_file.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match=error_code):
        create_temporal_identity_runtime(config)


@pytest.mark.parametrize(
    ("raw", "error_code"),
    [
        (b"{invalid", "pin_document_invalid"),
        (b'{"schema_version":1,"pins":[NaN]}', "pin_document_invalid"),
        (
            b'{"schema_version":1,"pins":["\\ud800"]}',
            "pin_document_invalid",
        ),
        (
            b'{"schema_version":1,"pins":' + b"[" * 9 + b"0" + b"]" * 9 + b"}",
            "pin_document_bounds_invalid",
        ),
    ],
)
def test_pin_decoder_rejects_malformed_nonstandard_unicode_and_deep_json(
    raw: bytes, error_code: str
) -> None:
    """JSON parser error paths stay bounded and return fixed error codes."""
    digest = hashlib.sha256(raw).hexdigest()

    with pytest.raises(TemporalIdentityRuntimeError, match=error_code):
        runtime_module._decode_pin_registry(raw, digest)


def test_json_node_budget_rejects_wide_documents() -> None:
    """A broad but shallow decoded document is bounded before schema work."""
    document = [None] * runtime_module._MAX_JSON_NODES

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_document_bounds_invalid"):
        runtime_module._check_json_budget(document)


def test_pin_file_digest_must_match_independent_configuration(tmp_path: Path) -> None:
    """A file cannot authorize its own pin set when its external digest differs."""
    pin_file, _digest = _write_pin_file(tmp_path / "pins.json")
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256="b" * 64,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_digest_mismatch"):
        create_temporal_identity_runtime(config)


@pytest.mark.parametrize("digest", [None, "A" * 64, "a" * 63])
def test_invalid_pin_digest_is_rejected_before_file_access(
    tmp_path: Path, digest: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Digest syntax is validated before opening the configured pin path."""

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("invalid digest attempted file access")

    monkeypatch.setattr(runtime_module, "_read_operator_pin_file", unexpected)
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=tmp_path / "pins.json",
        pin_sha256=digest,  # type: ignore[arg-type]
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_digest_invalid"):
        create_temporal_identity_runtime(config)


@pytest.mark.parametrize("pin_file", [None, True, 42])
def test_invalid_pin_path_type_is_rejected_without_reading(
    pin_file: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only string and Path pin inputs are accepted."""

    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("invalid pin path attempted file access")

    monkeypatch.setattr(runtime_module, "_read_operator_pin_file", unexpected)
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,  # type: ignore[arg-type]
        pin_sha256="a" * 64,
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_invalid"):
        create_temporal_identity_runtime(config)


def test_missing_operator_pin_file_has_content_free_error(tmp_path: Path) -> None:
    """A missing file fails before engine construction without exposing paths."""
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=tmp_path / "missing-pins.json",
        pin_sha256="a" * 64,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_unavailable"):
        create_temporal_identity_runtime(config)


def test_pin_reader_rejects_directory_as_pin_file(tmp_path: Path) -> None:
    """A directory cannot be mistaken for a regular operator pin file."""
    pin_directory = tmp_path / "pin-directory"
    pin_directory.mkdir()

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_not_regular"):
        runtime_module._read_operator_pin_file(pin_directory)


def test_pin_reader_fails_closed_when_required_platform_flags_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The secure descriptor-based reader does not fall back to weaker APIs."""
    monkeypatch.delattr(runtime_module.os, "O_NOFOLLOW")

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_platform_unsupported"):
        runtime_module._read_operator_pin_file(tmp_path / "unused-pins.json")


def test_pin_reader_rejects_file_replacement_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File identity is rechecked after opening the no-follow descriptor."""
    pin_file, _digest = _write_pin_file(tmp_path / "pins.json")
    monkeypatch.setattr(runtime_module, "_same_file", lambda _left, _right: False)

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_changed"):
        runtime_module._read_operator_pin_file(pin_file)


def test_pin_reader_rejects_parent_symlink_without_following_target(tmp_path: Path) -> None:
    """A symlinked directory component fails closed with a fixed error code."""
    target = tmp_path / "real-directory"
    target.mkdir()
    pin_file, _digest = _write_pin_file(target / "pins.json")
    linked_parent = tmp_path / "linked-directory"
    linked_parent.symlink_to(target, target_is_directory=True)

    with pytest.raises(TemporalIdentityRuntimeError) as exc:
        runtime_module._read_operator_pin_file(linked_parent / pin_file.name)
    assert exc.value.code in {"pin_symlink_rejected", "pin_file_unavailable"}


def test_pin_reader_rejects_mutation_during_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File metadata is checked again after bounded reading completes."""
    pin_file, _digest = _write_pin_file(tmp_path / "pins.json")
    comparisons = iter((True, True, False))
    monkeypatch.setattr(
        runtime_module,
        "_same_file",
        lambda _left, _right: next(comparisons),
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_changed"):
        runtime_module._read_operator_pin_file(pin_file)


def test_directory_binding_checker_detects_replaced_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader rejects a directory whose identity changed after opening."""
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        snapshot = os.fstat(descriptor)
        monkeypatch.setattr(runtime_module, "_same_directory", lambda _left, _right: False)
        with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_changed"):
            runtime_module._verify_directory_bindings([descriptor], [snapshot], ())
    finally:
        os.close(descriptor)


def test_directory_binding_checker_detects_changed_named_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened directories are re-bound to names before accepting the pin file."""
    child = tmp_path / "child"
    child.mkdir()
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    child_fd = os.open("child", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        snapshots = [os.fstat(parent_fd), os.fstat(child_fd)]
        comparisons = iter((True, True, False))
        monkeypatch.setattr(
            runtime_module,
            "_same_directory",
            lambda _left, _right: next(comparisons),
        )

        with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_changed"):
            runtime_module._verify_directory_bindings([parent_fd, child_fd], snapshots, ("child",))
    finally:
        os.close(child_fd)
        os.close(parent_fd)


def test_directory_guard_rejects_writable_directories_but_allows_sticky_ancestor() -> None:
    """Directory ownership and sticky-root exceptions are checked explicitly."""
    writable_owned = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o770,
        st_uid=os.geteuid(),
    )
    with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_unsafe"):
        runtime_module._check_directory(writable_owned, is_final=False)

    root_sticky = SimpleNamespace(
        st_mode=stat.S_IFDIR | stat.S_ISVTX | 0o1777,
        st_uid=0,
    )
    runtime_module._check_directory(root_sticky, is_final=False)
    with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_unsafe"):
        runtime_module._check_directory(root_sticky, is_final=True)


@pytest.mark.parametrize(
    ("mode", "owner", "links", "size", "expected_size", "error_code"),
    [
        (stat.S_IFDIR | 0o700, 0, 1, 1, None, "pin_file_not_regular"),
        (stat.S_IFREG | 0o600, os.geteuid(), 2, 1, None, "pin_file_not_regular"),
        (stat.S_IFREG | 0o644, os.geteuid(), 1, 1, None, "pin_file_permissions_unsafe"),
        (stat.S_IFREG | 0o600, os.geteuid(), 1, 2, 1, "pin_file_changed"),
        (stat.S_IFREG | 0o600, os.geteuid(), 1, 0, None, "pin_file_size_invalid"),
    ],
)
def test_pin_metadata_guard_rejects_non_private_files(
    mode: int,
    owner: int,
    links: int,
    size: int,
    expected_size: int | None,
    error_code: str,
) -> None:
    """The pin reader rejects non-regular, linked, permissive, or changed files."""
    info = SimpleNamespace(st_mode=mode, st_uid=owner, st_nlink=links, st_size=size)

    with pytest.raises(TemporalIdentityRuntimeError, match=error_code):
        runtime_module._check_pin_file(info, expected_size=expected_size)


def test_pin_file_rejects_symlink_without_reading_target(tmp_path: Path) -> None:
    """A symlink path is rejected even when it points to a valid private file."""
    actual, digest = _write_pin_file(tmp_path / "actual.json")
    link = tmp_path / "linked.json"
    link.symlink_to(actual)
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=link,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_symlink_rejected"):
        create_temporal_identity_runtime(config)


def test_pin_path_rejects_parent_traversal(tmp_path: Path) -> None:
    """The operator path parser rejects a traversal component before file access."""
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=tmp_path / ".." / "outside.json",
        pin_sha256="a" * 64,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_path_invalid"):
        create_temporal_identity_runtime(config)


@pytest.mark.parametrize(
    "raw_path",
    [
        "",
        "relative.json",
        "//tmp/pins.json",
        "/tmp//pins.json",
        "/tmp/pins.json/",
        "/tmp/./pins.json",
        r"/tmp/pins\file.json",
        "/tmp/pins\x00file.json",
    ],
)
def test_operator_path_parser_rejects_ambiguous_absolute_paths(raw_path: str) -> None:
    """No malformed POSIX path reaches descriptor-based file access."""
    with pytest.raises(ValueError):
        runtime_module._validated_absolute_path(raw_path)


def test_pin_file_rejects_group_or_world_access(tmp_path: Path) -> None:
    """Operator pins are accepted only from an owner-only regular file."""
    pin_file, digest = _write_pin_file(tmp_path / "permissive.json")
    pin_file.chmod(0o644)
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_permissions_unsafe"):
        create_temporal_identity_runtime(config)


def test_pin_file_size_limit_fails_before_json_decode(tmp_path: Path) -> None:
    """Oversized operator input is rejected by descriptor metadata, before parsing."""
    pin_file = tmp_path / "oversized.json"
    pin_file.write_bytes(b"{" + b" " * runtime_module.MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES)
    pin_file.chmod(0o600)
    digest = hashlib.sha256(pin_file.read_bytes()).hexdigest()
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_file_size_invalid"):
        create_temporal_identity_runtime(config)


def test_pin_count_limit_is_explicit(tmp_path: Path) -> None:
    """The runtime rejects more pins than the existing registry permits."""
    pins = []
    for index in range(runtime_module.MAX_TEMPORAL_IDENTITY_PINS + 1):
        pin = dict(_PIN)
        pin["dataset_id"] = f"instrument.identity.{index}"
        pins.append(pin)
    pin_file, digest = _write_pin_file(
        tmp_path / "too-many-pins.json", {"schema_version": 1, "pins": pins}
    )
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_count_invalid"):
        create_temporal_identity_runtime(config)


def test_duplicate_dataset_pins_fail_even_when_document_digest_matches(tmp_path: Path) -> None:
    """The exact registry rejects ambiguous duplicate dataset entries."""
    duplicate = {"schema_version": 1, "pins": [dict(_PIN), dict(_PIN)]}
    pin_file, digest = _write_pin_file(tmp_path / "duplicate.json", duplicate)
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        pin_file=pin_file,
        pin_sha256=digest,
        warehouse_sqlite_path=tmp_path / "missing.sqlite",
    )

    with pytest.raises(TemporalIdentityRuntimeError, match="pin_document_invalid"):
        create_temporal_identity_runtime(config)


class _Cursor:
    """Small fake cursor for checking MySQL read-only setup without a server."""

    def __init__(
        self,
        row: tuple[object, ...] | None = (1,),
        fail_on: str | None = None,
    ) -> None:
        self.statements: list[str] = []
        self.row = row
        self.fail_on = fail_on
        self.closed = False

    def execute(self, statement: str) -> None:
        """Record the SQL sent by the runtime hook."""
        self.statements.append(statement)
        if statement == self.fail_on:
            raise RuntimeError("synthetic execution failure")

    def fetchone(self) -> tuple[object, ...] | None:
        """Report a synthetic server-confirmed read-only transaction state."""
        return self.row

    def close(self) -> None:
        """Match the small subset of DB-API cursor behavior used by the hook."""
        self.closed = True


class _MySQLConnection:
    """Expose only a fake cursor for the checkout guard unit test."""

    def __init__(self, cursor: _Cursor | None = None) -> None:
        self.test_cursor = cursor or _Cursor()

    def cursor(self) -> _Cursor:
        """Return the cursor that records session SQL."""
        return self.test_cursor


def test_mysql_checkout_sets_session_read_only_but_does_not_claim_account_grants() -> None:
    """The runtime requests read-only transactions and documents grants as external."""
    connection = _MySQLConnection()

    runtime_module._set_mysql_transaction_read_only(connection, object(), object())

    assert connection.test_cursor.statements == [
        "SET SESSION TRANSACTION READ ONLY",
        "SELECT @@session.transaction_read_only",
    ]
    assert "cannot inspect" in runtime_module.__doc__


@pytest.mark.parametrize("row", [None, (0,), ("off",)])
def test_mysql_checkout_fails_closed_when_server_does_not_confirm_read_only(
    row: tuple[object, ...] | None,
) -> None:
    """An absent or false session flag rejects the checkout and closes its cursor."""
    connection = _MySQLConnection(_Cursor(row=row))

    with pytest.raises(TemporalIdentityRuntimeError, match="mysql_read_only_unavailable"):
        runtime_module._set_mysql_transaction_read_only(connection, object(), object())

    assert connection.test_cursor.closed is True


def test_mysql_checkout_sanitizes_driver_errors_and_closes_cursor() -> None:
    """A failed session setup is content-free and never leaks a live cursor."""
    connection = _MySQLConnection(_Cursor(fail_on="SET SESSION TRANSACTION READ ONLY"))

    with pytest.raises(TemporalIdentityRuntimeError, match="mysql_read_only_unavailable"):
        runtime_module._set_mysql_transaction_read_only(connection, object(), object())

    assert connection.test_cursor.closed is True


@pytest.mark.parametrize(
    ("guard", "error_code"),
    [
        (runtime_module._set_sqlite_query_only, "sqlite_read_only_unavailable"),
        (runtime_module._set_mysql_transaction_read_only, "mysql_read_only_unavailable"),
    ],
)
def test_read_only_checkout_sanitizes_cursor_creation_errors(guard: Any, error_code: str) -> None:
    """A DB-API cursor failure remains sanitized even when no cursor can close."""

    class BrokenConnection:
        def cursor(self) -> Any:
            raise RuntimeError("sensitive cursor creation error")

    with pytest.raises(TemporalIdentityRuntimeError, match=error_code) as exc:
        guard(BrokenConnection(), object(), object())
    assert "sensitive cursor creation error" not in str(exc.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        ("1", True),
        (" ON ", True),
        ("yes", True),
        (False, False),
        (0, False),
        ("true", False),
    ],
)
def test_mysql_read_only_flag_accepts_only_documented_values(value: object, expected: bool) -> None:
    """Only explicit server flag spellings are treated as read-only."""
    assert runtime_module._mysql_flag_is_true(value) is expected


@pytest.mark.parametrize("row", [None, (0,)])
def test_sqlite_checkout_requires_query_only_and_closes_cursor(
    row: tuple[object, ...] | None,
) -> None:
    """SQLite sessions are rejected unless query-only mode is confirmed."""
    connection = _MySQLConnection(_Cursor(row=row))

    with pytest.raises(TemporalIdentityRuntimeError, match="sqlite_read_only_unavailable"):
        runtime_module._set_sqlite_query_only(connection, object(), object())

    assert connection.test_cursor.closed is True


def test_sqlite_checkout_sanitizes_driver_errors_and_closes_cursor() -> None:
    """Failed PRAGMA execution is converted to a fixed error and closes the cursor."""
    connection = _MySQLConnection(_Cursor(fail_on="PRAGMA query_only = ON"))

    with pytest.raises(TemporalIdentityRuntimeError, match="sqlite_read_only_unavailable"):
        runtime_module._set_sqlite_query_only(connection, object(), object())

    assert connection.test_cursor.closed is True


@pytest.mark.parametrize(
    "query_options",
    [
        "init_command=DROP%20TABLE%20warehouse",
        "read_default_file=%2Ftmp%2Fmysql.cnf",
        "read_default_group=client",
        "autocommit=true",
        "client_flag=131072",
        "ssl_check_hostname=false",
        "ssl_ca=relative-ca.pem&ssl_verify_cert=true&ssl_verify_identity=true",
        "ssl_ca=%2Fetc%2Fssl%2Fca.pem",
        "ssl_ca=%2Fetc%2Fssl%2Fca.pem&ssl_verify_cert=true&ssl_verify_identity=false",
        "ssl_ca=%2Fetc%2Fssl%2Fca.pem&ssl_verify_cert=true",
        "ssl_ca=%2Fetc%2Fssl%2Fca.pem&ssl_ca=%2Ftmp%2Fother.pem"
        "&ssl_verify_cert=true&ssl_verify_identity=true",
    ],
)
def test_mysql_url_rejects_unreviewed_driver_options_before_engine_creation(
    query_options: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsafe startup SQL and non-verified TLS options never reach PyMySQL."""
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_url=f"mysql+pymysql://reader:placeholder@db.example/warehouse?{query_options}",
        mysql_select_only_account_acknowledged=True,
    )
    engine_creation_attempted = False

    def unexpected_engine_creation(_url: URL) -> Any:
        nonlocal engine_creation_attempted
        engine_creation_attempted = True
        raise AssertionError("invalid MySQL URL reached engine creation")

    monkeypatch.setattr(runtime_module, "_create_configured_engine", unexpected_engine_creation)

    with pytest.raises(TemporalIdentityRuntimeError, match="warehouse_url_invalid"):
        runtime_module._build_engine(config)
    assert engine_creation_attempted is False


def test_mysql_url_accepts_only_ca_pinned_hostname_verified_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supported URL policy explicitly pins a CA and checks host identity."""
    config = TemporalIdentityRuntimeConfig(
        enabled=True,
        warehouse_url=(
            "mysql+pymysql://reader:placeholder@db.example/warehouse"
            "?ssl_ca=%2Fetc%2Fssl%2Fmarket-ca.pem"
            "&ssl_verify_cert=true&ssl_verify_identity=true"
        ),
        mysql_select_only_account_acknowledged=True,
    )
    captured_urls: list[URL] = []
    captured_connect_args: list[dict[str, object]] = []
    engine = object()

    def capture_engine_url(url: URL, *, connect_args: dict[str, object]) -> Any:
        captured_urls.append(url)
        captured_connect_args.append(connect_args)
        return engine

    monkeypatch.setattr(runtime_module, "_create_configured_engine", capture_engine_url)

    result, owns_engine = runtime_module._build_engine(config)

    assert result is engine
    assert owns_engine is True
    assert len(captured_urls) == 1
    assert captured_urls[0].query == {}

    dialect_engine = create_engine(captured_urls[0])
    try:
        _driver_args, driver_kwargs = dialect_engine.dialect.create_connect_args(captured_urls[0])
    finally:
        dialect_engine.dispose()
    driver_kwargs.update(captured_connect_args[0])
    assert driver_kwargs["ssl_ca"] == "/etc/ssl/market-ca.pem"
    assert driver_kwargs["ssl_verify_cert"] is True
    assert driver_kwargs["ssl_verify_identity"] is True
    assert "ssl" not in driver_kwargs
