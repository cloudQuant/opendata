"""federal_reserve offline judgement: two CSV declarations, their rows by value, and refusals.

Every case here feeds a response body through ``FixedResponseTransport`` and reaches no socket.
The bodies are written in this file, but they are not invented shapes: the header cells and the
data rows are transcribed from the recorded upstream capture
``providers/federal_reserve/tests/record/http/test_federal_reserve_fetchers/*.yaml`` at the pinned
OpenBB commit, so each assertion is about the column names the Federal Reserve really prints
(``Time Period``, ``M1_N.M``, ``RIFLGFCY10_N.B``) rather than about a fixture's own vocabulary.
The bodies remain synthesized -- this file's rendering of that capture's table, not a byte-for-byte
replay of it.

Three facts this file exists to fix. The first is the one the task was written for: ``DecoderSpec``
is now declared by a production provider module, twice, with ``rows_pointer=""`` as the delimited
rule requires. The second is the gap the task did not predict and the engine has since closed: a Fed
``Output.aspx`` package body puts five metadata rows before its header, and upstream discards them
by counting (``read_csv(..., header=5)``). Each declaration therefore names ``preamble_rows=5``, and
the faithful body these cases feed is :attr:`Case.shipped_package` -- metadata rows, header, table.
:class:`TestCounterfacts` shows the count is load-bearing rather than decorative: the same table
without its preamble, and a preamble one row short or one row long, are each refused by name instead
of read one row off, and a body that is only preamble cannot become a zero-row success. The third is
what the release's own query-string constants are: ``rel``, ``series``, ``filetype`` and the rest
are :attr:`~opendata.data.providers._engine.spec.ModelSpec.static_query` entries, so they ride on
every send and no caller may re-address a reviewed model at another series hash -- the case that
used to declare this endpoint's third model, ``YieldCurve``, is gone because the oracle melts this
table into long rows, which is not declarable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from opendata.data.providers._engine.decoders import ResponseDecodeError
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    build_query_model,
    encode_query,
)
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers.federal_reserve import specs
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.models.money_measures import (
    FederalReserveMoneyMeasuresFetcher,
)
from opendata.data.providers.federal_reserve.models.treasury_rates import (
    FederalReserveTreasuryRatesFetcher,
)
from opendata.data.providers.federal_reserve.provider import PROVIDER
from opendata.data.providers.federal_reserve.specs import (
    DECLARED_GAPS,
    MONEY_MEASURES,
    NOT_DECLARED,
    TREASURY_RATES,
)

if TYPE_CHECKING:
    from opendata.data.protocol import Fetcher
    from opendata.data.providers._engine.spec import ModelSpec

#: The H.6 header, transcribed from the recorded capture: 30 published columns, 8 declared.
MONEY_MEASURES_HEADER = (
    "Time Period,M1_N.M,M2_N.M,MCU_N.M,MDD_N.M,MDL_N.M,MDTS_N.M,MMFGB_N.M,MIKD_N.M,MIKM_N.M,"
    "MIK_N.M,M1.M,M2.M,MCU.M,MDD.M,MDL.M,MDTS.M,MMFGB.M,RESMOC14A_N.M,RESMOB14A_N.M,"
    "RESMO14A_N.M,RESTR14A_N.M,RESBR14A_N.M,RESNB14A_N.M,MDO_N.M,MDU_N.M,MTC_N.M,MDO.M,MDU.M,"
    "MTC.M"
)
#: First and last published rows of that capture, verbatim including their blank cells.
MONEY_MEASURES_ROWS = (
    "1959-01,142.2,289.8,28.4,113.4,,11.7,,0.0,0.0,0.0,138.9,286.6,28.5,110.0,,11.7,,31.6,18.9,"
    "50.5,18.9,551.8,18.3,0.0,136.0,0.3,0.0,136.0,0.3\n"
    "2024-05,17943.5,20851.9,2264.5,5169.2,10509.8,1091.5,1816.9,469.0,544.8,1013.8,18036.4,"
    "20963.4,2253.3,5151.9,10631.1,1090.6,1836.4,2349.1,3376.2,5725.3,3376.2,120409.9,3255.8,"
    ",,,,,\n"
)
#: The 1959 row as the eight declared columns read it: MDL_N.M and MMFGB_N.M publish blank.
MONEY_MEASURES_1959 = {
    "month": "1959-01",
    "m1": 142.2,
    "m2": 289.8,
    "currency": 28.4,
    "demand_deposits": 113.4,
    "other_liquid_deposits": None,
    "small_denomination_time_deposits": 11.7,
    "retail_money_market_funds": None,
}
MONEY_MEASURES_2024 = {
    "month": "2024-05",
    "m1": 17943.5,
    "m2": 20851.9,
    "currency": 2264.5,
    "demand_deposits": 5169.2,
    "other_liquid_deposits": 10509.8,
    "small_denomination_time_deposits": 1091.5,
    "retail_money_market_funds": 1816.9,
}

#: The H.15 header, transcribed from the recorded capture; TreasuryRates is its only reader here.
H15_HEADER = (
    "Time Period,RIFLGFCM01_N.B,RIFLGFCM03_N.B,RIFLGFCM06_N.B,RIFLGFCY01_N.B,RIFLGFCY02_N.B,"
    "RIFLGFCY03_N.B,RIFLGFCY05_N.B,RIFLGFCY07_N.B,RIFLGFCY10_N.B,RIFLGFCY20_N.B,RIFLGFCY30_N.B"
)
#: A row of published percents, a row that prints the source's ND sentinel, and the last row.
#: All three are verbatim rows of that capture, so each has the header's twelve fields.
H15_ROWS = (
    "1962-01-02,,,,3.22,,3.70,3.88,,4.06,4.07,\n"
    "1962-02-12,,,,ND,,ND,ND,,ND,ND,\n"
    "2024-06-25,5.43,5.49,5.37,5.10,4.65,4.45,4.25,4.23,4.23,4.46,4.36\n"
)
H15_1962 = {
    "date": date(1962, 1, 2),
    "month_1": None,
    "month_3": None,
    "month_6": None,
    "year_1": "3.22",
    "year_2": None,
    "year_3": "3.70",
    "year_5": "3.88",
    "year_7": None,
    "year_10": "4.06",
    "year_20": "4.07",
    "year_30": None,
}
#: The same table one row later, with ND kept verbatim because the columns are declared str.
H15_ND = {
    "date": date(1962, 2, 12),
    "month_1": None,
    "month_3": None,
    "month_6": None,
    "year_1": "ND",
    "year_2": None,
    "year_3": "ND",
    "year_5": "ND",
    "year_7": None,
    "year_10": "ND",
    "year_20": "ND",
    "year_30": None,
}
H15_2024 = {
    "date": date(2024, 6, 25),
    "month_1": "5.43",
    "month_3": "5.49",
    "month_6": "5.37",
    "year_1": "5.10",
    "year_2": "4.65",
    "year_3": "4.45",
    "year_5": "4.25",
    "year_7": "4.23",
    "year_10": "4.23",
    "year_20": "4.46",
    "year_30": "4.36",
}

#: The five metadata rows the capture puts before its header, first cells transcribed verbatim;
#: the description cells are this file's abbreviations of the same rows.
H6_PREAMBLE = (
    'Series Description,"M1; Not seasonally adjusted","M2; Not seasonally adjusted"\n'
    "Unit:,Currency,Currency\n"
    "Multiplier:,1e+09,1e+09\n"
    "Currency:,USD,USD\n"
    "Unique Identifier:,H6/H6_M1/M1_N.M,H6/H6_M2/M2_N.M\n"
)
H15_PREAMBLE = (
    'Series Description,"Market yield on U.S. Treasury securities at 1-month constant '
    'maturity","Market yield on U.S. Treasury securities at 10-year constant maturity"\n'
    "Unit:,Percent:_Per_Year,Percent:_Per_Year\n"
    "Multiplier:,1,1\n"
    "Currency:,NA,NA\n"
    "Unique Identifier:,H15/H15/RIFLGFCM01_N.B,H15/H15/RIFLGFCY10_N.B\n"
)

#: The friendly labels ``label=include`` prints instead of the series ids, for a header case.
H6_RENAMED_HEADER = "Month,M1,M2,MCU,MDD,MDL,MDTS,MMFGB"
H15_RENAMED_HEADER = (
    "Date,1-Month,3-Month,6-Month,1-Year,2-Year,3-Year,5-Year,7-Year,10-Year,20-Year,30-Year"
)

#: The query the oracle composes for each release, in upstream's own key order.
H6_QUERY = {
    "rel": "H6",
    "series": "798e2796917702a5f8423426ba7e6b42",
    "lastobs": "",
    "filetype": "csv",
    "label": "include",
    "layout": "seriescolumn",
    "type": "package",
}
H15_QUERY = {
    "rel": "H15",
    "series": "bf17364827e38702b42a58cf8eaa3f78",
    "lastobs": "",
    "filetype": "csv",
    "label": "include",
    "layout": "seriescolumn",
    "type": "package",
}
DOWNLOAD_URL = "https://www.federalreserve.gov/datadownload/Output.aspx"


@dataclass(frozen=True)
class Case:
    """One model, everything a judgement about it needs in one place.

    A dataclass rather than ten positional parametrise arguments: each test below reads the fields
    it cares about by name, so adding a model cannot shift an argument order.
    """

    name: str
    spec: ModelSpec
    fetcher_type: type[Fetcher[Any, Any]]
    header: str
    rows: str
    first_row: dict[str, Any]
    last_row: dict[str, Any]
    required: str
    preamble: str
    renamed_header: str
    query: dict[str, str]

    @property
    def table(self) -> bytes:
        """The header row plus every data row, without the metadata rows in front of them."""
        return body(self.header, *self.lines)

    @property
    def lines(self) -> tuple[str, ...]:
        """The data rows of :attr:`rows`, without their trailing newline."""
        return tuple(self.rows.strip().splitlines())

    @property
    def preamble_lines(self) -> tuple[str, ...]:
        """The five metadata rows one at a time, so a case can miscount them."""
        return tuple(self.preamble.strip().splitlines())

    @property
    def shipped_package(self) -> bytes:
        """What the endpoint really answers: five metadata rows, then the header, then the table."""
        return self.packaged(self.header, *self.lines)

    def packaged(self, *lines: str, preamble: int | None = None) -> bytes:
        """``lines`` behind ``preamble`` metadata rows, five when the case does not say.

        The metadata rows are this model's own, truncated or repeated, because the only fact a
        declared preamble depends on is how many non-blank rows precede the header.
        """
        count = len(self.preamble_lines) if preamble is None else preamble
        return body(*(self.preamble_lines * 2)[:count], *lines)

    def packaged_with_preamble(self, count: int) -> bytes:
        """The table behind ``count`` metadata rows: five is shipped, the rest are miscounts."""
        return self.packaged(self.header, *self.lines, preamble=count)


CASES = (
    Case(
        name="MoneyMeasures",
        spec=MONEY_MEASURES,
        fetcher_type=FederalReserveMoneyMeasuresFetcher,
        header=MONEY_MEASURES_HEADER,
        rows=MONEY_MEASURES_ROWS,
        first_row=MONEY_MEASURES_1959,
        last_row=MONEY_MEASURES_2024,
        required="month",
        preamble=H6_PREAMBLE,
        renamed_header=H6_RENAMED_HEADER,
        query=H6_QUERY,
    ),
    Case(
        name="TreasuryRates",
        spec=TREASURY_RATES,
        fetcher_type=FederalReserveTreasuryRatesFetcher,
        header=H15_HEADER,
        rows=H15_ROWS,
        first_row=H15_1962,
        last_row=H15_2024,
        required="date",
        preamble=H15_PREAMBLE,
        renamed_header=H15_RENAMED_HEADER,
        query=H15_QUERY,
    ),
)

every_case = pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])


def body(*lines: str) -> bytes:
    """Join lines with the CRLF the download publishes."""
    return "\r\n".join(lines).encode("utf-8")


def served(case: Case, table: bytes) -> tuple[Fetcher[Any, Any], FixedResponseTransport]:
    """Wire one generated fetcher class to a canned response and return it with its recorder."""
    transport = FixedResponseTransport(HttpResponse(200, None, payload=table))
    bound = type(case.fetcher_type.__name__, (case.fetcher_type,), {"http_transport": transport})
    return bound(), transport


def rows_of(fetcher: Fetcher[Any, Any], spec: ModelSpec, **kwargs: object) -> tuple[Any, ...]:
    """Fetch through an offline grant and return the normalized rows."""
    return tuple(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), **kwargs))


def code_raised(fetcher: Fetcher[Any, Any], spec: ModelSpec, **kwargs: object) -> str:
    """Return the stable code a failing fetch reported."""
    with pytest.raises(ProviderEngineError) as raised:
        rows_of(fetcher, spec, **kwargs)
    return str(raised.value.code)


class TestDeclarationIsTheContract:
    """The two rows the census blocked on ``csv_decoder`` are declared CSV now."""

    @every_case
    def test_the_body_decoder_is_declared_in_production(self, case: Case) -> None:
        """The fact this package exists to establish, asserted per model."""
        spec = case.spec
        assert spec.decoder.kind == "csv"
        assert spec.decoder.delimited is True
        assert spec.decoder.effective_delimiter == ","
        assert spec.decoder.preamble_rows == len(case.preamble_lines) == 5
        assert spec.rows_pointer == "", "a delimited body is the record list itself"
        assert spec.pagination.kind == "none", "one package download is one send"
        assert spec.error_prefix == "FEDERAL_RESERVE"
        assert spec.scenario.strip()
        assert spec.credential is None

    @every_case
    def test_the_request_is_the_one_upstream_composes(self, case: Case) -> None:
        """Same origin, same path, same keys, same values: the declared constants are upstream's.

        Two faces of one claim. ``case.query`` is transcribed from the URL literal the oracle
        inlines, and :attr:`ModelSpec.static_query` is what the declaration sends, so the assertion
        that they are equal is what makes the declaration answer for that literal.
        """
        fetcher, transport = served(case, case.shipped_package)
        rows_of(fetcher, case.spec)
        assert dict(case.spec.static_query) == case.query
        assert transport.calls[0]["url"] == DOWNLOAD_URL
        assert transport.calls[0]["params"] == case.query
        assert len(transport.calls) == 1

    @every_case
    def test_a_caller_cannot_re_address_the_release_it_is_shown(self, case: Case) -> None:
        """The boundary the constants exist for: ``series`` and ``filetype`` are not inputs.

        A model id is a reviewed promise about one endpoint. Were ``series`` a defaulted parameter,
        a caller could ask the same id for another series hash, or for ``filetype=json``, and the
        declaration's rows would no longer describe the bytes that came back.
        """
        query_fields = set(build_query_model(case.spec).model_fields)
        constants = dict(case.spec.static_query)
        assert {key for key, _ in case.spec.static_query}.isdisjoint(query_fields)
        for name in ("rel", "series", "filetype", "layout", "type", "label", "lastobs"):
            with pytest.raises(ValidationError):
                build_query_model(case.spec).model_validate({name: "x"})
        sent = encode_query(build_query_model(case.spec)(), case.spec)
        assert {key: sent[key] for key in constants} == constants

    @every_case
    def test_the_blank_window_keys_ride_only_when_a_caller_sets_them(self, case: Case) -> None:
        """``from=``/``to=`` are the release's own keys and upstream sends them empty.

        The declaration gives the two keys a typed meaning it cannot verify offline, so it sends
        them only when a caller asks for a window -- the difference is recorded in ``notes``.
        """
        fetcher, transport = served(case, case.shipped_package)
        rows_of(fetcher, case.spec, start_date=date(1962, 1, 2), end_date=date(2024, 6, 25))
        sent = transport.calls[0]["params"]
        assert sent["from"] == "1962-01-02"
        assert sent["to"] == "2024-06-25"
        assert encode_query(build_query_model(case.spec)(), case.spec).get("from") is None

    def test_the_provider_descriptor_binds_two_generated_fetchers(self) -> None:
        """Registration-ready: the descriptor loads each model out of its own module."""
        assert PROVIDER.source == SOURCE == "federal_reserve"
        assert PROVIDER.credentials == ()
        assert PROVIDER.website == "https://www.federalreserve.gov/"
        assert [binding.canonical_model_ids for binding in PROVIDER.fetcher_bindings] == [
            ("MoneyMeasures",),
            ("TreasuryRates",),
        ]
        assert [type(fetcher).__name__ for fetcher in PROVIDER.fetchers] == [
            "FederalReserveMoneyMeasuresFetcher",
            "FederalReserveTreasuryRatesFetcher",
        ]
        assert [fetcher.canonical_model for fetcher in PROVIDER.fetchers] == [
            "MoneyMeasures",
            "TreasuryRates",
        ]
        assert {capability.domain for capability in PROVIDER.capabilities} == {
            "federal_reserve_money_measures",
            "federal_reserve_treasury_rates",
        }
        assert all(capability.source == SOURCE for capability in PROVIDER.capabilities)
        assert all(not capability.verified for capability in PROVIDER.capabilities)
        assert PROVIDER.health_check().status == "ready"

    def test_the_retracted_model_leaves_no_half_declaration_behind(self) -> None:
        """A retraction is only honest if the id cannot still be reached from any face.

        ``YieldCurve`` was withdrawn because the oracle's model is the long melt of this table, not
        the table itself. A leftover module, binding, spec or gap row would keep the id addressable
        while answering with the wrong shape, so each of those is read here.
        """
        declared = (
            Path(__file__).resolve().parents[1]
            / "opendata"
            / "data"
            / "providers"
            / "federal_reserve"
            / "models"
        )
        assert sorted(path.name for path in declared.glob("*.py")) == [
            "__init__.py",
            "money_measures.py",
            "treasury_rates.py",
        ]
        assert {fetcher.canonical_model for fetcher in PROVIDER.fetchers} == {
            "MoneyMeasures",
            "TreasuryRates",
        }
        assert not hasattr(specs, "YIELD_CURVE"), "the spec object must be gone, not renamed"
        assert "YieldCurve" not in DECLARED_GAPS
        reason = NOT_DECLARED["YieldCurve"]
        assert "melt" in reason and "yield_curve.py" in reason
        assert PROVIDER.source == SOURCE

    def test_each_declared_model_records_what_it_cannot_do(self) -> None:
        """A declaration is not a finished capability: the gaps are named in two places."""
        models = {case.name for case in CASES}
        assert models == set(DECLARED_GAPS) == {"MoneyMeasures", "TreasuryRates"}
        assert all(gap.strip() for gap in DECLARED_GAPS.values())
        for spec in (MONEY_MEASURES, TREASURY_RATES):
            assert "header=5" in spec.notes, spec.model
            assert "preamble_rows=5" in spec.notes, spec.model
        # The gap closed this round must not still be listed as open.
        assert all("metadata rows" not in gap for gap in DECLARED_GAPS.values())
        assert len(NOT_DECLARED) == 11
        assert set(NOT_DECLARED).isdisjoint(models)
        assert len(NOT_DECLARED) + len(models) == 13


class TestRowsByValue:
    """The published table, row by row, against the capture's own cells."""

    @every_case
    def test_the_table_becomes_the_declared_rows(self, case: Case) -> None:
        """Every emitted row is named by value, first and last, not merely non-empty."""
        fetcher, _ = served(case, case.shipped_package)
        emitted = rows_of(fetcher, case.spec)
        assert len(emitted) == len(case.lines)
        assert emitted[0].model_dump() == case.first_row
        assert emitted[-1].model_dump() == case.last_row

    def test_the_declared_eight_columns_come_out_of_a_thirty_column_row(self) -> None:
        """H.6 keeps 8 of the 30 columns it is handed, and the rest never reach a row."""
        money_measures = CASES[0]
        fetcher, _ = served(money_measures, money_measures.shipped_package)
        first, second = rows_of(fetcher, MONEY_MEASURES)
        assert set(first.model_dump()) == {column.name for column in MONEY_MEASURES.columns}
        assert first.model_dump() == MONEY_MEASURES_1959
        assert second.model_dump() == MONEY_MEASURES_2024

    def test_blank_cells_are_nulls_and_nd_is_a_value(self) -> None:
        """Empty cells are the engine's nulls; the source's sentinel survives as text."""
        treasury_rates = CASES[1]
        fetcher, _ = served(treasury_rates, treasury_rates.shipped_package)
        emitted = rows_of(fetcher, TREASURY_RATES)
        assert emitted[0].model_dump() == H15_1962
        assert emitted[1].model_dump() == H15_ND
        assert emitted[1].year_1 == "ND"
        assert emitted[1].month_1 is None

    def test_the_h15_download_is_read_once_and_stays_wide(self) -> None:
        """One GET, one row per published row: the long triple is not something we re-emit.

        The oracle's ``YieldCurve`` melts these eleven maturity columns into ``(date, maturity,
        rate)`` rows, so a declaration that kept both shapes would answer two model ids with one
        table. The retraction is asserted here by absence: no long-format column exists on the only
        model that reads this download.
        """
        declared = [column.name for column in TREASURY_RATES.columns]
        assert {"maturity", "rate", "maturity_years"}.isdisjoint(declared)
        treasury_rates = CASES[1]
        fetcher, _ = served(treasury_rates, treasury_rates.shipped_package)
        emitted = rows_of(fetcher, TREASURY_RATES)
        assert len(emitted) == len(treasury_rates.lines)
        assert emitted[-1].model_dump() == H15_2024


