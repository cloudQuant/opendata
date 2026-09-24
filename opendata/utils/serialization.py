"""Value serialization utilities for database/API responses.

Provides consistent serialization of Decimal, datetime, bytes, etc.
for JSON/CSV export and API responses.
"""

import decimal
from datetime import date, datetime
from typing import Any

#: Leading characters a spreadsheet treats as the start of a formula.
#: ``-`` and ``+`` are here because ``-2+3+cmd|...`` is a live formula
#: in Excel and Sheets even though it reads as arithmetic.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Prefix that forces a spreadsheet to treat the cell as literal text.
FORMULA_ESCAPE = "'"


def escape_csv_formula(value: str) -> str:
    """Neutralize a CSV cell that a spreadsheet would run as a formula.

    Design §10.1: an exported cell starting with ``=``/``+``/``-``/``@``
    (or a leading tab/CR) is a formula-injection vector - ``=cmd|'/c
    calc'!A0`` executes when the file is opened. Prefixing with an
    apostrophe is the OWASP-recommended neutralization: Excel and Sheets
    show the literal text, and the value survives a round trip.

    Only text is escaped. Numbers are left alone on purpose - a Decimal
    ``-1.5`` is a number, not a formula, and quoting it would turn a
    price column into text.

    Args:
        value: The cell text.

    Returns:
        The text, prefixed when it could be read as a formula.
    """
    if value.startswith(FORMULA_PREFIXES):
        return f"{FORMULA_ESCAPE}{value}"
    return value


def serialize_for_json(value: Any) -> Any:  # noqa: ANN401
    """Serialize a value for JSON response (float for Decimal, isoformat for dates).

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
    """Serialize a value for CSV export (str for Decimal, isoformat for dates).

    Text is passed through :func:`escape_csv_formula`, so every export
    built on this helper is safe against formula injection by
    construction rather than by each caller remembering to escape.

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
        return escape_csv_formula(value.decode("utf-8", errors="replace"))
    if isinstance(value, str):
        return escape_csv_formula(value)
    return value
