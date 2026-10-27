"""Evidence-bound planning for all-A-share financial statement recovery.

This module consumes an explicit, reviewed security manifest and upstream
capability evidence. It does not discover securities, read a database, contact
a provider, create :class:`RunRequest` objects, submit work, or certify stored
history. A member snapshot alone is never treated as a complete A-share
universe. Fuyao returns its currently effective statement version; disclosure
dates do not recreate historical point-in-time (PIT) revisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Final, TypeVar
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Iterable

PLAN_VERSION: Final[str] = "financial-backfill-manifest-v2"
ALL_A_SHARE_SCOPE_ID: Final[str] = "cn-a-share-equity"
MAX_FINANCIAL_WINDOW_SPAN_DAYS: Final[int] = 365 * 10
MAX_SECURITY_ROWS: Final[int] = 100_000
_EVIDENCE_REF_MAX_CHARS: Final[int] = 512
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_TICKER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{6}$")
_SOURCE_SYMBOL_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class Exchange(str, Enum):
    """Mainland China A-share exchanges in the required universe scope."""

    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"


_EXCHANGE_SUFFIX: Final[dict[Exchange, str]] = {
    Exchange.SSE: "SH",
    Exchange.SZSE: "SZ",
    Exchange.BSE: "BJ",
}
ALL_A_SHARE_EXCHANGES: Final[frozenset[Exchange]] = frozenset(Exchange)
_EnumMember = TypeVar("_EnumMember", bound=Enum)


class LifecycleStatus(str, Enum):
    """Evidence-backed lifecycle state of one security."""

    ACTIVE = "active"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class UniverseEvidenceKind(str, Enum):
    """How the caller claims a universe member set was assembled.

    This is descriptive input only; the planner does not verify source artifacts,
    signatures, or the truth of a claimed review.
    """

    REVIEWED_SECURITY_MASTER = "reviewed_security_master"
    SOURCE_SNAPSHOT = "source_snapshot"


class StatementType(str, Enum):
    """Fuyao statement endpoints represented by the current contract."""

    INCOME = "income"
    BALANCE = "balance"
    CASHFLOW = "cashflow"


class ReportingPeriod(str, Enum):
    """Fuyao financial statement query periods."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class FinancialPlanBlockReason(str, Enum):
    """Reasons why the evidence does not support a complete recovery claim."""

    SNAPSHOT_CANNOT_PROVE_UNIVERSE = "snapshot_cannot_prove_universe"
    UNIVERSE_SCOPE_ID_MISMATCH = "universe_scope_id_mismatch"
    UNIVERSE_MEMBER_COUNT_MISMATCH = "universe_member_count_mismatch"
    UNIVERSE_MEMBER_DIGEST_MISMATCH = "universe_member_digest_mismatch"
    UNIVERSE_EXCHANGE_SCOPE_MISMATCH = "universe_exchange_scope_mismatch"
    UNIVERSE_EXCHANGE_MEMBERS_MISSING = "universe_exchange_members_missing"
    UNIVERSE_DELISTED_SECURITIES_OMITTED = "universe_delisted_securities_omitted"
    UNIVERSE_LISTING_BOUNDARIES_OMITTED = "universe_listing_boundaries_omitted"
    LIFECYCLE_STATUS_UNKNOWN = "lifecycle_status_unknown"
    LIFECYCLE_STATUS_STALE = "lifecycle_status_stale"
    LIFECYCLE_EVIDENCE_AFTER_AS_OF = "lifecycle_evidence_after_as_of"
    LISTING_BOUNDARY_UNKNOWN = "listing_boundary_unknown"
    LISTING_BOUNDARY_AFTER_AS_OF = "listing_boundary_after_as_of"
    SOURCE_SYMBOL_MAPPING_MISSING = "source_symbol_mapping_missing"
    SOURCE_SYMBOL_EXCHANGE_MISMATCH = "source_symbol_exchange_mismatch"
    EXCHANGE_CAPABILITY_UNVERIFIED = "exchange_capability_unverified"
    STATEMENT_CAPABILITY_UNVERIFIED = "statement_capability_unverified"
    PERIOD_CAPABILITY_UNVERIFIED = "period_capability_unverified"
    SOURCE_HISTORY_BOUNDARY_UNKNOWN = "source_history_boundary_unknown"
    SOURCE_HISTORY_GAP = "source_history_gap"
    NO_SOURCE_WINDOW = "no_source_window"
    LIFECYCLE_BOUNDARY_AFTER_AS_OF = "lifecycle_boundary_after_as_of"


class FinancialManifestError(ValueError):
    """Raised when manifest or source evidence is internally inconsistent."""


def _identifier(value: str, name: str) -> str:
    """Validate a bounded stable identifier without silently normalizing it."""
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise FinancialManifestError(f"{name} must be a 1-128 character stable identifier")
    return value


def _evidence_ref(value: str | None, name: str, *, required: bool = True) -> str | None:
    """Validate a bounded reference to a separately retained evidence artifact."""
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _EVIDENCE_REF_MAX_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        raise FinancialManifestError(f"{name} must be a bounded nonempty evidence reference")
    return value


def _strict_date(value: date | None, name: str) -> date | None:
    """Accept an optional date while rejecting datetimes and implicit coercion."""
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise FinancialManifestError(f"{name} must be a datetime.date")
    return value


