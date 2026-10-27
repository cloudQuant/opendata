"""Lightweight, offline handlers used by real Sina process tests."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Mapping

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
FIXTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
CHILD_FIXTURE_PROTOCOL = "sina-fzb-capture-v1"
CHILD_FIXTURE_MAX_RAW_RESPONSE_BYTES = 4_194_304
OTHER_SYMBOL = "600001.SH"
RETRIEVED_AT_TEXT = "2026-10-27T02:00:00.123456Z"

FIXTURE_OPERATION_ID = "test.sina.fixture.capture"
MAX_BODY_OPERATION_ID = "test.sina.fixture.max-body"
WRONG_SYMBOL_OPERATION_ID = "test.sina.fixture.wrong-symbol"
CHILD_ERROR_OPERATION_ID = "test.sina.fixture.child-error"
MALFORMED_RESULT_OPERATION_ID = "test.sina.fixture.malformed-result"
TIMEOUT_OPERATION_ID = "test.sina.fixture.timeout"
CANCEL_OPERATION_ID = "test.sina.fixture.cancel"


def _recorded_body() -> bytes:
    """Return only the SHA-pinned offline upstream response body."""
    archive = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == FIXTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    return base64.b64decode(call["content_b64"], validate=True)


def _fixture_envelope(symbol: str) -> dict[str, JsonValue]:
    """Return the exact frozen child response envelope around the offline bytes."""
    body = _recorded_body()
    return {
        "protocol": CHILD_FIXTURE_PROTOCOL,
        "query": {"symbol": symbol},
        "retrieved_at_utc": RETRIEVED_AT_TEXT,
        "status_code": 200,
        "body_b64": base64.b64encode(body).decode("ascii"),
    }


def fixture_sina_fzb_capture(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return the pinned response in a separate test-owned real-Popen operation."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
        or type(parameters["symbol"]) is not str
    ):
        raise ValueError("fixture_process_request_invalid")
    return _fixture_envelope(parameters["symbol"])


def fixture_sina_fzb_max_body(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a synthetic maximum-size opaque body for the real IPC text cap."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
        or type(parameters["symbol"]) is not str
    ):
        raise ValueError("fixture_process_request_invalid")
    return {
        "protocol": CHILD_FIXTURE_PROTOCOL,
        "query": {"symbol": parameters["symbol"]},
        "retrieved_at_utc": RETRIEVED_AT_TEXT,
        "status_code": 200,
        "body_b64": base64.b64encode(b"x" * CHILD_FIXTURE_MAX_RAW_RESPONSE_BYTES).decode("ascii"),
    }


def fixture_sina_fzb_wrong_symbol(
    parameters: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return a canonical but mismatched source identity for atomicity coverage."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
        or type(parameters["symbol"]) is not str
    ):
        raise ValueError("fixture_process_request_invalid")
    return _fixture_envelope(OTHER_SYMBOL)


def fixture_sina_fzb_child_error(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Raise an intentionally sensitive child exception that must be sanitized."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
    ):
        raise ValueError("fixture_process_request_invalid")
    raise RuntimeError("Authorization=Bearer secret-wave12-fixture-value")


def _private_marker_path(name: str) -> Path:
    """Use the inherited empty runner cwd only; never write into the source tree."""
    cwd = Path.cwd().resolve()
    source_root = Path(__file__).resolve().parents[1]
    if cwd == source_root or source_root in cwd.parents:
        raise RuntimeError("offline process tests require an external private cwd")
    return cwd / name


def fixture_sina_fzb_malformed_result(
    parameters: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return one malformed envelope from a real child and record its PID."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
        or type(parameters["symbol"]) is not str
    ):
        raise ValueError("fixture_process_request_invalid")
    marker = _private_marker_path("wave12-sina-child.malformed.started")
    marker.write_text(str(os.getpid()), encoding="ascii")
    envelope = _fixture_envelope(parameters["symbol"])
    envelope.pop("status_code")
    return envelope


def _sleeping_fixture(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Mark that the real child started, then wait until parent kill/reap."""
    if (
        type(parameters) is not dict
        or set(parameters) != {"protocol", "symbol"}
        or parameters["protocol"] != CHILD_FIXTURE_PROTOCOL
        or type(parameters["symbol"]) is not str
    ):
        raise ValueError("fixture_process_request_invalid")
    marker = _private_marker_path("wave12-sina-child.started")
    marker.write_text(str(os.getpid()), encoding="ascii")
    time.sleep(60.0)
    return _fixture_envelope(parameters["symbol"])
