"""Bounded single-source ODS-to-DWD backfill for an explicit date window.

The backfill reads one registered source leg at a time, applies the
source's declared mapping, and reuses the normal single-source DWD
merge and key-upsert writer. It never creates warehouse tables or
selects a source implicitly.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pandas as pd
from sqlalchemy import MetaData, Table, and_, inspect, or_, select

from opendata.data.domains import dwd_table, ods_table, require_domain
from opendata.data.mapping import DomainMapping, normalize_frame, require_domain_mapping
from opendata.pipeline.dwd_merge import TRACE_COLUMNS, DwdWriter, merge_source_frames
from opendata.pipeline.query import resolve_time_field

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.sql.elements import ColumnElement


DEFAULT_PAGE_SIZE = 2_000


class BackfillWriter(Protocol):
    """The key-upsert seam used by a backfill run."""

    def write(self, frame: pd.DataFrame, *, table: str, key: Sequence[str]) -> int:
        """Persist one bounded DWD chunk and return submitted rows."""


@dataclass(frozen=True)
class DwdBackfillStats:
    """Counters for a completed or refused backfill selection."""

    domain: str
    source: str
    start: date
    end: date
    ods_table: str
    dwd_table: str
    preflight_pages: int = 0
    rows_preflighted: int = 0
    pages_read: int = 0
    rows_read: int = 0
    rows_normalized: int = 0
    rows_merged: int = 0
    rows_written: int = 0
    diff_flagged: int = 0
    collision_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-ready representation with ISO date bounds."""
        values = asdict(self)
        values["start"] = self.start.isoformat()
        values["end"] = self.end.isoformat()
        return values


class DwdBackfillError(RuntimeError):
    """A failed backfill with an honest snapshot of completed work."""

    def __init__(self, message: str, *, stats: DwdBackfillStats) -> None:
        """Initialize with the failure message and completed-work counters.

        Args:
            message: Human-readable failure reason.
            stats: Exact progress reached before the failure.
        """
        super().__init__(message)
        self.stats = stats


@dataclass
class _Progress:
    """Mutable internal counters; snapshots are immutable for callers."""

    preflight_pages: int = 0
    rows_preflighted: int = 0
    pages_read: int = 0
    rows_read: int = 0
    rows_normalized: int = 0
    rows_merged: int = 0
    rows_written: int = 0
    diff_flagged: int = 0
    collision_keys: list[str] = field(default_factory=list)

    def snapshot(
        self,
        *,
        domain: str,
        source: str,
        start: date,
        end: date,
        source_table: str,
        target_table: str,
    ) -> DwdBackfillStats:
        """Freeze the counters alongside the requested selection."""
        return DwdBackfillStats(
            domain=domain,
            source=source,
            start=start,
            end=end,
            ods_table=source_table,
            dwd_table=target_table,
            preflight_pages=self.preflight_pages,
            rows_preflighted=self.rows_preflighted,
            pages_read=self.pages_read,
            rows_read=self.rows_read,
            rows_normalized=self.rows_normalized,
            rows_merged=self.rows_merged,
            rows_written=self.rows_written,
            diff_flagged=self.diff_flagged,
            collision_keys=tuple(self.collision_keys),
        )


