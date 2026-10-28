"""Deterministic in-memory tests for evidence-bound daily-gap reconciliation."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime

import pytest

import opendata.pipeline.stock_daily_reconcile as reconcile_module
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.pipeline.history_coverage import HistoryCoverageRequest
from opendata.pipeline.run_identity import TaskDefinition
from opendata.pipeline.stock_daily_backfill_plan import (
    BackfillBlocker,
    BackfillBlockReason,
    StockDailyBackfillPlan,
    build_stock_daily_backfill_plan,
)
from opendata.pipeline.stock_daily_reconcile import (
    DailyPublicationSnapshot,
    EndBoundaryEvidence,
    EndBoundaryKind,
    InstrumentExpectedObservationEvidence,
    ReconciliationIssueCode,
    SourceRetrievalEvidence,
    StockDailySeriesEvidence,
    reconcile_stock_daily_gaps,
)

DATASET_ID = "cn-equity-stock-daily"
SOURCE_ID = "akshare.daily"
INSTRUMENT_ID = "CN:600519"
START = date(2024, 2, 5)
END = date(2024, 2, 9)
WEEKDAYS = tuple(date.fromisoformat(f"2024-02-{day:02d}") for day in range(5, 10))


def _contract() -> DatasetContract:
    """Build the disabled stock_daily adapter's exact raw-bar contract."""
    return DatasetContract(
        dataset_id=DATASET_ID,
        schema_version="1.0.0",
        semantic_version="stock-daily-raw-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=("market", "symbol"),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id=SOURCE_ID, source=SOURCE),
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
            "amount": "CNY",
        },
        field_currencies={
            "open": "CNY",
            "high": "CNY",
            "low": "CNY",
            "close": "CNY",
            "amount": "CNY",
        },
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
            "amount": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _plan(
    *,
    start: date = START,
    end: date = END,
    instrument_id: str = INSTRUMENT_ID,
    max_window_days: int = 3660,
    alias_overrides: tuple[InstrumentAlias, ...] | None = None,
) -> StockDailyBackfillPlan:
    """Build an offline alias-pinned plan from synthetic lifecycle evidence."""
    lifecycle = InstrumentLifecycle(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        instrument_id=instrument_id,
        lifecycle_version="life-v1",
        status=LifecycleStatus.ACTIVE,
        status_as_of=end,
        status_evidence_ref="review:active-status-v1",
        known_start_date=start,
        start_boundary_unknown=False,
        start_evidence_ref="exchange:listing-v1",
        delisted_on=None,
        delist_evidence_ref=None,
    )
    alias = InstrumentAlias(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        alias="600519.SH",
        instrument_id=instrument_id,
        alias_version="ticker-v1",
        evidence_ref="review:ticker-v1",
        valid_from=start,
        valid_through=end,
    )
    registry = InstrumentReferenceRegistry(
        registry_version="registry-v1",
        evidence_ref="review:registry-v1",
        lifecycles=(lifecycle,),
        aliases=(alias,) if alias_overrides is None else alias_overrides,
    )
    task = TaskDefinition(
        job_id="cn-stock-daily-reconcile-fixture",
        version="v1",
        dataset_id=DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        schedule=None,
        enabled=False,
    )
    return build_stock_daily_backfill_plan(
        registry,
        task=task,
        contract=_contract(),
        as_of=end,
        max_window_days=max_window_days,
    )


def _series(
    *,
    instrument_id: str = INSTRUMENT_ID,
    expected: tuple[date, ...] = WEEKDAYS,
    observed: tuple[date, ...] = WEEKDAYS,
    unresolved: tuple[date, ...] = (),
    with_publication: bool = True,
    with_expectation: bool = True,
    with_retention: bool = True,
    known_start: date = START,
    end_kind: EndBoundaryKind = EndBoundaryKind.ACTIVE_AS_OF,
    provider_symbols: tuple[str, ...] = ("600519",),
    publication_source: str = SOURCE_ID,
    expectation_source: str = SOURCE_ID,
) -> StockDailySeriesEvidence:
    """Build one pinned, source-specific series using only literal fixture data."""
    request = HistoryCoverageRequest(
        dataset=DATASET_ID,
        source=SOURCE_ID,
        instrument_key=instrument_id,
        request_start=START,
        request_end=END,
        expected_business_dates=expected,
        observed_business_dates=observed if with_publication else (),
        calendar_id="SSE",
        calendar_version="SSE-fixture-v1",
        business_calendar_evidence="calendar:fixture-v1",
        calendar_covered_from=START,
        calendar_covered_through=END,
        known_start_date=known_start,
        start_boundary_unknown=False,
        independent_start_evidence="exchange:listing-v1",
        source_retained_from=START if with_retention else None,
        source_retained_through=END if with_retention else None,
        source_retention_evidence="provider:retention-v1" if with_retention else None,
    )
    publication = (
        DailyPublicationSnapshot(
            dataset_id=DATASET_ID,
            source_id=publication_source,
            instrument_id=instrument_id,
            publication_id="pub:fixture-001",
            content_sha256="a" * 64,
            observed_business_dates=observed,
        )
        if with_publication
        else None
    )
    expectation = (
        InstrumentExpectedObservationEvidence(
            dataset_id=DATASET_ID,
            source_id=expectation_source,
            instrument_id=instrument_id,
            calendar_id="SSE",
            calendar_version="SSE-fixture-v1",
            calendar_evidence_ref="calendar:fixture-v1",
            instrument_evidence_ref="security-eligibility:fixture-v1",
            covered_from=START,
            covered_through=END,
            expected_business_dates=expected,
            unresolved_observation_dates=unresolved,
        )
        if with_expectation
        else None
    )
    if end_kind is EndBoundaryKind.UNKNOWN:
        terminal = EndBoundaryEvidence(DATASET_ID, instrument_id, end_kind)
    else:
        terminal = EndBoundaryEvidence(
            DATASET_ID,
            instrument_id,
            end_kind,
            boundary_date=END,
            evidence_ref="review:active-status-v1",
        )
    capability = SourceRetrievalEvidence(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        instrument_id=instrument_id,
        evidence_ref="provider:symbol-window-v1",
        queryable_from=START,
        queryable_through=END,
        provider_symbols=provider_symbols,
        max_window_days=30,
    )
    return StockDailySeriesEvidence(
        coverage_request=request,
        publication=publication,
        expected_observations=expectation,
        end_boundary=terminal,
        source_retrieval=capability,
    )


