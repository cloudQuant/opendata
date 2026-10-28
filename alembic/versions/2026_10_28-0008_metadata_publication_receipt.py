"""Create the empty control-database publication receipt table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-28

The table can store a receiver's immutable copy of one explicitly supplied
publication event. It does not authenticate the producer, validate a contract,
or mark publication data ready. This revision never backfills old events.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "od_metadata_publication_receipts"
_MAX_EVENT_BYTES = 8_192
_MAX_RECORD_COUNT = 2_147_483_647
_MAX_TIMESTAMP_US = 253_402_300_799_999_999

_BINARY_32 = sa.String(length=32).with_variant(
    mysql.VARCHAR(length=32, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_40 = sa.String(length=40).with_variant(
    mysql.VARCHAR(length=40, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_64 = sa.String(length=64).with_variant(
    mysql.VARCHAR(length=64, collation="utf8mb4_0900_bin"), "mysql"
)
_BINARY_128 = sa.String(length=128).with_variant(
    mysql.VARCHAR(length=128, collation="utf8mb4_0900_bin"), "mysql"
)


def _require_supported_target() -> None:
    """Require SQLite or MySQL 8.0.16+, where the declared checks are enforced."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0008 requires a live SQLite or MySQL connection")

    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0008 supports MySQL 8.0.16+, not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0008 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise RuntimeError("revision 0008 requires MySQL 8.0.16 or later within MySQL 8")


def upgrade() -> None:
    """Create the empty receipt-copy table without populating historical rows."""
    _require_supported_target()
    op.create_table(
        _TABLE,
        sa.Column("producer_identity", _BINARY_128, nullable=False),
        sa.Column("producer_identity_evidence_kind", _BINARY_40, nullable=False),
        sa.Column("publication_id", _BINARY_32, nullable=False),
        sa.Column("dataset_id", _BINARY_128, nullable=False),
        sa.Column("publication_sequence", sa.BigInteger(), nullable=False),
        sa.Column("record_count", sa.BigInteger(), nullable=False),
        sa.Column("payload_sha256", _BINARY_64, nullable=False),
        sa.Column("contract_sha256", _BINARY_64, nullable=True),
        sa.Column("contract_evidence_kind", _BINARY_40, nullable=False),
        sa.Column("provenance_sha256", _BINARY_64, nullable=False),
        sa.Column("schema_version", _BINARY_64, nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column(
            "event_json_utf8",
            sa.LargeBinary(length=_MAX_EVENT_BYTES),
            nullable=False,
        ),
        sa.Column("event_sha256", _BINARY_64, nullable=False),
        sa.Column("published_at_us", sa.BigInteger(), nullable=False),
        sa.Column("received_at_us", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "producer_identity",
            "publication_id",
            name="pk_od_metadata_publication_receipt",
        ),
        sa.UniqueConstraint(
            "producer_identity",
            "dataset_id",
            "publication_sequence",
            name="uq_od_metadata_receipt_source_sequence",
        ),
        sa.CheckConstraint(
            "length(producer_identity) BETWEEN 1 AND 128",
            name="ck_od_meta_receipt_producer_len",
        ),
        sa.CheckConstraint(
            "producer_identity_evidence_kind = 'caller_supplied_external_pin_unverified'",
            name="ck_od_meta_receipt_producer_evidence",
        ),
        sa.CheckConstraint("length(publication_id) = 32", name="ck_od_meta_receipt_pub_id_len"),
        sa.CheckConstraint(
            "length(dataset_id) BETWEEN 1 AND 128",
            name="ck_od_meta_receipt_dataset_len",
        ),
        sa.CheckConstraint("publication_sequence > 0", name="ck_od_meta_receipt_sequence"),
        sa.CheckConstraint(
            f"record_count BETWEEN 0 AND {_MAX_RECORD_COUNT}",
            name="ck_od_meta_receipt_record_count",
        ),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_od_meta_receipt_payload_sha"),
        sa.CheckConstraint(
            "contract_sha256 IS NULL OR length(contract_sha256) = 64",
            name="ck_od_meta_receipt_contract_sha",
        ),
        sa.CheckConstraint(
            "(contract_evidence_kind = 'absent' AND contract_sha256 IS NULL) OR "
            "(contract_evidence_kind = 'caller_supplied_unverified' "
            "AND contract_sha256 IS NOT NULL)",
            name="ck_od_meta_receipt_contract_evidence",
        ),
        sa.CheckConstraint(
            "length(provenance_sha256) = 64",
            name="ck_od_meta_receipt_provenance_sha",
        ),
        sa.CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 64",
            name="ck_od_meta_receipt_schema",
        ),
        sa.CheckConstraint("event_version = 1", name="ck_od_meta_receipt_event_version"),
        sa.CheckConstraint(
            f"length(event_json_utf8) BETWEEN 2 AND {_MAX_EVENT_BYTES}",
            name="ck_od_meta_receipt_event_size",
        ),
        sa.CheckConstraint("length(event_sha256) = 64", name="ck_od_meta_receipt_event_sha"),
        sa.CheckConstraint(
            f"published_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
            name="ck_od_meta_receipt_published_at",
        ),
        sa.CheckConstraint(
            f"received_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
            name="ck_od_meta_receipt_received_at",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    """Drop the receipt table only if no receiver copies have been stored."""
    _require_supported_target()
    bind = op.get_bind()
    receipt_rows = sa.table(_TABLE, sa.column("publication_id", sa.String(length=32)))
    if bind.execute(sa.select(sa.literal(1)).select_from(receipt_rows).limit(1)).first():
        raise RuntimeError("revision 0008 refuses to drop a non-empty publication receipt table")
    op.drop_table(_TABLE)
