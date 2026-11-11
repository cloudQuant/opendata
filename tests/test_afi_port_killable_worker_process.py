"""Regression tests for the killable AkShare script worker.

The ledger has to mean what it says.  On the AFI live host ``crypto_hourly_market``
was recorded ``failed`` with ``Script call timed out after 600 seconds`` at
14:13:01, and the same run kept committing ~60,000 rows until 14:18:44, because
``asyncio.wait_for(asyncio.to_thread(...))`` cancels the awaitable but cannot
stop the thread.  These tests pin the replacement contract: a describable
entrypoint runs in a child process group that is killed *and reaped* before the
parent raises, so a write cannot land after the failure is recorded.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import time
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from opendata.afi_port.runtime import script_worker_runtime as runtime_module
from opendata.afi_port.runtime.write_fence import (
    committed_mysql_writes,
    committed_row_count,
    current_write_fence,
    fence_identity,
    merge_committed_mysql_writes,
    record_committed_write,
    reset_write_fence,
    set_write_fence,
)
from opendata.afi_port.services.script import AkshareScriptService
from tests import afi_killable_worker_cases as cases

CASES_MODULE = "tests.afi_killable_worker_cases"

# Each of these tests spawns a real interpreter; the suite-wide 30s per-test
# budget measures the parent's own work, not a cold `python -m` child start.
pytestmark = pytest.mark.timeout(240)


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wait_for_rows(path: Path, timeout: float = 60.0) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_rows(path)
        if rows:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"worker never wrote its first row to {path}")


async def _wait_for_rows_async(path: Path, timeout: float = 90.0) -> list[dict[str, Any]]:
    """Poll without blocking the loop, so the pending worker task can start."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _read_rows(path)
        if rows:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(f"worker never wrote its first row to {path}")


def _assert_process_group_gone(pid: int) -> None:
    """The child must be dead *and* reaped, so no post-failure write is possible."""
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


@pytest.fixture(scope="module")
def worker_start_budget() -> float:
    """Measure one real worker round trip; the kill tests schedule around it.

    The child has to import ``opendata`` and pandas before it can write anything, so
    a fixed sub-second timeout would kill it before its first commit and the
    test would prove nothing about writes after the failure.
    """
    started = time.monotonic()
    result = asyncio.run(
        AkshareScriptService._execute_callable(cases.echo_value, {"value": 1}, timeout_s=120)
    )
    assert result["pid"] != os.getpid()
    return time.monotonic() - started


@pytest.mark.asyncio
async def test_describable_entrypoint_runs_in_a_killable_child_process() -> None:
    result = await AkshareScriptService._execute_callable(
        cases.echo_value, {"value": "value"}, timeout_s=60
    )

    assert result["value"] == "value"
    assert result["pid"] != os.getpid()


@pytest.mark.asyncio
async def test_child_error_type_and_message_cross_the_process_boundary() -> None:
    with pytest.raises(ValueError, match="provider rejected the request"):
        await AkshareScriptService._execute_callable(cases.raise_value_error, {}, timeout_s=60)


@pytest.mark.asyncio
async def test_dataframe_result_round_trips_through_the_worker(tmp_path: Path) -> None:
    result = await AkshareScriptService._execute_callable(cases.return_frame, {}, timeout_s=60)

    assert isinstance(result, pd.DataFrame)
    assert list(result.columns) == ["symbol", "price"]


