"""Key-level ods writer (design §8.1, milestone A4.2).

Replaces the legacy provider insert path and its two defects:

* tables used to be created without a primary key, so
  ``ON DUPLICATE KEY UPDATE`` never fired and every run appended
  duplicates;
* a content-hash (``row_hash``) unique key turned a *corrected* price
  into a new row.

This writer upserts on the **business key** declared by the caller:
re-writing the same key updates in place (idempotent by key, not by
content). Two paths are provided:

* ``direct`` - batched ``executemany`` of the alias-form upsert;
* ``staging`` (default) - a temporary staging table plus
  ``INSERT ... SELECT ... ON DUPLICATE KEY UPDATE``, the large-volume
  path from design §8.1, with the client-side memory bounded by
  ``batch_size`` and one transaction per chunk.

Metadata columns are stamped by the writer: ``_source``,
``_fetched_at`` (naive UTC) and ``_batch_id``. Columns the source adds
are ignored and reported (design: schema growth is explicit, never an
implicit ALTER), while missing key columns fail closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, cast

import pandas as pd

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine

#: Same rule as the DDL generator: word characters only, Unicode
#: letters included (ods keeps source naming).
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)

#: ``_batch_id`` is ``char(36)``: a UUID string.
_BATCH_ID_LENGTH = 36

#: Commit granularity from design §8.1 (5万~10万行).
DEFAULT_BATCH_SIZE = 50_000

#: Columns the writer stamps.
METADATA_COLUMNS = ("_source", "_fetched_at", "_batch_id")

WriteMode = Literal["staging", "direct"]


@dataclass(frozen=True)
class FramePlan:
    """What one write call decided about the frame.

    Attributes:
        written: Columns sent to the table, in table order.
        ignored: Source columns the table does not own (dropped).
    """

    written: list[str]
    ignored: list[str]


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one write call.

    Attributes:
        rows: Rows submitted (after column filtering).
        batches: Number of chunks committed.
    """

    rows: int
    batches: int


def plan_frame(
    frame: pd.DataFrame,
    *,
    table_columns: Sequence[str],
    key: Sequence[str],
    source: str,
    batch_id: str,
    fetched_at: datetime | None = None,
) -> tuple[pd.DataFrame, FramePlan]:
    """Align a source frame with the target table.

    Table columns drive the result: present columns keep their values,
    absent ones are filled with NULL, source-owned extras are dropped
    and reported, and the metadata trio is stamped.

    Args:
        frame: Source frame as delivered by the provider.
        table_columns: Target table columns in table order.
        key: Business-key columns (must be present in frame and table).
        source: Source identifier written to ``_source``.
        batch_id: UUID string written to ``_batch_id``.
        fetched_at: Fetch timestamp; defaults to now (naive UTC).

    Returns:
        The aligned frame plus the plan (written/ignored columns).

    Raises:
        ValueError: If ``batch_id`` is not a 36-character UUID string,
            or a business-key column is missing from the frame or the
            table (fail closed).
    """
    if not isinstance(batch_id, str) or len(batch_id) != _BATCH_ID_LENGTH:
        raise ValueError(f"invalid batch_id {batch_id!r}: expected a 36-character UUID string")
    for column in key:
        if column not in frame.columns:
            raise ValueError(f"business key column {column!r} is missing from the frame")
        if column not in table_columns:
            raise ValueError(f"business key column {column!r} is not a column of the target table")
    timestamp = fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)

    values: dict[str, object] = {}
    for column in table_columns:
        if column == "_source":
            values[column] = source
        elif column == "_fetched_at":
            values[column] = timestamp
        elif column == "_batch_id":
            values[column] = batch_id
        elif column in frame.columns:
            values[column] = frame[column]
        else:
            values[column] = pd.Series([None] * len(frame), index=frame.index)
    prepared = pd.DataFrame(values, columns=list(table_columns))
    ignored = [str(column) for column in frame.columns if column not in table_columns]
    return prepared, FramePlan(written=list(table_columns), ignored=ignored)


def build_upsert_sql(table: str, columns: Sequence[str], key: Sequence[str]) -> str:
    """Build the alias-form key-level upsert statement.

    Args:
        table: Target table name.
        columns: Columns to write, in order.
        key: Business-key columns (never rewritten by the update part).

    Returns:
        The ``INSERT ... AS new ON DUPLICATE KEY UPDATE`` statement.

    Raises:
        ValueError: On an unsafe identifier or an empty column list.
    """
    if not columns:
        raise ValueError("cannot build an upsert without columns")
    quoted_table = _quote(table)
    column_list = ", ".join(_quote(column) for column in columns)
    placeholders = ", ".join(f":{_placeholder(column)}" for column in columns)
    updates = [column for column in columns if column not in set(key)] or list(columns)
    assignments = ", ".join(f"{_quote(column)} = new.{_quote(column)}" for column in updates)
    return (
        f"INSERT INTO {quoted_table} ({column_list}) VALUES ({placeholders}) AS new "
        f"ON DUPLICATE KEY UPDATE {assignments}"
    )


