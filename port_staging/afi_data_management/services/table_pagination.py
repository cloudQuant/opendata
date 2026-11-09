"""Deterministic ordering and bounded totals for warehouse table pagination.

The generic warehouse browser (``GET /api/v1/data/tables/{table_id}/data``) serves
arbitrary physical tables created by many different collection scripts, so it may
not assume any particular column layout.  ``LIMIT/OFFSET`` without ``ORDER BY`` is
not a stable pagination: MySQL may return rows in any order per statement, so a
row can be served on two pages while another is served on none, and any caller
reconciling source bytes against warehouse rows page by page gets an unreliable
sample.  This module derives a *total order* from the reflected schema and refuses
to page a table for which no such order is provable.

Ordering ladder - the first level that resolves is used, and each level is a total
order over the rows of the table:

1. ``primary_key``: the table's PRIMARY KEY columns.  A primary key is unique and
   NOT NULL in every warehouse dialect this platform uses (MySQL in production,
   SQLite in tests/development), so exactly one row carries each key value and no
   row carries NULL; ascending component-wise order over all key columns therefore
   ranks every row of the table.
2. ``unique_key``: a UNIQUE constraint whose columns are all declared NOT NULL.
   Uniqueness alone is not sufficient - MySQL and SQLite both permit several NULLs
   in a unique column, and rows with a NULL key component are not totally ordered
   relative to each other.  Candidates are ranked deterministically (fewest columns
   first, then column names) so the same schema always yields the same ordering.
3. ``rowid``: SQLite only.  Every rowid (i.e. non ``WITHOUT ROWID``) table has an
   implicit unique, NOT NULL, integer ``rowid`` that can be selected and ordered
   on, so it is a real key.  MySQL/InnoDB has no selectable hidden row id (its
   internal DB_ROW_ID is not addressable in SQL), so a keyless MySQL table cannot
   use this level.

If no level resolves, paging fails closed with
:class:`StableOrderingUnavailableError` instead of silently serving unordered
pages.  The remedy is schema-side: give the table a PRIMARY KEY, or a NOT NULL
UNIQUE key (the ``data_fetch`` scripts already do this with their ``R_ID`` column).

Total counts: an exact ``COUNT(*)`` is expensive and its cost does not depend on
``page_size``, so re-measuring it for every page request multiplies cost by the
number of pages a user walks.  :class:`TotalCountCache` measures it once per
(database, table, filter) shape and reuses it; see its docstring for the exact
staleness contract.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

# Identifiers we are willing to embed in SQL.  ``\w`` is Unicode-aware for ``str``
# patterns, so the native Chinese column names used by several warehouse tables
# (``日期``, ``成交量``, ...) are accepted; anything else is refused.
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)

STRATEGY_PRIMARY_KEY = "primary_key"
STRATEGY_UNIQUE_KEY = "unique_key"
STRATEGY_ROWID = "rowid"
_ROWID_COLUMN = "rowid"


class StableOrderingUnavailableError(RuntimeError):
    """Raised when a table has no provable total order, so paging must not run.

    Failing closed is deliberate: an unordered ``LIMIT/OFFSET`` page looks like a
    normal page to the caller but can duplicate one row and drop another.
    """


@dataclass(frozen=True)
class TableOrdering:
    """A provable total order for one warehouse table."""

    strategy: str
    columns: tuple[str, ...]

    def as_response(self) -> dict[str, Any]:
        """Return the ordering as it appears in the API payload.

        Exposing the key lets a caller see exactly what it paged by, and detect a
        table whose ordering key changed between requests.
        """
        return {
            "strategy": self.strategy,
            "columns": list(self.columns),
            "direction": "asc",
            "is_total_order": True,
        }


def quote_identifier(identifier: str) -> str:
    """Quote one warehouse identifier for MySQL and SQLite.

    Both dialects accept backtick-delimited identifiers with embedded backticks
    doubled, so this is a single quoting rule for the whole ladder.  Names that are
    not plain identifiers are rejected: a key column we cannot quote safely is not
    a usable ordering key, and the caller fails closed.
    """
    name = str(identifier)
    if "`" in name or not _IDENTIFIER_RE.match(name):
        raise StableOrderingUnavailableError(
            f"Cannot use identifier {name!r} as an ordering key: it is not a plain name"
        )
    return f"`{name}`"


def _unique_key_candidates(unique_constraints: Sequence[Sequence[str]]) -> list[tuple[str, ...]]:
    """Rank unique-key candidates deterministically: fewest columns, then names.

    Sorting keeps the choice reproducible across connections and dialects; without
    it, two equivalent unique keys could be picked in different orders and each
    request might page by a different key.
    """
    candidates = [
        tuple(str(column) for column in columns) for columns in unique_constraints if columns
    ]
    return sorted(candidates, key=lambda columns: (len(columns), columns))


def resolve_ordering(
    *,
    table_name: str,
    dialect_name: str,
    pk_columns: Sequence[str],
    unique_constraints: Sequence[Sequence[str]],
    column_nullable: Mapping[str, bool],
    has_selectable_rowid: bool = True,
) -> TableOrdering:
    """Derive a total order from reflected schema facts, or fail closed.

    ``column_nullable`` maps column name to its declared nullability; it is only a
    hard requirement for unique keys (a primary key is NOT NULL by definition in
    the dialects we support, and SQLite backfills a NULL integer primary key with a
    fresh rowid, so stored key values are never NULL there).
    """
    pk = [str(column) for column in pk_columns if str(column)]
    if pk:
        try:
            return TableOrdering(STRATEGY_PRIMARY_KEY, _validate_key_columns(pk))
        except StableOrderingUnavailableError:
            # A primary key on an unquotable name is unusable; fall through to the
            # next ladder level rather than emitting SQL we cannot trust.
            pass

    for candidate in _unique_key_candidates(unique_constraints):
        names = [str(column) for column in candidate]
        if not names:
            continue
        if any(name not in column_nullable for name in names):
            continue
        if any(column_nullable[name] for name in names):
            # NULL key components are not totally ordered, so this key is not stable.
            continue
        try:
            return TableOrdering(STRATEGY_UNIQUE_KEY, _validate_key_columns(names))
        except StableOrderingUnavailableError:
            continue

    if dialect_name == "sqlite" and has_selectable_rowid:
        # SQLite rowid tables expose an implicit unique, NOT NULL integer key.
        return TableOrdering(STRATEGY_ROWID, (_ROWID_COLUMN,))

    raise StableOrderingUnavailableError(
        f"Warehouse table {table_name} has no provable stable ordering key for dialect "
        f"{dialect_name!r}: PRIMARY KEY={list(pk) or 'none'}, "
        f"UNIQUE candidates={_unique_key_candidates(unique_constraints) or 'none'}, "
        f"selectable rowid={dialect_name == 'sqlite' and has_selectable_rowid}. "
        "LIMIT/OFFSET without a total order can serve one row on two pages and another on "
        "none, so paging is refused. Add a PRIMARY KEY, or a UNIQUE key whose columns are "
        "all NOT NULL, to make the table browsable."
    )


def _validate_key_columns(names: Sequence[str]) -> tuple[str, ...]:
    """Validate that every ordering key name can be embedded in SQL."""
    for name in names:
        quote_identifier(name)
    return tuple(str(name) for name in names)


def order_by_clause(ordering: TableOrdering) -> str:
    """Render the ``ORDER BY`` fragment for one resolved ordering."""
    if not ordering.columns:  # pragma: no cover - resolve_ordering never returns empty
        raise StableOrderingUnavailableError("Ordering key has no columns")
    return "ORDER BY " + ", ".join(f"{quote_identifier(column)} ASC" for column in ordering.columns)


def _sqlite_table_has_rowid(sync_conn: Connection, table_name: str) -> bool:
    """Return True unless the SQLite table was declared ``WITHOUT ROWID``."""
    row = sync_conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).first()
    if row is None or row[0] is None:
        # Temporary tables are not in sqlite_master; they still have a rowid.
        return True
    return "without rowid" not in str(row[0]).lower()


def _named_columns(value: object) -> list[str]:
    """Normalize a reflected ``column_names``/``constrained_columns`` entry."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(column) for column in value if column is not None]


