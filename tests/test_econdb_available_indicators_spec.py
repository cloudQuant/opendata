"""Offline witness for the declared econdb ``AvailableIndicators`` model.

Census row ``OBB2-econdb-AvailableIndicators`` -- one GET of the static roster
``econdb.com/static/help/main_tickers.csv`` (helpers.py:431, read at :449), whose five needs the
roadmap's ``shipped_cover_rows`` maps onto ``decode.delimited``, ``rows.filter``,
``rows.order_limit``, ``columns.null_sentinel`` and ``columns.derive``. Four of the five are carried
by :data:`AVAILABLE_INDICATORS` and asserted positively below; the fifth is witnessed as a real gap
(``DeriveStage`` admits ``cast`` and ``scale`` and nothing else, so ``symbol_root``'s string surgery
has no plan to be written in), and :data:`DECLARED_GAPS` prices it.

No case touches a socket or a file. The body is the *written* contract, hand-typed here the way the
intrinio witness types its filings: nothing in this repo has ever captured this CSV, so every case
proves declaration-to-row mapping and none of them proves the published roster. That is stated in
the spec's own docstring and it is the honest limit of this file.

Every break arm names its reading BEFORE the run:

* ``ROSTER_HEAD`` -- the reading is ``rows[0].symbol == "DEUPCP"``, the descending-``last_date``
  head of the roster. Breaking ``order_by`` makes it red: the published order puts ``FRUPCP`` first.
* ``OECD_BLOCK`` -- the reading is ``[r.symbol] == ["DEUPCP", "JPUPCP", "FRUPCP"]``. Breaking
  ``row_filters`` makes it red by admitting ``OECDUPCP``.
* ``SENTINEL_NULLING`` -- the reading is ``rows[2].source is None`` beside
  ``rows[2].currency is None``. Stripping those two sentinels makes it red *without* an exception:
  ``"nan"`` reaches the answer verbatim as a published value, which is the silent swallow the
  declaration exists to prevent. The empty cell reads null either way -- the normalizer already
  treats ``""`` as no value for a nullable column -- so it is the ``N/A``/``nan`` spellings of
  helpers.py:481 that the sentinel is for, and the arm says so rather than claiming more.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.spec import ColumnSpec, RowFilterSpec
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers._engine.value_transform import DerivePlan, DeriveStage
from opendata.data.providers.econdb._source import SOURCE
from opendata.data.providers.econdb.provider import PROVIDER
from opendata.data.providers.econdb.specs import (
    AVAILABLE_INDICATORS,
    DECLARED_GAPS,
    DECLARED_MODELS,
    ECONDB_NULL_SENTINEL,
    NOT_DECLARED,
    READY_MODELS,
    UPSTREAM_ROWS,
)

ROSTER_URL = "https://www.econdb.com/static/help/main_tickers.csv"

#: The roster's header row, in the order the published file spells it. ``iso`` is deliberately NOT
#: one of the declared columns: the filter tests it on the raw record and the contract drops it.
ROSTER_HEADER = (
    "short_ticker,entity,iso,transformation,scale,currency,source,"
    "first_date,last_date,last_insert_timestamp"
)

#: Published order: the oldest surviving indicator first, so the declared descending order is what
#: moves ``DEUPCP`` to the head and a broken ``order_by`` is observable rather than cosmetic.
ROSTER_ROWS = (
    # currency is the empty cell read_csv leaves, source spells "nan", first_date is empty again:
    # all three are the sentinel arm of this fixture.
    "FRUPCP,France,FR,Level,Index,,nan,,2019-03-01,2019-04-05T02:00:00",
    "DEUPCP,Germany,DE,Level,Index,EUR,Eurostat,1991-01-01,2024-06-01,2024-07-01T09:00:00",
    # The obsolete OECD block helpers.py:451 discards before anything else happens to the roster.
    "OECDUPCP,OECD aggregates,OECD,Level,Index,USD,OECD,1990-01-01,2024-05-01,2024-06-01T09:00:00",
    "JPUPCP,Japan,JP,Level,Index,JPY,Cabinet Office,2010-01-01,2023-11-01,2023-12-05T02:00:00",
)

ROSTER_CSV = "\n".join((ROSTER_HEADER, *ROSTER_ROWS))

#: The three rows the declaration answers with, in the order the declaration answers them in.
EXPECTED_SYMBOLS = ["DEUPCP", "JPUPCP", "FRUPCP"]


def _fetch(spec, transport, sends=1, **query):
    """Drive one declaration over one scripted transport and return ``(calls, rows)``."""
    cls = make_http_json_fetcher(SOURCE, spec)
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=sends), **query))
    return transport.calls, rows


def _run(spec=None, payload=ROSTER_CSV):
    """Drive the roster declaration over one hand-written CSV body with a single response."""
    declaration = AVAILABLE_INDICATORS if spec is None else spec
    transport = FixedResponseTransport(HttpResponse(200, None, payload=payload))
    return _fetch(declaration, transport)


def _without_text_sentinels(spec):
    """Return ``spec`` with the sentinel set removed from two *text* columns only.

    Narrow on purpose: an empty cell reaching a ``date`` column would raise on the cast instead of
    being read, and the reading this arm prices is the silent one -- published sentinel text that
    answers as if it were a value.
    """
    return replace(
        spec,
        columns=tuple(
            replace(column, null_sentinel=()) if column.name in ("currency", "source") else column
            for column in spec.columns
        ),
    )


class TestCensusRowIsDeclared:
    """The need set is carried by fields that exist, and the residue is written down."""

    def test_the_model_is_declared_and_no_longer_refused(self) -> None:
        assert AVAILABLE_INDICATORS.model == "AvailableIndicators"
        assert DECLARED_MODELS == {"AvailableIndicators": AVAILABLE_INDICATORS}
        assert "AvailableIndicators" not in NOT_DECLARED
        implemented = set(DECLARED_MODELS) | set(READY_MODELS)
        assert not implemented & set(NOT_DECLARED), "implemented *and* refused"
        assert implemented | set(NOT_DECLARED) == set(UPSTREAM_ROWS), "a silent drop"
        assert len(UPSTREAM_ROWS) == 8, "this provider's census has eight rows"
        assert set(READY_MODELS) == {
            "EconomicIndicators",
            "ExportDestinations",
            "GdpNominal",
            "GdpReal",
            "PortVolume",
            "YieldCurve",
        }
        assert set(NOT_DECLARED) == {"CountryProfile"}
        assert "symbol_root" in DECLARED_GAPS["AvailableIndicators"]

    def test_the_provider_descriptor_binds_the_declared_and_ready_models(self) -> None:
        assert PROVIDER.source == "econdb"
        bound = {
            model_id
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert bound == set(DECLARED_MODELS) | set(READY_MODELS)
        assert len(PROVIDER.credentials) == 1
        assert PROVIDER.credentials[0].environment_variable == "ECONDB_API_KEY"

    def test_the_five_facets_the_need_set_names_are_the_ones_declared(self) -> None:
        # decode.delimited
        assert AVAILABLE_INDICATORS.decoder.kind == "csv"
        assert AVAILABLE_INDICATORS.rows_pointer == ""
        # rows.filter / rows.order_limit
        assert len(AVAILABLE_INDICATORS.row_filters) == 1
        assert AVAILABLE_INDICATORS.row_filters[0].op == "not_equals"
        assert AVAILABLE_INDICATORS.row_filters[0].columns == ("iso",)
        assert AVAILABLE_INDICATORS.order_by is not None
        assert AVAILABLE_INDICATORS.order_by.column == "last_date"
        assert AVAILABLE_INDICATORS.order_by.direction == "desc"
        # columns.null_sentinel: every column but the one non-nullable key carries the set
        sentinelled = [c.name for c in AVAILABLE_INDICATORS.columns if c.null_sentinel]
        assert sentinelled == [  # noqa: C416 -- preserve the assertion AST and literal test oracle
            name
            for name in (
                "country",
                "transformation",
                "currency",
                "scale",
                "multiplier",
                "source",
                "first_date",
                "last_date",
                "last_insert_timestamp",
            )
        ]
        assert all(
            c.null_sentinel == ECONDB_NULL_SENTINEL
            for c in AVAILABLE_INDICATORS.columns
            if c.null_sentinel
        )
        # columns.derive: refused for this row, and the refusal is the kernel's, not this file's
        assert AVAILABLE_INDICATORS.column_derives == ()

    def test_a_derive_plan_cannot_spell_the_rosters_string_surgery(self) -> None:
        "WITNESS for the priced ``derived_columns`` gap: the stage vocabulary is cast and scale."
        with pytest.raises(ValueError, match="cast.*scale|stage kind"):
            DeriveStage(kind="string_surgery")  # type: ignore[call-overload]
        with pytest.raises(ValueError):
            DerivePlan(
                column="symbol_root",
                source_kind="record",
                stages=(
                    DeriveStage(kind="cast", cast=None),  # type: ignore[arg-type]
                ),
            )


class TestCsvRosterRows:
    """POSITIVE: one GET, the roster header becomes the row vocabulary, nothing else is sent."""

    def test_the_roster_answers_three_rows_in_descending_last_date_order(self) -> None:
        calls, rows = _run()
        assert len(calls) == 1
        assert calls[0]["url"] == ROSTER_URL
        assert len(rows) == 3
        assert [row.symbol for row in rows] == EXPECTED_SYMBOLS
        assert [row.last_date for row in rows] == [
            date(2024, 6, 1),
            date(2023, 11, 1),
            date(2019, 3, 1),
        ]

    def test_the_row_carries_the_contract_names_not_the_record_names(self) -> None:
        """``symbol``/``country`` are the aliases; ``short_ticker``/``entity``/``iso`` are gone."""
        _, rows = _run()
        dumped = rows[0].model_dump()
        assert set(dumped) == {c.name for c in AVAILABLE_INDICATORS.columns}
        for retired in ("short_ticker", "entity", "iso", "symbol_root"):
            assert retired not in dumped
        assert dumped["symbol"] == "DEUPCP"
        assert dumped["country"] == "Germany"
        assert dumped["transformation"] == "Level"
        assert dumped["scale"] == "Index"
        assert dumped["currency"] == "EUR"
        assert dumped["source"] == "Eurostat"
        assert dumped["first_date"] == date(1991, 1, 1)
        assert dumped["last_insert_timestamp"] == "2024-07-01T09:00:00"

    def test_the_roster_publishes_no_multiplier_and_the_row_reads_it_null(self) -> None:
        """The gap's runtime face: upstream derives it at helpers.py:476, no plan can spell it."""
        _, rows = _run()
        assert [row.multiplier for row in rows] == [None, None, None]

    def test_no_request_parameter_is_sent(self) -> None:
        calls, _ = _run()
        assert calls[0]["params"] == {}


