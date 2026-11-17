"""In-process unit tests for the killable script worker child module.

``opendata.afi_port.runtime.script_worker`` is the child half of the killable worker:
the parent describes one entrypoint, the child rebuilds it, installs the write
fence, runs the call and writes a single pickled receipt.  The end-to-end tests
exercise it by spawning a real interpreter, which is a process the parent's
coverage cannot see.  These tests drive the same functions directly so every
decision the child makes is pinned where it is made.
"""

from __future__ import annotations

import contextvars
import io
import os
import pickle
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

if TYPE_CHECKING:
    from pathlib import Path

from opendata.afi_port.runtime import script_worker as worker
from opendata.afi_port.runtime import script_worker_runtime as runtime_module
from opendata.afi_port.runtime.write_fence import (
    committed_mysql_writes,
    current_execution_id,
    current_write_fence,
    fence_identity,
    record_committed_write,
    record_execution_id,
    require_write_fence,
    reset_write_fence,
    set_write_fence,
)
from tests import afi_killable_worker_cases as cases

CASES_MODULE = "tests.afi_killable_worker_cases"
THIS_MODULE = "tests.test_afi_port_script_worker_unit"


class _ExitRequested(BaseException):
    """Raised in place of ``os._exit`` so a test can observe the exit request."""


class _Holder:
    """Nested attribute chain and bound/class-level entrypoints."""

    table_name = "HOLDER_TABLE"

    class Inner:
        @staticmethod
        def entry() -> str:
            return "nested"

    @classmethod
    def class_entry(cls) -> str:
        return f"classmethod:{cls.__name__}"

    def method_entry(self) -> str:
        return f"method:{self.table_name}"


async def _async_entry(value: Any = None) -> dict[str, Any]:
    return {"awaited": value}


def _awaitable_returning_entry(value: Any = None) -> Any:
    return _async_entry(value)


def _strict_entry(fund_code: str | None = None, year: str | None = None) -> dict[str, Any]:
    """A run()-style entrypoint that does NOT accept internal control kwargs."""
    return {"fund_code": fund_code, "year": year}


def _kwargs_catchall_entry(**kwargs: Any) -> dict[str, Any]:
    """An entrypoint with a catch-all, which must receive every parameter."""
    return dict(kwargs)


@pytest.fixture
def fenced() -> Any:
    """Hold a write fence with committed-write tracking for one test."""
    token = set_write_fence(
        claim_key="claim-unit",
        owner_id="owner-unit",
        fencing_token=7,
        execution_id="ak_exec_unit",
    )
    try:
        yield current_write_fence()
    finally:
        reset_write_fence(token)


def _run_isolated(call: Any) -> None:
    """Run ``call`` in a fresh context so a fence it installs cannot leak."""
    contextvars.copy_context().run(call)


# --------------------------------------------------------------------------
# Descriptor rebuild: the child must resolve exactly what the parent described.
# --------------------------------------------------------------------------


def test_function_descriptor_rebuilds_the_identical_callable() -> None:
    descriptor = runtime_module._callable_descriptor(cases.echo_value)

    assert descriptor == {"kind": "function", "module": CASES_MODULE, "qualname": "echo_value"}
    assert worker._resolve_callable(descriptor) is cases.echo_value


def test_method_descriptor_rebuilds_a_bound_method_on_a_fresh_instance() -> None:
    instance = cases.RecordingWriter()
    descriptor = runtime_module._callable_descriptor(instance.fetch_data)
    assert descriptor is not None
    assert descriptor["kind"] == "method"

    rebuilt = worker._resolve_callable(descriptor)

    assert isinstance(rebuilt.__self__, cases.RecordingWriter)
    # The child gets its own instance; it must not alias the parent's object.
    assert rebuilt.__self__ is not instance


def test_classmethod_descriptor_rebuilds_the_class_level_call() -> None:
    rebuilt = worker._resolve_callable(
        {
            "kind": "classmethod",
            "module": THIS_MODULE,
            "class_qualname": "_Holder",
            "method": "class_entry",
        }
    )

    assert rebuilt() == "classmethod:_Holder"


def test_method_descriptor_without_class_qualname_is_not_resolvable() -> None:
    with pytest.raises(KeyError):
        worker._resolve_callable({"kind": "method", "module": CASES_MODULE, "method": "fetch_data"})


