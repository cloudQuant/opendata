"""Subscription hub of the data feed (design §10.2).

The pipeline publishes here when a batch lands; the WebSocket route in
:mod:`opendata.api.data_subscribe` is one consumer of it. Keeping the
fan-out out of the API module is deliberate: the publisher is the
pipeline (step 5 of the six-step orchestration), and a pipeline that
had to import a router to announce its result would invert the
layering.

Three rules from the design shape this module:

* **meta is the first-class citizen.** The default event is a
  notification - domain, source, batch, window, row count - and the
  client pulls the rows over REST. ``full`` is experimental and opt-in.
* **Framing has hard ceilings.** A full frame carries at most
  ``MAX_FRAME_ROWS`` rows and ``MAX_FRAME_BYTES``; a payload past the
  frame ceiling is split into chunks, and one past the total ceiling
  degrades to meta with ``truncated: true`` rather than pushing an
  unbounded frame.
* **A slow client must not stall the pipeline.** Each subscriber owns a
  bounded queue; when it fills, the hub downgrades full payloads to
  meta and finally drops the event, counting both so the condition is
  observable instead of silent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

from loguru import logger

from opendata.pipeline.watermark import BatchWatermark, new_batch_id, utcnow

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

#: Payload modes of a subscription.
PAYLOAD_MODES = ("meta", "full")
#: Layers a subscription may target.
SUBSCRIBABLE_LAYERS = ("ods", "dwd")
#: Rows in a single ``full`` frame.
MAX_FRAME_ROWS = 5_000
#: Bytes in a single ``full`` frame.
MAX_FRAME_BYTES = 2 * 1024 * 1024
#: Rows in a whole ``full`` payload; beyond this it degrades to meta.
MAX_FULL_ROWS = 100_000
#: Bytes in a whole ``full`` payload; beyond this it degrades to meta.
MAX_FULL_BYTES = 10 * 1024 * 1024
#: Domains one connection may subscribe to (design §10.2).
MAX_SUBSCRIPTIONS_PER_CONNECTION = 10
#: Outbound queue depth before the hub starts shedding payload.
MAX_BACKLOG = 1_000


@dataclass(frozen=True)
class Subscription:
    """One connection's interest in one domain.

    Attributes:
        domain: Registered domain identifier.
        layer: Layer the client watches (``dwd`` by default).
        payload: ``meta`` (default) or ``full``.
        symbols: Symbol filter applied to live events; empty means all.
            Replays read the watermark, which stores no symbol set, so
            a replayed event is delivered to the whole domain and the
            client filters it.
    """

    domain: str
    layer: str = "dwd"
    payload: str = "meta"
    symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class BatchEvent:
    """A batch that landed, ready to be framed for subscribers.

    Attributes:
        watermark: The recorded batch.
        symbols: Symbols the batch touched, when the publisher knows
            them (live events do; replays do not).
    """

    watermark: BatchWatermark
    symbols: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        """The domain of the batch."""
        return self.watermark.domain

    @property
    def layer(self) -> str:
        """The layer the batch landed in."""
        return self.watermark.layer

    def matches(self, subscription: Subscription) -> bool:
        """Report whether a subscription wants this event.

        Args:
            subscription: The subscription to test.

        Returns:
            True when domain and layer match and, if the subscription
            names symbols, the batch touched at least one of them.
        """
        if subscription.domain != self.watermark.domain:
            return False
        if subscription.layer != self.watermark.layer:
            return False
        if not subscription.symbols:
            return True
        if not self.symbols:
            # Unknown symbol set (a replay): deliver and let the client
            # filter, rather than silently dropping a real update.
            return True
        return bool(set(subscription.symbols) & set(self.symbols))

    def meta_message(self, *, truncated: bool = False) -> dict[str, Any]:
        """Render the meta notification of this event.

        Args:
            truncated: Whether a ``full`` payload was degraded to this
                meta frame because it exceeded the total ceiling.

        Returns:
            The ``data.update`` message with ``payload: "meta"``.
        """
        message: dict[str, Any] = {
            "type": "data.update",
            "payload": "meta",
            **self.watermark.to_event(),
        }
        if truncated:
            message["truncated"] = True
            message["reason"] = "payload exceeds the full-mode ceiling; pull the batch over REST"
        return message


def frame_full(
    event: BatchEvent,
    rows: Sequence[dict[str, Any]],
    *,
    chunk_id: str | None = None,
) -> list[dict[str, Any]]:
    """Frame a ``full`` payload, splitting or degrading it.

    Args:
        event: The batch being delivered.
        rows: The batch rows to send.
        chunk_id: Chunk group id (generated when omitted; injected by
            tests for deterministic output).

    Returns:
        One message when everything fits, several chunked frames when
        the payload exceeds a single frame, or a single ``meta`` frame
        with ``truncated: true`` when it exceeds the total ceiling.
    """
    payload = [_jsonable(row) for row in rows]
    if len(payload) > MAX_FULL_ROWS or _payload_bytes(payload) > MAX_FULL_BYTES:
        return [event.meta_message(truncated=True)]
    chunks = _split_chunks(payload)
    group = chunk_id or new_batch_id()
    total = len(chunks)
    return [
        {
            "type": "data.update",
            "payload": "full",
            **event.watermark.to_event(),
            "chunk": {"chunk_id": group, "seq": index + 1, "total": total},
            "data": chunk,
        }
        for index, chunk in enumerate(chunks)
    ]


def diff_alert_message(
    *,
    domain: str,
    source_a: str,
    source_b: str,
    batch_id: str,
    mismatch_ratio: float,
    mismatches: int,
    compared: int,
) -> dict[str, Any]:
    """Render a ``data.diff_alert`` broadcast (design §10.2, AC-9).

    Args:
        domain: Domain the cross-check ran on.
        source_a: First source of the pair.
        source_b: Second source of the pair.
        batch_id: Cross-check batch identifier.
        mismatch_ratio: Mismatching share of the compared keys.
        mismatches: Number of mismatching keys.
        compared: Number of compared keys.

    Returns:
        The broadcast message.
    """
    return {
        "type": "data.diff_alert",
        "domain": domain,
        "source_a": source_a,
        "source_b": source_b,
        "batch_id": batch_id,
        "mismatch_ratio": mismatch_ratio,
        "mismatches": mismatches,
        "compared": compared,
        "created_at": utcnow().isoformat(),
    }


@dataclass
class Subscriber:
    """One connected consumer of the hub.

    Attributes:
        send: Async sink the hub writes framed messages to.
        subscriptions: The subscriber's interests, keyed by domain.
        queue: Bounded outbound buffer (``None`` until attached).
        dropped: Events dropped because the queue stayed full.
        downgraded: Full payloads shed down to meta under backpressure.
    """

    send: Callable[[dict[str, Any]], Awaitable[None]]
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    queue: asyncio.Queue[dict[str, Any] | None] | None = None
    dropped: int = 0
    downgraded: int = 0

    def wants_full(self, event: BatchEvent) -> bool:
        """Report whether any matching subscription asked for rows.

        Args:
            event: The batch being delivered.

        Returns:
            True when at least one matching subscription wants ``full``.
        """
        return any(
            subscription.payload == "full" and event.matches(subscription)
            for subscription in self.subscriptions.values()
        )


class SubscriptionHub:
    """Fan-out of batch events to connected subscribers.

    The hub is transport-agnostic: subscribers register an async
    ``send`` callable, which the WebSocket route backs with its socket
    writer. That keeps the pipeline able to publish without importing
    the API layer, and keeps the hub unit-testable without sockets.
    """

    def __init__(self) -> None:
        """Create an empty hub with no subscribers."""
        self._subscribers: list[Subscriber] = []
        self._lock = asyncio.Lock()

    async def register(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> Subscriber:
        """Attach a subscriber.

        Args:
            send: Async sink called with each framed message.

        Returns:
            The subscriber handle (pass it to :meth:`unregister`).
        """
        subscriber = Subscriber(send=send)
        async with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    async def unregister(self, subscriber: Subscriber) -> None:
        """Detach a subscriber.

        Args:
            subscriber: The handle returned by :meth:`register`.
        """
        async with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscriber]

    def attach_queue(
        self, subscriber: Subscriber, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        """Route a subscriber's messages through a bounded queue.

        Args:
            subscriber: The subscriber handle.
            queue: The bounded outbound queue.
        """
        subscriber.queue = queue

    @property
    def subscriber_count(self) -> int:
        """Number of attached subscribers."""
        return len(self._subscribers)

    def wants_full(self, event: BatchEvent) -> bool:
        """Report whether any subscriber wants the rows of an event.

        The publisher asks this before reading the batch back out of
        the warehouse: ``full`` is opt-in and rare, and paying a query
        for it when nobody asked would put that cost on every run.

        Args:
            event: The batch about to be published.

        Returns:
            True when at least one subscriber wants ``full``.
        """
        return any(subscriber.wants_full(event) for subscriber in self._subscribers)

    async def publish_batch(
        self,
        event: BatchEvent,
        *,
        rows: Sequence[dict[str, Any]] | None = None,
    ) -> int:
        """Deliver one batch event to every interested subscriber.

        Args:
            event: The batch that landed.
            rows: Batch rows, supplied only when a subscriber wants
                ``full``; ignored otherwise.

        Returns:
            Number of subscribers the event reached.
        """
        delivered = 0
        for subscriber in list(self._subscribers):
            if not any(event.matches(sub) for sub in subscriber.subscriptions.values()):
                continue
            messages = (
                frame_full(event, rows or ())
                if rows is not None and subscriber.wants_full(event)
                else [event.meta_message()]
            )
            for message in messages:
                await self._deliver(subscriber, message)
            delivered += 1
        return delivered

    async def publish_diff_alert(self, message: dict[str, Any]) -> int:
        """Broadcast a cross-check alert to every subscriber.

        The alert carries no rows and is not domain-filtered beyond the
        subscription's domain: a subscriber that watches a domain hears
        about that domain's differences.

        Args:
            message: The alert message (see :func:`diff_alert_message`).

        Returns:
            Number of subscribers the alert reached.
        """
        domain = message.get("domain")
        delivered = 0
        for subscriber in list(self._subscribers):
            if not any(sub.domain == domain for sub in subscriber.subscriptions.values()):
                continue
            await self._deliver(subscriber, message)
            delivered += 1
        return delivered

    async def _deliver(self, subscriber: Subscriber, message: dict[str, Any]) -> None:
        """Hand one message to a subscriber, shedding under backlog.

        Args:
            subscriber: Target subscriber.
            message: The framed message.
        """
        queue = subscriber.queue
        if queue is None:
            try:
                await subscriber.send(message)
            except Exception as exc:  # a broken sink must not stop the fan-out
                logger.debug(f"subscription send failed: {exc}")
            return
        # Shed the payload *before* enqueueing, not after a failed put: a
        # full queue has no room for the downgraded frame either, so
        # "downgrade on QueueFull" would be dead code. Degrading from
        # half-full keeps notifications flowing under pressure - the
        # rows are recoverable over REST, a lost notification is not.
        degraded = message.get("payload") == "full" and queue.qsize() >= _degrade_threshold(queue)
        if degraded:
            message = _downgrade(message)
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            subscriber.dropped += 1
            logger.warning(
                f"subscription backlog full, dropping event "
                f"(dropped={subscriber.dropped} type={message.get('type')})"
            )
            return
        if degraded:
            # Counted only once the smaller frame actually made it in, so
            # "downgraded" means "delivered as meta" rather than "tried".
            subscriber.downgraded += 1
            logger.warning(
                f"subscription backlog high, degraded full payload to meta "
                f"(downgraded={subscriber.downgraded})"
            )


def _degrade_threshold(queue: asyncio.Queue) -> int:
    """Backlog depth at which a full payload is shed down to meta.

    Derived from the queue's own bound rather than a second constant, so
    the two can never drift apart: the socket's queue is the backlog
    limit.

    Args:
        queue: A bounded outbound queue.

    Returns:
        The depth at which ``full`` frames degrade (at least 1).
    """
    return max(1, (queue.maxsize if queue.maxsize > 0 else MAX_BACKLOG) // 2)


def _downgrade(message: dict[str, Any]) -> dict[str, Any]:
    """Turn a full frame into its meta equivalent.

    Args:
        message: A ``payload: "full"`` frame.

    Returns:
        The same event with the rows stripped and ``truncated`` set.
    """
    meta = {key: value for key, value in message.items() if key not in {"data", "chunk"}}
    meta["payload"] = "meta"
    meta["truncated"] = True
    meta["reason"] = "subscriber backlog exceeded; pull the batch over REST"
    return meta


def _split_chunks(payload: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split rows into frames bounded by count and size.

    Args:
        payload: JSON-ready rows.

    Returns:
        One or more row groups; a single empty group for an empty
        payload, so ``full`` always emits at least one frame.
    """
    if not payload:
        return [[]]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0
    for row in payload:
        size = _row_bytes(row)
        if current and (len(current) >= MAX_FRAME_ROWS or current_bytes + size > MAX_FRAME_BYTES):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(row)
        current_bytes += size
    chunks.append(current)
    return chunks


