"""Parent-side supervisor for the killable AkShare script worker process.

``script_worker.py`` is the child entrypoint that runs *inside* the subprocess.
This module is the other half: the parent side, which describes a script
entrypoint so the child can rebuild it from ``module`` + ``qualname``, starts
``python -m opendata.afi_port.runtime.script_worker`` in its own session (process
group), carries the write fence (``claim_key`` / ``owner_id`` /
``fencing_token`` / ``execution_id``) into the child, merges the child's
committed-write receipts back onto the parent tracker, and SIGKILLs *and reaps*
the whole group before a failure leaves this module.

The kill must happen here rather than in the async layer: ``asyncio.wait_for``
around a thread cancels the awaitable but cannot stop the thread, which is
exactly how the live host recorded ``crypto_hourly_market`` as ``failed`` at 601s
and then watched the same run save ~60,000 rows five minutes later.

``AkshareScriptService`` keeps only the call site: the budget a script call gets
(:func:`_script_timeout_seconds`), whether the entrypoint is describable
(:func:`_callable_descriptor`), and which execution path runs it --
:func:`_execute_callable_in_worker` for the killable child process group,
:func:`_execute_callable_in_thread` for a callable the child cannot rebuild.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import functools
import importlib
import inspect
import os
import pickle
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opendata.afi_port.runtime.network_proxy import configure_akshare_network_proxy
from opendata.afi_port.runtime.write_fence import merge_committed_mysql_writes

if TYPE_CHECKING:
    from opendata.models.data_script import DataScript

_WORKER_MODULE = "opendata.afi_port.runtime.script_worker"
_WORKER_REAP_TIMEOUT_SECONDS = 10.0
_WORKER_CEILING_GRACE_SECONDS = 5.0

# The kill budget a script call gets, i.e. the point at which the child process group
# is killed and reaped.  It lives beside the supervisor that enforces it because an
# hourly task whose budget is shorter than its honest write time is exactly the
# failure the killable child exists to make visible.
_SCRIPT_MIN_TIMEOUT_SECONDS: dict[str, float] = {
    "macro_bank_english_interest_rate": 90.0,
    "macro_china_agricultural_product": 150.0,
    "macro_china_bdti_index": 150.0,
    "macro_china_bond_public": 150.0,
    "macro_china_cpi_yearly": 150.0,
    "macro_china_exports_yoy": 150.0,
    "macro_china_insurance": 420.0,
    "macro_china_retail_price_index": 240.0,
    "macro_china_society_traffic_volume": 240.0,
    "macro_cons_gold": 240.0,
    "macro_cons_opec_month": 180.0,
    "macro_cons_silver": 180.0,
    "macro_euro_cpi_yoy": 120.0,
    "macro_euro_ppi_mom": 120.0,
    "macro_global_sox_index": 240.0,
    "etf_fund_hist_em": 600.0,
    # Measured on this host 2026-11-05: the 14:03 UTC occurrence was marked failed at
    # 601 s, yet the same run kept committing rows until 14:18:44 (~946 s) — 59,983
    # rows. An hourly task with a 3,600 s cadence gets a 1,800 s floor: ~2x the observed
    # catch-up run, still inside the next occurrence. The task/script rows still say 600;
    # this floor is the auditable place where the budget lives, not a hand-edited row.
    "crypto_hourly_market": 1800.0,
    "macro_shipping_bdi": 120.0,
    "macro_shipping_bpi": 120.0,
    "macro_usa_api_crude_stock": 120.0,
    "macro_usa_cb_consumer_confidence": 120.0,
    "macro_usa_core_cpi_monthly": 150.0,
    "macro_usa_core_pce_price": 120.0,
    "macro_usa_cpi_monthly": 150.0,
    "macro_usa_eia_crude_rate": 150.0,
    "macro_usa_exist_home_sales": 120.0,
    "macro_usa_house_starts": 120.0,
    "macro_usa_industrial_production": 120.0,
    "macro_usa_initial_jobless": 180.0,
    "macro_usa_ism_pmi": 120.0,
    "macro_usa_michigan_consumer_sentiment": 120.0,
    "macro_usa_new_home_sales": 120.0,
    "macro_usa_nfib_small_business": 150.0,
    "macro_usa_personal_spending": 120.0,
}


def _script_timeout_seconds(
    script: DataScript | None = None,
    timeout_seconds: int | float | None = None,
) -> float:
    script_id = script.script_id if script is not None else None
    min_timeout = _SCRIPT_MIN_TIMEOUT_SECONDS.get(script_id, 0.0) if script_id is not None else 0.0
    if timeout_seconds is not None and float(timeout_seconds) > 0:
        return max(float(timeout_seconds), min_timeout)
    script_timeout = script.timeout if script is not None else None
    if script_timeout is not None and float(script_timeout) > 0:
        return max(float(script_timeout), min_timeout)
    raw_timeout = os.getenv("AKSHARE_SCRIPT_TIMEOUT") or os.getenv("AKSHARE_CALL_TIMEOUT") or "60"
    try:
        return max(float(raw_timeout), min_timeout)
    except ValueError:
        return max(60.0, min_timeout)


class _WorkerTimeoutError(RuntimeError):
    """Raised inside the blocking worker wait when the child outlives its budget."""


def _resolve_attribute_chain(root: Any, qualname: str) -> Any:  # noqa: ANN401
    """Follow a dotted ``qualname`` from an imported module, as the child does."""
    target = root
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _backend_root() -> str:
    """Directory holding the ``app`` package; used as the worker import root."""
    return str(Path(__file__).resolve().parents[3])


def _worker_environment() -> dict[str, str]:
    """Environment for the child worker, with the parent's import roots exported."""
    env = os.environ.copy()
    paths: list[str] = [_backend_root()]
    existing = env.get("PYTHONPATH")
    if existing:
        paths.extend(part for part in str(existing).split(os.pathsep) if part)
    paths.extend(
        entry for entry in sys.path if isinstance(entry, str) and entry and os.path.isdir(entry)
    )
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return env


