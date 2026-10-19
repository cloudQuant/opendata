"""Provider descriptor and lazy bindings for evidence-backed TMX products."""

from opendata.data.provider import LazyFetcherBinding, Provider

_FETCHERS = "opendata.data.providers.tmx.models.fetchers"
_FETCHERS_W33_GRAPHQL = "opendata.data.providers.tmx.models.fetchers_w33_graphql"
_FETCHERS_W33_ETF_LOADERS = "opendata.data.providers.tmx.models.fetchers_w33_etf_loaders"
_FETCHERS_W34_ENVELOPE = "opendata.data.providers.tmx.models.fetchers_w34_envelope"

#: (module, class name, canonical model id, scenario) for every bound TMX model.
_BINDINGS = (
    (_FETCHERS, "EquitySearchFetcher", "EquitySearch", "company keyword search"),
    (_FETCHERS, "EquityHistoricalFetcher", "EquityHistorical", "dated daily equity history"),
    (_FETCHERS, "EquityInfoFetcher", "EquityInfo", "company profile fields"),
    (_FETCHERS, "EquityQuoteFetcher", "EquityQuote", "current quote fields"),
    (_FETCHERS, "CompanyFilingsFetcher", "CompanyFilings", "public company filing records"),
    (_FETCHERS, "HistoricalDividendsFetcher", "HistoricalDividends", "paged dividend history"),
    (_FETCHERS, "IndexConstituentsFetcher", "IndexConstituents", "index constituent directory"),
    (_FETCHERS, "PriceTargetConsensusFetcher", "PriceTargetConsensus", "analyst consensus fields"),
    (
        _FETCHERS_W33_GRAPHQL,
        "CalendarEarningsFetcher",
        "CalendarEarnings",
        "earnings calendar rows",
    ),
    (_FETCHERS_W33_GRAPHQL, "CompanyNewsFetcher", "CompanyNews", "company news articles"),
    (_FETCHERS_W33_GRAPHQL, "EquityGainersFetcher", "EquityGainers", "ranked gainers screen"),
    (
        _FETCHERS_W33_GRAPHQL,
        "InsiderTradingFetcher",
        "InsiderTrading",
        "insider transaction records",
    ),
    (_FETCHERS_W33_GRAPHQL, "EtfHistoricalFetcher", "EtfHistorical", "dated daily ETF history"),
    (_FETCHERS_W33_ETF_LOADERS, "EtfInfoFetcher", "EtfInfo", "ETF profile fields"),
    (_FETCHERS_W33_ETF_LOADERS, "EtfSearchFetcher", "EtfSearch", "ETF directory search"),
    (_FETCHERS_W33_ETF_LOADERS, "EtfSectorsFetcher", "EtfSectors", "ETF sector weights"),
    (_FETCHERS_W33_ETF_LOADERS, "EtfCountriesFetcher", "EtfCountries", "ETF country weights"),
    (_FETCHERS_W33_ETF_LOADERS, "EtfHoldingsFetcher", "EtfHoldings", "ETF holdings rows"),
    (_FETCHERS_W33_ETF_LOADERS, "BondPricesFetcher", "BondPrices", "designated bond price listing"),
    (
        _FETCHERS_W33_ETF_LOADERS,
        "TreasuryPricesFetcher",
        "TreasuryPrices",
        "treasury bond price listing",
    ),
    (
        _FETCHERS_W33_ETF_LOADERS,
        "AvailableIndicesFetcher",
        "AvailableIndices",
        "index directory listing",
    ),
    (_FETCHERS_W33_ETF_LOADERS, "IndexSectorsFetcher", "IndexSectors", "index sector weights"),
    (
        _FETCHERS_W34_ENVELOPE,
        "OptionsChainsFetcher",
        "OptionsChains",
        "listed option chain rows for one underlying",
    ),
    (
        _FETCHERS_W34_ENVELOPE,
        "IndexSnapshotsFetcher",
        "IndexSnapshots",
        "regional index snapshot panel",
    ),
)

PROVIDER = Provider(
    source="tmx",
    name="TMX Money",
    description=(
        "Clean-room TMX Money adapter for public company search, dated prices, "
        "quotes, profile fields, filings, dividends, index constituents, and "
        "analyst consensus."
    ),
    website="https://money.tmx.com/",
    fetcher_bindings=tuple(
        LazyFetcherBinding(
            module,
            class_name,
            canonical_model_ids=(model,),
            scenario=scenario,
        )
        for module, class_name, model, scenario in _BINDINGS
    ),
    credentials=(),
)
