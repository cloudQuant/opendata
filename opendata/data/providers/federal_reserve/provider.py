"""Provider metadata and lazy bindings for federal_reserve."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="federal_reserve",
    name="Federal Reserve",
    description="Public Federal Reserve Board and regional Federal Reserve data adapters.",
    website="https://www.federalreserve.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.money_measures",
            "FederalReserveMoneyMeasuresFetcher",
            canonical_model_ids=("MoneyMeasures",),
            scenario=(
                "美联储货币供应量月表（federal_reserve_money_measures 域，"
                "声明式引擎读取 H.6 的七个 _N.M 序列列）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.treasury_rates",
            "FederalReserveTreasuryRatesFetcher",
            canonical_model_ids=("TreasuryRates",),
            scenario=(
                "美国国债收益率日表（federal_reserve_treasury_rates 域，"
                "声明式引擎读取 H.15 宽表的期限列并保留发布百分比）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.svensson_yield_curve",
            "FederalReserveSvenssonYieldCurveFetcher",
            canonical_model_ids=("SvenssonYieldCurve",),
            scenario=(
                "美联储 Svensson 拟合因子收益率曲线（federal_reserve_svensson_yield_curve 域，"
                "声明式引擎读取 FEDS 名义收益率曲线宽表 99 列）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.federal_funds_rate",
            "FederalReserveFederalFundsRateFetcher",
            canonical_model_ids=("FederalFundsRate",),
            scenario=(
                "美国有效联邦基金利率日表（federal_reserve_federal_funds_rate 域，"
                "声明式引擎读取 NY Fed EFFR JSON 并把百分比折成小数）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.sofr",
            "FederalReserveSOFRFetcher",
            canonical_model_ids=("SOFR",),
            scenario=(
                "美联储 SOFR 担保隔夜融资利率日表（federal_reserve_sofr 域，"
                "声明式引擎读取 NY Fed sofr/search.json 的五个百分比列）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.overnight_bank_funding_rate",
            "FederalReserveOvernightBankFundingRateFetcher",
            canonical_model_ids=("OvernightBankFundingRate",),
            scenario=(
                "美联储 OBFR 隔夜银行融资利率日表（federal_reserve_overnight_bank_funding_rate 域，"
                "声明式引擎按日期边界读取 NY Fed obfr/search.json）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.yield_curve",
            "FederalReserveYieldCurveFetcher",
            canonical_model_ids=("YieldCurve",),
            scenario=(
                "Federal Reserve Treasury Yield Curve（federal_reserve_yield_curve 域，"
                "从官方 H.15 CSV 读取名义恒定到期收益率长表）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.primary_dealer_fails",
            "FederalReservePrimaryDealerFailsFetcher",
            canonical_model_ids=("PrimaryDealerFails",),
            scenario=(
                "Federal Reserve Primary Dealer Fails（federal_reserve_primary_dealer_fails 域，"
                "按资产类别精确筛选交易商融资交割失败）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.primary_dealer_positioning",
            "FederalReservePrimaryDealerPositioningFetcher",
            canonical_model_ids=("PrimaryDealerPositioning",),
            scenario=(
                "Federal Reserve Primary Dealer Positioning（"
                "federal_reserve_primary_dealer_positioning 域，"
                "读取手工声明的 Table III 净持仓键组）"
            ),
        ),
        # C101 splice (done): these three models are bound to the iteration-2 modules
        # models/central_bank_holdings.py, models/inflation_expectations.py and
        # models/total_factor_productivity.py, which publish the reviewed FederalReserve*
        # contracts the governed query service serializes and name the ``federal_reserve_*``
        # domains both the provider descriptor test and
        # docs/evidence/C100/federal-reserve/integration.json read. The domains.yaml entries
        # that made the registration land replaced the bare ``central_bank_holdings`` /
        # ``inflation_expectations`` / ``total_factor_productivity`` ids: rest_path is unique
        # per domain (opendata/data/domains.py::_REST_PATH_RE + the duplicate-route guard), so
        # the prefixed entries could not coexist with the bare ones. The legacy clean_economy
        # classes stay importable for their own tests but are no longer descriptor-bound.
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.central_bank_holdings",
            "FederalReserveCentralBankHoldingsFetcher",
            canonical_model_ids=("CentralBankHoldings",),
            scenario=(
                "Federal Reserve SOMA Holdings（federal_reserve_central_bank_holdings 域，"
                "读取 SOMA 周月明细、汇总与 WAM）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.inflation_expectations",
            "FederalReserveInflationExpectationsFetcher",
            canonical_model_ids=("InflationExpectations",),
            scenario=(
                "Federal Reserve Inflation Expectations（"
                "federal_reserve_inflation_expectations 域，"
                "归一费城 Fed SPF 通胀工作簿）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.total_factor_productivity",
            "FederalReserveTotalFactorProductivityFetcher",
            canonical_model_ids=("TotalFactorProductivity",),
            scenario=(
                "Federal Reserve Total Factor Productivity（"
                "federal_reserve_total_factor_productivity 域，"
                "读取旧金山 Fed 季报年报表与公布均值）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.fomc_documents",
            "FederalReserveFomcDocumentsFetcher",
            canonical_model_ids=("FomcDocuments",),
            scenario=(
                "Federal Reserve FOMC Documents（federal_reserve_fomc_documents 域，"
                "列出官方 FOMC 材料且不下载附件）"
            ),
        ),
    ),
    credentials=(),
)
