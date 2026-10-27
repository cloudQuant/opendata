"""Real-process tests for the reviewed pipeline process executor."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

from opendata.pipeline import process_executor as process_executor_module
from opendata.pipeline.process_executor import (
    ProcessExecutionCancelledError,
    ProcessExecutionCleanupError,
    ProcessExecutionCrashedError,
    ProcessExecutionHandlerError,
    ProcessExecutionProtocolError,
    ProcessExecutionTimeoutError,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)


def _echo(parameters: dict[str, object]) -> dict[str, object]:
    return parameters


def _write_pid_and_wait(parameters: dict[str, object]) -> dict[str, object]:
    marker = Path(str(parameters["marker"]))
    marker.write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(0.02)


def _crash_without_response(_parameters: dict[str, object]) -> dict[str, object]:
    os._exit(23)


def _return_large_value(_parameters: dict[str, object]) -> str:
    return "x" * 10_000


def _return_synthetic_four_mib_base64_body(_parameters: dict[str, object]) -> dict[str, object]:
    """Return a deterministic synthetic payload exercising Base64 IPC expansion."""
    raw_body = b"\x00" * (4 * 1024 * 1024)
    return {"body_b64": base64.b64encode(raw_body).decode("ascii")}


def _return_default_text_cap_plus_one(_parameters: dict[str, object]) -> str:
    return "x" * (process_executor_module.MAX_JSON_TEXT_BYTES + 1)


def _return_result_text_cap_plus_one(_parameters: dict[str, object]) -> str:
    return "x" * (process_executor_module.MAX_RESULT_TEXT_BYTES + 1)


def _return_secret_like_value(_parameters: dict[str, object]) -> dict[str, object]:
    return {"authorization": "Bearer secret-value-should-not-cross-ipc"}


def _print_large_output(_parameters: dict[str, object]) -> dict[str, object]:
    print("sensitive-output-" * 100)
    print("warning-output-" * 100, file=sys.stderr)
    return {"ok": True}


def _raise_with_secret(_parameters: dict[str, object]) -> dict[str, object]:
    raise RuntimeError("provider response included secret-value-should-not-escape")


def _spawn_descendant_and_wait(parameters: dict[str, object]) -> dict[str, object]:
    child_pid = os.fork()
    if child_pid == 0:
        time.sleep(30)
        os._exit(0)
    Path(str(parameters["marker"])).write_text(str(child_pid), encoding="utf-8")
    while True:
        time.sleep(0.02)


def _spawn_descendant_then_return(parameters: dict[str, object]) -> dict[str, object]:
    child_pid = os.fork()
    if child_pid == 0:
        time.sleep(30)
        os._exit(0)
    Path(str(parameters["marker"])).write_text(str(child_pid), encoding="utf-8")
    return {"done": True}


def _registry(**operations) -> ReviewedProcessRegistry:
    return ReviewedProcessRegistry(operations)


def _load_worker_helper():
    helper_path = Path(process_executor_module.__file__).with_name("process_executor_worker.py")
    spec = importlib.util.spec_from_file_location(
        "process_executor_worker_test_helper", helper_path
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def _frame(message: bytes) -> bytes:
    return struct.pack(">I", len(message)) + message


def _write_test_worker(tmp_path: Path, mode: str, marker: Path | None = None) -> Path:
    script = tmp_path / f"worker_{mode}.py"
    if mode == "before_command":
        if marker is None:
            raise ValueError("before_command worker requires a marker path")
        script.write_text(
            "import os, time\n"
            f"with open({str(marker)!r}, 'w', encoding='utf-8') as marker_file:\n"
            "    marker_file.write(str(os.getpid()))\n"
            "while True:\n"
            "    time.sleep(0.02)\n",
            encoding="utf-8",
        )
        return script
    common = """
import json
import os
import socket
import struct
import sys
import time

channel = socket.socket(fileno=int(sys.argv[1]))
def read_exact(size):
    value = bytearray()
    while len(value) < size:
        chunk = channel.recv(size - len(value))
        if not chunk:
            raise EOFError
        value.extend(chunk)
    return bytes(value)
size = struct.unpack('>I', read_exact(4))[0]
command = json.loads(read_exact(size))
marker = command['parameters']['marker']
def send(value):
    encoded = json.dumps(value, separators=(',', ':')).encode()
    channel.sendall(struct.pack('>I', len(encoded)) + encoded)
"""
    if mode == "partial":
        body = """
send({'type': 'ready', 'isolated': True})
channel.sendall(struct.pack('>I', 32))
with open(marker, 'w', encoding='utf-8') as marker_file:
    marker_file.write(str(os.getpid()))
while True:
    time.sleep(0.02)
"""
    elif mode == "before_ready":
        body = """
with open(marker, 'w', encoding='utf-8') as marker_file:
    marker_file.write(str(os.getpid()))
while True:
    time.sleep(0.02)
"""
    elif mode == "before_ready_fork":
        body = """
child_pid = os.fork()
if child_pid == 0:
    while True:
        time.sleep(0.02)
with open(marker, 'w', encoding='utf-8') as marker_file:
    marker_file.write(str(child_pid))
while True:
    time.sleep(0.02)
"""
    elif mode == "after_result":
        body = """
send({'type': 'ready', 'isolated': True})
send({'type': 'result', 'ok': True, 'value': {'ready': True}, 'stdout_bytes': 0,
      'stderr_bytes': 0, 'stdout_truncated': False, 'stderr_truncated': False})