def backfill_dwd(
    engine: Engine,
    *,
    domain: str,
    source: str,
    start: date,
    end: date,
    page_size: int = DEFAULT_PAGE_SIZE,
    writer: BackfillWriter | None = None,
    merged_at: datetime | None = None,
) -> DwdBackfillStats:
    """Backfill one explicitly selected mapped source into its DWD table.

    A key-ordered preflight checks the whole bounded ODS selection for
    normalized-key collisions before the first DWD upsert. The execution
    pass then reads and writes one page at a time. If a later database
    error follows committed pages, the error carries those counters and
    states that repeating the same selection is safe because writes are
    key-level upserts.

    Args:
        engine: Injected warehouse engine; tests may use an isolated local
            engine. This function never creates an engine itself.
        domain: Registered warehouse domain.
        source: Explicit registered source leg; ``auto`` is not accepted.
        start: Inclusive contract-date lower bound.
        end: Inclusive contract-date upper bound.
        page_size: Maximum rows held per read/merge/write page.
        writer: Optional key-upsert writer seam; defaults to :class:`DwdWriter`.
        merged_at: Timezone-aware timestamp to stamp, defaulting to current UTC.

    Returns:
        Exact scan, normalization, merge, write, and collision counters.

    Raises:
        DwdBackfillError: A normalized key is ambiguous or a page fails
            after the target selection has been validated.
        LookupError: The domain/source mapping or provider capability is
            not registered.
        ValueError: The selection or reflected table contracts are invalid.
    """
    if source == "auto":
        raise ValueError("DWD backfill requires an explicit source; source='auto' is refused")
    if type(start) is not date or type(end) is not date:
        raise ValueError("start and end must be date values, not datetimes")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    if page_size <= 0:
        raise ValueError(f"page_size must be positive, got {page_size}")

    require_domain(domain)
    mapping = require_domain_mapping(source, domain)
    if mapping.pivot is not None:
        raise ValueError(
            f"{domain}/{source} declares a wide-frame pivot; direct ODS backfill "
            "requires a 1:1 source mapping"
        )
    _require_registered_capability(domain, source)

    time_field = resolve_time_field(domain)
    if time_field not in mapping.fields:
        raise ValueError(f"mapping for {domain}/{source} lacks date field {time_field!r}")
    if time_field not in mapping.key:
        raise ValueError(f"date field {time_field!r} is not part of the DWD business key")
    time_column = mapping.fields[time_field].source_column
    source_table = ods_table(domain, source)
    target_table = dwd_table(domain)
    key = tuple(mapping.key)
    source_key = tuple(mapping.source_key)
    primary_key = _inspect_tables(
        engine,
        source_table=source_table,
        target_table=target_table,
        source_key=source_key,
        target_key=key,
        time_column=time_column,
        mapping=mapping,
    )
    ods = Table(source_table, MetaData(), autoload_with=engine)
    dwd_writer = writer or DwdWriter(engine)
    stamp = merged_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("merged_at must be timezone-aware")
    stamp = stamp.astimezone(timezone.utc)
    progress = _Progress()

    source_connection = engine.connect()
    try:
        if engine.dialect.name == "mysql":
            # Keep preflight and execution on one consistent InnoDB read view even
            # when the engine was configured with a weaker isolation level.
            source_connection = source_connection.execution_options(
                isolation_level="REPEATABLE READ"
            )
        with source_connection.begin():
            _preflight_collisions(
                source_connection,
                ods,
                mapping=mapping,
                time_column=time_column,
                primary_key=primary_key,
                start=start,
                end=end,
                page_size=page_size,
                progress=progress,
                domain=domain,
                source=source,
                source_table=source_table,
                target_table=target_table,
            )

            cursor: tuple[object, ...] | None = None
            while True:
                try:
                    rows = _read_page(
                        source_connection,
                        ods,
                        time_column=time_column,
                        primary_key=primary_key,
                        start=start,
                        end=end,
                        page_size=page_size,
                        after=cursor,
                    )
                except Exception as exc:
                    raise DwdBackfillError(
                        "DWD backfill read failed after any earlier pages were committed; "
                        "rerunning the same explicit selection is safe",
                        stats=progress.snapshot(
                            domain=domain,
                            source=source,
                            start=start,
                            end=end,
                            source_table=source_table,
                            target_table=target_table,
                        ),
                    ) from exc
                if not rows:
                    break
                progress.pages_read += 1
                progress.rows_read += len(rows)
                cursor = tuple(rows[-1][column] for column in primary_key)

                try:
                    normalized = normalize_frame(pd.DataFrame(rows), mapping)
                    progress.rows_normalized += len(normalized)
                    merged, merge_stats = merge_source_frames(
                        domain,
                        {source: normalized},
                        authority=(source,),
                        key=key,
                        as_of=stamp.date(),
                        merged_at=stamp,
                    )
                    if merge_stats.colliding:
                        progress.collision_keys.extend(merge_stats.colliding)
                        raise DwdBackfillError(
                            f"source {source!r} has duplicate normalized DWD keys; refusing "
                            "to choose a row",
                            stats=progress.snapshot(
                                domain=domain,
                                source=source,
                                start=start,
                                end=end,
                                source_table=source_table,
                                target_table=target_table,
                            ),
                        )
                    progress.rows_merged += len(merged)
                    progress.diff_flagged += merge_stats.diff_flagged
                    rows_written = dwd_writer.write(merged, table=target_table, key=key)
                    progress.rows_written += rows_written
                    if rows_written != len(merged):
                        raise RuntimeError(
                            f"writer reported {rows_written} rows for a {len(merged)}-row page"
                        )
                except DwdBackfillError:
                    raise
                except Exception as exc:
                    raise DwdBackfillError(
                        "DWD backfill page failed after any earlier pages were committed; "
                        "rerunning the same explicit selection is safe because writes are "
                        "key-level upserts",
                        stats=progress.snapshot(
                            domain=domain,
                            source=source,
                            start=start,
                            end=end,
                            source_table=source_table,
                            target_table=target_table,
                        ),
                    ) from exc
    finally:
        source_connection.close()

    return progress.snapshot(
        domain=domain,
        source=source,
        start=start,
        end=end,
        source_table=source_table,
        target_table=target_table,
    )


