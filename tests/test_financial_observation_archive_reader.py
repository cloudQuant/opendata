"""Behavioral tests for the bounded financial archive read helper.

The connection double returns only synthetic SQL rows. These tests do not
create an engine, open a database, load application configuration, or contact
an external source. MySQL transaction semantics remain an integration concern.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.financial_observation_archive_reader import (
    FinancialObservationArchivePage,
    FinancialObservationArchiveReadError,
    read_financial_observation_page,
)


class _Result:
    """Minimal SQLAlchemy result protocol over fixed synthetic rows."""

    def __init__(self, rows: list[dict[str, object]] | None = None, scalar: object = None) -> None:
        self._rows = [] if rows is None else rows
        self._scalar = scalar

    def mappings(self) -> _Result:
        """Return this mapping-result double."""
        return self

    def all(self) -> list[dict[str, object]]:
        """Return fixed synthetic rows."""
        return self._rows

    def scalar_one_or_none(self) -> object:
        """Return the configured synthetic table-engine metadata."""
        return self._scalar


class _Connection:
    """Connection double recording only the helper's SELECT statements."""

    def __init__(
        self,
        replies: list[list[dict[str, object]]],
        *,
        isolation: str = "REPEATABLE READ",
        table_engine: object = "InnoDB",
        dialect_name: str = "mysql",
        server_version_info: tuple[int, ...] | None = (8, 4, 0),
        is_mariadb: bool = False,
        active: bool = True,
        transaction_available: bool = True,
        transaction_active: bool = True,
        isolation_error: bool = False,
        engine_lookup_error: bool = False,
    ) -> None:
        self.dialect = SimpleNamespace(
            name=dialect_name,
            server_version_info=server_version_info,
            is_mariadb=is_mariadb,
        )
        self._isolation = isolation
        self._table_engine = table_engine
        self._active = active
        self._isolation_error = isolation_error
        self._engine_lookup_error = engine_lookup_error
        self._replies = replies
        self._transaction = (
            SimpleNamespace(is_active=transaction_active) if transaction_available else None
        )
        self.calls: list[tuple[str, dict[str, object]]] = []

    def in_transaction(self) -> bool:
        """Report one live synthetic transaction."""
        return self._active

    def get_transaction(self) -> object | None:
        """Return the stable synthetic transaction object."""
        return self._transaction

    def get_isolation_level(self) -> str:
        """Return the configured synthetic isolation level."""
        if self._isolation_error:
            raise RuntimeError("isolation metadata is unavailable")
        return self._isolation

    def execute(self, statement: Any, params: dict[str, object]) -> _Result:
        """Record a SELECT and return its fixed synthetic result."""
        statement_text = statement.text
        self.calls.append((statement_text, dict(params)))
        if "information_schema.tables" in statement_text:
            if self._engine_lookup_error:
                raise RuntimeError("synthetic metadata lookup failed")
            return _Result(scalar=self._table_engine)
        if "FROM od_financial_observations" not in statement_text:
            raise AssertionError("archive helper issued an unexpected SQL statement")
        reply_index = sum("FROM od_financial_observations" in sql for sql, _ in self.calls) - 1
        return _Result(rows=self._replies[reply_index][: int(params["fetch_limit"])])


def _row(
    observation_id: str,
    *,
    item_code: str,
    observed_at: datetime,
    decimal_text: str | None,
) -> dict[str, object]:
    """Build one stored row with distinct reporting, disclosure, and read times."""
    return {
        "observation_id": observation_id,
        "natural_key_sha256": "a" * 64,
        "content_sha256": "b" * 64,
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "report_period": date(2024, 12, 31),
        "source_disclosure_date": date(2025, 3, 31),
        "observed_at_utc": observed_at,
        "item_code": item_code,
        "value_decimal_text": decimal_text,
        "native_unit": "CNY",
        "source_metadata_json": '{"period":"annual"}',
        "observation_policy": "current_as_observed_only",
    }


def _read(connection: _Connection, **overrides: object) -> FinancialObservationArchivePage:
    """Read the fixed test scope with optional field overrides."""
    arguments: dict[str, object] = {
        "source_id": "ths_fuyao",
        "instrument_id": "600519.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "start_report_period": date(2020, 1, 1),
        "end_report_period": date(2025, 12, 31),
        "page_size": 2,
    }
    arguments.update(overrides)
    return read_financial_observation_page(connection, **arguments)  # type: ignore[arg-type]


