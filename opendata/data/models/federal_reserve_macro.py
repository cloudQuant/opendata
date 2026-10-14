"""Source-faithful contracts for the Fed's H.6, H.15, Svensson curve and reference-rate rows.

These are the tables and documents the release endpoints publish: the two wide tables behind
``datadownload/Output.aspx``, the ``feds200628.csv`` fitted-factor curve, and the three
York Fed reference-rate JSON feeds, all declared in
``opendata/data/providers/federal_reserve/specs.py``. Like the cboe and intrinio engine models
before them, each declaration gets its own contract family and its own domain, and the declaration
must name exactly these fields: ``build_row_model`` publishes this class -- and refuses with a
column-symmetric-difference error -- whenever the domain's semantics are declared.

Field types follow the source, not the wish:

* ``month`` is ``str`` because the H.6 body prints ``1959-01``, which no declared date kind
  parses; ``m1`` .. ``retail_money_market_funds`` are ``float | None`` because that capture's
  missing cells are blank and the engine reads a blank as null.
* ``date`` is a real ``datetime.date``: the H.15 body prints ``1962-01-02``, a strict ISO day.
  The eleven maturity columns are ``str | None`` on purpose -- the same body carries its own
  missing-data token ``ND`` in 8003 cells, and a numeric column would refuse those published
  rows. ``None`` is the blank cell; ``"ND"`` stays the token the source printed.
* The three reference-rate rows are the York Fed JSON feeds, and every number in them is
  ``float | None``: a cell the feed omits stays absent and the engine reads an absent optional key
  as null, while SOFR spells its gaps ``""``, ``"''"`` and ``"NA"`` which the declaration nulls with
  ``columns.null_sentinel`` ahead of the cast. So the contract sees a number or a null and never a
  token. ``revision_indicator`` is the one non-numeric column anywhere among them, and the federal
  funds row owns it alone; the OBFR row prints no revision cell at all -- its ``transform_data``
  pops the marker before validation -- so that declaration publishes seven fields and no sentinel,
  because its own validator guards only ``None`` and divides whatever else arrives.
"""

from __future__ import annotations

from datetime import date as date_type
from decimal import Decimal
from typing import Literal

from opendata.data.models.base import ContractModel


class FederalReserveMoneyMeasure(ContractModel):
    """One monthly H.6 money-stock row: the period label plus the seven not-adjusted totals."""

    month: str
    m1: float | None = None
    m2: float | None = None
    currency: float | None = None
    demand_deposits: float | None = None
    other_liquid_deposits: float | None = None
    small_denomination_time_deposits: float | None = None
    retail_money_market_funds: float | None = None


class FederalReserveTreasuryRate(ContractModel):
    """One daily H.15 row, wide, with each maturity as the token the source published."""

    date: date_type
    month_1: str | None = None
    month_3: str | None = None
    month_6: str | None = None
    year_1: str | None = None
    year_2: str | None = None
    year_3: str | None = None
    year_5: str | None = None
    year_7: str | None = None
    year_10: str | None = None
    year_20: str | None = None
    year_30: str | None = None


