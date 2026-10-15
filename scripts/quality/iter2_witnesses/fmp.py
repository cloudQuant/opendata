"""Offline witness configuration for the fmp provider models.

Every entry here is a hand-written FMP adapter row: extraction goes through
``opendata.data.providers.fmp.models._client.fetch_array``, whose shape refusals are
:class:`FMPProviderError` instances that attribute the contract column pydantic rejected,
translated through the row model's own alias map. So each counterfact below names the
published column, never the wire key it was read from.

The 21 rows in this module are the financial-statement, macro and ``fmp_iter2`` family. Five
shards under ``_parts/fmp/`` carry the rest: ``rows_a`` the 8 market rows, ``rows_b`` the 8 ETF
rows, ``rows_c`` the 16 disclosure rows, ``rows_d`` the 4 calendars plus quote/screener/index, and
``rows_e`` the 7 rows whose frozen class was published only as ``record_model``. Measured against
the pinned inventory, fmp has 69 identities and 68 of them are witnessed
(``WITKEYS_NOT_FMP_IDENTITY=[]``, ``WITNESS_KEYS_WITHOUT_FROZEN_ROW=[]``).

Two claims this module used to make are now withdrawn, both because they were measured rather than
assumed:

* The four company calendars do not need a weaker fetcher or a page budget.
  ``_fetch_calendar_pages`` stops on an *empty* page, so the client seam's
  ``sequence=[rows, []]`` proves a bounded window in two requests. ``rows_d`` witnesses all four.
* The market, ETF and disclosure adapters are not row-less. They publish ``row_model`` already;
  seven further fetchers published the same class under the sibling name ``record_model`` and
  ``rows_e`` declares ``row_model`` alongside it, so every witnessed row class is the class the
  governed service serializes, not a label.

``EquityHistorical`` is the single inventory identity left unwitnessed, and it is a product gap
rather than a witness gap: ``EquityHistoricalFetcher``
(``opendata/data/providers/fmp/models/equity_historical.py:45``) is typed
``Fetcher[EquityHistoricalQuery, list[dict[str, Any]]]`` and declares neither ``row_model`` nor
``record_model``, so the witness instrument's own frozen-row requirement has nothing to read.

One further product observation is recorded without a witness: ``CryptoSearchFetcher`` and
``CurrencyPairsFetcher`` each override ``transform_data`` outright
(``iter2_market.py:894`` and ``:948``) and never consult ``key_fields``, so the
``FMP_DUPLICATE_ROW`` guard at ``iter2_market.py:685`` is unreachable on those two directory routes
even though ``CurrencyPairsFetcher`` re-declares the key at ``:938``.
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"


def _statement_row(**values: Any) -> dict[str, Any]:  # noqa: ANN401  # fixture JSON values
    """One FMP statement record: period identity plus the requested line items."""
    row: dict[str, Any] = {
        "symbol": "AAPL",
        "date": "2024-09-28",
        "fiscalYear": 2024,
        "period": "FY",
        "reportedCurrency": "USD",
    }
    row.update(values)
    return row


def _prior_year(row: dict[str, Any], year: int, period_ending: str) -> dict[str, Any]:
    """The same line items one fiscal year back, with the filing lineage FMP republishes."""
    prior: dict[str, Any] = {**row, "date": period_ending, "fiscalYear": year}
    if "filingDate" in row:
        prior["filingDate"] = "2023-11-03"
    if "acceptedDate" in row:
        prior["acceptedDate"] = "2023-11-03 17:41:12"
    return prior


def _period_only(row: dict[str, Any]) -> dict[str, Any]:
    """The period identity a growth route publishes, without statement line items."""
    return {key: row[key] for key in ("symbol", "date", "fiscalYear", "period", "reportedCurrency")}


def _fmp_balance_sheet_document() -> list[dict[str, Any]]:
    current = _statement_row(
        cik="0000320193",
        filingDate="2024-11-01",
        acceptedDate="2024-11-01 18:15:43",
        cashAndCashEquivalents=29943_000_000,
        shortTermInvestments=91_524_000_000,
        netReceivables=66_246_000_000,
        inventory=4_408_000_000,
        totalCurrentAssets=124_909_000_000,
        propertyPlantEquipmentNet=3_850_000_000,
        goodwill=260_000_000,
        totalAssets=351_652_000_000,
        accountPayables=68_960_000_000,
        shortTermDebt=0,
        taxPayables=26_683_000_000,
        totalCurrentLiabilities=176_420_000_000,
        longTermDebt=85_771_000_000,
    )
    prior = _prior_year(current, 2023, "2023-09-29")
    return [current, prior]


def _fmp_income_statement_document() -> list[dict[str, Any]]:
    current = _statement_row(
        cik="0000320193",
        filingDate="2024-11-01",
        acceptedDate="2024-11-01 18:15:43",
        revenue=391_035_000_000,
        costOfRevenue=210_352_000_000,
        grossProfit=180_683_000_000,
        researchAndDevelopmentExpenses=31_370_000_000,
        operatingExpenses=54_809_000_000,
        operatingIncome=125_874_000_000,
        incomeBeforeTax=125_532_000_000,
        incomeTaxExpense=14_891_000_000,
        netIncome=93_736_000_000,
        eps=6.08,
        weightedAverageShsOut=15_408_000_000,
    )
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_cash_flow_document() -> list[dict[str, Any]]:
    current = _statement_row(
        cik="0000320193",
        filingDate="2024-11-01",
        acceptedDate="2024-11-01 18:15:43",
        netIncome=93_736_000_000,
        depreciationAndAmortization=11_003_000_000,
        stockBasedCompensation=10_818_000_000,
        changeInWorkingCapital=10_643_000_000,
        netCashProvidedByOperatingActivities=111_037_000_000,
        investmentsInPropertyPlantAndEquipment=15_425_000_000,
        purchasesOfInvestments=25_316_000_000,
        salesMaturitiesOfInvestments=43_150_000_000,
        netCashProvidedByInvestingActivities=3_125_000_000,
        netDividendsPaid=15_279_000_000,
        commonStockRepurchased=78_291_000_000,
        netCashProvidedByFinancingActivities=-98_829_000_000,
        netChangeInCash=15_333_000_000,
        cashAtEndOfPeriod=42_351_000_000,
    )
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_balance_sheet_growth_document() -> list[dict[str, Any]]:
    base = _period_only(_fmp_balance_sheet_document()[0])
    current = {
        **base,
        "growthCashAndCashEquivalents": -0.149,
        "growthShortTermInvestments": -0.514,
        "growthCashAndShortTermInvestments": -0.415,
        "growthNetReceivables": 0.037,
        "growthInventory": 0.356,
        "growthOtherCurrentAssets": -0.011,
        "growthTotalCurrentAssets": 0.022,
        "growthPropertyPlantEquipmentNet": -0.038,
        "growthGoodwill": 0.0,
        "growthIntangibleAssets": -0.071,
        "growthLongTermInvestments": -0.122,
        "growthTotalNonCurrentAssets": -0.084,
        "growthTotalAssets": -0.057,
        "growthAccountPayables": 0.234,
        "growthShortTermDebt": 0.0,
        "growthTaxPayables": -0.181,
        "growthDeferredRevenue": 0.105,
        "growthTotalCurrentLiabilities": 0.061,
        "growthLongTermDebt": -0.383,
        "growthOtherNonCurrentLiabilities": -0.091,
        "growthTotalNonCurrentLiabilities": -0.142,
        "growthTotalLiabilities": -0.062,
    }
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_income_statement_growth_document() -> list[dict[str, Any]]:
    current = {
        **_period_only(_fmp_income_statement_document()[0]),
        "growthRevenue": 0.0202,
        "growthCostOfRevenue": -0.0019,
        "growthGrossProfit": 0.0465,
        "growthGrossProfitRatio": 0.907,
        "growthResearchAndDevelopmentExpenses": 0.186,
        "growthGeneralAndAdministrativeExpenses": 0.064,
        "growthSellingAndMarketingExpenses": 0.071,
        "growthOtherExpenses": -0.022,
        "growthOperatingExpenses": 0.061,
        "growthCostAndExpenses": 0.049,
        "growthInterestIncome": 0.118,
        "growthInterestExpense": -0.034,
        "growthDepreciationAndAmortization": 0.087,
        "growthEBITDA": 0.058,
        "growthEBIT": 0.052,
        "growthOperatingIncome": 0.052,
        "growthIncomeBeforeTax": 0.073,
        "growthIncomeTaxExpense": -0.196,
        "growthNetIncome": 0.084,
        "growthEPS": 0.097,
        "growthEPSDiluted": 0.097,
        "growthWeightedAverageShsOut": -0.027,
        "growthWeightedAverageShsOutDil": -0.029,
    }
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_cash_flow_growth_document() -> list[dict[str, Any]]:
    current = {
        **_period_only(_fmp_cash_flow_document()[0]),
        "growthNetIncome": 0.084,
        "growthDepreciationAndAmortization": 0.087,
        "growthDeferredIncomeTax": -0.241,
        "growthStockBasedCompensation": 0.091,
        "growthChangeInWorkingCapital": 1.31,
        "growthAccountsReceivables": -0.172,
        "growthInventory": 0.356,
        "growthAccountsPayables": 0.234,
        "growthOtherWorkingCapital": 0.019,
        "growthOtherNonCashItems": -0.402,
        "growthNetCashProvidedByOperatingActivites": 0.034,
        "growthInvestmentsInPropertyPlantAndEquipment": -0.105,
        "growthAcquisitionsNet": 0.0,
        "growthPurchasesOfInvestments": -0.229,
        "growthSalesMaturitiesOfInvestments": -0.117,
        "growthOtherInvestingActivites": 0.288,
        "growthNetCashUsedForInvestingActivites": -0.601,
        "growthDebtRepayment": -0.089,
        "growthCommonStockIssued": 0.004,
        "growthCommonStockRepurchased": -0.103,
        "growthPreferredDividendsPaid": 0.0,
        "growthDividendsPaid": 0.036,
        "growthOtherFinancingActivites": -0.162,
        "growthNetCashUsedProvidedByFinancingActivities": -0.106,
        "growthEffectOfForexChangesOnCash": 0.0,
        "growthNetChangeInCash": 1.597,
        "growthCashAtEndOfPeriod": 0.109,
        "growthCashAtBeginningOfPeriod": 0.351,
        "growthOperatingCashFlow": 0.034,
        "growthCapitalExpenditure": -0.105,
        "growthFreeCashFlow": 0.081,
    }
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_financial_ratios_document() -> list[dict[str, Any]]:
    current = _statement_row(
        grossProfitMargin=0.462,
        ebitMargin=0.321,
        ebitdaMargin=0.349,
        operatingProfitMargin=0.321,
        pretaxProfitMargin=0.321,
        netProfitMargin=0.2397,
        receivablesTurnover=6.69,
        inventoryTurnover=35.05,
        assetTurnover=1.21,
        currentRatio=0.855,
        quickRatio=0.83,
        cashRatio=0.179,
        priceToEarningsRatio=35.1,
        priceToBookRatio=54.6,
        debtToEquityRatio=1.87,
        debtToAssetsRatio=0.8,
        financialLeverageRatio=4.33,
    )
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _fmp_key_metrics_document() -> list[dict[str, Any]]:
    current = _statement_row(
        marketCap=3_765_000_000_000,
        enterpriseValue=3_468_000_000_000,
        evToSales=8.87,
        evToOperatingCashFlow=31.2,
        evToFreeCashFlow=42.7,
        evToEBITDA=25.3,
        netDebtToEBITDA=0.68,
        currentRatio=0.855,
        incomeQuality=1.001,
        grahamNumber=33.05,
        grahamNetNet=6.98,
        taxBurden=0.881,
        interestBurden=1.0,
        workingCapital=-51_511_000_000,
        investedCapital=274_942_000_000,
        returnOnAssets=0.28,
        returnOnEquity=1.038,
        earningsYield=0.0249,
        freeCashFlowYield=0.028,
        capexToOperatingCashFlow=0.139,
    )
    return [current, _prior_year(current, 2023, "2023-09-29")]


def _segment_period(period_ending: str, fiscal_year: int, data: dict[str, int]) -> dict[str, Any]:
    """One FMP flat segmentation record: period identity plus a named value map."""
    return {
        "symbol": "AAPL",
        "date": period_ending,
        "fiscalYear": fiscal_year,
        "period": "FY",
        "reportedCurrency": "USD",
        "data": data,
    }


def _fmp_revenue_business_line_document() -> list[dict[str, Any]]:
    return [
        _segment_period(
            "2024-09-28",
            2024,
            {
                "iPhone": 201_185_000_000,
                "Mac": 29_984_000_000,
                "iPad": 26_694_000_000,
                "Wearables, Home and Accessories": 37_005_000_000,
                "Services": 96_169_000_000,
            },
        ),
        _segment_period(
            "2023-09-29",
            2023,
            {
                "iPhone": 194_102_000_000,
                "Mac": 28_885_000_000,
                "iPad": 28_414_000_000,
                "Wearables, Home and Accessories": 37_887_000_000,
                "Services": 83_736_000_000,
            },
        ),
    ]


def _fmp_revenue_geographic_document() -> list[dict[str, Any]]:
    return [
        _segment_period(
            "2024-09-28",
            2024,
            {
                "United States": 169_698_000_000,
                "Greater China": 68_962_000_000,
                "Europe": 91_840_000_000,
                "Other Americas": 24_490_000_000,
                "Japan": 24_047_000_000,
                "Rest of Asia Pacific": 12_048_000_000,
            },
        ),
        _segment_period(
            "2023-09-29",
            2023,
            {
                "United States": 162_044_000_000,
                "Greater China": 72_559_000_000,
                "Europe": 89_743_000_000,
                "Other Americas": 24_212_000_000,
                "Japan": 22_580_000_000,
                "Rest of Asia Pacific": 13_163_000_000,
            },
        ),
    ]


def _fmp_available_indices_document() -> list[dict[str, Any]]:
    def index(symbol: str, name: str, exchange: str) -> dict[str, Any]:
        return {"symbol": symbol, "name": name, "exchange": exchange, "currency": "USD"}

    return [
        index("SPX", "S&P 500", "US Equity Markets"),
        index("DJI", "Dow Jones Industrial Average", "US Equity Markets"),
        index("NDX", "Nasdaq 100", "US Equity Markets"),
    ]


def _fmp_economic_calendar_document() -> list[dict[str, Any]]:
    return [
        {
            "date": "2026-01-14 08:30:00",
            "country": "US",
            "event": "Consumer Price Index (YoY)",
            "currency": "USD",
            "previous": 2.6,
            "estimate": 2.7,
            "actual": 2.7,
            "change": 0.3,
            "changePercentage": 0.0385,
            "impact": "High",
            "unit": "%",
        },
        {
            "date": "2026-01-15 13:00:00",
            "country": "US",
            "event": "Index of Leading Economic Measures",
            "currency": "USD",
            "previous": 102.4,
            "estimate": None,
            "actual": 102.8,
            "change": 0.4,
            "changePercentage": 0.0039,
            "impact": None,
            "unit": "index",
        },
    ]


def _treasury_row(day: str, front: float, long_end: float) -> dict[str, Any]:
    """One wide FMP Treasury record: every documented maturity for one date."""
    maturities = {
        "month1": front,
        "month2": front - 0.04,
        "month3": front - 0.07,
        "month6": front - 0.1,
        "year1": front - 0.14,
        "year2": front - 0.17,
        "year3": long_end - 0.1,
        "year5": long_end - 0.06,
        "year7": long_end - 0.03,
        "year10": long_end,
        "year20": long_end + 0.09,
        "year30": long_end + 0.12,
    }
    return {"date": day, **maturities}


def _fmp_treasury_rates_document() -> list[dict[str, Any]]:
    return [_treasury_row("2026-01-14", 4.28, 4.18), _treasury_row("2026-01-15", 4.26, 4.15)]


def _fmp_yield_curve_document() -> list[dict[str, Any]]:
    return _fmp_treasury_rates_document()


def _fmp_risk_premium_document() -> list[dict[str, Any]]:
    return [
        {
            "country": "United States",
            "continent": "North America",
            "countryRiskPremium": 0.0,
            "totalEquityRiskPremium": 0.0451,
        },
        {
            "country": "Germany",
            "continent": "Europe",
            "countryRiskPremium": 0.0055,
            "totalEquityRiskPremium": 0.0506,
        },
    ]


def _estimate_row(day: str) -> dict[str, Any]:
    """One FMP analyst-estimates record with every published consensus band."""
    return {
        "symbol": "AAPL",
        "date": day,
        "revenueLow": 133_922_000_000,
        "revenueHigh": 143_377_000_000,
        "revenueAvg": 138_500_000_000,
        "ebitdaLow": 41_721_000_000,
        "ebitdaHigh": 46_590_000_000,
        "ebitdaAvg": 44_123_000_000,
        "ebitLow": 34_111_000_000,
        "ebitHigh": 38_902_000_000,
        "ebitAvg": 36_488_000_000,
        "netIncomeLow": 26_842_000_000,
        "netIncomeHigh": 30_511_000_000,
        "netIncomeAvg": 28_544_000_000,
        "sgaExpenseLow": 14_002_000_000,
        "sgaExpenseHigh": 15_884_000_000,
        "sgaExpenseAvg": 14_943_000_000,
        "epsAvg": 1.85,
        "epsHigh": 1.98,
        "epsLow": 1.63,
        "numAnalystsRevenue": 22,
        "numAnalystsEps": 27,
    }


def _fmp_analyst_estimates_document() -> list[dict[str, Any]]:
    return [_estimate_row("2026-03-28"), _estimate_row("2026-06-27")]


def _fmp_esg_score_document() -> list[dict[str, Any]]:
    def disclosure(day: str, accepted: str, form: str, scores: dict[str, float]) -> dict[str, Any]:
        return {
            "date": day,
            "acceptedDate": accepted,
            "symbol": "AAPL",
            "cik": "0000320193",
            "companyName": "Apple Inc.",
            "formType": form,
            "environmentalScore": scores["environmental"],
            "socialScore": scores["social"],
            "governanceScore": scores["governance"],
            "ESGScore": scores["total"],
            "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000031/",
        }

    return [
        disclosure(
            "2024-03-25",
            "2024-03-25",
            "10-K",
            {"environmental": 45.0, "social": 62.0, "governance": 55.0, "total": 54.0},
        ),
        disclosure(
            "2024-05-02",
            "2024-05-02",
            "ARSG",
            {"environmental": 41.0, "social": 58.0, "governance": 51.0, "total": 50.0},
        ),
    ]


def _nport_holding(cusip: str, security: str, issuer: str, percent: float) -> dict[str, Any]:
    """One N-PORT holding as the fund disclosure route publishes it."""
    return {
        "cik": "0000928876",
        "date": "2024-09-30",
        "acceptedDate": "2024-11-04",
        "symbol": security,
        "name": issuer,
        "lei": None,
        "title": f"{issuer} common stock",
        "cusip": cusip,
        "isin": None,
        "balance": 1_204_500,
        "units": "SH",
        "cur_cd": "USD",
        "valUsd": 228_571_000,
        "pctVal": percent,
        "payoffProfile": "Long",
        "assetCat": "ECM",
        "issuerCat": "CORP",
        "invCountry": "US",
        "isRestrictedSec": "N",
        "fairValLevel": "1",
        "isCashCollateral": "N",
        "isNonCashCollateral": "N",
        "isLoanByFund": "N",
    }


def _fmp_nport_disclosure_document() -> list[dict[str, Any]]:
    return [
        _nport_holding("037833100", "GOOGL", "Alphabet Inc.", 3.412),
        _nport_holding("67066G104", "NVDA", "NVIDIA Corp.", 2.987),
    ]


def _fmp_calendar_ipo_document() -> list[dict[str, Any]]:
    return [
        {
            "symbol": "ACQUIS",
            "date": "2026-01-06",
            "daa": "2026-01-02 07:00:00",
            "company": "Acquisition Ventures Ltd.",
            "exchange": "NASDAQ",
            "actions": "InitialPublicOffering",
            "shares": 12_500_000,
            "priceRange": "18.00 - 20.00",
            "marketCap": 1_350_000_000,
            "dealSize": 250_000_000,
        },
        {
            "symbol": "NORTHBR",
            "date": "2026-01-08",
            "daa": "2026-01-05 07:30:00",
            "company": "Northbridge Rail Holdings",
            "exchange": "NYSE",
            "actions": "InitialPublicOffering",
            "shares": 34_000_000,
            "priceRange": "21.50 - 24.50",
            "marketCap": 4_080_000_000,
            "dealSize": 807_500_000,
        },
    ]


_CONTRACTS_FINANCIALS = [
    "opendata/data/models/fmp_financials.py",
    "opendata/data/domains.yaml",
]
_CONTRACTS_ITER2 = [
    "opendata/data/models/fmp_iter2.py",
    "opendata/data/domains.yaml",
]
_SOURCE_FINANCIALS = ["opendata/data/providers/fmp/models/iter2_financials.py", _FMP_SOURCE]
_SOURCE_ITER2 = ["opendata/data/providers/fmp/models/iter2.py", _FMP_SOURCE]
_SOURCE_MACRO = ["opendata/data/providers/fmp/models/iter2_analyst_macro.py", _FMP_SOURCE]

_SHAPE = {"refusal_class": "FMPProviderError", "refusal_code": "FMP_BAD_SHAPE"}


def _entry(
    *,
    fixture: Any,  # noqa: ANN401  # fixture builder callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    reason: str,
    source_files: list[str],
    contract_files: list[str],
    missing: list[str] | None = None,
    rejected: list[str] | None = None,
    invalid: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble one witness entry from the shared typed-shape refusal face."""
    entry: dict[str, Any] = {
        "fixture_document": fixture,
        "query": query,
        "tamper": tamper,
        "tamper_reason": reason,
        "source_files": source_files,
        "contract_files": contract_files,
        **_SHAPE,
    }
    if missing:
        entry["expected_missing"] = missing
    if rejected:
        entry["expected_rejected"] = rejected
    if invalid:
        entry["expected_invalid"] = invalid
    return entry


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.fmp.registration",
    "credential_env": {"FMP_API_KEY": "fixture-token-never-sent"},
    "allowed_hosts": ["financialmodelingprep.com"],
    "models": {
        "BalanceSheet": _entry(
            fixture=_fmp_balance_sheet_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={"op": "drop", "path": [0, "fiscalYear"], "column": "fiscal_year"},
            missing=["fiscal_year"],
            reason=(
                "the balance-sheet route stamps every period with fiscalYear, and the contract "
                "requires it as an uncoerced four-digit year; a period row without it cannot "
                "state which fiscal year it reports, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing missing=('fiscal_year',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "BalanceSheetGrowth": _entry(
            fixture=_fmp_balance_sheet_growth_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={
                "op": "set",
                "path": [0, "growthTotalAssets"],
                "value": "-5.7%",
                "column": "growth_total_assets",
            },
            invalid=["growth_total_assets"],
            reason=(
                "the growth route publishes a year-over-year ratio as a JSON number; a percent-"
                "formatted string is not that number, and pydantic would keep the row by "
                "dropping the cell, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "invalid=('growth_total_assets',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "CashFlowStatement": _entry(
            fixture=_fmp_cash_flow_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={"op": "drop", "path": [0, "date"], "column": "period_ending"},
            missing=["period_ending"],
            reason=(
                "the cash-flow route names the period an statement closes with 'date'; without "
                "it the row has no period end the natural key can identify, so the adapter must "
                "refuse with FMP_BAD_SHAPE attributing missing=('period_ending',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "CashFlowStatementGrowth": _entry(
            fixture=_fmp_cash_flow_growth_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={
                "op": "set",
                "path": [0, "growthNetIncome"],
                "value": True,
                "column": "growth_net_income",
            },
            invalid=["growth_net_income"],
            reason=(
                "a growth ratio is a signed percentage, never a flag; pydantic lax mode widens "
                "True to 1.0 and would publish a 100% net-income growth, so the adapter must "
                "refuse with FMP_BAD_SHAPE attributing invalid=('growth_net_income',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "IncomeStatement": _entry(
            fixture=_fmp_income_statement_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={"op": "set", "path": [0, "revenue"], "value": "three hundred ninety billion"},
            invalid=["revenue"],
            reason=(
                "the income statement publishes revenue in reported-currency units as a JSON "
                "integer; prose where a statement amount belongs is not an amount, so the "
                "adapter must refuse with FMP_BAD_SHAPE attributing invalid=('revenue',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "IncomeStatementGrowth": _entry(
            fixture=_fmp_income_statement_growth_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={
                "op": "set",
                "path": [0, "growthEBITDA"],
                "value": "8.4%",
                "column": "growth_ebitda",
            },
            invalid=["growth_ebitda"],
            reason=(
                "the growth route already states EBITDA growth as a decimal ratio, so a "
                "percent-formatted string is a different unit rather than the same number; the "
                "adapter must refuse with FMP_BAD_SHAPE attributing "
                "invalid=('growth_ebitda',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "FinancialRatios": _entry(
            fixture=_fmp_financial_ratios_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={"op": "drop", "path": [0, "period"], "column": "fiscal_period"},
            missing=["fiscal_period"],
            reason=(
                "the ratios route publishes 'period' to say whether a ratio set is annual or a "
                "quarter, and the requested frequency is checked against it; a row without it "
                "cannot be placed on the fiscal calendar, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing missing=('fiscal_period',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "KeyMetrics": _entry(
            fixture=_fmp_key_metrics_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={"op": "drop", "path": [0, "symbol"]},
            missing=["symbol"],
            reason=(
                "every key-metrics row is published under the issuer's symbol; without it a "
                "capital-structure ratio has no owner, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing missing=('symbol',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "RevenueBusinessLine": _entry(
            fixture=_fmp_revenue_business_line_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={
                "op": "set",
                "path": [0, "date"],
                "value": "09/28/2024",
                "column": "period_ending",
            },
            invalid=["period_ending"],
            reason=(
                "the flat segmentation record carries its period identity alongside the named "
                "value map, and 'date' is documented as an exact ISO date; a locale-formatted "
                "string is a different cell than the one the contract declares, so the adapter "
                "must refuse with FMP_BAD_SHAPE attributing invalid=('period_ending',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "RevenueGeographic": _entry(
            fixture=_fmp_revenue_geographic_document,
            query={"symbol": "AAPL", "period": "annual"},
            tamper={
                "op": "set",
                "path": [0, "data", "Europe"],
                "value": "91.8B",
                "column": "revenue",
            },
            invalid=["revenue"],
            reason=(
                "each named region in a flat segmentation map carries that region's revenue in "
                "reported-currency units as a JSON number; a magnitude-suffixed string under a "
                "region name is not a segment amount, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing invalid=('revenue',)"
            ),
            source_files=_SOURCE_FINANCIALS,
            contract_files=_CONTRACTS_FINANCIALS,
        ),
        "AvailableIndices": _entry(
            fixture=_fmp_available_indices_document,
            query={},
            tamper={"op": "drop", "path": [0, "exchange"]},
            missing=["exchange"],
            reason=(
                "the index directory publishes the market an index is calculated for, and "
                "(symbol, exchange) is the reviewed natural key; a listing without 'exchange' is "
                "not addressable, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "missing=('exchange',)"
            ),
            source_files=_SOURCE_ITER2,
            contract_files=_CONTRACTS_ITER2,
        ),
        "EconomicCalendar": _entry(
            fixture=_fmp_economic_calendar_document,
            query={"start_date": "2026-01-12", "end_date": "2026-01-16"},
            tamper={
                "op": "set",
                "path": [0, "changePercentage"],
                "value": "3.85%",
                "column": "change_percentage",
            },
            invalid=["change_percentage"],
            reason=(
                "the economic calendar publishes changePercentage as the numeric period-over-"
                "period change; a percent-formatted string is not that number, and the contract "
                "keeps the cell rather than inventing a value, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing invalid=('change_percentage',)"
            ),
            source_files=_SOURCE_ITER2,
            contract_files=_CONTRACTS_ITER2,
        ),
        "TreasuryRates": _entry(
            fixture=_fmp_treasury_rates_document,
            query={"start_date": "2026-01-14", "end_date": "2026-01-15"},
            tamper={"op": "drop", "path": [0, "year30"]},
            missing=["year30"],
            reason=(
                "one Treasury observation carries every documented maturity, the 30-year column "
                "included, even when the rate is unreported for a session; a wide row that omits "
                "the key does not state the long end, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing missing=('year30',)"
            ),
            source_files=_SOURCE_ITER2,
            contract_files=_CONTRACTS_ITER2,
        ),
        "YieldCurve": _entry(
            fixture=_fmp_yield_curve_document,
            query={"start_date": "2026-01-14", "end_date": "2026-01-15"},
            tamper={"op": "set", "path": [0, "year10"], "value": "four point one five"},
            invalid=["year10"],
            reason=(
                "the curve is expanded from the wide Treasury row, whose 10-year column is a JSON "
                "number in percentage points; prose in that cell cannot become a maturity "
                "observation, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "invalid=('year10',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "RiskPremium": _entry(
            fixture=_fmp_risk_premium_document,
            query={},
            tamper={
                "op": "drop",
                "path": [0, "totalEquityRiskPremium"],
                "column": "total_equity_risk_premium",
            },
            missing=["total_equity_risk_premium"],
            reason=(
                "the country route publishes the total equity risk premium next to the country "
                "component, and the contract requires both; a row without the total states only "
                "the sovereign add-on, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "missing=('total_equity_risk_premium',)"
            ),
            source_files=_SOURCE_ITER2,
            contract_files=_CONTRACTS_ITER2,
        ),
        "AnalystEstimates": _entry(
            fixture=_fmp_analyst_estimates_document,
            query={"symbol": "AAPL", "period": "annual", "limit": 5},
            tamper={"op": "drop", "path": [0, "epsAvg"], "column": "eps_average"},
            missing=["eps_average"],
            reason=(
                "an analyst-estimates row is the consensus band, and epsAvg is its central "
                "estimate; a record without it is a range without a consensus, so the adapter "
                "must refuse with FMP_BAD_SHAPE attributing missing=('eps_average',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "ForwardEbitdaEstimates": _entry(
            fixture=_fmp_analyst_estimates_document,
            query={"symbol": "AAPL", "period": "annual", "limit": 5},
            tamper={
                "op": "set",
                "path": [0, "ebitdaLow"],
                "value": "41.7B",
                "column": "ebitda_low",
            },
            invalid=["ebitda_low"],
            reason=(
                "the projection reads the EBITDA low end straight out of the consensus row, "
                "where it is published in currency units as a JSON number; a magnitude-suffixed "
                "string is not that number, so the adapter must refuse with FMP_BAD_SHAPE "
                "attributing invalid=('ebitda_low',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "ForwardEpsEstimates": _entry(
            fixture=_fmp_analyst_estimates_document,
            query={"symbol": "AAPL", "period": "annual", "limit": 5},
            tamper={
                "op": "drop",
                "path": [0, "numAnalystsEps"],
                "column": "num_analysts_eps",
            },
            missing=["num_analysts_eps"],
            reason=(
                "the forward EPS row carries the analyst count behind the consensus, which is "
                "what makes the band interpretable; a projection without it cannot report "
                "coverage, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "missing=('num_analysts_eps',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "EsgScore": _entry(
            fixture=_fmp_esg_score_document,
            query={"symbol": "AAPL"},
            tamper={
                "op": "drop",
                "path": [0, "ESGScore"],
                "column": "esg_score",
            },
            missing=["esg_score"],
            reason=(
                "each ESG disclosure publishes the total score next to its three components, and "
                "the contract requires it; a filing without ESGScore cannot be ordered against "
                "another, so the adapter must refuse with FMP_BAD_SHAPE attributing "
                "missing=('esg_score',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "NportDisclosure": _entry(
            fixture=_fmp_nport_disclosure_document,
            query={"symbol": "VTSYX", "year": 2024, "quarter": 1},
            tamper={"op": "drop", "path": [0, "pctVal"], "column": "portfolio_percent"},
            missing=["portfolio_percent"],
            reason=(
                "an N-PORT holding is published with its share of the portfolio, which is the "
                "only reason a balance and a dollar value are meaningful together; a holding "
                "without pctVal is not a disclosure row, so the adapter must refuse with "
                "FMP_BAD_SHAPE attributing missing=('portfolio_percent',)"
            ),
            source_files=_SOURCE_MACRO,
            contract_files=_CONTRACTS_ITER2,
        ),
        "CalendarIpo": _entry(
            fixture=_fmp_calendar_ipo_document,
            query={"start_date": "2026-01-05", "end_date": "2026-01-09"},
            tamper={"op": "drop", "path": [0, "dealSize"], "column": "deal_size"},
            missing=["deal_size"],
            reason=(
                "the IPO calendar states the size of the offering for every listed deal, and the "
                "contract requires the cell (null when the deal has not priced it, never absent); "
                "a row without dealSize is not a calendar record, so the adapter must refuse "
                "with FMP_BAD_SHAPE attributing missing=('deal_size',)"
            ),
            source_files=_SOURCE_ITER2,
            contract_files=_CONTRACTS_ITER2,
        ),
    },
}
