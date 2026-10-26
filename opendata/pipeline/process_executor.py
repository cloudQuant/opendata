"""Run reviewed JSON-only provider work in bounded, killable child processes.

The executor accepts only explicitly registered top-level callables and plain
JSON values. It does not pass warehouse connections, run contexts, publishers,
or arbitrary import paths to a child. This is process lifecycle control, not a
Python security sandbox: reviewed code still runs with the inherited
environment and operating-system permissions.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess  # nosec B404
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import TypeAlias, cast

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
ReviewedHandler: TypeAlias = Callable[[Mapping[str, JsonValue]], JsonValue]

MAX_PARAMETER_BYTES = 4 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 250_000
MAX_JSON_TEXT_BYTES = 4 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 3_600.0
MAX_STDIO_BYTES = 16 * 1024
_MAX_IPC_OVERHEAD = 2_048
_MAX_COMMAND_OVERHEAD = 8_192
_WORKER_SCRIPT = Path(__file__).with_name("process_executor_worker.py")
_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"^(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})$"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$"),
    re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@"),
)


class ProcessExecutionError(RuntimeError):
    """Base class for sanitized process execution failures."""


class ProcessExecutionTimeoutError(ProcessExecutionError):
    """Raised when a child exceeds its wall-clock deadline."""


class ProcessExecutionCancelledError(ProcessExecutionError):
    """Raised when the parent cancellation callback requests termination."""


class ProcessExecutionHandlerError(ProcessExecutionError):
    """Raised when a reviewed handler fails or returns invalid JSON data."""


class ProcessExecutionCrashedError(ProcessExecutionError):
    """Raised when a child exits without a valid protocol response."""


class ProcessExecutionProtocolError(ProcessExecutionError):
    """Raised when a child sends an oversized or malformed protocol message."""


class ProcessExecutionCleanupError(ProcessExecutionError):
    """Raised when a child process cannot be reaped within the cleanup bound."""


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    """A bounded JSON result and metadata for Python-level discarded output."""

    value: JsonValue
    stdout_discarded_bytes: int
    stderr_discarded_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True, slots=True)
class _ReviewedHandlerReference:
    """Import reference captured from a registered top-level function."""

    module: str
    qualified_name: str


class ReviewedProcessRegistry:
    """Map stable operation identifiers to reviewed top-level Python functions."""

    def __init__(self, operations: Mapping[str, ReviewedHandler]) -> None:
        """Copy and validate an explicit registry without accepting import paths.

        Args:
            operations: Mapping from a stable operation ID to a module-level
                function whose sole input is a JSON object.

        Raises:
            TypeError: If an entry is not a plain top-level Python function.
            ValueError: If an operation ID is invalid or duplicated.
        """
        if not isinstance(operations, Mapping):
            raise TypeError("operations must be a mapping of reviewed callables")
        indexed: dict[str, _ReviewedHandlerReference] = {}
        for operation_id, handler in operations.items():
            if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
                raise ValueError("operation ID has an invalid format")
            if operation_id in indexed:
                raise ValueError("duplicate operation ID")
            if not callable(handler) or not _is_importable_top_level_function(handler):
                raise TypeError("handler must be an importable top-level Python function")
            module = handler.__module__
            qualified_name = handler.__qualname__
            if not _module_exports_handler(module, qualified_name, handler):
                raise TypeError("handler must be exported by its defining module")
            indexed[operation_id] = _ReviewedHandlerReference(module, qualified_name)
        self._operations: Mapping[str, _ReviewedHandlerReference] = MappingProxyType(indexed)

    def resolve(self, operation_id: str) -> _ReviewedHandlerReference | None:
        """Return a registry-captured import reference for one operation ID."""
        return self._operations.get(operation_id)


class ReviewedProcessExecutor:
    """Execute registered callables with spawn, JSON IPC, and bounded cleanup."""

    def __init__(
        self,
        registry: ReviewedProcessRegistry,
        *,
        max_parameter_bytes: int = 1_048_576,
        max_result_bytes: int = MAX_RESULT_BYTES,
        stdout_limit_bytes: int = MAX_STDIO_BYTES,
        stderr_limit_bytes: int = MAX_STDIO_BYTES,
        poll_interval_seconds: float = 0.05,
        checkpoint_interval_seconds: float = 1.0,
        shutdown_grace_seconds: float = 0.25,
    ) -> None:
        """Bind immutable reviewed operations and explicit resource ceilings.

        The stdout/stderr limits count bytes passed through the child's Python
        text streams. Native writes to file descriptors 1 and 2 are discarded
        at ``/dev/null`` and are not included in those counters.
        """
        if not isinstance(registry, ReviewedProcessRegistry):
            raise TypeError("registry must be a ReviewedProcessRegistry")
        self._max_parameter_bytes = _bounded_integer(
            max_parameter_bytes, "max_parameter_bytes", 1, MAX_PARAMETER_BYTES
        )
        self._max_result_bytes = _bounded_integer(
            max_result_bytes, "max_result_bytes", 1, MAX_RESULT_BYTES
        )
        self._stdout_limit_bytes = _bounded_integer(
            stdout_limit_bytes, "stdout_limit_bytes", 0, MAX_STDIO_BYTES
        )
        self._stderr_limit_bytes = _bounded_integer(
            stderr_limit_bytes, "stderr_limit_bytes", 0, MAX_STDIO_BYTES
        )
        self._poll_interval = _bounded_number(
            poll_interval_seconds, "poll_interval_seconds", 0.005, 1.0
        )
        self._checkpoint_interval = _bounded_number(
            checkpoint_interval_seconds, "checkpoint_interval_seconds", 0.01, 60.0
        )
        self._shutdown_grace = _bounded_number(
            shutdown_grace_seconds, "shutdown_grace_seconds", 0.01, 5.0
        )
        self._registry = registry

    def run(
        self,
        operation_id: str,
        parameters: Mapping[str, JsonValue],
        *,
        timeout_seconds: float,
        checkpoint: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ProcessExecutionResult:
        """Run one registered operation and return only validated JSON data.

        ``checkpoint`` executes in the parent while the child is active. A
        raised exception propagates after the child and its process group have
        been stopped and reaped. ``cancelled`` is polled by the parent and must
        return ``True`` to request cancellation.

        Args:
            operation_id: Exact ID registered by the application.
            parameters: Plain JSON object with bounded size and nesting.
            timeout_seconds: Total startup and execution wall-clock budget.
            checkpoint: Optional parent-side lease heartbeat callback.
            cancelled: Optional parent-side cancellation predicate.

        Raises:
            ProcessExecutionError: For timeout, cancellation, protocol, child,
                or cleanup failures. Messages use fixed non-sensitive codes.
            TypeError: If callbacks or parameters have unsupported types.
            ValueError: If the operation, deadline, or parameter bounds are invalid.
        """
        handler_reference = self._registry.resolve(operation_id)
        if handler_reference is None:
            raise ValueError("operation ID is not registered")
        timeout = _bounded_number(timeout_seconds, "timeout_seconds", 0.01, MAX_TIMEOUT_SECONDS)
        if checkpoint is not None and not callable(checkpoint):
            raise TypeError("checkpoint must be callable")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable")
        if os.name != "posix":
            raise ProcessExecutionError("process_executor_platform_unsupported")
        parameters_json = _encode_json_object(parameters, self._max_parameter_bytes)

        receiver, sender = socket.socketpair()
        receiver.setblocking(False)
        process: subprocess.Popen[bytes] | None = None
        started_at = time.monotonic()
        deadline = started_at + timeout
        next_checkpoint = started_at + self._checkpoint_interval
        ready = False
        isolated_group = False
        response: Mapping[str, JsonValue] | None = None
        message_reader = _JsonSocketReader(receiver, self._max_result_bytes)
        try:
            try:
                # The command is a fixed adjacent helper; arguments contain only the inherited FD.
                process = subprocess.Popen(  # noqa: S603  # nosec B603
                    [sys.executable, "-I", str(_WORKER_SCRIPT), str(sender.fileno())],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(sender.fileno(),),
                    start_new_session=True,
                )
            except Exception:
                raise ProcessExecutionCrashedError("process_start_failed") from None
            command_frame = _encode_message_frame(
                {
                    "type": "command",
                    "handler_module": handler_reference.module,
                    "handler_qualified_name": handler_reference.qualified_name,
                    "parameters": _decode_json_object(parameters_json),
                    "max_result_bytes": self._max_result_bytes,
                    "stdout_limit_bytes": self._stdout_limit_bytes,
                    "stderr_limit_bytes": self._stderr_limit_bytes,
                },
                self._max_parameter_bytes + _MAX_COMMAND_OVERHEAD,
            )
            next_checkpoint = _send_parent_command(
                receiver,
                command_frame,
                process=process,
                deadline=deadline,
                next_checkpoint=next_checkpoint,
                poll_interval=self._poll_interval,
                checkpoint_interval=self._checkpoint_interval,
                checkpoint=checkpoint,
                cancelled=cancelled,
            )
            sender.close()
            while response is None:
                now = time.monotonic()
                if now >= deadline:
                    raise ProcessExecutionTimeoutError("process_execution_timeout")
                if cancelled is not None and cancelled():
                    raise ProcessExecutionCancelledError("process_execution_cancelled")
                if checkpoint is not None and now >= next_checkpoint:
                    checkpoint()
                    next_checkpoint = now + self._checkpoint_interval

                wait_seconds = min(self._poll_interval, deadline - now)
                message = message_reader.receive()
                if message is not None:
                    message_type = message.get("type")
                    if message_type == "ready" and not ready:
                        if set(message) != {"type", "isolated"}:
                            raise ProcessExecutionProtocolError("process_message_shape_invalid")
                        isolated_value = message.get("isolated")
                        if type(isolated_value) is not bool:
                            raise ProcessExecutionProtocolError("process_message_shape_invalid")
                        ready = True
                        isolated_group = isolated_value
                        continue
                    if message_type == "result" and ready:
                        response = message
                        continue
                    if (
                        message_type == "result"
                        and not ready
                        and message.get("code") == "worker_setup_failed"
                    ):
                        response = message
                        continue
                    raise ProcessExecutionProtocolError("process_protocol_invalid")
                if process.poll() is not None:
                    raise ProcessExecutionCrashedError("process_exited_without_result")
                if message_reader.eof:
                    raise ProcessExecutionCrashedError("process_socket_closed")
                time.sleep(wait_seconds)

            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    raise ProcessExecutionTimeoutError("process_execution_timeout")
                if cancelled is not None and cancelled():
                    raise ProcessExecutionCancelledError("process_execution_cancelled")
                if checkpoint is not None and now >= next_checkpoint:
                    checkpoint()
                    next_checkpoint = now + self._checkpoint_interval
                time.sleep(min(self._poll_interval, deadline - now))
            if process.returncode != 0:
                raise ProcessExecutionCrashedError("process_exit_status_invalid")
            if isolated_group:
                _stop_group_descendants(process.pid, self._shutdown_grace)
                isolated_group = False
            extra_message = message_reader.receive()
            if extra_message is not None or message_reader.has_partial_message:
                raise ProcessExecutionProtocolError("process_sent_extra_message")
            if type(response.get("ok")) is not bool:
                raise ProcessExecutionProtocolError("process_response_invalid")
            if not response["ok"]:
                code = response.get("code")
                if set(response) != {
                    "type",
                    "ok",
                    "code",
                    "stdout_bytes",
                    "stderr_bytes",
                    "stdout_truncated",
                    "stderr_truncated",
                }:
                    raise ProcessExecutionProtocolError("process_response_invalid")
                if code == "result_limit_exceeded":
                    raise ProcessExecutionHandlerError("process_result_limit_exceeded")
                if code == "result_contains_secret_like_value":
                    raise ProcessExecutionHandlerError("process_result_rejected")
                if code == "handler_failed":
                    raise ProcessExecutionHandlerError("process_handler_failed")
                if code == "worker_setup_failed":
                    raise ProcessExecutionCrashedError("process_worker_setup_failed")
                raise ProcessExecutionProtocolError("process_response_invalid")

            if set(response) != {
                "type",
                "ok",
                "value",
                "stdout_bytes",
                "stderr_bytes",
                "stdout_truncated",
                "stderr_truncated",
            }:
                raise ProcessExecutionProtocolError("process_response_invalid")
            value_json = _encode_json_value(response.get("value"), self._max_result_bytes)
            value = _decode_json_value(value_json)
            stdout_count = _response_counter(response, "stdout_bytes")
            stderr_count = _response_counter(response, "stderr_bytes")
            if stdout_count > self._stdout_limit_bytes + 1:
                raise ProcessExecutionProtocolError("process_response_invalid")
            if stderr_count > self._stderr_limit_bytes + 1:
                raise ProcessExecutionProtocolError("process_response_invalid")
            return ProcessExecutionResult(
                value=value,
                stdout_discarded_bytes=stdout_count,
                stderr_discarded_bytes=stderr_count,
                stdout_truncated=_response_bool(response, "stdout_truncated"),
                stderr_truncated=_response_bool(response, "stderr_truncated"),
            )
        finally:
            with suppress(OSError):
                sender.close()
            try:
                if process is not None:
                    if process.poll() is None:
                        _stop_process(
                            process,
                            isolated_group=isolated_group,
                            grace_seconds=self._shutdown_grace,
                        )
                    else:
                        process.wait()
                        if isolated_group:
                            _stop_group_descendants(process.pid, self._shutdown_grace)
            finally:
                receiver.close()


class _DiscardingTextStream:
    """Discard Python text writes while counting a bounded UTF-8 prefix."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._seen_bytes = 0
        self._truncated = False

    @property
    def seen_bytes(self) -> int:
        """Return the observed UTF-8 byte count, capped at limit plus one."""
        return self._seen_bytes

    @property
    def truncated(self) -> bool:
        """Return whether a Python text write exceeded the configured limit."""
        return self._truncated

    def write(self, value: str) -> int:
        """Discard one write and count at most the configured prefix plus one."""
        if not isinstance(value, str):
            raise TypeError("text stream write requires str")
        remaining = self._max_bytes + 1 - self._seen_bytes
        if remaining > 0:
            byte_count, exceeded = _utf8_prefix_size(value, remaining)
            self._seen_bytes += byte_count
            self._truncated = self._truncated or exceeded or self._seen_bytes > self._max_bytes
        elif value:
            self._truncated = True
        return len(value)

    def flush(self) -> None:
        """Accept flush calls because writes are discarded immediately."""

    def isatty(self) -> bool:
        """Report that the discard stream is not an interactive terminal."""
        return False


