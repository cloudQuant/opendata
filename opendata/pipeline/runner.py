"""Pipeline six-step orchestration with shard-level resume (A4.4).

Design §9.1/§9.2:

```
DataPipeline(domain, source)
  1 fetch        one symbol at a time (per-symbol failures never block the shard)
  2 ods upsert   injected ``write_ods`` (the A4.2 writer), shard by shard
  3 cross-check  injected hook (A4.5)
  4 dwd merge    injected hook (A4.6)
  5 ws push      injected hook (§10.2)
  6 meta         exact ODS/DWD aggregates in main-database ``data_tables``
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
import hashlib
import inspect
import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from opendata.models.pipeline import PipelineProgress, ShardStatus
from opendata.pipeline.affected_keys import AffectedKeySpool, SymbolPartitioning

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
    from collections.abc import Mapping as MappingType

    import pandas as pd
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    #: Fetch one symbol for one window; raises on any source failure.
    FetchSymbol = Callable[[str, "Window"], "pd.DataFrame | Awaitable[pd.DataFrame]"]
    #: Write one shard frame into ods; returns rows written.
    WriteOds = Callable[["pd.DataFrame"], int]
    #: Step hooks (A4.5 / A4.6 / §10.2); None means the step is skipped.
    #: Hooks may return a value (their stats/summary) - the runner
    #: ignores it, callers and tests use it.
    Hook = Callable[["PipelineContext"], Awaitable[object]]
    LoadAffectedKeys = Callable[["PipelineContext"], Iterable[tuple[object, ...]]]


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
        variant: Stable caller-defined request options included in the run id.
    """

    domain: str
    source: str
    key: tuple[str, ...]
    symbols: Sequence[str]
    shard_size: int = 500
    variant: str = ""

    @property
    def table(self) -> str:
        """The ods table this pipeline writes (derived, design §8.1)."""
        from opendata.data.domains import ods_table

        return ods_table(self.domain, self.source)


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
        symbols: Requested universe retained for scoped hook reads.
        source_windows: Effective fetch windows by source and symbol.
        comparison_windows: Per-symbol union shared by cross-check and merge.
        pipeline_id: Fingerprinted run id used to isolate hook reports.
    """

    domain: str
    source: str
    window: Window
    affected_keys: Sequence[tuple[object, ...]]
    symbols: Sequence[str] = ()
    source_windows: MappingType[str, MappingType[str, Window | None]] = field(default_factory=dict)
    comparison_windows: MappingType[str, Window | None] = field(default_factory=dict)
    pipeline_id: str = ""
    partition_contexts: Callable[[], Iterator[PipelineContext]] | None = None


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
        source_windows: Persisted effective windows by source and symbol.
    """

    pipeline_id: str
    shards_total: int = 0
    shards_done: int = 0
    shards_failed: int = 0
    resumed_shards: int = 0
    rows_written: int = 0
    failures: list[ShardFailure] = field(default_factory=list)
    source_windows: dict[str, dict[str, Window | None]] = field(default_factory=dict)


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
        meta: Hook | None = None,
        load_affected_keys: LoadAffectedKeys | None = None,
        bounded_partitions: SymbolPartitioning | None = None,
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
            meta: Step 6 DataTable metadata hook; None skips the step.
            load_affected_keys: Rebuild scoped ODS keys if every shard was
                skipped during resume.
            bounded_partitions: Optional symbol partitioning for bounded
                downstream hooks. Omitted for the generic legacy path.
        """
        self.spec = spec
        self.session_maker = session_maker
        self.write_ods = write_ods
        self.fetch_symbol = fetch_symbol
        self.cross_check = cross_check
        self.merge = merge
        self.notify = notify
        self.meta = meta
        self.load_affected_keys = load_affected_keys
        self.bounded_partitions = bounded_partitions

    @property
    def lock_key(self) -> str:
        """Process-internal lock key of this pipeline."""
        return f"{self.spec.domain}:{self.spec.source}"

    async def run(
        self,
        window: Window,
        *,
        resume: bool = True,
        source_windows: MappingType[str, MappingType[str, Window | None]] | None = None,
    ) -> PipelineOutcome:
        """Run the pipeline, resuming completed shards by default.

        Args:
            window: Date window of this run.
            resume: Skip shards an earlier run of the same window
                already completed.
            source_windows: Optional per-source, per-symbol effective
                fetch windows. ``None`` for a symbol means that source is
                already current through the requested end.

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
            return await self._run_locked(window, resume=resume, source_windows=source_windows)
        finally:
            lock.release()

    async def _run_locked(
        self,
        window: Window,
        *,
        resume: bool,
        source_windows: MappingType[str, MappingType[str, Window | None]] | None,
    ) -> PipelineOutcome:
        if self.bounded_partitions is None:
            return await self._run_locked_impl(
                window, resume=resume, source_windows=source_windows, spool=None
            )
        with AffectedKeySpool() as spool:
            return await self._run_locked_impl(
                window, resume=resume, source_windows=source_windows, spool=spool
            )

    async def _run_locked_impl(
        self,
        window: Window,
        *,
        resume: bool,
        source_windows: MappingType[str, MappingType[str, Window | None]] | None,
        spool: AffectedKeySpool | None,
    ) -> PipelineOutcome:
        """Execute the plan while the lock is held."""
        pipeline_id = self._pipeline_id(window)
        symbols = sorted(set(self.spec.symbols))
        request_partitions = self._request_partitions(symbols)
        allowed_partition_symbols = set(request_partitions.values())
        shards = plan_shards(symbols, shard_size=self.spec.shard_size)
        outcome = PipelineOutcome(pipeline_id=pipeline_id, shards_total=len(shards))
        completed = await self._completed_shards(pipeline_id) if resume else set()
        if not resume:
            await self._clear_step_state(pipeline_id)
        resolved_windows = await self._resolve_source_windows(
            pipeline_id,
            window,
            symbols,
            source_windows,
            resume=resume,
        )
        current_windows = resolved_windows.get(self.spec.source, {})
        current_windows = {symbol: current_windows.get(symbol, window) for symbol in symbols}
        comparison_windows = _union_symbol_windows(symbols, resolved_windows)
        outcome.source_windows = resolved_windows
        affected: list[tuple[object, ...]] = []
        for index, symbols in enumerate(shards):
            if index in completed:
                outcome.resumed_shards += 1
                continue
            await self._record(pipeline_id, index, window, ShardStatus.RUNNING)
            active_symbols = [
                symbol for symbol in symbols if current_windows.get(symbol) is not None
            ]
            if not active_symbols:
                await self._record(pipeline_id, index, window, ShardStatus.DONE, rows=0)
                outcome.shards_done += 1
                continue
            frame, failures = await self._fetch_shard(
                index, active_symbols, current_windows, window
            )
            outcome.failures.extend(failures)
            if frame is None or frame.empty:
                error = failures[0].error if failures else "no rows returned"
                await self._record(pipeline_id, index, window, ShardStatus.FAILED, error=error)
                outcome.shards_failed += 1
                continue
            # Invalidate before the warehouse commit. If the process stops
            # after ODS commits, a previously completed merge/notification
            # cannot be mistaken for the new data's downstream state.
            await self._invalidate_hooks(pipeline_id)
            try:
                rows = self.write_ods(frame)
            except Exception as exc:  # shard-level failure: record and move on
                await self._record(pipeline_id, index, window, ShardStatus.FAILED, error=str(exc))
                outcome.shards_failed += 1
                outcome.failures.append(ShardFailure(index, None, str(exc)))
                continue
            outcome.rows_written += rows
            if spool is None:
                affected.extend(self._affected_keys(frame))
            else:
                partitioning = self._require_bounded_partitions()
                for key in self._iter_affected_keys(frame):
                    normalized_symbol = partitioning.for_key(key)
                    if normalized_symbol not in allowed_partition_symbols:
                        raise ValueError(
                            "affected key symbol is outside the normalized request universe"
                        )
                    spool.append(
                        key,
                        symbol=normalized_symbol,
                        dedupe_key=partitioning.canonicalize_key(key),
                    )
            if failures:
                error = "; ".join(failure.error for failure in failures)
                await self._record(
                    pipeline_id,
                    index,
                    window,
                    ShardStatus.FAILED,
                    rows=rows,
                    error=error,
                )
                outcome.shards_failed += 1
            else:
                await self._record(pipeline_id, index, window, ShardStatus.DONE, rows=rows)
                outcome.shards_done += 1

        hook_window = _enclosing_window(comparison_windows, fallback=window)
        context = PipelineContext(
            domain=self.spec.domain,
            source=self.spec.source,
            window=hook_window,
            affected_keys=affected if spool is None else spool,
            symbols=tuple(sorted(set(self.spec.symbols))),
            source_windows=resolved_windows,
            comparison_windows=comparison_windows,
            pipeline_id=pipeline_id,
        )
        if spool is not None:
            context = self._with_partition_contexts(
                context, spool=spool, request_partitions=request_partitions
            )
        has_comparison_rows = any(value is not None for value in comparison_windows.values())
        no_current_fetches = not any(value is not None for value in current_windows.values())
        if spool is not None:
            all_shards_done = (
                outcome.shards_failed == 0
                and outcome.shards_done + outcome.resumed_shards == outcome.shards_total
            )
            hook_names = [
                name
                for name, hook in (
                    ("cross_check", self.cross_check),
                    ("merge", self.merge),
                    ("notify", self.notify),
                    ("meta", self.meta),
                )
                if hook is not None
            ]
            all_hooks_done = resume and all(
                [await self._step_done(pipeline_id, name) for name in hook_names]
            )
            if all_shards_done and all_hooks_done:
                return outcome
        if (
            self.load_affected_keys is not None
            and has_comparison_rows
            and (bool(completed) or no_current_fetches)
        ):
            if spool is None:
                loaded = self.load_affected_keys(context)
                affected.extend(key for key in loaded if key not in affected)
            else:
                partitioning = self._require_bounded_partitions()
                for key in self.load_affected_keys(context):
                    normalized_symbol = partitioning.for_key(key)
                    if normalized_symbol not in allowed_partition_symbols:
                        raise ValueError(
                            "reconstructed key symbol is outside the normalized request universe"
                        )
                    spool.append(
                        key,
                        symbol=normalized_symbol,
                        dedupe_key=partitioning.canonicalize_key(key),
                    )
        if any(value is not None for value in comparison_windows.values()):
            await self._run_hooks(context, pipeline_id=pipeline_id, resume=resume)
        return outcome

    def _request_partitions(self, symbols: Sequence[str]) -> dict[str, str]:
        """Normalize request symbols and reject alias-overlapping batches."""
        partitioning = self.bounded_partitions
        if partitioning is None:
            return {}
        by_normalized: dict[str, str] = {}
        result: dict[str, str] = {}
        for symbol in symbols:
            normalized = partitioning.for_request(symbol)
            previous = by_normalized.get(normalized)
            if previous is not None and previous != symbol:
                raise ValueError("request symbols normalize to overlapping hook partitions")
            by_normalized[normalized] = symbol
            result[symbol] = normalized
        return result

    def _require_bounded_partitions(self) -> SymbolPartitioning:
        """Return the bounded partition configuration or fail closed."""
        if self.bounded_partitions is None:
            raise RuntimeError("bounded key processing has no symbol partition configuration")
        return self.bounded_partitions

    def _with_partition_contexts(
        self,
        context: PipelineContext,
        *,
        spool: AffectedKeySpool,
        request_partitions: MappingType[str, str],
    ) -> PipelineContext:
        """Attach a reusable factory of symbol-bounded child contexts."""
        partitioning = self._require_bounded_partitions()
        request_symbols = tuple(context.symbols)

        def iter_partitions() -> Iterator[PipelineContext]:
            for offset in range(0, len(request_symbols), partitioning.batch_size):
                batch = request_symbols[offset : offset + partitioning.batch_size]
                normalized = tuple(request_partitions[symbol] for symbol in batch)
                yield PipelineContext(
                    domain=context.domain,
                    source=context.source,
                    window=context.window,
                    affected_keys=spool.partition(normalized),
                    symbols=batch,
                    source_windows={
                        source: {symbol: windows.get(symbol) for symbol in batch}
                        for source, windows in context.source_windows.items()
                    },
                    comparison_windows={
                        symbol: context.comparison_windows.get(symbol) for symbol in batch
                    },
                    pipeline_id=context.pipeline_id,
                )

        return PipelineContext(
            domain=context.domain,
            source=context.source,
            window=context.window,
            affected_keys=context.affected_keys,
            symbols=context.symbols,
            source_windows=context.source_windows,
            comparison_windows=context.comparison_windows,
            pipeline_id=context.pipeline_id,
            partition_contexts=iter_partitions,
        )

    def _pipeline_id(self, window: Window) -> str:
        """Stable run id including the spec, shard plan, and symbol universe."""
        payload = {
            "domain": self.spec.domain,
            "source": self.spec.source,
            "key": list(self.spec.key),
            "symbols": sorted(set(self.spec.symbols)),
            "shard_size": self.spec.shard_size,
            "variant": self.spec.variant,
            "hooks": [
                name
                for name, hook in (
                    ("cross_check", self.cross_check),
                    ("merge", self.merge),
                    ("notify", self.notify),
                    ("meta", self.meta),
                )
                if hook is not None
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        return f"{self.spec.domain}:{self.spec.source}:{window.label()}:{digest}"

    async def _fetch_shard(
        self,
        index: int,
        symbols: Sequence[str],
        symbol_windows: MappingType[str, Window | None],
        fallback: Window,
    ) -> tuple[pd.DataFrame | None, list[ShardFailure]]:
        """Fetch each symbol sequentially, awaiting async callbacks and isolating failures."""
        import pandas as pd

        frames: list[pd.DataFrame] = []
        failures: list[ShardFailure] = []
        for symbol in symbols:
            try:
                result = self.fetch_symbol(symbol, symbol_windows.get(symbol) or fallback)
                frame = await result if inspect.isawaitable(result) else result
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

    def _iter_affected_keys(self, frame: pd.DataFrame) -> Iterator[tuple[object, ...]]:
        """Stream distinct keys from one shard frame into the disk spool."""
        columns = list(self.spec.key)
        records = frame[columns].drop_duplicates().itertuples(index=False, name=None)
        for record in records:
            yield tuple(record)

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

    async def _run_hooks(self, context: PipelineContext, *, pipeline_id: str, resume: bool) -> None:
        """Resume post-ODS hooks from their durable per-step checkpoints."""
        for name, hook in (
            ("cross_check", self.cross_check),
            ("merge", self.merge),
            ("notify", self.notify),
            ("meta", self.meta),
        ):
            if hook is None:
                continue
            if resume and await self._step_done(pipeline_id, name):
                continue
            await self._record_step(pipeline_id, name, ShardStatus.RUNNING.value)
            try:
                await hook(context)
            except Exception as exc:
                await self._record_step(pipeline_id, name, ShardStatus.FAILED.value, error=str(exc))
                raise
            await self._record_step(pipeline_id, name, ShardStatus.DONE.value)

    async def _resolve_source_windows(
        self,
        pipeline_id: str,
        fallback: Window,
        symbols: Sequence[str],
        supplied: MappingType[str, MappingType[str, Window | None]] | None,
        *,
        resume: bool,
    ) -> dict[str, dict[str, Window | None]]:
        """Restore a persisted plan or save the caller's first plan."""
        if resume:
            stored = await self._load_source_windows(pipeline_id)
            if stored:
                return stored
        plan: dict[str, dict[str, Window | None]] = {}
        source_maps = dict(supplied or {})
        source_maps.setdefault(self.spec.source, dict.fromkeys(symbols, fallback))
        for source, values in source_maps.items():
            plan[source] = {symbol: values.get(symbol, fallback) for symbol in symbols}
        await self._save_source_windows(pipeline_id, plan)
        return plan

    async def _load_source_windows(self, pipeline_id: str) -> dict[str, dict[str, Window | None]]:
        from opendata.models.pipeline_step import PipelineSymbolWindow

        async with self.session_maker() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineSymbolWindow).where(
                            PipelineSymbolWindow.pipeline_id == pipeline_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        result: dict[str, dict[str, Window | None]] = {}
        for row in rows:
            bounds = (
                Window(row.window_start, row.window_end)
                if row.window_start is not None and row.window_end is not None
                else None
            )
            result.setdefault(row.source, {})[row.symbol] = bounds
        return result

    async def _save_source_windows(
        self, pipeline_id: str, windows: MappingType[str, MappingType[str, Window | None]]
    ) -> None:
        from opendata.models.pipeline_step import PipelineSymbolWindow

        async with self.session_maker() as session:
            for source, symbol_map in windows.items():
                for symbol, bounds in symbol_map.items():
                    session.add(
                        PipelineSymbolWindow(
                            pipeline_id=pipeline_id,
                            source=source,
                            symbol=symbol,
                            window_start=bounds.start if bounds is not None else None,
                            window_end=bounds.end if bounds is not None else None,
                        )
                    )
            await session.commit()

    async def _clear_step_state(self, pipeline_id: str) -> None:
        from sqlalchemy import delete

        from opendata.models.pipeline_step import PipelineStepCheckpoint, PipelineSymbolWindow

        async with self.session_maker() as session:
            await session.execute(
                delete(PipelineStepCheckpoint).where(
                    PipelineStepCheckpoint.pipeline_id == pipeline_id
                )
            )
            await session.execute(
                delete(PipelineSymbolWindow).where(PipelineSymbolWindow.pipeline_id == pipeline_id)
            )
            await session.commit()

    async def _invalidate_hooks(self, pipeline_id: str) -> None:
        from opendata.models.pipeline_step import PipelineStepCheckpoint, PipelineStepStatus

        async with self.session_maker() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineStepCheckpoint).where(
                            PipelineStepCheckpoint.pipeline_id == pipeline_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            now = datetime.now(timezone.utc)
            for row in rows:
                row.status = PipelineStepStatus.PENDING
                row.error = None
                row.completed_at = None
                row.updated_at = now
            await session.commit()

    async def _step_done(self, pipeline_id: str, step: str) -> bool:
        from opendata.models.pipeline_step import PipelineStepCheckpoint, PipelineStepStatus

        async with self.session_maker() as session:
            status = (
                await session.execute(
                    select(PipelineStepCheckpoint.status).where(
                        PipelineStepCheckpoint.pipeline_id == pipeline_id,
                        PipelineStepCheckpoint.step == step,
                    )
                )
            ).scalar_one_or_none()
        return status is PipelineStepStatus.DONE

    async def _record_step(
        self,
        pipeline_id: str,
        step: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        from opendata.models.pipeline_step import PipelineStepCheckpoint, PipelineStepStatus

        step_status = PipelineStepStatus(status)
        now = datetime.now(timezone.utc)
        async with self.session_maker() as session:
            row = (
                await session.execute(
                    select(PipelineStepCheckpoint).where(
                        PipelineStepCheckpoint.pipeline_id == pipeline_id,
                        PipelineStepCheckpoint.step == step,
                    )
                )
            ).scalar_one_or_none()
            is_new = row is None
            if row is None:
                row = PipelineStepCheckpoint(
                    pipeline_id=pipeline_id,
                    step=step,
                    status=step_status,
                    attempts=1 if step_status is PipelineStepStatus.RUNNING else 0,
                    error=error,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.status = step_status
                row.error = error
                row.updated_at = now
            if step_status is PipelineStepStatus.RUNNING:
                if not is_new:
                    row.attempts += 1
                row.completed_at = None
            elif step_status is PipelineStepStatus.DONE:
                row.completed_at = now
            await session.commit()


def _union_symbol_windows(
    symbols: Sequence[str],
    source_windows: MappingType[str, MappingType[str, Window | None]],
) -> dict[str, Window | None]:
    """Union source windows per requested symbol."""
    union: dict[str, Window | None] = {}
    for symbol in symbols:
        windows = [
            window
            for values in source_windows.values()
            if (window := values.get(symbol)) is not None
        ]
        union[symbol] = (
            Window(
                start=min(item.start for item in windows),
                end=max(item.end for item in windows),
            )
            if windows
            else None
        )
    return union


def _enclosing_window(
    symbol_windows: MappingType[str, Window | None], *, fallback: Window
) -> Window:
    """Compress per-symbol windows to the hook API's finite summary."""
    windows = [window for window in symbol_windows.values() if window is not None]
    if not windows:
        return fallback
    return Window(min(item.start for item in windows), max(item.end for item in windows))
