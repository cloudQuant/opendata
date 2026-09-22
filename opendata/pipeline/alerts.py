"""Alert governance for cross-check differences (design §8.2, A4.5).

Three rules from the design:

* **known-difference whitelist** - expected ``(domain, field)``
  tolerances are registered and never alert;
* **fingerprint dedupe** - the same difference pattern (domain, the
  deviating fields and the verdict) alerts once; consecutive
  identical reports stay quiet;
* **leveling** - a diff-rate jump against the previous decision
  escalates to ``critical``, otherwise a difference is a ``warning``.

Delivery (SMTP / WS ``data.diff_alert``) is injected as a notifier
callable; this module only decides *whether* to alert and at which
level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from opendata.pipeline.cross_check import DiffSummary

    #: Notifier signature (WS push or SMTP); None disables delivery.
    Notifier = Callable[["AlertDecision"], Awaitable[None]]

#: Alert levels, in increasing severity.
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"

#: A diff-rate jump of this factor escalates an alert.
RATE_SPIKE_FACTOR = 2.0


@dataclass(frozen=True)
class AlertDecision:
    """Whether and how to alert about one comparison.

    Attributes:
        alert: Whether the notifier should fire.
        level: ``warning`` or ``critical``.
        reason: Why the decision was made (for logs and the WS event).
        fingerprint: Difference pattern identity.
    """

    alert: bool
    level: str
    reason: str
    fingerprint: str


@dataclass
class AlertPolicy:
    """Stateful alert policy for one process.

    Attributes:
        whitelist: ``(domain, field)`` pairs treated as known.
        alerted: Fingerprints already alerted in this process.
        last_rate: Diff rate of the previous decision (leveling base).
    """

    whitelist: set[tuple[str, str]] = field(default_factory=set)
    alerted: set[str] = field(default_factory=set)
    last_rate: float | None = None

    def decide(self, summary: DiffSummary) -> AlertDecision:
        """Decide whether one comparison deserves an alert.

        Args:
            summary: The comparison summary.

        Returns:
            The decision, including the level and the reason.
        """
        identity = fingerprint(summary)
        if not summary.has_diffs:
            self.last_rate = 0.0
            return AlertDecision(False, LEVEL_WARNING, "sources are consistent", identity)
        whitelisted = [
            name for name in summary.per_field if (summary.domain, name) in self.whitelist
        ]
        if whitelisted and len(whitelisted) == len(summary.per_field) and not summary.missing_count:
            return AlertDecision(
                False,
                LEVEL_WARNING,
                f"differences are whitelisted for {sorted(whitelisted)}",
                identity,
            )
        if identity in self.alerted:
            return AlertDecision(
                False, LEVEL_WARNING, "this difference pattern was already alerted", identity
            )
        rate = summary.diff_rate
        level = LEVEL_WARNING
        if (
            self.last_rate is not None
            and self.last_rate > 0
            and rate >= self.last_rate * RATE_SPIKE_FACTOR
        ):
            level = LEVEL_CRITICAL
        self.alerted.add(identity)
        self.last_rate = rate
        return AlertDecision(
            True,
            level,
            f"{summary.deviation_count} deviation(s) and {summary.missing_count} missing row(s) "
            f"over {summary.compared_keys} keys (rate {rate:.4f})",
            identity,
        )


def fingerprint(summary: DiffSummary) -> str:
    """Identity of a difference pattern (values and batch excluded).

    Args:
        summary: The comparison summary.

    Returns:
        A stable string over the domain, the deviating fields and the
        verdict mix.
    """
    fields = ",".join(sorted(summary.per_field))
    missing = "missing" if summary.missing_count else ""
    return f"{summary.domain}|{fields}|{missing}"
