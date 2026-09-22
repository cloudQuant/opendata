"""
Data acquisition service tests.

Tests for the data acquisition service module.
"""

from unittest.mock import AsyncMock


class TestDataAcquisitionService:
    """Test DataAcquisitionService class."""

    def test_service_exists(self):
        """Test DataAcquisitionService exists."""
        from opendata.services.data_acquisition import DataAcquisitionService

        assert DataAcquisitionService is not None

    def test_service_initialization(self):
        """Test DataAcquisitionService can be initialized."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()
        assert service is not None


class TestAkshareProvider:
    """Test akshare provider functionality."""

    def test_provider_exists(self):
        """Test akshare provider exists."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        assert AkshareProvider is not None

    def test_provider_initialization(self):
        """Test provider can be initialized."""
        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

        provider = AkshareProvider()
        assert provider is not None


class TestScriptService:
    """Test ScriptService functionality."""

    def test_service_exists(self):
        """Test ScriptService exists."""
        from opendata.services.script_service import ScriptService

        assert ScriptService is not None

    def test_service_initialization(self):
        """Test ScriptService can be initialized."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        assert service is not None


class TestExecutionService:
    """Test ExecutionService functionality."""

    def test_service_exists(self):
        """Test ExecutionService exists."""
        from opendata.services.execution_service import ExecutionService

        assert ExecutionService is not None

    def test_service_initialization(self):
        """Test ExecutionService can be initialized."""
        from opendata.services.execution_service import ExecutionService

        mock_db = AsyncMock()
        service = ExecutionService(mock_db)

        assert service is not None


class TestTaskModel:
    """Test Task model."""

    def test_task_model_exists(self):
        """Test ScheduledTask model exists."""
        from opendata.models.task import ScheduledTask

        assert ScheduledTask is not None

    def test_task_has_required_fields(self):
        """Test Task has required fields."""
        from opendata.models.task import ScheduledTask

        # Check model has expected attributes
        expected_attrs = [
            "id",
            "name",
            "script_id",
            "schedule_type",
            "schedule_expression",
            "is_active",
            "created_at",
        ]

        for attr in expected_attrs:
            assert hasattr(ScheduledTask, attr)

    def test_task_status_enum(self):
        """Test TaskStatus enum exists."""
        from opendata.models.task import TaskStatus

        assert TaskStatus is not None
        assert hasattr(TaskStatus, "__members__")
        assert len(TaskStatus.__members__) > 0


class TestSchedulerModule:
    """Test scheduler module."""

    def test_scheduler_module_exists(self):
        """Test scheduler module exists."""
        from opendata.services import scheduler

        assert scheduler is not None


class TestRetryServiceModule:
    """Test retry service module."""

    def test_retry_service_exists(self):
        """Test RetryService exists."""
        from opendata.services.retry_service import RetryService

        assert RetryService is not None


class TestTaskExecutionModel:
    """Test TaskExecution model."""

    def test_execution_model_exists(self):
        """Test TaskExecution model exists."""
        from opendata.models.task import TaskExecution

        assert TaskExecution is not None

    def test_execution_has_fields(self):
        """Test TaskExecution has expected fields."""
        from opendata.models.task import TaskExecution

        expected_fields = [
            "execution_id",
            "task_id",
            "status",
            "start_time",
            "end_time",
            "created_at",
        ]

        for field in expected_fields:
            assert hasattr(TaskExecution, field)


class TestDataScriptModel:
    """Test DataScript model."""

    def test_script_model_exists(self):
        """Test DataScript model exists."""
        from opendata.models.data_script import DataScript

        assert DataScript is not None

    def test_script_has_fields(self):
        """Test DataScript has expected fields."""
        from opendata.models.data_script import DataScript

        expected_fields = [
            "script_id",
            "script_name",
            "category",
            "frequency",
            "is_active",
            "created_at",
        ]

        for field in expected_fields:
            assert hasattr(DataScript, field)


class TestDataTableModel:
    """Test DataTable model."""

    def test_table_model_exists(self):
        """Test DataTable model exists."""
        from opendata.models.data_table import DataTable

        assert DataTable is not None

    def test_table_has_fields(self):
        """Test DataTable has expected fields."""
        from opendata.models.data_table import DataTable

        expected_fields = ["id", "table_name", "table_comment", "row_count", "created_at"]

        for field in expected_fields:
            assert hasattr(DataTable, field)


class TestInterfaceModel:
    """Test DataInterface model."""

    def test_interface_model_exists(self):
        """Test DataInterface model exists."""
        from opendata.models.interface import DataInterface

        assert DataInterface is not None

    def test_interface_has_fields(self):
        """Test DataInterface has expected fields."""
        from opendata.models.interface import DataInterface

        expected_fields = ["id", "name", "display_name", "category", "is_active", "created_at"]

        for field in expected_fields:
            assert hasattr(DataInterface, field)