def build_staging_upsert_sql(
    table: str,
    staging: str,
    columns: Sequence[str],
    key: Sequence[str],
) -> str:
    """Build the staging-table upsert statement.

    ``INSERT ... SELECT`` may reference the staging rows directly, so
    no alias form is used here (MySQL only supports the alias syntax
    for VALUES lists).

    Args:
        table: Target table name.
        staging: Staging table name.
        columns: Columns to write, in order.
        key: Business-key columns.

    Returns:
        The ``INSERT ... SELECT ... ON DUPLICATE KEY UPDATE`` statement.

    Raises:
        ValueError: On an unsafe identifier or an empty column list.
    """
    if not columns:
        raise ValueError("cannot build a staging upsert without columns")
    column_list = ", ".join(_quote(column) for column in columns)
    selected = ", ".join(f"s.{_quote(column)}" for column in columns)
    updates = [column for column in columns if column not in set(key)] or list(columns)
    assignments = ", ".join(f"{_quote(column)} = s.{_quote(column)}" for column in updates)
    return (
        f"INSERT INTO {_quote(table)} ({column_list}) "
        f"SELECT {selected} FROM {_quote(staging)} AS s "
        f"ON DUPLICATE KEY UPDATE {assignments}"
    )


def build_staging_insert_sql(staging: str, columns: Sequence[str]) -> str:
    """Build the plain insert that fills the staging table."""
    column_list = ", ".join(_quote(column) for column in columns)
    placeholders = ", ".join(f":{_placeholder(column)}" for column in columns)
    return f"INSERT INTO {_quote(staging)} ({column_list}) VALUES ({placeholders})"


def build_staging_create_sql(staging: str, table: str) -> str:
    """Build the staging-table creation (structure only, no keys)."""
    return f"CREATE TEMPORARY TABLE {_quote(staging)} AS SELECT * FROM {_quote(table)} WHERE 1=0"


def build_staging_drop_sql(staging: str) -> str:
    """Build the staging-table drop."""
    return f"DROP TEMPORARY TABLE IF EXISTS {_quote(staging)}"


def iter_batches(frame: pd.DataFrame, batch_size: int) -> Iterator[pd.DataFrame]:
    """Yield the frame in chunks of at most ``batch_size`` rows.

    Args:
        frame: Frame to slice.
        batch_size: Maximum rows per chunk (must be positive).

    Yields:
        Frame chunks in order.

    Raises:
        ValueError: If ``batch_size`` is not positive.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    for start in range(0, len(frame), batch_size):
        yield frame.iloc[start : start + batch_size]


class OdsWriter:
    """Write source frames into ods tables key-level idempotently."""

    def __init__(
        self,
        engine: Engine,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        mode: WriteMode = "staging",
    ) -> None:
        """Configure the writer.

        Args:
            engine: Engine of the warehouse database.
            batch_size: Commit granularity in rows.
            mode: ``staging`` (default) or ``direct``.

        Raises:
            ValueError: On an unknown mode or non-positive batch size.
        """
        if mode not in ("staging", "direct"):
            raise ValueError(f"unknown write mode {mode!r}; expected 'staging' or 'direct'")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        self.engine = engine
        self.batch_size = batch_size
        self.mode: WriteMode = mode

    def write(
        self,
        frame: pd.DataFrame,
        *,
        table: str,
        key: Sequence[str],
        source: str,
        batch_id: str,
        fetched_at: datetime | None = None,
    ) -> WriteResult:
        """Upsert a frame into an ods table on its business key.

        Args:
            frame: Source frame (may be empty).
            table: Target table name.
            key: Business-key columns.
            source: Source identifier.
            batch_id: UUID string of this fetch batch.
            fetched_at: Fetch timestamp; defaults to now.

        Returns:
            Rows submitted and chunks committed.
        """
        from sqlalchemy import inspect, text

        if frame.empty:
            return WriteResult(rows=0, batches=0)
        # One connection per call: the staging table is temporary and
        # must survive every chunk of this write.
        with self.engine.begin() as connection:
            table_columns = [column["name"] for column in inspect(connection).get_columns(table)]
            prepared, _plan = plan_frame(
                frame,
                table_columns=table_columns,
                key=key,
                source=source,
                batch_id=batch_id,
                fetched_at=fetched_at,
            )
            batches = 0
            if self.mode == "staging":
                staging = f"_stg_{table}"
                connection.execute(text(build_staging_create_sql(staging, table)))
                try:
                    for chunk in iter_batches(prepared, self.batch_size):
                        connection.execute(text(f"TRUNCATE {_quote(staging)}"))
                        connection.execute(
                            text(build_staging_insert_sql(staging, table_columns)),
                            _records(chunk),
                        )
                        connection.execute(
                            text(build_staging_upsert_sql(table, staging, table_columns, key))
                        )
                        batches += 1
                finally:
                    connection.execute(text(build_staging_drop_sql(staging)))
            else:
                statement = text(build_upsert_sql(table, table_columns, key))
                for chunk in iter_batches(prepared, self.batch_size):
                    connection.execute(statement, _records(chunk))
                    batches += 1
        return WriteResult(rows=len(prepared), batches=batches)


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Convert a frame to parameter dicts (NaN -> None, Python scalars)."""
    cleaned = frame.astype(object).where(pd.notna(frame), None)
    return cast("list[dict[str, object]]", cleaned.to_dict("records"))


def _placeholder(column: str) -> str:
    """Bind-parameter name for a column.

    SQLAlchemy expands ``:name`` client-side into the driver's own
    paramstyle, so the placeholder can carry the column name verbatim
    (Unicode included) - deterministic SQL, no hashing involved.
    """
    return column


def _quote(identifier: str) -> str:
    """Validate and backtick-quote an identifier.

    Args:
        identifier: Table or column name.

    Returns:
        The quoted identifier.

    Raises:
        ValueError: If it is not a safe identifier.
    """
    if not isinstance(identifier, str) or not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"invalid SQL identifier {identifier!r}")
    return f"`{identifier}`"
