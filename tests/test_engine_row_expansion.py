"""Offline acceptance suite for the declarative row-shape expansion kernel.

Every arm of :mod:`opendata.data.providers._engine.row_expansion` is shown on both sides here:
the four public functions (``expand_nested_array``, ``pivot_series``, ``transpose_wide_table``,
``repoint_records``) on census-shaped golden inputs -- TMX ETF holdings, the Intrinio insider
transactions and CalendarIpo nesting, the Fed's Svensson wide body -- checked against the exact
rows that come out, and every ``RowExpansionError`` raise site in the module reached with one
refusal arm asserting on the reason code, pointer, record index and ``saw`` attribution the
kernel passes.

The discipline the kernel exists to enforce is shown as behaviour, not prose: the absent-versus-
empty split (a missing child array is ``MISSING_POINTER`` where an empty array contributes no
rows), the silent-zero refusal (``EMPTY_RESULT``), and the parallel-array integrity of the
series pivot. One arm is red on purpose: the kernel's own ``isinstance`` refusal for junk
non-``FieldMapping`` entries is unreachable for the realistic junk shapes, which escape as raw
``AttributeError`` instead (see the parametrisation note below).

No case touches I/O: documents are the decoded shapes the census rows record, handed to the
kernel directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from opendata.data.providers._engine.row_expansion import (
    BAD_SERIES_LABEL,
    BAD_TIMESTAMP,
    DUPLICATE_SERIES_LABEL,
    DUPLICATE_TIMESTAMP,
    EMPTY_RESULT,
    INVALID_PLAN,
    MISSING_POINTER,
    NOT_A_RECORD,
    NOT_A_RECORD_LIST,
    PARALLEL_ARRAY_MISMATCH,
    FieldMapping,
    NestedArrayExpansion,
    RecordRepointPlan,
    RowExpansionError,
    SeriesPivotPlan,
    WideTablePivotPlan,
    expand_nested_array,
    pivot_series,
    repoint_records,
    transpose_wide_table,
)


def _assert_refusal(
    excinfo: pytest.ExceptionInfo[RowExpansionError],
    reason: str,
    *,
    pointer: str | None = None,
    record_index: int | None = None,
    saw_contains: str = "",
) -> RowExpansionError:
    """Check the stable code and the attribution the kernel is required to attach."""
    error = excinfo.value
    assert error.reason == reason, str(error)
    if pointer is not None:
        assert error.pointer == pointer, str(error)
    if record_index is not None:
        assert error.record_index == record_index, str(error)
    if saw_contains:
        assert saw_contains in str(error), str(error)
    return error


@dataclass
class _DuckField:
    """A plan entry shaped like a field mapping (has ``column``) but not of its type."""

    column: str = "amount"


# -------------------------------------------------------------------------- error attribution


def test_row_expansion_error_str_reports_the_full_attribution() -> None:
    """The message names the code, pointer, record position and what was seen."""
    full = RowExpansionError(NOT_A_RECORD, pointer="a.b", record_index=2, saw="a str")
    assert str(full) == "ROW_EXPANSION_NOT_A_RECORD pointer='a.b' record=2 saw=a str"
    assert str(RowExpansionError(MISSING_POINTER)) == "ROW_EXPANSION_MISSING_POINTER"
    assert isinstance(RowExpansionError(BAD_TIMESTAMP), RuntimeError)


# ------------------------------------------------------------------- declaration refusals (plan)


def test_field_mapping_requires_column_and_pointer_text() -> None:
    """A mapping that cannot name a column or address a key is refused at construction."""
    with pytest.raises(RowExpansionError) as excinfo:
        FieldMapping("  ", "pointer")
    _assert_refusal(excinfo, INVALID_PLAN, pointer="field column", saw_contains="is blank")
    with pytest.raises(RowExpansionError) as excinfo:
        FieldMapping("amount", "   ")
    _assert_refusal(
        excinfo, INVALID_PLAN, pointer="field 'amount' pointer", saw_contains="is blank"
    )


def test_nested_expansion_requires_a_child_array_pointer() -> None:
    """An explosion with no array to explode is the declaration's failure, not the document's."""
    with pytest.raises(RowExpansionError) as excinfo:
        NestedArrayExpansion(child_array_pointer="", child_fields=(FieldMapping("w", "w"),))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="child array pointer")


def test_nested_expansion_requires_child_fields() -> None:
    """Rows of nothing but parent facts are refused: the child array is the row source."""
    with pytest.raises(RowExpansionError) as excinfo:
        NestedArrayExpansion(child_array_pointer="holdings", child_fields=())
    _assert_refusal(excinfo, INVALID_PLAN, pointer="child_fields", saw_contains="no child fields")


def test_nested_expansion_rejects_a_column_declared_twice() -> None:
    """One output column claimed by a child field and a parent field collides by declaration."""
    with pytest.raises(RowExpansionError) as excinfo:
        NestedArrayExpansion(
            child_array_pointer="holdings",
            child_fields=(FieldMapping("name", "a"),),
            parent_fields=(FieldMapping("name", "b"),),
        )
    _assert_refusal(
        excinfo, INVALID_PLAN, pointer="child_fields/parent_fields", saw_contains="declared twice"
    )


def test_duck_typed_field_entries_are_refused_by_both_record_plans() -> None:
    """The isinstance guard fires for an entry that has ``column`` but is not a FieldMapping."""
    with pytest.raises(RowExpansionError) as excinfo:
        NestedArrayExpansion(child_array_pointer="h", child_fields=(_DuckField(),))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="child_fields", saw_contains="_DuckField")
    with pytest.raises(RowExpansionError) as excinfo:
        RecordRepointPlan(fields=(_DuckField(),))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="fields", saw_contains="_DuckField")


@pytest.mark.parametrize(
    ("declare", "junk"),
    [
        (
            lambda bad: NestedArrayExpansion(child_array_pointer="holdings", child_fields=(bad,)),
            "not-a-mapping",
        ),
        (lambda bad: RecordRepointPlan(fields=(bad,)), 7),
    ],
    ids=["nested-child-field", "repoint-field"],
)
def test_junk_field_entries_must_refuse_as_invalid_plan(declare: object, junk: object) -> None:
    """KERNEL DEFECT, named not papered over: junk plan entries escape as AttributeError.

    ``NestedArrayExpansion.__post_init__`` (row_expansion.py:253) and
    ``RecordRepointPlan.__post_init__`` (:375) each raise ``RowExpansionError`` with
    ``saw=_type_name(mapping)`` for a non-``FieldMapping`` entry, so the contract says a string
    or int entry is an ``INVALID_PLAN`` refusal. But the duplicate-column prelude at :248/:370
    touches ``mapping.column`` first, so any entry lacking the attribute -- every realistic
    junk shape -- raises a raw ``AttributeError`` before the isinstance check, making it dead
    code for exactly the inputs it was written for. Expected: ``RowExpansionError`` with
    ``reason == INVALID_PLAN``; observed: ``AttributeError: 'str' object has no attribute
    'column'``. Do not weaken this arm to ``pytest.raises(AttributeError)``.
    """
    with pytest.raises(RowExpansionError) as excinfo:
        declare(junk)  # type: ignore[operator]
    _assert_refusal(excinfo, INVALID_PLAN)


def test_series_pivot_plan_requires_every_pointer_and_column_name() -> None:
    """A pivot whose series, arrays or timestamp column cannot be addressed is void."""
    for blank in ("label_pointer", "dates_pointer", "values_pointer", "timestamp_column"):
        with pytest.raises(RowExpansionError) as excinfo:
            SeriesPivotPlan(**{blank: "  "})
        _assert_refusal(excinfo, INVALID_PLAN, saw_contains="is blank")


def test_wide_table_plan_requires_a_nameable_timestamp_and_series() -> None:
    """Blank keys, no series, a duplicate series and the timestamp doubling as a series."""
    with pytest.raises(RowExpansionError) as excinfo:
        WideTablePivotPlan(timestamp_key="", series_keys=("a",))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="timestamp key")
    with pytest.raises(RowExpansionError) as excinfo:
        WideTablePivotPlan(timestamp_key="d", series_keys=())
    _assert_refusal(excinfo, INVALID_PLAN, pointer="series_keys", saw_contains="no series")
    with pytest.raises(RowExpansionError) as excinfo:
        WideTablePivotPlan(timestamp_key="d", series_keys=("a", " "))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="series key", saw_contains="is blank")
    with pytest.raises(RowExpansionError) as excinfo:
        WideTablePivotPlan(timestamp_key="d", series_keys=("a", "a"))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="series_keys", saw_contains="declared twice")
    with pytest.raises(RowExpansionError) as excinfo:
        WideTablePivotPlan(timestamp_key="d", series_keys=("d", "a"))
    _assert_refusal(
        excinfo, INVALID_PLAN, pointer="series_keys", saw_contains="also a series column"
    )


def test_wide_table_plan_requires_its_output_keys() -> None:
    """The series-object keys pivot_series reads back must all be nameable."""
    for blank in ("label_key", "dates_key", "values_key"):
        with pytest.raises(RowExpansionError) as excinfo:
            WideTablePivotPlan(timestamp_key="d", series_keys=("a",), **{blank: ""})
        _assert_refusal(excinfo, INVALID_PLAN, saw_contains="is blank")


def test_repoint_plan_requires_fields_with_unique_columns() -> None:
    """A projection with no columns or two columns on one name is void by declaration."""
    with pytest.raises(RowExpansionError) as excinfo:
        RecordRepointPlan(fields=())
    _assert_refusal(excinfo, INVALID_PLAN, pointer="fields", saw_contains="no columns")
    with pytest.raises(RowExpansionError) as excinfo:
        RecordRepointPlan(fields=(FieldMapping("name", "a"), FieldMapping("name", "b")))
    _assert_refusal(excinfo, INVALID_PLAN, pointer="fields", saw_contains="declared twice")


# ---------------------------------------------------------------------------- expand_nested_array


def _tmx_document() -> dict:
    """The TMX ETF shape: holdings sit inside each parent record, one parent publishes none."""
    return {
        "recs": [
            {
                "issuer": {"name": "XYZ ETF"},
                "holdings": [
                    {"weight": 1.5, "ticker": "AAA"},
                    {"weight": 2.5, "ticker": "BBB"},
                ],
            },
            {"issuer": {"name": "QRS ETF"}, "holdings": []},
        ]
    }


def _tmx_plan() -> NestedArrayExpansion:
    return NestedArrayExpansion(
        records_pointer="recs",
        child_array_pointer="holdings",
        child_fields=(FieldMapping("weight", "weight"), FieldMapping("ticker", "ticker")),
        parent_fields=(
            FieldMapping("fund", "issuer.name"),
            FieldMapping("note", "note", required=False),
        ),
    )


def test_expand_nested_array_golden_path_copies_parent_facts_onto_child_rows() -> None:
    """One row per child item, parent fields carried (nested pointer honoured), empty array
    contributes nothing rather than refusing."""
    assert expand_nested_array(_tmx_document(), _tmx_plan()) == [
        {"fund": "XYZ ETF", "note": None, "weight": 1.5, "ticker": "AAA"},
        {"fund": "XYZ ETF", "note": None, "weight": 2.5, "ticker": "BBB"},
    ]


def test_expand_nested_array_accepts_a_whole_document_record_list() -> None:
    """An empty records pointer means the document itself is the record list."""
    rows = expand_nested_array(
        [{"holdings": [{"ticker": "AAA"}]}],
        NestedArrayExpansion(
            child_array_pointer="holdings", child_fields=(FieldMapping("t", "ticker"),)
        ),
    )
    assert rows == [{"t": "AAA"}]


def test_expand_nested_array_refuses_a_records_pointer_that_is_not_a_list() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": {"a": 1}}, _tmx_plan())
    _assert_refusal(excinfo, NOT_A_RECORD_LIST, pointer="recs", saw_contains="a dict")
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"other": []}, _tmx_plan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="recs", saw_contains="no records path")


def test_expand_nested_array_refuses_a_parent_that_is_not_a_record() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": ["oops"]}, _tmx_plan())
    _assert_refusal(
        excinfo,
        NOT_A_RECORD,
        pointer="recs",
        record_index=0,
        saw_contains="parent record of type str",
    )


def test_expand_nested_array_distinguishes_a_missing_child_array_from_an_empty_one() -> None:
    """A record that carries no holdings key at all is a shape failure; [] is a published answer."""
    document = {"recs": [{"issuer": {"name": "XYZ ETF"}}]}
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array(document, _tmx_plan())
    _assert_refusal(
        excinfo,
        MISSING_POINTER,
        pointer="holdings",
        record_index=0,
        saw_contains="carries no nested array",
    )


def test_expand_nested_array_refuses_a_child_array_that_is_not_a_list() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": [{"holdings": "flat"}]}, _tmx_plan())
    _assert_refusal(
        excinfo,
        NOT_A_RECORD_LIST,
        pointer="holdings",
        record_index=0,
        saw_contains="a str where an array was declared",
    )


def test_expand_nested_array_refuses_a_child_that_is_not_a_record() -> None:
    """The offending child position is attributed inside the parent record's refusal."""
    document = {
        "recs": [{"issuer": {"name": "X"}, "holdings": [{"weight": 1.0, "ticker": "A"}, 7]}]
    }
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array(document, _tmx_plan())
    _assert_refusal(
        excinfo, NOT_A_RECORD, pointer="holdings", record_index=0, saw_contains="at position 1"
    )


