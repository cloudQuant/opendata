"""Exercise revision 0019 against a private SQLite warehouse snapshot.

MySQL coverage is limited to static DDL compilation; no live MySQL server
acceptance is performed or claimed by this test module.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateColumn

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_DIRECTORY = REPOSITORY_ROOT / "alembic_data" / "versions"
_MIGRATIONS = {
    "0011": (
        _MIGRATION_DIRECTORY / "20261026-0011_publication_snapshots.py",
        "69a9203b0a34a1da0b71438d3d310f96c70683c594fecf328a85d1c54d4b08f0",
    ),
    "0012": (
        _MIGRATION_DIRECTORY / "20261027-0012_instrument_capture_scope.py",
        "24c55bce92d97aa65ae2afb7b7b516ade21704d1956aa85c50827177aca2b9f2",
    ),
    "0017": (
        _MIGRATION_DIRECTORY / "20261028-0017_sina_capture_discovery_index.py",
        "afac37f3e53af404577a89539fd5292b5c5d2094492179d9c307a6fc2276475f",
    ),
    "0018": (
        _MIGRATION_DIRECTORY / "20261028-0018_publication_outbox.py",
        "f1aaa213125f2b56af06cd3ecd9301a67096648d7aeac32a8896f0f28ff64d7a",
    ),
}
MIGRATION_PATH = _MIGRATION_DIRECTORY / "20261028-0019_publication_market_symbol_scope.py"
FACT_TABLE = "od_published_fact_versions"
SNAPSHOT_TABLE = "od_publication_snapshots"
VERSION_TABLE = "alembic_version_data"


def _load_module(path: Path, module_name: str) -> ModuleType:
    """Load a revision without evaluating the Alembic environment."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_frozen_revision(revision: str) -> ModuleType:
    """Load one prior revision only if its frozen source still matches this fixture."""
    path, expected_sha256 = _MIGRATIONS[revision]
    actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual_sha256 == expected_sha256
    return _load_module(path, f"publication_market_scope_base_{revision}")


def _new_database(tmp_path: Path) -> Engine:
    """Create a private file-backed SQLite database under pytest's temp directory."""
    path = tmp_path / "publication-market-symbol-migration.sqlite"
    return sa.create_engine(f"sqlite:///{path}")


def _run_revision(engine: Engine, migration: ModuleType, operation_name: str) -> None:
    """Run one revision function against this test-owned SQLite connection."""
    with engine.begin() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"version_table": VERSION_TABLE},
        )
        with Operations.context(context):
            getattr(migration, operation_name)()


def _set_version_marker(engine: Engine, value: str) -> None:
    """Set a single exact marker in the temporary test database."""
    with engine.begin() as connection:
        inspector = sa.inspect(connection)
        if not inspector.has_table(VERSION_TABLE):
            connection.execute(
                sa.text(
                    "CREATE TABLE alembic_version_data "
                    "(version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
                )
            )
        else:
            connection.execute(sa.text("DELETE FROM alembic_version_data"))
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:value)"),
            {"value": value},
        )


def _prepare_revision_0018(engine: Engine) -> None:
    """Apply frozen publication revisions through the exact 0018 schema marker."""
    _run_revision(engine, _load_frozen_revision("0011"), "upgrade")
    _run_revision(engine, _load_frozen_revision("0012"), "upgrade")
    _set_version_marker(engine, "0016_legacy_turnover_nullable")
    _run_revision(engine, _load_frozen_revision("0017"), "upgrade")
    _set_version_marker(engine, "0017_sina_capture_discovery_index")
    _run_revision(engine, _load_frozen_revision("0018"), "upgrade")
    _set_version_marker(engine, "0018_publication_outbox")


def _apply_target(engine: Engine, operation_name: str = "upgrade") -> None:
    """Run the new migration against the temporary database."""
    migration = _load_module(MIGRATION_PATH, "publication_market_symbol_scope_0019")
    _run_revision(engine, migration, operation_name)


def _insert_publication(connection: Connection) -> None:
    """Insert the parent required by both immutable publication tables."""
    connection.execute(
        sa.text(
            "INSERT INTO od_publications (publication_id, dataset_id, source_id, sequence, "
            "schema_version, semantic_version, unit, adjustment_basis, quality_json, "
            "provenance_json, watermark_json, idempotency_key, payload_hash, record_count, "
            "published_at_us) VALUES (:publication_id, :dataset_id, :source_id, :sequence, "
            ":schema_version, :semantic_version, :unit, :adjustment_basis, :quality_json, "
            ":provenance_json, :watermark_json, :idempotency_key, :payload_hash, "
            ":record_count, :published_at_us)"
        ),
        {
            "publication_id": "publication-market-scope-001",
            "dataset_id": "test_dataset",
            "source_id": "test-source",
            "sequence": 1,
            "schema_version": "source-native-v1",
            "semantic_version": "source-native-test-v1",
            "unit": "per-field",
            "adjustment_basis": "not_applicable",
            "quality_json": "{}",
            "provenance_json": "{}",
            "watermark_json": None,
            "idempotency_key": "publication-market-scope-fixture",
            "payload_hash": "a" * 64,
            "record_count": 4,
            "published_at_us": 1,
        },
    )


