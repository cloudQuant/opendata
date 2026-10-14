"""Offline fixtures for engine-driven provider models.

Nothing here authorizes live access. A grant built by :func:`fixture_context`
is a local test credential for a *synthetic* transport, mirroring
``tests/provider_budget_fixtures.py``: it names one exact source/model/operation,
is finite, expires, and states its evidence as a test fixture. Deriving it from
the declaration rather than a hand-maintained table is what lets a new provider
model be tested without editing shared code.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from opendata.data.protocol import FetchContext
from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import HttpResponse
from opendata.data.providers._engine.row_expansion import (
    NestedArrayExpansion,
    RecordRepointPlan,
    SeriesPivotPlan,
    WideTablePivotPlan,
)
from opendata.data.providers._engine.spec import ROW_FILTER_VALUE_SEPARATOR
from opendata.data.providers._engine.value_transform import CastSpec, apply_derive
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opendata.data.providers._engine.row_expansion import FieldMapping
    from opendata.data.providers._engine.spec import ColumnSpec, DerivePlan, ModelSpec

RIGHTS_EVIDENCE = "test-fixture:offline-only:fake-transport"

#: A value per declared kind, offset by ``index`` so a multi-record page is not N copies of one row.
_SAMPLE_VALUES: dict[str, tuple[object, ...]] = {
    "str": ("alpha", "beta", "gamma"),
    "int": (1, 2, 3),
    "float": (0.5, 1.5, 2.5),
    "bool": (True, False, True),
    "date": ("2026-01-01", "2026-01-02", "2026-01-03"),
    "enum": ("listed", "listed", "listed"),
    "str_list": (["one"], ["two", "three"], ["four"]),
}


def sample_value(kind: str, index: int = 0) -> object:
    """Return a raw JSON value of ``kind`` that the engine's row model will accept.

    Dates are published as ISO strings because that is what a JSON endpoint sends and what an uncast
    ``date`` column reads: :func:`opendata.data.providers._engine.http_json.normalize_record` hands
    the cell to the row model, which parses the ISO text into a ``date``. A column whose declaration
    runs a ``cast`` stage reads a different wire shape altogether -- an epoch integer, a date in the
    source's own format, a bool spelled the source's way -- and its cell comes from
    :func:`cast_input_sample` rather than from this table, because the declaration says what the
    body has to publish.
    """
    options = _SAMPLE_VALUES.get(kind)
    if options is None:
        raise AssertionError(f"no sample value for declared kind {kind!r}")
    return options[index % len(options)]


#: The date the pre-pivot timestamp column counts up from. One date per record position, so a page
#: of any length publishes a strictly ascending, duplicate-free timestamp -- which is what a wide
#: release looks like and what :func:`transpose_wide_table` refuses a repeat of.
PIVOT_TIMESTAMP_EPOCH = date(2026, 1, 1)


def pivot_timestamp(index: int) -> str:
    """Return the ISO timestamp a wide-table record at position ``index`` publishes."""
    return (PIVOT_TIMESTAMP_EPOCH + timedelta(days=index)).isoformat()


def descending_timestamp(index: int) -> str:
    """Return the ISO date a record at position ``index`` publishes for a descending sort column.

    The same epoch :func:`pivot_timestamp` counts up from, run the other way: strictly descending
    and duplicate-free, which is what makes the page a ``RowOrderSpec`` with ``direction="desc"``
    hands back the page this fixture scripted. See :func:`ordered_cell`.
    """
    return (PIVOT_TIMESTAMP_EPOCH - timedelta(days=index)).isoformat()


def wide_table_expansion(spec: ModelSpec) -> tuple[WideTablePivotPlan, SeriesPivotPlan] | None:
    """Return the transpose-and-join pair ``spec`` opens its expansion with, or None if it has none.

    A declaration that opens with a :class:`WideTablePivotPlan` publishes a *pre-pivot* record list
    -- one row per date, one column per series -- and the columns its row model carries are what the
    following :class:`SeriesPivotPlan` joins out of that list. The fixtures therefore have two
    vocabularies to choose between, and choosing is a reading of the declaration rather than a fact
    to remember per model, so it comes from the plans.

    Raises:
        AssertionError: The declaration opens with a wide table the fixture cannot model -- no join
            stage after it, a join reading pointers the transpose does not publish, a series header
            no declared column reads, a declared column the wide table cannot publish, or no column
            for the join's timestamp. Each of these would otherwise be answered with a body that
            quietly reads a column as a null, which is the silent zero this kernel exists to refuse.
    """
    if not spec.row_expansion or not isinstance(spec.row_expansion[0], WideTablePivotPlan):
        return None
    stage = spec.row_expansion[0]
    if len(spec.row_expansion) < 2 or not isinstance(spec.row_expansion[1], SeriesPivotPlan):
        raise AssertionError(
            f"{spec.model}: opens its expansion with a wide table but names no SeriesPivotPlan to "
            f"join it into rows, so no fixture can say which body publishes which column"
        )
    join = spec.row_expansion[1]
    if (join.label_pointer, join.dates_pointer, join.values_pointer) != (
        stage.label_key,
        stage.dates_key,
        stage.values_key,
    ):
        raise AssertionError(
            f"{spec.model}: its join reads "
            f"{(join.label_pointer, join.dates_pointer, join.values_pointer)} "
            f"but its transpose publishes {(stage.label_key, stage.dates_key, stage.values_key)}"
        )
    declared_names = {column.name for column in spec.columns}
    if join.timestamp_column not in declared_names:
        raise AssertionError(
            f"{spec.model}: its join writes "
            f"{join.timestamp_column!r}, which no declared column reads"
        )
    published_keys = declared_names | {column.source_key or column.name for column in spec.columns}
    unread = [key for key in stage.series_keys if key not in published_keys]
    if unread:
        raise AssertionError(
            f"{spec.model}: its wide table publishes {unread}, which no declared column names"
        )
    stamped = {plan.column for plan in spec.column_derives if plan.source_kind != "record"}
    unreachable = [
        column.name
        for column in spec.columns
        if column.name not in (join.timestamp_column, *stamped, *spec.row_envelope)
        and (column.source_key or column.name) not in stage.series_keys
    ]
    if unreachable:
        raise AssertionError(
            f"{spec.model}: declares {unreachable} that its wide table cannot publish"
        )
    return stage, join


def repoint_expansion(spec: ModelSpec) -> RecordRepointPlan | None:
    """Return the per-record repoint plan ``spec`` expands with, or None if it declares none.

    A :class:`RecordRepointPlan` reaches a record list that is already flat and re-resolves one
    dotted pointer *inside each record*, so a column published out of a nested ``company`` or
    ``security`` object arrives from ``company.id`` rather than from the flat declared name
    ``company_id``. The body this fixture scripts and every helper that answers "which key publishes
    this column" therefore have to speak the plan's pointer vocabulary; reading it off the plan is
    what keeps a falsifier edit and the oracle that judges it from disagreeing about where in the
    record a repointed column lives.

    Raises:
        AssertionError: The declaration names a repoint alongside another expansion kind, whose
            combined record vocabulary this fixture cannot decide, or the plan and the declared
            columns disagree about which column the record publishes. Each would otherwise be
            answered with a body that quietly reads a column as a null, which is the silent zero
            this kernel exists to refuse.
    """
    plans = [stage for stage in spec.row_expansion if isinstance(stage, RecordRepointPlan)]
    if not plans:
        return None
    if len(plans) != len(spec.row_expansion):
        raise AssertionError(
            f"{spec.model}: expands through "
            f"{[type(stage).__name__ for stage in spec.row_expansion]}, so one record vocabulary "
            f"cannot be read off a composition of expansion kinds"
        )
    plan = plans[0]
    published = {mapping.column for mapping in plan.fields}
    read_by = {column.source_key or column.name for column in spec.columns}
    unread = sorted(published - read_by)
    if unread:
        raise AssertionError(
            f"{spec.model}: its repoint publishes {unread}, which no declared column reads"
        )
    stamped = {param.column for param in spec.column_derives if param.source_kind != "record"}
    unreachable = sorted(
        column.name
        for column in spec.columns
        if (column.source_key or column.name) not in published
        and column.name not in (*stamped, *spec.row_envelope)
    )
    if unreachable:
        raise AssertionError(
            f"{spec.model}: declares {unreachable} that its repoint plan cannot publish"
        )
    return plan


def repoint_field(spec: ModelSpec, column: ColumnSpec) -> FieldMapping | None:
    """Return the mapping of ``spec``'s repoint plan that publishes ``column``, or None.

    The plan keys the row it builds by ``column.source_key or column.name``, which is the same key
    :func:`opendata.data.providers._engine.http_json.normalize_record` reads back, so a column whose
    declaration names a ``source_key`` of its own is matched on that key rather than on its name.
    """
    plan = repoint_expansion(spec)
    if plan is None:
        return None
    published = column.source_key or column.name
    return next((mapping for mapping in plan.fields if mapping.column == published), None)


def child_prefix(plan: NestedArrayExpansion) -> str:
    """The one spelling this fixture uses for a cell inside an exploded child item.

    A :class:`NestedArrayExpansion` reads its child fields out of each item of an array the record
    publishes, so the cell a row carries is not a field of the record a falsifier is handed. The
    fixture therefore names it by its two steps -- the array, then the pointer inside the item --
    and every function that reads or damages a key resolves that spelling before it walks.
    """
    return f"{plan.child_array_pointer}."


def nested_array_expansion(spec: ModelSpec) -> NestedArrayExpansion | None:
    """Return the explode ``spec`` expands with, or None if it declares none.

    A :class:`NestedArrayExpansion` splits its record vocabulary in two: ``parent_fields`` are read
    once from the record :func:`published_records` hands back, and ``child_fields`` from each item
    of the array ``child_array_pointer`` names, with the two sets' columns merged onto every child
    row. The scripts and the falsifiers here have to follow that split rather than remember it per
    model, so both halves are read off the plan.

    Raises:
        AssertionError: The plan sits alongside another expansion kind, whose combined record
            vocabulary this fixture cannot decide; the parent and child sets publish the same key,
            so no spelling says which of the two a row carries; a plan field is read by no declared
            column; a declared column is published by neither the plan, a stamp, nor the envelope;
            or a column is both exploded and re-sourced by a record-sourced derive, which would
            leave the fixture guessing which cell the body carries. Each would otherwise be
            answered with a body that quietly reads a column as a null -- the silent zero this
            kernel refuses.
    """
    plans = [stage for stage in spec.row_expansion if isinstance(stage, NestedArrayExpansion)]
    if not plans:
        return None
    if len(plans) != len(spec.row_expansion):
        raise AssertionError(
            f"{spec.model}: expands through "
            f"{[type(stage).__name__ for stage in spec.row_expansion]}, so one record vocabulary "
            f"cannot be read off a composition of expansion kinds"
        )
    plan = plans[0]
    parents = {mapping.column for mapping in plan.parent_fields}
    children = {mapping.column for mapping in plan.child_fields}
    shared = sorted(parents & children)
    if shared:
        raise AssertionError(
            f"{spec.model}: {shared} is published by both the filing record and its child item, so "
            f"no key says which half of the explode a row carries it from"
        )
    read_by = {column.source_key or column.name for column in spec.columns}
    unread = sorted((parents | children) - read_by)
    if unread:
        raise AssertionError(
            f"{spec.model}: its explode publishes {unread}, which no declared column reads"
        )
    stamped = {param.column for param in spec.column_derives if param.source_kind != "record"}
    unreachable = sorted(
        column.name
        for column in spec.columns
        if (column.source_key or column.name) not in (parents | children)
        and column.name not in (*stamped, *spec.row_envelope)
    )
    if unreachable:
        raise AssertionError(
            f"{spec.model}: declares {unreachable} that neither its record nor its child item "
            f"publishes"
        )
    return plan


def nested_child_field(spec: ModelSpec, column: ColumnSpec) -> FieldMapping | None:
    """Return the mapping that publishes ``column`` from inside one exploded child item, or None."""
    plan = nested_array_expansion(spec)
    if plan is None:
        return None
    published = column.source_key or column.name
    return next((mapping for mapping in plan.child_fields if mapping.column == published), None)


def nested_parent_field(spec: ModelSpec, column: ColumnSpec) -> FieldMapping | None:
    """Return the mapping that copies ``column`` down from the record being exploded, or None."""
    plan = nested_array_expansion(spec)
    if plan is None:
        return None
    published = column.source_key or column.name
    return next((mapping for mapping in plan.parent_fields if mapping.column == published), None)


def expansion_refuses_before_the_normalizer(spec: ModelSpec, column: ColumnSpec) -> bool:
    """True when a declared expansion kernel reads ``column``'s key, so it refuses a missing one.

    Two layers can answer "this body does not publish that column", and they disagree about both
    the code and the name they give: the kernels run ahead of :func:`normalize_record` and refuse
    with ``ROW_EXPANSION_MISSING_POINTER`` on the *pointer*, while the normalizer refuses with the
    model's own ``*_SHAPE_INVALID`` on the *column*. Which one a column belongs to is a property of
    its declaration -- a wide table's every header, or one mapping the plan reads as ``required``
    -- and reading it here is what keeps the expected code and the expected name from being chosen
    by two different rules that can drift apart.
    """
    if wide_table_expansion(spec) is not None:
        return True
    field = nested_child_field(spec, column) or nested_parent_field(spec, column)
    if field is None:
        field = repoint_field(spec, column)
    return field is not None and field.required


def explode_child_cell(
    spec: ModelSpec, record: dict[str, object], key: str
) -> tuple[dict[str, object], str] | None:
    """Return the first child item and its in-item pointer when ``key`` addresses one, else None.

    ``key`` arrives in the spelling :func:`child_prefix` gives: the array the plan explodes, then
    the pointer the plan reads inside an item. The item is the object the kernel actually reads, so
    this is where a falsifier's edit has to land for the row to move.

    Raises:
        AssertionError: The key addresses the exploded array but the record publishes no object at
            that item, so the edit would damage nothing the kernel reads -- the refusal an edit to a
            byte no read path visits deserves rather than a silent pass.
    """
    plan = nested_array_expansion(spec)
    if plan is None:
        return None
    prefix = child_prefix(plan)
    if not key.startswith(prefix):
        return None
    array = record.get(plan.child_array_pointer)
    if not isinstance(array, list) or not array or not isinstance(array[0], dict):
        raise AssertionError(
            f"{spec.model}: its record publishes no {plan.child_array_pointer!r} item, so editing "
            f"{key!r} damages nothing the explode reads"
        )
    return array[0], key[len(prefix) :]


def stamped_plan(spec: ModelSpec, column: ColumnSpec) -> DerivePlan | None:
    """Return the plan that produces ``column`` from outside the record, or None.

    ``source_kind="record"`` is a copy with stages, so the record still publishes the key; only a
    parameter or a constant makes a column one the body cannot be edited to lack.
    """
    for plan in spec.column_derives:
        if plan.column == column.name and plan.source_kind != "record":
            return plan
    return None


def staged_record_plan(spec: ModelSpec, column: ColumnSpec) -> DerivePlan | None:
    """Return the plan that copies ``column``'s record value and stages something over it, or None.

    The two halves of ``column_derives`` ask different things of a generic fixture. A stamped
    column has no record key to script and :func:`expected_row_value` refuses to restate its
    cast; a record-sourced plan is a copy WITH stages -- the record still publishes the key, the
    kernel runs on the published value, and a fixture that scripted the key but read the row as a
    verbatim copy would describe a body whose value the declared stages never saw. Every
    record-sourced plan stages something (:class:`DerivePlan` refuses one that does not), so this
    is the record half of the seam.
    """
    for plan in spec.column_derives:
        if plan.column == column.name and plan.source_kind == "record" and plan.stages:
            return plan
    return None


#: The instant a declared cast's wire sample counts up from, one day per record position, in UTC.
#: It is the same calendar day :data:`_SAMPLE_VALUES` spells for a ``date`` column: a cast changes
#: the shape the body publishes, not the day the row carries, so the epoch sample and the ISO sample
#: have to agree on which day they are describing.
CAST_SAMPLE_MOMENT = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: The cast kinds whose input is an epoch count, spelled here rather than imported from the kernel's
#: private set so the fixture keeps its own reading of the declaration's vocabulary.
_EPOCH_CASTS = ("epoch_ms_to_date", "epoch_ms_to_datetime", "epoch_s_to_date")


def declared_cast(plan: DerivePlan) -> CastSpec | None:
    """Return the cast one record-sourced plan runs, or None when it stages no cast.

    ``STAGE_SEQUENCE`` admits at most one cast and puts it ahead of the rescale, so a plan carries
    no more than one input vocabulary for the fixture to read.
    """
    for stage in plan.stages:
        if stage.kind == "cast" and stage.cast is not None:
            return stage.cast
    return None


def cast_input_sample(cast: CastSpec, kind: str, index: int) -> object:
    """Return the raw cell ``cast`` reads, for the sample of ``kind`` at position ``index``.

    A :class:`~opendata.data.providers._engine.value_transform.CastSpec` names its own input shape,
    and it is not the shape of the value the column publishes: the epoch kinds read an integer
    count (milliseconds or seconds, per the kind), ``date_string_to_iso`` reads text in the format
    the declaration names, ``bool_from_spellings`` reads one of the two spelling sets it declares,
    and the numeric kinds refuse a binary float and so take an integer or exact decimal text. The
    generic body therefore has to spell the cast's *input* while the row keeps the value the cast
    produces -- the same day, said the way the source says it. Reading it off the declaration is
    what keeps a new epoch provider testable without editing shared code, exactly as
    :func:`wide_table_expansion` reads a pivot's vocabulary off its plans.

    Raises:
        AssertionError: The column's declared kind has no sample this cast's input can be spelled
            from -- a bool cast on a non-boolean column, an epoch cast on anything but a ``date``, a
            numeric cast on a kind that is not a quantity -- so the fixture would be scripting a
            value the declaration was never written to read.
    """
    moment = CAST_SAMPLE_MOMENT + timedelta(days=index)
    if cast.kind in _EPOCH_CASTS:
        if kind != "date":
            raise AssertionError(
                f"cast {cast.kind} on a {kind!r} column: the epoch kinds produce a date, so no "
                f"published count reaches a {kind!r} row field"
            )
        seconds = int(moment.timestamp())
        return seconds * 1000 if cast.kind.startswith("epoch_ms") else seconds
    if cast.kind == "date_string_to_iso":
        if kind not in ("date", "str"):
            raise AssertionError(
                f"cast {cast.kind} on a {kind!r} column: a published date string is text, so only "
                f"a date or a text column can read it"
            )
        return moment.strftime(cast.input_format)
    if cast.kind == "bool_from_spellings":
        if kind != "bool":
            raise AssertionError(
                f"cast {cast.kind} on a {kind!r} column: a bool cast produces True or False, so no "
                f"spelling reaches a {kind!r} row field"
            )
        spellings = cast.true_spellings if index % 2 == 0 else cast.false_spellings
        return spellings[0]
    if kind not in ("int", "float"):
        raise AssertionError(
            f"cast {cast.kind} on a {kind!r} column: the numeric casts read a quantity, so a "
            f"{kind!r} column has no published number to rescale"
        )
    # The kernel refuses a binary float, so a float sample goes as exact decimal text.
    value = sample_value(kind, index)
    return str(value) if isinstance(value, float) else value


def staged_sample(column: ColumnSpec, index: int, plan: DerivePlan) -> object:
    """The raw cell the generic body publishes for one staged record column.

    Two halves of a plan ask two different things of the cell, and both are read off the plan
    rather than remembered per model. A ``cast`` stage names its own input shape, so the cell comes
    from :func:`cast_input_sample` -- an epoch integer for ``epoch_s_to_date``, never the ISO text a
    plain ``date`` column publishes. A scale-only plan reads a quantity, and the kernel refuses a
    binary float -- "publish it as a string or declare a cast first" -- so a numeric staged column
    spells the kind's sample value as text rather than handing back the float the sample table
    carries; every other kind keeps its published sample, which is already a string its stages read.
    """
    cast = declared_cast(plan)
    if cast is not None:
        return cast_input_sample(cast, column.kind, index)
    value = sample_value(column.kind, index)
    if column.kind in ("float", "int") and isinstance(value, float):
        return str(value)
    return value


def raw_key(spec: ModelSpec, column: ColumnSpec) -> str | None:
    """Return the record key that publishes ``column``, or None when there is no such key.

    Five declaration shapes make a column and its body key different things, and a falsifier that
    edits the wrong one scripts a body the source could not have sent: a stamped column has no
    record key at all, a wide table's timestamp column is published under the pivot's own header
    (the Fed's ``Date``) rather than under the declared name (``date``), a repointed column is
    published at the pointer its plan walks -- a dotted path, not a flat name -- a column an explode
    reads out of a child item is published at ``<array>.<pointer>``, one step inside the array the
    plan iterates, and an ordinary column may name a ``source_key`` of its own. A record-sourced
    plan may also read a key the model publishes no column for, and :func:`normalize_record`
    reads it before any stage, so the same override is honored here.
    """
    if stamped_plan(spec, column) is not None:
        return None
    explode = nested_array_expansion(spec)
    if explode is not None:
        child = nested_child_field(spec, column)
        if child is not None:
            if staged_record_plan(spec, column) is not None:
                raise AssertionError(
                    f"{spec.model}: {column.name} is both exploded out of "
                    f"{explode.child_array_pointer!r} and re-sourced by a record derive, so this "
                    f"fixture cannot say which cell the body carries"
                )
            return f"{child_prefix(explode)}{child.pointer}"
        parent = nested_parent_field(spec, column)
        if parent is not None:
            return parent.pointer
    field = repoint_field(spec, column)
    if field is not None:
        return field.pointer
    key = column.source_key or column.name
    plan = staged_record_plan(spec, column)
    if plan is not None and plan.source.strip():
        key = plan.source
    expansion = wide_table_expansion(spec)
    if expansion is None:
        return key
    stage, join = expansion
    if column.name == join.timestamp_column:
        return stage.timestamp_key
    return key if key in stage.series_keys else None


def absent_key_reads_as_null(spec: ModelSpec, column: ColumnSpec) -> str | None:
    """Return the record key whose absence the engine reads as a null for ``column``, or None.

    Two readings have to be reconciled to answer that, and a case that guesses wrong scripts a body
    no source could send: the row normalizer turns an absent optional key into a null, while the
    expansion kernels run ahead of it and refuse a record missing a header they were told to read --
    so every key of a wide table, the timestamp included, is published-or-refused rather than
    absent-and-null. A repoint plan says the same thing per field: a mapping the kernel reads as
    required is published-or-refused, and only an optional one turns an absent pointer into a null.
    A stamped column has no key to leave out at all.
    """
    key = raw_key(spec, column)
    if key is None:
        return None
    if nested_array_expansion(spec) is not None:
        field = nested_child_field(spec, column) or nested_parent_field(spec, column)
        return None if field is not None and field.required else key
    field = repoint_field(spec, column)
    if field is not None:
        return None if field.required else field.pointer
    expansion = wide_table_expansion(spec)
    if expansion is not None and key in {expansion[0].timestamp_key, *expansion[0].series_keys}:
        return None
    return key


def selection_vocabulary(spec: ModelSpec, index: int = 0) -> dict[str, object]:
    """The cells one generic record publishes for the keys its ``row_filters`` test.

    :func:`opendata.data.providers._engine.http_json.apply_row_filters` judges a selection on the
    decoded record and says so itself: a filter can test a key the model does not publish. econdb's
    roster is exactly that shape -- ``iso`` selects the obsolete OECD block out of the published
    file and becomes no output column of its own -- and the same function reads a record that does
    not publish a tested key as a non-match, so a record list carrying no ``iso`` makes *every* row
    fail a ``not_equals`` and the declaration answers with nothing: the empty answer every positive
    face of the contract suite then mistakes for the model. These keys therefore join the scripted
    record and, through :func:`record_vocabulary`, the rendered header.

    A key spelled as a declared column's name or ``source_key`` is not one of them: a filter may
    test the output name where the record publishes the renamed key (cboe's search declares both
    spellings precisely because no live body has settled which one the directory uses), and
    inventing a second cell for one column would script a record the source does not send.

    Each cell comes from its own predicate, because a literal needle is the declaration's own
    constant and a body written against anything else is a body the declaration discards: the needle
    for the ops that read it (``equals``, ``contains``, ``prefix``), the first alternative of an
    ``in`` set, and for ``not_equals`` the string sample at ``index`` that differs from it.

    Raises:
        AssertionError: The declaration expands its rows, so the keys a filter reads are the ones
            its stages write and no record this module scripts can carry them; or the needle comes
            from a parameter, so whether a row survives is the caller's question rather than the
            declaration's; or every string sample is a needle the ``not_equals`` refuses, so no cell
            can say "different from this". Each would be answered with a body whose rows the
            selection discards -- the silent zero this kernel exists to refuse.
    """
    cells: dict[str, object] = {}
    if not spec.row_filters:
        return cells
    carried = {column.name for column in spec.columns}
    carried |= {column.source_key or column.name for column in spec.columns}
    carried |= {key for key in (raw_key(spec, column) for column in spec.columns) if key}
    for row_filter in spec.row_filters:
        for key in row_filter.columns:
            if key in carried or key in cells:
                continue
            if spec.row_expansion:
                raise AssertionError(
                    f"{spec.model}: its row filter tests {key!r}, which no column publishes, while "
                    f"its row_expansion writes the very records the filter is judged on, so no "
                    f"record this fixture scripts can carry the key"
                )
            if row_filter.value_param:
                raise AssertionError(
                    f"{spec.model}: its row filter tests the undeclared key {key!r} against "
                    f"parameter {row_filter.value_param!r}, so which rows survive is the caller's "
                    f"needle and not a cell this fixture can script"
                )
            needle = row_filter.value
            if row_filter.op == "in":
                cells[key] = needle.split(ROW_FILTER_VALUE_SEPARATOR)[0]
            elif row_filter.op == "not_equals":
                folded = needle.casefold() if row_filter.ignore_case else needle
                options = [
                    str(option)
                    for option in _SAMPLE_VALUES["str"]
                    if (str(option).casefold() if row_filter.ignore_case else str(option)) != folded
                ]
                if not options:
                    raise AssertionError(
                        f"{spec.model}: every string sample of {key!r} is the needle "
                        f"{needle!r} its not_equals refuses, so this fixture cannot publish a cell "
                        f"the selection keeps"
                    )
                cells[key] = options[index % len(options)]
            else:
                cells[key] = needle
    return cells


def ordered_cell(spec: ModelSpec, column: ColumnSpec, index: int) -> object | None:
    """The cell the generic body publishes for the column ``spec``'s ``order_by`` reads, or None.

    :func:`expected_row_value` answers for the row at position ``index``, while the ordering stage
    puts the records wherever its declared direction says they belong. A body whose cells count
    *against* that direction therefore comes back reordered, and every column of the row at position
    ``index`` then carries the sample of some other position -- a disagreement no reading of the
    declaration can untangle, because the sample table is exactly what the case was told to expect.
    Ascending is what the samples already do (each kind counts up with ``index``, the way
    :func:`pivot_timestamp` counts up for a wide table), so an ascending declaration is answered
    with None and its published sample stands; a descending one gets its sort column from
    :func:`descending_timestamp`, which makes the page the ordering stage hands back the page this
    module scripted. Ties and unrankable cells keep published order, as ``apply_row_order``
    documents, so nothing else moves.

    Raises:
        AssertionError: The declaration sorts descending on a column this fixture has no descending
            cell for -- a kind other than ``date``, or one a derive stage computes, whose wire shape
            is the cast's input rather than the row's value. Scripting either against the declared
            direction reverses the page, and publishing equal cells for every row would leave the
            ordering stage nothing to judge; both belong named rather than absorbed.
    """
    order = spec.order_by
    if order is None or order.direction != "desc":
        return None
    carried = {column.name, column.source_key or column.name, raw_key(spec, column)}
    if order.column not in carried:
        return None
    staged = staged_record_plan(spec, column)
    if column.kind != "date" or staged is not None:
        raise AssertionError(
            f"{spec.model}: order_by sorts {order.column!r} descending, and this fixture has no "
            f"descending cell for a {column.kind!r} column"
            f"{' its derive plan computes' if staged is not None else ''}, so the page it scripts "
            f"would come back ordered against the declaration"
        )
    return descending_timestamp(index)


def record_vocabulary(spec: ModelSpec) -> list[str]:
    """The keys one raw record of ``spec``'s source publishes, in header order.

    For a declaration that repoints, these are the pointers its plan re-resolves per record, so a
    dotted entry names a path through a nested object rather than one flat key. The keys its row
    filters test beyond its columns join the list, because the header a source prints is every key
    its records carry, not only the ones a declaration turns into output columns.
    """
    cells = selection_vocabulary(spec)
    expansion = wide_table_expansion(spec)
    if expansion is not None:
        return [expansion[0].timestamp_key, *expansion[0].series_keys, *cells]
    keys: list[str] = []
    for column in spec.columns:
        key = raw_key(spec, column)
        if key is not None:
            keys.append(key)
    keys.extend(cells)
    return keys


def expected_row_value(
    spec: ModelSpec, column: ColumnSpec, index: int = 0, query: Mapping[str, object] | None = None
) -> object:
    """The value the row at position ``index`` must carry for ``column``, given this module's body.

    The synthesis publishes the raw cell a JSON or text body would carry; the engine's declared kind
    is what turns a ``date`` column into a ``date``, a stamped column's plan is what turns the query
    parameter into the column, a record-sourced plan is what turns its cast's wire input into the
    row's value, and a wide table's pivot header is what the timestamp column reads. Reading both
    sides here is what keeps a fixture edit and the case that judges it from disagreeing about what
    "normalize the declared columns" promised. A column its plan repoints is answered with the same
    sample, because a repoint moves where in the record the value is published, not what value
    reaches the row, and the column an ``order_by`` sorts is answered with the cell
    :func:`ordered_cell` scripts for it, because the ordering stage decides which record reaches
    which position.

    Raises:
        AssertionError: The stamp declares stages -- re-running the cast here would be the
            kernel's own case, not this fixture's claim -- or the plan names a parameter the
            caller did not pass.
    """
    plan = stamped_plan(spec, column)
    if plan is not None:
        if plan.stages:
            raise AssertionError(
                f"{spec.model}: {column.name} is stamped "
                f"through {len(plan.stages)} stage(s), which "
                f"test_engine_value_transform.py judges; this fixture cannot restate that cast"
            )
        if query is None or plan.source not in query:
            raise AssertionError(
                f"{spec.model}: {column.name} is stamped from parameter {plan.source}, which the "
                f"case did not pass"
            )
        return query[plan.source]
    staged = staged_record_plan(spec, column)
    if staged is not None:
        # Asked for its verdict only: a descending ``order_by`` over a column its plan computes has
        # no cell this fixture can count down, because the body carries the cast's input rather than
        # the row's value, and a page that comes back ordered against the declaration makes every
        # other column's per-index expectation a lie about a different record.
        ordered_cell(spec, column, index)
        # The body publishes the staged column's raw cell (:func:`staged_sample` spells it the way
        # the kernel reads it), and the row carries what the plan computes from it. Restating the
        # composition here is what lets the generic fixture judge a staged declaration at all --
        # the kernel's own rules are measured by test_engine_value_transform.py, and this only
        # answers "which value must the row carry". A float row field holds exactly
        # ``float(produced)`` after pydantic, so the equality compares the same coercion.
        produced = apply_derive(staged, staged_sample(column, index, staged))
        if column.kind != "float":
            return produced
        # apply_derive's documented product set is date / Decimal / float / bool / the source
        # itself, so a Decimal reaching a float column is the kernel working as declared, not a
        # mismatch to refuse. Anything else (a date, a list) would raise TypeError inside float().
        if not isinstance(produced, (str, bytes, int, float, Decimal)):
            raise AssertionError(
                f"{spec.model}: {column.name} is declared float but the staged plan produced "
                f"{type(produced).__name__}"
            )
        return float(produced)
    expansion = wide_table_expansion(spec)
    published: object = sample_value(column.kind, index)
    if expansion is not None and raw_key(spec, column) == expansion[0].timestamp_key:
        published = pivot_timestamp(index)
    else:
        ordered = ordered_cell(spec, column, index)
        if ordered is not None:
            published = ordered
    if column.kind != "date":
        return published
    # Both producers hand back the ISO string a body would carry; str() only says so to mypy.
    return date.fromisoformat(str(published))


def _publish(record: dict[str, object], pointer: str, value: object) -> None:
    """Script ``value`` at ``pointer`` inside ``record``, opening the objects the path names.

    :func:`opendata.data.providers._engine.row_expansion.repoint_records` re-resolves the pointer
    with the engine's own dotted grammar, where every non-index segment is a mapping key, so
    ``company.id`` reads a record holding a ``company`` object. A flat key spelled ``"company.id"``
    would be read as absent, which is precisely the accidental null this fixture must not script.

    Raises:
        AssertionError: The pointer walks a path through a key the record already publishes as
            something other than an object, or indexes into a list -- a shape no record of
            per-column samples carries, so the disagreement is named rather than papered over
            with a value the kernel would never reach.
    """
    parts = pointer.split(".")
    cursor: dict[str, object] = record
    for part in parts[:-1]:
        if part.isdigit():
            raise AssertionError(
                f"the pointer {pointer!r} indexes a list, which a record of per-column samples "
                f"does not carry"
            )
        if part not in cursor:
            cursor[part] = {}
        nested = cursor[part]
        if not isinstance(nested, dict):
            raise AssertionError(
                f"the pointer {pointer!r} walks through {part!r}, which the record publishes as a "
                f"value rather than an object"
            )
        cursor = nested
    cursor[parts[-1]] = value


def damage_record_field(
    spec: ModelSpec,
    record: dict[str, object],
    key: str,
    *,
    value: object = None,
    drop: bool = False,
) -> None:
    """Edit one live record at the one place the engine reads ``key`` from.

    ``key`` is the spelling :func:`raw_key` gives back, and where it lands depends on the
    declaration: a spec that repoints re-resolves the key as a dotted pointer *inside* the record,
    so the damage has to open that path exactly as :func:`_publish` did when the clean body was
    scripted, while a spec with no repoint plan reads one flat cell -- where a dot is part of the
    name rather than a step (the Fed publishes ``M1_N.M`` as one header), and splitting it would
    script a nested object no decoder ever emits. Editing the wrong place is how a falsifier damages
    a byte the fetch never reads, so the refusal it then reports belongs to the fixture and not to
    the engine.

    Args:
        spec: The declaration whose expansion vocabulary the edit follows.
        record: One record of the published list -- the object the fetch itself reads, as returned
            by :func:`published_records`.
        key: The key or pointer that names the column being falsified.
        value: What to publish at ``key`` when ``drop`` is False.
        drop: Remove the field ``key`` names instead of publishing a value, which is the body a
            source sends when it does not carry that column.

    Raises:
        AssertionError: ``drop`` names a field the record does not publish, so the edit would damage
            nothing and the case would be judging an unchanged body. A pointer that indexes a list
            or steps through a scalar is refused by :func:`_publish` for the same reason.
    """
    walks = repoint_expansion(spec) is not None or nested_array_expansion(spec) is not None
    child = explode_child_cell(spec, record, key)
    if child is not None:
        record, key = child
    if not drop:
        if walks:
            _publish(record, key, value)
        else:
            record[key] = value
        return
    steps = key.split(".") if walks else [key]
    parent: dict[str, object] = record
    for step in steps[:-1]:
        if step.isdigit():
            raise AssertionError(
                f"{spec.model}: the pointer {key!r} indexes a list, which no record of per-column "
                f"samples carries to cut"
            )
        nested = parent.get(step)
        if not isinstance(nested, dict):
            raise AssertionError(
                f"{spec.model}: the pointer {key!r} walks through {step!r}, which the record does "
                f"not publish as an object, so the field it names is already absent"
            )
        parent = nested
    leaf = steps[-1]
    if leaf not in parent:
        raise AssertionError(
            f"{spec.model}: its record publishes no {key!r}, so dropping it damages nothing"
        )
    del parent[leaf]


def synthetic_record(spec: ModelSpec, index: int = 0) -> dict[str, object]:
    """Build one raw record in the vocabulary ``spec``'s body publishes, at position ``index``.

    Each column reaches the record under the key the engine reads it from (:func:`raw_key`), so
    the facets that do not publish their declared columns verbatim are scripted the way their
    source looks: a wide-table declaration gets the table its own decoder produces -- a strictly
    ascending timestamp header plus one cell per series column -- a declaration that repoints gets
    the nested object each of its plan's pointers walks through, a column a ``column_derives`` plan
    casts or rescales gets the raw cell that plan reads (:func:`staged_sample`), the column an
    ``order_by`` sorts gets the cell :func:`ordered_cell` counts in its declared direction, and a
    column a ``column_derives`` plan stamps from a parameter or a constant is left out, because the
    record list does not carry it. The keys the declaration's ``row_filters`` test beyond its
    columns join the record too (:func:`selection_vocabulary`), since a source that selects on a key
    prints that key.
    """
    expansion = wide_table_expansion(spec)
    repoint = repoint_expansion(spec)
    explode = nested_array_expansion(spec)
    record: dict[str, object] = {}
    children: dict[str, object] = {}
    prefix = child_prefix(explode) if explode is not None else ""
    for column in spec.columns:
        key = raw_key(spec, column)
        if key is None:
            continue
        ordered = ordered_cell(spec, column, index)
        if expansion is not None and key == expansion[0].timestamp_key:
            record[key] = pivot_timestamp(index)
            continue
        staged = staged_record_plan(spec, column)
        if staged is not None:
            # A staged record column is published the way its kernel reads it -- the raw shape its
            # cast declares, or decimal text for a numeric sample value -- so the declared stages
            # run on the cell the body carries.
            value = staged_sample(column, index, staged)
        else:
            value = sample_value(column.kind, index) if ordered is None else ordered
        if explode is not None and key.startswith(prefix):
            children[key[len(prefix) :]] = value
            continue
        if repoint is None and explode is None:
            record[key] = value
            continue
        _publish(record, key, value)
    record.update(selection_vocabulary(spec, index))
    if explode is not None:
        record[explode.child_array_pointer] = [children]
    return record


def synthetic_page(
    spec: ModelSpec, count: int = 1, *, start: int = 0, **envelope: object
) -> HttpResponse:
    """Wrap ``count`` synthetic records in the document shape ``spec`` points at.

    ``rows_pointer`` may be empty (the document *is* the list) or dotted, so the body is assembled
    by walking the pointer rather than hard-coding an envelope. ``start`` is the index of the first
    record on this page: a multi-page answer scripted without it publishes the same values on every
    page, which is precisely the repeated-page body a paging judgement exists to be able to tell
    apart from a result that advanced. ``envelope`` keys land at the document root because that is
    where the engine reads them — both ``total_key`` and ``cursor_field`` are resolved against the
    whole response, not against the record list's own nesting level — so a fixture that parked them
    beside the records would publish a total no judgement could see.
    """
    pointer = spec.rows_pointer
    records = [synthetic_record(spec, start + index) for index in range(count)]
    if not pointer:
        if envelope:
            raise AssertionError(
                f"{spec.model}: a document that is itself the record list cannot carry an envelope"
            )
        return HttpResponse(200, records)
    document: dict[str, object] = dict(envelope)
    parts = pointer.split(".")
    cursor: dict[str, object] = document
    for part in parts[:-1]:
        nested: dict[str, object] = {}
        cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = records
    return HttpResponse(200, document)


def published_records(document: object, spec: ModelSpec) -> list[dict[str, object]]:
    """The record list ``spec``'s fetch reads out of ``document``, before its stages rebuild it.

    :func:`opendata.data.providers._engine.http_json.resolve_rows` walks ``rows_pointer`` and hands
    what it found to the declaration's expansion stages, and every stage projects each record into a
    fresh dict: for the repoint kernel the row it returns is
    ``{mapping.column: _read_field(record, ...)}``, a copy. Measured on ``intrinio::CalendarIpo``,
    ``resolved[0] is published[0]`` was False, writing a key onto the returned row left the value
    the next fetch read at ``0.5``, and writing it onto the document's own record moved that same
    value. So a falsifier that edits what :func:`resolve_rows` returns damages a byte no read path
    revisits, and every refusal (and every non-refusal) it then reports is an accident of the
    fixture. This performs the same walk with the same engine function on a stage-free copy of the
    declaration, which stops precisely where the source's rows are; the objects it returns are the
    ones the stages are handed.

    Raises:
        AssertionError: The walk reached something other than a list of mappings, so this page holds
            no record for a falsifier to damage and a case that expected a refusal from editing one
            would be judging the fixture rather than the body. An empty list is not an error here --
            it is what the empty-answer cases script.
    """
    unexpanded = replace(spec, row_expansion=())
    records = http_json.resolve_rows(document, spec.rows_pointer, unexpanded)
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise AssertionError(
                f"{spec.model}: the record at position {position} is a "
                f"{type(record).__name__}, so there is no field here to damage"
            )
    return records


def _cell_text(value: object) -> str:
    """Render one synthetic sample the way a delimited body publishes it."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, list):
        return ";".join(str(item) for item in value)
    return "" if value is None else str(value)


def _preamble_rows(count: int) -> list[list[str]]:
    """Render ``count`` metadata rows that hold none of the declared column names.

    Structurally faithful rather than verbatim: a real preamble is a source's own text (the Fed's
    five ``Series Description``/``Unit:`` lines), and what the decoder depends on is only that the
    rows are non-blank and are not the header. A blank row would be dropped by the same
    blank-filter the decoder applies before it counts, which would silently shift the skip.
    """
    return [[f"preamble row {index} of {count}", "metadata"] for index in range(1, count + 1)]


def body_from_records(
    spec: ModelSpec,
    records: Sequence[Mapping[str, object]],
    *,
    keys: Sequence[str] | None = None,
    member: str | None = None,
    preamble_rows: int | None = None,
) -> bytes:
    """Render arbitrary ``records`` as the raw body ``spec``'s decoder declares.

    falsifier can script a body whose header or cells differ from the clean synthesis. ``keys`` is
    the header row and defaults to :func:`record_vocabulary`, the keys this declaration's own
    records publish (its expansion reads a wide table, so those are the pivot's headers, not its
    column names); passing a shortened list is how a case publishes a table the source genuinely
    omitted a column from -- the only form a text body has for "this key is not here", since a cell
    left blank decodes back as a present key holding a null.

    A declaration that skips a preamble gets one, because a body without it is a different body than
    the endpoint publishes: ``preamble_rows`` defaults to the count the declaration names, and a
    falsifier that passes some other count is how an off-by-one skip is shown to be a refusal.

    SYNTHETIC like :func:`synthetic_body`: every byte is generated from the declaration, never
    recorded from an upstream response.

    Raises:
        AssertionError: The declaration reads a JSON document, so there is no delimited body to
            render -- the case asked the wrong fixture for its bytes -- or its repoint plan reads a
            nested pointer, which a text header cannot name: a dotted header cell decodes back as
            one flat key, never as the object the plan walks through.
    """
    decoder = spec.decoder
    if not decoder.delimited:
        raise AssertionError(f"{spec.model}: declares a json decoder, so no delimited body exists")
    header = record_vocabulary(spec) if keys is None else list(keys)
    if repoint_expansion(spec) is not None or nested_array_expansion(spec) is not None:
        nested = [key for key in header if "." in key]
        if nested:
            raise AssertionError(
                f"{spec.model}: its expansion reads {nested}, which a {decoder.kind} header cannot "
                f"carry: the cell decodes back as one flat key, not as a nested object or an array "
                f"item"
            )
    table = (
        _preamble_rows(decoder.preamble_rows if preamble_rows is None else preamble_rows)
        + [header]
        + [[_cell_text(record.get(key)) for key in header] for record in records]
    )
    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter=decoder.effective_delimiter, lineterminator="\n").writerows(table)
    text = buffer.getvalue()
    if decoder.kind != "zip_csv":
        return text.encode("utf-8")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(member or decoder.member or "records.csv", text)
    return archive.getvalue()