def _callable_descriptor(callable_obj: Any) -> dict[str, str] | None:  # noqa: ANN401
    """Describe an entrypoint as ``module`` + ``qualname`` for child execution.

    Returns ``None`` for anything the child cannot rebuild identically
    (closures, ``functools.partial``, locally defined functions, objects
    whose defining module is not importable); those keep the legacy thread
    path, which is the only option for a non-importable callable.
    """
    if isinstance(callable_obj, functools.partial):
        return None
    module_name = getattr(callable_obj, "__module__", None)
    qualname = getattr(callable_obj, "__qualname__", None)
    if not isinstance(module_name, str) or not isinstance(qualname, str):
        return None
    if not module_name or "<locals>" in qualname:
        return None
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None

    owner = getattr(callable_obj, "__self__", None)
    if owner is not None:
        method_name = getattr(callable_obj, "__name__", None)
        if not isinstance(method_name, str) or not method_name:
            return None
        owner_class = owner if isinstance(owner, type) else type(owner)
        class_module_name = getattr(owner_class, "__module__", None)
        class_qualname = getattr(owner_class, "__qualname__", None)
        if not isinstance(class_module_name, str) or not isinstance(class_qualname, str):
            return None
        if "<locals>" in class_qualname:
            return None
        try:
            class_module = importlib.import_module(class_module_name)
            resolved_class = _resolve_attribute_chain(class_module, class_qualname)
        except Exception:
            return None
        if resolved_class is not owner_class or not isinstance(owner_class, type):
            return None
        class_attribute = getattr(owner_class, method_name, None)
        if class_attribute is None:
            return None
        if isinstance(class_attribute, (classmethod, staticmethod)):
            underlying = class_attribute.__func__
        else:
            underlying = class_attribute
        expected = getattr(callable_obj, "__func__", callable_obj)
        if underlying is not expected:
            return None
        kind = "classmethod" if isinstance(owner, type) else "method"
        return {
            "kind": kind,
            "module": class_module_name,
            "class_qualname": class_qualname,
            "method": method_name,
        }

    try:
        resolved = _resolve_attribute_chain(module, qualname)
    except AttributeError:
        return None
    if resolved is not callable_obj:
        return None
    return {"kind": "function", "module": module_name, "qualname": qualname}