def test_expand_nested_array_refuses_a_required_child_field_the_child_lacks() -> None:
    """_read_field's judgement: required absence is MISSING_POINTER, optional absence is null."""
    document = {"recs": [{"issuer": {"name": "X"}, "holdings": [{"weight": 1.0}]}]}
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array(document, _tmx_plan())
    _assert_refusal(
        excinfo,
        MISSING_POINTER,
        pointer="ticker",
        record_index=0,
        saw_contains="does not publish this path",
    )


def test_expand_nested_array_refuses_a_required_parent_field_the_parent_lacks() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": [{"holdings": [{"weight": 1.0, "ticker": "A"}]}]}, _tmx_plan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="issuer.name", record_index=0)


def test_expand_nested_array_allows_empty_records_but_refuses_empty_children() -> None:
    """An empty source record list is valid; nonempty parents with no child rows are not."""
    empty_records = NestedArrayExpansion(
        records_pointer="recs",
        child_array_pointer="holdings",
        child_fields=(FieldMapping("w", "w"),),
    )
    assert expand_nested_array({"recs": []}, empty_records) == []
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": [{"holdings": []}, {"holdings": []}]}, empty_records)
    _assert_refusal(excinfo, EMPTY_RESULT, pointer="holdings", saw_contains="2 record(s)")


