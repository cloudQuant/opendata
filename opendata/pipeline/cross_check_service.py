"""Cross-check service: the pipeline's step-3 hook (A4.5, design §8.2).

Binds the pieces for one domain: two sources, their field mappings,
the readers that load each source's ods rows for a window, the
``dq_diff_report`` writer and the alert policy. ``run_hook`` matches
the :class:`~opendata.pipeline.runner.PipelineContext` signature so
A4.7's P0 template can pass it as ``cross_check=service.run_hook``.

Single-source domains skip the step by passing ``cross_check=None``
to the pipeline; a domain whose second mapping or reader is missing
fails closed instead of comparing a source with itself.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from opendata.pipeline.alerts import AlertPolicy
from opendata.pipeline.cross_check import (
    DEFAULT_SAMPLE_LIMIT,
    DiffSummary,
    FieldDiff,
    compare_source_frames,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pandas as pd

    from opendata.data.mapping import DomainMapping
    from opendata.pipeline.alerts import Notifier
    from opendata.pipeline.runner import PipelineContext, Window

    #: Loads one source's ods rows for a window.
    Reader = Callable[[Window], "pd.DataFrame"]
    #: Persists the comparison; returns rows written.
    WriteReport = Callable[[DiffSummary], int]


def batch_id_for(domain: str, window: Window) -> str:
    """The cross-check batch id of one (domain, window) pair.

    Deterministic per pair so a re-run of the same window updates the same
    ``dq_diff_report`` rows instead of duplicating them. Both triggers
    name their batch through here: the pipeline hook and the scheduled full
    check are otherwise two key spaces, and a report reader could not tell
    which run produced a row.

    Args:
        domain: Domain the comparison ran on.
        window: Date window the comparison covered.

    Returns:
        The batch identifier.
    """
    return f"xcheck:{domain}:{window.label()}"


def hook_batch_id(context: PipelineContext) -> str:
    """Derive the cross-check batch id from a pipeline context.

    Args:
        context: The pipeline context of the current run.

    Returns:
        The batch identifier.
    """
    base = batch_id_for(context.domain, context.window)
    if not context.pipeline_id:
        return base
    digest = hashlib.sha256(context.pipeline_id.encode("utf-8")).hexdigest()[:16]
    return f"{base}:{digest}"


@dataclass
class CrossCheckService:
    """Compare two sources of one domain and report the differences.

    Attributes:
        domain: Domain identifier.
        sources: The two source identifiers (A, B).
        mappings: Source identifier to its domain mapping.
        readers: Source identifier to its window reader.
        write_report: Report writer (``DiffReportWriter.write``).
        notifier: Alert delivery, ``(summary, decision)``; None disables it.
        policy: Alert policy; a fresh one is created when omitted, which
            is right for a test and wrong for a scheduler - production
            passes :func:`~opendata.pipeline.diff_alerts.shared_policy` so
            dedupe and the rate baseline outlive one comparison.
        checked_at: Fixed comparison timestamp (tests); None means now.
    """

    domain: str
    sources: tuple[str, str]
    mappings: Mapping[str, DomainMapping]
    readers: Mapping[str, Reader]
    write_report: WriteReport
    notifier: Notifier | None = None
    policy: AlertPolicy | None = None
    checked_at: datetime | None = None
    _policy: AlertPolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Materialize the alert policy (stateful across runs)."""
        self._policy = self.policy if self.policy is not None else AlertPolicy()

    async def run(
        self,
        batch_id: str,
        window: Window,
        *,
        symbols: Sequence[str] | None = None,
        symbol_windows: Mapping[str, Window | None] | None = None,
    ) -> DiffSummary:
        """Compare both sources for one window.

        Args:
            batch_id: Batch identifier of the run.
            window: Date window to compare.
            symbols: Requested universe for a bounded source read.
            symbol_windows: Per-symbol union ranges shared by both
                source readers.

        Returns:
            The comparison summary (also written to the report).

        Raises:
            LookupError: If a source lacks a mapping or a reader
                (fail closed: never compare a source with itself).
        """
        source_a, source_b = self.sources
        summary = compare_source_frames(
            self.domain,
            self._read(source_a, window, symbols=symbols, symbol_windows=symbol_windows),
            self._mapping(source_a),
            self._read(source_b, window, symbols=symbols, symbol_windows=symbol_windows),
            self._mapping(source_b),
            source_a=source_a,
            source_b=source_b,
            batch_id=batch_id,
            checked_at=self.checked_at,
        )
        self.write_report(summary)
        await self._notify(summary)
        return summary

    async def run_hook(self, context: PipelineContext) -> DiffSummary:
        """Pipeline hook entry point (step 3).

        Args:
            context: The pipeline context of the current run.

        Returns:
            The comparison summary (ignored by the runner, useful for
            logging and tests).
        """
        if context.partition_contexts is not None:
            batch_id = hook_batch_id(context)
            checked_at = self.checked_at or datetime.now(timezone.utc)
            compared_keys = 0
            deviation_count = 0
            missing_count = 0
            per_field: dict[str, int] = {}
            samples: list[FieldDiff] = []
            for partition in context.partition_contexts():
                summary = self._compare_batch(
                    batch_id,
                    context.window,
                    symbols=partition.symbols,
                    symbol_windows=partition.comparison_windows,
                    checked_at=checked_at,
                )
                compared_keys += summary.compared_keys
                deviation_count += summary.deviation_count
                missing_count += summary.missing_count
                for field_name, count in summary.per_field.items():
                    per_field[field_name] = per_field.get(field_name, 0) + count
                if len(samples) < DEFAULT_SAMPLE_LIMIT:
                    samples.extend(summary.samples[: DEFAULT_SAMPLE_LIMIT - len(samples)])
            aggregate = DiffSummary(
                domain=self.domain,
                source_a=self.sources[0],
                source_b=self.sources[1],
                checked_at=checked_at,
                batch_id=batch_id,
                compared_keys=compared_keys,
                deviation_count=deviation_count,
                missing_count=missing_count,
                per_field=per_field,
                samples=samples,
            )
            self.write_report(aggregate)
            await self._notify(aggregate)
            return aggregate
        if not context.symbols or not context.comparison_windows:
            return await self.run(hook_batch_id(context), context.window)
        return await self.run(
            hook_batch_id(context),
            context.window,
            symbols=context.symbols,
            symbol_windows=context.comparison_windows,
        )

    def _compare_batch(
        self,
        batch_id: str,
        window: Window,
        *,
        symbols: Sequence[str],
        symbol_windows: Mapping[str, Window | None],
        checked_at: datetime,
    ) -> DiffSummary:
        """Compare one symbol batch without writing or notifying."""
        source_a, source_b = self.sources
        return compare_source_frames(
            self.domain,
            self._read(source_a, window, symbols=symbols, symbol_windows=symbol_windows),
            self._mapping(source_a),
            self._read(source_b, window, symbols=symbols, symbol_windows=symbol_windows),
            self._mapping(source_b),
            source_a=source_a,
            source_b=source_b,
            batch_id=batch_id,
            checked_at=checked_at,
            sample_limit=DEFAULT_SAMPLE_LIMIT,
        )

    def _mapping(self, source: str) -> DomainMapping:
        """Return one source's domain mapping or fail closed."""
        if source not in self.mappings:
            raise LookupError(
                f"no mapping registered for source {source!r} in domain {self.domain!r}"
            )
        return self.mappings[source]

    def _reader(self, source: str) -> Reader:
        """Return one source's window reader or fail closed."""
        if source not in self.readers:
            raise LookupError(
                f"no reader registered for source {source!r} in domain {self.domain!r}"
            )
        return self.readers[source]

    def _read(
        self,
        source: str,
        window: Window,
        *,
        symbols: Sequence[str] | None,
        symbol_windows: Mapping[str, Window | None] | None,
    ) -> pd.DataFrame:
        """Pass the allowed symbol/window selection to scoped readers."""
        reader = self._reader(source)
        try:
            parameters = inspect.signature(reader).parameters
        except (TypeError, ValueError):
            parameters = None
        if parameters is not None and (
            "symbol_windows" in parameters
            or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        ):
            from typing import cast

            scoped_reader = cast("Callable[..., pd.DataFrame]", reader)
            return scoped_reader(window, symbols=symbols, symbol_windows=symbol_windows)
        return reader(window)

    async def _notify(self, summary: DiffSummary) -> None:
        """Apply the alert policy and hand the pair to the notifier.

        The decision does not suppress the call: the notifier sees
        suppressed differences too, so "governance kept this quiet" is a
        recorded outcome rather than an absence of evidence.
        """
        decision = self._policy.decide(summary)
        if self.notifier is not None:
            await self.notifier(summary, decision)