def _payload_bytes(payload: Sequence[dict[str, Any]]) -> int:
    """Serialized size of a payload.

    Args:
        payload: JSON-ready rows.

    Returns:
        Byte length of the JSON encoding.
    """
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))


def _row_bytes(row: dict[str, Any]) -> int:
    """Serialized size of one row.

    Args:
        row: A JSON-ready row.

    Returns:
        Byte length of the JSON encoding.
    """
    return len(json.dumps(row, default=str, ensure_ascii=False).encode("utf-8"))


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """Render one warehouse row as JSON-ready values.

    Args:
        row: A row as read from the warehouse.

    Returns:
        The row with dates and other non-JSON scalars stringified.
    """
    output: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, date):
            output[key] = value.isoformat()
        else:
            output[key] = value
    return output


#: Process-wide hub. The pipeline publishes into it and every WebSocket
#: route registers with it (single-process deployment, design §9.3).
hub = SubscriptionHub()


async def publish_batch(
    watermark: BatchWatermark,
    *,
    symbols: Sequence[str] = (),
    rows: Sequence[dict[str, Any]] | None = None,
) -> int:
    """Publish one batch into the process-wide hub.

    Args:
        watermark: The recorded batch.
        symbols: Symbols the batch touched, when known.
        rows: Batch rows, supplied only when a subscriber wants full.

    Returns:
        Number of subscribers reached.
    """
    return await hub.publish_batch(
        BatchEvent(watermark=watermark, symbols=tuple(symbols)), rows=rows
    )