def test_an_empty_exploded_dimension_drops_its_row_and_nothing_else() -> None:
    """PINNED SEMANTIC: an empty exploded dimension drops the record; only the total zero refuses.

    The three readings expansion could take -- drop, publish a null-filled row, or refuse per
    record -- are told apart on one document. Each arm fails if the semantic changes: publishing
    a null row grows ``rows`` with a ``w is None`` carrier for fund ``A``/``C``; a per-record
    refusal raises on a document that has published rows at all; reserving the drop for the
    all-empty list alone answers three rows or ``EMPTY_RESULT`` for this mixed one; and letting
    the all-empty case pass silently drops the arm below, which keeps ``EMPTY_RESULT`` exactly
    where the shipped typed-refusal contract puts the silent zero.
    """
    plan = NestedArrayExpansion(
        records_pointer="recs",
        child_array_pointer="holdings",
        child_fields=(FieldMapping("w", "w"),),
        parent_fields=(FieldMapping("fund", "fund", required=False),),
    )
    rows = expand_nested_array(
        {
            "recs": [
                {"fund": "A", "holdings": []},
                {"fund": "B", "holdings": [{"w": 1.0}]},
                {"fund": "C", "holdings": []},
            ]
        },
        plan,
    )
    assert rows == [{"fund": "B", "w": 1.0}]
    assert not any(row["w"] is None for row in rows), "no null-filled row stands in for []"
    assert {row["fund"] for row in rows} == {"B"}, "the empty parents published no row"
    with pytest.raises(RowExpansionError) as excinfo:
        expand_nested_array({"recs": [{"fund": "A", "holdings": []}]}, plan)
    _assert_refusal(excinfo, EMPTY_RESULT, pointer="holdings", saw_contains="1 record(s)")


