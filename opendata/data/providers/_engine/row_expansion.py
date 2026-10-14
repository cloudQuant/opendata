"""Row expansion: declarations for documents whose rows are not one flat record list.

The engine's read face resolves one pointer into one document and normalizes each reached record
(:func:`opendata.data.providers._engine.http_json.resolve_rows`). A capability census over the
OpenBB provider map found a family of models that shape cannot express: a TMX ETF document holds
its answer inside a *nested array* of the selected record, an Intrinio filing carries a second
record list per filing whose filing-level fields must be copied onto every transaction row, an
Intrinio IPO item nests ``company`` and ``security`` objects whose scalars are the published
columns, and the Fed's Svensson release is a wide table that has to be read per series and pivoted
so one row is one timestamp with one column per series.

This module is the pure kernel for that capability. Every function takes plain decoded values --
the parsed document and a frozen plan -- and returns plain row dicts; none performs I/O, imports
a ``ModelSpec`` or knows the HTTP transport. A declaration that later reaches
:class:`~opendata.data.providers._engine.spec.ModelSpec` will reference these plans, and the
integration owns where the second document comes from: the kernel re-resolves pointers *inside*
the document it is given and never fetches.

The failure discipline is the census's whole complaint inverted: a shape the plan was not given
raises :class:`RowExpansionError` with the pointer and record position that made it a shape
failure, and a row set that came out empty after non-empty input refuses rather than publishing
the silent zero :func:`~opendata.data.providers._engine.decoders` was written to refuse. An empty
source record list is a published empty answer. Within a non-empty parent list, an empty nested
array contributes no rows and a missing array key is refused; if all non-empty parents yield no
rows, the expansion still refuses.

Nothing here performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A path the plan says is there and the document does not carry. Distinct from the path carrying
#: an empty list, which is a published answer of no rows, the same absent-versus-empty split AC2-10
#: asks every model to keep.
MISSING_POINTER = "MISSING_POINTER"

#: The pointer resolved but did not land on a list of records.
NOT_A_RECORD_LIST = "NOT_A_RECORD_LIST"

#: A value the plan reads as a record is not a mapping.
NOT_A_RECORD = "NOT_A_RECORD"

#: A plan field is blank, duplicated or of a type no declaration could mean.
INVALID_PLAN = "INVALID_PLAN"

#: One series published a different number of dates and values; the pairing is unrecoverable.
PARALLEL_ARRAY_MISMATCH = "PARALLEL_ARRAY_MISMATCH"

#: Two series claim the same output column, or a series is named after the timestamp itself.
DUPLICATE_SERIES_LABEL = "DUPLICATE_SERIES_LABEL"

#: One series repeats a timestamp; two values for one cell would silently pick one of them.
DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP"

#: A timestamp, a wide-table cell or a series label that is not the scalar the plan declared.
BAD_TIMESTAMP = "BAD_TIMESTAMP"

#: A series published a label that is not a non-blank string, so its column cannot be named.
BAD_SERIES_LABEL = "BAD_SERIES_LABEL"

#: Every input record was well-formed and no row came out -- the silent zero this kernel exists
#: to refuse.
EMPTY_RESULT = "EMPTY_RESULT"

#: The output keys :func:`transpose_wide_table` writes each series object under, and the defaults
#: :class:`SeriesPivotPlan` reads them back through.
SERIES_LABEL_KEY = "series"
SERIES_DATES_KEY = "dates"
SERIES_VALUES_KEY = "values"


class RowExpansionError(RuntimeError):
    """A document that is not the shape the expansion plan declared.

    Attributes:
        reason: Stable refusal code, one of the module constants.
        pointer: The plan pointer that made the refusal, when a pointer caused it.
        record_index: Position of the offending record in the resolved list, when one caused it.
        saw: What the kernel actually found there, for the review record.
    """

    def __init__(
        self,
        reason: str,
        *,
        pointer: str = "",
        record_index: int | None = None,
        saw: str = "",
    ) -> None:
        """Store the attribution a silently empty row set would have hidden."""
        self.reason = reason
        self.pointer = pointer
        self.record_index = record_index
        self.saw = saw
        super().__init__(reason)

    def __str__(self) -> str:
        """Report the reason, the pointer, the record position and what was seen."""
        parts = [f"ROW_EXPANSION_{self.reason}"]
        if self.pointer:
            parts.append(f"pointer={self.pointer!r}")
        if self.record_index is not None:
            parts.append(f"record={self.record_index}")
        if self.saw:
            parts.append(f"saw={self.saw}")
        return " ".join(parts)


class _Missing:
    """Sentinel type for a path the document does not carry."""


#: Returned by :func:`_follow` when a pointer's path is absent, as distinct from present-and-null.
MISSING = _Missing()


def _type_name(value: object) -> str:
    """Name the type the kernel actually found, for the refusal's ``saw`` text."""
    return type(value).__name__