def test_resolve_qualname_walks_a_dotted_attribute_chain() -> None:
    import asyncio as stdlib_asyncio

    # The child rebuilds from ``module`` + dotted ``qualname`` only, so nested
    # attributes and class owners must resolve to the very same objects.
    assert worker._resolve_qualname(worker, "asyncio.run") is stdlib_asyncio.run
    assert worker._resolve_qualname(_Holder, "Inner.entry") is _Holder.Inner.entry
    assert worker._resolve_class({"module": THIS_MODULE, "class_qualname": "_Holder.Inner"}) is (
        _Holder.Inner
    )


def test_resolve_callable_falls_back_to_qualname_when_kind_is_absent() -> None:
    assert worker._resolve_callable({"module": CASES_MODULE, "qualname": "echo_value"}) is (
        cases.echo_value
    )


# --------------------------------------------------------------------------
# _install_write_recorder: committed legacy batches stay attributable.
# --------------------------------------------------------------------------


def test_recorder_attributes_writes_to_the_instance_default_table(fenced: Any) -> None:
    instance = cases.RecordingWriter()
    worker._install_write_recorder(instance.fetch_data)

    instance.save_data(pd.DataFrame({"symbol": ["a", "b"]}))

    assert committed_mysql_writes() == [{"table_name": cases.RECEIPT_TABLE, "row_count": 2}]


def test_recorder_prefers_an_explicit_positional_table_name(fenced: Any) -> None:
    instance = cases.RecordingWriter()
    worker._install_write_recorder(instance.fetch_data)

    instance.save_data(pd.DataFrame({"symbol": ["a"]}), "OTHER_TABLE")

    assert committed_mysql_writes() == [{"table_name": "OTHER_TABLE", "row_count": 1}]


def test_recorder_prefers_an_explicit_keyword_table_name(fenced: Any) -> None:
    instance = cases.RecordingWriter()
    worker._install_write_recorder(instance.fetch_data)

    instance.save_data(pd.DataFrame({"symbol": ["a"]}), table_name="KW_TABLE")

    assert committed_mysql_writes() == [{"table_name": "KW_TABLE", "row_count": 1}]


def test_recorder_keeps_the_original_return_value(fenced: Any) -> None:
    instance = cases.RecordingWriter()
    worker._install_write_recorder(instance.fetch_data)

    assert instance.save_data(pd.DataFrame({"symbol": ["a", "b", "c"]})) == 3


def test_recorder_ignores_writes_that_committed_nothing(fenced: Any) -> None:
    class _ZeroWriter:
        table_name = "ZERO_TABLE"

        def save_data(self, df: pd.DataFrame, table_name: str | None = None) -> int:
            return 0

    instance = _ZeroWriter()
    worker._install_write_recorder(instance.save_data)

    instance.save_data(pd.DataFrame({"symbol": ["a"]}))

    assert committed_mysql_writes() == []


def test_recorder_ignores_non_int_and_boolean_results(fenced: Any) -> None:
    class _OddWriter:
        table_name = "ODD_TABLE"

        def __init__(self) -> None:
            self.next_result: Any = None

        def save_data(self, df: pd.DataFrame, table_name: str | None = None) -> Any:
            return self.next_result

    instance = _OddWriter()
    worker._install_write_recorder(instance.save_data)

    for result in (None, True, False, "12", 3.0):
        instance.next_result = result
        instance.save_data(pd.DataFrame({"symbol": ["a"]}))

    assert committed_mysql_writes() == []


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(cases.echo_value, id="plain-function"),
        pytest.param(_Holder.class_entry, id="classmethod-bound-to-type"),
    ],
)
def test_recorder_is_a_noop_for_callables_without_an_instance(target: Any, fenced: Any) -> None:
    worker._install_write_recorder(target)

    assert committed_mysql_writes() == []


def test_recorder_leaves_instances_without_save_data_untouched(fenced: Any) -> None:
    class _NoSave:
        def run(self) -> str:
            return "ok"

    instance = _NoSave()
    worker._install_write_recorder(instance.run)

    assert instance.run() == "ok"
    assert committed_mysql_writes() == []


# --------------------------------------------------------------------------
# _invoke: sync, coroutine-function and awaitable-returning entrypoints.
# --------------------------------------------------------------------------


def test_invoke_runs_a_plain_function_entrypoint() -> None:
    result = worker._invoke(
        {"kind": "function", "module": CASES_MODULE, "qualname": "echo_value"},
        {"value": 5},
    )

    assert result["value"] == 5
    # Same process: the child contract is that the result is produced in-process.
    assert result["pid"] == os.getpid()


