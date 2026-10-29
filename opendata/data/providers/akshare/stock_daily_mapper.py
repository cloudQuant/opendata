"""Map source-native stock rows onto externally pinned temporal identities.

This module is an offline, all-or-nothing mapping boundary. It never fetches,
stores, repairs, or numerically interprets stock data. A caller must supply the
operator-validated pin registry and the matching identity registry obtained
from the trusted runtime or an equivalently reviewed boundary. The mapper
recomputes the canonical snapshot byte digest and validates its binding, but
cannot authenticate the caller, the pin issuer, source rights, or completeness.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    StockDailyNativeEnvelope,
    StockDailyNativeRequest,
)
from opendata.data.temporal_instrument_identity import (
    Exchange,
    IdentityResolution,
    IdentityResolutionSegment,
    IdentityResolutionStatus,
    MarketSymbolPair,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    TemporalIdentitySnapshotError,
    encode_temporal_identity_snapshot,
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$", re.ASCII)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_Identifier = Annotated[str, StringConstraints(strict=True, pattern=_IDENTIFIER_PATTERN.pattern)]
_Sha256 = Annotated[str, StringConstraints(strict=True, pattern=_SHA256_PATTERN.pattern)]


class _IdentityPinRegistry(Protocol):
    """Describe the exact dataset lookup exposed by the trusted runtime."""

    def get(self, dataset_id: str) -> object:
        """Return a pin bound to exactly one dataset, if configured."""


@dataclass(frozen=True, slots=True)
class _OperatorPinFields:
    """Validated field shape copied from the caller's operator pin context."""

    dataset_id: str
    source_id: str
    authority_id: str
    registry_id: str
    reference_version: str
    snapshot_sha256: str


