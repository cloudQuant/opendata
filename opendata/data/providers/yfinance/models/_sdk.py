"""yfinance SDK access point (C1 P0, FR-7 optional-dependency rule).

The SDK is an **optional** dependency: this module is the only place that
imports it, lazily, so environments without it can still import and register
the provider package. A missing SDK fails closed with a stable code instead
of an ImportError (the routing layer then skips the capability).

Both seams translate the contract's dates into the upstream's conventions;
the measured behaviours that force the translation are documented on each
function.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sized
from contextvars import ContextVar, Token
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

import pandas as pd

from opendata.data.providers.yfinance.models._session import (
    _ensure_yfdata_session,
    governed_yfinance_session,
)
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from datetime import date

_ACTIVE_SDK: ContextVar[tuple[Any, Any | None] | None] = ContextVar(
    "yfinance_active_sdk_call", default=None
)
_SdkArgs = ParamSpec("_SdkArgs")
_SdkResult = TypeVar("_SdkResult")


def _governed_sdk_call(
    function: Callable[_SdkArgs, _SdkResult],
) -> Callable[_SdkArgs, _SdkResult]:
    """Hold the source session lock across construction and all SDK property I/O."""

    @wraps(function)
    def wrapped(*args: _SdkArgs.args, **kwargs: _SdkArgs.kwargs) -> _SdkResult:
        if _ACTIVE_SDK.get() is not None:
            return function(*args, **kwargs)
        sdk = _require_sdk()
        with governed_yfinance_session(sdk) as session:
            token: Token[tuple[Any, Any | None] | None] = _ACTIVE_SDK.set((sdk, session))
            try:
                return function(*args, **kwargs)
            finally:
                _ACTIVE_SDK.reset(token)

    return wrapped


def _ticker(yfinance: Any, symbol: str) -> Any:  # noqa: ANN401
    active = _ACTIVE_SDK.get()
    session = active[1] if active is not None else None
    normalized_symbol = symbol.strip()
    if session is None:
        return yfinance.Ticker(normalized_symbol)
    ticker = yfinance.Ticker(normalized_symbol, session=session)
    _ensure_yfdata_session(yfinance, session, initialize=False)
    return ticker


@_governed_sdk_call
def require_history_frame(
    symbol: str,
    *,
    start: date | None,
    end: date | None,
    timeout: float | None,
    interval: str = "1d",
    prepost: bool = False,
    actions: bool = True,
    auto_adjust: bool = False,
) -> pd.DataFrame | None:
    """Fetch one symbol's requested interval history through the SDK.

    Three measured upstream behaviours shape this call:

    * ``auto_adjust=False`` is the whole point of the seam. The SDK's default
      folds **dividend** adjustment into ``Open``/``High``/``Low``/``Close``
      (measured against sina's as-traded close over 2026-08-03 .. 2026-09-22:
      KO ``5.93e-03``, MSFT ``1.88e-03``, AAPL ``8.62e-04``, while 0700.HK -
      no dividend in that window - is unaffected), which D10 forbids in
      storage. In raw mode the frame carries the split-adjusted ``Close``
      plus ``Adj Close``, ``Dividends`` and ``Stock Splits``.
      Recompute: ``python scripts/ops/yfinance_overseas_cross_check.py
      --counterfactual``.
    * ``end`` is **exclusive**, while every contract query date is inclusive,
      so the seam shifts it by a day (otherwise the last session of every
      window is silently dropped).
    * While a session is still open the SDK returns it as a bar with ``NaN``
      prices; the normalizer drops those rows.

    Args:
        symbol: Upstream ticker (for example ``AAPL``).
        start: Inclusive start date; ``None`` lets the SDK default apply.
        end: Inclusive end date; ``None`` lets the SDK default apply.
        timeout: Optional request timeout override.
        interval: Documented yfinance history interval.
        prepost: Whether to include pre-market and post-market bars.
        actions: Whether to include dividends and split events.
        auto_adjust: Whether to adjust OHLC values for dividends and splits.

    Returns:
        The SDK's history frame, or ``None`` when the SDK returns nothing.

    Raises:
        YfinanceProviderError: The SDK is not installed (stable code, so the
            caller can treat the capability as unavailable).
    """
    yfinance = _require_sdk()
    history_kwargs: dict[str, object] = {
        "interval": interval,
        "prepost": prepost,
        "actions": actions,
        "auto_adjust": auto_adjust,
    }
    if start is not None:
        history_kwargs["start"] = start.isoformat()
    if end is not None:
        history_kwargs["end"] = (end + timedelta(days=1)).isoformat()
    if timeout is not None:
        history_kwargs["timeout"] = timeout
    history: object = _ticker(yfinance, symbol).history(**history_kwargs)
    if history is None:
        return None
    if not isinstance(history, pd.DataFrame):
        raise YfinanceProviderError("YFINANCE_HISTORY_SHAPE_INVALID")
    return history


@_governed_sdk_call
def require_split_history(symbol: str) -> pd.Series:
    """Fetch one symbol's **complete** split history through the SDK.

    The full list, not the window's rows: the upstream de-visions a price by
    every later split regardless of the requested range (measured: AAPL
    2020-07-14 arrives as 97.0575 although the session traded 388.23, and the
    4:1 event sits outside that window).

    Args:
        symbol: Upstream ticker.

    Returns:
        Split ratios keyed by their session date; empty when the symbol never
        split.

    Raises:
        YfinanceProviderError: The SDK is not installed, or the request failed
            (an empty result would be indistinguishable from "no splits", so
            a transport error must not be read as "nothing to undo").
    """
    yfinance = _require_sdk()
    try:
        splits: object = _ticker(yfinance, symbol).splits
    except RequestBudgetError:
        raise
    except Exception as exc:  # the SDK re-raises transport errors as requests' own types
        raise YfinanceProviderError("YFINANCE_SPLITS_UNAVAILABLE") from exc
    if splits is None:
        return pd.Series(dtype="float64")
    if not isinstance(splits, Sized):
        raise YfinanceProviderError("YFINANCE_SPLITS_SHAPE_INVALID")
    if len(splits) == 0:
        return pd.Series(dtype="float64")
    if isinstance(splits, pd.DataFrame):
        column = "Stock Splits" if "Stock Splits" in splits.columns else splits.columns[0]
        splits = splits[column]
    converted: object = pd.Series(splits).astype("float64")
    if not isinstance(converted, pd.Series):
        raise YfinanceProviderError("YFINANCE_SPLITS_SHAPE_INVALID")
    return converted


@_governed_sdk_call
def require_statement_frame(
    symbol: str,
    statement_type: str,
    *,
    frequency: str,
) -> pd.DataFrame | None:
    """Fetch one source-native financial statement frame.

    yfinance exposes three distinct ``Ticker.get_*`` methods. Their public signatures
    accept ``as_dict``, ``pretty``, and ``freq``; this seam requests the unformatted
    DataFrame so the adapter can retain the provider's original line-item names.
    The SDK methods do not expose a per-call timeout parameter.
    """
    method_names = {
        "balance_sheet": "get_balance_sheet",
        "cash_flow_statement": "get_cashflow",
        "income_statement": "get_income_stmt",
    }
    method_name = method_names.get(statement_type)
    if method_name is None:
        raise YfinanceProviderError("YFINANCE_BAD_STATEMENT_TYPE")

    yfinance = _require_sdk()
    try:
        ticker = _ticker(yfinance, symbol)
        statement = getattr(ticker, method_name)(as_dict=False, pretty=False, freq=frequency)
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if statement is None:
        return None
    if not isinstance(statement, pd.DataFrame):
        raise YfinanceProviderError("YFINANCE_STATEMENT_SHAPE_INVALID")
    return statement


@_governed_sdk_call
def require_dividends_series(symbol: str) -> pd.Series | None:
    """Fetch the Ticker's historical dividend Series without substituting a snapshot."""
    yfinance = _require_sdk()
    try:
        dividends = _ticker(yfinance, symbol).dividends
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if dividends is None:
        return None
    if not isinstance(dividends, pd.Series):
        raise YfinanceProviderError("YFINANCE_DIVIDENDS_SHAPE_INVALID")
    return dividends


