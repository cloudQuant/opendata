"""consumer api keys

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

Design §10.3: machine-to-machine consumers authenticate with an API key
that acts as its owner. The table stores only ``sha256(pepper + key)``
(equality-indexed, compared in constant time) plus the lifecycle
columns: ``scopes`` domain whitelist, per-key ``rate_limit``,
``expires_at``, and ``rotated_at`` so a rotation is traceable.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the api_keys table."""
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("rate_limit", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "REVOKED", name="apikeystatus"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_api_keys_key_hash"), "api_keys", ["key_hash"], unique=True)
    op.create_index(op.f("ix_api_keys_owner_user_id"), "api_keys", ["owner_user_id"])


def downgrade() -> None:
    """Drop the api_keys table.

    Dropping the table also drops its indexes: MySQL refuses to drop
    ``ix_api_keys_owner_user_id`` on its own because the foreign key
    depends on it (error 1553).
    """
    op.drop_table("api_keys")
