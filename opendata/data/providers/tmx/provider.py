"""Provider descriptor and lazy bindings for evidence-backed TMX products."""

from opendata.data.provider import LazyFetcherBinding, Provider

_BINDINGS = (
    ("EquitySearchFetcher", "EquitySearch", "company keyword search"),
    ("EquityHistoricalFetcher", "EquityHistorical", "dated daily equity history"),
    ("EquityInfoFetcher", "EquityInfo", "company profile fields"),
    ("EquityQuoteFetcher", "EquityQuote", "current quote fields"),
    ("CompanyFilingsFetcher", "CompanyFilings", "public company filing records"),
    ("HistoricalDividendsFetcher", "HistoricalDividends", "paged dividend history"),
    ("IndexConstituentsFetcher", "IndexConstituents", "index constituent directory"),
    ("PriceTargetConsensusFetcher", "PriceTargetConsensus", "analyst consensus fields"),
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
            "opendata.data.providers.tmx.models.fetchers",
            class_name,
            canonical_model_ids=(model,),
            scenario=scenario,
        )
        for class_name, model, scenario in _BINDINGS
    ),
    credentials=(),
)