@_governed_sdk_call
def require_history_snapshot(
    symbol: str,
    start: date | None,
    end: date | None,
    *,
    timeout: float | None,
    interval: str = "1d",
    prepost: bool = False,
    actions: bool = True,
    auto_adjust: bool = False,
) -> pd.DataFrame | None:
    """Fetch one bounded ticker history window using documented history options."""
    try:
        return require_history_frame(
            symbol,
            start=start,
            end=end,
            timeout=timeout,
            interval=interval,
            prepost=prepost,
            actions=actions,
            auto_adjust=auto_adjust,
        )
    except YfinanceProviderError:
        raise
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc


@_governed_sdk_call
def require_news(symbol: str, *, count: int) -> list[dict[str, object]]:
    """Fetch the documented Ticker.get_news result list."""
    yfinance = _require_sdk()
    try:
        response = _ticker(yfinance, symbol).get_news(count=count, tab="news")
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
        raise YfinanceProviderError("YFINANCE_NEWS_SHAPE_INVALID")
    return cast("list[dict[str, object]]", response)


@_governed_sdk_call
def require_info(symbol: str) -> dict[str, object]:
    """Fetch the ticker's current ``get_info`` mapping."""
    yfinance = _require_sdk()
    try:
        response = _ticker(yfinance, symbol).get_info()
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not isinstance(response, Mapping):
        raise YfinanceProviderError("YFINANCE_INFO_SHAPE_INVALID")
    result = dict(response)
    if not result:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    return cast("dict[str, object]", result)


@_governed_sdk_call
def require_fast_info(symbol: str) -> dict[str, object]:
    """Fetch yfinance's fast quote mapping without substituting history bars."""
    yfinance = _require_sdk()
    try:
        response = _ticker(yfinance, symbol).fast_info
        result = dict(response)
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not result:
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    return cast("dict[str, object]", result)


