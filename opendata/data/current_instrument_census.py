"""Report differences between two caller-provided instrument identity sets.

This module is a bounded, immutable comparison utility. It does not acquire or
verify source data, determine whether inputs are comparable, or establish
pagination, freshness, asset scope, or full-market coverage.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from itertools import islice

__all__ = [
    "CensusAcquisitionStatus",
    "CensusComparison",
    "CensusSnapshot",
    "CrossExchangeCollision",
    "InstrumentCensusError",
    "InstrumentIdentity",
    "compare_census_snapshots",
]

_ALLOWED_EXCHANGES = frozenset({"SH", "SZ", "BJ"})
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_IDENTITIES = 100_000
_MAX_SCOPE_EXCHANGES = 3


class InstrumentCensusError(ValueError):
    """Raised when an input is malformed, ambiguous, or exceeds its bound."""


class CensusAcquisitionStatus(str, Enum):
    """Caller-reported attempt state, separate from dataset coverage."""

    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, order=True)
class InstrumentIdentity:
    """One explicitly exchange-qualified six-digit China stock identity.

    The exchange must be supplied. It is never inferred from the symbol prefix,
    because bare six-digit codes can collide across exchange namespaces.
    """

    exchange: str
    symbol: str

    def __post_init__(self) -> None:
        """Reject unknown exchanges and symbols without normalizing input."""
        if type(self.exchange) is not str or self.exchange not in _ALLOWED_EXCHANGES:
            raise InstrumentCensusError("exchange must be explicitly SH, SZ, or BJ")
        if (
            type(self.symbol) is not str
            or len(self.symbol) != 6
            or not self.symbol.isascii()
            or not self.symbol.isdigit()
        ):
            raise InstrumentCensusError("symbol must be exactly six ASCII decimal digits")


@dataclass(frozen=True, slots=True)
class CensusSnapshot:
    """Immutable, bounded identity set and its caller-supplied context.

    Iterable inputs are copied into sorted tuples at construction, so later
    mutations to caller-owned lists cannot change the snapshot. The source ID,
    exchange scope, acquisition status, and observation time are supplied
    metadata. Their presence does not verify source authenticity, pagination,
    date or asset alignment, freshness, or market coverage.
    """

    source_id: str
    exchanges: tuple[str, ...]
    identities: tuple[InstrumentIdentity, ...]
    acquisition_status: CensusAcquisitionStatus | str
    observed_at: datetime

    def __post_init__(self) -> None:
        """Validate and freeze source, scope, status, time, and identity rows."""
        if type(self.source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None:
            raise InstrumentCensusError(
                "source_id must be a lowercase stable identifier of at most 64 characters"
            )

        scope = _bounded_values(self.exchanges, "exchanges", _MAX_SCOPE_EXCHANGES)
        if not scope:
            raise InstrumentCensusError("exchanges must explicitly scope at least one exchange")
        scope_values: list[str] = []
        for exchange in scope:
            if type(exchange) is not str or exchange not in _ALLOWED_EXCHANGES:
                raise InstrumentCensusError("exchanges must contain only SH, SZ, or BJ")
            scope_values.append(exchange)
        if len(scope) != len(set(scope)):
            raise InstrumentCensusError("exchanges must not contain duplicates")
        declared_scope = tuple(sorted(scope_values))
        object.__setattr__(self, "exchanges", declared_scope)

        try:
            status = CensusAcquisitionStatus(self.acquisition_status)
        except (TypeError, ValueError):
            raise InstrumentCensusError(
                "acquisition_status must be succeeded, partial, or failed"
            ) from None
        object.__setattr__(self, "acquisition_status", status)

        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InstrumentCensusError("observed_at must be a timezone-aware datetime")
        if self.observed_at.utcoffset() is None:
            raise InstrumentCensusError("observed_at must be a timezone-aware datetime")

        rows = _bounded_values(self.identities, "identities", _MAX_IDENTITIES)
        if any(not isinstance(row, InstrumentIdentity) for row in rows):
            raise InstrumentCensusError("identities must contain InstrumentIdentity values")
        instruments = tuple(row for row in rows if isinstance(row, InstrumentIdentity))
        if any(row.exchange not in declared_scope for row in instruments):
            raise InstrumentCensusError("every identity must belong to the declared exchange scope")
        if len(instruments) != len(set(instruments)):
            raise InstrumentCensusError("identities must not contain duplicates")
        object.__setattr__(self, "identities", tuple(sorted(instruments)))


@dataclass(frozen=True, slots=True, order=True)
class CrossExchangeCollision:
    """A bare symbol represented on multiple exchanges across both input sets."""

    symbol: str
    reference_exchanges: tuple[str, ...]
    candidate_exchanges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CensusComparison:
    """Identity-set differences and unchanged caller-supplied metadata.

    This result does not judge whether the snapshots are comparable, fresh,
    fully paginated, source-authentic, date-aligned, or complete for a market.
    """

    reference_source_id: str
    candidate_source_id: str
    reference_exchanges: tuple[str, ...]
    candidate_exchanges: tuple[str, ...]
    reference_status: CensusAcquisitionStatus
    candidate_status: CensusAcquisitionStatus
    reference_observed_at: datetime
    candidate_observed_at: datetime
    missing_from_candidate: tuple[InstrumentIdentity, ...]
    extra_in_candidate: tuple[InstrumentIdentity, ...]
    cross_exchange_collisions: tuple[CrossExchangeCollision, ...]


def compare_census_snapshots(
    reference: CensusSnapshot, candidate: CensusSnapshot
) -> CensusComparison:
    """Report identity-set differences for two caller-provided snapshots.

    The comparison does not require matching sources, exchange scopes,
    acquisition states, or observation times. Those fields are returned as
    supplied context. No comparability, pagination, source authenticity, date
    alignment, asset scope, freshness, or market-coverage conclusion is made.

    Args:
        reference: First identity set; the name does not establish authority.
        candidate: Second identity set.

    Returns:
        Stable, identity-sorted missing, extra, and bare-code collision rows,
        together with both snapshots' source, scope, state, and time metadata.

    Raises:
        InstrumentCensusError: If either input is not a validated snapshot.
    """
    if not isinstance(reference, CensusSnapshot) or not isinstance(candidate, CensusSnapshot):
        raise InstrumentCensusError("both inputs must be CensusSnapshot values")

    reference_ids = set(reference.identities)
    candidate_ids = set(candidate.identities)
    missing = tuple(sorted(reference_ids - candidate_ids))
    extra = tuple(sorted(candidate_ids - reference_ids))
    collisions = _cross_exchange_collisions(reference.identities, candidate.identities)
    return CensusComparison(
        reference_source_id=reference.source_id,
        candidate_source_id=candidate.source_id,
        reference_exchanges=reference.exchanges,
        candidate_exchanges=candidate.exchanges,
        reference_status=CensusAcquisitionStatus(reference.acquisition_status),
        candidate_status=CensusAcquisitionStatus(candidate.acquisition_status),
        reference_observed_at=reference.observed_at,
        candidate_observed_at=candidate.observed_at,
        missing_from_candidate=missing,
        extra_in_candidate=extra,
        cross_exchange_collisions=collisions,
    )


def _cross_exchange_collisions(
    reference: tuple[InstrumentIdentity, ...], candidate: tuple[InstrumentIdentity, ...]
) -> tuple[CrossExchangeCollision, ...]:
    """Return bare codes present on multiple exchanges in the combined inputs."""
    reference_by_symbol = _exchanges_by_symbol(reference)
    candidate_by_symbol = _exchanges_by_symbol(candidate)
    collisions: list[CrossExchangeCollision] = []
    for symbol in sorted(reference_by_symbol.keys() | candidate_by_symbol.keys()):
        reference_exchanges = reference_by_symbol.get(symbol, ())
        candidate_exchanges = candidate_by_symbol.get(symbol, ())
        if len(set(reference_exchanges) | set(candidate_exchanges)) > 1:
            collisions.append(
                CrossExchangeCollision(
                    symbol=symbol,
                    reference_exchanges=reference_exchanges,
                    candidate_exchanges=candidate_exchanges,
                )
            )
    return tuple(collisions)


def _exchanges_by_symbol(
    identities: tuple[InstrumentIdentity, ...],
) -> dict[str, tuple[str, ...]]:
    """Group sorted, validated identities by their bare six-digit symbols."""
    grouped: dict[str, set[str]] = {}
    for identity in identities:
        grouped.setdefault(identity.symbol, set()).add(identity.exchange)
    return {symbol: tuple(sorted(exchanges)) for symbol, exchanges in grouped.items()}


def _bounded_values(values: Iterable[object], name: str, limit: int) -> tuple[object, ...]:
    """Materialize a caller iterable once and reject scalar or oversized input."""
    if isinstance(values, (str, bytes, bytearray, dict)) or not isinstance(values, Iterable):
        raise InstrumentCensusError(f"{name} must be a bounded iterable")
    try:
        items = tuple(islice(iter(values), limit + 1))
    except TypeError:
        raise InstrumentCensusError(f"{name} must be a bounded iterable") from None
    if len(items) > limit:
        raise InstrumentCensusError(f"{name} exceeds the {limit:,}-item limit")
    return items
