"""Add the ported data-management governance schema and audit columns.

Revision ID: 0023_afi_data_management_plane
Revises: 0022_publication_visibility_seal
Create Date: 2026-11-09

This revision materializes the AFI data-management plane that OpenData now
owns. It creates the eight ``dg_*`` tables declared by
``opendata.models.data_governance`` and adds the ten columns declared by
``opendata.models.data_script``, ``data_table`` and ``interface``.

The DDL is fixed and reads no live metadata, so it renders offline. Tables are
created parent-first (providers, datasets, storage targets, storage bindings,
endpoints, then the endpoint children) and dropped in the opposite order. Every
model-side Python default (``default=``) stays client-side: no server default is
declared here, including for the NOT NULL ``metadata``, ``extra_config`` and
``updated_at`` columns. SQLite executes the column changes in batch mode, which
this revision needs for the new foreign key and table rebuilds.

MySQL tables are created with the control plane's utf8mb4/utf8mb4_unicode_ci so
the ``data_tables.dataset_storage_id`` foreign key compares against
``dg_dataset_storages.id`` under one collation.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from alembic import op

revision = "0023_afi_data_management_plane"
down_revision = "0022_publication_visibility_seal"
branch_labels = None
depends_on = None

_IndexSpec = tuple[tuple[str, tuple[str, ...], bool], ...]

_DG_PROVIDER_INDEXES: _IndexSpec = (
    ("ix_dg_providers_provider_id", ("provider_id",), True),
    ("ix_dg_providers_category", ("category",), False),
)
_DG_DATASET_INDEXES: _IndexSpec = (
    ("ix_dg_datasets_dataset_code", ("dataset_code",), True),
    ("ix_dg_datasets_domain", ("domain",), False),
)
_DG_STORAGE_TARGET_INDEXES: _IndexSpec = (
    ("ix_dg_storage_targets_storage_id", ("storage_id",), True),
)
_DG_DATASET_STORAGE_INDEXES: _IndexSpec = (
    ("ix_dg_dataset_storages_dataset_id", ("dataset_id",), False),
    ("ix_dg_dataset_storages_storage_target_id", ("storage_target_id",), False),
)
_DG_ENDPOINT_INDEXES: _IndexSpec = (
    ("ix_dg_endpoints_provider_id", ("provider_id",), False),
    ("ix_dg_endpoints_dataset_id", ("dataset_id",), False),
    ("ix_dg_endpoints_endpoint_name", ("endpoint_name",), False),
    ("ix_dg_endpoints_category", ("category",), False),
    ("ix_dg_endpoints_legacy_interface_name", ("legacy_interface_name",), False),
)
_DG_ENDPOINT_PARAM_INDEXES: _IndexSpec = (
    ("ix_dg_endpoint_params_endpoint_id", ("endpoint_id",), False),
)
_DG_INGEST_JOB_INDEXES: _IndexSpec = (
    ("ix_dg_ingest_jobs_endpoint_id", ("endpoint_id",), False),
    ("ix_dg_ingest_jobs_idempotency_key", ("idempotency_key",), False),
)
_DG_QUALITY_RULE_INDEXES: _IndexSpec = (
    ("ix_dg_quality_rules_endpoint_id", ("endpoint_id",), False),
)
_DATA_SCRIPT_INDEXES: _IndexSpec = (
    ("ix_data_scripts_created_by", ("created_by",), False),
    ("ix_data_scripts_updated_by", ("updated_by",), False),
)
_DATA_TABLE_INDEXES: _IndexSpec = (
    ("ix_data_tables_dataset_storage_id", ("dataset_storage_id",), False),
    ("ix_data_tables_symbol_raw", ("symbol_raw",), False),
    ("ix_data_tables_symbol_normalized", ("symbol_normalized",), False),
    ("ix_data_tables_market", ("market",), False),
    ("ix_data_tables_asset_type", ("asset_type",), False),
)
_DATA_TABLE_FK = "fk_data_tables_dataset_storage_id_dg_dataset_storages"


def _is_sqlite() -> bool:
    """Return whether the active migration target is SQLite."""
    return op.get_context().dialect.name == "sqlite"


def _create_indexes(table_name: str, indexes: _IndexSpec) -> None:
    """Create a table's model-declared indexes."""
    for name, columns, unique in indexes:
        op.create_index(name, table_name, list(columns), unique=unique)


