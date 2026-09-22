"""Freshness checks and the operational alert matrix (A4.8).

Freshness: each domain has one date column that answers "how current
is this data?" - it is derived from the contract model (the first
``date``-typed field: ``trade_date`` for bars, ``ex_date`` for
corporate actions, ``report_period`` for financials, ``as_of`` for
index membership, ``date`` for calendars) and the check reads
``MAX(<column>)`` from the domain's warehouse table. A table that does
not exist is a ``missing`` report - an alert, never a crash - which is
the design's "模拟数据缺失触发告警" case.

The alert matrix turns the four operational signals from the
requirements into one decision list: pipeline failures, consecutive
failures, partition gaps (reusing the A4.3 horizon planner through
``plan_yearly_partitions``) and disk water level. Alert delivery is
injected elsewhere (SMTP / WS ``data.diff_alert`` style notifier), so
this module only decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import text

from opendata.data.domains import contract_model, dwd_table, require_domain

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Engine

#: Severity levels, in increasing urgency.
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

#: Freshness statuses.
STATUS_FRESH = "fresh"
STATUS_STALE = "stale"
STATUS_MISSING = "missing"


@dataclass(frozen=True)
class FreshnessReport:
    """How current one domain's data is.

    Attributes:
        domain: Domain identifier.
        source: Source identifier (None for the dwd view).
        field: Date column the check read.
        latest: Latest date in the table, None when absent.
        expected: Date the data should have reached.
        lag_days: ``expected - latest`` in days, None when absent.
        status: ``fresh`` / ``stale`` / ``missing``.
    """

    domain: str
    source: str | None
    field: str
    latest: date | None
    expected: date
    lag_days: int | None
    status: str


@dataclass(frozen=True)
class PipelineFailure:
    """One pipeline's failure state.

    Attributes:
        domain: Domain identifier.
        source: Source identifier.
        consecutive_failures: Failures in a row (1 for a single one).
        last_error: Last error message.
    """

    domain: str
    source: str
    consecutive_failures: int
    last_error: str


@dataclass(frozen=True)
class DiskUsage:
    """Disk water level of the warehouse volume.

    Attributes:
        used_bytes: Used bytes.
        total_bytes: Total bytes.
    """

    used_bytes: int
    total_bytes: int

    @property
    def used_ratio(self) -> float:
        """Used share in ``[0, 1]``."""
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes


@dataclass(frozen=True)
class AlertSettings:
    """Thresholds of the alert matrix.

    Attributes:
        stale_lag_days: Lag that makes data stale (warning).
        stale_lag_days_critical: Lag that escalates freshness.
        consecutive_failure_critical: Failures in a row that escalate
            a pipeline failure.
        disk_ratio_warning: Disk usage ratio that warns.
        disk_ratio_critical: Disk usage ratio that escalates.
    """

    stale_lag_days: int = 1
    stale_lag_days_critical: int = 3
    consecutive_failure_critical: int = 3
    disk_ratio_warning: float = 0.8
    disk_ratio_critical: float = 0.9


@dataclass(frozen=True)
class Alert:
    """One alert of the matrix.

    Attributes:
        rule: ``freshness`` / ``pipeline_failure`` /
            ``consecutive_failures`` / ``partition_missing`` /
            ``disk_water``.
        severity: ``warning`` or ``critical``.
        subject: What the alert is about (domain, table, volume).
        detail: Human-readable explanation.
    """

    rule: str
    severity: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Return the alert as a JSON-ready mapping."""
        return {
            "rule": self.rule,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
        }


def freshness_field(domain: str) -> str:
    """Derive the date column that measures a domain's freshness.

    The first ``date``-typed contract field wins; the contract field
    order is stable, so the rule is deterministic and needs no extra
    registry entry.

    Args:
        domain: Registered domain identifier.

    Returns:
        The date column name.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If the contract model has no date field (the domain
            cannot be freshness-checked; fail closed).
    """
    require_domain(domain)
    model = contract_model(domain)
    for name, contract_field in model.model_fields.items():
        if _is_date_type(contract_field.annotation):
            return name
    raise ValueError(f"domain {domain!r} has no date field to measure freshness with")


