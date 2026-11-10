"""Execution-local write fence for AKShare script runs.

The scheduler owns run identity; this module carries that identity into the
killable script worker process and merges the child's committed-write receipts
back onto the parent tracker, so a warehouse write stays attributable to
exactly one ledger execution.

Scope note: this tree models no akshare lease/claim table (the scheduler
serialises jobs with an in-process semaphore and the ledger row is the only
durable identity), so the fence is deliberately process-local. ``claim_key``
names the exclusive write claim, ``owner_id`` names its holder and
``fencing_token`` is the monotonic generation of that holder; a writer that
cannot show all three records nothing.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _WriteTracker:
    """Committed-write receipts accumulated for one execution."""

    execution_id: str | None = None
    committed: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AkshareWriteFence:
    """Identity of the write claim held by the current execution."""

    claim_key: str
    owner_id: str
    fencing_token: int
    tracker: _WriteTracker = field(default_factory=_WriteTracker)


_write_fence: ContextVar[AkshareWriteFence | None] = ContextVar("akshare_write_fence", default=None)


def set_write_fence(
    *,
    claim_key: str,
    owner_id: str,
    fencing_token: int,
    execution_id: str | None = None,
) -> Token[AkshareWriteFence | None]:
    """Install the write fence for the current context and return its reset token."""
    if not claim_key:
        raise ValueError("Write fence requires a claim key")
    if not owner_id:
        raise ValueError("Write fence requires an owner id")
    if fencing_token < 0:
        raise ValueError("Write fence requires a non-negative fencing token")
    fence = AkshareWriteFence(
        claim_key=claim_key,
        owner_id=owner_id,
        fencing_token=int(fencing_token),
        tracker=_WriteTracker(execution_id=execution_id),
    )
    return _write_fence.set(fence)


def reset_write_fence(token: Token[AkshareWriteFence | None]) -> None:
    """Drop the write fence installed by :func:`set_write_fence`."""
    _write_fence.reset(token)


def current_write_fence() -> AkshareWriteFence | None:
    """Return the fence held by the current context, if any."""
    return _write_fence.get()


def require_write_fence() -> AkshareWriteFence:
    """Return the current fence or raise when the context is unfenced."""
    fence = _write_fence.get()
    if fence is None:
        raise RuntimeError("Akshare write attempted without a write fence")
    return fence


def record_execution_id(execution_id: str) -> None:
    """Attach the ledger execution id to the active fence tracker."""
    fence = _write_fence.get()
    if fence is None:
        return
    fence.tracker.execution_id = execution_id


def current_execution_id() -> str | None:
    """Return the ledger execution id carried by the active fence."""
    fence = _write_fence.get()
    if fence is None:
        return None
    return fence.tracker.execution_id


def fence_identity(fence: AkshareWriteFence | None) -> dict[str, Any] | None:
    """Serialise a fence for transport into a child process."""
    if fence is None:
        return None
    return {
        "claim_key": fence.claim_key,
        "owner_id": fence.owner_id,
        "fencing_token": fence.fencing_token,
        "execution_id": fence.tracker.execution_id,
    }


def record_committed_write(table_name: str, row_count: int) -> bool:
    """Record one committed warehouse write on the active fence tracker.

    Returns ``False`` when no fence is active: an unfenced process must not
    attribute writes to somebody else's execution.  The tracker lives in a
    context variable, so a writer that commits from a plain ``threading.Thread``
    (which starts with a fresh context) records nothing; use ``copy_context()``
    there if its receipts must reach the ledger.
    """
    fence = _write_fence.get()
    if fence is None:
        return False
    if not table_name or int(row_count) <= 0:
        return False
    fence.tracker.committed.append({"table_name": table_name, "row_count": int(row_count)})
    return True


def committed_mysql_writes(table_name: str | None = None) -> list[dict[str, Any]]:
    """Return committed-write receipts, optionally filtered by table name.

    Table matching is case-insensitive (AkShare warehouse tables are addressed
    case-insensitively) while the recorded name keeps its original case.
    """
    fence = _write_fence.get()
    if fence is None:
        return []
    records = list(fence.tracker.committed)
    if table_name is None:
        return records
    needle = str(table_name).casefold()
    return [
        dict(record)
        for record in records
        if isinstance(record.get("table_name"), str)
        and str(record["table_name"]).casefold() == needle
    ]


def merge_committed_mysql_writes(records: object) -> int:
    """Merge committed-write receipts reported by a child worker.

    Malformed entries (missing table name or non-positive row count) are
    dropped. Returns the number of receipts merged into the parent tracker.
    """
    fence = _write_fence.get()
    if fence is None or not isinstance(records, list):
        return 0
    merged = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        table_name = record.get("table_name")
        row_count = record.get("row_count")
        if not isinstance(table_name, str) or not table_name:
            continue
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count <= 0:
            continue
        fence.tracker.committed.append({"table_name": table_name, "row_count": int(row_count)})
        merged += 1
    return merged


def committed_row_count(table_name: str | None = None) -> int:
    """Sum committed rows on the active fence, optionally for one table."""
    return sum(int(record["row_count"]) for record in committed_mysql_writes(table_name))
