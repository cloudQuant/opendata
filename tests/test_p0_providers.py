"""Tests for the P0 provider fetchers and their registration (A2.4).

The upstream flat functions are mocked with frames shaped exactly
like the ported upstream output (Chinese column names, upstream
units), so the mapping/unit-conversion assertions double as
documentation of the upstream shapes.
"""

from datetime import date

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import opendata.services.interface_loader as loader_module
import opendata_http
from opendata.data.models import (
    Bar,
    CorporateAction,
    FinancialIndicator,
    FinancialStatement,
    IndexConstituent,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.akshare.models import (
    AkshareFinancialIndicatorFetcher,
    AkshareFinancialStatementFetcher,
    AkshareIndexConstituentFetcher,
    AkshareStockActionFetcher,
    AkshareStockDailyFetcher,
)
from opendata.data.providers.akshare.registration import FETCHERS, register
from opendata.data.registry import ProviderRegistry
from opendata.models.interface import DataInterface
from opendata.services.interface_loader import InterfaceLoader

P0_DOMAINS = {
    "stock_daily",
    "stock_action",
    "financial_statement",
    "financial_indicator",
    "index_constituent",
}


def _em_klines_frame() -> pd.DataFrame:
    """Upstream-shaped eastmoney kline frame (volume in lots)."""
    return pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "股票代码": ["600519", "600519", "600519"],
            "开盘": [1685.0, 1690.0, None],
            "收盘": [1688.0, 1695.0, 1692.0],
            "最高": [1690.0, 1698.0, 1694.0],
            "最低": [1680.0, 1685.0, 1688.0],
            "成交量": [30000.0, 28000.0, 32000.0],
            "成交额": [5.06e9, 4.74e9, 5.41e9],
        }
    )


def _sina_dividend_frame() -> pd.DataFrame:
    """Upstream-shaped sina dividend page (per-10-share units)."""
    return pd.DataFrame(
        {
            "公告日期": ["2024-06-30", "2023-06-30"],
            "送股": [0.0, 0.0],
            "转增": [0.0, 0.0],
            "派息": [308.76, 259.11],
            "进度": ["实施", "实施"],
            "除权除息日": ["2024-07-01", "2023-07-01"],
            "股权登记日": ["2024-06-28", "2023-06-28"],
            "红股上市日": ["-", "-"],
        }
    )


def _sina_rights_frame() -> pd.DataFrame:
    """Upstream-shaped sina rights page."""
    return pd.DataFrame(
        {
            "公告日期": ["2020-03-10"],
            "配股方案": ["10配3"],
            "配股价格": [180.0],
            "基准股本": [1256197800],
            "除权日": ["2020-04-10"],
            "股权登记日": ["2020-04-09"],
            "缴款起始日": ["2020-04-13"],
            "缴款终止日": ["2020-04-17"],
            "配股上市日": ["2020-04-27"],
            "募集资金合计": [678.4e8],
        }
    )


def _sina_statement_frame() -> pd.DataFrame:
    """Upstream-shaped wide sina statement frame."""
    return pd.DataFrame(
        {
            "报告日": ["2024-06-30", "2024-03-31"],
            "货币资金": [5.8e10, 5.6e10],
            "应收账款": [None, 1.2e8],
            "数据源": ["新浪", "新浪"],
            "是否审计": ["是", "是"],
            "公告日期": ["2024-08-30", "2024-04-28"],
            "币种": ["人民币", "人民币"],
            "类型": ["合并报表", "合并报表"],
            "更新日期": ["2024-08-30", "2024-04-28"],
        }
    )


def _em_indicator_frame() -> pd.DataFrame:
    """Upstream-shaped eastmoney F10 indicator frame."""
    return pd.DataFrame(
        {
            "SECUCODE": ["600519.SH", "600519.SH"],
            "SECURITY_NAME_ABBR": ["贵州茅台", "贵州茅台"],
            "REPORT_DATE": ["2024-06-30", "2024-03-31"],
            "NOTICE_DATE": ["2024-08-30", "2024-04-28"],
            "EPSJB": [23.88, 26.02],
            "ROEJQ": [16.9, 20.1],
        }
    )


