"""
Akshare data management ORM models.
"""

import enum
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, TypeAlias

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.models.data_governance import DgDatasetStorage

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


class ScriptFrequency(str, enum.Enum):
    """Script execution frequency."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ONCE = "once"
    MANUAL = "manual"


class TaskStatus(str, enum.Enum):
    """Task execution status."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ScheduleType(str, enum.Enum):
    """Task schedule type."""

    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CRON = "cron"
    INTERVAL = "interval"


class TriggeredBy(str, enum.Enum):
    """Execution trigger type."""

    SCHEDULER = "scheduler"
    MANUAL = "manual"
    API = "api"


class ParameterType(str, enum.Enum):
    """Interface parameter type."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    LIST = "list"
    OPTION = "option"


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Persist string enum values so ORM types match Alembic's lowercase labels."""
    return [str(member.value) for member in enum_class]


class DataScript(Base):
    """Metadata for akshare-backed data scripts."""

    __tablename__ = "ak_data_scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    script_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    script_name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    sub_category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    frequency: Mapped[ScriptFrequency | None] = mapped_column(
        Enum(ScriptFrequency, values_callable=_enum_values),
        nullable=True,
        default=ScriptFrequency.DAILY,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="akshare", nullable=False)
    target_table: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    module_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    function_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dependencies: Mapped[JSONValue | None] = mapped_column(JSON, nullable=True)
    estimated_duration: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    timeout: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    tasks: Mapped[list["ScheduledTask"]] = relationship("ScheduledTask", back_populates="script")


class DataTable(Base):
    """Metadata describing tables stored in the akshare data warehouse."""

    __tablename__ = "ak_data_tables"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    table_comment: Mapped[str | None] = mapped_column(String(200), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    script_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    dataset_storage_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "dg_dataset_storages.id",
            name="fk_ak_data_tables_dataset_storage_id_dg_dataset_storages",
        ),
        nullable=True,
        index=True,
    )
    row_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_update_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_update_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    data_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    symbol_raw: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    symbol_normalized: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    market: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    asset_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    metadata_json: Mapped[dict[str, JSONValue]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
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


class InterfaceCategory(Base):
    """Category used to group akshare interfaces."""

    __tablename__ = "ak_interface_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    interfaces: Mapped[list["DataInterface"]] = relationship(
        "DataInterface", back_populates="category"
    )


class DataInterface(Base):
    """Browsable akshare interface definition."""

    __tablename__ = "ak_data_interfaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ak_interface_categories.id"), nullable=False
    )
    module_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    function_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parameters: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    extra_config: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    return_type: Mapped[str] = mapped_column(String(50), default="DataFrame", nullable=False)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    category: Mapped["InterfaceCategory"] = relationship(
        "InterfaceCategory", back_populates="interfaces"
    )
    params: Mapped[list["InterfaceParameter"]] = relationship(
        "InterfaceParameter",
        back_populates="interface",
        cascade="all, delete-orphan",
    )


class InterfaceParameter(Base):
    """Parameter definition for a data interface."""

    __tablename__ = "ak_interface_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interface_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ak_data_interfaces.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    param_type: Mapped[ParameterType] = mapped_column(
        Enum(ParameterType, values_callable=_enum_values),
        default=ParameterType.STRING,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    options: Mapped[JSONValue | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    interface: Mapped["DataInterface"] = relationship("DataInterface", back_populates="params")


class ScheduledTask(Base):
    """Scheduled task metadata for akshare scripts."""

    __tablename__ = "ak_scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    script_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("ak_data_scripts.script_id"), nullable=False
    )
    schedule_type: Mapped[ScheduleType] = mapped_column(
        Enum(ScheduleType, values_callable=_enum_values),
        default=ScheduleType.DAILY,
        nullable=False,
    )
    schedule_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, JSONValue]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retry_on_failure: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    timeout: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_execution_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_execution_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    script: Mapped["DataScript"] = relationship("DataScript", back_populates="tasks")
    executions: Mapped[list["TaskExecution"]] = relationship(
        "TaskExecution",
        back_populates="task",
        cascade="all, delete-orphan",
    )


class TaskExecution(Base):
    """Execution record for a scheduled or manual akshare task run."""

    __tablename__ = "ak_task_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("ak_scheduled_tasks.id"), nullable=True, index=True
    )
    script_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    params: Mapped[JSONValue | None] = mapped_column(JSON, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, values_callable=_enum_values),
        default=TaskStatus.PENDING,
        nullable=False,
        index=True,
    )
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[JSONValue | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    triggered_by: Mapped[TriggeredBy] = mapped_column(
        Enum(TriggeredBy, values_callable=_enum_values),
        default=TriggeredBy.SCHEDULER,
        nullable=False,
    )
    operator_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, index=True
    )
    airflow_dag_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    airflow_run_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    airflow_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    task: Mapped["ScheduledTask"] = relationship("ScheduledTask", back_populates="executions")


__all__ = [
    "DataInterface",
    "DataScript",
    "DataTable",
    "InterfaceCategory",
    "InterfaceParameter",
    "ParameterType",
    "ScheduleType",
    "ScheduledTask",
    "ScriptFrequency",
    "TaskExecution",
    "TaskStatus",
    "TriggeredBy",
]
