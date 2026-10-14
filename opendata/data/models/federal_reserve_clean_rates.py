"""Typed clean-room contracts for Federal Reserve rate and money series."""

from __future__ import annotations

from datetime import date as date_type

from pydantic import Field

from opendata.data.models.base import ContractModel


class FederalReserveMoneyMeasure(ContractModel):
    """One monthly money-stock observation, reported in billions of dollars."""

    month: date_type = Field(description="Monthly observation date.")
    m1: float = Field(description="M1 money stock, billions of dollars.")
    m2: float = Field(description="M2 money stock, billions of dollars.")
    currency: float | None = Field(default=None, description="Currency, billions of dollars.")
    demand_deposits: float | None = Field(
        default=None, description="Demand deposits, billions of dollars."
    )
    retail_money_market_funds: float | None = Field(
        default=None, description="Retail money market funds, billions of dollars."
    )
    other_liquid_deposits: float | None = Field(
        default=None, description="Other liquid deposits, billions of dollars."
    )
    small_denomination_time_deposits: float | None = Field(
        default=None, description="Small-denomination time deposits, billions of dollars."
    )


class FederalReserveTreasuryRate(ContractModel):
    """One daily Treasury rate observation, retaining native rate bases."""

    date: date_type = Field(description="Daily observation date.")
    week_4: float | None = Field(
        default=None,
        description=(
            "4-week bill secondary-market discount-basis annual rate; this is not "
            "the investment-basis constant-maturity convention."
        ),
    )
    month_1: float | None = Field(
        default=None,
        description="1-month constant-maturity investment-basis rate, percent per year.",
    )
    month_2: float | None = Field(
        default=None,
        description=(
            "Public contract field; the selected H.15 package has no 2-month series, "
            "so this remains null pending a documented source mapping."
        ),
    )
    month_3: float | None = Field(
        default=None,
        description="3-month constant-maturity investment-basis rate, percent per year.",
    )
    month_6: float | None = Field(
        default=None,
        description="6-month constant-maturity investment-basis rate, percent per year.",
    )
    year_1: float | None = Field(
        default=None,
        description="1-year constant-maturity investment-basis rate, percent per year.",
    )
    year_2: float | None = Field(
        default=None,
        description="2-year constant-maturity investment-basis rate, percent per year.",
    )
    year_3: float | None = Field(
        default=None,
        description="3-year constant-maturity investment-basis rate, percent per year.",
    )
    year_5: float | None = Field(
        default=None,
        description="5-year constant-maturity investment-basis rate, percent per year.",
    )
    year_7: float | None = Field(
        default=None,
        description="7-year constant-maturity investment-basis rate, percent per year.",
    )
    year_10: float | None = Field(
        default=None,
        description="10-year constant-maturity investment-basis rate, percent per year.",
    )
    year_20: float | None = Field(
        default=None,
        description="20-year constant-maturity investment-basis rate, percent per year.",
    )
    year_30: float | None = Field(
        default=None,
        description="30-year constant-maturity investment-basis rate, percent per year.",
    )


_SVENSSON_FORWARD_DESCRIPTION = "".join(
    (
        "Instantaneous forward rate, continuously compounded; raw ",
        "source scale preserved.",
    )
)