def _require_registered_capability(domain: str, source: str) -> None:
    """Require an explicit registered capability without routing through auto."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    if not any(
        capability.domain == domain and capability.source == source
        for capability in get_registry().capabilities()
    ):
        raise LookupError(f"source {source!r} has no registered capability for {domain!r}")


def _inspect_tables(
    engine: Engine,
    *,
    source_table: str,
    target_table: str,
    source_key: Sequence[str],
    target_key: Sequence[str],
    time_column: str,
    mapping: DomainMapping,
) -> tuple[str, ...]:
    """Validate existing table shapes; backfill never auto-creates DDL."""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    missing_tables = [table for table in (source_table, target_table) if table not in table_names]
    if missing_tables:
        raise LookupError(f"required warehouse tables are missing: {missing_tables}")

    source_columns = {column["name"] for column in inspector.get_columns(source_table)}
    target_columns = {column["name"] for column in inspector.get_columns(target_table)}
    mapped_columns = {field.source_column for field in mapping.fields.values()}
    required_source_columns = mapped_columns | {time_column, *source_key}
    missing_source_columns = sorted(required_source_columns - source_columns)
    if missing_source_columns:
        raise ValueError(
            f"ODS table {source_table!r} lacks mapped columns {missing_source_columns}"
        )
    required_target_columns = set(mapping.fields) | set(TRACE_COLUMNS)
    missing_target_columns = sorted(required_target_columns - target_columns)
    if missing_target_columns:
        raise ValueError(
            f"DWD table {target_table!r} lacks contract/trace columns {missing_target_columns}"
        )

    source_pk = tuple(
        (inspector.get_pk_constraint(source_table) or {}).get("constrained_columns") or ()
    )
    if not source_pk:
        raise ValueError(f"ODS table {source_table!r} must have a primary key for stable paging")
    if source_pk[: len(source_key)] != tuple(source_key):
        raise ValueError(
            f"ODS primary key {source_pk} must start with mapped source key {tuple(source_key)}; "
            "normalized DWD-key groups could otherwise cross a page silently"
        )
    if tuple(target_key) != tuple(
        (inspector.get_pk_constraint(target_table) or {}).get("constrained_columns") or ()
    ):
        raise ValueError(
            f"DWD primary key of {target_table!r} must match mapped business key "
            f"{tuple(target_key)}"
        )
    if time_column not in source_pk[: len(source_key)]:
        raise ValueError("mapped window date column must be part of the ODS business key")
    return source_pk


def _preflight_collisions(
    connection: Connection,
    table: Table,
    *,
    mapping: DomainMapping,
    time_column: str,
    primary_key: Sequence[str],
    start: date,
    end: date,
    page_size: int,
    progress: _Progress,
    domain: str,
    source: str,
    source_table: str,
    target_table: str,
) -> None:
    """Spool all normalized keys to disk and reject collisions before writes.

    The source primary-key order need not place aliases that normalize to the
    same DWD key next to each other. A temporary SQLite unique index catches
    those aliases across the entire selection without retaining its keys in
    Python memory.
    """
    cursor: tuple[object, ...] | None = None
    try:
        with (
            tempfile.TemporaryDirectory(prefix="opendata-dwd-preflight-") as temp_dir,
            sqlite3.connect(Path(temp_dir) / "normalized-keys.sqlite3") as spool,
        ):
            spool.execute("PRAGMA cache_size = -2048")
            spool.execute("PRAGMA temp_store = FILE")
            spool.execute("CREATE TABLE seen_keys (key_signature TEXT PRIMARY KEY)")
            while True:
                rows = _read_page(
                    connection,
                    table,
                    time_column=time_column,
                    primary_key=primary_key,
                    start=start,
                    end=end,
                    page_size=page_size,
                    after=cursor,
                )
                if not rows:
                    return
                progress.preflight_pages += 1
                progress.rows_preflighted += len(rows)
                for row in rows:
                    contract_key = _row_contract_key(row, mapping)
                    signature = _canonical_key_signature(contract_key)
                    try:
                        spool.execute(
                            "INSERT INTO seen_keys (key_signature) VALUES (?)", (signature,)
                        )
                    except sqlite3.IntegrityError as exc:
                        rendered = f"{source}: {contract_key!r}"
                        progress.collision_keys.append(rendered)
                        raise DwdBackfillError(
                            f"ambiguous normalized DWD key {contract_key!r} in "
                            f"{source_table!r}; no rows were written. Resolve source rows "
                            "before rerunning",
                            stats=progress.snapshot(
                                domain=domain,
                                source=source,
                                start=start,
                                end=end,
                                source_table=source_table,
                                target_table=target_table,
                            ),
                        ) from exc
                spool.commit()
                cursor = tuple(rows[-1][column] for column in primary_key)
    except DwdBackfillError:
        raise
    except Exception as exc:
        raise DwdBackfillError(
            "DWD backfill preflight failed; no DWD rows were written",
            stats=progress.snapshot(
                domain=domain,
                source=source,
                start=start,
                end=end,
                source_table=source_table,
                target_table=target_table,
            ),
        ) from exc


def _canonical_key_signature(key: Sequence[object]) -> str:
    """Encode supported scalar key types without conflating their values."""
    return json.dumps(
        [_canonical_key_value(value) for value in key],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _canonical_key_value(value: object) -> object:
    """Type-tag a scalar so strings, dates, and numbers stay distinct."""
    if value is None:
        return ["none", None]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value.hex()]
    item = getattr(value, "item", None)
    if callable(item):
        return _canonical_key_value(item())
    raise TypeError(f"unsupported normalized DWD key type {type(value).__name__}")


def _row_contract_key(row: Mapping[str, object], mapping: DomainMapping) -> tuple[object, ...]:
    """Normalize one raw ODS source key into the mapped DWD spelling."""
    raw_key = tuple(row[column] for column in mapping.source_key)
    return mapping.to_contract_key(raw_key)


def _read_page(
    connection: Connection,
    table: Table,
    *,
    time_column: str,
    primary_key: Sequence[str],
    start: date,
    end: date,
    page_size: int,
    after: tuple[object, ...] | None,
) -> list[dict[str, object]]:
    """Read one bounded page with stable composite-key keyset pagination."""
    predicates = [table.c[time_column] >= start, table.c[time_column] <= end]
    if after is not None:
        predicates.append(_after_key(table, primary_key, after))
    statement = (
        select(table)
        .where(and_(*predicates))
        .order_by(*(table.c[column] for column in primary_key))
        .limit(page_size)
    )
    result = connection.execute(statement)
    return [dict(row) for row in result.mappings().all()]


def _after_key(
    table: Table, primary_key: Sequence[str], cursor: Sequence[object]
) -> ColumnElement[bool]:
    """Build the lexicographic continuation predicate for a composite key."""
    terms = []
    for index, column in enumerate(primary_key):
        prefix = [
            table.c[name] == cursor[prefix_index]
            for prefix_index, name in enumerate(primary_key[:index])
        ]
        terms.append(and_(*prefix, table.c[column] > cursor[index]))
    return or_(*terms)