def _follow(container: object, pointer: str) -> object:
    """Walk a dotted pointer through mappings and list indices; return ``MISSING`` if absent.

    The grammar is the engine's ``rows_pointer`` grammar: dot-separated segments, a digit segment
    indexing a list, and every other segment a mapping key. A path that steps into a scalar, names
    a key a mapping lacks, or indexes past the end of a list is ``MISSING`` -- not a refusal here,
    because whether an absent path is fatal is the caller's declared ``required`` judgement.
    """
    if not pointer:
        return container
    current = container
    for part in pointer.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return MISSING
    return current


def _resolve_records(document: object, pointer: str, *, plan_field: str) -> list[Any]:
    """Follow ``pointer`` into ``document`` and return the record list it must be.

    Raises:
        RowExpansionError: ``MISSING_POINTER`` for an absent path, ``NOT_A_RECORD_LIST`` for a
            path that lands on anything but a list. An empty list passes here; what an empty
            published answer means downstream is each expansion's own judgement.
    """
    reached = _follow(document, pointer)
    if reached is MISSING:
        raise RowExpansionError(MISSING_POINTER, pointer=pointer, saw=f"no {plan_field} path")
    if not isinstance(reached, list):
        raise RowExpansionError(NOT_A_RECORD_LIST, pointer=pointer, saw=f"a {_type_name(reached)}")
    return reached


def _require_text(value: str, label: str) -> str:
    """Return ``value`` when a plan text field is a non-blank string."""
    if not isinstance(value, str) or not value.strip():
        raise RowExpansionError(INVALID_PLAN, pointer=label, saw=f"{value!r} is blank")
    return value


def _read_field(
    container: dict[str, Any],
    mapping: FieldMapping,
    *,
    record_index: int,
    context: str,
) -> Any:  # noqa: ANN401 - a published cell is any JSON scalar or nested value
    """Read one declared field out of one record.

    Raises:
        RowExpansionError: ``MISSING_POINTER`` when a required field's path is absent -- an
            optional field's absence is a null, never a refusal.
    """
    value = _follow(container, mapping.pointer)
    if value is MISSING:
        if mapping.required:
            raise RowExpansionError(
                MISSING_POINTER,
                pointer=f"{context}{mapping.pointer}",
                record_index=record_index,
                saw="the record does not publish this path",
            )
        return None
    return value


@dataclass(frozen=True)
class FieldMapping:
    """One output column and the pointer inside a record that carries it.

    Attributes:
        column: Name the produced row publishes the value under.
        pointer: Dotted path inside the record; it may descend into nested objects, which is how
            an Intrinio transaction carries ``owner.name`` from its filing.
        required: Absent means a shape failure; optional means a null, mirroring ColumnSpec.
    """

    column: str
    pointer: str
    required: bool = True

    def __post_init__(self) -> None:
        """Reject a mapping that cannot name a column or address a key."""
        _require_text(self.column, "field column")
        _require_text(self.pointer, f"field {self.column!r} pointer")


