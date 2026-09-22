"""
Models module comprehensive tests.

Comprehensive tests for all database models.
"""


class TestUserModel:
    """Test User model."""

    def test_user_model_exists(self):
        """Test User model exists."""
        from opendata.models.user import User

        assert User is not None

    def test_user_role_enum(self):
        """Test UserRole enum."""
        from opendata.models.user import UserRole

        assert UserRole.ADMIN is not None
        assert UserRole.USER is not None

    def test_user_role_values(self):
        """Test UserRole enum values."""
        from opendata.models.user import UserRole

        assert UserRole.ADMIN.value == "admin"
        assert UserRole.USER.value == "user"

    def test_user_has_timestamps(self):
        """Test User has timestamp fields."""
        from opendata.models.user import User

        assert hasattr(User, "created_at")
        assert hasattr(User, "updated_at")


class TestDataTableModel:
    """Test DataTable model."""

    def test_table_model_exists(self):
        """Test DataTable model exists."""
        from opendata.models.data_table import DataTable

        assert DataTable is not None

    def test_table_has_table_comment(self):
        """Test DataTable has table_comment field."""
        from opendata.models.data_table import DataTable

        assert hasattr(DataTable, "table_comment")

    def test_table_has_size_fields(self):
        """Test DataTable has size fields."""
        from opendata.models.data_table import DataTable

        assert hasattr(DataTable, "row_count")
        assert hasattr(DataTable, "category")


class TestInterfaceCategoryModel:
    """Test InterfaceCategory model."""

    def test_category_model_exists(self):
        """Test InterfaceCategory model exists."""
        from opendata.models.interface import InterfaceCategory

        assert InterfaceCategory is not None

    def test_category_has_sort_order(self):
        """Test InterfaceCategory has sort_order."""
        from opendata.models.interface import InterfaceCategory

        assert hasattr(InterfaceCategory, "sort_order")


class TestInterfaceParameterModel:
    """Test InterfaceParameter model."""

    def test_parameter_model_exists(self):
        """Test InterfaceParameter model exists."""
        from opendata.models.interface import InterfaceParameter

        assert InterfaceParameter is not None

    def test_parameter_has_type_field(self):
        """Test InterfaceParameter has type field."""
        from opendata.models.interface import InterfaceParameter

        assert hasattr(InterfaceParameter, "param_type")

    def test_parameter_type_enum(self):
        """Test ParameterType enum."""
        from opendata.models.interface import ParameterType

        assert ParameterType.STRING is not None
        assert ParameterType.INTEGER is not None


class TestScheduledTaskModel:
    """Test ScheduledTask model."""

    def test_task_model_exists(self):
        """Test ScheduledTask model exists."""
        from opendata.models.task import ScheduledTask

        assert ScheduledTask is not None

    def test_task_has_retry_config(self):
        """Test ScheduledTask has retry configuration."""
        from opendata.models.task import ScheduledTask

        assert hasattr(ScheduledTask, "retry_on_failure")
        assert hasattr(ScheduledTask, "max_retries")

    def test_task_has_execution_fields(self):
        """Test ScheduledTask has execution tracking."""
        from opendata.models.task import ScheduledTask

        assert hasattr(ScheduledTask, "last_execution_at")


class TestTaskExecutionModel:
    """Test TaskExecution model."""

    def test_execution_model_exists(self):
        """Test TaskExecution model exists."""
        from opendata.models.task import TaskExecution

        assert TaskExecution is not None

    def test_execution_has_result(self):
        """Test TaskExecution has result field."""
        from opendata.models.task import TaskExecution

        assert hasattr(TaskExecution, "result")

    def test_execution_has_error_info(self):
        """Test TaskExecution has error fields."""
        from opendata.models.task import TaskExecution

        assert hasattr(TaskExecution, "error_message")

    def test_execution_has_retry_count(self):
        """Test TaskExecution has retry_count."""
        from opendata.models.task import TaskExecution

        assert hasattr(TaskExecution, "retry_count")


class TestDataScriptModel:
    """Test DataScript model."""

    def test_script_model_exists(self):
        """Test DataScript model exists."""
        from opendata.models.data_script import DataScript

        assert DataScript is not None

    def test_script_has_frequency(self):
        """Test DataScript has frequency."""
        from opendata.models.data_script import DataScript

        assert hasattr(DataScript, "frequency")

    def test_script_has_config(self):
        """Test DataScript has configuration."""
        from opendata.models.data_script import DataScript

        assert hasattr(DataScript, "estimated_duration")
        assert hasattr(DataScript, "timeout")


class TestScriptFrequencyEnum:
    """Test ScriptFrequency enum."""

    def test_frequency_enum_exists(self):
        """Test ScriptFrequency enum exists."""
        from opendata.models.data_script import ScriptFrequency

        assert ScriptFrequency is not None

    def test_frequency_values(self):
        """Test ScriptFrequency has expected values."""
        from opendata.models.data_script import ScriptFrequency

        assert hasattr(ScriptFrequency, "DAILY")
        assert hasattr(ScriptFrequency, "HOURLY")
        assert hasattr(ScriptFrequency, "MANUAL")


class TestModelsInit:
    """Test models module initialization."""

    def test_models_module_has_all_models(self):
        """Test models module exports all expected models."""
        from opendata.models import (
            DataInterface,
            DataScript,
            DataTable,
            InterfaceCategory,
            InterfaceParameter,
            ScheduledTask,
            ScriptFrequency,
            TaskExecution,
            TaskStatus,
            User,
            UserRole,
        )

        assert User is not None
        assert UserRole is not None
        assert DataTable is not None
        assert DataInterface is not None
        assert InterfaceCategory is not None
        assert InterfaceParameter is not None
        assert ScheduledTask is not None
        assert TaskExecution is not None
        assert TaskStatus is not None
        assert DataScript is not None
        assert ScriptFrequency is not None


class TestTaskStatusEnum:
    """Test TaskStatus enum."""

    def test_status_enum_exists(self):
        """Test TaskStatus enum exists."""
        from opendata.models.task import TaskStatus

        assert TaskStatus is not None

    def test_status_values(self):
        """Test TaskStatus has expected values."""
        from opendata.models.task import TaskStatus

        expected = ["PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"]
        for status in expected:
            assert hasattr(TaskStatus, status)
