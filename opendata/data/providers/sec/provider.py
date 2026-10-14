"""Provider metadata and source-owned SEC iteration-2 bindings."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="sec",
    name="SEC",
    description=(
        "Official EDGAR adapter for company identities, US-GAAP XBRL facts, fails-to-deliver "
        "records, insider ownership filings, and filing/document disclosure data."
    ),
    website="https://www.sec.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_directory",
            "CikMapFetcher",
            canonical_model_ids=("CikMap",),
            scenario="SEC company ticker/CIK directory and bounded local page",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_directory",
            "EquitySearchFetcher",
            canonical_model_ids=("EquitySearch",),
            scenario="SEC exchange ticker directory substring and exchange search",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_directory",
            "SicSearchFetcher",
            canonical_model_ids=("SicSearch",),
            scenario="SEC official SIC table search",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_directory",
            "SymbolMapFetcher",
            canonical_model_ids=("SymbolMap",),
            scenario="SEC company ticker/CIK directory reverse map",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "BalanceSheetFetcher",
            canonical_model_ids=("BalanceSheet",),
            scenario="SEC companyfacts instant US-GAAP statement facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "BalanceSheetGrowthFetcher",
            canonical_model_ids=("BalanceSheetGrowth",),
            scenario="SEC companyfacts comparable prior-year instant facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "CashFlowStatementFetcher",
            canonical_model_ids=("CashFlowStatement",),
            scenario="SEC companyfacts duration cash-flow statement facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "CashFlowStatementGrowthFetcher",
            canonical_model_ids=("CashFlowStatementGrowth",),
            scenario="SEC companyfacts comparable prior-year cash-flow facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "CompareCompanyFactsFetcher",
            canonical_model_ids=("CompareCompanyFacts",),
            scenario="SEC XBRL frames comparison for two resolved issuer tickers",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "IncomeStatementFetcher",
            canonical_model_ids=("IncomeStatement",),
            scenario="SEC companyfacts duration income statement facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_financial",
            "IncomeStatementGrowthFetcher",
            canonical_model_ids=("IncomeStatementGrowth",),
            scenario="SEC companyfacts comparable prior-year income facts",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_ftd",
            "EquityFtdFetcher",
            canonical_model_ids=("EquityFTD",),
            scenario="SEC settlement-date FTD archives from the official download index",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_insider",
            "InsiderTradingFetcher",
            canonical_model_ids=("InsiderTrading",),
            scenario="SEC issuer Form 4 search, filing index, and ownership XML",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "CompanyFilingsFetcher",
            canonical_model_ids=("CompanyFilings",),
            scenario="SEC submissions recent and bounded historical filing rows",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "FilingsFetcher",
            canonical_model_ids=("Filings",),
            scenario="SEC filing list with its distinct public model identity",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "SecFilingFetcher",
            canonical_model_ids=("SecFiling",),
            scenario="SEC filing index JSON and typed cover-page metadata",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "SecHtmFileFetcher",
            canonical_model_ids=("SecHtmFile",),
            scenario="SEC filing HTML document body",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "RssLitigationFetcher",
            canonical_model_ids=("RssLitigation",),
            scenario="SEC official litigation releases RSS feed",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "SchemaFilesFetcher",
            canonical_model_ids=("SchemaFiles",),
            scenario="SEC XBRL taxonomy directory and schema records",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "LatestFinancialReportsFetcher",
            canonical_model_ids=("LatestFinancialReports",),
            scenario="SEC latest financial reports and filing document metadata",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "ManagementDiscussionAnalysisFetcher",
            canonical_model_ids=("ManagementDiscussionAnalysis",),
            scenario="SEC filing MD&A HTML content extraction",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "Form13FHRFetcher",
            canonical_model_ids=("Form13FHR",),
            scenario="SEC Form 13F-HR filing header and information table",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_documents",
            "NportDisclosureFetcher",
            canonical_model_ids=("NportDisclosure",),
            scenario="SEC Form N-PORT structured disclosure XML",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.sec.models.iter2_institutions",
            "InstitutionsSearchFetcher",
            canonical_model_ids=("InstitutionsSearch",),
            scenario=(
                "SEC Form 13F institution directory（sec_institutions_search 域，"
                "检索最新 13F 批次中的申报机构）"
            ),
        ),
    ),
    credentials=(),
)

__all__ = ["PROVIDER"]
