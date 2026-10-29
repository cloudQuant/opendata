"""Shared pin model and bounded parser for temporal identity configuration.

This low-level module depends only on the standard library and Pydantic. It is
shared by the API, runtime and offline staging code, and deliberately imports
no route, database, settings, filesystem or provider modules. Digests bind the
exact supplied bytes; they do not authenticate a source or establish rights or
coverage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, NoReturn

from pydantic import BaseModel, ConfigDict, StringConstraints

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES",
    "MAX_TEMPORAL_IDENTITY_PIN_JSON_DEPTH",
    "MAX_TEMPORAL_IDENTITY_PIN_JSON_NODES",
    "MAX_TEMPORAL_IDENTITY_PINS",
    "TemporalIdentityConfigurationError",
    "TemporalIdentityPin",
    "TemporalIdentityPinDocumentError",
    "TemporalIdentityPinRegistry",
    "check_temporal_identity_pin_json_budget",
    "decode_temporal_identity_pin_document",
    "decode_temporal_identity_pin_registry",
]

MAX_TEMPORAL_IDENTITY_PINS = 2_048
MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES = 1024 * 1024
MAX_TEMPORAL_IDENTITY_PIN_JSON_DEPTH = 8
MAX_TEMPORAL_IDENTITY_PIN_JSON_NODES = 40_000

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SHA256 = re.compile(_SHA256_PATTERN, re.ASCII)
_PIN_FIELDS = frozenset(
    {
        "dataset_id",
        "source_id",
        "authority_id",
        "registry_id",
        "reference_version",
        "snapshot_sha256",
    }
)
_ROOT_FIELDS = frozenset({"schema_version", "pins"})
_Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
_Sha256 = Annotated[str, StringConstraints(strict=True, pattern=_SHA256_PATTERN)]


class TemporalIdentityConfigurationError(ValueError):
    """Represent an invalid operator pin without exposing its contents."""

    def __init__(self) -> None:
        """Create a fixed, content-free pin configuration message."""
        super().__init__("Invalid temporal identity pin configuration")


class TemporalIdentityPin(BaseModel):
    """Bind a dataset and source grant to one immutable identity snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: _Identifier
    source_id: _Identifier
    authority_id: _Identifier
    registry_id: _Identifier
    reference_version: _Identifier
    snapshot_sha256: _Sha256


@dataclass(frozen=True, slots=True, init=False)
class TemporalIdentityPinRegistry:
    """Immutable pin lookup with one exact entry per dataset identifier."""

    _pins: Mapping[str, TemporalIdentityPin]

    def __init__(self, pins: Sequence[TemporalIdentityPin]) -> None:
        """Freeze a bounded sequence of unique, strictly validated pin models."""
        if (
            not isinstance(pins, (tuple, list))
            or len(pins) > MAX_TEMPORAL_IDENTITY_PINS
            or any(type(pin) is not TemporalIdentityPin for pin in pins)
        ):
            raise TemporalIdentityConfigurationError
        by_dataset: dict[str, TemporalIdentityPin] = {}
        for pin in pins:
            if pin.dataset_id in by_dataset:
                raise TemporalIdentityConfigurationError
            by_dataset[pin.dataset_id] = pin
        object.__setattr__(self, "_pins", MappingProxyType(by_dataset))

    def get(self, dataset_id: str) -> TemporalIdentityPin | None:
        """Return only the exact pin for one dataset identifier."""
        return self._pins.get(dataset_id)


class TemporalIdentityPinDocumentError(ValueError):
    """Report a fixed parser error code without echoing pin document contents."""

    def __init__(self, code: str) -> None:
        """Create a parser error with the stable code used by runtime adapters."""
        self.code = code
        super().__init__(code)


