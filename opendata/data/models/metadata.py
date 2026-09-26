"""Metadata contract models (design §4.1).

``Instrument`` and ``TradingCalendar`` are contracts the batch is built
on, not just tables a consumer may query:
:func:`opendata.pipeline.jobs.landed_instruments` reads the landed
catalog through ``Instrument`` to decide which codes a run may fetch
(:func:`opendata.pipeline.jobs.drop_inactive_symbols`), and
:func:`opendata.pipeline.trading_calendar.warehouse_calendar` reads the
landed calendar through ``TradingCalendar`` to build the ``CalendarView``
that fixes a window's end - there ``prev_trade_date`` is the witness the
open/closed rows are checked against, not the primary answer.

What is not automatic is said plainly, because the design's wording invites
a stronger reading: nothing refreshes these tables as a side effect of a
batch. :func:`opendata.pipeline.templates.refresh_metadata_backbone` is the
step zero that does it (two provider legs, one contract check per row, one
dwd upsert per domain), but it runs when an operator asks for it - landing
``dwd_instrument`` / ``dwd_trading_calendar`` is a warehouse decision, and
until it has run both reads above report an absent backbone.

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