def _required_date(value: object, name: str) -> date:
    """Accept a required date while rejecting datetimes and implicit coercion."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise FinancialManifestError(f"{name} must be a datetime.date")
    return value


def _aware_utc(value: datetime, name: str) -> datetime:
    """Require an aware timestamp and normalize equivalent instants to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FinancialManifestError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _enum_set(
    values: Iterable[object], enum_type: type[_EnumMember], name: str
) -> frozenset[_EnumMember]:
    """Materialize a bounded unique enum collection, rejecting string pseudo-iterables."""
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise FinancialManifestError(f"{name} must be an iterable of enum values")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise FinancialManifestError(f"{name} must be an iterable of enum values") from exc
    if len(materialized) > 16:
        raise FinancialManifestError(f"{name} contains too many values")
    normalized: list[_EnumMember] = []
    for value in materialized:
        try:
            member = enum_type(value)
        except (TypeError, ValueError) as exc:
            raise FinancialManifestError(f"{name} contains an unsupported value") from exc
        if member in normalized:
            raise FinancialManifestError(f"{name} must not contain duplicates")
        normalized.append(member)
    return frozenset(normalized)


@dataclass(frozen=True, slots=True)
class FinancialSecurityEvidence:
    """One explicit security row with independently sourced identity boundaries.

    Unknown listing boundaries and provider symbols are representable so the
    plan can retain the row and attach blockers. They never become guessed
    dates, aliases, or executable requests.
    """

    ticker: str
    exchange: Exchange | str
    listing_date: date | None
    listing_evidence_ref: str | None
    lifecycle_status: LifecycleStatus | str
    lifecycle_as_of: date
    lifecycle_evidence_ref: str
    delisted_on: date | None = None
    delisting_evidence_ref: str | None = None
    source_symbol: str | None = None
    source_symbol_evidence_ref: str | None = None
    source_history_boundary_unknown: bool = True
    source_history_available_from: date | None = None
    source_history_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate ticker, exchange, lifecycle, dates, and evidence references."""
        if not isinstance(self.ticker, str) or not _TICKER_PATTERN.fullmatch(self.ticker):
            raise FinancialManifestError("ticker must be exactly six ASCII digits")
        try:
            exchange = Exchange(self.exchange)
            status = LifecycleStatus(self.lifecycle_status)
        except (TypeError, ValueError) as exc:
            raise FinancialManifestError("exchange or lifecycle_status is unsupported") from exc
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "lifecycle_status", status)
        listing_date = _strict_date(self.listing_date, "listing_date")
        lifecycle_as_of = _required_date(self.lifecycle_as_of, "lifecycle_as_of")
        delisted_on = _strict_date(self.delisted_on, "delisted_on")
        object.__setattr__(self, "listing_date", listing_date)
        object.__setattr__(self, "lifecycle_as_of", lifecycle_as_of)
        object.__setattr__(self, "delisted_on", delisted_on)
        _evidence_ref(self.lifecycle_evidence_ref, "lifecycle_evidence_ref")

        if listing_date is None:
            _evidence_ref(self.listing_evidence_ref, "listing_evidence_ref", required=False)
        else:
            _evidence_ref(self.listing_evidence_ref, "listing_evidence_ref")
        if delisted_on is not None and listing_date is not None and listing_date > delisted_on:
            raise FinancialManifestError("listing_date cannot follow delisted_on")
        if status is LifecycleStatus.DELISTED:
            if delisted_on is None:
                raise FinancialManifestError("delisted securities require delisted_on")
            _evidence_ref(self.delisting_evidence_ref, "delisting_evidence_ref")
            if lifecycle_as_of < delisted_on:
                raise FinancialManifestError("lifecycle_as_of cannot precede delisted_on")
        elif delisted_on is not None or self.delisting_evidence_ref is not None:
            raise FinancialManifestError("delisting evidence is only valid for delisted securities")

        if self.source_symbol is None:
            _evidence_ref(
                self.source_symbol_evidence_ref,
                "source_symbol_evidence_ref",
                required=False,
            )
        else:
            if not isinstance(self.source_symbol, str) or not _SOURCE_SYMBOL_PATTERN.fullmatch(
                self.source_symbol
            ):
                raise FinancialManifestError("source_symbol must use a qualified six-digit code")
            if self.source_symbol.partition(".")[0] != self.ticker:
                raise FinancialManifestError("source_symbol code must match ticker")
            _evidence_ref(self.source_symbol_evidence_ref, "source_symbol_evidence_ref")
        if type(self.source_history_boundary_unknown) is not bool:
            raise FinancialManifestError("source_history_boundary_unknown must be a bool")
        source_history_start = _strict_date(
            self.source_history_available_from, "source_history_available_from"
        )
        object.__setattr__(self, "source_history_available_from", source_history_start)
        if self.source_history_boundary_unknown:
            if source_history_start is not None or self.source_history_evidence_ref is not None:
                raise FinancialManifestError(
                    "unknown security history boundary cannot carry a date or evidence"
                )
        else:
            if source_history_start is None:
                raise FinancialManifestError("known security history boundary requires a date")
            _evidence_ref(self.source_history_evidence_ref, "source_history_evidence_ref")

    @property
    def instrument_id(self) -> str:
        """Return the canonical exchange-qualified A-share identity."""
        return f"cn-equity:{Exchange(self.exchange).value}:{self.ticker}"


@dataclass(frozen=True, slots=True)
class FinancialUniverseEvidence:
    """Caller-supplied claim binding explicit rows to a universe scope.

    ``members_sha256`` binds the complete serialized rows; it is a fingerprint,
    not a signature. ``evidence_ref`` is retained as a reference only; this
    in-memory planner does not load, verify, or authenticate that external
    evidence.
    """

    scope_id: str
    registry_version: str
    evidence_ref: str
    evidence_kind: UniverseEvidenceKind | str
    as_of: date
    expected_member_count: int
    members_sha256: str
    exchanges: Iterable[Exchange | str]
    includes_delisted: bool
    includes_listing_boundaries: bool

    def __post_init__(self) -> None:
        """Validate the claimed all-A-share scope and member-set fingerprint."""
        object.__setattr__(self, "scope_id", _identifier(self.scope_id, "scope_id"))
        object.__setattr__(
            self, "registry_version", _identifier(self.registry_version, "registry_version")
        )
        _evidence_ref(self.evidence_ref, "evidence_ref")
        try:
            evidence_kind = UniverseEvidenceKind(self.evidence_kind)
        except (TypeError, ValueError) as exc:
            raise FinancialManifestError("evidence_kind is unsupported") from exc
        object.__setattr__(self, "evidence_kind", evidence_kind)
        as_of = _required_date(self.as_of, "as_of")
        object.__setattr__(self, "as_of", as_of)
        if type(self.expected_member_count) is not int or self.expected_member_count <= 0:
            raise FinancialManifestError("expected_member_count must be a positive integer")
        if self.expected_member_count > MAX_SECURITY_ROWS:
            raise FinancialManifestError("expected_member_count exceeds the manifest row limit")
        if not isinstance(self.members_sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.members_sha256
        ):
            raise FinancialManifestError("members_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(
            self,
            "exchanges",
            _enum_set(self.exchanges, Exchange, "exchanges"),
        )
        if (
            type(self.includes_delisted) is not bool
            or type(self.includes_listing_boundaries) is not bool
        ):
            raise FinancialManifestError("universe inclusion claims must be booleans")


@dataclass(frozen=True, slots=True)
class FinancialSourceCapabilityEvidence:
    """Observed source limits and scope; absent scope becomes a plan blocker."""

    source_id: str
    version: str
    evidence_ref: str
    observed_at: datetime
    max_window_span_days: int
    supported_exchanges: Iterable[Exchange | str]
    supported_statements: Iterable[StatementType | str]
    supported_periods: Iterable[ReportingPeriod | str]
    history_boundary_unknown: bool = True
    history_available_from: date | None = None
    history_boundary_evidence_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate observed source capabilities and enforce the measured 10-year cap."""
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source_id"))
        object.__setattr__(self, "version", _identifier(self.version, "version"))
        _evidence_ref(self.evidence_ref, "evidence_ref")
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        if (
            type(self.max_window_span_days) is not int
            or not 1 <= self.max_window_span_days <= MAX_FINANCIAL_WINDOW_SPAN_DAYS
        ):
            raise FinancialManifestError(
                f"max_window_span_days must be 1..{MAX_FINANCIAL_WINDOW_SPAN_DAYS}"
            )
        object.__setattr__(
            self,
            "supported_exchanges",
            _enum_set(self.supported_exchanges, Exchange, "supported_exchanges"),
        )
        object.__setattr__(
            self,
            "supported_statements",
            _enum_set(self.supported_statements, StatementType, "supported_statements"),
        )
        object.__setattr__(
            self,
            "supported_periods",
            _enum_set(self.supported_periods, ReportingPeriod, "supported_periods"),
        )
        if type(self.history_boundary_unknown) is not bool:
            raise FinancialManifestError("history_boundary_unknown must be a bool")
        history_available_from = _strict_date(self.history_available_from, "history_available_from")
        object.__setattr__(self, "history_available_from", history_available_from)
        if self.history_boundary_unknown:
            if history_available_from is not None or self.history_boundary_evidence_ref is not None:
                raise FinancialManifestError(
                    "unknown history boundary cannot carry a date or boundary evidence"
                )
        else:
            if history_available_from is None:
                raise FinancialManifestError("known history boundary requires a date")
            _evidence_ref(self.history_boundary_evidence_ref, "history_boundary_evidence_ref")


