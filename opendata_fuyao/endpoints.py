"""fuyao P0 端点：日线、指数/期货/期权 K 线、除复权、标的、日历、成分股、财务报表、基金分红（A3.2）.

每个端点都是"请求构造 + 响应归一化"两段，直接产出中台契约模型
（``Bar`` / ``CorporateAction`` / ``Instrument`` / ``TradingCalendar`` /
``IndexConstituent`` / ``FinancialStatement``），供 A3.4 的 provider 层直接注册路由。

时间语义（同生态 API 事实，A3.2/C6/C9 实测确认）：
- 请求窗口 ``start`` / ``end`` 为**毫秒戳**，闭区间；股票/指数日线跨度 ≤ 10 年
  （超过 code=1003），期货/期权日 K 实测不受该上限约束；
  平台内部为半开窗口，故 ``end`` 取 ``end_date`` 上海零点 - 1ms。
- 股票/指数日线响应行内日期字段是 ``date_ms``，期货/期权日 K 是 ``timestamp``，
  两者都是交易日的 ``Asia/Shanghai`` 零点，归一化为该交易日 date。
- 成分股端点的 ``data.timestamp`` 是**请求时刻**（精确到毫秒、每次调用都变），
  不是指数公司的清单生效日；快照类端点没有别的日期可取，故 ``as_of`` 取该时刻
  的上海日期，语义为「本次观测日」（见 :func:`normalize_index_constituents`）。
- 财务报表端点的 ``data.timestamp`` 则是**窗口内最新报告期末**（实测：quarterly
  limit=6 给 2026-06-30、2023..2024 区间给 2024-12-31），不是请求时刻；报表行的
  日期只取行内的 ``period_end_ms``（报告期末）与 ``report_date_ms``（披露日），
  两者都是上海零点毫秒戳，按 UTC 解释会整体早一天。
- 复权请求值 ``none|forward|backward``，中台语义 ``unadjusted|qfq|hfq``；
  指数、期货、期权三类标的均无复权语义，请求不接受 ``adjust``。
- ETF 日线的窗口约束与股票/指数**不同量级**（C20 二分实测）：单次跨度上限
  1826 天（不是 10 年），且可答起点滚动跟随「今天」（地板 = 今天 − 1827 天，
  两种窗口长度下地板同位）。越界不报错而是回 ``code=0`` 的空 ``item``，
  故只能在本地前置拦截，见 :func:`build_fund_etf_prices_request`。
- ETF 日线信封的 ``data.timestamp`` 实测是**窗口内末根**（== 末根 ``date_ms``）
  而不是请求时刻 ⇒ 它是窗口相对的，新鲜度判据只能取末根交易日，拿信封戳当
  「上游更新到哪天」会静默漏判（C20 实测，与成分股端点那个「请求时刻」相反）。

D10：``Bar`` 只存不复权价，复权序列由本地因子合成；入库路径不应持久化
``adjust != unadjusted`` 的结果。ETF 日线端点（``/api/fund/market/historical``）
只发前复权序列，而 ``adjust`` 在那里是**接受但完全无效**的参数（C20 实测：六种
取值与缺省回同一指纹，``data.adjust`` 恒为 ``null``）——比「拒收」更危险，因为
请求侧看不出没生效。故 ETF 腿必须由 :func:`unadjust_bars` 在本层用
:func:`fetch_fund_dividends` 的分红流换回不复权（实测口径：前复权价 = 不复权价
− 该日之后每份现金分红之和，四只基金共 10,608 格逐格可对）才谈得上入库。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from opendata.data.models import (
    Bar,
    CorporateAction,
    FinancialStatement,
    IndexConstituent,
    Instrument,
    TradingCalendar,
)
from opendata_fuyao.envelope import FuyaoEnvelope
from opendata_fuyao.errors import error_for_transport
from opendata_fuyao.http_client import FuyaoHttpClient

if TYPE_CHECKING:
    from opendata_fuyao.envelope import FuyaoEnvelope
    from opendata_fuyao.http_client import FuyaoHttpClient

_UTC = timezone.utc
_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 端点路径（A3.2 实测确认）。
PRICES_ENDPOINT = "/api/a-share/prices/historical"
INDEX_PRICES_ENDPOINT = "/api/a-share-index/prices/historical"
FUTURES_PRICES_ENDPOINT = "/api/futures/prices/daily"
OPTIONS_PRICES_ENDPOINT = "/api/options/prices/daily"
ADJUSTMENT_FACTORS_ENDPOINT = "/api/a-share/corporate-actions/adjustment-factors"
INDEX_CONSTITUENTS_ENDPOINT = "/api/a-share-index/constituents/ths-stock-list"
TICKERS_LIST_ENDPOINT = "/api/meta/tickers/list"
TICKERS_SEARCH_ENDPOINT = "/api/meta/tickers/search"
CALENDAR_ENDPOINT = "/api/a-share/calendar/trading-days"
INCOME_STATEMENTS_ENDPOINT = "/api/a-share/financials/income-statements"
BALANCE_SHEETS_ENDPOINT = "/api/a-share/financials/balance-sheets"
CASH_FLOW_STATEMENTS_ENDPOINT = "/api/a-share/financials/cash-flow-statements"
FUND_DIVIDENDS_ENDPOINT = "/api/fund/corporate-actions/dividends"
FUND_ETF_PRICES_ENDPOINT = "/api/fund/market/historical"

#: 财务报表三张表（``statement_type`` → 端点）。
FINANCIAL_STATEMENT_ENDPOINTS: Mapping[str, str] = {
    "income": INCOME_STATEMENTS_ENDPOINT,
    "balance": BALANCE_SHEETS_ENDPOINT,
    "cashflow": CASH_FLOW_STATEMENTS_ENDPOINT,
}

#: 每张表 ``data.item[]`` 里承载数值的科目（2026-09-25 实测三张表的完整键集，
#: 减去共有元数据键 ``thscode`` / ``ticker`` / ``period`` / ``fiscal_year`` /
#: ``fiscal_period`` / ``report_date_ms`` / ``period_end_ms`` / ``currency``）。
#: 落库时 ``item`` 即取这里的英文名——契约 ``FinancialStatement.item`` 本就要求
#: 「normalized statement item code」，上游给的正是这一层，故不再翻成中文科目。
FINANCIAL_STATEMENT_ITEMS: Mapping[str, tuple[str, ...]] = {
    "income": (
        "operating_income",
        "operating_costs",
        "operating_expenses",
        "sales_fee",
        "manage_fee",
        "research_and_development_expenses",
        "operating_profit",
        "interest_expenses",
        "profit_total",
        "income_tax_expense",
        "net_profit",
        "parent_holder_net_profit",
        "basic_eps",
    ),
    "balance": (
        "assets_total",
        "total_current_assets",
        "non_current_nets_total",
        "cash",
        "accounts_receivable",
        "total_debt",
        "holder_equity_total",
    ),
    "cashflow": (
        "act_cash_flow_net",
        "invest_cash_flow_net",
        "financing_cash_flow_net",
        "pay_fixed_assets_etc_cash",
        "pay_dividends_profits_interest_cash",
        "cash_equivalents_net_addition",
    ),
}

#: ``fiscal_period`` → 报告期末（月, 日）。实测 ``quarterly`` 模式会给 ``Q4`` 行，
#: 且其值与 ``annual`` 的同财年 ``FY`` 行逐位相同（都是 12-31 累计数）。
FINANCIAL_PERIOD_END: Mapping[str, tuple[int, int]] = {
    "Q1": (3, 31),
    "Q2": (6, 30),
    "Q3": (9, 30),
    "Q4": (12, 31),
    "FY": (12, 31),
}

#: 财务报表的报告期类型（上游 ``period`` 取值；实测**必填**，文档写的默认值不成立）。
FINANCIAL_PERIODS = ("annual", "quarterly")

#: 财务报表 ``limit`` 的上游边界（实测 1..20 可用，0 与 21 均被拒）。
FINANCIAL_STATEMENT_MAX_LIMIT = 20

#: 历史窗口上限：10 年（上游硬约束，超过返回 code=1003）。
MAX_WINDOW = timedelta(days=365 * 10)

#: 复权语义 → 上游 ``adjust`` 取值。
ADJUSTMENTS: Mapping[str, str] = {
    "unadjusted": "none",
    "qfq": "forward",
    "hfq": "backward",
}

#: 标的检索/列表的参数边界（上游约束）。
MAX_SEARCH_LIMIT = 50
MAX_LIST_LIMIT = 10_000

#: 指数标的的上游 ``asset_type`` 取值（A 股指数、同花顺指数与板块共用）。
INDEX_ASSET_TYPE = "a-share-index"

#: 期货/期权标的的上游 ``asset_type`` 取值（目录列举用，实测为复数）。
FUTURES_ASSET_TYPE = "futures"
OPTIONS_ASSET_TYPE = "options"

#: 场内 ETF 的上游 ``asset_type`` 取值（实测 1,696 行、裸码在交易所间唯一、
#: 后缀只有 ``SH``/``SZ``，故一次列举即可解析）。
FUND_ASSET_TYPE = "fund-etf"

#: 期货/期权日 K 周期；端外只允许 ``day_1``（``week_1`` 等被拒但错误文案失真）。
DAILY_TIME_PERIOD = "day_1"

#: 期货/期权日 K 的日期字段名（与股票/指数日线的 ``date_ms`` 不同）。
PERIOD_BAR_DATE_KEY = "timestamp"

#: 日线响应字段 → 中台 ``Bar`` 字段。
_BAR_FIELDS: Mapping[str, str] = {
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "close_price": "close",
    "volume": "volume",
    "turnover": "amount",
}


def shanghai_midnight_millis(value: date) -> int:
    """把交易日转成上游毫秒戳（该日 ``Asia/Shanghai`` 零点）.

    Args:
        value: 交易日。

    Returns:
        毫秒戳。
    """
    return int(datetime.combine(value, time.min, tzinfo=_SHANGHAI).timestamp() * 1000)


def shanghai_today(now: datetime | None = None) -> date:
    """取「今天」的 ``Asia/Shanghai`` 语义日期（供滚动深度地板用）.

    上游是中文市场通道，它的「今天」是上海日期；UTC 日期在 16:00 UTC 之后会比
    上海晚一天，拿 UTC 当天算地板会整体早一天，正好把请求推进「静默空帧」那一档
    （见 :data:`FUND_ETF_DEPTH_DAYS`）。

    Args:
        now: 判定用的时刻；``None`` 取当前时间（测试传固定值）。

    Returns:
        上海时区当天。
    """
    moment = now if now is not None else datetime.now(_UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_SHANGHAI).date()


def millis_to_trading_date(value: object) -> date:
    """把上游 ``date_ms`` / ``ex_date_ms`` 归一化为交易日.

    Args:
        value: 上游毫秒戳。

    Returns:
        交易日（``Asia/Shanghai`` 语义）。

    Raises:
        FuyaoError: 值不是整数毫秒戳。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise error_for_transport("envelope_invalid", detail="date_ms")
    return datetime.fromtimestamp(value / 1000.0, tz=_UTC).astimezone(_SHANGHAI).date()


