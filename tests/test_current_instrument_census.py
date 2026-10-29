"""Synthetic behavior checks for explicit instrument-set comparison."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from opendata.data.current_instrument_census import (
    CensusAcquisitionStatus,
    CensusSnapshot,
    CrossExchangeCollision,
    InstrumentCensusError,
    InstrumentIdentity,
    compare_census_snapshots,
)

_OBSERVED_AT = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)


def _identity(exchange: str, symbol: str) -> InstrumentIdentity:
    """Construct one identity from synthetic values only."""
    return InstrumentIdentity(exchange=exchange, symbol=symbol)


def _snapshot(
    source: str,
    identities: tuple[InstrumentIdentity, ...] | list[InstrumentIdentity],
    *,
    exchanges: tuple[str, ...] | list[str] = ("SH", "SZ", "BJ"),
    status: CensusAcquisitionStatus | str = CensusAcquisitionStatus.SUCCEEDED,
    observed_at: datetime = _OBSERVED_AT,
) -> CensusSnapshot:
    """Construct a bounded snapshot from synthetic, explicitly scoped values."""
    return CensusSnapshot(
        source_id=source,
        exchanges=exchanges,
        identities=identities,
        acquisition_status=status,
        observed_at=observed_at,
    )


def test_comparison_reports_sorted_gaps_extras_and_cross_exchange_collision() -> None:
    """Exchange qualification prevents bare-code overlap from hiding a mismatch."""
    reference = _snapshot(
        "akshare",
        (
            _identity("SH", "600001"),
            _identity("SZ", "000001"),
            _identity("SH", "000001"),
            _identity("BJ", "430001"),
        ),
    )
    candidate = _snapshot(
        "eastmoney",
        (
            _identity("SH", "600001"),
            _identity("SZ", "000001"),
            _identity("SZ", "000002"),
            _identity("BJ", "430001"),
        ),
    )

    result = compare_census_snapshots(reference, candidate)

    assert result.missing_from_candidate == (_identity("SH", "000001"),)
    assert result.extra_in_candidate == (_identity("SZ", "000002"),)
    assert result.cross_exchange_collisions == (
        CrossExchangeCollision(
            symbol="000001",
            reference_exchanges=("SH", "SZ"),
            candidate_exchanges=("SZ",),
        ),
    )
    assert result.reference_exchanges == ("BJ", "SH", "SZ")
    assert result.candidate_exchanges == ("BJ", "SH", "SZ")


def test_snapshot_copies_and_sorts_mutable_input() -> None:
    """Snapshot copies mutable input and sorts exchange scope for stable echo."""
    identities = [_identity("SZ", "000002"), _identity("SH", "600001")]
    exchanges = ["SZ", "SH"]

    snapshot = _snapshot("akshare", identities, exchanges=exchanges)
    identities.clear()
    exchanges.append("BJ")

    assert snapshot.identities == (_identity("SH", "600001"), _identity("SZ", "000002"))
    assert snapshot.exchanges == ("SH", "SZ")


def test_single_exchange_comparison_only_returns_set_differences() -> None:
    """A single-exchange pair has no completeness or readiness conclusion."""
    reference = _snapshot("sh.exchange-feed", (_identity("SH", "600001"),), exchanges=("SH",))
    candidate = _snapshot(
        "broker.catalog_v2", (), exchanges=("SH",), status=CensusAcquisitionStatus.PARTIAL
    )

    result = compare_census_snapshots(reference, candidate)

    assert result.missing_from_candidate == (_identity("SH", "600001"),)
    assert result.reference_source_id == "sh.exchange-feed"
    assert result.candidate_source_id == "broker.catalog_v2"
    assert result.reference_status is CensusAcquisitionStatus.SUCCEEDED
    assert result.candidate_status is CensusAcquisitionStatus.PARTIAL
    assert not hasattr(result, "assessment_ready")


def test_same_bare_code_on_multiple_exchanges_is_reported_even_when_sets_match() -> None:
    """A collision describes multi-exchange presence, not disagreement alone."""
    identities = (_identity("SH", "000001"), _identity("SZ", "000001"))
    reference = _snapshot("source_a", identities, exchanges=("SZ", "SH"))
    candidate = _snapshot("source_b", identities, exchanges=("SH", "SZ"))

    result = compare_census_snapshots(reference, candidate)

    assert result.missing_from_candidate == ()
    assert result.extra_in_candidate == ()
    assert result.cross_exchange_collisions == (
        CrossExchangeCollision(
            symbol="000001",
            reference_exchanges=("SH", "SZ"),
            candidate_exchanges=("SH", "SZ"),
        ),
    )
    assert result.reference_exchanges == ("SH", "SZ")
    assert result.candidate_exchanges == ("SH", "SZ")


def test_source_id_accepts_any_formatted_identifier_including_exchange_names() -> None:
    """Source IDs are format-checked without a hardcoded vendor allowlist."""
    snapshot = _snapshot("sse", (_identity("SH", "600001"),), exchanges=("SH",))
    assert snapshot.source_id == "sse"
    with pytest.raises(InstrumentCensusError, match="source_id"):
        _snapshot("Unregistered Vendor", (_identity("SH", "600001"),))


@pytest.mark.parametrize(
    ("exchange", "symbol"),
    [
        ("", "000001"),
        ("NYSE", "000001"),
        ("sh", "000001"),
        ("SH", "12345"),
        ("SH", "1234567"),
        ("SH", "１２３４５６"),
        ("SH", "12A456"),
    ],
)
def test_identity_rejects_unknown_exchange_and_noncanonical_symbol(
    exchange: str, symbol: str
) -> None:
    """No exchange inference, Unicode digit coercion, or symbol normalization."""
    with pytest.raises(InstrumentCensusError):
        _identity(exchange, symbol)


def test_snapshot_rejects_duplicate_identity() -> None:
    """One set cannot repeat an exchange-qualified identity."""
    row = _identity("SH", "600001")
    with pytest.raises(InstrumentCensusError, match="duplicates"):
        _snapshot("akshare", (row, row))


def test_snapshot_rejects_scope_errors_and_identity_outside_scope() -> None:
    """Each input has its own explicit, unique exchange scope."""
    with pytest.raises(InstrumentCensusError, match="duplicates"):
        _snapshot("akshare", (_identity("SH", "600001"),), exchanges=("SH", "SH"))
    with pytest.raises(InstrumentCensusError, match="only SH, SZ, or BJ"):
        _snapshot("akshare", (), exchanges=("NYSE",), status="failed")
    with pytest.raises(InstrumentCensusError, match="declared exchange scope"):
        _snapshot("akshare", (_identity("SZ", "000001"),), exchanges=("SH",))


def test_empty_identity_set_is_reported_without_a_completeness_claim() -> None:
    """An empty response remains an empty set even when its attempt says succeeded."""
    empty = _snapshot("akshare", (), exchanges=("SH",))

    result = compare_census_snapshots(empty, empty)

    assert empty.identities == ()
    assert result.missing_from_candidate == ()
    assert result.extra_in_candidate == ()
    assert not hasattr(result, "assessment_ready")


def test_invalid_attempt_state_is_rejected_without_complete_state() -> None:
    """Attempt-state vocabulary does not encode a dataset completeness verdict."""
    with pytest.raises(InstrumentCensusError, match="acquisition_status"):
        _snapshot("akshare", (), exchanges=("SH",), status="complete")


def test_different_scope_and_time_are_echoed_without_comparability_judgment() -> None:
    """Scope and timestamps remain visible metadata and do not block set diffing."""
    later_time = datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc)
    reference = _snapshot("akshare", (_identity("SH", "600001"),), exchanges=("SH",))
    candidate = _snapshot(
        "sina",
        (_identity("SZ", "000001"),),
        exchanges=("SZ",),
        status="failed",
        observed_at=later_time,
    )

    result = compare_census_snapshots(reference, candidate)

    assert result.reference_exchanges == ("SH",)
    assert result.candidate_exchanges == ("SZ",)
    assert result.reference_observed_at == _OBSERVED_AT
    assert result.candidate_observed_at == later_time
    assert result.reference_status is CensusAcquisitionStatus.SUCCEEDED
    assert result.candidate_status is CensusAcquisitionStatus.FAILED
    assert result.missing_from_candidate == (_identity("SH", "600001"),)
    assert result.extra_in_candidate == (_identity("SZ", "000001"),)
    assert not hasattr(result, "assessment_ready")


def test_comparison_rejects_wrong_input_types() -> None:
    """The report accepts only validated immutable snapshot values."""
    left = _snapshot("akshare", (_identity("SH", "600001"),), exchanges=("SH",))
    with pytest.raises(InstrumentCensusError, match="CensusSnapshot"):
        compare_census_snapshots(left, object())  # type: ignore[arg-type]


def test_snapshot_rejects_naive_timestamp() -> None:
    """Observation time must retain a timezone instead of relying on local time."""
    with pytest.raises(InstrumentCensusError, match="timezone-aware"):
        CensusSnapshot(
            source_id="akshare",
            exchanges=("SH",),
            identities=(_identity("SH", "600001"),),
            acquisition_status="succeeded",
            observed_at=datetime(2026, 10, 29),
        )


def test_snapshot_rejects_more_than_the_fixed_identity_limit() -> None:
    """Iterable materialization stops at the explicit safety bound plus one."""
    with pytest.raises(InstrumentCensusError, match="exceeds the 100,000-item limit"):
        _snapshot(
            "akshare",
            (_identity("SH", f"{index:06d}") for index in range(100_001)),
            exchanges=("SH",),
            status="partial",
        )