def test_invoke_drives_a_coroutine_function_entrypoint() -> None:
    result = worker._invoke(
        {"kind": "function", "module": THIS_MODULE, "qualname": "_async_entry"},
        {"value": "awaited"},
    )

    assert result == {"awaited": "awaited"}


def test_invoke_drives_an_awaitable_returned_by_a_sync_entrypoint() -> None:
    result = worker._invoke(
        {"kind": "function", "module": THIS_MODULE, "qualname": "_awaitable_returning_entry"},
        {"value": 2},
    )

    assert result == {"awaited": 2}


def test_invoke_returns_a_plain_non_awaitable_result() -> None:
    assert (
        worker._invoke(
            {"kind": "function", "module": CASES_MODULE, "qualname": "write_progress_log"},
            {},
        )
        == "logged"
    )


def test_invoke_propagates_the_entrypoint_error_type() -> None:
    with pytest.raises(ValueError, match="provider rejected the request"):
        worker._invoke(
            {"kind": "function", "module": CASES_MODULE, "qualname": "raise_value_error"},
            {},
        )


def test_invoke_installs_the_recorder_before_calling(fenced: Any) -> None:
    descriptor = runtime_module._callable_descriptor(cases.RecordingWriter().fetch_data)
    assert descriptor is not None

    result = worker._invoke(descriptor, {"rows": 4})

    assert isinstance(result, pd.DataFrame)
    assert committed_mysql_writes(cases.RECEIPT_TABLE) == [
        {"table_name": cases.RECEIPT_TABLE, "row_count": 4}
    ]


# --------------------------------------------------------------------------
# _accepted_kwargs: internal control params must not crash strict run() entrypoints.
# --------------------------------------------------------------------------


def test_accepted_kwargs_drops_undeclared_internal_controls() -> None:
    # Mirrors fund_portfolio_hold_em.run(fund_code, year): _call_timeout and
    # other injected controls are not declared, so they must be filtered out.
    filtered = worker._accepted_kwargs(
        _strict_entry,
        {"fund_code": "000001", "year": "2024", "_call_timeout": 60, "_ingest_mode": "backfill"},
    )
    assert filtered == {"fund_code": "000001", "year": "2024"}


def test_invoke_strips_unexpected_keyword_instead_of_raising() -> None:
    descriptor = {"kind": "function", "module": THIS_MODULE, "qualname": "_strict_entry"}
    result = worker._invoke(
        descriptor,
        {"fund_code": "000001", "year": "2024", "_call_timeout": 90},
    )
    assert result == {"fund_code": "000001", "year": "2024"}


def test_accepted_kwargs_keeps_everything_for_var_kwargs_entrypoint() -> None:
    params = {"symbol": "1.000001", "_call_timeout": 30, "extra": True}
    assert worker._accepted_kwargs(_kwargs_catchall_entry, params) == params


def test_invoke_forwards_all_params_to_catchall() -> None:
    descriptor = {"kind": "function", "module": THIS_MODULE, "qualname": "_kwargs_catchall_entry"}
    params = {"symbol": "1.000001", "_call_timeout": 30}
    assert worker._invoke(descriptor, params) == params


# --------------------------------------------------------------------------
# _apply_fence: the parent's identity travels into the child.
# --------------------------------------------------------------------------


def test_apply_fence_installs_identity_and_execution_id() -> None:
    observed: dict[str, Any] = {}

    def _child() -> None:
        worker._apply_fence(
            {
                "claim_key": "claim-a",
                "owner_id": "owner-a",
                "fencing_token": 4,
                "execution_id": "ak_exec_42",
            }
        )
        observed["identity"] = fence_identity(require_write_fence())

    _run_isolated(_child)

    assert observed["identity"] == {
        "claim_key": "claim-a",
        "owner_id": "owner-a",
        "fencing_token": 4,
        "execution_id": "ak_exec_42",
    }
    # The fence belongs to the child context only; it never leaks outward.
    assert current_write_fence() is None


def test_apply_fence_without_execution_id_leaves_the_ledger_id_unset() -> None:
    observed: dict[str, Any] = {}

    def _child() -> None:
        worker._apply_fence({"claim_key": "claim-b", "owner_id": "owner-b", "fencing_token": 0})
        observed["execution_id"] = current_execution_id()

    _run_isolated(_child)

    assert observed["execution_id"] is None


