"""Import fuyao market-dumps into the ods layer (A3.3b).

The dumps are the bulk path (A3.3 downloads and parses them); this module
turns the parsed frames into the two source-raw ods tables, idempotently:

* ``ods_stock_daily_ths`` - unadjusted daily K only (D10), with the derived
  ``trade_date`` that partitions the table.
* ``ods_stock_action_ths`` - the adjustment-factor events, with the derived
  ``ex_date`` plus an ``event_key`` digest as part of the business key. The
  digest is what makes the import idempotent without collapsing genuinely
  distinct events: the live dump contains rows sharing
  ``(thscode, ex_date_ms)`` with different values, and one exact duplicate.

Frames are prepared as pure functions so they can be tested without a
database; the write itself goes through the existing :class:`OdsWriter`
(key-level upsert, staging path).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from opendata.pipeline.ods_writer import OdsWriter
from opendata_fuyao.dumps import (
    ADJUSTMENT_FACTOR_COLUMNS,
    DAILY_K_COLUMNS,
    DUMP_SPECS,
    UNADJUSTED,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

#: 该导入器只服务已登记的 fuyao 源（其它源走各自的 provider 路径）。
FUYAO_SOURCE = "ths"

#: 事件幂等键长度（sha256 前 32 位十六进制）。
EVENT_KEY_LENGTH = 32

#: 事件幂等键覆盖的载荷列（改变任一列即为不同事件）。
_EVENT_KEY_COLUMNS = (
    "thscode",
    "ex_date_ms",
    "dividend_per_share",
    "per_share_bonus",
    "allotment_ratio",
    "allotment_price",
)


class DumpImportError(RuntimeError):
    """Raised when a dump cannot be imported as requested (fail closed)."""


@dataclass(frozen=True)
class DumpImportStats:
    """What one dump import did.

    Attributes:
        table: Target ods table.
        dump_id: Dump identifier the rows came from.
        rows_in_dump: Rows read from the Parquet file.
        rows_selected: Rows kept after the basis/filter step.
        rows_written: Rows handed to the ods writer.
        batch_id: Batch identifier stamped on the rows.
    """

    table: str
    dump_id: str
    rows_in_dump: int
    rows_selected: int
    rows_written: int
    batch_id: str


def _require_dump_id(dump_id: str) -> str:
    """Validate the dump id against the reviewed catalogue."""
    if not isinstance(dump_id, str) or dump_id.strip() not in DUMP_SPECS:
        raise DumpImportError(f"unregistered dump {dump_id!r}")
    return dump_id.strip()


def _require_source(source: str) -> str:
    """Validate the ods source label."""
    if source != FUYAO_SOURCE:
        raise DumpImportError(f"import only supports source {FUYAO_SOURCE!r}, got {source!r}")
    return source


def _require_batch_id(batch_id: str) -> str:
    """Validate the batch identifier against the ods writer's contract.

    The writer stamps every row with ``_batch_id`` and only accepts a
    canonical UUID string, so the importer checks it here instead of
    letting a well-formed dump fail deep inside the writer.
    """
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise DumpImportError("batch_id must be a non-empty string")
    candidate = batch_id.strip()
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError) as exc:
        raise DumpImportError(f"batch_id {candidate!r} must be a UUID string") from exc
    if str(parsed) != candidate:
        raise DumpImportError(f"batch_id {candidate!r} must be a canonical UUID string")
    return candidate


def _require_columns(frame: pd.DataFrame, expected: tuple[str, ...], *, context: str) -> None:
    """Fail closed when the Parquet schema drifted from the reviewed contract."""
    missing = [name for name in expected if name not in frame.columns]
    if missing:
        raise DumpImportError(f"{context} dump is missing columns: {', '.join(missing)}")


def shanghai_dates(millis: pd.Series) -> pd.Series:
    """Derive Shanghai trading dates from an epoch-millisecond column.

    Args:
        millis: Integer millisecond timestamps.

    Returns:
        A ``date`` series (the upstream millisecond value is the Shanghai
        midnight of the trading day).
    """
    return pd.to_datetime(millis, unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.date


def event_digest(row: pd.Series) -> str:
    """Digest the event payload of one adjustment-factor row.

    Args:
        row: One dump row.

    Returns:
        The first :data:`EVENT_KEY_LENGTH` hex characters of the payload hash.
    """
    payload = "|".join(str(row[column]) for column in _EVENT_KEY_COLUMNS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:EVENT_KEY_LENGTH]


def prepare_daily_k_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Prepare a parsed daily-K dump for the ods table.

    Keeps only the unadjusted basis (D10), derives ``trade_date`` and drops
    the columns the table does not own.

    Args:
        frame: Parsed Parquet frame.

    Returns:
        Frame with the ods columns plus the derived ``trade_date``.

    Raises:
        DumpImportError: Schema drift or no unadjusted rows.
    """
    _require_columns(frame, DAILY_K_COLUMNS, context="daily_k")
    selected = frame[frame["adjusted"].astype(str).str.strip().str.lower() == UNADJUSTED].copy()
    if selected.empty:
        raise DumpImportError(f"daily K dump carries no {UNADJUSTED!r} rows")
    selected["trade_date"] = shanghai_dates(selected["date_ms"])
    return selected[
        [
            "thscode",
            "trade_date",
            "date_ms",
            "currency",
            "interval",
            "adjusted",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "turnover",
        ]
    ]


