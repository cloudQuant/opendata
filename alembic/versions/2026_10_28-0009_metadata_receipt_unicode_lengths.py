"""Align publication receipt text checks with publisher character limits.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-28

The publisher defines dataset_id and schema_version limits in characters.
SQLite length(TEXT) already counts characters; MySQL LENGTH(TEXT) counts bytes,
so the MySQL checks must use CHAR_LENGTH. No receipt rows, keys, or digests are
rewritten by this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "od_metadata_publication_receipts"
_DATASET_CHECK = "ck_od_meta_receipt_dataset_len"
_SCHEMA_CHECK = "ck_od_meta_receipt_schema"


def _require_supported_target() -> str:
    """Require a live SQLite or MySQL 8.0.16+ connection."""
    context = op.get_context()
    if getattr(context, "as_sql", False):
        raise RuntimeError("revision 0009 requires a live SQLite or MySQL connection")

    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0009 supports MySQL 8.0.16+, not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0009 supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise RuntimeError("revision 0009 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _replace_mysql_checks(*, character_length: bool) -> None:
    """Replace both checks in one MySQL DDL statement.

    MySQL DDL implicitly commits. Combining each drop/add pair in one ALTER
    TABLE avoids an intermediate state without either length constraint.
    MySQL 8 atomic DDL preserves the original table if this statement fails.
    """
    if character_length:
        dataset_expression = "CHAR_LENGTH(`dataset_id`)"
        schema_expression = "CHAR_LENGTH(`schema_version`)"
    else:
        dataset_expression = "LENGTH(`dataset_id`)"
        schema_expression = "LENGTH(`schema_version`)"

    op.execute(
        sa.text(
            f"ALTER TABLE `{_TABLE}` "
            f"DROP CHECK `{_DATASET_CHECK}`, "
            f"DROP CHECK `{_SCHEMA_CHECK}`, "
            f"ADD CONSTRAINT `{_DATASET_CHECK}` CHECK "
            f"({dataset_expression} BETWEEN 1 AND 128), "
            f"ADD CONSTRAINT `{_SCHEMA_CHECK}` CHECK "
            f"({schema_expression} BETWEEN 1 AND 64)"
        )
    )


def _has_values_outside_previous_byte_limits() -> bool:
    """Return whether any MySQL row violates the prior byte-based checks."""
    statement = sa.text(
        "SELECT 1 FROM `od_metadata_publication_receipts` "
        "WHERE LENGTH(`dataset_id`) > 128 "
        "OR LENGTH(`schema_version`) > 64 LIMIT 1"
    )
    return op.get_bind().execute(statement).first() is not None


def upgrade() -> None:
    """Use character counts on both supported database families."""
    dialect_name = _require_supported_target()
    if dialect_name == "mysql":
        _replace_mysql_checks(character_length=True)
    # Revision 0008's SQLite length(TEXT) checks already count characters.
    # Keeping them untouched avoids a needless table rebuild and preserves rows.


def downgrade() -> None:
    """Restore byte bounds only when existing text values still satisfy them."""
    dialect_name = _require_supported_target()
    if dialect_name == "mysql" and _has_values_outside_previous_byte_limits():
        raise RuntimeError(
            "revision 0009 refuses downgrade: multibyte dataset_id or schema_version "
            "values exceed the previous byte-length limits; preserve the rows and stay "
            "on revision 0009"
        )

    if dialect_name == "mysql":
        _replace_mysql_checks(character_length=False)
    # SQLite's 0008 constraints already had the same character semantics.