@pytest.mark.asyncio
async def test_worker_writes_late_when_it_is_allowed_to_finish(
    tmp_path: Path, worker_start_budget: float
) -> None:
    """Control: the late write really happens when nobody kills the child."""
    rows_path = tmp_path / "committed.jsonl"
    late_path = tmp_path / "late.jsonl"

    await AkshareScriptService._execute_callable(
        cases.commit_then_late_write,
        {
            "rows_path": str(rows_path),
            "late_path": str(late_path),
            "commit_delay": 0.05,
            "late_delay": 1.0,
            "hold_seconds": 0.0,
        },
        timeout_s=worker_start_budget + 30,
    )

    assert len(_read_rows(rows_path)) == 1
    late_rows = _read_rows(late_path)
    assert len(late_rows) == 1
    assert late_rows[0]["phase"] == "write-after-timeout"


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_the_child_group_before_raising(
    tmp_path: Path, worker_start_budget: float
) -> None:
    rows_path = tmp_path / "committed.jsonl"
    late_path = tmp_path / "late.jsonl"
    timeout_s = int(worker_start_budget + 3)
    started = time.monotonic()

    with pytest.raises(
        asyncio.TimeoutError, match=f"Script call timed out after {timeout_s} seconds"
    ):
        await AkshareScriptService._execute_callable(
            cases.commit_then_late_write,
            {
                "rows_path": str(rows_path),
                "late_path": str(late_path),
                "commit_delay": 0.05,
                "late_delay": 20.0,
                "hold_seconds": 60.0,
            },
            timeout_s=timeout_s,
        )

    elapsed = time.monotonic() - started
    assert elapsed < 30, f"kill path was not bounded: {elapsed:.1f}s"
    committed = _wait_for_rows(rows_path)
    child_pid = int(committed[0]["pid"])
    # The ledger can only claim "failed, no side effects" if the writer is gone
    # first: kill the group, reap it, then raise.
    _assert_process_group_gone(child_pid)

    time.sleep(5.0)
    assert _read_rows(late_path) == []


