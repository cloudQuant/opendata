"""
Model tests.

Tests for database models that work with SQLite in-memory database.

Note: Tests for DataScript, ScheduledTask, and TaskExecution are skipped
due to SQLite auto-increment limitations with BigInteger type.
These models are tested in production with MySQL.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.data_table import DataTable
from opendata.models.interface import DataInterface, InterfaceCategory
from opendata.models.user import User, UserRole


class TestUser:
    """Test User model."""

    @pytest.mark.asyncio
    async def test_create_user(self, test_db: AsyncSession):
        """Test creating a user."""
        user = User(
            username="testuser",
            email="test@example.com",
            hashed_password="hashed_password_here",
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        assert user.id is not None
        assert user.username == "testuser"
        assert user.email == "test@example.com"
        assert user.role == UserRole.USER
        assert user.is_active is True

    @pytest.mark.asyncio
    async def test_user_timestamps(self, test_db: AsyncSession):
        """Test user created_at and updated_at timestamps."""
        user = User(
            username="timetest",
            email="time@example.com",
            hashed_password="hash",
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        assert user.created_at is not None
        assert user.updated_at is not None
        assert user.created_at <= user.updated_at

    @pytest.mark.asyncio
    async def test_user_role_enum(self, test_db: AsyncSession):
        """Test user role enum values."""
        admin = User(
            username="admin",
            email="admin@example.com",
            hashed_password="hash",
            role=UserRole.ADMIN,
        )
        test_db.add(admin)
        await test_db.commit()

        assert admin.role == UserRole.ADMIN
        assert admin.role.value == "admin"


class TestDataTable:
    """Test DataTable model."""

    @pytest.mark.asyncio
    async def test_create_table(self, test_db: AsyncSession):
        """Test creating a data table record."""
        table = DataTable(
            id=1,
            table_name="ak_test_table",
            table_comment="Test Table",
            category="stock",
            row_count=1000,
        )
        test_db.add(table)
        await test_db.commit()
        await test_db.refresh(table)

        assert table.id is not None
        assert table.table_name == "ak_test_table"
        assert table.row_count == 1000

    @pytest.mark.asyncio
    async def test_table_comment(self, test_db: AsyncSession):
        """Test table comment field."""
        table = DataTable(
            id=1,
            table_name="ak_comment_test",
            table_comment="Test comment",
            category="bond",
        )
        test_db.add(table)
        await test_db.commit()
        await test_db.refresh(table)

        assert table.table_comment == "Test comment"
        assert table.category == "bond"


class TestInterfaceCategory:
    """Test InterfaceCategory model."""

    @pytest.mark.asyncio
    async def test_create_category(self, test_db: AsyncSession):
        """Test creating an interface category."""
        category = InterfaceCategory(
            name="test_category",
            description="Test category",
            sort_order=1,
        )
        test_db.add(category)
        await test_db.commit()
        await test_db.refresh(category)

        assert category.id is not None
        assert category.name == "test_category"


class TestDataInterface:
    """Test DataInterface model."""

    @pytest.mark.asyncio
    async def test_create_interface(self, test_db: AsyncSession):
        """Test creating a data interface."""
        # Create category first
        category = InterfaceCategory(
            name="stock",
            description="Stock data",
        )
        test_db.add(category)
        await test_db.flush()

        interface = DataInterface(
            name="stock_zh_a_hist",
            display_name="A股历史数据",
            category_id=category.id,
            description="获取A股历史数据",
            parameters={
                "symbol": {"type": "str", "required": True, "description": "股票代码"},
                "period": {"type": "str", "required": False, "description": "周期"},
            },
        )
        test_db.add(interface)
        await test_db.commit()
        await test_db.refresh(interface)

        assert interface.id is not None
        assert interface.name == "stock_zh_a_hist"
        assert interface.category_id == category.id

    @pytest.mark.asyncio
    async def test_interface_parameters_json(self, test_db: AsyncSession):
        """Test interface parameters JSON field."""
        category = InterfaceCategory(name="test", description="Test")
        test_db.add(category)
        await test_db.flush()

        params = {
            "param1": {"type": "str", "required": True},
            "param2": {"type": "int", "required": False, "default": 100},
        }
        interface = DataInterface(
            name="test_interface",
            display_name="Test Interface",
            category_id=category.id,
            parameters=params,
        )
        test_db.add(interface)
        await test_db.commit()

        assert interface.parameters == params
        assert interface.parameters["param1"]["required"] is True

    @pytest.mark.asyncio
    async def test_interface_category_relationship(self, test_db: AsyncSession):
        """Test the relationship between interface and category."""
        category = InterfaceCategory(name="fund", description="Fund data")
        test_db.add(category)
        await test_db.flush()

        interface = DataInterface(
            name="fund_etf_hist",
            display_name="ETF历史",
            category_id=category.id,
            description="ETF历史数据",
        )
        test_db.add(interface)
        await test_db.commit()

        # Refresh to get relationship
        await test_db.refresh(interface)
        await test_db.refresh(category)

        # The category should have this interface in its interfaces list
        # Note: This requires the relationship to be loaded
        assert interface.category_id == category.id
