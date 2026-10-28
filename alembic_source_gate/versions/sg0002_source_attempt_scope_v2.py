"""Add the immutable v2 scope sidecar to existing source-attempt reservations.

Revision ID: sg0002_source_attempt_scope_v2
Revises: sg0001_gate_v1_adopt
Create Date: 2026-10-28

No historical reservation is backfilled. A scope row is inserted only by the
runtime in the same transaction as its corresponding new reservation.
"""

from sqlalchemy.exc import SQLAlchemyError

from alembic import op
from alembic_source_gate.schema_contract import create_scope_v2, validate_v1_schema

revision = "sg0002_source_attempt_scope_v2"
down_revision = "sg0001_gate_v1_adopt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Validate v1 again, then create the sidecar and immutable triggers."""
    connection = op.get_bind()
    validate_v1_schema(connection)
    create_scope_v2(connection)


def downgrade() -> None:
    """Preserve append-only v2 scope evidence; downgrade is intentionally blocked."""
    connection = op.get_bind()
    try:
        count = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM od_source_attempt_scope_v2"
        ).scalar_one()
    except SQLAlchemyError as exc:
        raise RuntimeError("source gate scope v2 schema is unavailable") from exc
    if count:
        raise RuntimeError("source gate scope v2 evidence must be archived before downgrade")
    connection.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_od_source_attempt_scope_v2_immutable_delete"
    )
    connection.exec_driver_sql(
        "DROP TRIGGER IF EXISTS trg_od_source_attempt_scope_v2_immutable_update"
    )
    connection.exec_driver_sql("DROP TABLE od_source_attempt_scope_v2")
