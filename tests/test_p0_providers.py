"""Tests for the P0 provider fetchers and their registration (A2.4).

The upstream flat functions are mocked with frames shaped exactly
like the ported upstream output (Chinese column names, upstream
units), so the mapping/unit-conversion assertions double as
documentation of the upstream shapes. The sina corporate-action
frames are copied cell by cell from a live reading
(``docs/evidence/C26/fixture-live-cells.txt``): a hand-written frame
that disagrees with the live dtype is what let the ÷10 配股 bug pass
as correct.
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

#: B1.2 registrations: ported P1 daily-bar domains, verified=False.
B1_DOMAINS = {
    "futures_daily",
    "index_daily",
    "fund_etf_daily",
    "option_daily",
    "bond_daily",
}


@pytest.fixture
def log_lines():
    """Collect ``(level, message)`` from loguru for the duration of a test.

    ``FetchResult`` is a bare sequence/dataframe, so a provider has no
    structured channel to report a dropped row: a log line is the only thing
    a caller can see, and the drop tests have to read it back.

    Yields:
        Records appended by the sink.
    """
    from loguru import logger

    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield records
    finally:
        logger.remove(handler_id)


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
    """Live cells of sina's 600519 分红 page (per-10-share amounts).

    Copied from the 2026-09-25 reading in
    ``docs/evidence/C26/fixture-live-cells.txt``, types included: the page
    hands back ``datetime.date`` cells and ``NaT`` for the dates it does not
    have, not strings.
    """
    return pd.DataFrame(
        {
            "公告日期": [date(2024, 6, 12), date(2023, 6, 26)],
            "送股": [0.0, 0.0],
            "转增": [0.0, 0.0],
            "派息": [308.76, 259.11],
            "进度": ["实施", "实施"],
            "除权除息日": [date(2024, 6, 19), date(2023, 6, 30)],
            "股权登记日": [date(2024, 6, 18), date(2023, 6, 29)],
            "红股上市日": [pd.NaT, pd.NaT],
        }
    )


def _sina_601318_2018_frame() -> pd.DataFrame:
    """Live cells of sina's 601318 分红 page around the 2018-06-07 ex-date.

    Three rows, from ``docs/evidence/C26/fixture-live-cells.txt``: sina lists
    a plan and its implementation separately, and the 预案 row carries no
    ex-date at all while fuyao publishes 1.2 for 2018-06-07 - the dated row
    alone returns 1.0 (C26).
    """
    return pd.DataFrame(
        {
            "公告日期": [date(2018, 8, 30), date(2018, 5, 31), date(2018, 4, 27)],
            "送股": [0.0, 0.0, 0.0],
            "转增": [0.0, 0.0, 0.0],
            "派息": [6.2, 10.0, 2.0],
            "进度": ["实施", "实施", "预案"],
            "除权除息日": [date(2018, 9, 6), date(2018, 6, 7), pd.NaT],
            "股权登记日": [date(2018, 9, 5), date(2018, 6, 6), pd.NaT],
            "红股上市日": [pd.NaT, pd.NaT, pd.NaT],
        }
    )


def _sina_rights_frame() -> pd.DataFrame:
    """Live cells of sina's 600030 配股 page (2022 allotment).

    The types matter: the ported page coerces ``配股方案`` with
    ``pd.to_numeric``, so the live cell is the per-ten-shares quantity
    ``1.5`` rather than the ``"10配3"`` spelling an earlier fixture hand-wrote
    (that mismatch is why the ÷10 bug read as correct, C26).
    """
    return pd.DataFrame(
        {
            "公告日期": [date(2022, 1, 14)],
            "配股方案": [1.5],
            "配股价格": [14.43],
            "基准股本": [10648400000],
            "除权日": [date(2022, 1, 27)],
            "股权登记日": [date(2022, 1, 18)],
            "缴款起始日": [pd.NaT],
            "缴款终止日": [date(2022, 1, 25)],
            "配股上市日": [date(2022, 2, 15)],
            "募集资金合计": [float("nan")],
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
    def test_registers_the_p0_capabilities_plus_b1_domains(self):
        registry = ProviderRegistry()
        registered = register(registry)

        # P0 closure + the B1.2 P1 registrations (verified=False)
        assert {cap.domain for cap in registered} == P0_DOMAINS | B1_DOMAINS
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
        assert len(FETCHERS) == 5 + len(B1_DOMAINS)  # 5 P0 + B1.2


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
        assert actions[0].ex_date == date(2024, 6, 19)
        assert actions[0].cash_dividend == pytest.approx(30.876)  # 308.76 per 10 -> per share
        assert actions[0].stock_dividend == 0.0

    def test_maps_rights_ratio_per_share(self, monkeypatch):
        """``配股方案`` 是每 10 股的量：live 单元格是数字 ``1.5``，读成每股 0.15。"""

        def fake_detail(symbol, indicator):
            return _sina_rights_frame() if indicator == "配股" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600030"))

        assert len(actions) == 1
        assert actions[0].ex_date == date(2022, 1, 27)
        assert actions[0].rights_shares == pytest.approx(0.15)

    def test_maps_rights_price_as_published(self, monkeypatch):
        """``配股价格`` 已经是每股价，不能再 ÷10.

        东方财富同一事件写「10配1.5 / 配股价 14.43」，与 sina 的 14.43 逐字相等。
        C26 实测：6 个有除权日的配股事件里 ``rights_price`` 被 ÷10 的 0 条
        （``docs/evidence/C26/rights-after-fix.txt`` C 节），其中东方财富同报的
        6 条与适配层现值逐字一致（同档 E 节 3 条 + ``rights-after-fix-retry.txt``
        E 节 5 条，两次跑各有一个符号因网络未判读，取并集 6 条，不符 0 条）。
        """

        def fake_detail(symbol, indicator):
            return _sina_rights_frame() if indicator == "配股" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600030"))

        assert actions[0].rights_price == pytest.approx(14.43)

    def test_spelled_rights_plan_text_also_parses(self, monkeypatch):
        """上游哪天不再做 ``pd.to_numeric``，``"10配3"`` 的原写法也要读出 0.3。"""
        frame = _sina_rights_frame().assign(配股方案=["10配3"], 配股价格=[8.2])

        def fake_detail(symbol, indicator):
            return frame if indicator == "配股" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600030"))

        assert actions[0].rights_shares == pytest.approx(0.3)
        assert actions[0].rights_price == pytest.approx(8.2)

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

    def test_undated_plan_drop_warns_with_the_amount(self, monkeypatch, log_lines):
        """预案行没有除息日，必须留下可归因的痕迹（本轮的可见化判据）。

        A silent drop here is what made 601318 2018-06-07 read 1.0 while the
        primary leg publishes 1.2 for the same day, so the warning has to name
        the count, the announcement date and the amount that went missing.
        """

        def fake_detail(symbol, indicator):
            return _sina_601318_2018_frame() if indicator == "分红" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="601318"))

        assert [action.ex_date for action in actions] == [date(2018, 9, 6), date(2018, 6, 7)]
        assert actions[1].cash_dividend == pytest.approx(1.0)
        warnings = [message for level, message in log_lines if level == "WARNING"]
        assert len(warnings) == 1
        assert "1 undated dividend plan" in warnings[0]
        assert "2018-04-27:0.2" in warnings[0]
        assert "under-report" in warnings[0]

    def test_contract_drops_are_counted_not_warned(self, monkeypatch, log_lines):
        """窗口与全零是契约要求的丢弃：计数进 DEBUG，不许淹没 WARNING。"""
        frame = pd.concat(
            [
                _sina_dividend_frame(),
                _sina_dividend_frame().iloc[[0]].assign(除权除息日=[date(2024, 3, 20)], 派息=[0.0]),
            ],
            ignore_index=True,
        )

        def fake_detail(symbol, indicator):
            return frame if indicator == "分红" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(
            fetcher.fetch(
                symbol="600519",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
            )
        )

        assert [action.ex_date for action in actions] == [date(2024, 6, 19)]
        assert [level for level, _ in log_lines if level == "WARNING"] == []
        debug = " | ".join(message for level, message in log_lines if level == "DEBUG")
        assert "1 row(s) outside the requested window" in debug
        assert "1 all-zero row(s)" in debug

    def test_undated_rights_row_warns_with_ratio_and_price(self, monkeypatch, log_lines):
        """配股页的无日期行走同一条判据：比例和价格都不能悄悄丢。

        The dividend branch is the one that produced the measured 601318 gap, but
        the rights branch is a separate ``if`` in ``transform_data``; an
        untested branch is what let the 配股 unit error live behind a green
        fixture, so this one is pinned on its own (C26).
        """

        def fake_detail(symbol, indicator):
            return (
                _sina_rights_frame().assign(除权日=[pd.NaT])
                if indicator == "配股"
                else pd.DataFrame()
            )

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="600030"))

        assert actions == []
        warnings = [message for level, message in log_lines if level == "WARNING"]
        assert len(warnings) == 1
        assert "0 undated dividend plan(s) []" in warnings[0]
        assert "1 undated rights plan(s)" in warnings[0]
        assert "2022-01-14:0.15@14.43" in warnings[0]

    def test_rights_page_contract_drops_are_counted_too(self, monkeypatch, log_lines):
        """配股页的窗口裁剪与全零行同样只进 DEBUG 计数（与分红页分开的一条分支）。"""
        frame = pd.concat(
            [
                _sina_rights_frame(),
                _sina_rights_frame().assign(除权日=[date(2015, 1, 20)]),
                _sina_rights_frame().assign(配股方案=[0.0], 配股价格=[0.0]),
            ],
            ignore_index=True,
        )

        def fake_detail(symbol, indicator):
            return frame if indicator == "配股" else pd.DataFrame()

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(
            fetcher.fetch(
                symbol="600030",
                start_date=date(2022, 1, 1),
                end_date=date(2022, 12, 31),
            )
        )

        assert [action.ex_date for action in actions] == [date(2022, 1, 27)]
        assert actions[0].rights_shares == pytest.approx(0.15)
        assert [level for level, _ in log_lines if level == "WARNING"] == []
        debug = " | ".join(message for level, message in log_lines if level == "DEBUG")
        assert "1 row(s) outside the requested window" in debug
        assert "1 all-zero row(s)" in debug

    def test_undated_rows_without_economics_stay_silent(self, monkeypatch, log_lines):
        """分级判据的另一半：没有金额的无日期行**不**报警，否则预案告警会被淹没。

        A 节 counts 601318's page as 无除息日 2 条 of which 有金额 1 条, so the
        ``进度=不分配`` row is the quiet kind and it really ships. The rights-page
        half is the same guard in the other branch; no such live row was observed,
        so that row is ablated from the dated fixture rather than quoted (C26).
        """

        def fake_detail(symbol, indicator):
            if indicator == "分红":
                return _sina_dividend_frame().assign(
                    公告日期=[date(2024, 6, 12), date(2009, 4, 9)],
                    派息=[308.76, 0.0],
                    进度=["实施", "不分配"],
                    除权除息日=[date(2024, 6, 19), pd.NaT],
                )
            return _sina_rights_frame().assign(除权日=[pd.NaT], 配股方案=[0.0], 配股价格=[0.0])

        monkeypatch.setattr(opendata_http, "stock_history_dividend_detail", fake_detail)
        fetcher = AkshareStockActionFetcher()

        actions = list(fetcher.fetch(symbol="601318"))

        assert [action.ex_date for action in actions] == [date(2024, 6, 19)]
        assert [level for level, _ in log_lines if level == "WARNING"] == []


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

    async def test_load_interfaces_creates_domain_named_rows(self, registry_loader):
        loader, _ = registry_loader

        count = await loader.load_interfaces()

        assert count == 5 + len(B1_DOMAINS)  # 5 P0 + B1.2

    async def test_rows_are_named_by_domain_with_contract_models(self, registry_loader, test_db):
        loader, _ = registry_loader

        await loader.load_interfaces()

        async with async_sessionmaker(
            test_db.bind, class_=AsyncSession, expire_on_commit=False
        )() as session:
            result = await session.execute(
                select(DataInterface).where(DataInterface.name.in_(P0_DOMAINS))
            )
            rows = result.scalars().all()
        assert len(rows) == 5  # the query filters to the P0 domains
        by_name = {row.name: row for row in rows}
        assert by_name["stock_daily"].display_name == "A股日线行情"
        assert by_name["stock_daily"].return_type == "Bar"
        assert by_name["stock_daily"].category_id is not None

    async def test_load_interfaces_is_idempotent(self, registry_loader):
        loader, _ = registry_loader

        await loader.load_interfaces()
        assert await loader.load_interfaces() == 0


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
        # the domains it serves, so the listing reports it there; the remaining
        # P0 domains still come from akshare.
        assert by_domain["stock_daily"]["source"] == "ths"
        assert by_domain["stock_daily"]["asset_class"] == "equity"
        assert by_domain["stock_daily"]["verified"] is True
        # C10: 财务报表经跨 vendor 逐科目对照转正，权威源随权威序翻到 ths。
        assert by_domain["financial_statement"]["source"] == "ths"
        assert by_domain["financial_statement"]["verified"] is True

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