class FederalReserveSvenssonYieldCurve(ContractModel):
    """Federal Reserve Svensson curve parameters and maturity yields."""

    date: date_type = Field(description="Daily observation date.")
    beta0: float | None = Field(
        default=None, description="Svensson level coefficient; raw Federal Reserve source value."
    )
    beta1: float | None = Field(
        default=None, description="Svensson slope coefficient; raw Federal Reserve source value."
    )
    beta2: float | None = Field(
        default=None,
        description="First Svensson curvature coefficient; raw Federal Reserve source value.",
    )
    beta3: float | None = Field(
        default=None,
        description="Second Svensson curvature coefficient; raw Federal Reserve source value.",
    )
    tau1: float | None = Field(
        default=None,
        description="First Svensson decay coefficient; raw Federal Reserve source value.",
    )
    tau2: float | None = Field(
        default=None,
        description="Second Svensson decay coefficient; raw Federal Reserve source value.",
    )
    sven1f01: float | None = Field(
        default=None,
        description="One-year forward rate, coupon-equivalent; raw source scale preserved.",
    )
    sven1f04: float | None = Field(
        default=None,
        description="One-year forward rate, coupon-equivalent; raw source scale preserved.",
    )
    sven1f09: float | None = Field(
        default=None,
        description="One-year forward rate, coupon-equivalent; raw source scale preserved.",
    )
    svenf01: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf02: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf03: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf04: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf05: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf06: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf07: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf08: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf09: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf10: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf11: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf12: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf13: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf14: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf15: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf16: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf17: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf18: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf19: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf20: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf21: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf22: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf23: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf24: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf25: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf26: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf27: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf28: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf29: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenf30: float | None = Field(
        default=None,
        description=_SVENSSON_FORWARD_DESCRIPTION,
    )
    svenpy01: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy02: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy03: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy04: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy05: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy06: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy07: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy08: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy09: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy10: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy11: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy12: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy13: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy14: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy15: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy16: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy17: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy18: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy19: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy20: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy21: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy22: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy23: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy24: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy25: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy26: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy27: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy28: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy29: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    svenpy30: float | None = Field(
        default=None, description="Par yield, coupon-equivalent; raw source scale preserved."
    )
    sveny01: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny02: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny03: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny04: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny05: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny06: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny07: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny08: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny09: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny10: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny11: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny12: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny13: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny14: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny15: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny16: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny17: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny18: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny19: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny20: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny21: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny22: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny23: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny24: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny25: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny26: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny27: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny28: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny29: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )
    sveny30: float | None = Field(
        default=None,
        description="Zero-coupon yield, continuously compounded; raw source scale preserved.",
    )


class FederalReserveOvernightBankFundingRate(ContractModel):
    """One NY Fed overnight bank funding rate observation."""

    date: date_type = Field(description="Effective date of the reference rate.")
    rate: float = Field(description="OBFR, percent per year as reported by NY Fed.")
    percentile_1: float | None = Field(default=None, description="1st transaction percentile.")
    percentile_25: float | None = Field(default=None, description="25th transaction percentile.")
    percentile_75: float | None = Field(default=None, description="75th transaction percentile.")
    percentile_99: float | None = Field(default=None, description="99th transaction percentile.")
    volume: float | None = Field(default=None, description="Notional volume, billions of dollars.")
    revision_indicator: str | None = Field(default=None, description="NY Fed revision indicator.")


class FederalReserveFederalFundsRate(ContractModel):
    """One NY Fed effective federal funds rate observation."""

    date: date_type = Field(description="Effective date of the reference rate.")
    rate: float = Field(description="Effective federal funds rate, percent per year.")
    target_range_upper: float | None = Field(default=None, description="Upper target-range bound.")
    target_range_lower: float | None = Field(default=None, description="Lower target-range bound.")
    percentile_1: float | None = Field(default=None, description="1st transaction percentile.")
    percentile_25: float | None = Field(default=None, description="25th transaction percentile.")
    percentile_75: float | None = Field(default=None, description="75th transaction percentile.")
    percentile_99: float | None = Field(default=None, description="99th transaction percentile.")
    volume: float | None = Field(default=None, description="Notional volume, billions of dollars.")
    intraday_low: float | None = Field(
        default=None,
        description=(
            "Pre-2016 intraday low from the NY Fed effr-pre-2016-record; "
            "not present on current records."
        ),
    )
    intraday_high: float | None = Field(
        default=None,
        description=(
            "Pre-2016 intraday high from the NY Fed effr-pre-2016-record; "
            "not present on current records."
        ),
    )
    standard_deviation: float | None = Field(
        default=None,
        description=(
            "Pre-2016 standard deviation from the NY Fed effr-pre-2016-record; "
            "not present on current records."
        ),
    )
    revision_indicator: str | None = Field(default=None, description="NY Fed revision indicator.")


__all__ = [
    "FederalReserveFederalFundsRate",
    "FederalReserveMoneyMeasure",
    "FederalReserveOvernightBankFundingRate",
    "FederalReserveSvenssonYieldCurve",
    "FederalReserveTreasuryRate",
]
