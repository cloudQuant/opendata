"""
Data script model for managing data acquisition scripts.

Extends the interface concept to include executable scripts with
scheduling and execution tracking capabilities.
"""

import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base


class ScriptFrequency(str, enum.Enum):
    """Script execution frequency enumeration."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONCE = "once"
    MANUAL = "manual"


class DataScript(Base):
    """
    Data acquisition script metadata model.

    Represents a script that fetches data from akshare or other sources
    and stores it in the database.

    Attributes:
        id: Primary key
        script_id: Unique script identifier (e.g., 'stock_zh_a_hist_em')
        script_name: Display name
        category: Category (stocks/funds/futures/etc.)
        sub_category: Sub-category (daily/weekly/monthly/hourly)
        frequency: Execution frequency
        description: Script description
        source: Data source (default: 'akshare')
        target_table: Target database table name
        module_path: Python module path for import
        function_name: Function name to execute
        dependencies: Script dependencies (JSON)
        estimated_duration: Estimated execution time in seconds
        timeout: Timeout in seconds (0 = unlimited)
        is_active: Whether script is enabled
        created_at: Creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "data_scripts"
    __table_args__ = {
        "extend_existing": True,
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    script_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    script_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    sub_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    frequency: Mapped[ScriptFrequency] = mapped_column(
        Enum(ScriptFrequency),
        nullable=True,
        default=ScriptFrequency.DAILY,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="akshare", nullable=False)
    target_table: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    module_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    function_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dependencies: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    estimated_duration: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    timeout: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "script_id": self.script_id,
            "script_name": self.script_name,
            "category": self.category,
            "sub_category": self.sub_category,
            "frequency": self.frequency.value if self.frequency else None,
            "description": self.description,
            "source": self.source,
            "target_table": self.target_table,
            "module_path": self.module_path,
            "function_name": self.function_name,
            "estimated_duration": self.estimated_duration,
            "timeout": self.timeout,
            "is_active": self.is_active,
            "is_custom": self.is_custom,
            "parameters": self.dependencies,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<DataScript(script_id={self.script_id}, name={self.script_name})>"
