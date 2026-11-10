"""Data table model for managing stored data.

Defines model for tracking data tables created by data acquisition.
"""

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, Date, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opendata.core.database import Base

if TYPE_CHECKING:
    from opendata.models.data_governance import DgDatasetStorage


class DataTable(Base):
    """Data table model for tracking stored data.

    Tracks metadata about tables created by data acquisition tasks.
    """

    __tablename__ = "data_tables"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    table_comment: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    script_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    dataset_storage_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "dg_dataset_storages.id",
            name="fk_data_tables_dataset_storage_id_dg_dataset_storages",
        ),
        nullable=True,
        index=True,
    )
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_update_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_update_status: Mapped[str | None] = mapped_column(
        Enum("success", "failed", name="update_status"), nullable=True
    )
    data_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    symbol_raw: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    symbol_normalized: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    market: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    asset_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    # Serialized under the historical column name ``metadata``.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    dataset_storage: Mapped["DgDatasetStorage"] = relationship(
        "DgDatasetStorage", back_populates="data_tables"
    )

    def __repr__(self) -> str:
        """Debug form carrying id, table name and row count."""
        return f"<DataTable(id={self.id}, table_name={self.table_name}, rows={self.row_count})>"