def _insert_fact(
    connection: Connection,
    *,
    business_key: str,
    market: str | None = None,
    symbol: str | None = None,
    instrument_id: str | None = None,
    capture_id: str | None = None,
    include_market_symbol_columns: bool = True,
) -> None:
    """Write one explicit version row without deriving selector values from payloads."""
    if not include_market_symbol_columns and (market is not None or symbol is not None):
        raise ValueError("The pre-0019 fixture cannot store market/symbol selectors.")
    if include_market_symbol_columns:
        statement = sa.text(
            "INSERT INTO od_published_fact_versions (dataset_id, business_key, revision, "
            "publication_id, publication_sequence, business_date, event_at, observed_at, "
            "source_published_at, available_at, record_json, record_hash, is_deleted, "
            "instrument_id, capture_id, market, symbol) VALUES (:dataset_id, :business_key, "
            ":revision, :publication_id, :publication_sequence, :business_date, :event_at, "
            ":observed_at, :source_published_at, :available_at, :record_json, :record_hash, "
            ":is_deleted, :instrument_id, :capture_id, :market, :symbol)"
        )
    else:
        statement = sa.text(
            "INSERT INTO od_published_fact_versions (dataset_id, business_key, revision, "
            "publication_id, publication_sequence, business_date, event_at, observed_at, "
            "source_published_at, available_at, record_json, record_hash, is_deleted, "
            "instrument_id, capture_id) VALUES (:dataset_id, :business_key, :revision, "
            ":publication_id, :publication_sequence, :business_date, :event_at, :observed_at, "
            ":source_published_at, :available_at, :record_json, :record_hash, :is_deleted, "
            ":instrument_id, :capture_id)"
        )
    parameters: dict[str, object] = {
        "dataset_id": "test_dataset",
        "business_key": business_key,
        "revision": 1,
        "publication_id": "publication-market-scope-001",
        "publication_sequence": 1,
        "business_date": None,
        "event_at": None,
        "observed_at": None,
        "source_published_at": None,
        "available_at": None,
        "record_json": '{"value":"immutable-source-value"}',
        "record_hash": "b" * 64,
        "is_deleted": False,
        "instrument_id": instrument_id,
        "capture_id": capture_id,
    }
    if include_market_symbol_columns:
        parameters.update(market=market, symbol=symbol)
    connection.execute(statement, parameters)


def _insert_snapshot(
    connection: Connection,
    *,
    snapshot_id: str,
    market: str | None = None,
    symbol: str | None = None,
    instrument_id: str | None = None,
    capture_id: str | None = None,
    include_market_symbol_columns: bool = True,
) -> None:
    """Write one fixed snapshot with explicit old and new selector pairs."""
    if not include_market_symbol_columns and (market is not None or symbol is not None):
        raise ValueError("The pre-0019 fixture cannot store market/symbol selectors.")
    if include_market_symbol_columns:
        statement = sa.text(
            "INSERT INTO od_publication_snapshots (snapshot_id, dataset_id, schema_version, "
            "publication_id, publication_sequence, start_date, end_date, page_size, "
            "access_scope, selection_sha256, content_hash, record_count, as_of_us, "
            "created_at_us, expires_at_us, instrument_id, capture_id, market, symbol) "
            "VALUES (:snapshot_id, :dataset_id, :schema_version, :publication_id, "
            ":publication_sequence, :start_date, :end_date, :page_size, :access_scope, "
            ":selection_sha256, :content_hash, :record_count, :as_of_us, :created_at_us, "
            ":expires_at_us, :instrument_id, :capture_id, :market, :symbol)"
        )
    else:
        statement = sa.text(
            "INSERT INTO od_publication_snapshots (snapshot_id, dataset_id, schema_version, "
            "publication_id, publication_sequence, start_date, end_date, page_size, "
            "access_scope, selection_sha256, content_hash, record_count, as_of_us, "
            "created_at_us, expires_at_us, instrument_id, capture_id) "
            "VALUES (:snapshot_id, :dataset_id, :schema_version, :publication_id, "
            ":publication_sequence, :start_date, :end_date, :page_size, :access_scope, "
            ":selection_sha256, :content_hash, :record_count, :as_of_us, :created_at_us, "
            ":expires_at_us, :instrument_id, :capture_id)"
        )
    parameters: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "dataset_id": "test_dataset",
        "schema_version": "source-native-v1",
        "publication_id": "publication-market-scope-001",
        "publication_sequence": 1,
        "start_date": None,
        "end_date": None,
        "page_size": 100,
        "access_scope": "test-scope",
        "selection_sha256": "c" * 64,
        "content_hash": "d" * 64,
        "record_count": 1,
        "as_of_us": 1,
        "created_at_us": 1,
        "expires_at_us": 2,
        "instrument_id": instrument_id,
        "capture_id": capture_id,
    }
    if include_market_symbol_columns:
        parameters.update(market=market, symbol=symbol)
    connection.execute(statement, parameters)


