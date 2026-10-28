"""Behavioral tests for deterministic registry-backed stock-daily planning."""

from datetime import date, datetime, timedelta

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
from opendata.pipeline.stock_daily_backfill_plan import (
    BackfillBlockReason,
    StockDailyBackfillPlan,
    build_stock_daily_backfill_plan,
)

DATASET_ID = "cn-equity-stock-daily"
SOURCE_ID = "akshare.daily"
AS_OF = date(2026, 10, 27)
START = date(2020, 1, 1)


def _contract() -> DatasetContract:
    """Build the exact unadjusted daily-bar contract accepted by stock_daily."""
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


def _task(*, enabled: bool = False) -> TaskDefinition:
    """Return the disabled task declaration for the current stock_daily adapter."""
    return TaskDefinition(
        job_id="cn-stock-daily-backfill-fixture",
        version="v1",
        dataset_id=DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        schedule=(
            ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri") if enabled else None
        ),
        enabled=enabled,
    )


def _lifecycle(
    instrument_id: str = "CN:600519",
    *,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    status_as_of: date = AS_OF,
    known_start_date: date | None = START,
    start_boundary_unknown: bool = False,
    delisted_on: date | None = None,
) -> InstrumentLifecycle:
    """Build one lifecycle with explicit independent boundary evidence."""
    return InstrumentLifecycle(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        instrument_id=instrument_id,
        lifecycle_version="life-v1",
        status=status,
        status_as_of=status_as_of,
        status_evidence_ref="review:status-v1",
        known_start_date=known_start_date,
        start_boundary_unknown=start_boundary_unknown,
        start_evidence_ref=None if start_boundary_unknown else "exchange:listing-v1",
        delisted_on=delisted_on,
        delist_evidence_ref="exchange:delist-v1" if delisted_on is not None else None,
    )


def _alias(
    symbol: str,
    instrument_id: str = "CN:600519",
    *,
    valid_from: date | None = START,
    valid_through: date | None = AS_OF,
    version: str = "ticker-v1",
    evidence_ref: str | None = None,
) -> InstrumentAlias:
    """Build one effective-dated alias with pinned evidence."""
    return InstrumentAlias(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        alias=symbol,
        instrument_id=instrument_id,
        alias_version=version,
        evidence_ref=evidence_ref or f"review:{version}",
        valid_from=valid_from,
        valid_through=valid_through,
    )


def _registry(
    lifecycles: tuple[InstrumentLifecycle, ...],
    aliases: tuple[InstrumentAlias, ...],
    *,
    evidence_ref: str = "review:registry-v1",
) -> InstrumentReferenceRegistry:
    """Freeze test rows in a versioned registry snapshot."""
    return InstrumentReferenceRegistry(
        registry_version="registry-v1",
        evidence_ref=evidence_ref,
        lifecycles=lifecycles,
        aliases=aliases,
    )


def _build(
    lifecycles: tuple[InstrumentLifecycle, ...],
    aliases: tuple[InstrumentAlias, ...],
    *,
    as_of: date = AS_OF,
    max_window_days: int = 3660,
    contract: DatasetContract | None = None,
) -> StockDailyBackfillPlan:
    """Build a plan for explicit in-memory evidence fixtures."""
    return build_stock_daily_backfill_plan(
        _registry(lifecycles, aliases),
        task=_task(),
        contract=contract or _contract(),
        as_of=as_of,
        max_window_days=max_window_days,
    )


def test_closed_3661_day_history_splits_into_3660_and_one_valid_job_requests() -> None:
    start = AS_OF - timedelta(days=3660)
    lifecycle = _lifecycle(known_start_date=start)
    mapping = _alias("600519.SH", valid_from=start)

    plan = _build((lifecycle,), (mapping,))

    assert [chunk.window_days for chunk in plan.candidate_chunks] == [3660, 1]
    assert plan.candidate_chunks[0].window_start == start
    assert plan.candidate_chunks[0].window_end + timedelta(days=1) == (
        plan.candidate_chunks[1].window_start
    )
    assert plan.candidate_chunks[-1].window_end == AS_OF
    assert all(chunk.request.run_type == "backfill" for chunk in plan.candidate_chunks)
    assert all(
        chunk.request.backfill_start == chunk.window_start for chunk in plan.candidate_chunks
    )
    assert all(chunk.request.backfill_end == chunk.window_end for chunk in plan.candidate_chunks)
    assert all(chunk.request.shard == "600519" for chunk in plan.candidate_chunks)
    assert all(chunk.request.partition == {} for chunk in plan.candidate_chunks)
    isolated_request = plan.candidate_chunks[0].request
    isolated_request.query["symbol"] = "000001.SZ"
    assert plan.candidate_chunks[0].request.query["symbol"] == "600519.SH"
    assert plan.execution_authorized is False
    assert plan.full_history_ready is False
    assert plan.source_history_limit == "unknown"