@dataclass(frozen=True)
class NestedArrayExpansion:
    """A declaration that one record's nested array, not the record, is the row source.

    This is what the TMX ETF models and Intrinio InsiderTrading recorded as
    ``nested_array_explode``: after the engine resolves the record list, each record points at a
    child array (``holdings_top10``, ``sectors``, ``transactions``); every child item becomes one
    row carrying its own renamed fields plus the parent fields the plan chooses to copy down.

    Attributes:
        records_pointer: Dotted path to the parent record list; empty means the document itself.
        child_array_pointer: Dotted path, inside a parent record, to the array to explode.
        child_fields: Columns read out of each child item.
        parent_fields: Columns copied from each parent record onto every row that record yields.
    """

    records_pointer: str = ""
    child_array_pointer: str = ""
    child_fields: tuple[FieldMapping, ...] = ()
    parent_fields: tuple[FieldMapping, ...] = ()

    def __post_init__(self) -> None:
        """Reject an expansion that could not produce one named column set per child row."""
        _require_text(self.child_array_pointer, "child array pointer")
        if not self.child_fields:
            raise RowExpansionError(
                INVALID_PLAN,
                pointer="child_fields",
                saw="an explosion with no child fields would emit rows of nothing but parent facts",
            )
        for mapping in (*self.child_fields, *self.parent_fields):
            if not isinstance(mapping, FieldMapping):
                raise RowExpansionError(
                    INVALID_PLAN, pointer="child_fields", saw=_type_name(mapping)
                )
        columns = [mapping.column for mapping in (*self.child_fields, *self.parent_fields)]
        if len(columns) != len(set(columns)):
            raise RowExpansionError(
                INVALID_PLAN, pointer="child_fields/parent_fields", saw="a column is declared twice"
            )


@dataclass(frozen=True)
class SeriesPivotPlan:
    """A declaration that the answer is a pivot, not a record list.

    The series document is a list of series objects, each holding parallel arrays of timestamps and
    values; the pivot produces one row per timestamp with one column per series -- the shape the
    Fed's Svensson release is recorded as needing (``series_pivot``) once its wide body has been
    read into series objects.

    Attributes:
        series_pointer: Dotted path to the list of series objects; empty means the document.
        label_pointer: Path inside a series object naming the series; the name becomes its column.
        dates_pointer: Path to the series' timestamp array; every entry must be a non-blank string.
        values_pointer: Path to the value array, index-aligned with ``dates_pointer``.
        timestamp_column: Name of the timestamp column on every produced row.
    """

    series_pointer: str = ""
    label_pointer: str = SERIES_LABEL_KEY
    dates_pointer: str = SERIES_DATES_KEY
    values_pointer: str = SERIES_VALUES_KEY
    timestamp_column: str = "date"

    def __post_init__(self) -> None:
        """Reject a pivot whose output columns are not all nameable."""
        for label, value in (
            ("label pointer", self.label_pointer),
            ("dates pointer", self.dates_pointer),
            ("values pointer", self.values_pointer),
            ("timestamp column", self.timestamp_column),
        ):
            _require_text(value, label)


@dataclass(frozen=True)
class WideTablePivotPlan:
    """A declaration that a per-record wide table must first be read as series objects.

    The Fed publishes ``feds200628.csv`` as one row per date and one column per series; the body
    decoders already turn that into header-keyed records. :func:`transpose_wide_table` reads those
    records per declared column into the parallel-array series form :class:`SeriesPivotPlan`
    consumes, which is how the census's ``series_pivot`` need on a wide body becomes two pure,
    reviewable steps.

    Attributes:
        records_pointer: Dotted path to the wide records; empty means the document itself.
        timestamp_key: Record key publishing the row's timestamp; required on every record.
        series_keys: Record keys read as one series each; a cell may be null, never a container.
        label_key: Output key each series object publishes its name under.
        dates_key: Output key each series object publishes its timestamp array under.
        values_key: Output key each series object publishes its aligned value array under.
    """

    records_pointer: str = ""
    timestamp_key: str = ""
    series_keys: tuple[str, ...] = ()
    label_key: str = SERIES_LABEL_KEY
    dates_key: str = SERIES_DATES_KEY
    values_key: str = SERIES_VALUES_KEY

    def __post_init__(self) -> None:
        """Reject a transpose whose series could not be addressed or named."""
        _require_text(self.timestamp_key, "timestamp key")
        if not self.series_keys:
            raise RowExpansionError(
                INVALID_PLAN, pointer="series_keys", saw="no series column is declared"
            )
        for key in self.series_keys:
            _require_text(key, "series key")
        if len(set(self.series_keys)) != len(self.series_keys):
            raise RowExpansionError(
                INVALID_PLAN, pointer="series_keys", saw="a series column is declared twice"
            )
        if self.timestamp_key in self.series_keys:
            raise RowExpansionError(
                INVALID_PLAN,
                pointer="series_keys",
                saw=f"the timestamp key {self.timestamp_key!r} is also a series column",
            )
        for label, value in (
            ("label key", self.label_key),
            ("dates key", self.dates_key),
            ("values key", self.values_key),
        ):
            _require_text(value, label)