def _seed_legacy_rows(engine: Engine) -> None:
    """Create null-selector and old capture-selector history before revision 0019."""
    with engine.begin() as connection:
        _insert_publication(connection)
        _insert_fact(
            connection,
            business_key="legacy-null",
            include_market_symbol_columns=False,
        )
        _insert_fact(
            connection,
            business_key="legacy-capture",
            instrument_id="CN.600519",
            capture_id="c" * 64,
            include_market_symbol_columns=False,
        )
        _insert_snapshot(
            connection,
            snapshot_id="legacy-null-snapshot",
            include_market_symbol_columns=False,
        )
        _insert_snapshot(
            connection,
            snapshot_id="legacy-capture-snapshot",
            instrument_id="CN.600519",
            capture_id="c" * 64,
            include_market_symbol_columns=False,
        )


def _schema_signature(connection: Connection) -> tuple[object, ...]:
    """Capture affected columns, named indexes, and checks before fail-closed calls."""
    inspector = sa.inspect(connection)
    signature: list[object] = []
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = tuple(
            (item["name"], str(item["type"]), item["nullable"])
            for item in inspector.get_columns(table_name)
        )
        indexes = tuple(
            sorted(
                (
                    item["name"],
                    tuple(item["column_names"]),
                    bool(item["unique"]),
                )
                for item in inspector.get_indexes(table_name)
            )
        )
        checks = tuple(
            sorted(
                (item["name"], " ".join(item["sqltext"].split()))
                for item in inspector.get_check_constraints(table_name)
            )
        )
        signature.append((table_name, columns, indexes, checks))
    return tuple(signature)


def _assert_selector_columns(engine: Engine, *, present: bool) -> None:
    """Assert exact paired selector widths on both affected tables."""
    inspector = sa.inspect(engine)
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = {item["name"]: item for item in inspector.get_columns(table_name)}
        if present:
            assert columns["market"]["nullable"] is True
            assert columns["market"]["type"].length == 32
            assert columns["symbol"]["nullable"] is True
            assert columns["symbol"]["type"].length == 128
        else:
            assert "market" not in columns
            assert "symbol" not in columns


