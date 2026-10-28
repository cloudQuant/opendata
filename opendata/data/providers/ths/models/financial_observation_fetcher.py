"""Explicit, bounded Fuyao fetches for source-native financial observations.

This service is intentionally not registered with the provider or scheduler.
It accepts only one registry-resolved A-share, one statement and one cadence,
and either one inclusive window of at most ten years or a source limit. A
trusted caller must establish the request scope and its source-attempt binding
before invoking it. The service neither issues grants nor archives responses.

Native-unit profiles and registry evidence are injected declarations. A
mapping digest detects drift from the declared mapping, but neither the digest
nor an evidence reference proves that the unit semantics were independently
verified. Decoded rows and quarantined rows are observations, not a coverage
or completeness assessment.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeGuard

import httpx

from opendata.data.instrument_reference import (
    InstrumentReferenceError,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.providers.ths.endpoints import (
    BALANCE_SHEETS_ENDPOINT,
    CASH_FLOW_STATEMENTS_ENDPOINT,
    FINANCIAL_PERIOD_END,
    FINANCIAL_STATEMENT_MAX_LIMIT,
    INCOME_STATEMENTS_ENDPOINT,
    MAX_WINDOW,
    build_financial_statements_request,
    financial_statement_items,
    millis_to_trading_date,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    MAX_BODY_BYTES,
    MAX_FINANCIAL_ROWS,
    FinancialObservationDecodeError,
    FinancialObservationSourceReference,
    QuarantinedFinancialObservationRow,
    decode_financial_observation_body,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestExecutionScope,
    RequestOperation,
    SourceAttemptBinding,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import SourceAttemptGate

if TYPE_CHECKING:
    from opendata.data.financial_observation import Periodicity, StatementType

ObservationCoverage = Literal["not_assessed"]
ObservationUsability = Literal[
    "NO_USABLE_OBSERVATIONS",
    "PARTIAL",
    "USABLE_OBSERVATIONS_PRESENT",
]
UnitEvidenceKind = Literal["operator_declared"]

_FINANCIAL_STATEMENT_DATASET_ID = "financial_statement"
_THS_SOURCE_ID = "ths"
_MODEL_ID = "financial_statement"
_CANONICAL_INSTRUMENT_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_UNIT_VERSION_MAX_CHARS = 128
_UNIT_EVIDENCE_MAX_CHARS = 512
_MAX_TRANSPORT_ATTEMPTS = 3
_FINANCIAL_REQUEST_SUMMARY_FIELDS = frozenset(
    {"thscode", "period", "start", "end", "limit"}
)
_STATEMENT_ENDPOINTS: Mapping[StatementType, str] = MappingProxyType(
    {
        "income": INCOME_STATEMENTS_ENDPOINT,
        "balance": BALANCE_SHEETS_ENDPOINT,
        "cashflow": CASH_FLOW_STATEMENTS_ENDPOINT,
    }
)
_STATEMENT_TYPES = frozenset({"income", "balance", "cashflow"})
_PERIODICITIES = frozenset({"annual", "quarterly"})


class FinancialObservationFetchError(RuntimeError):
    """Safe, stable failure raised before or after one bounded observation fetch."""

    def __init__(self, code: str) -> None:
        """Create an error without embedding identifiers, response data or secrets."""
        self.code = code
        super().__init__(code)


def _is_statement_type(value: object) -> TypeGuard[StatementType]:
    """Return whether a value is one exact supported statement token."""
    return type(value) is str and value in _STATEMENT_TYPES


def _is_periodicity(value: object) -> TypeGuard[Periodicity]:
    """Return whether a value is one exact supported reporting cadence."""
    return type(value) is str and value in _PERIODICITIES


def canonical_native_unit_mapping_sha256(
    *,
    statement_type: StatementType,
    version: str,
    native_units: Mapping[str, str],
) -> str:
    """Hash the normalized statement-specific unit declaration.

    The canonical representation sorts item names and includes the statement
    and profile version. The resulting fingerprint establishes only that the
    injected mapping matches its declared digest; it does not attest unit truth.

    Args:
        statement_type: One supported financial statement.
        version: Operator-declared mapping version.
        native_units: Complete item-to-unit mapping for this statement.

    Returns:
        Lowercase SHA-256 hex digest of the canonical JSON bytes.
    """
    normalized = _validate_unit_mapping(native_units)
    if not _is_statement_type(statement_type):
        raise ValueError("statement_type must be a supported exact token")
    _validate_unit_version(version)
    payload = {
        "statement_type": statement_type,
        "units": [[item, normalized[item]] for item in sorted(normalized)],
        "version": version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(encoded).hexdigest()


def _validate_unit_version(value: object) -> str:
    """Validate an operator-supplied unit-profile version token."""
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _UNIT_VERSION_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("unit mapping version must be a bounded trimmed token")
    return value


def _validate_unit_mapping(value: object) -> dict[str, str]:
    """Copy and validate bounded unit declarations without guessing missing units."""
    if not isinstance(value, Mapping):
        raise ValueError("native_units must be an explicit mapping")
    try:
        entries = dict(value.items())
    except Exception:
        raise ValueError("native_units could not be read safely") from None
    if not entries or len(entries) > 256:
        raise ValueError("native_units must contain between 1 and 256 items")
    for item, unit in entries.items():
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or len(item) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
            or type(unit) is not str
            or not unit
            or unit != unit.strip()
            or len(unit) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in unit)
        ):
            raise ValueError("native_units contains an invalid item or unit token")
    return entries


@dataclass(frozen=True, slots=True)
class ThsFinancialNativeUnitProfile:
    """Operator-declared native units for one exact THS statement family.

    ``evidence_ref`` points to a caller-maintained artifact. It is not a
    signature, an independent review, or proof of the source's unit semantics.
    The supplied digest is recalculated from a copied normalized mapping at
    construction and again before each request.
    """

    statement_type: StatementType
    version: str
    native_units: Mapping[str, str] = field(repr=False)
    mapping_sha256_declared: str
    evidence_ref: str

    def __post_init__(self) -> None:
        """Freeze values and reject an incomplete or mismatched declaration."""
        if not _is_statement_type(self.statement_type):
            raise ValueError("statement_type must be a supported exact token")
        version = _validate_unit_version(self.version)
        units = _validate_unit_mapping(self.native_units)
        if (
            type(self.mapping_sha256_declared) is not str
            or _SHA256_PATTERN.fullmatch(self.mapping_sha256_declared) is None
        ):
            raise ValueError("mapping_sha256_declared must be lowercase SHA-256")
        computed = canonical_native_unit_mapping_sha256(
            statement_type=self.statement_type,
            version=version,
            native_units=units,
        )
        if computed != self.mapping_sha256_declared:
            raise ValueError("declared native-unit mapping digest does not match mapping")
        if (
            type(self.evidence_ref) is not str
            or not self.evidence_ref
            or self.evidence_ref != self.evidence_ref.strip()
            or len(self.evidence_ref) > _UNIT_EVIDENCE_MAX_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in self.evidence_ref)
        ):
            raise ValueError("evidence_ref must be a bounded nonempty operator reference")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "native_units", MappingProxyType(units))

    @property
    def evidence_kind(self) -> UnitEvidenceKind:
        """Return the fixed provenance class of this caller-supplied declaration."""
        return "operator_declared"


@dataclass(frozen=True, slots=True)
class FinancialObservationFetchResult:
    """One in-memory result tied to the exact entity bytes and echoed request.

    ``raw_body`` is the HTTPX-buffered entity body exposed by
    :class:`FuyaoHttpResponse`; it is not a compressed wire capture. Raw bytes
    and decoded collections are hidden from ``repr`` to reduce
    accidental disclosure in logs. ``coverage_assessment`` is permanently
    ``not_assessed``: this result says nothing about missing periods, archive
    history, or completeness. ``usability_status`` only distinguishes rows
    returned by this response; it never asserts that the requested history is
    complete or suitable for publishing. ``request_summary_sha256`` is a
    caller-side v1 digest over the validated parameter mapping handed to the
    transport; it is not an independent source receipt or proof of what the
    provider received.
    """

    instrument_id: str
    statement_type: StatementType
    periodicity: Periodicity
    source_request_id: str
    body_sha256: str
    observed_at: datetime
    status_code: int
    unit_mapping_version_declared: str
    unit_mapping_sha256_declared: str
    unit_mapping_evidence_kind: UnitEvidenceKind
    coverage_assessment: ObservationCoverage
    usability_status: ObservationUsability
    raw_body: bytes = field(repr=False, compare=False)
    observations: tuple[FinancialObservationSourceReference, ...] = field(repr=False, compare=False)
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...] = field(
        repr=False, compare=False
    )
    request_summary_sha256: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Enforce the response-byte, digest, time and decoded-reference binding."""
        if self.request_summary_sha256 is not None and (
            type(self.request_summary_sha256) is not str
            or _SHA256_PATTERN.fullmatch(self.request_summary_sha256) is None
        ):
            raise ValueError("request_summary_sha256 must be lowercase SHA-256 or None")
        if type(self.raw_body) is not bytes or not self.raw_body:
            raise ValueError("raw_body must contain the exact nonempty HTTP response bytes")
        digest = hashlib.sha256(self.raw_body).hexdigest()
        if self.body_sha256 != digest:
            raise ValueError("body_sha256 does not match raw_body")
        if (
            type(self.source_request_id) is not str
            or not self.source_request_id
            or self.source_request_id != self.source_request_id.strip()
        ):
            raise ValueError("source_request_id must be a nonempty trimmed token")
        if type(self.observed_at) is not datetime or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        try:
            observed_offset = self.observed_at.utcoffset()
            observed_timezone_name = self.observed_at.tzname()
        except (NotImplementedError, OverflowError, TypeError, ValueError):
            raise ValueError("observed_at must be timezone-aware") from None
        if observed_offset != timedelta(0) or observed_timezone_name != "UTC":
            raise ValueError("observed_at must use a zero-offset UTC timezone")
        if type(self.status_code) is not int or self.status_code not in {200, 201}:
            raise ValueError("status_code must be a successful Fuyao response status")
        if type(self.observations) is not tuple or type(self.quarantined_rows) is not tuple:
            raise ValueError("decoded references must be immutable tuples")
        if any(reference.body_sha256 != digest for reference in self.observations):
            raise ValueError("observation source reference is not bound to raw_body")
        if any(row.body_sha256 != digest for row in self.quarantined_rows):
            raise ValueError("quarantine reference is not bound to raw_body")
        if self.coverage_assessment != "not_assessed":
            raise ValueError("decoded observations cannot assert completeness")
        if self.usability_status != _usability_status(self.observations, self.quarantined_rows):
            raise ValueError("usability_status does not match decoded rows")


