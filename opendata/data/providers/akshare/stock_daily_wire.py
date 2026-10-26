"""Strict Eastmoney wire reader for unadjusted A-share daily klines."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn

import pandas as pd

from opendata.data.http_client import get_shared_http_client
from opendata.data.providers.akshare._source import SOURCE

if TYPE_CHECKING:
    from collections.abc import Sequence

EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
MAX_RAW_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_KLINE_ROWS = 10_000

_KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_KLINE_COLUMNS = (
    "日期",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
)
_FRAME_COLUMNS = (*_KLINE_COLUMNS, "股票代码")


def fetch_stock_daily_wire(
    symbol: str,
    *,
    start_date: str,
    end_date: str,
    timeout: float | None,
) -> pd.DataFrame:
    """Read fixed Eastmoney daily raw prices and verify their wire identity.

    The governed client performs request authorization, attempt accounting,
    deadline handling, and scoped rate limiting. Its current ``get`` API
    returns a fully buffered response; the byte check in the parser is a
    post-buffer parsing limit, not an ingress or streaming memory bound.

    Args:
        symbol: Canonical six-digit A-share code already validated by the query.
        start_date: Inclusive query start in Eastmoney's YYYYMMDD representation.
        end_date: Inclusive query end in Eastmoney's YYYYMMDD representation.
        timeout: Fetch context timeout, when supplied.

    Returns:
        A source-native frame with the verified response code attached.

    Raises:
        ValueError: If the transport response or Eastmoney wire identity is invalid.
    """
    market = _market_for_symbol(symbol)
    request_params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": _KLINE_FIELDS,
        "klt": "101",
        "fqt": "0",
        "secid": f"{market}.{symbol}",
        "beg": start_date,
        "end": end_date,
    }
    response = get_shared_http_client().get(
        EASTMONEY_KLINE_URL,
        params=request_params,
        timeout=timeout,
        source=SOURCE,
    )
    if response.status_code != 200:
        raise ValueError(f"Eastmoney stock history returned HTTP {response.status_code}")
    return parse_stock_daily_wire(response.content, symbol=symbol, expected_market=market)


def parse_stock_daily_wire(
    content: bytes,
    *,
    symbol: str,
    expected_market: int,
) -> pd.DataFrame:
    """Parse bounded raw JSON and reject absent or mismatched source identity.

    Args:
        content: Fully buffered response content from the governed client.
        symbol: Requested canonical six-digit A-share code.
        expected_market: Eastmoney market identity encoded by the request secid.

    Returns:
        A frame made from the original kline strings, before normalization.

    Raises:
        ValueError: If the body, envelope, identity, or row shape is invalid.
    """
    if not isinstance(content, bytes):
        raise ValueError("Eastmoney stock history response content must be bytes")
    if len(content) > MAX_RAW_RESPONSE_BYTES:
        raise ValueError("Eastmoney stock history response exceeds the post-buffer byte limit")
    try:
        payload = json.loads(
            content,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("Eastmoney stock history response is not strict JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Eastmoney stock history response must be a JSON object")

    status = payload.get("rc")
    if type(status) is not int or status != 0:
        raise ValueError("Eastmoney stock history response has a missing or non-success rc")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Eastmoney stock history response is missing its data object")

    response_code = data.get("code")
    if not isinstance(response_code, str) or response_code != symbol:
        raise ValueError("Eastmoney response code does not exactly match the requested symbol")
    response_market = data.get("market")
    if type(response_market) is not int or response_market != expected_market:
        raise ValueError("Eastmoney response market does not match the requested secid")

    klines = data.get("klines")
    if not isinstance(klines, list):
        raise ValueError("Eastmoney stock history response is missing its klines list")
    if len(klines) > MAX_KLINE_ROWS:
        raise ValueError("Eastmoney stock history response exceeds the row limit")

    rows: list[list[str]] = []
    for row_number, kline in enumerate(klines, start=1):
        if not isinstance(kline, str):
            raise ValueError(f"Eastmoney kline row {row_number} must be a string")
        fields = kline.split(",")
        if len(fields) != len(_KLINE_COLUMNS):
            raise ValueError(f"Eastmoney kline row {row_number} has an invalid field count")
        rows.append(fields)

    frame = pd.DataFrame(rows, columns=_KLINE_COLUMNS)
    frame["股票代码"] = response_code
    return frame.loc[:, list(_FRAME_COLUMNS)]


def _market_for_symbol(symbol: str) -> int:
    """Return the fixed Eastmoney secid market for an already validated code."""
    return 1 if symbol.startswith("6") else 0


def _object_without_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    """Reject JavaScript-style NaN and Infinity tokens accepted by Python JSON."""
    raise ValueError(f"non-finite JSON constant {value}")
