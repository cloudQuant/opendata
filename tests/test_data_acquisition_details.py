"""
Data acquisition service detailed tests.

Tests for DataAcquisitionService methods.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


class TestDataAcquisitionExecuteDownload:
    """Test DataAcquisitionService execute_download method."""

    @pytest.mark.asyncio
    async def test_execute_download_interface_not_found(self, test_db: AsyncSession):
        """Test execute_download with non-existent interface."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        with pytest.raises(ValueError, match="Interface .* not found"):
            await service.execute_download(
                execution_id=1,
                interface_id=999,
                parameters={},
                db=test_db,
            )


class TestDataAcquisitionStoreData:
    """Test DataAcquisitionService _store_data method."""

    @pytest.mark.asyncio
    async def test_store_data_creates_table(self, test_db: AsyncSession):
        """Test _store_data creates table record."""
        import pandas as pd

        from opendata.models.interface import DataInterface, InterfaceCategory
        from opendata.services.data_acquisition import DataAcquisitionService

        # Create category and interface
        category = InterfaceCategory(name="test", description="Test")
        test_db.add(category)
        await test_db.flush()

        interface = DataInterface(
            name="test_interface",
            display_name="Test Interface",
            category_id=category.id,
            parameters={},
        )
        test_db.add(interface)
        await test_db.flush()

        service = DataAcquisitionService()

        # Create test data
        data = pd.DataFrame(
            {
                "col1": [1, 2, 3],
                "col2": ["a", "b", "c"],
            }
        )

        with (
            patch.object(service, "_create_table_if_not_exists"),
            patch.object(service, "_insert_data", return_value=3),
            patch.object(service, "_update_table_metadata"),  # Mock this to avoid table query
        ):
            rows_affected = await service._store_data(
                data=data,
                interface=interface,
                execution_id=1,
                db=test_db,
            )

        assert rows_affected == 3


class TestDataAcquisitionHelpers:
    """Test DataAcquisitionService helper methods."""

    @pytest.mark.asyncio
    async def test_generate_table_name_with_dots(self):
        """Test table name generation with dots."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        result = service._generate_table_name("stock.zh.a.hist")
        assert result == "ak_stock_zh_a_hist"

    @pytest.mark.asyncio
    async def test_generate_table_name_with_dashes(self):
        """Test table name generation with dashes."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        result = service._generate_table_name("fund-etf-hist")
        assert result == "ak_fund_etf_hist"

    @pytest.mark.asyncio
    async def test_clean_column_names_handles_special_chars(self):
        """Test column name cleaning with special characters."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        columns = ["Column-With-Dashes", "Column With Spaces", "Column.With.Dots"]
        cleaned = service._clean_column_names(columns)

        assert "column_with_dashes" in cleaned
        assert "column_with_spaces" in cleaned
        assert "column_with_dots" in cleaned

    @pytest.mark.asyncio
    async def test_clean_column_names_truncates_long_names(self):
        """Test column name cleaning truncates long names."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        long_name = "a" * 100
        columns = [long_name]
        cleaned = service._clean_column_names(columns)

        assert len(cleaned[0]) == 64


class TestDataAcquisitionProgress:
    """Test DataAcquisitionService progress tracking."""

    @pytest.mark.asyncio
    async def test_get_progress_non_existent(self):
        """Test getting progress for non-existent execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        progress = service.get_progress(999)

        assert progress == {
            "execution_id": 999,
            "status": "not_found",
            "progress": 0,
        }

    @pytest.mark.asyncio
    async def test_get_progress_active(self):
        """Test getting progress for active execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        # Manually add active execution
        service._active_executions[1] = {
            "interface_id": 1,
            "parameters": {},
            "started_at": None,
        }

        progress = service.get_progress(1)

        assert progress["execution_id"] == 1
        assert progress["status"] == "running"


class TestDataAcquisitionCancel:
    """Test DataAcquisitionService cancel operations."""

    @pytest.mark.asyncio
    async def test_cancel_execution(self):
        """Test cancelling an execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        # Add active execution
        service._active_executions[1] = {
            "interface_id": 1,
            "parameters": {},
            "started_at": None,
        }

        result = service.cancel_execution(1)

        assert result is True
        assert 1 not in service._active_executions

    @pytest.mark.asyncio
    async def test_cancel_non_existent_execution(self):
        """Test cancelling non-existent execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        result = service.cancel_execution(999)

        assert result is False