def test_check_canonicalization_preserves_internal_parenthesis_semantics() -> None:
    """Accept MySQL's leaf wrappers and reject changed boolean precedence or grouping."""
    migration = _load_module(MIGRATION_PATH, "publication_market_scope_check_canonicalization")
    expected = (
        "(instrument_id IS NULL AND capture_id IS NULL) OR "
        "(instrument_id IS NOT NULL AND capture_id IS NOT NULL)"
    )
    outer_wrapped = f"(({expected}))"
    mysql_reflected = (
        "(((`instrument_id` is null) and (`capture_id` is null)) or "
        "((`instrument_id` is not null) and (`capture_id` is not null)))"
    )
    precedence_equivalent = (
        "instrument_id IS NULL AND capture_id IS NULL OR "
        "instrument_id IS NOT NULL AND capture_id IS NOT NULL"
    )
    sqlite_doublequoted = (
        '"instrument_id" IS NULL AND "capture_id" IS NULL OR '
        '"instrument_id" IS NOT NULL AND "capture_id" IS NOT NULL'
    )
    mysql_doublequoted = '"instrument_id" IS NULL AND "capture_id" IS NULL'
    mysql_quoted_number = 'revision > "0"'
    sqlite_quoted_number = 'revision > "0"'
    unknown_sqlite_quoted_identifier = '"unreviewed_column" IS NULL'
    reverse_precedence = (
        "instrument_id IS NULL OR capture_id IS NULL AND "
        "instrument_id IS NOT NULL OR capture_id IS NOT NULL"
    )
    regrouped = (
        "instrument_id IS NULL AND (capture_id IS NULL OR instrument_id IS NOT NULL) "
        "AND capture_id IS NOT NULL"
    )

    assert migration._canonical_check(outer_wrapped, "sqlite") == migration._canonical_check(
        expected, "sqlite"
    )
    assert migration._canonical_check(mysql_reflected, "mysql") == migration._canonical_check(
        expected, "mysql"
    )
    assert migration._canonical_check(
        precedence_equivalent, "sqlite"
    ) == migration._canonical_check(expected, "sqlite")
    assert migration._canonical_check(sqlite_doublequoted, "sqlite") == migration._canonical_check(
        expected, "sqlite"
    )
    assert migration._canonical_check(reverse_precedence, "sqlite") != migration._canonical_check(
        expected, "sqlite"
    )
    assert migration._canonical_check(regrouped, "sqlite") != migration._canonical_check(
        expected, "sqlite"
    )
    with pytest.raises(ValueError, match="double quotes"):
        migration._canonical_check(mysql_doublequoted, "mysql")
    with pytest.raises(ValueError, match="double quotes"):
        migration._canonical_check(mysql_quoted_number, "mysql")
    with pytest.raises(ValueError, match="quoted CHECK token"):
        migration._canonical_check(sqlite_quoted_number, "sqlite")
    with pytest.raises(ValueError, match="quoted CHECK token"):
        migration._canonical_check(unknown_sqlite_quoted_identifier, "sqlite")
    with pytest.raises(ValueError, match="unsupported CHECK"):
        migration._canonical_check("instrument_id IS NULL XOR capture_id IS NULL", "sqlite")

    class RegroupedCheckInspector:
        """Provide a reflected CHECK whose parentheses change its boolean semantics."""

        @staticmethod
        def get_check_constraints(_table_name: str) -> list[dict[str, str]]:
            return [{"name": "ck_selector_profile", "sqltext": regrouped}]

    with pytest.raises(RuntimeError, match="unexpected definition for ck_selector_profile"):
        migration._require_checks(
            RegroupedCheckInspector(),
            "od_published_fact_versions",
            {"ck_selector_profile": expected},
            "sqlite",
        )


def test_require_checks_rejects_duplicate_and_extra_constraint_rows() -> None:
    """Reject duplicate names and extra CHECK rows before converting to a mapping."""
    migration = _load_module(MIGRATION_PATH, "publication_market_scope_duplicate_checks")
    expected = {
        "ck_selector_revision": "revision > 0",
        "ck_selector_page_size": "page_size > 0",
    }

    class CheckListInspector:
        """Return the exact supplied reflected CHECK rows."""

        def __init__(self, checks: list[dict[str, str]]) -> None:
            self._checks = checks

        def get_check_constraints(self, _table_name: str) -> list[dict[str, str]]:
            return self._checks

    cases = (
        [
            {"name": "ck_selector_revision", "sqltext": expected["ck_selector_revision"]},
            {"name": "ck_selector_revision", "sqltext": expected["ck_selector_revision"]},
        ],
        [
            {"name": "ck_selector_revision", "sqltext": expected["ck_selector_revision"]},
            {"name": "ck_unexpected", "sqltext": expected["ck_selector_revision"]},
        ],
        [{"name": "", "sqltext": expected["ck_selector_revision"]}],
        [{"sqltext": expected["ck_selector_revision"]}],
    )
    for checks in cases:
        with pytest.raises(RuntimeError, match="unexpected od_published_fact_versions CHECK set"):
            migration._require_checks(
                CheckListInspector(checks),
                "od_published_fact_versions",
                expected,
                "sqlite",
            )


def _attempt_bad_fact(
    connection: Connection,
    *,
    key: str,
    selectors: dict[str, str | None],
) -> None:
    """Attempt one invalid fact selector shape inside the caller's savepoint."""
    _insert_fact(
        connection,
        business_key=key,
        market=selectors.get("market"),
        symbol=selectors.get("symbol"),
        instrument_id=selectors.get("instrument_id"),
        capture_id=selectors.get("capture_id"),
    )


def _attempt_bad_snapshot(
    connection: Connection,
    *,
    key: str,
    selectors: dict[str, str | None],
) -> None:
    """Attempt one invalid snapshot selector shape inside the caller's savepoint."""
    _insert_snapshot(
        connection,
        snapshot_id=key,
        market=selectors.get("market"),
        symbol=selectors.get("symbol"),
        instrument_id=selectors.get("instrument_id"),
        capture_id=selectors.get("capture_id"),
    )