with open(marker, 'w', encoding='utf-8') as marker_file:
    marker_file.write(str(os.getpid()))
while True:
    time.sleep(0.02)
"""
    else:
        raise ValueError("unsupported test worker mode")
    script.write_text(common + body, encoding="utf-8")
    return script


def _pid_is_running(pid: int) -> bool:
    """Treat absent and zombie processes as stopped on Linux CI hosts."""
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            return stat_path.read_text(encoding="utf-8").split()[2] != "Z"
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_stopped(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.02)
    return not _pid_is_running(pid)


class _FakeSocket:
    def __init__(self, underlying: socket.socket) -> None:
        self.underlying = underlying
        self.closed = False

    def setblocking(self, blocking: bool) -> None:
        self.underlying.setblocking(blocking)

    def recv(self, size: int) -> bytes:
        return self.underlying.recv(size)

    def sendall(self, message: bytes) -> None:
        self.underlying.sendall(message)

    def send(self, message: bytes | memoryview) -> int:
        return self.underlying.send(message)

    def fileno(self) -> int:
        return self.underlying.fileno()

    def close(self) -> None:
        self.underlying.close()
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        stays_alive: bool = False,
        exitcode: int = 0,
        resists_terminate: bool = False,
        resists_kill: bool = False,
    ) -> None:
        self.pid = 123456
        self.alive = stays_alive
        self.returncode: int | None = None if stays_alive else exitcode
        self.exitcode = exitcode
        self.killed = False
        self.resists_terminate = resists_terminate
        self.resists_kill = resists_kill

    def poll(self) -> int | None:
        return None if self.alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.alive and timeout:
            time.sleep(min(timeout, 0.01))
        if self.alive:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        return self.returncode or 0

    def terminate(self) -> None:
        if not self.resists_terminate:
            self.alive = False
            self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        if not self.resists_kill:
            self.alive = False
            self.returncode = -9


class _FakeContext:
    def __init__(self, messages: list[bytes], *, process: _FakeProcess | None = None) -> None:
        receiver, sender = socket.socketpair()
        self.receiver = _FakeSocket(receiver)
        self.sender = _FakeSocket(sender)
        for message in messages:
            self.sender.sendall(struct.pack(">I", len(message)) + message)
        self.process = process or _FakeProcess(stays_alive=True)
        self.auto_exit_after_command = process is None
        self.command: list[str] | None = None
        self.options: dict[str, object] = {}
        self.command_thread: threading.Thread | None = None

    def socketpair(self) -> tuple[_FakeSocket, _FakeSocket]:
        return self.receiver, self.sender

    def launch(self, *args: object, **kwargs: object) -> _FakeProcess:
        self.command = cast("list[str]", args[0])
        self.options = kwargs

        def consume_command() -> None:
            command_socket = socket.socket(fileno=os.dup(self.sender.fileno()))
            try:
                header = bytearray()
                while len(header) < 4:
                    chunk = command_socket.recv(4 - len(header))
                    if not chunk:
                        return
                    header.extend(chunk)
                size = struct.unpack(">I", header)[0]
                remaining = size
                while remaining:
                    chunk = command_socket.recv(remaining)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                if self.auto_exit_after_command or self.process.exitcode != 0:
                    self.process.alive = False
                    self.process.returncode = self.process.exitcode
            finally:
                command_socket.close()

        self.command_thread = threading.Thread(target=consume_command, daemon=True)
        self.command_thread.start()
        return self.process


def _message(**payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _patch_context(monkeypatch, context: _FakeContext) -> None:
    monkeypatch.setattr(process_executor_module.socket, "socketpair", context.socketpair)
    monkeypatch.setattr(process_executor_module.subprocess, "Popen", context.launch)


def test_registered_spawned_handler_round_trips_only_bounded_json() -> None:
    executor = ReviewedProcessExecutor(_registry(echo=_echo))

    result = executor.run(
        "echo", {"symbol": "600000.SH", "dates": ["2025-01-01"]}, timeout_seconds=5
    )

    assert result.value == {"dates": ["2025-01-01"], "symbol": "600000.SH"}
    assert result.stdout_discarded_bytes == 0
    assert result.stderr_discarded_bytes == 0
    assert result.stdout_truncated is False
    assert result.stderr_truncated is False


def test_worker_command_uses_isolated_no_bytecode_flags(monkeypatch) -> None:
    context = _FakeContext(
        [
            _message(type="ready", isolated=False),
            _message(
                type="result",
                ok=True,
                value={"ok": True},
                stdout_bytes=0,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
        ]
    )
    _patch_context(monkeypatch, context)

    result = ReviewedProcessExecutor(_registry(echo=_echo)).run("echo", {}, timeout_seconds=1)

    assert result.value == {"ok": True}
    assert context.command is not None
    assert context.command[0] == sys.executable
    assert context.command[1:3] == ["-I", "-B"]
    assert context.command[3] == str(process_executor_module._WORKER_SCRIPT)
    assert context.command_thread is not None
    context.command_thread.join(timeout=1)
    assert not context.command_thread.is_alive()


def test_fixed_worker_helper_validates_descriptor_and_dispatches(monkeypatch) -> None:
    helper = _load_worker_helper()
    assert helper.main() == 2

    monkeypatch.setattr(helper.sys, "argv", ["process_executor_worker.py", "-1"])
    assert helper.main() == 2

    class FakeChannel:
        closed = False

        def close(self) -> None:
            self.closed = True

    channel = FakeChannel()
    monkeypatch.setattr(helper.sys, "argv", ["process_executor_worker.py", "17"])
    monkeypatch.setattr(helper.socket, "socket", lambda *, fileno: channel)
    dispatched: list[object] = []
    monkeypatch.setattr(
        process_executor_module, "_worker_bootstrap", lambda value: dispatched.append(value)
    )

    assert helper.main() == 0
    assert dispatched == [channel]


def test_fixed_worker_helper_sanitizes_socket_and_bootstrap_failures(monkeypatch) -> None:
    helper = _load_worker_helper()
    monkeypatch.setattr(helper.sys, "argv", ["process_executor_worker.py", "not-a-fd"])
    assert helper.main() == 2

    monkeypatch.setattr(helper.sys, "argv", ["process_executor_worker.py", "17"])

    def bad_socket(*, fileno: int):
        raise OSError(f"secret socket failure {fileno}")

    monkeypatch.setattr(helper.socket, "socket", bad_socket)
    assert helper.main() == 2

    class FakeChannel:
        closed = False

        def close(self) -> None:
            self.closed = True

    channel = FakeChannel()
    monkeypatch.setattr(helper.socket, "socket", lambda *, fileno: channel)

    def failed_bootstrap(_channel: object) -> None:
        raise RuntimeError("bootstrap secret")

    monkeypatch.setattr(process_executor_module, "_worker_bootstrap", failed_bootstrap)
    assert helper.main() == 3
    assert channel.closed is True


def test_handler_import_output_and_exception_text_stay_in_child(monkeypatch, capsys):
    module_name = f"opendata._executor_bootstrap_probe_{os.getpid()}"
    env_name = f"OD_EXECUTOR_BOOTSTRAP_PROBE_{os.getpid()}"
    module_path = Path(process_executor_module.__file__).resolve().parents[1] / (
        f"_executor_bootstrap_probe_{os.getpid()}.py"
    )
    module_path.write_text(
        "import os, sys\n"
        f"if os.environ.get({env_name!r}) == '1':\n"
        "    print('bootstrap-secret-do-not-leak', file=sys.stderr)\n"
        "    raise RuntimeError('bootstrap-secret-do-not-leak')\n"
        "def handle(parameters):\n"
        "    return {'value': parameters.get('value')}\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    handler = module.handle
    monkeypatch.setenv(env_name, "1")

    try:
        with pytest.raises(ProcessExecutionHandlerError, match="process_handler_failed"):
            ReviewedProcessExecutor(_registry(probe=handler)).run(
                "probe", {"value": "x"}, timeout_seconds=5
            )

        captured = capsys.readouterr()
        assert "bootstrap-secret-do-not-leak" not in captured.out + captured.err
    finally:
        sys.modules.pop(module_name, None)
        module_path.unlink(missing_ok=True)


def test_large_python_stdout_and_stderr_are_discarded_and_counted_as_truncated() -> None:
    executor = ReviewedProcessExecutor(
        _registry(printer=_print_large_output), stdout_limit_bytes=20, stderr_limit_bytes=24
    )

    result = executor.run("printer", {}, timeout_seconds=5)

    assert result.value == {"ok": True}
    assert result.stdout_discarded_bytes == 21
    assert result.stderr_discarded_bytes == 25
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_timeout_terminates_and_reaps_child_and_closes_process_resources(tmp_path: Path) -> None:
    marker = tmp_path / "worker-pid"
    executor = ReviewedProcessExecutor(
        _registry(waiter=_write_pid_and_wait), shutdown_grace_seconds=0.05
    )

    with pytest.raises(ProcessExecutionTimeoutError, match="process_execution_timeout"):
        executor.run("waiter", {"marker": str(marker)}, timeout_seconds=3)

    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(worker_pid)


def test_cancel_callback_stops_worker_and_reaps_it(tmp_path: Path) -> None:
    marker = tmp_path / "worker-pid"
    executor = ReviewedProcessExecutor(
        _registry(waiter=_write_pid_and_wait),
        checkpoint_interval_seconds=0.1,
        poll_interval_seconds=0.01,
        shutdown_grace_seconds=0.05,
    )

    with pytest.raises(ProcessExecutionCancelledError, match="process_execution_cancelled"):
        executor.run(
            "waiter",
            {"marker": str(marker)},
            timeout_seconds=5,
            cancelled=lambda: marker.exists(),
        )

    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(worker_pid)


def test_partial_ipc_frame_does_not_block_parent_deadline_or_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "partial-frame-worker-pid"
    monkeypatch.setattr(
        process_executor_module, "_WORKER_SCRIPT", _write_test_worker(tmp_path, "partial")
    )
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo), poll_interval_seconds=0.005, shutdown_grace_seconds=0.05
    )
    started = time.monotonic()

    with pytest.raises(ProcessExecutionTimeoutError, match="process_execution_timeout"):
        executor.run("echo", {"marker": str(marker)}, timeout_seconds=0.25)

    elapsed = time.monotonic() - started
    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert elapsed < 1.5
    assert _wait_until_stopped(worker_pid)


def test_deadline_and_cancel_remain_responsive_while_command_send_is_backpressured(
    tmp_path: Path, monkeypatch
) -> None:
    original_socketpair = socket.socketpair

    def constrained_socketpair() -> tuple[socket.socket, socket.socket]:
        receiver, sender = original_socketpair()
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16_384)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16_384)
        return receiver, sender

    marker = tmp_path / "command-backpressure-worker-pid"
    monkeypatch.setattr(process_executor_module.socket, "socketpair", constrained_socketpair)
    monkeypatch.setattr(
        process_executor_module,
        "_WORKER_SCRIPT",
        _write_test_worker(tmp_path, "before_command", marker),
    )
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo),
        max_parameter_bytes=800_000,
        poll_interval_seconds=0.005,
        shutdown_grace_seconds=0.05,
    )
    started = time.monotonic()

    with pytest.raises(ProcessExecutionTimeoutError, match="process_execution_timeout"):
        executor.run("echo", {"payload": "x" * 600_000}, timeout_seconds=0.25)

    assert time.monotonic() - started < 1.5
    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(worker_pid)


def test_cancellation_before_ready_remains_responsive(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "before-ready-worker-pid"
    monkeypatch.setattr(
        process_executor_module, "_WORKER_SCRIPT", _write_test_worker(tmp_path, "before_ready")
    )
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo), poll_interval_seconds=0.005, shutdown_grace_seconds=0.05
    )
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return marker.exists()

    with pytest.raises(ProcessExecutionCancelledError, match="process_execution_cancelled"):
        executor.run("echo", {"marker": str(marker)}, timeout_seconds=5, cancelled=cancelled)

    assert calls > 1
    assert _wait_until_stopped(int(marker.read_text(encoding="utf-8")))


def test_timeout_before_ready_kills_descendant_in_launch_isolated_group(
    tmp_path: Path, monkeypatch
) -> None:
    if os.name != "posix":
        pytest.skip("process groups are POSIX-specific")
    marker = tmp_path / "before-ready-descendant-pid"
    monkeypatch.setattr(
        process_executor_module,
        "_WORKER_SCRIPT",
        _write_test_worker(tmp_path, "before_ready_fork"),
    )
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo), poll_interval_seconds=0.005, shutdown_grace_seconds=0.05
    )

    with pytest.raises(ProcessExecutionTimeoutError, match="process_execution_timeout"):
        executor.run("echo", {"marker": str(marker)}, timeout_seconds=3)

    descendant_pid = int(marker.read_text(encoding="utf-8"))
    stopped = _wait_until_stopped(descendant_pid)
    if not stopped:
        with suppress(ProcessLookupError):
            os.kill(descendant_pid, 9)
    assert stopped


def test_cancellation_after_result_during_process_join_is_responsive(
    tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "after-result-worker-pid"
    monkeypatch.setattr(
        process_executor_module, "_WORKER_SCRIPT", _write_test_worker(tmp_path, "after_result")
    )
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo), poll_interval_seconds=0.005, shutdown_grace_seconds=0.05
    )
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return marker.exists()

    with pytest.raises(ProcessExecutionCancelledError, match="process_execution_cancelled"):
        executor.run("echo", {"marker": str(marker)}, timeout_seconds=5, cancelled=cancelled)

    assert calls > 1
    assert _wait_until_stopped(int(marker.read_text(encoding="utf-8")))


def test_checkpoint_exception_stops_worker_before_propagating(tmp_path: Path) -> None:
    marker = tmp_path / "worker-pid"
    executor = ReviewedProcessExecutor(
        _registry(waiter=_write_pid_and_wait),
        checkpoint_interval_seconds=0.1,
        poll_interval_seconds=0.01,
        shutdown_grace_seconds=0.05,
    )

    def fail_checkpoint() -> None:
        if marker.exists():
            raise RuntimeError("lease-lost")

    with pytest.raises(RuntimeError, match="lease-lost"):
        executor.run(
            "waiter", {"marker": str(marker)}, timeout_seconds=5, checkpoint=fail_checkpoint
        )

    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(worker_pid)


def test_worker_crash_is_reported_without_child_text_and_pipe_leak() -> None:
    executor = ReviewedProcessExecutor(_registry(crash=_crash_without_response))

    with pytest.raises(ProcessExecutionCrashedError, match="process_exited_without_result"):
        executor.run("crash", {}, timeout_seconds=5)


def test_oversized_result_fails_closed_without_partial_value() -> None:
    executor = ReviewedProcessExecutor(_registry(large=_return_large_value), max_result_bytes=1_024)

    with pytest.raises(ProcessExecutionHandlerError, match="process_result_limit_exceeded"):
        executor.run("large", {}, timeout_seconds=5)


def test_default_result_text_cap_remains_four_mib() -> None:
    executor = ReviewedProcessExecutor(_registry(large=_return_default_text_cap_plus_one))

    with pytest.raises(ProcessExecutionHandlerError, match="process_result_limit_exceeded"):
        executor.run("large", {}, timeout_seconds=10)


def test_eight_mib_result_text_cap_accepts_four_mib_raw_base64_payload() -> None:
    executor = ReviewedProcessExecutor(
        _registry(synthetic_base64=_return_synthetic_four_mib_base64_body),
        max_result_text_bytes=process_executor_module.MAX_RESULT_TEXT_BYTES,
    )

    result = executor.run("synthetic_base64", {}, timeout_seconds=20)

    assert isinstance(result.value, dict)
    body_b64 = result.value["body_b64"]
    assert isinstance(body_b64, str)
    expected = base64.b64encode(b"\x00" * (4 * 1024 * 1024)).decode("ascii")
    assert len(body_b64) == 5_592_408
    assert body_b64 == expected


def test_eight_mib_result_text_cap_rejects_cap_plus_one_without_partial_value() -> None:
    executor = ReviewedProcessExecutor(
        _registry(large=_return_result_text_cap_plus_one),
        max_result_text_bytes=process_executor_module.MAX_RESULT_TEXT_BYTES,
    )

    with pytest.raises(ProcessExecutionHandlerError, match="process_result_limit_exceeded"):
        executor.run("large", {}, timeout_seconds=20)


def test_eight_mib_text_cap_does_not_widen_result_value_byte_cap() -> None:
    executor = ReviewedProcessExecutor(
        _registry(large=_return_large_value),
        max_result_bytes=1_024,
        max_result_text_bytes=process_executor_module.MAX_RESULT_TEXT_BYTES,
    )

    with pytest.raises(ProcessExecutionHandlerError, match="process_result_limit_exceeded"):
        executor.run("large", {}, timeout_seconds=10)


def test_larger_result_text_cap_does_not_widen_input_text_limit() -> None:
    executor = ReviewedProcessExecutor(
        _registry(echo=_echo),
        max_parameter_bytes=process_executor_module.MAX_PARAMETER_BYTES,
        max_result_text_bytes=process_executor_module.MAX_RESULT_TEXT_BYTES,
    )

    with pytest.raises(ValueError, match="process_parameters_invalid"):
        executor.run(
            "echo",
            {"text": "x" * (process_executor_module.MAX_JSON_TEXT_BYTES + 1)},
            timeout_seconds=5,
        )


def test_secret_like_return_is_rejected_without_disclosing_the_value() -> None:
    executor = ReviewedProcessExecutor(_registry(secret=_return_secret_like_value))

    with pytest.raises(ProcessExecutionHandlerError, match="process_result_rejected") as raised:
        executor.run("secret", {}, timeout_seconds=5)

    assert "secret-value-should-not-cross-ipc" not in str(raised.value)


def test_handler_exception_text_and_secret_never_cross_the_protocol(capsys) -> None:
    executor = ReviewedProcessExecutor(_registry(failure=_raise_with_secret))

    with pytest.raises(ProcessExecutionHandlerError, match="process_handler_failed") as raised:
        executor.run("failure", {}, timeout_seconds=5)

    output = capsys.readouterr()
    assert "secret-value-should-not-escape" not in str(raised.value)
    assert "secret-value-should-not-escape" not in output.out + output.err


def test_posix_timeout_kills_descendants_in_the_isolated_worker_group(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("process groups are POSIX-specific")
    marker = tmp_path / "descendant-pid"
    executor = ReviewedProcessExecutor(
        _registry(descendant=_spawn_descendant_and_wait), shutdown_grace_seconds=0.05
    )

    with pytest.raises(ProcessExecutionTimeoutError, match="process_execution_timeout"):
        executor.run("descendant", {"marker": str(marker)}, timeout_seconds=3)

    descendant_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(descendant_pid)


def test_successful_worker_does_not_leave_a_background_descendant(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("process groups are POSIX-specific")
    marker = tmp_path / "descendant-pid"
    executor = ReviewedProcessExecutor(
        _registry(descendant=_spawn_descendant_then_return), shutdown_grace_seconds=0.05
    )

    result = executor.run("descendant", {"marker": str(marker)}, timeout_seconds=5)

    assert result.value == {"done": True}
    descendant_pid = int(marker.read_text(encoding="utf-8"))
    assert _wait_until_stopped(descendant_pid)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_parameter_bytes": 0}, "max_parameter_bytes"),
        ({"max_result_bytes": True}, "max_result_bytes"),
        ({"max_result_text_bytes": True}, "max_result_text_bytes"),
        ({"max_result_text_bytes": 0}, "max_result_text_bytes"),
        (
            {"max_result_text_bytes": process_executor_module.MAX_RESULT_TEXT_BYTES + 1},
            "max_result_text_bytes",
        ),
        ({"max_result_text_bytes": "large"}, "max_result_text_bytes"),
        ({"stdout_limit_bytes": 16_385}, "stdout_limit_bytes"),
        ({"stderr_limit_bytes": -1}, "stderr_limit_bytes"),
        ({"poll_interval_seconds": float("nan")}, "poll_interval_seconds"),
        ({"checkpoint_interval_seconds": 0}, "checkpoint_interval_seconds"),
        ({"checkpoint_interval_seconds": "slow"}, "checkpoint_interval_seconds"),
        ({"shutdown_grace_seconds": 10}, "shutdown_grace_seconds"),
    ],
)
def test_executor_rejects_unbounded_or_invalid_limits(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ReviewedProcessExecutor(_registry(echo=_echo), **kwargs)


def test_executor_rejects_non_registry_and_non_callable_parent_hooks() -> None:
    with pytest.raises(TypeError, match="ReviewedProcessRegistry"):
        ReviewedProcessExecutor(cast("ReviewedProcessRegistry", object()))
    executor = ReviewedProcessExecutor(_registry(echo=_echo))
    with pytest.raises(ValueError, match="timeout_seconds"):
        executor.run("echo", {}, timeout_seconds=float("inf"))
    with pytest.raises(TypeError, match="checkpoint"):
        executor.run("echo", {}, timeout_seconds=5, checkpoint=cast("object", 1))
    with pytest.raises(TypeError, match="cancelled"):
        executor.run("echo", {}, timeout_seconds=5, cancelled=cast("object", 1))


def test_executor_rejects_integer_subclass_for_result_text_limit() -> None:
    class IntegerSubclass(int):
        pass

    with pytest.raises(ValueError, match="max_result_text_bytes must be an integer"):
        ReviewedProcessExecutor(
            _registry(echo=_echo),
            max_result_text_bytes=IntegerSubclass(1024),
        )


@pytest.mark.parametrize(
    ("messages", "process", "error", "message"),
    [
        (
            [_message(type="ready", isolated=True, extra=1)],
            None,
            ProcessExecutionProtocolError,
            "shape_invalid",
        ),
        (
            [_message(type="ready", isolated="yes")],
            None,
            ProcessExecutionProtocolError,
            "shape_invalid",
        ),
        ([b"[]"], None, ProcessExecutionProtocolError, "message_shape_invalid"),
        ([b"not-json"], None, ProcessExecutionProtocolError, "message_invalid"),
        (
            [_message(type="result", ok=True, value=1)],
            None,
            ProcessExecutionProtocolError,
            "protocol_invalid",
        ),
        (
            [
                _message(
                    type="result",
                    ok=False,
                    code="worker_setup_failed",
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                )
            ],
            None,
            ProcessExecutionCrashedError,
            "worker_setup_failed",
        ),
        (
            [_message(type="ready", isolated=False), _message(type="result", ok="yes")],
            None,
            ProcessExecutionProtocolError,
            "response_invalid",
        ),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=False,
                    code="handler_failed",
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    extra=True,
                ),
            ],
            None,
            ProcessExecutionProtocolError,
            "response_invalid",
        ),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=True,
                    value=1,
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                    extra=True,
                ),
            ],
            None,
            ProcessExecutionProtocolError,
            "response_invalid",
        ),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=True,
                    value=1,
                    stdout_bytes=process_executor_module.MAX_STDIO_BYTES + 2,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ],
            None,
            ProcessExecutionProtocolError,
            "response_invalid",
        ),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=True,
                    value=1,
                    stdout_bytes=0,
                    stderr_bytes=process_executor_module.MAX_STDIO_BYTES + 2,
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ],
            None,
            ProcessExecutionProtocolError,
            "response_invalid",
        ),
        ([], None, ProcessExecutionCrashedError, "process_exited_without_result"),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=True,
                    value=1,
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ],
            _FakeProcess(stays_alive=True),
            ProcessExecutionTimeoutError,
            "process_execution_timeout",
        ),
        (
            [
                _message(type="ready", isolated=False),
                _message(
                    type="result",
                    ok=True,
                    value=1,
                    stdout_bytes=0,
                    stderr_bytes=0,
                    stdout_truncated=False,
                    stderr_truncated=False,
                ),
            ],
            _FakeProcess(stays_alive=True, exitcode=4),
            ProcessExecutionCrashedError,
            "process_exit_status_invalid",
        ),
    ],
)
def test_parent_rejects_invalid_worker_protocol_and_cleans_handles(
    monkeypatch,
    messages: list[bytes],
    process: _FakeProcess | None,
    error: type[Exception],
    message: str,
) -> None:
    context = _FakeContext(messages, process=process)
    _patch_context(monkeypatch, context)

    with pytest.raises(error, match=message):
        ReviewedProcessExecutor(_registry(echo=_echo)).run("echo", {}, timeout_seconds=1)

    assert context.receiver.closed is True
    assert context.sender.closed is True
    assert context.process.poll() is not None
    assert context.command_thread is not None
    context.command_thread.join(timeout=1)
    assert not context.command_thread.is_alive()


def test_parent_rejects_extra_ipc_messages_and_escalates_worker_cleanup(monkeypatch) -> None:
    result = _message(
        type="result",
        ok=True,
        value=1,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    context = _FakeContext(
        [_message(type="ready", isolated=False), result, _message(type="unexpected")]
    )
    _patch_context(monkeypatch, context)

    with pytest.raises(ProcessExecutionProtocolError, match="extra_message"):
        ReviewedProcessExecutor(_registry(echo=_echo)).run("echo", {}, timeout_seconds=1)
    assert context.command_thread is not None
    context.command_thread.join(timeout=1)

    stuck = _FakeProcess(stays_alive=True, resists_terminate=True)
    process_executor_module._stop_process(stuck, isolated_group=False, grace_seconds=0.01)
    assert stuck.killed is True
    assert stuck.alive is False


def test_socket_closes_even_when_a_child_cannot_be_reaped(monkeypatch) -> None:
    stuck = _FakeProcess(stays_alive=True, resists_terminate=True, resists_kill=True)
    context = _FakeContext([], process=stuck)
    _patch_context(monkeypatch, context)
    stuck.kill = lambda: None

    with pytest.raises(ProcessExecutionCleanupError, match="process_cleanup_failed"):
        ReviewedProcessExecutor(
            _registry(echo=_echo), shutdown_grace_seconds=0.01, poll_interval_seconds=0.005
        ).run("echo", {}, timeout_seconds=0.01)

    assert context.receiver.closed is True
    assert context.sender.closed is True
    assert stuck.poll() is None
    assert context.command_thread is not None
    context.command_thread.join(timeout=1)


def test_socket_reader_rejects_invalid_and_truncated_frames(monkeypatch) -> None:
    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    reader = process_executor_module._JsonSocketReader(
        receiver, process_executor_module.MAX_RESULT_BYTES
    )
    sender.sendall(
        struct.pack(
            ">I",
            process_executor_module.MAX_RESULT_BYTES
            + process_executor_module._MAX_IPC_OVERHEAD
            + 1,
        )
    )
    with pytest.raises(ProcessExecutionProtocolError, match="size_invalid"):
        reader.receive()
    receiver.close()
    sender.close()

    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    reader = process_executor_module._JsonSocketReader(
        receiver, process_executor_module.MAX_RESULT_BYTES
    )
    encoded = b'{"type":"ready","authorization":"hidden"}'
    sender.sendall(struct.pack(">I", len(encoded)) + encoded)
    with pytest.raises(ProcessExecutionProtocolError, match="invalid"):
        reader.receive()
    receiver.close()
    sender.close()

    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    reader = process_executor_module._JsonSocketReader(
        receiver, process_executor_module.MAX_RESULT_BYTES
    )
    sender.sendall(b"\x00\x00")
    sender.close()
    with pytest.raises(ProcessExecutionProtocolError, match="truncated"):
        reader.receive()
    receiver.close()

    receiver, sender = socket.socketpair()
    receiver.setblocking(False)
    reader = process_executor_module._JsonSocketReader(receiver, 1)
    sender.sendall(struct.pack(">I", process_executor_module._MAX_IPC_OVERHEAD + 2))
    with pytest.raises(ProcessExecutionProtocolError, match="size_invalid"):
        reader.receive()
    receiver.close()
    sender.close()

    stream = process_executor_module._DiscardingTextStream(2)
    assert stream.write("你") == 1
    assert stream.truncated is True
    assert stream.seen_bytes == 3
    with pytest.raises(TypeError, match="requires str"):
        stream.write(cast("str", b"bytes"))
    monkeypatch.setattr(process_executor_module.os, "name", "nt")
    assert process_executor_module._start_process_group() is False


def test_registry_rejects_invalid_mapping_entries_and_duplicate_ids() -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        ReviewedProcessRegistry(cast("dict[str, object]", []))
    with pytest.raises(ValueError, match="invalid format"):
        ReviewedProcessRegistry(cast("dict[str, object]", {1: _echo}))

    class DuplicateItems(dict):
        def items(self):
            return [("echo", _echo), ("echo", _echo)]

    with pytest.raises(ValueError, match="duplicate operation ID"):
        ReviewedProcessRegistry(DuplicateItems())

    class CallableObject:
        def __call__(self, _parameters):
            return {}

    with pytest.raises(TypeError, match="top-level"):
        ReviewedProcessRegistry({"callable": CallableObject()})


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (float("nan"), "nonfinite"),
        (1 << 300, "integer_limit"),
        ("\ud800", "text_invalid"),
        ("x" * (4 * 1024 * 1024 + 1), "text_limit"),
        ({1: "invalid-key"}, "object_key"),
        ({"private_key": "value"}, "secret_like"),
        ({"message": "AKIA1234567890ABCDEF"}, "secret_like"),
        ({"url": "postgresql://alice:secret@localhost/db"}, "secret_like"),
        ({"nested": object()}, "type_invalid"),
        (set(), "type_invalid"),
    ],
)
def test_json_boundary_rejects_non_json_and_sensitive_values(value: object, code: str) -> None:
    with pytest.raises(ValueError, match=code):
        process_executor_module._validate_json_value(value)


def test_json_boundary_detects_cycles_depth_nodes_and_duplicate_wire_keys() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    deep: object = None
    for _ in range(process_executor_module.MAX_JSON_DEPTH + 2):
        deep = [deep]

    with pytest.raises(ValueError, match="cycle"):
        process_executor_module._validate_json_value(cyclic)
    with pytest.raises(ValueError, match="depth"):
        process_executor_module._validate_json_value(deep)
    with pytest.raises(ValueError, match="node_limit"):
        process_executor_module._validate_json_value(
            [None] * process_executor_module.MAX_JSON_NODES
        )
    with pytest.raises(ValueError, match="duplicate"):
        process_executor_module._decode_json_value(b'{"x":1,"x":2}')
    with pytest.raises(ValueError, match="nonstandard"):
        process_executor_module._decode_json_value(b"NaN")
    with pytest.raises(ValueError, match="document"):
        process_executor_module._decode_json_value(b"\xff")
    with pytest.raises(ValueError, match="parameters_shape"):
        process_executor_module._decode_json_object(b"[]")


def test_json_boundaries_cover_shape_limits_and_safe_error_codes(monkeypatch) -> None:
    process_executor_module._validate_json_value(
        {"none": None, "truth": True, "number": 4, "fraction": 1.25, "text": "ok"}
    )
    wide_list = [[None, None] for _ in range(process_executor_module.MAX_JSON_NODES // 2)]
    with pytest.raises(ValueError, match="node_limit"):
        process_executor_module._validate_json_value(wide_list)
    wide_map = {f"field-{index}": None for index in range(process_executor_module.MAX_JSON_NODES)}
    with pytest.raises(ValueError, match="node_limit"):
        process_executor_module._validate_json_value(wide_map)
    with pytest.raises(ValueError, match="text_limit"):
        process_executor_module._validate_json_value(
            {"x" * (process_executor_module.MAX_JSON_TEXT_BYTES + 1): None}
        )

    error_type = process_executor_module._ProtocolValueError
    assert process_executor_module._safe_protocol_error(error_type("other")) == "handler_failed"
    assert (
        process_executor_module._safe_protocol_error(error_type("result_limit_exceeded"))
        == "result_limit_exceeded"
    )
    assert (
        process_executor_module._safe_protocol_error(
            error_type("result_contains_secret_like_value")
        )
        == "result_contains_secret_like_value"
    )

    def broken_dumps(*_args, **_kwargs):
        raise TypeError("sensitive encoder detail")

    monkeypatch.setattr(process_executor_module.json, "dumps", broken_dumps)
    with pytest.raises(error_type, match="json_encoding_invalid"):
        process_executor_module._encode_json_value({"value": 1}, 100)


def test_output_helpers_bound_counts_and_cleanup_platform_branches(monkeypatch) -> None:
    stream = process_executor_module._DiscardingTextStream(2)
    assert stream.write("ab") == 2
    assert stream.truncated is False
    assert stream.write("c") == 1
    assert stream.truncated is True
    assert stream.seen_bytes == 3
    assert stream.write("d") == 1
    assert stream.seen_bytes == 3
    assert stream.isatty() is False
    with pytest.raises(TypeError, match="requires str"):
        stream.write(cast("str", b"bytes"))

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_executor_module.os, "open", lambda *_args: 1)
    monkeypatch.setattr(
        process_executor_module.os,
        "dup2",
        lambda source, target: calls.append((source, target)),
    )
    monkeypatch.setattr(process_executor_module.os, "close", lambda _fd: pytest.fail("closed fd"))
    process_executor_module._redirect_child_output()
    assert calls == [(1, 1), (1, 2)]

    def missing_group(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(process_executor_module.os, "killpg", missing_group)
    assert process_executor_module._process_group_exists(1) is False
    process_executor_module._signal_process_group(1, 15)

    def inaccessible_group(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(process_executor_module.os, "killpg", inaccessible_group)
    assert process_executor_module._process_group_exists(1) is True


def test_process_helpers_cover_setup_and_bounded_message_failures(monkeypatch) -> None:
    idle = _FakeProcess()
    process_executor_module._stop_process(idle, isolated_group=False, grace_seconds=0.01)

    receiver, sender = socket.socketpair()
    try:
        with pytest.raises(
            process_executor_module._ProtocolValueError, match="result_limit_exceeded"
        ):
            process_executor_module._send_message(
                sender, {"type": "result", "value": "x" * 3_000}, 2
            )
    finally:
        receiver.close()
        sender.close()

    monkeypatch.setattr(
        process_executor_module.os,
        "open",
        lambda *_args: 3,
    )
    closed: list[int] = []
    monkeypatch.setattr(process_executor_module.os, "dup2", lambda *_args: None)
    monkeypatch.setattr(process_executor_module.os, "close", closed.append)
    process_executor_module._redirect_child_output()
    assert closed == [3]


def test_process_group_cleanup_kills_a_descendant_after_success(monkeypatch) -> None:
    signals: list[int] = []
    exists = iter((True, True, True))
    clock = [0.0]
    monkeypatch.setattr(
        process_executor_module,
        "_process_group_exists",
        lambda _pid: next(exists),
    )
    monkeypatch.setattr(
        process_executor_module,
        "_signal_process_group",
        lambda _pid, signal_number: signals.append(signal_number),
    )
    monkeypatch.setattr(process_executor_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        process_executor_module.time,
        "sleep",
        lambda duration: clock.__setitem__(0, clock[0] + duration),
    )

    process_executor_module._stop_group_descendants(123, 0.01)

    assert signals == [
        process_executor_module.signal.SIGTERM,
        process_executor_module.signal.SIGKILL,
    ]


def test_response_validators_reject_out_of_range_values() -> None:
    with pytest.raises(ProcessExecutionProtocolError, match="response_invalid"):
        process_executor_module._response_counter({"count": True}, "count")
    with pytest.raises(ProcessExecutionProtocolError, match="response_invalid"):
        process_executor_module._response_counter({"count": -1}, "count")
    with pytest.raises(ProcessExecutionProtocolError, match="response_invalid"):
        process_executor_module._response_bool({"valid": 1}, "valid")


def test_registry_captures_immutable_handler_reference(monkeypatch) -> None:
    executor = ReviewedProcessExecutor(_registry(echo=_echo))
    monkeypatch.setattr(_echo, "__module__", "missing_reviewed_module")

    assert executor.run("echo", {}, timeout_seconds=5).value == {}

    with pytest.raises(TypeError, match="exported"):
        ReviewedProcessRegistry({"echo": _echo})


def test_registry_and_parameters_reject_unreviewed_or_non_json_inputs() -> None:
    with pytest.raises(TypeError, match="top-level"):
        ReviewedProcessRegistry({"closure": lambda _value: None})
    with pytest.raises(ValueError, match="invalid format"):
        ReviewedProcessRegistry({"arbitrary/import.path": _echo})
    with pytest.raises(ValueError, match="not registered"):
        ReviewedProcessExecutor(_registry(echo=_echo)).run("unknown", {}, timeout_seconds=1)
    with pytest.raises(TypeError, match="plain dict"):
        ReviewedProcessExecutor(_registry(echo=_echo)).run(
            "echo", cast("dict[str, object]", ("not", "a", "dict")), timeout_seconds=1
        )
    with pytest.raises(ValueError, match="secret"):
        ReviewedProcessExecutor(_registry(echo=_echo)).run(
            "echo", {"api_key": "must-not-enter-child"}, timeout_seconds=1
        )
    with pytest.raises(ValueError, match="size_limit"):
        ReviewedProcessExecutor(_registry(echo=_echo), max_parameter_bytes=8).run(
            "echo", {"payload": "too-large"}, timeout_seconds=1
        )
