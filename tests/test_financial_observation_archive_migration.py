"""Check the isolated financial archive revision on owned temporary SQLite."""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError

from alembic import command

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT
    / "alembic_financial_archive"
    / "versions"
    / "20261027-0001_financial_observation_archive.py"
)
CONFIG_PATH = REPOSITORY_ROOT / "alembic_financial_archive.ini"
CAPTURES = "od_financial_captures"
OBSERVATIONS = "od_financial_observations"
LINKS = "od_financial_capture_observation_links"
QUARANTINE = "od_financial_observation_quarantine"


def _load_migration() -> ModuleType:
    """Load only the new revision module without importing an Alembic env."""
    spec = importlib.util.spec_from_file_location(
        "financial_archive_migration_0001", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _engine(tmp_path: Path, *, filename: str = "test_financial_archive_001.db") -> Engine:
    """Create a foreign-key-enforcing SQLite engine under pytest's private temp path."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / filename}")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: sqlite3.Connection, _: object) -> None:
        """Enable SQLite foreign keys on this test-owned connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _environment_config(*x_arguments: str) -> Config:
    """Build a Config that runs the actual env.py with explicit x arguments."""
    config = Config(str(CONFIG_PATH))
    config.cmd_opts = Namespace(x=list(x_arguments))
    return config


def _deny_engine_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure rejected env targets cannot create any engine or open a connection."""

    def fail_create_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("the env must reject this target before creating an engine")

    monkeypatch.setattr(sa, "create_engine", fail_create_engine)


class _FakeDialect:
    """Expose only dialect metadata needed by env.py validation."""

    def __init__(
        self,
        name: str,
        server_version_info: tuple[int, ...] | None,
        *,
        is_mariadb: bool = False,
    ) -> None:
        self.name = name
        self.server_version_info = server_version_info
        self.is_mariadb = is_mariadb


class _FakeConnection:
    """A connection-shaped object that cannot perform database operations."""

    def __init__(self, dialect: _FakeDialect) -> None:
        self.dialect = dialect


class _FakeConnectable:
    """Return a fake connection so dialect guards run without any network access."""

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    @contextmanager
    def connect(self) -> Iterator[_FakeConnection]:
        yield self.connection

    def dispose(self) -> None:
        return None


def _apply_revision(engine: Engine, operation_name: str) -> None:
    """Execute one migration operation against the explicitly owned engine."""
    migration = _load_migration()
    operation = getattr(migration, operation_name)
    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            operation()


def _capture_values(**overrides: object) -> dict[str, object]:
    """Build a valid capture row with explicit safe fixture provenance."""
    values: dict[str, object] = {
        "capture_id": "a" * 32,
        "provider_id": "ths",
        "dataset_id": "ths_financial_statement",
        "operation_id": "income_annual",
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "observed_at_utc": datetime(2025, 4, 1, 2, 30, 0, 123456),
        "captured_at_utc": datetime(2025, 4, 1, 2, 30, 0, 654321),
        "source_request_id": "fixture-request-id",
        "http_status": 200,
        "request_summary_sha256": "1" * 64,
        "raw_object_sha256": "2" * 64,
        "raw_object_size_bytes": 100,
        "raw_object_key": "22/" + "2" * 64 + ".blob",
        "raw_manifest_sha256": "3" * 64,
        "raw_manifest_size_bytes": 500,
        "raw_manifest_key": "33/" + "3" * 64 + ".json",
        "retention_class": "durable_candidate",
        "source_history_limit": "unknown",
        "native_unit_mapping_version_declared": "operator-declared-v1",
        "native_unit_mapping_sha256_declared": "4" * 64,
        "unit_mapping_declaration_status": "operator_declared",
        "decode_status": "decoded",
    }
    values.update(overrides)
    return values


def _observation_values(**overrides: object) -> dict[str, object]:
    """Build a valid exact source-native observation row."""
    values: dict[str, object] = {
        "observation_id": "5" * 64,
        "natural_key_sha256": "6" * 64,
        "content_sha256": "7" * 64,
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "report_period": date(2024, 12, 31),
        "source_disclosure_date": date(2025, 3, 31),
        "observed_at_utc": datetime(2025, 4, 1, 2, 30, 0, 123456),
        "item_code": "net_profit",
        "value_decimal_text": "86228150000.123456789012345678",
        "native_unit": "CNY",
        "source_metadata_json": '{"currency":"CNY"}',
        "observation_policy": "current_as_observed_only",
    }
    values.update(overrides)
    return values


def _insert_capture(connection: Connection, **overrides: object) -> None:
    """Insert one capture through named parameters and no implicit defaults."""
    values = _capture_values(**overrides)
    capture_table = sa.Table(CAPTURES, sa.MetaData(), autoload_with=connection)
    connection.execute(sa.insert(capture_table).values(**values))


def _insert_observation(connection: Connection, **overrides: object) -> None:
    """Insert one observation without converting source numeric text to a number."""
    values = _observation_values(**overrides)
    observation_table = sa.Table(OBSERVATIONS, sa.MetaData(), autoload_with=connection)
    connection.execute(sa.insert(observation_table).values(**values))


def _insert_link(
    connection: Connection,
    *,
    link_id: str,
    capture_id: str,
    value_pointer: str,
    observation_id: str,
    row_pointer: str,
) -> None:
    """Insert one source-value pointer through reflected table metadata."""
    link_table = sa.Table(LINKS, sa.MetaData(), autoload_with=connection)
    connection.execute(
        sa.insert(link_table).values(
            link_id=link_id,
            capture_id=capture_id,
            value_pointer=value_pointer,
            observation_id=observation_id,
            row_pointer=row_pointer,
        )
    )


def _insert_quarantine(
    connection: Connection,
    *,
    quarantine_id: str,
    capture_id: str,
    row_pointer: str,
    disclosure_pointer: str | None,
    reason: str,
) -> None:
    """Insert one pointer-only quarantine row through bound values."""
    quarantine_table = sa.Table(QUARANTINE, sa.MetaData(), autoload_with=connection)
    connection.execute(
        sa.insert(quarantine_table).values(
            quarantine_id=quarantine_id,
            capture_id=capture_id,
            row_pointer=row_pointer,
            disclosure_pointer=disclosure_pointer,
            reason=reason,
        )
    )


def test_isolated_config_has_no_default_database_and_uses_own_version_table() -> None:
    """Keep the new chain unconfigured by application and warehouse defaults."""
    config = Config(str(CONFIG_PATH))

    assert config.get_main_option("sqlalchemy.url") == ""
    assert config.get_main_option("version_table") == "alembic_version_financial_archive"
    script_location = config.get_main_option("script_location")
    assert script_location is not None
    assert Path(script_location).is_dir()
    env_source = (REPOSITORY_ROOT / "alembic_financial_archive" / "env.py").read_text()
    assert "opendata.core.config" not in env_source
    assert "os.environ" not in env_source


def test_actual_alembic_env_uses_explicit_temporary_sqlite_target(tmp_path: Path) -> None:
    """Apply the real env.py only to the explicitly named disposable SQLite DB."""
    database_path = tmp_path / "test_financial_archive_env.db"
    database_url = f"sqlite:///{database_path}"
    config = _environment_config(f"financial_archive_url={database_url}")

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    try:
        names = set(inspect(engine).get_table_names())
        assert {
            CAPTURES,
            OBSERVATIONS,
            LINKS,
            QUARANTINE,
            "alembic_version_financial_archive",
        } <= names
        assert "alembic_version" not in names
        assert "alembic_version_data" not in names
    finally:
        engine.dispose()


def test_actual_alembic_env_accepts_in_memory_sqlite_without_disk_artifact() -> None:
    """Exercise the env's explicitly permitted in-memory SQLite target."""
    config = _environment_config("financial_archive_url=sqlite:///:memory:")

    command.upgrade(config, "head")


@pytest.mark.parametrize(
    ("dialect_name", "version", "is_mariadb", "error_message"),
    [
        ("postgresql", (16, 0), False, "temporary SQLite only"),
        ("mariadb", (10, 6, 0), False, "do not support MariaDB"),
        ("mysql", (8, 0, 15), False, "requires MySQL 8.0.16"),
        ("mysql", (8, 0), False, "requires MySQL 8.0.16"),
        ("mysql", None, False, "requires MySQL 8.0.16"),
        ("mysql", (8, 0, 16), True, "do not support MariaDB"),
        ("mysql", (9, 0, 0), False, "requires MySQL 8.0.16"),
    ],
)
def test_actual_alembic_env_rejects_unsupported_connected_dialects_without_database(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    version: tuple[int, ...] | None,
    is_mariadb: bool,
    error_message: str,
) -> None:
    """Exercise connected-dialect guards with a fake connectable and no database."""
    fake_connection = _FakeConnection(_FakeDialect(dialect_name, version, is_mariadb=is_mariadb))
    fake_connectable = _FakeConnectable(fake_connection)
    created_engines: list[object] = []

    def create_fake_engine(*args: object, **kwargs: object) -> _FakeConnectable:
        created_engines.append((args, kwargs))
        return fake_connectable

    monkeypatch.setattr(sa, "create_engine", create_fake_engine)
    config = _environment_config("financial_archive_url=mysql://invalid.invalid/never_connect")

    with pytest.raises(RuntimeError, match=error_message):
        command.upgrade(config, "head")

    assert len(created_engines) == 1


def test_actual_alembic_env_rejects_offline_rendering_without_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse offline SQL generation without opening even a temporary database."""
    _deny_engine_creation(monkeypatch)
    database_path = tmp_path / "test_financial_archive_offline.db"
    database_url = f"sqlite:///{database_path}"
    config = _environment_config(f"financial_archive_url={database_url}")

    with pytest.raises(RuntimeError, match="offline SQL rendering is unsupported"):
        command.upgrade(config, "head", sql=True)

    assert not database_path.exists()


def test_actual_alembic_env_rejects_missing_url_before_engine_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require an explicit target URL rather than application or environment defaults."""
    _deny_engine_creation(monkeypatch)
    config = _environment_config()

    with pytest.raises(RuntimeError, match="target is unset"):
        command.upgrade(config, "head")


@pytest.mark.parametrize(
    ("target_url", "error_message", "database_name"),
    [
        ("not-a-sqlalchemy-url", "target URL is invalid", "malformed.db"),
        (
            "postgresql://invalid.invalid/never_connect",
            "temporary SQLite only",
            "unsupported.db",
        ),
        (
            None,
            "SQLite target must be a test_financial_archive_",
            "unsafe_name.db",
        ),
    ],
)
def test_actual_alembic_env_rejects_invalid_targets_before_engine_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_url: str | None,
    error_message: str,
    database_name: str,
) -> None:
    """Reject malformed, unsupported, and unsafe SQLite targets before connections."""
    _deny_engine_creation(monkeypatch)
    database_path = tmp_path / database_name
    if target_url is None:
        target_url = f"sqlite:///{database_path}"
    config = _environment_config(f"financial_archive_url={target_url}")

    with pytest.raises(RuntimeError, match=error_message):
        command.upgrade(config, "head")

    assert not database_path.exists()


