"""Concrete WSJ mover fetchers built from the three declarations."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.wsj._source import SOURCE
from opendata.data.providers.wsj.specs import ETF_ACTIVE, ETF_GAINERS, ETF_LOSERS

WSJETFGainersFetcher = make_http_json_fetcher(SOURCE, ETF_GAINERS)
WSJETFLosersFetcher = make_http_json_fetcher(SOURCE, ETF_LOSERS)
WSJETFActiveFetcher = make_http_json_fetcher(SOURCE, ETF_ACTIVE)
for _cls in (WSJETFGainersFetcher, WSJETFLosersFetcher, WSJETFActiveFetcher):
    _cls.__name__ = _cls.__qualname__ = "WSJETF" + _cls.__name__.split("ETF", 1)[1]

__all__ = ["WSJETFActiveFetcher", "WSJETFGainersFetcher", "WSJETFLosersFetcher"]