def _provider_symbol(plan: StockDailyBackfillPlan) -> str:
    """Return the provider symbol emitted by the synthetic plan."""
    return plan.candidate_chunks[0].provider_symbol


def test_head_interior_and_tail_gaps_are_bound_to_publication_and_symbol_route() -> None:
    """Report three proven gaps and only alias-pinned date windows."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[1], WEEKDAYS[3]),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.status.value == "PARTIAL"
    assert item.coverage.head_gaps == (WEEKDAYS[0],)
    assert item.coverage.interior_gaps == (WEEKDAYS[2],)
    assert item.coverage.tail_gaps == (WEEKDAYS[4],)
    assert item.confirmed_expected_gap_dates == (WEEKDAYS[0], WEEKDAYS[2], WEEKDAYS[4])
    assert [candidate.expected_missing_dates for candidate in item.candidates] == [
        (WEEKDAYS[0],),
        (WEEKDAYS[2],),
        (WEEKDAYS[4],),
    ]
    assert all(candidate.provider_symbol == _provider_symbol(plan) for candidate in item.candidates)
    assert all(candidate.to_dict()["dispatch_authorized"] is False for candidate in item.candidates)
    assert item.unplanned_gap_dates == ()
    assert report.all_a_universe_complete is False


def test_adjacent_missing_expected_dates_are_grouped_without_fabricating_weekend_dates() -> None:
    """A review window lists expected dates exactly, not fabricated daily bars."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[0], WEEKDAYS[3], WEEKDAYS[4]),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    candidate = report.series[0].candidates[0]
    assert candidate.window_start == WEEKDAYS[1]
    assert candidate.window_end == WEEKDAYS[2]
    assert candidate.expected_missing_dates == (WEEKDAYS[1], WEEKDAYS[2])
    assert candidate.window_days == 2
    assert report.to_dict()["bars_created"] is False


