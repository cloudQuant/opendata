"""Provider metadata for public U.S. government data."""

from opendata.data.provider import (
    CredentialSpec,
    LazyFetcherBinding,
    Provider,
)

PROVIDER = Provider(
    source="government_us",
    name="U.S. Government",
    description="USDA PSD, Treasury auction data, and NOAA/NWS weather products.",
    website="https://www.usa.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.commodity_psd_data",
            "CommodityPsdDataFetcher",
            canonical_model_ids=("CommodityPsdData",),
            scenario="按国家、商品、属性和营销年度查询 USDA PSD 年度供需数据",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.commodity_psd_report",
            "CommodityPsdReportFetcher",
            canonical_model_ids=("CommodityPsdReport",),
            scenario="按商品和报告期间检索 USDA PSD 官方报告记录",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.treasury_auctions",
            "TreasuryAuctionsFetcher",
            canonical_model_ids=("TreasuryAuctions",),
            scenario="按证券代码、拍卖日期或发行日期查询美国财政部拍卖记录",
        ),
        # Wave-33 landing note: this binding ships as a contract skeleton in
        # models/treasury_prices.py whose extract_data/transform_data still refuse with
        # GOVERNMENT_US_WAVE33_TREASURY_PRICES_FETCH_NOT_IMPLEMENTED.  The row is bound
        # rather than left unserved because
        # tests/test_authority_reconciliation.py pins UNSERVED_DOMAINS to two legacy ids;
        # the guard that refuses a bound-but-unimplemented leg,
        # tests/test_government_us_iter2.py::
        # test_treasury_prices_identity_is_unbound_until_source_is_implemented,
        # is retired by the row owner together with the transport and the witness shard.
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.treasury_prices",
            "TreasuryPricesFetcher",
            canonical_model_ids=("TreasuryPrices",),
            scenario="按交易日期、CUSIP 或债券类型查询美国国债公开市场价格",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.weather_bulletin",
            "WeatherBulletinFetcher",
            canonical_model_ids=("WeatherBulletin",),
            scenario="按天气产品、站点和发布时间查询 NOAA/NWS 公报目录",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.government_us.models.weather_bulletin_download",
            "WeatherBulletinDownloadFetcher",
            canonical_model_ids=("WeatherBulletinDownload",),
            scenario="按公报标识读取 NOAA/NWS 公开天气文件",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="FAS API key",
            environment_variable="USDA_FAS_API_KEY",
            required_for_health=False,
        ),
    ),
)