def _csindex_weight_frame() -> pd.DataFrame:
    """Upstream-shaped CSI close-weight frame."""
    return pd.DataFrame(
        {
            "日期": [date(2024, 7, 31), date(2024, 7, 31)],
            "指数代码": ["000300", "000300"],
            "指数名称": ["沪深300", "沪深300"],
            "成分券代码": ["600519", None],
            "成分券名称": ["贵州茅台", None],
            "交易所": ["上海证券交易所", None],
            "权重": [4.15, None],
        }
    )


class TestRegistration:
    def test_registers_five_p0_capabilities(self):
        registry = ProviderRegistry()
        registered = register(registry)

        assert {cap.domain for cap in registered} == P0_DOMAINS
        assert all(cap.source == "akshare" for cap in registered)
        assert all(cap.market == "cn" for cap in registered)

    def test_capabilities_declared_unverified(self):
        registry = ProviderRegistry()
        register(registry)

        assert all(not cap.verified for cap in registry.capabilities())
        assert all(cap.participates_in_auto() is False for cap in registry.capabilities())

    def test_registration_is_idempotent(self):
        registry = ProviderRegistry()
        register(registry)

        assert register(registry) == []

    def test_explicit_source_routes_unverified_fetcher(self):
        registry = ProviderRegistry()
        register(registry)

        fetcher = registry.resolve("equity", "stock_daily", source="akshare")
        assert isinstance(fetcher, AkshareStockDailyFetcher)

    def test_auto_routing_excludes_unverified(self):
        registry = ProviderRegistry()
        register(registry)

        with pytest.raises(LookupError, match="no verified capability"):
            registry.resolve("equity", "stock_daily")

    def test_fetchers_match_fetcher_count(self):
        assert len(FETCHERS) == 5


class TestStockDailyFetcher:
    def test_maps_klines_to_bars_with_share_volume(self, monkeypatch):
        monkeypatch.setattr(opendata_http, "stock_zh_a_hist", lambda **kwargs: _em_klines_frame())
        fetcher = AkshareStockDailyFetcher()

        result = fetcher.fetch(symbol="600519.SH")

        bars = list(result)
        assert len(bars) == 2  # the row with a missing price dropped
        assert all(isinstance(bar, Bar) for bar in bars)
        assert bars[0].symbol == "600519"
        assert bars[0].trade_date == date(2024, 1, 2)
        assert bars[0].volume == 3_000_000.0  # lots -> shares (x100)
        assert bars[0].amount == 5.06e9

    def test_query_rejects_unknown_fields(self):
        fetcher = AkshareStockDailyFetcher()

        with pytest.raises(Exception, match="bogus"):
            fetcher.fetch(symbol="600519", bogus=1)

    def test_extract_passes_plain_symbol_and_dates(self, monkeypatch):
        seen = {}

        def fake_hist(**kwargs):
            seen.update(kwargs)
            return _em_klines_frame()

        monkeypatch.setattr(opendata_http, "stock_zh_a_hist", fake_hist)
        fetcher = AkshareStockDailyFetcher()

        fetcher.fetch(symbol="600519.SH", start_date=date(2024, 1, 1), adjust="qfq")

        assert seen["symbol"] == "600519"
        assert seen["period"] == "daily"
        assert seen["start_date"] == "20240101"
        assert seen["adjust"] == "qfq"

    def test_timeout_context_is_forwarded(self, monkeypatch):
        seen = {}

        def fake_hist(**kwargs):
            seen.update(kwargs)
            return _em_klines_frame()

        monkeypatch.setattr(opendata_http, "stock_zh_a_hist", fake_hist)
        fetcher = AkshareStockDailyFetcher()

        fetcher.fetch(symbol="600519", ctx=FetchContext(timeout=12.5))

        assert seen["timeout"] == 12.5


