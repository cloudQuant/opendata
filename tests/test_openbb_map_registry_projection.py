"""Registry-derived OpenBB map projection tests."""

from __future__ import annotations

import socket
import sys
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from opendata.data.openbb_map import OpenBBMapError, load_openbb_map
from opendata.data.provider import LazyFetcherBinding
from opendata.data.providers import register_providers
from opendata.data.providers.catalog import get_provider
from opendata.data.registry import ProviderRegistry

if TYPE_CHECKING:
    from pathlib import Path

CANONICAL_SCENARIOS = {
    ("fred", "FredSearch"): "FRED序列发现与研究参数选择",
    ("fred", "FredSeries"): "保留修订区间及请求变换上下文的宏观研究",
    ("fred", "SOFR"): "保留SOFR原始数值、修订区间与请求变换上下文的融资成本研究",
    ("fred", "SONIA"): "保留SONIA七测度、原始数值与修订区间的英镑融资成本研究",
    ("bls", "BlsSearch"): "BLS调查目录发现与原生维度查询",
    ("bls", "BlsSeries"): "保留原生期间及脚注的劳工统计研究",
    ("fmp", "EquityHistorical"): "保留FMP原生价格调整说明的日线研究",
    ("fmp", "EquityQuote"): "当前报价查询，不提供历史快照承诺",
    ("ecb", "CurrencyReferenceRates"): "保留ECB源维度与属性的欧元日参考汇率研究",
    ("ecb", "YieldCurve"): "保留ECB七维与源属性的日度收益率曲线研究",
    ("ecb", "BalanceOfPayments"): "保留ECB十七维与原月季度期间的国际收支研究",
    ("cboe", "AvailableIndices"): "指数目录发现：列出 cboe 发布的全部指数及其计算时段与延迟",
    ("cboe", "IndexConstituents"): "成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
    ("cboe", "IndexSearch"): "指数检索：在 cboe 美国指数目录中按代码或名称与描述筛选指数",
    (
        "cboe",
        "EquitySearch",
    ): "股票目录检索：按代码或公司名筛选 All Access 官方证券目录",
    (
        "federal_reserve",
        "MoneyMeasures",
    ): (
        "美联储货币供应量月表（federal_reserve_money_measures 域，"
        "声明式引擎读取 H.6 的七个 _N.M 序列列）"
    ),
    (
        "federal_reserve",
        "TreasuryRates",
    ): (
        "美国国债收益率日表（federal_reserve_treasury_rates 域，"
        "声明式引擎读取 H.15 宽表的期限列并保留发布百分比）"
    ),
    (
        "federal_reserve",
        "SvenssonYieldCurve",
    ): (
        "美联储 Svensson 拟合因子收益率曲线（federal_reserve_svensson_yield_curve 域，"
        "声明式引擎读取 FEDS 名义收益率曲线宽表 99 列）"
    ),
    (
        "intrinio",
        "CurrencyPairs",
    ): "外汇对目录：列出 intrinio 发布的全部可交易货币对并按币种代码子串筛选",
    (
        "intrinio",
        "EquitySearch",
    ): "公司检索：在 intrinio 美股公司库中检索并返回改名后的 symbol 与 intrinio_id",
    (
        "intrinio",
        "HistoricalDividends",
    ): "分红拆股：按证券代码取回该证券的除息日分红与拆股调整序列",
    (
        "intrinio",
        "CalendarIpo",
    ): "IPO日历：按状态与发行规模取回新股发行日历并展平嵌套公司证券字段",
    (
        "federal_reserve",
        "FederalFundsRate",
    ): (
        "美国有效联邦基金利率日表（federal_reserve_federal_funds_rate 域，"
        "声明式引擎读取 NY Fed EFFR JSON 并把百分比折成小数）"
    ),
    (
        "federal_reserve",
        "SOFR",
    ): (
        "美联储 SOFR 担保隔夜融资利率日表（federal_reserve_sofr 域，"
        "声明式引擎读取 NY Fed sofr/search.json 的五个百分比列）"
    ),
    (
        "intrinio",
        "InsiderTrading",
    ): "内部人交易：按公司代码取回内幕交易申报，并把每笔申报的交易逐条展开成行",
    ("benzinga", "PriceTarget"): "目标价评级：按代码与评级动作取回 benzinga 分析师目标价日历单页",
    (
        "econdb",
        "AvailableIndicators",
    ): "指标目录名册：取出 econdb 静态 main_tickers.csv，剔除 OECD 旧块并按最新更新日期倒序",
    (
        "tiingo",
        "CryptoHistorical",
    ): "加密货币历史价格：一次请求取回多个 ticker 的嵌套 priceData 并按日期升序展开成行",
    (
        "federal_reserve",
        "OvernightBankFundingRate",
    ): (
        "美联储 OBFR 隔夜银行融资利率日表（federal_reserve_overnight_bank_funding_rate 域，"
        "声明式引擎按日期边界读取 NY Fed obfr/search.json）"
    ),
    ("fmp", "AvailableIndices"): "全球股票指数目录查询",
    ("fmp", "CalendarDividend"): "按明确日期窗口读取股息日历",
    ("fmp", "CalendarEarnings"): "按明确日期窗口读取业绩日历",
    ("fmp", "EtfCountries"): "读取ETF国家权重配置",
    ("fmp", "EtfEquityExposure"): "查询ETF对单只股票的暴露",
    ("fmp", "EtfHoldings"): "读取ETF成分持仓",
    ("fmp", "EtfInfo"): "读取ETF基本信息",
    ("fmp", "EtfPricePerformance"): "读取ETF当前价格表现",
    ("fmp", "EtfSearch"): "检索FMP ETF目录",
    ("fmp", "EtfSectors"): "读取ETF行业权重配置",
    ("fmp", "EtfHistorical"): "读取ETF日历史价格",
    ("tradier", "EquityHistorical"): "带显式日期窗口的美股历史日线、周线或月线",
    ("tradier", "EtfHistorical"): "先核验ETF类型再读取显式日期窗口历史价格",
    ("tradier", "EquityQuote"): "当前美股报价快照，不接受历史日期窗口",
    ("tradier", "EquitySearch"): "按部分代码或公司名称检索证券目录",
    ("tradier", "OptionsChains"): "按标的和到期日读取有界期权链，可选Greeks",
    (
        "tradingeconomics",
        "EconomicCalendar",
    ): "经济事件日历：按国家、分组、重要度、日期窗或 Trading Economics calendar ID 查询",
    ("multpl", "SP500Multiples"): "S&P 500 多估值历史：按公开表格中的估值指标和日期返回估算标记",
    (
        "alpha_vantage",
        "EquityHistorical",
    ): "用于按股票代码分析区间内的历史行情，支持日、周、月及原生分钟级 OHLCV 序列。",
    (
        "alpha_vantage",
        "EtfHistorical",
    ): "用于按 ETF 代码分析基金历史价格与成交量，支持日、周、月及原生分钟级 OHLCV 序列。",
    (
        "alpha_vantage",
        "HistoricalEps",
    ): "用于追踪公司年度或季度每股收益及其相对市场预期的历史变化。",
    (
        "fred",
        "TreasuryConstantMaturity",
    ): "按固定期限查询美国国债恒定期限收益率",
    ("fred", "SelectedTreasuryBill"): "按固定期限查询美国国库券收益率",
    (
        "fred",
        "SelectedTreasuryConstantMaturity",
    ): "按固定期限查询美国国债恒定期限收益率",
    ("fred", "Ameribor"): "查询有官方FRED序列映射的AMERIBOR指标",
    (
        "fred",
        "DiscountWindowPrimaryCreditRate",
    ): "查询美联储一级信贷贴现窗利率",
    ("fred", "EuropeanCentralBankInterestRates"): "查询欧洲央行政策利率",
    ("fred", "TipsYields"): "查询美国通胀保值国债恒定期限收益率",
    ("congress_gov", "CongressBills"): "按国会届次和法案类型查询官方分页法案列表",
    ("congress_gov", "CongressBillInfo"): "查询单项法案详情及明确选择的官方子资源",
    ("congress_gov", "CongressBillText"): "按API公布的文本版本下载法案原始文档",
    ("congress_gov", "CongressAmendments"): "按届次和修正案类型查询官方分页修正案列表",
    (
        "congress_gov",
        "CongressAmendmentInfo",
    ): "查询单项修正案详情及明确选择的官方子资源",
    ("congress_gov", "CongressAmendmentText"): "按API公布的文本版本下载修正案原始文档",
    ("congress_gov", "CongressCommitteeInfo"): "查询官方委员会身份、历史、分委会及活动计数",
    (
        "congress_gov",
        "CongressCommitteeDocuments",
    ): "查询官方委员会法案、报告、提名、通信或会议文档元数据",
}