def test_effective_alias_version_changes_split_requests_at_inclusive_boundaries() -> None:
    lifecycle = _lifecycle(known_start_date=date(2020, 1, 1))
    first_end = date(2020, 12, 31)
    aliases = (
        _alias(
            "600519.SH",
            valid_from=date(2020, 1, 1),
            valid_through=first_end,
            version="ticker-old",
        ),
        _alias(
            "688001.SH",
            valid_from=date(2021, 1, 1),
            valid_through=AS_OF,
            version="ticker-new",
        ),
    )

    plan = _build((lifecycle,), aliases, as_of=date(2021, 1, 2))

    chunks = plan.candidate_chunks
    assert [(chunk.source_symbol, chunk.alias_version) for chunk in chunks] == [
        ("600519.SH", "ticker-old"),
        ("688001.SH", "ticker-new"),
    ]
    assert chunks[0].window_start == date(2020, 1, 1)
    assert chunks[0].window_end == first_end
    assert chunks[1].window_start == date(2021, 1, 1)
    assert chunks[1].window_end == date(2021, 1, 2)


def test_alias_gap_and_overlapping_aliases_are_explicitly_blocked() -> None:
    plan_end = date(2020, 1, 6)
    lifecycle = _lifecycle(known_start_date=date(2020, 1, 1), status_as_of=plan_end)
    aliases = (
        _alias(
            "600519.SH",
            valid_from=date(2020, 1, 1),
            valid_through=date(2020, 1, 2),
            version="ticker-a",
        ),
        _alias(
            "600521.SH",
            valid_from=date(2020, 1, 5),
            valid_through=plan_end,
            version="ticker-b",
        ),
        _alias(
            "600522.SH",
            valid_from=date(2020, 1, 5),
            valid_through=plan_end,
            version="ticker-c",
        ),
    )

    plan = _build((lifecycle,), aliases, as_of=plan_end)

    result = plan.instruments[0]
    assert [(chunk.window_start, chunk.window_end) for chunk in result.candidate_chunks] == [
        (date(2020, 1, 1), date(2020, 1, 2))
    ]
    assert any(
        blocker.reason is BackfillBlockReason.SOURCE_ALIAS_GAP
        and blocker.range_start == date(2020, 1, 3)
        and blocker.range_end == date(2020, 1, 4)
        for blocker in result.blockers
    )
    assert any(
        blocker.reason is BackfillBlockReason.AMBIGUOUS_SOURCE_ALIAS
        and blocker.range_start == date(2020, 1, 5)
        and blocker.range_end == plan_end
        for blocker in result.blockers
    )


def test_unknown_start_blocks_candidate_history_and_unknown_status_blocks_full_readiness() -> None:
    unknown_start = _lifecycle(
        "CN:UNKNOWN-START",
        known_start_date=None,
        start_boundary_unknown=True,
    )
    status_cutoff = date(2020, 1, 3)
    unknown_status = _lifecycle(
        "CN:UNKNOWN-STATUS",
        status=LifecycleStatus.UNKNOWN,
        status_as_of=status_cutoff,
        known_start_date=date(2020, 1, 1),
    )
    aliases = (
        _alias("600519.SH", "CN:UNKNOWN-START", valid_from=None, version="start-unknown"),
        _alias(
            "000001.SZ",
            "CN:UNKNOWN-STATUS",
            valid_from=date(2020, 1, 1),
            valid_through=AS_OF,
            version="status-unknown",
        ),
    )

    plan = _build((unknown_start, unknown_status), aliases)
    results = {result.instrument_id: result for result in plan.instruments}

    assert results["CN:UNKNOWN-START"].candidate_chunks == ()
    assert any(
        blocker.reason is BackfillBlockReason.UNKNOWN_START_BOUNDARY and blocker.range_start is None
        for blocker in results["CN:UNKNOWN-START"].blockers
    )
    assert results["CN:UNKNOWN-STATUS"].candidate_chunks
    assert results["CN:UNKNOWN-STATUS"].candidate_chunks[-1].window_end == status_cutoff
    assert any(
        blocker.reason is BackfillBlockReason.UNKNOWN_LIFECYCLE_STATUS
        for blocker in results["CN:UNKNOWN-STATUS"].blockers
    )
    assert plan.registry_lifecycle_count == 2
    assert plan.full_history_ready is False


