"""Adopt only the exact existing SQLite source-attempt gate v1 schema.

Revision ID: sg0001_gate_v1_adopt
Revises:
Create Date: 2026-10-28

This revision writes no source-gate tables or data. It validates the complete
schema produced by the pinned warehouse-chain revision 0015, then records this
independent chain's revision marker in the same SQLite transaction. The 0015
code runs only against an isolated in-memory SQLite database to construct the
comparison baseline.
"""

from alembic import op
from alembic_source_gate.schema_contract import validate_v1_schema

revision = "sg0001_gate_v1_adopt"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Validate the existing v1 schema before the standalone chain adopts it."""
    validate_v1_schema(op.get_bind())


def downgrade() -> None:
    """Refuse to remove an adoption marker that may have governed v2 data."""
    raise RuntimeError("source gate v1 adoption is permanent and cannot be downgraded")