class FederalReserveSvenssonYieldCurve(ContractModel):
    """One date of the Fed's Svensson curve: the six fitted parameters plus 93 series columns.

    The field names are the published contract's own -- they are the keys of the pinned upstream
    model's ``__alias_dict__``, and ``specs.SVENSSON_SERIES_COLUMNS`` pairs each one with the header
    cell it is read out of, so ``build_row_model``'s symmetric-difference check and
    ``scripts/quality/declaration_provenance.py`` read the same 100 names.

    All 99 non-date columns are ``str | None`` rather than the ``float | None`` the contract types
    them as. The reason is measured and is in ``specs.SVENSSON_YIELD_CURVE.notes``: the release's
    own missing-data spellings are ``''``, ``NA``, ``ND``, ``N/A`` and the ``-999.99`` TAU2
    sentinel, the engine has no sentinel-to-null map, and ``columns.derive`` casts -- so a numeric
    column here would refuse cells the source publishes. ``None`` is the blank cell; a token stays
    the token.
    """

    date: date_type
    beta0: str | None = None
    beta1: str | None = None
    beta2: str | None = None
    beta3: str | None = None
    tau1: str | None = None
    tau2: str | None = None
    sven1f01: str | None = None
    sven1f04: str | None = None
    sven1f09: str | None = None
    svenf01: str | None = None
    svenf02: str | None = None
    svenf03: str | None = None
    svenf04: str | None = None
    svenf05: str | None = None
    svenf06: str | None = None
    svenf07: str | None = None
    svenf08: str | None = None
    svenf09: str | None = None
    svenf10: str | None = None
    svenf11: str | None = None
    svenf12: str | None = None
    svenf13: str | None = None
    svenf14: str | None = None
    svenf15: str | None = None
    svenf16: str | None = None
    svenf17: str | None = None
    svenf18: str | None = None
    svenf19: str | None = None
    svenf20: str | None = None
    svenf21: str | None = None
    svenf22: str | None = None
    svenf23: str | None = None
    svenf24: str | None = None
    svenf25: str | None = None
    svenf26: str | None = None
    svenf27: str | None = None
    svenf28: str | None = None
    svenf29: str | None = None
    svenf30: str | None = None
    svenpy01: str | None = None
    svenpy02: str | None = None
    svenpy03: str | None = None
    svenpy04: str | None = None
    svenpy05: str | None = None
    svenpy06: str | None = None
    svenpy07: str | None = None
    svenpy08: str | None = None
    svenpy09: str | None = None
    svenpy10: str | None = None
    svenpy11: str | None = None
    svenpy12: str | None = None
    svenpy13: str | None = None
    svenpy14: str | None = None
    svenpy15: str | None = None
    svenpy16: str | None = None
    svenpy17: str | None = None
    svenpy18: str | None = None
    svenpy19: str | None = None
    svenpy20: str | None = None
    svenpy21: str | None = None
    svenpy22: str | None = None
    svenpy23: str | None = None
    svenpy24: str | None = None
    svenpy25: str | None = None
    svenpy26: str | None = None
    svenpy27: str | None = None
    svenpy28: str | None = None
    svenpy29: str | None = None
    svenpy30: str | None = None
    sveny01: str | None = None
    sveny02: str | None = None
    sveny03: str | None = None
    sveny04: str | None = None
    sveny05: str | None = None
    sveny06: str | None = None
    sveny07: str | None = None
    sveny08: str | None = None
    sveny09: str | None = None
    sveny10: str | None = None
    sveny11: str | None = None
    sveny12: str | None = None
    sveny13: str | None = None
    sveny14: str | None = None
    sveny15: str | None = None
    sveny16: str | None = None
    sveny17: str | None = None
    sveny18: str | None = None
    sveny19: str | None = None
    sveny20: str | None = None
    sveny21: str | None = None
    sveny22: str | None = None
    sveny23: str | None = None
    sveny24: str | None = None
    sveny25: str | None = None
    sveny26: str | None = None
    sveny27: str | None = None
    sveny28: str | None = None
    sveny29: str | None = None
    sveny30: str | None = None


class FederalReserveFederalFundsRate(ContractModel):
    """One day of the effective federal funds rate: the published rate plus its distribution.

    The 13 fields are exactly the columns
    ``providers/federal_reserve/specs.py::FEDERAL_FUNDS_RATE`` declares, renamed off the feed's own
    keys (``effectiveDate``, ``percentRate``, ``targetRateTo``). ``date`` is the only non-null one:
    a row without an effective date is not a day of the series. Eleven of the remaining twelve are
    floats, and all eleven are nullable because the feed omits cells it has no observation for:
    nine of them (the rate, its two target-range bounds, the four percentiles and the intraday low
    and high) are published percents and carry the engine's scale stage, while ``volume`` (billions
    of USD) and ``standard_deviation`` are read as published. ``revision_indicator`` is the one
    non-numeric column.
    """

    date: date_type
    rate: float | None = None
    target_range_upper: float | None = None
    target_range_lower: float | None = None
    percentile_1: float | None = None
    percentile_25: float | None = None
    percentile_75: float | None = None
    percentile_99: float | None = None
    volume: float | None = None
    intraday_low: float | None = None
    intraday_high: float | None = None
    standard_deviation: float | None = None
    revision_indicator: str | None = None


class FederalReserveSofr(ContractModel):
    """One day of the secured overnight financing rate as the York Fed publishes it.

    The 7 fields are ``providers/federal_reserve/specs.py::SOFR``'s columns. The five percent
    columns are declared with a ``''``/``"''"``/``NA`` null sentinel because that feed spells its
    gaps three ways; ``volume`` has no such spelling in the body and is nullable only because the
    key can be absent. ``FederalReserveSofr`` rather than ``...SofrRate`` because the domain is the
    rate itself, and ``sofr`` -- the FRED leg of the same series -- keeps its own reviewed contract
    ``FredSofrObservation``.
    """

    date: date_type
    rate: float | None = None
    percentile_1: float | None = None
    percentile_25: float | None = None
    percentile_75: float | None = None
    percentile_99: float | None = None
    volume: float | None = None


class FederalReserveOvernightBankFundingRate(ContractModel):
    """One day of the overnight bank funding rate as the York Fed publishes it.

    The 7 fields are ``providers/federal_reserve/specs.py::OVERNIGHT_BANK_FUNDING_RATE``'s columns,
    the same shape as :class:`FederalReserveSofr` beside it and a different set of reasons. Five of
    the six numbers are percents the declaration folds to fractions with a ``columns.scale`` stage
    (rate, percentile_1, percentile_25, percentile_75, percentile_99); ``volume`` is published
    alongside them in billions of USD and carries no stage, exactly as for SOFR. Every non-date
    field is nullable because the key can be absent from the body.

    What separates this row from its sibling is the sentinel: SOFR's feed spells its gaps ``""``,
    ``"''"`` and ``NA`` and the declaration nulls all three ahead of the cast, while this one's
    validator guards only ``None`` and divides whatever is not None -- so this declaration names no
    sentinel, and a published token would reach the cast rather than become a null here. That is
    measured on the cited contract, not assumed from the sibling.
    """

    date: date_type
    rate: float | None = None
    percentile_1: float | None = None
    percentile_25: float | None = None
    percentile_75: float | None = None
    percentile_99: float | None = None
    volume: float | None = None


