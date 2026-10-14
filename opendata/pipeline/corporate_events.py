"""Typed event-version records used by the corporate-action factor pipeline."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Literal

IdentityBasis = Literal["source_event_id", "reviewed_composite"]
PublicationPrecision = Literal["unknown", "date", "timestamp"]


@dataclass(frozen=True)
class CorporateEventIdentity:
    """An explicitly assigned event identity, separate from payload hashes."""

    source: str
    event_identity: str
    identity_basis: IdentityBasis
    symbol: str
    ex_date: date
    economic_event_id: str | None = None

    def __post_init__(self) -> None:
        """Validate the explicit source-level event identity."""
        _required_text(self.source, "source")
        _required_text(self.event_identity, "event_identity")
        _required_text(self.symbol, "symbol")
        if self.identity_basis not in ("source_event_id", "reviewed_composite"):
            raise ValueError("identity_basis must be source_event_id or reviewed_composite")
        if self.economic_event_id is not None:
            _required_text(self.economic_event_id, "economic_event_id")
        if not isinstance(self.ex_date, date) or isinstance(self.ex_date, datetime):
            raise ValueError("ex_date must be a date")


@dataclass(frozen=True)
class CorporateEventVersion:
    """One immutable, typed revision of a corporate-action event."""

    identity: CorporateEventIdentity
    source_revision: str
    revision_order: int
    observed_at: datetime
    cash_dividend: float = 0.0
    stock_dividend: float = 0.0
    rights_shares: float = 0.0
    rights_price: float = 0.0
    pre_event_share_basis: str | None = None
    published_date: date | None = None
    published_timestamp: datetime | None = None
    publication_precision: PublicationPrecision = "unknown"
    source_timezone: str | None = None

    def __post_init__(self) -> None:
        """Validate revision, publication precision, and action values."""
        _required_text(self.source_revision, "source_revision")
        if type(self.revision_order) is not int or self.revision_order < 0:
            raise ValueError("revision_order must be a non-negative integer")
        for name, value in (
            ("cash_dividend", self.cash_dividend),
            ("stock_dividend", self.stock_dividend),
            ("rights_shares", self.rights_shares),
            ("rights_price", self.rights_price),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.pre_event_share_basis is not None:
            _required_text(self.pre_event_share_basis, "pre_event_share_basis")
        if self.publication_precision not in ("unknown", "date", "timestamp"):
            raise ValueError("invalid publication_precision")
        if self.publication_precision == "unknown":
            if self.published_date is not None or self.published_timestamp is not None:
                raise ValueError("unknown publication precision cannot carry a published value")
        elif self.publication_precision == "date":
            if (
                not isinstance(self.published_date, date)
                or isinstance(self.published_date, datetime)
                or self.published_timestamp is not None
            ):
                raise ValueError("date precision requires published_date only")
            if not self.source_timezone:
                raise ValueError("date precision requires source_timezone")
        else:
            if (
                not isinstance(self.published_timestamp, datetime)
                or self.published_date is not None
            ):
                raise ValueError("timestamp precision requires published_timestamp only")
            if (
                self.published_timestamp.tzinfo is None
                or self.published_timestamp.utcoffset() is None
            ):
                raise ValueError("published_timestamp must be timezone-aware")
            if not self.source_timezone:
                raise ValueError("timestamp precision requires source_timezone")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be timezone-aware")
        if self.source_timezone is not None:
            _required_text(self.source_timezone, "source_timezone")

    def content_payload(self) -> dict[str, str | float | None]:
        """Return the normalized content fields hashed for this version."""
        published_timestamp = self.published_timestamp
        return {
            "symbol": self.identity.symbol,
            "ex_date": self.identity.ex_date.isoformat(),
            "identity_basis": self.identity.identity_basis,
            "cash_dividend": float(self.cash_dividend),
            "stock_dividend": float(self.stock_dividend),
            "rights_shares": float(self.rights_shares),
            "rights_price": float(self.rights_price),
            "pre_event_share_basis": self.pre_event_share_basis,
            "economic_event_id": self.identity.economic_event_id,
            "published_date": (
                None if self.published_date is None else self.published_date.isoformat()
            ),
            "published_timestamp": (
                None
                if published_timestamp is None
                else published_timestamp.astimezone(timezone.utc).isoformat()
            ),
            "publication_precision": self.publication_precision,
            "source_timezone": self.source_timezone,
        }

    @property
    def content_digest(self) -> str:
        """SHA-256 over normalized content, never used as event identity."""
        encoded = json.dumps(
            self.content_payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawCorporateEventObservation:
    """Raw evidence whose economic identity is not yet known.

    ``raw_payload_json`` is retained byte-for-byte as UTF-8 text. Its digest
    is only a content fingerprint, and ``observation_id`` assigned by the
    store is only a storage-record key; neither is an event identity.
    """

    source: str
    raw_payload_json: str
    observed_at: datetime
    symbol: str | None = None
    ex_date: date | None = None

    def __post_init__(self) -> None:
        """Validate and preserve unresolved raw source evidence."""
        _required_text(self.source, "source")
        if not isinstance(self.raw_payload_json, str) or not self.raw_payload_json:
            raise ValueError("raw_payload_json must be non-empty JSON text")
        try:
            payload = json.loads(self.raw_payload_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("raw_payload_json must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("raw_payload_json must be a JSON object")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be timezone-aware")
        if self.symbol is not None:
            _required_text(self.symbol, "symbol")
        if self.ex_date is not None and (
            not isinstance(self.ex_date, date) or isinstance(self.ex_date, datetime)
        ):
            raise ValueError("ex_date must be a date")

    @property
    def content_digest(self) -> str:
        """Digest the exact raw JSON bytes without promoting it to identity."""
        return hashlib.sha256(self.raw_payload_json.encode("utf-8")).hexdigest()


def _required_text(value: str, name: str) -> None:
    """Require a non-empty, already-trimmed text identifier."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")


__all__ = [
    "CorporateEventIdentity",
    "CorporateEventVersion",
    "IdentityBasis",
    "PublicationPrecision",
    "RawCorporateEventObservation",
]
