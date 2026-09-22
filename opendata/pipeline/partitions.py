"""Partition maintenance for large ods/dwd tables (A4.3, design §8.1).

Large time-series tables are partitioned by year with a ``MAXVALUE``
fallback (the partition trio of A4.1). This module keeps the horizon
alive: a daily check reads ``information_schema.PARTITIONS`` and, when
the latest real upper bound is behind ``current year + years_ahead``,
splits the ``pmax`` partition into the missing yearly partitions.

Without this, new-year rows land in ``pmax``: writes still succeed but
the table stops being prunable, so every date-bounded query degrades
into a full scan (design §8.1 "分区维护任务为强制任务").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

#: Word characters only (Unicode letters included), never a backtick.
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)

#: Name of the fallback partition the maintenance reorganizes.
FALLBACK_PARTITION = "pmax"

#: Design default: keep at least current_year + this many years ready.
DEFAULT_YEARS_AHEAD = 2

_PARTITIONS_QUERY = text(
    "SELECT PARTITION_NAME AS name, PARTITION_DESCRIPTION AS description "
    "FROM information_schema.PARTITIONS "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
    "AND PARTITION_NAME IS NOT NULL "
    "ORDER BY PARTITION_ORDINAL_POSITION"
)


@dataclass(frozen=True)
class PartitionState:
    """One existing partition of a table.

    Attributes:
        name: Partition name (e.g. ``p2026`` / ``pmax``).
        upper_bound: Exclusive upper bound, or None for ``MAXVALUE``.
    """

    name: str
    upper_bound: date | None


def parse_partition_rows(rows: Sequence[tuple[str, str | None]]) -> list[PartitionState]:
    """Parse ``information_schema.PARTITIONS`` rows.

    Args:
        rows: ``(name, description)`` pairs; the description is
            ``'2027-01-01'`` for RANGE COLUMNS date partitions (quotes
            included) and ``MAXVALUE`` for the fallback.

    Returns:
        Partition states in the order given.
    """
    states: list[PartitionState] = []
    for name, description in rows:
        if description is None or description.strip().upper() == "MAXVALUE":
            states.append(PartitionState(name, None))
            continue
        cleaned = description.strip().strip("'")
        bound = date.fromisoformat(cleaned)
        states.append(PartitionState(name, bound))
    return states


def plan_yearly_partitions(
    existing: Sequence[PartitionState],
    *,
    current_year: int,
    years_ahead: int = DEFAULT_YEARS_AHEAD,
) -> list[tuple[str, date]]:
    """Plan the yearly partitions needed to restore the horizon.

    Args:
        existing: Current partitions of the table.
        current_year: Current calendar year.
        years_ahead: How many years beyond the current one must exist
            (design default 2).

    Returns:
        ``(partition_name, upper_bound)`` pairs for the missing years,
        oldest first; empty when the latest upper bound already reaches
        Jan 1 of ``current_year + years_ahead``.

    Raises:
        ValueError: If ``years_ahead`` is not positive.
    """
    if years_ahead <= 0:
        raise ValueError(f"years_ahead must be positive, got {years_ahead}")
    # The rule from design §8.1: the latest real upper bound must be
    # at least Jan 1 of (current year + years_ahead).
    horizon = date(current_year + years_ahead, 1, 1)
    have = {state.name for state in existing}
    latest = max(
        (state.upper_bound for state in existing if state.upper_bound is not None),
        default=None,
    )
    start_year = latest.year if latest is not None else current_year
    plan: list[tuple[str, date]] = []
    for year in range(start_year, horizon.year):
        name = f"p{year}"
        if name not in have:
            plan.append((name, date(year + 1, 1, 1)))
    return plan


def reorganize_sql(table: str, additions: Sequence[tuple[str, date]]) -> str:
    """Build the ``REORGANIZE PARTITION`` that adds yearly partitions.

    The fallback partition is re-created after the additions so the
    table keeps covering every future date.

    Args:
        table: Partitioned table name.
        additions: ``(partition_name, upper_bound)`` pairs to add.

    Returns:
        The ``ALTER TABLE ... REORGANIZE PARTITION pmax INTO (...)``
        statement.

    Raises:
        ValueError: If the table name is unsafe or no partition is
            requested.
    """
    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid SQL identifier {table!r}")
    if not additions:
        raise ValueError("no partitions to add")
    clauses = [
        f"PARTITION {name} VALUES LESS THAN ('{bound.isoformat()}')" for name, bound in additions
    ]
    clauses.append(f"PARTITION {FALLBACK_PARTITION} VALUES LESS THAN (MAXVALUE)")
    return (
        f"ALTER TABLE `{table}` REORGANIZE PARTITION {FALLBACK_PARTITION} "
        f"INTO ({', '.join(clauses)})"
    )


class PartitionMaintainer:
    """Apply the partition horizon rule to warehouse tables."""

    def __init__(self, engine: Engine) -> None:
        """Bind the warehouse engine.

        Args:
            engine: Engine of the warehouse database.
        """
        self.engine = engine

    def is_partitioned(self, table: str) -> bool:
        """Whether the table has partitions at all.

        Unpartitioned InnoDB tables report one ``information_schema``
        row with a NULL partition name; ``REORGANIZE`` on them is an
        error (MySQL 1505), so maintenance must no-op instead.

        Args:
            table: Table name.

        Returns:
            True when at least one partition exists.
        """
        with self.engine.connect() as connection:
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.PARTITIONS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                    "AND PARTITION_NAME IS NOT NULL"
                ),
                {"table": table},
            ).scalar()
        return bool(count)

    def state(self, table: str) -> list[PartitionState]:
        """Read the current partitions of a table.

        Args:
            table: Table name.

        Returns:
            The parsed partition states (empty for an unpartitioned or
            missing table).
        """
        with self.engine.connect() as connection:
            rows = connection.execute(_PARTITIONS_QUERY, {"table": table}).all()
        return parse_partition_rows([(row.name, row.description) for row in rows])

    def ensure(
        self,
        table: str,
        *,
        partition_key: str,
        current_year: int,
        years_ahead: int = DEFAULT_YEARS_AHEAD,
    ) -> list[str]:
        """Extend the partitions of one table when the horizon lags.

        Args:
            table: Partitioned table name.
            partition_key: The partition column (for reporting) - the
                statement itself reorganizes by partition name.
            current_year: Current calendar year.
            years_ahead: Years beyond the current one that must exist.

        Returns:
            Names of the partitions applied by this call (empty for an
            unpartitioned table, which has no horizon to maintain).
        """
        if not self.is_partitioned(table):
            return []
        plan = plan_yearly_partitions(
            self.state(table), current_year=current_year, years_ahead=years_ahead
        )
        if not plan:
            return []
        with self.engine.begin() as connection:
            connection.execute(text(reorganize_sql(table, plan)))
        return [name for name, _ in plan]