def test_upgrade_preserves_old_history_and_enforces_exact_market_symbol_scope(
    tmp_path: Path,
) -> None:
    """Upgrade adds nullable selectors, preserves old profiles, and indexes exact pairs."""
    engine = _new_database(tmp_path)
    try:
        _prepare_revision_0018(engine)
        _seed_legacy_rows(engine)
        with engine.connect() as connection:
            facts_before = tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT dataset_id, business_key, revision, publication_id, "
                        "publication_sequence, record_json, record_hash, is_deleted, "
                        "instrument_id, capture_id FROM od_published_fact_versions "
                        "ORDER BY business_key"
                    )
                )
            )
            snapshots_before = tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT snapshot_id, dataset_id, publication_id, "
                        "publication_sequence, content_hash, instrument_id, capture_id "
                        "FROM od_publication_snapshots ORDER BY snapshot_id"
                    )
                )
            )

        _apply_target(engine)
        _assert_selector_columns(engine, present=True)

        with engine.begin() as connection:
            facts_after = tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT dataset_id, business_key, revision, publication_id, "
                        "publication_sequence, record_json, record_hash, is_deleted, "
                        "instrument_id, capture_id, market, symbol "
                        "FROM od_published_fact_versions ORDER BY business_key"
                    )
                )
            )
            snapshots_after = tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT snapshot_id, dataset_id, publication_id, publication_sequence, "
                        "content_hash, instrument_id, capture_id, market, symbol "
                        "FROM od_publication_snapshots ORDER BY snapshot_id"
                    )
                )
            )
            assert facts_after == tuple((*row, None, None) for row in facts_before)
            assert snapshots_after == tuple((*row, None, None) for row in snapshots_before)

            _insert_fact(
                connection,
                business_key="new-market-symbol",
                market="SZ",
                symbol="000001",
            )
            _insert_snapshot(
                connection,
                snapshot_id="new-market-symbol-snapshot",
                market="SZ",
                symbol="000001",
            )

            invalid_shapes = (
                {"market": "SH", "symbol": None},
                {"market": None, "symbol": "600519"},
                {
                    "market": "SH",
                    "symbol": "600519",
                    "instrument_id": "CN.600519",
                    "capture_id": "e" * 64,
                },
                {"instrument_id": "CN.600519", "capture_id": None},
            )
            for index, selectors in enumerate(invalid_shapes):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _attempt_bad_fact(
                        connection,
                        key=f"invalid-fact-{index}",
                        selectors=selectors,
                    )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _attempt_bad_snapshot(
                        connection,
                        key=f"invalid-snapshot-{index}",
                        selectors=selectors,
                    )

            for business_key, market, symbol in (
                ("case-upper", "SH", "600519"),
                ("case-lower", "sh", "600519"),
                ("symbol-tail-space", "SH", "600519 "),
            ):
                _insert_fact(
                    connection,
                    business_key=business_key,
                    market=market,
                    symbol=symbol,
                )
            for snapshot_id, market, symbol in (
                ("case-upper-snapshot", "SH", "600519"),
                ("case-lower-snapshot", "sh", "600519"),
                ("symbol-tail-space-snapshot", "SH", "600519 "),
            ):
                _insert_snapshot(
                    connection,
                    snapshot_id=snapshot_id,
                    market=market,
                    symbol=symbol,
                )

            for market, symbol, expected in (
                ("SH", "600519", "case-upper"),
                ("sh", "600519", "case-lower"),
                ("SH", "600519 ", "symbol-tail-space"),
            ):
                selected = (
                    connection.execute(
                        sa.text(
                            "SELECT business_key FROM od_published_fact_versions "
                            "WHERE dataset_id = :dataset_id AND market = :market "
                            "AND symbol = :symbol"
                        ),
                        {"dataset_id": "test_dataset", "market": market, "symbol": symbol},
                    )
                    .scalars()
                    .all()
                )
                assert selected == [expected]
                selected_snapshots = (
                    connection.execute(
                        sa.text(
                            "SELECT snapshot_id FROM od_publication_snapshots "
                            "WHERE dataset_id = :dataset_id AND market = :market "
                            "AND symbol = :symbol"
                        ),
                        {"dataset_id": "test_dataset", "market": market, "symbol": symbol},
                    )
                    .scalars()
                    .all()
                )
                assert selected_snapshots == [f"{expected}-snapshot"]

            fact_checks = {
                item["name"] for item in sa.inspect(connection).get_check_constraints(FACT_TABLE)
            }
            snapshot_checks = {
                item["name"]
                for item in sa.inspect(connection).get_check_constraints(SNAPSHOT_TABLE)
            }
            assert {
                "ck_od_fact_market_symbol_pair",
                "ck_od_fact_selector_profile",
                "ck_od_fact_capture_pair",
            } <= fact_checks
            assert {
                "ck_od_snapshot_market_symbol_pair",
                "ck_od_snapshot_selector_profile",
                "ck_od_snapshot_capture_pair",
            } <= snapshot_checks

            indexes = {
                item["name"]: tuple(item["column_names"])
                for item in sa.inspect(connection).get_indexes(FACT_TABLE)
            }
            assert indexes["ix_od_fact_market_symbol_read"] == (
                "dataset_id",
                "market",
                "symbol",
                "publication_sequence",
            )
            parts = connection.execute(
                sa.text(
                    'SELECT name, coll, "key" FROM pragma_index_xinfo '
                    "('ix_od_fact_market_symbol_read') ORDER BY seqno"
                )
            ).all()
            key_parts = [row for row in parts if row[2] == 1]
            assert tuple(row[0] for row in key_parts) == (
                "dataset_id",
                "market",
                "symbol",
                "publication_sequence",
            )
            assert all(str(row[1]).casefold() == "binary" for row in key_parts)

    finally:
        engine.dispose()