@dataclass(frozen=True, slots=True)
class FinancialSecurityManifest:
    """Immutable, explicit instrument rows paired with a universe attestation."""

    evidence: FinancialUniverseEvidence
    securities: Iterable[FinancialSecurityEvidence]

    def __post_init__(self) -> None:
        """Freeze bounded rows and reject duplicate canonical securities."""
        if type(self.evidence) is not FinancialUniverseEvidence:
            raise FinancialManifestError("evidence must be FinancialUniverseEvidence")
        if isinstance(self.securities, (str, bytes, bytearray, dict)):
            raise FinancialManifestError("securities must be an iterable of explicit rows")
        try:
            rows = tuple(self.securities)
        except TypeError as exc:
            raise FinancialManifestError("securities must be an iterable of explicit rows") from exc
        if not rows or len(rows) > MAX_SECURITY_ROWS:
            raise FinancialManifestError(f"securities must contain 1..{MAX_SECURITY_ROWS} rows")
        if any(type(row) is not FinancialSecurityEvidence for row in rows):
            raise FinancialManifestError("securities must contain FinancialSecurityEvidence rows")
        identities = [row.instrument_id for row in rows]
        if len(identities) != len(set(identities)):
            raise FinancialManifestError(
                "manifest contains duplicate exchange-qualified securities"
            )
        object.__setattr__(self, "securities", rows)


