"""
API schemas tests.

Tests for Pydantic schemas used in API requests/responses.
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
            password="Password1",
            password_confirm="Password1",
        )

        assert data.email == "test@example.com"
        assert data.password == "Password1"

    def test_register_request_passwords_match(self):
        """Test RegisterRequest with matching passwords."""
        from opendata.api.schemas import RegisterRequest

        data = RegisterRequest(
            email="test@example.com",
            password="Password1",
            password_confirm="Password1",
        )

        assert data.password == "Password1"

    def test_register_request_invalid_email(self):
        """Test RegisterRequest with invalid email."""
        from opendata.api.schemas import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(
                email="not-an-email",
                password="Password1",
                password_confirm="Password1",
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

    def test_login_request_valid(self):
        """Test valid LoginRequest."""
        from opendata.api.schemas import LoginRequest

        data = LoginRequest(
            email="test@example.com",
            password="password123",
        )

        assert data.email == "test@example.com"
        assert data.password == "password123"


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