@pytest.mark.asyncio
async def test_cancellation_kills_the_child_before_the_task_returns(
    tmp_path: Path,
) -> None:
    rows_path = tmp_path / "committed.jsonl"
    late_path = tmp_path / "late.jsonl"

    task = asyncio.create_task(
        AkshareScriptService._execute_callable(
            cases.commit_then_late_write,
            {
                "rows_path": str(rows_path),
                "late_path": str(late_path),
                "commit_delay": 0.2,
                "late_delay": 20.0,
                "hold_seconds": 60.0,
            },
            timeout_s=120,
        )
    )
    committed = await _wait_for_rows_async(rows_path)
    child_pid = int(committed[0]["pid"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_process_group_gone(child_pid)
    time.sleep(5.0)
    assert _read_rows(late_path) == []


@pytest.mark.asyncio
async def test_worker_log_output_still_reaches_the_parent_stream(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Killing must not cost the operator the writer's progress trail in the log."""
    result = await AkshareScriptService._execute_callable(
        cases.write_progress_log, {}, timeout_s=90
    )

    captured = capfd.readouterr()
    assert result == "logged"
    assert "worker progress line" in captured.err


@pytest.mark.asyncio
async def test_non_describable_callable_uses_the_visible_thread_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[Any] = []
    real_popen = runtime_module.subprocess.Popen

    def guard_popen(*args: Any, **kwargs: Any) -> Any:
        command = " ".join(str(part) for part in (args[0] if args else []))
        if runtime_module._WORKER_MODULE in command:
            spawned.append(command)
            raise AssertionError("a non-describable callable must not spawn a worker")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", guard_popen)

    def local_callable() -> dict[str, Any]:
        return {"pid": os.getpid()}

    assert AkshareScriptService._callable_descriptor(local_callable) is None
    assert AkshareScriptService._callable_descriptor(lambda: {"ok": True}) is None
    assert (
        AkshareScriptService._callable_descriptor(functools.partial(cases.echo_value, value="x"))
        is None
    )

    result = await AkshareScriptService._execute_callable(local_callable, {}, timeout_s=5)

    assert spawned == []
    assert result == {"pid": os.getpid()}


@pytest.mark.asyncio
async def test_describable_callables_prefer_the_child_process() -> None:
    descriptor = AkshareScriptService._callable_descriptor(cases.echo_value)

    assert descriptor == {"kind": "function", "module": CASES_MODULE, "qualname": "echo_value"}


def test_the_measured_production_entrypoint_is_describable() -> None:
    """``crypto_hourly_market`` is the run that wrote rows after its own failure."""
    from opendata.data_fetch.scripts.cryptos.hourly.crypto_hourly_market import CryptoHourlyMarket

    descriptor = AkshareScriptService._callable_descriptor(CryptoHourlyMarket().fetch_data)

    assert descriptor == {
        "kind": "method",
        "module": "opendata.data_fetch.scripts.cryptos.hourly.crypto_hourly_market",
        "class_qualname": "CryptoHourlyMarket",
        "method": "fetch_data",
    }


@pytest.mark.asyncio
async def test_bound_method_descriptor_round_trips_through_the_worker() -> None:
    instance = cases.RecordingWriter()
    descriptor = AkshareScriptService._callable_descriptor(instance.fetch_data)

    assert descriptor == {
        "kind": "method",
        "module": CASES_MODULE,
        "class_qualname": "RecordingWriter",
        "method": "fetch_data",
    }

    result = await AkshareScriptService._execute_callable_in_worker(
        descriptor or {}, {"rows": 4}, timeout_s=60
    )

    assert isinstance(result, pd.DataFrame)


@pytest.mark.asyncio
async def test_worker_receives_the_parent_write_fence() -> None:
    descriptor = AkshareScriptService._callable_descriptor(cases.return_fence_context)
    assert descriptor is not None
    token = set_write_fence(
        claim_key="claim-1",
        owner_id="owner-1",
        fencing_token=7,
        execution_id="ak_exec_parent",
    )
    try:
        fence = fence_identity(current_write_fence())
        assert fence == {
            "claim_key": "claim-1",
            "owner_id": "owner-1",
            "fencing_token": 7,
            "execution_id": "ak_exec_parent",
        }
        result = await AkshareScriptService._execute_callable_in_worker(
            descriptor, {}, timeout_s=60, fence=fence
        )
    finally:
        reset_write_fence(token)

    assert result == fence


@pytest.mark.asyncio
async def test_child_committed_writes_are_merged_into_the_parent_tracker() -> None:
    instance = cases.RecordingWriter()
    descriptor = AkshareScriptService._callable_descriptor(instance.fetch_data)
    assert descriptor is not None
    token = set_write_fence(
        claim_key="claim-1",
        owner_id="owner-1",
        fencing_token=3,
        execution_id="ak_exec_merge",
    )
    try:
        await AkshareScriptService._execute_callable_in_worker(
            descriptor,
            {"rows": 12},
            timeout_s=60,
            fence=fence_identity(current_write_fence()),
        )
        merged = committed_mysql_writes("worker_receipt_target")
        merged_rows = committed_row_count(cases.RECEIPT_TABLE)
    finally:
        reset_write_fence(token)

    assert merged == [{"table_name": cases.RECEIPT_TABLE, "row_count": 12}]
    assert merged_rows == 12


def test_unfenced_process_records_no_committed_writes() -> None:
    assert record_committed_write(cases.RECEIPT_TABLE, 5) is False
    assert committed_mysql_writes() == []


def test_merge_drops_malformed_receipts_and_matches_table_case() -> None:
    token = set_write_fence(claim_key="claim-1", owner_id="owner-1", fencing_token=3)
    try:
        merged = merge_committed_mysql_writes(
            [
                {"table_name": "FUTURES_DAILY_MARKET", "row_count": 12},
                {"table_name": None, "row_count": 1},
                {"table_name": "FUTURES_DAILY_MARKET", "row_count": 0},
                "not-a-receipt",
            ]
        )

        assert merged == 1
        assert committed_mysql_writes("futures_daily_market") == [
            {"table_name": "FUTURES_DAILY_MARKET", "row_count": 12}
        ]
    finally:
        reset_write_fence(token)