class TestRosterHeadReading:
    """The pre-named reading ``ROSTER_HEAD`` and the break that turns it red."""

    def test_the_clean_arm_head_is_the_most_recently_updated_indicator(self) -> None:
        """ROSTER_HEAD (clean): ``rows[0].symbol == "DEUPCP"``, published order was FRUPCP first."""
        _, rows = _run()
        assert rows[0].symbol == "DEUPCP"
        assert rows[0].last_date == date(2024, 6, 1)

    def test_dropping_order_by_makes_the_head_reading_red(self) -> None:
        """COUNTERFACT on ``rows.order_limit``: the same body answers in the file's own order."""
        unsorted_spec = replace(AVAILABLE_INDICATORS, order_by=None)
        _, rows = _run(unsorted_spec)
        assert rows[0].symbol == "FRUPCP", "ROSTER_HEAD is red: the head is now the published first"
        assert [row.symbol for row in rows] == ["FRUPCP", "DEUPCP", "JPUPCP"]
        assert rows[0].last_date < rows[1].last_date, "the descending sequence no longer holds"


class TestOecdBlockSelection:
    """POSITIVE and COUNTERFACT for ``OECD_BLOCK``: the filter is what removes the obsolete rows."""

    def test_the_oecd_row_is_selected_out_of_the_published_roster(self) -> None:
        _, rows = _run()
        assert "OECDUPCP" not in [row.symbol for row in rows]
        assert [row.symbol for row in rows] == EXPECTED_SYMBOLS

    def test_dropping_the_filter_admits_the_oecd_row_and_its_newer_date(self) -> None:
        """COUNTERFACT on ``rows.filter``: four rows, and the OECD date reorders the answer."""
        unfiltered = replace(AVAILABLE_INDICATORS, row_filters=())
        _, rows = _run(unfiltered)
        assert len(rows) == 4
        assert "OECDUPCP" in [row.symbol for row in rows]
        assert [row.symbol for row in rows] == ["DEUPCP", "OECDUPCP", "JPUPCP", "FRUPCP"]
        assert rows[1].country == "OECD aggregates"

    def test_a_filter_that_names_no_needle_is_refused_when_written(self) -> None:
        """The declaration cannot say "select on nothing" and then drop everything quietly."""
        with pytest.raises(ValueError, match="no needle|declares no needle"):
            RowFilterSpec(op="not_equals", columns=("iso",))