# -------------------------------------------------------------------------------- pivot_series


def _svensson_series() -> list[dict]:
    """Two parallel-array series whose timestamps overlap but are neither equal nor sorted."""
    return [
        {"series": "SOFR", "dates": ["2024-01-02", "2024-01-01"], "values": [5.3, 5.2]},
        {"series": "IBIT", "dates": ["2024-01-01"], "values": [4.9]},
    ]


def test_pivot_series_golden_path_joins_series_on_the_timestamp() -> None:
    """One row per timestamp, ascending by published string; an absent series is absent."""
    rows = pivot_series(_svensson_series(), SeriesPivotPlan())
    assert rows == [
        {"date": "2024-01-01", "SOFR": 5.2, "IBIT": 4.9},
        {"date": "2024-01-02", "SOFR": 5.3},
    ]
    assert "IBIT" not in rows[1]


def test_pivot_series_reads_nested_pointers_declared_by_the_plan() -> None:
    """series/label/dates/values pointers are the engine grammar, digit segments included."""
    document = {"result": {"items": [{"name": "GDP", "t": ["2020-01-01"], "v": [1.0]}]}}
    plan = SeriesPivotPlan(
        series_pointer="result.items",
        label_pointer="name",
        dates_pointer="t",
        values_pointer="v",
        timestamp_column="observation_date",
    )
    assert pivot_series(document, plan) == [{"observation_date": "2020-01-01", "GDP": 1.0}]


