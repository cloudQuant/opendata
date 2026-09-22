"""
Service layer tests.

Tests for service methods that don't require complex model relationships.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


class TestDataAcquisitionService:
    """Test DataAcquisitionService utility methods."""

    @pytest.mark.asyncio
    async def test_initialization(self, test_db: AsyncSession):
        """Test service initialization."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()
        assert service._active_executions == {}

    @pytest.mark.asyncio
    async def test_generate_table_name(self, test_db: AsyncSession):
        """Test table name generation."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        assert service._generate_table_name("stock_zh_a_hist") == "ak_stock_zh_a_hist"
        assert service._generate_table_name("fund.etf.fetch") == "ak_fund_etf_fetch"

    @pytest.mark.asyncio
    async def test_clean_column_names(self, test_db: AsyncSession):
        """Test column name cleaning."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        columns = ["Column Name", "Another-Column", "Test"]
        cleaned = service._clean_column_names(columns)

        assert cleaned == ["column_name", "another_column", "test"]


class TestRetryService:
    """Test RetryService non-database methods."""

    def test_calculate_retry_delay(self):
        """Test retry delay calculation."""
        from opendata.services.retry_service import RetryService

        service = RetryService(None)

        # Test exponential backoff
        assert service.calculate_retry_delay(0) == 60
        assert service.calculate_retry_delay(1) == 120
        assert service.calculate_retry_delay(2) == 240
        assert service.calculate_retry_delay(3) == 480

        # Test max delay cap
        assert service.calculate_retry_delay(10) == 3600  # Capped at MAX_RETRY_DELAY


class TestScriptService:
    """Test ScriptService basic operations."""

    @pytest.mark.asyncio
    async def test_get_scripts_empty(self, test_db: AsyncSession):
        """Test getting scripts when none exist."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)
        scripts, total = await service.get_scripts()

        assert total == 0
        assert scripts == []

    @pytest.mark.asyncio
    async def test_get_script_not_found(self, test_db: AsyncSession):
        """Test getting a non-existent script."""
        from opendata.services.script_service import ScriptService

        service = ScriptService(test_db)
        found = await service.get_script("nonexistent")

        assert found is None


class TestExecutionService:
    """Test ExecutionService methods that work with simple models."""

    @pytest.mark.asyncio
    async def test_get_execution_stats_empty(self, test_db: AsyncSession):
        """Test stats with no executions."""
        from opendata.services.execution_service import ExecutionService

        service = ExecutionService(test_db)
        stats = await service.get_execution_stats()

        assert stats["total_count"] == 0
        assert stats["success_count"] == 0
        assert stats["failed_count"] == 0
        assert stats["success_rate"] == 0
        assert stats["today_executions"] == 0

    @pytest.mark.asyncio
    async def test_get_failed_executions_empty(self, test_db: AsyncSession):
        """Test getting failed executions when none exist."""
        from opendata.services.execution_service import ExecutionService

        service = ExecutionService(test_db)
        failed = await service.get_failed_executions()

        assert failed == []

    @pytest.mark.asyncio
    async def test_delete_executions_by_status_empty(self, test_db: AsyncSession):
        """Test deleting executions when none exist."""
        from opendata.models.task import TaskStatus
        from opendata.services.execution_service import ExecutionService

        service = ExecutionService(test_db)
        count = await service.delete_executions_by_status(TaskStatus.FAILED)

        assert count == 0
