"""
Comprehensive tests for helpers utility module.

Covers table name generation, column cleaning, size/duration formatting, string truncation.
"""

from opendata.utils.helpers import (
    clean_column_names,
    format_duration,
    format_size,
    generate_table_name,
    truncate_string,
)


class TestGenerateTableName:
    """Test table name generation."""

    def test_basic_name(self):
        assert generate_table_name("stock_zh_a_hist") == "ak_stock_zh_a_hist"

    def test_name_with_dots(self):
        assert generate_table_name("stock.hist") == "ak_stock_hist"

    def test_name_with_dashes(self):
        assert generate_table_name("stock-hist") == "ak_stock_hist"

    def test_name_with_special_chars(self):
        result = generate_table_name("stock@#$hist")
        assert result == "ak_stockhist"

    def test_name_starting_with_number(self):
        result = generate_table_name("123stock")
        assert result.startswith("ak_t_")

    def test_long_name_truncated(self):
        result = generate_table_name("a" * 100)
        assert len(result) <= 63  # ak_ prefix + 60 max

    def test_custom_prefix(self):
        result = generate_table_name("test", prefix="data_")
        assert result == "data_test"


class TestCleanColumnNames:
    """Test column name cleaning."""

    def test_basic_columns(self):
        result = clean_column_names(["Name", "Value"])
        assert result == ["name", "value"]

    def test_spaces_replaced(self):
        result = clean_column_names(["First Name", "Last Name"])
        assert result == ["first_name", "last_name"]

    def test_special_chars_replaced(self):
        result = clean_column_names(["价格(元)", "涨跌幅%"])
        assert all("(" not in c for c in result)

    def test_long_column_truncated(self):
        result = clean_column_names(["a" * 100])
        assert len(result[0]) <= 64

    def test_empty_column_name(self):
        result = clean_column_names([""])
        assert result == ["column"]

    def test_numeric_column(self):
        result = clean_column_names([123])
        assert result == ["123"]


class TestFormatSize:
    """Test size formatting."""

    def test_bytes(self):
        assert format_size(500) == "500.0 B"

    def test_kilobytes(self):
        assert format_size(2048) == "2.0 KB"

    def test_megabytes(self):
        assert format_size(1048576) == "1.0 MB"

    def test_gigabytes(self):
        assert format_size(1073741824) == "1.0 GB"

    def test_terabytes(self):
        assert format_size(1099511627776) == "1.0 TB"

    def test_none(self):
        assert format_size(None) == "Unknown"

    def test_zero(self):
        assert format_size(0) == "0.0 B"


class TestFormatDuration:
    """Test duration formatting."""

    def test_milliseconds(self):
        assert format_duration(500) == "500ms"

    def test_seconds(self):
        assert format_duration(5000) == "5.0s"

    def test_minutes(self):
        assert format_duration(120000) == "2.0m"

    def test_none(self):
        assert format_duration(None) == "Unknown"

    def test_zero(self):
        assert format_duration(0) == "0ms"


class TestTruncateString:
    """Test string truncation."""

    def test_short_string(self):
        assert truncate_string("hello", 10) == "hello"

    def test_exact_length(self):
        assert truncate_string("hello", 5) == "hello"

    def test_truncated(self):
        result = truncate_string("hello world", 8)
        assert result == "hello..."
        assert len(result) == 8

    def test_custom_suffix(self):
        result = truncate_string("hello world", 9, suffix="..")
        assert result.endswith("..")
