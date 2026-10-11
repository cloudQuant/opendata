"""path.explode at the spec plane: the Fed's wide Svensson body, read per series and pivoted.

The kernel in :mod:`opendata.data.providers._engine.row_expansion` shipped whole and unwired.
These cases are the wiring: a production declaration names its stages in
``ModelSpec.row_expansion``, and ``resolve_rows`` hands the record list it resolved to
``_expand_rows``, which applies them in declaration order.

Both arms are offline: a canned CSV body through the transport seam, and an autouse guard replacing
the module's only HTTP read function, so "no network" is a property of the run rather than of the
fixtures. The refusal arm is the reachable one -- a declared series column the published body does
not carry -- and it must arrive as :class:`RowExpansionError` naming the pointer and the record
position, never as an empty row set.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import pytest

from opendata.data.providers._engine import http_json
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    build_row_model,
    normalize_record,
    resolve_rows,
)
from opendata.data.providers._engine.row_expansion import (
    MISSING_POINTER,
    RowExpansionError,
    SeriesPivotPlan,
    WideTablePivotPlan,
)
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    absent_key_reads_as_null,
    expected_row_value,
    fixture_context,
    raw_key,
    record_vocabulary,
    repoint_expansion,
    synthetic_record,
    valid_query_kwargs,
)
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.models.svensson_yield_curve import (
    FederalReserveSvenssonYieldCurveFetcher,
)
from opendata.data.providers.federal_reserve.specs import SVENSSON_YIELD_CURVE, TREASURY_RATES
from opendata.data.providers.intrinio.specs import CALENDAR_IPO

#: The 99 series headers the release publishes behind its ``Date`` cell, rebuilt here from the
#: families the contract documents -- 4 fitted betas, 2 tau decay parameters, 3 one-year forwards
#: and three 30-tenor families -- rather than read off the declaration. Writing them out is what
#: makes the equality arms below fail if a spec renames, drops or reorders a published column.
SVENSSON_HEADERS: Final[tuple[str, ...]] = (
    *(f"BETA{index}" for index in range(4)),
    "TAU1",
    "TAU2",
    *(f"SVEN1F{suffix}" for suffix in ("01", "04", "09")),
    *(f"SVENF{tenor:02d}" for tenor in range(1, 31)),
    *(f"SVENPY{tenor:02d}" for tenor in range(1, 31)),
    *(f"SVENY{tenor:02d}" for tenor in range(1, 31)),
)

#: The contract field each header is published under, in the same family order.
SVENSSON_FIELDS: Final[tuple[str, ...]] = (
    *(f"beta{index}" for index in range(4)),
    "tau1",
    "tau2",
    *(f"sven1f{suffix}" for suffix in ("01", "04", "09")),
    *(f"svenf{tenor:02d}" for tenor in range(1, 31)),
    *(f"svenpy{tenor:02d}" for tenor in range(1, 31)),
    *(f"sveny{tenor:02d}" for tenor in range(1, 31)),
)


def _row(timestamp: str, prefix: str, headers: tuple[str, ...] = SVENSSON_HEADERS) -> str:
    """One wide CSV row whose every cell is tagged with that column's header position."""
    return ",".join((timestamp, *(f"{prefix}{index:02d}" for index in range(len(headers)))))


#: Two published dates, deliberately out of order so the pivot's timestamp ordering is under test.
#: Every cell carries its column position, which is what makes a mis-paired ``source_key`` answer
#: with a different column's value instead of passing silently.
SVENSSON_BODY = "\n".join(
    (",".join(("Date", *SVENSSON_HEADERS)), _row("2024-01-02", "a"), _row("2024-01-01", "b"), "")
)

#: The same table with BETA3 missing from the header, so a declared series column is one the
#: published body does not carry.
BODY_WITHOUT_A_SERIES = "\n".join(
    (
        ",".join(("Date", *(key for key in SVENSSON_HEADERS if key != "BETA3"))),
        _row("2024-01-01", "b", tuple(key for key in SVENSSON_HEADERS if key != "BETA3")),
        "",
    )
)


