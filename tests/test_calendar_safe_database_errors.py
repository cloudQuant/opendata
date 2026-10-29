"""Calendar database fallback logs must not reveal driver exception details."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest
from loguru import logger
from pymysql.err import MySQLError
from sqlalchemy.exc import DBAPIError

from opendata.pipeline.trading_calendar import TIER_WEEKDAY, resolve_calendar

if TYPE_CHECKING:
    from sqlalchemy import Engine

_WARNING = "trading calendar contract unavailable; using weekday fallback"
_CANARIES = ("CANARY_SQL", "CANARY_PARAMS", "CANARY_DSN", "CANARY_DRIVER")
_DB_ERROR_TEXT = "CANARY_SQL CANARY_PARAMS CANARY_DSN CANARY_DRIVER"


class _FailingEngine:
    """Engine-shaped object that fails before opening any database connection."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def connect(self) -> None:
        """Raise the configured synthetic error without accessing a database."""
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            DBAPIError(
                "SELECT * FROM dwd_trading_calendar WHERE token = 'CANARY_SQL'",
                {"token": "CANARY_PARAMS", "dsn": "CANARY_DSN"},
                RuntimeError("CANARY_DRIVER"),
                connection_invalidated=False,
            ),
            id="sqlalchemy-dbapi-error",
        ),
        pytest.param(sqlite3.OperationalError(_DB_ERROR_TEXT), id="sqlite-dbapi-error"),
        pytest.param(MySQLError(_DB_ERROR_TEXT), id="pymysql-dbapi-error"),
        pytest.param(RuntimeError(f"{_DB_ERROR_TEXT} CANARY_PROGRAMMING_ERROR"), id="other-error"),
    ],
)
def test_resolve_calendar_safely_falls_back_without_logging_exception_details(
    error: Exception,
) -> None:
    """Every historical fallback path emits only the fixed warning text."""
    expected_canaries = (*_CANARIES, "CANARY_PROGRAMMING_ERROR")
    error_text = str(error)
    assert all(marker in error_text for marker in _CANARIES)

    rendered_logs: list[tuple[str, object]] = []
    handler_id = logger.add(
        lambda message: rendered_logs.append((str(message), message.record["exception"])),
        level="WARNING",
    )
    try:
        calendar = resolve_calendar(cast("Engine", _FailingEngine(error)))
    finally:
        logger.remove(handler_id)

    assert calendar.tier == TIER_WEEKDAY
    assert calendar.expected_data_date(date(2026, 9, 26)) == date(2026, 9, 25)
    assert len(rendered_logs) == 1
    rendered_message, exception_info = rendered_logs[0]
    assert _WARNING in rendered_message
    assert exception_info is None
    assert all(marker not in rendered_message for marker in expected_canaries)
