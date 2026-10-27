"""Immutable source-native observations for archived A-share statements.

This module defines a pure data contract. It does not fetch, persist, schedule,
or publish observations. The contract records what a source returned and when
OpenData actually observed it; a source disclosure date is never substituted
for a missing observation timestamp. Values use bounded exact decimals, while
the source's native unit and scalar metadata remain attached to each item.

The source adapters currently expose only the latest effective statement
values. Such a row is ``current_as_observed`` and cannot reconstruct an
unarchived historical point-in-time value. Repeated content is idempotent by
content digest; a changed value or source payload produces another observation
identity under the same natural key.

This contract does not retain the source's numeric token spelling or raw HTTP
response. The current THS financial normalizer emits Python floats and is not
wired to this model. An adapter must parse source numeric tokens directly as
``Decimal``, preserve any required raw response/number lexeme at its capture
boundary, and supply the required ``source_metadata``. That integration is not
implemented; this model does not prove that live THS precision is retained.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias
from zoneinfo import ZoneInfo

StatementType: TypeAlias = Literal["income", "balance", "cashflow"]
"""Canonical financial statement kind used by the Fuyao adapter."""

Periodicity: TypeAlias = Literal["annual", "quarterly"]
"""Source-requested statement cadence; quarterly includes the Q4 report."""

ObservationPolicy: TypeAlias = Literal["current_as_observed_only"]
"""Policy that forbids reconstructing pre-archive historical PIT values."""

FinancialJsonScalar: TypeAlias = str | int | Decimal | bool | None
"""Immutable JSON scalar preserved from one source row; floats are excluded."""

_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_INSTRUMENT_ID_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_DECIMAL_TEXT_PATTERN = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$")
_STATEMENT_TYPES = frozenset({"income", "balance", "cashflow"})
_PERIODICITIES = frozenset({"annual", "quarterly"})
_QUARTER_ENDS = frozenset({(3, 31), (6, 30), (9, 30), (12, 31)})
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_DECIMAL_DIGITS = 38
_MAX_DECIMAL_SCALE = 18
_MAX_DECIMAL_INTEGER_DIGITS = 20
_MAX_METADATA_FIELDS = 256
_MAX_METADATA_BYTES = 65_536
_MAX_METADATA_VALUE_BYTES = 8_192


@dataclass(frozen=True, slots=True)
class FinancialStatementObservation:
    """One source-native line item observed for an A-share reporting period.

    ``natural_key`` identifies the report item independent of its content.
    ``observation_id`` adds a deterministic digest of the content, so a later
    restatement is retained as a distinct observation. ``observed_at`` is the
    actual retrieval instant and is intentionally excluded from the content
    digest: rereading identical content does not manufacture a restatement.

    Args:
        instrument_id: Exchange-qualified six-digit A-share identifier.
        statement_type: ``income``, ``balance``, or ``cashflow``.
        periodicity: Source request cadence, ``annual`` or ``quarterly``.
        report_period: Exact fiscal period end, not a retrieval or disclosure date.
        source_disclosure_date: Source-reported calendar disclosure date.
        observed_at: Actual aware retrieval instant, normalized to UTC.
        source_id: Stable lowercase identifier for the data source.
        item_code: Source-native item identifier; no cross-source mapping is inferred.
        value: Exact finite decimal, decimal text, integer, or ``None`` for source null.
        native_unit: Unit as supplied or verified for this item, without conversion.
        source_metadata: Immutable scalar source fields retained with the observation.
        observation_policy: Fixed policy documenting the limits of PIT reconstruction.

    Raises:
        TypeError: If a field has an unsupported runtime type.
        ValueError: If identifiers, dates, numbers, units, or metadata are invalid.
    """

    instrument_id: str
    statement_type: StatementType
    periodicity: Periodicity
    report_period: date
    source_disclosure_date: date
    observed_at: datetime
    source_id: str
    item_code: str
    value: Decimal | int | str | None
    native_unit: str
    source_metadata: tuple[tuple[str, FinancialJsonScalar], ...]
    observation_policy: ObservationPolicy = "current_as_observed_only"

    def __post_init__(self) -> None:
        """Validate fields and normalize exact values and observation time."""
        if (
            type(self.instrument_id) is not str
            or _INSTRUMENT_ID_PATTERN.fullmatch(self.instrument_id) is None
        ):
            raise ValueError("instrument_id must be a six-digit SH, SZ, or BJ identifier")
        if type(self.statement_type) is not str or self.statement_type not in _STATEMENT_TYPES:
            raise ValueError("statement_type must be income, balance, or cashflow")
        if type(self.periodicity) is not str or self.periodicity not in _PERIODICITIES:
            raise ValueError("periodicity must be annual or quarterly")
        _validate_period_end(self.report_period, self.periodicity)
        if type(self.source_disclosure_date) is not date:
            raise TypeError("source_disclosure_date must be a date without a time")
        if self.source_disclosure_date < self.report_period:
            raise ValueError("source_disclosure_date cannot precede report_period")
        if type(self.observed_at) is not datetime:
            raise TypeError("observed_at must be a datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        observed_utc = self.observed_at.astimezone(timezone.utc)
        if self.source_disclosure_date > observed_utc.astimezone(_SHANGHAI).date():
            raise ValueError("source_disclosure_date cannot be later than observed_at")
        object.__setattr__(self, "observed_at", observed_utc)
        if type(self.source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None:
            raise ValueError("source_id must be a stable lowercase identifier")
        _validate_text_token(self.item_code, "item_code", max_length=256)
        _validate_text_token(self.native_unit, "native_unit", max_length=128)
        if self.observation_policy != "current_as_observed_only":
            raise ValueError("financial observations cannot claim unarchived historical PIT")
        decimal_value = _exact_decimal(self.value)
        object.__setattr__(self, "value", decimal_value)
        _validate_source_metadata(self.source_metadata)

    @property
    def natural_key(self) -> tuple[str, str, str, str, str, str]:
        """Return stable source/item identity, excluding mutable reported content."""
        return (
            self.source_id,
            self.instrument_id,
            self.statement_type,
            self.periodicity,
            self.report_period.isoformat(),
            self.item_code,
        )

    @property
    def content_sha256(self) -> str:
        """Return a canonical digest of source content, unit, and disclosure date."""
        content = {
            "instrument_id": self.instrument_id,
            "statement_type": self.statement_type,
            "periodicity": self.periodicity,
            "report_period": self.report_period.isoformat(),
            "source_disclosure_date": self.source_disclosure_date.isoformat(),
            "source_id": self.source_id,
            "item_code": self.item_code,
            "value": self.value,
            "native_unit": self.native_unit,
            "source_metadata": dict(self.source_metadata),
            "observation_policy": self.observation_policy,
        }
        return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()

    @property
    def observation_id(self) -> str:
        """Return an idempotent observation identity that changes with content."""
        identity = {"natural_key": self.natural_key, "content_sha256": self.content_sha256}
        return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()

    @property
    def source_metadata_json(self) -> str:
        """Return retained source metadata as deterministic compact JSON."""
        return _canonical_json(dict(self.source_metadata))


def _validate_period_end(value: date, periodicity: Periodicity) -> None:
    """Require an exact calendar period end for the declared cadence."""
    if type(value) is not date:
        raise TypeError("report_period must be a date without a time")
    if periodicity == "annual" and (value.month, value.day) != (12, 31):
        raise ValueError("annual report_period must end on December 31")
    if periodicity == "quarterly" and (value.month, value.day) not in _QUARTER_ENDS:
        raise ValueError("quarterly report_period must be a calendar quarter end")


def _validate_text_token(value: str, name: str, *, max_length: int) -> None:
    """Reject blank, padded, oversized, and control-containing identifiers."""
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip() or len(value) > max_length:
        raise ValueError(f"{name} must be a trimmed nonblank token of at most {max_length} chars")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} cannot contain control characters")


def _exact_decimal(value: object) -> Decimal | None:
    """Parse a bounded exact decimal without accepting binary floats or booleans."""
    if value is None:
        return None
    if type(value) is bool or isinstance(value, float):
        raise TypeError("value must be an exact Decimal, integer, decimal string, or None")
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is int:
        parsed = Decimal(value)
    elif type(value) is str:
        if len(value) > 128 or _DECIMAL_TEXT_PATTERN.fullmatch(value) is None:
            raise ValueError("value must be finite decimal text with at most 128 characters")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("value must be a valid finite decimal") from exc
    else:
        raise TypeError("value must be an exact Decimal, integer, decimal string, or None")
    if not parsed.is_finite():
        raise ValueError("value must be finite")
    if parsed.is_zero():
        return Decimal(0)

    sign, raw_digits, exponent = parsed.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("value must be a finite decimal")
    digits = list(raw_digits)
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    significant_digits = len(digits)
    fractional_digits = max(0, -exponent)
    integer_digits = max(0, significant_digits + exponent)
    if significant_digits > _MAX_DECIMAL_DIGITS:
        raise ValueError("value exceeds 38 significant decimal digits")
    if fractional_digits > _MAX_DECIMAL_SCALE:
        raise ValueError("value exceeds 18 fractional decimal places")
    if integer_digits > _MAX_DECIMAL_INTEGER_DIGITS:
        raise ValueError("value exceeds 20 integer decimal digits")
    return Decimal((sign, tuple(digits), exponent))


def _validate_source_metadata(
    metadata: tuple[tuple[str, FinancialJsonScalar], ...],
) -> None:
    """Validate bounded immutable source fields and their JSON-safe values."""
    if type(metadata) is not tuple:
        raise TypeError("source_metadata must be an immutable tuple of key/value pairs")
    if not metadata or len(metadata) > _MAX_METADATA_FIELDS:
        raise ValueError("source_metadata must contain between 1 and 256 fields")
    names: set[str] = set()
    for entry in metadata:
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError("source_metadata entries must be key/value tuples")
        key, value = entry
        _validate_text_token(key, "source_metadata key", max_length=128)
        if key in names:
            raise ValueError("source_metadata keys must be unique")
        names.add(key)
        if type(value) is float:
            raise TypeError("source_metadata numbers must use exact Decimal values, not floats")
        if isinstance(value, Decimal) or type(value) is int:
            _exact_decimal(value)
        elif type(value) is str:
            if len(value.encode("utf-8")) > _MAX_METADATA_VALUE_BYTES:
                raise ValueError("source_metadata text exceeds the 8192-byte limit")
        elif value is not None and type(value) is not bool:
            raise TypeError("source_metadata values must be JSON scalars")
    if len(_canonical_json(dict(metadata)).encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("source_metadata exceeds the 65536-byte limit")


def _canonical_json(value: object) -> str:
    """Serialize bounded JSON-compatible values with exact decimal number text."""
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if type(value) is int:
        decimal_value = _exact_decimal(value)
        if decimal_value is None:
            raise ValueError("integer metadata cannot be null")
        return _decimal_text(decimal_value)
    if isinstance(value, Decimal):
        decimal_value = _exact_decimal(value)
        if decimal_value is None:
            raise ValueError("decimal metadata cannot be null")
        return _decimal_text(decimal_value)
    if isinstance(value, tuple):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        pairs = (
            json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            + ":"
            + _canonical_json(value[key])
            for key in sorted(value)
        )
        return "{" + ",".join(pairs) + "}"
    raise TypeError("value is not JSON-compatible with the financial observation contract")


def _decimal_text(value: Decimal) -> str:
    """Render a finite bounded decimal in canonical non-exponent notation."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0", "+0"}:
        return "0"
    if rendered.startswith("+"):
        return rendered[1:]
    return rendered


__all__ = [
    "FinancialStatementObservation",
    "FinancialJsonScalar",
    "ObservationPolicy",
    "Periodicity",
    "StatementType",
]