def test_same_alias_metadata_does_not_merge_adjacent_dates_from_distinct_chunks() -> None:
    """Adjacent missing dates split at exact chunk identity, even with the same alias."""
    plan = _plan(max_window_days=1)
    evidence = _series(
        observed=(WEEKDAYS[0], WEEKDAYS[3], WEEKDAYS[4]),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    candidates = report.series[0].candidates
    assert [candidate.expected_missing_dates for candidate in candidates] == [
        (WEEKDAYS[1],),
        (WEEKDAYS[2],),
    ]
    assert candidates[0].provider_symbol == candidates[1].provider_symbol
    assert candidates[0].alias_version == candidates[1].alias_version
    assert candidates[0].alias_evidence_ref == candidates[1].alias_evidence_ref
    chunk_indexes = {
        next(
            chunk.chunk_index
            for chunk in plan.candidate_chunks
            if chunk.window_start <= day <= chunk.window_end
        )
        for candidate in candidates
        for day in candidate.expected_missing_dates
    }
    assert len(chunk_indexes) == 2
    assert all(candidate.window_start == candidate.window_end for candidate in candidates)


def test_exchange_calendar_without_security_expected_observation_evidence_stays_unknown() -> None:
    """A plain exchange-session list cannot classify a security-level row gap."""
    evidence = _series(
        observed=(WEEKDAYS[1],),
        with_expectation=False,
    )

    report = reconcile_stock_daily_gaps((evidence,))

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.diagnostic_gap_dates
    assert item.confirmed_expected_gap_dates == ()
    assert item.candidates == ()
    assert item.unplanned_gap_dates == ()
    assert any(
        issue.code is ReconciliationIssueCode.INSTRUMENT_EXPECTATION_EVIDENCE_MISSING
        for issue in item.issues
    )


def test_unresolved_suspension_date_blocks_completeness_without_becoming_a_gap_candidate() -> None:
    """An unresolved suspension date is neither an expected gap nor a candidate."""
    expected = (WEEKDAYS[0], WEEKDAYS[1], WEEKDAYS[3], WEEKDAYS[4])
    evidence = _series(
        expected=expected,
        observed=expected,
        unresolved=(WEEKDAYS[2],),
    )

    report = reconcile_stock_daily_gaps((evidence,))

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.confirmed_expected_gap_dates == ()
    assert item.unresolved_observation_dates == (WEEKDAYS[2],)
    assert item.candidates == ()
    assert any(
        issue.code is ReconciliationIssueCode.UNCERTAIN_INSTRUMENT_OBSERVATION_STATUS
        for issue in item.issues
    )


def test_unknown_terminal_boundary_prevents_candidate_and_complete_claim() -> None:
    """Missing bars stay unplanned until active or delisted end evidence exists."""
    evidence = _series(observed=(WEEKDAYS[0],), end_kind=EndBoundaryKind.UNKNOWN)

    report = reconcile_stock_daily_gaps(
        (evidence,), backfill_plans={(DATASET_ID, SOURCE_ID): _plan()}
    )

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(issue.code is ReconciliationIssueCode.END_BOUNDARY_UNKNOWN for issue in item.issues)


def test_unknown_source_retention_prevents_completeness_and_suppresses_windows() -> None:
    """Unknown source retention blocks both completeness and gap recovery candidates."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[0],),
        with_retention=False,
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.SOURCE_RETENTION_UNKNOWN for issue in item.issues
    )


def test_missing_publication_does_not_treat_an_empty_observed_set_as_proven_absence() -> None:
    """No publication identity leaves all date conclusions unknown."""
    evidence = _series(with_publication=False)

    report = reconcile_stock_daily_gaps((evidence,))

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.diagnostic_gap_dates == WEEKDAYS
    assert item.confirmed_expected_gap_dates == ()
    assert item.candidates == ()
    assert item.unplanned_gap_dates == ()
    assert any(issue.code is ReconciliationIssueCode.PUBLICATION_MISSING for issue in item.issues)


def test_expected_observation_before_known_start_boundary_is_rejected() -> None:
    """Conflicting start evidence cannot be filtered into a false COMPLETE result."""
    with pytest.raises(ValueError, match="precede the independently evidenced known_start_date"):
        _series(known_start=WEEKDAYS[2])


def test_alias_chunk_query_interval_must_fit_inside_alias_validity() -> None:
    """A malformed alias bound blocks every affected date from candidate planning."""
    plan = _plan()
    original_chunk = plan.candidate_chunks[0]
    invalid_chunk = replace(original_chunk, alias_valid_from=WEEKDAYS[2])
    invalid_instrument = replace(plan.instruments[0], candidate_chunks=(invalid_chunk,))
    invalid_plan = replace(plan, instruments=(invalid_instrument,))
    evidence = _series(
        observed=(WEEKDAYS[0],),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): invalid_plan},
    )

    item = report.series[0]
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH for issue in item.issues
    )


def test_unknown_source_capability_keeps_confirmed_gaps_unplanned() -> None:
    """A plan alone cannot make a gap queryable when source capability is absent."""
    plan = _plan()
    evidence = replace(
        _series(observed=(WEEKDAYS[0],), provider_symbols=(_provider_symbol(plan),)),
        source_retrieval=None,
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.confirmed_expected_gap_dates == WEEKDAYS[1:]
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.SOURCE_CAPABILITY_UNKNOWN for issue in item.issues
    )


def test_source_retention_start_blocks_only_earlier_gap_dates() -> None:
    """Known provider retention excludes pre-retention gaps without hiding them."""
    plan = _plan()
    base = _series(observed=(WEEKDAYS[0],), provider_symbols=(_provider_symbol(plan),))
    request = replace(base.coverage_request, source_retained_from=WEEKDAYS[2])
    evidence = replace(base, coverage_request=request)

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert (
        tuple(day for candidate in item.candidates for day in candidate.expected_missing_dates)
        == WEEKDAYS[2:]
    )
    assert item.unplanned_gap_dates == (WEEKDAYS[1],)
    retention_issue = next(
        issue
        for issue in item.issues
        if issue.code is ReconciliationIssueCode.SOURCE_RETENTION_EXCLUDES_DATE
    )
    assert retention_issue.affected_dates == (WEEKDAYS[1],)


@pytest.mark.parametrize("missing_day", (WEEKDAYS[0], WEEKDAYS[1], WEEKDAYS[4]))
def test_missing_plan_chunk_is_reported_as_an_alias_gap_for_that_date(
    missing_day: date,
) -> None:
    """A date outside every indexed chunk is not assigned to a neighboring alias."""
    plan = _plan(max_window_days=1)
    instrument = replace(
        plan.instruments[0],
        candidate_chunks=tuple(
            chunk for chunk in plan.candidate_chunks if chunk.window_start != missing_day
        ),
    )
    plan_with_missing_chunk = replace(plan, instruments=(instrument,))
    evidence = _series(
        observed=tuple(day for day in WEEKDAYS if day != missing_day),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan_with_missing_chunk},
    )

    item = report.series[0]
    assert item.candidates == ()
    assert item.unplanned_gap_dates == (missing_day,)
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_ALIAS_MISSING
        and issue.affected_dates == (missing_day,)
        for issue in item.issues
    )


def test_plan_lifecycle_conflict_keeps_gap_unplanned_and_status_unknown() -> None:
    """A candidate plan must agree with the independently pinned start evidence."""
    plan = _plan()
    instrument = replace(plan.instruments[0], start_evidence_ref="review:other-start")
    invalid_plan = replace(plan, instruments=(instrument,))
    evidence = _series(observed=(WEEKDAYS[0],))

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): invalid_plan},
    )

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH for issue in item.issues
    )


def test_instrument_blocker_applies_only_to_its_inclusive_date_range() -> None:
    """An alias blocker on later dates cannot suppress an earlier safe candidate."""
    plan = _plan()
    scoped_blockers = (
        BackfillBlocker(
            reason=BackfillBlockReason.UNKNOWN_START_BOUNDARY,
            detail="fixture blocker starts after this date",
            instrument_id=INSTRUMENT_ID,
            range_start=WEEKDAYS[2],
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.SOURCE_ALIAS_GAP,
            detail="fixture blocker ended before this date",
            instrument_id=INSTRUMENT_ID,
            range_end=WEEKDAYS[0],
        ),
        BackfillBlocker(
            reason=BackfillBlockReason.DISPATCH_NOT_AUTHORIZED,
            detail="review candidates remain non-dispatching",
            instrument_id=INSTRUMENT_ID,
        ),
    )
    instrument = replace(plan.instruments[0], blockers=scoped_blockers)
    scoped_plan = replace(plan, instruments=(instrument,))
    evidence = _series(observed=tuple(day for day in WEEKDAYS if day != WEEKDAYS[1]))

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): scoped_plan},
    )

    item = report.series[0]
    assert [candidate.expected_missing_dates for candidate in item.candidates] == [(WEEKDAYS[1],)]
    assert not any(
        issue.code is ReconciliationIssueCode.BACKFILL_ROUTE_BLOCKED for issue in item.issues
    )


def test_in_scope_alias_blocker_maps_to_an_explicit_missing_alias_issue() -> None:
    """An in-range planner blocker is surfaced against the exact missing date."""
    plan = _plan()
    instrument = replace(
        plan.instruments[0],
        blockers=(
            BackfillBlocker(
                reason=BackfillBlockReason.SOURCE_ALIAS_GAP,
                detail="fixture source alias is unavailable for the missing session",
                instrument_id=INSTRUMENT_ID,
                range_start=WEEKDAYS[1],
                range_end=WEEKDAYS[1],
            ),
        ),
    )
    blocked_plan = replace(plan, instruments=(instrument,))
    evidence = _series(observed=tuple(day for day in WEEKDAYS if day != WEEKDAYS[1]))

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): blocked_plan},
    )

    item = report.series[0]
    assert item.candidates == ()
    assert item.unplanned_gap_dates == (WEEKDAYS[1],)
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_ALIAS_MISSING
        and issue.affected_dates == (WEEKDAYS[1],)
        for issue in item.issues
    )


def test_beijing_provider_mapping_blocker_is_not_planned_as_a_candidate() -> None:
    """A syntactically accepted BSE alias stays blocked without a verified route."""
    instrument_id = "CN:430001"
    alias = InstrumentAlias(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        alias="430001.BJ",
        instrument_id=instrument_id,
        alias_version="ticker-bj-v1",
        evidence_ref="review:ticker-bj-v1",
        valid_from=START,
        valid_through=END,
    )
    plan = _plan(instrument_id=instrument_id, alias_overrides=(alias,))
    evidence = _series(
        instrument_id=instrument_id,
        observed=(WEEKDAYS[0],),
        provider_symbols=("430001",),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED for issue in item.issues
    )


def test_independent_delisted_boundary_resolves_only_the_matching_plan_blocker() -> None:
    """A distinct end-boundary proof resolves the planner's delisting uncertainty."""
    plan = _plan()
    instrument = replace(
        plan.instruments[0],
        lifecycle_status=LifecycleStatus.DELISTED,
        delisted_on=END,
        delist_evidence_ref="plan:delisted-v1",
        blockers=(
            BackfillBlocker(
                reason=BackfillBlockReason.DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED,
                detail="planner needs independent last-expected-observation evidence",
                instrument_id=INSTRUMENT_ID,
                range_start=END,
                range_end=END,
            ),
        ),
    )
    delisted_plan = replace(plan, instruments=(instrument,))
    evidence = _series(
        observed=(WEEKDAYS[0],),
        end_kind=EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION,
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): delisted_plan},
    )

    item = report.series[0]
    assert item.status.value == "PARTIAL"
    assert [candidate.expected_missing_dates for candidate in item.candidates] == [WEEKDAYS[1:]]