def test_keyset_pages_keep_decimal_date_and_observation_semantics() -> None:
    """Continuation is ordered, transaction-bound, and keeps source fields exact."""
    observed_one = datetime(2025, 4, 1, 1, 2, 3, 123456)
    observed_two = datetime(2025, 4, 1, 1, 2, 4, 654321)
    precise_text = "12345678901234567890.123456789012345678"
    rows = [
        _row("1" * 64, item_code="income_a", observed_at=observed_one, decimal_text=precise_text),
        _row("2" * 64, item_code="income_a", observed_at=observed_two, decimal_text=None),
        _row(
            "3" * 64,
            item_code="income_b",
            observed_at=observed_two,
            decimal_text="-0.000000000000000001",
        ),
    ]
    connection = _Connection([rows, [rows[2]]])

    first = _read(connection)
    second = _read(connection, cursor=first.next_cursor)

    assert len(first.rows) == 2
    assert first.has_more is True
    assert first.rows[0].value_decimal_text == precise_text
    assert first.rows[1].value_decimal_text is None
    assert first.rows[0].source_disclosure_date == date(2025, 3, 31)
    assert first.rows[0].observed_at_utc.isoformat() == "2025-04-01T01:02:03.123456+00:00"
    assert first.rows[0].source_disclosure_date != first.rows[0].observed_at_utc.date()
    assert first.completeness == "unknown"
    assert first.point_in_time_reconstruction == "unavailable"
    assert len(second.rows) == 1
    assert second.has_more is False

    page_sql, page_params = next(
        (sql, params) for sql, params in connection.calls if "FROM od_financial_observations" in sql
    )
    assert (
        "ORDER BY report_period ASC, item_code ASC, observed_at_utc ASC, observation_id ASC"
        in page_sql
    )
    assert page_params["fetch_limit"] == 3
    continuation_sql, continuation_params = [
        (sql, params) for sql, params in connection.calls if "FROM od_financial_observations" in sql
    ][1]
    assert "observation_id > :after_observation_id" in continuation_sql
    assert continuation_params["after_report_period"] == date(2024, 12, 31)
    assert continuation_params["after_item_code"] == "income_a"
    assert continuation_params["after_observed_at_utc"] == observed_two
    assert continuation_params["after_observation_id"] == "2" * 64


def test_cursor_cannot_cross_connection_transaction_or_scope() -> None:
    """A continuation cannot silently switch its MVCC snapshot or query bounds."""
    first_rows = [
        _row("1" * 64, item_code="revenue", observed_at=datetime(2025, 4, 1), decimal_text="1"),
        _row("2" * 64, item_code="revenue", observed_at=datetime(2025, 4, 2), decimal_text="2"),
        _row("3" * 64, item_code="revenue", observed_at=datetime(2025, 4, 3), decimal_text="3"),
    ]
    connection = _Connection([first_rows])
    first = _read(connection)
    assert first.next_cursor is not None

    prior_calls = len(connection.calls)
    with pytest.raises(FinancialObservationArchiveReadError, match="original connection"):
        _read(_Connection([]), cursor=first.next_cursor)
    with pytest.raises(FinancialObservationArchiveReadError, match="original connection"):
        connection._transaction = SimpleNamespace(is_active=True)
        _read(connection, cursor=first.next_cursor)
    with pytest.raises(ValueError, match="scope"):
        connection._transaction = first.next_cursor._transaction
        _read(connection, cursor=first.next_cursor, end_report_period=date(2024, 12, 31))
    assert len(connection.calls) == prior_calls