class StockDailyNativeMappedRow(BaseModel):
    """One unchanged source row joined to its unique effective identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    dataset_id: _Identifier
    source_id: _Identifier
    authority_id: _Identifier
    registry_id: _Identifier
    identity_reference_version: _Identifier
    identity_snapshot_sha256: _Sha256
    instrument_id: _Identifier
    assignment_id: _Identifier
    exchange: Exchange
    symbol: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{6}$")]
    trade_date: date
    source_row_index: StrictInt = Field(ge=1)
    source_line: StrictStr
    raw_tokens: tuple[StrictStr, ...] = Field(min_length=11, max_length=11)

    @model_validator(mode="after")
    def validate_source_line(self) -> StockDailyNativeMappedRow:
        """Keep the source line byte-for-byte equivalent to its eleven tokens."""
        if tuple(self.source_line.split(",")) != self.raw_tokens:
            raise ValueError("mapped source line and raw tokens differ")
        if self.raw_tokens[0] not in {
            self.trade_date.isoformat(),
            self.trade_date.strftime("%Y%m%d"),
        }:
            raise ValueError("mapped trade date differs from its source token")
        return self


class StockDailyNativeMappedBatch(BaseModel):
    """An identity-bound source-native candidate without coverage claims."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: Literal["cn_equity.daily.eastmoney_push2his.native.v1"] = (
        "cn_equity.daily.eastmoney_push2his.native.v1"
    )
    adapter_id: Literal["akshare"] = "akshare"
    underlying_product_id: Literal["eastmoney.push2his.stock.kline"] = (
        "eastmoney.push2his.stock.kline"
    )
    endpoint_url: Literal["https://push2his.eastmoney.com/api/qt/stock/kline/get"] = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    )
    operation_id: Literal["stock-daily-wire"] = "stock-daily-wire"
    mapping_status: Literal["identity_bound_candidate"] = "identity_bound_candidate"
    dataset_id: _Identifier
    source_id: _Identifier
    authority_id: _Identifier
    registry_id: _Identifier
    identity_reference_version: _Identifier
    identity_snapshot_sha256: _Sha256
    request: StockDailyNativeRequest
    capture_id: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
    manifest_sha256: _Sha256
    request_summary_sha256: _Sha256
    response_sha256: _Sha256
    response_size_bytes: StrictInt = Field(ge=0)
    http_status: StrictInt = Field(ge=100, le=599)
    observed_at_utc: datetime | None = None
    observed_at_status: Literal["unavailable", "caller_declared_unverified"]
    captured_at_utc: datetime
    retention_class: Literal["unknown", "ephemeral", "restricted", "durable_candidate"]
    source_history_limit: Literal[
        "unknown",
        "provider_retention_window",
        "snapshot_only",
        "partial_history",
        "none_documented",
    ]
    evidence_integrity_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    source_identity_status: Literal["unverified"] = "unverified"
    requested_adjustment_parameter: Literal["fqt=0"] = "fqt=0"
    adjustment_semantics: Literal["unverified"] = "unverified"
    price_unit_semantics: Literal["unknown"] = "unknown"
    volume_unit_semantics: Literal["unknown"] = "unknown"
    amount_unit_semantics: Literal["unknown"] = "unknown"
    source_authorization_status: Literal["unknown"] = "unknown"
    retention_status: Literal["caller_declared_unverified"] = "caller_declared_unverified"
    history_coverage_status: Literal["unknown"] = "unknown"
    missing_date_interpretation: Literal["unknown"] = "unknown"
    rows: tuple[StockDailyNativeMappedRow, ...] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_rows(self) -> StockDailyNativeMappedBatch:
        """Keep row identity, request scope, and response keys consistent."""
        seen_dates: set[date] = set()
        seen_indexes: set[int] = set()
        for row in self.rows:
            if (
                row.dataset_id != self.dataset_id
                or row.source_id != self.source_id
                or row.authority_id != self.authority_id
                or row.registry_id != self.registry_id
                or row.identity_reference_version != self.identity_reference_version
                or row.identity_snapshot_sha256 != self.identity_snapshot_sha256
            ):
                raise ValueError("mapped row pin differs from its batch pin")
            if row.trade_date in seen_dates or row.source_row_index in seen_indexes:
                raise ValueError("mapped batch contains duplicate source rows")
            if not self.request.start_date <= row.trade_date <= self.request.end_date:
                raise ValueError("mapped row is outside the requested business-date window")
            if row.exchange is not _exchange_for_request(self.request):
                raise ValueError("mapped row exchange differs from the native request")
            if row.symbol != self.request.symbol:
                raise ValueError("mapped row symbol differs from the native request")
            seen_dates.add(row.trade_date)
            seen_indexes.add(row.source_row_index)
        if tuple(sorted(self.rows, key=lambda item: item.trade_date)) != self.rows:
            raise ValueError("mapped rows must be sorted by trade date")
        return self


