"""Strict raw-candidate DTOs for identity-bound China stock daily v2.

The existing temporal identity assignment, exchange-qualified pair, and
operator pin models are reused directly. These DTOs retain source-native row
tokens and explicitly unknown identity gaps; they do not normalize prices,
authenticate pins, verify source rights or coverage, publish data, or make
qfq/hfq values consumable. An assignment on a raw row is only a caller claim;
canonicalization must resolve the row again against the verified registry
loaded for its operator pin before it may call that identity known.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from opendata.data.temporal_identity_pins import TemporalIdentityPin
from opendata.data.temporal_instrument_identity import (
    MarketSymbolPair,
    TemporalIdentityAssignment,
)

__all__ = [
    "IdentityBoundStockDailyV2RawCandidate",
    "IdentityBoundStockDailyV2RawRow",
    "IdentityBoundStockDailyV2Window",
    "IdentityBoundStockV2RawProvenance",
]

MAX_IDENTITY_BOUND_STOCK_V2_WINDOW_DAYS = 3_660
MAX_IDENTITY_BOUND_STOCK_V2_ROWS = 10_000
MAX_IDENTITY_BOUND_STOCK_V2_RAW_BYTES = 16 * 1024 * 1024
MAX_IDENTITY_BOUND_STOCK_V2_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_IDENTITY_BOUND_STOCK_V2_RAW_TOKENS_PER_ROW = 64
MAX_IDENTITY_BOUND_STOCK_V2_RAW_TOKEN_CHARS = 4_096

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_CAPTURE_ID_PATTERN = r"^[0-9a-f]{32}$"
_Identifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN),
]
_Sha256 = Annotated[str, StringConstraints(strict=True, pattern=_SHA256_PATTERN)]
_RawToken = Annotated[
    str,
    StringConstraints(strict=True, max_length=MAX_IDENTITY_BOUND_STOCK_V2_RAW_TOKEN_CHARS),
]


class IdentityBoundStockDailyV2Window(BaseModel):
    """One bounded, half-open calendar-date request window ``[start, end)``."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    start: date
    end_exclusive: date

    @model_validator(mode="after")
    def validate_window(self) -> IdentityBoundStockDailyV2Window:
        """Require a finite nonempty interval within the identity resolver bound."""
        if self.start >= self.end_exclusive:
            raise ValueError("window must be a nonempty [start, end_exclusive) interval")
        if (self.end_exclusive - self.start).days > MAX_IDENTITY_BOUND_STOCK_V2_WINDOW_DAYS:
            raise ValueError("window exceeds the 3660-calendar-day limit")
        return self


