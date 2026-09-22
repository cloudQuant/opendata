"""
Data table model for managing stored data.

Defines model for tracking data tables created by data acquisition.
"""

from datetime import UTC, date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base


class DataTable(Base):
    """
    Data table model for tracking stored data.

    Tracks metadata about tables created by data acquisition tasks.
    """

    __tablename__ = "data_tables"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    table_comment: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    script_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_update_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_update_status: Mapped[str | None] = mapped_column(
        Enum("success", "failed", name="update_status"), nullable=True
    )
    data_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<DataTable(id={self.id}, table_name={self.table_name}, rows={self.row_count})>"