@pytest.mark.parametrize("scoped_table", [FACT_TABLE, SNAPSHOT_TABLE])
def test_downgrade_is_disabled_and_preserves_market_history_before_any_ddl(
    tmp_path: Path,
    scoped_table: str,
) -> None:
    """Refuse automatic rollback even when data exists and preserve rows and schema."""
    engine = _new_database(tmp_path)
    try:
        _prepare_revision_0018(engine)
        _seed_legacy_rows(engine)
        _apply_target(engine)
        _set_version_marker(engine, "0019_publication_market_symbol_scope")
        with engine.begin() as connection:
            if scoped_table == FACT_TABLE:
                _insert_fact(
                    connection,
                    business_key="market-scope-to-preserve",
                    market="SH",
                    symbol="600519",
                )
            else:
                _insert_snapshot(
                    connection,
                    snapshot_id="market-snapshot-to-preserve",
                    market="SH",
                    symbol="600519",
                )
        with engine.connect() as connection:
            before = _schema_signature(connection)

        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """Record SQL sent during the deliberately rejected downgrade."""
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with pytest.raises(RuntimeError, match="automatic downgrade is disabled"):
                _apply_target(engine, "downgrade")
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        assert not any(
            re.match(r"^\s*(?:CREATE|ALTER|DROP)\b", statement, re.IGNORECASE)
            for statement in statements
        )
        assert not any(
            re.search(rf"\bFROM\s+{table_name}\b", statement, re.IGNORECASE)
            for statement in statements
            for table_name in (FACT_TABLE, SNAPSHOT_TABLE)
        )
        with engine.connect() as connection:
            assert _schema_signature(connection) == before
            if scoped_table == FACT_TABLE:
                row = connection.execute(
                    sa.text(
                        "SELECT market, symbol FROM od_published_fact_versions "
                        "WHERE business_key = 'market-scope-to-preserve'"
                    )
                ).one()
            else:
                row = connection.execute(
                    sa.text(
                        "SELECT market, symbol FROM od_publication_snapshots "
                        "WHERE snapshot_id = 'market-snapshot-to-preserve'"
                    )
                ).one()
            assert tuple(row) == ("SH", "600519")
    finally:
        engine.dispose()


def test_downgrade_is_disabled_when_new_selectors_are_empty(
    tmp_path: Path,
) -> None:
    """Reject rollback without selector values, before scanning history or changing schema."""
    engine = _new_database(tmp_path)
    try:
        _prepare_revision_0018(engine)
        _seed_legacy_rows(engine)
        _apply_target(engine)
        _set_version_marker(engine, "0019_publication_market_symbol_scope")
        with engine.connect() as connection:
            before = _schema_signature(connection)

        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """Record SQL sent while automatic downgrade is rejected."""
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with pytest.raises(RuntimeError, match="automatic downgrade is disabled"):
                _apply_target(engine, "downgrade")
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        assert not any(
            re.match(r"^\s*(?:CREATE|ALTER|DROP)\b", statement, re.IGNORECASE)
            for statement in statements
        )
        assert not any(
            re.search(rf"\bFROM\s+{table_name}\b", statement, re.IGNORECASE)
            for statement in statements
            for table_name in (FACT_TABLE, SNAPSHOT_TABLE)
        )
        with engine.connect() as connection:
            assert _schema_signature(connection) == before
            _assert_selector_columns(engine, present=True)
            fact = connection.execute(
                sa.text(
                    "SELECT business_key, record_json, instrument_id, capture_id, market, symbol "
                    "FROM od_published_fact_versions WHERE business_key = 'legacy-capture'"
                )
            ).one()
            snapshot = connection.execute(
                sa.text(
                    "SELECT snapshot_id, instrument_id, capture_id, market, symbol FROM "
                    "od_publication_snapshots WHERE snapshot_id = 'legacy-capture-snapshot'"
                )
            ).one()
            assert tuple(fact) == (
                "legacy-capture",
                '{"value":"immutable-source-value"}',
                "CN.600519",
                "c" * 64,
                None,
                None,
            )
            assert tuple(snapshot) == (
                "legacy-capture-snapshot",
                "CN.600519",
                "c" * 64,
                None,
                None,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("failure", ["wrong_marker", "partial_column"])
def test_upgrade_fails_closed_without_rewriting_unknown_schema(
    tmp_path: Path,
    failure: str,
) -> None:
    """Reject wrong revision state or partial DDL before touching warehouse history."""
    engine = _new_database(tmp_path)
    try:
        _prepare_revision_0018(engine)
        _seed_legacy_rows(engine)
        if failure == "wrong_marker":
            _set_version_marker(engine, "0017_sina_capture_discovery_index")
        else:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "ALTER TABLE od_published_fact_versions ADD COLUMN market VARCHAR(32) NULL"
                    )
                )
        with engine.connect() as connection:
            before = _schema_signature(connection)

        with pytest.raises(RuntimeError):
            _apply_target(engine)

        with engine.connect() as connection:
            assert _schema_signature(connection) == before
            assert connection.execute(
                sa.text(
                    "SELECT business_key, record_json FROM od_published_fact_versions "
                    "WHERE business_key = 'legacy-null'"
                )
            ).one() == ("legacy-null", '{"value":"immutable-source-value"}')
    finally:
        engine.dispose()


