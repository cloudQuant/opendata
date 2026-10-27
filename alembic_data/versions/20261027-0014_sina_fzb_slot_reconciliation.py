"""Persist bounded UTC cron-slot reconciliation for Sina FZB.

Revision ID: 0014_sina_fzb_slot_reconciliation
Revises: 0013_sina_fzb_fanout_groups
Create Date: 2026-10-27

This schema records cron-only slot observations, not trading-day validity or
historical universe selection. The explicit lower boundary is a caller
declaration; earlier history remains NOT_ASSESSED. A recovered row preserves
its first missing timestamp and records only a late fanout-group reference.
The migration does not create groups or enqueue collection work.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    String,
    Text,
    column,
    inspect,
    select,
    table,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

from alembic import op

revision = "0014_sina_fzb_slot_reconciliation"
down_revision = "0013_sina_fzb_fanout_groups"
branch_labels = None
depends_on = None

_STATE_TABLE = "od_sina_fzb_slot_reconciliation_state"
_SLOTS_TABLE = "od_sina_fzb_reconciled_slots"
_JOB_ID = "sina-fzb-capture"
_GRACE_SECONDS = 120
_MAX_BASIS_LENGTH = 255
_MAX_SELECTION_REFERENCE_LENGTH = 512
_MAX_JOB_VERSION_LENGTH = 128
_ALEMBIC_VERSION_COLUMN_LENGTH = 64
_PAIR_STATE_CHECK = (
    "(status = 'PRESENT' AND first_missing_at_us IS NULL "
    "AND first_fanout_status IS NOT NULL AND first_manifest_sha256 IS NOT NULL "
    "AND first_selection_evidence_status = 'GROUP_REFERENCE_ONLY' "
    "AND first_selection_snapshot_ref IS NOT NULL "
    "AND recovered_at_us IS NULL AND recovery_fanout_status IS NULL "
    "AND recovery_manifest_sha256 IS NULL "
    "AND recovery_group_selection_snapshot_ref IS NULL "
    "AND recovery_evidence_status IS NULL) OR "
    "(status = 'MISSING' AND first_missing_at_us = first_checked_at_us "
    "AND first_fanout_status IS NULL AND first_manifest_sha256 IS NULL "
    "AND first_selection_evidence_status = 'UNKNOWN' "
    "AND first_selection_snapshot_ref IS NULL "
    "AND recovered_at_us IS NULL AND recovery_fanout_status IS NULL "
    "AND recovery_manifest_sha256 IS NULL "
    "AND recovery_group_selection_snapshot_ref IS NULL "
    "AND recovery_evidence_status IS NULL) OR "
    "(status = 'RECOVERED' AND first_missing_at_us = first_checked_at_us "
    "AND first_fanout_status IS NULL AND first_manifest_sha256 IS NULL "
    "AND first_selection_evidence_status = 'UNKNOWN' "
    "AND first_selection_snapshot_ref IS NULL "
    "AND recovered_at_us IS NOT NULL AND recovered_at_us >= first_missing_at_us "
    "AND recovery_fanout_status IS NOT NULL AND recovery_manifest_sha256 IS NOT NULL "
    "AND recovery_group_selection_snapshot_ref IS NOT NULL "
    "AND recovery_evidence_status = 'LATE_GROUP_ONLY')"
)


def _mysql_identifier(length: int) -> String:
    """Freeze the repository's exact binary identifier collation for MySQL."""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _require_supported_database() -> None:
    """Allow only the tested SQLite and MySQL 8 migration targets."""
    migration_context = op.get_context()
    dialect = migration_context.dialect
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("This migration supports MySQL 8.0.16+ but not MariaDB.")
    if dialect.name == "sqlite":
        return
    if dialect.name != "mysql":
        raise RuntimeError("This migration supports SQLite and MySQL 8.0.16+ only.")
    if getattr(migration_context, "as_sql", False):
        raise RuntimeError(
            "MySQL offline SQL generation is unsupported: revision 0014 must inspect the live "
            "version table and reject partial DDL before changing schema."
        )
    dialect = op.get_bind().dialect
    version = dialect.server_version_info
    if version is None or len(version) < 3 or version[0] != 8 or version[:3] < (8, 0, 16):
        raise RuntimeError("This migration requires MySQL 8.0.16 or later within MySQL 8.")


