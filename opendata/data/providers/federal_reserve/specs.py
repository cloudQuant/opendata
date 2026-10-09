"""Declarable federal_reserve provider models.

Thirteen upstream models live in ``providers/federal_reserve/openbb_federal_reserve/models`` at
the pinned OpenBB commit ``3e071fcc2cd9f891cac6040ae60296dba76dab46``. Two of them -- the rows the
iteration-2 census blocked on ``csv_decoder`` and ``csv_header_offset`` -- are single-GET CSV
downloads of the Fed's ``datadownload/Output.aspx`` package endpoint and are declared here as data
(:class:`~opendata.data.providers._engine.spec.ModelSpec`) with
``decoder=DecoderSpec(kind="csv", preamble_rows=5)``.
The other eleven need composition the declaration format cannot express and are recorded in
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

from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    ParamSpec,
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
        "years, money_measures.py:87-90) and sorts by month; the engine emits every published row "
        "in published order and reads a blank cell -- the sentinel this capture actually uses -- "
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

#: What each declared model still cannot do, per measurement. A declaration is not a finished
#: capability: these are the post-processing steps the oracle performs and the engine has no field
#: to express, so a roadmap can price them without re-reading the upstream tree.
DECLARED_GAPS: dict[str, str] = {
    "MoneyMeasures": "ND/- to null sentinel map; float coercion of cells; client-side date window "
    "and month sort; adjusted picks between the _N.M and .M column families",
    "TreasuryRates": "ND to null; divide every maturity by 100; drop all-empty rows; positional "
    "(not named) column rename; client-side date window",
}

#: The eleven upstream models this package does not declare, and the capability each one needs.
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
    "OvernightBankFundingRate": "newyorkfed.org JSON with a composed window "
    "(overnight_bank_funding_rate.py:96-102)",
    "PrimaryDealerFails": "two or three requests concatenated by a condition on the requested date "
    "(primary_dealer_fails.py:83-105)",
    "PrimaryDealerPositioning": "one request per symbol in a category branch, gathered, then "
    "columns mapped from two dictionaries (primary_dealer_positioning.py:108-122)",
    "CentralBankHoldings": "delegates to a SomaHoldings helper that resolves the endpoint per data "
    "type (central_bank_holdings.py:317)",
    "SvenssonYieldCurve": "plain CSV, but a series_type branch picks different columns and the "
    "zero-coupon/par-yield series are computed from the fitted parameters "
    "(svensson_yield_curve.py:1116-1157)",
    "InflationExpectations": "an Excel workbook read from a zipped download "
    "(inflation_expectations.py:149-154)",
    "TotalFactorProductivity": "an Excel workbook with per-sheet skiprows "
    "(total_factor_productivity.py:549-557)",
    "FomcDocuments": "enumerates document URLs from a packaged asset and downloads each document "
    "(pdf/htm) separately (fomc_documents.py:100-129)",
}
