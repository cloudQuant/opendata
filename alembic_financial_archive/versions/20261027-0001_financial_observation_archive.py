"""Create durable source-native financial capture and observation storage.

Revision ID: financial_archive_0001
Revises:
Create Date: 2026-10-27

This first revision belongs to an independent migration chain and version
table. It does not alter the warehouse chain or its SQLite-only revision 0015.
MySQL support is limited to MySQL 8.0.16 through the MySQL 8 major version;
SQLite is permitted only for in-memory or explicitly named disposable test
databases. Raw bytes and manifests remain external content-addressed files;
the database records their identifiers and sizes, not their payloads. Exact
digest-to-relative-key validation belongs to the store API because portable
SQL check expressions cannot safely express the canonical key encoding across
MySQL and SQLite.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "financial_archive_0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None

_CAPTURES = "od_financial_captures"
_OBSERVATIONS = "od_financial_observations"
_LINKS = "od_financial_capture_observation_links"
_QUARANTINE = "od_financial_observation_quarantine"
_SOURCE_IDS = "length(source_id) BETWEEN 1 AND 64"
_STATEMENT_TYPES = "statement_type IN ('income', 'balance', 'cashflow')"
_PERIODICITIES = "periodicity IN ('annual', 'quarterly')"


def _key(length: int) -> String:
    """Return portable key text with byte-exact MySQL comparison semantics."""
    return String(length).with_variant(mysql.VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _fixed_key(length: int) -> String:
    """Return fixed-width hashes and IDs with byte-exact MySQL collation."""
    return String(length).with_variant(mysql.CHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _utc_datetime() -> DateTime:
    """Use naive UTC values in SQL, with microsecond-preserving MySQL DATETIME."""
    return DateTime(timezone=False).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _large_text() -> Text:
    """Keep bounded JSON metadata below MySQL MEDIUMTEXT limits."""
    return Text().with_variant(mysql.MEDIUMTEXT(), "mysql")


def _require_supported_dialect(
    dialect_name: str,
    server_version_info: tuple[int, ...] | None,
    *,
    is_mariadb: bool = False,
) -> None:
    """Require MySQL 8.0.16+ or SQLite; reject MariaDB and other engines."""
    if dialect_name == "sqlite":
        return
    if dialect_name == "mariadb" or is_mariadb:
        raise RuntimeError("financial archive requires MySQL 8.0.16 or later within MySQL 8")
    if dialect_name != "mysql":
        raise RuntimeError("financial archive supports MySQL 8.0.16+ and temporary SQLite only")
    if (
        server_version_info is None
        or len(server_version_info) < 3
        or server_version_info[0] != 8
        or server_version_info[:3] < (8, 0, 16)
    ):
        raise RuntimeError("financial archive requires MySQL 8.0.16 or later within MySQL 8")


def _require_supported_database() -> None:
    """Fail before DDL on unsupported dialects or server versions."""
    bind = op.get_bind()
    dialect = bind.dialect
    if dialect.name == "sqlite":
        url = bind.engine.url
        database = url.database
        if database in {None, "", ":memory:"}:
            return
        if not isinstance(database, str) or database.startswith("file:"):
            raise RuntimeError("SQLite archive migrations require an in-memory or temporary DB")
        path = Path(database).resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            path.relative_to(temporary_root)
        except ValueError as exc:
            raise RuntimeError(
                "SQLite archive database must be under the system temp directory"
            ) from exc
        if not path.name.startswith("test_financial_archive_") or path.suffix not in {
            ".db",
            ".sqlite",
        }:
            raise RuntimeError("SQLite archive database must use a test_financial_archive_ name")
        return
    _require_supported_dialect(
        dialect.name,
        dialect.server_version_info,
        is_mariadb=bool(getattr(dialect, "is_mariadb", False)),
    )


def upgrade() -> None:
    """Create capture, observation, link, and quarantine storage tables."""
    _require_supported_database()
    op.create_table(
        _CAPTURES,
        Column("capture_id", _fixed_key(32), primary_key=True, nullable=False),
        Column("provider_id", _key(128), nullable=False),
        Column("dataset_id", _key(128), nullable=False),
        Column("operation_id", _key(128), nullable=False),
        Column("source_id", _key(64), nullable=False),
        Column("instrument_id", _key(10), nullable=False),
        Column("statement_type", _key(16), nullable=False),
        Column("periodicity", _key(16), nullable=False),
        Column("observed_at_utc", _utc_datetime(), nullable=False),
        Column("captured_at_utc", _utc_datetime(), nullable=False),
        Column("source_request_id", _key(256), nullable=False),
        Column("http_status", SmallInteger(), nullable=False),
        Column("request_summary_sha256", _fixed_key(64), nullable=False),
        Column("raw_object_sha256", _fixed_key(64), nullable=False),
        Column("raw_object_size_bytes", BigInteger(), nullable=False),
        Column("raw_object_key", _key(512), nullable=False),
        Column("raw_manifest_sha256", _fixed_key(64), nullable=False),
        Column("raw_manifest_size_bytes", BigInteger(), nullable=False),
        Column("raw_manifest_key", _key(512), nullable=False),
        Column("retention_class", _key(24), nullable=False),
        Column("source_history_limit", _key(32), nullable=False),
        Column("native_unit_mapping_version_declared", _key(128), nullable=False),
        Column("native_unit_mapping_sha256_declared", _fixed_key(64), nullable=False),
        Column("unit_mapping_declaration_status", _key(24), nullable=False),
        Column("decode_status", _key(24), nullable=False),
        CheckConstraint("length(capture_id) = 32", name="ck_od_fin_capture_id_length"),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="ck_od_fin_capture_http_status"),
        CheckConstraint("raw_object_size_bytes >= 0", name="ck_od_fin_capture_object_size"),
        CheckConstraint("raw_manifest_size_bytes >= 0", name="ck_od_fin_capture_manifest_size"),
        CheckConstraint(
            "length(request_summary_sha256) = 64 AND length(raw_object_sha256) = 64 "
            "AND length(raw_manifest_sha256) = 64 "
            "AND length(native_unit_mapping_sha256_declared) = 64",
            name="ck_od_fin_capture_hash_lengths",
        ),
        CheckConstraint(
            "length(raw_object_key) BETWEEN 1 AND 512 "
            "AND length(raw_manifest_key) BETWEEN 1 AND 512",
            name="ck_od_fin_capture_keys",
        ),
        CheckConstraint(
            "retention_class IN ('unknown', 'ephemeral', 'restricted', 'durable_candidate')",
            name="ck_od_fin_capture_retention",
        ),
        CheckConstraint(
            "source_history_limit IN ('unknown', 'provider_retention_window', 'snapshot_only', "
            "'partial_history', 'none_documented')",
            name="ck_od_fin_capture_history_limit",
        ),
        CheckConstraint(
            "length(native_unit_mapping_version_declared) BETWEEN 1 AND 128",
            name="ck_od_fin_capture_unit_version",
        ),
        CheckConstraint(_SOURCE_IDS, name="ck_od_fin_capture_source_id"),
        CheckConstraint(
            "statement_type IN ('income', 'balance', 'cashflow')",
            name="ck_od_fin_capture_statement_type",
        ),
        CheckConstraint(
            "periodicity IN ('annual', 'quarterly')", name="ck_od_fin_capture_periodicity"
        ),
        CheckConstraint(
            "unit_mapping_declaration_status = 'operator_declared'",
            name="ck_od_fin_capture_unit_declared",
        ),
        CheckConstraint(
            "decode_status IN ('decoded', 'decoded_with_quarantine')",
            name="ck_od_fin_capture_decode_status",
        ),
        Index(
            "ix_od_fin_capture_scope_observed",
            "source_id",
            "instrument_id",
            "statement_type",
            "periodicity",
            "observed_at_utc",
        ),
        mysql_engine="InnoDB",
        mysql_row_format="DYNAMIC",
    )

    op.create_table(
        _OBSERVATIONS,
        Column("observation_id", _fixed_key(64), primary_key=True, nullable=False),
        Column("natural_key_sha256", _fixed_key(64), nullable=False),
        Column("content_sha256", _fixed_key(64), nullable=False),
        Column("source_id", _key(64), nullable=False),
        Column("instrument_id", _key(10), nullable=False),
        Column("statement_type", _key(16), nullable=False),
        Column("periodicity", _key(16), nullable=False),
        Column("report_period", Date(), nullable=False),
        Column("source_disclosure_date", Date(), nullable=False),
        Column("observed_at_utc", _utc_datetime(), nullable=False),
        Column("item_code", _key(256), nullable=False),
        Column("value_decimal_text", _key(128), nullable=True),
        Column("native_unit", _key(128), nullable=False),
        Column("source_metadata_json", _large_text(), nullable=False),
        Column("observation_policy", _key(40), nullable=False),
        CheckConstraint(_SOURCE_IDS, name="ck_od_fin_observation_source_id"),
        CheckConstraint(_STATEMENT_TYPES, name="ck_od_fin_observation_statement_type"),
        CheckConstraint(_PERIODICITIES, name="ck_od_fin_observation_periodicity"),
        CheckConstraint(
            "length(observation_id) = 64 AND length(natural_key_sha256) = 64 "
            "AND length(content_sha256) = 64",
            name="ck_od_fin_observation_hash_lengths",
        ),
        CheckConstraint(
            "source_disclosure_date >= report_period",
            name="ck_od_fin_observation_disclosure_not_before_period",
        ),
        CheckConstraint(
            "length(trim(item_code)) BETWEEN 1 AND 256 "
            "AND length(trim(native_unit)) BETWEEN 1 AND 128",
            name="ck_od_fin_observation_tokens",
        ),
        CheckConstraint(
            "length(value_decimal_text) BETWEEN 1 AND 128 OR value_decimal_text IS NULL",
            name="ck_od_fin_observation_decimal_text_length",
        ),
        CheckConstraint(
            "length(source_metadata_json) BETWEEN 2 AND 65536",
            name="ck_od_fin_observation_metadata_size",
        ),
        CheckConstraint(
            "observation_policy = 'current_as_observed_only'",
            name="ck_od_fin_observation_policy",
        ),
        UniqueConstraint(
            "natural_key_sha256",
            "content_sha256",
            name="uq_od_fin_observation_natural_content",
        ),
        Index(
            "ix_od_fin_observation_history",
            "source_id",
            "instrument_id",
            "statement_type",
            "periodicity",
            "report_period",
        ),
        mysql_engine="InnoDB",
        mysql_row_format="DYNAMIC",
    )

    op.create_table(
        _LINKS,
        Column("link_id", _fixed_key(64), primary_key=True, nullable=False),
        Column("capture_id", _fixed_key(32), nullable=False),
        Column("value_pointer", _key(512), nullable=False),
        Column("observation_id", _fixed_key(64), nullable=False),
        Column("row_pointer", _key(512), nullable=False),
        ForeignKeyConstraint(
            ["capture_id"],
            [_CAPTURES + ".capture_id"],
            ondelete="RESTRICT",
            name="fk_od_fin_link_capture",
        ),
        ForeignKeyConstraint(
            ["observation_id"],
            [_OBSERVATIONS + ".observation_id"],
            ondelete="RESTRICT",
            name="fk_od_fin_link_observation",
        ),
        CheckConstraint("length(link_id) = 64", name="ck_od_fin_link_id_length"),
        CheckConstraint(
            "length(value_pointer) BETWEEN 1 AND 512 AND length(row_pointer) BETWEEN 1 AND 512",
            name="ck_od_fin_link_pointer_lengths",
        ),
        UniqueConstraint(
            "capture_id", "value_pointer", name="uq_od_fin_link_capture_value_pointer"
        ),
        Index("ix_od_fin_link_observation", "observation_id"),
        mysql_engine="InnoDB",
        mysql_row_format="DYNAMIC",
    )

    op.create_table(
        _QUARANTINE,
        Column("quarantine_id", _fixed_key(64), primary_key=True, nullable=False),
        Column("capture_id", _fixed_key(32), nullable=False),
        Column("row_pointer", _key(512), nullable=False),
        Column("disclosure_pointer", _key(512), nullable=True),
        Column("reason", _key(48), nullable=False),
        ForeignKeyConstraint(
            ["capture_id"],
            [_CAPTURES + ".capture_id"],
            ondelete="RESTRICT",
            name="fk_od_fin_quarantine_capture",
        ),
        CheckConstraint("length(quarantine_id) = 64", name="ck_od_fin_quarantine_id_length"),
        CheckConstraint(
            "length(row_pointer) BETWEEN 1 AND 512 AND "
            "(length(disclosure_pointer) BETWEEN 1 AND 512 OR disclosure_pointer IS NULL)",
            name="ck_od_fin_quarantine_pointer_lengths",
        ),
        CheckConstraint(
            "reason IN ('disclosure_date_missing', 'disclosure_date_invalid', "
            "'disclosure_date_after_observed_at')",
            name="ck_od_fin_quarantine_reason",
        ),
        UniqueConstraint("capture_id", "row_pointer", name="uq_od_fin_quarantine_capture_row"),
        Index("ix_od_fin_quarantine_capture", "capture_id"),
        mysql_engine="InnoDB",
        mysql_row_format="DYNAMIC",
    )


def downgrade() -> None:
    """Refuse rollback on every dialect; this evidence schema is forward-only."""
    raise RuntimeError(
        "financial archive migrations are forward-only; deploy a forward repair migration "
        "and preserve retained evidence"
    )


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
