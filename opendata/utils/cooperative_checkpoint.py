"""Small, execution-local throttle for cooperative lease renewal."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class _CooperativeCheckpoint:
    """Run a lease callback at a bounded cadence without owning its state."""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        interval_seconds: float = 0.2,
        immediate: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create one local throttle around a lease-renewal callback.

        Args:
            callback: Synchronous lease renewal. Its exceptions are propagated.
            interval_seconds: Maximum time between successful renewals.
            immediate: Whether the first invocation renews unconditionally.
            clock: Monotonic clock, injectable only to make cadence tests deterministic.

        Raises:
            TypeError: If the callback or clock is not callable.
            ValueError: If the interval is not finite and positive.
        """
        if not callable(callback) or not callable(clock):
            raise TypeError("checkpoint callback and clock must be callable")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("checkpoint interval must be finite and positive")
        self._callback = callback
        self._interval_seconds = float(interval_seconds)
        self._clock = clock
        self._last_success = None if immediate else self._clock()

    def __call__(self) -> None:
        """Renew when due; do not swallow cancellation or lease-loss errors."""
        now = self._clock()
        if self._last_success is None or now - self._last_success >= self._interval_seconds:
            self._callback()
            self._last_success = self._clock()

    def mark_renewed(self) -> None:
        """Reset the cadence after an explicit, successful phase checkpoint."""
        self._last_success = self._clock()