@dataclass(frozen=True, slots=True)
class FinancialPlanBlocker:
    """One evidence or capability blocker preserved in a plan assessment."""

    reason: FinancialPlanBlockReason
    detail: str
    instrument_id: str | None = None
    statement_type: StatementType | None = None
    reporting_period: ReportingPeriod | None = None
    range_start: date | None = None
    range_end: date | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FinancialBackfillCandidate:
    """Non-executable candidate window; it is deliberately not a RunRequest."""

    stable_id: str
    instrument_id: str
    ticker: str
    exchange: Exchange
    source_symbol: str
    statement_type: StatementType
    reporting_period: ReportingPeriod
    window_start: date
    window_end: date
    manifest_sha256: str
    source_capability_sha256: str
    evidence_refs: tuple[str, ...]
    dispatch_authorized: bool = field(default=False, init=False)

    @property
    def window_span_days(self) -> int:
        """Return the endpoint's inclusive-date span measured as end minus start."""
        return (self.window_end - self.window_start).days


@dataclass(frozen=True, slots=True)
class FinancialSeriesAssessment:
    """Plan output for one security × statement × reporting-period combination."""

    instrument_id: str
    statement_type: StatementType
    reporting_period: ReportingPeriod
    candidates: tuple[FinancialBackfillCandidate, ...]
    blockers: tuple[FinancialPlanBlocker, ...]


@dataclass(frozen=True, slots=True)
class FinancialBackfillPlan:
    """Immutable plan with claim consistency and explicit evidence limitations.

    A consistent caller claim is not independent verification. The planner
    always reports that external evidence and the resulting all-A-share scope
    remain unverified.
    """

    plan_version: str
    source_id: str
    source_version: str
    as_of: date
    planned_observation_at: datetime
    manifest_sha256: str
    source_capability_sha256: str
    member_set_claim_consistent: bool
    member_set_independently_verified: bool = field(default=False, init=False)
    lifecycle_boundaries_complete: bool
    lifecycle_boundary_blockers: tuple[FinancialPlanBlocker, ...]
    all_a_share_universe_verified: bool = field(default=False, init=False)
    historical_pit_supported: bool = field(default=False, init=False)
    revision_history_available: bool = field(default=False, init=False)
    announce_date_is_observation_time: bool = field(default=False, init=False)
    history_reconciled: bool = field(default=False, init=False)
    execution_authorized: bool = field(default=False, init=False)
    max_window_span_days: int
    universe_blockers: tuple[FinancialPlanBlocker, ...]
    series: tuple[FinancialSeriesAssessment, ...]

    @property
    def candidates(self) -> tuple[FinancialBackfillCandidate, ...]:
        """Flatten the deterministic candidate windows without creating requests."""
        return tuple(candidate for item in self.series for candidate in item.candidates)


def _security_payload(security: FinancialSecurityEvidence) -> dict[str, object]:
    """Return the canonical evidence-bound row used in manifest hashing."""
    return {
        "ticker": security.ticker,
        "exchange": Exchange(security.exchange).value,
        "listing_date": security.listing_date.isoformat() if security.listing_date else None,
        "listing_evidence_ref": security.listing_evidence_ref,
        "lifecycle_status": LifecycleStatus(security.lifecycle_status).value,
        "lifecycle_as_of": security.lifecycle_as_of.isoformat(),
        "lifecycle_evidence_ref": security.lifecycle_evidence_ref,
        "delisted_on": security.delisted_on.isoformat() if security.delisted_on else None,
        "delisting_evidence_ref": security.delisting_evidence_ref,
        "source_symbol": security.source_symbol,
        "source_symbol_evidence_ref": security.source_symbol_evidence_ref,
        "source_history_boundary_unknown": security.source_history_boundary_unknown,
        "source_history_available_from": (
            security.source_history_available_from.isoformat()
            if security.source_history_available_from
            else None
        ),
        "source_history_evidence_ref": security.source_history_evidence_ref,
    }