def test_revision_creates_only_four_archive_tables_and_preserves_neighbor(tmp_path: Path) -> None:
    """Limit DDL ownership to the independent capture archive schema."""
    engine = _engine(tmp_path)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE od_financial_archive_neighbor (marker VARCHAR(16) NOT NULL)")
            )
            connection.execute(
                sa.text("INSERT INTO od_financial_archive_neighbor (marker) VALUES ('keep')")
            )

        _apply_revision(engine, "upgrade")

        names = set(inspect(engine).get_table_names())
        assert {CAPTURES, OBSERVATIONS, LINKS, QUARANTINE, "od_financial_archive_neighbor"} <= names
        assert "alembic_version_data" not in names
        assert "alembic_version" not in names
        columns = {column["name"] for column in inspect(engine).get_columns(CAPTURES)}
        assert {"raw_object_sha256", "raw_object_size_bytes", "raw_manifest_sha256"} <= columns
        assert "raw_body" not in columns
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT marker FROM od_financial_archive_neighbor"))
                == "keep"
            )
    finally:
        engine.dispose()


def test_exact_decimal_text_keeps_null_distinct_from_zero_and_datetime_micros(
    tmp_path: Path,
) -> None:
    """Retain decimal tokens and both source and raw-capture microsecond times."""
    engine = _engine(tmp_path)
    try:
        _apply_revision(engine, "upgrade")
        with engine.begin() as connection:
            _insert_observation(connection, value_decimal_text=None)
            _insert_observation(
                connection,
                observation_id="8" * 64,
                natural_key_sha256="9" * 64,
                content_sha256="a" * 64,
                item_code="operating_cashflow",
                value_decimal_text="0",
            )
            _insert_capture(connection)
            observation_table = sa.Table(OBSERVATIONS, sa.MetaData(), autoload_with=connection)
            rows = (
                connection.execute(
                    sa.select(observation_table.c.value_decimal_text).order_by(
                        observation_table.c.observation_id
                    )
                )
                .scalars()
                .all()
            )
            assert rows == [None, "0"]
            capture_table = sa.Table(CAPTURES, sa.MetaData(), autoload_with=connection)
            observed, captured = connection.execute(
                sa.select(
                    capture_table.c.observed_at_utc,
                    capture_table.c.captured_at_utc,
                ).where(capture_table.c.capture_id == "a" * 32)
            ).one()
            assert observed.microsecond == 123456
            assert captured.microsecond == 654321
    finally:
        engine.dispose()