class TestStockActionFetcher:
    def test_maps_dividends_per_share(self, monkeypatch):
        def fake_detail(symbol, indicator):
            return _sina_dividend_frame() if indicator == "分红" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600519"))

        assert len(actions) == 2
        assert all(isinstance(action, CorporateAction) for action in actions)
        assert actions[0].ex_date == date(2024, 7, 1)
        assert actions[0].cash_dividend == pytest.approx(30.876)  # 308.76 per 10 -> per share
        assert actions[0].stock_dividend == 0.0

    def test_maps_rights_plan_ratio(self, monkeypatch):
        def fake_detail(symbol, indicator):
            return _sina_rights_frame() if indicator == "配股" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600519"))

        assert len(actions) == 1
        assert actions[0].ex_date == date(2020, 4, 10)
        assert actions[0].rights_shares == pytest.approx(0.3)  # 10配3 -> 0.3 per share
        assert actions[0].rights_price == pytest.approx(18.0)  # 180 per 10 -> per share

    def test_all_zero_rows_dropped(self, monkeypatch):
        frame = _sina_dividend_frame()
        frame.loc[0, "派息"] = 0.0

        def fake_detail(symbol, indicator):
            return frame if indicator == "分红" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600519"))

        assert len(actions) == 1  # only the zero row dropped
        assert actions[0].cash_dividend == pytest.approx(25.911)


class TestFinancialStatementFetcher:
    def test_melts_wide_statement_to_long_rows(self, monkeypatch):
        monkeypatch.setattr(
            opendata_http, "stock_financial_report_sina", lambda **kwargs: _sina_statement_frame()
        )
        fetcher = AkshareFinancialStatementFetcher()

        statements = list(fetcher.fetch(symbol="600519", statement_type="资产负债表"))

        items = {statement.item for statement in statements}
        assert items == {"货币资金", "应收账款"}  # metadata columns excluded
        by_key = {(statement.report_period, statement.item): statement for statement in statements}
        assert len(statements) == 3  # 2 report periods x surviving items
        row = by_key[(date(2024, 6, 30), "货币资金")]
        assert isinstance(row, FinancialStatement)
        assert row.symbol == "600519"
        assert row.statement_type == "资产负债表"
        assert row.announce_date == date(2024, 8, 30)
        assert row.value == 5.8e10

    def test_extract_uses_sina_prefixed_symbol(self, monkeypatch):
        seen = {}

        def fake_report(**kwargs):
            seen.update(kwargs)
            return _sina_statement_frame()

        monkeypatch.setattr(opendata_http, "stock_financial_report_sina", fake_report)
        AkshareFinancialStatementFetcher().fetch(symbol="600519")

        assert seen["stock"] == "sh600519"


class TestFinancialIndicatorFetcher:
    def test_melts_em_dataset_with_passthrough_codes(self, monkeypatch):
        monkeypatch.setattr(
            opendata_http,
            "stock_financial_analysis_indicator_em",
            lambda **kwargs: _em_indicator_frame(),
        )
        fetcher = AkshareFinancialIndicatorFetcher()

        indicators = list(fetcher.fetch(symbol="600519.SH"))

        codes = {indicator.indicator for indicator in indicators}
        assert codes == {"EPSJB", "ROEJQ"}  # string metadata drops out via coercion
        row = indicators[0]
        assert isinstance(row, FinancialIndicator)
        assert row.symbol == "600519"
        assert row.report_period == date(2024, 6, 30)
        assert row.announce_date == date(2024, 8, 30)

    def test_extract_uses_em_suffixed_symbol(self, monkeypatch):
        seen = {}

        def fake_indicator(**kwargs):
            seen.update(kwargs)
            return _em_indicator_frame()

        monkeypatch.setattr(opendata_http, "stock_financial_analysis_indicator_em", fake_indicator)
        AkshareFinancialIndicatorFetcher().fetch(symbol="600519")

        assert seen["symbol"] == "600519.SH"

    def test_missing_notice_date_fails_closed(self, monkeypatch):
        frame = _em_indicator_frame().drop(columns=["NOTICE_DATE"])
        monkeypatch.setattr(
            opendata_http, "stock_financial_analysis_indicator_em", lambda **kwargs: frame
        )
        fetcher = AkshareFinancialIndicatorFetcher()

        with pytest.raises(ValueError, match="NOTICE_DATE"):
            fetcher.fetch(symbol="600519")