class TestCounterfacts:
    """A wrong reading of a real body is named, never quietly nulled."""

    @every_case
    def test_a_tab_separated_body_is_a_delimiter_mismatch(self, case: Case) -> None:
        """One tab-for-comma mistranslation must not read as a single-column table."""
        tabbed = case.packaged(
            case.header.replace(",", "\t"),
            *(line.replace(",", "\t") for line in case.lines),
        )
        fetcher, _ = served(case, tabbed)
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_DELIMITER_MISMATCH"
        assert raised.value.model == case.spec.model
        assert "produced 1 column" in str(raised.value)

    @every_case
    def test_a_header_naming_none_of_the_declared_columns_refuses(self, case: Case) -> None:
        """The friendly labels are not the ids this declaration reads, so it says so loudly."""
        fetcher, _ = served(case, case.packaged(case.renamed_header, *case.lines))
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_HEADER_MISMATCH"
        assert raised.value.model == case.spec.model
        assert raised.value.decoder == "csv"
        assert "publishes none of the declared columns" in str(raised.value)

    @every_case
    def test_a_renamed_required_column_is_named_not_a_silent_null(self, case: Case) -> None:
        """Only the period cell is renamed: the ids still match, so the row must be refused."""
        renamed = case.header.replace("Time Period", "Publication Date", 1)
        fetcher, _ = served(case, case.packaged(renamed, *case.lines))
        with pytest.raises(ProviderEngineError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_SHAPE_INVALID"
        assert list(raised.value.missing) == [case.required]

    @every_case
    def test_the_declared_preamble_is_the_row_that_makes_the_header_the_header(
        self, case: Case
    ) -> None:
        """The positive control for the skip: the same bytes with the count are the table.

        Asserted against :meth:`test_a_package_body_without_its_declared_preamble_is_refused`
        below, which feeds bytes differing only in those five rows.
        """
        fetcher, _ = served(case, case.shipped_package)
        emitted = rows_of(fetcher, case.spec)
        assert len(emitted) == len(case.lines)
        assert emitted[0].model_dump() == case.first_row

    @every_case
    def test_a_package_body_without_its_declared_preamble_is_refused(self, case: Case) -> None:
        """A table that starts at its header is not the body this declaration describes."""
        fetcher, _ = served(case, case.table)
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_EMPTY_TABLE"
        # The whole table is inside the skip, and the refusal says how many rows against how many.
        assert f"a body of {len(case.lines) + 1} row(s) with a declared preamble of 5" in (
            raised.value.saw
        )

    @every_case
    def test_a_body_of_only_its_preamble_rows_leaves_no_header(self, case: Case) -> None:
        """Five metadata rows and nothing after them is a refusal at the boundary, not a zero."""
        fetcher, _ = served(case, body(*case.preamble_lines))
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_EMPTY_TABLE"
        assert "leaves no header row" in raised.value.saw

    @every_case
    @pytest.mark.parametrize(
        ("count", "code"),
        [
            # Four rows skip the header along with the preamble, so a data row is asked to name the
            # columns -- and a published row has blank cells, which is an unusable header.
            (4, "FEDERAL_RESERVE_HEADER_INVALID"),
            # Six leaves the first metadata row standing where the header should be.
            (6, "FEDERAL_RESERVE_HEADER_MISMATCH"),
        ],
    )
    def test_a_preamble_miscounted_by_one_row_is_a_refusal_not_a_short_table(
        self, case: Case, count: int, code: str
    ) -> None:
        """The count is load-bearing: off by one, some other row stands as the header."""
        landed = case.lines[0] if count < 5 else case.preamble_lines[0]
        fetcher, _ = served(case, case.packaged_with_preamble(count))
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(fetcher, case.spec)
        assert raised.value.code == code
        assert f"['{landed.split(',')[0]}'" in raised.value.saw

    @every_case
    def test_a_json_document_cannot_answer_a_csv_declaration(self, case: Case) -> None:
        """The transport must hand bytes; a parsed document is a wiring fault, not an answer."""
        transport = FixedResponseTransport(HttpResponse(200, [{"Time Period": "2024-05"}]))
        fetcher = type(
            case.fetcher_type.__name__, (case.fetcher_type,), {"http_transport": transport}
        )()
        assert code_raised(fetcher, case.spec) == "FEDERAL_RESERVE_PAYLOAD_MISSING"

    @every_case
    def test_an_empty_body_and_a_headerless_table_are_refusals(self, case: Case) -> None:
        """Neither a body of nothing nor a header with no rows becomes a silent zero-row result."""
        empty_fetcher, _ = served(case, b"")
        assert code_raised(empty_fetcher, case.spec) == "FEDERAL_RESERVE_EMPTY_BODY"
        header_fetcher, _ = served(case, case.packaged(case.header))
        with pytest.raises(ResponseDecodeError) as raised:
            rows_of(header_fetcher, case.spec)
        assert raised.value.code == "FEDERAL_RESERVE_EMPTY_TABLE"
        assert "0 data row(s)" in str(raised.value)