def test_stale_active_status_plans_only_through_evidence_date_and_blocks_tail() -> None:
    status_cutoff = date(2020, 1, 3)
    lifecycle = _lifecycle(known_start_date=date(2020, 1, 1), status_as_of=status_cutoff)
    mapping = _alias("600519.SH", valid_from=date(2020, 1, 1), valid_through=AS_OF)

    plan = _build((lifecycle,), (mapping,), as_of=date(2020, 1, 6))

    result = plan.instruments[0]
    assert result.candidate_chunks[-1].window_end == status_cutoff
    stale = next(
        blocker
        for blocker in result.blockers
        if blocker.reason is BackfillBlockReason.STALE_ACTIVE_STATUS
    )
    assert (stale.range_start, stale.range_end) == (date(2020, 1, 4), date(2020, 1, 6))
    assert plan.full_history_ready is False


def test_delisted_boundary_is_included_in_candidate_and_semantics_remain_blocked() -> None:
    last_recorded_date = date(2020, 1, 3)
    lifecycle = _lifecycle(
        status=LifecycleStatus.DELISTED,
        status_as_of=AS_OF,
        known_start_date=date(2020, 1, 1),
        delisted_on=last_recorded_date,
    )
    mapping = _alias(
        "600519.SH",
        valid_from=date(2020, 1, 1),
        valid_through=last_recorded_date,
    )

    plan = _build((lifecycle,), (mapping,), as_of=AS_OF)

    assert plan.candidate_chunks[0].window_end == last_recorded_date
    assert any(
        blocker.reason is BackfillBlockReason.DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED
        and blocker.evidence_refs == ("exchange:delist-v1",)
        for blocker in plan.instruments[0].blockers
    )


def test_bj_alias_is_blocked_before_candidate_request_generation() -> None:
    lifecycle = _lifecycle("CN:830001", known_start_date=START)
    mapping = _alias("830001.BJ", "CN:830001", valid_from=START, valid_through=AS_OF)

    plan = _build((lifecycle,), (mapping,))

    assert plan.candidate_chunks == ()
    assert any(
        blocker.reason is BackfillBlockReason.BJ_PROVIDER_MAPPING_UNVERIFIED
        and blocker.range_start == START
        and blocker.range_end == AS_OF
        and blocker.evidence_refs == (mapping.evidence_ref,)
        for blocker in plan.instruments[0].blockers
    )
    assert not any(
        blocker.reason is BackfillBlockReason.INVALID_PROVIDER_SYMBOL
        for blocker in plan.instruments[0].blockers
    )
    assert plan.execution_authorized is False


def test_invalid_provider_alias_is_preserved_as_blocker_without_a_request() -> None:
    lifecycle = _lifecycle(known_start_date=START)

    plan = _build((lifecycle,), (_alias("not-a-stock-symbol"),))

    assert plan.candidate_chunks == ()
    assert plan.instruments[0].blockers[0].reason is BackfillBlockReason.INVALID_PROVIDER_SYMBOL


def test_missing_alias_and_future_start_are_recorded_without_guessing_a_range() -> None:
    lifecycle = _lifecycle(known_start_date=START)
    no_alias_plan = _build((lifecycle,), ())
    future_lifecycle = _lifecycle(known_start_date=date(2027, 1, 1))
    future_plan = _build(
        (future_lifecycle,),
        (_alias("600519.SH", valid_from=date(2027, 1, 1), valid_through=None),),
        as_of=AS_OF,
    )

    assert no_alias_plan.candidate_chunks == ()
    assert no_alias_plan.instruments[0].blockers[0].reason is BackfillBlockReason.SOURCE_ALIAS_GAP
    assert future_plan.candidate_chunks == ()
    future_blocker = future_plan.instruments[0].blockers[0]
    assert future_blocker.reason is BackfillBlockReason.NOT_YET_IN_SCOPE
    assert future_blocker.range_start == date(2027, 1, 1)
    assert future_blocker.range_end is None