def _prepare_mysql_version_table() -> None:
    """Validate revision state and widen Alembic's marker before MySQL DDL.

    MySQL DDL commits independently of the surrounding migration transaction.
    The target tables therefore must both be absent before any write; an older
    failed 0014 attempt is left for explicit offline recovery instead of being
    guessed at or resumed automatically.
    """
    migration_context = op.get_context()
    if getattr(migration_context, "as_sql", False):
        raise RuntimeError(
            "MySQL offline SQL generation is unsupported: revision 0014 requires live state "
            "inspection."
        )

    bind = op.get_bind()
    inspector = inspect(bind)
    existing_targets = [name for name in (_STATE_TABLE, _SLOTS_TABLE) if inspector.has_table(name)]
    if existing_targets:
        names = ", ".join(existing_targets)
        raise RuntimeError(
            "Revision 0014 found pre-existing reconciliation table(s) "
            f"{names}; possible partial or prior DDL requires offline verification before retry."
        )

    version_table = migration_context.version_table
    version_schema = migration_context.version_table_schema
    if not isinstance(version_table, str) or not version_table.strip():
        raise RuntimeError("Revision 0014 requires a configured Alembic version table name.")
    if version_schema is not None and (
        not isinstance(version_schema, str) or not version_schema.strip()
    ):
        raise RuntimeError("Revision 0014 requires a valid Alembic version table schema.")
    if not inspector.has_table(version_table, schema=version_schema):
        raise RuntimeError(
            "Revision 0014 requires the configured Alembic version table to exist before upgrade."
        )

    columns = inspector.get_columns(version_table, schema=version_schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError(
            "Revision 0014 requires the Alembic version table to contain only version_num."
        )
    version_column = columns[0]
    version_type = version_column.get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or version_type.length < len(down_revision)
    ):
        raise RuntimeError(
            "Revision 0014 requires a bounded VARCHAR version_num column large enough to hold "
            "the current revision."
        )
    if version_column.get("nullable") is not False:
        raise RuntimeError("Revision 0014 requires version_num to be NOT NULL.")
    if version_column.get("default") is not None:
        raise RuntimeError("Revision 0014 does not alter a version_num column with a default.")

    primary_key = inspector.get_pk_constraint(version_table, schema=version_schema)
    if primary_key.get("constrained_columns") != ["version_num"]:
        raise RuntimeError(
            "Revision 0014 requires version_num as the version table's sole primary key."
        )

    version_rows = (
        bind.execute(
            select(
                table(
                    version_table,
                    column("version_num", version_type),
                    schema=version_schema,
                ).c.version_num
            )
        )
        .scalars()
        .all()
    )
    if version_rows != [down_revision]:
        raise RuntimeError(
            "Revision 0014 requires exactly one version row matching its 0013 parent; "
            "unexpected or truncated version state requires offline verification."
        )

    if version_type.length < _ALEMBIC_VERSION_COLUMN_LENGTH:
        widened_type = version_type.adapt(type(version_type), length=_ALEMBIC_VERSION_COLUMN_LENGTH)
        op.alter_column(
            version_table,
            "version_num",
            schema=version_schema,
            existing_type=version_type,
            type_=widened_type,
            existing_nullable=False,
            existing_comment=version_column.get("comment"),
        )