class TestIndexConstituentFetcher:
    def test_maps_weights_in_percent(self, monkeypatch):
        monkeypatch.setattr(
            opendata_http,
            "index_stock_cons_weight_csindex",
            lambda **kwargs: _csindex_weight_frame(),
        )
        fetcher = AkshareIndexConstituentFetcher()

        constituents = list(fetcher.fetch(symbol="000300"))

        assert len(constituents) == 1  # the row without a constituent code dropped
        row = constituents[0]
        assert isinstance(row, IndexConstituent)
        assert row.index_symbol == "000300"
        assert row.symbol == "600519"
        assert row.as_of == date(2024, 7, 31)
        assert row.weight == pytest.approx(4.15)  # percent, passthrough


class TestRegistryScan:
    @pytest_asyncio.fixture
    async def registry_loader(self, monkeypatch, test_engine):
        """Loader whose registry scan writes through the test engine."""
        loader = InterfaceLoader()
        registry = ProviderRegistry()
        register(registry)
        monkeypatch.setattr("opendata.data.registry.get_registry", lambda: registry)

        test_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(loader_module, "async_session_maker", test_maker)
        yield loader, registry

    async def test_load_from_registry_creates_domain_named_rows(self, registry_loader):
        loader, _ = registry_loader

        count = await loader.load_from_registry()

        assert count == 5

    async def test_rows_are_named_by_domain_with_contract_models(self, registry_loader, test_db):
        loader, _ = registry_loader

        await loader.load_from_registry()

        async with async_sessionmaker(
            test_db.bind, class_=AsyncSession, expire_on_commit=False
        )() as session:
            result = await session.execute(
                select(DataInterface).where(DataInterface.name.in_(P0_DOMAINS))
            )
            rows = result.scalars().all()
        assert len(rows) == 5
        by_name = {row.name: row for row in rows}
        assert by_name["stock_daily"].display_name == "A股日线行情"
        assert by_name["stock_daily"].return_type == "Bar"
        assert by_name["stock_daily"].category_id is not None

    async def test_load_from_registry_is_idempotent(self, registry_loader):
        loader, _ = registry_loader

        await loader.load_from_registry()
        assert await loader.load_from_registry() == 0


class TestCapabilitiesEndpoint:
    async def test_capabilities_api_lists_p0_capabilities(
        self, test_client, test_user_token, monkeypatch
    ):
        from opendata.data.providers import register_providers

        register_providers()  # process singleton; idempotent across tests

        response = await test_client.get(
            "/api/v1/data/capabilities",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        payload = response.json()
        by_domain = {entry["domain"]: entry for entry in payload}
        assert set(by_domain) >= P0_DOMAINS
        # A3.4: the fuyao (ths) provider is now registered and authoritative for
        # the two domains it serves, so the listing reports it there; the other
        # P0 domains still come from akshare.
        assert by_domain["stock_daily"]["source"] == "ths"
        assert by_domain["stock_daily"]["asset_class"] == "equity"
        assert by_domain["stock_daily"]["verified"] is True
        assert by_domain["financial_statement"]["source"] == "akshare"
        assert by_domain["financial_statement"]["verified"] is False

    async def test_capabilities_requires_authentication(self, test_client):
        response = await test_client.get("/api/v1/data/capabilities")

        assert response.status_code == 401

    async def test_sources_api_exposes_authority_and_registered(self, test_client, test_user_token):
        from opendata.data.providers import register_providers

        register_providers()

        response = await test_client.get(
            "/api/v1/data/sources",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["authority"]["stock_daily"] == ["ths", "akshare"]
        assert set(payload["registered"]["akshare"]) >= P0_DOMAINS

    async def test_sources_requires_authentication(self, test_client):
        response = await test_client.get("/api/v1/data/sources")

        assert response.status_code == 401