def reflect_table_ordering(sync_conn: Connection, table_name: str) -> TableOrdering:
    """Reflect one table's key constraints and resolve its ordering.

    Reflection is a handful of catalog queries and runs on every request on purpose:
    a cached ordering could keep serving pages after the key that made them stable
    was dropped.  The row *values* are always read live; only totals are cached.
    """
    inspector = inspect(sync_conn)
    dialect_name = sync_conn.dialect.name
    pk_constraint = inspector.get_pk_constraint(table_name) or {}
    pk_columns = _named_columns(pk_constraint.get("constrained_columns"))
    unique_constraints = [
        _named_columns(constraint.get("column_names"))
        for constraint in inspector.get_unique_constraints(table_name)
    ]
    # Standalone unique indexes are as good a key as an inline unique constraint, and
    # the dialects do not agree on where they report them: MySQL returns them from
    # ``get_unique_constraints``, SQLite only from ``get_indexes``.  Collect both and
    # let the ladder decide.
    unique_constraints.extend(
        _named_columns(index.get("column_names"))
        for index in inspector.get_indexes(table_name)
        if index.get("unique")
    )
    columns = inspector.get_columns(table_name)
    if not columns:
        raise StableOrderingUnavailableError(
            f"Warehouse table {table_name} does not exist or has no columns"
        )
    column_nullable = {
        str(column["name"]): bool(column.get("nullable", True)) for column in columns
    }
    has_selectable_rowid = dialect_name != "sqlite" or _sqlite_table_has_rowid(
        sync_conn, table_name
    )
    return resolve_ordering(
        table_name=table_name,
        dialect_name=dialect_name,
        pk_columns=pk_columns,
        unique_constraints=unique_constraints,
        column_nullable=column_nullable,
        has_selectable_rowid=has_selectable_rowid,
    )


