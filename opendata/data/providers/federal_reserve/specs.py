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
Two more, :data:`FEDERAL_FUNDS_RATE` and :data:`SOFR`, left :data:`NOT_DECLARED` in C98: the
New York Fed JSON rows were refused while the rescale, the sentinel-before-the-stage reading, the
request-date bound and the declared row order were each still missing, and every facet they need
has since shipped, so the honest move was to declare them and price what little remains in
:data:`DECLARED_GAPS`.
The remaining identities use dedicated source-specific fetchers where the declaration format
cannot express their response composition. Their provider bindings and typed contracts are kept
beside those fetchers; :data:`NOT_DECLARED` records only identities with no implementation.

One of those eleven was declared here and retracted. ``YieldCurve`` reads the same H.15 download as
:data:`TREASURY_RATES`, so a wide declaration would be a second model id for rows the first one
already publishes. The separate long-form ``YieldCurve`` identity is represented by a dedicated
adapter that expands the eleven published maturity columns into ``(date, maturity, rate)`` rows;
that response composition is not expressible by this declaration format.

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

from datetime import date
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
    RelativeDefault,
    RowOrderSpec,
)
from opendata.data.providers._engine.value_transform import (
    DerivePlan,
    DeriveStage,
    ScaleSpec,
)

#: Upstream base for the Fed's ``datadownload`` package endpoint. No model sends a credential.
FEDERAL_RESERVE_BASE_URL = "https://www.federalreserve.gov"

#: Upstream base for the New York Fed's reference-rate JSON the two C98 rows read.
NEW_YORK_FED_BASE_URL = "https://markets.newyorkfed.org"

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


#: The missing-data token the two release bodies print for a series the Fed does not publish for
#: that date, and the four tokens the Svensson file prints. These are published spellings read off
#: the pinned upstream models, not this declaration's invention: money_measures.py:85 does
#: ``df.replace("ND", None)`` over the seven _N.M columns, treasury_rates.py:97 does
#: ``.replace("ND", nan)`` over the eleven maturity columns before dividing them by 100 at :115, and
#: SvenssonYieldCurve's own validator (:1062-1082) nulls ``""``, ``NA``, ``ND`` and ``N/A`` before
#: any float coercion. The numeric ``-999.99`` that validator also recognises is deliberately not
#: carried here: these columns are declared ``str`` because the body prints the text tokens, and a
#: sentinel is matched on the text the source publishes before coercion.
H6_NULL_SENTINEL: Final[tuple[str, ...]] = ("ND",)
H15_NULL_SENTINEL: Final[tuple[str, ...]] = ("ND",)
SVENSSON_NULL_SENTINEL: Final[tuple[str, ...]] = ("", "NA", "ND", "N/A")

#: The spellings the SOFR validator nulls before it divides (sofr.py:46-48): ``if v not in
#: (None, "", "''", "NA")``. A JSON ``null`` is not a sentinel here -- an absent or null cell on
#: a staged column is a refusal the engine keeps as ``ValueTransformError``, which is recorded in
#: :data:`DECLARED_GAPS`, not a spelling this field can spell (a sentinel matches published text).
SOFR_NULL_SENTINEL: Final[tuple[str, ...]] = ("", "''", "NA")

#: The spellings the Federal Funds Rate validator nulls on the revision indicator
#: (federal_funds_rate.py:57-61): ``return None if v in ("", "''") else v``.
REVISION_NULL_SENTINEL: Final[tuple[str, ...]] = ("", "''")