def test_pivot_series_refuses_a_series_pointer_that_misses_or_is_not_a_list() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series({"nope": []}, SeriesPivotPlan(series_pointer="series"))
    _assert_refusal(excinfo, MISSING_POINTER, pointer="series", saw_contains="no series path")
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series({"series": {"a": 1}}, SeriesPivotPlan(series_pointer="series"))
    _assert_refusal(excinfo, NOT_A_RECORD_LIST, pointer="series", saw_contains="a dict")


def test_pivot_series_refuses_an_empty_series_list_as_the_silent_zero() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([], SeriesPivotPlan())
    _assert_refusal(excinfo, EMPTY_RESULT, pointer="", saw_contains="0 series")


def test_pivot_series_refuses_a_series_that_is_not_a_mapping() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series(["oops"], SeriesPivotPlan())
    _assert_refusal(
        excinfo, NOT_A_RECORD, pointer="", record_index=0, saw_contains="a series of type str"
    )


def test_pivot_series_refuses_a_series_without_its_label_path() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"dates": [], "values": []}], SeriesPivotPlan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="series", record_index=0)


def test_pivot_series_refuses_labels_that_cannot_name_a_column() -> None:
    """A non-string or blank label cannot become an output column name."""
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": 123, "dates": [], "values": []}], SeriesPivotPlan())
    _assert_refusal(
        excinfo, BAD_SERIES_LABEL, pointer="series", record_index=0, saw_contains="label of 123"
    )
    with pytest.raises(RowExpansionError):
        pivot_series([{"series": "  ", "dates": [], "values": []}], SeriesPivotPlan())