def test_apply_fence_skips_an_empty_execution_id() -> None:
    observed: dict[str, Any] = {}

    def _child() -> None:
        worker._apply_fence(
            {
                "claim_key": "claim-c",
                "owner_id": "owner-c",
                "fencing_token": 1,
                "execution_id": "",
            }
        )
        observed["execution_id"] = current_execution_id()

    _run_isolated(_child)

    assert observed["execution_id"] is None


@pytest.mark.parametrize("fence", [None, "not-a-dict", 5, ["claim"]])
def test_apply_fence_ignores_a_non_dict_fence(fence: Any) -> None:
    _run_isolated(lambda: worker._apply_fence(fence))

    assert current_write_fence() is None


def test_apply_fence_needs_claim_key_owner_and_token() -> None:
    with pytest.raises(KeyError):
        _run_isolated(lambda: worker._apply_fence({"owner_id": "o", "fencing_token": 1}))

    with pytest.raises(ValueError, match="claim key"):
        _run_isolated(
            lambda: worker._apply_fence({"claim_key": "", "owner_id": "o", "fencing_token": 1})
        )

    with pytest.raises(ValueError, match="owner id"):
        _run_isolated(
            lambda: worker._apply_fence({"claim_key": "c", "owner_id": "", "fencing_token": 1})
        )

    with pytest.raises(ValueError, match="non-negative"):
        _run_isolated(
            lambda: worker._apply_fence({"claim_key": "c", "owner_id": "o", "fencing_token": -1})
        )


# --------------------------------------------------------------------------
# Write-fence primitives the child depends on while unfenced.
# --------------------------------------------------------------------------


def test_unfenced_context_has_no_identity_and_records_nothing() -> None:
    assert current_write_fence() is None
    assert fence_identity(None) is None
    assert current_execution_id() is None
    assert record_execution_id("ak_exec_orphan") is None
    assert record_committed_write("SOME_TABLE", 10) is False
    assert committed_mysql_writes() == []
    with pytest.raises(RuntimeError, match="without a write fence"):
        require_write_fence()


def test_record_committed_write_rejects_unusable_receipts(fenced: Any) -> None:
    assert record_committed_write("", 10) is False
    assert record_committed_write("TABLE_A", 0) is False
    assert record_committed_write("TABLE_A", -3) is False
    assert committed_mysql_writes() == []


def test_record_execution_id_attaches_the_ledger_id_to_the_active_fence(fenced: Any) -> None:
    record_execution_id("ak_exec_overwrite")

    assert current_execution_id() == "ak_exec_overwrite"


# --------------------------------------------------------------------------
# _write_receipt: one pickled receipt, always.
# --------------------------------------------------------------------------


def test_write_receipt_round_trips_the_body(tmp_path: Path) -> None:
    path = tmp_path / "receipt.pkl"

    worker._write_receipt(str(path), {"ok": True, "result": {"value": 1}})

    with open(path, "rb") as handle:
        # Trusted fixture: the test itself wrote this receipt.
        assert pickle.load(handle) == {"ok": True, "result": {"value": 1}}  # noqa: S301


def test_write_receipt_falls_back_when_the_result_is_unpicklable(
    tmp_path: Path, fenced: Any
) -> None:
    path = tmp_path / "receipt.pkl"
    record_committed_write("TABLE_A", 3)

    worker._write_receipt(str(path), {"ok": True, "result": lambda: None})

    with open(path, "rb") as handle:
        receipt = pickle.load(handle)  # noqa: S301  # trusted test-written receipt
    assert receipt["ok"] is False
    assert receipt["error_type"] == "UnpicklableResult"
    assert "could not be serialized" in receipt["error"]
    assert receipt["traceback"]
    # The committed rows stay attributable even when the result cannot travel.
    assert receipt["committed_writes"] == [{"table_name": "TABLE_A", "row_count": 3}]


# --------------------------------------------------------------------------
# main(): the whole child turn - envelope in, receipt out, hard exit.
# --------------------------------------------------------------------------


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    envelope: dict[str, Any],
) -> dict[str, Any]:
    """Drive ``worker.main`` with a canned envelope and a catchable exit."""
    result_path = str(tmp_path / "receipt.pkl")
    envelope = {**envelope, "result_path": result_path}
    stdin = SimpleNamespace(buffer=io.BytesIO(pickle.dumps(envelope, protocol=5)))

    def _exit(_code: int) -> None:
        raise _ExitRequested

    monkeypatch.setattr(worker.sys, "stdin", stdin)
    monkeypatch.setattr(worker.os, "_exit", _exit)
    proxy_calls: list[bool] = []
    monkeypatch.setattr(
        worker,
        "configure_akshare_network_proxy",
        lambda: proxy_calls.append(True),
    )

    with pytest.raises(_ExitRequested):
        _run_isolated(worker.main)

    with open(result_path, "rb") as handle:
        receipt = pickle.load(handle)  # noqa: S301  # trusted test-written receipt
    receipt["_proxy_configured"] = bool(proxy_calls)
    return receipt


