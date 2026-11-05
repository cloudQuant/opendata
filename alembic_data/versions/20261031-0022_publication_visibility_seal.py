"""Create post-commit publication visibility seal storage and its monotonic ledger.

Revision ID: 0022_publication_visibility_seal
Revises: 0021_temporal_identity_snapshot
Create Date: 2026-10-31

This revision adds warehouse storage only. It performs no backfill and marks no
historical publication visible: a publication is sealed only by an explicit
post-commit seal request after its fact partition, publication pointer, and
cross-database receipt are all committed. The existing in-transaction
``published_at_us`` column on ``od_publications`` is deliberately left
untouched and remains non-probative; the seal instant and the visibility
sequence stored here are separate, strictly post-commit facts.

The DDL is fixed and declares no values chosen from live inspection, so it
renders offline like revisions up to 0013. Online it verifies the exact 0021
Alembic marker and the ``od_publications`` identity columns before creating
anything, and re-verifies the created shape afterwards. A crash between the two
CREATE TABLE statements is recoverable: a complete existing table is verified
and only the missing table is created, while a table whose shape differs from
this revision is left untouched for offline inspection.

SQLite and MySQL 8.0.16+ are supported, with enforced CHECK constraints and
utf8mb4_0900_bin identity collations on MySQL. This revision does not claim live
MySQL acceptance.

Automatic downgrade is refused. Dropping the seal ledger would let a later
publication reuse an already-issued visibility sequence, which is exactly the
guarantee this storage exists to provide, so removal requires a separately
reviewed destructive migration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    column,
    inspect,
    select,
    table,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine.reflection import Inspector

revision = "0022_publication_visibility_seal"
down_revision = "0021_temporal_identity_snapshot"
branch_labels = None
depends_on = None

_SEALS = "od_publication_visibility_seals"
_STATE = "od_visibility_sequence_state"
_PUBLICATIONS = "od_publications"
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_EXPECTED_MARKER = "0021_temporal_identity_snapshot"
_MYSQL_COLLATION = "utf8mb4_0900_bin"
_MYSQL_MIN_VERSION = (8, 0, 16)
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_MAX_CLOCK_EVIDENCE_BYTES = 4_096

_SEAL_COLUMNS = (
    "publication_id",
    "dataset_id",
    "publication_sequence",
    "record_count",
    "schema_version",
    "producer_identity",
    "payload_sha256",
    "fact_set_sha256",
    "receipt_event_sha256",
    "visibility_sequence",
    "sealed_at_us",
    "clock_evidence_json",
    "seal_version",
)
_STATE_COLUMNS = (
    "state_id",
    "last_visibility_sequence",
    "last_sealed_at_us",
    "seal_count",
    "updated_at_us",
)
_SEAL_CHECKS = {
    "ck_od_visibility_seal_pub_sequence": "publication_sequence > 0",
    "ck_od_visibility_seal_record_count": "record_count >= 0",
    "ck_od_visibility_seal_schema": "length(schema_version) BETWEEN 1 AND 64",
    "ck_od_visibility_seal_producer_len": "length(producer_identity) BETWEEN 1 AND 128",
    "ck_od_visibility_seal_payload_sha": "length(payload_sha256) = 64",
    "ck_od_visibility_seal_fact_sha": "length(fact_set_sha256) = 64",
    "ck_od_visibility_seal_receipt_sha": "length(receipt_event_sha256) = 64",
    "ck_od_visibility_seal_sequence": (f"visibility_sequence BETWEEN 1 AND {_MAX_SEQUENCE}"),
    "ck_od_visibility_seal_sealed_at": f"sealed_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
    "ck_od_visibility_seal_clock_evidence_size": (
        f"length(clock_evidence_json) BETWEEN 2 AND {_MAX_CLOCK_EVIDENCE_BYTES}"
    ),
    "ck_od_visibility_seal_version": "seal_version = 1",
}
_STATE_CHECKS = {
    "ck_od_visibility_state_single_row": "state_id = 1",
    "ck_od_visibility_state_last_sequence": (
        f"last_visibility_sequence BETWEEN 0 AND {_MAX_SEQUENCE}"
    ),
    "ck_od_visibility_state_last_sealed_at": (
        f"last_sealed_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}"
    ),
    "ck_od_visibility_state_seal_count": "seal_count >= 0",
    "ck_od_visibility_state_updated_at": f"updated_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
    "ck_od_visibility_state_count_bounded": "seal_count <= last_visibility_sequence",
}
_SEAL_UNIQUE_INDEXES = {
    "uq_od_visibility_seal_sequence": ("visibility_sequence",),
    "uq_od_visibility_seal_dataset_sequence": ("dataset_id", "publication_sequence"),
}
_SEAL_INDEXES = {
    "ix_od_visibility_seal_dataset_order": ("dataset_id", "visibility_sequence"),
    "ix_od_visibility_seal_sealed_at": ("sealed_at_us",),
}


def _target_mode() -> str:
    """Return ``offline`` for SQL rendering, otherwise validate the live target."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        return "offline"
    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0022 supports SQLite and MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0022 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < _MYSQL_MIN_VERSION
    ):
        raise RuntimeError("revision 0022 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(context: object) -> str:
    """Return the configured warehouse Alembic version table name."""
    value = getattr(context, "version_table", None) or _VERSION_TABLE_FALLBACK
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("revision 0022 requires a valid Alembic version table name")
    return value


def _require_marker(expected: str) -> None:
    """Require one exact Alembic marker before changing the schema."""
    context = op.get_context()
    version_table_name = _version_table_name(context)
    schema = getattr(context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table_name, schema=schema):
        raise RuntimeError("revision 0022 requires the configured Alembic version table")
    columns = inspector.get_columns(version_table_name, schema=schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0022 requires a version table containing only version_num")
    version_type = columns[0].get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or columns[0].get("nullable") is not False
        or columns[0].get("default") is not None
    ):
        raise RuntimeError("revision 0022 requires a bounded, non-null VARCHAR version_num")
    if inspector.get_pk_constraint(version_table_name, schema=schema).get(
        "constrained_columns"
    ) != ["version_num"]:
        raise RuntimeError("revision 0022 requires version_num as the sole version-table key")
    marker_table = table(version_table_name, column("version_num", version_type), schema=schema)
    markers: Sequence[str] = bind.execute(select(marker_table.c.version_num)).scalars().all()
    if markers != [expected]:
        raise RuntimeError(
            f"revision 0022 requires exactly one Alembic marker matching {expected}; "
            "unexpected or partial migration state requires offline verification"
        )


def _require_publication_parent(mode: str) -> None:
    """Require the publication identity columns the seal foreign key depends on."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table(_PUBLICATIONS):
        raise RuntimeError("revision 0022 requires od_publications")
    columns = {item.get("name"): item for item in inspector.get_columns(_PUBLICATIONS)}
    if not {"publication_id", "dataset_id", "sequence", "published_at_us"}.issubset(columns):
        raise RuntimeError("revision 0022 found an incomplete od_publications schema")
    publication_id_type = columns["publication_id"].get("type")
    if (
        not isinstance(publication_id_type, String)
        or publication_id_type.length != 32
        or columns["publication_id"].get("nullable") is not False
        or inspector.get_pk_constraint(_PUBLICATIONS).get("constrained_columns")
        != ["publication_id"]
    ):
        raise RuntimeError("revision 0022 requires publication_id as od_publications primary key")
    if not isinstance(columns["published_at_us"].get("type"), BigInteger):
        raise RuntimeError("revision 0022 requires published_at_us to remain a BIGINT column")
    if mode == "mysql":
        options = inspector.get_table_options(_PUBLICATIONS)
        engine = options.get("mysql_engine") if isinstance(options, dict) else None
        if not isinstance(engine, str) or engine.casefold() != "innodb":
            raise RuntimeError("revision 0022 requires od_publications to use InnoDB")


def _identity(length: int) -> String:
    """Declare an exact, case-sensitive identity column for both supported dialects."""
    return String(length).with_variant(VARCHAR(length, collation=_MYSQL_COLLATION), "mysql")


def _seal_checks() -> list[CheckConstraint]:
    """Return the fixed seal-table CHECK constraints."""
    return [CheckConstraint(expression, name=name) for name, expression in _SEAL_CHECKS.items()]


def _state_checks() -> list[CheckConstraint]:
    """Return the fixed sequence-ledger CHECK constraints."""
    return [CheckConstraint(expression, name=name) for name, expression in _STATE_CHECKS.items()]


def _create_seals() -> None:
    """Create the seal table with its unique visibility-sequence identity."""
    op.create_table(
        _SEALS,
        Column("publication_id", String(32), primary_key=True, nullable=False),
        Column("dataset_id", _identity(128), nullable=False),
        Column("publication_sequence", BigInteger(), nullable=False),
        Column("record_count", BigInteger(), nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("producer_identity", _identity(128), nullable=False),
        Column("payload_sha256", String(64), nullable=False),
        Column("fact_set_sha256", String(64), nullable=False),
        Column("receipt_event_sha256", String(64), nullable=False),
        Column("visibility_sequence", BigInteger(), nullable=False),
        Column("sealed_at_us", BigInteger(), nullable=False),
        Column("clock_evidence_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
        Column("seal_version", Integer(), nullable=False),
        *_seal_checks(),
        ForeignKeyConstraint(
            ["publication_id"],
            ["od_publications.publication_id"],
            name="fk_od_visibility_seal_publication",
            ondelete="RESTRICT",
        ),
        mysql_engine="InnoDB",
    )
    for name, columns in _SEAL_UNIQUE_INDEXES.items():
        op.create_index(name, _SEALS, list(columns), unique=True)
    for name, columns in _SEAL_INDEXES.items():
        op.create_index(name, _SEALS, list(columns))


def _create_state() -> None:
    """Create the single-row persistent visibility sequence ledger."""
    op.create_table(
        _STATE,
        Column("state_id", Integer(), primary_key=True, nullable=False),
        Column("last_visibility_sequence", BigInteger(), nullable=False),
        Column("last_sealed_at_us", BigInteger(), nullable=False),
        Column("seal_count", BigInteger(), nullable=False),
        Column("updated_at_us", BigInteger(), nullable=False),
        *_state_checks(),
        mysql_engine="InnoDB",
    )


def _require_column_set(inspector: Inspector, table_name: str, expected: tuple[str, ...]) -> None:
    """Require the exact column name set of one already created table."""
    names = {str(item.get("name")) for item in inspector.get_columns(table_name)}
    if names != set(expected):
        raise RuntimeError(f"revision 0022 found an unexpected {table_name} column set")


def _require_primary_key(inspector: Inspector, table_name: str, expected: tuple[str, ...]) -> None:
    """Require one exact ordered primary key."""
    if tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ()) != expected:
        raise RuntimeError(f"revision 0022 requires the exact {table_name} primary key")


def _require_checks(inspector: Inspector, table_name: str, expected: dict[str, str]) -> None:
    """Require the exact named CHECK set without comparing dialect-formatted text."""
    names = {str(item.get("name")) for item in inspector.get_check_constraints(table_name)}
    if names != set(expected):
        raise RuntimeError(f"revision 0022 found an unexpected {table_name} CHECK set")


def _require_indexes(inspector: Inspector, table_name: str) -> None:
    """Require the exact seal index set, including uniqueness, on a created table."""
    found: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for item in inspector.get_indexes(table_name):
        name = str(item.get("name"))
        columns = tuple(str(column_name) for column_name in item.get("column_names") or ())
        found[name] = (bool(item.get("unique")), columns)
    expected: dict[str, tuple[bool, tuple[str, ...]]] = {
        **{name: (True, columns) for name, columns in _SEAL_UNIQUE_INDEXES.items()},
        **{name: (False, columns) for name, columns in _SEAL_INDEXES.items()},
    }
    if found != expected:
        raise RuntimeError(f"revision 0022 found an unexpected {table_name} index set")


def _require_existing_shape(mode: str) -> None:
    """Verify each already present table exactly before creating the missing one."""
    inspector = inspect(op.get_bind())
    if inspector.has_table(_SEALS):
        _require_column_set(inspector, _SEALS, _SEAL_COLUMNS)
        _require_primary_key(inspector, _SEALS, ("publication_id",))
        _require_checks(inspector, _SEALS, _SEAL_CHECKS)
        _require_indexes(inspector, _SEALS)
        foreign_keys = inspector.get_foreign_keys(_SEALS)
        if len(foreign_keys) != 1 or foreign_keys[0].get("name") != (
            "fk_od_visibility_seal_publication"
        ):
            raise RuntimeError("revision 0022 requires the exact seal publication foreign key")
        if mode == "mysql":
            options = inspector.get_table_options(_SEALS)
            engine = options.get("mysql_engine") if isinstance(options, dict) else None
            if not isinstance(engine, str) or engine.casefold() != "innodb":
                raise RuntimeError("revision 0022 requires od_publication_visibility_seals InnoDB")
    if inspector.has_table(_STATE):
        _require_column_set(inspector, _STATE, _STATE_COLUMNS)
        _require_primary_key(inspector, _STATE, ("state_id",))
        _require_checks(inspector, _STATE, _STATE_CHECKS)
        if inspector.get_indexes(_STATE) or inspector.get_foreign_keys(_STATE):
            raise RuntimeError("revision 0022 refuses unexpected ledger indexes or foreign keys")


def upgrade() -> None:
    """Create seal storage and its monotonic ledger, recovering a crashed first table."""
    mode = _target_mode()
    if mode == "offline":
        # A fixed declaration needs no live read, so the offline render stays honest.
        _create_seals()
        _create_state()
        return
    _require_marker(down_revision)
    _require_publication_parent(mode)
    inspector = inspect(op.get_bind())
    _require_existing_shape(mode)
    if not inspector.has_table(_SEALS):
        _create_seals()
    if not inspector.has_table(_STATE):
        _create_state()
    _require_existing_shape(mode)


def downgrade() -> None:
    """Refuse rollback because a dropped ledger can re-issue a visibility sequence."""
    raise RuntimeError(
        "revision 0022 downgrade is refused; dropping the seal ledger or its monotonic "
        "visibility sequence can re-issue a sequence value that was already published"
    )


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
