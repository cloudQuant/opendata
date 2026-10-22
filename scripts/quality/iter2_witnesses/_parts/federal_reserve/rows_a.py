"""Wave 8 shard: the eight federal_reserve rows that read workbook, CSV, SOMA JSON or HTML carriers.

The base module withheld these rows because of the *body* its seam could hand over: the probe's
engine path answers with ``HttpResponse(200, document)``, whose :attr:`~HttpResponse.payload` is
always ``None``, and every one of these three declarations is delimited
(``DecoderSpec(kind="csv")`` for H.6, H.15 and the FEDS wide table), so a delimited declaration
answered :attr:`payload` with ``FEDERAL_RESERVE_PAYLOAD_MISSING`` -- the fixture's missing bytes,
not a contract refusal. Wave 8's ``client_seam`` capability closes exactly that: the engine's own
production transport (:func:`opendata.data.providers._engine.http_json._http_get_json`) resolves
``get_shared_http_client`` from :mod:`opendata.data.http_client` *inside the request method*, so
patching that one module attribute routes the real transport through the fixture, and a delimited
declaration gets a genuine ``response.content``. The declared seam is therefore
``opendata.data.http_client`` for the three engine rows and
``opendata.data.providers.federal_reserve._iter2_client`` for the four hand-written rows, which
read the same attribute at module level. Nothing here replaces a fetch stage: the shipped decoders,
value transforms, row models and refusal sites are the code under test, and no leg can open a
socket (the runner blocks sockets by construction).

Every ``expected_*`` face below was read off the *contract*, not off a run:

* ``TreasuryRates`` -- the frozen H.15 row model
  (:mod:`opendata.data.models.federal_reserve_clean_rates`) makes every tenor a
  ``float | None`` and the declaration spells the published missing-data token ``ND`` as a
  ``null_sentinel``, so the only thing that can reach a tenor cell is decimal text. The tamper
  rewrites one data row's ``RIFLGFCY10_N.B`` cell to non-numeric text; the engine builds the row
  model, pydantic refuses the field, and the shipped attribution is
  ``FEDERAL_RESERVE_SHAPE_INVALID`` with ``rejected=(column name,)`` -- the contract field name
  ``year_10``, which is what this entry declares. (``http_json.py:790-796``.)
* ``MoneyMeasures`` -- ``m1``/``m2`` are declared ``required=True, nullable=False`` precisely
  because the contract makes them non-nullable floats and no sentinel is declarable on them, so a
  corrupt ``M2_N.M`` cell must be refused as ``rejected=('m2',)`` rather than nulled.
* ``SvenssonYieldCurve`` -- all 99 series fields of the reviewed row are ``float | None`` and the
  sentinel list spells only the four published texts, so any other text in a ``SVENY10`` cell is
  outside the documented shape and refuses as ``rejected=('sveny10',)``.
* ``CentralBankHoldings``, ``InflationExpectations``, ``TotalFactorProductivity`` -- these refuse
  through this provider's own typed, code-bearing error ``FederalReserveSourceError`` (a
  ``ProviderEngineError`` subclass with a ``status`` face), which is code-only: the numeric guards
  :func:`parse_decimal` / :func:`parse_float` deliberately do not attribute a column, so all
  three attribution faces stay empty, exactly as branch B requires.
* ``YieldCurve`` -- the same code-only shape, and the row wave 28 retargeted onto its own contract:
  :file:`models/yield_curve.py` now builds the class ``domains.yaml`` names for the domain,
  ``FederalReserveYieldCurveObservation``, whose ``rate`` is a ``float | None`` reachable only
  through the published ``ND`` token or a blank cell, so prose in a ``RIFLGFCY10_N.B`` cell refuses
  as ``FEDERAL_RESERVE_BAD_NUMERIC_VALUE`` and names no column.

All eight assigned rows are witnessed here. ``FomcDocuments`` was the row wave 8 withheld on the
provider's look-alike contract defect: ``models/fomc_documents.py`` built
``opendata.data.models.federal_reserve_macro.FederalReserveFomcDocument`` while
``contract_model('federal_reserve_fomc_documents')`` resolves to
``opendata.data.models.federal_reserve_clean_documents.FederalReserveFomcDocument`` -- same name,
different module, different fields -- so declaring either class as ``row_model`` either tripped
``tests/test_federal_reserve_iter2_models.py:392-413`` or hid the defect. Wave 31 step 4 settled the
published face (ruling #68: the five fields ``domains.yaml`` declares, not the macro class's seven)
and retargeted the reader onto it, which is what lets this entry exist. Its rows are therefore
``date``/``doc_type``/``doc_format``/``url`` plus a ``release_date`` this index page does not state;
the year, title and meeting heading the look-alike carried are classification input, not output.
Faces: ``docs/evidence/C133/wave31-step4-federal-reserve-fomc-documents-retarget-20261018/``.

``YieldCurve`` was the second row withheld this way and no longer is. The wording above used to call
it a same-name pair like ``FomcDocuments``; the instrument measured the opposite -- the look-alike
``federal_reserve_macro.FederalReserveYieldCurve`` and the contract
``federal_reserve_clean_documents.FederalReserveYieldCurveObservation`` differ in name
(``names_equal=False``) and agree only on their field face (``field_face_equal=True``, ``date`` /
``maturity`` / ``rate`` in both). Retargeting the reader to the declared contract is what let the
frozen guard ``tests/test_federal_reserve_iter2_models.py:392-413`` hold for it, and the guard's
discriminating power was shown by forging the look-alike back into ``row_model`` in memory: the
check goes false and restores true. See
``docs/evidence/C128/plane1-residue-readiness-20261017/probes/fed_h15_official_shape_price.py``.

Witnesses run exclusively through the offline seam. No leg touches the network and no
``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any, Final

from openpyxl import Workbook

from opendata.data.providers.federal_reserve.models.total_factor_productivity import (
    _HEADER as _TFP_HEADER,
)
from opendata.data.providers.federal_reserve.models.yield_curve import (
    _HEADER as _H15_LONG_HEADER,
)
from opendata.data.providers.federal_reserve.specs import SVENSSON_SERIES_COLUMNS

_H15_SERIES: Final[tuple[tuple[str, str], ...]] = (
    ("RIFSGFSW04_N.B", "week_4"),
    ("RIFLGFCM01_N.B", "month_1"),
    ("RIFLGFCM03_N.B", "month_3"),
    ("RIFLGFCM06_N.B", "month_6"),
    ("RIFLGFCY01_N.B", "year_1"),
    ("RIFLGFCY02_N.B", "year_2"),
    ("RIFLGFCY03_N.B", "year_3"),
    ("RIFLGFCY05_N.B", "year_5"),
    ("RIFLGFCY07_N.B", "year_7"),
    ("RIFLGFCY10_N.B", "year_10"),
    ("RIFLGFCY20_N.B", "year_20"),
    ("RIFLGFCY30_N.B", "year_30"),
)
_H6_SERIES: Final[tuple[tuple[str, str], ...]] = (
    ("M1_N.M", "m1"),
    ("M2_N.M", "m2"),
    ("MCU_N.M", "currency"),
    ("MDD_N.M", "demand_deposits"),
    ("MMFGB_N.M", "retail_money_market_funds"),
    ("MDL_N.M", "other_liquid_deposits"),
    ("MDTS_N.M", "small_denomination_time_deposits"),
)
#: The five metadata rows an ``Output.aspx`` ``type=package`` body prints ahead of its header --
#: the count the H.6 and H.15 declarations carry as ``preamble_rows=5`` and the labels the pinned
#: H.15 long-form reader compares, in order.
_DDP_PREAMBLE: Final[tuple[str, ...]] = (
    "Series Description",
    "Unit:            ,Percent per year",
    "Multiplier:    ,1.000000",
    "Currency:    ,USD",
    "Unique Identifier:    ,FED",
)

#: The 99 published series tokens, read from the declaration itself so the witness header can never
#: drift from the columns the model spec declares.
SVENSSON_SERIES_KEYS: Final[tuple[str, ...]] = tuple(token for _, token in SVENSSON_SERIES_COLUMNS)
SVENSSON_FIELD_BY_TOKEN: Final[dict[str, str]] = dict(SVENSSON_SERIES_COLUMNS)


def _ddp_body(header: tuple[str, ...], rows: tuple[str, ...]) -> list[str]:
    """One release package body: five metadata rows, the published header, then the data rows."""
    return [*_DDP_PREAMBLE, ",".join(header), *rows]


#: Two H.15 business days in the published percent scale: 13 cells, ``Time Period`` plus the
#: twelve series ids the declaration reads (one of which, ``month_2``, no package publishes).
#: The first day leaves ``week_4`` blank and the second prints the token ``ND`` for ``month_3`` --
#: both are the frozen float-plus-``null_sentinel`` reading the H.15 ruling applied, so they reach
#: the contract's ``float | None`` fields as ``None`` instead of refusing.
_TREASURY_RATES_ROWS: Final[tuple[str, ...]] = (
    "2024-06-24,,5.37,5.36,5.34,5.30,5.24,5.21,5.09,4.92,4.83,4.63,4.39",
    "2024-06-25,4.38,5.36,ND,5.33,5.29,5.23,5.20,5.08,4.91,4.82,4.61,4.38",
)
#: The zero-based cell index of the 10-year series inside an H.15 data row (cell 0 is the date).
_H15_YEAR_10_CELL: Final[int] = 1 + [series for series, _ in _H15_SERIES].index("RIFLGFCY10_N.B")


def _fed_treasury_rates_envelope() -> dict[str, Any]:
    """The H.15 daily package body the TreasuryRates declaration decodes."""
    return {
        "responses": [
            {
                "url_contains": "datadownload/Output.aspx",
                "content_type": "text/csv",
                "body": _ddp_body(
                    ("Time Period", *(series for series, _ in _H15_SERIES)),
                    _TREASURY_RATES_ROWS,
                ),
            }
        ]
    }


def _treasury_rates_first_row(year_10: str) -> str:
    """The first H.15 row with its 10-year cell replaced (the counterfact's tampered line)."""
    cells = list(_TREASURY_RATES_ROWS[0].split(","))
    cells[_H15_YEAR_10_CELL] = year_10
    return ",".join(cells)


#: The five metadata rows a released H.15 ``type=package`` body prints ahead of its table. Only
#: each line's first cell is compared, and the fifth is published padded -- ``Unique Identifier: ``
#: with a trailing space before its comma -- which is the byte the long-form reader used to refuse
#: and now strips before comparing.
_YIELD_CURVE_PREAMBLE: Final[tuple[str, ...]] = (
    "Series Description,Market yield on U.S. Treasury securities at constant maturity",
    "Unit:,Percent:_Per_Year",
    "Multiplier:,1",
    "Currency:,NA",
    "Unique Identifier: ,H15/H15/RIFLGFCM01_N.B",
)
#: The first two rows are the 2026-10-07 and 2026-10-08 observations of the eleven-series package
#: exactly as the official capture publishes them; the third row adds the release's two null
#: spellings for a tenor it does not observe or has not yet revised (``ND`` and a blank), both of
#: which the retargeted reader has to land as ``rate=None`` on the contract instead of refusing.
_YIELD_CURVE_ROWS: Final[tuple[str, ...]] = (
    "2026-10-07,4.07,4.22,4.28,4.42,4.77,4.87,5.03,5.15,5.28,5.71,5.67",
    "2026-10-08,4.14,4.23,4.30,4.44,4.75,4.85,4.99,5.11,5.22,5.64,5.60",
    "2026-10-09,4.11,,4.26,4.40,4.73,4.83,4.97,5.09,5.20,ND,5.58",
)
#: The zero-based cell index of the 10-year series in a long-form row. Cell 0 is the date and header
#: cell 0 is ``Time Period``, so the header position is the cell position -- no offset.
_H15_LONG_YEAR_10_CELL: Final[int] = _H15_LONG_HEADER.index("RIFLGFCY10_N.B")


def _fed_yield_curve_envelope() -> dict[str, Any]:
    """The H.15 long-form package body the pinned reader expands to date/maturity/rate rows."""
    return {
        "responses": [
            {
                "url_contains": "rel=H15",
                "content_type": "text/csv",
                "body": [*_YIELD_CURVE_PREAMBLE, ",".join(_H15_LONG_HEADER), *_YIELD_CURVE_ROWS],
            }
        ]
    }


def _yield_curve_first_row(year_10: str) -> str:
    """The first long-form row with its ``RIFLGFCY10_N.B`` cell replaced."""
    cells = list(_YIELD_CURVE_ROWS[0].split(","))
    cells[_H15_LONG_YEAR_10_CELL] = year_10
    return ",".join(cells)


def _fed_money_measures_envelope() -> dict[str, Any]:
    """Two H.6 monthly observations in the ``_N.M`` (not seasonally adjusted) family.

    ``Time Period`` is published as ``%Y-%m`` and the declaration's ``CastSpec.date_string`` reads
    it as the first of that month; ``MCU_N.M`` prints the release's ``ND`` token for a component
    the Board does not publish that month, which the H.6 sentinel ruling keeps as a null.
    """
    return {
        "responses": [
            {
                "url_contains": "datadownload/Output.aspx",
                "content_type": "text/csv",
                "body": _ddp_body(
                    ("Time Period", *(series for series, _ in _H6_SERIES)),
                    (
                        "2024-04,17916.3,208680.4,4321.5,14491.2,ND,55210.7,41880.3",
                        "2024-05,17625.9,208140.6,4310.8,14330.5,130.4,55102.1,41799.8",
                    ),
                ),
            }
        ]
    }


def _money_measures_first_row(m2: str) -> str:
    """The first H.6 row with its ``M2_N.M`` cell replaced."""
    return ",".join(
        ("2024-04", "17916.3", m2, "4321.5", "14491.2", "ND", "55210.7", "41880.3"),
    )


def _svensson_row(timestamp: str, *, blanks: tuple[int, ...] = ()) -> str:
    """One FEDS wide-table row: the ``Date`` cell plus one cell per published series token."""
    cells = [timestamp]
    cells.extend(
        "" if index in blanks else f"{2.0 + index / 100:.4f}"
        for index in range(len(SVENSSON_SERIES_KEYS))
    )
    return ",".join(cells)


def _svensson_corrupt_row(bad_field: str, bad_value: str) -> str:
    """One wide row identical to the clean first row except for one series cell."""
    offset = SVENSSON_SERIES_KEYS.index(SVENSSON_FIELD_BY_TOKEN[bad_field])
    cells = _svensson_row("2024-06-24").split(",")
    cells[offset + 1] = bad_value
    return ",".join(cells)


def _fed_svensson_yield_curve_envelope() -> dict[str, Any]:
    """Two observation dates across all 99 published series of ``feds200628.csv``.

    The body has no preamble (``DecoderSpec(kind="csv")`` with ``preamble_rows=0``) and its header
    prints ``Date`` plus every series token, because ``WideTablePivotPlan`` requires each wide row
    to publish the timestamp key and every declared series key. The second row leaves the ``TAU2``
    and ``SVENF01`` cells blank -- the published spelling the declaration's sentinel set
    ``("", "NA", "ND", "N/A")`` was written to absorb.
    """
    return {
        "responses": [
            {
                "url_contains": "feds200628.csv",
                "content_type": "text/csv",
                "body": [
                    ",".join(("Date", *SVENSSON_SERIES_KEYS)),
                    _svensson_row("2024-06-24"),
                    _svensson_row("2024-06-25", blanks=(5, 8)),
                ],
            }
        ]
    }


# The quarterly tab's header is imported from the reader itself: it compares the tuple verbatim, so
# a witness that retyped it would silently drift from the release it claims to mimic.
_TFP_DTFP_INDEX: Final[int] = _TFP_HEADER.index("dtfp")


def _workbook_base64(sheets: dict[str, list[tuple[object, ...]]]) -> str:
    """Serialize one in-memory workbook exactly the way the official release tab is published."""
    workbook = Workbook()
    default = workbook.active
    for position, (name, rows) in enumerate(sheets.items()):
        worksheet = default if position == 0 else workbook.create_sheet()
        worksheet.title = name
        for row in rows:
            worksheet.append(list(row))
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _tfp_worksheet(dtfp_cell: object = None) -> list[tuple[object, ...]]:
    """The ``quarterly`` tab: its units note, its exact header, then two ascending quarters."""
    width = len(_TFP_HEADER) - 1
    first: list[object] = ["2019:Q1"] + [round(1.0 + index / 50, 4) for index in range(width)]
    second: list[object] = ["2019:Q2"] + [round(2.0 + index / 50, 4) for index in range(width)]
    if dtfp_cell is not None:
        first[_TFP_DTFP_INDEX] = dtfp_cell
    return [
        ("Values are annualized, expressed as an annual rate of percent change.",),
        _TFP_HEADER,
        tuple(first),
        tuple(second),
    ]


def _inflation_worksheet(infpgdp1yr_cell: object = None) -> list[tuple[object, ...]]:
    """The ``INFLATION`` tab: the five published headers, then two ascending SPF quarters."""
    first: list[object] = [2024, 1, 2.30, 2.40, 2.50]
    if infpgdp1yr_cell is not None:
        first[2] = infpgdp1yr_cell
    return [
        ("YEAR", "QUARTER", "INFPGDP1YR", "INFCPI1YR", "INFCPI10YR"),
        tuple(first),
        (2024, 2, 2.35, 2.45, "#N/A"),
    ]


def _fed_inflation_expectations_envelope() -> dict[str, Any]:
    """The SPF ``Inflation.xlsx`` carrier: one ``INFLATION`` sheet, the five published headers."""
    return {
        "responses": [
            {
                "url_contains": "Inflation.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "body_b64": _workbook_base64({"INFLATION": _inflation_worksheet()}),
            }
        ]
    }


def _fed_total_factor_productivity_envelope() -> dict[str, Any]:
    """The FRBSF ``quarterly_tfp.xlsx`` carrier: a readme vintage line plus the quarterly tab."""
    return {
        "responses": [
            {
                "url_contains": "quarterly_tfp.xlsx",
                "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "body_b64": _workbook_base64(
                    {
                        "readme": [
                            ("SF Fed TFP data",),
                            ("Data produced on 2024-07-15",),
                        ],
                        "quarterly": _tfp_worksheet(),
                    }
                ),
            }
        ]
    }