@pytest.fixture(autouse=True)
def _no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the governed client unreachable for every case in this file."""

    def _unreachable(*args: object, **kwargs: object) -> HttpResponse:
        raise AssertionError("row-expansion witnesses must never reach the governed client")

    monkeypatch.setattr(http_json, "_http_get_json", _unreachable)


def _fetch(body: str) -> tuple[object, ...]:
    """Fetch one canned body through the fetcher the production declaration generates."""
    transport = FixedResponseTransport(HttpResponse(200, None, payload=body))
    fetcher = type(
        FederalReserveSvenssonYieldCurveFetcher.__name__,
        (FederalReserveSvenssonYieldCurveFetcher,),
        {"http_transport": transport},
    )()
    return tuple(
        fetcher.fetch(
            ctx=fixture_context(SOURCE, SVENSSON_YIELD_CURVE, sends=1),
            **valid_query_kwargs(SVENSSON_YIELD_CURVE),
        )
    )


class TestDeclaredExpansion:
    """POSITIVE: the real declaration turns the wide body into one row per timestamp."""

    def test_the_declaration_names_its_stages_as_data(self) -> None:
        """The capability is a spec-plane field, not a branch written for one model."""
        assert [type(stage).__name__ for stage in SVENSSON_YIELD_CURVE.row_expansion] == [
            "WideTablePivotPlan",
            "SeriesPivotPlan",
        ]
        transpose, pivot = SVENSSON_YIELD_CURVE.row_expansion
        assert isinstance(transpose, WideTablePivotPlan)
        assert isinstance(pivot, SeriesPivotPlan)
        assert len(SVENSSON_HEADERS) == 99
        assert transpose.timestamp_key == "Date"
        assert transpose.series_keys == SVENSSON_HEADERS

    def test_the_declared_columns_are_the_published_contract(self) -> None:
        """One column per published cell, each field paired with the header it is read out of."""
        columns = SVENSSON_YIELD_CURVE.columns
        assert [column.name for column in columns] == ["date", *SVENSSON_FIELDS]
        assert [(column.name, column.source_key) for column in columns[1:]] == list(
            zip(SVENSSON_FIELDS, SVENSSON_HEADERS, strict=True)
        )

    def test_the_wide_body_becomes_one_row_per_timestamp(self) -> None:
        """Two published dates, so two rows -- ascending by timestamp, not by published order."""
        rows = _fetch(SVENSSON_BODY)
        assert [row.date for row in rows] == [date(2024, 1, 1), date(2024, 1, 2)]

    def test_every_row_carries_one_column_per_series(self) -> None:
        """The pivot's point: the wide body's 99 series columns are 99 fields on each row."""
        rows = _fetch(SVENSSON_BODY)
        assert len(rows[0].model_fields) == 100
        assert set(rows[0].model_fields) == {"date", *SVENSSON_FIELDS}

    def test_each_field_carries_the_value_its_own_header_published(self) -> None:
        """The alignment arm: a mis-paired source key answers with another column's tagged value."""
        earlier, later = _fetch(SVENSSON_BODY)
        for position, field in enumerate(SVENSSON_FIELDS):
            assert getattr(earlier, field) == f"b{position:02d}", field
            assert getattr(later, field) == f"a{position:02d}", field

    def test_a_declaration_naming_no_stage_resolves_the_list_as_before(self) -> None:
        """The byte-identical arm: row_expansion defaults empty, so nothing is routed."""
        records = [{"date": "2024-01-01"}]
        assert not TREASURY_RATES.row_expansion
        assert resolve_rows({"rows": records}, "rows", TREASURY_RATES) == records


class TestExpansionRefusals:
    """REFUSAL: a pointer the document does not have is attributed, not answered with nothing."""

    def test_a_declared_series_column_the_body_lacks_refuses_by_pointer(self) -> None:
        """The kernel keeps the pointer and record index a silently empty frame would hide."""
        with pytest.raises(Exception) as caught:
            _fetch(BODY_WITHOUT_A_SERIES)
        error = caught.value
        assert isinstance(error, RowExpansionError), f"a {type(error).__name__} escaped: {error}"
        assert error.reason == MISSING_POINTER
        assert error.pointer == "BETA3"
        assert error.record_index == 0
        assert "BETA3" in str(error)
        assert "record=0" in str(error)

    def test_a_timestamp_header_the_body_calls_DATE_is_a_refusal(self) -> None:  # noqa: N802
        """The published token is Date, so the uppercase spelling first guessed here is not it."""
        with pytest.raises(Exception) as caught:
            _fetch("\n".join((",".join(("DATE", *SVENSSON_HEADERS)), _row("2024-01-01", "b"), "")))
        error = caught.value
        assert isinstance(error, RowExpansionError), f"a {type(error).__name__} escaped: {error}"
        assert error.reason == MISSING_POINTER
        assert error.pointer == "Date"