@dataclass(frozen=True)
class RecordRepointPlan:
    """A declaration that each record's columns live behind per-record sub-pointers.

    This is the ``per_record_nested_flatten`` the Intrinio CalendarIpo census recorded: the record
    list is already flat, but the published columns sit inside nested ``company`` and ``security``
    objects, so the pointer that finds the value must be re-resolved once per record rather than
    once per document. No fetch happens here; the plan re-reads the document it was given.

    Attributes:
        records_pointer: Dotted path to the record list; empty means the document itself.
        fields: One mapping per output column; the pointer may descend into nested objects.
    """

    records_pointer: str = ""
    fields: tuple[FieldMapping, ...] = ()

    def __post_init__(self) -> None:
        """Reject a repoint that could not publish a row with any declared column."""
        if not self.fields:
            raise RowExpansionError(
                INVALID_PLAN, pointer="fields", saw="a record projection with no columns"
            )
        for mapping in self.fields:
            if not isinstance(mapping, FieldMapping):
                raise RowExpansionError(INVALID_PLAN, pointer="fields", saw=_type_name(mapping))
        columns = [mapping.column for mapping in self.fields]
        if len(columns) != len(set(columns)):
            raise RowExpansionError(
                INVALID_PLAN, pointer="fields", saw="a column is declared twice"
            )


def expand_nested_array(document: object, plan: NestedArrayExpansion) -> list[dict[str, Any]]:
    """Explode each record's declared nested array into one plain row dict per child item.

    Produces the TMX ``EtfHoldings``/``EtfSectors``/``EtfCountries`` and Intrinio
    ``InsiderTrading`` shape: parent fields (ETF symbol, filing date, ``owner.name``) are copied
    onto every child row; child fields keep their own values under the columns the plan names.
    Parent and child keys are read with the engine's dotted-pointer grammar, so a carried field may
    itself sit inside a nested object.

    Raises:
        RowExpansionError: ``MISSING_POINTER``/``NOT_A_RECORD_LIST`` for a records pointer that
            does not reach a list; ``NOT_A_RECORD`` for a parent or child that is not a mapping;
            ``MISSING_POINTER`` for a record that does not carry the child array at all (an empty
            array contributes no rows); ``MISSING_POINTER`` for a required field path a record does
            not publish; ``EMPTY_RESULT`` when parent records exist but no row came out. An empty
            resolved parent list returns an empty result.
    """
    records = _resolve_records(document, plan.records_pointer, plan_field="records")
    if not records:
        return []
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RowExpansionError(
                NOT_A_RECORD,
                pointer=plan.records_pointer,
                record_index=index,
                saw=f"a parent record of type {_type_name(record)}",
            )
        children = _follow(record, plan.child_array_pointer)
        if children is MISSING:
            raise RowExpansionError(
                MISSING_POINTER,
                pointer=plan.child_array_pointer,
                record_index=index,
                saw="the record carries no nested array at this path",
            )
        if not isinstance(children, list):
            raise RowExpansionError(
                NOT_A_RECORD_LIST,
                pointer=plan.child_array_pointer,
                record_index=index,
                saw=f"a {_type_name(children)} where an array was declared",
            )
        carried = {
            mapping.column: _read_field(record, mapping, record_index=index, context="")
            for mapping in plan.parent_fields
        }
        for child_index, child in enumerate(children):
            if not isinstance(child, dict):
                raise RowExpansionError(
                    NOT_A_RECORD,
                    pointer=plan.child_array_pointer,
                    record_index=index,
                    saw=f"a child item of type {_type_name(child)} at position {child_index}",
                )
            row = dict(carried)
            for mapping in plan.child_fields:
                row[mapping.column] = _read_field(child, mapping, record_index=index, context="")
            rows.append(row)
    if not rows:
        raise RowExpansionError(
            EMPTY_RESULT,
            pointer=plan.child_array_pointer,
            saw=f"{len(records)} record(s) published no child rows",
        )
    return rows