class _JsonSocketReader:
    """Read one bounded length-prefixed JSON frame without blocking the parent."""

    def __init__(self, receiver: socket.socket, max_message_bytes: int) -> None:
        self._receiver = receiver
        self._max_message_bytes = max_message_bytes + _MAX_IPC_OVERHEAD
        self._buffer = bytearray()
        self._expected_bytes: int | None = None
        self.eof = False

    @property
    def has_partial_message(self) -> bool:
        """Report whether a frame header or body is incomplete."""
        return self._expected_bytes is not None or bool(self._buffer)

    def receive(self) -> dict[str, JsonValue] | None:
        """Consume available socket bytes and return one complete JSON frame."""
        while True:
            if self._expected_bytes is not None and len(self._buffer) == self._expected_bytes:
                encoded = bytes(self._buffer)
                self._buffer.clear()
                self._expected_bytes = None
                return _decode_message(encoded)

            if self._expected_bytes is None and len(self._buffer) == 4:
                self._expected_bytes = struct.unpack(">I", self._buffer)[0]
                self._buffer.clear()
                if self._expected_bytes > self._max_message_bytes:
                    raise ProcessExecutionProtocolError("process_message_size_invalid")
                continue

            if self.eof:
                if self.has_partial_message:
                    raise ProcessExecutionProtocolError("process_message_truncated")
                return None

            remaining = (
                4 - len(self._buffer)
                if self._expected_bytes is None
                else self._expected_bytes - len(self._buffer)
            )
            try:
                chunk = self._receiver.recv(min(remaining, 65_536))
            except BlockingIOError:
                return None
            except OSError as exc:
                raise ProcessExecutionProtocolError("process_message_read_failed") from exc
            if not chunk:
                self.eof = True
                if self.has_partial_message:
                    raise ProcessExecutionProtocolError("process_message_truncated")
                return None
            self._buffer.extend(chunk)