def test_alias_transition_gaps_match_the_chunk_for_each_effective_alias() -> None:
    """Missing dates on both sides of an alias change retain their own pin."""
    aliases = (
        InstrumentAlias(
            dataset_id=DATASET_ID,
            source_id=SOURCE_ID,
            alias="600519.SH",
            instrument_id=INSTRUMENT_ID,
            alias_version="ticker-v1",
            evidence_ref="review:ticker-v1",
            valid_from=START,
            valid_through=WEEKDAYS[1],
        ),
        InstrumentAlias(
            dataset_id=DATASET_ID,
            source_id=SOURCE_ID,
            alias="SH600519",
            instrument_id=INSTRUMENT_ID,
            alias_version="ticker-v2",
            evidence_ref="review:ticker-v2",
            valid_from=WEEKDAYS[2],
            valid_through=END,
        ),
    )
    plan = _plan(alias_overrides=aliases)
    evidence = _series(
        observed=(WEEKDAYS[0], WEEKDAYS[2], WEEKDAYS[4]),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    candidates = report.series[0].candidates
    assert [candidate.expected_missing_dates for candidate in candidates] == [
        (WEEKDAYS[1],),
        (WEEKDAYS[3],),
    ]
    assert [candidate.alias_version for candidate in candidates] == [
        "ticker-v1",
        "ticker-v2",
    ]
    assert [candidate.alias_evidence_ref for candidate in candidates] == [
        "review:ticker-v1",
        "review:ticker-v2",
    ]
    assert [candidate.provider_symbol for candidate in candidates] == ["600519", "600519"]
    for candidate in candidates:
        matching_chunks = [
            chunk
            for chunk in plan.candidate_chunks
            if chunk.window_start <= candidate.window_start <= chunk.window_end
        ]
        assert len(matching_chunks) == 1
        chunk = matching_chunks[0]
        assert chunk.alias_valid_from is not None
        assert chunk.alias_valid_through is not None
        assert candidate.window_start >= chunk.alias_valid_from
        assert candidate.window_end <= chunk.alias_valid_through
        assert candidate.alias_version == chunk.alias_version
        assert candidate.alias_evidence_ref == chunk.alias_evidence_ref
        assert candidate.provider_symbol == chunk.provider_symbol


def test_candidate_window_count_is_bounded_and_unplanned_dates_are_preserved() -> None:
    """A low cap leaves later gaps visible with an explicit blocker."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[1], WEEKDAYS[3]),
        provider_symbols=(_provider_symbol(plan),),
    )

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
        max_candidate_windows=2,
    )

    item = report.series[0]
    assert len(item.candidates) == 2
    assert item.unplanned_gap_dates == (WEEKDAYS[4],)
    assert report.candidate_limit_reached is True
    assert any(
        issue.code is ReconciliationIssueCode.CANDIDATE_WINDOW_LIMIT for issue in item.issues
    )


def test_unverified_provider_symbol_blocks_candidates_even_with_calendar_and_publication() -> None:
    """A symbol must match the source capability pin and the alias-pinned plan."""
    plan = _plan()
    evidence = _series(observed=(WEEKDAYS[0],), provider_symbols=("000001",))

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.confirmed_expected_gap_dates == WEEKDAYS[1:]
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED for issue in item.issues
    )


def test_exact_dataset_source_plan_key_is_required() -> None:
    """A plan from another logical source is not reused for this publication."""
    evidence = _series(observed=(WEEKDAYS[0],))

    report = reconcile_stock_daily_gaps((evidence,), backfill_plans={})

    item = report.series[0]
    assert item.candidates == ()
    assert any(issue.code is ReconciliationIssueCode.BACKFILL_PLAN_MISSING for issue in item.issues)


def test_publication_source_identity_must_match_coverage_source() -> None:
    """Published rows cannot be silently attributed to a different provider."""
    with pytest.raises(ValueError, match="publication identity"):
        _series(publication_source="fuyao.financial")


def test_expected_observation_calendar_source_identity_must_match() -> None:
    """Security eligibility evidence is bound to the same logical source."""
    with pytest.raises(ValueError, match="expected-observation evidence identity"):
        _series(expectation_source="eastmoney.daily")


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("coverage_request", "coverage_request must be a HistoryCoverageRequest"),
        ("publication", "publication must be a DailyPublicationSnapshot or None"),
        ("expected_observations", "expected_observations has an unsupported type"),
        ("end_boundary", "end_boundary must be an EndBoundaryEvidence"),
        ("source_retrieval", "source_retrieval has an unsupported type"),
    ),
)
def test_series_evidence_rejects_unsupported_member_types(field: str, message: str) -> None:
    """All evidence members are type-checked before any reconciliation is attempted."""
    with pytest.raises(TypeError, match=message):
        replace(_series(), **{field: object()})


def test_series_evidence_requires_one_instrument_and_matching_end_identity() -> None:
    """A business-key scope or foreign terminal boundary cannot stand in for a security."""
    evidence = _series()
    business_key_request = replace(
        evidence.coverage_request,
        instrument_key=None,
        business_key="market,symbol",
    )
    with pytest.raises(ValueError, match="requires exactly one instrument_key"):
        replace(evidence, coverage_request=business_key_request)

    foreign_boundary = replace(evidence.end_boundary, instrument_id="CN:000001")
    with pytest.raises(ValueError, match="end-boundary evidence identity"):
        replace(evidence, end_boundary=foreign_boundary)


def test_unpublished_observed_rows_are_rejected() -> None:
    """Observed dates cannot be asserted when the publication pin is absent."""
    with pytest.raises(ValueError, match="observed dates require a pinned publication"):
        replace(_series(), publication=None)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("expected_dates", "coverage expected dates do not match"),
        ("calendar_pin", "different calendar pin"),
        ("coverage_scope", "does not cover the scope"),
        ("unresolved_scope", "unresolved observation dates must fall inside the request scope"),
        ("source_identity", "source-retrieval evidence identity"),
    ),
)
def test_series_evidence_rejects_mismatched_expectation_or_capability_pins(
    mutation: str,
    message: str,
) -> None:
    """Calendar, coverage, and source capability pins must bind to this exact request."""
    evidence = _series()
    expectation = evidence.expected_observations
    capability = evidence.source_retrieval
    assert expectation is not None
    assert capability is not None
    changed: dict[str, object]

    if mutation == "expected_dates":
        changed_expectation = replace(expectation, expected_business_dates=(WEEKDAYS[0],))
        changed = {"expected_observations": changed_expectation}
    elif mutation == "calendar_pin":
        changed_expectation = replace(expectation, calendar_version="SSE-fixture-v2")
        changed = {"expected_observations": changed_expectation}
    elif mutation == "coverage_scope":
        scoped_dates = WEEKDAYS[1:]
        changed_request = replace(
            evidence.coverage_request,
            expected_business_dates=scoped_dates,
        )
        changed_expectation = replace(
            expectation,
            covered_from=WEEKDAYS[1],
            expected_business_dates=scoped_dates,
        )
        changed = {
            "coverage_request": changed_request,
            "expected_observations": changed_expectation,
        }
    elif mutation == "unresolved_scope":
        outside_date = date(2024, 2, 4)
        changed_expectation = replace(
            expectation,
            covered_from=outside_date,
            unresolved_observation_dates=(outside_date,),
        )
        changed = {"expected_observations": changed_expectation}
    else:
        changed_capability = replace(capability, instrument_id="CN:000001")
        changed = {"source_retrieval": changed_capability}

    with pytest.raises(ValueError, match=message):
        replace(evidence, **changed)


def test_delisted_terminal_boundary_requires_the_last_expected_date() -> None:
    """An evidenced delisting endpoint is covered only at the last expected date."""
    evidence = _series(
        observed=WEEKDAYS[:-1],
        end_kind=EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION,
    )

    report = reconcile_stock_daily_gaps((evidence,))

    assert report.series[0].status.value == "PARTIAL"
    assert not any(
        issue.code is ReconciliationIssueCode.END_BOUNDARY_UNKNOWN
        for issue in report.series[0].issues
    )


def test_calendar_coverage_shortfall_keeps_completeness_unknown() -> None:
    """A weekday list without full requested calendar coverage cannot prove completion."""
    evidence = _series()
    request = replace(evidence.coverage_request, calendar_covered_through=WEEKDAYS[-2])

    report = reconcile_stock_daily_gaps((replace(evidence, coverage_request=request),))

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert any(
        issue.code is ReconciliationIssueCode.CALENDAR_COVERAGE_INCOMPLETE for issue in item.issues
    )


def test_unknown_listing_boundary_blocks_gap_candidates() -> None:
    """Candidate planning remains blocked when the security start is unproved."""
    evidence = _series(observed=(WEEKDAYS[0],))
    request = replace(
        evidence.coverage_request,
        known_start_date=None,
        start_boundary_unknown=True,
        independent_start_evidence=None,
    )
    plan = _plan()

    report = reconcile_stock_daily_gaps(
        (replace(evidence, coverage_request=request),),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    item = report.series[0]
    assert item.status.value == "UNKNOWN"
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.START_BOUNDARY_UNKNOWN for issue in item.issues
    )


def test_publication_observation_dates_must_match_the_pinned_snapshot() -> None:
    """The evaluator cannot be given rows that differ from the publication pin."""
    plan = _plan()
    evidence = _series(observed=(WEEKDAYS[0],), provider_symbols=(_provider_symbol(plan),))
    mismatched = DailyPublicationSnapshot(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        instrument_id=INSTRUMENT_ID,
        publication_id="pub:other",
        content_sha256="b" * 64,
        observed_business_dates=(WEEKDAYS[0], WEEKDAYS[1]),
    )

    with pytest.raises(ValueError, match="do not match the pinned publication"):
        StockDailySeriesEvidence(
            coverage_request=evidence.coverage_request,
            publication=mismatched,
            expected_observations=evidence.expected_observations,
            end_boundary=evidence.end_boundary,
            source_retrieval=evidence.source_retrieval,
        )


def test_publication_digest_requires_sha256_pin_shape() -> None:
    """A free-form label cannot masquerade as an immutable publication pin."""
    with pytest.raises(ValueError, match="lowercase 64-character SHA-256"):
        DailyPublicationSnapshot(
            DATASET_ID,
            SOURCE_ID,
            INSTRUMENT_ID,
            "pub:bad-sha",
            "not-a-digest",
            (),
        )


@pytest.mark.parametrize("invalid_label", ("", "   ", None))
def test_publication_identity_rejects_blank_or_non_string_labels(invalid_label: object) -> None:
    """Publication evidence requires a concrete dataset identity."""
    publication = _series().publication
    assert publication is not None

    with pytest.raises(ValueError, match="dataset_id must be a nonempty string"):
        replace(publication, dataset_id=invalid_label)


@pytest.mark.parametrize("invalid_dates", ("2024-02-05", None, {"date": WEEKDAYS[0]}))
def test_publication_dates_reject_non_date_iterable_shapes(invalid_dates: object) -> None:
    """Strings, mappings, and non-iterables cannot masquerade as date sequences."""
    publication = _series().publication
    assert publication is not None

    with pytest.raises(ValueError, match="observed_business_dates must be an iterable"):
        replace(publication, observed_business_dates=invalid_dates)


def test_publication_dates_enforce_the_per_sequence_materialization_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A date sequence over the configured cap is refused during validation."""
    publication = _series().publication
    assert publication is not None
    monkeypatch.setattr(reconcile_module, "MAX_EVIDENCE_DATES", 1)

    with pytest.raises(ValueError, match="observed_business_dates exceeds the 1-date limit"):
        replace(publication, observed_business_dates=WEEKDAYS[:2])


@pytest.mark.parametrize(
    ("field", "invalid_date"),
    (("covered_from", datetime(2024, 2, 5)), ("covered_through", "2024-02-09")),
)
def test_expected_observation_bounds_reject_timestamps_and_date_strings(
    field: str,
    invalid_date: object,
) -> None:
    """Lifecycle evidence accepts dates, never timestamp or string substitutes."""
    expectation = _series().expected_observations
    assert expectation is not None

    with pytest.raises(ValueError, match=f"{field} must be a datetime.date"):
        replace(expectation, **{field: invalid_date})


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"covered_from": END, "covered_through": START}, "covered_from must be on or before"),
        (
            {"expected_business_dates": (WEEKDAYS[0], WEEKDAYS[0])},
            "expected_business_dates must not contain duplicates",
        ),
        (
            {"unresolved_observation_dates": (WEEKDAYS[0], WEEKDAYS[0])},
            "unresolved_observation_dates must not contain duplicates",
        ),
        (
            {"unresolved_observation_dates": (WEEKDAYS[0],)},
            "unresolved dates must not be classified as expected observations",
        ),
        (
            {"covered_from": WEEKDAYS[1]},
            "expected_business_dates must fall inside the evidenced calendar coverage",
        ),
        (
            {"unresolved_observation_dates": (date(2024, 2, 4),)},
            "unresolved_observation_dates must fall inside the evidenced calendar coverage",
        ),
    ),
)
def test_expected_observation_evidence_rejects_conflicting_calendar_dates(
    changes: dict[str, object],
    message: str,
) -> None:
    """Duplicate, overlapping, inverted, and out-of-range expectation evidence is rejected."""
    expectation = _series().expected_observations
    assert expectation is not None

    with pytest.raises(ValueError, match=message):
        replace(expectation, **changes)