def _percent_fraction_derives(columns: tuple[str, ...]) -> tuple[DerivePlan, ...]:
    """Declare one divide-by-100 stage per published percent column.

    Both C98 models read a New York Fed reference-rate JSON whose validator divides each percent
    field it touches (federal_funds_rate.py:76-81, sofr.py:44-50). The rescale is not a ColumnSpec
    field: it is a :class:`~opendata.data.providers._engine.value_transform.DeriveStage` of kind
    ``"scale"`` on a record-sourced :class:`DerivePlan`, keyed by the declared column it produces.
    """
    return tuple(
        DerivePlan(
            column=name,
            stages=(DeriveStage(kind="scale", scale=ScaleSpec.percent_to_fraction()),),
            note=(
                f"{name} is published in percent and rescaled to a fraction by dividing by 100, "
                "the same rule the upstream before-validator runs (federal_funds_rate.py:80, "
                "sofr.py:49)."
            ),
        )
        for name in columns
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
        ColumnSpec(
            "m1",
            "float",
            source_key="M1_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "m2",
            "float",
            source_key="M2_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "currency",
            "float",
            source_key="MCU_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "demand_deposits",
            "float",
            source_key="MDD_N.M",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "other_liquid_deposits",
            "float",
            source_key="MDL_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "small_denomination_time_deposits",
            "float",
            source_key="MDTS_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
        ),
        ColumnSpec(
            "retail_money_market_funds",
            "float",
            source_key="MMFGB_N.M",
            units="currency, multiplier 1e+09",
            null_sentinel=H6_NULL_SENTINEL,
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
        ColumnSpec(
            "month_1",
            "str",
            source_key="RIFLGFCM01_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "month_3",
            "str",
            source_key="RIFLGFCM03_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "month_6",
            "str",
            source_key="RIFLGFCM06_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_1",
            "str",
            source_key="RIFLGFCY01_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_2",
            "str",
            source_key="RIFLGFCY02_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_3",
            "str",
            source_key="RIFLGFCY03_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_5",
            "str",
            source_key="RIFLGFCY05_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_7",
            "str",
            source_key="RIFLGFCY07_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_10",
            "str",
            source_key="RIFLGFCY10_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_20",
            "str",
            source_key="RIFLGFCY20_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
        ColumnSpec(
            "year_30",
            "str",
            source_key="RIFLGFCY30_N.B",
            units="percent per year",
            null_sentinel=H15_NULL_SENTINEL,
        ),
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
        "published percent scale, keeps all-empty rows as null rows, and reads both blank "
        "and the ND token as null -- the token half is declared, not assumed: "
        "null_sentinel=H15_NULL_SENTINEL on each of the eleven columns is this round's C97 kernel "
        "in production use, and it is the same replace upstream does at treasury_rates.py:97."
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
        *(
            ColumnSpec(field, "str", source_key=header, null_sentinel=SVENSSON_NULL_SENTINEL)
            for field, header in SVENSSON_SERIES_COLUMNS
        ),
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
        "grew ModelSpec.column_derives -- and since C97 it has a sentinel-to-null map: "
        "null_sentinel=SVENSSON_NULL_SENTINEL declares the four text tokens on every one of the 99 "
        "series columns, so a cast stage no longer refuses the cells the contract was written to "
        "absorb and the H.15 body's 8003 ND cells read as nulls. What the field still cannot spell "
        "is -999.99: a sentinel is matched on the published text before coercion and these columns "
        "are str, so that one missing-value number stays priced in DECLARED_GAPS. Declaring the "
        "source tokens is the honest face; the cast is one step behind them and is not declared "
        "here. Units are omitted "
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

#: The 9 percent columns the effective-federal-funds-rate record publishes and whose upstream
#: validator divides by 100 (federal_funds_rate.py:63-75, the division at :80). ``volume`` and
#: ``standard_deviation`` are published alongside them but are NOT in that validator list, so no
#: rescale is declared for them -- declaring one would divide a cell upstream copies verbatim.
FFR_PERCENT_FIELDS: Final[tuple[str, ...]] = (
    "rate",
    "target_range_upper",
    "target_range_lower",
    "percentile_1",
    "percentile_25",
    "percentile_75",
    "percentile_99",
    "intraday_low",
    "intraday_high",
)

#: The effective federal funds rate, one GET of the New York Fed's ``effr/search.json``.
FEDERAL_FUNDS_RATE = ModelSpec(
    model="FederalFundsRate",
    domain="federal_reserve_federal_funds_rate",
    asset_class="macro",
    period="d",
    market="us",
    base_url=NEW_YORK_FED_BASE_URL,
    path="/api/rates/unsecured/effr/search.json",
    rows_pointer="refRates",
    params=(
        ParamSpec(
            "start_date",
            "date",
            query_key="startDate",
            default=date(2016, 3, 1),
            note="window start; upstream fills 2016-03-01 when the caller names none "
            "(federal_funds_rate.py:99-100) and spells it into the query string (:116-119)",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="endDate",
            relative_default=RelativeDefault(),
            note="window end: the request date itself, upstream's ``datetime.now().date()`` "
            "fill (federal_funds_rate.py:101-102) carried as the all-zero RelativeDefault the "
            "C96 field was grown for; only filled when the caller names no bound",
        ),
    ),
    columns=(
        ColumnSpec("date", "date", source_key="effectiveDate", required=True, nullable=False),
        ColumnSpec("rate", "float", source_key="percentRate", units="published percent"),
        ColumnSpec(
            "target_range_upper", "float", source_key="targetRateTo", units="published percent"
        ),
        ColumnSpec(
            "target_range_lower", "float", source_key="targetRateFrom", units="published percent"
        ),
        ColumnSpec(
            "percentile_1", "float", source_key="percentPercentile1", units="published percent"
        ),
        ColumnSpec(
            "percentile_25", "float", source_key="percentPercentile25", units="published percent"
        ),
        ColumnSpec(
            "percentile_75", "float", source_key="percentPercentile75", units="published percent"
        ),
        ColumnSpec(
            "percentile_99", "float", source_key="percentPercentile99", units="published percent"
        ),
        ColumnSpec("volume", "float", source_key="volumeInBillions", units="billions of USD"),
        ColumnSpec("intraday_low", "float", source_key="intraDayLow", units="published percent"),
        ColumnSpec("intraday_high", "float", source_key="intraDayHigh", units="published percent"),
        ColumnSpec("standard_deviation", "float", source_key="stdDeviation"),
        ColumnSpec(
            "revision_indicator",
            "str",
            source_key="revisionIndicator",
            null_sentinel=REVISION_NULL_SENTINEL,
        ),
    ),
    column_derives=_percent_fraction_derives(FFR_PERCENT_FIELDS),
    order_by=RowOrderSpec(column="effectiveDate", direction="asc", limit=None),
    scenario="联邦基金有效利率日表：取出纽约联储参考利率 JSON 的百分序列并折成小数",
    error_prefix="FEDERAL_RESERVE",
    notes=(
        "Census carrier OBB2-federal_reserve-FederalFundsRate. One GET of the New York Fed "
        "endpoint; the rows are the document's refRates list (federal_funds_rate.py:116-127), and "
        "the date window is a QUERY-PAIR shape, not a path composition: startDate and endDate are "
        "ordinary query keys the URL literal interpolates (:116-119). Every field name and source "
        "key is a pair out of the pinned contract's own __alias_dict__ "
        "(federal_funds_rate.py:24-38). The nine percent columns carry the scale stage this round "
        "spells via ModelSpec.column_derives (validator list :63-75, division at :80; 0 stays 0 "
        "there, and 0/100 is 0 here, so the arms agree); revision_indicator carries "
        "null_sentinel=REVISION_NULL_SENTINEL, the exact ('', \"''\") rewrite of the before-"
        "validator at :57-61, matched on the record text before any stage -- the upstream order. "
        "The transform_data pops of type/footnoteId (:137-138) are NO-OPS for this engine: "
        "normalize_record builds each row from declared columns only, so an undeclared key never "
        "reaches the row model and no drop facet is declared for them. order_by declares the "
        "ascending date sort (:141) on the record key the document publishes, limit=None. C98 "
        "registered the domain federal_reserve_federal_funds_rate in domains.yaml with all six "
        "semantics fields (series / time_field=date / natural_key=[date] / filter_dims=[date] / "
        "storage_mode=transient / permissions=[query]) and bound the reviewed contract "
        "FederalReserveFederalFundsRate, so build_row_model publishes that class instead of "
        "synthesizing a FederalFundsRateRow look-alike -- measured on this spec: the contract's 13 "
        "fields against the 13 declared columns leave an empty symmetric difference in both "
        "directions. No capture for this body is pinned here, so the offline test "
        "feeds a synthesized document whose cells are spelled the way the kernel reads them: the "
        "remaining refusals (a JSON float cell, an explicit null on a staged column, a full "
        "timestamp spelling of effectiveDate) are priced in DECLARED_GAPS, not worked around."
    ),
)

#: The 5 percent columns the SOFR record publishes that the upstream validator's list reaches
#: (sofr.py:34-44). The list also names target_range_upper/target_range_lower, but the contract's
#: own __alias_dict__ (:24-32) publishes no such key, so those validator arms have nothing to
#: divide here and no scale stage is declared for them. ``volume`` is likewise outside the list.
SOFR_PERCENT_FIELDS: Final[tuple[str, ...]] = (
    "rate",
    "percentile_1",
    "percentile_25",
    "percentile_75",
    "percentile_99",
)

#: The secured overnight financing rate, one GET of the New York Fed's ``sofr/search.json``.
SOFR = ModelSpec(
    model="SOFR",
    domain="federal_reserve_sofr",
    asset_class="macro",
    period="d",
    market="us",
    base_url=NEW_YORK_FED_BASE_URL,
    path="/api/rates/secured/sofr/search.json",
    rows_pointer="refRates",
    params=(
        ParamSpec(
            "start_date",
            "date",
            query_key="startDate",
            default=date(2018, 4, 2),
            note="window start; upstream fills 2018-04-02 when the caller names none (sofr.py:"
            "66-67) and spells it into the query string (:83-86)",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="endDate",
            relative_default=RelativeDefault(),
            note="window end: the request date itself, upstream's datetime.now().date() fill "
            "(sofr.py:68-69) carried as the all-zero RelativeDefault; only filled when the "
            "caller names no bound",
        ),
    ),
    columns=(
        ColumnSpec("date", "date", source_key="effectiveDate", required=True, nullable=False),
        ColumnSpec(
            "rate",
            "float",
            source_key="percentRate",
            units="published percent",
            null_sentinel=SOFR_NULL_SENTINEL,
        ),
        ColumnSpec(
            "percentile_1",
            "float",
            source_key="percentPercentile1",
            units="published percent",
            null_sentinel=SOFR_NULL_SENTINEL,
        ),
        ColumnSpec(
            "percentile_25",
            "float",
            source_key="percentPercentile25",
            units="published percent",
            null_sentinel=SOFR_NULL_SENTINEL,
        ),
        ColumnSpec(
            "percentile_75",
            "float",
            source_key="percentPercentile75",
            units="published percent",
            null_sentinel=SOFR_NULL_SENTINEL,
        ),
        ColumnSpec(
            "percentile_99",
            "float",
            source_key="percentPercentile99",
            units="published percent",
            null_sentinel=SOFR_NULL_SENTINEL,
        ),
        ColumnSpec("volume", "float", source_key="volumeInBillions", units="billions of USD"),
    ),
    column_derives=_percent_fraction_derives(SOFR_PERCENT_FIELDS),
    order_by=RowOrderSpec(column="effectiveDate", direction="asc", limit=None),
    scenario="有担保隔夜融资利率日表：取出纽约联储 SOFR JSON 的百分序列并折成小数",
    error_prefix="FEDERAL_RESERVE",
    notes=(
        "Census carrier OBB2-federal_reserve-SOFR. One GET; the rows are the document's refRates "
        "list (sofr.py:83-92), and sofr.py:83-86 composes the URL by interpolating startDate and "
        "endDate into the QUERY STRING -- a query-pair shape the two ParamSpecs carry, not a path "
        "composition, so no path facet was invented for it. Field names and source keys come from "
        "the pinned contract's __alias_dict__ (:24-32). Each of the five published percent "
        "columns declares BOTH null_sentinel=SOFR_NULL_SENTINEL and a scale stage: the validator "
        "at :46-50 nulls None/''/\"''\"/NA BEFORE dividing (:49), and the engine matches a "
        "sentinel on the record text before the stage runs, so sentinel-plus-scale on one "
        "record-sourced column is the same composition, in the same order. A published 0 is not a "
        "sentinel (a sentinel spells text) and 0/100 keeps it 0, matching :49. The pops at "
        ":103-105 are no-ops here: normalize_record reads declared columns only. order_by declares "
        "the ascending date sort (:108) with no cap. C98 registered the domain "
        "federal_reserve_sofr in domains.yaml with all six semantics fields (series / "
        "time_field=date / natural_key=[date] / filter_dims=[date] / storage_mode=transient / "
        "permissions=[query]) and bound the reviewed contract FederalReserveSofr, so "
        "build_row_model publishes that class instead of synthesizing a SOFRRow look-alike -- "
        "measured on this spec: the contract's 7 fields against the 7 declared columns leave an "
        "empty symmetric difference in both directions. What remains outside the declaration "
        "(an explicit JSON null on a staged column, a float-spelled cell) is priced in "
        "DECLARED_GAPS."
    ),
)

#: The 5 percent columns the OBFR record publishes and whose upstream validator divides by 100
#: (overnight_bank_funding_rate.py:47-55, the division at :60). ``volume`` is published alongside
#: them (the alias dict at :26-34) but is outside that validator list, so no scale stage runs on
#: it -- declaring one would divide a cell upstream copies verbatim.
OBFR_PERCENT_FIELDS: Final[tuple[str, ...]] = (
    "rate",
    "percentile_1",
    "percentile_25",
    "percentile_75",
    "percentile_99",
)

#: The overnight bank funding rate, one GET of the New York Fed's ``obfr/search.json``.
OVERNIGHT_BANK_FUNDING_RATE = ModelSpec(
    model="OvernightBankFundingRate",
    domain="federal_reserve_overnight_bank_funding_rate",
    asset_class="macro",
    period="d",
    market="us",
    base_url=NEW_YORK_FED_BASE_URL,
    path="/api/rates/unsecured/obfr/search.json",
    rows_pointer="refRates",
    params=(
        ParamSpec(
            "start_date",
            "date",
            query_key="startDate",
            default=date(2016, 3, 1),
            note="window start; upstream fills the ABSOLUTE literal 2016-03-01 when the caller "
            "names none (overnight_bank_funding_rate.py:79-80) and spells it into the query "
            "string (:96-98)",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="endDate",
            relative_default=RelativeDefault(),
            note="window end: the request date itself, upstream's datetime.now().date() fill "
            "(overnight_bank_funding_rate.py:78, 81-82) carried as the all-zero RelativeDefault; "
            "only filled when the caller names no bound",
        ),
    ),
    columns=(
        ColumnSpec("date", "date", source_key="effectiveDate", required=True, nullable=False),
        ColumnSpec("rate", "float", source_key="percentRate", units="published percent"),
        ColumnSpec(
            "percentile_1", "float", source_key="percentPercentile1", units="published percent"
        ),
        ColumnSpec(
            "percentile_25", "float", source_key="percentPercentile25", units="published percent"
        ),
        ColumnSpec(
            "percentile_75", "float", source_key="percentPercentile75", units="published percent"
        ),
        ColumnSpec(
            "percentile_99", "float", source_key="percentPercentile99", units="published percent"
        ),
        ColumnSpec("volume", "float", source_key="volumeInBillions", units="billions of USD"),
    ),
    column_derives=_percent_fraction_derives(OBFR_PERCENT_FIELDS),
    order_by=RowOrderSpec(column="effectiveDate", direction="asc", limit=None),
    scenario="隔夜银行融资利率日表：取出纽约联储 OBFR JSON 的五个百分序列并折成小数",
    error_prefix="FEDERAL_RESERVE",
    notes=(
        "Census carrier OBB2-federal_reserve-OvernightBankFundingRate, the sibling of SOFR on the "
        "same New York Fed reference-rate shape: one GET, the rows are the document's refRates "
        "list (overnight_bank_funding_rate.py:102-103), and :96-98 interpolates startDate and "
        "endDate into the query string -- a query-pair shape, not a path composition. Field "
        "names and source keys are the pinned contract's own __alias_dict__ (:26-34). The five "
        "percent columns carry the scale stage (validator list :47-55, division at :60; 0 stays 0 "
        "there and 0/100 is 0 here). NO null sentinel is declared on this row, and that is "
        "measured, not omitted: normalize_percent guards ONLY None (:57-61) -- where SOFR's "
        "validator at sofr.py:48 nulls '', \"''\" and NA before dividing, this one divides "
        "whatever is not None, so there is no published text token this column nulls ahead of "
        "the cast (the C92 sentinel claim the census refuted, re-measured on the oracle at "
        ":57-61). The revision_indicator validator (:41-45) is dead text FOR THE PUBLISHED ROW: "
        "transform_data pops type/revisionIndicator/footnoteId (:117-119) before validation, so "
        "the row carries no revision cell; normalize_record reads declared columns only, so all "
        "three pops are no-ops here and none is declared, exactly as for SOFR's :103-105. "
        "order_by declares the ascending date sort (:122) with no cap. The census need set "
        "(published_value_rescale, client_side_sort, relative_date_default) is complete: all "
        "three facets ship as columns.rescale, rows.order_limit and param.relative_default, "
        "which is why this row was in roadmap shipped_cover_rows rather than the build pool. "
        "C99 registered this domain in domains.yaml beside federal_reserve_sofr and bound the "
        "reviewed contract FederalReserveOvernightBankFundingRate: its 7 fields against the 7 "
        "declared columns leave an empty symmetric difference, so no row class is synthesized."
    ),
)


#: What each declared model still cannot do, per measurement. A declaration is not a finished
#: capability: these are the post-processing steps the oracle performs and the engine has no field
#: to express, so a roadmap can price them without re-reading the upstream tree.
DECLARED_GAPS: dict[str, str] = {
    "MoneyMeasures": "the '-' cell is NOT a declared sentinel and this model keeps it as text, "
    "which is what the oracle does too (money_measures.py:93 floats every cell except '-'), but a "
    "float column here refuses it rather than passing it through; float coercion of cells; "
    "client-side date window and month sort; adjusted picks between the _N.M and .M column "
    "families. The ND half of the old 'ND/- to null sentinel map' text is declared since C97: "
    "null_sentinel=H6_NULL_SENTINEL on the seven _N.M columns.",
    "TreasuryRates": "divide every maturity by 100 (the columns are declared str, so no rescale "
    "stage runs on them); drop all-empty rows; positional (not named) column rename; client-side "
    "date window. ND to null is declared since C97: null_sentinel=H15_NULL_SENTINEL on the eleven "
    "RIFLGFC* columns, matching treasury_rates.py:97.",
    "SvenssonYieldCurve": "the numeric -999.99 that the pinned validator (:1062-1082) treats as a "
    "missing TAU2 is NOT declared: a sentinel is matched on the published text before coercion and "
    "this model's columns are str, so carrying it would mean casting the cell first. The four text "
    "tokens ('', NA, ND, N/A) are declared since C97 on all 99 series columns. The header row is "
    "found by searching for 'Date,' while preamble_rows counts; series_type selects which columns "
    "the answer carries and start_date/end_date window the rows, all client-side; a row with an "
    "unparseable date is skipped upstream and refused here. The zero-coupon and par-yield series "
    "are READ, not computed from the fitted factors -- the earlier gap text said they were, and "
    "transform_data (svensson_yield_curve.py:1107-1196) shows a column allow-list and a copy.",
    "FederalFundsRate": "the kernel reads a staged column's input as exact decimal text or an "
    "integer (value_transform refuses a binary float), so a percent cell the endpoint publishes as "
    "a JSON float is refused rather than divided -- upstream's float(v) absorbs it "
    "(federal_funds_rate.py:80). Closing this needs a declared float-to-exact-decimal cast "
    "(Decimal(repr(v))), not a package workaround. An explicit JSON null on one of the nine staged "
    "columns is likewise refused where the validator returns None (:79-81); the capability would "
    "be a declared null passthrough for a nullable staged column (an ABSENT key already reads as "
    "null, so only the explicit null is priced). No capture pins the effectiveDate spelling: a "
    "full timestamp cell (with time and UTC offset) would refuse against the declared date kind, "
    "where upstream pydantic reads a date off it; a date_string cast names one format, not a "
    "spelling family. Upstream raises EmptyDataError on a missing or empty refRates list "
    "(:122-125); this engine answers an empty list with zero rows and refuses only an "
    "unfollowable pointer.",
    "SOFR": "same two kernel-priced refusals as FederalFundsRate: a JSON-float percent cell (the "
    "float-to-exact-decimal cast is absent) and an explicit null on a staged column, which the "
    "validator at sofr.py:48 maps to None but no sentinel can spell (a sentinel matches text). "
    "The validator list (:36-37) also names target_range_upper/target_range_lower, keys the "
    "contract's own __alias_dict__ (:24-32) does not publish, so those two arms divide nothing "
    "here and declare nothing. Upstream raises EmptyDataError on a missing or empty refRates "
    "list (:89-91); this engine answers an empty list with zero rows and refuses only a document "
    "whose pointer cannot be followed.",
    "OvernightBankFundingRate": "the same two kernel-priced refusals as SOFR, plus one the "
    "census priced from the first reading: a JSON-float percent cell is refused where upstream's "
    "v / 100 (:60) absorbs it, and an explicit JSON null on a staged column is refused where "
    "normalize_percent returns None (:59-61) -- an absent key already reads as null. No capture "
    "pins this body, so the offline test feeds a synthesized document. Upstream raises "
    "EmptyDataError on a missing or empty refRates list (:104-105); this engine answers zero "
    "rows. The C99 splice registered the domain and bound the reviewed contract, so the class the "
    "fetcher publishes is that contract, not a row model synthesized from the declaration.",
}

#: Models with no local adapter or typed contract. Iteration 2 added dedicated source-owned
#: parsers for the remaining seven identities; the list stays explicit so provider census tests
#: can assert it is empty rather than silently dropping models.
NOT_DECLARED: dict[str, str] = {}