def _child_entry(
    handler_module: str,
    handler_qualified_name: str,
    parameters_json: bytes,
    sender: socket.socket,
    max_result_bytes: int,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> None:
    """Resolve and execute one registry-captured callable after output redirection."""
    stdout_stream = _DiscardingTextStream(stdout_limit_bytes)
    stderr_stream = _DiscardingTextStream(stderr_limit_bytes)
    try:
        _redirect_child_output()
        sys.stdout = stdout_stream
        sys.stderr = stderr_stream
        isolated = _start_process_group()
        parameters = _decode_json_object(parameters_json)
        _send_message(sender, {"type": "ready", "isolated": isolated}, max_result_bytes)
        try:
            handler = _resolve_handler(handler_module, handler_qualified_name)
            value = handler(parameters)
            _validate_json_value(value)
            result_json = _encode_json_value(value, max_result_bytes)
            result_value = _decode_json_value(result_json)
            message = {
                "type": "result",
                "ok": True,
                "value": result_value,
                "stdout_bytes": stdout_stream.seen_bytes,
                "stderr_bytes": stderr_stream.seen_bytes,
                "stdout_truncated": stdout_stream.truncated,
                "stderr_truncated": stderr_stream.truncated,
            }
            _send_message(sender, message, max_result_bytes)
        except _ProtocolValueError as exc:
            _send_failure(
                sender, _safe_protocol_error(exc), max_result_bytes, stdout_stream, stderr_stream
            )
        except BaseException:
            _send_failure(sender, "handler_failed", max_result_bytes, stdout_stream, stderr_stream)
    except BaseException:
        with suppress(BaseException):
            _send_failure(sender, "worker_setup_failed", max_result_bytes)
    finally:
        with suppress(OSError):
            sender.close()


def _send_failure(
    sender: socket.socket,
    code: str,
    max_result_bytes: int,
    stdout_stream: _DiscardingTextStream | None = None,
    stderr_stream: _DiscardingTextStream | None = None,
) -> None:
    """Send only a fixed error code, never exception text or output contents."""
    _send_message(
        sender,
        {
            "type": "result",
            "ok": False,
            "code": code,
            "stdout_bytes": stdout_stream.seen_bytes if stdout_stream else 0,
            "stderr_bytes": stderr_stream.seen_bytes if stderr_stream else 0,
            "stdout_truncated": stdout_stream.truncated if stdout_stream else False,
            "stderr_truncated": stderr_stream.truncated if stderr_stream else False,
        },
        max_result_bytes,
    )


def _send_message(
    sender: socket.socket,
    message: dict[str, object],
    max_result_bytes: int,
) -> None:
    """Send one size-checked JSON message over the one-way pipe."""
    sender.sendall(_encode_message_frame(message, max_result_bytes))


def _encode_message_frame(message: dict[str, object], max_message_bytes: int) -> bytes:
    """Encode a bounded JSON frame without writing it to a socket."""
    encoded = json.dumps(
        message, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    if len(encoded) > max_message_bytes + _MAX_IPC_OVERHEAD:
        raise _ProtocolValueError("result_limit_exceeded")
    return struct.pack(">I", len(encoded)) + encoded


def _send_parent_command(
    channel: socket.socket,
    frame: bytes,
    *,
    process: subprocess.Popen[bytes],
    deadline: float,
    next_checkpoint: float,
    poll_interval: float,
    checkpoint_interval: float,
    checkpoint: Callable[[], None] | None,
    cancelled: Callable[[], bool] | None,
) -> float:
    """Incrementally send a command while preserving the parent-side deadline."""
    view = memoryview(frame)
    offset = 0
    while offset < len(view):
        now = time.monotonic()
        if now >= deadline:
            raise ProcessExecutionTimeoutError("process_execution_timeout")
        if cancelled is not None and cancelled():
            raise ProcessExecutionCancelledError("process_execution_cancelled")
        if checkpoint is not None and now >= next_checkpoint:
            checkpoint()
            next_checkpoint = now + checkpoint_interval
        if process.poll() is not None:
            raise ProcessExecutionCrashedError("process_exited_before_command")
        try:
            sent = channel.send(view[offset:])
        except BlockingIOError:
            sent = 0
        except OSError:
            raise ProcessExecutionCrashedError("process_command_send_failed") from None
        if sent > 0:
            offset += sent
            continue
        time.sleep(min(poll_interval, max(0.0, deadline - now)))
    return next_checkpoint


def _decode_message(encoded: bytes) -> dict[str, JsonValue]:
    """Decode one bounded UTF-8 JSON protocol message."""
    try:
        parsed = _decode_json_value(encoded)
    except _ProtocolValueError as exc:
        raise ProcessExecutionProtocolError("process_message_invalid") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
        raise ProcessExecutionProtocolError("process_message_shape_invalid")
    return parsed


def _worker_bootstrap(channel: socket.socket) -> None:
    """Read one bounded command frame and invoke the child worker entry point."""
    max_result_bytes = MAX_RESULT_BYTES
    try:
        header = _read_exact(channel, 4)
        command_size = struct.unpack(">I", header)[0]
        if command_size > MAX_PARAMETER_BYTES + _MAX_COMMAND_OVERHEAD + _MAX_IPC_OVERHEAD:
            raise ProcessExecutionProtocolError("process_command_size_invalid")
        command = _decode_message(_read_exact(channel, command_size))
        expected_fields = {
            "type",
            "handler_module",
            "handler_qualified_name",
            "parameters",
            "max_result_bytes",
            "stdout_limit_bytes",
            "stderr_limit_bytes",
        }
        if set(command) != expected_fields or command.get("type") != "command":
            raise ProcessExecutionProtocolError("process_command_invalid")
        module_name = command.get("handler_module")
        qualified_name = command.get("handler_qualified_name")
        parameters = command.get("parameters")
        if not isinstance(module_name, str) or not isinstance(qualified_name, str):
            raise ProcessExecutionProtocolError("process_command_invalid")
        if type(parameters) is not dict:
            raise ProcessExecutionProtocolError("process_command_invalid")
        parameters_json = _encode_json_object(parameters, MAX_PARAMETER_BYTES)
        max_result_bytes = _bounded_integer(
            command.get("max_result_bytes"), "max_result_bytes", 1, MAX_RESULT_BYTES
        )
        stdout_limit = _bounded_integer(
            command.get("stdout_limit_bytes"), "stdout_limit_bytes", 0, MAX_STDIO_BYTES
        )
        stderr_limit = _bounded_integer(
            command.get("stderr_limit_bytes"), "stderr_limit_bytes", 0, MAX_STDIO_BYTES
        )
        _child_entry(
            module_name,
            qualified_name,
            parameters_json,
            channel,
            max_result_bytes,
            stdout_limit,
            stderr_limit,
        )
    except BaseException:
        with suppress(BaseException):
            _send_failure(channel, "worker_setup_failed", max_result_bytes)
    finally:
        with suppress(OSError):
            channel.close()


def _read_exact(channel: socket.socket, size: int) -> bytes:
    """Read exactly the declared bounded frame bytes from a blocking socket."""
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(min(size - len(result), 65_536))
        if not chunk:
            raise EOFError("worker command ended early")
        result.extend(chunk)
    return bytes(result)


def _encode_json_object(value: Mapping[str, JsonValue], max_bytes: int) -> bytes:
    """Validate a plain mapping and encode it as bounded strict JSON bytes."""
    if type(value) is not dict:
        raise TypeError("parameters must be a plain dict JSON object")
    try:
        _validate_json_value(value)
        encoded = _encode_json_value(value, max_bytes)
    except _ProtocolValueError as exc:
        if exc.code == "result_limit_exceeded":
            raise ValueError("process_parameters_size_limit_exceeded") from None
        if exc.code == "result_contains_secret_like_value":
            raise ValueError("process_parameters_rejected_secret_like_content") from None
        raise ValueError("process_parameters_invalid") from None
    return encoded


def _encode_json_value(value: object, max_bytes: int) -> bytes:
    """Encode one JSON value after structural and secret-like value checks."""
    _validate_json_value(value)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise _ProtocolValueError("json_encoding_invalid") from exc
    if len(encoded) > max_bytes:
        raise _ProtocolValueError("result_limit_exceeded")
    return encoded


def _decode_json_object(encoded: bytes) -> dict[str, JsonValue]:
    """Decode strict JSON parameters and revalidate the resulting plain object."""
    value = _decode_json_value(encoded)
    if type(value) is not dict:
        raise _ProtocolValueError("parameters_shape_invalid")
    _validate_json_value(value)
    return value


def _decode_json_value(encoded: bytes) -> JsonValue:
    """Decode one JSON document while rejecting duplicate keys and NaN values."""

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, value in pairs:
            if key in values:
                raise _ProtocolValueError("json_duplicate_key")
            values[key] = value
        return values

    def reject_constant(_: str) -> None:
        raise _ProtocolValueError("json_nonstandard_number")

    try:
        parsed: object = json.loads(
            encoded,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise _ProtocolValueError("json_document_invalid") from exc
    _validate_json_value(parsed)
    return parsed  # type: ignore[return-value]


def _validate_json_value(value: object) -> None:
    """Enforce plain JSON types, bounded shape, and conservative secret checks."""
    active: set[int] = set()
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    node_count = 0
    text_bytes = 0
    while stack:
        current, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(current))
            continue
        node_count += 1
        if node_count > MAX_JSON_NODES:
            raise _ProtocolValueError("json_node_limit_exceeded")
        if depth > MAX_JSON_DEPTH:
            raise _ProtocolValueError("json_depth_limit_exceeded")
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if current.bit_length() > 256:
                raise _ProtocolValueError("json_integer_limit_exceeded")
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise _ProtocolValueError("json_nonfinite_number")
            continue
        if type(current) is str:
            size = _text_size(current)
            text_bytes += size
            if text_bytes > MAX_JSON_TEXT_BYTES:
                raise _ProtocolValueError("json_text_limit_exceeded")
            if any(pattern.search(current) for pattern in _SECRET_VALUE_PATTERNS):
                raise _ProtocolValueError("result_contains_secret_like_value")
            continue
        if type(current) is dict or type(current) is list:
            object_id = id(current)
            if object_id in active:
                raise _ProtocolValueError("json_cycle_detected")
            active.add(object_id)
            stack.append((current, depth, True))
            if type(current) is dict:
                if len(current) > MAX_JSON_NODES - node_count:
                    raise _ProtocolValueError("json_node_limit_exceeded")
                for key, child in current.items():
                    if type(key) is not str:
                        raise _ProtocolValueError("json_object_key_invalid")
                    key_size = _text_size(key)
                    text_bytes += key_size
                    if text_bytes > MAX_JSON_TEXT_BYTES:
                        raise _ProtocolValueError("json_text_limit_exceeded")
                    normalized_key = key.casefold().replace("_", "").replace("-", "")
                    if any(part in normalized_key for part in _SECRET_KEY_PARTS):
                        raise _ProtocolValueError("result_contains_secret_like_value")
                    stack.append((child, depth + 1, False))
            elif type(current) is list:
                if len(current) > MAX_JSON_NODES - node_count:
                    raise _ProtocolValueError("json_node_limit_exceeded")
                stack.extend((child, depth + 1, False) for child in current)
            continue
        raise _ProtocolValueError("json_type_invalid")


class _ProtocolValueError(ValueError):
    """Internal typed failure with an allowlisted, non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_protocol_error(error: _ProtocolValueError) -> str:
    """Map validator failures to the narrow public child error vocabulary."""
    if error.code == "result_limit_exceeded":
        return "result_limit_exceeded"
    if error.code == "result_contains_secret_like_value":
        return "result_contains_secret_like_value"
    return "handler_failed"


def _redirect_child_output() -> None:
    """Redirect native stdout and stderr to the null device before invoking code."""
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    finally:
        if null_fd not in (1, 2):
            os.close(null_fd)


def _start_process_group() -> bool:
    """Create an isolated POSIX group so forced cleanup reaches descendants."""
    if os.name != "posix":
        return False
    if os.getsid(0) != os.getpid():
        os.setsid()
    return True


def _stop_process(
    process: subprocess.Popen[bytes],
    *,
    isolated_group: bool,
    grace_seconds: float,
) -> None:
    """Terminate, escalate to kill, and reap the child within two grace periods."""
    process_id = process.pid
    if isolated_group:
        _signal_process_group(process_id, signal.SIGTERM)
    elif process.poll() is None:
        process.terminate()
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=grace_seconds)
    if isolated_group and _process_group_exists(process_id):
        _signal_process_group(process_id, signal.SIGKILL)
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired as exc:
        raise ProcessExecutionCleanupError("process_cleanup_failed") from exc


def _stop_group_descendants(process_id: int, grace_seconds: float) -> None:
    """Stop any POSIX descendants left in the worker's group after normal exit."""
    if not _process_group_exists(process_id):
        return
    _signal_process_group(process_id, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and _process_group_exists(process_id):
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    if _process_group_exists(process_id):
        _signal_process_group(process_id, signal.SIGKILL)


def _signal_process_group(process_id: int, signal_number: int) -> None:
    """Signal an isolated child process group and ignore a vanished group."""
    with suppress(ProcessLookupError):
        os.killpg(process_id, signal_number)


def _process_group_exists(process_id: int) -> bool:
    """Check whether one isolated POSIX process group still has members."""
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _response_counter(response: Mapping[str, object], name: str) -> int:
    """Read a bounded nonnegative integer output counter from the IPC result."""
    value = response.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProcessExecutionProtocolError("process_response_invalid")
    return value


def _response_bool(response: Mapping[str, object], name: str) -> bool:
    """Read one strict boolean response flag."""
    value = response.get(name)
    if type(value) is not bool:
        raise ProcessExecutionProtocolError("process_response_invalid")
    return value


def _is_importable_top_level_function(value: object) -> bool:
    """Accept only importable module-level functions, never methods or closures."""
    if type(value) is not FunctionType:
        return False
    module = getattr(value, "__module__", None)
    qualified_name = getattr(value, "__qualname__", None)
    name = getattr(value, "__name__", None)
    return (
        isinstance(module, str)
        and module not in {"", "__main__"}
        and isinstance(qualified_name, str)
        and qualified_name == name
        and "<locals>" not in qualified_name
        and name != "<lambda>"
    )


def _module_exports_handler(module_name: str, qualified_name: str, handler: object) -> bool:
    """Verify the reviewed function is directly exported by its defining module."""
    module = sys.modules.get(module_name)
    return module is not None and getattr(module, qualified_name, None) is handler


def _resolve_handler(module_name: str, qualified_name: str) -> ReviewedHandler:
    """Import and verify a handler using a registry-captured module reference."""
    if (
        not module_name
        or module_name == "__main__"
        or any(not part.isidentifier() for part in module_name.split("."))
        or not qualified_name.isidentifier()
    ):
        raise TypeError("handler reference is invalid")
    module = importlib.import_module(module_name)
    handler = getattr(module, qualified_name, None)
    if not _is_importable_top_level_function(handler):
        raise TypeError("handler reference is invalid")
    resolved_handler = cast("ReviewedHandler", handler)
    if (
        resolved_handler.__module__ != module_name
        or resolved_handler.__qualname__ != qualified_name
    ):
        raise TypeError("handler reference is invalid")
    return resolved_handler


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    """Validate an integer constructor option without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def _bounded_number(value: float, name: str, minimum: float, maximum: float) -> float:
    """Validate a finite numeric duration without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < minimum or normalized > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return normalized


def _text_size(value: str) -> int:
    """Return UTF-8 size while rejecting invalid surrogate strings."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _ProtocolValueError("json_text_invalid") from exc


def _utf8_prefix_size(value: str, max_bytes: int) -> tuple[int, bool]:
    """Count a bounded UTF-8 prefix and indicate whether more bytes were present."""
    total = 0
    for character in value:
        character_bytes = len(character.encode("utf-8", errors="replace"))
        if total + character_bytes > max_bytes:
            return max_bytes, True
        total += character_bytes
    return total, False


__all__ = [
    "ProcessExecutionCancelledError",
    "ProcessExecutionCleanupError",
    "ProcessExecutionCrashedError",
    "ProcessExecutionError",
    "ProcessExecutionHandlerError",
    "ProcessExecutionProtocolError",
    "ProcessExecutionResult",
    "ProcessExecutionTimeoutError",
    "ReviewedProcessExecutor",
    "ReviewedProcessRegistry",
]
