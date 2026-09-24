"""Batch watermark store for the WS data subscription (design §10.2).

Every finished pipeline run appends one row to ``batch_watermark``: the
``batch_id`` it stamped on its rows, the domain/source/layer it wrote
and the window and row count of the batch. The table is the durable
half of the subscription protocol - a client that reconnects with
``since_batch_id`` gets every meta event it missed replayed from here,
which is what turns "the socket dropped" from "events are lost
forever" into "the client catches up".

Ordering is by the auto-increment ``seq``, not by ``created_at``:
timestamps collide at millisecond resolution inside a burst of batches,
and the replay contract needs a total order that never ties.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

#: Warehouse table holding the batch watermarks.
WATERMARK_TABLE = "batch_watermark"
#: Default page size of a replay.
DEFAULT_REPLAY_LIMIT = 500
#: Hard ceiling of a replay (a client that fell far behind uses REST).
MAX_REPLAY_LIMIT = 5_000


@dataclass(frozen=True)
class BatchWatermark:
    """One recorded batch.

    Attributes:
        batch_id: Identifier stamped on the batch's ods rows.
        domain: Registered domain identifier.
        source: Source identifier.
        layer: Layer the batch landed in (``ods`` / ``dwd``).
        window_start: First date of the batch window, if known.
        window_end: Last date of the batch window, if known.
        rows: Rows written by the batch.
        created_at: When the batch finished (UTC).
        seq: Monotonic sequence number assigned by the warehouse.
    """

    batch_id: str
    domain: str
    source: str
    layer: str
    window_start: date | None
    window_end: date | None
    rows: int
    created_at: datetime
    seq: int = 0

    def to_event(self) -> dict[str, object]:
        """Render the ``data.update`` meta payload of this batch.

        Returns:
            The event fields shared by live pushes and replays, so a
            replayed event is byte-identical to the one the client
            would have received live.
        """
        return {
            "domain": self.domain,
            "source": self.source,
            "layer": self.layer,
            "batch_id": self.batch_id,
            "window": {
                "start": None if self.window_start is None else self.window_start.isoformat(),
                "end": None if self.window_end is None else self.window_end.isoformat(),
            },
            "rows": self.rows,
            "created_at": self.created_at.isoformat(),
        }


def new_batch_id() -> str:
    """Return a fresh batch identifier.

    Returns:
        A canonical UUID4 string, the same shape the ods writer stamps
        on its ``_batch_id`` column.
    """
    return str(uuid.uuid4())


def record_batch(engine: Engine, watermark: BatchWatermark) -> None:
    """Append one batch to the watermark table.

    The insert is idempotent by ``batch_id``: a pipeline that retries a
    shard re-records the same batch and the replay still sees one
    event, not one per attempt.

    Args:
        engine: Warehouse engine.
        watermark: The batch to record.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO `{WATERMARK_TABLE}` "  # noqa: S608  # module constant table
                "(`batch_id`, `domain`, `source`, `layer`, `window_start`, `window_end`, "
                "`rows_written`, `created_at`) "
                "VALUES (:batch_id, :domain, :source, :layer, :window_start, :window_end, "
                ":rows, :created_at) "
                "ON DUPLICATE KEY UPDATE `rows_written` = VALUES(`rows_written`)"
            ),
            {
                "batch_id": watermark.batch_id,
                "domain": watermark.domain,
                "source": watermark.source,
                "layer": watermark.layer,
                "window_start": watermark.window_start,
                "window_end": watermark.window_end,
                "rows": watermark.rows,
                "created_at": watermark.created_at,
            },
        )


def replay_since(
    engine: Engine,
    *,
    domain: str,
    since_batch_id: str,
    limit: int = DEFAULT_REPLAY_LIMIT,
) -> list[BatchWatermark]:
    """Return the batches of one domain recorded after ``since_batch_id``.

    The reference batch itself is excluded. An unknown
    ``since_batch_id`` (older than retention, or never recorded) is not
    an error: the caller gets the most recent ``limit`` batches, which
    is the honest answer to "I lost track - show me what I missed".

    Args:
        engine: Warehouse engine.
        domain: Domain whose batches to replay.
        since_batch_id: Last batch the client already processed.
        limit: Maximum events to return (capped at
            ``MAX_REPLAY_LIMIT``).

    Returns:
        The batches in write order (oldest first).
    """
    bounded = max(1, min(limit, MAX_REPLAY_LIMIT))
    rows = _fetch(
        engine,
        "SELECT `batch_id`, `domain`, `source`, `layer`, `window_start`, `window_end`, "  # noqa: S608
        "`rows_written`, `created_at`, `seq` FROM `" + WATERMARK_TABLE + "` "
        "WHERE `domain` = :domain AND `seq` > COALESCE("
        "(SELECT `seq` FROM `" + WATERMARK_TABLE + "` WHERE `batch_id` = :since), 0) "
        "ORDER BY `seq` ASC LIMIT :limit",
        {"domain": domain, "since": since_batch_id, "limit": bounded},
    )
    return [_to_watermark(row) for row in rows]


def latest_batches(
    engine: Engine,
    *,
    domain: str,
    limit: int = DEFAULT_REPLAY_LIMIT,
) -> list[BatchWatermark]:
    """Return the most recent batches of one domain, oldest first.

    Used when a client subscribes without a watermark: it gets the tail
    of the stream so it can resync without a REST round trip.

    Args:
        engine: Warehouse engine.
        domain: Domain whose batches to read.
        limit: Maximum events to return (capped at
            ``MAX_REPLAY_LIMIT``).

    Returns:
        The batches in write order (oldest first).
    """
    bounded = max(1, min(limit, MAX_REPLAY_LIMIT))
    rows = _fetch(
        engine,
        f"SELECT `batch_id`, `domain`, `source`, `layer`, `window_start`, `window_end`, "  # noqa: S608
        f"`rows_written`, `created_at`, `seq` FROM (SELECT * FROM `{WATERMARK_TABLE}` "
        f"WHERE `domain` = :domain ORDER BY `seq` DESC LIMIT :limit) AS tail "
        f"ORDER BY `seq` ASC",
        {"domain": domain, "limit": bounded},
    )
    return [_to_watermark(row) for row in rows]


def _fetch(engine: Engine, sql: str, params: dict[str, object]) -> list[dict[str, Any]]:
    """Run a read query and return dict rows (sync)."""
    with engine.connect() as connection:
        result = connection.execute(text(sql), params)
        columns = list(result.keys())
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _to_watermark(row: dict[str, Any]) -> BatchWatermark:
    """Build a watermark from a raw row."""
    return BatchWatermark(
        batch_id=str(row["batch_id"]),
        domain=str(row["domain"]),
        source=str(row["source"]),
        layer=str(row["layer"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        rows=int(row["rows_written"]),
        created_at=row["created_at"],
        seq=int(row["seq"]),
    )


def utcnow() -> datetime:
    """Return the current UTC time (single clock for the watermark rows).

    Returns:
        A timezone-aware UTC datetime.
    """
    return datetime.now(timezone.utc)


def domains_of(watermarks: Sequence[BatchWatermark]) -> list[str]:
    """Return the distinct domains of a watermark list, in order.

    Args:
        watermarks: The batches.

    Returns:
        Distinct domain identifiers.
    """
    seen: list[str] = []
    for watermark in watermarks:
        if watermark.domain not in seen:
            seen.append(watermark.domain)
    return seen
