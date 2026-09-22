"""Metadata contract models (design §4.1).

``Instrument`` and ``TradingCalendar`` are first-class citizens: the
full-market backfill refreshes ``Instrument`` (including delisted
symbols) as its step zero, and incremental windows are computed from
``TradingCalendar``'s previous trade date.

``TradingCalendar`` has a field literally named ``date`` (fixed by the
design), which would shadow ``datetime.date`` in unqualified
annotations; this module therefore uses module-qualified annotations.
"""

from __future__ import annotations

import datetime

from opendata.data.models.base import ContractModel


class Instrument(ContractModel):
    """Tradable instrument metadata.

    Field set is fixed by design §4.1: symbol / exchange / name /
    list_date / delist_date / board / status / currency. ``symbol`` is
    the normalized exchange-suffixed key (e.g. ``600519.SH``); key
    normalization happens in ``normalize()`` (design §4.2).
    """

    symbol: str
    exchange: str
    name: str
    status: str
    currency: str
    list_date: datetime.date | None = None
    delist_date: datetime.date | None = None
    board: str | None = None


class TradingCalendar(ContractModel):
    """Exchange trading calendar, one row per calendar date.

    Field set is fixed by design §4.1: exchange / date / is_open /
    prev_trade_date / next_trade_date. Non-trading dates are stored too
    (``is_open=False``) so date arithmetic works uniformly.
    """

    exchange: str
    date: datetime.date
    is_open: bool
    prev_trade_date: datetime.date | None = None
    next_trade_date: datetime.date | None = None
