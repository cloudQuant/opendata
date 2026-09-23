"""fuyao P0 端点：日线、除复权事件、标的、交易日历（A3.2）.

每个端点都是"请求构造 + 响应归一化"两段，直接产出中台契约模型
（``Bar`` / ``CorporateAction`` / ``Instrument`` / ``TradingCalendar``），
供 A3.4 的 provider 层直接注册路由。

时间语义（同生态 API 事实，A3.2 实测确认）：
- 请求窗口 ``start`` / ``end`` 为**毫秒戳**，闭区间，跨度 ≤ 10 年（超过 code=1003）；
  平台内部为半开窗口，故 ``end`` 取 ``end_date`` 上海零点 - 1ms。
- 响应 ``date_ms`` 为交易日的 ``Asia/Shanghai`` 零点，归一化为该交易日 date。
- 复权请求值 ``none|forward|backward``，中台语义 ``unadjusted|qfq|hfq``。

D10：``Bar`` 只存不复权价，复权序列由本地因子合成；入库路径不应持久化
``adjust != unadjusted`` 的结果。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from opendata.data.models import Bar, CorporateAction, Instrument, TradingCalendar
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
ADJUSTMENT_FACTORS_ENDPOINT = "/api/a-share/corporate-actions/adjustment-factors"
TICKERS_LIST_ENDPOINT = "/api/meta/tickers/list"
TICKERS_SEARCH_ENDPOINT = "/api/meta/tickers/search"
CALENDAR_ENDPOINT = "/api/a-share/calendar/trading-days"

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


def normalize_bars(envelope: FuyaoEnvelope, *, symbol: str) -> tuple[Bar, ...]:
    """把日线响应归一化为 ``Bar``（按交易日升序）.

    上游行内**不含** ``thscode``（只在 ``data.thscode`` 回显），故标的自请求
    参数显式传入并校验——契约要求 ``Bar.symbol`` 非空，空值会写坏 ods 主键。

    Args:
        envelope: 成功信封。
        symbol: 请求使用的上游标的代码。

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
        trade_date = millis_to_trading_date(_require_value(row, "date_ms", context="bar"))
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


def normalize_instruments(envelope: FuyaoEnvelope) -> tuple[Instrument, ...]:
    """把标的列表/检索归一化为 ``Instrument``.

    ``status`` 由 ``end_date`` 推导（无退市日 = ``active``），``list_date`` /
    ``delist_date`` 直接取上游日期；``name`` 可能为空字符串。

    Args:
        envelope: 成功信封。

    Returns:
        标的信息。

    Raises:
        FuyaoError: 行缺 ``thscode``。
    """
    instruments: list[Instrument] = []
    for row in _items(envelope, context="ticker"):
        symbol = _require_value(row, "thscode", context="ticker")
        if not isinstance(symbol, str) or not symbol.strip():
            raise error_for_transport("envelope_invalid", detail="ticker_thscode")
        exchange = row.get("exchange") or row.get("asset_type") or ""
        delist = _parse_optional_date(row.get("end_date"))
        instruments.append(
            Instrument(
                symbol=symbol.strip(),
                exchange=str(exchange),
                name=str(row.get("name") or ""),
                status="active" if delist is None else "delisted",
                currency=str(row.get("currency") or "CNY"),
                list_date=_parse_optional_date(row.get("list_date")),
                delist_date=delist,
            )
        )
    return tuple(instruments)


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
    """取 A 股交易日历（上游一次返回整段，无窗口参数）.

    Args:
        client: 传输层客户端。
        exchange: 交易所标识（显式给出，上游不区分交易所）。

    Returns:
        交易日历行。
    """
    response = client.get(CALENDAR_ENDPOINT, params={})
    return normalize_calendar(response.envelope, exchange=exchange)


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


__all__ = [
    "ADJUSTMENTS",
    "ADJUSTMENT_FACTORS_ENDPOINT",
    "CALENDAR_ENDPOINT",
    "MAX_LIST_LIMIT",
    "MAX_SEARCH_LIMIT",
    "MAX_WINDOW",
    "PRICES_ENDPOINT",
    "TICKERS_LIST_ENDPOINT",
    "TICKERS_SEARCH_ENDPOINT",
    "build_adjustment_factors_request",
    "build_prices_request",
    "build_tickers_list_request",
    "build_tickers_search_request",
    "fetch_adjustment_factors",
    "fetch_daily_bars",
    "fetch_trading_calendar",
    "list_instruments",
    "millis_to_trading_date",
    "normalize_adjustment_factors",
    "normalize_bars",
    "normalize_calendar",
    "normalize_instruments",
    "search_instruments",
    "shanghai_midnight_millis",
]
