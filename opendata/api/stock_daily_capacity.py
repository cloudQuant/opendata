"""Default-off capacity gate for the pinned stock-daily publication route.

The gate is a deployment/capacity control, not data authorization. Enabling it
requires an operator flag plus a SHA-256-pinned JSON attestation that names the
exact dataset, source, semantic version, contract digest and request bounds the
deployment was reviewed for. The digest binds exact file bytes; it is not a
signature, a license review, or evidence that the underlying source is complete
or that real data exists.

The loader performs no environment lookup and never logs file contents. All
configuration failures are converted to the route's uniform 404 by the
dependency factory, so callers cannot distinguish a disabled route from an
invalid pin.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from opendata.api.published_data import _not_found, _server_setting

MAX_CAPACITY_ATTESTATION_BYTES = 65_536
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)
_BoundedText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$"),
]


class StockDailyCapacityError(ValueError):
    """Report a content-free attestation configuration failure."""

    def __init__(self) -> None:
        """Initialize a fixed error that cannot echo file contents or settings."""
        super().__init__("Invalid stock-daily capacity attestation")


class StockDailyCapacityAttestation(BaseModel):
    """Reviewed bounds and identity bindings for one stock-daily deployment.

    ``contract_sha256`` is the canonical dataset contract digest. The
    publication store records that same digest under
    ``provenance["contract_sha256"]``; the route compares this value with the
    digest returned by contract resolution, not with a publication attribute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["stock-daily-capacity-v1"]
    dataset_id: Literal["cn-equity-stock-daily"]
    source_id: Literal["akshare.daily"]
    semantic_version: Literal["stock-daily-raw-v1"]
    contract_sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    publication_id: (
        Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")] | None
    ) = None
    max_window_days: StrictInt = Field(ge=1, le=3_660)
    max_page_size: StrictInt = Field(ge=1, le=1_000)
    not_before: datetime
    expires_at: datetime
    evidence_ref: _BoundedText
    attested_by: _BoundedText

    @field_validator("not_before", "expires_at", mode="before")
    @classmethod
    def _require_utc_rfc3339(cls, value: object) -> datetime:
        """Accept JSON RFC3339 strings or aware datetimes and normalize to UTC."""
        if type(value) is str:
            if _RFC3339.fullmatch(value) is None:
                raise ValueError("attestation timestamps must be RFC3339 strings")
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                raise ValueError("attestation timestamps must be RFC3339 strings") from None
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise ValueError("attestation timestamps must be RFC3339 strings")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("attestation timestamps must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _require_ordered_validity(self) -> StockDailyCapacityAttestation:
        """Require a non-empty, half-open validity interval."""
        if self.expires_at <= self.not_before:
            raise ValueError("expires_at must follow not_before")
        return self

    def is_active(self, now: datetime | None = None) -> bool:
        """Return whether ``now`` (UTC) falls inside the half-open validity window."""
        moment = datetime.now(timezone.utc) if now is None else now
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            return False
        current = moment.astimezone(timezone.utc)
        return self.not_before <= current < self.expires_at


def load_stock_daily_capacity_attestation(
    path: str | Path,
    expected_sha256: str,
) -> StockDailyCapacityAttestation:
    """Load and verify one bounded, duplicate-free capacity attestation file.

    The caller supplies both the explicit server path and the independently
    delivered digest. This function performs no environment lookup and does not
    log or print the file contents.

    Args:
        path: Explicit path to the operator-owned JSON attestation.
        expected_sha256: Lowercase 64-hex SHA-256 of the exact file bytes.

    Returns:
        The validated, frozen attestation.

    Raises:
        StockDailyCapacityError: If the path, size, digest, JSON shape, or field
            values fail any fixed check.
    """
    if type(expected_sha256) is not str or _SHA256.fullmatch(expected_sha256) is None:
        raise StockDailyCapacityError
    if not isinstance(path, (str, Path)) or isinstance(path, bool):
        raise StockDailyCapacityError
    raw_path = str(path)
    if not raw_path or "\x00" in raw_path:
        raise StockDailyCapacityError
    try:
        with Path(raw_path).open("rb") as attestation_file:
            content = attestation_file.read(MAX_CAPACITY_ATTESTATION_BYTES + 1)
        if not content or len(content) > MAX_CAPACITY_ATTESTATION_BYTES:
            raise StockDailyCapacityError
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise StockDailyCapacityError
        decoded = content.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(document, dict):
            raise StockDailyCapacityError
        return cast(
            "StockDailyCapacityAttestation",
            StockDailyCapacityAttestation.model_validate(document),
        )
    except StockDailyCapacityError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError, RecursionError):
        raise StockDailyCapacityError from None


def get_stock_daily_capacity_gate() -> StockDailyCapacityAttestation:
    """Return the active attestation or fail with the route's uniform 404.

    Required settings are read through the existing server-setting reader:

    - ``STOCK_DAILY_PUBLICATION_API_ENABLED`` must be the literal ``true``.
    - ``STOCK_DAILY_PUBLICATION_CAPACITY_FILE`` is an operator-owned path.
    - ``STOCK_DAILY_PUBLICATION_CAPACITY_SHA256`` is the 64-hex file digest.

    The file is re-read and verified per request. It is bounded to 64 KiB and
    no global state is cached, so tests and configuration changes cannot leak
    an attestation across app instances.
    """
    try:
        enabled = _server_setting("STOCK_DAILY_PUBLICATION_API_ENABLED")
        path = _server_setting("STOCK_DAILY_PUBLICATION_CAPACITY_FILE")
        expected_sha256 = _server_setting("STOCK_DAILY_PUBLICATION_CAPACITY_SHA256")
        if enabled != "true" or path is None or expected_sha256 is None:
            raise StockDailyCapacityError
        if not path or path != path.strip():
            raise StockDailyCapacityError
        attestation = load_stock_daily_capacity_attestation(path, expected_sha256)
        if not attestation.is_active():
            raise StockDailyCapacityError
        return attestation
    except Exception:
        raise _not_found() from None


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting duplicate keys at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON NaN and infinity literals."""
    raise ValueError(f"invalid JSON constant: {value}")


__all__ = [
    "MAX_CAPACITY_ATTESTATION_BYTES",
    "StockDailyCapacityAttestation",
    "StockDailyCapacityError",
    "get_stock_daily_capacity_gate",
    "load_stock_daily_capacity_attestation",
]