def check_freshness(
    engine: Engine,
    *,
    domain: str,
    source: str | None,
    table: str,
    expected: date,
    field: str | None = None,
) -> FreshnessReport:
    """Read one table's latest date and compare it with the expectation.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier (for the report and the field lookup).
        source: Source identifier; None measures the dwd view.
        table: Table to check.
        expected: Date the data should have reached (the last trading
            day, supplied by the caller / calendar).
        field: Date column override; defaults to the derived field.

    Returns:
        The freshness report; a missing table yields ``missing``
        rather than an exception.
    """
    column = field or freshness_field(domain)
    from sqlalchemy.exc import SQLAlchemyError

    try:
        with engine.connect() as connection:
            latest = connection.execute(
                text(f"SELECT MAX(`{column}`) FROM `{table}`")  # noqa: S608  # derived column
            ).scalar()
    except SQLAlchemyError:  # absent table/column: report, never raise
        return FreshnessReport(
            domain=domain,
            source=source,
            field=column,
            latest=None,
            expected=expected,
            lag_days=None,
            status=STATUS_MISSING,
        )
    latest_date = _as_date(latest)
    if latest_date is None:
        return FreshnessReport(
            domain=domain,
            source=source,
            field=column,
            latest=None,
            expected=expected,
            lag_days=None,
            status=STATUS_MISSING,
        )
    lag = (expected - latest_date).days
    return FreshnessReport(
        domain=domain,
        source=source,
        field=column,
        latest=latest_date,
        expected=expected,
        lag_days=lag,
        status=STATUS_FRESH if lag <= 0 else STATUS_STALE,
    )


def ods_freshness(
    engine: Engine,
    domain: str,
    source: str,
    *,
    table: str,
    expected: date,
) -> FreshnessReport:
    """Freshness of one source's ods table.

    The ods layer keeps the source's own column names (design §8.1),
    so the column checked is the mapping's source column for the
    domain's freshness field - not the contract field name.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        source: Source identifier.
        table: The ods table to check.
        expected: Date the data should have reached.

    Returns:
        The freshness report.

    Raises:
        LookupError: If the source has no mapping for the domain, or
            the mapping lacks the freshness field (fail closed).
    """
    from opendata.data.mapping import require_domain_mapping

    field = freshness_field(domain)
    mapping = require_domain_mapping(source, domain)
    if field not in mapping.fields:
        raise LookupError(
            f"mapping {source!r}/{domain!r} has no field {field!r} to measure freshness with"
        )
    return check_freshness(
        engine,
        domain=domain,
        source=source,
        table=table,
        expected=expected,
        field=mapping.fields[field].source_column,
    )


def dwd_freshness(engine: Engine, domain: str, *, expected: date) -> FreshnessReport:
    """Convenience wrapper for the dwd view of a domain.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        expected: Date the data should have reached.

    Returns:
        The freshness report of ``dwd_<domain>``.
    """
    return check_freshness(
        engine, domain=domain, source=None, table=dwd_table(domain), expected=expected
    )


def partition_plans_for(
    engine: Engine,
    tables: Sequence[str],
    *,
    current_year: int,
    years_ahead: int = 2,
) -> dict[str, list[tuple[str, date]]]:
    """Collect the partition gaps of the given tables.

    Thin composition over the A4.3 maintainer so the freshness task and
    the alert matrix share one definition of "partition missing".

    Args:
        engine: Warehouse engine.
        tables: Tables to check (unpartitioned ones yield no entry).
        current_year: Current calendar year.
        years_ahead: Years beyond the current one that must exist.

    Returns:
        Table name to the missing ``(partition, bound)`` pairs; tables
        without a gap are omitted.
    """
    from opendata.pipeline.partitions import PartitionMaintainer, plan_yearly_partitions

    maintainer = PartitionMaintainer(engine)
    plans: dict[str, list[tuple[str, date]]] = {}
    for table in tables:
        if not maintainer.is_partitioned(table):
            continue
        plan = plan_yearly_partitions(
            maintainer.state(table), current_year=current_year, years_ahead=years_ahead
        )
        if plan:
            plans[table] = plan
    return plans


