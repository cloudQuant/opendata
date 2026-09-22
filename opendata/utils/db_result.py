"""
DB result helpers.

Centralizes type handling for SQLAlchemy result objects where stubs are incomplete.
Reduces scattered type: ignore comments.
"""

from typing import Any


def get_rowcount(result: Any) -> int:  # noqa: ANN401
    """
    Safely get rowcount from SQLAlchemy CursorResult/Result.

    Args:
        result: SQLAlchemy execute result (CursorResult, etc.)

    Returns:
        Number of rows affected, or 0 if unavailable.
    """
    return int(getattr(result, "rowcount", 0) or 0)


def get_columns_from_result(result: Any) -> list[str]:  # noqa: ANN401
    """
    Get column names from SQLAlchemy Result if it returns rows.

    Args:
        result: SQLAlchemy execute result (CursorResult, Result, etc.)

    Returns:
        List of column names, or empty list if result has no rows or keys.
    """
    if not getattr(result, "returns_rows", False):
        return []
    keys = getattr(result, "keys", None)
    if keys is not None and callable(keys):
        return list(keys())
    return []
