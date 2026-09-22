"""
Utility function tests.
"""

import decimal
from datetime import date, datetime

from opendata.utils.helpers import (
    clean_column_names,
    format_duration,
    format_size,
    generate_table_name,
    truncate_string,
)
from opendata.utils.serialization import serialize_for_csv, serialize_for_json
from opendata.utils.validators import (
    validate_email,
    validate_password,
    validate_schedule_expression,
    validate_username,
)


class TestSerialization:
    """Test value serialization for JSON/CSV."""

    def test_serialize_for_json_decimal(self):
        """Test Decimal serializes to float for JSON."""
        assert serialize_for_json(decimal.Decimal("3.14")) == 3.14

    def test_serialize_for_json_date(self):
        """Test date serializes to isoformat."""
        d = date(2024, 1, 15)
        assert serialize_for_json(d) == "2024-01-15"

    def test_serialize_for_json_datetime(self):
        """Test datetime serializes to isoformat."""
        dt = datetime(2024, 1, 15, 10, 30)
        assert serialize_for_json(dt) == "2024-01-15T10:30:00"

    def test_serialize_for_csv_decimal(self):
        """Test Decimal serializes to str for CSV."""
        assert serialize_for_csv(decimal.Decimal("3.14")) == "3.14"

    def test_serialize_for_csv_passthrough(self):
        """Test non-special types pass through."""
        assert serialize_for_csv(42) == 42
        assert serialize_for_csv("hello") == "hello"


class TestHelpers:
    """Test helper utility functions."""

    def test_generate_table_name_basic(self):
        """Test basic table name generation."""
        result = generate_table_name("stock_zh_a_hist")
        assert result == "ak_stock_zh_a_hist"

    def test_generate_table_name_dots(self):
        """Test table name with dots."""
        result = generate_table_name("fund.etf.fetch")
        assert result == "ak_fund_etf_fetch"

    def test_generate_table_name_long_name(self):
        """Test table name truncation."""
        long_name = "a" * 70
        result = generate_table_name(long_name)
        assert len(result) <= 63  # prefix (3) + truncated name (60)
        assert result.startswith("ak_")

    def test_clean_column_names_basic(self):
        """Test basic column name cleaning."""
        result = clean_column_names(["Column Name", "Another Column"])
        assert result == ["column_name", "another_column"]

    def test_clean_column_names_special_chars(self):
        """Test column name cleaning with special characters."""
        result = clean_column_names(["Col@#$%", "Test-Column"])
        assert result == ["col", "test_column"]

    def test_format_size_bytes(self):
        """Test size formatting for bytes."""
        assert format_size(512) == "512.0 B"
        assert format_size(1024) == "1.0 KB"
        assert format_size(1024 * 1024) == "1.0 MB"
        assert format_size(1024 * 1024 * 1024) == "1.0 GB"

    def test_format_size_none(self):
        """Test size formatting for None."""
        assert format_size(None) == "Unknown"

    def test_format_duration(self):
        """Test duration formatting."""
        assert format_duration(500) == "500ms"
        assert format_duration(1500) == "1.5s"
        assert format_duration(65000) == "1.1m"

    def test_truncate_string(self):
        """Test string truncation."""
        assert truncate_string("short", 50) == "short"
        assert truncate_string("a" * 100, 50) == "a" * 47 + "..."
        assert truncate_string("test", 10, "***") == "test"


class TestValidators:
    """Test validation utility functions."""

    def test_validate_email_valid(self):
        """Test valid email addresses."""
        assert validate_email("test@example.com") is True
        assert validate_email("user.name+tag@domain.co.uk") is True

    def test_validate_email_invalid(self):
        """Test invalid email addresses."""
        assert validate_email("invalid") is False
        assert validate_email("@example.com") is False
        assert validate_email("test@") is False

    def test_validate_schedule_daily(self):
        """Test daily schedule validation."""
        assert validate_schedule_expression("09:30", "daily") is True
        assert validate_schedule_expression("23:59", "daily") is True
        assert validate_schedule_expression("25:00", "daily") is False
        assert validate_schedule_expression("invalid", "daily") is False

    def test_validate_schedule_cron(self):
        """Test cron expression validation."""
        assert validate_schedule_expression("0 9 * * *", "cron") is True
        assert validate_schedule_expression("*/5 * * * *", "cron") is True
        assert validate_schedule_expression("invalid", "cron") is False

    def test_validate_username_valid(self):
        """Test valid usernames."""
        assert validate_username("testuser")[0] is True
        assert validate_username("user123")[0] is True
        assert validate_username("test_user")[0] is True

    def test_validate_username_invalid(self):
        """Test invalid usernames."""
        assert validate_username("")[0] is False
        assert validate_username("ab")[0] is False  # Too short
        assert validate_username("user@name")[0] is False  # Invalid chars

    def test_validate_password_valid(self):
        """Test valid passwords (min 8 chars, letter + digit)."""
        assert validate_password("password123")[0] is True
        assert validate_password("abc12345")[0] is True

    def test_validate_password_invalid(self):
        """Test invalid passwords."""
        assert validate_password("")[0] is False
        assert validate_password("12345")[0] is False  # Too short
        assert validate_password("12345678")[0] is False  # No letter
