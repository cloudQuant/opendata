"""Add an immutable single-consumption scope lock to the source-attempt gate.

Revision ID: sg0004_source_one_shot_v4
Revises: sg0003_source_attempt_scope_v3
Create Date: 2026-10-30

The revision adds no configuration or permit rows. A trusted local gate writer
may later lock one exact v3 request scope in a fresh, already configured gate.
The permit remains append-only and the scope is bounded to this SQLite file.
"""

from alembic import op
from alembic_source_gate.schema_contract import (
    V3_REVISION,
    V4_REVISION,
    create_one_shot_v4,
    validate_migration_state,
)

revision = V4_REVISION
down_revision = V3_REVISION
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Require the exact v3 schema, then install one-shot tables and guards."""
    connection = op.get_bind()
    marker = validate_migration_state(connection)
    if marker != V3_REVISION:
        raise RuntimeError("source gate one-shot v4 requires the exact v3 revision")
    create_one_shot_v4(connection)


def downgrade() -> None:
    """Preserve the one-shot configuration and permit ledger permanently."""
    raise RuntimeError("source gate one-shot v4 is permanent and cannot be downgraded")