def _split_window(start: date, end: date) -> tuple[tuple[date, date], ...]:
    """把 ``[start, end)`` 按 ≤ 10 年切块（上游硬约束，本地前置拦截）.

    Args:
        start: 起始交易日（含）。
        end: 结束交易日（不含）。

    Returns:
        连续子窗口（同样半开）。

    Raises:
        FuyaoError: 窗口为空或反向。
    """
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        candidate = cursor + MAX_WINDOW
        chunk_end = min(candidate, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return tuple(chunks)


def build_prices_request(
    *, symbol: str, start: date, end: date, adjust: str = "unadjusted"
) -> dict[str, Any]:
    """构造日线请求参数（毫秒戳闭区间）.

    Args:
        symbol: 上游标的代码（如 ``600519.SH``）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。
        adjust: ``unadjusted`` / ``qfq`` / ``hfq``。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空、复权语义未知或窗口非法。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    if adjust not in ADJUSTMENTS:
        raise error_for_transport("envelope_invalid", detail="adjust")
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    return {
        "thscode": symbol.strip(),
        "interval": "1d",
        "start": shanghai_midnight_millis(start),
        "end": shanghai_midnight_millis(end) - 1,
        "adjust": ADJUSTMENTS[adjust],
    }


def build_index_prices_request(*, symbol: str, start: date, end: date) -> dict[str, Any]:
    """构造指数日线请求参数（毫秒戳闭区间，**无** ``adjust``）.

    指数没有复权语义（上游 ``data.adjust`` 固定为 ``null``），故不接受
    ``adjust``；窗口约束与股票日线一致（≤ 10 年，超过 ``code=1003``）。

    Args:
        symbol: 上游指数代码（如 ``000300.SH``、``886042.TI``）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空或窗口非法。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    return {
        "thscode": symbol.strip(),
        "interval": "1d",
        "start": shanghai_midnight_millis(start),
        "end": shanghai_midnight_millis(end) - 1,
    }


def build_period_daily_request(*, symbol: str, start: date, end: date) -> dict[str, Any]:
    """构造期货/期权日 K 请求参数（``time_period=day_1``，**无** ``interval``）.

    与股票/指数日线端点的三处差异（A3.2/C6 实测）：周期参数名是
    ``time_period``（传 ``interval`` 直接 ``标的或字段不受支持``）；``start``/``end``
    必须**成对**给出，否则只回最近 100 根；实测 10.7 年窗口不被拒，故本函数
    不切块。合约价格本身即观测值，无复权语义，故不接受 ``adjust``。

    Args:
        symbol: 上游完整代码（须带市场/交易所后缀）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空或窗口非法。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    return {
        "thscode": symbol.strip(),
        "time_period": DAILY_TIME_PERIOD,
        "start": shanghai_midnight_millis(start),
        "end": shanghai_midnight_millis(end) - 1,
    }


def build_index_constituents_request(*, symbol: str) -> dict[str, Any]:
    """构造指数成分股请求参数（单指数、**必须**带市场后缀）.

    上游只接受一个 ``thscode``（实测：逗号分隔的批量写法返回 ``code=1002``
    「请求参数超出取值域」，``limit`` / ``offset`` 被忽略、整份清单一次给全）。
    裸码同样被 ``1002`` 拒收，而后缀对指数并不唯一（沪深 300 既有
    ``000300.SH`` 也有 ``399300.SZ``），故本地先拦下裸码，让调用方去
    :func:`opendata.data.providers.ths.models._client.resolve_index_code`
    解析，而不是在这里猜一个。

    Args:
        symbol: 上游指数代码（``000300.SH`` / ``886042.TI``）。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空、不是字符串或不含市场后缀。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    candidate = symbol.strip()
    if "." not in candidate:
        raise error_for_transport("envelope_invalid", detail="constituents_symbol_qualified")
    return {"thscode": candidate}


def build_tickers_search_request(
    *,
    query: str,
    limit: int = 10,
    exchange: str | None = None,
    asset_type: str | None = None,
) -> dict[str, Any]:
    """构造标的检索参数（``q`` 必填，上限 50）.

    Args:
        query: 关键词。
        limit: 返回上限（1~50）。
        exchange: 可选交易所过滤。
        asset_type: 可选资产类型过滤。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 关键词为空或上限越界。
    """
    if not isinstance(query, str) or not query.strip():
        raise error_for_transport("envelope_invalid", detail="query")
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise error_for_transport("envelope_invalid", detail="search_limit")
    params: dict[str, Any] = {"q": query.strip(), "limit": limit}
    if exchange is not None:
        params["exchange"] = exchange
    if asset_type is not None:
        params["asset_type"] = asset_type
    return params


def build_tickers_list_request(
    *, limit: int = 1000, offset: int = 0, asset_type: str | None = None
) -> dict[str, Any]:
    """构造标的列表参数（分页，上限 10000）.

    Args:
        limit: 单页数量（1~10000）。
        offset: 偏移（≥0）。
        asset_type: 可选资产类型过滤。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 上限或偏移越界。
    """
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise error_for_transport("envelope_invalid", detail="list_limit")
    if offset < 0:
        raise error_for_transport("envelope_invalid", detail="offset")
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if asset_type is not None:
        params["asset_type"] = asset_type
    return params


def build_adjustment_factors_request(
    *, symbol: str, start: date | None = None, end: date | None = None
) -> dict[str, Any]:
    """构造除复权事件请求（``from`` / ``to`` 为 ``YYYY-MM-DD``，可选）.

    Args:
        symbol: 上游标的代码。
        start: 可选起始日期（含）。
        end: 可选结束日期（含）。
        （两者须同时给出或同时省略：上游只接受整段或不限。）

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空或只给出一端。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    if (start is None) != (end is None):
        raise error_for_transport("envelope_invalid", detail="adjustment_window")
    params: dict[str, Any] = {"thscode": symbol.strip()}
    if start is not None and end is not None:
        if start > end:
            raise error_for_transport("envelope_invalid", detail="adjustment_window")
        params["from"] = start.isoformat()
        params["to"] = end.isoformat()
    return params


def _items(envelope: FuyaoEnvelope, *, context: str) -> tuple[Mapping[str, Any], ...]:
    """取出 ``data.item`` 并要求每行为映射."""
    rows: list[Mapping[str, Any]] = []
    for item in envelope.items:
        if not isinstance(item, Mapping):
            raise error_for_transport("envelope_invalid", detail=f"{context}_item")
        rows.append(item)
    return tuple(rows)


def _require_value(row: Mapping[str, Any], key: str, *, context: str) -> Any:  # noqa: ANN401  # 上游原值
    """取必需字段，缺失即失败关闭."""
    if key not in row:
        raise error_for_transport("envelope_invalid", detail=f"{context}_{key}")
    return row[key]


def normalize_bars(
    envelope: FuyaoEnvelope, *, symbol: str, date_key: str = "date_ms"
) -> tuple[Bar, ...]:
    """把日线响应归一化为 ``Bar``（按交易日升序）.

    上游行内**不含** ``thscode``（只在 ``data.thscode`` 回显），故标的自请求
    参数显式传入并校验——契约要求 ``Bar.symbol`` 非空，空值会写坏 ods 主键。

    Args:
        envelope: 成功信封。
        symbol: 请求使用的上游标的代码。
        date_key: 行内交易日字段名（股票/指数为 ``date_ms``，期货/期权日 K
            为 ``timestamp``）。

    Returns:
        未复权日线行。

    Raises:
        FuyaoError: 标的为空、行结构非法（缺交易日或字段类型不符）。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    resolved_symbol = symbol.strip()
    bars: list[Bar] = []
    for row in _items(envelope, context="bar"):
        trade_date = millis_to_trading_date(_require_value(row, date_key, context="bar"))
        try:
            bars.append(
                Bar(
                    symbol=resolved_symbol,
                    trade_date=trade_date,
                    open=float(_require_value(row, "open_price", context="bar")),
                    high=float(_require_value(row, "high_price", context="bar")),
                    low=float(_require_value(row, "low_price", context="bar")),
                    close=float(_require_value(row, "close_price", context="bar")),
                    volume=float(_require_value(row, "volume", context="bar")),
                    amount=float(_require_value(row, "turnover", context="bar")),
                )
            )
        except (TypeError, ValueError) as exc:
            raise error_for_transport("envelope_invalid", detail="bar_fields") from exc
    bars.sort(key=lambda bar: bar.trade_date)
    return tuple(bars)


def normalize_adjustment_factors(
    envelope: FuyaoEnvelope, *, symbol: str
) -> tuple[CorporateAction, ...]:
    """把除复权事件流归一化为 ``CorporateAction``（按除权日降序）.

    上游不返回事件类型：``dividend_per_share`` → 现金分红、``per_share_bonus``
    → 送股，保留该语义，不伪造其它字段。

    Args:
        envelope: 成功信封。
        symbol: 请求使用的上游标的代码（行内只有 ``ticker`` 短码）。

    Returns:
        除权除息事件。

    Raises:
        FuyaoError: 标的为空或行结构非法。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    resolved_symbol = symbol.strip()
    events: list[CorporateAction] = []
    for row in _items(envelope, context="adjustment"):
        ex_date = millis_to_trading_date(_require_value(row, "ex_date_ms", context="adjustment"))
        try:
            events.append(
                CorporateAction(
                    symbol=resolved_symbol,
                    ex_date=ex_date,
                    cash_dividend=float(row.get("dividend_per_share") or 0.0),
                    stock_dividend=float(row.get("per_share_bonus") or 0.0),
                )
            )
        except (TypeError, ValueError) as exc:
            raise error_for_transport("envelope_invalid", detail="adjustment_fields") from exc
    events.sort(key=lambda event: event.ex_date, reverse=True)
    return tuple(events)


#: 上游对期权合约的 ``thscode`` 是不透明序号（可读码在 ``ticker``，实测
#: ``90007464.SZ`` ↔ ``159922P2612M003800``），故「去后缀等于 ticker」这条对账
#: 只对其余资产类型成立（C6 实测，见 ``resolve_option_code``）。
OPAQUE_CODE_ASSET_TYPES = frozenset({"options"})

#: 期货**合成序列**的展示码尾标（2026-09-25 实测：期货目录 1,139 行里有 265 行
#: 无任何日期，全属这三类，且 874 个真实合约的行 100% 对账相等）：
#: ``AP00.CZC`` ↔ ``AP7777``（连续）、``ICZL.CFE`` ↔ ``IC9999``（主连）、
#: ``IC8888.CFE`` ↔ ``IC8888``（加权，两个写法恰好相同）。前两类两个写法本就
#: 不同，是上游约定而非脏数据；真实月份合约（``AP612`` / ``IF2612``）的尾四位
#: 是年月，不可能是这四个数，故只豁免这三类尾标。
SYNTHETIC_SERIES_TICKER_TAILS = frozenset({"7777", "8888", "9999"})

#: 指数目录的 ``ticker`` 是**另一套编号**，不是 ``thscode`` 的另一种写法，故两者
#: 之间不存在可对的账（2026-09-25 实测 ``docs/evidence/C18/index-code-rule-audit.txt``：
#: 指数目录 1,431 行里 206 行「去后缀 ≠ ticker」——201 行是通达信的字母数字指数码，
#: 其中就有主系列自己（``000001.SH`` ↔ ``1A0001`` 上证指数、``000002.SH`` ↔ ``1A0002``
#: Ａ股指数、``991001.TI`` ↔ ``1C0003`` 旧码），另 5 行是币种/R 份额变体
#: （``970006.SZ`` ↔ ``988006`` 创业板指港币 CNH）。既没有取值规则能把这两类与「脏
#: 数据」分开，「只丢掉不等的行」更会把上证综指从目录里丢掉；而对账本身换不来任何保护：
#: ``ticker`` 既不落库（``Instrument`` 没有该字段）也不参与裸码解析
#: （``_resolve_by_listing`` 只匹配 ``thscode`` 前缀，实测 1,431 个前缀两两互异 ⇒ 裸码
#: 唯一）。股票（5,578 行）与场内 ETF（1,696 行）目录实测 **0 行**不等，对账在那些资产
#: 类型上照旧生效。
DISPLAY_CODE_ASSET_TYPES = frozenset({INDEX_ASSET_TYPE})


def _codes_agree(symbol: str, plain: object, asset_type: str) -> bool:
    """这一行的两个代码写法是否可接受（代码对账是否成立）.

    Args:
        symbol: 上游 ``thscode``（带交易所后缀）。
        plain: 上游 ``ticker``（展示码，可能不是字符串）。
        asset_type: 上游规范化资产类型。

    Returns:
        期权（不透明序号）、指数（展示码另成一套编号）、合成序列尾标与 ``ticker``
        缺失的行返回 ``True``（不参与对账）；其余要求去后缀后逐字相等。
    """
    if asset_type in OPAQUE_CODE_ASSET_TYPES or asset_type in DISPLAY_CODE_ASSET_TYPES:
        return True
    if not isinstance(plain, str) or not plain:
        return True
    if plain[-4:] in SYNTHETIC_SERIES_TICKER_TAILS:
        return True
    return symbol.partition(".")[0] == plain


def normalize_instruments(
    envelope: FuyaoEnvelope, *, as_of: date | None = None
) -> tuple[Instrument, ...]:
    """把标的列表/检索归一化为 ``Instrument``.

    2026-09-25 实测后改写了三处口径（原实现把「在册」当成「在市」）：

    * ``delist_date``：上游 ``end_date`` 是**合约到期（交割）日**，
      ``last_trade_date`` 才是**停止可交易之日**，中台字段的语义是后者，故取
      ``last_trade_date``、无该字段时回落到 ``end_date``。实测两者可以差四天
      （``BZ2609.DCE`` 最后交易日 2026-09-24、到期日 2026-09-28），按 ``end_date``
      判「在市」会把已停牌的合约多算四个可交易日。
    * ``status``：不再由「``end_date`` 是否为空」派生。该判据把期货目录 1,139 行
      里 **874 个在市月份合约全标成 ``delisted``**，而 265 个无任何日期的合成序列
      （连续/主连/加权）全标成 ``active`` —— 恰好全反。现与快照观测日比较：
      ``delist_date`` **早于**观测日才算停止交易（最后一天当天仍可交易，故取严格
      小于）。观测日默认取信封 ``data.timestamp``（实测是**快照加载时刻**，本例
      2026-09-24 16:00，不是请求时刻）；信封不带该字段时状态判不了，回落到
      ``"unknown"`` 而不是墙上时钟 —— 代码消歧路径（``search_instruments``）只消费
      ``symbol``，不该因为一个状态字段无从派生而整体失败。
    * 代码对账：``thscode`` 去后缀必须等于 ``ticker``，否则整页失败关闭；期权按
      :data:`OPAQUE_CODE_ASSET_TYPES`、指数按 :data:`DISPLAY_CODE_ASSET_TYPES` 豁免
      （两者的 ``ticker`` 都不是 ``thscode`` 的另一种写法），期货合成序列按
      :data:`SYNTHETIC_SERIES_TICKER_TAILS` 豁免（见各常量与 :func:`_codes_agree`
      的实测依据）。

    Args:
        envelope: 成功信封。
        as_of: 状态判定基准日；缺省取信封快照日，两者都没有则 ``status`` 为
            ``"unknown"``。

    Returns:
        标的信息（按 ``item`` 原序）。

    Raises:
        FuyaoError: 行缺 ``thscode``，或两个代码写法对不上（非豁免类）。
    """
    snapshot = as_of if as_of is not None else envelope_snapshot(envelope)
    instruments: list[Instrument] = []
    for row in _items(envelope, context="ticker"):
        symbol = _require_value(row, "thscode", context="ticker")
        if not isinstance(symbol, str) or not symbol.strip():
            raise error_for_transport("envelope_invalid", detail="ticker_thscode")
        exchange = row.get("exchange") or row.get("asset_type") or ""
        if not _codes_agree(symbol, row.get("ticker"), str(row.get("asset_type") or "")):
            raise error_for_transport("envelope_invalid", detail="ticker_code_mismatch")
        delist = _parse_optional_date(row.get("last_trade_date")) or _parse_optional_date(
            row.get("end_date")
        )
        instruments.append(
            Instrument(
                symbol=symbol.strip(),
                exchange=str(exchange),
                name=str(row.get("name") or ""),
                status=_instrument_status(delist, snapshot),
                currency=str(row.get("currency") or "CNY"),
                list_date=_parse_optional_date(row.get("list_date")),
                delist_date=delist,
            )
        )
    return tuple(instruments)


def _instrument_status(delist: date | None, snapshot: date | None) -> str:
    """按基准日给出在册标的的可交易状态.

    Args:
        delist: 停止可交易之日（``None`` 表示上游未发布）。
        snapshot: 基准日（``None`` 表示无观测时刻）。

    Returns:
        ``active`` / ``delisted`` / ``unknown``。
    """
    if snapshot is None:
        return "unknown"
    return "delisted" if delist is not None and delist < snapshot else "active"


def envelope_snapshot(envelope: FuyaoEnvelope) -> date | None:
    """从信封 ``data.timestamp`` 取快照观测日（上海时区语义）.

    Args:
        envelope: 成功信封。

    Returns:
        快照观测日；信封不带 ``data.timestamp`` 时为 ``None``。
    """
    if envelope.data_timestamp_ms is None:
        return None
    return millis_to_trading_date(envelope.data_timestamp_ms)


def _parse_optional_date(value: object) -> date | None:
    """解析上游 ``YYYY-MM-DD`` 日期；空值返回 ``None``."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise error_for_transport("envelope_invalid", detail="date_field")
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError as exc:
        raise error_for_transport("envelope_invalid", detail="date_field") from exc


def normalize_calendar(envelope: FuyaoEnvelope, *, exchange: str) -> tuple[TradingCalendar, ...]:
    """把交易日历归一化为 ``TradingCalendar``（按日期升序）.

    上游日历只给交易日，故 ``is_open`` 恒为 ``True``；``exchange`` 由调用方
    显式给出（上游 A 股日历不区分交易所）。

    Args:
        envelope: 成功信封。
        exchange: 交易所标识（如 ``CN-SSE``）。

    Returns:
        交易日历行。

    Raises:
        FuyaoError: 行缺交易日，或 ``exchange`` 为空。
    """
    if not isinstance(exchange, str) or not exchange.strip():
        raise error_for_transport("envelope_invalid", detail="exchange")
    days: list[TradingCalendar] = []
    for row in _items(envelope, context="calendar"):
        trading_date = millis_to_trading_date(_require_value(row, "date_ms", context="calendar"))
        days.append(TradingCalendar(exchange=exchange.strip(), date=trading_date, is_open=True))
    days.sort(key=lambda day: day.date)
    return tuple(days)


def normalize_index_constituents(
    envelope: FuyaoEnvelope, *, index_symbol: str
) -> tuple[IndexConstituent, ...]:
    """把成分股清单归一化为 ``IndexConstituent``（按成员代码升序）.

    三处口径都是实测后如实落地，不把猜测写进契约：

    * ``as_of``：上游行内没有生效日，``data.timestamp`` 是**请求时刻**的毫秒戳
      （实测精确到毫秒、每次调用都变），故快照日期取该时刻的上海日期，语义是
      「本次观测日」，不是指数公司的调整生效日。
    * ``weight``：该端点不发布权重（响应行只有 ``thscode`` / ``ticker`` /
      ``name``），留 ``None`` 而不补算、不从别处借数。
    * 成员身份用 ``thscode`` 与 ``ticker`` 两路**对账**（前者去后缀须等于后者），
      不一致即失败关闭——落库用裸 6 位码做合并键，选错一路不会报错，只会把成分
      股接到另一个标的上。

    Args:
        envelope: 成功信封。
        index_symbol: 请求使用的上游指数代码。

    Returns:
        成分股快照行；``symbol`` 为裸 6 位代码，``index_symbol`` 原样透传请求值。

    Raises:
        FuyaoError: ``index_symbol`` 为空、缺 ``data.timestamp``、行结构非法
            或两路成员标识不一致。
    """
    if not isinstance(index_symbol, str) or not index_symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    if envelope.data_timestamp_ms is None:
        raise error_for_transport("envelope_invalid", detail="constituents_timestamp")
    as_of = millis_to_trading_date(envelope.data_timestamp_ms)
    resolved_index = index_symbol.strip()
    rows: list[IndexConstituent] = []
    for row in _items(envelope, context="constituent"):
        qualified = _require_value(row, "thscode", context="constituent")
        plain = _require_value(row, "ticker", context="constituent")
        if not isinstance(qualified, str) or not isinstance(plain, str):
            raise error_for_transport("envelope_invalid", detail="constituent_symbol_type")
        if not plain.strip() or qualified.partition(".")[0] != plain:
            raise error_for_transport("envelope_invalid", detail="constituent_symbol_mismatch")
        rows.append(
            IndexConstituent(
                index_symbol=resolved_index,
                symbol=plain.strip(),
                as_of=as_of,
                weight=None,
            )
        )
    rows.sort(key=lambda row: row.symbol)
    return tuple(rows)


def fetch_daily_bars(
    client: FuyaoHttpClient,
    *,
    symbol: str,
    start: date,
    end: date,
    adjust: str = "unadjusted",
) -> tuple[Bar, ...]:
    """取日线（窗口超 10 年自动切块合并）.

    Args:
        client: 传输层客户端。
        symbol: 上游标的代码。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。
        adjust: ``unadjusted`` / ``qfq`` / ``hfq``；D10 要求入库路径用
            ``unadjusted``，复权序列由本地因子合成。

    Returns:
        按交易日升序的 ``Bar``。
    """
    bars: list[Bar] = []
    for chunk_start, chunk_end in _split_window(start, end):
        response = client.get(
            PRICES_ENDPOINT,
            params=build_prices_request(
                symbol=symbol, start=chunk_start, end=chunk_end, adjust=adjust
            ),
        )
        bars.extend(normalize_bars(response.envelope, symbol=symbol))
    bars.sort(key=lambda bar: bar.trade_date)
    return tuple(bars)


def fetch_index_daily_bars(
    client: FuyaoHttpClient, *, symbol: str, start: date, end: date
) -> tuple[Bar, ...]:
    """取指数日线（窗口超 10 年自动切块合并）.

    Args:
        client: 传输层客户端。
        symbol: 上游指数代码（须带市场后缀）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。

    Returns:
        按交易日升序的 ``Bar``。指数价格本身即为可观测序列，无复权口径。
    """
    bars: list[Bar] = []
    for chunk_start, chunk_end in _split_window(start, end):
        response = client.get(
            INDEX_PRICES_ENDPOINT,
            params=build_index_prices_request(symbol=symbol, start=chunk_start, end=chunk_end),
        )
        bars.extend(normalize_bars(response.envelope, symbol=symbol))
    bars.sort(key=lambda bar: bar.trade_date)
    return tuple(bars)


def fetch_period_daily_bars(
    client: FuyaoHttpClient, *, endpoint: str, symbol: str, start: date, end: date
) -> tuple[Bar, ...]:
    """取期货/期权日 K（单次请求，行内日期字段为 ``timestamp``）.

    Args:
        client: 传输层客户端。
        endpoint: :data:`FUTURES_PRICES_ENDPOINT` 或 :data:`OPTIONS_PRICES_ENDPOINT`。
        symbol: 上游完整代码（须带交易所后缀）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。

    Returns:
        按交易日升序的 ``Bar``。合约价为观测值，无复权口径。
        商品指数一类标的上游把 ``turnover`` 回成 ``null``，而 ``Bar.amount``
        非空，故这类标的在此失败关闭（不归零、不补估）。

    Raises:
        FuyaoError: 行结构非法或缺字段。
    """
    response = client.get(
        endpoint, params=build_period_daily_request(symbol=symbol, start=start, end=end)
    )
    return normalize_bars(response.envelope, symbol=symbol, date_key=PERIOD_BAR_DATE_KEY)


def fetch_index_constituents(
    client: FuyaoHttpClient, *, symbol: str
) -> tuple[IndexConstituent, ...]:
    """取单个指数的当前成分股清单（一次请求给全，无分页）.

    Args:
        client: 传输层客户端。
        symbol: 上游指数代码（须带市场后缀，裸码由 provider 层先解析）。

    Returns:
        按成员代码升序的 ``IndexConstituent``；``weight`` 恒为 ``None``
        （上游不发布权重）。

    Raises:
        FuyaoError: 参数非法、标的不在上游指数全集内，或行结构非法。
    """
    response = client.get(
        INDEX_CONSTITUENTS_ENDPOINT, params=build_index_constituents_request(symbol=symbol)
    )
    return normalize_index_constituents(response.envelope, index_symbol=symbol)


def search_instruments(
    client: FuyaoHttpClient,
    *,
    query: str,
    limit: int = 10,
    exchange: str | None = None,
    asset_type: str | None = None,
) -> tuple[Instrument, ...]:
    """按关键词检索标的.

    Args:
        client: 传输层客户端。
        query: 关键词。
        limit: 返回上限（1~50）。
        exchange: 可选交易所过滤。
        asset_type: 可选资产类型过滤。

    Returns:
        标的信息。
    """
    response = client.get(
        TICKERS_SEARCH_ENDPOINT,
        params=build_tickers_search_request(
            query=query, limit=limit, exchange=exchange, asset_type=asset_type
        ),
    )
    return normalize_instruments(response.envelope)


def list_instruments(
    client: FuyaoHttpClient,
    *,
    limit: int = 1000,
    offset: int = 0,
    asset_type: str | None = None,
) -> tuple[Instrument, ...]:
    """列出标的（分页）.

    Args:
        client: 传输层客户端。
        limit: 单页数量（1~10000）。
        offset: 偏移。
        asset_type: 可选资产类型过滤。

    Returns:
        标的信息。
    """
    response = client.get(
        TICKERS_LIST_ENDPOINT,
        params=build_tickers_list_request(limit=limit, offset=offset, asset_type=asset_type),
    )
    return normalize_instruments(response.envelope)


def fetch_trading_calendar(
    client: FuyaoHttpClient, *, exchange: str
) -> tuple[TradingCalendar, ...]:
    """取 A 股交易日历（上游只给**滚动的一年窗口**，实测无窗口参数）.

    2026-09-25 凌晨实测：一次返回 242 行，区间是「请求日往前一年」到**上一
    个已完成交易日**（2025-09-25..2026-09-24），当天的行情尚未收盘故不在内；
    ``start_date`` / ``begin_date`` / ``start`` / ``year`` / ``days`` / ``limit``
    六种候选参数逐个试过后响应一字不差，说明它们全部被忽略，因此这里不传参数。
    语义后果：可算 ``prev_trade_date``，**算不了**未来窗口的
    ``next_trade_date``，也不能用于历史回填的日序还原。

    Args:
        client: 传输层客户端。
        exchange: 交易所标识（显式给出，上游不区分交易所）。

    Returns:
        交易日历行，按日期升序；只含交易日（上游不发布 ``is_open=False`` 行）。
    """
    response = client.get(CALENDAR_ENDPOINT, params={})
    return normalize_calendar(response.envelope, exchange=exchange)


def build_financial_statements_request(
    *,
    symbol: str,
    period: str = "annual",
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """构造财务报表请求参数（单标的、必须带后缀、两种取数模式互斥）.

    三处按 2026-09-25 实测落地，都不照抄文档：

    * ``period`` 文档写「必需，默认 ``annual``」——实测省略即 ``code=1001``
      「请求参数缺失或格式非法」，故这里总是显式发出。
    * 裸码、逗号分隔多码、错误后缀、未知 ``period`` 一律 ``code=1002``；后缀对
      股票虽只有 ``SH``/``SZ``/``BJ``，也不在此猜，交给
      :func:`opendata.data.providers.ths.models._client.resolve_code`。
    * ``limit`` 与 ``start``/``end`` 互斥、``start``/``end`` 须成对：上游对这两
      种非法组合回的是「请求的标的或字段不受支持」（与真实原因无关），本地先拦。

    Args:
        symbol: 上游标的代码（``600519.SH``）。
        period: ``annual``（仅 Q4 报告期）或 ``quarterly``（每个季度末，累计值）。
        start: 时间区间模式起始日（含，报告期末按上海零点比较）。
        end: 时间区间模式结束日（含）。
        limit: 最近 N 期模式，``1..20``。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空或无后缀、``period`` 未知、两种模式混用、
            窗口只给一端 / 反向 / 超过 10 年、``limit`` 越界。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    candidate = symbol.strip()
    if "." not in candidate:
        raise error_for_transport("envelope_invalid", detail="financial_symbol_qualified")
    if period not in FINANCIAL_PERIODS:
        raise error_for_transport("envelope_invalid", detail="financial_period")
    params: dict[str, Any] = {"thscode": candidate, "period": period}
    if (start is None) != (end is None):
        raise error_for_transport("envelope_invalid", detail="financial_window")
    if start is not None and end is not None:
        if limit is not None:
            raise error_for_transport("envelope_invalid", detail="financial_mode")
        if start > end:
            raise error_for_transport("envelope_invalid", detail="financial_window")
        if end - start > MAX_WINDOW:
            raise error_for_transport("envelope_invalid", detail="financial_window_span")
        params["start"] = shanghai_midnight_millis(start)
        params["end"] = shanghai_midnight_millis(end)
        return params
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise error_for_transport("envelope_invalid", detail="financial_limit_type")
        if not 1 <= limit <= FINANCIAL_STATEMENT_MAX_LIMIT:
            raise error_for_transport("envelope_invalid", detail="financial_limit")
        params["limit"] = limit
    return params


def _financial_report_period(row: Mapping[str, Any], *, context: str) -> date:
    """取报告期末，并用 ``fiscal_year`` / ``fiscal_period`` 交叉校验毫秒戳.

    ``period_end_ms`` 是上海零点：按 UTC 解释会整体早一天（实测
    ``1767110400000`` 上海语义为 2025-12-31、UTC 语义为 2025-12-30），而财年 +
    报告期本身已把期末唯一确定，故两路对账、不一致即失败关闭。

    Args:
        row: 上游响应行。
        context: 失败信息前缀。

    Returns:
        报告期末。

    Raises:
        FuyaoError: 财年/报告期缺失或类型非法、报告期未知、与毫秒戳不一致。
    """
    year = _require_value(row, "fiscal_year", context=context)
    fiscal_period = _require_value(row, "fiscal_period", context=context)
    if not isinstance(year, int) or isinstance(year, bool) or not isinstance(fiscal_period, str):
        raise error_for_transport("envelope_invalid", detail=f"{context}_fiscal_type")
    if fiscal_period not in FINANCIAL_PERIOD_END:
        raise error_for_transport("envelope_invalid", detail=f"{context}_fiscal_period")
    month, day = FINANCIAL_PERIOD_END[fiscal_period]
    derived = date(year, month, day)
    stamped = millis_to_trading_date(_require_value(row, "period_end_ms", context=context))
    if derived != stamped:
        raise error_for_transport("envelope_invalid", detail=f"{context}_period_mismatch")
    return derived


def normalize_financial_statements(
    envelope: FuyaoEnvelope, *, statement_type: str
) -> tuple[FinancialStatement, ...]:
    """把三张报表的宽行摊成长表 ``FinancialStatement``.

    四条口径都来自实测：

    * ``item`` 用上游的英文科目名（契约要求「normalized code」，上游给的正是
      这一层）；只取 :data:`FINANCIAL_STATEMENT_ITEMS` 里登记的键，其余键是元数据。
    * ``value`` 单位是**原币元**（``basic_eps`` 为元/股），A 股 ``currency`` 实测
      恒为 ``CNY``；出现其它币种即失败关闭——契约没有币种字段，静默入库会把
      非人民币数当成人民币。
    * ``announce_date`` 取 ``report_date_ms``（披露日）。实测它与新浪的「公告日期」
      逐期一致，包括追溯调整造成的错位（茅台 FY2024 与 FY2025 同为 2026-04-17、
      FY2021 为 2023-03-31），故不本地推导。
    * ``revision`` 恒为 ``1``：上游一次响应只给**当前生效**的那一版数值，没有
      历史修订序列，故不猜修订号（跨请求的时点差异由 ``announce_date`` 承载）。

    Args:
        envelope: 成功信封。
        statement_type: ``income`` / ``balance`` / ``cashflow``。

    Returns:
        长表行，按 ``(report_period, announce_date, item)`` 升序。

    Raises:
        FuyaoError: ``statement_type`` 未知、行结构非法、科目键缺失或值非数值、
            币种非 CNY、报告期末两路对账不一致。
    """
    if statement_type not in FINANCIAL_STATEMENT_ITEMS:
        raise error_for_transport("envelope_invalid", detail="financial_statement_type")
    items = FINANCIAL_STATEMENT_ITEMS[statement_type]
    rows: list[FinancialStatement] = []
    for row in _items(envelope, context="financial"):
        context = f"financial_{statement_type}"
        qualified = _require_value(row, "thscode", context=context)
        plain = _require_value(row, "ticker", context=context)
        if not isinstance(qualified, str) or not isinstance(plain, str):
            raise error_for_transport("envelope_invalid", detail=f"{context}_symbol_type")
        if not plain.strip() or qualified.partition(".")[0] != plain:
            raise error_for_transport("envelope_invalid", detail=f"{context}_symbol_mismatch")
        currency = _require_value(row, "currency", context=context)
        if currency != "CNY":
            raise error_for_transport("envelope_invalid", detail=f"{context}_currency")
        report_period = _financial_report_period(row, context=context)
        announce_date = millis_to_trading_date(
            _require_value(row, "report_date_ms", context=context)
        )
        for key in items:
            if key not in row:
                raise error_for_transport("envelope_invalid", detail=f"{context}_{key}")
            value = row[key]
            if value is None:
                continue  # 上游真实缺值（标准化为 null），不是键缺失
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise error_for_transport("envelope_invalid", detail=f"{context}_{key}_type")
            rows.append(
                FinancialStatement(
                    symbol=plain.strip(),
                    statement_type=statement_type,
                    report_period=report_period,
                    announce_date=announce_date,
                    item=key,
                    value=float(value),
                    revision=1,
                )
            )
    rows.sort(key=lambda r: (r.report_period, r.announce_date, r.item))
    return tuple(rows)


def fetch_financial_statements(
    client: FuyaoHttpClient,
    *,
    symbol: str,
    statement_type: str,
    period: str = "annual",
    start: date | None = None,
    end: date | None = None,
    limit: int | None = None,
) -> tuple[FinancialStatement, ...]:
    """取一张财务报表的多期序列（宽行摊长为长表）.

    Args:
        client: 传输层客户端。
        symbol: 上游标的代码（须带市场后缀，裸码由 provider 层先解析）。
        statement_type: ``income`` / ``balance`` / ``cashflow``。
        period: ``annual`` 或 ``quarterly``（季度累计值）。
        start: 时间区间模式起始日（含）。
        end: 时间区间模式结束日（含）。
        limit: 最近 N 期模式（``1..20``）。

    Returns:
        长表行；单位原币元。

    Raises:
        FuyaoError: 参数非法、标的不可查，或行结构非法。
    """
    if statement_type not in FINANCIAL_STATEMENT_ENDPOINTS:
        raise error_for_transport("envelope_invalid", detail="financial_statement_type")
    response = client.get(
        FINANCIAL_STATEMENT_ENDPOINTS[statement_type],
        params=build_financial_statements_request(
            symbol=symbol, period=period, start=start, end=end, limit=limit
        ),
    )
    return normalize_financial_statements(response.envelope, statement_type=statement_type)


def fetch_adjustment_factors(
    client: FuyaoHttpClient,
    *,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> tuple[CorporateAction, ...]:
    """取除复权事件流（按除权日降序）.

    Args:
        client: 传输层客户端。
        symbol: 上游标的代码。
        start: 可选起始日期（含）。
        end: 可选结束日期（含）。

    Returns:
        除权除息事件。
    """
    response = client.get(
        ADJUSTMENT_FACTORS_ENDPOINT,
        params=build_adjustment_factors_request(symbol=symbol, start=start, end=end),
    )
    return normalize_adjustment_factors(response.envelope, symbol=symbol)


#: 上游 ``progress`` 的「已实施」码（2026-09-25 实测：510300.SH 的 14 笔、
#: 510050.SH 的 18 笔全部为 ``"2"``；文档响应示例写的是中文「实施」，实际发的是
#: 数字码）。未知码不猜语义——分红尚未落地就写进事件流，等于把预案当既成事实。
IMPLEMENTED_PROGRESS = frozenset({"2"})

#: ``dividend_total`` 与逐笔现金分红求和的容差：上游汇总值是浮点，实测
#: ``0.8800000000000001`` 这类末位噪声，故比对留位而非逐字相等。
DIVIDEND_TOTAL_TOLERANCE = 1e-6


def _qualified_fund_symbol(symbol: object) -> str:
    """校验基金标的：非空且带市场后缀，返回去空格后的代码.

    两条基金腿（分红、ETF 日线）共用同一条规则：裸码上游回 ``code=1002``，
    必须在本地拦住而不是让上游回话；错误 detail 也保持一致，排障时才能按码找人。

    Args:
        symbol: 请求使用的上游基金代码。

    Returns:
        去空格后的代码。

    Raises:
        FuyaoError: 标的为空或无市场后缀。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    candidate = symbol.strip()
    if "." not in candidate:
        raise error_for_transport("envelope_invalid", detail="fund_symbol_qualified")
    return candidate


def build_fund_dividends_request(*, symbol: str) -> dict[str, Any]:
    """构造基金分红请求参数（单标的、必须带市场后缀）.

    Args:
        symbol: 上游基金代码（``510300.SH``）。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空或无市场后缀（裸码上游回 ``code=1002``，本地先拦）。
    """
    return {"thscode": _qualified_fund_symbol(symbol)}


def _is_placeholder_dividend(row: Mapping[str, Any]) -> bool:
    """这一行是不是「该基金从无分红」的占位行（每个字段都是 ``null``）.

    实测（2026-09-25）：159915.SZ 与 512880.SH 都回 ``dividend_count: 0`` 但
    ``item`` 里躺着一行十字段全 ``null`` 的记录，而不是 ``item: []``。若按行归一化，
    这条空记录会变成一条 ``ex_date`` 缺失的事件——合法无数据必须落成空事件流。

    Args:
        row: 上游响应行。

    Returns:
        全字段为空时为 ``True``。
    """
    return all(value is None for value in row.values())


def normalize_fund_dividends(
    envelope: FuyaoEnvelope, *, symbol: str
) -> tuple[CorporateAction, ...]:
    """把基金分红记录归一化为 ``CorporateAction``（按除息日降序）.

    口径都是 2026-09-25 实测后落地的，不照抄文档：

    * 上游给的是**每 10 份**现金分红（``per_ten_cash_before_tax``），契约要的是
      每份，故除以 10；落**税前**值，与 A 股 :func:`normalize_adjustment_factors`
      同口径，税后只在实测中与税前逐笔相等（ETF 分发），故不作为契约字段。
    * ``dividend_count`` 与 ``dividend_total`` 是上游自带的两道自校验：前者须等于
      非占位行数，后者须等于逐笔求和（元/份口径，不是每 10 份）。
    * 只有 ``progress`` 为「已实施」码的行才成为事件；未知码失败关闭。

    Args:
        envelope: 成功信封。
        symbol: 请求使用的上游基金代码。

    Returns:
        分红事件；该基金无分红时为空元组。

    Raises:
        FuyaoError: 标的为空、行缺除息日或金额、进度码未知、自带汇总与逐笔对不上。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise error_for_transport("envelope_invalid", detail="symbol")
    resolved_symbol = symbol.strip()
    declared = envelope.data.get("dividend_count")
    if declared is not None and (not isinstance(declared, int) or isinstance(declared, bool)):
        raise error_for_transport("envelope_invalid", detail="fund_dividend_count")
    events: list[CorporateAction] = []
    for row in _items(envelope, context="fund_dividend"):
        if _is_placeholder_dividend(row):
            continue
        progress = row.get("progress")
        if progress not in IMPLEMENTED_PROGRESS:
            raise error_for_transport("envelope_invalid", detail="fund_dividend_progress")
        ex_date = millis_to_trading_date(
            _require_value(row, "ex_dividend_date_ms", context="fund_dividend")
        )
        try:
            per_ten = _require_value(row, "per_ten_cash_before_tax", context="fund_dividend")
            cash = float(per_ten) / 10.0
        except (TypeError, ValueError) as exc:
            raise error_for_transport("envelope_invalid", detail="fund_dividend_fields") from exc
        if cash <= 0.0:
            raise error_for_transport("envelope_invalid", detail="fund_dividend_cash")
        events.append(CorporateAction(symbol=resolved_symbol, ex_date=ex_date, cash_dividend=cash))
    events.sort(key=lambda event: event.ex_date, reverse=True)
    if declared is not None and declared != len(events):
        raise error_for_transport("envelope_invalid", detail="fund_dividend_count_mismatch")
    total = envelope.data.get("dividend_total")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        summed = sum(event.cash_dividend for event in events)
        if abs(summed - float(total)) > DIVIDEND_TOTAL_TOLERANCE:
            raise error_for_transport("envelope_invalid", detail="fund_dividend_total_mismatch")
    return tuple(events)


def fetch_fund_dividends(client: FuyaoHttpClient, *, symbol: str) -> tuple[CorporateAction, ...]:
    """取单只基金的历史分红事件流（按除息日降序）.

    Args:
        client: 传输层客户端。
        symbol: 上游基金代码（``510300.SH``）。

    Returns:
        分红事件；无分红的基金为空元组。
    """
    response = client.get(
        FUND_DIVIDENDS_ENDPOINT, params=build_fund_dividends_request(symbol=symbol)
    )
    return normalize_fund_dividends(response.envelope, symbol=symbol)


#: ETF 日线通道的可答深度（日历天，C20 二分实测）：起点早于
#: ``today - FUND_ETF_DEPTH_DAYS`` 时上游**不报错**，而是回 ``code=0`` 的空
#: ``item``——与「这只基金从没交易过」在信封上不可区分（C11a 指数腿同一类缺陷）。
#: 地板随「今天」滚动（两种窗口长度下地板同位），故只能在发请求前拦。
FUND_ETF_DEPTH_DAYS = 1827

#: ETF 日线通道的单次跨度上限（日历天，C20 二分实测：1826 可答、1827 起报
#: ``code=1003``）。上游文案写「单次不超过 10 年」，那是股票/指数腿的上限
#: （:data:`MAX_WINDOW`），照抄会把排障引去错的腿。
FUND_ETF_MAX_SPAN_DAYS = 1826

#: 换算后价格的保留位数：ETF 最小变动价位 0.001，逐日分红求和带来的浮点噪声在
#: 1e-13 量级；取 6 位吞掉噪声而不动有效位。
_UNADJUST_DECIMALS = 6


def build_fund_etf_prices_request(
    *, symbol: str, start: date, end: date, today: date
) -> dict[str, Any]:
    """构造 ETF 日线请求参数（毫秒戳闭区间，**无** ``adjust``）.

    与股票/指数日线的两点差异都来自 C20 实测。一是 ``adjust`` 在这里被**接受
    但完全无效**（``0/1/2/forward/backward`` 与缺省回同一序列指纹，
    ``data.adjust`` 恒为 ``null``），发它只会让人以为复权语义生效了，比拒收更
    危险，故本函数不接受该参数。二是两道守卫的数值互不嵌套：可答深度 1827 天
    比单次跨度上限 1826 天还多一天，所以「取满深度又取到今天」在一次请求里
    做不到，:func:`fetch_fund_etf_bars` 按 :data:`FUND_ETF_MAX_SPAN_DAYS` 切块。

    Args:
        symbol: 上游基金代码（``510300.SH``，裸码上游回 ``code=1002``）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。
        today: 判深度用的「今天」，须是上海日期（:func:`shanghai_today`）。
            显式传入而非内部读系统时钟：本模块的请求构造函数对时钟保持纯净，
            同一组入参必须永远产出同一份参数。

    Returns:
        查询参数。

    Raises:
        FuyaoError: 标的为空/裸码、窗口空或反向、跨度超
            :data:`FUND_ETF_MAX_SPAN_DAYS`、起点早于服务端深度地板。
    """
    resolved_symbol = _qualified_fund_symbol(symbol)
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    if (end - start).days > FUND_ETF_MAX_SPAN_DAYS:
        raise error_for_transport("envelope_invalid", detail="fund_window_span")
    if start < today - timedelta(days=FUND_ETF_DEPTH_DAYS):
        raise error_for_transport("envelope_invalid", detail="fund_window_depth")
    return {
        "thscode": resolved_symbol,
        "interval": "1d",
        "start": shanghai_midnight_millis(start),
        "end": shanghai_midnight_millis(end) - 1,
    }


def _split_fund_etf_window(start: date, end: date, *, today: date) -> tuple[tuple[date, date], ...]:
    """把 ``[start, end)`` 按 ETF 通道的跨度上限切块，并先拦掉早于地板的起点.

    深度只在首块上判一次即够：块是自起点向后连续排布的，后续块的起点只会更晚。
    越深的数据切块也拿不到（地板之前上游根本不作答），故这里不是「分批取全量」，
    只是让取满可答范围成为可能。

    Args:
        start: 起始交易日（含）。
        end: 结束交易日（不含）。
        today: 判深度用的「今天」（上海日期）。

    Returns:
        连续子窗口（同样半开，每块跨度 ≤ :data:`FUND_ETF_MAX_SPAN_DAYS`）。

    Raises:
        FuyaoError: 窗口空或反向、起点早于服务端深度地板。
    """
    if start >= end:
        raise error_for_transport("envelope_invalid", detail="window")
    if start < today - timedelta(days=FUND_ETF_DEPTH_DAYS):
        raise error_for_transport("envelope_invalid", detail="fund_window_depth")
    max_span = timedelta(days=FUND_ETF_MAX_SPAN_DAYS)
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + max_span, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return tuple(chunks)


def unadjust_bars(bars: Iterable[Bar], events: Iterable[CorporateAction]) -> tuple[Bar, ...]:
    """把上游的前复权 ETF 日线换算成不复权（**只动价格，不动量额**）.

    实测口径（C15 采样、C20 四只基金各 2,652 格、合计 10,608 格逐格判）：
    ``前复权价 = 不复权价 − 严格晚于该日的每份现金分红之和``。除息日**当天**已经
    是复权后的价格，所以边界是「之后」而非「不早于」——C20 在两只多年分红的基金
    上于 5 个除息日观测到两种口径的唯一分歧，「其后」逐格相等，「不早于」恰好在
    除息日上错。无分红的基金换算退化为恒等（判据 B′ 要求这条真被观测到）。

    「量额不参与复权」不靠断言，靠自证（判据 C'/C''）：``额/量`` 是当天真实成交
    均价，换算后必然落进当日不复权高低价区间（实测四只各 663 根、合计 2652/2652
    命中），而拿发布价判只在最后除息日之后的新行上成立。上游量的单位已是「份」（对新浪
    逐根相对差 ≤3.6e-08、对腾讯标「手」列 ×100 逐根命中），故这里不做单位换算。

    Args:
        bars: 前复权日线（:func:`normalize_bars` 的产物）。
        events: 分红事件流（:func:`normalize_fund_dividends` 的产物）。

    Returns:
        不复权日线，顺序与入参一致。

    Raises:
        FuyaoError: 分红流里出现 ``bars`` 之外的标的（会静默污染价格），或换算后
            价格不为正——分红只会把价格往上加，所以这只能是发布价本身就是
            0/负数的脏行，而它一旦过了换算就再也看不出问题。
    """
    collected = list(bars)
    dividend_map: dict[str, list[CorporateAction]] = {}
    for event in events:
        dividend_map.setdefault(event.symbol, []).append(event)
    unknown = sorted(set(dividend_map) - {bar.symbol for bar in collected})
    if unknown:
        raise error_for_transport("envelope_invalid", detail="fund_unadjust_symbol")
    unadjusted: list[Bar] = []
    for bar in collected:
        tail = sum(
            event.cash_dividend
            for event in dividend_map.get(bar.symbol, ())
            if event.ex_date > bar.trade_date
        )
        updates = {
            field: round(getattr(bar, field) + tail, _UNADJUST_DECIMALS)
            for field in ("open", "high", "low", "close")
        }
        if min(updates.values()) <= 0.0:
            raise error_for_transport("envelope_invalid", detail="fund_unadjust_price")
        unadjusted.append(bar.model_copy(update=updates))
    return tuple(unadjusted)


def fetch_fund_etf_bars(
    client: FuyaoHttpClient,
    *,
    symbol: str,
    start: date,
    end: date,
    today: date,
) -> tuple[Bar, ...]:
    """取 ETF 日线并换回不复权口径（分红腿先走，取不到就不发价格请求）.

    顺序是有意的：这条端点只发前复权序列，而 ``adjust`` 是接受但无效的，没有
    分红流就无法换算，而「拿前复权序列冒充不复权入库」正是 D10 明令禁止的那类
    错误——它看起来完全正常，只在除息日附近悄悄改变收益率。故分红取失败时直接
    抛出，不退化成恒等。分红也只取一次：它是「自成立至今」的全量事件流，与价格
    窗口无关，逐块重取既多发请求又可能跨块取到不一致的两份。

    Args:
        client: 传输层客户端。
        symbol: 上游基金代码（``510300.SH``）。
        start: 起始交易日（含）。
        end: 结束交易日（不含）。
        today: 判深度用的「今天」（上海日期，见 :func:`shanghai_today`）。

    Returns:
        按交易日升序的不复权 ``Bar``。
    """
    chunks = _split_fund_etf_window(start, end, today=today)
    events = fetch_fund_dividends(client, symbol=symbol)
    bars: list[Bar] = []
    for chunk_start, chunk_end in chunks:
        response = client.get(
            FUND_ETF_PRICES_ENDPOINT,
            params=build_fund_etf_prices_request(
                symbol=symbol, start=chunk_start, end=chunk_end, today=today
            ),
        )
        bars.extend(normalize_bars(response.envelope, symbol=symbol))
    return unadjust_bars(bars, events)


__all__ = [
    "ADJUSTMENTS",
    "ADJUSTMENT_FACTORS_ENDPOINT",
    "BALANCE_SHEETS_ENDPOINT",
    "CALENDAR_ENDPOINT",
    "CASH_FLOW_STATEMENTS_ENDPOINT",
    "DAILY_TIME_PERIOD",
    "DISPLAY_CODE_ASSET_TYPES",
    "DIVIDEND_TOTAL_TOLERANCE",
    "FINANCIAL_PERIODS",
    "FINANCIAL_PERIOD_END",
    "FINANCIAL_STATEMENT_ENDPOINTS",
    "FINANCIAL_STATEMENT_ITEMS",
    "FINANCIAL_STATEMENT_MAX_LIMIT",
    "FUND_ASSET_TYPE",
    "FUND_DIVIDENDS_ENDPOINT",
    "FUND_ETF_DEPTH_DAYS",
    "FUND_ETF_MAX_SPAN_DAYS",
    "FUND_ETF_PRICES_ENDPOINT",
    "FUTURES_ASSET_TYPE",
    "FUTURES_PRICES_ENDPOINT",
    "INCOME_STATEMENTS_ENDPOINT",
    "INDEX_ASSET_TYPE",
    "INDEX_CONSTITUENTS_ENDPOINT",
    "INDEX_PRICES_ENDPOINT",
    "IMPLEMENTED_PROGRESS",
    "MAX_LIST_LIMIT",
    "MAX_SEARCH_LIMIT",
    "MAX_WINDOW",
    "OPTIONS_ASSET_TYPE",
    "OPTIONS_PRICES_ENDPOINT",
    "OPAQUE_CODE_ASSET_TYPES",
    "PERIOD_BAR_DATE_KEY",
    "PRICES_ENDPOINT",
    "SYNTHETIC_SERIES_TICKER_TAILS",
    "TICKERS_LIST_ENDPOINT",
    "TICKERS_SEARCH_ENDPOINT",
    "build_adjustment_factors_request",
    "build_financial_statements_request",
    "build_fund_dividends_request",
    "build_fund_etf_prices_request",
    "build_index_constituents_request",
    "build_index_prices_request",
    "build_period_daily_request",
    "build_prices_request",
    "build_tickers_list_request",
    "build_tickers_search_request",
    "envelope_snapshot",
    "fetch_adjustment_factors",
    "fetch_daily_bars",
    "fetch_financial_statements",
    "fetch_fund_dividends",
    "fetch_fund_etf_bars",
    "fetch_index_constituents",
    "fetch_index_daily_bars",
    "fetch_period_daily_bars",
    "fetch_trading_calendar",
    "list_instruments",
    "millis_to_trading_date",
    "normalize_adjustment_factors",
    "normalize_bars",
    "normalize_calendar",
    "normalize_financial_statements",
    "normalize_fund_dividends",
    "normalize_index_constituents",
    "normalize_instruments",
    "search_instruments",
    "shanghai_midnight_millis",
    "shanghai_today",
    "unadjust_bars",
]