def test_foreign_keys_and_idempotence_keys_keep_each_capture_event(tmp_path: Path) -> None:
    """Link a repeated observation to distinct captures without overwriting either."""
    engine = _engine(tmp_path)
    try:
        _apply_revision(engine, "upgrade")
        with engine.begin() as connection:
            _insert_capture(connection, capture_id="a" * 32)
            _insert_capture(
                connection,
                capture_id="b" * 32,
                observed_at_utc=datetime(2025, 4, 2, 2, 30),
            )
            _insert_observation(connection)
            _insert_link(
                connection,
                link_id="c" * 64,
                capture_id="a" * 32,
                value_pointer="/data/item/0/net_profit",
                observation_id="5" * 64,
                row_pointer="/data/item/0",
            )
            _insert_link(
                connection,
                link_id="d" * 64,
                capture_id="b" * 32,
                value_pointer="/data/item/0/net_profit",
                observation_id="5" * 64,
                row_pointer="/data/item/0",
            )
            with pytest.raises(IntegrityError):
                _insert_link(
                    connection,
                    link_id="e" * 64,
                    capture_id="a" * 32,
                    value_pointer="/data/item/0/net_profit",
                    observation_id="5" * 64,
                    row_pointer="/data/item/0",
                )
            link_table = sa.Table(LINKS, sa.MetaData(), autoload_with=connection)
            count = connection.scalar(sa.select(sa.func.count()).select_from(link_table))
            assert count == 2
            capture_table = sa.Table(CAPTURES, sa.MetaData(), autoload_with=connection)
            times = (
                connection.execute(
                    sa.select(capture_table.c.observed_at_utc)
                    .select_from(
                        capture_table.join(
                            link_table, link_table.c.capture_id == capture_table.c.capture_id
                        )
                    )
                    .order_by(capture_table.c.capture_id)
                )
                .scalars()
                .all()
            )
            assert len(times) == 2
            assert times[0] != times[1]
    finally:
        engine.dispose()


