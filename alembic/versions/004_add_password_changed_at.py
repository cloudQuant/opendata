"""add password_changed_at column to users

Revision ID: 004
Revises: 003
Create Date: 2024-03-13

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Column, DateTime
from sqlalchemy.sql import func


revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', Column('password_changed_at', DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'password_changed_at')
