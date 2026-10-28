"""Canonical, bounded caller-side summaries of request parameters.

This module has no application, configuration, database, filesystem, or network
side effects. The versioned digest describes only a caller's explicitly
allowlisted scalar parameter mapping; it does not prove which bytes were sent or
what a provider received.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import TypeAlias

RequestValue: TypeAlias = str | int | float | bool | date | None

_REQUEST_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|secret|token|auth|authorization|cookie|header|"
    r"password|credential|signature|session)(?:_|$)",
    re.IGNORECASE,
)
_MAX_REQUEST_SUMMARY_BYTES = 32 * 1024
_MAX_REQUEST_ALLOWLIST_FIELDS = 128
_MAX_REQUEST_PARAMETER_ITEMS = 256
_MAX_REQUEST_INTEGER_BITS = 256


class RawEvidenceError(ValueError):
    """Raised when a request summary or raw-evidence operation is unsafe.

    ``opendata.pipeline.raw_evidence`` re-exports this class for compatibility.
    Its ``__module__`` now names this side-effect-free module, while the legacy
    import path still resolves to the identical class object.
    """


def request_summary_sha256(
    parameters: Mapping[str, RequestValue], *, allowed_fields: Iterable[str]
) -> str:
    """Hash explicitly allowlisted scalar request parameters without storing them.

    The allowlist is source/job specific and mandatory. Credential-like keys are
    rejected even if included in it. Values are encoded only into the digest input;
    the raw mapping is never copied into a response manifest or written to disk.
    Version 1 describes only the supplied parameter mapping; it omits transport
    method, URL, headers, and provider-side receipt or validation.

    Args:
        parameters: Bounded scalar parameter mapping to summarize.
        allowed_fields: Explicit iterable of permitted parameter names.

    Returns:
        Lowercase SHA-256 hex digest of the version 1 canonical JSON bytes.

    Raises:
        RawEvidenceError: If the mapping, allowlist, key, or value is unsafe.
    """
    if not isinstance(parameters, Mapping):
        raise RawEvidenceError("request parameters must be a mapping")
    try:
        parameter_count = len(parameters)
    except (TypeError, OverflowError) as exc:
        raise RawEvidenceError("request parameter count is unavailable") from exc
    if parameter_count > _MAX_REQUEST_PARAMETER_ITEMS:
        raise RawEvidenceError("request parameter item count exceeds its limit")
    if isinstance(allowed_fields, (str, bytes)):
        raise RawEvidenceError("request allowlist must be an iterable of field names")
    try:
        allowed_iterator = iter(allowed_fields)
    except TypeError as exc:
        raise RawEvidenceError("request allowlist must be iterable") from exc

    allowed: set[str] = set()
    for index, key in enumerate(allowed_iterator):
        if index >= _MAX_REQUEST_ALLOWLIST_FIELDS:
            raise RawEvidenceError("request allowlist exceeds its field-count limit")
        if not isinstance(key, str) or _REQUEST_KEY_PATTERN.fullmatch(key) is None:
            raise RawEvidenceError("request allowlist contains an invalid field name")
        if _CREDENTIAL_KEY_PATTERN.search(key):
            raise RawEvidenceError("credential-like request field is forbidden")
        if key in allowed:
            raise RawEvidenceError("request allowlist contains a duplicate field")
        allowed.add(key)

    normalized: dict[str, str | int | float | bool | None] = {}
    for index, (key, value) in enumerate(parameters.items()):
        if index >= _MAX_REQUEST_PARAMETER_ITEMS:
            raise RawEvidenceError("request parameter item count exceeds its limit")
        if not isinstance(key, str) or _REQUEST_KEY_PATTERN.fullmatch(key) is None:
            raise RawEvidenceError("request parameter name is invalid")
        if _CREDENTIAL_KEY_PATTERN.search(key):
            raise RawEvidenceError("credential-like request field is forbidden")
        if key not in allowed:
            raise RawEvidenceError("request parameter is outside the explicit allowlist")
        if key in normalized:
            raise RawEvidenceError("request parameters contain a duplicate field")
        if isinstance(value, datetime):
            raise RawEvidenceError("datetime request values are not supported; use a business date")
        if isinstance(value, date):
            normalized[key] = value.isoformat()
        elif value is None or isinstance(value, (str, bool)):
            if isinstance(value, str) and len(value.encode("utf-8")) > 1024:
                raise RawEvidenceError("request parameter value exceeds the bounded summary limit")
            normalized[key] = value
        elif isinstance(value, int):
            if int.bit_length(value) > _MAX_REQUEST_INTEGER_BITS:
                raise RawEvidenceError("integer request parameter exceeds its bit-length limit")
            normalized[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise RawEvidenceError("non-finite request parameter values are forbidden")
            normalized[key] = value
        else:
            raise RawEvidenceError("request parameters must be bounded scalar values")

    encoded = json.dumps(
        {"parameters": normalized, "version": 1},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_REQUEST_SUMMARY_BYTES:
        raise RawEvidenceError("canonical request summary exceeds its size limit")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["RawEvidenceError", "RequestValue", "request_summary_sha256"]