async def resolve_total_count(
    conn: AsyncConnection,
    *,
    count_sql: str,
    cache: TotalCountCache,
    key: TotalCountKey,
    params: Mapping[str, Any] | None = None,
) -> tuple[int, str, float]:
    """Return ``(total, source, age_seconds)`` for one query shape.

    ``source`` is ``"live"`` when the exact ``COUNT(*)`` ran for this request and
    ``"cached"`` when the value came from the bounded cache, so a stale total is
    never presented as an authoritative one.

    ``params`` binds the predicate of a filtered count.  It is never interpolated
    into ``count_sql``; the caller must also fold the same predicate into ``key``
    (see :class:`TotalCountKey`) so a filtered total cannot be reused unfiltered.
    """
    cached = cache.get(key)
    if cached is not None:
        total, age_seconds = cached
        return total, "cached", age_seconds
    result = await conn.execute(text(count_sql), params or {})
    total = int(result.scalar() or 0)
    cache.put(key, total)
    return total, "live", 0.0


@dataclass(frozen=True)
class TotalCountKey:
    """Cache key for one exact total: (database, table, filter) query shape.

    ``filters`` is part of the key so a filtered total can never be reused for an
    unfiltered page (or vice versa).  Any new ``WHERE`` support must fold its
    predicate into this field before it touches the cache.
    """

    database: str
    table_name: str
    filters: tuple[tuple[str, str], ...] = ()

    @property
    def table_bucket(self) -> str:
        return f"{self.database}\x00{self.table_name}"


class TotalCountCache:
    """Bounded TTL cache of exact ``COUNT(*)`` results per query shape.

    Staleness contract - stated precisely because a total drives the pager UI:

    * Values are exact counts measured against the warehouse; nothing is estimated
      and no number is invented.  A miss runs ``COUNT(*)`` for that shape.
    * A hit is returned only within ``ttl_seconds`` and is always labelled
      ``source="cached"`` together with its measured age, so a consumer can tell a
      reused total from one measured for this request.
    * Writes made through this process evict the affected table immediately
      (``evict_table``), so a page after a local write re-measures.
    * The warehouse is *also* written by out-of-process collection scripts, which
      cannot reach this cache.  Their writes therefore become visible in the total
      with a delay of at most ``ttl_seconds``.  Page rows themselves are always
      live, so a fresh page can briefly disagree with the cached total by up to
      that window - which is disclosed, not hidden.
    * Keys are LRU-bounded, so long-lived processes cannot grow the cache with the
      number of tables browsed.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[TotalCountKey, tuple[int, float]] = OrderedDict()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def get(self, key: TotalCountKey) -> tuple[int, float] | None:
        """Return ``(total, age_seconds)`` for an entry younger than the TTL, else None.

        An entry that has reached ``ttl_seconds`` is treated as expired, so "cached"
        always means strictly younger than the stated window.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            age_seconds = float(self._clock()) - entry[1]
            if age_seconds >= self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return entry[0], max(age_seconds, 0.0)

    def put(self, key: TotalCountKey, total: int) -> None:
        with self._lock:
            self._entries[key] = (int(total), float(self._clock()))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def evict_table(self, table_name: str) -> None:
        """Drop every cached total for one table, across databases and filters."""
        with self._lock:
            for key in [key for key in self._entries if key.table_name == str(table_name)]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_TOTAL_COUNT_CACHE = TotalCountCache()


def get_total_count_cache() -> TotalCountCache:
    """Return the process-wide total cache used by the warehouse browser."""
    return _TOTAL_COUNT_CACHE


@dataclass(frozen=True)
class WarehouseTablePage:
    """One deterministic page of a warehouse table plus the facts behind it."""

    columns: list[str]
    rows: list[dict[str, Any]]
    total: int
    total_source: str
    total_age_seconds: float
    ordering: TableOrdering
    # ``None`` when the physical table has no ``adjustment`` column, so tables
    # without that column keep the exact pre-existing response shape.
    adjustment_basis: dict[str, Any] | None = None