def _parallel_array(reached: object, pointer: str, record_index: int) -> list[object]:
    """Return the value one parallel pointer reached as the array its plan declares.

    An absent path and a non-list are two different refusals the caller must not conflate: a series
    that publishes no dates key at all is a missing pointer, while a dates key holding an object is
    a shape that cannot line up with anything.
    """
    if reached is MISSING:
        raise RowExpansionError(MISSING_POINTER, pointer=pointer, record_index=record_index)
    if not isinstance(reached, list):
        raise RowExpansionError(
            PARALLEL_ARRAY_MISMATCH,
            pointer=pointer,
            record_index=record_index,
            saw=f"a {_type_name(reached)} where a parallel array was declared",
        )
    return reached


def pivot_series(document: object, plan: SeriesPivotPlan) -> list[dict[str, Any]]:
    """Pivot a list of series objects into one row per timestamp, one column per series.

    Each series publishes parallel arrays -- its ``dates`` and its ``values``, index-aligned --
    and the pivot joins every series on the timestamp. The rows come out in ascending timestamp
    order (timestamps are compared as the published strings, which is what sorts ISO dates), and a
    timestamp one series lacks is simply absent from that row for the declared column set to
    read as a null.

    Raises:
        RowExpansionError: ``MISSING_POINTER``/``NOT_A_RECORD_LIST`` for a series pointer that
            does not reach a list; ``NOT_A_RECORD`` for a series that is not a mapping;
            ``MISSING_POINTER`` for a series lacking its label, dates or values path;
            ``BAD_SERIES_LABEL`` for a label that is not a non-blank string, or
            ``BAD_TIMESTAMP`` for a non-string or blank timestamp; a dates/values path that is
            not a list and ``PARALLEL_ARRAY_MISMATCH`` when the two arrays differ in length;
            ``DUPLICATE_TIMESTAMP`` within one series; ``DUPLICATE_SERIES_LABEL`` when two series
            claim one column or a series is named after the timestamp column; ``EMPTY_RESULT``
            for a document that publishes no series or whose every series is empty.
    """
    series_list = _resolve_records(document, plan.series_pointer, plan_field="series")
    if not series_list:
        raise RowExpansionError(EMPTY_RESULT, pointer=plan.series_pointer, saw="0 series")
    by_timestamp: dict[str, dict[str, Any]] = {}
    seen_labels: set[str] = set()
    for index, series in enumerate(series_list):
        if not isinstance(series, dict):
            raise RowExpansionError(
                NOT_A_RECORD,
                pointer=plan.series_pointer,
                record_index=index,
                saw=f"a series of type {_type_name(series)}",
            )
        label = _follow(series, plan.label_pointer)
        if label is MISSING:
            raise RowExpansionError(MISSING_POINTER, pointer=plan.label_pointer, record_index=index)
        if not isinstance(label, str) or not label.strip():
            raise RowExpansionError(
                BAD_SERIES_LABEL,
                pointer=plan.label_pointer,
                record_index=index,
                saw=f"a series label of {label!r}",
            )
        if label == plan.timestamp_column or label in seen_labels:
            raise RowExpansionError(
                DUPLICATE_SERIES_LABEL,
                pointer=plan.label_pointer,
                record_index=index,
                saw=f"label {label!r} claims an already-taken column",
            )
        seen_labels.add(label)
        dates = _parallel_array(_follow(series, plan.dates_pointer), plan.dates_pointer, index)
        values = _parallel_array(_follow(series, plan.values_pointer), plan.values_pointer, index)
        if len(dates) != len(values):
            raise RowExpansionError(
                PARALLEL_ARRAY_MISMATCH,
                pointer=plan.dates_pointer,
                record_index=index,
                saw=f"{len(dates)} date(s) against {len(values)} value(s)",
            )
        own_timestamps: set[str] = set()
        for offset, timestamp in enumerate(dates):
            if not isinstance(timestamp, str) or not timestamp.strip():
                raise RowExpansionError(
                    BAD_TIMESTAMP,
                    pointer=plan.dates_pointer,
                    record_index=index,
                    saw=f"a timestamp {timestamp!r} at position {offset}",
                )
            if timestamp in own_timestamps:
                raise RowExpansionError(
                    DUPLICATE_TIMESTAMP,
                    pointer=plan.dates_pointer,
                    record_index=index,
                    saw=f"{timestamp!r} repeats within one series",
                )
            own_timestamps.add(timestamp)
            row = by_timestamp.setdefault(timestamp, {plan.timestamp_column: timestamp})
            row[label] = values[offset]
    if not by_timestamp:
        raise RowExpansionError(
            EMPTY_RESULT,
            pointer=plan.series_pointer,
            saw=f"{len(series_list)} series(es) published no timestamps",
        )
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def transpose_wide_table(document: object, plan: WideTablePivotPlan) -> list[dict[str, Any]]:
    """Read per-record wide rows into one parallel-array series object per declared column.

    This is the first half of the Fed's ``series_pivot``: the body decoder has already produced
    one header-keyed record per date, and the transpose walks those records once per declared
    series column, publishing ``{label, dates, values}`` objects whose arrays are index-aligned.
    Its output feeds :func:`pivot_series` with the default pointer names; a null cell stays a
    null in the value array.

    Raises:
        RowExpansionError: ``MISSING_POINTER``/``NOT_A_RECORD_LIST`` for a records pointer that
            does not reach a list; ``NOT_A_RECORD`` for a wide row that is not a mapping;
            ``MISSING_POINTER`` for a record that does not publish the timestamp key or a
            declared series key; ``BAD_TIMESTAMP`` for a timestamp that is not a non-blank string,
            or a series cell that is a container rather than a scalar; ``EMPTY_RESULT`` when no
            record was published.
    """
    records = _resolve_records(document, plan.records_pointer, plan_field="records")
    if not records:
        raise RowExpansionError(EMPTY_RESULT, pointer=plan.records_pointer, saw="0 wide rows")
    timestamps: list[str] = []
    columns: dict[str, list[Any]] = {key: [] for key in plan.series_keys}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RowExpansionError(
                NOT_A_RECORD,
                pointer=plan.records_pointer,
                record_index=index,
                saw=f"a wide row of type {_type_name(record)}",
            )
        timestamp = record.get(plan.timestamp_key)
        if timestamp is None:
            raise RowExpansionError(MISSING_POINTER, pointer=plan.timestamp_key, record_index=index)
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise RowExpansionError(
                BAD_TIMESTAMP,
                pointer=plan.timestamp_key,
                record_index=index,
                saw=f"a timestamp {timestamp!r}",
            )
        if timestamp in timestamps:
            raise RowExpansionError(
                DUPLICATE_TIMESTAMP,
                pointer=plan.timestamp_key,
                record_index=index,
                saw=f"{timestamp!r} repeats across wide rows",
            )
        timestamps.append(timestamp)
        for key in plan.series_keys:
            if key not in record:
                raise RowExpansionError(
                    MISSING_POINTER, pointer=key, record_index=index, saw="no such header column"
                )
            cell = record[key]
            if isinstance(cell, (list, dict)):
                raise RowExpansionError(
                    BAD_TIMESTAMP,
                    pointer=key,
                    record_index=index,
                    saw=f"a {_type_name(cell)} cell where a scalar series value was declared",
                )
            columns[key].append(cell)
    return [
        {plan.label_key: key, plan.dates_key: list(timestamps), plan.values_key: columns[key]}
        for key in plan.series_keys
    ]


