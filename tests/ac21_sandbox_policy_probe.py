#!/usr/bin/env python3
"""Build and, only with ``--execute``, run AC21's loopback policy probe.

The candidate Seatbelt rule uses ``localhost:<port>`` because the pinned local
parser rejected numeric IPv4 host literals.  A successful localhost connection
alone never proves the requested exact-IPv4 boundary; the three counterfactuals
must be measured before the result can be ``PASS_EXACT_HOST``.

This module uses only the standard library and performs no socket or process
work at import time.  Its public CLI accepts only ``--execute``.  That option
starts only the fixed sandbox-exec/Anaconda worker command and creates listeners
on loopback addresses owned by this process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import signal
import socket
import subprocess  # nosec B404  # only fixed argv below; no shell or caller-provided command
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

BASE_ENVIRONMENT = Path("/Users/yunjinqi/opt/anaconda3")
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
WORKER_BOOTSTRAP = (
    "import runpy,sys; "
    "namespace=runpy.run_path(sys.argv[1]); "
    "raise SystemExit(namespace['_worker_main'](sys.argv[2:]))"
)
WORKER_TIMEOUT_SECONDS = 20
SOCKET_TIMEOUT_SECONDS = 1.0

STATUS_DRY_RUN = "DRY_RUN_NOT_EXECUTED"
STATUS_UNAVAILABLE = "NOT_RUN_UNAVAILABLE"
STATUS_UNSATISFIED = "UNSATISFIED_EXACT_HOST"
STATUS_PASS = "PASS_EXACT_HOST"  # nosec B105  # required outcome label, not credential material
STATUS_FAILED = "PROBE_FAILED"

ENDPOINT_NAMES = (
    "approved_ipv4",
    "wrong_port_ipv4",
    "ipv6_alias_same_port",
    "ipv4_alias_same_port",
)
NEGATIVE_ENDPOINT_NAMES = ENDPOINT_NAMES[1:]


class SocketLike(Protocol):
    """The socket operations used by owned-listener setup and cleanup."""

    def setsockopt(self, level: int, option: int, value: int) -> None: ...

    def bind(self, address: tuple[str, int]) -> None: ...

    def listen(self, backlog: int = ...) -> None: ...

    def getsockname(self) -> tuple[object, ...]: ...

    def fileno(self) -> int: ...

    def setblocking(self, flag: bool) -> None: ...

    def accept(self) -> tuple[SocketLike, object]: ...

    def close(self) -> None: ...


SocketFactory = Callable[[int, int], SocketLike]


@dataclass(frozen=True)
class Endpoint:
    name: str
    host: str
    port: int
    family: int


@dataclass(frozen=True)
class OwnedListener:
    endpoint: Endpoint
    sock: SocketLike


@dataclass(frozen=True)
class BindResult:
    approved_port: int
    wrong_port: int
    listeners: tuple[OwnedListener, ...]


@dataclass(frozen=True)
class Attempt:
    endpoint_name: str
    connected: bool
    error_type: str | None
    error_number: int | None


@dataclass(frozen=True)
class WorkerSummary:
    stage: str
    pid: int
    attempts: tuple[Attempt, ...]
    grandchild: WorkerSummary | None = None


@dataclass(frozen=True)
class Classification:
    status: str
    reason: str


class ListenerUnavailableError(RuntimeError):
    """Raised when the host cannot bind every process-owned counterfactual."""

    def __init__(self, message: str, cleanup: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.cleanup = dict(cleanup or {})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_port(port: object, label: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{label} must be an integer TCP port in [1, 65535]")
    return port


def render_profile(approved_port: int) -> str:
    """Render the one fixed candidate policy; caller cannot supply a host."""
    port = _validate_port(approved_port, "approved_port")
    return (
        "(version 1) (deny default) (allow process-fork) (allow process-exec) "
        "(allow file-read*) "
        f'(allow network-outbound (remote ip "localhost:{port}"))'
    )


def _endpoint_specs(approved_port: int, wrong_port: int) -> tuple[Endpoint, ...]:
    approved = _validate_port(approved_port, "approved_port")
    wrong = _validate_port(wrong_port, "wrong_port")
    if approved == wrong:
        raise ValueError("wrong_port must differ from approved_port")
    return (
        Endpoint("approved_ipv4", "127.0.0.1", approved, socket.AF_INET),
        Endpoint("wrong_port_ipv4", "127.0.0.1", wrong, socket.AF_INET),
        Endpoint("ipv6_alias_same_port", "::1", approved, socket.AF_INET6),
        Endpoint("ipv4_alias_same_port", "127.0.0.2", approved, socket.AF_INET),
    )


def _port_from_socket(sock: SocketLike) -> int:
    address = sock.getsockname()
    if len(address) < 2 or isinstance(address[1], bool) or not isinstance(address[1], int):
        raise ListenerUnavailableError("socket did not return a usable local port")
    return _validate_port(address[1], "bound_port")


def open_owned_listeners(socket_factory: SocketFactory = socket.socket) -> BindResult:
    """Bind all four fixed loopback endpoints on ports selected by the OS.

    A partial bind is closed immediately and reported as unavailable.  No
    preexisting listener is discovered, reused, or closed.
    """
    sockets: list[tuple[str, SocketLike]] = []
    current_name = "approved_ipv4"
    try:
        approved_sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append((current_name, approved_sock))
        approved_sock.bind(("127.0.0.1", 0))
        approved_port = _port_from_socket(approved_sock)
        approved_sock.listen(4)

        current_name = "ipv4_alias_same_port"
        ipv4_alias_sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append((current_name, ipv4_alias_sock))
        ipv4_alias_sock.bind(("127.0.0.2", approved_port))
        if _port_from_socket(ipv4_alias_sock) != approved_port:
            raise ListenerUnavailableError("IPv4 alias did not bind the approved port")
        ipv4_alias_sock.listen(4)

        current_name = "ipv6_alias_same_port"
        ipv6_sock = socket_factory(socket.AF_INET6, socket.SOCK_STREAM)
        sockets.append((current_name, ipv6_sock))
        ipv6_sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        ipv6_sock.bind(("::1", approved_port))
        if _port_from_socket(ipv6_sock) != approved_port:
            raise ListenerUnavailableError("IPv6 alias did not bind the approved port")
        ipv6_sock.listen(4)

        current_name = "wrong_port_ipv4"
        wrong_sock = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        sockets.append((current_name, wrong_sock))
        wrong_sock.bind(("127.0.0.1", 0))
        wrong_port = _port_from_socket(wrong_sock)
        if wrong_port == approved_port:
            raise ListenerUnavailableError("OS returned the approved port for wrong-port control")
        wrong_sock.listen(4)
    except (OSError, ListenerUnavailableError, ValueError) as exc:
        closed: list[str] = []
        close_errors: dict[str, str] = {}
        for socket_name, owned_socket in sockets:
            close_error = _close_one_socket(owned_socket)
            if close_error is None:
                closed.append(socket_name)
            else:
                close_errors[socket_name] = close_error
        cleanup = {
            "closed_listener_names": closed,
            "close_errors": close_errors,
            "partial_listener_names": [name for name, _ in sockets],
            "failed_endpoint_name": current_name,
        }
        raise ListenerUnavailableError(
            f"loopback listener setup unavailable: {type(exc).__name__}", cleanup
        ) from None

    endpoints = _endpoint_specs(approved_port, wrong_port)
    listener_by_name = {
        "approved_ipv4": approved_sock,
        "wrong_port_ipv4": wrong_sock,
        "ipv6_alias_same_port": ipv6_sock,
        "ipv4_alias_same_port": ipv4_alias_sock,
    }
    listeners = tuple(
        OwnedListener(endpoint, listener_by_name[endpoint.name]) for endpoint in endpoints
    )
    return BindResult(approved_port, wrong_port, listeners)


def classify_observations(
    connect_successes: Mapping[str, int],
    listener_accepts: Mapping[str, int],
    *,
    unavailable: bool = False,
) -> Classification:
    """Classify exact-host evidence; syntax acceptance is never sufficient."""
    if unavailable:
        return Classification(
            STATUS_UNAVAILABLE, "required owned loopback listener was unavailable"
        )
    expected_names = set(ENDPOINT_NAMES)
    if set(connect_successes) != expected_names or set(listener_accepts) != expected_names:
        raise ValueError("counter maps must contain exactly the four fixed endpoint names")
    for counts in (connect_successes, listener_accepts):
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise ValueError("counter values must be non-negative integers")

    if any(
        connect_successes[name] > 0 or listener_accepts[name] > 0
        for name in NEGATIVE_ENDPOINT_NAMES
    ):
        return Classification(
            STATUS_UNSATISFIED,
            "at least one wrong-port or loopback-alias counterfactual connected or was accepted",
        )
    if connect_successes["approved_ipv4"] != 2 or listener_accepts["approved_ipv4"] != 2:
        return Classification(
            STATUS_FAILED,
            "both child generations must connect to and reach the owned approved IPv4 listener",
        )
    return Classification(
        STATUS_PASS,
        "approved IPv4 succeeded in child and grandchild; all negative controls were denied",
    )


def _serialize_attempt(attempt: Attempt) -> dict[str, object]:
    return {
        "endpoint_name": attempt.endpoint_name,
        "connected": attempt.connected,
        "error_type": attempt.error_type,
        "error_number": attempt.error_number,
    }


def _serialize_worker(summary: WorkerSummary) -> dict[str, object]:
    return {
        "stage": summary.stage,
        "pid": summary.pid,
        "attempts": [_serialize_attempt(attempt) for attempt in summary.attempts],
        "grandchild": None if summary.grandchild is None else _serialize_worker(summary.grandchild),
    }


def _decode_worker(payload: object, expected_stage: str | None = None) -> WorkerSummary:
    if not isinstance(payload, dict):
        raise ValueError("worker output must be a JSON object")
    stage = payload.get("stage")
    pid = payload.get("pid")
    attempts_payload = payload.get("attempts")
    grandchild_payload = payload.get("grandchild")
    if stage not in ("child", "grandchild") or not isinstance(stage, str):
        raise ValueError("worker output has an invalid stage")
    if expected_stage is not None and stage != expected_stage:
        raise ValueError("worker output stage did not match the requested stage")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("worker output has an invalid pid")
    if not isinstance(attempts_payload, list):
        raise ValueError("worker output attempts must be a list")
    attempts: list[Attempt] = []
    for item in attempts_payload:
        if not isinstance(item, dict):
            raise ValueError("worker attempt must be a JSON object")
        name = item.get("endpoint_name")
        connected = item.get("connected")
        error_type = item.get("error_type")
        error_number = item.get("error_number")
        if not isinstance(name, str) or name not in ENDPOINT_NAMES:
            raise ValueError("worker attempt names an unknown endpoint")
        if not isinstance(connected, bool):
            raise ValueError("worker attempt connected value must be boolean")
        if error_type is not None and not isinstance(error_type, str):
            raise ValueError("worker attempt error_type must be a string or null")
        if error_number is not None and (
            isinstance(error_number, bool) or not isinstance(error_number, int)
        ):
            raise ValueError("worker attempt error_number must be an integer or null")
        attempts.append(Attempt(name, connected, error_type, error_number))
    if tuple(attempt.endpoint_name for attempt in attempts) != ENDPOINT_NAMES:
        raise ValueError("worker must report every fixed endpoint exactly once in order")
    grandchild: WorkerSummary | None = None
    if stage == "child":
        grandchild = _decode_worker(grandchild_payload, "grandchild")
    elif grandchild_payload is not None:
        raise ValueError("grandchild worker cannot report another descendant")
    return WorkerSummary(stage, pid, tuple(attempts), grandchild)


def _worker_attempts(approved_port: int, wrong_port: int) -> tuple[Attempt, ...]:
    attempts: list[Attempt] = []
    for endpoint in _endpoint_specs(approved_port, wrong_port):
        client = socket.socket(endpoint.family, socket.SOCK_STREAM)
        try:
            client.settimeout(SOCKET_TIMEOUT_SECONDS)
            client.connect((endpoint.host, endpoint.port))
        except OSError as exc:
            attempts.append(Attempt(endpoint.name, False, type(exc).__name__, exc.errno))
        else:
            attempts.append(Attempt(endpoint.name, True, None, None))
        finally:
            client.close()
    return tuple(attempts)


def _connect_success_counts(attempts: Sequence[Attempt]) -> dict[str, int]:
    counts = dict.fromkeys(ENDPOINT_NAMES, 0)
    for attempt in attempts:
        counts[attempt.endpoint_name] += int(attempt.connected)
    return counts


def _sum_counts(*counter_maps: Mapping[str, int]) -> dict[str, int]:
    total = dict.fromkeys(ENDPOINT_NAMES, 0)
    for counters in counter_maps:
        for name in ENDPOINT_NAMES:
            total[name] += counters[name]
    return total


def _worker_main(arguments: Sequence[str]) -> int:
    if len(arguments) != 3:
        return 64
    stage, approved_text, wrong_text = arguments
    if stage not in ("child", "grandchild"):
        return 64
    try:
        approved_port = _validate_port(int(approved_text), "approved_port")
        wrong_port = _validate_port(int(wrong_text), "wrong_port")
        attempts = _worker_attempts(approved_port, wrong_port)
        grandchild: WorkerSummary | None = None
        if stage == "child":
            grandchild_argv = [
                sys.executable,
                "-S",
                "-c",
                WORKER_BOOTSTRAP,
                str(Path(__file__).resolve()),
                "grandchild",
                str(approved_port),
                str(wrong_port),
            ]
            child = subprocess.run(  # noqa: S603  # nosec B603  # fixed interpreter, bootstrap, script, stage, and owned ports
                grandchild_argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=WORKER_TIMEOUT_SECONDS,
                check=False,
                close_fds=True,
                shell=False,
                env=os.environ.copy(),
            )
            if child.returncode != 0:
                return child.returncode
            decoded: object = json.loads(child.stdout)
            grandchild = _decode_worker(decoded, "grandchild")
        summary = WorkerSummary(stage, os.getpid(), attempts, grandchild)
        print(json.dumps(_serialize_worker(summary), sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return 70


def _worker_argv(stage: str, approved_port: int, wrong_port: int) -> list[str]:
    if stage not in ("child", "grandchild"):
        raise ValueError("worker stage is not in the fixed child set")
    approved = _validate_port(approved_port, "approved_port")
    wrong = _validate_port(wrong_port, "wrong_port")
    if approved == wrong:
        raise ValueError("wrong_port must differ from approved_port")
    return [
        sys.executable,
        "-S",
        "-c",
        WORKER_BOOTSTRAP,
        str(Path(__file__).resolve()),
        stage,
        str(approved),
        str(wrong),
    ]


def _source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _executable_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
    }


def _attempt_success_counts(summary: WorkerSummary) -> dict[str, int]:
    counts = _connect_success_counts(summary.attempts)
    if summary.grandchild is not None:
        counts = _sum_counts(counts, _connect_success_counts(summary.grandchild.attempts))
    return counts


def _accept_ready_count(listener: OwnedListener) -> tuple[int, str | None]:
    count = 0
    try:
        listener.sock.setblocking(False)
        while True:
            readable, _, _ = select.select([listener.sock], [], [], 0)
            if not readable:
                return count, None
            connection, _address = listener.sock.accept()
            count += 1
            connection.close()
    except OSError as exc:
        return count, type(exc).__name__


def _close_one_socket(sock: SocketLike) -> str | None:
    try:
        sock.close()
    except OSError as exc:
        return type(exc).__name__
    return None


def _drain_owned_listeners(
    listeners: Sequence[OwnedListener],
) -> tuple[dict[str, int], dict[str, str]]:
    accepts = dict.fromkeys(ENDPOINT_NAMES, 0)
    errors: dict[str, str] = {}
    for owned in listeners:
        count, error = _accept_ready_count(owned)
        accepts[owned.endpoint.name] = count
        if error is not None:
            errors[owned.endpoint.name] = error
    return accepts, errors


def _close_owned_listeners(listeners: Sequence[OwnedListener]) -> dict[str, object]:
    closed: list[str] = []
    errors: dict[str, str] = {}
    for owned in listeners:
        error = _close_one_socket(owned.sock)
        if error is None:
            closed.append(owned.endpoint.name)
        else:
            errors[owned.endpoint.name] = error
    return {"closed_listener_names": closed, "close_errors": errors}


def _decode_outer_worker(stdout: str) -> WorkerSummary:
    decoded: object = json.loads(stdout)
    return _decode_worker(decoded, "child")


def enforce_cleanup_gate(
    classification: Classification,
    *,
    expected_listener_names: Sequence[str],
    cleanup: Mapping[str, object],
    process_group_kill_error: str | None,
) -> Classification:
    """Fail closed if an owned socket or timed-out worker group was not cleaned up."""
    closed = cleanup.get("closed_listener_names")
    close_errors = cleanup.get("close_errors")
    closed_names_match = (
        isinstance(closed, list)
        and all(isinstance(name, str) for name in closed)
        and tuple(expected_listener_names) == tuple(closed)
    )
    if not closed_names_match:
        reason = "owned listener cleanup did not close every socket created by this run"
    elif not isinstance(close_errors, dict) or close_errors:
        reason = "owned listener cleanup reported one or more close errors"
    elif process_group_kill_error is not None:
        reason = f"owned worker process-group cleanup failed: {process_group_kill_error}"
    else:
        return classification
    return Classification(
        STATUS_FAILED,
        f"{reason}; prior observation was {classification.status}: {classification.reason}",
    )


def execute_probe() -> dict[str, object]:
    """Run only the fixed local probe; caller must have supplied ``--execute``."""
    started_at = _utc_now()
    run_id = str(uuid.uuid4())
    owned_listeners: tuple[OwnedListener, ...] = ()
    process: subprocess.Popen[str] | None = None
    process_group_id: int | None = None
    process_group_kill_attempted = False
    process_group_killed = False
    process_group_kill_error: str | None = None
    worker_summary: WorkerSummary | None = None
    stdout = ""
    stderr = ""
    run_error: str | None = None
    unavailable_reason: str | None = None
    bind_setup_cleanup: dict[str, object] = {}
    listener_accepts = dict.fromkeys(ENDPOINT_NAMES, 0)
    preflight_attempts: tuple[Attempt, ...] = ()
    preflight_accepts = dict.fromkeys(ENDPOINT_NAMES, 0)
    listener_errors: dict[str, str] = {}
    cleanup: dict[str, object] = {"closed_listener_names": [], "close_errors": {}}
    command_argv: list[str] = []
    profile = ""
    executable_hash: str | None = None
    source_hash_before: str | None = None
    source_hash_after: str | None = None
    approved_port: int | None = None
    wrong_port: int | None = None
    try:
        if Path(sys.prefix).resolve() != BASE_ENVIRONMENT.resolve():
            unavailable_reason = "execute mode must run from the configured Anaconda base prefix"
            raise ListenerUnavailableError(unavailable_reason, bind_setup_cleanup)
        if not SANDBOX_EXEC.is_file() or not os.access(SANDBOX_EXEC, os.X_OK):
            unavailable_reason = "sandbox-exec is unavailable at the pinned system path"
            raise ListenerUnavailableError(unavailable_reason, bind_setup_cleanup)
        source_hash_before = _source_sha256()
        executable_hash = _executable_sha256(SANDBOX_EXEC)
        bound = open_owned_listeners()
        owned_listeners = bound.listeners
        approved_port = bound.approved_port
        wrong_port = bound.wrong_port
        profile = render_profile(approved_port)
        preflight_attempts = _worker_attempts(approved_port, wrong_port)
        preflight_accepts, preflight_errors = _drain_owned_listeners(owned_listeners)
        preflight_successes = _connect_success_counts(preflight_attempts)
        if preflight_errors or any(
            preflight_successes[name] != 1 or preflight_accepts[name] != 1
            for name in ENDPOINT_NAMES
        ):
            unavailable_reason = "one or more owned loopback canaries were not locally reachable"
            bind_setup_cleanup = {
                "preflight_listener_errors": preflight_errors,
                "preflight_connect_successes": preflight_successes,
                "preflight_listener_accepts": preflight_accepts,
            }
            raise ListenerUnavailableError(unavailable_reason, bind_setup_cleanup)
        command_argv = [
            str(SANDBOX_EXEC),
            "-p",
            profile,
            *_worker_argv("child", approved_port, wrong_port),
        ]
        process = subprocess.Popen(  # noqa: S603  # nosec B603  # fixed sandbox, interpreter, bootstrap, script, stage, and owned ports
            command_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
            shell=False,
            start_new_session=True,
            env=_minimal_environment(),
        )
        process_group_id = process.pid
        try:
            stdout, stderr = process.communicate(timeout=WORKER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process_group_kill_attempted = True
            try:
                os.killpg(process_group_id, signal.SIGKILL)
                process_group_killed = True
            except ProcessLookupError:
                pass
            except OSError as exc:
                process_group_kill_error = type(exc).__name__
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process_group_kill_error = process_group_kill_error or "worker_group_wait_timeout"
            run_error = "owned worker process group timed out and was terminated"
        if process.returncode != 0 and run_error is None:
            run_error = f"sandbox worker exited with code {process.returncode}"
        if process.returncode == 0:
            worker_summary = _decode_outer_worker(stdout)
    except ListenerUnavailableError as exc:
        unavailable_reason = unavailable_reason or str(exc)
        bind_setup_cleanup = exc.cleanup
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        run_error = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None and process.poll() is None and process_group_id is not None:
            process_group_kill_attempted = True
            try:
                os.killpg(process_group_id, signal.SIGKILL)
                process_group_killed = True
            except ProcessLookupError:
                pass
            except OSError as exc:
                process_group_kill_error = type(exc).__name__
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process_group_kill_error = process_group_kill_error or "worker_group_wait_timeout"
                run_error = run_error or "owned worker process group did not exit after termination"
        if owned_listeners:
            listener_accepts, listener_errors = _drain_owned_listeners(owned_listeners)
            cleanup = _close_owned_listeners(owned_listeners)
        if source_hash_before is not None:
            try:
                source_hash_after = _source_sha256()
            except OSError:
                run_error = run_error or "source file became unreadable after the probe"

    connect_successes = (
        _attempt_success_counts(worker_summary)
        if worker_summary is not None
        else dict.fromkeys(ENDPOINT_NAMES, 0)
    )
    if unavailable_reason is not None:
        classification = classify_observations(
            connect_successes, listener_accepts, unavailable=True
        )
    elif any(
        connect_successes[name] > 0 or listener_accepts[name] > 0
        for name in NEGATIVE_ENDPOINT_NAMES
    ):
        classification = classify_observations(connect_successes, listener_accepts)
    elif run_error is not None or worker_summary is None or listener_errors:
        classification = Classification(
            STATUS_FAILED, run_error or "worker/listener evidence was incomplete"
        )
    else:
        classification = classify_observations(connect_successes, listener_accepts)
    if source_hash_before is not None and source_hash_after != source_hash_before:
        classification = Classification(
            STATUS_FAILED, "probe source bytes changed during execution"
        )
    expected_listener_names = tuple(owned.endpoint.name for owned in owned_listeners)
    cleanup_evidence: Mapping[str, object] = cleanup
    if not expected_listener_names:
        partial_names = bind_setup_cleanup.get("partial_listener_names", [])
        if isinstance(partial_names, list) and all(isinstance(name, str) for name in partial_names):
            expected_listener_names = tuple(partial_names)
            cleanup_evidence = bind_setup_cleanup
    classification = enforce_cleanup_gate(
        classification,
        expected_listener_names=expected_listener_names,
        cleanup=cleanup_evidence,
        process_group_kill_error=process_group_kill_error,
    )

    ended_at = _utc_now()
    process_ids: dict[str, int | None] = {
        "probe_pid": os.getpid(),
        "sandbox_launcher_pid": None if process is None else process.pid,
        "worker_pid": None if worker_summary is None else worker_summary.pid,
        "grandchild_pid": (
            None
            if worker_summary is None or worker_summary.grandchild is None
            else worker_summary.grandchild.pid
        ),
    }
    attempts: list[dict[str, object]] = []
    if worker_summary is not None:
        attempts.extend(
            _serialize_attempt(item) | {"stage": "child"} for item in worker_summary.attempts
        )
        if worker_summary.grandchild is not None:
            attempts.extend(
                _serialize_attempt(item) | {"stage": "grandchild"}
                for item in worker_summary.grandchild.attempts
            )
    return {
        "schema_version": 1,
        "record_type": "ac21_sandbox_loopback_policy_probe",
        "run_id": run_id,
        "started_at_utc": started_at,
        "ended_at_utc": ended_at,
        "status": classification.status,
        "reason": classification.reason,
        "execute_requested": True,
        "profile": profile,
        "profile_sha256": hashlib.sha256(profile.encode("utf-8")).hexdigest() if profile else None,
        "profile_host_syntax": (
            "localhost:<approved-port>; exact IPv4 only if measured controls prove it"
        ),
        "sandbox_exec_path": str(SANDBOX_EXEC),
        "sandbox_exec_sha256": executable_hash,
        "source_path": str(Path(__file__).resolve()),
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "argv": command_argv,
        "environment_names": sorted(_minimal_environment()),
        "process_ids": process_ids,
        "ports": {"approved_port": approved_port, "wrong_port": wrong_port},
        "listener_bindings": [
            {
                "name": owned.endpoint.name,
                "host": owned.endpoint.host,
                "port": owned.endpoint.port,
                "owned_by_probe": True,
            }
            for owned in owned_listeners
        ],
        "attempts": attempts,
        "preflight_attempts": [_serialize_attempt(item) for item in preflight_attempts],
        "counters": {
            "preflight_connect_successes": _connect_success_counts(preflight_attempts),
            "preflight_listener_accepts": preflight_accepts,
            "connect_successes": connect_successes,
            "listener_accepts": listener_accepts,
            "listener_accepts_total": _sum_counts(preflight_accepts, listener_accepts),
            "listener_errors": listener_errors,
        },
        "process": {
            "return_code": None if process is None else process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "process_group_id": process_group_id,
            "process_group_kill_attempted": process_group_kill_attempted,
            "process_group_killed": process_group_killed,
            "process_group_kill_error": process_group_kill_error,
            "error": run_error,
        },
        "cleanup": cleanup | {"partial_bind_cleanup": bind_setup_cleanup},
    }


def dry_run_report() -> dict[str, object]:
    """Describe the fixed plan without hashing files, binding, or spawning."""
    return {
        "schema_version": 1,
        "record_type": "ac21_sandbox_loopback_policy_probe",
        "status": STATUS_DRY_RUN,
        "execute_requested": False,
        "generated_at_utc": _utc_now(),
        "probe_pid": os.getpid(),
        "profile_template": (
            "(version 1) (deny default) (allow process-fork) (allow process-exec) "
            "(allow file-read*) "
            '(allow network-outbound (remote ip "localhost:<approved-port>"))'
        ),
        "profile_host_syntax": (
            "localhost:<port> is not exact-IP proof; aliases require measured counterfactuals"
        ),
        "fixed_endpoints": [
            {"name": "approved_ipv4", "host": "127.0.0.1", "port": "OS-assigned"},
            {"name": "wrong_port_ipv4", "host": "127.0.0.1", "port": "different OS-assigned port"},
            {"name": "ipv6_alias_same_port", "host": "::1", "port": "approved port"},
            {"name": "ipv4_alias_same_port", "host": "127.0.0.2", "port": "approved port"},
        ],
        "effects": {
            "socket_created": False,
            "process_spawned": False,
            "probe_filesystem_output_written": False,
        },
    }


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the fixed local loopback probe and emit its receipt as JSON",
    )
    parsed = parser.parse_args(arguments)
    report = execute_probe() if parsed.execute else dry_run_report()
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
