"""Offline tests for native stock rows joined to pinned temporal identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest

import opendata.data.providers.akshare.stock_daily_mapper as mapper_module
from opendata.data.providers.akshare.stock_daily_mapper import (
    StockDailyNativeMappedBatch,
    StockDailyNativeMappedRow,
    map_stock_daily_native_envelope,
)
from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    OPERATION_ID,
    StockDailyNativeEnvelope,
    StockDailyNativeEvidence,
    StockDailyNativeRequest,
    parse_eastmoney_stock_daily_response,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.temporal_instrument_identity import (
    Exchange,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    TemporalIdentitySnapshotError,
    encode_temporal_identity_snapshot,
)

_DATASET_ID = "stock_daily"
_SOURCE_ID = ADAPTER_ID
_AUTHORITY_ID = "reviewed-market-identities"
_REGISTRY_ID = "cn-equity-listings"
_REFERENCE_VERSION = "identity-v1"
_CAPTURED_AT = datetime(2024, 1, 5, 1, 0, tzinfo=timezone.utc)
_OBSERVED_AT = datetime(2024, 1, 4, 8, 0, tzinfo=timezone.utc)
_LINES = (
    "2024-01-02,1.200,1.300,1.400,1.100,0010,20.00,2.0,3.0,0.1,0.5",
    "2024-01-03,1.300,1.350,1.450,1.250,0000,0,,NULL,0,",
)


@dataclass(frozen=True)
class _Pin:
    """Synthetic pin value for an offline mapper contract test."""

    dataset_id: str
    source_id: str
    authority_id: str
    registry_id: str
    reference_version: str
    snapshot_sha256: str


class _PinRegistry:
    """Minimal synthetic exact lookup matching the runtime's get-only shape."""

    def __init__(self, pin: _Pin) -> None:
        self._pin = pin

    def get(self, dataset_id: str) -> object:
        """Return the test pin only for its exact dataset key."""
        return self._pin if dataset_id == self._pin.dataset_id else None


def _request(
    *,
    start_date: str = "2024-01-01",
    end_date: str = "2024-01-04",
    symbol: str = "600519.SH",
    exchange: str = "SH",
) -> StockDailyNativeRequest:
    """Build one bounded synthetic Shanghai query."""
    return StockDailyNativeRequest.model_validate(
        {
            "symbol": symbol,
            "exchange": exchange,
            "start_date": start_date,
            "end_date": end_date,
        }
    )


def _registry(
    *,
    source_id: str = _SOURCE_ID,
    exchange: Exchange = Exchange.XSHG,
    symbol: str = "600519",
    starts: tuple[tuple[str, str | None], ...] = (
        ("2024-01-01", "2024-01-03"),
        ("2024-01-03", None),
    ),
) -> TemporalIdentityRegistry:
    """Build synthetic, non-overlapping assignments for one exchange and code."""
    rows = tuple(
        TemporalIdentityAssignment(
            assignment_id=f"assignment-{index}",
            instrument_id=f"instrument-{index}",
            exchange=exchange,
            symbol=symbol,
            valid_from=date.fromisoformat(start),
            valid_to=date.fromisoformat(end) if end is not None else None,
            source_id=source_id,
            source_version="fixture-v1",
            evidence_ref=f"TEST-IDENTITY-{index}",
        )
        for index, (start, end) in enumerate(starts, start=1)
    )
    return TemporalIdentityRegistry(
        reference_version=_REFERENCE_VERSION,
        evidence_ref="TEST-SYNTHETIC-IDENTITY-SOURCE",
        assignments=rows,
    )


