"""Small SQL-result and stored-value boundaries shared by durable services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypeGuard

from sqlalchemy.engine import CursorResult


def cursor_rowcount(result: object) -> int:
    """Return a DML cursor rowcount, treating non-cursor results as zero."""
    if isinstance(result, CursorResult):
        return result.rowcount or 0
    return 0


def stored_utc(value: datetime) -> datetime:
    """Normalize a UTC timestamp read back from a portable database column."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def optional_stored_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional stored timestamp without changing null semantics."""
    return stored_utc(value) if value is not None else None


def is_lower_hex_sha256(value: object) -> bool:
    """Return whether a value is exactly 64 lower-case hexadecimal characters."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def is_holdout_sha256(value: object) -> TypeGuard[str]:
    """Preserve holdout's existing lower-case Python hexadecimal parsing rule."""
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def is_positive_integer(value: object) -> bool:
    """Return whether a value is an integer fence or visibility sequence."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_fetch_lease_handle(value: object) -> bool:
    """Validate the structural lease fields used to fence a publication write."""
    return (
        is_lower_hex_sha256(getattr(value, "lease_key_sha256", None))
        and is_positive_integer(getattr(value, "fence_token", None))
        and isinstance(getattr(value, "owner_token", None), str)
        and bool(getattr(value, "owner_token", "").strip())
    )