def _canonical_sha256(value: object) -> str:
    """Hash a JSON-native value with stable key and UTF-8 serialization."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_security_manifest(
    securities: Iterable[FinancialSecurityEvidence],
) -> str:
    """Fingerprint explicit security evidence rows without asserting universe completeness.

    Args:
        securities: The exact member rows to bind into an external review artifact.

    Returns:
        A lowercase SHA-256 digest over canonical exchange-qualified rows.

    Raises:
        FinancialManifestError: If rows are empty, duplicated, unbounded, or malformed.
    """
    if isinstance(securities, (str, bytes, bytearray, dict)):
        raise FinancialManifestError("securities must be an iterable of explicit rows")
    try:
        rows = tuple(securities)
    except TypeError as exc:
        raise FinancialManifestError("securities must be an iterable of explicit rows") from exc
    if not rows or len(rows) > MAX_SECURITY_ROWS:
        raise FinancialManifestError(f"securities must contain 1..{MAX_SECURITY_ROWS} rows")
    if any(type(row) is not FinancialSecurityEvidence for row in rows):
        raise FinancialManifestError("securities must contain FinancialSecurityEvidence rows")
    identities = [row.instrument_id for row in rows]
    if len(identities) != len(set(identities)):
        raise FinancialManifestError("security fingerprint contains duplicate identities")
    payload = [_security_payload(row) for row in sorted(rows, key=lambda item: item.instrument_id)]
    return _canonical_sha256(payload)


def _manifest_sha256(manifest: FinancialSecurityManifest) -> str:
    """Hash the attestation and its exact canonical member rows."""
    evidence = manifest.evidence
    payload = {
        "plan_version": PLAN_VERSION,
        "scope_id": evidence.scope_id,
        "registry_version": evidence.registry_version,
        "evidence_ref": evidence.evidence_ref,
        "evidence_kind": UniverseEvidenceKind(evidence.evidence_kind).value,
        "as_of": evidence.as_of.isoformat(),
        "expected_member_count": evidence.expected_member_count,
        "members_sha256": evidence.members_sha256,
        "exchanges": sorted(Exchange(exchange).value for exchange in evidence.exchanges),
        "includes_delisted": evidence.includes_delisted,
        "includes_listing_boundaries": evidence.includes_listing_boundaries,
        "securities": [
            _security_payload(row)
            for row in sorted(manifest.securities, key=lambda item: item.instrument_id)
        ],
    }
    return _canonical_sha256(payload)


def _source_capability_sha256(capability: FinancialSourceCapabilityEvidence) -> str:
    """Hash the observed provider capability evidence used by every candidate."""
    return _canonical_sha256(
        {
            "source_id": capability.source_id,
            "version": capability.version,
            "evidence_ref": capability.evidence_ref,
            "observed_at": capability.observed_at.isoformat(),
            "max_window_span_days": capability.max_window_span_days,
            "supported_exchanges": sorted(
                Exchange(item).value for item in capability.supported_exchanges
            ),
            "supported_statements": sorted(
                StatementType(item).value for item in capability.supported_statements
            ),
            "supported_periods": sorted(
                ReportingPeriod(item).value for item in capability.supported_periods
            ),
            "history_boundary_unknown": capability.history_boundary_unknown,
            "history_available_from": (
                capability.history_available_from.isoformat()
                if capability.history_available_from
                else None
            ),
            "history_boundary_evidence_ref": capability.history_boundary_evidence_ref,
        }
    )


def _universe_blockers(
    manifest: FinancialSecurityManifest,
    *,
    member_sha256: str,
) -> tuple[FinancialPlanBlocker, ...]:
    """Compare caller claims with explicit rows without verifying external truth."""
    evidence = manifest.evidence
    blockers: list[FinancialPlanBlocker] = []
    if evidence.evidence_kind is UniverseEvidenceKind.SOURCE_SNAPSHOT:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.SNAPSHOT_CANNOT_PROVE_UNIVERSE,
                "a provider snapshot is not an independently reconciled A-share security master",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    if evidence.scope_id != ALL_A_SHARE_SCOPE_ID:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_SCOPE_ID_MISMATCH,
                f"universe evidence must attest the exact {ALL_A_SHARE_SCOPE_ID} scope",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    if evidence.expected_member_count != len(tuple(manifest.securities)):
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_MEMBER_COUNT_MISMATCH,
                "attested member count differs from the explicit securities manifest",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    if evidence.members_sha256 != member_sha256:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_MEMBER_DIGEST_MISMATCH,
                "attested digest does not bind these exact identity and lifecycle rows",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    if frozenset(Exchange(exchange) for exchange in evidence.exchanges) != ALL_A_SHARE_EXCHANGES:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_EXCHANGE_SCOPE_MISMATCH,
                "the claimed all-A-share scope must explicitly include SSE, SZSE, and BSE",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    present_exchanges = frozenset(row.exchange for row in manifest.securities)
    if present_exchanges != ALL_A_SHARE_EXCHANGES:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_EXCHANGE_MEMBERS_MISSING,
                "member rows must contain evidenced securities for all three exchanges",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    if not evidence.includes_delisted:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_DELISTED_SECURITIES_OMITTED,
                "the all-A-share history scope must state whether delisted securities are included",
                evidence_refs=(evidence.evidence_ref,),
            )
        )
    return tuple(blockers)


def _source_symbol_matches_exchange(security: FinancialSecurityEvidence) -> bool:
    """Check that the qualified provider symbol suffix agrees with its exchange."""
    if security.source_symbol is None:
        return False
    expected_suffix = _EXCHANGE_SUFFIX[Exchange(security.exchange)]
    return security.source_symbol.endswith(f".{expected_suffix}")


def _security_lifecycle_blockers(
    security: FinancialSecurityEvidence, *, as_of: date
) -> tuple[FinancialPlanBlocker, ...]:
    """Return blockers for one security's listing and lifecycle boundaries."""
    evidence_refs = [security.lifecycle_evidence_ref]
    if security.listing_evidence_ref:
        evidence_refs.append(security.listing_evidence_ref)
    if security.delisting_evidence_ref:
        evidence_refs.append(security.delisting_evidence_ref)

    blockers: list[FinancialPlanBlocker] = []
    if security.lifecycle_status is LifecycleStatus.UNKNOWN:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LIFECYCLE_STATUS_UNKNOWN,
                "current active or delisted status is not established",
                instrument_id=security.instrument_id,
                evidence_refs=tuple(evidence_refs),
            )
        )
    elif security.lifecycle_as_of < as_of:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LIFECYCLE_STATUS_STALE,
                "lifecycle status evidence predates the requested plan as-of date",
                instrument_id=security.instrument_id,
                range_start=security.lifecycle_as_of + timedelta(days=1),
                range_end=as_of,
                evidence_refs=tuple(evidence_refs),
            )
        )
    elif security.lifecycle_as_of > as_of:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LIFECYCLE_EVIDENCE_AFTER_AS_OF,
                "lifecycle evidence postdates the requested historical evaluation date",
                instrument_id=security.instrument_id,
                range_start=as_of + timedelta(days=1),
                range_end=security.lifecycle_as_of,
                evidence_refs=tuple(evidence_refs),
            )
        )
    if (
        security.lifecycle_status is LifecycleStatus.DELISTED
        and security.delisted_on is not None
        and security.delisted_on > as_of
    ):
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LIFECYCLE_BOUNDARY_AFTER_AS_OF,
                "delisting evidence is after the requested lifecycle evaluation date",
                instrument_id=security.instrument_id,
                range_start=as_of + timedelta(days=1),
                range_end=security.delisted_on,
                evidence_refs=tuple(evidence_refs),
            )
        )
    if security.listing_date is None:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LISTING_BOUNDARY_UNKNOWN,
                "listing boundary is unknown; the earliest returned row is not a substitute",
                instrument_id=security.instrument_id,
                evidence_refs=tuple(evidence_refs),
            )
        )
    elif security.listing_date > as_of:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.LISTING_BOUNDARY_AFTER_AS_OF,
                "listing boundary is after the requested lifecycle evaluation date",
                instrument_id=security.instrument_id,
                range_start=as_of + timedelta(days=1),
                range_end=security.listing_date,
                evidence_refs=tuple(evidence_refs),
            )
        )
    return tuple(blockers)