def _envelope(
    *,
    dataset_id: str = _DATASET_ID,
    lines: tuple[str, ...] = _LINES,
    include_evidence: bool = True,
    request: StockDailyNativeRequest | None = None,
) -> StockDailyNativeEnvelope:
    """Build and validate an Eastmoney-shaped local synthetic response."""
    query = request or _request()
    body = json.dumps(
        {
            "rc": 0,
            "data": {"code": query.symbol, "market": query.market_id, "klines": list(lines)},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    parsed = parse_eastmoney_stock_daily_response(
        body,
        request=query,
        observed_at_utc=_OBSERVED_AT,
    )
    if not include_evidence:
        return parsed
    evidence = StockDailyNativeEvidence(
        capture_id="0123456789abcdef0123456789abcdef",
        provider_id=ADAPTER_ID,
        dataset_id=dataset_id,
        operation_id=OPERATION_ID,
        request_summary_sha256=request_summary_sha256(
            {
                "adjust": "",
                "end_date": query.end_date,
                "market": "cn",
                "source": query.request_source,
                "start_date": query.start_date,
                "symbol": query.symbol,
            },
            allowed_fields=("adjust", "end_date", "market", "source", "start_date", "symbol"),
        ),
        http_status=200,
        response_sha256=parsed.response_sha256,
        response_size_bytes=parsed.response_size_bytes,
        manifest_sha256="a" * 64,
        captured_at_utc=_CAPTURED_AT,
        retention_class="durable_candidate",
        source_history_limit="none_documented",
    )
    return StockDailyNativeEnvelope.model_validate(
        {**parsed.model_dump(mode="python"), "evidence": evidence}
    )


def _pinned_registry(
    registry: TemporalIdentityRegistry,
    *,
    dataset_id: str = _DATASET_ID,
    source_id: str = _SOURCE_ID,
    reference_version: str = _REFERENCE_VERSION,
    snapshot_sha256: str | None = None,
) -> _PinRegistry:
    """Bind synthetic runtime-shaped pin metadata to exact snapshot bytes."""
    digest = (
        snapshot_sha256 or hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()
    )
    return _PinRegistry(
        _Pin(
            dataset_id=dataset_id,
            source_id=source_id,
            authority_id=_AUTHORITY_ID,
            registry_id=_REGISTRY_ID,
            reference_version=reference_version,
            snapshot_sha256=digest,
        )
    )


def _map(
    envelope: StockDailyNativeEnvelope,
    registry: TemporalIdentityRegistry | None = None,
    *,
    pin_registry: _PinRegistry | None = None,
) -> StockDailyNativeMappedBatch:
    """Invoke the pure mapper with a snapshot-bound synthetic pin."""
    identity = registry or _registry()
    return map_stock_daily_native_envelope(
        envelope,
        operator_pin_registry=pin_registry or _pinned_registry(identity),
        identity_registry=identity,
    )


def _mapped_batch_payload() -> tuple[dict[str, object], list[dict[str, object]]]:
    """Return a valid mapper result as mutable input for model-boundary tests."""
    batch = _map(_envelope())
    rows = [row.model_dump(mode="python") for row in batch.rows]
    payload: dict[str, object] = batch.model_dump(mode="python")
    payload["rows"] = tuple(rows)
    return payload, rows


def test_maps_rows_by_effective_date_and_preserves_every_source_token() -> None:
    """Attach the correct identity across a handoff without changing native values."""
    batch = _map(_envelope())

    assert len(batch.rows) == 2
    assert [(row.trade_date, row.instrument_id, row.assignment_id) for row in batch.rows] == [
        (date(2024, 1, 2), "instrument-1", "assignment-1"),
        (date(2024, 1, 3), "instrument-2", "assignment-2"),
    ]
    assert batch.rows[0].raw_tokens == tuple(_LINES[0].split(","))
    assert batch.rows[0].source_line == _LINES[0]
    assert batch.rows[1].raw_tokens[4:7] == ("1.250", "0000", "0")
    assert batch.requested_adjustment_parameter == "fqt=0"
    assert batch.mapping_status == "identity_bound_candidate"
    assert batch.adjustment_semantics == "unverified"
    assert batch.price_unit_semantics == "unknown"
    assert batch.volume_unit_semantics == "unknown"
    assert batch.amount_unit_semantics == "unknown"
    assert batch.source_authorization_status == "unknown"
    assert batch.history_coverage_status == "unknown"
    assert batch.missing_date_interpretation == "unknown"
    assert batch.evidence_integrity_status == "caller_declared_unverified"
    assert batch.source_identity_status == "unverified"
    assert batch.retention_status == "caller_declared_unverified"


def test_request_inclusive_end_is_resolved_as_half_open_next_day() -> None:
    """Include the final requested date in identity resolution at its boundary."""
    request = _request(start_date="2024-01-03", end_date="2024-01-03")
    envelope = _envelope(request=request, lines=(_LINES[1],))

    batch = _map(envelope)

    assert batch.rows[0].trade_date == date(2024, 1, 3)
    assert batch.rows[0].instrument_id == "instrument-2"


def test_rejects_request_window_with_unknown_identity_even_if_rows_are_known() -> None:
    """Do not silently claim a pair has an identity throughout an unknown gap."""
    partial = _registry(starts=(("2024-01-02", "2024-01-03"), ("2024-01-03", None)))

    with pytest.raises(ValueError, match="contains unknown identity dates"):
        _map(_envelope(), partial)


def test_rejects_unknown_tail_in_entire_requested_identity_window() -> None:
    """Reject incomplete identity coverage even when every returned row is known."""
    partial = _registry(starts=(("2024-01-01", "2024-01-03"),))
    envelope = _envelope(lines=(_LINES[0], _LINES[1]))
    # The registry has no assignment after 2024-01-03 and the request ends on Jan 4.
    with pytest.raises(ValueError, match="contains unknown identity dates"):
        _map(envelope, partial)


def test_rejects_duplicate_business_dates_without_dropping_rows() -> None:
    """Reject the native parser's explicitly retained duplicate-date rows."""
    envelope = _envelope(lines=(_LINES[0], _LINES[0]))

    with pytest.raises(ValueError, match="duplicate stock daily trade dates"):
        _map(envelope)


def test_rejects_empty_response_instead_of_calling_it_complete_history() -> None:
    """An empty source response cannot become a publishable mapped batch."""
    envelope = _envelope(lines=())

    with pytest.raises(ValueError, match="empty stock daily response"):
        _map(envelope)


def test_requires_capture_evidence_metadata_for_dataset_pin_lookup() -> None:
    """Do not map body-only bytes that lack a dataset evidence reference."""
    envelope = _envelope(include_evidence=False)

    with pytest.raises(ValueError, match="requires capture evidence metadata/reference"):
        _map(envelope)


def test_rejects_missing_dataset_pin() -> None:
    """Require an exact pin lookup for the capture's dataset identifier."""
    envelope = _envelope(dataset_id="other_dataset")
    pin_registry = _pinned_registry(_registry())

    with pytest.raises(ValueError, match="pin is missing or malformed"):
        _map(envelope, pin_registry=pin_registry)


def test_rejects_pin_for_another_dataset_even_if_lookup_returns_it() -> None:
    """Defend against a broken pin facade returning a pin for a different key."""
    envelope = _envelope()
    registry = _registry()
    pin_registry = _pinned_registry(registry, dataset_id="other_dataset")

    class _AlwaysPin:
        """Synthetic malformed facade whose lookup ignores its requested key."""

        def get(self, dataset_id: str) -> object:
            """Return a mismatched pin to exercise caller-boundary validation."""
            del dataset_id
            return pin_registry.get("other_dataset")

    with pytest.raises(ValueError, match="dataset does not match"):
        map_stock_daily_native_envelope(
            envelope,
            operator_pin_registry=_AlwaysPin(),  # type: ignore[arg-type]
            identity_registry=registry,
        )


def test_rejects_complete_snapshot_digest_mismatch() -> None:
    """A self-consistent registry with different bytes cannot satisfy the pin."""
    registry = _registry()
    pin_registry = _pinned_registry(registry, snapshot_sha256="f" * 64)

    with pytest.raises(ValueError, match="do not match the operator pin"):
        _map(_envelope(), registry, pin_registry=pin_registry)


def test_rejects_pin_reference_version_mismatch() -> None:
    """Require the exact registry version in addition to matching bytes."""
    registry = _registry()
    pin_registry = _pinned_registry(registry, reference_version="identity-v2")

    with pytest.raises(ValueError, match="version does not match"):
        _map(_envelope(), registry, pin_registry=pin_registry)


def test_rejects_registry_assignments_from_another_source() -> None:
    """The operator-pinned source must bind every identity assignment."""
    registry = _registry(source_id="other_source")
    pin_registry = _pinned_registry(registry)

    with pytest.raises(ValueError, match="assignments do not match pinned source"):
        _map(_envelope(), registry, pin_registry=pin_registry)


def test_rejects_pin_source_that_differs_from_assignment_source() -> None:
    """Reject a correctly hashed snapshot whose assignment source is misbound."""
    registry = _registry()
    pin_registry = _pinned_registry(registry, source_id="other_source")

    with pytest.raises(ValueError, match="does not match the native adapter"):
        _map(_envelope(), registry, pin_registry=pin_registry)


def test_revalidates_envelope_built_without_model_validation() -> None:
    """Do not let model_construct bypass native envelope duplicate summaries."""
    parsed = _envelope()
    malformed = parsed.model_construct(
        **{**parsed.model_dump(mode="python"), "response_symbol": "000001"}
    )

    with pytest.raises(ValueError, match="fails native contract validation"):
        _map(malformed)


def test_rejects_malformed_pin_facade_without_disclosing_exception_details() -> None:
    """Normalize an operator lookup failure to a content-free mapping error."""

    class _BrokenPinRegistry:
        """Facade that simulates an unavailable trusted runtime lookup."""

        def get(self, dataset_id: str) -> object:
            """Raise an exception whose detail must not escape the mapper."""
            del dataset_id
            raise RuntimeError("private pin bytes")

    with pytest.raises(ValueError, match="operator identity pin lookup failed") as captured:
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=_BrokenPinRegistry(),  # type: ignore[arg-type]
            identity_registry=_registry(),
        )
    assert "private pin bytes" not in str(captured.value)


def test_rejects_unsupported_identity_registry_type() -> None:
    """Require the domain registry type without claiming to authenticate its source."""
    with pytest.raises(TypeError, match="identity_registry must be a TemporalIdentityRegistry"):
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=_pinned_registry(_registry()),
            identity_registry=object(),  # type: ignore[arg-type]
        )


