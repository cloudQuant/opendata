"""Add the transactional publication outbox table.

Revision ID: 0018_publication_outbox
Revises: 0017_sina_capture_discovery_index
Create Date: 2026-10-28

This revision is storage-only for historical rows: it performs no backfill.
Runtime insertion is wired to the new-publication branch in the same publisher
transaction. It does not add a relay, cross-database receipt, retry lease, or
seal protocol.
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
    UniqueConstraint,
    column,
    func,
    inspect,
    select,
    table,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

from alembic import op

if TYPE_CHECKING:
    from sqlalchemy.engine.reflection import Inspector

revision = "0018_publication_outbox"
down_revision = "0017_sina_capture_discovery_index"
branch_labels = None
depends_on = None

_OUTBOX = "od_publication_outbox"
_PUBLICATION_HEADS = "od_publication_heads"
_PUBLICATIONS = "od_publications"
_FACT_VERSIONS = "od_published_fact_versions"
_TRANSACTIONAL_PARENT_TABLES = (_PUBLICATION_HEADS, _PUBLICATIONS, _FACT_VERSIONS)
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_MAX_EVENT_BYTES = 8_192


def _target_mode() -> str:
    """Require an online SQLite or MySQL 8.0.16+ migration target."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0018 requires a live connection for schema checks")
    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0018 supports MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0018 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise RuntimeError("revision 0018 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(context: object) -> str:
    """Return the configured Alembic version table name."""
    value = getattr(context, "version_table", None) or _VERSION_TABLE_FALLBACK
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("revision 0018 requires a valid Alembic version table name")
    return value


def _require_marker(expected: str, mode: str) -> None:
    """Require one exact Alembic marker before changing the schema."""
    migration_context = op.get_context()
    version_table = _version_table_name(migration_context)
    schema = getattr(migration_context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table, schema=schema):
        raise RuntimeError("revision 0018 requires the configured Alembic version table")
    columns = inspector.get_columns(version_table, schema=schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0018 requires a version table containing only version_num")
    version_type = columns[0].get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or columns[0].get("nullable") is not False
        or columns[0].get("default") is not None
    ):
        raise RuntimeError("revision 0018 requires a bounded, non-null VARCHAR version_num")
    if mode == "mysql" and version_type.length < max(len(expected), len(revision)):
        raise RuntimeError("revision 0018 requires version_num wide enough for both revision IDs")
    if inspector.get_pk_constraint(version_table, schema=schema).get("constrained_columns") != [
        "version_num"
    ]:
        raise RuntimeError("revision 0018 requires version_num as the sole version-table key")
    marker_table = table(
        version_table,
        column("version_num", version_type),
        schema=schema,
    )
    markers = bind.execute(select(marker_table.c.version_num)).scalars().all()
    if markers != [expected]:
        raise RuntimeError(
            f"revision 0018 requires exactly one Alembic marker matching {expected}; "
            "unexpected or partial migration state requires offline verification"
        )


def _require_innodb_tables(inspector: Inspector, table_names: tuple[str, ...]) -> None:
    """Fail closed unless every existing publication transaction table is InnoDB."""
    for table_name in table_names:
        if not inspector.has_table(table_name):
            raise RuntimeError(f"revision 0018 requires {table_name} to exist")
        options = inspector.get_table_options(table_name)
        engine = options.get("mysql_engine") if isinstance(options, dict) else None
        if not isinstance(engine, str) or engine.casefold() != "innodb":
            raise RuntimeError(f"revision 0018 requires {table_name} to use InnoDB")


def _require_publication_parent(mode: str) -> None:
    """Require publication FK fields and transactional parent engines on MySQL."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_PUBLICATIONS):
        raise RuntimeError("revision 0018 requires od_publications")
    columns = {item.get("name"): item for item in inspector.get_columns(_PUBLICATIONS)}
    expected_names = {
        "publication_id",
        "dataset_id",
        "sequence",
        "schema_version",
        "provenance_json",
        "payload_hash",
        "record_count",
        "published_at_us",
    }
    if not expected_names.issubset(columns):
        raise RuntimeError("revision 0018 found an incomplete od_publications schema")
    publication_id = columns["publication_id"]
    if (
        not isinstance(publication_id.get("type"), String)
        or publication_id["type"].length != 32
        or publication_id.get("nullable") is not False
        or inspector.get_pk_constraint(_PUBLICATIONS).get("constrained_columns")
        != ["publication_id"]
    ):
        raise RuntimeError("revision 0018 requires publication_id as od_publications primary key")
    dataset = columns["dataset_id"].get("type")
    sequence = columns["sequence"].get("type")
    schema_version = columns["schema_version"].get("type")
    provenance = columns["provenance_json"].get("type")
    payload_hash = columns["payload_hash"].get("type")
    count = columns["record_count"].get("type")
    published_at = columns["published_at_us"].get("type")
    if (
        not isinstance(dataset, String)
        or dataset.length != 128
        or not isinstance(sequence, BigInteger)
        or not isinstance(schema_version, String)
        or schema_version.length != 64
        or not isinstance(provenance, (String, Text))
        or not isinstance(payload_hash, String)
        or payload_hash.length != 64
        or not isinstance(count, Integer)
        or not isinstance(published_at, BigInteger)
    ):
        raise RuntimeError("revision 0018 found unsupported od_publications column types")
    if mode == "mysql":
        _require_innodb_tables(inspector, _TRANSACTIONAL_PARENT_TABLES)


def _outbox_table_columns() -> list[Column]:
    """Return the fixed outbox table columns used by this revision."""
    return [
        Column("publication_id", String(32), primary_key=True, nullable=False),
        Column(
            "dataset_id",
            String(128).with_variant(VARCHAR(128, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=False,
        ),
        Column("publication_sequence", BigInteger(), nullable=False),
        Column("record_count", BigInteger(), nullable=False),
        Column("payload_sha256", String(64), nullable=False),
        Column("contract_sha256", String(64), nullable=True),
        Column("contract_evidence_kind", String(32), nullable=False),
        Column("provenance_sha256", String(64), nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("event_version", Integer(), nullable=False),
        Column("event_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
        Column("event_sha256", String(64), nullable=False),
        Column("published_at_us", BigInteger(), nullable=False),
    ]


def _require_outbox_absent() -> None:
    """Reject an existing table instead of adopting an unknown partial state."""
    if inspect(op.get_bind()).has_table(_OUTBOX):
        raise RuntimeError("revision 0018 found od_publication_outbox already present")


def _require_outbox_present() -> None:
    """Require the outbox table and its reviewed key columns before downgrade."""
    inspector = inspect(op.get_bind())
    if not inspector.has_table(_OUTBOX):
        raise RuntimeError("revision 0018 downgrade requires od_publication_outbox")
    columns = {item.get("name") for item in inspector.get_columns(_OUTBOX)}
    if columns != {
        "publication_id",
        "dataset_id",
        "publication_sequence",
        "record_count",
        "payload_sha256",
        "contract_sha256",
        "contract_evidence_kind",
        "provenance_sha256",
        "schema_version",
        "event_version",
        "event_json",
        "event_sha256",
        "published_at_us",
    }:
        raise RuntimeError("revision 0018 downgrade found an unexpected outbox table shape")
    if inspector.get_pk_constraint(_OUTBOX).get("constrained_columns") != ["publication_id"]:
        raise RuntimeError("revision 0018 downgrade requires publication_id as outbox primary key")


def upgrade() -> None:
    """Create the empty outbox table with immutable publication identity keys."""
    mode = _target_mode()
    _require_marker(down_revision, mode)
    _require_publication_parent(mode)
    _require_outbox_absent()
    op.create_table(
        _OUTBOX,
        *_outbox_table_columns(),
        CheckConstraint("publication_sequence > 0", name="ck_od_pub_outbox_sequence"),
        CheckConstraint("record_count >= 0", name="ck_od_pub_outbox_record_count"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_od_pub_outbox_payload_sha"),
        CheckConstraint(
            "contract_sha256 IS NULL OR length(contract_sha256) = 64",
            name="ck_od_pub_outbox_contract_sha",
        ),
        CheckConstraint(
            "(contract_evidence_kind = 'absent' AND contract_sha256 IS NULL) OR "
            "(contract_evidence_kind = 'caller_supplied_unverified' "
            "AND contract_sha256 IS NOT NULL)",
            name="ck_od_pub_outbox_contract_evidence",
        ),
        CheckConstraint(
            "length(provenance_sha256) = 64",
            name="ck_od_pub_outbox_provenance_sha",
        ),
        CheckConstraint("length(schema_version) > 0", name="ck_od_pub_outbox_schema"),
        CheckConstraint("event_version = 1", name="ck_od_pub_outbox_event_version"),
        CheckConstraint(
            f"length(event_json) BETWEEN 2 AND {_MAX_EVENT_BYTES}",
            name="ck_od_pub_outbox_event_size",
        ),
        CheckConstraint("length(event_sha256) = 64", name="ck_od_pub_outbox_event_sha"),
        CheckConstraint("published_at_us >= 0", name="ck_od_pub_outbox_published_at"),
        UniqueConstraint(
            "dataset_id",
            "publication_sequence",
            name="uq_od_pub_outbox_dataset_sequence",
        ),
        ForeignKeyConstraint(
            ["publication_id"],
            ["od_publications.publication_id"],
            name="fk_od_pub_outbox_publication",
            ondelete="RESTRICT",
        ),
        mysql_engine="InnoDB",
    )


def downgrade() -> None:
    """Drop only an empty outbox table; never discard queued publication events."""
    mode = _target_mode()
    _require_marker(revision, mode)
    _require_outbox_present()
    outbox_table = table(_OUTBOX)
    count = op.get_bind().execute(select(func.count()).select_from(outbox_table)).scalar_one()
    if type(count) is not int or count != 0:
        raise RuntimeError("revision 0018 downgrade refuses to discard publication outbox events")
    op.drop_table(_OUTBOX)