def synthetic_body(
    spec: ModelSpec, count: int = 1, *, start: int = 0, member: str | None = None
) -> bytes:
    """Render ``count`` records as the raw body ``spec``'s decoder declares.

    SYNTHETIC: every byte is generated from the declaration's own columns and sample values by this
    fixture. It is not a recorded upstream capture and must never be presented as one -- a real
    OECD, FINRA or Fama-French body belongs to its own model's verification task.

    The header row is the declared source keys in declared order, because those names are the only
    keys the row normalizer reads. A ``zip_csv`` declaration is served a real in-memory archive
    holding one member named by ``member`` (or by its own selector), so the selector itself is
    what a case exercises.

    Raises:
        AssertionError: The declaration reads a JSON document, so there is no delimited body to
            render -- the case asked the wrong fixture for its bytes.
    """
    return body_from_records(
        spec,
        [synthetic_record(spec, start + index) for index in range(count)],
        member=member,
    )


def synthetic_raw_page(
    spec: ModelSpec,
    count: int = 1,
    *,
    start: int = 0,
    member: str | None = None,
    status: int = 200,
) -> HttpResponse:
    """Return one response whose body is the synthetic raw bytes ``spec``'s decoder reads.

    ``document`` stays empty on purpose: a delimited declaration never reads it, and populating it
    would let a decoder that ignored the payload pass on a JSON fixture's rows.
    """
    body = synthetic_body(spec, count, start=start, member=member)
    return HttpResponse(status, None, payload=body)


