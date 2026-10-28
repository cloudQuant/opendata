"""Resource-budget guards for deterministic stock-daily backfill planning."""

from datetime import timedelta

import pytest

from opendata.data.providers.akshare.models.stock_daily import AkshareStockDailyFetcher
from opendata.pipeline import stock_daily_backfill_plan as planner
from opendata.pipeline.stock_daily_backfill_plan import BackfillBlockReason
from tests.test_stock_daily_backfill_plan import (
    AS_OF,
    _alias,
    _build,
    _contract,
    _lifecycle,
    _registry,
    _task,
)

_MAX_EXPECTED_CHUNKS = planner._MAX_CANDIDATE_CHUNKS + 1


def test_daily_full_market_sized_request_is_rejected_before_request_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AS_OF - timedelta(days=planner._MAX_CANDIDATE_CHUNKS)
    lifecycle = _lifecycle(known_start_date=start)
    alias = _alias("600519.SH", valid_from=start, valid_through=AS_OF)
    registry = _registry((lifecycle,), (alias,))

    def unexpected_materialization(*args: object, **kwargs: object) -> None:
        pytest.fail("preflight must reject before planning any instrument")

    monkeypatch.setattr(planner, "_plan_instrument", unexpected_materialization)
    errors: list[str] = []
    for _ in range(2):
        with pytest.raises(planner._CandidateChunkBudgetError) as caught:
            planner.build_stock_daily_backfill_plan(
                registry,
                task=_task(),
                contract=_contract(),
                as_of=AS_OF,
                max_window_days=1,
            )
        errors.append(str(caught.value))

    assert errors[0] == errors[1]
    assert f"estimated {_MAX_EXPECTED_CHUNKS} candidate chunks" in errors[0]
    assert "no partial plan was returned" in errors[0]
    assert "Partition by reviewed instrument/time scope" in errors[0]
    assert "do not establish full universe or history completeness" in errors[0]


def test_candidate_budget_ignores_unroutable_or_uncovered_intervals() -> None:
    start = AS_OF - timedelta(days=planner._MAX_CANDIDATE_CHUNKS)
    lifecycle = _lifecycle(known_start_date=start)
    alias = _alias("600519.SH", valid_from=AS_OF, valid_through=AS_OF)

    plan = _build(
        (lifecycle,),
        (alias,),
        max_window_days=1,
    )

    assert len(plan.candidate_chunks) == 1
    assert plan.candidate_chunks[0].window_start == AS_OF
    assert plan.candidate_chunks[0].window_end == AS_OF
    assert any(
        blocker.reason is BackfillBlockReason.SOURCE_ALIAS_GAP
        and blocker.range_start == start
        and blocker.range_end == AS_OF - timedelta(days=1)
        for blocker in plan.instruments[0].blockers
    )
    assert plan.full_history_ready is False


def test_invalid_alias_does_not_create_a_false_chunk_budget_rejection() -> None:
    start = AS_OF - timedelta(days=planner._MAX_CANDIDATE_CHUNKS)
    lifecycle = _lifecycle(known_start_date=start)
    alias = _alias("not-a-stock-symbol", valid_from=start, valid_through=AS_OF)

    plan = _build((lifecycle,), (alias,), max_window_days=1)

    assert plan.candidate_chunks == ()
    assert plan.instruments[0].blockers[0].reason is BackfillBlockReason.INVALID_PROVIDER_SYMBOL