def test_pivot_series_refuses_two_series_claiming_one_column() -> None:
    """Duplicate labels would overwrite one series' values in the joined row: refuse."""
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series(
            [
                {"series": "A", "dates": ["2024-01-01"], "values": [1.0]},
                {"series": "A", "dates": ["2024-01-02"], "values": [2.0]},
            ],
            SeriesPivotPlan(),
        )
    _assert_refusal(
        excinfo,
        DUPLICATE_SERIES_LABEL,
        pointer="series",
        record_index=1,
        saw_contains="already-taken column",
    )


def test_pivot_series_refuses_a_series_named_after_the_timestamp_column() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "date", "dates": ["d"], "values": [1]}], SeriesPivotPlan())
    _assert_refusal(excinfo, DUPLICATE_SERIES_LABEL, record_index=0)


def test_pivot_series_refuses_a_missing_dates_or_values_path() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A"}], SeriesPivotPlan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="dates", record_index=0)
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A", "dates": []}], SeriesPivotPlan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="values", record_index=0)


def test_pivot_series_refuses_array_paths_that_are_not_arrays_as_the_mismatch() -> None:
    """A string where a parallel array was declared is unrecoverable pairing, code E512 arm."""
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A", "dates": "x", "values": [1]}], SeriesPivotPlan())
    _assert_refusal(
        excinfo,
        PARALLEL_ARRAY_MISMATCH,
        pointer="dates",
        record_index=0,
        saw_contains="a str where a parallel array was declared",
    )
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A", "dates": ["d"], "values": {"v": 1}}], SeriesPivotPlan())
    _assert_refusal(excinfo, PARALLEL_ARRAY_MISMATCH, pointer="values")


def test_pivot_series_refuses_dates_and_values_of_different_lengths() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series(
            [{"series": "A", "dates": ["2024-01-01", "2024-01-02"], "values": [1]}],
            SeriesPivotPlan(),
        )
    _assert_refusal(
        excinfo,
        PARALLEL_ARRAY_MISMATCH,
        pointer="dates",
        record_index=0,
        saw_contains="2 date(s) against 1 value(s)",
    )


def test_pivot_series_refuses_timestamps_that_are_not_non_blank_strings() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A", "dates": [20240101], "values": [1]}], SeriesPivotPlan())
    _assert_refusal(
        excinfo, BAD_TIMESTAMP, pointer="dates", record_index=0, saw_contains="at position 0"
    )
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series(
            [{"series": "A", "dates": ["2024-01-01", " "], "values": [1, 2]}], SeriesPivotPlan()
        )
    _assert_refusal(excinfo, BAD_TIMESTAMP, pointer="dates", saw_contains="at position 1")


def test_pivot_series_refuses_a_timestamp_repeated_within_one_series() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series(
            [{"series": "A", "dates": ["2024-01-01", "2024-01-01"], "values": [1, 2]}],
            SeriesPivotPlan(),
        )
    _assert_refusal(
        excinfo,
        DUPLICATE_TIMESTAMP,
        pointer="dates",
        record_index=0,
        saw_contains="repeats within one series",
    )


def test_pivot_series_refuses_series_that_all_publish_no_timestamps() -> None:
    """Well-formed but empty parallel arrays are the silent zero for the pivot too."""
    with pytest.raises(RowExpansionError) as excinfo:
        pivot_series([{"series": "A", "dates": [], "values": []}], SeriesPivotPlan())
    _assert_refusal(excinfo, EMPTY_RESULT, saw_contains="published no timestamps")


# --------------------------------------------------------------------------- transpose_wide_table


