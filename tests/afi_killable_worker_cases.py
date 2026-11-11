"""Child-importable entrypoints for the killable AkShare script worker tests.

Every callable here is module-level (or a method of a module-level class) so
``AkshareScriptService._callable_descriptor`` can describe it and the worker
process can rebuild it from ``module`` + ``qualname`` alone.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import pandas as pd

RECEIPT_TABLE = "WORKER_RECEIPT_TARGET"


def _append_row(path: str, payload: dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def echo_value(value: Any = None) -> dict[str, Any]:
    """Return the payload plus the pid that produced it."""
    return {"value": value, "pid": os.getpid()}


def return_frame() -> pd.DataFrame:
    """Return a DataFrame that must survive the pickled receipt boundary."""
    return pd.DataFrame({"symbol": ["a", "b"], "price": [1.0, 2.0]})


def raise_value_error() -> None:
    """Fail with a provider-shaped error the parent must re-raise verbatim."""
    raise ValueError("provider rejected the request")


def sleep_for(secs: float) -> str:
    """Block for ``secs`` seconds; only ever used past its own budget."""
    time.sleep(secs)
    return "late"


def write_progress_log() -> str:
    """Emit one progress line the way a legacy writer does, on stderr."""
    print("worker progress line", file=sys.stderr, flush=True)  # log-visibility probe
    return "logged"


def commit_then_late_write(
    rows_path: str,
    late_path: str,
    commit_delay: float = 0.0,
    late_delay: float = 2.0,
    hold_seconds: float = 30.0,
) -> dict[str, Any]:
    """Commit one row, then commit again after a deliberate second delay.

    The second write is the regression: it is exactly what the live host
    observed when ``crypto_hourly_market`` was recorded ``failed`` at 601s and
    kept saving rows five minutes later.  With a killable group the late row can
    never appear.
    """
    if commit_delay:
        time.sleep(commit_delay)
    _append_row(rows_path, {"pid": os.getpid(), "phase": "committed-before-timeout"})
    time.sleep(late_delay)
    _append_row(late_path, {"pid": os.getpid(), "phase": "write-after-timeout"})
    time.sleep(hold_seconds)
    return {"pid": os.getpid()}


def return_fence_context() -> dict[str, Any]:
    """Echo the write fence the parent installed in this process."""
    from opendata.afi_port.runtime.write_fence import current_write_fence

    fence = current_write_fence()
    if fence is None:
        return {}
    return {
        "claim_key": fence.claim_key,
        "owner_id": fence.owner_id,
        "fencing_token": fence.fencing_token,
        "execution_id": fence.tracker.execution_id,
    }


class RecordingWriter:
    """Legacy-shaped writer: commits batches through ``save_data``."""

    table_name = RECEIPT_TABLE

    def __init__(self) -> None:
        self.saved: list[tuple[str, int]] = []

    def save_data(self, df: pd.DataFrame, table_name: str | None = None) -> int:
        table = str(table_name or self.table_name)
        self.saved.append((table, int(len(df))))
        return int(len(df))

    def fetch_data(self, rows: int = 3, fence_path: str | None = None) -> pd.DataFrame:
        """Write ``rows`` rows and record the fence this process is holding."""
        if fence_path:
            from opendata.afi_port.runtime.write_fence import current_write_fence

            fence = current_write_fence()
            payload: dict[str, Any] = {"fence": None}
            if fence is not None:
                payload = {
                    "fence": {
                        "claim_key": fence.claim_key,
                        "owner_id": fence.owner_id,
                        "fencing_token": fence.fencing_token,
                        "execution_id": fence.tracker.execution_id,
                    }
                }
            with open(fence_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
        frame = pd.DataFrame({"symbol": ["a"] * int(rows)})
        written = self.save_data(frame, self.table_name)
        return pd.DataFrame({"symbol": ["written"], "rows": [written]})