def _manifest_lifecycle_boundary_blockers(
    manifest: FinancialSecurityManifest, *, as_of: date
) -> tuple[FinancialPlanBlocker, ...]:
    """Report boundary completeness independently from member-set claim checks."""
    blockers: list[FinancialPlanBlocker] = []
    if not manifest.evidence.includes_listing_boundaries:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.UNIVERSE_LISTING_BOUNDARIES_OMITTED,
                "the universe evidence does not claim independently sourced listing boundaries",
                evidence_refs=(manifest.evidence.evidence_ref,),
            )
        )
    for security in manifest.securities:
        blockers.extend(_security_lifecycle_blockers(security, as_of=as_of))
    return tuple(blockers)


def _chunk_dates(start: date, end: date, max_span_days: int) -> tuple[tuple[date, date], ...]:
    """Split an inclusive requested date range into gap-free capped date spans."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_span_days), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return tuple(chunks)


def _candidate_id(
    *,
    manifest_sha256: str,
    capability_sha256: str,
    security: FinancialSecurityEvidence,
    statement: StatementType,
    period: ReportingPeriod,
    start: date,
    end: date,
) -> str:
    """Create a deterministic idempotency token for one exact source window."""
    digest = _canonical_sha256(
        {
            "plan_version": PLAN_VERSION,
            "manifest_sha256": manifest_sha256,
            "capability_sha256": capability_sha256,
            "instrument_id": security.instrument_id,
            "source_symbol": security.source_symbol,
            "statement_type": statement.value,
            "reporting_period": period.value,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        }
    )
    return f"fbm2-{digest}"


def _security_blockers(
    security: FinancialSecurityEvidence,
    capability: FinancialSourceCapabilityEvidence,
    *,
    as_of: date,
) -> tuple[FinancialPlanBlocker, ...]:
    """Collect shared lifecycle, source-symbol, exchange, and retention blockers."""
    blockers = list(_security_lifecycle_blockers(security, as_of=as_of))
    evidence_refs = [security.lifecycle_evidence_ref, capability.evidence_ref]
    if security.listing_evidence_ref:
        evidence_refs.append(security.listing_evidence_ref)
    if security.delisting_evidence_ref:
        evidence_refs.append(security.delisting_evidence_ref)
    if security.source_symbol_evidence_ref:
        evidence_refs.append(security.source_symbol_evidence_ref)
    if security.source_history_evidence_ref:
        evidence_refs.append(security.source_history_evidence_ref)

    if security.source_symbol is None:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.SOURCE_SYMBOL_MAPPING_MISSING,
                "no evidence-backed provider symbol mapping is available",
                instrument_id=security.instrument_id,
                evidence_refs=tuple(evidence_refs),
            )
        )
    elif not _source_symbol_matches_exchange(security):
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.SOURCE_SYMBOL_EXCHANGE_MISMATCH,
                "provider symbol suffix conflicts with the manifest exchange",
                instrument_id=security.instrument_id,
                evidence_refs=tuple(evidence_refs),
            )
        )
    if security.exchange not in capability.supported_exchanges:
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.EXCHANGE_CAPABILITY_UNVERIFIED,
                f"source capability evidence does not cover {Exchange(security.exchange).value}",
                instrument_id=security.instrument_id,
                evidence_refs=(capability.evidence_ref,),
            )
        )
    if capability.history_boundary_unknown or security.source_history_boundary_unknown:
        history_refs = [capability.evidence_ref]
        if capability.history_boundary_evidence_ref:
            history_refs.append(capability.history_boundary_evidence_ref)
        if security.source_history_evidence_ref:
            history_refs.append(security.source_history_evidence_ref)
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.SOURCE_HISTORY_BOUNDARY_UNKNOWN,
                "source retention is not established for the provider or this security",
                instrument_id=security.instrument_id,
                range_start=security.listing_date,
                range_end=as_of,
                evidence_refs=tuple(dict.fromkeys(history_refs)),
            )
        )
    known_history_starts = tuple(
        source_start
        for source_start in (
            capability.history_available_from,
            security.source_history_available_from,
        )
        if source_start is not None
    )
    retained_from = max(known_history_starts) if known_history_starts else None
    if (
        security.listing_date is not None
        and security.listing_date <= as_of
        and retained_from is not None
        and retained_from > security.listing_date
    ):
        history_refs = [capability.evidence_ref]
        if capability.history_boundary_evidence_ref:
            history_refs.append(capability.history_boundary_evidence_ref)
        if security.source_history_evidence_ref:
            history_refs.append(security.source_history_evidence_ref)
        blockers.append(
            FinancialPlanBlocker(
                FinancialPlanBlockReason.SOURCE_HISTORY_GAP,
                "source-retained history starts after the independently evidenced listing date",
                instrument_id=security.instrument_id,
                range_start=security.listing_date,
                range_end=min(retained_from - timedelta(days=1), as_of),
                evidence_refs=tuple(dict.fromkeys(history_refs)),
            )
        )
    return tuple(blockers)


def build_financial_backfill_plan(
    manifest: FinancialSecurityManifest,
    capability: FinancialSourceCapabilityEvidence,
    *,
    as_of: date,
    planned_observation_at: datetime,
) -> FinancialBackfillPlan:
    """Build evidence-bound candidate windows for all six financial series per security.

    Args:
        manifest: Explicit securities plus an attestation for the exact member set.
        capability: Observed source support, retention, and maximum window evidence.
        as_of: Inclusive report-period cutoff. No system clock is consulted.
        planned_observation_at: Aware timestamp recording when this plan was produced.

    Returns:
        A deterministic plan with six annual/quarterly statement assessments per
        security. Each candidate is only a descriptor; dispatch is always denied.

    Raises:
        FinancialManifestError: If inputs are malformed, future-dated, or the
            manifest/evidence objects are not exact trusted model instances.
    """
    if type(manifest) is not FinancialSecurityManifest:
        raise FinancialManifestError("manifest must be a FinancialSecurityManifest")
    if type(capability) is not FinancialSourceCapabilityEvidence:
        raise FinancialManifestError("capability must be FinancialSourceCapabilityEvidence")
    plan_as_of = _required_date(as_of, "as_of")
    planned_at = _aware_utc(planned_observation_at, "planned_observation_at")
    shanghai_date = planned_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if plan_as_of > shanghai_date:
        raise FinancialManifestError(
            "as_of cannot be after planned_observation_at in Asia/Shanghai"
        )
    if manifest.evidence.as_of < plan_as_of:
        raise FinancialManifestError("universe evidence as_of predates the requested plan cutoff")
    if manifest.evidence.as_of > shanghai_date:
        raise FinancialManifestError("universe evidence cannot be future-dated at plan time")
    if capability.observed_at > planned_at:
        raise FinancialManifestError("source capability evidence cannot be observed after planning")

    member_sha256 = fingerprint_security_manifest(manifest.securities)
    manifest_digest = _manifest_sha256(manifest)
    capability_digest = _source_capability_sha256(capability)
    universe_blockers = _universe_blockers(manifest, member_sha256=member_sha256)
    lifecycle_boundary_blockers = _manifest_lifecycle_boundary_blockers(manifest, as_of=plan_as_of)
    membership_claim_mismatch_reasons = {
        FinancialPlanBlockReason.UNIVERSE_SCOPE_ID_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_MEMBER_COUNT_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_MEMBER_DIGEST_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_EXCHANGE_SCOPE_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_EXCHANGE_MEMBERS_MISSING,
        FinancialPlanBlockReason.UNIVERSE_DELISTED_SECURITIES_OMITTED,
    }
    member_set_claim_consistent = not any(
        blocker.reason in membership_claim_mismatch_reasons for blocker in universe_blockers
    )
    assessments: list[FinancialSeriesAssessment] = []
    statement_types = tuple(StatementType)
    periods = tuple(ReportingPeriod)

    for security in sorted(manifest.securities, key=lambda item: item.instrument_id):
        shared_blockers = _security_blockers(security, capability, as_of=plan_as_of)
        listing_date = security.listing_date
        can_build_windows = (
            listing_date is not None
            and security.lifecycle_status is not LifecycleStatus.UNKNOWN
            and security.lifecycle_as_of == plan_as_of
            and not (
                security.lifecycle_status is LifecycleStatus.DELISTED
                and security.delisted_on is not None
                and security.delisted_on > plan_as_of
            )
            and security.lifecycle_as_of >= plan_as_of
            and security.source_symbol is not None
            and _source_symbol_matches_exchange(security)
            and security.exchange in capability.supported_exchanges
        )
        window_start = listing_date
        known_history_starts = tuple(
            source_start
            for source_start in (
                capability.history_available_from,
                security.source_history_available_from,
            )
            if source_start is not None
        )
        if can_build_windows and listing_date is not None and known_history_starts:
            window_start = max(listing_date, *known_history_starts)
        if window_start is not None and window_start > plan_as_of:
            can_build_windows = False
            shared_blockers = (
                *shared_blockers,
                FinancialPlanBlocker(
                    FinancialPlanBlockReason.NO_SOURCE_WINDOW,
                    "no query window remains between the supported history boundary and as-of date",
                    instrument_id=security.instrument_id,
                    evidence_refs=tuple(
                        ref
                        for ref in (
                            capability.evidence_ref,
                            capability.history_boundary_evidence_ref,
                            security.source_history_evidence_ref,
                            security.listing_evidence_ref,
                        )
                        if ref is not None
                    ),
                ),
            )

        for statement in statement_types:
            for period in periods:
                combination_blockers = list(shared_blockers)
                if statement not in capability.supported_statements:
                    combination_blockers.append(
                        FinancialPlanBlocker(
                            FinancialPlanBlockReason.STATEMENT_CAPABILITY_UNVERIFIED,
                            f"source capability evidence does not cover {statement.value}",
                            instrument_id=security.instrument_id,
                            statement_type=statement,
                            reporting_period=period,
                            evidence_refs=(capability.evidence_ref,),
                        )
                    )
                if period not in capability.supported_periods:
                    combination_blockers.append(
                        FinancialPlanBlocker(
                            FinancialPlanBlockReason.PERIOD_CAPABILITY_UNVERIFIED,
                            f"source capability evidence does not cover {period.value}",
                            instrument_id=security.instrument_id,
                            statement_type=statement,
                            reporting_period=period,
                            evidence_refs=(capability.evidence_ref,),
                        )
                    )
                candidates: list[FinancialBackfillCandidate] = []
                supported_combo = (
                    statement in capability.supported_statements
                    and period in capability.supported_periods
                )
                if can_build_windows and supported_combo and window_start is not None:
                    for chunk_start, chunk_end in _chunk_dates(
                        window_start, plan_as_of, capability.max_window_span_days
                    ):
                        evidence_refs = [
                            manifest.evidence.evidence_ref,
                            security.lifecycle_evidence_ref,
                            security.listing_evidence_ref or manifest.evidence.evidence_ref,
                            security.source_symbol_evidence_ref or manifest.evidence.evidence_ref,
                            capability.evidence_ref,
                        ]
                        if security.delisting_evidence_ref:
                            evidence_refs.append(security.delisting_evidence_ref)
                        if capability.history_boundary_evidence_ref:
                            evidence_refs.append(capability.history_boundary_evidence_ref)
                        if security.source_history_evidence_ref:
                            evidence_refs.append(security.source_history_evidence_ref)
                        candidates.append(
                            FinancialBackfillCandidate(
                                stable_id=_candidate_id(
                                    manifest_sha256=manifest_digest,
                                    capability_sha256=capability_digest,
                                    security=security,
                                    statement=statement,
                                    period=period,
                                    start=chunk_start,
                                    end=chunk_end,
                                ),
                                instrument_id=security.instrument_id,
                                ticker=security.ticker,
                                exchange=Exchange(security.exchange),
                                source_symbol=security.source_symbol or "",
                                statement_type=statement,
                                reporting_period=period,
                                window_start=chunk_start,
                                window_end=chunk_end,
                                manifest_sha256=manifest_digest,
                                source_capability_sha256=capability_digest,
                                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                            )
                        )
                assessments.append(
                    FinancialSeriesAssessment(
                        instrument_id=security.instrument_id,
                        statement_type=statement,
                        reporting_period=period,
                        candidates=tuple(candidates),
                        blockers=tuple(combination_blockers),
                    )
                )

    return FinancialBackfillPlan(
        plan_version=PLAN_VERSION,
        source_id=capability.source_id,
        source_version=capability.version,
        as_of=plan_as_of,
        planned_observation_at=planned_at,
        manifest_sha256=manifest_digest,
        source_capability_sha256=capability_digest,
        member_set_claim_consistent=member_set_claim_consistent,
        lifecycle_boundaries_complete=not lifecycle_boundary_blockers,
        lifecycle_boundary_blockers=lifecycle_boundary_blockers,
        max_window_span_days=capability.max_window_span_days,
        universe_blockers=universe_blockers,
        series=tuple(assessments),
    )


__all__ = [
    "ALL_A_SHARE_EXCHANGES",
    "ALL_A_SHARE_SCOPE_ID",
    "MAX_FINANCIAL_WINDOW_SPAN_DAYS",
    "PLAN_VERSION",
    "Exchange",
    "FinancialBackfillCandidate",
    "FinancialBackfillPlan",
    "FinancialManifestError",
    "FinancialPlanBlockReason",
    "FinancialPlanBlocker",
    "FinancialSecurityEvidence",
    "FinancialSecurityManifest",
    "FinancialSeriesAssessment",
    "FinancialSourceCapabilityEvidence",
    "FinancialUniverseEvidence",
    "LifecycleStatus",
    "ReportingPeriod",
    "StatementType",
    "UniverseEvidenceKind",
    "build_financial_backfill_plan",
    "fingerprint_security_manifest",
]
