"""
Data interface models for managing akshare interfaces.

Defines models for data interfaces, categories, and parameters.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opendata.core.database import Base


class ParameterType(str, enum.Enum):
    """Parameter type enumeration."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    LIST = "list"
    OPTION = "option"


class InterfaceCategory(Base):
    """
    Interface category for grouping data interfaces.

    Attributes:
        id: Primary key
        name: Category identifier (e.g., 'stock', 'fund')
        description: Category description
        icon: Icon identifier for UI
        sort_order: Display order
        interfaces: Related data interfaces
    """

    __tablename__ = "interface_categories"
    __table_args__ = {
        "extend_existing": True,
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    interfaces: Mapped[list["DataInterface"]] = relationship(
        back_populates="category",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<InterfaceCategory(id={self.id}, name={self.name})>"


class DataInterface(Base):
    """
    Data interface model representing an akshare data function.

    Attributes:
        id: Primary key
        name: Interface name (function name)
        display_name: Display name for UI
        description: Interface description
        category_id: Foreign key to category
        module_path: Python module path
        function_name: Function name to call
        parameters: JSON schema of parameters
        return_type: Expected return type
        example: Usage example
        is_active: Whether interface is available
        created_at: Creation timestamp
    """

    __tablename__ = "data_interfaces"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    category_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("interface_categories.id"),
        nullable=False,
    )
    module_path: Mapped[str] = mapped_column(String(255), nullable=True)
    function_name: Mapped[str] = mapped_column(String(100), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    return_type: Mapped[str] = mapped_column(String(50), default="DataFrame")
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    category: Mapped["InterfaceCategory"] = relationship(back_populates="interfaces")
    params: Mapped[list["InterfaceParameter"]] = relationship(
        back_populates="interface",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<DataInterface(id={self.id}, name={self.name}, display_name={self.display_name})>"


class InterfaceParameter(Base):
    """
    Interface parameter definition.

    Attributes:
        id: Primary key
        interface_id: Foreign key to interface
        name: Parameter name
        display_name: Display name for UI
        param_type: Parameter type
        description: Parameter description
        default_value: Default value
        required: Whether parameter is required
        options: List of valid options (for OPTION type)
        sort_order: Display order
    """

    __tablename__ = "interface_parameters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    interface_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("data_interfaces.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    param_type: Mapped[ParameterType] = mapped_column(
        Enum(ParameterType),
        default=ParameterType.STRING,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    options: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Relationships
    interface: Mapped["DataInterface"] = relationship(back_populates="params")

    def __repr__(self) -> str:
        return f"<InterfaceParameter(id={self.id}, name={self.name}, type={self.param_type.value})>"
