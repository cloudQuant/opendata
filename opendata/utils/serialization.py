"""
Value serialization utilities for database/API responses.

Provides consistent serialization of Decimal, datetime, bytes, etc.
for JSON/CSV export and API responses.
"""

import decimal
from datetime import date, datetime
from typing import Any


def serialize_for_json(value: Any) -> Any:  # noqa: ANN401
    """
    Serialize a value for JSON response (float for Decimal, isoformat for dates).

    Args:
        value: Value to serialize (Decimal, date, datetime, bytes, etc.)

    Returns:
        JSON-serializable value
    """
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def serialize_for_csv(value: Any) -> str | Any:  # noqa: ANN401
    """
    Serialize a value for CSV export (str for Decimal, isoformat for dates).

    Args:
        value: Value to serialize

    Returns:
        String or primitive suitable for CSV
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
