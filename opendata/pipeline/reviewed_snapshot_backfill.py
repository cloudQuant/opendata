"""Build offline stock-daily candidates from caller-supplied snapshot bytes.

The adapter only decodes explicit bytes and passes the resulting registry to
the existing stock-daily planner. It does not load or select a snapshot, read a
database, contact a provider, authorize execution, or establish universe,
source-history, coverage, or point-in-time completeness. Hashes detect changes
according to their documented scope; they do not prove review approval.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date  # noqa: TC003 -- public annotations must resolve for get_type_hints.

from opendata.data.instrument_reference_snapshot import decode_instrument_reference_snapshot
from opendata.data.platform_contracts import DatasetContract  # noqa: TC001 -- runtime annotations.
from opendata.pipeline.run_identity import TaskDefinition  # noqa: TC001 -- runtime annotations.
from opendata.pipeline.stock_daily_backfill_plan import (
    MAX_STOCK_DAILY_WINDOW_DAYS,
    StockDailyBackfillPlan,
    build_stock_daily_backfill_plan,
)


@dataclass(frozen=True, slots=True)
class SnapshotStockDailyBackfillCandidate:
    """Pair the exact input byte digest with the planner's candidate result.

    ``snapshot_bytes_sha256`` identifies the exact caller-supplied encoding.
    ``plan.registry_sha256`` identifies the planner's normalized registry, and
    ``plan.plan_sha256`` identifies its canonical plan and blockers. Those
    digests are separate values with separate meanings. None proves that the
    data was reviewed, that collection is permitted, or that history is complete.
    """

    snapshot_bytes_sha256: str
    plan: StockDailyBackfillPlan


def build_stock_daily_backfill_plan_from_snapshot(
    snapshot_bytes: bytes,
    *,
    task: TaskDefinition,
    contract: DatasetContract,
    as_of: date,
    max_window_days: int = MAX_STOCK_DAILY_WINDOW_DAYS,
) -> SnapshotStockDailyBackfillCandidate:
    """Build deterministic offline candidates from explicit snapshot bytes.

    The snapshot codec validates the document's self-excluding content digest.
    The existing planner computes its own normalized registry digest and
    canonical plan digest, including every blocker. This adapter separately
    hashes the exact bytes only after decoding and planning succeed.

    Args:
        snapshot_bytes: Immutable snapshot bytes supplied directly by the caller.
        task: Exact existing, disabled stock-daily task definition.
        contract: Exact dataset contract accepted by the stock-daily job.
        as_of: Explicit final business-date cutoff; no clock is consulted.
        max_window_days: Inclusive calendar-day request cap accepted by the planner.

    Returns:
        Exact input-byte SHA-256 and the existing non-dispatchable candidate plan.

    Raises:
        InstrumentReferenceSnapshotError: If bytes, JSON, schema, or snapshot
            content hash are invalid.
        TypeError: If task, contract, or date inputs have an invalid type.
        ValueError: If planner inputs are invalid or the snapshot has no lifecycle
            in the exact task/source scope.
    """
    registry = decode_instrument_reference_snapshot(snapshot_bytes)
    plan = build_stock_daily_backfill_plan(
        registry,
        task=task,
        contract=contract,
        as_of=as_of,
        max_window_days=max_window_days,
    )
    return SnapshotStockDailyBackfillCandidate(
        snapshot_bytes_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        plan=plan,
    )


__all__ = [
    "SnapshotStockDailyBackfillCandidate",
    "build_stock_daily_backfill_plan_from_snapshot",
]
