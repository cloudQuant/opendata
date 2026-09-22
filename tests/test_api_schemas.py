"""
API schemas comprehensive tests.

Comprehensive tests for Pydantic schemas used in API requests/responses.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError


class TestAuthSchemas:
    """Test authentication-related schemas."""

    def test_register_request_valid(self):
        """Test valid RegisterRequest."""
        from opendata.api.schemas import RegisterRequest

        data = RegisterRequest(
            email="test@example.com",
            password="Password1!",
            password_confirm="Password1!",
        )

        assert data.email == "test@example.com"
        assert data.password == "Password1!"

    def test_register_request_passwords_match(self):
        """Test RegisterRequest with matching passwords."""
        from opendata.api.schemas import RegisterRequest

        data = RegisterRequest(
            email="test@example.com",
            password="Password1!",
            password_confirm="Password1!",
        )

        assert data.password == "Password1!"

    def test_register_request_invalid_email(self):
        """Test RegisterRequest with invalid email."""
        from opendata.api.schemas import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(
                email="not-an-email",
                password="Password1!",
                password_confirm="Password1!",
            )

    def test_register_request_weak_password(self):
        """Test RegisterRequest with weak password."""
        from opendata.api.schemas import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(
                email="test@example.com",
                password="weak",
                password_confirm="weak",
            )

    def test_register_request_password_mismatch(self):
        """RegisterRequest rejects a password / confirmation mismatch."""
        from opendata.api.schemas import RegisterRequest

        with pytest.raises(ValidationError, match="Passwords do not match"):
            RegisterRequest(
                email="test@example.com",
                password="Password1!",
                password_confirm="Password2!",
            )

    def test_login_request_valid(self):
        """Test valid LoginRequest."""
        from opendata.api.schemas import LoginRequest

        data = LoginRequest(
            email="test@example.com",
            password="password123",
        )

        assert data.email == "test@example.com"
        assert data.password == "password123"

    def test_login_request_missing_fields(self):
        """Test LoginRequest with missing fields."""
        from opendata.api.schemas import LoginRequest

        with pytest.raises(ValidationError):
            LoginRequest(email="test@example.com")

    def test_refresh_request_valid(self):
        """Test valid RefreshTokenRequest."""
        from opendata.api.schemas import RefreshTokenRequest

        data = RefreshTokenRequest(refresh_token="valid_refresh_token")
        assert data.refresh_token == "valid_refresh_token"


class TaskSchemas:
    """Test task-related schemas."""

    def test_task_create_request_valid(self):
        """Test valid TaskCreateRequest."""
        from opendata.api.schemas import TaskCreateRequest

        data = TaskCreateRequest(
            name="Test Task",
            script_id="stock_zh_a_hist",
            schedule_type="daily",
            schedule_expression="15:00",
        )

        assert data.name == "Test Task"
        assert data.script_id == "stock_zh_a_hist"

    def test_task_update_request_valid(self):
        """Test valid TaskUpdateRequest."""
        from opendata.api.schemas import TaskUpdateRequest

        data = TaskUpdateRequest(
            name="Updated Task",
            is_active=False,
        )

        assert data.name == "Updated Task"
        assert data.is_active is False

    def test_task_update_request_all_optional(self):
        """Test TaskUpdateRequest with no fields."""
        from opendata.api.schemas import TaskUpdateRequest

        data = TaskUpdateRequest()
        assert data is not None


class ExecutionSchemas:
    """Test execution-related schemas."""

    def test_execution_response_valid(self):
        """Test valid TaskExecutionResponse."""
        from opendata.api.schemas import TaskExecutionResponse

        data = TaskExecutionResponse(
            execution_id="exec_001",
            task_id=1,
            script_id="stock_zh_a_hist",
            status="completed",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC) + timedelta(seconds=10),
            duration=10.0,
            rows_before=100,
            rows_after=150,
        )

        assert data.execution_id == "exec_001"
        assert data.status == "completed"
        assert data.duration == 10.0


class ScriptSchemas:
    """Test script-related schemas."""

    def test_script_response_valid(self):
        """Test valid DataScriptResponse."""
        from opendata.api.schemas import DataScriptResponse

        data = DataScriptResponse(
            script_id="test_script",
            script_name="Test Script",
            category="stocks",
            frequency="daily",
            is_active=True,
        )

        assert data.script_id == "test_script"
        assert data.frequency == "daily"


class UserSchemas:
    """Test user-related schemas."""

    def test_user_response_valid(self):
        """Test valid UserResponse."""
        from opendata.api.schemas import UserResponse

        data = UserResponse(
            id=1,
            username="testuser",
            email="test@example.com",
            role="user",
            is_active=True,
            created_at=datetime.now(UTC),
        )

        assert data.username == "testuser"
        assert data.role == "user"

    def test_user_update_request_valid(self):
        """Test valid UserUpdateRequest."""
        from opendata.api.schemas import UserUpdateRequest

        data = UserUpdateRequest(
            username="newusername",
            email="new@example.com",
        )

        assert data.username == "newusername"
        assert data.email == "new@example.com"


class APISchemas:
    """Test generic API response schemas."""

    def test_api_response_valid(self):
        """Test valid APIResponse."""
        from opendata.api.schemas import APIResponse

        data = APIResponse(
            success=True,
            message="Operation successful",
            data={"key": "value"},
        )

        assert data.success is True
        assert data.message == "Operation successful"

    def test_api_response_minimal(self):
        """Test APIResponse with minimal fields."""
        from opendata.api.schemas import APIResponse

        data = APIResponse(success=True)
        assert data.success is True

    def test_paginated_response_valid(self):
        """Test valid PaginatedResponse."""
        from opendata.api.schemas import PaginatedResponse

        data = PaginatedResponse(
            data=[{"id": 1}, {"id": 2}],
            total=2,
            page=1,
            page_size=10,
        )

        assert data.total == 2
        assert data.page == 1

    def test_error_response_valid(self):
        """Test valid ErrorResponse."""
        from opendata.api.schemas import ErrorResponse

        data = ErrorResponse(
            detail="Error occurred",
            code="validation_error",
        )

        assert data.detail == "Error occurred"
        assert data.code == "validation_error"


class TableSchemas:
    """Test table-related schemas."""

    def test_table_response_valid(self):
        """Test valid TableResponse."""
        from opendata.api.schemas import TableResponse

        data = TableResponse(
            id=1,
            table_name="test_table",
            display_name="Test Table",
            row_count=100,
            created_at=datetime.now(UTC),
        )

        assert data.table_name == "test_table"
        assert data.row_count == 100

    def test_table_schema_response_valid(self):
        """Test valid TableSchemaResponse."""
        from opendata.api.schemas import TableSchemaResponse

        data = TableSchemaResponse(
            table_name="test_table",
            columns=[{"name": "id", "type": "int"}],
            row_count=100,
        )

        assert data.table_name == "test_table"
        assert len(data.columns) == 1


class ScriptSchemasAdmin:
    """Test admin script management schemas."""

    def test_script_create_request_valid(self):
        """Test valid ScriptCreateRequest."""
        from opendata.api.schemas import ScriptCreateRequest

        data = ScriptCreateRequest(
            script_id="custom_script",
            script_name="Custom Script",
            category="custom",
            module_path="custom.module",
        )

        assert data.script_id == "custom_script"
        assert data.category == "custom"

    def test_script_update_request_valid(self):
        """Test valid ScriptUpdateRequest."""
        from opendata.api.schemas import ScriptUpdateRequest

        data = ScriptUpdateRequest(
            script_name="Updated Name",
            description="Updated description",
        )

        assert data.script_name == "Updated Name"


class SettingsSchemas:
    """Test settings-related schemas."""

    def test_database_config_response_valid(self):
        """Test valid DatabaseConfigResponse."""
        from opendata.api.schemas import DatabaseConfigResponse

        data = DatabaseConfigResponse(
            host="localhost",
            port=3306,
            database="test_db",
        )

        assert data.host == "localhost"
        assert data.port == 3306

    def test_warehouse_config_response_valid(self):
        """Test valid WarehouseConfigResponse."""
        from opendata.api.schemas import WarehouseConfigResponse

        data = WarehouseConfigResponse(
            warehouse_type="mysql",
            host="localhost",
            port=3306,
        )

        assert data.warehouse_type == "mysql"


class InterfaceSchemas:
    """Test interface-related schemas."""

    def test_interface_response_valid(self):
        """Test valid DataInterfaceResponse."""
        from opendata.api.schemas import DataInterfaceResponse

        data = DataInterfaceResponse(
            interface_id="test_interface",
            interface_name="Test Interface",
            category="stock",
            is_active=True,
        )

        assert data.interface_id == "test_interface"
        assert data.category == "stock"


class FilterAndSearchSchemas:
    """Test filter and search schemas."""

    def test_filter_params_valid(self):
        """Test FilterParams with valid data."""
        from opendata.api.schemas import FilterParams

        data = FilterParams(
            search="test",
            category="stock",
            is_active=True,
        )

        assert data.search == "test"
        assert data.category == "stock"

    def test_filter_params_all_optional(self):
        """Test FilterParams with no filters."""
        from opendata.api.schemas import FilterParams

        data = FilterParams()
        assert data is not None


class PaginationSchemas:
    """Test pagination schemas."""

    def test_paginated_params_default(self):
        """Test PaginatedParams with defaults."""
        from opendata.api.schemas import PaginatedParams

        data = PaginatedParams()
        assert data.page == 1
        assert data.page_size == 20

    def test_paginated_params_custom(self):
        """Test PaginatedParams with custom values."""
        from opendata.api.schemas import PaginatedParams

        data = PaginatedParams(page=2, page_size=50)
        assert data.page == 2
        assert data.page_size == 50


class ScheduleSchemas:
    """Test schedule-related schemas."""

    def test_schedule_expression_valid_daily(self):
        """Test daily schedule expression."""
        from opendata.api.schemas import ScheduleExpression

        data = ScheduleExpression(
            type="daily",
            time="15:00",
        )

        assert data.type == "daily"
        assert data.time == "15:00"

    def test_schedule_expression_valid_cron(self):
        """Test cron schedule expression."""
        from opendata.api.schemas import ScheduleExpression

        data = ScheduleExpression(
            type="cron",
            cron="0 9 * * MON-FRI",
        )

        assert data.type == "cron"
        assert data.cron == "0 9 * * MON-FRI"