def map_stock_daily_native_envelope(
    envelope: StockDailyNativeEnvelope,
    *,
    operator_pin_registry: _IdentityPinRegistry,
    identity_registry: TemporalIdentityRegistry,
) -> StockDailyNativeMappedBatch:
    """Bind each unchanged native row to a pinned, time-valid instrument ID.

    The complete request interval is checked as the half-open interval
    ``[start_date, end_date + 1 day)``. Every day must resolve to a known
    assignment, every returned date must resolve to exactly one assignment,
    and duplicate business dates reject the whole response. No dates are
    synthesized, and an empty response is not evidence of an empty history.

    Args:
        envelope: A validated native response with local raw-evidence metadata.
        operator_pin_registry: The pin registry that the caller already
            validated using its trusted runtime or reviewed HTTP boundary.
            This function checks pin fields against the dataset and canonical
            snapshot bytes; it does not authenticate who supplied the registry.
        identity_registry: The immutable temporal registry returned for that
            pin by the trusted runtime. Its canonical bytes must match the
            independently pinned complete-snapshot SHA-256.

    Returns:
        A bounded source-native batch retaining every raw token and attaching
        the unique instrument and assignment IDs effective on each trade date.

    Raises:
        ValueError: If evidence, pin, source, window, identity, or rows disagree.
        TypeError: If the caller supplies unsupported envelope or registry types.
    """
    valid_envelope = _revalidate_envelope(envelope)
    evidence = valid_envelope.evidence
    if evidence is None:
        raise ValueError("stock daily mapping requires capture evidence metadata/reference")
    if type(identity_registry) is not TemporalIdentityRegistry:
        raise TypeError("identity_registry must be a TemporalIdentityRegistry")

    pin = _lookup_pin(operator_pin_registry, evidence.dataset_id)
    _validate_pin_binding(
        pin,
        evidence.dataset_id,
        evidence.provider_id,
        identity_registry,
    )
    _validate_source_binding(pin, identity_registry)
    resolution = _resolve_request_window(valid_envelope.request, identity_registry)
    _require_fully_known(resolution.segments)

    if not valid_envelope.rows:
        raise ValueError("empty stock daily response cannot be mapped")
    if valid_envelope.duplicate_trade_dates:
        raise ValueError("duplicate stock daily trade dates cannot be mapped")

    rows: list[StockDailyNativeMappedRow] = []
    seen_dates: set[date] = set()
    for row in valid_envelope.rows:
        if row.trade_date in seen_dates:
            raise ValueError("duplicate stock daily trade dates cannot be mapped")
        seen_dates.add(row.trade_date)
        assignments = [
            segment
            for segment in resolution.segments
            if segment.valid_from <= row.trade_date < segment.valid_to
        ]
        if len(assignments) != 1:
            raise ValueError("stock daily row does not have one temporal identity assignment")
        segment = assignments[0]
        if (
            segment.status is not IdentityResolutionStatus.KNOWN
            or segment.instrument_id is None
            or segment.assignment_id is None
            or segment.pair is None
            or segment.pair.exchange is not _exchange_for_request(valid_envelope.request)
            or segment.pair.symbol != valid_envelope.request.symbol
            or segment.source_id != pin.source_id
        ):
            raise ValueError("stock daily row identity does not match its pinned request")
        rows.append(
            StockDailyNativeMappedRow(
                dataset_id=pin.dataset_id,
                source_id=pin.source_id,
                authority_id=pin.authority_id,
                registry_id=pin.registry_id,
                identity_reference_version=pin.reference_version,
                identity_snapshot_sha256=pin.snapshot_sha256,
                instrument_id=segment.instrument_id,
                assignment_id=segment.assignment_id,
                exchange=Exchange(segment.pair.exchange),
                symbol=segment.pair.symbol,
                trade_date=row.trade_date,
                source_row_index=row.row_index,
                source_line=row.source_line,
                raw_tokens=row.raw_tokens,
            )
        )

    rows.sort(key=lambda item: item.trade_date)
    return StockDailyNativeMappedBatch(
        dataset_id=pin.dataset_id,
        source_id=pin.source_id,
        authority_id=pin.authority_id,
        registry_id=pin.registry_id,
        identity_reference_version=pin.reference_version,
        identity_snapshot_sha256=pin.snapshot_sha256,
        request=valid_envelope.request,
        capture_id=evidence.capture_id,
        manifest_sha256=evidence.manifest_sha256,
        request_summary_sha256=evidence.request_summary_sha256,
        response_sha256=valid_envelope.response_sha256,
        response_size_bytes=valid_envelope.response_size_bytes,
        http_status=evidence.http_status,
        observed_at_utc=valid_envelope.observed_at_utc,
        observed_at_status=valid_envelope.observed_at_status,
        captured_at_utc=evidence.captured_at_utc,
        retention_class=evidence.retention_class,
        source_history_limit=evidence.source_history_limit,
        rows=tuple(rows),
    )


def _revalidate_envelope(envelope: StockDailyNativeEnvelope) -> StockDailyNativeEnvelope:
    """Re-run model validation even if the caller used Pydantic model_construct."""
    if type(envelope) is not StockDailyNativeEnvelope:
        raise TypeError("envelope must be a StockDailyNativeEnvelope")
    try:
        return StockDailyNativeEnvelope.model_validate(envelope.model_dump(mode="python"))
    except Exception:
        raise ValueError("stock daily envelope fails native contract validation") from None