def _kill_worker_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the worker's process group and reap it before returning."""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, AttributeError):
        pgid = None
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    try:
        proc.wait(timeout=_WORKER_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        proc.wait(timeout=_WORKER_REAP_TIMEOUT_SECONDS)


def _await_worker(
    proc: subprocess.Popen[bytes],
    payload: bytes,
    timeout_s: float,
) -> tuple[int, bytes, bytes]:
    """Block for the child, killing and reaping its group if it runs long.

    The kill happens here rather than in the async layer so that
    ``_execute_callable_in_worker`` cannot raise a failure while the worker
    is still able to commit rows.
    """
    try:
        stdout, stderr = proc.communicate(input=payload, timeout=timeout_s)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        _kill_worker_group(proc)
        leftover = _drain_worker_pipes(proc)
        raise _WorkerTimeoutError(
            f"worker exceeded {timeout_s:g} seconds: {leftover[-300:].decode('utf-8', 'replace')}"
        ) from None


def _drain_worker_pipes(proc: subprocess.Popen[bytes]) -> bytes:
    """Read whatever the finished worker left on its captured pipes."""
    output = b""
    try:
        if proc.stdout is not None:
            output += proc.stdout.read()
    except (OSError, ValueError):
        pass
    return output


def _read_worker_receipt(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            receipt = pickle.load(handle)  # noqa: S301  # receipt is a local temp file written by the worker child
    except (OSError, ValueError, EOFError, pickle.UnpicklingError):
        return None
    return receipt if isinstance(receipt, dict) else None


def _worker_exception(receipt: dict[str, Any]) -> Exception:
    error_type = str(receipt.get("error_type") or "RuntimeError")
    message = str(receipt.get("error") or "Script call failed in the worker process")
    candidate: Any = getattr(builtins, error_type, None)
    exception_class: type[Exception] = RuntimeError
    if isinstance(candidate, type) and issubclass(candidate, Exception):
        exception_class = candidate
    return exception_class(message)


async def _execute_callable_in_worker(
    descriptor: dict[str, str],
    params: dict[str, Any],
    timeout_s: float,
    fence: dict[str, Any] | None = None,
) -> Any:  # noqa: ANN401
    """Run one script entrypoint in a killable child process group.

    On timeout or cancellation the whole group is SIGKILLed and reaped
    before the error leaves this function, so no provider write can land
    after the caller records the failure.  Committed-write receipts the
    child collected are merged into the parent's write-fence tracker.
    """
    receipt_dir = tempfile.mkdtemp(prefix="akshare-script-worker-")
    receipt_path = os.path.join(receipt_dir, "receipt.pkl")
    payload = pickle.dumps(
        {
            "descriptor": descriptor,
            "params": params,
            "result_path": receipt_path,
            "fence": fence,
        },
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    proc = subprocess.Popen(  # noqa: S603  # fixed argv: sys.executable -m <first-party worker module>
        [sys.executable, "-m", _WORKER_MODULE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # Inherit the parent's stderr so the writer's progress lines keep
        # flowing into the operator log stream.
        stderr=None,
        cwd=_backend_root(),
        env=_worker_environment(),
        start_new_session=True,
    )
    try:
        try:
            returncode, stdout, _ = await asyncio.wait_for(
                asyncio.to_thread(_await_worker, proc, payload, timeout_s),
                timeout=timeout_s + _WORKER_CEILING_GRACE_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            _kill_worker_group(proc)
            raise asyncio.TimeoutError(
                f"Script call timed out after {timeout_s:g} seconds"
            ) from exc
        except _WorkerTimeoutError as exc:
            _kill_worker_group(proc)
            raise asyncio.TimeoutError(
                f"Script call timed out after {timeout_s:g} seconds"
            ) from exc
        except asyncio.CancelledError:
            _kill_worker_group(proc)
            raise

        receipt = _read_worker_receipt(receipt_path)
        if receipt is None:
            detail = stdout[-500:].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Script worker exited with code {returncode} and left no receipt: {detail}"
            )
        merge_committed_mysql_writes(receipt.get("committed_writes"))
        if not receipt.get("ok"):
            raise _worker_exception(receipt)
        return receipt.get("result")
    finally:
        if proc.poll() is None:
            _kill_worker_group(proc)
        shutil.rmtree(receipt_dir, ignore_errors=True)


_CONTROL_PARAMETER_KEYS = frozenset({"claim_key", "owner_id", "fencing_token", "execution_id"})


def _signature_parameters(callable_obj: Any, params: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """Drop stored parameters the entrypoint does not declare.

    Scheduled tasks keep legacy keys (``overlap_days`` and friends) that older
    runners accepted. A callable without ``**kwargs`` rejects them, so only the
    names it declares are forwarded; callables with ``**kwargs`` still receive
    every non-control parameter.
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return params
    parameters = signature.parameters.values()
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
        return params
    accepted = {
        name
        for name, item in signature.parameters.items()
        if item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {key: value for key, value in params.items() if key in accepted}


async def _execute_callable_in_thread(
    callable_obj: Any,  # noqa: ANN401
    params: dict[str, Any],
    timeout_s: float,
) -> Any:  # noqa: ANN401
    configure_akshare_network_proxy()
    if inspect.iscoroutinefunction(callable_obj):
        awaitable = callable_obj(**params)
    else:
        awaitable = asyncio.to_thread(callable_obj, **params)

    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise asyncio.TimeoutError(f"Script call timed out after {timeout_s:g} seconds") from exc


async def _execute_callable(
    callable_obj: Any,  # noqa: ANN401
    params: dict[str, Any],
    timeout_s: float,
    fence: dict[str, Any] | None = None,
) -> Any:  # noqa: ANN401
    """Execute a script entrypoint, preferring a killable child process.

    Script entrypoints are module functions or bound methods resolved from
    ``module`` + ``qualname``; those run in a child process group the parent
    can kill.  A callable the child cannot rebuild identically (a closure, a
    ``functools.partial``, a locally defined function) falls back to the
    legacy thread path, which cannot be killed and is therefore only used
    when there is no describable entrypoint.
    """
    # ``_``-prefixed keys are dispatch metadata (``_ingest_mode``,
    # ``_recovery_scope_hash``, ...) and the fence identity travels beside the
    # call. Entrypoints forward ``**kwargs`` upstream, which rejects both, so
    # neither may reach the script.
    script_params = {
        key: value
        for key, value in params.items()
        if not str(key).startswith("_") and key not in _CONTROL_PARAMETER_KEYS
    }
    script_params = _signature_parameters(callable_obj, script_params)
    descriptor = _callable_descriptor(callable_obj)
    if descriptor is not None:
        return await _execute_callable_in_worker(
            descriptor,
            script_params,
            timeout_s,
            fence=fence,
        )
    return await _execute_callable_in_thread(callable_obj, script_params, timeout_s)