def _soma_summary_item(as_of_date: str, total: object) -> dict[str, object]:
    """One NY Fed SOMA ``summary`` row: the ten keys the reader requires, in published dollars."""
    return {
        "asOfDate": as_of_date,
        "mbs": "2180000.00",
        "cmbs": "60000.00",
        "tips": "1900000.00",
        "frn": "350000.00",
        "tipsInflationCompensation": "41000.00",
        "notesbonds": "3200000.00",
        "bills": "410000.00",
        "agencies": "0.00",
        "total": total,
    }


def _fed_central_bank_holdings_envelope() -> dict[str, Any]:
    """Two weekly SOMA summary rows in published dollars, answered through the governed seam.

    ``summary=True`` is the one selector of this route that answers in a single GET, so the
    witness declares exactly one page; the reader converts each published dollar amount to the
    contract's ``$Thousands`` scale with an exact ``Decimal`` divide.
    """
    return {
        "responses": [
            {
                "url_contains": "/soma/summary.json",
                "body": {
                    "soma": {
                        "summary": [
                            _soma_summary_item("2024-06-12", "8230000.00"),
                            _soma_summary_item("2024-06-19", "8215000.00"),
                        ]
                    }
                },
            }
        ]
    }


#: The official statement-anchor shape and the document date it publishes. Line 2 of the index slice
#: below is this anchor, so the tamper rewrites the cell the reader's date guard parses.
_FOMC_STATEMENT_ANCHOR: Final[str] = (
    '<a href="/monetarypolicy/files/FOMC{date}statement.pdf">Statement for New Release</a>'
)
_FOMC_DOCUMENT_DATE: Final[str] = "20240612"
#: A slice of the Board's published meeting index: one heading, two date-addressable document links,
#: and one year page whose ``fomchistorical2024`` token carries no 8-digit document date, so the
#: reader's date filter has to drop it.
_FOMC_INDEX_LINES: Final[tuple[str, ...]] = (
    "<html>",
    "<h5>June 11-12 Meeting - 2024</h5>",
    _FOMC_STATEMENT_ANCHOR.format(date=_FOMC_DOCUMENT_DATE),
    '<a href="/monetarypolicy/files/FOMC20240612minutes.pdf">Minutes</a>',
    '<a href="/monetarypolicy/fomchistorical2024.htm">2024 meeting archive</a>',
    "</html>",
)


