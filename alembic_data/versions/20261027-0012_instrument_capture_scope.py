"""Add explicit instrument/capture partitions to fixed publication snapshots.

Revision ID: 0012_instrument_capture_scope
Revises: 0011_publication_snapshots
Create Date: 2026-10-27

Legacy records retain NULL selectors. No historical payload is interpreted or
backfilled. A downgrade with scoped facts or snapshots is rejected before DDL:
removing their selectors could otherwise broaden an existing fixed snapshot.
MySQL requires version 8.0.16 or later within major version 8 for enforced
CHECK constraints and the exact NO PAD collation; MariaDB is not covered.
"""

from sqlalchemy import Column, String, column, or_, select, table
from sqlalchemy.dialects.mysql import VARCHAR

from alembic import op

revision = "0012_instrument_capture_scope"
down_revision = "0011_publication_snapshots"
branch_labels = None
depends_on = None

_FACT_TABLE = "od_published_fact_versions"
_SNAPSHOT_TABLE = "od_publication_snapshots"
_PAIR_CHECK = (
    "(instrument_id IS NULL AND capture_id IS NULL) OR "
    "(instrument_id IS NOT NULL AND capture_id IS NOT NULL)"
)


def _key_type(length: int) -> String:
    """Freeze the same exact MySQL key comparison as the publication schema."""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _require_supported_database() -> None:
    """Reject MySQL versions that cannot enforce the paired-selector checks."""
    dialect = op.get_bind().dialect
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("This migration requires MySQL 8.0.16 or later within MySQL 8.")
    if dialect.name != "mysql":
        return
    version = dialect.server_version_info
    if version is None or len(version) < 3 or version[0] != 8 or version[:3] < (8, 0, 16):
        raise RuntimeError("This migration requires MySQL 8.0.16 or later within MySQL 8.")


def upgrade() -> None:
    """Add nullable paired selectors and the bounded capture-read index."""
    _require_supported_database()
    with op.batch_alter_table(_FACT_TABLE) as batch:
        batch.add_column(Column("instrument_id", _key_type(128), nullable=True))
        batch.add_column(Column("capture_id", _key_type(64), nullable=True))
        batch.create_check_constraint("ck_od_fact_capture_pair", _PAIR_CHECK)
        batch.create_index(
            "ix_od_fact_capture_read",
            ["dataset_id", "instrument_id", "capture_id", "publication_sequence"],
            unique=False,
        )
    with op.batch_alter_table(_SNAPSHOT_TABLE) as batch:
        batch.add_column(Column("instrument_id", _key_type(128), nullable=True))
        batch.add_column(Column("capture_id", _key_type(64), nullable=True))
        batch.create_check_constraint("ck_od_snapshot_capture_pair", _PAIR_CHECK)


def _has_scoped_rows(table_name: str) -> bool:
    """Check the frozen selector columns without importing runtime metadata."""
    scoped = table(table_name, column("instrument_id"), column("capture_id"))
    statement = (
        select(1)
        .select_from(scoped)
        .where(or_(scoped.c.instrument_id.is_not(None), scoped.c.capture_id.is_not(None)))
        .limit(1)
    )
    return op.get_bind().execute(statement).first() is not None


def downgrade() -> None:
    """Remove selector support only when every stored row is still unscoped.

    Raises:
        RuntimeError: Scoped data or snapshots require a separate explicit
            archival procedure before schema rollback can be safe.
    """
    _require_supported_database()
    if _has_scoped_rows(_FACT_TABLE) or _has_scoped_rows(_SNAPSHOT_TABLE):
        raise RuntimeError("Scoped publication data must be archived before schema downgrade.")
    with op.batch_alter_table(_SNAPSHOT_TABLE) as batch:
        batch.drop_constraint("ck_od_snapshot_capture_pair", type_="check")
        batch.drop_column("capture_id")
        batch.drop_column("instrument_id")
    with op.batch_alter_table(_FACT_TABLE) as batch:
        batch.drop_index("ix_od_fact_capture_read")
        batch.drop_constraint("ck_od_fact_capture_pair", type_="check")
        batch.drop_column("capture_id")
        batch.drop_column("instrument_id")