@_governed_sdk_call
def require_fund_sections(symbol: str) -> dict[str, object]:
    """Read documented FundsData properties for ETF holdings and metadata."""
    yfinance = _require_sdk()
    try:
        funds = _ticker(yfinance, symbol).funds_data
        result = {
            name: getattr(funds, name)
            for name in (
                "asset_classes",
                "description",
                "fund_operations",
                "fund_overview",
                "equity_holdings",
                "top_holdings",
                "sector_weightings",
                "bond_holdings",
                "bond_ratings",
            )
        }
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if all(value is None for value in result.values()):
        raise YfinanceProviderError("YFINANCE_EMPTY_RESPONSE")
    return result


@_governed_sdk_call
def require_screen(
    query: str | dict[str, object],
    *,
    offset: int,
    limit: int,
    predefined: bool,
    sort_field: str | None = None,
    sort_ascending: bool | None = None,
) -> dict[str, object]:
    """Execute the documented predefined or custom equity screener query."""
    yfinance = _require_sdk()
    sdk_query: object = query
    if isinstance(query, dict):
        query_type = getattr(yfinance, "EquityQuery", None)
        if query_type is None:
            raise YfinanceProviderError("YFINANCE_SCREEN_QUERY_UNAVAILABLE")

        def build(node: dict[str, object]) -> object:
            operator = node["operator"]
            operands = node["operands"]
            if not isinstance(operator, str) or not isinstance(operands, list):
                raise YfinanceProviderError("YFINANCE_SCREEN_QUERY_INVALID")
            sdk_operands = [
                build(value) if isinstance(value, dict) else value for value in operands
            ]
            return query_type(operator, sdk_operands)

        try:
            sdk_query = build(query)
        except Exception as exc:
            raise YfinanceProviderError("YFINANCE_SCREEN_QUERY_INVALID") from exc
    try:
        active = _ACTIVE_SDK.get()
        session = active[1] if active is not None else None
        session_kwargs = {"session": session} if session is not None else {}
        if predefined:
            response = yfinance.screen(
                sdk_query,
                offset=offset,
                count=limit,
                sortField=sort_field,
                sortAsc=sort_ascending,
                **session_kwargs,
            )
        else:
            response = yfinance.screen(
                sdk_query,
                offset=offset,
                size=limit,
                sortField=sort_field,
                sortAsc=sort_ascending,
                **session_kwargs,
            )
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not isinstance(response, Mapping) or not isinstance(response.get("quotes"), list):
        raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
    if any(not isinstance(item, Mapping) for item in response["quotes"]):
        raise YfinanceProviderError("YFINANCE_SCREEN_SHAPE_INVALID")
    return cast("dict[str, object]", dict(response))


@_governed_sdk_call
def require_valuation_measures(symbol: str, *, frequency: str, periods: int) -> pd.DataFrame | None:
    """Fetch documented current and historical valuation measures."""
    yfinance = _require_sdk()
    try:
        response = _ticker(yfinance, symbol).get_valuation_measures(freq=frequency, periods=periods)
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if response is None:
        return None
    if not isinstance(response, pd.DataFrame):
        raise YfinanceProviderError("YFINANCE_METRICS_SHAPE_INVALID")
    return response


@_governed_sdk_call
def require_options_chain(symbol: str, expiration: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fetch one explicitly requested expiration's call and put frames."""
    yfinance = _require_sdk()
    try:
        chain = _ticker(yfinance, symbol).option_chain(expiration)
        calls: object = chain.calls
        puts: object = chain.puts
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not isinstance(calls, pd.DataFrame) or not isinstance(puts, pd.DataFrame):
        raise YfinanceProviderError("YFINANCE_OPTIONS_SHAPE_INVALID")
    return calls, puts


@_governed_sdk_call
def require_price_targets(symbol: str) -> dict[str, object]:
    """Fetch analyst target summary from the documented Ticker method."""
    yfinance = _require_sdk()
    try:
        response = _ticker(yfinance, symbol).get_analyst_price_targets()
    except RequestBudgetError:
        raise
    except Exception as exc:
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc
    if not isinstance(response, Mapping):
        raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SHAPE_INVALID")
    required = {"current", "low", "high", "mean", "median"}
    if not required.issubset(response):
        raise YfinanceProviderError("YFINANCE_PRICE_TARGET_SHAPE_INVALID")
    return cast("dict[str, object]", dict(response))


def _require_sdk() -> Any:  # noqa: ANN401  # untyped optional SDK
    """Import the optional SDK or fail closed with the stable code.

    ``Any`` rather than ``ModuleType``: the SDK has no type stubs, and this
    module is the one seam that is allowed to see its untyped surface.

    Returns:
        The ``yfinance`` module.

    Raises:
        YfinanceProviderError: ``YFINANCE_SDK_MISSING``.
    """
    active = _ACTIVE_SDK.get()
    if active is not None:
        return active[0]
    try:
        import yfinance
    except ImportError as exc:
        raise YfinanceProviderError("YFINANCE_SDK_MISSING") from exc
    return yfinance