def test_operator_declaration_and_quarantine_reason_are_constrained(tmp_path: Path) -> None:
    """Prevent operator declarations from being stored as verified authorization."""
    engine = _engine(tmp_path)
    try:
        _apply_revision(engine, "upgrade")
        with engine.begin() as connection:
            with pytest.raises(IntegrityError):
                _insert_capture(connection, unit_mapping_declaration_status="verified")
            _insert_capture(connection)
            with pytest.raises(IntegrityError):
                _insert_quarantine(
                    connection,
                    quarantine_id="f" * 64,
                    capture_id="a" * 32,
                    row_pointer="/data/item/0",
                    disclosure_pointer=None,
                    reason="unclassified",
                )
            _insert_quarantine(
                connection,
                quarantine_id="0" * 64,
                capture_id="a" * 32,
                row_pointer="/data/item/0",
                disclosure_pointer=None,
                reason="disclosure_date_missing",
            )
            with pytest.raises(IntegrityError):
                _insert_quarantine(
                    connection,
                    quarantine_id="1" * 64,
                    capture_id="a" * 32,
                    row_pointer="/data/item/0",
                    disclosure_pointer="/data/item/0/report_date_ms",
                    reason="disclosure_date_invalid",
                )
    finally:
        engine.dispose()


def test_downgrade_fails_closed_for_empty_and_populated_schema(tmp_path: Path) -> None:
    """Keep this evidence schema forward-only even when the tables look empty."""
    engine = _engine(tmp_path)
    try:
        _apply_revision(engine, "upgrade")
        expected_tables = {CAPTURES, OBSERVATIONS, LINKS, QUARANTINE}
        migration = _load_migration()
        with engine.begin() as connection:
            migration_context = MigrationContext.configure(connection)
            with (
                Operations.context(migration_context),
                pytest.raises(RuntimeError, match="forward-only"),
            ):
                migration.downgrade()
        assert expected_tables <= set(inspect(engine).get_table_names())

        with engine.begin() as connection:
            _insert_capture(connection)
            migration_context = MigrationContext.configure(connection)
            with (
                Operations.context(migration_context),
                pytest.raises(RuntimeError, match="forward-only"),
            ):
                migration.downgrade()
        assert expected_tables <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            capture_table = sa.Table(CAPTURES, sa.MetaData(), autoload_with=connection)
            assert connection.scalar(sa.select(sa.func.count()).select_from(capture_table)) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("dialect_name", "version", "is_mariadb", "accepted"),
    [
        ("sqlite", None, False, True),
        ("mysql", (8, 0, 15), False, False),
        ("mysql", (8, 0, 16), False, True),
        ("mysql", (8, 4, 0), False, True),
        ("mysql", (9, 0, 0), False, False),
        ("mysql", (10, 6, 0), True, False),
        ("postgresql", (16, 0), False, False),
    ],
)
def test_migration_support_matrix_is_fail_closed(
    dialect_name: str,
    version: tuple[int, ...] | None,
    is_mariadb: bool,
    accepted: bool,
) -> None:
    """Check dialect gates without connecting to any real database server."""
    migration = _load_migration()
    if accepted:
        migration._require_supported_dialect(dialect_name, version, is_mariadb=is_mariadb)
    else:
        with pytest.raises(RuntimeError):
            migration._require_supported_dialect(dialect_name, version, is_mariadb=is_mariadb)