@pytest.mark.parametrize(
    ("invalid_symbol", "expected_reason"),
    [
        ("not-a-stock-symbol", BackfillBlockReason.INVALID_PROVIDER_SYMBOL),
        ("830001.BJ", BackfillBlockReason.BJ_PROVIDER_MAPPING_UNVERIFIED),
    ],
)
def test_invalid_alias_after_candidate_budget_is_full_remains_a_blocker(
    monkeypatch: pytest.MonkeyPatch,
    invalid_symbol: str,
    expected_reason: BackfillBlockReason,
) -> None:
    monkeypatch.setattr(planner, "_MAX_CANDIDATE_CHUNKS", 1)
    first_lifecycle = _lifecycle("CN:000001", known_start_date=AS_OF)
    second_lifecycle = _lifecycle("CN:999999", known_start_date=AS_OF)
    registry = _registry(
        (first_lifecycle, second_lifecycle),
        (
            _alias("000001.SZ", "CN:000001", valid_from=AS_OF, valid_through=AS_OF),
            _alias(invalid_symbol, "CN:999999", valid_from=AS_OF, valid_through=AS_OF),
        ),
    )

    plan = planner.build_stock_daily_backfill_plan(
        registry,
        task=_task(),
        contract=_contract(),
        as_of=AS_OF,
        max_window_days=1,
    )

    assert len(plan.candidate_chunks) == 1
    assert plan.candidate_chunks[0].instrument_id == "CN:000001"
    assert plan.instruments[1].candidate_chunks == ()
    assert any(
        blocker.reason is expected_reason
        and blocker.instrument_id == "CN:999999"
        and blocker.evidence_refs == ("review:ticker-v1",)
        for blocker in plan.instruments[1].blockers
    )


def test_runtime_budget_guard_refuses_an_underestimated_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = AS_OF - timedelta(days=2)
    lifecycle = _lifecycle(known_start_date=start)
    alias = _alias("600519.SH", valid_from=start, valid_through=AS_OF)
    registry = _registry((lifecycle,), (alias,))
    monkeypatch.setattr(planner, "_MAX_CANDIDATE_CHUNKS", 2)

    original_transform_query = AkshareStockDailyFetcher.transform_query
    validation_calls = 0

    def succeeds_after_preflight(fetcher: AkshareStockDailyFetcher, **kwargs: object) -> object:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 1:
            raise ValueError("simulated transient route validation")
        return original_transform_query(fetcher, **kwargs)

    original_chunk_type = planner.StockDailyBackfillChunk
    constructed_chunks = 0

    def count_chunks(**kwargs: object) -> object:
        nonlocal constructed_chunks
        constructed_chunks += 1
        return original_chunk_type(**kwargs)

    monkeypatch.setattr(AkshareStockDailyFetcher, "transform_query", succeeds_after_preflight)
    monkeypatch.setattr(planner, "StockDailyBackfillChunk", count_chunks)

    with pytest.raises(planner._CandidateChunkBudgetError, match="at least 3 candidate chunks"):
        planner.build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
            max_window_days=1,
        )

    assert constructed_chunks == 2


def test_alias_sweep_matches_closed_boundary_and_duplicate_event_semantics() -> None:
    start = AS_OF.replace(year=2020, month=1, day=1)
    switch_date = start + timedelta(days=2)
    end = start + timedelta(days=4)
    first = _alias(
        "600519.SH",
        valid_from=start,
        valid_through=switch_date - timedelta(days=1),
    )
    second = _alias("000001.SZ", valid_from=switch_date, valid_through=end)
    same_day = _alias(
        "600001.SZ",
        valid_from=switch_date,
        valid_through=switch_date,
    )
    aliases = (first, first, second, same_day)

    summaries = tuple(planner._iter_alias_segment_summaries(start, end, aliases))
    materialized = planner._alias_segments(start, end, aliases)
    expected_summaries = tuple(
        (segment_start, segment_end, len(active), active[0] if len(active) == 1 else None)
        for segment_start, segment_end, active in materialized
    )

    assert summaries == expected_summaries
    assert [(segment[0], segment[1], segment[2]) for segment in summaries] == [
        (start, switch_date - timedelta(days=1), 1),
        (switch_date, switch_date, 2),
        (switch_date + timedelta(days=1), end, 1),
    ]


