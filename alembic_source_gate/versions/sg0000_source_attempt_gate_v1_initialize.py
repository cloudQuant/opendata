"""Initialize the exact v1 source-attempt schema in an explicitly empty SQLite file.

Revision ID: sg0000_gate_v1_initialize
Revises:
Create Date: 2026-10-31

Legacy databases that already contain the exact 0015 schema pass through this
revision without DDL. A genuinely empty file is initialized only when the
dedicated CLI opt-in was supplied before the migration transaction began.
"""

from sqlalchemy.engine import Connection

from alembic import context, op
from alembic_source_gate import schema_contract

revision = schema_contract.V0_REVISION
down_revision = None
branch_labels = None
depends_on = None


def _require_empty_source_gate_marker(connection: Connection) -> None:
    """Require Alembic's newly created version table to be canonical and empty."""
    objects = connection.exec_driver_sql(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'view', 'trigger')"
    ).all()
    user_objects = {str(row[1]) for row in objects if not str(row[1]).startswith("sqlite_")}
    if user_objects != {schema_contract.VERSION_TABLE}:
        raise RuntimeError("empty source-gate initialization found unexpected schema objects")

    columns = connection.exec_driver_sql(
        f"PRAGMA table_info({schema_contract.VERSION_TABLE})"
    ).all()
    if len(columns) != 1:
        raise RuntimeError("source-gate initialization requires Alembic's empty version table")
    _cid, name, column_type, not_null, default, primary_key = columns[0]
    if (
        name != "version_num"
        or str(column_type).upper() != "VARCHAR(32)"
        or not_null != 1
        or default is not None
        or primary_key != 1
    ):
        raise RuntimeError("source-gate initialization found an unexpected version-table layout")

    indexes = connection.exec_driver_sql(
        f"PRAGMA index_list({schema_contract.VERSION_TABLE})"
    ).all()
    if len(indexes) != 1 or indexes[0][2] != 1 or indexes[0][3] != "pk":
        raise RuntimeError("source-gate initialization found an unexpected version-table key")
    triggers = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=:name",
        {"name": schema_contract.VERSION_TABLE},
    ).all()
    rows = connection.exec_driver_sql("SELECT version_num FROM alembic_version_source_gate").all()
    if triggers or rows:
        raise RuntimeError("source-gate initialization refuses a non-empty or modified marker")


def upgrade() -> None:
    """Initialize an opted-in empty file or leave an exact legacy v1 gate intact."""
    connection = op.get_bind()
    mode = context.config.attributes.get("source_gate_initialization_mode")
    if mode == "adopt-existing-v1":
        schema_contract.validate_v1_schema(connection)
        return
    if mode != "initialize-empty":
        raise RuntimeError("source-gate v1 initialization requires a validated starting state")

    _require_empty_source_gate_marker(connection)
    schema_contract._apply_v1_reference(connection)
    schema_contract.validate_v1_schema(connection)


def downgrade() -> None:
    """Refuse to remove source-gate tables that may contain permanent evidence."""
    raise RuntimeError("source-gate v1 initialization is permanent and cannot be downgraded")


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
