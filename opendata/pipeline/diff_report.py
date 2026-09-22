"""``dq_diff_report`` writer (design §8.2, milestone A4.5).

The report lives in the data warehouse. Only the aggregate counters
and a sampled detail list are stored (the full detail is exported
separately, design §8.2) - a million-row diff must not flood the
warehouse. One row per sampled difference, keyed by
``(batch_id, biz_key, field)`` so re-running the same batch updates
the same rows instead of duplicating them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from opendata.pipeline.cross_check import DiffSummary

#: Report table name in the warehouse (created by alembic_data 0002).
DQ_DIFF_REPORT_TABLE = "dq_diff_report"

#: Columns written per sampled difference; the order defines the SQL.
REPORT_COLUMNS = (
    "batch_id",
    "domain",
    "source_a",
    "source_b",
    "biz_key",
    "field",
    "value_a",
    "value_b",
    "deviation",
    "verdict",
    "checked_at",
)

#: Key of the report rows (idempotent re-runs of one batch).
REPORT_KEY = ("batch_id", "biz_key", "field")

_IDENTIFIER_SAFE = DQ_DIFF_REPORT_TABLE.replace("_", "").isalnum()


@dataclass(frozen=True)
class ReportRow:
    """One sampled difference as the report stores it.

    Attributes:
        batch_id: Batch identifier of the run.
        domain: Domain identifier.
        source_a: First source identifier.
        source_b: Second source identifier.
        biz_key: Business key rendered with ``|`` separators.
        field: Contract field, or ``*`` for a whole-row verdict.
        value_a: Rendered value of source A.
        value_b: Rendered value of source B.
        deviation: Relative deviation for numeric fields.
        verdict: Verdict value.
        checked_at: Comparison timestamp.
    """

    batch_id: str
    domain: str
    source_a: str
    source_b: str
    biz_key: str
    field: str
    value_a: str | None
    value_b: str | None
    deviation: float | None
    verdict: str
    checked_at: Any

    def as_params(self) -> dict[str, Any]:
        """Return the row as bind parameters."""
        return {column: getattr(self, column) for column in REPORT_COLUMNS}


def summarize_for_report(summary: DiffSummary) -> list[ReportRow]:
    """Turn a summary's samples into report rows.

    Args:
        summary: The comparison summary.

    Returns:
        One row per sampled difference.
    """
    return [
        ReportRow(
            batch_id=summary.batch_id,
            domain=summary.domain,
            source_a=summary.source_a,
            source_b=summary.source_b,
            biz_key="|".join(str(part) for part in diff.biz_key),
            field=diff.field,
            value_a=_render(diff.value_a),
            value_b=_render(diff.value_b),
            deviation=diff.deviation,
            verdict=diff.verdict.value,
            checked_at=summary.checked_at,
        )
        for diff in summary.samples
    ]


def build_report_insert_sql() -> str:
    """Build the key-level upsert for report rows.

    Returns:
        The ``INSERT ... AS new ON DUPLICATE KEY UPDATE`` statement.
    """
    columns = ", ".join(f"`{column}`" for column in REPORT_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in REPORT_COLUMNS)
    updates = [column for column in REPORT_COLUMNS if column not in REPORT_KEY]
    assignments = ", ".join(f"`{column}` = new.`{column}`" for column in updates)
    return (
        f"INSERT INTO `{DQ_DIFF_REPORT_TABLE}` ({columns}) VALUES ({placeholders}) AS new "
        f"ON DUPLICATE KEY UPDATE {assignments}"
    )


class DiffReportWriter:
    """Write cross-check samples into ``dq_diff_report``."""

    def __init__(self, engine: Engine) -> None:
        """Bind the warehouse engine.

        Args:
            engine: Engine of the data-warehouse database.
        """
        self.engine = engine

    def write(self, summary: DiffSummary) -> int:
        """Write the summary's samples (idempotent per batch).

        Args:
            summary: The comparison summary.

        Returns:
            Number of rows written.
        """
        rows = summarize_for_report(summary)
        if not rows:
            return 0
        statement = text(build_report_insert_sql())
        with self.engine.begin() as connection:
            connection.execute(statement, [row.as_params() for row in rows])
        return len(rows)


def _render(value: object) -> str | None:
    """Render a value for the report's text columns."""
    if value is None:
        return None
    return str(value)