@pytest.mark.parametrize(
    ("kind", "boundary_date", "evidence_ref", "message"),
    (
        ("future", None, None, "kind must be a supported EndBoundaryKind"),
        (EndBoundaryKind.UNKNOWN, START, "review:end", "unknown end boundary cannot include"),
        (EndBoundaryKind.ACTIVE_AS_OF, None, "review:end", "requires both a date and evidence_ref"),
        (EndBoundaryKind.ACTIVE_AS_OF, END, None, "requires both a date and evidence_ref"),
    ),
)
def test_end_boundary_requires_supported_kind_and_complete_evidence(
    kind: object,
    boundary_date: date | None,
    evidence_ref: str | None,
    message: str,
) -> None:
    """Unknown and known lifecycle boundaries have disjoint evidence shapes."""
    with pytest.raises(ValueError, match=message):
        EndBoundaryEvidence(DATASET_ID, INSTRUMENT_ID, kind, boundary_date, evidence_ref)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        (
            {"queryable_from": END, "queryable_through": START},
            "queryable_from must be on or before queryable_through",
        ),
        ({"max_window_days": True}, "max_window_days must be from 1 to"),
        ({"provider_symbols": "600519"}, "provider_symbols must be a nonempty iterable"),
        ({"provider_symbols": None}, "provider_symbols must be a nonempty iterable"),
        ({"provider_symbols": ()}, "provider_symbols must be nonempty and unique"),
        (
            {"provider_symbols": ("600519", "600519")},
            "provider_symbols must be nonempty and unique",
        ),
    ),
)
def test_source_retrieval_evidence_rejects_invalid_ranges_and_symbols(
    changes: dict[str, object],
    message: str,
) -> None:
    """Source query capability must use a bounded range and unique symbol set."""
    capability = _series().source_retrieval
    assert capability is not None

    with pytest.raises(ValueError, match=message):
        replace(capability, **changes)


