"""Read bounded pages from the immutable financial observation archive.

The caller injects an active SQLAlchemy Core connection and owns its complete
transaction lifecycle. Before starting the transaction, the caller must set
MySQL isolation to ``REPEATABLE READ``. It must keep that isolation unchanged
and must not use the connection concurrently until paging ends. SQLAlchemy's
isolation accessor reports the connection/session setting; it cannot prove
that a caller did not change that setting after beginning the transaction.
This module does not load application settings, create engines, commit, roll
back, schedule work, or expose an API. Every page in a traversal must reuse the
same MySQL 8 InnoDB transaction; the first archive-table SELECT establishes the
consistent read view and the process-local cursor is rejected on any other
connection or transaction.

The query is limited to one explicit source, security, statement, cadence, and
inclusive report-period window of at most ten years. It preserves stored
decimal text, source disclosure dates, and UTC observation instants as distinct
fields. A page reports completeness as unknown and does not reconstruct
historical point-in-time values. Its cursor is intentionally process-local and
cannot be serialized for a REST API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection, RootTransaction

_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_INSTRUMENT_ID_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_STATEMENT_TYPES = frozenset({"income", "balance", "cashflow"})
_PERIODICITIES = frozenset({"annual", "quarterly"})
_MAX_REPORT_PERIOD_SPAN_DAYS = 3_660
_MIN_PAGE_SIZE = 1
_MAX_PAGE_SIZE = 200
_TABLE_NAME = "od_financial_observations"

_TABLE_ENGINE_SQL = text(
    "SELECT ENGINE FROM information_schema.tables "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
)

_FIRST_PAGE_SQL = text(
    """
    SELECT observation_id, natural_key_sha256, content_sha256, source_id,
           instrument_id, statement_type, periodicity, report_period,
           source_disclosure_date, observed_at_utc, item_code,
           value_decimal_text, native_unit, source_metadata_json,
           observation_policy
    FROM od_financial_observations
    WHERE source_id = :source_id
      AND instrument_id = :instrument_id
      AND statement_type = :statement_type
      AND periodicity = :periodicity
      AND report_period >= :start_report_period
      AND report_period <= :end_report_period
    ORDER BY report_period ASC, item_code ASC, observed_at_utc ASC, observation_id ASC
    LIMIT :fetch_limit
    """
)

_NEXT_PAGE_SQL = text(
    """
    SELECT observation_id, natural_key_sha256, content_sha256, source_id,
           instrument_id, statement_type, periodicity, report_period,
           source_disclosure_date, observed_at_utc, item_code,
           value_decimal_text, native_unit, source_metadata_json,
           observation_policy
    FROM od_financial_observations
    WHERE source_id = :source_id
      AND instrument_id = :instrument_id
      AND statement_type = :statement_type
      AND periodicity = :periodicity
      AND report_period >= :start_report_period
      AND report_period <= :end_report_period
      AND (
          report_period > :after_report_period
          OR (report_period = :after_report_period AND item_code > :after_item_code)
          OR (report_period = :after_report_period AND item_code = :after_item_code
              AND observed_at_utc > :after_observed_at_utc)
          OR (report_period = :after_report_period AND item_code = :after_item_code
              AND observed_at_utc = :after_observed_at_utc
              AND observation_id > :after_observation_id)
      )
    ORDER BY report_period ASC, item_code ASC, observed_at_utc ASC, observation_id ASC
    LIMIT :fetch_limit
    """
)


class FinancialObservationArchiveReadError(RuntimeError):
    """Raised when the injected connection cannot provide a stable archive read."""


@dataclass(frozen=True, slots=True)
class FinancialObservationArchiveRow:
    """One exact source-native observation returned from the archive.

    ``value_decimal_text`` is the exact stored token and is not parsed or
    converted. ``source_disclosure_date`` is the source's calendar disclosure
    date; ``observed_at_utc`` is when OpenData observed the response.
    """

    observation_id: str
    natural_key_sha256: str
    content_sha256: str
    source_id: str
    instrument_id: str
    statement_type: str
    periodicity: str
    report_period: date
    source_disclosure_date: date
    observed_at_utc: datetime
    item_code: str
    value_decimal_text: str | None
    native_unit: str
    source_metadata_json: str
    observation_policy: Literal["current_as_observed_only"]


@dataclass(frozen=True, slots=True)
class FinancialObservationArchiveCursor:
    """Process-local keyset position bound to one connection transaction.

    The private connection and transaction references deliberately prevent
    serialization and cross-transaction continuation. The public position is
    not an authentication token and is not suitable for an HTTP API.
    """

    _scope: tuple[str, str, str, str, date, date] = field(repr=False)
    _last_key: tuple[date, str, datetime, str] = field(repr=False)
    _connection: object = field(repr=False, compare=False)
    _transaction: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class FinancialObservationArchivePage:
    """A bounded page whose completeness and historical PIT status are unknown."""

    rows: tuple[FinancialObservationArchiveRow, ...]
    next_cursor: FinancialObservationArchiveCursor | None
    completeness: Literal["unknown"] = "unknown"
    point_in_time_reconstruction: Literal["unavailable"] = "unavailable"

    @property
    def has_more(self) -> bool:
        """Return whether a continuation cursor is available."""
        return self.next_cursor is not None


def read_financial_observation_page(
    connection: Connection,
    *,
    source_id: str,
    instrument_id: str,
    statement_type: str,
    periodicity: str,
    start_report_period: date,
    end_report_period: date,
    page_size: int = 100,
    cursor: FinancialObservationArchiveCursor | None = None,
) -> FinancialObservationArchivePage:
    """Read one keyset page under the caller's MySQL consistent snapshot.

    The query is read-only and never takes transaction ownership. Callers must
    set isolation to ``REPEATABLE READ`` before beginning the transaction, keep
    it unchanged, and reuse the same SQLAlchemy ``Connection`` without
    concurrent access for every continuation. SQLAlchemy exposes the
    connection/session isolation setting, which cannot prove that the caller
    did not change it after transaction start. The caller closes or commits
    after traversal. The first archive-table SELECT establishes the consistent
    read view; if the transaction already performed a consistent read, pages
    share that earlier view. This function does not assign a snapshot timestamp.

    Args:
        connection: Caller-owned active SQLAlchemy Core connection.
        source_id: Explicit lowercase source identity.
        instrument_id: One exchange-qualified six-digit security identifier.
        statement_type: ``income``, ``balance``, or ``cashflow``.
        periodicity: ``annual`` or ``quarterly``.
        start_report_period: Inclusive lower report-period bound.
        end_report_period: Inclusive upper report-period bound.
        page_size: Number of returned observations, from 1 through 200.
        cursor: Continuation returned by the preceding page in this transaction.

    Returns:
        A bounded ordered page. ``completeness`` is always ``unknown`` and
        ``point_in_time_reconstruction`` is always ``unavailable``.

    Raises:
        TypeError: If a filter has an unsupported Python type.
        ValueError: If an identifier, scope, date range, or page size is invalid.
        FinancialObservationArchiveReadError: If the connection or table cannot
            provide the required repeatable InnoDB snapshot, or a row violates
            the archive's declared storage types.
    """
    scope = _validate_request(
        source_id=source_id,
        instrument_id=instrument_id,
        statement_type=statement_type,
        periodicity=periodicity,
        start_report_period=start_report_period,
        end_report_period=end_report_period,
        page_size=page_size,
    )
    transaction = _require_repeatable_read_transaction(connection)
    if cursor is not None:
        _validate_cursor(cursor, scope=scope, connection=connection, transaction=transaction)

    _require_innodb_table(connection)
    params: dict[str, object] = {
        "source_id": source_id,
        "instrument_id": instrument_id,
        "statement_type": statement_type,
        "periodicity": periodicity,
        "start_report_period": start_report_period,
        "end_report_period": end_report_period,
        "fetch_limit": page_size + 1,
    }
    if cursor is None:
        statement = _FIRST_PAGE_SQL
    else:
        statement = _NEXT_PAGE_SQL
        after_report_period, after_item_code, after_observed_at, after_observation_id = (
            cursor._last_key
        )
        params.update(
            {
                "after_report_period": after_report_period,
                "after_item_code": after_item_code,
                "after_observed_at_utc": after_observed_at.replace(tzinfo=None),
                "after_observation_id": after_observation_id,
            }
        )

    result = connection.execute(statement, params)
    selected = list(result.mappings().all())
    has_more = len(selected) > page_size
    returned = tuple(_row_from_mapping(row) for row in selected[:page_size])
    next_cursor = None
    if has_more and returned:
        last_row = returned[-1]
        next_cursor = FinancialObservationArchiveCursor(
            _scope=scope,
            _last_key=(
                last_row.report_period,
                last_row.item_code,
                last_row.observed_at_utc,
                last_row.observation_id,
            ),
            _connection=connection,
            _transaction=transaction,
        )
    return FinancialObservationArchivePage(rows=returned, next_cursor=next_cursor)


def _validate_request(
    *,
    source_id: str,
    instrument_id: str,
    statement_type: str,
    periodicity: str,
    start_report_period: date,
    end_report_period: date,
    page_size: int,
) -> tuple[str, str, str, str, date, date]:
    """Validate required single-security scope before touching the connection."""
    if type(source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
        raise ValueError("source_id must be an explicit lowercase source identifier")
    if type(instrument_id) is not str or _INSTRUMENT_ID_PATTERN.fullmatch(instrument_id) is None:
        raise ValueError("instrument_id must be one six-digit SH, SZ, or BJ identifier")
    if type(statement_type) is not str or statement_type not in _STATEMENT_TYPES:
        raise ValueError("statement_type must be income, balance, or cashflow")
    if type(periodicity) is not str or periodicity not in _PERIODICITIES:
        raise ValueError("periodicity must be annual or quarterly")
    if type(start_report_period) is not date or type(end_report_period) is not date:
        raise TypeError("report-period bounds must be dates without times")
    if start_report_period > end_report_period:
        raise ValueError("start_report_period cannot be after end_report_period")
    if (end_report_period - start_report_period).days > _MAX_REPORT_PERIOD_SPAN_DAYS:
        raise ValueError("report-period range cannot exceed 3,660 days")
    if type(page_size) is not int:
        raise TypeError("page_size must be an integer")
    if not _MIN_PAGE_SIZE <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError("page_size must be between 1 and 200")
    return (
        source_id,
        instrument_id,
        statement_type,
        periodicity,
        start_report_period,
        end_report_period,
    )


def _require_repeatable_read_transaction(connection: Connection) -> RootTransaction:
    """Check for a live MySQL 8+ transaction and configured repeatable-read isolation."""
    try:
        dialect = connection.dialect
        transaction = connection.get_transaction()
        active = connection.in_transaction()
        isolation = connection.get_isolation_level()
    except Exception as exc:
        raise FinancialObservationArchiveReadError(
            "archive reader requires an active SQLAlchemy Core connection"
        ) from exc
    server_version = dialect.server_version_info
    if (
        dialect.name != "mysql"
        or bool(getattr(dialect, "is_mariadb", False))
        or not isinstance(server_version, tuple)
        or len(server_version) < 3
        or any(type(part) is not int for part in server_version[:3])
        or server_version[0] != 8
        or server_version[:3] < (8, 0, 16)
    ):
        raise FinancialObservationArchiveReadError(
            "archive reads require MySQL 8.0.16 or later; MariaDB is unsupported"
        )
    if active is not True or transaction is None or not getattr(transaction, "is_active", False):
        raise FinancialObservationArchiveReadError(
            "archive reads require a caller-owned active transaction"
        )
    if type(isolation) is not str or isolation.upper() != "REPEATABLE READ":
        raise FinancialObservationArchiveReadError(
            "archive reads require REPEATABLE READ for stable keyset pagination"
        )
    return transaction


def _require_innodb_table(connection: Connection) -> None:
    """Fail closed unless the exact archive table uses InnoDB MVCC."""
    try:
        engine = connection.execute(
            _TABLE_ENGINE_SQL, {"table_name": _TABLE_NAME}
        ).scalar_one_or_none()
    except Exception as exc:
        raise FinancialObservationArchiveReadError(
            "could not verify the financial observation table engine"
        ) from exc
    if type(engine) is not str or engine.casefold() != "innodb":
        raise FinancialObservationArchiveReadError(
            "financial observation reads require the InnoDB table engine"
        )


def _validate_cursor(
    cursor: FinancialObservationArchiveCursor,
    *,
    scope: tuple[str, str, str, str, date, date],
    connection: Connection,
    transaction: RootTransaction,
) -> None:
    """Bind a continuation to its original exact scope and transaction."""
    if type(cursor) is not FinancialObservationArchiveCursor:
        raise TypeError("cursor must come from a prior archive page")
    if cursor._scope != scope:
        raise ValueError("cursor scope must exactly match the original archive query")
    if cursor._connection is not connection or cursor._transaction is not transaction:
        raise FinancialObservationArchiveReadError(
            "cursor continuation must reuse the original connection and transaction"
        )


def _row_from_mapping(row: Mapping[str, object]) -> FinancialObservationArchiveRow:
    """Map a validated SQL row without changing source decimal text."""
    string_fields = (
        "observation_id",
        "natural_key_sha256",
        "content_sha256",
        "source_id",
        "instrument_id",
        "statement_type",
        "periodicity",
        "item_code",
        "native_unit",
        "source_metadata_json",
    )
    values: dict[str, str] = {}
    for name in string_fields:
        value = row[name]
        if type(value) is not str:
            raise FinancialObservationArchiveReadError(
                f"archive column {name} must be returned as text"
            )
        values[name] = value

    decimal_text = row["value_decimal_text"]
    if decimal_text is not None and type(decimal_text) is not str:
        raise FinancialObservationArchiveReadError(
            "archive value_decimal_text must remain text or source null"
        )
    report_period = row["report_period"]
    disclosure_date = row["source_disclosure_date"]
    observed_at = row["observed_at_utc"]
    if type(report_period) is not date or type(disclosure_date) is not date:
        raise FinancialObservationArchiveReadError("archive report dates must be SQL DATE values")
    if type(observed_at) is not datetime or observed_at.tzinfo is not None:
        raise FinancialObservationArchiveReadError(
            "archive observed_at_utc must be a naive UTC SQL DATETIME"
        )
    policy = row["observation_policy"]
    if policy != "current_as_observed_only":
        raise FinancialObservationArchiveReadError(
            "archive observation policy does not permit a PIT reconstruction claim"
        )

    return FinancialObservationArchiveRow(
        observation_id=values["observation_id"],
        natural_key_sha256=values["natural_key_sha256"],
        content_sha256=values["content_sha256"],
        source_id=values["source_id"],
        instrument_id=values["instrument_id"],
        statement_type=values["statement_type"],
        periodicity=values["periodicity"],
        report_period=report_period,
        source_disclosure_date=disclosure_date,
        observed_at_utc=observed_at.replace(tzinfo=timezone.utc),
        item_code=values["item_code"],
        value_decimal_text=decimal_text,
        native_unit=values["native_unit"],
        source_metadata_json=values["source_metadata_json"],
        observation_policy="current_as_observed_only",
    )


__all__ = [
    "FinancialObservationArchiveCursor",
    "FinancialObservationArchivePage",
    "FinancialObservationArchiveReadError",
    "FinancialObservationArchiveRow",
    "read_financial_observation_page",
]
