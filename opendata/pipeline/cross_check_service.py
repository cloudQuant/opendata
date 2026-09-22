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

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from opendata.pipeline.alerts import AlertPolicy
from opendata.pipeline.cross_check import DiffSummary, compare_source_frames

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    import pandas as pd

    from opendata.data.mapping import DomainMapping
    from opendata.pipeline.alerts import Notifier
    from opendata.pipeline.runner import PipelineContext, Window

    #: Loads one source's ods rows for a window.
    Reader = Callable[[Window], "pd.DataFrame"]
    #: Persists the comparison; returns rows written.
    WriteReport = Callable[[DiffSummary], int]


def hook_batch_id(context: PipelineContext) -> str:
    """Derive the cross-check batch id from a pipeline context.

    Deterministic per (domain, window) so a re-run of the same window
    updates the same ``dq_diff_report`` rows instead of duplicating
    them.

    Args:
        context: The pipeline context of the current run.

    Returns:
        The batch identifier.
    """
    return f"xcheck:{context.domain}:{context.window.label()}"


@dataclass
class CrossCheckService:
    """Compare two sources of one domain and report the differences.

    Attributes:
        domain: Domain identifier.
        sources: The two source identifiers (A, B).
        mappings: Source identifier to its domain mapping.
        readers: Source identifier to its window reader.
        write_report: Report writer (``DiffReportWriter.write``).
        notifier: Optional alert delivery.
        policy: Alert policy; a fresh one is created when omitted.
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

    async def run(self, batch_id: str, window: Window) -> DiffSummary:
        """Compare both sources for one window.

        Args:
            batch_id: Batch identifier of the run.
            window: Date window to compare.

        Returns:
            The comparison summary (also written to the report).

        Raises:
            LookupError: If a source lacks a mapping or a reader
                (fail closed: never compare a source with itself).
        """
        source_a, source_b = self.sources
        summary = compare_source_frames(
            self.domain,
            self._reader(source_a)(window),
            self._mapping(source_a),
            self._reader(source_b)(window),
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
        return await self.run(hook_batch_id(context), context.window)

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

    async def _notify(self, summary: DiffSummary) -> None:
        """Apply the alert policy and deliver the decision."""
        decision = self._policy.decide(summary)
        if self.notifier is not None:
            await self.notifier(decision)