def test_source_symbol_cap_is_checked_before_materializing_the_complete_iterable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-symbol evidence fails closed when its sequence exceeds the cap."""
    capability = _series().source_retrieval
    assert capability is not None
    monkeypatch.setattr(reconcile_module, "MAX_PROVIDER_SYMBOLS", 1)

    with pytest.raises(ValueError, match="provider_symbols exceeds 1 entries"):
        replace(capability, provider_symbols=("600519", "000001"))


def test_complete_series_still_does_not_attest_all_a_universe() -> None:
    """One complete instrument never proves the registry or market universe census."""
    report = reconcile_stock_daily_gaps((_series(),))

    assert report.series[0].status.value == "COMPLETE"
    assert report.all_requested_series_complete is True
    assert report.all_a_universe_complete is False
    assert report.to_dict()["scope"]["universe_census_attested"] is False


def test_report_records_total_evidence_date_entries_at_the_budget_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact date and plan-size budgets are accepted and recorded in the report."""
    monkeypatch.setattr(reconcile_module, "MAX_TOTAL_EVIDENCE_DATE_ENTRIES", 20)
    plan = _plan()
    monkeypatch.setattr(reconcile_module, "MAX_TOTAL_PLAN_INSTRUMENTS", 1)
    monkeypatch.setattr(reconcile_module, "MAX_TOTAL_PLAN_CHUNKS", len(plan.candidate_chunks))

    report = reconcile_stock_daily_gaps(
        (_series(),),
        backfill_plans={(DATASET_ID, SOURCE_ID): plan},
    )

    limits = report.to_dict()["limits"]
    assert limits["max_total_evidence_date_entries"] == 20
    assert limits["total_evidence_date_entries"] == 20
    assert limits["max_total_plan_instruments"] == 1
    assert limits["total_plan_instruments"] == 1
    assert limits["max_total_plan_chunks"] == len(plan.candidate_chunks)
    assert limits["total_plan_chunks"] == len(plan.candidate_chunks)
    assert limits["total_plan_matching_work"] == 1