def test_mysql_selector_columns_compile_with_the_explicit_no_pad_binary_collation() -> None:
    """Keep MySQL selector DDL independent of a server or the table default collation."""
    migration = _load_module(MIGRATION_PATH, "publication_market_symbol_mysql_ddl_0019")
    market = sa.Column("market", migration._selector_type(32), nullable=True)
    symbol = sa.Column("symbol", migration._selector_type(128), nullable=True)
    market_ddl = str(CreateColumn(market).compile(dialect=mysql.dialect()))
    symbol_ddl = str(CreateColumn(symbol).compile(dialect=mysql.dialect()))
    market_sqlite_ddl = str(CreateColumn(market).compile(dialect=sqlite.dialect()))
    symbol_sqlite_ddl = str(CreateColumn(symbol).compile(dialect=sqlite.dialect()))
    assert "VARCHAR(32) COLLATE utf8mb4_0900_bin" in market_ddl
    assert "VARCHAR(128) COLLATE utf8mb4_0900_bin" in symbol_ddl
    assert "NOT NULL" not in market_ddl
    assert "NOT NULL" not in symbol_ddl
    assert re.search(r'COLLATE "?BINARY"?', market_sqlite_ddl)
    assert re.search(r'COLLATE "?BINARY"?', symbol_sqlite_ddl)


def test_record_json_type_matches_0011_mysql_mediumtext_capacity() -> None:
    """Accept 0011's MySQL MEDIUMTEXT variant while rejecting capacity drift."""
    migration = _load_module(MIGRATION_PATH, "publication_market_symbol_mysql_record_json_0019")

    assert migration._matches_column_type(mysql.MEDIUMTEXT(), sa.Text, "record_json", "mysql")
    assert not migration._matches_column_type(mysql.TEXT(), sa.Text, "record_json", "mysql")
    assert not migration._matches_column_type(mysql.LONGTEXT(), sa.Text, "record_json", "mysql")
    assert migration._matches_column_type(sa.Text(), sa.Text, "record_json", "sqlite")