def test_registry_order_is_stable_and_digest_binds_all_registry_contents() -> None:
    first_lifecycle = _lifecycle("CN:600519")
    second_lifecycle = _lifecycle("CN:000001")
    first_alias = _alias("600519.SH", "CN:600519")
    second_alias = _alias("000001.SZ", "CN:000001")
    first = _build((first_lifecycle, second_lifecycle), (first_alias, second_alias))
    reversed_order = _build(
        (second_lifecycle, first_lifecycle),
        (second_alias, first_alias),
    )
    changed_evidence = _registry(
        (first_lifecycle, second_lifecycle),
        (first_alias, second_alias),
        evidence_ref="review:registry-v2",
    )
    changed = build_stock_daily_backfill_plan(
        changed_evidence,
        task=_task(),
        contract=_contract(),
        as_of=AS_OF,
    )
    changed_row = _build(
        (first_lifecycle, second_lifecycle),
        (
            _alias("600519.SH", "CN:600519", evidence_ref="review:changed-alias-evidence"),
            second_alias,
        ),
    )

    assert first.plan_sha256 == reversed_order.plan_sha256
    assert first.registry_sha256 == reversed_order.registry_sha256
    assert first.registry_lifecycle_count == 2
    assert first.plan_sha256 != changed.plan_sha256
    assert first.registry_sha256 != changed.registry_sha256
    assert first.registry_sha256 != changed_row.registry_sha256
    assert [result.instrument_id for result in first.instruments] == [
        "CN:000001",
        "CN:600519",
    ]


def test_contract_mismatch_and_empty_registry_scope_fail_closed() -> None:
    lifecycle = _lifecycle()
    mapping = _alias("600519.SH")
    wrong_contract = _contract().model_copy(update={"dataset_id": "other-dataset"})

    with pytest.raises(ValueError, match="contract identity"):
        _build((lifecycle,), (mapping,), contract=wrong_contract)
    with pytest.raises(ValueError, match="no lifecycle rows"):
        build_stock_daily_backfill_plan(
            _registry((), ()),
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
        )


@pytest.mark.parametrize("invalid_limit", (True, 0, 3661))
def test_invalid_date_and_window_limits_are_rejected(invalid_limit: int) -> None:
    lifecycle = _lifecycle()
    mapping = _alias("600519.SH")
    registry = _registry((lifecycle,), (mapping,))

    with pytest.raises(ValueError, match="as_of"):
        build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=datetime(2026, 10, 27),
        )
    with pytest.raises(ValueError, match="max_window_days"):
        build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
            max_window_days=invalid_limit,
        )


def test_task_must_match_disabled_stock_daily_fixture() -> None:
    lifecycle = _lifecycle()
    mapping = _alias("600519.SH")
    registry = _registry((lifecycle,), (mapping,))
    wrong_provider = _task().model_copy(update={"provider": "other-provider"})
    enabled_task = _task(enabled=True)

    with pytest.raises(ValueError, match="akshare stock_daily"):
        build_stock_daily_backfill_plan(
            registry,
            task=wrong_provider,
            contract=_contract(),
            as_of=AS_OF,
        )
    with pytest.raises(ValueError, match="must remain disabled"):
        build_stock_daily_backfill_plan(
            registry,
            task=enabled_task,
            contract=_contract(),
            as_of=AS_OF,
        )


def test_date_max_boundary_generates_one_day_without_overflow() -> None:
    final_date = date.max
    lifecycle = _lifecycle(
        known_start_date=final_date,
        status_as_of=final_date,
    )
    mapping = _alias("600519.SH", valid_from=final_date, valid_through=None)

    plan = _build((lifecycle,), (mapping,), as_of=final_date)

    assert len(plan.candidate_chunks) == 1
    assert plan.candidate_chunks[0].window_start == final_date
    assert plan.candidate_chunks[0].window_end == final_date
    assert plan.candidate_chunks[0].window_days == 1
