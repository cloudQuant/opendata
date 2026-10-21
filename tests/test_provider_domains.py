"""Production-domain declarations bind to canonical local provider models."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from opendata.data.domains import contract_model, load_domains, require_domain_semantics
from opendata.data.providers.catalog import (
    engine_declared_models,
    list_providers,
    register_providers,
)
from opendata.data.registry import ProviderRegistry
from opendata.services.api_key_service import key_allows_domain, normalize_scope_input

if TYPE_CHECKING:
    from opendata.data.capability import Capability


LEGACY_DOMAIN_IDS = {
    "stock_daily",
    "economy_cpi",
    "economy_gdp",
    "economy_rate",
    "economy_unemployment",
    "stock_daily_overseas",
    "stock_adjust",
    "stock_action",
    "financial_statement",
    "financial_indicator",
    "index_constituent",
    "futures_daily",
    "index_daily",
    "fund_etf_daily",
    "option_daily",
    "bond_daily",
    "futures_fundamentals",
    "instrument",
    "trading_calendar",
    "fund_action",
}

EXPECTED_DOMAINS = {
    "fred_search": (
        "fred",
        "FredSearch",
        "FredSearchFetcher",
        "SeriesCatalogItem",
        "snapshot",
        "last_updated",
        ("series_id",),
        ("series_id", "frequency", "units", "seasonal_adjustment"),
        "transient",
        ("query",),
    ),
    "fred_series": (
        "fred",
        "FredSeries",
        "FredSeriesFetcher",
        "SeriesObservation",
        "series",
        "date",
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "upsert",
        ("query", "store", "export"),
    ),
    "sofr": (
        "fred",
        "SOFR",
        "FredSofrFetcher",
        "FredSofrObservation",
        "series",
        "date",
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "transient",
        ("query",),
    ),
    "sonia": (
        "fred",
        "SONIA",
        "FredSoniaFetcher",
        "FredSoniaObservation",
        "series",
        "date",
        (
            "series_id",
            "parameter",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        (
            "series_id",
            "parameter",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "transient",
        ("query",),
    ),
    "bls_search": (
        "bls",
        "BlsSearch",
        "BlsSearchFetcher",
        "BlsCatalogItem",
        "snapshot",
        "catalog_as_of",
        ("survey", "series_id"),
        ("survey", "series_id"),
        "transient",
        ("query",),
    ),
    "bls_series": (
        "bls",
        "BlsSeries",
        "BlsSeriesFetcher",
        "BlsObservation",
        "series",
        "year",
        ("series_id", "year", "period"),
        ("series_id", "year", "period", "api_version"),
        "upsert",
        ("query", "store", "export"),
    ),
    "equity_historical": (
        "fmp",
        "EquityHistorical",
        "EquityHistoricalFetcher",
        "EquityHistorical",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date", "query_window_scope", "close_adjustment_semantics"),
        "upsert",
        ("query", "store", "export"),
    ),
    "equity_quote": (
        "fmp",
        "EquityQuote",
        "EquityQuoteFetcher",
        "EquityQuote",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "exchange"),
        "transient",
        ("query",),
    ),
    "currency_reference_rates": (
        "ecb",
        "CurrencyReferenceRates",
        "EcbCurrencyReferenceRatesFetcher",
        "CurrencyReferenceRate",
        "series",
        "date",
        ("series_key", "date"),
        ("series_key", "date", "quote_currency", "base_currency"),
        "transient",
        ("query",),
    ),
    "yield_curve": (
        "ecb",
        "YieldCurve",
        "EcbYieldCurveFetcher",
        "EcbYieldCurveObservation",
        "series",
        "date",
        ("series_key", "date"),
        ("series_key", "date", "frequency", "ref_area", "currency"),
        "transient",
        ("query",),
    ),
    "balance_of_payments": (
        "ecb",
        "BalanceOfPayments",
        "EcbBalanceOfPaymentsFetcher",
        "EcbBalanceOfPaymentsObservation",
        "series",
        "period",
        ("series_key", "period"),
        ("series_key", "period", "frequency", "ref_area", "counterpart_area", "unit_measure"),
        "transient",
        ("query",),
    ),
    "fmp_index_directory": (
        "fmp",
        "AvailableIndices",
        "AvailableIndicesFetcher",
        "AvailableIndex",
        "snapshot",
        None,
        ("symbol", "exchange"),
        ("symbol", "exchange", "currency"),
        "transient",
        ("query",),
    ),
    "fmp_dividend_calendar": (
        "fmp",
        "CalendarDividend",
        "CalendarDividendFetcher",
        "DividendCalendarEvent",
        "event",
        "date",
        ("symbol", "date"),
        ("symbol", "date", "record_date", "payment_date", "declaration_date"),
        "transient",
        ("query",),
    ),
    "fmp_earnings_calendar": (
        "fmp",
        "CalendarEarnings",
        "CalendarEarningsFetcher",
        "EarningsCalendarEvent",
        "event",
        "date",
        ("symbol", "date"),
        ("symbol", "date"),
        "transient",
        ("query",),
    ),
    "fmp_etf_countries": (
        "fmp",
        "EtfCountries",
        "EtfCountriesFetcher",
        "FmpEtfCountry",
        "snapshot",
        None,
        ("symbol", "country"),
        ("symbol", "country"),
        "transient",
        ("query",),
    ),
    "fmp_etf_equity_exposure": (
        "fmp",
        "EtfEquityExposure",
        "EtfEquityExposureFetcher",
        "FmpEtfEquityExposure",
        "snapshot",
        None,
        ("equity_symbol", "etf_symbol"),
        ("equity_symbol", "etf_symbol"),
        "transient",
        ("query",),
    ),
    "fmp_etf_holdings": (
        "fmp",
        "EtfHoldings",
        "EtfHoldingsFetcher",
        "FmpEtfHolding",
        "snapshot",
        None,
        ("symbol", "asset"),
        ("symbol", "asset"),
        "transient",
        ("query",),
    ),
    "fmp_etf_info": (
        "fmp",
        "EtfInfo",
        "EtfInfoFetcher",
        "FmpEtfInfo",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "asset_class"),
        "transient",
        ("query",),
    ),
    "fmp_etf_price_performance": (
        "fmp",
        "EtfPricePerformance",
        "EtfPricePerformanceFetcher",
        "FmpEtfPricePerformance",
        "snapshot",
        None,
        ("symbol",),
        ("symbol",),
        "transient",
        ("query",),
    ),
    "fmp_etf_search": (
        "fmp",
        "EtfSearch",
        "EtfSearchFetcher",
        "FmpEtfSearch",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "name"),
        "transient",
        ("query",),
    ),
    "fmp_etf_sectors": (
        "fmp",
        "EtfSectors",
        "EtfSectorsFetcher",
        "FmpEtfSector",
        "snapshot",
        None,
        ("symbol", "sector"),
        ("symbol", "sector"),
        "transient",
        ("query",),
    ),
    "fmp_etf_historical": (
        "fmp",
        "EtfHistorical",
        "EtfHistoricalFetcher",
        "FmpEtfHistorical",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date"),
        "upsert",
        ("query", "store", "export"),
    ),
    "tradier_equity_historical": (
        "tradier",
        "EquityHistorical",
        "TradierEquityHistoricalFetcher",
        "TradierEquityHistorical",
        "series",
        "date",
        ("symbol", "date"),
        (
            "symbol",
            "date",
            "interval",
            "dividend_adjustment_semantics",
            "split_adjustment_semantics",
        ),
        "upsert",
        ("query", "store", "export"),
    ),
    "tradier_etf_historical": (
        "tradier",
        "EtfHistorical",
        "TradierEtfHistoricalFetcher",
        "TradierEtfHistorical",
        "series",
        "date",
        ("symbol", "date"),
        (
            "symbol",
            "date",
            "interval",
            "dividend_adjustment_semantics",
            "split_adjustment_semantics",
        ),
        "upsert",
        ("query", "store", "export"),
    ),
    "tradier_equity_quote": (
        "tradier",
        "EquityQuote",
        "TradierEquityQuoteFetcher",
        "TradierEquityQuote",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "security_type"),
        "transient",
        ("query",),
    ),
    "tradier_equity_search": (
        "tradier",
        "EquitySearch",
        "TradierEquitySearchFetcher",
        "TradierEquitySearch",
        "snapshot",
        None,
        ("symbol", "exchange"),
        ("symbol", "exchange", "security_type"),
        "transient",
        ("query",),
    ),
    "tradier_options_chains": (
        "tradier",
        "OptionsChains",
        "TradierOptionsChainsFetcher",
        "TradierOptionChain",
        "snapshot",
        None,
        ("symbol",),
        ("symbol", "underlying", "expiration_date", "option_type", "strike"),
        "transient",
        ("query",),
    ),
    "economic_calendar": (
        "tradingeconomics",
        "EconomicCalendar",
        "TradingEconomicsEconomicCalendarFetcher",
        "TradingEconomicsEconomicCalendar",
        "event",
        "date",
        ("calendar_id",),
        ("date", "country", "category", "event", "importance", "calendar_id"),
        "transient",
        ("query",),
    ),
    "sp500_multiples": (
        "multpl",
        "SP500Multiples",
        "SP500MultiplesFetcher",
        "SP500Multiples",
        "series",
        "date",
        ("metric", "date"),
        ("metric", "date"),
        "upsert",
        ("query", "store", "export"),
    ),
    "alpha_vantage_equity_historical": (
        "alpha_vantage",
        "EquityHistorical",
        "AlphaVantageEquityHistoricalFetcher",
        "AlphaVantageEquityHistorical",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date"),
        "upsert",
        ("query", "store", "export"),
    ),
    "alpha_vantage_etf_historical": (
        "alpha_vantage",
        "EtfHistorical",
        "AlphaVantageEtfHistoricalFetcher",
        "AlphaVantageEtfHistorical",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date"),
        "upsert",
        ("query", "store", "export"),
    ),
    "alpha_vantage_historical_eps": (
        "alpha_vantage",
        "HistoricalEps",
        "AlphaVantageHistoricalEpsFetcher",
        "AlphaVantageHistoricalEps",
        "series",
        "date",
        ("symbol", "date"),
        ("symbol", "date"),
        "upsert",
        ("query", "store", "export"),
    ),
    "fred_treasury_constant_maturity": (
        "fred",
        "TreasuryConstantMaturity",
        "TreasuryConstantMaturityFetcher",
        "FredTreasuryConstantMaturity",
        "series",
        "date",
        ("date",),
        ("date",),
        "transient",
        ("query",),
    ),
    "fred_selected_treasury_bill": (
        "fred",
        "SelectedTreasuryBill",
        "SelectedTreasuryBillFetcher",
        "FredSelectedTreasuryBill",
        "series",
        "date",
        ("date",),
        ("date",),
        "transient",
        ("query",),
    ),
    "fred_selected_treasury_constant_maturity": (
        "fred",
        "SelectedTreasuryConstantMaturity",
        "SelectedTreasuryConstantMaturityFetcher",
        "FredSelectedTreasuryConstantMaturity",
        "series",
        "date",
        ("date",),
        ("date",),
        "transient",
        ("query",),
    ),
    "fred_ameribor": (
        "fred",
        "Ameribor",
        "AmeriborFetcher",
        "FredAmeribor",
        "series",
        "date",
        ("date", "symbol"),
        ("date", "maturity", "symbol"),
        "transient",
        ("query",),
    ),
    "fred_discount_window_primary_credit_rate": (
        "fred",
        "DiscountWindowPrimaryCreditRate",
        "DiscountWindowPrimaryCreditRateFetcher",
        "FredDiscountWindowPrimaryCreditRate",
        "series",
        "date",
        ("date",),
        ("date",),
        "transient",
        ("query",),
    ),
    "fred_european_central_bank_interest_rates": (
        "fred",
        "EuropeanCentralBankInterestRates",
        "EuropeanCentralBankInterestRatesFetcher",
        "FredEuropeanCentralBankInterestRate",
        "series",
        "date",
        ("date",),
        ("date",),
        "transient",
        ("query",),
    ),
    "fred_tips_yields": (
        "fred",
        "TipsYields",
        "TipsYieldsFetcher",
        "FredTipsYield",
        "series",
        "date",
        ("date", "symbol"),
        ("date", "symbol"),
        "transient",
        ("query",),
    ),
    "congress_bills": (
        "congress_gov",
        "CongressBills",
        "CongressBillsFetcher",
        "CongressBill",
        "snapshot",
        "update_date",
        ("congress", "bill_type", "bill_number"),
        ("congress", "bill_type", "update_date", "latest_action_date"),
        "transient",
        ("query",),
    ),
    "congress_bill_info": (
        "congress_gov",
        "CongressBillInfo",
        "CongressBillInfoFetcher",
        "CongressBillInfo",
        "snapshot",
        None,
        ("source_url",),
        (),
        "transient",
        ("query",),
    ),
    "congress_bill_text": (
        "congress_gov",
        "CongressBillText",
        "CongressBillTextFetcher",
        "CongressBillText",
        "document",
        "version_date",
        ("congress", "bill_type", "bill_number", "source_url"),
        ("congress", "bill_type", "bill_number", "version_date", "document_format"),
        "transient",
        ("query",),
    ),
    "congress_amendments": (
        "congress_gov",
        "CongressAmendments",
        "CongressAmendmentsFetcher",
        "CongressAmendment",
        "snapshot",
        "update_date",
        ("congress", "amendment_type", "number"),
        ("congress", "amendment_type", "update_date", "latest_action_date"),
        "transient",
        ("query",),
    ),
    "congress_amendment_info": (
        "congress_gov",
        "CongressAmendmentInfo",
        "CongressAmendmentInfoFetcher",
        "CongressAmendmentInfo",
        "snapshot",
        None,
        ("source_url",),
        (),
        "transient",
        ("query",),
    ),
    "congress_amendment_text": (
        "congress_gov",
        "CongressAmendmentText",
        "CongressAmendmentTextFetcher",
        "CongressAmendmentText",
        "document",
        "version_date",
        ("congress", "amendment_type", "amendment_number", "source_url"),
        ("congress", "amendment_type", "amendment_number", "version_date", "document_format"),
        "transient",
        ("query",),
    ),
    "congress_committee_info": (
        "congress_gov",
        "CongressCommitteeInfo",
        "CongressCommitteeInfoFetcher",
        "CongressCommitteeInfo",
        "snapshot",
        None,
        ("source_url",),
        (),
        "transient",
        ("query",),
    ),
    "congress_committee_documents": (
        "congress_gov",
        "CongressCommitteeDocuments",
        "CongressCommitteeDocumentsFetcher",
        "CongressCommitteeDocument",
        "event",
        "event_date",
        ("document_category", "committee_code", "chamber", "congress", "document_id"),
        ("document_category", "committee_code", "chamber", "congress", "event_date"),
        "transient",
        ("query",),
    ),
}

EXPECTED_DOMAINS.update(
    {
        "fmp_crypto_historical": (
            "fmp",
            "CryptoHistorical",
            "CryptoHistoricalFetcher",
            "CryptoHistorical",
            "series",
            "date",
            ("date",),
            ("date",),
            "transient",
            ("query",),
        ),
        "fmp_crypto_search": (
            "fmp",
            "CryptoSearch",
            "CryptoSearchFetcher",
            "CryptoSearch",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "name", "exchange"),
            "transient",
            ("query",),
        ),
        "fmp_currency_historical": (
            "fmp",
            "CurrencyHistorical",
            "CurrencyHistoricalFetcher",
            "CurrencyHistorical",
            "series",
            "date",
            ("date",),
            ("date",),
            "transient",
            ("query",),
        ),
        "fmp_currency_pairs": (
            "fmp",
            "CurrencyPairs",
            "CurrencyPairsFetcher",
            "CurrencyPair",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "name", "from_currency", "to_currency", "from_name", "to_name"),
            "transient",
            ("query",),
        ),
        "fmp_currency_snapshots": (
            "fmp",
            "CurrencySnapshots",
            "CurrencySnapshotsFetcher",
            "CurrencySnapshot",
            "snapshot",
            "last_rate_timestamp",
            ("base_currency", "counter_currency"),
            ("base_currency", "counter_currency"),
            "transient",
            ("query",),
        ),
        "fmp_equity_active": (
            "fmp",
            "EquityActive",
            "EquityActiveFetcher",
            "EquityActive",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "exchange"),
            "transient",
            ("query",),
        ),
        "fmp_equity_peers": (
            "fmp",
            "EquityPeers",
            "EquityPeersFetcher",
            "EquityPeers",
            "snapshot",
            None,
            ("symbol",),
            ("symbol",),
            "transient",
            ("query",),
        ),
        "fmp_equity_info": (
            "fmp",
            "EquityInfo",
            "EquityInfoFetcher",
            "EquityInfo",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "sector", "industry_category", "hq_country", "is_etf", "is_fund"),
            "transient",
            ("query",),
        ),
        "fmp_equity_gainers": (
            "fmp",
            "EquityGainers",
            "EquityGainersFetcher",
            "EquityGainers",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "exchange"),
            "transient",
            ("query",),
        ),
        "fmp_equity_losers": (
            "fmp",
            "EquityLosers",
            "EquityLosersFetcher",
            "EquityLosers",
            "snapshot",
            None,
            ("symbol",),
            ("symbol", "exchange"),
            "transient",
            ("query",),
        ),
        "fmp_equity_screener": (
            "fmp",
            "EquityScreener",
            "EquityScreenerFetcher",
            "EquityScreener",
            "snapshot",
            None,
            ("symbol",),
            (
                "symbol",
                "sector",
                "industry",
                "exchange",
                "exchange_name",
                "country",
                "is_etf",
                "is_fund",
                "actively_trading",
            ),
            "transient",
            ("query",),
        ),
        "fmp_market_snapshots": (
            "fmp",
            "MarketSnapshots",
            "MarketSnapshotsFetcher",
            "MarketSnapshot",
            "snapshot",
            "last_price_timestamp",
            ("symbol",),
            ("symbol", "exchange"),
            "transient",
            ("query",),
        ),
        "fmp_price_performance": (
            "fmp",
            "PricePerformance",
            "PricePerformanceFetcher",
            "PricePerformance",
            "snapshot",
            None,
            ("symbol",),
            ("symbol",),
            "transient",
            ("query",),
        ),
        "fmp_share_statistics": (
            "fmp",
            "ShareStatistics",
            "ShareStatisticsFetcher",
            "ShareStatistics",
            "snapshot",
            "date",
            ("symbol", "date"),
            ("symbol", "date"),
            "transient",
            ("query",),
        ),
        "fmp_index_constituents": (
            "fmp",
            "IndexConstituents",
            "IndexConstituentsFetcher",
            "FmpIndexConstituent",
            "event",
            "date",
            ("symbol", "date"),
            (
                "symbol",
                "name",
                "sector",
                "industry",
                "headquarter",
                "date_added",
                "cik",
                "founded",
                "removed_symbol",
                "reason",
                "date",
            ),
            "transient",
            ("query",),
        ),
        "fmp_index_historical": (
            "fmp",
            "IndexHistorical",
            "IndexHistoricalFetcher",
            "IndexHistorical",
            "series",
            "date",
            ("symbol", "date"),
            ("symbol", "date"),
            "transient",
            ("query",),
        ),
    }
)

EXPECTED_DOMAINS.update(
    {
        "sec_cik_map": (
            "sec",
            "CikMap",
            "CikMapFetcher",
            "SecCikMapRecord",
            "snapshot",
            None,
            ("ticker", "cik"),
            ("ticker", "cik"),
            "transient",
            ("query",),
        ),
        "sec_symbol_map": (
            "sec",
            "SymbolMap",
            "SymbolMapFetcher",
            "SecSymbolMapRecord",
            "snapshot",
            None,
            ("cik", "ticker"),
            ("cik", "ticker"),
            "transient",
            ("query",),
        ),
        "sec_equity_search": (
            "sec",
            "EquitySearch",
            "EquitySearchFetcher",
            "SecEquitySearchRecord",
            "snapshot",
            None,
            ("cik", "ticker", "exchange"),
            ("cik", "ticker", "exchange"),
            "transient",
            ("query",),
        ),
        "sec_sic_search": (
            "sec",
            "SicSearch",
            "SicSearchFetcher",
            "SecSicCode",
            "snapshot",
            None,
            ("sic_code",),
            ("sic_code", "office", "industry_title"),
            "transient",
            ("query",),
        ),
        "sec_balance_sheet": (
            "sec",
            "BalanceSheet",
            "BalanceSheetFetcher",
            "SecBalanceSheetStatement",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_balance_sheet_growth": (
            "sec",
            "BalanceSheetGrowth",
            "BalanceSheetGrowthFetcher",
            "SecBalanceSheetGrowthRow",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_cash_flow_statement": (
            "sec",
            "CashFlowStatement",
            "CashFlowStatementFetcher",
            "SecCashFlowStatementRow",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_cash_flow_statement_growth": (
            "sec",
            "CashFlowStatementGrowth",
            "CashFlowStatementGrowthFetcher",
            "SecCashFlowStatementGrowthRow",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_income_statement": (
            "sec",
            "IncomeStatement",
            "IncomeStatementFetcher",
            "SecIncomeStatementRow",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_income_statement_growth": (
            "sec",
            "IncomeStatementGrowth",
            "IncomeStatementGrowthFetcher",
            "SecIncomeStatementGrowthRow",
            "series",
            "period_ending",
            ("cik", "accession", "period_ending"),
            ("symbol", "cik", "fiscal_period", "fiscal_year", "form", "reported_currency"),
            "transient",
            ("query",),
        ),
        "sec_compare_company_facts": (
            "sec",
            "CompareCompanyFacts",
            "CompareCompanyFactsFetcher",
            "SecCompanyFactComparison",
            "series",
            "reported_date",
            ("cik", "accession", "tag", "unit", "period_ending"),
            ("cik", "symbol", "tag", "unit", "frame", "period_ending", "reported_date", "form"),
            "transient",
            ("query",),
        ),
        "sec_equity_ftd": (
            "sec",
            "EquityFTD",
            "EquityFtdFetcher",
            "SecFtdRecord",
            "series",
            "settlement_date",
            ("settlement_date", "cusip", "symbol"),
            ("settlement_date", "cusip", "symbol"),
            "transient",
            ("query",),
        ),
        "sec_insider_trading": (
            "sec",
            "InsiderTrading",
            "InsiderTradingFetcher",
            "SecInsiderTransaction",
            "event",
            "transaction_date",
            (
                "issuer_cik",
                "accession_number",
                "security_type",
                "security_title",
                "transaction_date",
                "transaction_code",
                "acquired_disposed",
                "shares",
            ),
            (
                "company_cik",
                "symbol",
                "filing_date",
                "transaction_date",
                "security_type",
                "transaction_type",
                "ownership_type",
            ),
            "transient",
            ("query",),
        ),
    }
)

EXPECTED_DOMAINS.update(
    {
        "euro_short_term_rate": (
            "fred",
            "EuroShortTermRate",
            "EuroShortTermRateFetcher",
            "FredEuroShortTermRate",
            "series",
            "date",
            ("date",),
            ("date",),
            "transient",
            ("query",),
        ),
        "commercial_paper": (
            "fred",
            "CommercialPaper",
            "CommercialPaperFetcher",
            "FredCommercialPaper",
            "series",
            "date",
            ("date", "symbol"),
            ("date", "symbol", "maturity", "asset_type"),
            "transient",
            ("query",),
        ),
        "spot_rate": (
            "fred",
            "SpotRate",
            "SpotRateFetcher",
            "FredSpotRate",
            "series",
            "date",
            ("date", "category", "maturity"),
            ("date", "category", "maturity"),
            "transient",
            ("query",),
        ),
        "hqm_corporate_bond": (
            "fred",
            "HighQualityMarketCorporateBond",
            "HighQualityMarketCorporateBondFetcher",
            "FredHighQualityMarketCorporateBond",
            "series",
            "date",
            ("date", "yield_curve", "maturity"),
            ("date", "yield_curve", "maturity"),
            "transient",
            ("query",),
        ),
        "bond_indices": (
            "fred",
            "BondIndices",
            "BondIndicesFetcher",
            "FredBondIndices",
            "series",
            "date",
            ("date", "symbol"),
            ("date", "symbol", "maturity", "title"),
            "transient",
            ("query",),
        ),
        "commodity_spot_prices": (
            "fred",
            "CommoditySpotPrices",
            "CommoditySpotPricesFetcher",
            "FredCommoditySpotPrices",
            "series",
            "date",
            ("date", "commodity"),
            ("date", "commodity", "symbol"),
            "transient",
            ("query",),
        ),
        "fred_yield_curve": (
            "fred",
            "YieldCurve",
            "YieldCurveFetcher",
            "FredYieldCurve",
            "series",
            "date",
            ("date", "yield_curve_type", "maturity"),
            ("date", "yield_curve_type", "maturity"),
            "transient",
            ("query",),
        ),
        "energy_short_term_energy_outlook": (
            "eia",
            "ShortTermEnergyOutlook",
            "ShortTermEnergyOutlookFetcher",
            "ShortTermEnergyOutlook",
            "series",
            "date",
            ("release_date", "table", "symbol", "order", "date"),
            ("release_date", "is_forecast", "table", "symbol", "date"),
            "transient",
            ("query",),
        ),
        "finviz_compare_groups": (
            "finviz",
            "CompareGroups",
            "FinvizCompareGroupsFetcher",
            "FinvizGroupComparison",
            "snapshot",
            None,
            ("group", "name"),
            ("group",),
            "transient",
            ("query",),
        ),
        "finviz_equity_info": (
            "finviz",
            "EquityInfo",
            "FinvizEquityInfoFetcher",
            "FinvizEquityInfo",
            "snapshot",
            None,
            ("symbol",),
            ("symbol",),
            "transient",
            ("query",),
        ),
        "finviz_key_metrics": (
            "finviz",
            "KeyMetrics",
            "FinvizKeyMetricsFetcher",
            "FinvizKeyMetrics",
            "snapshot",
            None,
            ("symbol",),
            ("symbol",),
            "transient",
            ("query",),
        ),
        "finviz_price_target": (
            "finviz",
            "PriceTarget",
            "FinvizPriceTargetFetcher",
            "FinvizPriceTarget",
            "event",
            "date",
            ("symbol", "date", "analyst", "action", "price_target_usd"),
            ("symbol", "date"),
            "transient",
            ("query",),
        ),
        "imf_port_info": (
            "imf",
            "PortInfo",
            "ImfPortInfoFetcher",
            "ImfPortInfo",
            "snapshot",
            None,
            ("port_code",),
            ("country_code", "continent"),
            "transient",
            ("query",),
        ),
        "imf_port_volume": (
            "imf",
            "PortVolume",
            "ImfPortVolumeFetcher",
            "ImfPortVolume",
            "series",
            "date",
            ("port_code", "date"),
            ("port_code", "country_code", "date"),
            "transient",
            ("query",),
        ),
        "imf_maritime_chokepoint_info": (
            "imf",
            "MaritimeChokePointInfo",
            "ImfChokePointInfoFetcher",
            "ImfMaritimeChokePointInfo",
            "snapshot",
            None,
            ("chokepoint_code",),
            (),
            "transient",
            ("query",),
        ),
        "imf_maritime_chokepoint_volume": (
            "imf",
            "MaritimeChokePointVolume",
            "ImfChokePointVolumeFetcher",
            "ImfMaritimeChokePointVolume",
            "series",
            "date",
            ("chokepoint", "date"),
            ("chokepoint", "date"),
            "transient",
            ("query",),
        ),
    }
)

EXPECTED_YFINANCE_DOMAINS = {
    "yfinance_dividend_history": (
        "yfinance",
        "HistoricalDividends",
        "YfinanceHistoricalDividendsFetcher",
        "YfinanceDividendObservation",
        "series",
        "ex_dividend_date",
        ("symbol", "ex_dividend_date"),
        ("symbol", "ex_dividend_date"),
        "transient",
        ("query",),
    ),
    "yfinance_key_executives": (
        "yfinance",
        "KeyExecutives",
        "YfinanceKeyExecutivesFetcher",
        "YfinanceKeyExecutiveObservation",
        "snapshot",
        "retrieved_at_utc",
        ("symbol", "ordinal", "retrieved_at_utc"),
        ("symbol",),
        "transient",
        ("query",),
    ),
    "yfinance_key_metrics": (
        "yfinance",
        "KeyMetrics",
        "YfinanceKeyMetricsFetcher",
        "YfinanceKeyMetricObservation",
        "series",
        "period_ending",
        ("symbol", "period_kind", "period_ending"),
        ("symbol", "period_kind", "period_ending"),
        "transient",
        ("query",),
    ),
    "yfinance_price_target_consensus": (
        "yfinance",
        "PriceTargetConsensus",
        "YfinancePriceTargetFetcher",
        "YfinancePriceTargetConsensus",
        "snapshot",
        "retrieved_at_utc",
        ("symbol", "retrieved_at_utc"),
        ("symbol",),
        "transient",
        ("query",),
    ),
}

EXPECTED_FINVIZ_DOMAINS = {
    "finviz_price_performance": (
        "finviz",
        "PricePerformance",
        "FinvizPricePerformanceFetcher",
        "FinvizPricePerformance",
        "snapshot",
        None,
        ("symbol",),
        ("symbol",),
        "transient",
        ("query",),
    ),
    "finviz_etf_price_performance": (
        "finviz",
        "EtfPricePerformance",
        "FinvizEtfPricePerformanceFetcher",
        "FinvizEtfPricePerformance",
        "snapshot",
        None,
        ("symbol",),
        ("symbol",),
        "transient",
        ("query",),
    ),
}
BATCH5_SNAPSHOT_SHA256 = "894e47460a689655acca8c215e3ccb331c76c815fc99f314af12269386bb2b2e"
BATCH5_DOMAIN_SOURCE_SHA256 = "0e31e843beb711f1e04b534a54645c173249f24b47030c8c37948326fba6d20d"
BATCH5_CANDIDATE_SHA256 = "775dd322e145d84f0a1a850d46fd5f0754814ac68445c3b02bab4a41239e047e"


def _batch5_identity_snapshot() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures/registry_batch5_snapshot.json"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == BATCH5_SNAPSHOT_SHA256
    snapshot = json.loads(payload)
    repository_root = Path(__file__).resolve().parents[1]
    for relative_path, expected_hash in snapshot["source_hashes_sha256"].items():
        source_path = repository_root / relative_path
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == expected_hash, relative_path
    assert snapshot["snapshot_id"] == "central-wiring-batch5-candidate"
    assert snapshot["snapshot_status"] == "MANUAL_EXPECTED_FIXTURE_NOT_ACCEPTANCE"
    assert snapshot["candidate_sha256"] == BATCH5_CANDIDATE_SHA256
    assert snapshot["batch5_count_adjudication"] == {
        "path": (
            "docs/evidence/C100/integration/candidates/"
            "central-wiring-batch5-count-adjudication.json"
        ),
        "sha256": "2a929fd8614af09e28aa2ae16fa781c7da49d7692dcd6a3b6c78f2118c0906de",
        "supersedes_label_only": "329 public_contract_model_exports",
        "counts": {
            "fixed_identities": 350,
            "fixed_bound": 317,
            "fixed_unbound": 33,
            "domain_specs": 337,
            "provider_model_legs": 350,
            "provider_descriptors": 34,
            "all_symbols": 330,
            "contract_model_classes_including_base": 326,
            "non_base_contract_model_classes": 325,
            "non_contract_exports": [
                "BlsCatalogPage",
                "FredOutputType",
                "FredSeniorLoanOfficerSurveyQuery",
                "FredTransformUnits",
            ],
        },
        "domain_capability_gaps": {
            "domain_ids": ["futures_fundamentals", "stock_adjust"],
            "classification": "LEGACY_DOMAIN_CAPABILITY_GAP",
            "not_queryable": True,
            "not_covered_or_complete": True,
            "additional_differences_fail": True,
        },
        "runtime_status": "FAIL_GUARDED_METADATA_SMOKE",
    }
    assert snapshot["batch5_source_freeze"] == {
        "path": (
            "docs/evidence/C100/integration/runs/"
            "central-wiring-20261012T193703Z-batch5-sloos-retake1/"
            "batch5-source-freeze.json"
        ),
        "sha256": "adcf3986456fad52e56b85b1764fbdfd5d716ebad886c0f66a107da07136d6c8",
        "status": "STATIC_SOURCE_FREEZE_NO_NEW_RUNTIME",
    }
    assert snapshot["counts"]["domain_count"] == 363
    assert snapshot["counts"]["query_semantic_domain_count"] == 343
    assert snapshot["counts"]["legacy_dwd_domain_count"] == 20
    assert snapshot["counts"]["provider_model_identity_count"] == 376
    assert snapshot["counts"]["deferred_source_domain_leg_count"] == 343
    assert snapshot["counts"]["legacy_source_domain_leg_count"] == 33
    assert snapshot["counts"]["descriptor_eligible_identity_count"] == 354
    assert snapshot["counts"]["model_key_only_identity_count"] == 22
    assert snapshot["counts"]["descriptor_eligible_legacy_identity_count"] == 11
    assert (
        snapshot["counts"]["descriptor_eligible_identity_count"]
        + snapshot["counts"]["model_key_only_identity_count"]
        == snapshot["counts"]["provider_model_identity_count"]
    )
    assert snapshot["candidate_counts"]["fixed_identities"] == 376
    assert snapshot["candidate_counts"]["domain_specs"] == 363
    assert snapshot["candidate_counts"]["fixed_bound"] == 343
    assert snapshot["candidate_counts"]["fixed_unbound"] == 33
    assert (
        snapshot["candidate_counts"]["fixed_bound"] + snapshot["candidate_counts"]["fixed_unbound"]
        == snapshot["candidate_counts"]["fixed_identities"]
    )
    assert snapshot["candidate_counts"]["provider_model_legs"] == 376
    assert snapshot["candidate_counts"]["contract_model_classes_including_base"] == 348
    assert snapshot["candidate_counts"]["non_base_contract_model_classes"] == 347
    assert snapshot["candidate_counts"]["all_symbols"] == 352
    assert snapshot["candidate_counts"]["non_contract_exports"] == [
        {
            "name": "BlsCatalogPage",
            "kind": "sequence_dataclass",
            "module": "opendata.data.models.period_series",
        },
        {
            "name": "FredOutputType",
            "kind": "literal",
            "values": [1, 2, 3, 4],
        },
        {
            "name": "FredTransformUnits",
            "kind": "literal",
            "values": ["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"],
        },
        {
            "name": "FredSeniorLoanOfficerSurveyQuery",
            "kind": "query_model",
            "module": "opendata.data.models.fred_sloos",
        },
    ]
    assert snapshot["candidate_counts"]["registered_sources_or_descriptors"] == 34
    assert snapshot["batch4_baseline"]["all_symbols"] == 328
    assert snapshot["batch4_baseline"]["contract_model_classes_including_base"] == 325
    assert snapshot["batch4_baseline"]["non_base_contract_model_classes"] == 324
    assert snapshot["batch4_baseline"]["non_contract_exports"] == [
        "BlsCatalogPage",
        "FredOutputType",
        "FredTransformUnits",
    ]
    assert snapshot["source_hashes_sha256"]["opendata/data/domains.yaml"] == (
        BATCH5_DOMAIN_SOURCE_SHA256
    )
    assert snapshot["source_hashes_sha256"]["opendata/data/models/__init__.py"] == (
        "e3285297f81a6a6351d8cfba8fa9e4cf99de9398a646e0408817905aab7c8eec"
    )
    assert len(snapshot["provider_model_identities"]) == 376
    assert snapshot["approved_delta"] == {
        "source": "fred",
        "domain": "senior_loan_officer_survey",
        "canonical": "SeniorLoanOfficerSurvey",
        "binding": "opendata.data.providers.fred.models.sloos:FredSeniorLoanOfficerSurveyFetcher",
        "contract": "FredSeniorLoanOfficerSurvey",
        "verified": False,
        "scenario_present": False,
        "domain_spec": {
            "display_name": "FRED Senior Loan Officer Survey",
            "rest_path": "fred/economy/survey/senior-loan-officer",
            "contract": "FredSeniorLoanOfficerSurvey",
            "temporal_kind": "series",
            "time_field": "date",
            "natural_key": ["date", "symbol", "category", "transform"],
            "filter_dims": ["date", "symbol", "category", "transform"],
            "storage_mode": "transient",
            "permissions": ["query"],
        },
        "contract_model_export": "opendata.data.models.fred_sloos.FredSeniorLoanOfficerSurvey",
        "query_model_export": "opendata.data.models.fred_sloos.FredSeniorLoanOfficerSurveyQuery",
        "source_status": (
            "PARTIAL; source-level offline acceptance is PASS, central service "
            "and live FRED are NOT_RUN."
        ),
        "scenario_policy": (
            "No matching C65-approved use case was found; scenario remains absent and unconfirmed."
        ),
        "verified_policy": (
            "Capability verified remains false; no source verification or "
            "cross-source parity is claimed."
        ),
    }
    assert next(
        row
        for row in snapshot["provider_model_identities"]
        if row["domain"] == "senior_loan_officer_survey"
    ) == {
        "binding": "opendata.data.providers.fred.models.sloos:FredSeniorLoanOfficerSurveyFetcher",
        "canonical_ids_declared": True,
        "contract": "FredSeniorLoanOfficerSurvey",
        "domain": "senior_loan_officer_survey",
        "model": "SeniorLoanOfficerSurvey",
        "scenario_present": True,
        "source": "fred",
        "verified": False,
    }
    # batch5 reviewed this identity with no scenario. The iteration-2 消费用途 backfill wrote a
    # descriptive scenario string onto the binding, which is a census move; the approved use case is
    # still absent, so the reviewed delta record stays byte-frozen and the conflict is recorded.
    conflict = snapshot["iter2_census_rebase"]["registry_census_rebase"]["approved_delta_conflict"]
    assert snapshot["approved_delta"]["scenario_present"] is False
    assert conflict["batch5_pin_scenario_present"] is False
    assert conflict["live_scenario_present"] is True
    assert conflict["c65_use_case_approved"] is False
    assert snapshot["approved_delta"]["verified"] is False
    assert len(EXPECTED_DOMAINS) == 92
    assert len(EXPECTED_YFINANCE_DOMAINS) == 4
    assert len(EXPECTED_FINVIZ_DOMAINS) == 2
    return snapshot


def _assert_exact_set(actual: set[object], expected: set[object], *, label: str) -> None:
    missing = expected - actual
    unexpected = actual - expected
    assert not missing and not unexpected, (
        f"{label} mismatch; missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}"
    )


EXPECTED_P0_DOMAINS = {
    "stock_daily",
    "stock_adjust",
    "stock_action",
    "financial_statement",
    "financial_indicator",
    "index_constituent",
    "futures_daily",
    "futures_fundamentals",
}
EXPECTED_P1_DOMAINS = {
    "energy_petroleum_status_report",
    "energy_short_term_energy_outlook",
}


def _capability_identity(capability: Capability) -> tuple[str, str, str, str, str]:
    return (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )


def _declared_domain_ids() -> set[str]:
    """Domain ids the declarative engine publishes.

    A declaration names a domain that already exists or adds nothing the platform can route; listing
    these here is what keeps a new engine model from silently reusing a legacy domain whose contract
    model describes different columns.
    """
    return {spec.domain for _source, spec in engine_declared_models()}


def test_production_domains_preserve_v1_legacy_entries_and_explicit_semantics() -> None:
    registry_path = Path(__file__).parents[1] / "opendata/data/domains.yaml"
    raw_registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    specs = load_domains()
    declared_domains = _declared_domain_ids()
    snapshot = _batch5_identity_snapshot()
    expected_domain_ids = set(snapshot["domain_ids"])

    assert raw_registry["version"] == 1
    _assert_exact_set(set(specs), expected_domain_ids, label="registered domain identities")
    assert len(specs) == snapshot["counts"]["domain_count"]
    assert set(snapshot["legacy_dwd_domain_ids"]) == LEGACY_DOMAIN_IDS
    assert (
        set(EXPECTED_DOMAINS) | set(EXPECTED_YFINANCE_DOMAINS) | set(EXPECTED_FINVIZ_DOMAINS)
    ) <= expected_domain_ids
    assert declared_domains <= expected_domain_ids
    assert not LEGACY_DOMAIN_IDS & declared_domains, "a declaration may not reuse a legacy domain"
    assert all(not specs[domain].semantics_declared for domain in LEGACY_DOMAIN_IDS)
    assert {domain for domain, spec in specs.items() if spec.priority == "P0"} == (
        EXPECTED_P0_DOMAINS
    )
    assert {domain for domain, spec in specs.items() if spec.priority == "P1"} == (
        EXPECTED_P1_DOMAINS
    )

    for domain, expected in EXPECTED_DOMAINS.items():
        (
            _source,
            _model_id,
            _fetcher_class,
            contract,
            temporal_kind,
            time_field,
            natural_key,
            filter_dims,
            storage_mode,
            permissions,
        ) = expected
        spec = require_domain_semantics(domain)
        assert spec.contract == contract
        assert contract_model(domain).__name__ == contract
        assert spec.temporal_kind == temporal_kind
        assert spec.time_field == time_field
        assert spec.natural_key == natural_key
        assert spec.filter_dims == filter_dims
        assert spec.storage_mode == storage_mode
        assert spec.permissions == permissions
        assert spec.priority == ("P1" if domain in EXPECTED_P1_DOMAINS else None)

    # These four YFinance contracts were reviewed separately after the original 92-domain
    # leaf-contract matrix was frozen. Keep their literal expected semantics visible here.
    for domain, expected in EXPECTED_YFINANCE_DOMAINS.items():
        (
            _source,
            _model_id,
            _fetcher_class,
            contract,
            temporal_kind,
            time_field,
            natural_key,
            filter_dims,
            storage_mode,
            permissions,
        ) = expected
        spec = require_domain_semantics(domain)
        assert spec.contract == contract
        assert contract_model(domain).__name__ == contract
        assert spec.temporal_kind == temporal_kind
        assert spec.time_field == time_field
        assert spec.natural_key == natural_key
        assert spec.filter_dims == filter_dims
        assert spec.storage_mode == storage_mode
        assert spec.permissions == permissions
        assert spec.priority is None

    # The two reviewed Finviz snapshots use only the public symbol dimension and have no event time.
    for domain, expected in EXPECTED_FINVIZ_DOMAINS.items():
        (
            _source,
            _model_id,
            _fetcher_class,
            contract,
            temporal_kind,
            time_field,
            natural_key,
            filter_dims,
            storage_mode,
            permissions,
        ) = expected
        spec = require_domain_semantics(domain)
        assert spec.contract == contract
        assert contract_model(domain).__name__ == contract
        assert spec.temporal_kind == temporal_kind
        assert spec.time_field == time_field
        assert spec.natural_key == natural_key
        assert spec.filter_dims == filter_dims
        assert spec.storage_mode == storage_mode
        assert spec.permissions == permissions
        assert spec.priority is None


def test_declared_domain_contracts_match_exact_registered_provider_models() -> None:
    registry = ProviderRegistry()
    register_providers(registry)
    snapshot = _batch5_identity_snapshot()
    expected_rows = snapshot["provider_model_identities"]
    deferred_legs = {
        (row["source"], row["domain"]) for row in snapshot["deferred_source_domain_legs"]
    }
    legacy_legs = {(row["source"], row["domain"]) for row in snapshot["legacy_source_domain_legs"]}
    all_snapshot_legs = {(row["source"], row["domain"]) for row in snapshot["source_domain_legs"]}
    assert len(deferred_legs) == snapshot["counts"]["deferred_source_domain_leg_count"]
    assert len(legacy_legs) == snapshot["counts"]["legacy_source_domain_leg_count"]
    assert not deferred_legs & legacy_legs
    assert len(all_snapshot_legs) == snapshot["counts"]["registered_source_domain_leg_count"]
    _assert_exact_set(
        deferred_legs | legacy_legs,
        all_snapshot_legs,
        label="pinned batch5 deferred and legacy source/domain partition",
    )
    _assert_exact_set(
        {(row["source"], row["domain"]) for row in expected_rows},
        all_snapshot_legs,
        label="pinned batch5 source/domain identity legs",
    )
    expected_registered_rows = {
        (row["source"], row["model"], row["domain"])
        for row in expected_rows
        if (row["source"], row["domain"]) in deferred_legs
    }
    registered_rows = [
        row for row in expected_rows if (row["source"], row["domain"]) in deferred_legs
    ]
    # A descriptor exists only for a binding that names canonical model ids; the legacy partition
    # has model_key-only bindings, so the descriptor census needs its own pin.
    descriptor_rows = [row for row in expected_rows if row["canonical_ids_declared"]]
    expected_descriptor_rows = {
        (row["source"], row["model"], row["domain"]) for row in descriptor_rows
    }
    descriptors = registry.list_model_descriptors()
    actual_identities = {
        (descriptor.source, descriptor.model, descriptor.domain) for descriptor in descriptors
    }
    actual_qsem_identities = {
        identity for identity in actual_identities if (identity[0], identity[2]) in deferred_legs
    }
    actual_legacy_identities = actual_identities - actual_qsem_identities
    assert len(expected_rows) == snapshot["counts"]["provider_model_identity_count"]
    assert len(expected_registered_rows) == snapshot["counts"]["deferred_source_domain_leg_count"]
    assert len(descriptor_rows) == snapshot["counts"]["descriptor_eligible_identity_count"]
    assert len(descriptors) == snapshot["counts"]["descriptor_eligible_identity_count"]
    _assert_exact_set(
        actual_identities,
        expected_descriptor_rows,
        label="registered provider/model/domain descriptor identities",
    )
    _assert_exact_set(
        actual_qsem_identities,
        expected_registered_rows,
        label="query-semantic registered model identities",
    )
    assert actual_legacy_identities, "no legacy binding names a canonical model: the pin is stale"
    assert {(source, domain) for source, _model, domain in actual_legacy_identities} <= legacy_legs
    assert (
        len(actual_legacy_identities)
        == snapshot["counts"]["descriptor_eligible_legacy_identity_count"]
    )
    assert {source for source, _model, _domain in actual_legacy_identities} == {"ths"}

    descriptors_by_pair = {(item.source, item.model): item for item in descriptors}

    # The original 92 hand-reviewed leaf contracts keep their exact fetcher/contract checks.
    for domain, expected in EXPECTED_DOMAINS.items():
        source, model_id, fetcher_class, contract, *_ = expected
        fetcher = registry.resolve_model(source, model_id)
        assert type(fetcher).__name__ == fetcher_class
        assert fetcher.capability.source == source
        assert fetcher.capability.domain == domain
        assert contract_model(domain).__name__ == contract

    # The four separately reviewed YFinance contracts are literal, independent assertions.
    for domain, expected in EXPECTED_YFINANCE_DOMAINS.items():
        source, model_id, fetcher_class, contract, *_ = expected
        fetcher = registry.resolve_model(source, model_id)
        assert type(fetcher).__name__ == fetcher_class
        assert fetcher.capability.source == source
        assert fetcher.capability.domain == domain
        assert contract_model(domain).__name__ == contract

    # Compare every current binding target and contract against the independent batch5 oracle.
    static_bindings = {
        (provider.source, model_id): binding
        for provider in list_providers()
        for binding in provider.fetcher_bindings
        for model_id in (binding.canonical_model_ids or (binding.model_key,))
    }
    assert set(static_bindings) == {(row["source"], row["model"]) for row in expected_rows}
    for row in expected_rows:
        binding = static_bindings[(row["source"], row["model"])]
        assert f"{binding.module}:{binding.class_name}" == row["binding"]
        assert (binding.scenario is not None) is row["scenario_present"]

    sloos_spec = load_domains()["senior_loan_officer_survey"]
    expected_sloos_spec = snapshot["approved_delta"]["domain_spec"]
    assert {
        "display_name": sloos_spec.display_name,
        "rest_path": sloos_spec.rest_path,
        "contract": sloos_spec.contract,
        "temporal_kind": sloos_spec.temporal_kind,
        "time_field": sloos_spec.time_field,
        "natural_key": list(sloos_spec.natural_key),
        "filter_dims": list(sloos_spec.filter_dims),
        "storage_mode": sloos_spec.storage_mode,
        "permissions": list(sloos_spec.permissions),
    } == expected_sloos_spec

    for row in registered_rows:
        source, model_id, domain = row["source"], row["model"], row["domain"]
        fetcher = registry.resolve_model(source, model_id)
        descriptor = next(
            item
            for item in descriptors
            if (item.source, item.model, item.domain) == (source, model_id, domain)
        )
        assert f"{type(fetcher).__module__}:{type(fetcher).__name__}" == row["binding"]
        assert fetcher.capability.source == source
        assert fetcher.capability.domain == domain
        assert descriptor.capability_identity == _capability_identity(fetcher.capability)
        assert descriptor.verified is False
        assert contract_model(domain).__name__ == row["contract"]

    for source, spec in engine_declared_models():
        descriptor = descriptors_by_pair.get((source, spec.model))
        domain = load_domains().get(spec.domain)
        fetcher = registry.resolve_model(source, spec.model)
        assert descriptor is not None, f"{source}::{spec.model} has no registered model descriptor"
        assert domain is not None, f"{source}::{spec.model} names unregistered domain {spec.domain}"
        assert descriptor.domain == spec.domain
        assert descriptor.capability_identity == _capability_identity(fetcher.capability)
        assert descriptor.verified is False
        # A declaration is only servable while its rows are the domain's contract class, and the
        # engine picks that class only for a domain whose semantics are declared.
        assert domain.semantics_declared, f"{spec.domain} declares no semantics"
        assert "query" in domain.permissions, f"{spec.domain} cannot be queried"
        assert set(contract_model(spec.domain).model_fields) == {
            column.name for column in spec.columns
        }


def test_exact_domain_identity_assertion_rejects_removed_and_added_domains() -> None:
    expected = set(_batch5_identity_snapshot()["domain_ids"])
    registered = set(load_domains())

    _assert_exact_set(registered, expected, label="registered domain identities")
    with pytest.raises(AssertionError, match="registered domain identities mismatch"):
        _assert_exact_set(
            registered - {"yfinance_dividend_history"},
            expected,
            label="registered domain identities",
        )
    with pytest.raises(AssertionError, match="registered domain identities mismatch"):
        _assert_exact_set(
            registered | {"unexpected_counterfactual_domain"},
            expected,
            label="registered domain identities",
        )


def test_public_exports_match_reviewed_batch5_counts_and_types() -> None:
    from collections.abc import Sequence
    from dataclasses import is_dataclass
    from typing import Literal, get_args, get_origin

    from opendata.data import models
    from opendata.data.models.base import ContractModel
    from opendata.data.protocol import QueryParams

    snapshot = _batch5_identity_snapshot()
    expected_contract_count = snapshot["candidate_counts"]["contract_model_classes_including_base"]
    expected_non_base_contract_count = snapshot["candidate_counts"][
        "non_base_contract_model_classes"
    ]
    expected_symbol_count = snapshot["candidate_counts"]["all_symbols"]
    exported_objects = [getattr(models, name) for name in models.__all__]
    actual_contract_count = sum(
        isinstance(export, type) and issubclass(export, ContractModel)
        for export in exported_objects
    )
    actual_non_base_contract_count = sum(
        isinstance(export, type)
        and export is not ContractModel
        and issubclass(export, ContractModel)
        for export in exported_objects
    )
    actual_non_contract_names = sorted(
        name
        for name in models.__all__
        if not (
            isinstance(getattr(models, name), type)
            and issubclass(getattr(models, name), ContractModel)
        )
    )

    rebase = snapshot["iter2_census_rebase"]
    assert snapshot["candidate_counts"]["all_symbols"] == rebase["live_counts"]["all_symbols"]
    assert (
        snapshot["candidate_counts"]["contract_model_classes_including_base"]
        == rebase["live_counts"]["contract_model_classes_including_base"]
    )
    assert (
        snapshot["candidate_counts"]["non_base_contract_model_classes"]
        == rebase["live_counts"]["non_base_contract_model_classes"]
    )
    assert rebase["batch5_adjudication_counts"] == {
        "all_symbols": 330,
        "contract_model_classes_including_base": 326,
        "non_base_contract_model_classes": 325,
    }
    assert {
        key: rebase["live_counts"][key] - snapshot["batch5_count_adjudication"]["counts"][key]
        for key in rebase["delta_vs_adjudication"]
    } == rebase["delta_vs_adjudication"]
    assert actual_non_contract_names == [
        "BlsCatalogPage",
        "FredOutputType",
        "FredSeniorLoanOfficerSurveyQuery",
        "FredTransformUnits",
    ]
    assert actual_contract_count == expected_contract_count
    assert actual_non_base_contract_count == expected_non_base_contract_count
    assert len(models.__all__) == expected_symbol_count
    assert len(set(models.__all__)) == expected_symbol_count
    assert models.ContractModel is ContractModel
    assert "ContractModel" in models.__all__
    assert issubclass(models.FredSeniorLoanOfficerSurvey, ContractModel)
    assert models.FredSeniorLoanOfficerSurvey.__module__ == "opendata.data.models.fred_sloos"
    assert is_dataclass(models.BlsCatalogPage)
    assert issubclass(models.BlsCatalogPage, Sequence)
    assert models.BlsCatalogPage.__module__ == "opendata.data.models.period_series"
    assert get_origin(models.FredOutputType) is Literal
    assert get_args(models.FredOutputType) == (1, 2, 3, 4)
    assert get_origin(models.FredTransformUnits) is Literal
    assert get_args(models.FredTransformUnits) == (
        "lin",
        "chg",
        "ch1",
        "pch",
        "pc1",
        "pca",
        "cch",
        "cca",
        "log",
    )
    assert issubclass(models.FredSeniorLoanOfficerSurveyQuery, QueryParams)
    assert not issubclass(models.FredSeniorLoanOfficerSurveyQuery, ContractModel)
    assert models.FredSeniorLoanOfficerSurveyQuery.__module__ == ("opendata.data.models.fred_sloos")


def test_domain_query_permission_does_not_grant_default_api_key_scope() -> None:
    default_scopes = normalize_scope_input(None)

    assert default_scopes == []
    for domain in EXPECTED_DOMAINS:
        assert "query" in load_domains()[domain].permissions
        assert not key_allows_domain(default_scopes, domain)


def test_fmp_market_bindings_match_exact_domain_contracts() -> None:
    """The FMP market batch is wired to its reviewed row contracts."""
    market_domains = {
        "fmp_crypto_historical",
        "fmp_crypto_search",
        "fmp_currency_historical",
        "fmp_currency_pairs",
        "fmp_currency_snapshots",
        "fmp_equity_active",
        "fmp_equity_peers",
        "fmp_equity_info",
        "fmp_equity_gainers",
        "fmp_equity_losers",
        "fmp_equity_screener",
        "fmp_market_snapshots",
        "fmp_price_performance",
        "fmp_share_statistics",
        "fmp_index_constituents",
        "fmp_index_historical",
    }
    registry = ProviderRegistry()
    register_providers(registry)
    descriptors = {
        (descriptor.source, descriptor.model): descriptor
        for descriptor in registry.list_model_descriptors()
    }

    for domain in market_domains:
        expected = EXPECTED_DOMAINS[domain]
        source, model_id, fetcher_class, contract, *_ = expected
        fetcher = registry.resolve_model(source, model_id)
        descriptor = descriptors[(source, model_id)]
        spec = require_domain_semantics(domain)
        assert type(fetcher).__name__ == fetcher_class
        assert descriptor.domain == domain
        assert fetcher.capability.source == source
        assert fetcher.capability.domain == domain
        assert descriptor.verified is False
        assert contract_model(domain).__name__ == contract
        assert spec.contract == contract