class TestSentinelNulling:
    "POSITIVE and COUNTERFACT for ``SENTINEL_NULLING``: text spellings become null, values stay."

    def test_the_empty_and_nan_cells_read_as_null(self) -> None:
        _, rows = _run()
        oldest = rows[2]
        assert oldest.symbol == "FRUPCP"
        assert oldest.currency is None
        assert oldest.source is None
        assert oldest.first_date is None
        assert oldest.scale == "Index"
        assert oldest.last_date == date(2019, 3, 1)

    def test_the_same_cells_without_the_sentinels_answer_as_published_text(self) -> None:
        """COUNTERFACT: no exception, no refusal -- the sentinel text reaches the answer.

        This is the reading the declaration exists to prevent, and it is why the arm is written
        against two text columns: a break that only crashes would credit the facet with catching
        something it never had to.
        """
        _, rows = _run(_without_text_sentinels(AVAILABLE_INDICATORS))
        oldest = rows[2]
        assert oldest.source == "nan", "SENTINEL_NULLING is red: 'nan' is published as a value"
        assert oldest.currency is None, "the empty cell reads null with or without the sentinel"
        assert oldest.first_date is None, "the date column still carries its sentinel in this arm"


class TestReachableRefusals:
    """REFUSAL arm: an input this declaration must not swallow silently."""

    def test_a_roster_that_publishes_no_ticker_column_refuses_as_a_shape_failure(self) -> None:
        """``symbol`` is required and non-nullable: a header without it cannot select or sort.

        Upstream indexes ``short_ticker`` unconditionally (helpers.py:453-457), so a file that does
        not publish it is a shape failure there too -- and here it is refused rather than answered
        as an empty roster or a row with a blank symbol.
        """
        header = ROSTER_HEADER.replace("short_ticker,", "")
        body = "\n".join((header, *(row.partition(",")[2] for row in ROSTER_ROWS)))
        with pytest.raises(Exception) as caught:
            _run(payload=body)
        error = caught.value
        assert str(getattr(error, "code", "")) == "ECONDB_SHAPE_INVALID", repr(error)
        assert "symbol" in tuple(getattr(error, "missing", ()) or ())


class TestColumnDeclarationsAreHonest:
    """The declaration's own shape, checked against the contract it claims to publish."""

    def test_every_declared_column_names_a_contract_field(self) -> None:
        assert [c.name for c in AVAILABLE_INDICATORS.columns] == [
            "symbol",
            "country",
            "transformation",
            "currency",
            "scale",
            "multiplier",
            "source",
            "first_date",
            "last_date",
            "last_insert_timestamp",
        ]
        aliased = {c.name: c.source_key for c in AVAILABLE_INDICATORS.columns if c.source_key}
        assert aliased == {"symbol": "short_ticker", "country": "entity"}

    def test_only_the_ticker_key_is_required_and_non_nullable(self) -> None:
        required = [c.name for c in AVAILABLE_INDICATORS.columns if c.required]
        assert required == ["symbol"]
        assert [c.name for c in AVAILABLE_INDICATORS.columns if not c.nullable] == ["symbol"]
        for column in AVAILABLE_INDICATORS.columns:
            assert isinstance(column, ColumnSpec)