def test_rejects_an_unsupported_envelope_type_before_mapping() -> None:
    """Reject arbitrary Python objects instead of treating them as native captures."""
    with pytest.raises(TypeError, match="envelope must be a StockDailyNativeEnvelope"):
        map_stock_daily_native_envelope(
            object(),  # type: ignore[arg-type]
            operator_pin_registry=_pinned_registry(_registry()),
            identity_registry=_registry(),
        )


def test_rejects_a_pin_facade_without_an_exact_get_method() -> None:
    """Reject a pin-registry object that does not expose a callable get lookup."""
    with pytest.raises(TypeError, match="must expose an exact get lookup"):
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=object(),  # type: ignore[arg-type]
            identity_registry=_registry(),
        )


def test_rejects_pin_properties_that_fail_during_metadata_read() -> None:
    """Normalize a hostile or unavailable pin property to a fixed error."""

    class _PropertyFailurePin:
        @property
        def dataset_id(self) -> str:
            raise RuntimeError("private metadata")

    class _PropertyFailureRegistry:
        def get(self, dataset_id: str) -> object:
            del dataset_id
            return _PropertyFailurePin()

    with pytest.raises(ValueError, match="operator identity pin is missing or malformed") as exc:
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=_PropertyFailureRegistry(),  # type: ignore[arg-type]
            identity_registry=_registry(),
        )
    assert "private metadata" not in str(exc.value)