class ThsFinancialObservationFetcher:
    """Fetch one bounded source-native statement using explicit trusted context.

    Construction requires an exact SQLite ``SourceAttemptGate``, trusted
    server-side Fuyao credentials, an immutable instrument registry, and one
    operator-declared unit profile for each statement. The service copies the
    credential into a private client pinned to the fixed Fuyao origin; callers
    do not share a mutable ``FuyaoHttpClient`` whose base URL could change
    between scope validation and send. Production composition must keep the
    credentials, gate and registry trusted. ``mock_transport`` is an exact
    HTTPX MockTransport seam for offline tests only. The service is not
    registered or invoked automatically. ``observed_at`` comes from one real
    system UTC clock sample immediately after the response is received; there
    is no public clock override. This runtime timestamp does not establish
    point-in-time correctness, historical revisions or archive completeness.
    """

    def __init__(
        self,
        *,
        credentials: FuyaoCredentials,
        source_attempt_gate: SourceAttemptGate,
        instrument_registry: InstrumentReferenceRegistry,
        unit_profiles: Mapping[str, ThsFinancialNativeUnitProfile],
        mock_transport: httpx.MockTransport | None = None,
    ) -> None:
        """Create a fetcher with explicit trusted dependencies and no discovery.

        Args:
            credentials: Exact trusted server-side credential configuration. Its
                base URL must be the fixed Fuyao origin; the private transport
                receives a copy of its API key and a fixed origin.
            source_attempt_gate: Real file-backed SQLite attempt gate.
            instrument_registry: Exact immutable reviewed instrument registry.
            unit_profiles: Complete operator-declared profiles for all three statements.
            mock_transport: Exact in-process HTTPX MockTransport for offline tests;
                omit it in production.

        Raises:
            TypeError: A dependency is not the exact supported implementation.
            ValueError: The credential origin or unit profiles differ.
        """
        if type(source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be an exact SourceAttemptGate")
        if type(instrument_registry) is not InstrumentReferenceRegistry:
            raise TypeError("instrument_registry must be an exact InstrumentReferenceRegistry")
        if type(credentials) is not FuyaoCredentials:
            raise TypeError("credentials must be exact trusted FuyaoCredentials")
        if credentials.base_url != DEFAULT_FUYAO_API_BASE_URL:
            raise FinancialObservationFetchError("fuyao_origin_not_allowed")
        if mock_transport is not None and type(mock_transport) is not httpx.MockTransport:
            raise TypeError("mock_transport must be an exact HTTPX MockTransport")
        if not isinstance(unit_profiles, Mapping):
            raise TypeError("unit_profiles must be an explicit mapping")
        copied_profiles = dict(unit_profiles.items())
        if set(copied_profiles) != _STATEMENT_TYPES:
            raise ValueError("unit_profiles must declare income, balance, and cashflow")
        for key, profile in copied_profiles.items():
            if (
                not _is_statement_type(key)
                or type(profile) is not ThsFinancialNativeUnitProfile
                or profile.statement_type != key
            ):
                raise ValueError("unit_profiles must match exact statement identities")
        # Keep the caller's mutable credential/client out of the request path.
        # The service-owned client uses only this copied API key and fixed origin.
        private_credentials = FuyaoCredentials(
            credentials.api_key,
            base_url=DEFAULT_FUYAO_API_BASE_URL,
        )
        self._client = FuyaoHttpClient(
            credentials=private_credentials,
            mock_transport=mock_transport,
            source_attempt_gate=source_attempt_gate,
            max_attempts=_MAX_TRANSPORT_ATTEMPTS,
            raw_response_cache=None,
        )
        self._source_attempt_gate = source_attempt_gate
        self._instrument_registry = instrument_registry
        self._unit_profiles = MappingProxyType(copied_profiles)

    def _validate_client(self) -> None:
        """Fail closed if the private, service-owned transport was altered."""
        if self._client._source_attempt_gate is not self._source_attempt_gate:
            raise FinancialObservationFetchError("source_attempt_gate_binding_invalid")
        if self._client.credentials.base_url != DEFAULT_FUYAO_API_BASE_URL:
            raise FinancialObservationFetchError("fuyao_origin_not_allowed")
        if not 1 <= self._client._max_attempts <= _MAX_TRANSPORT_ATTEMPTS:
            raise FinancialObservationFetchError("transport_attempt_limit_invalid")

    @staticmethod
    def _validate_scope() -> tuple[RequestExecutionScope, ...]:
        """Require one exact, consistently bound trusted THS query scope."""
        scopes = current_request_scopes()
        if type(scopes) is not tuple or not scopes:
            raise FinancialObservationFetchError("trusted_request_scope_required")
        first = scopes[0]
        if type(first) is not RequestExecutionScope:
            raise FinancialObservationFetchError("trusted_request_scope_invalid")
        binding = first.source_attempt_binding
        if (
            first.source != _THS_SOURCE_ID
            or first.canonical_model != _MODEL_ID
            or first.operation is not RequestOperation.QUERY
            or type(binding) is not SourceAttemptBinding
        ):
            raise FinancialObservationFetchError("trusted_request_scope_invalid")
        for scope in scopes:
            if (
                type(scope) is not RequestExecutionScope
                or scope.source != _THS_SOURCE_ID
                or scope.canonical_model != _MODEL_ID
                or scope.operation is not RequestOperation.QUERY
                or type(scope.source_attempt_binding) is not SourceAttemptBinding
                or scope.source_attempt_binding != binding
            ):
                raise FinancialObservationFetchError("trusted_request_scope_invalid")
        try:
            check_scope_liveness(scopes)
            authorize_request(scopes, _THS_SOURCE_ID, "fuyao.aicubes.cn")
        except RequestAuthorizationError:
            raise FinancialObservationFetchError("trusted_request_scope_unauthorized") from None
        return scopes

    def _validate_instrument(self, instrument_id: object) -> str:
        """Resolve only an exact canonical six-digit A-share registry identity."""
        if (
            type(instrument_id) is not str
            or _CANONICAL_INSTRUMENT_PATTERN.fullmatch(instrument_id) is None
        ):
            raise FinancialObservationFetchError("instrument_identity_invalid")
        try:
            resolved = self._instrument_registry.resolve(
                _FINANCIAL_STATEMENT_DATASET_ID,
                _THS_SOURCE_ID,
                instrument_id,
            )
        except InstrumentReferenceError:
            raise FinancialObservationFetchError("instrument_identity_unregistered") from None
        if (
            resolved.input_identifier != instrument_id
            or resolved.instrument_id != instrument_id
            or resolved.lifecycle.instrument_id != instrument_id
            or resolved.lifecycle.status is LifecycleStatus.UNKNOWN
        ):
            raise FinancialObservationFetchError("instrument_identity_unverified")
        return resolved.instrument_id

    @staticmethod
    def _validate_query(
        *,
        statement_type: object,
        periodicity: object,
        start_date: object,
        end_date: object,
        limit: object,
    ) -> tuple[StatementType, Periodicity, date | None, date | None, int | None]:
        """Require one bounded date-window or a positive provider period limit."""
        if not _is_statement_type(statement_type):
            raise FinancialObservationFetchError("statement_type_invalid")
        if not _is_periodicity(periodicity):
            raise FinancialObservationFetchError("periodicity_invalid")
        if (start_date is None) != (end_date is None):
            raise FinancialObservationFetchError("date_window_incomplete")
        windowed = start_date is not None and end_date is not None
        limited = limit is not None
        if windowed == limited:
            raise FinancialObservationFetchError("exactly_one_query_bound_required")
        if windowed:
            if type(start_date) is not date or type(end_date) is not date:
                raise FinancialObservationFetchError("date_window_invalid")
            if start_date > end_date:
                raise FinancialObservationFetchError("date_window_invalid")
            if end_date - start_date > MAX_WINDOW:
                raise FinancialObservationFetchError("date_window_exceeds_ten_years")
            return statement_type, periodicity, start_date, end_date, None
        if type(limit) is not int or not 1 <= limit <= FINANCIAL_STATEMENT_MAX_LIMIT:
            raise FinancialObservationFetchError("period_limit_invalid")
        return statement_type, periodicity, None, None, limit

    def _validated_profile(self, statement_type: StatementType) -> ThsFinancialNativeUnitProfile:
        """Recompute the mapping pin and require exact key coverage before I/O."""
        profile = self._unit_profiles[statement_type]
        if type(profile) is not ThsFinancialNativeUnitProfile:
            raise FinancialObservationFetchError("unit_mapping_profile_invalid")
        try:
            actual = canonical_native_unit_mapping_sha256(
                statement_type=profile.statement_type,
                version=profile.version,
                native_units=profile.native_units,
            )
            expected_items = financial_statement_items(statement_type)
            declared_items = set(profile.native_units)
        except (TypeError, ValueError, KeyError):
            raise FinancialObservationFetchError("unit_mapping_profile_invalid") from None
        if (
            profile.statement_type != statement_type
            or actual != profile.mapping_sha256_declared
            or declared_items != set(expected_items)
        ):
            raise FinancialObservationFetchError("unit_mapping_profile_mismatch")
        return profile

    def fetch(
        self,
        *,
        instrument_id: str,
        statement_type: StatementType,
        periodicity: Periodicity,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> FinancialObservationFetchResult:
        """Fetch and decode one explicitly authorized financial statement.

        Exactly one canonical registry identity and one statement/cadence are
        sent to a fixed Fuyao endpoint. Supply either both inclusive dates over
        no more than ten years or a provider limit from 1 through 20. No API
        field can select the source host, transport grant, unit mapping, or
        upstream code. The matching trusted ``request_execution_scope`` must
        already be active before this method is called.

        Args:
            instrument_id: Canonical six-digit A-share identity from the registry.
            statement_type: ``income``, ``balance`` or ``cashflow``.
            periodicity: ``annual`` or ``quarterly``.
            start_date: Optional inclusive beginning of a bounded report-period window.
            end_date: Optional inclusive end of a bounded report-period window.
            limit: Optional exact number of most recent source periods, 1 through 20.

        Returns:
            Exact entity bytes surfaced by the transport, body/request
            references, decoded source observations and quarantined row
            references. Coverage stays ``not_assessed``.

        Raises:
            FinancialObservationFetchError: Trusted scope, identity, query,
                transport or unit-profile preflight fails.
            FinancialObservationDecodeError: The response cannot be decoded
                under the raw observation contract.
            Fuyao transport errors: A bounded gated transport attempt fails.
        """
        scopes = self._validate_scope()
        self._validate_client()
        canonical_id = self._validate_instrument(instrument_id)
        statement, cadence, start, end, period_limit = self._validate_query(
            statement_type=statement_type,
            periodicity=periodicity,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        profile = self._validated_profile(statement)
        endpoint = _STATEMENT_ENDPOINTS[statement]
        try:
            built_params = build_financial_statements_request(
                symbol=canonical_id,
                period=cadence,
                start=start,
                end=end,
                limit=period_limit,
            )
            if type(built_params) is not dict:
                raise ValueError("upstream request parameters must be a dictionary")
            expected_params: dict[str, str | int] = {
                "thscode": canonical_id,
                "period": cadence,
            }
            if start is not None and end is not None:
                expected_params.update(
                    {
                        "start": shanghai_midnight_millis(start),
                        "end": shanghai_midnight_millis(end),
                    }
                )
            elif period_limit is not None:
                expected_params["limit"] = period_limit
            params = MappingProxyType(dict(built_params))
            if params.keys() != expected_params.keys() or any(
                type(params[key]) is not type(expected_value)
                or params[key] != expected_value
                for key, expected_value in expected_params.items()
            ):
                raise ValueError("upstream request parameters differ from the validated query")
            request_digest = request_summary_sha256(
                params,
                allowed_fields=_FINANCIAL_REQUEST_SUMMARY_FIELDS,
            )
        except (TypeError, ValueError):
            raise FinancialObservationFetchError("upstream_request_parameters_invalid") from None

        # The client itself reserves one gate row immediately before every
        # physical send and bypasses all response caches whenever gate-bound.
        self._validate_client()
        check_scope_liveness(scopes)
        response = self._client.get(endpoint, params=params)
        # This is the only production clock sample for this response. Keep it
        # immediately after receive so callers cannot backdate the record and
        # a second sample cannot spuriously straddle a Shanghai date boundary.
        observed_at = _utc_now()
        if (
            type(observed_at) is not datetime
            or observed_at.tzinfo is None
            or observed_at.utcoffset() is None
        ):
            raise FinancialObservationFetchError("response_observation_time_invalid")
        try:
            observed_utc = observed_at.astimezone(timezone.utc)
        except (OverflowError, TypeError, ValueError):
            raise FinancialObservationFetchError("response_observation_time_invalid") from None
        raw_body = response.raw_content
        if type(raw_body) is not bytes or not raw_body:
            raise FinancialObservationFetchError("response_body_empty_or_invalid")
        try:
            decoded = decode_financial_observation_body(
                raw_body,
                expected_instrument_id=canonical_id,
                statement_type=statement,
                periodicity=cadence,
                observed_at=observed_utc,
                native_units=profile.native_units,
                native_unit_mapping_version_declared=profile.version,
                native_unit_mapping_sha256_declared=profile.mapping_sha256_declared,
            )
        except FinancialObservationDecodeError:
            raise
        if (
            decoded.source_request_id != response.envelope.request_id
            or decoded.native_unit_mapping_sha256_declared != profile.mapping_sha256_declared
            or decoded.body_sha256 != hashlib.sha256(raw_body).hexdigest()
        ):
            raise FinancialObservationFetchError("response_binding_mismatch")
        if any(
            reference.observation.instrument_id != canonical_id
            or reference.observation.statement_type != statement
            or reference.observation.periodicity != cadence
            or reference.body_sha256 != decoded.body_sha256
            for reference in decoded.observations
        ):
            raise FinancialObservationFetchError("decoded_observation_binding_mismatch")
        if any(row.body_sha256 != decoded.body_sha256 for row in decoded.quarantined_rows):
            raise FinancialObservationFetchError("quarantine_body_binding_mismatch")
        report_periods = _validated_response_row_periods(
            raw_body,
            observations=decoded.observations,
            quarantined_rows=decoded.quarantined_rows,
        )
        if period_limit is not None and len(report_periods) > period_limit:
            raise FinancialObservationFetchError("response_rows_exceed_requested_limit")
        if (
            start is not None
            and end is not None
            and any(
                report_period < start or report_period > end for report_period in report_periods
            )
        ):
            raise FinancialObservationFetchError("response_report_period_outside_window")

        return FinancialObservationFetchResult(
            instrument_id=canonical_id,
            statement_type=statement,
            periodicity=cadence,
            source_request_id=decoded.source_request_id,
            body_sha256=decoded.body_sha256,
            observed_at=observed_utc,
            status_code=response.status_code,
            unit_mapping_version_declared=profile.version,
            unit_mapping_sha256_declared=profile.mapping_sha256_declared,
            unit_mapping_evidence_kind=profile.evidence_kind,
            coverage_assessment="not_assessed",
            usability_status=_usability_status(decoded.observations, decoded.quarantined_rows),
            raw_body=raw_body,
            observations=decoded.observations,
            quarantined_rows=decoded.quarantined_rows,
            request_summary_sha256=request_digest,
        )

    def close(self) -> None:
        """Close the private HTTPX client owned by this fetcher."""
        self._client.close()

    def __enter__(self) -> ThsFinancialObservationFetcher:
        """Return the fetcher as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close its private HTTPX client on context exit."""
        self.close()


def _usability_status(
    observations: tuple[FinancialObservationSourceReference, ...],
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...],
) -> ObservationUsability:
    """Describe usable versus quarantined rows without asserting completeness."""
    if not observations:
        return "NO_USABLE_OBSERVATIONS"
    if quarantined_rows:
        return "PARTIAL"
    return "USABLE_OBSERVATIONS_PRESENT"


def _validated_response_row_periods(
    raw_body: bytes,
    *,
    observations: tuple[FinancialObservationSourceReference, ...],
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...],
) -> tuple[date, ...]:
    """Return one checked report period per source row, including quarantines.

    The raw decoder has already strictly parsed and validated this same byte
    string. This bounded second pass recovers fiscal period metadata omitted
    from its quarantine references and verifies that every response row is
    accounted for by exactly one observation or quarantine reference.
    """
    if type(raw_body) is not bytes or not raw_body or len(raw_body) > MAX_BODY_BYTES:
        raise FinancialObservationFetchError("response_row_metadata_invalid")
    try:
        payload = json.loads(raw_body.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise FinancialObservationFetchError("response_row_metadata_invalid") from None
    if type(payload) is not dict:
        raise FinancialObservationFetchError("response_row_metadata_invalid")
    data = payload.get("data")
    rows = data.get("item") if type(data) is dict else None
    if type(rows) is not list or not rows or len(rows) > MAX_FINANCIAL_ROWS:
        raise FinancialObservationFetchError("response_row_metadata_invalid")

    expected_pointers = {f"/data/item/{index}" for index in range(len(rows))}
    observation_pointers = {reference.row_pointer for reference in observations}
    quarantine_pointers = [row.row_pointer for row in quarantined_rows]
    quarantine_pointer_set = set(quarantine_pointers)
    if (
        len(quarantine_pointer_set) != len(quarantine_pointers)
        or observation_pointers.intersection(quarantine_pointer_set)
        or observation_pointers.union(quarantine_pointer_set) != expected_pointers
    ):
        raise FinancialObservationFetchError("response_row_pointer_mismatch")

    report_periods: list[date] = []
    report_period_by_pointer: dict[str, date] = {}
    for row_index, row in enumerate(rows):
        if type(row) is not dict:
            raise FinancialObservationFetchError("response_row_metadata_invalid")
        year = row.get("fiscal_year")
        fiscal_period = row.get("fiscal_period")
        period_end_ms = row.get("period_end_ms")
        if type(year) is not int or type(fiscal_period) is not str:
            raise FinancialObservationFetchError("response_row_period_invalid")
        period_end = FINANCIAL_PERIOD_END.get(fiscal_period)
        if period_end is None:
            raise FinancialObservationFetchError("response_row_period_invalid")
        try:
            report_period = date(year, period_end[0], period_end[1])
            stamped_period = millis_to_trading_date(period_end_ms)
        except (OverflowError, OSError, TypeError, ValueError, RuntimeError):
            raise FinancialObservationFetchError("response_row_period_invalid") from None
        if report_period != stamped_period:
            raise FinancialObservationFetchError("response_row_period_invalid")
        report_periods.append(report_period)
        report_period_by_pointer[f"/data/item/{row_index}"] = report_period
    if any(
        reference.observation.report_period != report_period_by_pointer[reference.row_pointer]
        for reference in observations
    ):
        raise FinancialObservationFetchError("response_row_period_binding_mismatch")
    return tuple(report_periods)


def _utc_now() -> datetime:
    """Return the current aware UTC instant for a completed source response."""
    return datetime.now(timezone.utc)


__all__ = [
    "FinancialObservationFetchError",
    "FinancialObservationFetchResult",
    "ThsFinancialNativeUnitProfile",
    "ThsFinancialObservationFetcher",
    "canonical_native_unit_mapping_sha256",
]
