"""Pipeline step 5: record the batch and push it to subscribers (§10.2).

The runner's fifth hook. It does two things in one place because they
must not drift apart:

1. **record** - append the finished batch to ``batch_watermark``, which
   is what makes a reconnect able to replay what it missed;
2. **publish** - hand the batch to the subscription hub for live push.

The order matters: the watermark is written first, so a subscriber that
receives an event can always replay from the ``batch_id`` it carries -
if the event were published first, a client reconnecting inside that
window would find no watermark row and silently lose the batch.

``full`` payloads are read back from the warehouse by ``_batch_id``,
and only when a subscriber actually asked for them (the hub answers
that question before we pay for the query).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import text

from opendata.pipeline.subscription import BatchEvent, hub
from opendata.pipeline.watermark import (
    BatchWatermark,
    new_batch_id,
    record_batch,
    utcnow,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from opendata.pipeline.runner import PipelineContext

#: Cap on the rows read back for a ``full`` payload. Past it the hub
#: degrades to meta anyway, so reading further would be wasted work.
MAX_FULL_FETCH_ROWS = 100_000


@dataclass(frozen=True)
class NotifyResult:
    """What one notify-hook call did.

    Attributes:
        watermark: The recorded batch.
        delivered: Subscribers the event reached.
        rows_read: Rows read back for a ``full`` payload (0 for meta).
    """

    watermark: BatchWatermark
    delivered: int
    rows_read: int


class BatchNotifier:
    """Step-5 hook: record one finished batch and push it live."""

    def __init__(
        self,
        engine: Engine,
        *,
        batch_id: str | None = None,
        layer: str = "ods",
        table: str | None = None,
    ) -> None:
        """Bind the notifier to one pipeline run.

        Args:
            engine: Warehouse engine (watermark rows, full-payload reads).
            batch_id: The ods batch id this run stamps on its rows; None
                generates one (only correct when the writer does too).
            layer: Layer the batch landed in.
            table: Warehouse table to read a ``full`` payload from;
                derived from the context when omitted.
        """
        self.engine = engine
        self.batch_id = batch_id
        self.layer = layer
        self.table = table

    async def __call__(self, context: PipelineContext) -> NotifyResult:
        """Record and publish the batch of one pipeline run.

        Args:
            context: The runner's step context.

        Returns:
            What the call recorded and delivered. Failures inside the
            push are swallowed (a notification problem must not fail a
            pipeline that already wrote its data), so ``delivered`` is
            the count the hub managed to reach.
        """
        batch_id = self.batch_id or new_batch_id()
        rows = await asyncio.to_thread(self._count_rows, context, batch_id)
        watermark = BatchWatermark(
            batch_id=batch_id,
            domain=context.domain,
            source=context.source,
            layer=self.layer,
            window_start=context.window.start,
            window_end=context.window.end,
            rows=rows,
            created_at=utcnow(),
        )
        await asyncio.to_thread(record_batch, self.engine, watermark)
        event = BatchEvent(watermark=watermark, symbols=_symbols_of(context))
        payload = None
        if hub.wants_full(event):
            payload = await asyncio.to_thread(self._read_rows, context, batch_id)
        try:
            delivered = await hub.publish_batch(event, rows=payload)
        except Exception as exc:  # the data is already durable; push is best-effort
            logger.warning(f"batch notification failed for {batch_id}: {exc}")
            return NotifyResult(watermark=watermark, delivered=0, rows_read=0)
        return NotifyResult(
            watermark=watermark,
            delivered=delivered,
            rows_read=0 if payload is None else len(payload),
        )

    def _table(self, context: PipelineContext) -> str:
        """Resolve the table to read the batch back from."""
        if self.table is not None:
            return self.table
        from opendata.data.domains import ods_table

        return ods_table(context.domain, context.source)

    def _count_rows(self, context: PipelineContext, batch_id: str) -> int:
        """Count the rows this batch stamped in the warehouse.

        The count is read back rather than taken from the runner's
        counter because the writer is idempotent: a re-run of a shard
        overwrites rows, so "rows written by this run" and "rows of
        this batch" are not the same number.

        Args:
            context: The runner's step context.
            batch_id: The batch to count.

        Returns:
            The row count, or the affected-key count when the table
            cannot be read (the event still carries a usable figure).
        """
        try:
            with self.engine.connect() as connection:
                result = connection.execute(
                    text(
                        f"SELECT COUNT(*) FROM `{self._table(context)}` "  # noqa: S608  # registry-derived table
                        "WHERE `_batch_id` = :id"
                    ),
                    {"id": batch_id},
                )
                return int(result.scalar_one())
        except Exception as exc:
            logger.debug(f"batch row count unavailable: {exc}")
            return len(context.affected_keys)

    def _read_rows(self, context: PipelineContext, batch_id: str) -> list[dict[str, Any]]:
        """Read a batch back out of the warehouse for a ``full`` payload.

        Args:
            context: The runner's step context.
            batch_id: The batch to read.

        Returns:
            The batch rows (empty when the read fails - the hub then
            degrades the frame to meta).
        """
        try:
            with self.engine.connect() as connection:
                result = connection.execute(
                    text(
                        f"SELECT * FROM `{self._table(context)}` "  # noqa: S608  # registry-derived table
                        "WHERE `_batch_id` = :id LIMIT :limit"
                    ),
                    {"id": batch_id, "limit": MAX_FULL_FETCH_ROWS},
                )
                columns = list(result.keys())
                return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
        except Exception as exc:
            logger.warning(f"full payload read failed for batch {batch_id}: {exc}")
            return []


def _symbols_of(context: PipelineContext) -> tuple[str, ...]:
    """Extract the distinct symbols a run touched.

    The affected keys are business-key tuples whose first element is the
    symbol for every shipped domain (``(symbol, trade_date)`` and
    friends), so the first element is what the subscription's symbol
    filter matches against.

    Args:
        context: The runner's step context.

    Returns:
        Distinct symbol identifiers, in first-seen order.
    """
    seen: list[str] = []
    for key in context.affected_keys:
        if not key:
            continue
        symbol = str(key[0])
        if symbol not in seen:
            seen.append(symbol)
    return tuple(seen)


def build_notify_hook(
    engine: Engine,
    *,
    batch_id: str | None = None,
    layer: str = "ods",
    table: str | None = None,
) -> BatchNotifier:
    """Build the step-5 hook of a pipeline (design §10.2).

    Args:
        engine: Warehouse engine.
        batch_id: The ods batch id the run stamps on its rows.
        layer: Layer the batch lands in.
        table: Warehouse table override (tests).

    Returns:
        The hook, ready to be passed to ``DataPipeline(notify=...)``.
    """
    return BatchNotifier(engine, batch_id=batch_id, layer=layer, table=table)