@pytest.fixture
def registered_registry(monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    registry = ProviderRegistry()
    register_providers(registry)

    import opendata.data.registry as registry_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    return registry


def test_canonical_projection_matches_registry_binding_and_domain(
    registered_registry: ProviderRegistry,
) -> None:
    from opendata.data.domains import require_domain

    entries = load_openbb_map()
    descriptors = {
        (descriptor.source, descriptor.model): descriptor
        for descriptor in registered_registry.list_model_descriptors()
    }
    for identity, scenario in CANONICAL_SCENARIOS.items():
        source, model = identity
        descriptor = descriptors[identity]
        binding = get_provider(source).fetcher_dict[model]
        fetcher = registered_registry.resolve_model(source, model)
        domain = require_domain(descriptor.domain)
        mapped_entries = [
            entry for entry in entries if entry.model == model and entry.scenario == scenario
        ]

        assert len(mapped_entries) == 1
        assert model in binding.canonical_model_ids
        assert binding.scenario == scenario
        assert type(fetcher).__module__ == binding.module
        assert type(fetcher).__name__ == binding.class_name
        assert descriptor.domain == fetcher.capability.domain
        assert descriptor.capability_identity == (
            fetcher.capability.asset_class,
            fetcher.capability.domain,
            fetcher.capability.period,
            fetcher.capability.market,
            fetcher.capability.source,
        )
        assert len(mapped_entries[0].ours) == 1
        row = mapped_entries[0].ours[0]
        assert (row.provider, row.domain, row.contract, row.status) == (
            source,
            descriptor.domain,
            domain.contract,
            "verified" if descriptor.verified else "registered",
        )


def test_legacy_pending_aliases_and_old_scenarios_are_not_promoted(
    registered_registry: ProviderRegistry,
) -> None:
    entries = load_openbb_map()

    pending_rows = [
        row
        for entry in entries
        for row in entry.ours
        if row.provider == "ths" and row.domain == "financial_indicator"
    ]
    assert [(row.status, row.batch) for row in pending_rows] == [("pending", None)]
    assert any(
        entry.model == "TBD" and entry.scenario == "指数日线（业绩基准与指数增强回测 / dwd 指数域）"
        for entry in entries
    )
    assert not any(entry.model in {"FredCpi", "BlsObservation", "FmpQuote"} for entry in entries)

    # Census re-base 2026-10-14: descriptor population tracks the live registry, so the intent
    # ("a legacy/pending alias is not promoted") is checked as one-and-only-one resolution.
    def unresolved_or_promoted(live_descriptors: list) -> list:
        counts = Counter((descriptor.source, descriptor.model) for descriptor in live_descriptors)
        return sorted(identity for identity in CANONICAL_SCENARIOS if counts.get(identity, 0) != 1)

    live_descriptors = list(registered_registry.list_model_descriptors())
    print(
        f"\nlive descriptor population: {len(live_descriptors)} "
        f"vs canonical scenarios: {len(CANONICAL_SCENARIOS)}"
    )
    assert unresolved_or_promoted(live_descriptors) == []
    # live counterfact: promoting a legacy alias onto a canonical identity must go red.
    promoted_alias = next(
        descriptor
        for descriptor in live_descriptors
        if (descriptor.source, descriptor.model) == next(iter(CANONICAL_SCENARIOS))
    )
    assert unresolved_or_promoted([*live_descriptors, replace(promoted_alias)]) == [
        (promoted_alias.source, promoted_alias.model)
    ]


def test_registry_change_is_reflected_without_a_cached_projection(
    registered_registry: ProviderRegistry,
) -> None:
    initial = load_openbb_map()
    assert any(entry.model == "FredSearch" for entry in initial)

    search_fetcher = registered_registry.resolve_model("fred", "FredSearch")
    registered_registry.register_model("fred", "FredSearchAlias", search_fetcher)

    with pytest.raises(OpenBBMapError, match="no local binding"):
        load_openbb_map()


def test_legacy_status_is_derived_from_current_registry(
    registered_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def akshare_stock_daily_status() -> str:
        entry = next(
            entry
            for entry in load_openbb_map()
            if entry.model == "EquityHistorical"
            and entry.scenario == "行情日线（回测数据准备 / dwd 日线域）"
        )
        return next(
            row.status
            for row in entry.ours
            if row.provider == "akshare" and row.domain == "stock_daily"
        )

    capability = next(
        capability
        for capability in registered_registry.capabilities()
        if capability.source == "akshare" and capability.domain == "stock_daily"
    )
    assert akshare_stock_daily_status() == "registered"

    with monkeypatch.context() as patch:
        patch.setattr(capability, "verified", True)
        assert akshare_stock_daily_status() == "verified"

    assert capability.verified is False
    assert akshare_stock_daily_status() == "registered"


def test_shared_fetcher_capability_restores_across_two_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = get_provider("akshare").fetchers[0]
    first_registry = ProviderRegistry()
    second_registry = ProviderRegistry()
    first_registry.register(fetcher)
    second_registry.register(fetcher)
    first_capability = first_registry.capabilities()[0]
    second_capability = second_registry.capabilities()[0]
    original_verified = fetcher.capability.verified

    assert first_capability is second_capability is fetcher.capability

    with monkeypatch.context() as patch:
        patch.setattr(fetcher.capability, "verified", not original_verified)
        assert first_capability.verified is not original_verified
        assert second_capability.verified is not original_verified

    assert first_capability.verified is original_verified
    assert second_capability.verified is original_verified


def test_missing_canonical_scenario_fails_closed(
    registered_registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.providers.catalog as catalog

    provider = get_provider("fred")
    bindings = tuple(
        replace(binding, scenario=None) if "FredSearch" in binding.canonical_model_ids else binding
        for binding in provider.fetcher_bindings
    )
    provider_without_scenario = replace(provider, fetcher_bindings=bindings)
    providers_by_source = dict(catalog._PROVIDERS_BY_SOURCE)
    providers_by_source["fred"] = provider_without_scenario
    monkeypatch.setattr(catalog, "_PROVIDERS_BY_SOURCE", providers_by_source)

    with pytest.raises(OpenBBMapError, match="has no scenario"):
        load_openbb_map()


def test_empty_registry_is_not_silently_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    import opendata.data.registry as registry_module

    registry = ProviderRegistry()
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)

    with pytest.raises(OpenBBMapError, match="enabled legacy capability"):
        load_openbb_map()
    assert registry.capabilities() == []


def test_custom_v1_path_does_not_consult_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.registry as registry_module

    path = tmp_path / "custom.yaml"
    path.write_text(
        "version: 1\n"
        "entries:\n"
        "  - openbb: {model: TBD}\n"
        "    scenario: planned\n"
        "    ours:\n"
        "      - {provider: future, domain: not_registered, contract: Unknown, "
        "status: pending}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        registry_module,
        "get_registry",
        lambda: pytest.fail("custom map loading must not consult the provider registry"),
    )

    entries = load_openbb_map(str(path))
    assert entries[0].ours[0].status == "pending"
    assert entries[0].ours[0].domain == "not_registered"


def test_cold_catalog_metadata_does_not_load_fetchers_or_open_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.providers.catalog as catalog

    def deny(*args: object, **kwargs: object) -> None:
        raise AssertionError("metadata access attempted network I/O")

    def deny_fetcher_load(_binding: LazyFetcherBinding, _source: str) -> None:
        raise AssertionError("metadata access attempted to load a fetcher")

    modules_before = {
        name
        for name in sys.modules
        if name.startswith("opendata.data.providers.") and ".models." in name
    }
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(LazyFetcherBinding, "load", deny_fetcher_load)

    for (source, model), scenario in CANONICAL_SCENARIOS.items():
        assert catalog.get_provider(source).fetcher_dict[model].scenario == scenario

    modules_after = {
        name
        for name in sys.modules
        if name.startswith("opendata.data.providers.") and ".models." in name
    }
    assert modules_after == modules_before


def test_legacy_custom_schema_remains_strict(tmp_path: Path) -> None:
    path = tmp_path / "custom.yaml"
    path.write_text("version: 2\nentries: []\n", encoding="utf-8")

    with pytest.raises(OpenBBMapError, match="version 1"):
        load_openbb_map(str(path))


def test_binding_scenario_validation_rejects_blank_values() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.search",
            "FredSearchFetcher",
            scenario=" ",
        )