def test_cursor_rejects_a_value_not_created_by_a_prior_page() -> None:
    """A caller cannot use an arbitrary object as a keyset continuation."""
    connection = _Connection([])
    with pytest.raises(TypeError, match="prior archive page"):
        _read(connection, cursor=object())
    assert connection.calls == []


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"source_id": ""}, ValueError, "source_id"),
        ({"instrument_id": "600519"}, ValueError, "instrument_id"),
        ({"statement_type": "balance_sheet"}, ValueError, "statement_type"),
        ({"periodicity": "monthly"}, ValueError, "periodicity"),
        ({"start_report_period": datetime(2020, 1, 1)}, TypeError, "dates"),
        ({"end_report_period": date(2019, 12, 31)}, ValueError, "cannot be after"),
        ({"end_report_period": date(2032, 1, 1)}, ValueError, "3,660 days"),
        ({"page_size": True}, TypeError, "integer"),
        ({"page_size": 201}, ValueError, "between 1 and 200"),
    ],
)
def test_invalid_scope_is_rejected_before_connection_access(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    """Bad caller scopes fail before even reading connection transaction state."""
    connection = _Connection([])
    with pytest.raises(error, match=message):
        _read(connection, **kwargs)
    assert connection.calls == []


@pytest.mark.parametrize(
    ("connection_kwargs", "message"),
    [
        ({"isolation": "READ COMMITTED"}, "REPEATABLE READ"),
        ({"table_engine": "MyISAM"}, "InnoDB"),
        ({"dialect_name": "mariadb", "is_mariadb": True}, "MySQL 8.0.16"),
        ({"server_version_info": (8, 0, 15)}, "MySQL 8.0.16"),
        ({"server_version_info": None}, "MySQL 8.0.16"),
        ({"server_version_info": (8, 0)}, "MySQL 8.0.16"),
        ({"server_version_info": ("8", 0, 0)}, "MySQL 8.0.16"),
    ],
)
def test_reader_rejects_connections_without_repeatable_innodb_snapshot(
    connection_kwargs: dict[str, object], message: str
) -> None:
    """Unsupported isolation, table engine, and server versions fail closed."""
    connection = _Connection([], **connection_kwargs)
    with pytest.raises(FinancialObservationArchiveReadError, match=message):
        _read(connection)


@pytest.mark.parametrize(
    "connection_kwargs",
    [
        {"active": False},
        {"transaction_available": False},
        {"transaction_active": False},
        {"isolation_error": True},
    ],
)
def test_reader_rejects_missing_or_uninspectable_transaction(
    connection_kwargs: dict[str, object],
) -> None:
    """A missing, inactive, or unreadable caller transaction fails closed."""
    connection = _Connection([], **connection_kwargs)
    with pytest.raises(FinancialObservationArchiveReadError):
        _read(connection)


@pytest.mark.parametrize(
    ("connection_kwargs", "message"),
    [
        ({"table_engine": None}, "require the InnoDB table engine"),
        ({"engine_lookup_error": True}, "could not verify"),
    ],
)
def test_reader_rejects_missing_or_unreadable_table_metadata(
    connection_kwargs: dict[str, object], message: str
) -> None:
    """Missing InnoDB metadata or metadata query errors do not reach archive SQL."""
    connection = _Connection([], **connection_kwargs)
    with pytest.raises(FinancialObservationArchiveReadError, match=message):
        _read(connection)
    assert not any("FROM od_financial_observations" in sql for sql, _ in connection.calls)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("source_id", 17, "source_id must be returned as text"),
        ("value_decimal_text", 1.25, "value_decimal_text must remain text"),
        ("report_period", datetime(2024, 12, 31), "report dates must be SQL DATE"),
        ("source_disclosure_date", "2025-03-31", "report dates must be SQL DATE"),
        ("observed_at_utc", "2025-04-01T00:00:00", "naive UTC SQL DATETIME"),
        (
            "observed_at_utc",
            datetime(2025, 4, 1, tzinfo=timezone.utc),
            "naive UTC SQL DATETIME",
        ),
        ("observation_policy", "historical_pit", "does not permit a PIT reconstruction claim"),
    ],
)
def test_reader_rejects_rows_with_invalid_storage_types_or_policy(
    column: str, value: object, message: str
) -> None:
    """Malformed persisted values fail closed rather than being coerced."""
    row = _row(
        "1" * 64,
        item_code="income_a",
        observed_at=datetime(2025, 4, 1),
        decimal_text="123.45",
    )
    row[column] = value
    connection = _Connection([[row]])

    with pytest.raises(FinancialObservationArchiveReadError, match=message):
        _read(connection)


def test_archive_sql_is_select_only_and_page_scope_is_always_bounded() -> None:
    """The helper emits only scoped SELECT statements with a hard page limit."""
    connection = _Connection([[]])
    _read(connection)

    assert len(connection.calls) == 2
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql, _ in connection.calls)
    archive_sql, params = next(
        (sql, params) for sql, params in connection.calls if "FROM od_financial_observations" in sql
    )
    assert "source_id = :source_id" in archive_sql
    assert "instrument_id = :instrument_id" in archive_sql
    assert "statement_type = :statement_type" in archive_sql
    assert "periodicity = :periodicity" in archive_sql
    assert "report_period >= :start_report_period" in archive_sql
    assert "report_period <= :end_report_period" in archive_sql
    assert params["fetch_limit"] == 3