def test_mysql_publication_fk_support_index_is_expected_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept the MySQL FK support index only with its exact reflected definition."""
    migration = _load_module(MIGRATION_PATH, "publication_market_scope_mysql_fk_index_0019")

    class IndexInspector:
        """Return controlled SQLAlchemy-style index reflection rows."""

        def __init__(self, indexes: list[dict[str, object]]) -> None:
            self.indexes = indexes

        def get_indexes(self, _table_name: str) -> list[dict[str, object]]:
            return self.indexes

    def reflected_index(name: str, columns: tuple[str, ...]) -> dict[str, object]:
        return {
            "name": name,
            "column_names": list(columns),
            "unique": False,
        }

    def statistics_rows(
        name: str,
        columns: tuple[str, ...],
        overrides: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], ...]:
        """Simulate MySQL STATISTICS rows for one index, including physical attributes."""
        rows: list[dict[str, object]] = []
        for sequence, column_name in enumerate(columns, start=1):
            row: dict[str, object] = {
                "INDEX_NAME": name,
                "NON_UNIQUE": 1,
                "SEQ_IN_INDEX": sequence,
                "COLUMN_NAME": column_name,
                "SUB_PART": None,
                "INDEX_TYPE": "BTREE",
                "IS_VISIBLE": "YES",
            }
            if overrides is not None:
                row.update(overrides)
            rows.append(row)
        return tuple(rows)

    expected = {"ix_test_fact": ("dataset_id", "business_key")}
    indexes = [
        reflected_index("ix_test_fact", expected["ix_test_fact"]),
        reflected_index("publication_id", ("publication_id",)),
    ]
    statistics = {
        "ix_test_fact": statistics_rows("ix_test_fact", expected["ix_test_fact"]),
        "publication_id": statistics_rows("publication_id", ("publication_id",)),
    }
    queried_indexes: list[str] = []

    def fake_mysql_index_rows(_table_name: str, index_name: str) -> tuple[dict[str, object], ...]:
        queried_indexes.append(index_name)
        return statistics.get(index_name, ())

    monkeypatch.setattr(migration, "_mysql_index_rows", fake_mysql_index_rows)
    migration._require_indexes(IndexInspector(indexes), FACT_TABLE, expected, "mysql")
    assert queried_indexes == ["ix_test_fact", "publication_id"]

    with pytest.raises(RuntimeError, match="unexpected od_published_fact_versions index set"):
        migration._require_indexes(
            IndexInspector([*indexes, reflected_index("ix_unexpected", ("business_key",))]),
            FACT_TABLE,
            expected,
            "mysql",
        )

    malformed_reflection = [
        indexes[0],
        reflected_index("publication_id", ("publication_id", "dataset_id")),
    ]
    with pytest.raises(RuntimeError, match="unexpected definition for publication_id"):
        migration._require_indexes(
            IndexInspector(malformed_reflection), FACT_TABLE, expected, "mysql"
        )

    for invalid_attributes in (
        {"NON_UNIQUE": 0},
        {"SUB_PART": 8},
        {"INDEX_TYPE": "HASH"},
        {"IS_VISIBLE": "NO"},
    ):
        statistics["publication_id"] = statistics_rows(
            "publication_id", ("publication_id",), invalid_attributes
        )
        with pytest.raises(RuntimeError, match="reviewed MySQL publication_id index"):
            migration._require_indexes(IndexInspector(indexes), FACT_TABLE, expected, "mysql")


def test_index_columns_accepts_omitted_options_but_rejects_invalid_options() -> None:
    """Treat the MySQL dialect's omitted options as empty without accepting drift."""
    migration = _load_module(MIGRATION_PATH, "publication_market_scope_index_options_0019")
    ordinary_index: dict[str, object] = {
        "name": "ix_test_fact",
        "column_names": ["dataset_id", "business_key"],
        "unique": False,
    }
    expected_columns = ("dataset_id", "business_key")

    assert migration._index_columns(ordinary_index) == expected_columns
    assert migration._index_columns({**ordinary_index, "dialect_options": {}}) == expected_columns

    invalid_options: tuple[object, ...] = (
        None,
        [],
        "invalid-options",
        {"unknown_option": "value"},
        {"mysql_prefix": "FULLTEXT"},
        {"mysql_with_parser": "ngram"},
        {"mysql_length": {"business_key": 8}},
        {"sqlite_where": "business_key IS NOT NULL"},
    )
    for options in invalid_options:
        assert migration._index_columns({**ordinary_index, "dialect_options": options}) is None


def test_sqlite_index_expectation_does_not_include_mysql_fk_support_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep SQLite's exact index inventory independent of MySQL FK behavior."""
    migration = _load_module(MIGRATION_PATH, "publication_market_scope_sqlite_fk_index_0019")

    class IndexInspector:
        """Return controlled SQLite-style index reflection rows."""

        def __init__(self, indexes: list[dict[str, object]]) -> None:
            self.indexes = indexes

        def get_indexes(self, _table_name: str) -> list[dict[str, object]]:
            return self.indexes

    expected = {"ix_test_fact": ("dataset_id", "business_key")}
    ordinary_index = {
        "name": "ix_test_fact",
        "column_names": ["dataset_id", "business_key"],
        "unique": False,
        "dialect_options": {},
    }
    checked_indexes: list[str] = []
    monkeypatch.setattr(
        migration,
        "_require_sqlite_index",
        lambda _inspector, _table_name, name, _columns: checked_indexes.append(name),
    )

    migration._require_indexes(IndexInspector([ordinary_index]), FACT_TABLE, expected, "sqlite")
    assert checked_indexes == ["ix_test_fact"]

    publication_fk_index = {
        "name": "publication_id",
        "column_names": ["publication_id"],
        "unique": False,
        "dialect_options": {},
    }
    with pytest.raises(RuntimeError, match="unexpected od_published_fact_versions index set"):
        migration._require_indexes(
            IndexInspector([ordinary_index, publication_fk_index]),
            FACT_TABLE,
            expected,
            "sqlite",
        )