def decode_temporal_identity_pin_registry(
    raw: bytes,
    expected_sha256: str,
) -> TemporalIdentityPinRegistry:
    """Validate digest and strict v1 pin JSON, returning an immutable lookup.

    The digest must come from a caller-controlled channel separate from the pin
    bytes. A matching SHA-256 proves byte equality only; it does not establish
    source authenticity, operator authority, rights or dataset completeness.

    Args:
        raw: Complete pin document bytes supplied by the caller.
        expected_sha256: Separately supplied lowercase SHA-256 of ``raw``.

    Returns:
        A strict immutable registry with exactly one pin per dataset.

    Raises:
        TemporalIdentityPinDocumentError: If the digest, JSON, schema, values,
            uniqueness or resource bounds are invalid.
    """
    pins = decode_temporal_identity_pin_document(raw, expected_sha256)
    return TemporalIdentityPinRegistry(pins)


def decode_temporal_identity_pin_document(
    raw: bytes,
    expected_sha256: str,
) -> tuple[TemporalIdentityPin, ...]:
    """Validate a complete pin document and return its ordered pin values.

    This variant is for offline tools that need to preserve all document pins.
    It applies the same registry uniqueness validation as the runtime lookup.

    Args:
        raw: Complete pin document bytes supplied by the caller.
        expected_sha256: Separately supplied lowercase SHA-256 of ``raw``.

    Returns:
        Strict pin models in their original document order.

    Raises:
        TemporalIdentityPinDocumentError: If the digest, JSON, schema, values,
            uniqueness or resource bounds are invalid.
    """
    if type(raw) is not bytes:
        raise TemporalIdentityPinDocumentError("pin_document_invalid")
    if type(expected_sha256) is not str or _SHA256.fullmatch(expected_sha256) is None:
        raise TemporalIdentityPinDocumentError("pin_digest_mismatch")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise TemporalIdentityPinDocumentError("pin_digest_mismatch")
    if not raw or len(raw) > MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES:
        raise TemporalIdentityPinDocumentError("pin_document_invalid")

    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        check_temporal_identity_pin_json_budget(document)
        _check_pin_json_unicode(document)
        root = _require_object(document, _ROOT_FIELDS)
        if type(root["schema_version"]) is not int or root["schema_version"] != 1:
            raise TemporalIdentityPinDocumentError("pin_document_invalid")
        pins_value = root["pins"]
        if type(pins_value) is not list or not 1 <= len(pins_value) <= MAX_TEMPORAL_IDENTITY_PINS:
            raise TemporalIdentityPinDocumentError("pin_count_invalid")
        pins = tuple(
            TemporalIdentityPin.model_validate(_require_object(value, _PIN_FIELDS), strict=True)
            for value in pins_value
        )
        TemporalIdentityPinRegistry(pins)
        return pins
    except TemporalIdentityPinDocumentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise TemporalIdentityPinDocumentError("pin_document_invalid") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys at every JSON object depth."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    """Reject non-standard NaN and Infinity JSON values."""
    raise ValueError("non-standard JSON constant")


def check_temporal_identity_pin_json_budget(document: object) -> None:
    """Bound JSON nesting and node count before schema validation."""
    stack: list[tuple[object, int]] = [(document, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if (
            depth > MAX_TEMPORAL_IDENTITY_PIN_JSON_DEPTH
            or nodes > MAX_TEMPORAL_IDENTITY_PIN_JSON_NODES
        ):
            raise TemporalIdentityPinDocumentError("pin_document_bounds_invalid")
        if type(value) is dict:
            stack.extend((child, depth + 1) for child in value.values())
        elif type(value) is list:
            stack.extend((child, depth + 1) for child in value)


def _check_pin_json_unicode(document: object) -> None:
    """Reject escaped lone surrogate scalars before model validation."""
    stack: list[object] = [document]
    while stack:
        value = stack.pop()
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise TemporalIdentityPinDocumentError("pin_document_invalid") from None
        elif type(value) is dict:
            stack.extend(value.keys())
            stack.extend(value.values())
        elif type(value) is list:
            stack.extend(value)


def _require_object(value: object, fields: frozenset[str]) -> dict[str, object]:
    """Require an exact object field set without echoing operator content."""
    if type(value) is not dict or set(value) != fields:
        raise TemporalIdentityPinDocumentError("pin_document_invalid")
    return value