def _drop_indexes(table_name: str, indexes: _IndexSpec) -> None:
    """Drop a table's model-declared indexes in reverse declaration order."""
    for name, _columns, _unique in reversed(indexes):
        op.drop_index(name, table_name=table_name)


def _create_providers() -> None:
    """Create the dg_providers table and its indexes."""
    op.create_table(
        "dg_providers",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("provider_id", String(100), nullable=False),
        Column("name", String(200), nullable=False),
        Column("category", String(50), nullable=False),
        Column("auth_type", String(50), nullable=False),
        Column("api_key_env", String(100), nullable=True),
        Column("rate_limit", Integer(), nullable=False),
        Column("is_active", Boolean(), nullable=False),
        Column("created_at", DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_providers", _DG_PROVIDER_INDEXES)


def _create_datasets() -> None:
    """Create the dg_datasets table and its indexes."""
    op.create_table(
        "dg_datasets",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("dataset_code", String(160), nullable=False),
        Column("display_name", String(255), nullable=False),
        Column("domain", String(80), nullable=False),
        Column("canonical_schema", JSON(), nullable=False),
        Column("primary_key", JSON(), nullable=False),
        Column("is_active", Boolean(), nullable=False),
        Column("created_at", DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_datasets", _DG_DATASET_INDEXES)


def _create_storage_targets() -> None:
    """Create the dg_storage_targets table and its indexes."""
    op.create_table(
        "dg_storage_targets",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("storage_id", String(100), nullable=False),
        Column("engine", String(32), nullable=False),
        Column("url_env", String(100), nullable=False),
        Column("database_name", String(100), nullable=False),
        Column("role", String(32), nullable=False),
        Column("is_active", Boolean(), nullable=False),
        Column("created_at", DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_storage_targets", _DG_STORAGE_TARGET_INDEXES)


def _create_dataset_storages() -> None:
    """Create the dg_dataset_storages table, bindings, and indexes."""
    op.create_table(
        "dg_dataset_storages",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("dataset_id", String(36), nullable=False),
        Column("storage_target_id", String(36), nullable=False),
        Column("physical_table", String(128), nullable=False),
        Column("write_mode", String(32), nullable=False),
        Column("is_primary", Boolean(), nullable=False),
        Column("primary_dataset_id", String(36), nullable=True),
        Column("created_at", DateTime(), nullable=False),
        UniqueConstraint(
            "storage_target_id",
            "physical_table",
            "dataset_id",
            name="uq_dg_dataset_storage_target_table",
        ),
        UniqueConstraint(
            "primary_dataset_id",
            name="uq_dg_dataset_storages_primary_dataset",
        ),
        CheckConstraint(
            "(is_primary = true AND primary_dataset_id IS NOT NULL "
            "AND primary_dataset_id = dataset_id) "
            "OR (is_primary = false AND primary_dataset_id IS NULL)",
            name="ck_dg_dataset_storages_primary_slot",
        ),
        ForeignKeyConstraint(["dataset_id"], ["dg_datasets.id"]),
        ForeignKeyConstraint(["storage_target_id"], ["dg_storage_targets.id"]),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_dataset_storages", _DG_DATASET_STORAGE_INDEXES)


def _create_endpoints() -> None:
    """Create the dg_endpoints table, its provider/name key, and indexes."""
    op.create_table(
        "dg_endpoints",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("provider_id", String(36), nullable=False),
        Column("dataset_id", String(36), nullable=True),
        Column("endpoint_name", String(100), nullable=False),
        Column("display_name", String(255), nullable=False),
        Column("function_path", String(255), nullable=True),
        Column("category", String(50), nullable=False),
        Column("params_schema", JSON(), nullable=False),
        Column("auth_type", String(50), nullable=False),
        Column("api_key_env", String(100), nullable=True),
        Column("rate_limit", Integer(), nullable=False),
        Column("cache_ttl_sec", Integer(), nullable=False),
        Column("target_database", String(100), nullable=False),
        Column("target_table", String(100), nullable=True),
        Column("normalization_profile", JSON(), nullable=False),
        Column("quality_profile", JSON(), nullable=False),
        Column("incremental_sync_key", String(100), nullable=False),
        Column("is_active", Boolean(), nullable=False),
        Column("legacy_interface_name", String(100), nullable=True),
        Column("created_at", DateTime(), nullable=False),
        UniqueConstraint("provider_id", "endpoint_name", name="uq_dg_endpoint_provider_name"),
        ForeignKeyConstraint(["provider_id"], ["dg_providers.id"]),
        ForeignKeyConstraint(
            ["dataset_id"],
            ["dg_datasets.id"],
            name="fk_dg_endpoints_dataset_id_dg_datasets",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_endpoints", _DG_ENDPOINT_INDEXES)


def _create_endpoint_params() -> None:
    """Create the dg_endpoint_params table and its indexes."""
    op.create_table(
        "dg_endpoint_params",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("endpoint_id", String(36), nullable=False),
        Column("name", String(100), nullable=False),
        Column("param_type", String(50), nullable=False),
        Column("required", Boolean(), nullable=False),
        Column("default_value", Text(), nullable=True),
        Column("description", Text(), nullable=True),
        Column("options", JSON(), nullable=True),
        Column("sort_order", Integer(), nullable=False),
        ForeignKeyConstraint(["endpoint_id"], ["dg_endpoints.id"]),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_endpoint_params", _DG_ENDPOINT_PARAM_INDEXES)


def _create_ingest_jobs() -> None:
    """Create the dg_ingest_jobs table and its indexes."""
    op.create_table(
        "dg_ingest_jobs",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("endpoint_id", String(36), nullable=False),
        Column(
            "status",
            Enum("QUEUED", "RUNNING", "COMPLETED", "FAILED", name="dgjobstatus"),
            nullable=False,
        ),
        Column("params", JSON(), nullable=False),
        Column("row_count", Integer(), nullable=False),
        Column("idempotency_key", String(128), nullable=True),
        Column("error_message", Text(), nullable=True),
        Column("created_at", DateTime(), nullable=False),
        Column("updated_at", DateTime(), nullable=False),
        ForeignKeyConstraint(["endpoint_id"], ["dg_endpoints.id"]),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_ingest_jobs", _DG_INGEST_JOB_INDEXES)


def _create_quality_rules() -> None:
    """Create the dg_quality_rules table and its indexes."""
    op.create_table(
        "dg_quality_rules",
        Column("id", String(36), primary_key=True, nullable=False),
        Column("endpoint_id", String(36), nullable=False),
        Column("rule_name", String(100), nullable=False),
        Column("rule_type", String(50), nullable=False),
        Column("rule_config", JSON(), nullable=False),
        Column("is_active", Boolean(), nullable=False),
        Column("created_at", DateTime(), nullable=False),
        ForeignKeyConstraint(["endpoint_id"], ["dg_endpoints.id"]),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("dg_quality_rules", _DG_QUALITY_RULE_INDEXES)


def _create_governance_tables() -> None:
    """Create all governance tables in foreign-key-safe order."""
    _create_providers()
    _create_datasets()
    _create_storage_targets()
    _create_dataset_storages()
    _create_endpoints()
    _create_endpoint_params()
    _create_ingest_jobs()
    _create_quality_rules()


def _add_data_script_columns() -> None:
    """Add the two indexed provenance columns to data_scripts."""
    if _is_sqlite():
        with op.batch_alter_table("data_scripts") as batch:
            batch.add_column(Column("created_by", String(36), nullable=True))
            batch.add_column(Column("updated_by", String(36), nullable=True))
            batch.create_index("ix_data_scripts_created_by", ["created_by"])
            batch.create_index("ix_data_scripts_updated_by", ["updated_by"])
        return
    op.add_column("data_scripts", Column("created_by", String(36), nullable=True))
    op.add_column("data_scripts", Column("updated_by", String(36), nullable=True))
    _create_indexes("data_scripts", _DATA_SCRIPT_INDEXES)


def _add_data_table_columns() -> None:
    """Add the dataset-storage, symbol, market, asset, and metadata columns."""
    if _is_sqlite():
        with op.batch_alter_table("data_tables") as batch:
            batch.add_column(Column("dataset_storage_id", String(36), nullable=True))
            batch.add_column(Column("symbol_raw", String(100), nullable=True))
            batch.add_column(Column("symbol_normalized", String(100), nullable=True))
            batch.add_column(Column("market", String(50), nullable=True))
            batch.add_column(Column("asset_type", String(50), nullable=True))
            batch.add_column(Column("metadata", JSON(), nullable=False))
            for name, columns, _unique in _DATA_TABLE_INDEXES:
                batch.create_index(name, list(columns))
            batch.create_foreign_key(
                _DATA_TABLE_FK,
                "dg_dataset_storages",
                ["dataset_storage_id"],
                ["id"],
            )
        return
    op.add_column("data_tables", Column("dataset_storage_id", String(36), nullable=True))
    op.add_column("data_tables", Column("symbol_raw", String(100), nullable=True))
    op.add_column("data_tables", Column("symbol_normalized", String(100), nullable=True))
    op.add_column("data_tables", Column("market", String(50), nullable=True))
    op.add_column("data_tables", Column("asset_type", String(50), nullable=True))
    op.add_column("data_tables", Column("metadata", JSON(), nullable=False))
    _create_indexes("data_tables", _DATA_TABLE_INDEXES)
    op.create_foreign_key(
        _DATA_TABLE_FK,
        "data_tables",
        "dg_dataset_storages",
        ["dataset_storage_id"],
        ["id"],
    )


def _add_data_interface_columns() -> None:
    """Add extra_config and updated_at to data_interfaces."""
    if _is_sqlite():
        with op.batch_alter_table("data_interfaces") as batch:
            batch.add_column(Column("extra_config", JSON(), nullable=False))
            batch.add_column(Column("updated_at", DateTime(timezone=True), nullable=False))
        return
    op.add_column("data_interfaces", Column("extra_config", JSON(), nullable=False))
    op.add_column(
        "data_interfaces",
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )


def _drop_data_interface_columns() -> None:
    """Remove the two data_interfaces columns in reverse order."""
    if _is_sqlite():
        with op.batch_alter_table("data_interfaces") as batch:
            batch.drop_column("updated_at")
            batch.drop_column("extra_config")
        return
    op.drop_column("data_interfaces", "updated_at")
    op.drop_column("data_interfaces", "extra_config")


def _drop_data_table_columns() -> None:
    """Remove the data_tables foreign key, indexes, and columns."""
    if _is_sqlite():
        with op.batch_alter_table("data_tables") as batch:
            batch.drop_constraint(_DATA_TABLE_FK, type_="foreignkey")
            for name, _columns, _unique in reversed(_DATA_TABLE_INDEXES):
                batch.drop_index(name)
            batch.drop_column("metadata")
            batch.drop_column("asset_type")
            batch.drop_column("market")
            batch.drop_column("symbol_normalized")
            batch.drop_column("symbol_raw")
            batch.drop_column("dataset_storage_id")
        return
    op.drop_constraint(_DATA_TABLE_FK, "data_tables", type_="foreignkey")
    _drop_indexes("data_tables", _DATA_TABLE_INDEXES)
    op.drop_column("data_tables", "metadata")
    op.drop_column("data_tables", "asset_type")
    op.drop_column("data_tables", "market")
    op.drop_column("data_tables", "symbol_normalized")
    op.drop_column("data_tables", "symbol_raw")
    op.drop_column("data_tables", "dataset_storage_id")


def _drop_data_script_columns() -> None:
    """Remove the two data_scripts indexes and columns in reverse order."""
    if _is_sqlite():
        with op.batch_alter_table("data_scripts") as batch:
            batch.drop_index("ix_data_scripts_updated_by")
            batch.drop_index("ix_data_scripts_created_by")
            batch.drop_column("updated_by")
            batch.drop_column("created_by")
        return
    _drop_indexes("data_scripts", _DATA_SCRIPT_INDEXES)
    op.drop_column("data_scripts", "updated_by")
    op.drop_column("data_scripts", "created_by")


def _drop_table(table_name: str, indexes: _IndexSpec) -> None:
    """Drop one governance table after its indexes."""
    _drop_indexes(table_name, indexes)
    op.drop_table(table_name)


def upgrade() -> None:
    """Create the governance tables and add the ten ported columns."""
    _create_governance_tables()
    _add_data_script_columns()
    _add_data_table_columns()
    _add_data_interface_columns()


def downgrade() -> None:
    """Drop the governance tables in reverse order and restore the old columns."""
    _drop_data_interface_columns()
    _drop_data_table_columns()
    _drop_data_script_columns()
    _drop_table("dg_quality_rules", _DG_QUALITY_RULE_INDEXES)
    _drop_table("dg_ingest_jobs", _DG_INGEST_JOB_INDEXES)
    _drop_table("dg_endpoint_params", _DG_ENDPOINT_PARAM_INDEXES)
    _drop_table("dg_endpoints", _DG_ENDPOINT_INDEXES)
    _drop_table("dg_dataset_storages", _DG_DATASET_STORAGE_INDEXES)
    _drop_table("dg_storage_targets", _DG_STORAGE_TARGET_INDEXES)
    _drop_table("dg_datasets", _DG_DATASET_INDEXES)
    _drop_table("dg_providers", _DG_PROVIDER_INDEXES)


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