def valid_query_kwargs(spec: ModelSpec) -> dict[str, object]:
    """Return one accepted query for ``spec``: every required parameter, by declared kind."""
    kwargs: dict[str, object] = {}
    for parameter in spec.params:
        if not parameter.required and parameter.default is None:
            continue
        value: object = (
            parameter.enum[0]
            if parameter.kind == "enum"
            else {
                "str": "sampler",
                "int": 1,
                "float": 1.5,
                "bool": True,
                "date": date(2026, 1, 1),
                "str_list": ["one"],
            }[parameter.kind]
        )
        kwargs[parameter.name] = value
    return kwargs


def fixture_context(source: str, spec: ModelSpec, *, sends: int = 3) -> FetchContext:
    """Return one finite, single-identity grant context for a declared model.

    Args:
        source: Provider routing id.
        spec: The model declaration being exercised.
        sends: Bound on sends. The bounded-request profile is used because a
            declared ``max_pages`` can exceed the live-smoke per-source ceiling
            of three; the bound still has to be stated, so a fetcher that loops
            past its own declaration fails instead of hanging.

    Returns:
        A :class:`FetchContext` carrying a budget that allows exactly this
        model's QUERY operation against the declaration's own host.
    """
    host = urlsplit(spec.base_url).netloc
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=source,
        canonical_model=spec.model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=RIGHTS_EVIDENCE,
        task_attempts=sends,
        source_attempts=sends,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=sends,
        source_attempts=sends,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