class FederalReserveYieldCurve(ContractModel):
    """One published H.15 nominal yield observation in long form.

    H.15 publishes rates as annual percentages in a wide CSV. The dedicated
    YieldCurve adapter expands its eleven published maturity columns into
    one row per date and maturity and converts those percentage points to
    decimal fractions.
    """

    date: date_type
    maturity: str
    rate: float | None = None


class FederalReservePrimaryDealerFail(ContractModel):
    """One New York Fed weekly primary-dealer settlement-fails observation.

    The API's numeric text is retained as an exact decimal in millions of
    U.S. dollars. ``key_id`` and ``description`` preserve the source series
    identity; New York Fed notes that releases may be revised.
    """

    date: date_type
    key_id: str
    description: str
    amount_usd_millions: Decimal


class FederalReservePrimaryDealerPosition(ContractModel):
    """One New York Fed weekly primary-dealer position observation.

    The API's numeric text is retained as an exact decimal in millions of
    U.S. dollars. ``key_id`` and ``description`` preserve the source series
    identity; New York Fed notes that releases may be revised.
    """

    date: date_type
    key_id: str
    description: str
    position_usd_millions: Decimal


class FederalReserveCentralBankHolding(ContractModel):
    """One SOMA summary, security detail, or weighted-average-maturity result.

    Monetary amounts use the public holdings contract's thousands-of-USD units. The NY Fed
    Markets API sends these amounts in dollars, so its adapter divides monetary source values by
    1,000 using exact ``Decimal`` arithmetic. WAM values remain years. The two WAM fields extend
    the public data-field list because the public query contract exposes ``wam=True`` while the
    NY Fed endpoint returns both values.
    """

    date: date_type
    security_type: str | None = None
    description: str | None = None
    is_aggregated: Literal["Y"] | None = None
    cusip: str | None = None
    issuer: str | None = None
    maturity_date: date_type | None = None
    term: str | None = None
    face_value: Decimal | None = None
    par_value: Decimal | None = None
    coupon: Decimal | None = None
    spread: Decimal | None = None
    percent_outstanding: Decimal | None = None
    bills: Decimal | None = None
    frn: Decimal | None = None
    notes_and_bonds: Decimal | None = None
    tips: Decimal | None = None
    mbs: Decimal | None = None
    cmbs: Decimal | None = None
    agencies: Decimal | None = None
    total: Decimal | None = None
    tips_inflation_compensation: Decimal | None = None
    change_prior_week: Decimal | None = None
    change_prior_year: Decimal | None = None
    wam: float | None = None
    security_types: tuple[str, ...] | None = None


class FederalReserveInflationExpectations(ContractModel):
    """Quarterly Philadelphia Fed SPF one-year and ten-year inflation expectations."""

    date: date_type
    infpgdp1yr: float | None = None
    infcpi1yr: float | None = None
    infcpi10yr: float | None = None


class FederalReserveTotalFactorProductivity(ContractModel):
    """One quarterly San Francisco Fed productivity workbook observation.

    Growth-rate fields are the source's annualized percentage changes. The
    vintage date comes from the workbook readme and identifies the revision
    vintage used for all rows in that file.
    """

    period: str
    vintage_date: date_type
    dY_prod: float | None = None  # noqa: N815 - source workbook column spelling
    dY_inc: float | None = None  # noqa: N815 - source workbook column spelling
    dY: float | None = None  # noqa: N815 - source workbook column spelling
    dhours: float | None = None
    dLP: float | None = None  # noqa: N815 - source workbook column spelling
    dk: float | None = None
    dLQ_BLS_interpolated: float | None = None  # noqa: N815 - source workbook column spelling
    dLQ_Aaronson_Sullivan: float | None = None  # noqa: N815 - source workbook column spelling
    dLQ: float | None = None  # noqa: N815 - source workbook column spelling
    alpha: float | None = None
    dtfp: float | None = None
    dutil: float | None = None
    dtfp_util: float | None = None
    relativePrice: float | None = None  # noqa: N815 - source workbook column spelling
    invShare: float | None = None  # noqa: N815 - source workbook column spelling
    dtfp_I: float | None = None  # noqa: N815 - source workbook column spelling
    dtfp_C: float | None = None  # noqa: N815 - source workbook column spelling
    du_invest: float | None = None
    du_consumption: float | None = None
    dtfp_I_util: float | None = None  # noqa: N815 - source workbook column spelling
    dtfp_C_util: float | None = None  # noqa: N815 - source workbook column spelling


class FederalReserveFomcDocument(ContractModel):
    """One official FOMC document link indexed from a Federal Reserve page.

    This contract describes document metadata and the canonical document
    URL. It does not represent parsed PDF/HTML document contents.
    """

    year: int
    document_date: date_type
    title: str
    document_type: str
    format: str
    url: str
    meeting_range: str | None = None