def _fed_fomc_documents_envelope() -> dict[str, Any]:
    """The FOMC index page the bound reader parses into ``date``/``doc_type``/``doc_format``."""
    return {
        "responses": [
            {
                "url_contains": "fomccalendars.htm",
                "content_type": "text/html",
                "body": list(_FOMC_INDEX_LINES),
            }
        ]
    }


def _fomc_statement_link(document_date: str) -> str:
    """The statement anchor with its official URL's 8-digit document date replaced."""
    return _FOMC_STATEMENT_ANCHOR.format(date=document_date)


_HTTP_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.http_client"],
    "attr": "get_shared_http_client",
}
_ITER2_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.federal_reserve._iter2_client"],
    "attr": "get_shared_http_client",
}

_SPECS: Final[str] = "opendata/data/providers/federal_reserve/specs.py"
_DECODERS: Final[str] = "opendata/data/providers/_engine/decoders.py"
_ROW_EXPANSION: Final[str] = "opendata/data/providers/_engine/row_expansion.py"
_ENGINE: Final[str] = "opendata/data/providers/_engine/http_json.py"
_HTTP_CLIENT: Final[str] = "opendata/data/http_client.py"
_CLEAN_RATES: Final[str] = "opendata/data/models/federal_reserve_clean_rates.py"
_MACRO: Final[str] = "opendata/data/models/federal_reserve_macro.py"
_CLEAN_DOCUMENTS: Final[str] = "opendata/data/models/federal_reserve_clean_documents.py"
_ITER2_CLIENT: Final[str] = "opendata/data/providers/federal_reserve/_iter2_client.py"
_ITER2_COMMON: Final[str] = "opendata/data/providers/federal_reserve/models/_iter2_common.py"
_DOMAINS: Final[str] = "opendata/data/domains.yaml"

