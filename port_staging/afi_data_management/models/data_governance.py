import enum
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypeAlias

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from opendata.afi_port.models.akshare_mgmt import DataTable

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


def _utcnow_naive() -> datetime:
    """Return a UTC timestamp compatible with the schema's naive DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DgJobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class DgProvider(Base):
    __tablename__ = "dg_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    auth_type: Mapped[str] = mapped_column(String(50), default="none", nullable=False)
    api_key_env: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    endpoints: Mapped[list["DgEndpoint"]] = relationship(
        "DgEndpoint", back_populates="provider", cascade="all, delete-orphan"
    )


class DgDataset(Base):
    """Stable logical data product, independent of a provider or physical table."""

    __tablename__ = "dg_datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_code: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    canonical_schema: Mapped[dict[str, JSONValue]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    primary_key: Mapped[list[JSONValue]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    endpoints: Mapped[list["DgEndpoint"]] = relationship("DgEndpoint", back_populates="dataset")
    storage_bindings: Mapped[list["DgDatasetStorage"]] = relationship(
        "DgDatasetStorage", back_populates="dataset", cascade="all, delete-orphan"
    )


class DgStorageTarget(Base):
    """A registered physical store that materializes one or more datasets."""

    __tablename__ = "dg_storage_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    storage_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    url_env: Mapped[str] = mapped_column(String(100), nullable=False)
    database_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    dataset_bindings: Mapped[list["DgDatasetStorage"]] = relationship(
        "DgDatasetStorage", back_populates="storage_target", cascade="all, delete-orphan"
    )


class DgDatasetStorage(Base):
    """A dataset materialization in a registered physical store.

    A normalized fact table can carry several semantically distinct datasets.
    Each catalog binding is therefore unique per storage, physical table, and
    dataset rather than claiming the table for just one dataset.
    """

    __tablename__ = "dg_dataset_storages"
    __table_args__ = (
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
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_datasets.id"), nullable=False, index=True
    )
    storage_target_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_storage_targets.id"), nullable=False, index=True
    )
    physical_table: Mapped[str] = mapped_column(String(128), nullable=False)
    write_mode: Mapped[str] = mapped_column(String(32), default="legacy_read_only", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ``primary_dataset_id`` mirrors ``dataset_id`` only for the primary
    # binding. A UNIQUE constraint then allows many NULL secondary rows but
    # makes two primary rows for a dataset impossible across supported SQL
    # engines.
    primary_dataset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    dataset: Mapped["DgDataset"] = relationship("DgDataset", back_populates="storage_bindings")
    storage_target: Mapped["DgStorageTarget"] = relationship(
        "DgStorageTarget", back_populates="dataset_bindings"
    )
    data_tables: Mapped[list["DataTable"]] = relationship(
        "DataTable", back_populates="dataset_storage"
    )


@event.listens_for(DgDatasetStorage, "before_insert")
@event.listens_for(DgDatasetStorage, "before_update")
def _sync_primary_dataset_id(_mapper, _connection, target: DgDatasetStorage) -> None:
    """Persist the primary slot used by the portable uniqueness constraint."""
    target.primary_dataset_id = target.dataset_id if target.is_primary else None


class DgEndpoint(Base):
    __tablename__ = "dg_endpoints"
    __table_args__ = (
        UniqueConstraint("provider_id", "endpoint_name", name="uq_dg_endpoint_provider_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_providers.id"), nullable=False, index=True
    )
    dataset_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("dg_datasets.id", name="fk_dg_endpoints_dataset_id_dg_datasets"),
        nullable=True,
        index=True,
    )
    endpoint_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    function_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    params_schema: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    auth_type: Mapped[str] = mapped_column(String(50), default="none", nullable=False)
    api_key_env: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    cache_ttl_sec: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    target_database: Mapped[str] = mapped_column(
        String(100), default="akshare_data", nullable=False
    )
    target_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    normalization_profile: Mapped[dict[str, JSONValue]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    quality_profile: Mapped[dict[str, JSONValue]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    incremental_sync_key: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    legacy_interface_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)

    provider: Mapped["DgProvider"] = relationship("DgProvider", back_populates="endpoints")
    dataset: Mapped["DgDataset | None"] = relationship("DgDataset", back_populates="endpoints")
    params: Mapped[list["DgEndpointParam"]] = relationship(
        "DgEndpointParam", back_populates="endpoint", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["DgIngestJob"]] = relationship(
        "DgIngestJob", back_populates="endpoint", cascade="all, delete-orphan"
    )


class DgEndpointParam(Base):
    __tablename__ = "dg_endpoint_params"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_endpoints.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    param_type: Mapped[str] = mapped_column(String(50), default="string", nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    options: Mapped[JSONValue | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    endpoint: Mapped["DgEndpoint"] = relationship("DgEndpoint", back_populates="params")


class DgIngestJob(Base):
    __tablename__ = "dg_ingest_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_endpoints.id"), nullable=False, index=True
    )
    status: Mapped[DgJobStatus] = mapped_column(
        Enum(DgJobStatus), default=DgJobStatus.QUEUED, nullable=False
    )
    params: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow_naive,
        onupdate=_utcnow_naive,
        nullable=False,
    )

    endpoint: Mapped["DgEndpoint"] = relationship("DgEndpoint", back_populates="jobs")


class DgQualityRule(Base):
    __tablename__ = "dg_quality_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dg_endpoints.id"), nullable=False, index=True
    )
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_config: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