def _svensson_wide_rows() -> dict:
    """The Fed body shape: one header-keyed record per date, one null cell in series b."""
    return {
        "rows": [
            {"ts": "2024-01-01", "a": 1.0, "b": None},
            {"ts": "2024-01-02", "a": 2.0, "b": 3.0},
        ]
    }


def _wide_plan() -> WideTablePivotPlan:
    return WideTablePivotPlan(records_pointer="rows", timestamp_key="ts", series_keys=("a", "b"))


def test_transpose_wide_table_golden_path_builds_parallel_array_series() -> None:
    """One {series, dates, values} object per declared column; a null cell stays a null."""
    assert transpose_wide_table(_svensson_wide_rows(), _wide_plan()) == [
        {"series": "a", "dates": ["2024-01-01", "2024-01-02"], "values": [1.0, 2.0]},
        {"series": "b", "dates": ["2024-01-01", "2024-01-02"], "values": [None, 3.0]},
    ]


def test_transpose_then_pivot_reproduces_the_svensson_shaped_answer() -> None:
    """The census's two-step composition: the transpose output is default-plan pivot input."""
    series = transpose_wide_table(_svensson_wide_rows(), _wide_plan())
    assert pivot_series(series, SeriesPivotPlan()) == [
        {"date": "2024-01-01", "a": 1.0, "b": None},
        {"date": "2024-01-02", "a": 2.0, "b": 3.0},
    ]


def test_transpose_refuses_records_pointer_failures_and_the_silent_zero() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"nope": []}, _wide_plan())
    _assert_refusal(excinfo, MISSING_POINTER, pointer="rows", saw_contains="no records path")
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": 5}, _wide_plan())
    _assert_refusal(excinfo, NOT_A_RECORD_LIST, pointer="rows")
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": []}, _wide_plan())
    _assert_refusal(excinfo, EMPTY_RESULT, pointer="rows", saw_contains="0 wide rows")


def test_transpose_refuses_a_wide_row_that_is_not_a_mapping() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": [7]}, _wide_plan())
    _assert_refusal(
        excinfo, NOT_A_RECORD, pointer="rows", record_index=0, saw_contains="a wide row of type int"
    )


def test_transpose_refuses_a_missing_or_nulled_timestamp_key() -> None:
    """Both an absent ts header and a published null fail as MISSING_POINTER (record.get arm)."""
    for record in ({"a": 1.0, "b": 2.0}, {"ts": None, "a": 1.0, "b": 2.0}):
        with pytest.raises(RowExpansionError) as excinfo:
            transpose_wide_table({"rows": [record]}, _wide_plan())
        _assert_refusal(excinfo, MISSING_POINTER, pointer="ts", record_index=0)


def test_transpose_refuses_a_timestamp_that_is_not_a_non_blank_string() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": [{"ts": 20240101, "a": 1.0, "b": 2.0}]}, _wide_plan())
    _assert_refusal(excinfo, BAD_TIMESTAMP, pointer="ts", saw_contains="a timestamp 20240101")
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": [{"ts": "   ", "a": 1.0, "b": 2.0}]}, _wide_plan())
    _assert_refusal(excinfo, BAD_TIMESTAMP, pointer="ts")


def test_transpose_refuses_one_timestamp_published_by_two_wide_rows() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table(
            {
                "rows": [
                    {"ts": "2024-01-01", "a": 1.0, "b": 2.0},
                    {"ts": "2024-01-01", "a": 3.0, "b": 4.0},
                ]
            },
            _wide_plan(),
        )
    _assert_refusal(
        excinfo,
        DUPLICATE_TIMESTAMP,
        pointer="ts",
        record_index=1,
        saw_contains="repeats across wide rows",
    )


def test_transpose_refuses_a_record_lacking_a_declared_series_header() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": [{"ts": "2024-01-01", "a": 1.0}]}, _wide_plan())
    _assert_refusal(
        excinfo, MISSING_POINTER, pointer="b", record_index=0, saw_contains="no such header column"
    )