def test_alias_row_budget_rejects_before_alias_segment_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _lifecycle()
    aliases = (
        _alias("600519.SH"),
        _alias("600520.SH"),
        _alias("000001.SZ"),
    )
    registry = _registry((lifecycle,), aliases)
    monkeypatch.setattr(planner, "_MAX_SCOPED_ALIAS_ROWS", 2)

    def forbidden_materialization(*args: object, **kwargs: object) -> None:
        pytest.fail("row budget must reject before creating alias segments")

    monkeypatch.setattr(planner, "_alias_segments", forbidden_materialization)
    monkeypatch.setattr(planner, "_registry_sha256", forbidden_materialization)

    with pytest.raises(planner._AliasPlanningBudgetError, match="scoped alias rows"):
        planner.build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
        )


def test_active_alias_membership_budget_rejects_before_sorted_tuple_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _lifecycle()
    registry = _registry(
        (lifecycle,),
        (_alias("600519.SH"), _alias("600520.SH"), _alias("000001.SZ")),
    )
    monkeypatch.setattr(planner, "_MAX_SCOPED_ALIAS_ROWS", 10)
    monkeypatch.setattr(planner, "_MAX_ALIAS_SEGMENTS", 10)
    monkeypatch.setattr(planner, "_MAX_ACTIVE_ALIAS_MEMBERSHIPS", 2)

    def forbidden_materialization(*args: object, **kwargs: object) -> None:
        pytest.fail("membership budget must reject before creating sorted alias tuples")

    monkeypatch.setattr(planner, "_alias_segments", forbidden_materialization)
    monkeypatch.setattr(planner, "_registry_sha256", forbidden_materialization)

    with pytest.raises(
        planner._AliasPlanningBudgetError,
        match="active alias memberships observed 3",
    ):
        planner.build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
        )


def test_actual_alias_materialization_rechecks_membership_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _lifecycle()
    registry = _registry(
        (lifecycle,),
        (_alias("600519.SH"), _alias("600520.SH"), _alias("000001.SZ")),
    )
    monkeypatch.setattr(planner, "_MAX_SCOPED_ALIAS_ROWS", 10)
    monkeypatch.setattr(planner, "_MAX_ALIAS_SEGMENTS", 10)
    monkeypatch.setattr(planner, "_MAX_ACTIVE_ALIAS_MEMBERSHIPS", 2)
    original_sweep = planner._iter_alias_segment_summaries

    def undercount_memberships(*args: object, **kwargs: object):
        for segment_start, segment_end, active_count, unique_alias in original_sweep(
            *args, **kwargs
        ):
            yield segment_start, segment_end, min(active_count, 2), unique_alias

    monkeypatch.setattr(planner, "_iter_alias_segment_summaries", undercount_memberships)

    with pytest.raises(
        planner._AliasPlanningBudgetError,
        match="while materializing aliases: active alias memberships observed 3",
    ):
        planner.build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
        )


def test_alias_segment_budget_counts_gaps_and_refuses_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_end = AS_OF.replace(year=2020, month=1, day=2)
    second_start = first_end + timedelta(days=1)
    lifecycle = _lifecycle(known_start_date=AS_OF.replace(year=2020, month=1, day=1))
    registry = _registry(
        (lifecycle,),
        (
            _alias(
                "600519.SH",
                valid_from=lifecycle.known_start_date,
                valid_through=first_end,
            ),
            _alias("000001.SZ", valid_from=second_start, valid_through=AS_OF),
        ),
    )
    monkeypatch.setattr(planner, "_MAX_ALIAS_SEGMENTS", 1)
    monkeypatch.setattr(planner, "_MAX_ACTIVE_ALIAS_MEMBERSHIPS", 10)

    def forbidden_materialization(*args: object, **kwargs: object) -> None:
        pytest.fail("segment budget must reject before creating alias segments")

    monkeypatch.setattr(planner, "_alias_segments", forbidden_materialization)
    monkeypatch.setattr(planner, "_registry_sha256", forbidden_materialization)

    with pytest.raises(planner._AliasPlanningBudgetError, match="alias segments observed 2"):
        planner.build_stock_daily_backfill_plan(
            registry,
            task=_task(),
            contract=_contract(),
            as_of=AS_OF,
        )
