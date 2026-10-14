"""Provider metadata and lazy bindings for tiingo."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="tiingo",
    name="Tiingo",
    description=(
        "Tiingo adapters for documented equity/ETF, FX, news, distribution-yield and crypto "
        "responses. Requests are token-gated, validated before I/O, and bounded by per-model "
        "symbol/row limits."
    ),
    website="https://www.tiingo.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoEquityHistoricalFetcher",
            canonical_model_ids=("EquityHistorical",),
            scenario="Tiingo daily or IEX historical OHLCV for a bounded list of symbols",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoEtfHistoricalFetcher",
            canonical_model_ids=("EtfHistorical",),
            scenario="Tiingo historical OHLCV for ETFs",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoCompanyNewsFetcher",
            canonical_model_ids=("CompanyNews",),
            scenario="Tiingo news filtered by ticker symbols",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoWorldNewsFetcher",
            canonical_model_ids=("WorldNews",),
            scenario="Tiingo global financial-news feed",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.crypto_historical",
            "TiingoCryptoHistoricalFetcher",
            canonical_model_ids=("CryptoHistorical",),
            scenario=(
                "加密货币历史价格：一次请求取回多个 ticker 的嵌套 priceData 并按日期升序展开成行"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoCurrencyHistoricalFetcher",
            canonical_model_ids=("CurrencyHistorical",),
            scenario="Tiingo FX historical OHLC for a bounded list of currency pairs",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tiingo.models.iter2",
            "TiingoTrailingDividendYieldFetcher",
            canonical_model_ids=("TrailingDividendYield",),
            scenario="Tiingo trailing one-year distribution yield observations",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="token",
            environment_variable="TIINGO_TOKEN",
        ),
    ),
)