def test_transpose_refuses_a_series_cell_that_is_a_container() -> None:
    """A list or dict cell cannot join a value array as a scalar; the refusal is BAD_TIMESTAMP."""
    with pytest.raises(RowExpansionError) as excinfo:
        transpose_wide_table({"rows": [{"ts": "d", "a": [1], "b": 2.0}]}, _wide_plan())
    _assert_refusal(
        excinfo,
        BAD_TIMESTAMP,
        pointer="a",
        record_index=0,
        saw_contains="a list cell where a scalar series value was declared",
    )
    with pytest.raises(RowExpansionError):
        transpose_wide_table({"rows": [{"ts": "d", "a": {"n": 1}, "b": 2.0}]}, _wide_plan())


# ------------------------------------------------------------------------------ repoint_records


def test_repoint_records_golden_path_flattens_nested_objects() -> None:
    """The CalendarIpo shape: columns re-resolved per record, nested objects and list indices."""
    document = {
        "items": [
            {
                "company": {"name": "ACME"},
                "security": {"symbol": "ACME.U"},
                "holders": [{"id": 9}],
            }
        ]
    }
    plan = RecordRepointPlan(
        records_pointer="items",
        fields=(
            FieldMapping("name", "company.name"),
            FieldMapping("symbol", "security.symbol"),
            FieldMapping("holder", "holders.0.id"),
            FieldMapping("absent", "nope", required=False),
        ),
    )
    assert repoint_records(document, plan) == [
        {"name": "ACME", "symbol": "ACME.U", "holder": 9, "absent": None}
    ]


def test_repoint_refuses_records_pointer_failures_but_allows_empty_records() -> None:
    plan = RecordRepointPlan(fields=(FieldMapping("c", "c"),))
    with pytest.raises(RowExpansionError) as excinfo:
        repoint_records(
            {"nope": []}, RecordRepointPlan(records_pointer="items", fields=plan.fields)
        )
    _assert_refusal(excinfo, MISSING_POINTER, pointer="items", saw_contains="no records path")
    with pytest.raises(RowExpansionError) as excinfo:
        repoint_records(
            {"items": "flat"}, RecordRepointPlan(records_pointer="items", fields=plan.fields)
        )
    _assert_refusal(excinfo, NOT_A_RECORD_LIST, pointer="items", saw_contains="a str")
    assert repoint_records([], plan) == []


def test_repoint_refuses_a_record_that_is_not_a_mapping() -> None:
    with pytest.raises(RowExpansionError) as excinfo:
        repoint_records([7], RecordRepointPlan(fields=(FieldMapping("c", "c"),)))
    _assert_refusal(
        excinfo, NOT_A_RECORD, pointer="", record_index=0, saw_contains="a record of type int"
    )


def test_repoint_refuses_a_required_field_behind_a_scalar_or_missing_path() -> None:
    """A pointer stepping into a scalar is MISSING, same as an absent key: refusal or null."""
    document = {"company": {"name": "ACME"}}
    plan = RecordRepointPlan(
        fields=(
            FieldMapping("deep", "company.name.suffix"),
            FieldMapping("gone", "missing.field", required=False),
        )
    )
    with pytest.raises(RowExpansionError) as excinfo:
        repoint_records([document], plan)
    _assert_refusal(
        excinfo,
        MISSING_POINTER,
        pointer="company.name.suffix",
        record_index=0,
        saw_contains="does not publish this path",
    )
    optional_only = RecordRepointPlan(
        fields=(FieldMapping("gone", "missing.field", required=False),)
    )
    assert repoint_records([document], optional_only) == [{"gone": None}]


def test_repoint_preserves_non_scalar_values_as_published() -> None:
    """The kernel projects, it does not type: a pointer onto an object carries it through."""
    record = {"company": {"name": "ACME"}}
    rows = repoint_records(
        [record], RecordRepointPlan(fields=(FieldMapping("company", "company"),))
    )
    assert rows == [{"company": {"name": "ACME"}}]