def test_total_evidence_date_budget_rejects_cumulative_series_before_materializing_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cumulative date entries are counted incrementally across different securities."""
    monkeypatch.setattr(reconcile_module, "MAX_TOTAL_EVIDENCE_DATE_ENTRIES", 39)
    first = _series()
    second = _series(instrument_id="CN:000001")
    consumed: list[str] = []

    def series_input() -> Iterator[StockDailySeriesEvidence]:
        consumed.append("first")
        yield first
        consumed.append("second")
        yield second
        consumed.append("third")
        yield _series(instrument_id="CN:000002")

    with pytest.raises(ValueError, match="total evidence date entries exceed 39"):
        reconcile_stock_daily_gaps(series_input())

    assert consumed == ["first", "second"]


@pytest.mark.parametrize(
    ("options", "message"),
    (
        ({"max_candidate_windows": 0}, "max_candidate_windows must be from 1 to"),
        ({"max_candidate_windows": True}, "max_candidate_windows must be from 1 to"),
        ({"max_window_days": 0}, "max_window_days must be from 1 to"),
        ({"max_window_days": False}, "max_window_days must be from 1 to"),
    ),
)
def test_reconciliation_limits_require_bounded_positive_integers(
    options: dict[str, object],
    message: str,
) -> None:
    """Zero, booleans, and out-of-range values cannot weaken the work limits."""
    with pytest.raises(ValueError, match=message):
        reconcile_stock_daily_gaps((_series(),), **options)


@pytest.mark.parametrize("invalid_series", ("not-a-series", 7))
def test_series_input_rejects_non_iterable_containers(invalid_series: object) -> None:
    """The reconciliation input must be a sequence of typed series evidence."""
    with pytest.raises(ValueError, match="series must be an iterable"):
        reconcile_stock_daily_gaps(invalid_series)


def test_series_input_rejects_wrong_item_types() -> None:
    """A malformed row cannot be silently skipped from the reconciliation scope."""
    with pytest.raises(TypeError, match="series items must be StockDailySeriesEvidence"):
        reconcile_stock_daily_gaps((object(),))


def test_series_count_limit_is_checked_before_accepting_the_next_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global series cap rejects the first excess item without truncation."""
    monkeypatch.setattr(reconcile_module, "MAX_SERIES_PER_RECONCILIATION", 1)
    consumed: list[str] = []

    def series_input() -> Iterator[StockDailySeriesEvidence]:
        consumed.append("first")
        yield _series()
        consumed.append("second")
        yield _series(instrument_id="CN:000001")

    with pytest.raises(ValueError, match="series exceeds 1 items"):
        reconcile_stock_daily_gaps(series_input())

    assert consumed == ["first", "second"]


@pytest.mark.parametrize(
    ("limit_name", "message"),
    (
        ("MAX_TOTAL_PLAN_INSTRUMENTS", "total plan instruments exceed 0"),
        ("MAX_TOTAL_PLAN_CHUNKS", "total plan chunks exceed 0"),
    ),
)
def test_plan_size_budgets_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    message: str,
) -> None:
    """Oversized plans are rejected before reconciliation can omit their route rows."""
    plan = _plan()
    monkeypatch.setattr(reconcile_module, limit_name, 0)

    with pytest.raises(ValueError, match=message):
        reconcile_stock_daily_gaps(
            (_series(),),
            backfill_plans={(DATASET_ID, SOURCE_ID): plan},
        )


def test_plan_mapping_rejects_invalid_container_keys_values_and_identity() -> None:
    """Plans are keyed by exact dataset/source identity and cannot be duck-typed."""
    plan = _plan()

    with pytest.raises(TypeError, match="backfill_plans must be a mapping"):
        reconcile_stock_daily_gaps((_series(),), backfill_plans=[("invalid", plan)])
    with pytest.raises(ValueError, match="backfill plan keys must be"):
        reconcile_stock_daily_gaps((_series(),), backfill_plans={"invalid": plan})
    with pytest.raises(TypeError, match="backfill_plans values must be StockDailyBackfillPlan"):
        reconcile_stock_daily_gaps((_series(),), backfill_plans={(DATASET_ID, SOURCE_ID): object()})
    with pytest.raises(ValueError, match="mapping key differs from its declared identity"):
        reconcile_stock_daily_gaps(
            (_series(),), backfill_plans={(DATASET_ID, "another.source"): plan}
        )
    with pytest.raises(ValueError, match="plan_sha256 must be a lowercase 64-character SHA-256"):
        reconcile_stock_daily_gaps(
            (_series(),),
            backfill_plans={(DATASET_ID, SOURCE_ID): replace(plan, plan_sha256="invalid")},
        )


