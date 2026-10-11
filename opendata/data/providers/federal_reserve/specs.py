"""Declarable federal_reserve provider models.

Thirteen upstream models live in ``providers/federal_reserve/openbb_federal_reserve/models`` at
the pinned OpenBB commit ``3e071fcc2cd9f891cac6040ae60296dba76dab46``. Two of them -- the rows the
iteration-2 census blocked on ``csv_decoder`` and ``csv_header_offset`` -- are single-GET CSV
downloads of the Fed's ``datadownload/Output.aspx`` package endpoint and are declared here as data
(:class:`~opendata.data.providers._engine.spec.ModelSpec`) with
``decoder=DecoderSpec(kind="csv", preamble_rows=5)``. A third, :data:`SVENSSON_YIELD_CURVE`,
became declarable the round the engine grew ``ModelSpec.row_expansion``: its census need was
``series_pivot``, which is the wide body read once per declared series column and pivoted so one
row is one timestamp with one column per series, and the declaration states those two stages as
data instead of hand-writing the composition.
The other ten need composition the declaration format cannot express and are recorded in
:data:`NOT_DECLARED` rather than silently dropped from the count.

One of those eleven was declared here and retracted. ``YieldCurve`` reads the same H.15 download as
:data:`TREASURY_RATES`, so a wide declaration of it was a second model id for rows the first one
already published; the oracle's ``YieldCurve`` is not that table at all but the long triple
``(date, maturity, rate)`` it builds after the response arrives by melting eleven columns, ordering
maturity by a ``Categorical`` and selecting the requested dates with ``DatetimeIndex.asof``
(yield_curve.py:88-115). Melting is not declarable, so the record moved to :data:`NOT_DECLARED`.

What the read-only oracle actually publishes, with its two halves separated because they are not
equally carried. The upstream helper is not a helper: each of the three models inlines
``make_request(url)`` plus ``pandas.read_csv(BytesIO(r.content), header=5, ...)``
(money_measures.py:77-78, treasury_rates.py:93-95, yield_curve.py:72-73), and those line numbers,
the ``header=5`` count and the release constants below were re-read from the installed extension
(openbb_federal_reserve 1.6.2) rather than remembered. The *response* numbers are on weaker ground:
they were read from a capture at
``providers/federal_reserve/tests/record/http/test_federal_reserve_fetchers/*.yaml``, and that root
is not in this tree -- no ``providers/`` directory, no ``record/`` directory, and the installed
package contains no ``RIFLGFC`` string at all. So the header cells, row counts and ``ND`` census
below are assertions this file carries, not bytes a reader can re-measure here, and the test cannot
falsify them either: it hand-types the same eleven names into its own fixture, so it proves
spec-to-row mapping and nothing about the release. One more asymmetry belongs here: upstream names
that table *positionally* (``df.columns = ["date"] + maturities``, treasury_rates.py:96 over the
list at :18-29), so it never reads the header cells our ``source_key`` values are matched against. A
wrong name therefore yields null columns rather than a refusal. Fetching and archiving the capture
as a hashed fixture is the fix, and it needs the live-upstream authorization this phase lacks.

* MoneyMeasures (``rel=H6``, CRLF file of 790 logical CSV rows, 30 columns): logical row 5 is the
  header and publishes ``Time Period``, ``M1_N.M``, ``M2_N.M``, ``MCU_N.M``, ``MDD_N.M``,
  ``MDL_N.M``, ``MDTS_N.M``, ``MMFGB_N.M``, ``MIKD_N.M``, ``MIKM_N.M``, ``MIK_N.M``, ``M1.M``,
  ``M2.M``, ``MCU.M``, ``MDD.M``, ``MDL.M``, ``MDTS.M``, ``MMFGB.M`` and twelve more reserves/base
  ids. Rows 0-4 are ``Series Description`` / ``Unit:`` / ``Multiplier:`` / ``Currency:`` /
  ``Unique Identifier:`` metadata. Period cells are ``1959-01``.
* TreasuryRates and YieldCurve read the same download (``rel=H15``, 16306 CRLF rows, 12 columns):
  header row 5 publishes ``Time Period``, ``RIFLGFCM01_N.B``, ``RIFLGFCM03_N.B``,
  ``RIFLGFCM06_N.B``, ``RIFLGFCY01_N.B``, ``RIFLGFCY02_N.B``, ``RIFLGFCY03_N.B``,
  ``RIFLGFCY05_N.B``, ``RIFLGFCY07_N.B``, ``RIFLGFCY10_N.B``, ``RIFLGFCY20_N.B``,
  ``RIFLGFCY30_N.B``; period cells are ``1962-01-02``. The same body carries 8003 ``ND`` cells and
  30245 empty ones -- which is why the maturity columns below are ``str`` and not ``float``. Those
  eleven ids are the whole published tenor set: no ``RIFLGFCM02_N.B`` and no ``RIFLGFCYW04_N.B``
  occur anywhere in the pinned captures, which is why this declaration publishes 12 columns and not
  the 14 the shared ``TreasuryRatesData`` base class can carry.

The preamble every one of these bodies leads with is declared, not assumed. The Fed package is not
a table from byte zero: five metadata rows (``Series Description`` / ``Unit:`` / ``Multiplier:`` /
``Currency:`` / ``Unique Identifier:``) come before the header, and upstream discards them by
*counting* them -- ``pd.read_csv(..., header=5)`` -- which is why each declaration below carries
``decoder=DecoderSpec(kind="csv", preamble_rows=5)``. The engine skips that many rows from its
blank-filtered row list, the same list pandas counts because blank-line skipping is on upstream too,
so a count off by one is a refusal (the header lands on a data row, or on nothing) rather than a
table read one row short. :data:`DECLARED_GAPS` records what these declarations still cannot say.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.row_expansion import (
    SeriesPivotPlan,
    WideTablePivotPlan,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    ParamSpec,
    RowOrderSpec,
)

#: Upstream base for the Fed's ``datadownload`` package endpoint. No model sends a credential.
FEDERAL_RESERVE_BASE_URL = "https://www.federalreserve.gov"

#: The request path every ``Output.aspx`` download below is addressed to.
DOWNLOAD_PATH = "/datadownload/Output.aspx"


# The keys every ``Output.aspx`` download is requested with, in the order upstream spells them.
# They are URL literals inside the oracle's own ``extract_data`` (money_measures.py:72-75,
# treasury_rates.py:87-91), not fields of any ``QueryParams`` class, so they are declared as
# :attr:`~opendata.data.providers._engine.spec.ModelSpec.static_query` constants rather than as
# defaulted parameters: a caller may not re-address a reviewed model at another release or series
# hash, and may not switch ``filetype`` to a payload the declared CSV decoder cannot read.
def _download_constants(rel: str, series: str) -> tuple[tuple[str, str], ...]:
    """Compose the fixed query-string constants an ``Output.aspx`` download is sent with."""
    return (
        ("rel", rel),
        ("series", series),
        ("lastobs", ""),
        ("filetype", "csv"),
        ("label", "include"),
        ("layout", "seriescolumn"),
        ("type", "package"),
    )


# The one part of the upstream query string a caller may set: the date window, under the key names
# the endpoint spells. Upstream leaves ``from``/``to`` blank in the URL and filters the downloaded
# window in pandas instead (money_measures.py:87-90, treasury_rates.py:111-112), so the engine
# sends a key only when the caller names that bound. Whether a blank key and an absent one are the
# same request to this endpoint was not measured here -- no live call was made.
#
# This is also why none of the three bound models below takes the ``RelativeDefault`` carrier the
# engine grew in C96, even though the same upstream modules fill a window from today at query time
# (money_measures.py:51-56, treasury_rates.py:65-70): the filled bounds never reach the wire.
# ``extract_data`` spells the URL with ``&from=&to=`` blank as a literal (money_measures.py:72-75)
# and applies the window afterwards, in pandas, against those query values
# (money_measures.py:87-90, treasury_rates.py:111-112). Declaring a request-side bound here would
# send a window the oracle never sends. The client-side window itself is a row-range selection this
# engine cannot declare yet, and DECLARED_GAPS prices it there.
def _download_params() -> tuple[ParamSpec, ...]:
    """Declare the caller-set window keys of an ``Output.aspx`` download."""
    return (
        ParamSpec(
            "start_date",
            "date",
            query_key="from",
            note="window start; upstream leaves from= blank and filters in pandas instead",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="to",
            note="window end; upstream leaves to= blank and filters in pandas instead",
        ),
    )


#: H.6 monthly money stock measures, declared from the table the download publishes after its
#: five metadata rows.
MONEY_MEASURES = ModelSpec(
    model="MoneyMeasures",
    domain="federal_reserve_money_measures",
    asset_class="macro",
    period="m",
    market="us",
    base_url=FEDERAL_RESERVE_BASE_URL,
    path=DOWNLOAD_PATH,
    rows_pointer="",
    params=_download_params(),
    static_query=_download_constants("H6", "798e2796917702a5f8423426ba7e6b42"),
    columns=(
        ColumnSpec(
            "month",
            "str",
            source_key="Time Period",
            required=True,
            nullable=False,
            units="source format %Y-%m",
        ),
        ColumnSpec("m1", "float", source_key="M1_N.M", units="currency, multiplier 1e+09"),
        ColumnSpec("m2", "float", source_key="M2_N.M", units="currency, multiplier 1e+09"),
        ColumnSpec("currency", "float", source_key="MCU_N.M", units="currency, multiplier 1e+09"),
        ColumnSpec(
            "demand_deposits", "float", source_key="MDD_N.M", units="currency, multiplier 1e+09"
        ),
        ColumnSpec(
            "other_liquid_deposits",
            "float",
            source_key="MDL_N.M",
            units="currency, multiplier 1e+09",
        ),
        ColumnSpec(
            "small_denomination_time_deposits",
            "float",
            source_key="MDTS_N.M",
            units="currency, multiplier 1e+09",
        ),
        ColumnSpec(
            "retail_money_market_funds",
            "float",
            source_key="MMFGB_N.M",
            units="currency, multiplier 1e+09",
        ),
    ),
    scenario="货币供应量月表：取出 H.6 发布表在五行元数据之后的七个货币总量序列",
    error_prefix="FEDERAL_RESERVE",
    decoder=DecoderSpec(kind="csv", preamble_rows=5),
    order_by=RowOrderSpec(column="Time Period", direction="asc", limit=None),
    notes=(
        "The preamble is declared, not assumed: five metadata rows precede the header in the "
        "shipped body and preamble_rows=5 skips them by the same count upstream's read_csv "
        "header=5 discards, so a body that stops being a table at that count refuses with "
        "FEDERAL_RESERVE_EMPTY_TABLE naming both counts instead of reading one row short. "
        "The endpoint publishes 30 columns; this "
        "declaration keeps 8, matching the seven series upstream selects by name plus Time Period "
        "(money_measures.py:80-84). The kept ids are the _N.M (not seasonally adjusted) set, "
        "because adjusted defaults to True; adjusted=False selects the seasonally adjusted M1.M "
        "family instead, and a column choice driven by a parameter is not declarable. month is "
        "str because the source publishes 1959-01, which no declared date kind can parse -- "
        "upstream converts it with to_datetime. Upstream also maps ND and - to null, coerces each "
        "cell with float(), filters the window in pandas after the whole download (default ten "
        "years, money_measures.py:87-90) and sorts the transformed data ascending by month "
        "(money_measures.py:112); order_by declares that sort on the record key the download "
        "publishes (Time Period, the source_key of month), with limit=None so every kept row is "
        "returned, and the engine reads a blank cell -- the sentinel this capture actually uses -- "
        "as null."
    ),
)

#: H.15 daily Treasury nominal yields, the wide table as the download publishes it.
TREASURY_RATES = ModelSpec(
    model="TreasuryRates",
    domain="federal_reserve_treasury_rates",
    asset_class="macro",
    period="d",
    market="us",
    base_url=FEDERAL_RESERVE_BASE_URL,
    path=DOWNLOAD_PATH,
    rows_pointer="",
    params=_download_params(),
    static_query=_download_constants("H15", "bf17364827e38702b42a58cf8eaa3f78"),
    columns=(
        ColumnSpec("date", "date", source_key="Time Period", required=True, nullable=False),
        ColumnSpec("month_1", "str", source_key="RIFLGFCM01_N.B", units="percent per year"),
        ColumnSpec("month_3", "str", source_key="RIFLGFCM03_N.B", units="percent per year"),
        ColumnSpec("month_6", "str", source_key="RIFLGFCM06_N.B", units="percent per year"),
        ColumnSpec("year_1", "str", source_key="RIFLGFCY01_N.B", units="percent per year"),
        ColumnSpec("year_2", "str", source_key="RIFLGFCY02_N.B", units="percent per year"),
        ColumnSpec("year_3", "str", source_key="RIFLGFCY03_N.B", units="percent per year"),
        ColumnSpec("year_5", "str", source_key="RIFLGFCY05_N.B", units="percent per year"),
        ColumnSpec("year_7", "str", source_key="RIFLGFCY07_N.B", units="percent per year"),
        ColumnSpec("year_10", "str", source_key="RIFLGFCY10_N.B", units="percent per year"),
        ColumnSpec("year_20", "str", source_key="RIFLGFCY20_N.B", units="percent per year"),
        ColumnSpec("year_30", "str", source_key="RIFLGFCY30_N.B", units="percent per year"),
    ),
    scenario="国债收益率日表：取出 H.15 发布表在五行元数据之后的十一个期限档",
    error_prefix="FEDERAL_RESERVE",
    decoder=DecoderSpec(kind="csv", preamble_rows=5),
    notes=(
        "The preamble is a declared count, not a guess: five metadata rows precede the header, "
        "upstream drops them with read_csv header=5, and preamble_rows=5 states the same count "
        "here, so this record publishes the 12-column table after them, "
        "under the ids the header really prints. The eleven maturity columns are str on purpose: "
        "the pinned body carries its own missing-data token ND in 8003 cells, and a float column "
        "would refuse those rows with FEDERAL_RESERVE_SHAPE_INVALID -- declaring them numeric "
        "would describe a body the source does not publish. Upstream replaces ND with null, "
        "divides every maturity by 100 (treasury_rates.py:114-115) so its contract speaks "
        "normalized percent, drops rows that are entirely empty (treasury_rates.py:99), renames "
        "the 12 columns positionally rather than by name (treasury_rates.py:96), and applies its "
        "one-year default window in pandas after the whole download. The engine keeps the "
        "published percent scale, keeps all-empty rows as rows of nulls, and reads blank cells as "
        "null."
    ),
)


#: The 99 series columns the release publishes behind its ``Date`` header, as ``(contract field,
#: published header token)`` pairs.
#:
#: The tokens are read off the pinned upstream model's own ``__alias_dict__``
#: (svensson_yield_curve.py:217-291 at commit ``3e071fcc2cd9``), which is the same face
#: :mod:`scripts.quality.declaration_provenance` compares a declaration against, so this table is
#: the published output contract rather than a guess at it. Two parameter columns groups and three
#: maturity families: ``BETA0``-``BETA3`` and ``TAU1``/``TAU2`` are the fitted Svensson
#: coefficients, ``SVENF01``-30 the instantaneous forward rates, ``SVENPY01``-30 the par yields,
#: ``SVENY01``-30 the zero-coupon yields and ``SVEN1F01``/``04``/``09`` the one-year forwards --
#: 4+2+3+30+30+30 = 99 columns, one per tenor in each of the three one-to-thirty-year families.
SVENSSON_SERIES_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    *(("beta0", "BETA0"), ("beta1", "BETA1"), ("beta2", "BETA2"), ("beta3", "BETA3")),
    *(("tau1", "TAU1"), ("tau2", "TAU2")),
    *((f"sven1f{suffix}", f"SVEN1F{suffix}") for suffix in ("01", "04", "09")),
    *((f"svenf{tenor:02d}", f"SVENF{tenor:02d}") for tenor in range(1, 31)),
    *((f"svenpy{tenor:02d}", f"SVENPY{tenor:02d}") for tenor in range(1, 31)),
    *((f"sveny{tenor:02d}", f"SVENY{tenor:02d}") for tenor in range(1, 31)),
)

#: The timestamp header the release prints. The alias dictionary pairs ``date`` with ``Date`` and
#: the upstream reader looks for the line that starts with ``Date,``, so this is the published token
#: and not this declaration's invention.
SVENSSON_TIMESTAMP_HEADER: Final = "Date"


#: The Fed's Svensson fitted-factor daily yield-curve release: the census row whose body is a wide
#: CSV read per series and pivoted, declared as the two kernel stages that do exactly that.
SVENSSON_YIELD_CURVE = ModelSpec(
    model="SvenssonYieldCurve",
    domain="federal_reserve_svensson_yield_curve",
    asset_class="macro",
    period="d",
    market="us",
    base_url=FEDERAL_RESERVE_BASE_URL,
    path="/data/yield-curve-tables/feds200628.csv",
    rows_pointer="",
    columns=(
        ColumnSpec("date", "date", required=True, nullable=False),
        *(ColumnSpec(field, "str", source_key=header) for field, header in SVENSSON_SERIES_COLUMNS),
    ),
    row_expansion=(
        WideTablePivotPlan(
            records_pointer="",
            timestamp_key=SVENSSON_TIMESTAMP_HEADER,
            series_keys=tuple(header for _, header in SVENSSON_SERIES_COLUMNS),
        ),
        SeriesPivotPlan(),
    ),
    scenario="Svensson 拟合因子收益率曲线：宽表按序列读出并转置成一行一个日期",
    error_prefix="FEDERAL_RESERVE",
    decoder=DecoderSpec(kind="csv"),
    notes=(
        "Census carrier OBB2-federal_reserve-SvenssonYieldCurve: its notes read static "
        "feds200628.csv, rows are a per-series pivot of a wide CSV, not records, its evidence is "
        "providers/federal_reserve/openbb_federal_reserve/models/svensson_yield_curve.py:143, and "
        "its needs are csv_decoder + series_pivot + param_value_mapping. The first and the third "
        "were already present capabilities (decode.delimited, which this package declares twice "
        "above, and param.value_mapping), so series_pivot was the whole blocker and is what "
        "ModelSpec.row_expansion carries now: a WideTablePivotPlan that reads the decoded wide "
        "records once per declared series column into parallel-array series objects, then a "
        "SeriesPivotPlan that joins them on the timestamp, so one row is one date with one column "
        "per series. Nothing here fetches -- both stages re-resolve their pointers inside the "
        "record list resolve_rows already reached, which is the kernel's own discipline.\n"
        "The release vocabulary is now carried, which "
        "it was not when this declaration first shipped "
        "with four columns and a note saying the tokens "
        "could not be measured here. Every field name "
        "and every source key above is a pair out of the pinned contract's __alias_dict__ "
        "(svensson_yield_curve.py:217-291) -- the same output-contract face "
        "scripts/quality/declaration_provenance.py "
        "compares a declaration against -- and the URL is "
        "the string that file's download_csv hands to make_request (:134-147). Three things this "
        "round read off it are corrections, and they are the reason the earlier note was wrong to "
        "call them unknowable. The timestamp header is "
        "Date, not the DATE guessed here. The 99 series "
        "columns are the whole published set, so the "
        "four-column declaration was a narrowed contract "
        "-- which is exactly what this round's plane "
        "run reported, COLUMN_MISSING over 99 names and "
        "COLUMN_EXTRA over the four beta_0-style ones. And the download is "
        "/data/yield-curve-tables/feds200628.csv: /datadownload/ is where the H.6 and H.15 package "
        "endpoint lives, and this release does not sit beside it, so the path first declared here "
        "would have answered a live run with a 404.\n"
        "What is still not carried is the body. There is no capture root and no fixture for "
        "feds200628.csv -- the same absence this module's docstring records for the H.6 and H.15 "
        "bodies -- so which cells are actually blank, "
        "and how many rows the file has, stay unverified "
        "here. The tokens are no longer among the unknowns: "
        "the pinned contract's own field validator "
        "(:1062-1082) maps '', 'NA', 'ND' and 'N/A' to null and treats -999.99 as a missing TAU2. "
        "That is why the 99 columns are str and not float. The engine can cast a cell now -- "
        "columns.derive is present since this engine "
        "grew ModelSpec.column_derives -- but it has no "
        "sentinel-to-null map, so a cast stage would "
        "refuse the very cells the contract was written "
        "to absorb, and the H.15 body in this same package carries ND in 8003 cells. Declaring the "
        "source token is the honest face; the cast is "
        "one capability step behind a sentinel map, and "
        "DECLARED_GAPS prices it there. Units are omitted "
        "for the matching reason: these bodies ship "
        "no metadata rows quoting a unit, unlike the H.6 and H.15 downloads whose Unit: rows this "
        "file does quote.\n"
        "Three upstream behaviours stay outside the declaration, and none of them is a naming "
        "problem. The reader searches for the line starting with 'Date,' rather than counting a "
        "preamble, and DecoderSpec.preamble_rows is a "
        "count, so a body that leads with title rows is "
        "a refusal here instead of a recovered table. The series_type parameter picks which of the "
        "100 columns the answer carries -- zero_coupon, "
        "par_yield, forward_instantaneous, forward_1y, "
        "parameters, or bare column names -- and "
        "start_date/end_date window the rows, all three after "
        "the response arrives; selecting output columns "
        "by a parameter is not a field ModelSpec has, "
        "and the row filter this engine ships compares a declared value against a literal, not "
        "against a requested range. A row whose date is blank or unparseable is skipped there and "
        "refused here. What is not a gap, and was recorded as one: the zero-coupon and par-yield "
        "series are read out of the CSV, not computed from the fitted factors -- transform_data "
        "(svensson_yield_curve.py:1107-1196) only names a column allow-list per series_type and "
        "copies the cells through, so the earlier claim "
        "that this model needs a computed column was a "
        "misreading of that branch. The domain "
        "federal_reserve_svensson_yield_curve is registered in "
        "domains.yaml, so build_row_model publishes FederalReserveSvenssonYieldCurve and not a "
        "synthesized class."
    ),
)

#: What each declared model still cannot do, per measurement. A declaration is not a finished
#: capability: these are the post-processing steps the oracle performs and the engine has no field
#: to express, so a roadmap can price them without re-reading the upstream tree.
DECLARED_GAPS: dict[str, str] = {
    "MoneyMeasures": "ND/- to null sentinel map; float coercion of cells; client-side date window "
    "and month sort; adjusted picks between the _N.M and .M column families",
    "TreasuryRates": "ND to null; divide every maturity by 100; drop all-empty rows; positional "
    "(not named) column rename; client-side date window",
    "SvenssonYieldCurve": "a sentinel map ('', NA, ND, N/A and -999.99 per the pinned validator "
    ":1062-1082) before any float coercion, so the 99 series columns stay str; the header row is "
    "found by searching for 'Date,' while preamble_rows counts; series_type selects which columns "
    "the answer carries and start_date/end_date window the rows, all client-side; a row with an "
    "unparseable date is skipped upstream and refused here. The zero-coupon and par-yield series "
    "are READ, not computed from the fitted factors -- the earlier gap text said they were, and "
    "transform_data (svensson_yield_curve.py:1107-1196) shows a column allow-list and a copy.",
}

#: The ten upstream models this package does not declare, and the capability each one needs.
NOT_DECLARED: dict[str, str] = {
    "YieldCurve": "reads the same H.15 download as TreasuryRates, then melts its eleven maturity "
    "columns into long (date, maturity, rate) rows ordered by a Categorical, selects the requested "
    "dates with DatetimeIndex.asof and divides rate by 100 (yield_curve.py:88-115). A wide "
    "declaration of it would be a second model id over the rows TreasuryRates already "
    "publishes and would not be the oracle's model, so the declaration was retracted rather "
    "than reshaped.",
    "Sofr": "newyorkfed.org JSON whose URL composes the date window (sofr.py:84-90); not a CSV row "
    "and not judged by this task",
    "FederalFundsRate": "newyorkfed.org JSON with a composed window and a per-record pop of "
    "type/footnoteId/revisionIndicator (federal_funds_rate.py:116-127)",
    "OvernightBankFundingRate": "newyorkfed.org JSON whose window this package does not declare "
    "yet. The composed window is declarable now that ``ParamSpec.relative_default`` exists: the "
    "two keys are ordinary query parameters (``startDate``/``endDate``, "
    "overnight_bank_funding_rate.py:96-98), the absolute start literal is a plain ``default`` and "
    "the request-date end bound is an all-zero ``RelativeDefault`` (both filled only when the "
    "caller named none, :78-82). What still blocks the record is not the vocabulary: the package "
    "holds no ModelSpec for it because ``overnight_bank_funding_rate`` is not a registered domain "
    "in domains.yaml, and a declaration whose domain is unregistered publishes rows no query or "
    "ingest service accepts (http_json.build_row_model). Registering the domain and binding the "
    "model is the package's act, not an engine capability.",
    "PrimaryDealerFails": "two or three requests concatenated by a condition on the requested date "
    "(primary_dealer_fails.py:83-105)",
    "PrimaryDealerPositioning": "one request per symbol in a category branch, gathered, then "
    "columns mapped from two dictionaries (primary_dealer_positioning.py:108-122)",
    "CentralBankHoldings": "delegates to a SomaHoldings helper that resolves the endpoint per data "
    "type (central_bank_holdings.py:317)",
    "InflationExpectations": "an Excel workbook read from a zipped download "
    "(inflation_expectations.py:149-154)",
    "TotalFactorProductivity": "an Excel workbook with per-sheet skiprows "
    "(total_factor_productivity.py:549-557)",
    "FomcDocuments": "enumerates document URLs from a packaged asset and downloads each document "
    "(pdf/htm) separately (fomc_documents.py:100-129)",
}