class SyntheticTransport:
    """A replaceable transport that records every request it is asked to serve.

    Responses are keyed by an exact ``(url, params)`` pair, so a test states
    precisely which request must produce which body; an unkeyed request raises
    instead of quietly returning someone else's fixture.
    """

    def __init__(
        self, responses: dict[tuple[str, frozenset[tuple[str, str]]], HttpResponse]
    ) -> None:
        """Store the keyed responses and start with an empty call log."""
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    def record(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Append one request to the call log.

        Every transport shares this recorder, so a new request option cannot be recorded by one
        class and dropped by the other two -- which is what makes the recorded request the thing
        a case asserts on rather than a copy of what the case hoped to send.
        """
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
                "source": source,
                "headers": dict(headers or {}),
            }
        )

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Return the response keyed to this exact request, or fail loudly."""
        key = (url, frozenset(params.items()))
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        try:
            return self._responses[key]
        except KeyError:
            raise AssertionError(
                f"no synthetic response is keyed for {url}?{sorted(params.items())}"
            ) from None


class FixedResponseTransport(SyntheticTransport):
    """A transport answering every request with one canned response.

    Used by rejection cases where the body is irrelevant, e.g. an upstream 429
    or a malformed document; keyed requests stay available for shape cases.
    """

    def __init__(self, response: HttpResponse) -> None:
        """Start from an empty response table and keep the single response."""
        super().__init__({})
        self._response = response

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Record the request and return the canned response."""
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        return self._response


class SequencedResponseTransport(SyntheticTransport):
    """A transport answering the n-th request with the n-th prepared page.

    Paging cases need a different body per send, and they need to be falsifiable about how many
    sends happened: asking for a page beyond the script raises instead of repeating the last
    response, which is exactly the failure mode a pager that cannot tell "end of result" from
    "same page again" would otherwise pass. Requests stay recorded, so a case can also assert the
    paging keys each send carried.
    """

    def __init__(self, *pages: HttpResponse) -> None:
        """Start from an empty response table and keep the page script."""
        super().__init__({})
        self._pages = list(pages)

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Serve the next scripted page.

        Raises:
            AssertionError: The engine asked for more pages than the script prepared.
        """
        self.record(url, params, timeout=timeout, source=source, headers=headers)
        if not self._pages:
            raise AssertionError(
                f"the paging script ran out after {len(self.calls) - 1} page(s); "
                f"request {sorted(params.items())} had no prepared body"
            )
        return self._pages.pop(0)
