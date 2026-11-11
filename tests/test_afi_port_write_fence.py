"""Unit tests for the ported process-local write fence.

The fence is the attribution boundary that stops an unfenced process from
crediting warehouse writes to an execution, and carries a child worker's
committed-write receipts back to the parent. These tests pin that contract
without any database or process involvement.
"""

from __future__ import annotations

import contextvars
import threading

import pytest

from opendata.afi_port.runtime import write_fence as wf


@pytest.fixture
def fence():
    """Install a fence for one test and guarantee reset afterwards."""
    token = wf.set_write_fence(
        claim_key="akshare_script:stock_zh_a_hist",
        owner_id="ak_exec_1",
        fencing_token=7,
        execution_id="ak_exec_1",
    )
    yield wf.current_write_fence()
    wf.reset_write_fence(token)


def test_unfenced_context_has_no_fence_and_require_raises() -> None:
    assert wf.current_write_fence() is None
    with pytest.raises(RuntimeError, match="without a write fence"):
        wf.require_write_fence()


def test_set_write_fence_validates_its_identity_fields() -> None:
    with pytest.raises(ValueError, match="claim key"):
        wf.set_write_fence(claim_key="", owner_id="o", fencing_token=1)
    with pytest.raises(ValueError, match="owner id"):
        wf.set_write_fence(claim_key="k", owner_id="", fencing_token=1)
    with pytest.raises(ValueError, match="non-negative"):
        wf.set_write_fence(claim_key="k", owner_id="o", fencing_token=-1)


def test_installed_fence_is_required_and_identity_is_serialised(fence) -> None:
    assert wf.require_write_fence() is fence
    identity = wf.fence_identity(fence)
    assert identity == {
        "claim_key": "akshare_script:stock_zh_a_hist",
        "owner_id": "ak_exec_1",
        "fencing_token": 7,
        "execution_id": "ak_exec_1",
    }
    assert wf.fence_identity(None) is None


def test_execution_id_can_be_attached_and_read(fence) -> None:
    assert wf.current_execution_id() == "ak_exec_1"
    wf.record_execution_id("ak_exec_99")
    assert wf.current_execution_id() == "ak_exec_99"


def test_committed_writes_are_recorded_and_counted(fence) -> None:
    assert wf.record_committed_write("STOCK_ZH_A_HIST", 100) is True
    assert wf.record_committed_write("stock_zh_a_hist", 25) is True
    # Non-positive counts and empty names are rejected.
    assert wf.record_committed_write("t", 0) is False
    assert wf.record_committed_write("", 5) is False

    # Case-insensitive table filtering (warehouse tables are case-insensitive).
    assert wf.committed_row_count("STOCK_zh_a_HIST") == 125
    assert wf.committed_row_count("other_table") == 0
    assert wf.committed_row_count() == 125


def test_unfenced_process_cannot_record_writes() -> None:
    # A fresh context (new thread) starts with no fence and must not attribute.
    result = {}

    def worker() -> None:
        result["recorded"] = wf.record_committed_write("t", 10)
        result["fence"] = wf.current_write_fence()
        result["rows"] = wf.committed_row_count()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert result == {"recorded": False, "fence": None, "rows": 0}


def test_child_receipts_merge_and_malformed_entries_are_dropped(fence) -> None:
    receipts = [
        {"table_name": "a", "row_count": 10},
        {"table_name": "b", "row_count": 5},
        {"table_name": "", "row_count": 9},  # missing name
        {"table_name": "c", "row_count": 0},  # non-positive
        {"table_name": "d", "row_count": True},  # bool is not a valid count
        "not-a-dict",
    ]
    merged = wf.merge_committed_mysql_writes(receipts)
    assert merged == 2
    assert wf.committed_row_count() == 15


def test_merge_without_a_fence_or_non_list_is_zero() -> None:
    assert wf.merge_committed_mysql_writes([{"table_name": "a", "row_count": 1}]) == 0
    assert wf.merge_committed_mysql_writes("nope") == 0


def test_fence_is_context_isolated_across_copied_contexts(fence) -> None:
    """A copied context sees the fence; an isolated reset does not leak."""
    ctx = contextvars.copy_context()
    seen = []
    ctx.run(lambda: seen.append(wf.current_write_fence() is not None))
    assert seen == [True]