def evaluate_alerts(
    *,
    settings: AlertSettings,
    freshness: Sequence[FreshnessReport],
    failures: Sequence[PipelineFailure],
    partition_plans: Mapping[str, Sequence[tuple[str, date]]],
    disk: DiskUsage | None,
) -> list[Alert]:
    """Apply the alert matrix to the collected signals.

    Args:
        settings: Thresholds.
        freshness: Freshness reports (ods and/or dwd).
        failures: Pipeline failure states.
        partition_plans: Table name to the partitions the A4.3 planner
            says are missing (``plan_yearly_partitions`` output).
        disk: Warehouse disk usage, when measurable.

    Returns:
        The alerts to deliver, empty when everything is healthy.
    """
    alerts: list[Alert] = []
    alerts.extend(_freshness_alerts(freshness, settings))
    alerts.extend(_failure_alerts(failures, settings))
    alerts.extend(_partition_alerts(partition_plans))
    if disk is not None:
        alerts.extend(_disk_alerts(disk, settings))
    return alerts


def _freshness_alerts(reports: Sequence[FreshnessReport], settings: AlertSettings) -> list[Alert]:
    """Freshness rows of the matrix."""
    alerts: list[Alert] = []
    for report in reports:
        subject = f"{report.domain}:{report.source or 'dwd'}"
        if report.status == STATUS_MISSING:
            alerts.append(
                Alert(
                    "freshness",
                    SEVERITY_CRITICAL,
                    subject,
                    f"no data found for {report.field} (missing or empty table)",
                )
            )
            continue
        if report.status != STATUS_STALE:
            continue
        lag = report.lag_days or 0
        severity = (
            SEVERITY_CRITICAL if lag >= settings.stale_lag_days_critical else SEVERITY_WARNING
        )
        alerts.append(
            Alert(
                "freshness",
                severity,
                subject,
                f"latest {report.latest} is {lag} day(s) behind {report.expected}",
            )
        )
    return alerts


def _failure_alerts(failures: Sequence[PipelineFailure], settings: AlertSettings) -> list[Alert]:
    """Pipeline failure rows of the matrix."""
    alerts: list[Alert] = []
    for failure in failures:
        subject = f"{failure.domain}:{failure.source}"
        alerts.append(
            Alert(
                "pipeline_failure",
                SEVERITY_WARNING,
                subject,
                f"last run failed: {failure.last_error}",
            )
        )
        if failure.consecutive_failures >= settings.consecutive_failure_critical:
            alerts.append(
                Alert(
                    "consecutive_failures",
                    SEVERITY_CRITICAL,
                    subject,
                    f"{failure.consecutive_failures} consecutive failures "
                    f"(threshold {settings.consecutive_failure_critical})",
                )
            )
    return alerts


def _partition_alerts(
    plans: Mapping[str, Sequence[tuple[str, date]]],
) -> list[Alert]:
    """Partition gap rows of the matrix."""
    alerts: list[Alert] = []
    for table, missing in plans.items():
        if not missing:
            continue
        names = ", ".join(name for name, _ in missing)
        alerts.append(
            Alert(
                "partition_missing",
                SEVERITY_CRITICAL,
                table,
                f"missing yearly partitions: {names}",
            )
        )
    return alerts


def _disk_alerts(disk: DiskUsage, settings: AlertSettings) -> list[Alert]:
    """Disk water level rows of the matrix."""
    ratio = disk.used_ratio
    if ratio >= settings.disk_ratio_critical:
        severity = SEVERITY_CRITICAL
    elif ratio >= settings.disk_ratio_warning:
        severity = SEVERITY_WARNING
    else:
        return []
    return [
        Alert(
            "disk_water",
            severity,
            "warehouse volume",
            f"disk usage {ratio:.1%} (warning >= {settings.disk_ratio_warning:.0%}, "
            f"critical >= {settings.disk_ratio_critical:.0%})",
        )
    ]


def _is_date_type(annotation: object) -> bool:
    """Whether a contract field annotation is a plain ``date``."""
    from datetime import date as date_type
    from typing import get_args

    candidates = get_args(annotation) or (annotation,)
    return any(candidate is date_type for candidate in candidates)


def _as_date(value: object) -> date | None:
    """Coerce a driver value to a date, None when unparsable."""
    from datetime import datetime as datetime_type

    if isinstance(value, datetime_type):
        return value.date()
    if isinstance(value, date):
        return value
    return None
