"""Killable child-process worker for one AkShare script call.

Started as ``python -m opendata.afi_port.runtime.script_worker`` by the parent-side
supervisor in ``script_worker_runtime._execute_callable_in_worker``.  The parent
sends one pickled envelope on stdin and reads one pickled receipt from
``result_path``.

The child is started in its own process group so that a timeout can SIGKILL the
whole worker tree and reap it *before* the parent records the failure.  A
thread-based call cannot offer that: ``asyncio.wait_for`` cancels the awaitable
but the thread keeps running, and the provider rows it commits afterwards land
after the ledger already says ``failed``.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import os
import pickle
import sys
import traceback
from typing import TYPE_CHECKING, Any

from opendata.afi_port.runtime.network_proxy import configure_akshare_network_proxy
from opendata.afi_port.runtime.write_fence import (
    committed_mysql_writes,
    record_committed_write,
    record_execution_id,
    set_write_fence,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_ERROR_CHARS = 4000
_MAX_TRACEBACK_CHARS = 20000


def _resolve_qualname(module: Any, qualname: str) -> Any:  # noqa: ANN401
    target = module
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _resolve_class(descriptor: dict[str, Any]) -> Any:  # noqa: ANN401
    module = importlib.import_module(str(descriptor["module"]))
    return _resolve_qualname(module, str(descriptor["class_qualname"]))


def _resolve_callable(descriptor: dict[str, Any]) -> Any:  # noqa: ANN401
    """Rebuild the parent's entrypoint from its module + qualname descriptor."""
    kind = str(descriptor.get("kind") or "function")
    if kind == "method":
        owner_class = _resolve_class(descriptor)
        instance = owner_class()
        return getattr(instance, str(descriptor["method"]))
    if kind == "classmethod":
        owner_class = _resolve_class(descriptor)
        return getattr(owner_class, str(descriptor["method"]))
    module = importlib.import_module(str(descriptor["module"]))
    return _resolve_qualname(module, str(descriptor["qualname"]))


def _install_write_recorder(callable_obj: Any) -> None:  # noqa: ANN401
    """Attribute the legacy writer's committed batches to the active fence.

    Legacy AkShare scripts write through ``self.save_data(df, table_name)``,
    which commits on its own MySQL connection and returns the number of rows
    written.  Wrapping it on the resolved instance is the only way to collect
    committed-write receipts inside the child without editing the writer.
    """
    instance = getattr(callable_obj, "__self__", None)
    if instance is None or isinstance(instance, type):
        return
    save_data = getattr(instance, "save_data", None)
    if not callable(save_data):
        return
    default_table = getattr(instance, "table_name", None)

    @functools.wraps(save_data)
    def recording_save_data(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        written = save_data(*args, **kwargs)
        table_name = kwargs.get("table_name")
        if table_name is None and len(args) > 1:
            table_name = args[1]
        if table_name is None:
            table_name = default_table
        if isinstance(written, int) and not isinstance(written, bool) and written > 0:
            record_committed_write(str(table_name or ""), written)
        return written

    instance.save_data = recording_save_data


def _run_in_child_loop(awaitable_factory: Callable[[], Any]) -> Any:  # noqa: ANN401
    """Drive one awaitable to completion inside a fresh event loop."""

    async def _driver() -> Any:  # noqa: ANN401
        return await awaitable_factory()

    return asyncio.run(_driver())


def _accepted_kwargs(callable_obj: Any, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """Filter ``params`` to what ``callable_obj`` actually accepts.

    Legacy scripts carry internal controls (``_call_timeout``, ``_ingest_mode``,
    ``symbol`` defaults, ...) in the merged parameter set. They are meaningful
    for the AkshareToMySql fetch helpers that pop them, but a script's own
    ``run(**kwargs)`` often does not declare them, so passing them raises
    ``TypeError: unexpected keyword argument``. When the target accepts a
    ``**kwargs`` catch-all everything is forwarded; otherwise keep only its
    declared, callable parameters and drop the rest rather than crash.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        # Built-ins/partials without introspectable signatures: forward as-is.
        return dict(params)
    parameters = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return dict(params)
    accepted = {
        name: value
        for name, value in params.items()
        if name in parameters
        and parameters[name].kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }
    return accepted


def _invoke(descriptor: dict[str, Any], params: dict[str, Any]) -> Any:  # noqa: ANN401
    callable_obj = _resolve_callable(descriptor)
    _install_write_recorder(callable_obj)
    safe_params = _accepted_kwargs(callable_obj, params)
    if inspect.iscoroutinefunction(callable_obj):
        return _run_in_child_loop(lambda: callable_obj(**safe_params))
    result = callable_obj(**safe_params)
    if inspect.isawaitable(result):
        return _run_in_child_loop(lambda: result)
    return result


def _apply_fence(fence: Any) -> None:  # noqa: ANN401
    if not isinstance(fence, dict):
        return
    execution_id = fence.get("execution_id")
    set_write_fence(
        claim_key=str(fence["claim_key"]),
        owner_id=str(fence["owner_id"]),
        fencing_token=int(fence["fencing_token"]),
    )
    if isinstance(execution_id, str) and execution_id:
        record_execution_id(execution_id)


def _child_committed_writes() -> list[dict[str, Any]]:
    return committed_mysql_writes()


def _write_receipt(path: str, body: dict[str, Any]) -> None:
    try:
        with open(path, "wb") as handle:
            pickle.dump(body, handle, protocol=5)
    except BaseException:
        fallback = {
            "ok": False,
            "error_type": "UnpicklableResult",
            "error": "script result could not be serialized",
            "traceback": traceback.format_exc()[-_MAX_TRACEBACK_CHARS:],
            "committed_writes": _child_committed_writes(),
        }
        with open(path, "wb") as handle:
            pickle.dump(fallback, handle, protocol=5)


def main() -> None:
    """Child entrypoint: read one envelope from the parent and write its receipt."""
    # The envelope comes from the parent supervisor over an inherited stdin pipe,
    # not from an untrusted peer: the trust boundary is the local process family.
    envelope: Any = pickle.load(sys.stdin.buffer)  # noqa: S301
    result_path = str(envelope["result_path"])
    committed_writes: list[dict[str, Any]] = []
    body: dict[str, Any]
    try:
        _apply_fence(envelope.get("fence"))
        configure_akshare_network_proxy()
        result = _invoke(
            dict(envelope["descriptor"]),
            dict(envelope["params"]),
        )
        committed_writes = _child_committed_writes()
        body = {"ok": True, "result": result, "committed_writes": committed_writes}
    except BaseException as exc:
        try:
            committed_writes = _child_committed_writes()
        except BaseException:
            committed_writes = []
        body = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:_MAX_ERROR_CHARS],
            "traceback": traceback.format_exc()[-_MAX_TRACEBACK_CHARS:],
            "committed_writes": committed_writes,
        }
    _write_receipt(result_path, body)
    sys.stdout.flush()
    sys.stderr.flush()
    # Skip interpreter finalisation: a blocked non-daemon thread inside the
    # provider client must never keep the killable worker alive.
    os._exit(0)


if __name__ == "__main__":
    main()