def test_rejects_non_string_pin_metadata() -> None:
    """Reject typed values that are not exact strings before regex validation."""

    class _NonStringPin:
        dataset_id = 17
        source_id = _SOURCE_ID
        authority_id = _AUTHORITY_ID
        registry_id = _REGISTRY_ID
        reference_version = _REFERENCE_VERSION
        snapshot_sha256 = "a" * 64

    class _NonStringPinRegistry:
        def get(self, dataset_id: str) -> object:
            del dataset_id
            return _NonStringPin()

    with pytest.raises(ValueError, match="operator identity pin is missing or malformed"):
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=_NonStringPinRegistry(),  # type: ignore[arg-type]
            identity_registry=_registry(),
        )


def test_rejects_malformed_pin_identifier_fields() -> None:
    """Validate every opaque pin identifier before comparing binding values."""
    registry = _registry()
    valid_pin = _pinned_registry(registry)._pin
    bad_pin = _Pin(
        dataset_id=valid_pin.dataset_id,
        source_id=valid_pin.source_id,
        authority_id="authority with spaces",
        registry_id=valid_pin.registry_id,
        reference_version=valid_pin.reference_version,
        snapshot_sha256=valid_pin.snapshot_sha256,
    )

    class _FixedPinRegistry:
        def get(self, dataset_id: str) -> object:
            del dataset_id
            return bad_pin

    with pytest.raises(ValueError, match="operator identity pin fields are invalid"):
        map_stock_daily_native_envelope(
            _envelope(),
            operator_pin_registry=_FixedPinRegistry(),  # type: ignore[arg-type]
            identity_registry=registry,
        )