class IdentityBoundStockV2RawProvenance(BaseModel):
    """Bounded caller-declared source metadata for one retained raw response."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: _Identifier
    adapter_id: _Identifier
    product_id: _Identifier
    endpoint_id: _Identifier
    adapter_version: _Identifier
    capture_id: Annotated[str, StringConstraints(strict=True, pattern=_CAPTURE_ID_PATTERN)]
    request_sha256: _Sha256
    response_sha256: _Sha256
    response_size_bytes: StrictInt = Field(ge=0, le=MAX_IDENTITY_BOUND_STOCK_V2_RESPONSE_BYTES)
    captured_at_utc: datetime
    evidence_integrity_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    source_identity_status: Literal["unverified"] = "unverified"
    source_authorization_status: Literal["unknown"] = "unknown"
    retention_status: Literal["unknown"] = "unknown"
    adjustment_semantics: Literal["unknown"] = "unknown"
    price_unit_semantics: Literal["unknown"] = "unknown"
    volume_unit_semantics: Literal["unknown"] = "unknown"
    amount_unit_semantics: Literal["unknown"] = "unknown"
    history_coverage_status: Literal["unknown"] = "unknown"
    missing_date_interpretation: Literal["unknown"] = "unknown"

    @field_validator("captured_at_utc")
    @classmethod
    def require_utc_capture_time(cls, value: datetime) -> datetime:
        """Require an aware UTC timestamp and canonicalize its timezone."""
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("captured_at_utc must be timezone-aware UTC")
        return value.astimezone(timezone.utc)


class IdentityBoundStockDailyV2RawRow(BaseModel):
    """One unnormalized row with a declared assignment claim or unknown identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    trade_date: date
    source_row_index: StrictInt = Field(ge=1, le=MAX_IDENTITY_BOUND_STOCK_V2_ROWS)
    raw_tokens: tuple[_RawToken, ...] = Field(
        min_length=1, max_length=MAX_IDENTITY_BOUND_STOCK_V2_RAW_TOKENS_PER_ROW
    )
    assignment_claim_status: Literal["caller_declared_unverified", "unknown"]
    assignment: TemporalIdentityAssignment | None = None

    @field_validator("assignment", mode="before")
    @classmethod
    def require_validated_assignment(cls, value: object) -> TemporalIdentityAssignment | None:
        """Accept only an already validated immutable assignment or explicit null."""
        if value is None or type(value) is TemporalIdentityAssignment:
            return value
        raise ValueError("assignment must be a validated TemporalIdentityAssignment or null")

    @field_validator("raw_tokens")
    @classmethod
    def require_utf8_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require each retained token to be representable as exact UTF-8 text."""
        try:
            for token in value:
                token.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ValueError("raw tokens must contain valid UTF-8 scalar values") from None
        return value

    @model_validator(mode="after")
    def validate_identity_state(self) -> IdentityBoundStockDailyV2RawRow:
        """Prevent unknown rows carrying assignments or unverified claims being called known."""
        if self.assignment_claim_status == "unknown" and self.assignment is not None:
            raise ValueError("unknown identity rows cannot carry an assignment")
        if self.assignment_claim_status == "caller_declared_unverified" and self.assignment is None:
            raise ValueError("caller-declared assignment rows require an assignment")
        return self


class IdentityBoundStockDailyV2RawCandidate(BaseModel):
    """Identity-scoped source-native candidate; never a canonical or published bar set."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: Literal["cn_equity.daily.identity_bound.raw_candidate.v2"] = (
        "cn_equity.daily.identity_bound.raw_candidate.v2"
    )
    request_pair: MarketSymbolPair
    window: IdentityBoundStockDailyV2Window
    identity_pin: TemporalIdentityPin
    pin_provenance_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    requested_adjustment_mode: Literal["none"] = "none"
    candidate_status: Literal["raw_candidate_only"] = "raw_candidate_only"
    source_market_capability_status: Literal["unknown"] = "unknown"
    provenance: IdentityBoundStockV2RawProvenance
    rows: tuple[IdentityBoundStockDailyV2RawRow, ...] = Field(
        min_length=1, max_length=MAX_IDENTITY_BOUND_STOCK_V2_ROWS
    )

    @field_validator("request_pair", mode="before")
    @classmethod
    def require_explicit_pair(cls, value: object) -> MarketSymbolPair:
        """Require a validated explicit exchange-qualified pair, never a bare code."""
        if type(value) is MarketSymbolPair:
            return value
        raise ValueError("request_pair must be an explicit MarketSymbolPair")

    @field_validator("identity_pin", mode="before")
    @classmethod
    def require_validated_pin(cls, value: object) -> TemporalIdentityPin:
        """Require the existing strict pin model instead of coercing arbitrary input."""
        if type(value) is TemporalIdentityPin:
            return value
        raise ValueError("identity_pin must be a validated TemporalIdentityPin")

    @field_validator("provenance", mode="before")
    @classmethod
    def require_validated_provenance(cls, value: object) -> IdentityBoundStockV2RawProvenance:
        """Require source metadata already checked against the bounded provenance DTO."""
        if type(value) is IdentityBoundStockV2RawProvenance:
            return value
        raise ValueError("provenance must be a validated IdentityBoundStockV2RawProvenance")

    @field_validator("rows", mode="before")
    @classmethod
    def require_validated_rows(cls, value: object) -> tuple[IdentityBoundStockDailyV2RawRow, ...]:
        """Require a finite tuple of validated raw rows rather than implicit coercion."""
        if type(value) is not tuple or any(
            type(row) is not IdentityBoundStockDailyV2RawRow for row in value
        ):
            raise ValueError("rows must be a tuple of validated raw row DTOs")
        return value

    @model_validator(mode="after")
    def validate_candidate_binding(self) -> IdentityBoundStockDailyV2RawCandidate:
        """Bind rows to the request window, pair, and declared pin without trusting them."""
        if self.identity_pin.dataset_id != self.provenance.dataset_id:
            raise ValueError("identity pin dataset does not match raw provenance")
        if self.identity_pin.source_id != self.provenance.adapter_id:
            raise ValueError("identity pin source does not match raw adapter")

        seen_dates: set[date] = set()
        seen_indexes: set[int] = set()
        assignments_by_id: dict[str, TemporalIdentityAssignment] = {}
        assignments_by_pair: dict[MarketSymbolPair, list[TemporalIdentityAssignment]] = {}
        raw_bytes = 0
        for row in self.rows:
            if not self.window.start <= row.trade_date < self.window.end_exclusive:
                raise ValueError("raw row date is outside the half-open request window")
            if row.trade_date in seen_dates or row.source_row_index in seen_indexes:
                raise ValueError("raw candidate contains duplicate dates or row indexes")
            seen_dates.add(row.trade_date)
            seen_indexes.add(row.source_row_index)
            raw_bytes += sum(len(token.encode("utf-8")) for token in row.raw_tokens)

            assignment = row.assignment
            if assignment is not None:
                if assignment.pair != self.request_pair:
                    raise ValueError("row assignment pair differs from the explicit request pair")
                if assignment.source_id != self.identity_pin.source_id:
                    raise ValueError("row assignment source differs from the declared identity pin")
                if not assignment.valid_from <= row.trade_date or (
                    assignment.valid_to is not None and row.trade_date >= assignment.valid_to
                ):
                    raise ValueError("row date is outside the assignment half-open interval")
                prior_with_id = assignments_by_id.get(assignment.assignment_id)
                if prior_with_id is not None and prior_with_id != assignment:
                    raise ValueError("assignment_id is reused for conflicting assignment values")
                assignments_by_id[assignment.assignment_id] = assignment
                assignments_by_pair.setdefault(assignment.pair, []).append(assignment)

        for pair_assignments in assignments_by_pair.values():
            unique_assignments = tuple(set(pair_assignments))
            ordered_assignments = sorted(
                unique_assignments, key=lambda item: (item.valid_from, item.valid_to or date.max)
            )
            for previous, current in zip(
                ordered_assignments, ordered_assignments[1:], strict=False
            ):
                if previous.valid_to is None or current.valid_from < previous.valid_to:
                    raise ValueError("assignments for the same exchange-symbol pair overlap")

        if raw_bytes > MAX_IDENTITY_BOUND_STOCK_V2_RAW_BYTES:
            raise ValueError("raw candidate tokens exceed the 16 MiB UTF-8 byte budget")
        return self