def _drop(record: dict[str, object], pointer: str) -> None:
    """Delete the value one repoint pointer addresses, keeping the objects above it in place."""
    parts = pointer.split(".")
    cursor = record
    for part in parts[:-1]:
        nested = cursor[part]
        assert isinstance(nested, dict), f"{pointer!r} walks through a {type(nested).__name__}"
        cursor = nested
    cursor.pop(parts[-1], None)


def _normalize(record: dict[str, object]) -> object:
    """Repoint and normalize one scripted record through the engine's own two kernels."""
    document: dict[str, object] = {CALENDAR_IPO.rows_pointer: [record]}
    rows = resolve_rows(document, CALENDAR_IPO.rows_pointer, CALENDAR_IPO)
    return normalize_record(rows[0], document, CALENDAR_IPO, build_row_model(CALENDAR_IPO))


class TestRepointedRecordVocabulary:
    """POSITIVE and DAMAGE: the shared fixtures script a repoint declaration at its plan's paths.

    ``intrinio::CalendarIpo`` is the first declaration these fixtures face with a
    :class:`RecordRepointPlan`, whose kernel re-resolves one dotted pointer *inside* each already
    flat record. The body and the oracle that judges it can only agree by both reading the plan, so
    each case below states the placement the plan implies rather than a name this file remembered.
    """

    def test_the_plan_names_where_each_column_is_published(self) -> None:
        """A repointed column's key is its pointer, and a dotted pointer is a nested object."""
        columns = {column.name: column for column in CALENDAR_IPO.columns}
        assert repoint_expansion(CALENDAR_IPO) is CALENDAR_IPO.row_expansion[0]
        assert [raw_key(CALENDAR_IPO, columns[name]) for name in ("symbol", "ipo_date")] == [
            "ticker",
            "date",
        ]
        record = synthetic_record(CALENDAR_IPO, 0)
        assert record["company"] == dict.fromkeys(
            ("id", "ticker", "name", "lei", "cik"), "alpha"
        ), "a dotted pointer is scripted as a nested object"
        assert "company_lei" not in record, "the flat declared name is not the path the plan reads"

    def test_the_vocabulary_is_the_pointers_the_plan_re_resolves(self) -> None:
        """The record's own header list, so a falsifier can name a key the engine really reads."""
        vocabulary = record_vocabulary(CALENDAR_IPO)
        assert [key for key in vocabulary if "." in key][:2] == ["company.id", "company.ticker"]
        assert vocabulary[0] == "ticker"

    def test_every_repointed_column_reaches_its_row(self) -> None:
        """The agreement arm: the scripted body and the oracle answer in the plan's vocabulary."""
        row = _normalize(synthetic_record(CALENDAR_IPO, 1))
        for column in CALENDAR_IPO.columns:
            assert getattr(row, column.name) == expected_row_value(CALENDAR_IPO, column, 1), (
                column.name
            )

    def test_a_pointer_the_record_lacks_is_the_null_the_plan_promises(self) -> None:
        """The damage arm: a decoy under the declared name cannot stand in for the missing path."""
        record = synthetic_record(CALENDAR_IPO, 0)
        moved = [
            (column, key)
            for column in CALENDAR_IPO.columns
            if (key := absent_key_reads_as_null(CALENDAR_IPO, column)) is not None
            and key != column.name
        ]
        assert [column.name for column, _ in moved][:2] == ["symbol", "ipo_date"], moved
        for column, pointer in moved:
            _drop(record, pointer)
            record[column.name] = "DECOY"
        row = _normalize(record)
        assert [getattr(row, column.name) for column, _ in moved] == [None] * len(moved)
        assert row.status == "alpha", "a column whose pointer stayed published keeps its value"
