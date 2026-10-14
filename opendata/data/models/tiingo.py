"""Reviewed contract for tiingo's exploded crypto history rows.

The 9 fields are exactly the columns
``opendata/data/providers/tiingo/specs.py::CRYPTO_HISTORICAL`` declares -- the eight keys one
``priceData[]`` item publishes plus the ``ticker`` its ``nested_array_explode`` plan copies onto
every row it yields. ``build_row_model`` refuses a declaration whose column names are the symmetric
difference of this class's fields, so the two lists cannot drift apart silently.

Three required columns and two nullability shapes:

* ``symbol`` and ``date`` are required *and* non-nullable, the way the declaration spells them
  (``required=True, nullable=False``). The expansion refuses a ticker object whose child array has
  no date, because an exploded bar without its day is not a bar of the series.
* ``close`` is required but nullable: the plan marks the pointer required (crypto_historical.py's
  own price field) while the body may still print it null, so the field has no default yet admits
  ``None``.
* The remaining six -- ``open``, ``high``, ``low``, ``volume``, ``transactions``,
  ``volume_notional`` -- are optional with a null default.

``vwap`` is a field of the cited standard model that the tiingo body never publishes, so it is not
a column here and not a field of this contract.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class TiingoCryptoHistorical(ContractModel):
    """One crypto bar of one ticker, out of that ticker's nested ``priceData`` array."""

    symbol: str
    date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None
    volume: float | None = None
    transactions: int | None = None
    volume_notional: float | None = None


class TiingoEquityHistorical(ContractModel):
    """One daily or IEX intraday OHLCV bar returned by Tiingo."""

    symbol: StrictStr
    date: date | datetime
    open: StrictFloat | None = None
    high: StrictFloat | None = None
    low: StrictFloat | None = None
    close: StrictFloat
    volume: StrictInt | None = None
    adj_open: StrictFloat | None = None
    adj_high: StrictFloat | None = None
    adj_low: StrictFloat | None = None
    adj_close: StrictFloat | None = None
    adj_volume: StrictInt | None = None
    div_cash: StrictFloat | None = None
    split_factor: StrictFloat | None = None


class TiingoEtfHistorical(TiingoEquityHistorical):
    """One ETF historical bar; Tiingo documents ETF support on the same EOD route."""


class TiingoCurrencyHistorical(ContractModel):
    """One foreign-exchange OHLC bar from Tiingo's FX history endpoint."""

    symbol: StrictStr
    date: datetime
    open: StrictFloat
    high: StrictFloat
    low: StrictFloat
    close: StrictFloat


class TiingoTrailingDividendYield(ContractModel):
    """One Tiingo trailing-one-year distribution yield observation."""

    symbol: StrictStr
    date: datetime
    trailing_dividend_yield: StrictFloat | None = None


class TiingoNews(ContractModel):
    """One news item from Tiingo's documented financial-news response."""

    symbol: StrictStr | None = None
    id: StrictInt
    title: StrictStr
    url: StrictStr
    description: StrictStr | None = None
    published_date: datetime
    crawl_date: datetime
    source: StrictStr
    tickers: list[StrictStr]
    tags: list[StrictStr]


class TiingoCompanyNews(TiingoNews):
    """One Tiingo news item filtered to a requested ticker set."""


class TiingoWorldNews(TiingoNews):
    """One Tiingo news item from the unfiltered global feed."""