MODELS: Final[dict[str, dict[str, Any]]] = {
    "TreasuryRates": {
        "client_seam": _HTTP_CLIENT_SEAM,
        "fixture_document": _fed_treasury_rates_envelope,
        "query": {"start_date": "2024-06-01", "end_date": "2024-06-30"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 6],
            "value": _treasury_rates_first_row("ten point two"),
            "column": "year_10",
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
        "expected_rejected": ["year_10"],
        "tamper_reason": (
            "the frozen H.15 row model FederalReserveTreasuryRate makes year_10 a float | None and "
            "the declaration spells only the published token ND (plus a blank) as its "
            "null_sentinel, so a tenor cell of 'ten point two' is not a rate the contract can "
            "carry; the engine builds the row model and surfaces pydantic's field name as "
            "FEDERAL_RESERVE_SHAPE_INVALID rejected=('year_10',) instead of letting one "
            "business day's 10-year yield disappear into an invented number"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/treasury_rates.py",
            _SPECS,
            _DECODERS,
            _ENGINE,
            _HTTP_CLIENT,
        ],
        "contract_files": [_CLEAN_RATES, _DOMAINS],
    },
    "MoneyMeasures": {
        "client_seam": _HTTP_CLIENT_SEAM,
        "fixture_document": _fed_money_measures_envelope,
        "query": {"start_date": "2024-04-01", "end_date": "2024-05-31"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 6],
            "value": _money_measures_first_row("not-a-number"),
            "column": "m2",
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
        "expected_rejected": ["m2"],
        "tamper_reason": (
            "FederalReserveMoneyMeasure.m2 is a required non-nullable float and the H.6 "
            "declaration therefore carries NO null sentinel on m1/m2 (a sentinel there would "
            "rewrite a total the contract refuses to leave null), so a corrupt M2_N.M cell must "
            "refuse as FEDERAL_RESERVE_SHAPE_INVALID rejected=('m2',) rather than publish a money "
            "stock month with an invented M2"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/money_measures.py",
            _SPECS,
            _DECODERS,
            _ENGINE,
            _HTTP_CLIENT,
        ],
        "contract_files": [_CLEAN_RATES, _DOMAINS],
    },
    "SvenssonYieldCurve": {
        "client_seam": _HTTP_CLIENT_SEAM,
        "fixture_document": _fed_svensson_yield_curve_envelope,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 1],
            "value": _svensson_corrupt_row("sveny10", "N/A-ish"),
            "column": "sveny10",
        },
        "refusal_class": "ProviderEngineError",
        "refusal_code": "FEDERAL_RESERVE_SHAPE_INVALID",
        "expected_rejected": ["sveny10"],
        "tamper_reason": (
            "every one of the 99 series fields of FederalReserveSvenssonYieldCurve is a "
            "float | None and the declaration's sentinel list spells exactly the four published "
            "tokens '', 'NA', 'ND' and 'N/A', so a cell of 'N/A-ish' is a token the release does "
            "not publish: the pivot carries it through and the row model refuses it as "
            "FEDERAL_RESERVE_SHAPE_INVALID rejected=('sveny10',) naming the ten-year zero-coupon "
            "yield that was never fitted"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/svensson_yield_curve.py",
            _SPECS,
            _DECODERS,
            _ROW_EXPANSION,
            _ENGINE,
            _HTTP_CLIENT,
        ],
        "contract_files": [_CLEAN_RATES, _DOMAINS],
    },
    "CentralBankHoldings": {
        "client_seam": _ITER2_CLIENT_SEAM,
        "fixture_document": _fed_central_bank_holdings_envelope,
        "query": {"summary": True},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "soma", "summary", 0, "total"],
            "value": True,
            "column": "total",
        },
        "refusal_class": "FederalReserveSourceError",
        "refusal_code": "FEDERAL_RESERVE_BAD_NUMERIC_VALUE",
        "tamper_reason": (
            "the SOMA summary publishes total as a dollar amount and the reader converts it with "
            "parse_decimal into the contract's Decimal | None FederalReserveCentralBankHolding."
            "total; a JSON boolean is the one spelling that guard refuses outright "
            "(FEDERAL_RESERVE_BAD_NUMERIC_VALUE) rather than letting True become one thousand "
            "dollars of holdings, and the guard attributes no column so both faces stay empty"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/central_bank_holdings.py",
            _ITER2_CLIENT,
            _ITER2_COMMON,
        ],
        "contract_files": [_MACRO, _DOMAINS],
    },
    "InflationExpectations": {
        "client_seam": _ITER2_CLIENT_SEAM,
        "fixture_document": _fed_inflation_expectations_envelope,
        "query": {"start_date": "2024-01-01", "end_date": "2024-12-31"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _workbook_base64({"INFLATION": _inflation_worksheet("not-a-forecast")}),
            "column": "infpgdp1yr",
        },
        "refusal_class": "FederalReserveSourceError",
        "refusal_code": "FEDERAL_RESERVE_BAD_NUMERIC_VALUE",
        "tamper_reason": (
            "the SPF workbook keeps its own missing-value spelling '#N/A' and publishes a number "
            "everywhere else, and _forecast_value runs the other cells through "
            "parse_float(nullable=False) into FederalReserveInflationExpectations.infpgdp1yr; text "
            "that is neither the documented token nor a decimal has to refuse as "
            "FEDERAL_RESERVE_BAD_NUMERIC_VALUE instead of being dropped into the 2024 Q1 "
            "GDP-inflation forecast"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/inflation_expectations.py",
            _ITER2_CLIENT,
            _ITER2_COMMON,
        ],
        "contract_files": [_MACRO, _DOMAINS],
    },
    "TotalFactorProductivity": {
        "client_seam": _ITER2_CLIENT_SEAM,
        "fixture_document": _fed_total_factor_productivity_envelope,
        "query": {"start_period": "2019:Q1", "end_period": "2019:Q2"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body_b64"],
            "value": _workbook_base64(
                {
                    "readme": [
                        ("SF Fed TFP data",),
                        ("Data produced on 2024-07-15",),
                    ],
                    "quarterly": _tfp_worksheet("estimated"),
                }
            ),
            "column": "dtfp",
        },
        "refusal_class": "FederalReserveSourceError",
        "refusal_code": "FEDERAL_RESERVE_BAD_NUMERIC_VALUE",
        "tamper_reason": (
            "the quarterly tab publishes every factor column as an annualized percent number and "
            "the reader coerces each cell with parse_float before "
            "FederalReserveTotalFactorProductivity.model_validate, whose dtfp field is a float; a "
            "cell spelled 'estimated' is prose where the release prints a number, so the guard "
            "must refuse it as FEDERAL_RESERVE_BAD_NUMERIC_VALUE rather than publish a 2019 Q1 "
            "TFP growth reading that the source never measured"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/total_factor_productivity.py",
            _ITER2_CLIENT,
            _ITER2_COMMON,
        ],
        "contract_files": [_MACRO, _DOMAINS],
    },
    "YieldCurve": {
        "client_seam": _ITER2_CLIENT_SEAM,
        "fixture_document": _fed_yield_curve_envelope,
        "query": {"start_date": "2026-10-07", "end_date": "2026-10-09"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 6],
            "value": _yield_curve_first_row("five point two eight"),
            "column": "RIFLGFCY10_N.B",
        },
        "refusal_class": "FederalReserveSourceError",
        "refusal_code": "FEDERAL_RESERVE_BAD_NUMERIC_VALUE",
        "tamper_reason": (
            "the retargeted long-form reader sends every cell that is not the published token ND "
            "through parse_decimal(nullable=True) into "
            "FederalReserveYieldCurveObservation.rate, a float | None whose only null spellings "
            "are ND and a blank, so a 10-year cell spelled 'five point two eight' is prose where "
            "H.15 prints a number and must refuse as FEDERAL_RESERVE_BAD_NUMERIC_VALUE instead "
            "of publishing one business day's headline yield as an invented fraction; the guard "
            "attributes no column, so all three detail faces stay empty"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/yield_curve.py",
            _ITER2_CLIENT,
            _ITER2_COMMON,
        ],
        "contract_files": [_CLEAN_DOCUMENTS, _DOMAINS],
    },
    "FomcDocuments": {
        "client_seam": _ITER2_CLIENT_SEAM,
        "fixture_document": _fed_fomc_documents_envelope,
        "query": {"year": 2024},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 2],
            "value": _fomc_statement_link("20241340"),
            "column": "date",
        },
        "refusal_class": "FederalReserveSourceError",
        "refusal_code": "FEDERAL_RESERVE_BAD_FOMC_DOCUMENT_DATE",
        "tamper_reason": (
            "the retargeted index reader keeps only official ``/monetarypolicy/`` links whose URL "
            "carries an 8-digit date, spells the contract's ``date`` field from it and drops any "
            "link whose day does not exist, so a statement URL published as FOMC20241340 "
            "(month 13, day 40) is not a meeting the Board held and must refuse as "
            "FEDERAL_RESERVE_BAD_FOMC_DOCUMENT_DATE rather than publish a document row with an "
            "invented date; the guard attributes no column, so all three detail faces stay empty"
        ),
        "source_files": [
            "opendata/data/providers/federal_reserve/models/fomc_documents.py",
            _ITER2_CLIENT,
            _ITER2_COMMON,
        ],
        "contract_files": [_CLEAN_DOCUMENTS, _DOMAINS],
    },
}