def prepare_adjustment_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Prepare a parsed adjustment-factor dump for the ods table.

    Derives ``ex_date`` and the ``event_key`` digest, and drops the columns
    the table does not own.

    Args:
        frame: Parsed Parquet frame.

    Returns:
        Frame with the ods columns plus the derived key columns.

    Raises:
        DumpImportError: Schema drift or an empty dump.
    """
    _require_columns(frame, ADJUSTMENT_FACTOR_COLUMNS, context="adjustment")
    selected = frame.copy()
    if selected.empty:
        raise DumpImportError("adjustment dump is empty")
    selected["ex_date"] = shanghai_dates(selected["ex_date_ms"])
    selected["event_key"] = selected[list(_EVENT_KEY_COLUMNS)].apply(event_digest, axis=1)
    return selected[
        [
            "thscode",
            "ticker",
            "ex_date",
            "ex_date_ms",
            "event_key",
            "dividend_per_share",
            "per_share_bonus",
            "allotment_ratio",
            "allotment_price",
            "currency",
        ]
    ]


def _read_parquet(path: Path) -> pd.DataFrame:
    """Read a dump Parquet file.

    Args:
        path: File path.

    Returns:
        The parsed frame.

    Raises:
        DumpImportError: pyarrow is missing or the file is unreadable.
    """
    try:
        return pd.read_parquet(path)
    except ImportError as exc:  # pragma: no cover - engine presence varies
        raise DumpImportError("parquet support is missing (install pyarrow)") from exc
    except (OSError, ValueError) as exc:
        raise DumpImportError(f"dump {path} is unreadable: {exc}") from exc


def import_daily_k_dump(
    engine: Engine,
    path: Path,
    *,
    dump_id: str,
    batch_id: str,
    source: str = FUYAO_SOURCE,
    writer: OdsWriter | None = None,
) -> DumpImportStats:
    """Import a parsed daily-K dump into ``ods_stock_daily_ths``.

    Args:
        engine: Warehouse engine (used to build a default writer).
        path: Local Parquet path (see :func:`opendata_fuyao.dumps.download_dump`).
        dump_id: Dump identifier recorded in the stats.
        batch_id: Batch identifier stamped on every row.
        source: Ods source label (only ``ths``).
        writer: Optional pre-built writer (tests).

    Returns:
        Import statistics.

    Raises:
        DumpImportError: Unknown dump/source, schema drift or no usable rows.
    """
    resolved_dump = _require_dump_id(dump_id)
    resolved_source = _require_source(source)
    resolved_batch = _require_batch_id(batch_id)
    frame = _read_parquet(path)
    prepared = prepare_daily_k_frame(frame)
    active_writer = writer or OdsWriter(engine)
    result = active_writer.write(
        prepared,
        table=f"ods_stock_daily_{resolved_source}",
        key=("thscode", "trade_date"),
        source=resolved_source,
        batch_id=resolved_batch,
    )
    return DumpImportStats(
        table=f"ods_stock_daily_{resolved_source}",
        dump_id=resolved_dump,
        rows_in_dump=len(frame),
        rows_selected=len(prepared),
        rows_written=result.rows,
        batch_id=resolved_batch,
    )


def import_adjustment_factor_dump(
    engine: Engine,
    path: Path,
    *,
    dump_id: str,
    batch_id: str,
    source: str = FUYAO_SOURCE,
    writer: OdsWriter | None = None,
) -> DumpImportStats:
    """Import a parsed adjustment-factor dump into ``ods_stock_action_ths``.

    Args:
        engine: Warehouse engine (used to build a default writer).
        path: Local Parquet path.
        dump_id: Dump identifier recorded in the stats.
        batch_id: Batch identifier stamped on every row.
        source: Ods source label (only ``ths``).
        writer: Optional pre-built writer (tests).

    Returns:
        Import statistics.

    Raises:
        DumpImportError: Unknown dump/source, schema drift or an empty dump.
    """
    resolved_dump = _require_dump_id(dump_id)
    resolved_source = _require_source(source)
    resolved_batch = _require_batch_id(batch_id)
    frame = _read_parquet(path)
    prepared = prepare_adjustment_frame(frame)
    active_writer = writer or OdsWriter(engine)
    result = active_writer.write(
        prepared,
        table=f"ods_stock_action_{resolved_source}",
        key=("thscode", "ex_date", "event_key"),
        source=resolved_source,
        batch_id=resolved_batch,
    )
    return DumpImportStats(
        table=f"ods_stock_action_{resolved_source}",
        dump_id=resolved_dump,
        rows_in_dump=len(frame),
        rows_selected=len(prepared),
        rows_written=result.rows,
        batch_id=resolved_batch,
    )


def new_batch_id() -> str:
    """Build a batch identifier for one import run.

    Returns:
        A canonical UUID string (the only shape the ods writer accepts for
        ``_batch_id``); each dump import run gets its own id so the batch a
        row came from stays traceable.
    """
    return str(uuid.uuid4())


__all__ = [
    "EVENT_KEY_LENGTH",
    "FUYAO_SOURCE",
    "DumpImportError",
    "DumpImportStats",
    "event_digest",
    "import_adjustment_factor_dump",
    "import_daily_k_dump",
    "new_batch_id",
    "prepare_adjustment_frame",
    "prepare_daily_k_frame",
    "shanghai_dates",
]
