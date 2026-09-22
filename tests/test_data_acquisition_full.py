"""
Comprehensive tests for data acquisition service.

Covers DataAcquisitionService methods including progress tracking and cancellation.
"""

from datetime import UTC, datetime

from opendata.services.data_acquisition import DataAcquisitionService


class TestDataAcquisitionService:
    """Test DataAcquisitionService."""

    def test_init(self):
        """Test service initialization."""
        service = DataAcquisitionService()
        assert service._active_executions == {}

    def test_get_progress_not_found(self):
        """Test getting progress for non-existent execution."""
        service = DataAcquisitionService()
        result = service.get_progress(999)
        assert result["status"] == "not_found"
        assert result["progress"] == 0

    def test_get_progress_running(self):
        """Test getting progress for running execution."""
        service = DataAcquisitionService()
        service._active_executions[1] = {
            "interface_id": 10,
            "parameters": {},
            "started_at": datetime.now(UTC),
        }
        result = service.get_progress(1)
        assert result["status"] == "running"
        assert result["interface_id"] == 10

    def test_cancel_execution_success(self):
        """Test cancelling an active execution."""
        service = DataAcquisitionService()
        service._active_executions[1] = {"interface_id": 10}
        assert service.cancel_execution(1) is True
        assert 1 not in service._active_executions

    def test_cancel_execution_not_found(self):
        """Test cancelling non-existent execution."""
        service = DataAcquisitionService()
        assert service.cancel_execution(999) is False

    def test_generate_table_name(self):
        """Test table name generation."""
        service = DataAcquisitionService()
        assert service._generate_table_name("stock_zh_a_hist") == "ak_stock_zh_a_hist"
        assert service._generate_table_name("stock.hist") == "ak_stock_hist"

    def test_clean_column_names(self):
        """Test column name cleaning."""
        service = DataAcquisitionService()
        result = service._clean_column_names(["Name", "Price (USD)", "涨跌幅"])
        assert result[0] == "name"
        assert all(len(c) <= 64 for c in result)

    def test_clean_column_names_long(self):
        """Test column name cleaning with long names."""
        service = DataAcquisitionService()
        result = service._clean_column_names(["a" * 100])
        assert len(result[0]) <= 64
