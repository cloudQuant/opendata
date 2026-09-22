"""
Script service detailed tests.

Tests for ScriptService methods.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


class TestScriptServiceCreate:
    """Test ScriptService create operations."""

    @pytest.mark.asyncio
    async def test_create_script(self, test_db: AsyncSession):
        """Test creating a script."""
        from opendata.models.data_script import ScriptFrequency
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        script = await service.create_script(
            script_id="test_script_001",
            script_name="Test Script",
            category="test",
            module_path="test.module",
            frequency=ScriptFrequency.DAILY,
            description="Test description",
        )

        assert script is not None
        assert script.script_id == "test_script_001"
        assert script.script_name == "Test Script"
        assert script.category == "test"

    @pytest.mark.asyncio
    async def test_create_script_duplicate(self, test_db: AsyncSession):
        """Test creating duplicate script raises error."""
        from sqlalchemy.exc import IntegrityError

        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        # Create first script
        await service.create_script(
            script_id="test_script_002",
            script_name="Test Script",
            category="test",
            module_path="test.module",
        )

        # Try to create duplicate - should raise IntegrityError
        with pytest.raises(IntegrityError):
            await service.create_script(
                script_id="test_script_002",
                script_name="Another Name",
                category="other",
                module_path="other.module",
            )


class TestScriptServiceUpdate:
    """Test ScriptService update operations."""

    @pytest.mark.asyncio
    async def test_update_script(self, test_db: AsyncSession):
        """Test updating a script."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        # Create script first
        script = await service.create_script(
            script_id="test_script_003",
            script_name="Original Name",
            category="test",
            module_path="test.module",
        )

        # Update script
        updated = await service.update_script("test_script_003", script_name="Updated Name")

        assert updated is not None
        assert updated.script_name == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_script_not_found(self, test_db: AsyncSession):
        """Test updating non-existent script."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        updated = await service.update_script("nonexistent", script_name="New Name")

        assert updated is None


class TestScriptServiceDelete:
    """Test ScriptService delete operations."""

    @pytest.mark.asyncio
    async def test_delete_script(self, test_db: AsyncSession):
        """Test deleting a script."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        # Create script first
        script = await service.create_script(
            script_id="test_script_004",
            script_name="To Delete",
            category="test",
            module_path="test.module",
        )

        # Delete script
        result = await service.delete_script("test_script_004")

        assert result is True

        # Verify deleted
        found = await service.get_script("test_script_004")
        assert found is None

    @pytest.mark.asyncio
    async def test_delete_script_not_found(self, test_db: AsyncSession):
        """Test deleting non-existent script."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        result = await service.delete_script("nonexistent")

        assert result is False


class TestScriptServiceToggle:
    """Test ScriptService toggle operations."""

    @pytest.mark.asyncio
    async def test_toggle_script_active(self, test_db: AsyncSession):
        """Test toggling script active status."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        # Create script first
        script = await service.create_script(
            script_id="test_script_005",
            script_name="Toggle Test",
            category="test",
            module_path="test.module",
        )

        # Verify initially active
        assert script.is_active is True

        # Toggle to inactive
        result = await service.toggle_script("test_script_005", active=False)
        assert result is True

        # Verify the change
        updated = await service.get_script("test_script_005")
        assert updated.is_active is False

        # Toggle back to active
        result = await service.toggle_script("test_script_005", active=True)
        assert result is True

        # Verify the change
        updated = await service.get_script("test_script_005")
        assert updated.is_active is True


class TestScriptServiceCategories:
    """Test ScriptService category operations."""

    @pytest.mark.asyncio
    async def test_get_categories(self, test_db: AsyncSession):
        """Test getting all categories."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)

        # Create scripts in different categories
        await service.create_script(
            script_id="test_stocks_001",
            script_name="Stocks Test",
            category="stocks",
            module_path="stocks.test",
        )
        await service.create_script(
            script_id="test_funds_001",
            script_name="Funds Test",
            category="funds",
            module_path="funds.test",
        )

        categories = await service.get_categories()

        # Returns list of strings
        assert len(categories) >= 2
        assert "stocks" in categories
        assert "funds" in categories
