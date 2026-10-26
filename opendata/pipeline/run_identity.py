"""Versioned, declarative identities for future durable pipeline runs.

This module defines and validates job and run inputs only. It does not
schedule, persist, claim, or execute runs, and its digest is not a distributed
single-writer or exactly-once guarantee.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from datetime import timezone as dt_timezone
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION: Final[Literal[1]] = 1
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 100
MAX_JSON_STRING_LENGTH = 2_048
MAX_JSON_DOCUMENT_BYTES = 16_384
MAX_CANONICAL_BYTES = 65_536

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,255}$"
_SENSITIVE_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_BEARER_VALUE = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_COMMON_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})$"
)

Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
ShardIdentifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
IdempotencyToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=256, pattern=_IDEMPOTENCY_PATTERN),
]
BoundedText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]


class _StrictModel(BaseModel):
    """Base configuration shared by the declarative contract models."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ScheduleMetadata(_StrictModel):
    """Non-executable scheduling metadata retained with a job definition."""

    kind: Literal["cron", "interval", "date"]
    expression: BoundedText
    description: str | None = Field(default=None, max_length=512)

    @field_validator("expression", "description")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        """Reject control characters in human-readable schedule metadata."""
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("schedule metadata must not contain control characters")
        return value


class TaskDefinition(_StrictModel):
    """Validated job metadata with retained, default-disabled schedule settings."""

    contract_version: Literal[1] = CONTRACT_VERSION
    job_id: Identifier
    version: Identifier
    dataset_id: Identifier
    provider: Identifier
    model: Identifier
    timezone: BoundedText = "UTC"
    calendar: Identifier | None = None
    schedule: ScheduleMetadata | None = None
    enabled: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Require a valid IANA time-zone name."""
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA time-zone name") from exc
        return value

    @model_validator(mode="after")
    def require_schedule_when_enabled(self) -> TaskDefinition:
        """Require schedule metadata before enabling scheduled execution."""
        if self.enabled and self.schedule is None:
            raise ValueError("enabled jobs require schedule metadata")
        return self


class RunRequest(_StrictModel):
    """Explicit run request; scheduled partitions define distinct natural slots.

    Query, partition, shard, and run parameters are immutable payload data. For
    scheduled runs, partition and shard also separate natural slots; payload
    changes within a slot must be reported as conflicts by the durable ledger.
    """

    contract_version: Literal[1] = CONTRACT_VERSION
    run_type: Literal["scheduled", "manual", "backfill"]
    query: dict[str, JsonValue] = Field(default_factory=dict)
    partition: dict[str, JsonValue] = Field(default_factory=dict)
    shard: ShardIdentifier | None = None
    occurrence: datetime | None = None
    idempotency_key: IdempotencyToken | None = None
    backfill_start: date | None = None
    backfill_end: date | None = None

    @field_validator("query", "partition", mode="before")
    @classmethod
    def validate_json_object(cls, value: object) -> object:
        """Validate bounded JSON-native objects and reject credential material."""
        if type(value) is not dict:
            raise ValueError("query and partition must be JSON objects")
        _validate_json_value(value, path="$", depth=0)
        encoded = _canonical_json(value).encode("utf-8")
        if len(encoded) > MAX_JSON_DOCUMENT_BYTES:
            raise ValueError(f"query and partition must be at most {MAX_JSON_DOCUMENT_BYTES} bytes")
        return value

    @field_validator("occurrence")
    @classmethod
    def normalize_occurrence_to_utc(cls, value: datetime | None) -> datetime | None:
        """Require an aware occurrence and normalize equivalent instants to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled occurrence must be timezone-aware")
        return value.astimezone(dt_timezone.utc)

    @model_validator(mode="after")
    def validate_run_shape(self) -> RunRequest:
        """Enforce fields required or forbidden for each run type."""
        if self.run_type == "scheduled":
            if self.occurrence is None:
                raise ValueError("scheduled runs require an aware occurrence")
            if self.idempotency_key is not None:
                raise ValueError("scheduled runs use their occurrence, not a request key")
            if self.backfill_start is not None or self.backfill_end is not None:
                raise ValueError("scheduled runs must not include a backfill range")
        elif self.run_type == "manual":
            if self.idempotency_key is None:
                raise ValueError("manual runs require an explicit idempotency_key")
            if self.occurrence is not None:
                raise ValueError("manual runs must not include a scheduled occurrence")
            if self.backfill_start is not None or self.backfill_end is not None:
                raise ValueError("manual runs must not include a backfill range")
        else:
            if self.idempotency_key is None:
                raise ValueError("backfill runs require an explicit idempotency_key")
            if self.occurrence is not None:
                raise ValueError("backfill runs must not include a scheduled occurrence")
            if self.backfill_start is None or self.backfill_end is None:
                raise ValueError("backfill runs require both backfill_start and backfill_end")
            if self.backfill_start > self.backfill_end:
                raise ValueError("backfill_start must not be after backfill_end")
        return self


