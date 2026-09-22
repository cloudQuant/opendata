"""Pipeline six-step orchestration with shard-level resume (A4.4).

Design §9.1/§9.2:

```
DataPipeline(domain, source)
  1 fetch        one symbol at a time (per-symbol failures never block the shard)
  2 ods upsert   injected ``write_ods`` (the A4.2 writer), shard by shard
  3 cross-check  injected hook (A4.5)
  4 dwd merge    injected hook (A4.6)
  5 ws push      injected hook (§10.2)
  6 meta         progress rows in ``pipeline_progress``
```

Every shard is checkpointed in ``pipeline_progress``: a restarted
pipeline loads the ``done`` shards of the same deterministic
``pipeline_id`` (domain, source and window) and skips them. Shards
that fail are marked ``failed`` and retried on the next run, while a
single failing symbol only lands in the outcome's failure list - the
rest of its shard is still written.

Concurrency is guarded by a process-internal lock per
``(domain, source)`` (design §9.3: single-process deployment, a
distributed lock is deferred with multi-worker support).
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from opendata.models.pipeline import PipelineProgress, ShardStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import pandas as pd
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    #: Fetch one symbol for one window; raises on any source failure.
    FetchSymbol = Callable[[str, "Window"], "pd.DataFrame"]
    #: Write one shard frame into ods; returns rows written.
    WriteOds = Callable[["pd.DataFrame"], int]
    #: Step hooks (A4.5 / A4.6 / §10.2); None means the step is skipped.
    Hook = Callable[["PipelineContext"], Awaitable[None]]


@dataclass(frozen=True)
class Window:
    """Inclusive date window of one run.

    Attributes:
        start: First date (inclusive).
        end: Last date (inclusive).
    """

    start: date
    end: date

    def label(self) -> str:
        """Return the ``start..end`` label used in ids and logs."""
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True)
class PipelineSpec:
    """What to run and how to shard it.

    Attributes:
        domain: Registered domain identifier.
        source: Data source identifier.
        key: Business-key columns of the ods table.
        symbols: Symbols to process (the shard universe).
        shard_size: Symbols per shard (design: e.g. 500).
    """

    domain: str
    source: str
    key: tuple[str, ...]
    symbols: Sequence[str]
    shard_size: int = 500


@dataclass(frozen=True)
class ShardFailure:
    """One symbol that could not be fetched or written.

    Attributes:
        shard: Shard index the symbol belongs to.
        symbol: Symbol identifier, or None for a shard-wide failure.
        error: Failure description.
    """

    shard: int
    symbol: str | None
    error: str


@dataclass(frozen=True)
class PipelineContext:
    """What the step hooks receive.

    Attributes:
        domain: Domain identifier.
        source: Source identifier.
        window: Window of this run.
        affected_keys: Business keys written by this run (the union of
            the processed shards' keys).
    """

    domain: str
    source: str
    window: Window
    affected_keys: Sequence[tuple[object, ...]]


@dataclass
class PipelineOutcome:
    """Result of one pipeline run.

    Attributes:
        pipeline_id: Deterministic run id (domain, source, window).
        shards_total: Shards in this run's plan.
        shards_done: Shards completed by this run.
        shards_failed: Shards marked failed by this run.
        resumed_shards: Shards skipped because an earlier run finished them.
        rows_written: Rows written to ods by this run.
        failures: Per-symbol and per-shard failures.
    """

    pipeline_id: str
    shards_total: int = 0
    shards_done: int = 0
    shards_failed: int = 0
    resumed_shards: int = 0
    rows_written: int = 0
    failures: list[ShardFailure] = field(default_factory=list)


class PipelineLockedError(RuntimeError):
    """Raised when another run of the same domain/source holds the lock."""


_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _pipeline_lock(key: str) -> asyncio.Lock:
    """Return the process-internal lock of a domain/source pair.

    Args:
        key: ``domain:source``.

    Returns:
        The shared lock (created on first use).
    """
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


def release_pipeline_lock(key: str) -> None:
    """Drop a lock from the registry (test/teardown convenience).

    Args:
        key: ``domain:source``.
    """
    with _LOCKS_GUARD:
        _LOCKS.pop(key, None)


def plan_shards(symbols: Sequence[str], *, shard_size: int) -> list[list[str]]:
    """Split the symbol universe into fixed-size shards.

    Args:
        symbols: Symbols to process.
        shard_size: Maximum symbols per shard (must be positive).

    Returns:
        Shards in order; empty when there are no symbols.

    Raises:
        ValueError: If ``shard_size`` is not positive.
    """
    if shard_size <= 0:
        raise ValueError(f"shard_size must be positive, got {shard_size}")
    return [
        list(symbols[start : start + shard_size]) for start in range(0, len(symbols), shard_size)
    ]


class DataPipeline:
    """Run the six-step pipeline for one domain and source."""

    def __init__(
        self,
        spec: PipelineSpec,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        write_ods: WriteOds,
        fetch_symbol: FetchSymbol,
        cross_check: Hook | None = None,
        merge: Hook | None = None,
        notify: Hook | None = None,
    ) -> None:
        """Bind the pipeline's dependencies.

        Args:
            spec: Domain, source, key, symbols and shard size.
            session_maker: Main-database session factory (progress rows).
            write_ods: Ods write callable (A4.2 writer); returns rows written.
            fetch_symbol: Fetch one symbol for the window.
            cross_check: Step 3 hook (A4.5); None skips the step.
            merge: Step 4 hook (A4.6); None skips the step.
            notify: Step 5 hook (§10.2); None skips the step.
        """
        self.spec = spec
        self.session_maker = session_maker
        self.write_ods = write_ods
        self.fetch_symbol = fetch_symbol
        self.cross_check = cross_check
        self.merge = merge
        self.notify = notify

    @property
    def lock_key(self) -> str:
        """Process-internal lock key of this pipeline."""
        return f"{self.spec.domain}:{self.spec.source}"

    async def run(self, window: Window, *, resume: bool = True) -> PipelineOutcome:
        """Run the pipeline, resuming completed shards by default.

        Args:
            window: Date window of this run.
            resume: Skip shards an earlier run of the same window
                already completed.

        Returns:
            The run outcome (per-shard counters and failures).

        Raises:
            PipelineLockedError: If another run of this domain/source is
                still in flight.
        """
        lock = _pipeline_lock(self.lock_key)
        if lock.locked():
            raise PipelineLockedError(f"pipeline {self.lock_key} is already running")
        await lock.acquire()
        try:
            return await self._run_locked(window, resume=resume)
        finally:
            lock.release()

    async def _run_locked(self, window: Window, *, resume: bool) -> PipelineOutcome:
        """Execute the plan while the lock is held."""
        pipeline_id = self._pipeline_id(window)
        shards = plan_shards(self.spec.symbols, shard_size=self.spec.shard_size)
        outcome = PipelineOutcome(pipeline_id=pipeline_id, shards_total=len(shards))
        completed = await self._completed_shards(pipeline_id) if resume else set()
        affected: list[tuple[object, ...]] = []
        for index, symbols in enumerate(shards):
            if index in completed:
                outcome.resumed_shards += 1
                continue
            await self._record(pipeline_id, index, window, ShardStatus.RUNNING)
            frame, failures = self._fetch_shard(index, symbols, window)
            outcome.failures.extend(failures)
            if frame is None or frame.empty:
                error = failures[0].error if failures else "no rows returned"
                await self._record(pipeline_id, index, window, ShardStatus.FAILED, error=error)
                outcome.shards_failed += 1
                continue
            try:
                rows = self.write_ods(frame)
            except Exception as exc:  # shard-level failure: record and move on
                await self._record(pipeline_id, index, window, ShardStatus.FAILED, error=str(exc))
                outcome.shards_failed += 1
                outcome.failures.append(ShardFailure(index, None, str(exc)))
                continue
            await self._record(pipeline_id, index, window, ShardStatus.DONE, rows=rows)
            outcome.shards_done += 1
            outcome.rows_written += rows
            affected.extend(self._affected_keys(frame))
        if outcome.shards_done or outcome.shards_failed:
            await self._run_hooks(
                PipelineContext(
                    domain=self.spec.domain,
                    source=self.spec.source,
                    window=window,
                    affected_keys=affected,
                )
            )
        return outcome

    def _pipeline_id(self, window: Window) -> str:
        """Deterministic id so a restart of the same window resumes."""
        return f"{self.spec.domain}:{self.spec.source}:{window.label()}"

    def _fetch_shard(
        self, index: int, symbols: Sequence[str], window: Window
    ) -> tuple[pd.DataFrame | None, list[ShardFailure]]:
        """Fetch every symbol of one shard, isolating failures."""
        import pandas as pd

        frames: list[pd.DataFrame] = []
        failures: list[ShardFailure] = []
        for symbol in symbols:
            try:
                frame = self.fetch_symbol(symbol, window)
            except Exception as exc:  # per-symbol isolation (design §9.1)
                failures.append(ShardFailure(index, symbol, f"{type(exc).__name__}: {exc}"))
                continue
            if frame is not None and not frame.empty:
                frames.append(frame)
        if not frames:
            return None, failures
        return pd.concat(frames, ignore_index=True), failures

    def _affected_keys(self, frame: pd.DataFrame) -> list[tuple[object, ...]]:
        """Extract the distinct business keys of a written frame."""
        columns = list(self.spec.key)
        records = frame[columns].drop_duplicates().itertuples(index=False, name=None)
        return [tuple(record) for record in records]

    async def _completed_shards(self, pipeline_id: str) -> set[int]:
        """Load the shard indexes an earlier run already finished."""
        async with self.session_maker() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineProgress.shard).where(
                            PipelineProgress.pipeline_id == pipeline_id,
                            PipelineProgress.status == ShardStatus.DONE,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

    async def _record(
        self,
        pipeline_id: str,
        shard: int,
        window: Window,
        status: ShardStatus,
        *,
        rows: int = 0,
        error: str | None = None,
    ) -> None:
        """Upsert one shard's progress row (idempotent by key)."""
        async with self.session_maker() as session:
            existing = (
                await session.execute(
                    select(PipelineProgress).where(
                        PipelineProgress.pipeline_id == pipeline_id,
                        PipelineProgress.shard == shard,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    PipelineProgress(
                        pipeline_id=pipeline_id,
                        domain=self.spec.domain,
                        source=self.spec.source,
                        shard=shard,
                        window_start=window.start,
                        window_end=window.end,
                        status=status,
                        rows_written=rows,
                        error=error,
                    )
                )
            else:
                existing.status = status
                existing.rows_written = rows
                existing.error = error
                existing.updated_at = datetime.now(timezone.utc)
            await session.commit()

    async def _run_hooks(self, context: PipelineContext) -> None:
        """Run the injected step hooks (3-5) in design order."""
        for hook in (self.cross_check, self.merge, self.notify):
            if hook is not None:
                await hook(context)