def test_main_writes_a_success_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    receipt = _run_main(
        monkeypatch,
        tmp_path,
        {
            "fence": {
                "claim_key": "claim-main",
                "owner_id": "owner-main",
                "fencing_token": 2,
                "execution_id": "ak_exec_main",
            },
            "descriptor": {"kind": "function", "module": CASES_MODULE, "qualname": "echo_value"},
            "params": {"value": "hello"},
        },
    )

    assert receipt["ok"] is True
    assert receipt["result"]["value"] == "hello"
    assert receipt["committed_writes"] == []
    # The child configures the network proxy before running the entrypoint.
    assert receipt["_proxy_configured"] is True


def test_main_collects_committed_writes_of_a_successful_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _run_main(
        monkeypatch,
        tmp_path,
        {
            "fence": {
                "claim_key": "claim-writes",
                "owner_id": "owner-writes",
                "fencing_token": 1,
                "execution_id": "ak_exec_writes",
            },
            "descriptor": {
                "kind": "method",
                "module": CASES_MODULE,
                "class_qualname": "RecordingWriter",
                "method": "fetch_data",
            },
            "params": {"rows": 6},
        },
    )

    assert receipt["ok"] is True
    assert receipt["committed_writes"] == [{"table_name": cases.RECEIPT_TABLE, "row_count": 6}]


def test_main_writes_an_error_receipt_for_a_failing_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _run_main(
        monkeypatch,
        tmp_path,
        {
            "fence": None,
            "descriptor": {
                "kind": "function",
                "module": CASES_MODULE,
                "qualname": "raise_value_error",
            },
            "params": {},
        },
    )

    assert receipt["ok"] is False
    assert receipt["error_type"] == "ValueError"
    assert receipt["error"] == "provider rejected the request"
    assert "raise_value_error" in receipt["traceback"]
    assert receipt["_proxy_configured"] is True


def test_main_caps_a_very_long_error_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    receipt = _run_main(
        monkeypatch,
        tmp_path,
        {
            "fence": None,
            "descriptor": {"kind": "function", "module": THIS_MODULE, "qualname": "_raise_long"},
            "params": {},
        },
    )

    assert receipt["ok"] is False
    assert len(receipt["error"]) == worker._MAX_ERROR_CHARS
    assert len(receipt["traceback"]) <= worker._MAX_TRACEBACK_CHARS


def test_main_reports_empty_writes_when_the_tracker_itself_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _boom() -> list[dict[str, Any]]:
        raise RuntimeError("tracker unusable")

    monkeypatch.setattr(worker, "_child_committed_writes", _boom)

    receipt = _run_main(
        monkeypatch,
        tmp_path,
        {
            "fence": None,
            "descriptor": {
                "kind": "function",
                "module": CASES_MODULE,
                "qualname": "raise_value_error",
            },
            "params": {},
        },
    )

    # A failed run must still produce exactly one receipt the parent can read.
    assert receipt["ok"] is False
    assert receipt["committed_writes"] == []


def _raise_long() -> None:
    raise RuntimeError("x" * (worker._MAX_ERROR_CHARS + 500))


def test_main_exits_without_interpreter_finalisation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exits: list[int] = []

    def _exit(code: int) -> None:
        exits.append(code)
        raise _ExitRequested

    result_path = str(tmp_path / "receipt.pkl")
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                pickle.dumps(
                    {
                        "result_path": result_path,
                        "fence": None,
                        "descriptor": {
                            "kind": "function",
                            "module": CASES_MODULE,
                            "qualname": "echo_value",
                        },
                        "params": {"value": 1},
                    },
                    protocol=5,
                )
            )
        ),
    )
    monkeypatch.setattr(worker.os, "_exit", _exit)

    with pytest.raises(_ExitRequested):
        _run_isolated(worker.main)

    # ``os._exit(0)`` is the guarantee that a blocked non-daemon thread cannot
    # keep the killable worker alive after its receipt is on disk.
    assert exits == [0]