class RunIdentity(_StrictModel):
    """Stable slot digest and canonical request snapshot for one declared run."""

    contract_version: Literal[1]
    run_type: Literal["scheduled", "manual", "backfill"]
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_json: str = Field(min_length=2, max_length=MAX_CANONICAL_BYTES)


def build_run_identity(task: TaskDefinition, request: RunRequest) -> RunIdentity:
    """Build a deterministic slot key and a separate request-payload hash.

    Scheduled runs are keyed by their normalized UTC occurrence. Manual and
    backfill runs require an explicit request key, whose digest is included so
    separate requests on the same date do not occupy the scheduled slot. The
    canonical snapshot includes all execution parameters; changing parameters
    under an existing slot changes ``payload_hash`` while preserving the key.

    Args:
        task: The versioned job declaration.
        request: The validated mode-specific run request.

    Returns:
        A canonical JSON snapshot and SHA-256 idempotency key.

    Raises:
        ValueError: If contract versions differ or a request is malformed.
    """
    if task.contract_version != request.contract_version:
        raise ValueError("task and run request contract versions must match")

    occurrence = (
        _canonical_occurrence(request.occurrence) if request.run_type == "scheduled" else None
    )
    request_key_digest = (
        _sha256(request.idempotency_key.encode("utf-8"))
        if request.idempotency_key is not None
        else None
    )
    backfill_range = None
    if request.run_type == "backfill":
        if request.backfill_start is None or request.backfill_end is None:
            raise ValueError("backfill runs require both range boundaries")
        backfill_range = {
            "start": request.backfill_start.isoformat(),
            "end": request.backfill_end.isoformat(),
        }

    payload = {
        "contract_version": task.contract_version,
        "job": {
            "job_id": task.job_id,
            "version": task.version,
            "dataset_id": task.dataset_id,
            "provider": task.provider,
            "model": task.model,
        },
        "run": {
            "run_type": request.run_type,
            "occurrence": occurrence,
            "request_key_sha256": request_key_digest,
            "backfill_range": backfill_range,
            "shard": request.shard,
            "business_request": {
                "query": request.query,
                "partition": request.partition,
            },
        },
    }
    canonical_json = _canonical_json(payload)
    if len(canonical_json.encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ValueError(f"canonical request snapshot must be at most {MAX_CANONICAL_BYTES} bytes")

    slot_payload = {
        "contract_version": task.contract_version,
        "job_id": task.job_id,
        "version": task.version,
        "run_type": request.run_type,
    }
    if request.run_type == "scheduled":
        if occurrence is None:
            raise ValueError("scheduled runs require an aware occurrence")
        slot_payload["slot"] = {
            "occurrence": occurrence,
            "partition": request.partition,
            "shard": request.shard,
        }
    else:
        if request_key_digest is None:
            raise ValueError(f"{request.run_type} runs require an idempotency key")
        slot_payload["request_key_sha256"] = request_key_digest
    slot_json = _canonical_json(slot_payload)
    payload_hash = _sha256(canonical_json.encode("utf-8"))

    return RunIdentity(
        contract_version=task.contract_version,
        run_type=request.run_type,
        idempotency_key=_sha256(slot_json.encode("utf-8")),
        payload_hash=payload_hash,
        canonical_json=canonical_json,
    )


def _canonical_occurrence(value: datetime | None) -> str:
    """Serialize an aware occurrence in normalized UTC form."""
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled occurrence must be timezone-aware")
    normalized = value.astimezone(dt_timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    """Serialize JSON-native data with stable key ordering and no NaN values."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: bytes) -> str:
    """Return a lowercase hexadecimal SHA-256 digest."""
    return hashlib.sha256(value).hexdigest()


def _validate_json_value(value: object, *, path: str, depth: int) -> None:
    """Reject non-JSON values, unbounded structures, and likely secrets."""
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"JSON value at {path} exceeds maximum depth {MAX_JSON_DEPTH}")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"JSON number at {path} must be finite")
        return
    if type(value) is str:
        _validate_json_string(value, path)
        return
    if type(value) is list:
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError(f"JSON array at {path} exceeds {MAX_JSON_ITEMS} items")
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError(f"JSON object at {path} exceeds {MAX_JSON_ITEMS} items")
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"JSON object keys at {path} must be strings")
            _validate_json_string(key, f"{path}.<key>")
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"sensitive parameter name is not allowed at {path}.{key}")
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(f"value at {path} is not JSON-native")


def _validate_json_string(value: str, path: str) -> None:
    """Enforce string bounds and reject recognizable credential values."""
    if len(value) > MAX_JSON_STRING_LENGTH:
        raise ValueError(f"JSON string at {path} exceeds {MAX_JSON_STRING_LENGTH} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"JSON string at {path} must not contain control characters")
    if _BEARER_VALUE.match(value) or _COMMON_SECRET_VALUE.fullmatch(value):
        raise ValueError(f"credential-like value is not allowed at {path}")
