"""Pure in-memory behavior tests for financial backfill manifest planning."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from opendata.pipeline.financial_backfill_manifest import (
    ALL_A_SHARE_EXCHANGES,
    ALL_A_SHARE_SCOPE_ID,
    MAX_FINANCIAL_WINDOW_SPAN_DAYS,
    MAX_SECURITY_ROWS,
    Exchange,
    FinancialManifestError,
    FinancialPlanBlockReason,
    FinancialSecurityEvidence,
    FinancialSecurityManifest,
    FinancialSourceCapabilityEvidence,
    FinancialUniverseEvidence,
    LifecycleStatus,
    ReportingPeriod,
    StatementType,
    UniverseEvidenceKind,
    build_financial_backfill_plan,
    fingerprint_security_manifest,
)

AS_OF = date(2026, 10, 26)
PLANNED_AT = datetime(2026, 10, 26, 16, 0, tzinfo=timezone(timedelta(hours=8)))
OBSERVED_AT = datetime(2026, 10, 26, 15, 0, tzinfo=timezone(timedelta(hours=8)))
ALL_STATEMENTS = tuple(StatementType)
ALL_PERIODS = tuple(ReportingPeriod)


def _security(
    ticker: str,
    exchange: Exchange,
    *,
    listing_date: date | None = date(2020, 1, 1),
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE,
    lifecycle_as_of: date = AS_OF,
    delisted_on: date | None = None,
    source_symbol: str | None = None,
    missing_source_symbol: bool = False,
    security_history_unknown: bool = False,
    security_history_available_from: date = date(1990, 1, 1),
) -> FinancialSecurityEvidence:
    """Build a reviewed fixture row with exchange-matched provider mapping."""
    suffix = {Exchange.SSE: "SH", Exchange.SZSE: "SZ", Exchange.BSE: "BJ"}[exchange]
    mapped_symbol = (
        None
        if missing_source_symbol
        else source_symbol
        if source_symbol is not None
        else f"{ticker}.{suffix}"
    )
    delisted = lifecycle_status is LifecycleStatus.DELISTED
    return FinancialSecurityEvidence(
        ticker=ticker,
        exchange=exchange,
        listing_date=listing_date,
        listing_evidence_ref="listing:fixture-01" if listing_date else None,
        lifecycle_status=lifecycle_status,
        lifecycle_as_of=lifecycle_as_of,
        lifecycle_evidence_ref="lifecycle:fixture-01",
        delisted_on=delisted_on or (date(2023, 6, 1) if delisted else None),
        delisting_evidence_ref="delisting:fixture-01" if delisted else None,
        source_symbol=mapped_symbol,
        source_symbol_evidence_ref="source-symbol:fixture-01" if mapped_symbol else None,
        source_history_boundary_unknown=security_history_unknown,
        source_history_available_from=(
            None if security_history_unknown else security_history_available_from
        ),
        source_history_evidence_ref=(
            None if security_history_unknown else "security-history:fixture-01"
        ),
    )


def _manifest(
    securities: tuple[FinancialSecurityEvidence, ...] | None = None,
    *,
    evidence_kind: UniverseEvidenceKind = UniverseEvidenceKind.REVIEWED_SECURITY_MASTER,
    scope_id: str = ALL_A_SHARE_SCOPE_ID,
    exchanges: frozenset[Exchange] = ALL_A_SHARE_EXCHANGES,
    includes_delisted: bool = True,
    includes_listing_boundaries: bool = True,
    member_count_delta: int = 0,
    digest: str | None = None,
) -> FinancialSecurityManifest:
    """Bind a frozen fixture member set to explicit universe scope evidence."""
    rows = securities or (
        _security("600001", Exchange.SSE),
        _security("000001", Exchange.SZSE),
        _security("920001", Exchange.BSE),
        _security(
            "600002",
            Exchange.SSE,
            lifecycle_status=LifecycleStatus.DELISTED,
            delisted_on=date(2023, 6, 1),
        ),
    )
    member_digest = fingerprint_security_manifest(rows) if digest is None else digest
    evidence = FinancialUniverseEvidence(
        scope_id=scope_id,
        registry_version="security-master-v7",
        evidence_ref="review:all-a-share-security-master-v7",
        evidence_kind=evidence_kind,
        as_of=AS_OF,
        expected_member_count=len(rows) + member_count_delta,
        members_sha256=member_digest,
        exchanges=exchanges,
        includes_delisted=includes_delisted,
        includes_listing_boundaries=includes_listing_boundaries,
    )
    return FinancialSecurityManifest(evidence=evidence, securities=rows)


def _capability(
    *,
    supported_exchanges: frozenset[Exchange] = ALL_A_SHARE_EXCHANGES,
    supported_statements: tuple[StatementType, ...] = ALL_STATEMENTS,
    supported_periods: tuple[ReportingPeriod, ...] = ALL_PERIODS,
    max_window_span_days: int = MAX_FINANCIAL_WINDOW_SPAN_DAYS,
    history_boundary_unknown: bool = False,
    history_available_from: date | None = date(1990, 1, 1),
    history_boundary_evidence_ref: str | None = "source-history:fixture-01",
) -> FinancialSourceCapabilityEvidence:
    """Build explicit provider capability evidence for pure planner fixtures."""
    return FinancialSourceCapabilityEvidence(
        source_id="ths",
        version="fuyao-financial-v3",
        evidence_ref="source-capability:fixture-01",
        observed_at=OBSERVED_AT,
        max_window_span_days=max_window_span_days,
        supported_exchanges=supported_exchanges,
        supported_statements=supported_statements,
        supported_periods=supported_periods,
        history_boundary_unknown=history_boundary_unknown,
        history_available_from=history_available_from,
        history_boundary_evidence_ref=history_boundary_evidence_ref,
    )


def _plan(
    manifest: FinancialSecurityManifest | None = None,
    capability: FinancialSourceCapabilityEvidence | None = None,
    *,
    as_of: date = AS_OF,
    planned_at: datetime = PLANNED_AT,
):
    """Build a plan with an explicit fixture observation time and no I/O."""
    return build_financial_backfill_plan(
        manifest or _manifest(),
        capability or _capability(),
        as_of=as_of,
        planned_observation_at=planned_at,
    )


def _reasons(blockers: tuple[object, ...]) -> set[FinancialPlanBlockReason]:
    """Return typed reason values for concise, behavior-focused assertions."""
    return {blocker.reason for blocker in blockers}  # type: ignore[attr-defined]


def test_all_six_statement_period_series_are_created_for_every_explicit_security() -> None:
    plan = _plan()

    assert len(plan.series) == 4 * len(ALL_STATEMENTS) * len(ALL_PERIODS)
    assert {
        (series.instrument_id, series.statement_type, series.reporting_period)
        for series in plan.series
    } == {
        (security_id, statement, period)
        for security_id in (
            "cn-equity:SSE:600001",
            "cn-equity:SZSE:000001",
            "cn-equity:BSE:920001",
            "cn-equity:SSE:600002",
        )
        for statement in ALL_STATEMENTS
        for period in ALL_PERIODS
    }
    assert all(series.candidates for series in plan.series)


def test_plan_records_observation_context_and_never_claims_pit_or_dispatch() -> None:
    plan = _plan()

    assert plan.planned_observation_at == PLANNED_AT.astimezone(timezone.utc)
    assert plan.historical_pit_supported is False
    assert plan.revision_history_available is False
    assert plan.announce_date_is_observation_time is False
    assert plan.history_reconciled is False
    assert plan.execution_authorized is False
    assert all(candidate.dispatch_authorized is False for candidate in plan.candidates)
    assert all(not hasattr(candidate, "request") for candidate in plan.candidates)


def test_long_history_is_split_into_gap_free_windows_with_endpoint_span_cap() -> None:
    security = _security("600001", Exchange.SSE, listing_date=date(2000, 1, 1))
    plan = _plan(_manifest((security,)), _capability())
    candidates = next(
        series
        for series in plan.series
        if series.statement_type is StatementType.INCOME
        and series.reporting_period is ReportingPeriod.ANNUAL
    ).candidates

    assert len(candidates) > 1
    assert candidates[0].window_start == date(2000, 1, 1)
    assert candidates[-1].window_end == AS_OF
    assert all(
        candidate.window_span_days <= MAX_FINANCIAL_WINDOW_SPAN_DAYS for candidate in candidates
    )
    assert all(
        right.window_start == left.window_end + timedelta(days=1)
        for left, right in zip(candidates, candidates[1:], strict=False)
    )


def test_window_chunking_preserves_a_leap_day_start_without_gaps() -> None:
    leap_day = date(2016, 2, 29)
    security = _security("600001", Exchange.SSE, listing_date=leap_day)
    plan = _plan(_manifest((security,)), _capability())
    candidates = next(
        series
        for series in plan.series
        if series.statement_type is StatementType.INCOME
        and series.reporting_period is ReportingPeriod.ANNUAL
    ).candidates

    assert candidates[0].window_start == leap_day
    assert candidates[-1].window_end == AS_OF
    assert all(
        right.window_start == left.window_end + timedelta(days=1)
        for left, right in zip(candidates, candidates[1:], strict=False)
    )
    assert all(
        candidate.window_span_days <= MAX_FINANCIAL_WINDOW_SPAN_DAYS for candidate in candidates
    )


def test_candidate_ids_are_stable_across_plan_observation_times() -> None:
    first = _plan()
    second = _plan(planned_at=PLANNED_AT + timedelta(hours=1))

    assert [item.stable_id for item in first.candidates] == [
        item.stable_id for item in second.candidates
    ]
    assert first.planned_observation_at != second.planned_observation_at


def test_source_snapshot_never_establishes_all_a_share_universe_completeness() -> None:
    plan = _plan(_manifest(evidence_kind=UniverseEvidenceKind.SOURCE_SNAPSHOT))

    assert plan.member_set_claim_consistent is True
    assert plan.member_set_independently_verified is False
    assert plan.lifecycle_boundaries_complete is True
    assert plan.all_a_share_universe_verified is False
    assert FinancialPlanBlockReason.SNAPSHOT_CANNOT_PROVE_UNIVERSE in _reasons(
        plan.universe_blockers
    )


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"scope_id": "short-watchlist"}, FinancialPlanBlockReason.UNIVERSE_SCOPE_ID_MISMATCH),
        ({"member_count_delta": 1}, FinancialPlanBlockReason.UNIVERSE_MEMBER_COUNT_MISMATCH),
        ({"digest": "0" * 64}, FinancialPlanBlockReason.UNIVERSE_MEMBER_DIGEST_MISMATCH),
        (
            {"exchanges": frozenset({Exchange.SSE, Exchange.SZSE})},
            FinancialPlanBlockReason.UNIVERSE_EXCHANGE_SCOPE_MISMATCH,
        ),
        (
            {"includes_delisted": False},
            FinancialPlanBlockReason.UNIVERSE_DELISTED_SECURITIES_OMITTED,
        ),
        (
            {"includes_listing_boundaries": False},
            FinancialPlanBlockReason.UNIVERSE_LISTING_BOUNDARIES_OMITTED,
        ),
    ],
)
def test_universe_claim_mismatches_are_retained_as_blockers(
    kwargs: dict[str, object], expected_reason: FinancialPlanBlockReason
) -> None:
    plan = _plan(_manifest(**kwargs))

    assert plan.all_a_share_universe_verified is False
    relevant_blockers = (
        plan.lifecycle_boundary_blockers
        if expected_reason is FinancialPlanBlockReason.UNIVERSE_LISTING_BOUNDARIES_OMITTED
        else plan.universe_blockers
    )
    assert expected_reason in _reasons(relevant_blockers)


def test_manifest_without_bse_member_cannot_claim_all_a_share_scope() -> None:
    rows = (_security("600001", Exchange.SSE), _security("000001", Exchange.SZSE))
    plan = _plan(_manifest(rows))

    assert plan.member_set_claim_consistent is False
    assert plan.all_a_share_universe_verified is False
    assert FinancialPlanBlockReason.UNIVERSE_EXCHANGE_MEMBERS_MISSING in _reasons(
        plan.universe_blockers
    )


def test_bse_series_fail_closed_when_upstream_exchange_capability_is_unverified() -> None:
    plan = _plan(
        capability=_capability(supported_exchanges=frozenset({Exchange.SSE, Exchange.SZSE}))
    )
    bse_series = [item for item in plan.series if item.instrument_id.endswith("BSE:920001")]

    assert len(bse_series) == len(ALL_STATEMENTS) * len(ALL_PERIODS)
    assert all(not item.candidates for item in bse_series)
    assert all(
        FinancialPlanBlockReason.EXCHANGE_CAPABILITY_UNVERIFIED in _reasons(item.blockers)
        for item in bse_series
    )


def test_unknown_listing_boundary_is_not_inferred_from_available_history() -> None:
    security = _security("600001", Exchange.SSE, listing_date=None)
    plan = _plan(_manifest((security,)))

    assert len(plan.series) == len(ALL_STATEMENTS) * len(ALL_PERIODS)
    assert all(not item.candidates for item in plan.series)
    assert all(
        FinancialPlanBlockReason.LISTING_BOUNDARY_UNKNOWN in _reasons(item.blockers)
        for item in plan.series
    )


def test_unknown_lifecycle_and_stale_active_evidence_both_block_requests() -> None:
    unknown = _security("600001", Exchange.SSE, lifecycle_status=LifecycleStatus.UNKNOWN)
    stale = _security("000001", Exchange.SZSE, lifecycle_as_of=date(2026, 1, 1))
    plan = _plan(_manifest((unknown, stale)))

    unknown_series = [item for item in plan.series if item.instrument_id.endswith("SSE:600001")]
    stale_series = [item for item in plan.series if item.instrument_id.endswith("SZSE:000001")]
    assert all(not item.candidates for item in unknown_series)
    assert all(not item.candidates for item in stale_series)
    assert all(
        FinancialPlanBlockReason.LIFECYCLE_STATUS_UNKNOWN in _reasons(item.blockers)
        for item in unknown_series
    )
    assert all(
        FinancialPlanBlockReason.LIFECYCLE_STATUS_STALE in _reasons(item.blockers)
        for item in stale_series
    )
    assert plan.lifecycle_boundaries_complete is False


def test_member_set_claim_and_lifecycle_boundary_completeness_are_separate() -> None:
    rows = (
        _security("600001", Exchange.SSE, lifecycle_status=LifecycleStatus.UNKNOWN),
        _security("000001", Exchange.SZSE),
        _security("920001", Exchange.BSE),
    )
    plan = _plan(_manifest(rows))

    assert plan.member_set_claim_consistent is True
    assert plan.member_set_independently_verified is False
    assert plan.lifecycle_boundaries_complete is False
    assert plan.all_a_share_universe_verified is False
    assert FinancialPlanBlockReason.LIFECYCLE_STATUS_UNKNOWN in _reasons(
        plan.lifecycle_boundary_blockers
    )


def test_missing_source_symbol_mapping_is_a_blocker_and_emits_no_candidates() -> None:
    security = _security("600001", Exchange.SSE, missing_source_symbol=True)
    plan = _plan(_manifest((security,)))

    assert all(not item.candidates for item in plan.series)
    assert all(
        FinancialPlanBlockReason.SOURCE_SYMBOL_MAPPING_MISSING in _reasons(item.blockers)
        for item in plan.series
    )


def test_mismatched_source_suffix_is_not_used_for_a_candidate() -> None:
    security = _security("600001", Exchange.SSE, source_symbol="600001.SZ")
    plan = _plan(_manifest((security,)))

    assert all(not item.candidates for item in plan.series)
    assert all(
        FinancialPlanBlockReason.SOURCE_SYMBOL_EXCHANGE_MISMATCH in _reasons(item.blockers)
        for item in plan.series
    )


def test_unknown_source_retention_keeps_candidates_but_blocks_full_history_claim() -> None:
    plan = _plan(
        capability=_capability(
            history_boundary_unknown=True,
            history_available_from=None,
            history_boundary_evidence_ref=None,
        )
    )

    assert plan.candidates
    assert plan.history_reconciled is False
    assert all(
        FinancialPlanBlockReason.SOURCE_HISTORY_BOUNDARY_UNKNOWN in _reasons(item.blockers)
        for item in plan.series
    )


def test_unknown_per_security_history_boundary_is_not_hidden_by_global_source_limit() -> None:
    security = _security(
        "600001", Exchange.SSE, listing_date=date(2000, 1, 1), security_history_unknown=True
    )
    plan = _plan(_manifest((security,)))

    assert plan.candidates
    assert all(series.candidates[0].window_start == date(2000, 1, 1) for series in plan.series)
    assert all(candidate.window_start >= date(2000, 1, 1) for candidate in plan.candidates)
    assert all(
        right.window_start == left.window_end + timedelta(days=1)
        for series in plan.series
        for left, right in zip(series.candidates, series.candidates[1:], strict=False)
    )
    assert all(
        FinancialPlanBlockReason.SOURCE_HISTORY_BOUNDARY_UNKNOWN in _reasons(item.blockers)
        for item in plan.series
    )


def test_later_source_retention_boundary_reports_gap_and_starts_candidates_at_retained_date() -> (
    None
):
    security = _security("600001", Exchange.SSE, listing_date=date(2000, 1, 1))
    capability = _capability(
        history_boundary_unknown=False,
        history_available_from=date(2015, 1, 1),
        history_boundary_evidence_ref="source-history:retained-from-2015",
    )
    plan = _plan(_manifest((security,)), capability)

    assert plan.candidates
    assert all(series.candidates[0].window_start == date(2015, 1, 1) for series in plan.series)
    assert all(candidate.window_start >= date(2015, 1, 1) for candidate in plan.candidates)
    assert all(
        right.window_start == left.window_end + timedelta(days=1)
        for series in plan.series
        for left, right in zip(series.candidates, series.candidates[1:], strict=False)
    )
    assert all(
        FinancialPlanBlockReason.SOURCE_HISTORY_GAP in _reasons(item.blockers)
        for item in plan.series
    )
    gap = next(
        blocker
        for blocker in plan.series[0].blockers
        if blocker.reason is FinancialPlanBlockReason.SOURCE_HISTORY_GAP
    )
    assert gap.range_start == date(2000, 1, 1)
    assert gap.range_end == date(2014, 12, 31)


@pytest.mark.parametrize(
    ("supported_statements", "supported_periods", "expected_reason", "count_without_candidates"),
    [
        (
            (StatementType.BALANCE, StatementType.CASHFLOW),
            ALL_PERIODS,
            FinancialPlanBlockReason.STATEMENT_CAPABILITY_UNVERIFIED,
            2,
        ),
        (
            ALL_STATEMENTS,
            (ReportingPeriod.QUARTERLY,),
            FinancialPlanBlockReason.PERIOD_CAPABILITY_UNVERIFIED,
            3,
        ),
    ],
)
def test_unverified_statement_or_period_capability_blocks_only_affected_series(
    supported_statements: tuple[StatementType, ...],
    supported_periods: tuple[ReportingPeriod, ...],
    expected_reason: FinancialPlanBlockReason,
    count_without_candidates: int,
) -> None:
    plan = _plan(
        _manifest((_security("600001", Exchange.SSE),)),
        _capability(
            supported_statements=supported_statements,
            supported_periods=supported_periods,
        ),
    )

    blocked = [item for item in plan.series if not item.candidates]
    assert len(blocked) == count_without_candidates
    assert all(expected_reason in _reasons(item.blockers) for item in blocked)


def test_delisted_security_is_kept_in_scope_and_uses_as_of_not_delisting_date_as_end() -> None:
    security = _security(
        "600002",
        Exchange.SSE,
        listing_date=date(2001, 1, 1),
        lifecycle_status=LifecycleStatus.DELISTED,
        delisted_on=date(2019, 5, 17),
    )
    plan = _plan(_manifest((security,)))

    assert plan.member_set_claim_consistent is False  # Fixture lacks SZSE and BSE members.
    assert plan.all_a_share_universe_verified is False
    assert all(item.candidates[-1].window_end == AS_OF for item in plan.series)


def test_no_source_window_blocker_does_not_contain_an_inverted_range() -> None:
    security = _security(
        "600001",
        Exchange.SSE,
        listing_date=date(2020, 1, 1),
        security_history_available_from=date(2027, 1, 1),
    )
    plan = _plan(_manifest((security,)))

    assert all(not item.candidates for item in plan.series)
    no_window = next(
        blocker
        for blocker in plan.series[0].blockers
        if blocker.reason is FinancialPlanBlockReason.NO_SOURCE_WINDOW
    )
    assert no_window.range_start is None
    assert no_window.range_end is None


def test_candidate_fingerprint_changes_when_member_evidence_changes() -> None:
    base = _security("600001", Exchange.SSE)
    changed = replace(base, listing_evidence_ref="listing:corrected-source-02")
    first = _plan(_manifest((base,)))
    second = _plan(_manifest((changed,)))

    assert first.manifest_sha256 != second.manifest_sha256
    assert {item.stable_id for item in first.candidates}.isdisjoint(
        {item.stable_id for item in second.candidates}
    )


def test_manifest_rejects_duplicate_identities_and_invalid_lifecycle_shapes() -> None:
    security = _security("600001", Exchange.SSE)
    with pytest.raises(FinancialManifestError, match="duplicate"):
        FinancialSecurityManifest(
            evidence=_manifest((security,)).evidence, securities=(security, security)
        )
    with pytest.raises(FinancialManifestError, match="delisted_on"):
        FinancialSecurityEvidence(
            ticker="600001",
            exchange=Exchange.SSE,
            listing_date=date(2000, 1, 1),
            listing_evidence_ref="listing:fixture",
            lifecycle_status=LifecycleStatus.DELISTED,
            lifecycle_as_of=AS_OF,
            lifecycle_evidence_ref="lifecycle:fixture",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_window_span_days": MAX_FINANCIAL_WINDOW_SPAN_DAYS + 1},
        {"max_window_span_days": True},
        {"max_window_span_days": 0},
    ],
)
def test_provider_window_limit_is_bounded_to_observed_ten_year_cap(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(FinancialManifestError, match="max_window_span_days"):
        _capability(**kwargs)


def test_plan_rejects_naive_future_or_evidence_after_observation_times() -> None:
    with pytest.raises(FinancialManifestError, match="timezone-aware"):
        _plan(planned_at=datetime(2026, 10, 26, 16, 0))
    with pytest.raises(FinancialManifestError, match="as_of cannot be after"):
        _plan(as_of=date(2026, 10, 27))
    future_capability = replace(
        _capability(), observed_at=PLANNED_AT.astimezone(timezone.utc) + timedelta(seconds=1)
    )
    with pytest.raises(FinancialManifestError, match="cannot be observed after"):
        _plan(capability=future_capability)


def test_evidence_claims_with_unbound_or_wrong_scope_membership_remain_blocked() -> None:
    rows = (
        _security("600001", Exchange.SSE),
        _security("000001", Exchange.SZSE),
        _security("920001", Exchange.BSE),
    )
    plan = _plan(
        _manifest(
            rows,
            scope_id="sample-only",
            exchanges=frozenset({Exchange.SSE, Exchange.SZSE}),
            includes_delisted=False,
            includes_listing_boundaries=False,
        )
    )

    reasons = _reasons(plan.universe_blockers + plan.lifecycle_boundary_blockers)
    assert {
        FinancialPlanBlockReason.UNIVERSE_SCOPE_ID_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_EXCHANGE_SCOPE_MISMATCH,
        FinancialPlanBlockReason.UNIVERSE_DELISTED_SECURITIES_OMITTED,
        FinancialPlanBlockReason.UNIVERSE_LISTING_BOUNDARIES_OMITTED,
    } <= reasons


def test_universe_evidence_must_not_be_future_dated() -> None:
    manifest = _manifest()
    future_evidence = replace(manifest.evidence, as_of=date(2026, 10, 27))
    future_manifest = FinancialSecurityManifest(
        evidence=future_evidence, securities=manifest.securities
    )

    with pytest.raises(FinancialManifestError, match="future-dated"):
        _plan(future_manifest)


def test_delisting_boundary_after_as_of_blocks_historical_lifecycle_claim() -> None:
    security = _security(
        "600002",
        Exchange.SSE,
        lifecycle_status=LifecycleStatus.DELISTED,
        lifecycle_as_of=date(2028, 1, 1),
        delisted_on=date(2027, 1, 1),
    )
    plan = _plan(_manifest((security,)), planned_at=datetime(2028, 1, 2, tzinfo=timezone.utc))

    assert all(not item.candidates for item in plan.series)
    assert all(
        FinancialPlanBlockReason.LIFECYCLE_BOUNDARY_AFTER_AS_OF in _reasons(item.blockers)
        for item in plan.series
    )


def test_fingerprint_is_order_independent_but_binds_listing_and_delisting_evidence() -> None:
    left = _security("600001", Exchange.SSE)
    right = _security(
        "000001",
        Exchange.SZSE,
        lifecycle_status=LifecycleStatus.DELISTED,
        delisted_on=date(2023, 6, 1),
    )

    assert fingerprint_security_manifest((left, right)) == fingerprint_security_manifest(
        (right, left)
    )
    assert fingerprint_security_manifest((left, right)) != fingerprint_security_manifest(
        (left, replace(right, delisting_evidence_ref="delisting:corrected-02"))
    )


def test_constants_describe_the_full_scope_and_upstream_limit() -> None:
    assert {exchange.value for exchange in ALL_A_SHARE_EXCHANGES} == {"SSE", "SZSE", "BSE"}
    assert ALL_A_SHARE_SCOPE_ID == "cn-a-share-equity"
    assert MAX_FINANCIAL_WINDOW_SPAN_DAYS == 3650


def test_security_evidence_rejects_malformed_identity_and_boundary_claims() -> None:
    active = _security("600001", Exchange.SSE)
    with pytest.raises(FinancialManifestError, match="six ASCII digits"):
        replace(active, ticker="ABC")
    with pytest.raises(FinancialManifestError, match="exchange or lifecycle_status"):
        replace(active, exchange="LSE")
    with pytest.raises(FinancialManifestError, match="listing_date must be a datetime.date"):
        replace(active, listing_date=datetime(2020, 1, 1))
    with pytest.raises(FinancialManifestError, match="lifecycle_as_of must be a datetime.date"):
        replace(active, lifecycle_as_of=datetime(2026, 10, 26))
    with pytest.raises(FinancialManifestError, match="lifecycle_evidence_ref"):
        replace(active, lifecycle_evidence_ref=" lifecycle:fixture")
    with pytest.raises(FinancialManifestError, match="cannot follow delisted_on"):
        replace(active, delisted_on=date(2019, 1, 1))
    with pytest.raises(FinancialManifestError, match="only valid for delisted"):
        replace(
            active,
            delisted_on=date(2021, 1, 1),
            delisting_evidence_ref="delisting:fixture",
        )
    delisted = _security("600002", Exchange.SSE, lifecycle_status=LifecycleStatus.DELISTED)
    with pytest.raises(FinancialManifestError, match="cannot precede delisted_on"):
        replace(delisted, lifecycle_as_of=date(2020, 1, 1))
    with pytest.raises(FinancialManifestError, match="qualified six-digit code"):
        replace(active, source_symbol="600001")
    with pytest.raises(FinancialManifestError, match="code must match ticker"):
        replace(active, source_symbol="000002.SH")
    with pytest.raises(FinancialManifestError, match="must be a bool"):
        replace(active, source_history_boundary_unknown=1)
    with pytest.raises(FinancialManifestError, match="unknown security history boundary"):
        replace(active, source_history_boundary_unknown=True)
    with pytest.raises(FinancialManifestError, match="known security history boundary"):
        replace(
            active,
            source_history_boundary_unknown=False,
            source_history_available_from=None,
        )


def test_enum_collections_reject_invalid_shapes_and_values() -> None:
    capability = _capability()
    bad_inputs = (
        ("SSE", "iterable"),
        (7, "iterable"),
        (("UNKNOWN",), "unsupported"),
        ((Exchange.SSE, Exchange.SSE), "duplicates"),
        ((Exchange.SSE,) * 17, "too many"),
    )
    for values, message in bad_inputs:
        with pytest.raises(FinancialManifestError, match=message):
            replace(capability, supported_exchanges=values)


def test_universe_evidence_rejects_malformed_scope_and_attestation_fields() -> None:
    evidence = _manifest().evidence
    with pytest.raises(FinancialManifestError, match="stable identifier"):
        replace(evidence, scope_id="bad scope")
    with pytest.raises(FinancialManifestError, match="evidence reference"):
        replace(evidence, evidence_ref=" review:bad")
    with pytest.raises(FinancialManifestError, match="as_of must be a datetime.date"):
        replace(evidence, as_of=datetime(2026, 10, 26))
    with pytest.raises(FinancialManifestError, match="evidence_kind is unsupported"):
        replace(evidence, evidence_kind="unverified-kind")
    with pytest.raises(FinancialManifestError, match="positive integer"):
        replace(evidence, expected_member_count=True)
    with pytest.raises(FinancialManifestError, match="row limit"):
        replace(evidence, expected_member_count=MAX_SECURITY_ROWS + 1)
    with pytest.raises(FinancialManifestError, match="SHA-256 digest"):
        replace(evidence, members_sha256="not-a-digest")
    with pytest.raises(FinancialManifestError, match="inclusion claims must be booleans"):
        replace(evidence, includes_delisted=1)


def test_source_capability_rejects_invalid_observation_and_retention_evidence() -> None:
    capability = _capability()
    with pytest.raises(FinancialManifestError, match="timezone-aware"):
        replace(capability, observed_at=datetime(2026, 10, 26, 15, 0))
    with pytest.raises(FinancialManifestError, match="history_boundary_unknown must be a bool"):
        replace(capability, history_boundary_unknown=1)
    with pytest.raises(FinancialManifestError, match="unknown history boundary"):
        replace(capability, history_boundary_unknown=True)
    with pytest.raises(FinancialManifestError, match="known history boundary requires a date"):
        replace(
            capability,
            history_boundary_unknown=False,
            history_available_from=None,
        )
    with pytest.raises(FinancialManifestError, match="history_boundary_evidence_ref"):
        replace(capability, history_boundary_evidence_ref=" ")


def test_manifest_and_fingerprint_reject_malformed_row_collections() -> None:
    manifest = _manifest()
    with pytest.raises(FinancialManifestError, match="evidence must be"):
        FinancialSecurityManifest(evidence=None, securities=manifest.securities)
    with pytest.raises(FinancialManifestError, match="iterable of explicit rows"):
        FinancialSecurityManifest(evidence=manifest.evidence, securities="600001")
    with pytest.raises(FinancialManifestError, match="iterable of explicit rows"):
        FinancialSecurityManifest(evidence=manifest.evidence, securities=7)
    with pytest.raises(FinancialManifestError, match="must contain 1"):
        FinancialSecurityManifest(evidence=manifest.evidence, securities=())
    with pytest.raises(FinancialManifestError, match="must contain FinancialSecurityEvidence"):
        FinancialSecurityManifest(evidence=manifest.evidence, securities=(None,))

    with pytest.raises(FinancialManifestError, match="iterable of explicit rows"):
        fingerprint_security_manifest("600001")
    with pytest.raises(FinancialManifestError, match="iterable of explicit rows"):
        fingerprint_security_manifest(7)
    with pytest.raises(FinancialManifestError, match="must contain 1"):
        fingerprint_security_manifest(())
    with pytest.raises(FinancialManifestError, match="must contain FinancialSecurityEvidence"):
        fingerprint_security_manifest((None,))
    with pytest.raises(FinancialManifestError, match="duplicate identities"):
        fingerprint_security_manifest((manifest.securities[0], manifest.securities[0]))


def test_unknown_global_and_security_retention_still_anchors_candidates_at_listing() -> None:
    security = _security(
        "600001", Exchange.SSE, listing_date=date(2000, 1, 1), security_history_unknown=True
    )
    capability = _capability(
        history_boundary_unknown=True,
        history_available_from=None,
        history_boundary_evidence_ref=None,
    )
    plan = _plan(_manifest((security,)), capability)

    assert plan.candidates
    assert all(series.candidates[0].window_start == date(2000, 1, 1) for series in plan.series)
    assert all(
        FinancialPlanBlockReason.SOURCE_HISTORY_BOUNDARY_UNKNOWN in _reasons(series.blockers)
        for series in plan.series
    )


def test_listing_after_as_of_is_a_boundary_blocker_without_a_reversed_range() -> None:
    security = _security("600001", Exchange.SSE, listing_date=AS_OF + timedelta(days=1))
    plan = _plan(_manifest((security,)))

    assert all(not series.candidates for series in plan.series)
    listing_blocker = next(
        blocker
        for blocker in plan.lifecycle_boundary_blockers
        if blocker.reason is FinancialPlanBlockReason.LISTING_BOUNDARY_AFTER_AS_OF
    )
    assert listing_blocker.range_start == AS_OF + timedelta(days=1)
    assert listing_blocker.range_end == AS_OF + timedelta(days=1)
    no_window_blocker = next(
        blocker
        for blocker in plan.series[0].blockers
        if blocker.reason is FinancialPlanBlockReason.NO_SOURCE_WINDOW
    )
    assert no_window_blocker.range_start is None
    assert no_window_blocker.range_end is None


def test_universe_evidence_older_than_plan_cutoff_fails_closed() -> None:
    manifest = _manifest()
    stale_evidence = replace(manifest.evidence, as_of=AS_OF - timedelta(days=1))
    stale_manifest = FinancialSecurityManifest(
        evidence=stale_evidence, securities=manifest.securities
    )

    with pytest.raises(FinancialManifestError, match="predates the requested plan cutoff"):
        _plan(stale_manifest)
