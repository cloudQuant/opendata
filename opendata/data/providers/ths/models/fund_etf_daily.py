"""ETF daily bars fetcher (domain ``fund_etf_daily``, contract ``Bar``).

Wraps ``/api/fund/market/historical`` through :mod:`opendata_fuyao`. The
upstream publishes a **forward-adjusted** series and its ``adjust``
parameter is accepted but inert (six spellings and the default all answer
with the same series fingerprint, ``data.adjust`` stays ``null``), so the
unadjusted rows this leg publishes are produced locally:
:func:`opendata_fuyao.endpoints.unadjust_bars` adds back the distributions
whose ex-date strictly follows each bar, keyed on the
:class:`~opendata.data.providers.ths.models.fund_action.ThsFundActionFetcher`
event stream. Measured over 10,608 price cells in ``docs/evidence/C20``
(2,652 per fund across four funds: two distributing wide-base funds and
two never-distributing controls).

Two channel facts shape the window handling:

* coverage is a **rolling ~5 years** (1,827 days behind today) and the
  per-request span cap (1,826 days) is *one day shorter* than that depth, so
  the full reachable range takes two requests and is served by chunking in the
  transport layer;
* asking for anything older than the floor does not raise upstream - it
  answers ``code=0`` with an empty item list, indistinguishable from a fund
  that never traded (the C11a silent-empty class). So this leg refuses an
  out-of-depth ``start_date`` instead of clamping it: a backfill job must not
  be told "here is your history since 2005" when the channel cannot answer.
  It is an incremental source; ``akshare`` stays the full-history authority.

The depth floor counts Shanghai days, hence :func:`shanghai_today` rather than
a UTC date - after 16:00 UTC the UTC date is a day behind, which would shift
the floor into the empty-answer band.

Volume and amount are **not** rescaled: upstream's volume is already in fund
units, unlike eastmoney's lots (the akshare leg multiplies by 100). Three
independent measurements say so (per-bar agreement with sina to 3.6e-08
relative, Tencent's explicitly labelled 手 column hit only after x100, and
``amount/volume`` landing inside the unadjusted high-low range on every bar).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    resolve_fund_code,
)


class ThsFundEtfDailyQuery(QueryParams):
    """Validated query for the ETF daily bars domain."""

    symbol: str
    adjust: str = ""


class ThsFundEtfDailyFetcher(Fetcher[ThsFundEtfDailyQuery, tuple[Bar, ...]]):
    """Daily OHLCV bars for one on-exchange ETF, on the unadjusted basis.

    ``verified`` is true because the published series was reconciled cell for
    cell against two vendors outside fuyao (sina and Tencent) and against the
    fund's own distribution stream, and the conversion's boundary semantics
    were discriminated on live ex-dates (see ``docs/evidence/C20``).
    """

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="fund",
        domain="fund_etf_daily",
        period="1D",
        market="cn",
        source=SOURCE,
        verified=True,
        notes=(
            "unadjusted via local dividend conversion; rolling 1827d depth, not a "
            "backfill source; volume in fund units (no x100); aggregates quantised "
            "to 8 significant digits; one upstream close outlier measured on "
            "159915.SZ 2024-09-30 (C20)"
        ),
    )

    def transform_query(self, **kwargs: object) -> ThsFundEtfDailyQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, ``510300`` or ``510300.SH``),
                ``start_date``, ``end_date``, ``adjust``.

        Returns:
            The validated query.

        Raises:
            ValidationError: On unknown fields.
            ThsProviderError: When ``adjust`` asks for something other than the
                unadjusted basis. Checked here rather than after the fetch
                because this endpoint cannot honour any value of it at all:
                the parameter is accepted and ignored upstream, so a caller
                that believes it selected ``qfq`` would be reading a
                converted series (or, on the akshare leg, a genuinely
                different one). The two upstream calls this leg makes are also
                the reason not to spend them on a query that will be refused.
        """
        query = ThsFundEtfDailyQuery.model_validate(kwargs)
        if query.adjust not in {"", "unadjusted"}:
            raise ThsProviderError("THS_ADJUST_UNSUPPORTED")
        return query

    def extract_data(self, params: ThsFundEtfDailyQuery, ctx: FetchContext) -> tuple[Bar, ...]:
        """Fetch the upstream window and convert it to unadjusted (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context; carries the timeout override.

        Returns:
            Contract rows for the requested window, unadjusted.

        Raises:
            ThsProviderError: Credentials missing, or the code is not exactly
                one instrument in the ETF listing.
            FuyaoError: The window starts before the channel's rolling depth
                (refused locally - upstream would answer with an empty
                frame), or the distribution stream disagrees with its own
                totals, which leaves this leg unable to un-adjust anything.
        """
        from opendata.data.providers.ths.endpoints import (
            FUND_ETF_DEPTH_DAYS,
            fetch_fund_etf_bars,
            shanghai_today,
        )

        today = shanghai_today()
        start = params.start_date or today - timedelta(days=FUND_ETF_DEPTH_DAYS)
        end = params.end_date or today
        # 中台窗口为半开 [start, end)；调用方的 end_date 是闭区间日，故 +1 天。
        window_end = date.fromordinal(end.toordinal() + 1)
        with client(timeout_seconds=ctx.timeout) as active:
            code = resolve_fund_code(active, params.symbol)
            return fetch_fund_etf_bars(
                active, symbol=code, start=start, end=window_end, today=today
            )

    def transform_data(self, raw: tuple[Bar, ...], params: ThsFundEtfDailyQuery) -> tuple[Bar, ...]:
        """Validate and return the contract rows (the normalize stage).

        Args:
            raw: Rows from the extraction stage.
            params: The validated query.

        Returns:
            The rows, ordered by trading date.

        Raises:
            ThsProviderError: The response carries no rows for the query.
        """
        if not raw:
            raise ThsProviderError("THS_EMPTY_RESPONSE")
        return tuple(sorted(raw, key=lambda bar: bar.trade_date))


__all__ = ["ThsFundEtfDailyFetcher", "ThsFundEtfDailyQuery"]