def test_hides_canonical_snapshot_encoding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return a fixed binding error without leaking snapshot codec internals."""
    registry = _registry()
    pin_registry = _pinned_registry(registry)

    def fail_encoding(identity_registry: TemporalIdentityRegistry) -> bytes:
        del identity_registry
        raise TemporalIdentitySnapshotError("private snapshot details")

    monkeypatch.setattr(mapper_module, "encode_temporal_identity_snapshot", fail_encoding)
    with pytest.raises(
        ValueError, match="identity registry cannot be encoded as a canonical snapshot"
    ) as exc:
        _map(_envelope(), registry, pin_registry=pin_registry)
    assert "private snapshot details" not in str(exc.value)


def test_rejects_a_request_ending_on_maximum_date_before_resolution() -> None:
    """Fail closed when converting the inclusive maximum date to a half-open window."""
    request = _request(start_date="9999-12-31", end_date="9999-12-31")
    line = "9999-12-31,1,1,1,1,1,1,1,1,1,1"

    with pytest.raises(ValueError, match="invalid temporal identity window"):
        _map(_envelope(request=request, lines=(line,)))


def test_mapped_row_rejects_tokens_that_do_not_match_source_line() -> None:
    """Keep the output row's lexical tokens bound to its original source line."""
    with pytest.raises(ValueError, match="source line and raw tokens differ"):
        StockDailyNativeMappedRow(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            authority_id=_AUTHORITY_ID,
            registry_id=_REGISTRY_ID,
            identity_reference_version=_REFERENCE_VERSION,
            identity_snapshot_sha256="a" * 64,
            instrument_id="instrument-1",
            assignment_id="assignment-1",
            exchange=Exchange.XSHG,
            symbol="600519",
            trade_date=date(2024, 1, 2),
            source_row_index=1,
            source_line=_LINES[0],
            raw_tokens=(*_LINES[0].split(",")[:-1], "changed"),
        )