def upgrade() -> None:
    """Create the explicit cron reconciliation cursor and slot observations."""
    _require_supported_database()
    if op.get_bind().dialect.name == "mysql":
        _prepare_mysql_version_table()
    op.create_table(
        _STATE_TABLE,
        Column(
            "job_id",
            _mysql_identifier(128),
            primary_key=True,
            nullable=False,
        ),
        Column("job_version", _mysql_identifier(_MAX_JOB_VERSION_LENGTH), nullable=False),
        Column("schedule_fingerprint", String(64), nullable=False),
        Column(
            "schedule_snapshot_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False
        ),
        Column("calendar_policy", _mysql_identifier(16), nullable=False),
        Column("calendar_id", _mysql_identifier(128), nullable=True),
        Column("calendar_version", _mysql_identifier(128), nullable=True),
        Column("calendar_sha256", String(64), nullable=True),
        Column("grace_seconds", BigInteger(), nullable=False),
        Column("earlier_history_status", _mysql_identifier(16), nullable=False),
        Column("initial_from_basis", _mysql_identifier(_MAX_BASIS_LENGTH), nullable=False),
        Column("initial_from_us", BigInteger(), nullable=False),
        Column("reconciled_through_us", BigInteger(), nullable=False),
        Column("cursor_revision", BigInteger(), nullable=False),
        Column("status", _mysql_identifier(16), nullable=False),
        Column("created_at_us", BigInteger(), nullable=False),
        Column("updated_at_us", BigInteger(), nullable=False),
        Column("last_checked_at_us", BigInteger(), nullable=False),
        CheckConstraint(f"job_id = '{_JOB_ID}'", name="ck_od_sina_fzb_reconcile_job"),
        CheckConstraint(
            "calendar_policy = 'cron_only'",
            name="ck_od_sina_fzb_reconcile_calendar_policy",
        ),
        CheckConstraint(
            "calendar_id IS NULL AND calendar_version IS NULL AND calendar_sha256 IS NULL",
            name="ck_od_sina_fzb_reconcile_no_calendar",
        ),
        CheckConstraint(f"grace_seconds = {_GRACE_SECONDS}", name="ck_od_sina_fzb_reconcile_grace"),
        CheckConstraint(
            "earlier_history_status = 'NOT_ASSESSED'",
            name="ck_od_sina_fzb_reconcile_history",
        ),
        CheckConstraint(
            "length(schedule_fingerprint) = 64",
            name="ck_od_sina_fzb_reconcile_fingerprint_len",
        ),
        CheckConstraint(
            f"length(initial_from_basis) BETWEEN 1 AND {_MAX_BASIS_LENGTH}",
            name="ck_od_sina_fzb_reconcile_basis_nonempty",
        ),
        CheckConstraint(
            "reconciled_through_us >= initial_from_us",
            name="ck_od_sina_fzb_reconcile_watermark",
        ),
        CheckConstraint("cursor_revision >= 0", name="ck_od_sina_fzb_reconcile_revision"),
        CheckConstraint(
            "status IN ('ACTIVE', 'BLOCKED')",
            name="ck_od_sina_fzb_reconcile_status",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        _SLOTS_TABLE,
        Column("job_id", _mysql_identifier(128), primary_key=True, nullable=False),
        Column("occurrence_at_us", BigInteger(), primary_key=True, nullable=False),
        Column("job_version", _mysql_identifier(_MAX_JOB_VERSION_LENGTH), nullable=False),
        Column("schedule_fingerprint", String(64), nullable=False),
        Column("status", _mysql_identifier(16), nullable=False),
        Column("first_checked_at_us", BigInteger(), nullable=False),
        Column("first_missing_at_us", BigInteger(), nullable=True),
        Column("first_fanout_status", _mysql_identifier(16), nullable=True),
        Column("first_manifest_sha256", String(64), nullable=True),
        Column("first_selection_evidence_status", _mysql_identifier(32), nullable=False),
        Column(
            "first_selection_snapshot_ref",
            _mysql_identifier(_MAX_SELECTION_REFERENCE_LENGTH),
            nullable=True,
        ),
        Column("recovered_at_us", BigInteger(), nullable=True),
        Column("recovery_fanout_status", _mysql_identifier(16), nullable=True),
        Column("recovery_manifest_sha256", String(64), nullable=True),
        Column(
            "recovery_group_selection_snapshot_ref",
            _mysql_identifier(_MAX_SELECTION_REFERENCE_LENGTH),
            nullable=True,
        ),
        Column("recovery_evidence_status", _mysql_identifier(24), nullable=True),
        CheckConstraint(f"job_id = '{_JOB_ID}'", name="ck_od_sina_fzb_reconciled_job"),
        CheckConstraint(
            "status IN ('PRESENT', 'MISSING', 'RECOVERED')",
            name="ck_od_sina_fzb_reconciled_status",
        ),
        CheckConstraint(
            "length(schedule_fingerprint) = 64",
            name="ck_od_sina_fzb_reconciled_fingerprint_len",
        ),
        CheckConstraint(
            "first_selection_evidence_status IN ('UNKNOWN', 'GROUP_REFERENCE_ONLY')",
            name="ck_od_sina_fzb_reconciled_selection_evidence",
        ),
        CheckConstraint(
            "first_fanout_status IS NULL OR "
            "first_fanout_status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
            name="ck_od_sina_fzb_reconciled_first_fanout_status",
        ),
        CheckConstraint(
            "recovery_fanout_status IS NULL OR "
            "recovery_fanout_status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
            name="ck_od_sina_fzb_reconciled_recovery_fanout_status",
        ),
        CheckConstraint(_PAIR_STATE_CHECK, name="ck_od_sina_fzb_reconciled_state_shape"),
        CheckConstraint(
            "first_manifest_sha256 IS NULL OR length(first_manifest_sha256) = 64",
            name="ck_od_sina_fzb_reconciled_first_sha_len",
        ),
        CheckConstraint(
            "recovery_manifest_sha256 IS NULL OR length(recovery_manifest_sha256) = 64",
            name="ck_od_sina_fzb_reconciled_recovery_sha_len",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_od_sina_fzb_reconciled_recovery",
        _SLOTS_TABLE,
        ["job_id", "status", "occurrence_at_us"],
        unique=False,
    )


def _has_rows(table_name: str) -> bool:
    """Check whether a journal table still contains recovery evidence."""
    rows = table(table_name, column("job_id"))
    return op.get_bind().execute(select(1).select_from(rows).limit(1)).first() is not None


def downgrade() -> None:
    """Refuse to drop reconciliation history unless it has been archived."""
    _require_supported_database()
    if _has_rows(_SLOTS_TABLE) or _has_rows(_STATE_TABLE):
        raise RuntimeError(
            "Sina FZB slot reconciliation evidence must be archived before downgrade."
        )
    op.drop_index("ix_od_sina_fzb_reconciled_recovery", table_name=_SLOTS_TABLE)
    op.drop_table(_SLOTS_TABLE)
    op.drop_table(_STATE_TABLE)


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
