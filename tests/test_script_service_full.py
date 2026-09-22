"""
Comprehensive tests for script service.

Covers ScriptService CRUD, toggle, categories, stats, scan operations.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.data_script import DataScript, ScriptFrequency
from opendata.services.script_service import ScriptService


class TestScriptServiceGetScripts:
    """Test get_scripts method."""

    @pytest.mark.asyncio
    async def test_get_scripts_no_filter(self, test_db: AsyncSession):
        """Test getting scripts without filters."""
        service = ScriptService(test_db)
        scripts, total = await service.get_scripts()
        assert isinstance(scripts, list)
        assert isinstance(total, int)

    @pytest.mark.asyncio
    async def test_get_scripts_with_category(self, test_db: AsyncSession):
        """Test getting scripts filtered by category."""
        # Create a test script
        script = DataScript(
            script_id="cat_test",
            script_name="Category Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        scripts, total = await service.get_scripts(category="stock")
        assert total >= 1

    @pytest.mark.asyncio
    async def test_get_scripts_with_frequency(self, test_db: AsyncSession):
        """Test getting scripts filtered by frequency."""
        service = ScriptService(test_db)
        scripts, total = await service.get_scripts(frequency=ScriptFrequency.DAILY)
        assert isinstance(total, int)

    @pytest.mark.asyncio
    async def test_get_scripts_with_active_filter(self, test_db: AsyncSession):
        """Test getting scripts filtered by active status."""
        service = ScriptService(test_db)
        scripts, total = await service.get_scripts(is_active=True)
        assert isinstance(total, int)

    @pytest.mark.asyncio
    async def test_get_scripts_with_keyword(self, test_db: AsyncSession):
        """Test getting scripts with keyword search."""
        script = DataScript(
            script_id="keyword_test",
            script_name="Keyword Search Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
            description="A test script for keyword search",
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        scripts, total = await service.get_scripts(keyword="keyword")
        assert total >= 1


class TestScriptServiceCRUD:
    """Test CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_script(self, test_db: AsyncSession):
        """Test creating a script."""
        service = ScriptService(test_db)
        script = await service.create_script(
            script_id="create_test",
            script_name="Create Test",
            category="stock",
            module_path="opendata.data_fetch.scripts.stocks.test",
        )
        assert script.script_id == "create_test"

    @pytest.mark.asyncio
    async def test_get_script(self, test_db: AsyncSession):
        """Test getting a script by ID."""
        script = DataScript(
            script_id="get_test",
            script_name="Get Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        result = await service.get_script("get_test")
        assert result is not None
        assert result.script_id == "get_test"

    @pytest.mark.asyncio
    async def test_get_script_not_found(self, test_db: AsyncSession):
        """Test getting non-existent script."""
        service = ScriptService(test_db)
        result = await service.get_script("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_script(self, test_db: AsyncSession):
        """Test updating a script."""
        script = DataScript(
            script_id="update_test",
            script_name="Original Name",
            category="stock",
            frequency=ScriptFrequency.DAILY,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        updated = await service.update_script("update_test", script_name="Updated Name")
        assert updated is not None
        assert updated.script_name == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_nonexistent_script(self, test_db: AsyncSession):
        """Test updating non-existent script."""
        service = ScriptService(test_db)
        result = await service.update_script("nonexistent", script_name="X")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_script(self, test_db: AsyncSession):
        """Test deleting a script."""
        script = DataScript(
            script_id="delete_test",
            script_name="Delete Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        result = await service.delete_script("delete_test")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_script(self, test_db: AsyncSession):
        """Test deleting non-existent script."""
        service = ScriptService(test_db)
        result = await service.delete_script("nonexistent")
        assert result is False


class TestScriptServiceToggle:
    """Test toggle operations."""

    @pytest.mark.asyncio
    async def test_toggle_script(self, test_db: AsyncSession):
        """Test toggling script active status."""
        script = DataScript(
            script_id="toggle_test",
            script_name="Toggle Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        result = await service.toggle_script("toggle_test")
        assert result is True

    @pytest.mark.asyncio
    async def test_toggle_script_explicit(self, test_db: AsyncSession):
        """Test toggling script to explicit state."""
        script = DataScript(
            script_id="toggle_explicit",
            script_name="Toggle Explicit",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        result = await service.toggle_script("toggle_explicit", active=False)
        assert result is True

    @pytest.mark.asyncio
    async def test_toggle_nonexistent(self, test_db: AsyncSession):
        """Test toggling non-existent script."""
        service = ScriptService(test_db)
        result = await service.toggle_script("nonexistent")
        assert result is False


class TestScriptServiceCategories:
    """Test categories and stats."""

    @pytest.mark.asyncio
    async def test_get_categories(self, test_db: AsyncSession):
        """Test getting categories."""
        script = DataScript(
            script_id="cat_list_test",
            script_name="Category Test",
            category="futures",
            frequency=ScriptFrequency.DAILY,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        categories = await service.get_categories()
        assert isinstance(categories, list)
        assert "futures" in categories

    @pytest.mark.asyncio
    async def test_get_script_stats(self, test_db: AsyncSession):
        """Test getting script statistics."""
        script = DataScript(
            script_id="stats_test",
            script_name="Stats Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        stats = await service.get_script_stats()
        assert "total" in stats
        assert "active" in stats
        assert "inactive" in stats
        assert "by_category" in stats
        assert "by_frequency" in stats


class TestScriptServiceExecute:
    """Test script execution."""

    @pytest.mark.asyncio
    async def test_execute_nonexistent_script(self, test_db: AsyncSession):
        """Test executing non-existent script."""
        service = ScriptService(test_db)
        with pytest.raises(ValueError, match="Script not found"):
            await service.execute_script("nonexistent", "exec-1")

    @pytest.mark.asyncio
    async def test_execute_inactive_script(self, test_db: AsyncSession):
        """Test executing inactive script."""
        script = DataScript(
            script_id="inactive_exec",
            script_name="Inactive",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=False,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        with pytest.raises(ValueError, match="not active"):
            await service.execute_script("inactive_exec", "exec-1")

    @pytest.mark.asyncio
    async def test_execute_no_module_path(self, test_db: AsyncSession):
        """Test executing script without module path."""
        script = DataScript(
            script_id="no_module",
            script_name="No Module",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
            module_path=None,
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        with pytest.raises(ValueError, match="no module_path"):
            await service.execute_script("no_module", "exec-1")

    @pytest.mark.asyncio
    async def test_execute_import_error(self, test_db: AsyncSession):
        """Test executing script with import error."""
        script = DataScript(
            script_id="bad_module",
            script_name="Bad Module",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
            module_path="nonexistent.module.path",
        )
        test_db.add(script)
        await test_db.commit()

        service = ScriptService(test_db)
        result = await service.execute_script("bad_module", "exec-1")
        assert result["success"] is False
        assert result["error"] is not None


class TestScriptServiceSerialize:
    """Test result serialization."""

    def test_serialize_string(self):
        """Test serializing a string result."""
        service = ScriptService(AsyncMock())
        result = service._serialize_result("hello")
        assert result == "hello"

    def test_serialize_dict(self):
        """Test serializing a dict result."""
        service = ScriptService(AsyncMock())
        result = service._serialize_result({"key": "value"})
        assert result == {"key": "value"}

    def test_serialize_none(self):
        """Test serializing None result."""
        service = ScriptService(AsyncMock())
        result = service._serialize_result(None)
        assert result is None

    def test_serialize_dataframe(self):
        """Test serializing DataFrame result."""
        import pandas as pd

        service = ScriptService(AsyncMock())
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        result = service._serialize_result(df)
        assert result["type"] == "DataFrame"
        assert result["rows"] == 2

    def test_serialize_object_with_dict(self):
        """Test serializing object with __dict__."""
        service = ScriptService(AsyncMock())

        class MyObj:
            x = 1

        result = service._serialize_result(MyObj())
        assert isinstance(result, str)


class TestScriptServiceScan:
    """Test script scanning."""

    @pytest.mark.asyncio
    async def test_scan_and_register_empty(self, test_db: AsyncSession):
        """Test scanning with no scripts directory."""
        service = ScriptService(test_db)
        with patch("os.path.exists", return_value=False):
            result = await service.scan_and_register_scripts()
        assert "registered_count" in result
        assert "updated_count" in result

    def test_extract_target_table(self, tmp_path):
        """Test extracting target table from file."""
        service = ScriptService(AsyncMock())

        # Create a temp file with table name
        test_file = tmp_path / "test_script.py"
        test_file.write_text('self.table_name = "ak_stock_data"\n')

        result = service._extract_target_table(str(test_file))
        assert result == "ak_stock_data"

    def test_extract_target_table_not_found(self, tmp_path):
        """Test extracting target table when not present."""
        service = ScriptService(AsyncMock())

        test_file = tmp_path / "test_script.py"
        test_file.write_text("# no table name\n")

        result = service._extract_target_table(str(test_file))
        assert result is None

    def test_extract_target_table_file_not_found(self):
        """Test extracting target table from non-existent file."""
        service = ScriptService(AsyncMock())
        result = service._extract_target_table("/nonexistent/file.py")
        assert result is None