def repoint_records(document: object, plan: RecordRepointPlan) -> list[dict[str, Any]]:
    """Project each record through per-record sub-pointers into one plain row dict.

    This is the CalendarIpo shape and the "re-resolve a second pointer per record" seam: given
    the document it holds, the plan re-reads it once per record along paths that may descend into
    nested objects, and the rows that come out are already the flat dicts normalization wants.
    Nothing is fetched; a caller whose second pointer lives in a *different* document supplies
    that document here.

    Raises:
        RowExpansionError: ``MISSING_POINTER``/``NOT_A_RECORD_LIST`` for a records pointer that
            does not reach a list; ``NOT_A_RECORD`` for a record that is not a mapping;
            ``MISSING_POINTER`` for a required field path the record does not publish;
            ``EMPTY_RESULT`` when a non-empty published record list produced no rows. An empty
            resolved record list returns an empty result.
    """
    records = _resolve_records(document, plan.records_pointer, plan_field="records")
    if not records:
        return []
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise RowExpansionError(
                NOT_A_RECORD,
                pointer=plan.records_pointer,
                record_index=index,
                saw=f"a record of type {_type_name(record)}",
            )
        rows.append(
            {
                mapping.column: _read_field(record, mapping, record_index=index, context="")
                for mapping in plan.fields
            }
        )
    if not rows:
        raise RowExpansionError(
            EMPTY_RESULT, pointer=plan.records_pointer, saw="0 rows from the published records"
        )
    return rows