@pytest.mark.parametrize("invalid_member", ("instrument", "chunk"))
def test_plan_index_rejects_untyped_instrument_and_chunk_rows(invalid_member: str) -> None:
    """Plan indexes refuse malformed rows instead of silently dropping routes."""
    plan = _plan()
    if invalid_member == "instrument":
        invalid_plan = replace(plan, instruments=(object(),))
        expected_error = "plan instruments must be InstrumentBackfillResult values"
    else:
        invalid_instrument = replace(plan.instruments[0], candidate_chunks=(object(),))
        invalid_plan = replace(plan, instruments=(invalid_instrument,))
        expected_error = "plan chunks must be StockDailyBackfillChunk values"

    with pytest.raises(TypeError, match=expected_error):
        reconcile_stock_daily_gaps(
            (_series(),),
            backfill_plans={(DATASET_ID, SOURCE_ID): invalid_plan},
        )


def test_duplicate_instrument_rows_in_one_plan_are_not_selected_arbitrarily() -> None:
    """Duplicate canonical rows make that route ambiguous and leave gaps visible."""
    plan = _plan()
    duplicate_plan = replace(plan, instruments=(plan.instruments[0], plan.instruments[0]))
    evidence = _series(observed=(WEEKDAYS[0],))

    report = reconcile_stock_daily_gaps(
        (evidence,),
        backfill_plans={(DATASET_ID, SOURCE_ID): duplicate_plan},
    )

    item = report.series[0]
    assert item.candidates == ()
    assert set(item.unplanned_gap_dates) == set(WEEKDAYS[1:])
    assert any(
        issue.code is ReconciliationIssueCode.BACKFILL_INSTRUMENT_MISSING for issue in item.issues
    )


def test_unsorted_plan_chunks_are_rejected_before_matching() -> None:
    """Binary search is used only when the complete plan chunk sequence is valid."""
    plan = _plan(max_window_days=1)
    instrument = replace(
        plan.instruments[0],
        candidate_chunks=tuple(reversed(plan.candidate_chunks)),
    )
    invalid_plan = replace(plan, instruments=(instrument,))

    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        reconcile_stock_daily_gaps(
            (_series(),),
            backfill_plans={(DATASET_ID, SOURCE_ID): invalid_plan},
        )


def test_ordered_closed_interval_overlapping_plan_chunks_are_rejected() -> None:
    """Sorted chunks that share an inclusive endpoint are still overlapping."""
    plan = _plan(max_window_days=1)
    first, second, *remaining = plan.instruments[0].candidate_chunks
    overlapping_second = replace(second, window_start=first.window_end)
    chunks = (first, overlapping_second, *remaining)
    instrument = replace(plan.instruments[0], candidate_chunks=chunks)
    invalid_plan = replace(plan, instruments=(instrument,))

    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        reconcile_stock_daily_gaps(
            (_series(),),
            backfill_plans={(DATASET_ID, SOURCE_ID): invalid_plan},
        )


def test_total_plan_matching_work_budget_raises_instead_of_returning_partial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Instrument and chunk lookup work cannot silently exhaust a global budget."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[0],),
        provider_symbols=(_provider_symbol(plan),),
    )
    monkeypatch.setattr(reconcile_module, "MAX_TOTAL_PLAN_MATCHING_WORK", 2)

    with pytest.raises(ValueError, match="total plan matching work exceeds 2 units"):
        reconcile_stock_daily_gaps(
            (evidence,),
            backfill_plans={(DATASET_ID, SOURCE_ID): plan},
        )


def test_reconciliation_json_and_digest_are_deterministic() -> None:
    """Identical evidence produces byte-identical JSON and a stable content pin."""
    plan = _plan()
    evidence = _series(
        observed=(WEEKDAYS[0],),
        provider_symbols=(_provider_symbol(plan),),
    )

    first = reconcile_stock_daily_gaps((evidence,), backfill_plans={(DATASET_ID, SOURCE_ID): plan})
    second = reconcile_stock_daily_gaps((evidence,), backfill_plans={(DATASET_ID, SOURCE_ID): plan})

    assert first.to_json() == second.to_json()
    assert first.report_sha256 == second.report_sha256
    assert first.report_sha256 != "0" * 64


def test_duplicate_series_identity_is_rejected_instead_of_double_counted() -> None:
    """Duplicate publication scopes cannot inflate candidate or completeness counts."""
    evidence = _series()

    with pytest.raises(ValueError, match="may appear only once"):
        reconcile_stock_daily_gaps((evidence, evidence))


def test_source_query_range_is_applied_per_date() -> None:
    """A source capability pin limits candidates to dates it explicitly covers."""
    plan = _plan()
    base = _series(observed=(WEEKDAYS[0],), provider_symbols=(_provider_symbol(plan),))
    capability = SourceRetrievalEvidence(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        instrument_id=INSTRUMENT_ID,
        evidence_ref="provider:limited-window-v1",
        queryable_from=WEEKDAYS[1],
        queryable_through=WEEKDAYS[2],
        provider_symbols=(_provider_symbol(plan),),
        max_window_days=10,
    )
    evidence = StockDailySeriesEvidence(
        coverage_request=base.coverage_request,
        publication=base.publication,
        expected_observations=base.expected_observations,
        end_boundary=base.end_boundary,
        source_retrieval=capability,
    )

    report = reconcile_stock_daily_gaps((evidence,), backfill_plans={(DATASET_ID, SOURCE_ID): plan})

    item = report.series[0]
    assert tuple(
        day for candidate in item.candidates for day in candidate.expected_missing_dates
    ) == (
        WEEKDAYS[1],
        WEEKDAYS[2],
    )
    assert set(item.unplanned_gap_dates) == {WEEKDAYS[3], WEEKDAYS[4]}
    assert any(
        issue.code is ReconciliationIssueCode.SOURCE_CAPABILITY_EXCLUDES_DATE
        for issue in item.issues
    )
