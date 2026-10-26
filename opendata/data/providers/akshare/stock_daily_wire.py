"""Strict Eastmoney wire reader for unadjusted A-share daily klines."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NoReturn

import pandas as pd

from opendata.data.http_client import BoundedHttpResponse, get_shared_http_client
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_KLINE_HOST = "push2his.eastmoney.com"
MAX_RAW_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_KLINE_ROWS = 10_000
MAX_STOCK_DAILY_WINDOW_DAYS = 3_660
STOCK_DAILY_PROCESS_OPERATION_ID = "stock-daily-wire"
STOCK_DAILY_HTTP_OVERALL_TIMEOUT_SECONDS = 20.0
STOCK_DAILY_HTTP_CONNECT_TIMEOUT_SECONDS = 3.0
STOCK_DAILY_HTTP_READ_TIMEOUT_SECONDS = 5.0
STOCK_DAILY_PROCESS_TIMEOUT_SECONDS = 30.0
STOCK_DAILY_PROCESS_STARTUP_MARGIN_SECONDS = 2.0
MAX_BASE64_RESPONSE_BYTES = 4 * ((MAX_RAW_RESPONSE_BYTES + 2) // 3)
_PROCESS_RESULT_ENVELOPE_BYTES = len(
    json.dumps(
        {
            "body_b64": "",
            "retrieved_at": "9999-12-31T23:59:59.999999Z",
            "status_code": 200,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
)
MAX_PROCESS_RESULT_JSON_BYTES = _PROCESS_RESULT_ENVELOPE_BYTES + MAX_BASE64_RESPONSE_BYTES
MAX_STOCK_DAILY_PROCESS_PARAMETER_BYTES = len(
    json.dumps(
        {
            "symbol": "999999",
            "start_date": "99991231",
            "end_date": "99991231",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
)
_PROCESS_SYMBOL = re.compile(r"^[0-9]{6}$")
_PROCESS_DATE = re.compile(r"^[0-9]{8}$")

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


def fetch_stock_daily_process(parameters: Mapping[str, object]) -> dict[str, str | int]:
    """Fetch one fixed Eastmoney response for the registered child operation.

    This handler accepts only a canonical symbol and an inclusive date window.
    It installs a new child-local request budget and scope; the current rights
    declaration is deliberately UNKNOWN, so the governed client must reject the
    request before transport. The body is returned only through the executor's
    bounded JSON result channel and is never printed or logged.

    Args:
        parameters: Exact JSON object with ``symbol``, ``start_date`` and ``end_date``.

    Returns:
        The exact bounded process envelope for a successful HTTP 200 response.

    Raises:
        ValueError: If the request, authority, HTTP response, or response time is invalid.
    """
    symbol, start_date, end_date = _validate_process_parameters(parameters)
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
    budget = _new_child_request_budget()
    with request_execution_scope(
        source=SOURCE,
        canonical_model="stock_daily",
        operation=RequestOperation.QUERY,
        budget=budget,
        # Process workers must establish their own authority, never reuse an
        # ambient ContextVar stack when this handler is exercised in-process.
        inherited_scopes=(),
    ):
        response = get_shared_http_client().get_bounded(
            EASTMONEY_KLINE_URL,
            params=request_params,
            max_body_bytes=MAX_RAW_RESPONSE_BYTES,
            overall_timeout=STOCK_DAILY_HTTP_OVERALL_TIMEOUT_SECONDS,
            timeout=(
                STOCK_DAILY_HTTP_CONNECT_TIMEOUT_SECONDS,
                STOCK_DAILY_HTTP_READ_TIMEOUT_SECONDS,
            ),
            source=SOURCE,
        )
    if type(response) is not BoundedHttpResponse:
        raise ValueError("stock_daily_process_response_invalid")
    if type(response.status_code) is not int or response.status_code != 200:
        raise ValueError("stock_daily_process_http_status_invalid")
    if type(response.body) is not bytes or len(response.body) > MAX_RAW_RESPONSE_BYTES:
        raise ValueError("stock_daily_process_body_limit_exceeded")
    retrieved_at = _aware_utc_timestamp(response.retrieved_at)
    return {
        "body_b64": base64.b64encode(response.body).decode("ascii"),
        "status_code": response.status_code,
        "retrieved_at": retrieved_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    }


def _validate_process_parameters(parameters: Mapping[str, object]) -> tuple[str, str, str]:
    """Validate the exact child IPC query without accepting transport controls."""
    if type(parameters) is not dict or set(parameters) != {"symbol", "start_date", "end_date"}:
        raise ValueError("stock_daily_process_parameters_invalid")
    symbol = parameters["symbol"]
    start_date = parameters["start_date"]
    end_date = parameters["end_date"]
    if (
        type(symbol) is not str
        or _PROCESS_SYMBOL.fullmatch(symbol) is None
        or type(start_date) is not str
        or _PROCESS_DATE.fullmatch(start_date) is None
        or type(end_date) is not str
        or _PROCESS_DATE.fullmatch(end_date) is None
    ):
        raise ValueError("stock_daily_process_query_invalid")
    try:
        parsed_start = datetime.strptime(start_date, "%Y%m%d").date()
        parsed_end = datetime.strptime(end_date, "%Y%m%d").date()
    except ValueError:
        raise ValueError("stock_daily_process_query_invalid") from None
    if (
        parsed_start.strftime("%Y%m%d") != start_date
        or parsed_end.strftime("%Y%m%d") != end_date
        or parsed_start > parsed_end
        or (parsed_end - parsed_start).days + 1 > MAX_STOCK_DAILY_WINDOW_DAYS
    ):
        raise ValueError("stock_daily_process_query_invalid")
    try:
        from opendata.data.providers.akshare.models.stock_daily import StockDailyQuery

        query = StockDailyQuery.model_validate(
            {
                "source": SOURCE,
                "market": "cn",
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "adjust": "",
            }
        )
    except (TypeError, ValueError):
        raise ValueError("stock_daily_process_query_invalid") from None
    if query.symbol != symbol:
        raise ValueError("stock_daily_process_query_invalid")
    return symbol, start_date, end_date


def _new_child_request_budget() -> RequestBudget:
    """Create a fresh one-attempt child budget with unresolved source rights."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    unknown_grant = RequestGrant(
        source=SOURCE,
        canonical_model="stock_daily",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.UNKNOWN,
        rights_evidence="Eastmoney rights, retention, and history evidence unresolved",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={EASTMONEY_KLINE_HOST},
        expires_at=datetime.max.replace(tzinfo=timezone.utc),
        conditions=("rights review required before transport",),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(unknown_grant,),
        profile=profile,
    )


def _aware_utc_timestamp(value: object) -> datetime:
    """Validate and normalize one aware retrieval timestamp to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stock_daily_process_retrieved_at_invalid")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("stock_daily_process_retrieved_at_invalid") from None


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