def test_mapped_row_rejects_trade_date_that_differs_from_raw_date_token() -> None:
    """Retain the parsed business date only when its original token matches."""
    with pytest.raises(ValueError, match="trade date differs from its source token"):
        StockDailyNativeMappedRow(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            authority_id=_AUTHORITY_ID,
            registry_id=_REGISTRY_ID,
            identity_reference_version=_REFERENCE_VERSION,
            identity_snapshot_sha256="a" * 64,
            instrument_id="instrument-1",
            assignment_id="assignment-1",
            exchange=Exchange.XSHG,
            symbol="600519",
            trade_date=date(2024, 1, 3),
            source_row_index=1,
            source_line=_LINES[0],
            raw_tokens=tuple(_LINES[0].split(",")),
        )


def test_maps_sz_request_to_the_canonical_shenzhen_exchange() -> None:
    """Keep the native SZ route aligned with the XSHE identity namespace."""
    request = _request(
        start_date="2024-01-02",
        end_date="2024-01-02",
        symbol="000001.SZ",
        exchange="SZ",
    )
    registry = _registry(
        exchange=Exchange.XSHE,
        symbol="000001",
        starts=(("2024-01-01", None),),
    )

    batch = _map(_envelope(request=request, lines=(_LINES[0],)), registry)

    assert batch.rows[0].exchange is Exchange.XSHE
    assert batch.rows[0].symbol == "000001"


def test_mapped_batch_rejects_a_row_with_a_different_pin() -> None:
    """Do not permit a row's dataset or identity snapshot to escape the batch pin."""
    payload, rows = _mapped_batch_payload()
    rows[0]["registry_id"] = "another-registry"

    with pytest.raises(ValueError, match="mapped row pin differs"):
        StockDailyNativeMappedBatch.model_validate(payload)


def test_mapped_batch_rejects_duplicate_source_row_indexes() -> None:
    """A source response row index is unique even when business dates differ."""
    payload, rows = _mapped_batch_payload()
    rows[1]["source_row_index"] = rows[0]["source_row_index"]

    with pytest.raises(ValueError, match="duplicate source rows"):
        StockDailyNativeMappedBatch.model_validate(payload)


def test_mapped_batch_rejects_rows_outside_the_native_request_window() -> None:
    """Require each source business date to remain within the inclusive query."""
    payload, rows = _mapped_batch_payload()
    tokens = _LINES[0].split(",")
    tokens[0] = "2024-01-05"
    rows[0]["trade_date"] = date(2024, 1, 5)
    rows[0]["source_line"] = ",".join(tokens)
    rows[0]["raw_tokens"] = tuple(tokens)

    with pytest.raises(ValueError, match="outside the requested business-date window"):
        StockDailyNativeMappedBatch.model_validate(payload)


def test_mapped_batch_rejects_an_exchange_mismatch() -> None:
    """Do not join a source row to an identity from the other exchange."""
    payload, rows = _mapped_batch_payload()
    rows[0]["exchange"] = Exchange.XSHE

    with pytest.raises(ValueError, match="exchange differs from the native request"):
        StockDailyNativeMappedBatch.model_validate(payload)


def test_mapped_batch_rejects_a_symbol_mismatch() -> None:
    """The six-digit output symbol must be the one present in the request."""
    payload, rows = _mapped_batch_payload()
    rows[0]["symbol"] = "000001"

    with pytest.raises(ValueError, match="symbol differs from the native request"):
        StockDailyNativeMappedBatch.model_validate(payload)


def test_mapped_batch_rejects_rows_not_sorted_by_business_date() -> None:
    """Keep output ordering deterministic without sorting malformed caller rows."""
    payload, rows = _mapped_batch_payload()
    payload["rows"] = (rows[1], rows[0])

    with pytest.raises(ValueError, match="must be sorted by trade date"):
        StockDailyNativeMappedBatch.model_validate(payload)
