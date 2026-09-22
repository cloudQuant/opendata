"""
Helper utility functions.

Provides common utility functions used across the application.
"""

import re
from typing import Any


def generate_table_name(interface_name: str, prefix: str = "ak_") -> str:
    """
    Generate SQL table name from interface name.

    Args:
        interface_name: Name of the akshare interface
        prefix: Prefix for table names (default: "ak_")

    Returns:
        Safe SQL table name
    """
    # Replace dots and special characters with underscores
    clean_name = interface_name.replace(".", "_").replace("-", "_")
    # Remove any remaining non-alphanumeric characters except underscore
    clean_name = re.sub(r"[^a-zA-Z0-9_]", "", clean_name)
    # Ensure it doesn't start with a number
    if clean_name and clean_name[0].isdigit():
        clean_name = f"t_{clean_name}"
    # Limit length
    if len(clean_name) > 60:
        clean_name = clean_name[:60]
    return f"{prefix}{clean_name}"


def clean_column_names(columns: list[str] | Any) -> list[str]:  # noqa: ANN401
    """
    Clean DataFrame column names for SQL compatibility.

    Args:
        columns: List of column names

    Returns:
        List of cleaned column names
    """
    cleaned = []
    for col in columns:
        # Convert to string
        col_str = str(col)
        # Lowercase
        col_str = col_str.lower()
        # Replace spaces and special characters with underscore
        col_str = re.sub(r"[^a-zA-Z0-9]+", "_", col_str)
        # Remove leading/trailing underscores
        col_str = col_str.strip("_")
        # Limit length
        if len(col_str) > 64:
            col_str = col_str[:64]
        # Ensure non-empty
        if not col_str:
            col_str = "column"
        cleaned.append(col_str)
    return cleaned


def format_size(size_bytes: int | None) -> str:
    """
    Format byte size to human-readable format.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted size string
    """
    if size_bytes is None:
        return "Unknown"

    remaining: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if remaining < 1024.0:
            return f"{remaining:.1f} {unit}"
        remaining /= 1024.0
    return f"{remaining:.1f} PB"


def format_duration(milliseconds: int | None) -> str:
    """
    Format duration in milliseconds to human-readable format.

    Args:
        milliseconds: Duration in milliseconds

    Returns:
        Formatted duration string
    """
    if milliseconds is None:
        return "Unknown"

    if milliseconds < 1000:
        return f"{milliseconds}ms"
    if milliseconds < 60000:
        seconds = milliseconds / 1000
        return f"{seconds:.1f}s"
    minutes = milliseconds / 60000
    return f"{minutes:.1f}m"


def safe_column_name(name: str) -> str:
    """
    Validate and quote column name to prevent SQL injection.

    Args:
        name: Column name to validate

    Returns:
        Backtick-quoted safe column name

    Raises:
        ValueError: If column name contains invalid characters
    """
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        raise ValueError(f"Invalid column name: {name}")
    return f"`{name}`"


def safe_table_name(name: str) -> str:
    """
    Validate and quote table name to prevent SQL injection.

    Args:
        name: Table name to validate

    Returns:
        Backtick-quoted safe table name

    Raises:
        ValueError: If table name contains invalid characters
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"Invalid table name: {name}")
    return f"`{name}`"


def truncate_string(text: str, max_length: int = 50, suffix: str = "...") -> str:
    """
    Truncate string to maximum length.

    Args:
        text: String to truncate
        max_length: Maximum length
        suffix: Suffix to add if truncated

    Returns:
        Truncated string
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix
