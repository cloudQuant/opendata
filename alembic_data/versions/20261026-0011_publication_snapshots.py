"""Add immutable publication revisions and scope-bound read snapshots.

Revision ID: 0011_publication_snapshots
Revises: 0010_run_ledger
Create Date: 2026-10-26

This revision deliberately freezes its table declarations instead of importing
mutable runtime metadata. MySQL requires version 8 for the exact, NO PAD key
collation; MariaDB compatibility is not claimed by this revision.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0011_publication_snapshots"
down_revision = "0010_run_ledger"
branch_labels = None
depends_on = None


def _key_type(length: int) -> String:
    """Use exact, case-sensitive identities on MySQL 8 and other backends."""
    return String(length).with_variant(mysql.VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _json_text() -> Text:
    """Keep bounded UTF-8 JSON values larger than MySQL's 64 KiB TEXT limit."""
    return Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def upgrade() -> None:
    """Create publication heads, append-only versions, and fixed snapshots."""
    op.create_table(
        "od_publication_heads",
        Column("dataset_id", _key_type(128), primary_key=True),
        Column("latest_sequence", BigInteger, nullable=False),
        Column("latest_publication_id", String(32), nullable=True),
        Column("watermark_json", _json_text(), nullable=True),
        Column("updated_at_us", BigInteger, nullable=False),
        CheckConstraint("latest_sequence >= 0", name="ck_od_publication_head_sequence"),
    )
    op.create_table(
        "od_publications",
        Column("publication_id", String(32), primary_key=True),
        Column("dataset_id", _key_type(128), nullable=False),
        Column("source_id", _key_type(128), nullable=False),
        Column("sequence", BigInteger, nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("semantic_version", String(64), nullable=False),
        Column("unit", String(128), nullable=False),
        Column("adjustment_basis", String(128), nullable=False),
        Column("quality_json", _json_text(), nullable=False),
        Column("provenance_json", _json_text(), nullable=False),
        Column("watermark_json", _json_text(), nullable=True),
        Column("idempotency_key", _key_type(256), nullable=False),
        Column("payload_hash", String(64), nullable=False),
        Column("record_count", Integer, nullable=False),
        Column("published_at_us", BigInteger, nullable=False),
        CheckConstraint("sequence > 0", name="ck_od_publication_sequence"),
        CheckConstraint("record_count >= 0", name="ck_od_publication_record_count"),
    )
    op.create_index(
        "uq_od_publication_dataset_sequence",
        "od_publications",
        ["dataset_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "uq_od_publication_dataset_idempotency",
        "od_publications",
        ["dataset_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_od_publication_dataset_published",
        "od_publications",
        ["dataset_id", "published_at_us"],
    )
    op.create_table(
        "od_published_fact_versions",
        Column("dataset_id", _key_type(128), primary_key=True),
        Column("business_key", _key_type(512), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column(
            "publication_id",
            String(32),
            ForeignKey("od_publications.publication_id"),
            nullable=False,
        ),
        Column("publication_sequence", BigInteger, nullable=False),
        Column("business_date", Date, nullable=True),
        Column("event_at", String(40), nullable=True),
        Column("observed_at", String(40), nullable=True),
        Column("source_published_at", String(40), nullable=True),
        Column("available_at", String(40), nullable=True),
        Column("record_json", _json_text(), nullable=False),
        Column("record_hash", String(64), nullable=False),
        Column("is_deleted", Boolean, nullable=False),
        CheckConstraint("revision > 0", name="ck_od_published_fact_revision"),
    )
    op.create_index(
        "ix_od_published_fact_asof",
        "od_published_fact_versions",
        ["dataset_id", "business_key", "publication_sequence", "revision"],
    )
    op.create_index(
        "ix_od_published_fact_date",
        "od_published_fact_versions",
        ["dataset_id", "business_date", "business_key"],
    )
    op.create_table(
        "od_publication_snapshots",
        Column("snapshot_id", String(32), primary_key=True),
        Column("dataset_id", _key_type(128), nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column(
            "publication_id",
            String(32),
            ForeignKey("od_publications.publication_id"),
            nullable=False,
        ),
        Column("publication_sequence", BigInteger, nullable=False),
        Column("start_date", Date, nullable=True),
        Column("end_date", Date, nullable=True),
        Column("page_size", Integer, nullable=False),
        Column("access_scope", _key_type(128), nullable=False),
        Column("selection_sha256", String(64), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("record_count", BigInteger, nullable=False),
        Column("as_of_us", BigInteger, nullable=False),
        Column("created_at_us", BigInteger, nullable=False),
        Column("expires_at_us", BigInteger, nullable=False),
        CheckConstraint("page_size > 0", name="ck_od_snapshot_page_size"),
        CheckConstraint("record_count >= 0", name="ck_od_snapshot_record_count"),
    )
    op.create_index("ix_od_snapshot_expiry", "od_publication_snapshots", ["expires_at_us"])


def downgrade() -> None:
    """Drop only this revision's read snapshots, versions, and publication heads."""
    op.drop_table("od_publication_snapshots")
    op.drop_table("od_published_fact_versions")
    op.drop_table("od_publications")
    op.drop_table("od_publication_heads")