def _lookup_pin(pin_registry: _IdentityPinRegistry, dataset_id: str) -> _OperatorPinFields:
    """Read one exact dataset pin and reject malformed runtime facades."""
    lookup = getattr(pin_registry, "get", None)
    if not callable(lookup):
        raise TypeError("operator_pin_registry must expose an exact get lookup")
    try:
        pin = lookup(dataset_id)
    except Exception:
        raise ValueError("operator identity pin lookup failed") from None
    if pin is None:
        raise ValueError("operator identity pin is missing or malformed")
    field_names = (
        "dataset_id",
        "source_id",
        "authority_id",
        "registry_id",
        "reference_version",
        "snapshot_sha256",
    )
    try:
        values = tuple(getattr(pin, field) for field in field_names)
    except Exception:
        raise ValueError("operator identity pin is missing or malformed") from None
    if any(type(value) is not str for value in values):
        raise ValueError("operator identity pin is missing or malformed")
    return _OperatorPinFields(*values)


def _validate_pin_binding(
    pin: _OperatorPinFields,
    dataset_id: str,
    adapter_id: str,
    registry: TemporalIdentityRegistry,
) -> None:
    """Bind the exact dataset, version, and complete canonical bytes to a pin."""
    if (
        not _IDENTIFIER_PATTERN.fullmatch(pin.dataset_id)
        or not _IDENTIFIER_PATTERN.fullmatch(pin.source_id)
        or not _IDENTIFIER_PATTERN.fullmatch(pin.authority_id)
        or not _IDENTIFIER_PATTERN.fullmatch(pin.registry_id)
        or not _IDENTIFIER_PATTERN.fullmatch(pin.reference_version)
        or _SHA256_PATTERN.fullmatch(pin.snapshot_sha256) is None
    ):
        raise ValueError("operator identity pin fields are invalid")
    if pin.dataset_id != dataset_id:
        raise ValueError("operator identity pin dataset does not match capture evidence")
    if pin.source_id != adapter_id or adapter_id != ADAPTER_ID:
        raise ValueError("operator identity pin source does not match the native adapter")
    if pin.reference_version != registry.reference_version:
        raise ValueError("operator identity pin version does not match identity registry")
    try:
        snapshot_sha256 = hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()
    except TemporalIdentitySnapshotError:
        raise ValueError("identity registry cannot be encoded as a canonical snapshot") from None
    if not hmac.compare_digest(snapshot_sha256, pin.snapshot_sha256):
        raise ValueError("identity registry bytes do not match the operator pin")


def _validate_source_binding(pin: _OperatorPinFields, registry: TemporalIdentityRegistry) -> None:
    """Require this pinned registry to represent only the configured source."""
    if not registry.assignments or any(
        assignment.source_id != pin.source_id for assignment in registry.assignments
    ):
        raise ValueError("identity registry assignments do not match pinned source")


def _resolve_request_window(
    request: StockDailyNativeRequest,
    registry: TemporalIdentityRegistry,
) -> IdentityResolution:
    """Resolve the inclusive wire request using the domain's half-open dates."""
    try:
        exclusive_end = request.end_date + timedelta(days=1)
        return registry.resolve_pair(
            MarketSymbolPair(_exchange_for_request(request), request.symbol),
            request.start_date,
            exclusive_end,
        )
    except (OverflowError, TemporalInstrumentIdentityError):
        raise ValueError("stock daily request has an invalid temporal identity window") from None


def _require_fully_known(segments: tuple[IdentityResolutionSegment, ...]) -> None:
    """Reject any unknown calendar interval instead of inferring identity."""
    if not segments or any(
        segment.status is not IdentityResolutionStatus.KNOWN for segment in segments
    ):
        raise ValueError("stock daily request window contains unknown identity dates")


def _exchange_for_request(request: StockDailyNativeRequest) -> Exchange:
    """Translate the native SH/SZ route to the canonical exchange namespace."""
    return Exchange.XSHG if request.exchange == "SH" else Exchange.XSHE


__all__ = [
    "StockDailyNativeMappedBatch",
    "StockDailyNativeMappedRow",
    "map_stock_daily_native_envelope",
]
