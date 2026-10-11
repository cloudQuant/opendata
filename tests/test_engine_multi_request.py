"""Offline contract suite for the fan-out and join declarations.

No case can reach a network: the transport is a recording callable the kernel is handed, and the
only assertions about sending are the exact identity and order of the plan steps that callable
was given. Following the repo acceptance rule, every refusal guard here has a positive arm that
shows the guard is able to fire -- a zero (an empty plan, a dropped row) is only meaningful once
the test has watched the guard stop it.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from opendata.data.providers._engine.multi_request import (
    FanOutPlan,
    FanOutSpec,
    JoinFetchSpec,
    JoinSpec,
    MultiRequestError,
    PlanStep,
    build_fan_out_plan,
    build_join_plan,
    execute_plan,
    merge_rows,
    variants_from_rows,
)


class RecordingFetcher:
    """The injected transport seam: records every plan step it is handed."""

    def __init__(self, rows_by_variant: dict[str, list[dict[str, object]]]) -> None:
        self.calls: list[PlanStep] = []
        self._rows = rows_by_variant

    def __call__(self, step: PlanStep) -> list[dict[str, object]]:
        self.calls.append(step)
        return self._rows.get(step.variant, [])


def symbol_fan_out(**overrides: object) -> FanOutSpec:
    """One symbol-list fan-out in the shape OBB2-intrinio-EquityInfo declares."""
    declared: dict[str, object] = {"source": "param", "field": "symbols", "request_param": "symbol"}
    declared.update(overrides)
    return FanOutSpec(**declared)  # type: ignore[arg-type]


def ratios_join(**overrides: object) -> JoinSpec:
    """A hop-1-id -> hop-2-row join in the shape FinancialRatios declares."""
    declared: dict[str, object] = {"left_key": "id", "right_key": "fundamental_id", "how": "inner"}
    declared.update(overrides)
    return JoinSpec(**declared)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- fan-out: plan


def test_plan_enumerates_variants_in_declared_order() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL", "MSFT", "NVDA"])

    assert plan.variants == ("AAPL", "MSFT", "NVDA")
    assert plan.steps == (
        PlanStep(index=0, variant="AAPL", params={"symbol": "AAPL"}),
        PlanStep(index=1, variant="MSFT", params={"symbol": "MSFT"}),
        PlanStep(index=2, variant="NVDA", params={"symbol": "NVDA"}),
    )


def test_plan_dedupes_preserving_first_occurrence() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL", "MSFT", "AAPL"])

    assert plan.variants == ("AAPL", "MSFT")


def test_plan_from_first_response_rows() -> None:
    # The FinancialRatios hop: the variant list is the id column of the first response's rows.
    left = ({"id": 101}, {"id": 202}, {"id": 101})
    plan = build_fan_out_plan(FanOutSpec(source="rows", field="id"), variants_from_rows(left, "id"))

    assert plan.variants == ("101", "202")
    assert all(step.params == {"id": step.variant} for step in plan.steps)


def test_variant_cap_boundary_allows_exactly_the_cap() -> None:
    plan = build_fan_out_plan(symbol_fan_out(max_variants=3), ["A", "B", "C"])

    assert len(plan.steps) == 3


def test_variant_cap_refuses_one_over_the_bound() -> None:
    with pytest.raises(MultiRequestError) as refused:
        build_fan_out_plan(symbol_fan_out(max_variants=2), ["A", "B", "C"])

    assert refused.value.code == "FANOUT_VARIANT_CAP"


def test_plan_refuses_empty_variant_list() -> None:
    with pytest.raises(MultiRequestError) as refused:
        build_fan_out_plan(symbol_fan_out(), [])

    assert refused.value.code == "FANOUT_NO_VARIANTS"


def test_plan_refuses_one_bare_string_instead_of_a_list() -> None:
    # Iterating "AAPL,MSFT" character-by-character would enumerate ten one-letter requests.
    with pytest.raises(MultiRequestError) as refused:
        build_fan_out_plan(symbol_fan_out(), "AAPL,MSFT")

    assert refused.value.code == "FANOUT_VARIANTS_NOT_A_LIST"


def test_plan_refuses_variant_that_cannot_render() -> None:
    with pytest.raises(MultiRequestError) as refused:
        build_fan_out_plan(symbol_fan_out(), ["AAPL", 1.5])

    assert refused.value.code == "FANOUT_VARIANT_TYPE"


def test_variants_from_rows_refuses_row_missing_the_source_key() -> None:
    with pytest.raises(MultiRequestError) as refused:
        variants_from_rows(({"id": 1}, {"ticker": "AAPL"}), "id")

    assert refused.value.code == "FANOUT_SOURCE_KEY_MISSING"


# ----------------------------------------------------------------------------- fan-out: execute


def test_execute_hands_the_fetcher_exactly_the_plan_steps() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL", "MSFT"])
    fetch = RecordingFetcher({"AAPL": [{"p": 1}], "MSFT": [{"p": 2}]})

    rows = execute_plan(plan, fetch)

    # The proof the census asked for: the single-URL loop is gone -- one call per enumerated
    # variant, with the step objects this module built, in plan order.
    assert fetch.calls == list(plan.steps)
    assert rows == ({"p": 1}, {"p": 2})


def test_execute_fail_closed_on_variant_that_yielded_nothing() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL", "GONE"])
    fetch = RecordingFetcher({"AAPL": [{"p": 1}]})

    with pytest.raises(MultiRequestError) as refused:
        execute_plan(plan, fetch)

    assert refused.value.code == "FANOUT_EMPTY_VARIANT"
    assert fetch.calls == list(plan.steps)


def test_execute_skips_empty_variant_when_not_all_required() -> None:
    plan = build_fan_out_plan(symbol_fan_out(all_required=False), ["AAPL", "GONE"])

    rows = execute_plan(plan, RecordingFetcher({"AAPL": [{"p": 1}]}))

    assert rows == ({"p": 1},)


def test_execute_refuses_fetcher_that_returned_non_rows() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL"])

    def broken(_step: PlanStep) -> list[object]:
        return ["not a record"]

    with pytest.raises(MultiRequestError) as refused:
        execute_plan(plan, broken)  # type: ignore[arg-type]

    assert refused.value.code == "FANOUT_STEP_ROWS"


def test_declared_order_re_sorts_the_concatenation() -> None:
    plan = build_fan_out_plan(symbol_fan_out(order_by="symbol"), ["MSFT", "AAPL"])
    fetch = RecordingFetcher({"MSFT": [{"symbol": "MSFT"}], "AAPL": [{"symbol": "AAPL"}]})

    rows = execute_plan(plan, fetch)

    assert tuple(row["symbol"] for row in rows) == ("AAPL", "MSFT")


def test_order_by_refuses_missing_key_and_mixed_types() -> None:
    plan = build_fan_out_plan(symbol_fan_out(order_by="symbol"), ["AAPL"])
    with pytest.raises(MultiRequestError) as missing:
        execute_plan(plan, RecordingFetcher({"AAPL": [{"p": 1}]}))
    assert missing.value.code == "FANOUT_ORDER_KEY_MISSING"

    mixed = build_fan_out_plan(symbol_fan_out(order_by="symbol"), ["A", "B"])
    with pytest.raises(MultiRequestError) as kinds:
        execute_plan(mixed, RecordingFetcher({"A": [{"symbol": "A"}], "B": [{"symbol": 2}]}))
    assert kinds.value.code == "FANOUT_ORDER_KEY_TYPE"


def test_fan_out_result_is_deterministic_across_two_runs_of_the_same_plan() -> None:
    plan = build_fan_out_plan(symbol_fan_out(order_by="symbol"), ["MSFT", "AAPL", "NVDA"])
    script = {
        "MSFT": [{"symbol": "MSFT", "v": 1}],
        "AAPL": [{"symbol": "AAPL", "v": 2}, {"symbol": "AAPL", "v": 3}],
        "NVDA": [{"symbol": "NVDA", "v": 4}],
    }

    first = execute_plan(plan, RecordingFetcher(script))
    second = execute_plan(plan, RecordingFetcher(script))

    assert first == second
    assert tuple(row["symbol"] for row in first) == ("AAPL", "AAPL", "MSFT", "NVDA")


# ----------------------------------------------------------------------------- join: plan


def test_join_plan_one_step_per_distinct_left_key() -> None:
    left = ({"id": 7}, {"id": 9}, {"id": 7})
    plan = build_join_plan(JoinFetchSpec(source_key="id", request_param="fundamental_id"), left)

    assert plan.variants == ("7", "9")
    assert plan.steps[0].params == {"fundamental_id": "7"}


def test_join_plan_caps_requests() -> None:
    left = ({"id": 1}, {"id": 2}, {"id": 3})
    with pytest.raises(MultiRequestError) as refused:
        build_join_plan(JoinFetchSpec(source_key="id", max_requests=2), left)

    assert refused.value.code == "FANOUT_VARIANT_CAP"


def test_join_plan_refuses_left_row_missing_the_key() -> None:
    with pytest.raises(MultiRequestError) as refused:
        build_join_plan(JoinFetchSpec(source_key="id"), ({"id": 1}, {"name": "x"}))

    assert refused.value.code == "FANOUT_SOURCE_KEY_MISSING"


# ----------------------------------------------------------------------------- join: merge


def test_merge_inner_overlays_right_columns() -> None:
    left = ({"id": 1, "name": "a"}, {"id": 2, "name": "b"})
    right = ({"fundamental_id": 1, "roe": 0.2}, {"fundamental_id": 2, "roe": 0.3})

    merged = merge_rows(left, right, ratios_join())

    assert merged == (
        {"id": 1, "name": "a", "roe": 0.2},
        {"id": 2, "name": "b", "roe": 0.3},
    )
    # The merge copies: the caller's rows are never mutated.
    assert "roe" not in left[0]


def test_merge_inner_refuses_unmatched_right_row() -> None:
    left = ({"id": 1},)
    right = ({"fundamental_id": 1}, {"fundamental_id": 42})

    with pytest.raises(MultiRequestError) as refused:
        merge_rows(left, right, ratios_join())

    assert refused.value.code == "JOIN_INNER_UNMATCHED_RIGHT"


def test_merge_inner_refuses_unmatched_left_row() -> None:
    left = ({"id": 1}, {"id": 5})
    right = ({"fundamental_id": 1},)

    with pytest.raises(MultiRequestError) as refused:
        merge_rows(left, right, ratios_join())

    assert refused.value.code == "JOIN_INNER_UNMATCHED_LEFT"


def test_merge_left_join_preserves_unmatched_row_with_nulled_schema() -> None:
    # EtfPricePerformance's shape: a symbol whose analytics hop published nothing keeps its stats
    # row -- but only because the declaration said "left", not by silent default.
    join = JoinSpec(left_key="symbol", right_key="symbol", how="left", right_columns=("one_year",))
    left = ({"id": 1, "symbol": "AAPL"}, {"id": 2, "symbol": "GONE"})

    merged = merge_rows(left, ({"symbol": "AAPL", "one_year": 0.15},), join)

    assert merged == (
        {"id": 1, "symbol": "AAPL", "one_year": 0.15},
        {"id": 2, "symbol": "GONE", "one_year": None},
    )


def test_left_preserve_must_be_declared() -> None:
    with pytest.raises(TypeError):
        JoinSpec(left_key="id", right_key="id")  # how is required -- no silent default


def test_merge_refuses_duplicate_right_key_under_uniqueness() -> None:
    left = ({"id": 1},)
    right = ({"fundamental_id": 1, "v": 1}, {"fundamental_id": 1, "v": 2})

    with pytest.raises(MultiRequestError) as refused:
        merge_rows(left, right, ratios_join())

    assert refused.value.code == "JOIN_DUP_RIGHT_KEY"


def test_merge_non_unique_right_takes_first_match_deterministically() -> None:
    join = ratios_join(right_unique=False, right_columns=("v",))
    left = ({"id": 1},)
    right = ({"fundamental_id": 1, "v": 1}, {"fundamental_id": 1, "v": 2})

    merged = merge_rows(left, right, join)

    assert merged == ({"id": 1, "v": 1},)


def test_merge_refuses_str_vs_int_key_ambiguity() -> None:
    # Without the guard the inner join below would report one dropped row as a legitimate miss.
    join = JoinSpec(left_key="cik", right_key="cik", how="inner")
    with pytest.raises(MultiRequestError) as refused:
        merge_rows(({"cik": "5"},), ({"cik": 5},), join)

    assert refused.value.code == "JOIN_AMBIGUOUS_KEY_TYPE"


def test_merge_exact_int_keys_match() -> None:
    join = JoinSpec(left_key="cik", right_key="cik", how="inner")

    merged = merge_rows(({"cik": 5},), ({"cik": 5, "revenue": 9},), join)

    assert merged == ({"cik": 5, "revenue": 9},)


def test_merge_refuses_right_column_over_a_left_column() -> None:
    join = ratios_join(right_columns=("name",))
    with pytest.raises(MultiRequestError) as refused:
        merge_rows(({"id": 1, "name": "a"},), ({"fundamental_id": 1, "name": "b"},), join)

    assert refused.value.code == "JOIN_COLUMN_CONFLICT"


def test_merge_refuses_join_key_that_is_not_str_or_int() -> None:
    join = JoinSpec(left_key="cik", right_key="cik", how="left")

    with pytest.raises(MultiRequestError) as refused:
        merge_rows(({"cik": True},), (), join)

    assert refused.value.code == "JOIN_KEY_TYPE"


# ----------------------------------------------------------------------------- declarations


def test_malformed_declarations_refuse_at_construction() -> None:
    with pytest.raises(ValueError):
        FanOutSpec(source="param", field="symbols", max_variants=0)
    with pytest.raises(ValueError):
        JoinFetchSpec(source_key="   ")
    with pytest.raises(ValueError):
        JoinSpec(left_key="id", right_key="id", how="outer")
    with pytest.raises(ValueError):
        JoinSpec(left_key="id", right_key="id", how="inner", right_columns=("v", "v"))
    with pytest.raises(ValueError):
        FanOutSpec(source="teleport", field="symbols")


def test_plan_is_a_frozen_value_object() -> None:
    plan = build_fan_out_plan(symbol_fan_out(), ["AAPL"])
    with pytest.raises(FrozenInstanceError):
        plan.steps = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.steps[0].variant = "MSFT"  # type: ignore[misc]

    assert isinstance(plan, FanOutPlan)
