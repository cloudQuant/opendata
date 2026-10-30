"""Add an immutable v3 source-attempt scope sidecar.

Revision ID: sg0003_source_attempt_scope_v3
Revises: sg0002_source_attempt_scope_v2
Create Date: 2026-10-30

No historical reservation is backfilled. A v3 sidecar is inserted only by the
runtime in the same transaction as its corresponding new reservation.
"""

from sqlalchemy.exc import SQLAlchemyError

from alembic import op
from alembic_source_gate.schema_contract import (
    V2_REVISION,
    V3_REVISION,
    create_scope_v3,
    validate_migration_state,
)

revision = V3_REVISION
down_revision = V2_REVISION
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Require the exact v2 lineage, then add the scope-v3 sidecar."""
    connection = op.get_bind()
    marker = validate_migration_state(connection)
    if marker != V2_REVISION:
        raise RuntimeError("source gate scope v3 requires the exact v2 revision")
    create_scope_v3(connection)


def downgrade() -> None:
    """Drop v3 only when its append-only evidence table is empty."""
    connection = op.get_bind()
    marker = validate_migration_state(connection)
    if marker != V3_REVISION:
        raise RuntimeError("source gate scope v3 downgrade requires the exact v3 revision")
    try:
        count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM od_source_attempt_scope_v3"
        ).scalar_one()
    except SQLAlchemyError as exc:
        raise RuntimeError("source gate scope v3 schema is unavailable") from exc
    if count:
        raise RuntimeError("source gate scope v3 evidence must be archived before downgrade")
    connection.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_od_source_attempt_scope_v3_immutable_delete"
    )
    connection.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_od_source_attempt_scope_v3_immutable_update"
    )
    connection.exec_driver_sql("DROP TABLE od_source_attempt_scope_v3")
