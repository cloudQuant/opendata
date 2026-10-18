"""Wave-33 TMX GraphQL-family fetchers.

This module is the contract half of the wave-33 GraphQL family: five rows that all
travel the shipped :func:`opendata.data.providers.tmx.models.client.graphql` seam
(``https://app-money.tmx.com/graphql``).  The declarations below are the landing
contract — ``canonical_model``, ``domain``, ``row_model`` (always the domain's
``contract_model``), ``capability`` and ``query_type`` — plus the query validators,
the requests and the mappers.  Nothing here reads a socket on import; the socket
only opens inside ``client.graphql``, behind the allow-list.

``EtfHistorical`` is the one row here that needs no new operation: upstream binds the
model to the same price-history fetcher this provider already ships for
``EquityHistorical`` (OpenBB's TMX package maps ``"EtfHistorical"`` to
``TmxEquityHistoricalFetcher`` at ``openbb_tmx/__init__.py:58``), so the query type
reuses the landed window validation verbatim and this row is implemented.

WHY THE OTHER FOUR NEEDED A ``client.py`` CHANGE, stated so nobody reads them as a TODO.
``client.graphql`` is an allow-list (``models/client.py:40-171``): it resolves the operation name
through ``_OPERATIONS`` and raises
``TmxProviderError("TMX_OPERATION_NOT_ALLOWED")`` at ``models/client.py:183`` *before* the governed
client is asked for anything.  These four rows are served upstream by four operations that
allow-list did not carry -- ``getEnhancedEarningsForDate`` (OpenBB ``openbb_tmx/utils/gql.py:304``,
payload at ``:321``), ``getNewsAndEvents`` (``gql.py:176``, payload at ``:204``),
``getStockListSymbolsWithQuote`` (``gql.py:393``, payload at ``:425``) and
``getCompanyInsidersActivities`` (``gql.py:430``, payload at ``:464``) -- so a leg for any of them
refused at the gate and never reached a mapper.  This wave lands them the honest way, by
*declaring* those four operations in ``_OPERATIONS`` -- each entry carries the operation name the
envelope posts, the root field TMX publishes the reply under (an alias where upstream aliases), and
the selection that publishes the columns the mapper reads -- not by re-posting an envelope from a
fetcher, which would bypass the allow-list instead of using it, and not by mapping a row onto an
unrelated allow-listed operation, which would assert an upstream shape nothing here evidences.

Each of the four mappers refuses at a site its own fixture document reaches, and each refusal is a
rule the row needs rather than a screen a shared helper already applies: the ``newsid`` rule is the
only thing between a missing source id and a fabricated ``url``, which ``tmx_company_news`` files as
half its natural key; the ``stockListId`` echo rule is the only thing that notices a reply for a
different list than the one asked (the shipped precedent is the dividend page echo, which refuses a
``pageNumber`` that disagrees with the page requested); the ``buy``/``sell`` section rule is the
only thing that turns a missing source section into a typed refusal instead of a bare
``TypeError``; and the ``report_date`` rule is the only thing that notices a source row asserting
the very date this row must stamp.  ``_not_implemented`` and its
``TMX_WAVE33_GRAPHQL_FETCH_NOT_IMPLEMENTED`` code are gone: a scaffold refusal no row can reach is
the dead predicate this repo has already been burned by.
"""

from __future__ import annotations

import datetime
from typing import ClassVar, Final, Literal, TypeVar

from pydantic import ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from opendata.data.capability import Capability
from opendata.data.domains import contract_model
from opendata.data.models.base import ContractModel
from opendata.data.models.tmx import (
    TmxCalendarEarnings,
    TmxCompanyNews,
    TmxEquityGainers,
    TmxEtfHistorical,
    TmxInsiderTrading,
)
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers.tmx.models import client
from opendata.data.providers.tmx.models.client import GraphqlReply, TmxProviderError, list_rows
from opendata.data.providers.tmx.models.fetchers import (
    EquityHistoricalQuery,
    _history_limit,
    _root,
    _row,
    _SymbolQuery,
    _TmxQuery,
    _TypedFetcher,
)

_Date = datetime.date

_QueryT = TypeVar("_QueryT", bound=QueryParams)
_ContractT = TypeVar("_ContractT", bound=ContractModel)

#: TMX Money serves both the news and the stock-list operations in a fixed locale; upstream sends
#: ``"en"`` in the payload variables (OpenBB ``openbb_tmx/utils/gql.py:205`` and ``:426``), and this
#: provider never localizes, so the value is declared once here.
_LOCALE: Final = "en"

#: Article addresses are not published by ``getNewsAndEvents``: TMX's own UI builds them from the
#: article id, and upstream derives the same shape (OpenBB ``openbb_tmx/models/company_news.py:113``
#: -- ``money.tmx.com/quote/<SYMBOL>/news/<newsid>``).  It is a published *cell*, never a request,
#: so it adds no host to this provider's transport.
_NEWS_URL_BASE: Final = "https://money.tmx.com/quote"

#: TMX's own public stock-list identifiers, keyed by this row's declared ``category``.  The values
#: are TMX's ids, evidenced by OpenBB's TMX package at ``openbb_tmx/models/gainers.py:16-26``;
#: ``None`` category takes upstream's own default (``gainers.py:54-57``).
_STOCK_LISTS: Final[dict[str, str]] = {
    "52w_high": "TOP_WEEK_52_HIGH",
    "dividend": "TOP_DIVIDEND",
    "energy": "TOP_ENERGY",
    "healthcare": "TOP_HEALTHCARE",
    "industrials": "TOP_INDUSTRIALS",
    "price_performer": "TOP_PRICE_PERFORMER",
    "real_estate": "TOP_REAL_ESTATE",
    "rising_stars": "RISING_STARS",
    "tech": "TOP_TECH",
    "utilities": "TOP_UTILITIES",
    "volume": "TOP_VOLUME",
}
_DEFAULT_STOCK_LIST: Final = "price_performer"

#: The two arrays ``getCompanyInsidersActivities`` files each period's transactions under.  The
#: direction is not a cell upstream publishes; it is the array the record sits in (OpenBB
#: ``openbb_tmx/models/insider_trading.py:141-153`` flattens exactly these two keys).
_INSIDER_DIRECTIONS: Final[tuple[str, str]] = ("buy", "sell")


def _capability(domain: str, period: str, model: str, asset_class: str) -> Capability:
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period=period,
        market="CA",
        source="tmx",
        verified=False,
        notes=f"{model}: wave-33 TMX GraphQL family contract; synthetic fixtures only",
    )


def _row_model(domain: str, model: type[_ContractT]) -> type[_ContractT]:
    """Bind the domain's contract class, refusing a mismatch at import time."""
    resolved = contract_model(domain)
    if resolved is not model:
        raise TypeError(f"{domain}: contract resolves to {resolved.__name__}, not {model.__name__}")
    return model


def _stock_list_id(category: str | None) -> str:
    """Resolve this row's declared category to TMX's list id, refusing an unknown one.

    Reached from both stages: the request publishes the id and the mapper re-derives the same value
    to check the reply's echo, so the two cannot drift apart.
    """
    resolved = _STOCK_LISTS.get(_DEFAULT_STOCK_LIST if category is None else category)
    if resolved is None:
        raise TmxProviderError("TMX_BAD_FILTER")
    return resolved


class CalendarEarningsQuery(_TmxQuery):
    """One date of the TMX earnings calendar; the operation serves exactly one date."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: StrictStr | None = None
    start_date: _Date
    end_date: _Date | None = None

    @model_validator(mode="after")
    def _validate_single_date(self) -> CalendarEarningsQuery:
        if self.end_date is not None and self.end_date != self.start_date:
            raise ValueError(
                "getEnhancedEarningsForDate takes one date and publishes no per-row date, so this "
                "row serves one date per request rather than looping a window"
            )
        return self


class CompanyNewsQuery(_SymbolQuery):
    """One page of TMX company news for a single symbol."""

    limit: StrictInt = Field(default=25, ge=1, le=100)
    page: StrictInt = Field(default=1, ge=1, le=10_000)


class EquityGainersQuery(_TmxQuery):
    """Rank selection for the TMX gainers/losers screen."""

    sort: Literal["price", "change", "percent_change", "volume"] = "percent_change"
    category: StrictStr | None = None


class InsiderTradingQuery(_SymbolQuery):
    """Bounded insider-transaction page request for one symbol."""

    limit: StrictInt = Field(default=50, ge=1, le=1000)
    summary: StrictBool = False


class EtfHistoricalQuery(EquityHistoricalQuery):
    """Daily ETF history window, validated exactly like the landed equity history."""


class _GraphQLFetcher(_TypedFetcher[_QueryT]):
    """Shared GraphQL seam for the wave-33 rows: query validation only."""

    domain: ClassVar[str]
    query_type: ClassVar[type[_QueryT]]

    def transform_query(self, **kwargs: object) -> _QueryT:
        """Validate the request against this row's declared query type."""
        return self.query_type.model_validate(kwargs)


class CalendarEarningsFetcher(_GraphQLFetcher[CalendarEarningsQuery]):
    """Earnings-calendar rows from the TMX Money GraphQL family."""

    canonical_model: ClassVar[str | None] = "CalendarEarnings"
    domain: ClassVar[str] = "tmx_calendar_earnings"
    query_type: ClassVar[type[CalendarEarningsQuery]] = CalendarEarningsQuery
    row_model: ClassVar[type[TmxCalendarEarnings]] = _row_model(
        "tmx_calendar_earnings", TmxCalendarEarnings
    )
    capability: ClassVar[Capability] = _capability(
        "tmx_calendar_earnings", "event", "CalendarEarnings", "equity"
    )

    def extract_data(self, params: CalendarEarningsQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the single-date earnings-calendar operation for this row's one-date window.

        ``date`` is the operation's only variable (OpenBB ``openbb_tmx/utils/gql.py:304-318``,
        payload ``:321-325``), so the window's single date is what gets published.  ``symbol`` is
        refused rather than ignored: the selection filters nothing, so a symbol on the request
        would be dropped silently and the caller would read an unfiltered calendar as a filtered
        one.  Upstream loops one request per weekday of the range (OpenBB
        ``openbb_tmx/models/calendar_earnings.py:99-131``); this row keeps the shipped one-leg
        pipeline and the query validator refuses a wider window instead.
        """
        if params.symbol is not None:
            raise TmxProviderError("TMX_BAD_FILTER")
        return client.graphql(
            "getEnhancedEarningsForDate",
            {"date": params.start_date.isoformat()},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: CalendarEarningsQuery) -> FetchResult:
        """Map one date's earnings rows, refusing a row that argues about its own date.

        The selection publishes seven columns (``symbol``, ``companyName``, ``announceTime``,
        ``estimatedEps``, ``actualEps``, ``epsSurprisePercent``, ``epsSurpriseDollar``) and no date;
        ``report_date`` is the domain's ``time_field`` and half of its natural key, so the mapper
        stamps the requested date -- the same echo upstream makes at
        ``openbb_tmx/models/calendar_earnings.py:122``.  A source row that publishes ``report_date``
        itself is therefore a disagreement about which date the reply asserts, and it is refused
        rather than overwritten: projection here puts the requested date in every row, so without
        this guard a source-asserted date would vanish and the consumer could not tell a one-date
        calendar from a reply that carried several.  ``eps_previous`` stays unproduced because the
        selection does not publish it; nothing is invented to fill it.
        """
        source_rows = list_rows(_root(raw, "getEnhancedEarningsForDate"))
        rows = []
        for source_row in source_rows:
            if "report_date" in source_row:
                raise TmxProviderError("TMX_BAD_ROW")
            rows.append(
                _row(
                    TmxCalendarEarnings,
                    {
                        "report_date": params.start_date.isoformat(),
                        "symbol": source_row.get("symbol"),
                        "name": source_row.get("companyName"),
                        "eps_consensus": source_row.get("estimatedEps"),
                        "eps_actual": source_row.get("actualEps"),
                        "eps_surprise": source_row.get("epsSurpriseDollar"),
                        "surprise_percent": source_row.get("epsSurprisePercent"),
                        "reporting_time": source_row.get("announceTime"),
                    },
                )
            )
        return tuple(rows)


class CompanyNewsFetcher(_GraphQLFetcher[CompanyNewsQuery]):
    """Company news article rows from the TMX Money GraphQL family."""

    canonical_model: ClassVar[str | None] = "CompanyNews"
    domain: ClassVar[str] = "tmx_company_news"
    query_type: ClassVar[type[CompanyNewsQuery]] = CompanyNewsQuery
    row_model: ClassVar[type[TmxCompanyNews]] = _row_model("tmx_company_news", TmxCompanyNews)
    capability: ClassVar[Capability] = _capability(
        "tmx_company_news", "event", "CompanyNews", "equity"
    )

    def extract_data(self, params: CompanyNewsQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the news-and-events operation for one page of one symbol's articles.

        The variables are the operation's own four (``symbol``, ``page``, ``limit``, ``locale`` --
        OpenBB ``openbb_tmx/utils/gql.py:176-181``, payload ``:203-207``), all required, and this
        row publishes its declared page and ceiling into them.  TMX answers under the aliases
        ``news`` and ``events``; ``_OPERATIONS`` pins the reply to ``news``, which is the section
        this row maps.
        """
        return client.graphql(
            "getNewsAndEvents",
            {
                "symbol": params.symbol,
                "page": params.page,
                "limit": params.limit,
                "locale": _LOCALE,
            },
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: CompanyNewsQuery) -> FetchResult:
        """Map article rows, refusing one whose address cannot be formed.

        ``tmx_company_news`` files its natural key as ``url`` + ``date``, and the selection
        publishes neither a URL nor a link: it publishes ``newsid``, from which TMX's own article
        address is built (``_NEWS_URL_BASE``, evidenced at OpenBB
        ``openbb_tmx/models/company_news.py:113``).  So a record without a usable ``newsid`` is
        refused here rather than mapped -- formatting it anyway would produce a well-formed-looking
        ``.../news/None`` cell whose key no article has, which is exactly the invented value this
        provider refuses elsewhere.  The page ceiling is the ruling this provider already makes for
        bounded replies (``models/fetchers.py`` history and dividends, applied to the ETF row at
        :meth:`EtfHistoricalFetcher.transform_data`): ``limit`` is published to the source, so more
        records than the caller asked for are a contract violation, not a longer page, and are
        refused instead of truncated.  ``author``, ``body`` and ``images`` stay unproduced because
        the selection does not publish them.
        """
        source_rows = list_rows(_root(raw, "news"))
        if len(source_rows) > params.limit:
            raise TmxProviderError("TMX_BAD_ROWS")
        rows = []
        for source_row in source_rows:
            newsid = source_row.get("newsid")
            if not isinstance(newsid, str) or not newsid or newsid != newsid.strip():
                raise TmxProviderError("TMX_BAD_ROW")
            rows.append(
                _row(
                    TmxCompanyNews,
                    {
                        "date": source_row.get("datetime"),
                        "title": source_row.get("headline"),
                        "url": f"{_NEWS_URL_BASE}/{params.symbol.upper()}/news/{newsid}",
                        "source": source_row.get("source"),
                        "excerpt": source_row.get("summary"),
                        "symbols": params.symbol,
                    },
                )
            )
        return tuple(rows)


class EquityGainersFetcher(_GraphQLFetcher[EquityGainersQuery]):
    """Ranked gainers/losers rows from the TMX Money GraphQL family."""

    canonical_model: ClassVar[str | None] = "EquityGainers"
    domain: ClassVar[str] = "tmx_equity_gainers"
    query_type: ClassVar[type[EquityGainersQuery]] = EquityGainersQuery
    row_model: ClassVar[type[TmxEquityGainers]] = _row_model("tmx_equity_gainers", TmxEquityGainers)
    capability: ClassVar[Capability] = _capability(
        "tmx_equity_gainers", "snapshot", "EquityGainers", "equity"
    )

    def extract_data(self, params: EquityGainersQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the stock-list operation for this row's declared category.

        ``stockListId`` is the operation's only selector (OpenBB
        ``openbb_tmx/utils/gql.py:393-399``, payload ``:424-428``) and the list itself fixes the
        ranking, so ``sort`` is refused rather than ignored: a request for another ordering
        cannot be served by this operation and must not come back as the default list.
        ``symbol`` is refused for the same reason -- the operation selects a list, never one
        company, so a symbol on the request would be dropped and the caller would read the whole
        screen as if it had been filtered.
        """
        if params.sort != "percent_change":
            raise TmxProviderError("TMX_BAD_SORT")
        if params.symbol is not None:
            raise TmxProviderError("TMX_BAD_FILTER")
        return client.graphql(
            "getStockListSymbolsWithQuote",
            {"stockListId": _stock_list_id(params.category), "locale": _LOCALE},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EquityGainersQuery) -> FetchResult:
        """Map the ranked list, refusing a reply that is a different list than the one asked.

        TMX publishes one object under ``stockList`` whose ``listItems`` carry the rows, and the
        object echoes ``stockListId`` (OpenBB ``openbb_tmx/models/gainers.py:158`` reads
        ``data.stockList.listItems``).  That echo is this row's own integrity check, the same ruling
        the dividend pager makes on ``pageNumber``: a reply naming another list would map perfectly
        well -- every cell is present and typed -- and would silently serve the caller a screen it
        never requested, so the count and the shapes cannot catch it and only the echo can.  The
        list's ``metricTitle`` and each item's ``metric`` are not projected: ``TmxEquityGainers``
        declares fixed columns and no column named by the source, so the ranking metric cell has no
        place in this contract; ``thirty_day_price_change``, ``ninety_day_price_change``,
        ``dividend_yield`` and ``avg_volume_10d`` stay unproduced because the selection does not
        publish them.
        """
        payload = _root(raw, "stockList")
        if not isinstance(payload, dict):
            raise TmxProviderError("TMX_BAD_ROWS")
        if payload.get("stockListId") != _stock_list_id(params.category):
            raise TmxProviderError("TMX_BAD_ROWS")
        return tuple(
            _row(
                TmxEquityGainers,
                {
                    "symbol": source_row.get("symbol"),
                    "name": source_row.get("longName"),
                    "rank": source_row.get("rank"),
                    "price": source_row.get("price"),
                    "change": source_row.get("priceChange"),
                    "percent_change": source_row.get("percentChange"),
                    "volume": source_row.get("volume"),
                },
            )
            for source_row in list_rows(payload.get("listItems"))
        )


class InsiderTradingFetcher(_GraphQLFetcher[InsiderTradingQuery]):
    """Insider-transaction rows from the TMX Money GraphQL family."""

    canonical_model: ClassVar[str | None] = "InsiderTrading"
    domain: ClassVar[str] = "tmx_insider_trading"
    query_type: ClassVar[type[InsiderTradingQuery]] = InsiderTradingQuery
    row_model: ClassVar[type[TmxInsiderTrading]] = _row_model(
        "tmx_insider_trading", TmxInsiderTrading
    )
    capability: ClassVar[Capability] = _capability(
        "tmx_insider_trading", "event", "InsiderTrading", "equity"
    )

    def extract_data(self, params: InsiderTradingQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the insider-activity operation for one symbol.

        ``symbol`` is the operation's only variable (OpenBB ``openbb_tmx/utils/gql.py:430-434``,
        payload ``:463-468``); it is declared optional upstream and required by this row's query, so
        the reply is always one company's.  TMX answers with two sections and no page or limit
        variable, which is why the ceiling is applied here rather than published.
        """
        return client.graphql(
            "getCompanyInsidersActivities",
            {"symbol": params.symbol},
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: InsiderTradingQuery) -> FetchResult:
        """Flatten the section the request asked for, refusing a reply that does not carry it.

        ``summary`` routes the row between ``activitySummary`` (period totals) and
        ``insiderActivities`` (per-insider records, flattened from each period's ``buy`` and
        ``sell`` arrays the way OpenBB ``openbb_tmx/models/insider_trading.py:141-163`` does),
        mirroring the same flag on upstream's query.  The operation's reply is one object of those
        two sections, and the client's envelope check only proves the root field is present, so a
        reply missing
        the section this request asked for is refused here -- without the check the flatten would
        walk into ``None`` and the caller would get a bare ``TypeError`` with neither a code nor a
        field attribution.  ``period`` and ``symbol`` are stamped by the mapper: the operation
        publishes no symbol column and names its periods by ``periodkey``, and both are
        ``tmx_insider_trading`` natural-key columns, with the direction taken from the array the
        record sat in (``transaction_type`` carries the same word because the domain's natural key
        names it and the source's only direction evidence is that array).  ``limit`` is the ceiling
        the caller declares for itself: the operation has no limit variable, so this row cannot ask
        the source for fewer records and refuses an over-long reply instead of truncating it -- the
        same stance :meth:`EtfHistoricalFetcher.transform_data` takes.  Dates, CIKs, titles,
        ownership types and filing URLs stay unproduced: the selection does not publish them.
        """
        payload = _root(raw, "getCompanyInsidersActivities")
        if not isinstance(payload, dict):
            raise TmxProviderError("TMX_BAD_ROWS")
        records: list[dict[str, object]] = []
        if params.summary:
            summary = payload.get("activitySummary")
            if not isinstance(summary, list):
                raise TmxProviderError("TMX_BAD_ROWS")
            records.extend(
                {
                    "period": source_row.get("periodkey"),
                    "symbol": params.symbol,
                    "securities_bought": source_row.get("buyShares"),
                    "securities_sold": source_row.get("soldShares"),
                    "net_activity": source_row.get("netActivity"),
                    "securities_transacted": source_row.get("totalShares"),
                }
                for source_row in list_rows(summary)
            )
        else:
            activities = payload.get("insiderActivities")
            if not isinstance(activities, list):
                raise TmxProviderError("TMX_BAD_ROWS")
            for activity in list_rows(activities):
                for direction in _INSIDER_DIRECTIONS:
                    transactions = activity.get(direction)
                    if not isinstance(transactions, list):
                        raise TmxProviderError("TMX_BAD_ROW")
                    records.extend(
                        {
                            "period": activity.get("periodkey"),
                            "symbol": params.symbol,
                            "owner_name": source_row.get("name"),
                            "transaction_type": direction,
                            "acquisition_or_disposition": direction,
                            "number_of_trades": source_row.get("trades"),
                            "securities_transacted": source_row.get("shares"),
                            "securities_owned": source_row.get("sharesHeld"),
                            "trade_value": source_row.get("tradeValue"),
                        }
                        for source_row in list_rows(transactions)
                    )
        if len(records) > params.limit:
            raise TmxProviderError("TMX_BAD_ROWS")
        return tuple(_row(TmxInsiderTrading, record) for record in records)


class EtfHistoricalFetcher(_GraphQLFetcher[EtfHistoricalQuery]):
    """Daily ETF history over the already-shipped price-history operation."""

    canonical_model: ClassVar[str | None] = "EtfHistorical"
    domain: ClassVar[str] = "tmx_etf_historical"
    query_type: ClassVar[type[EtfHistoricalQuery]] = EtfHistoricalQuery
    row_model: ClassVar[type[TmxEtfHistorical]] = _row_model("tmx_etf_historical", TmxEtfHistorical)
    capability: ClassVar[Capability] = _capability(
        "tmx_etf_historical", "daily", "EtfHistorical", "etf"
    )

    def extract_data(self, params: EtfHistoricalQuery, ctx: FetchContext) -> GraphqlReply:
        """Run the TMX daily price-history GraphQL operation the ETF model is bound to.

        The operation, its variables and the ``limit`` it publishes are the landed
        ``EquityHistorical`` request verbatim (``models/fetchers.py:256-270``): upstream binds
        ``EtfHistorical`` to that same fetcher, and the ``getCompanyPriceHistory`` selection
        (``models/client.py:49-63``; OpenBB ``openbb_tmx/utils/gql.py:110-133``) is what
        publishes the eleven columns this row maps.
        """
        return client.graphql(
            "getCompanyPriceHistory",
            {
                "symbol": params.symbol,
                "start": params.start_date.isoformat(),
                "end": params.end_date.isoformat(),
                "adjusted": False,
                "adjustmentType": None,
                "unadjusted": None,
                "limit": _history_limit(params),
            },
            timeout=self._timeout(ctx),
        )

    def transform_data(self, raw: GraphqlReply, params: EtfHistoricalQuery) -> FetchResult:
        """Map daily source bars into the ETF history contract, refusing over-delivery.

        ``TMX_BAD_ROWS`` here is the ruling this provider already makes twice
        (``models/fetchers.py:277`` for history, ``:449`` for filings), applied to the ETF row:
        a reply longer than the window's own ``limit`` is a source contract violation, not a
        longer history, and truncating it silently is indistinguishable from a source with no
        more bars.  ``change_percent`` is published as ``None`` for the same reason the landed
        history mapper gives at ``models/fetchers.py:292``: the scale of TMX's raw
        ``changePercent`` is not established by the UI selection, so no number is invented.
        """
        source_rows = list_rows(_root(raw, "getCompanyPriceHistory"))
        if len(source_rows) > _history_limit(params):
            raise TmxProviderError("TMX_BAD_ROWS")
        return tuple(
            _row(
                TmxEtfHistorical,
                {
                    "symbol": params.symbol,
                    "date": source_row.get("datetime"),
                    "open": source_row.get("openPrice"),
                    "high": source_row.get("high"),
                    "low": source_row.get("low"),
                    "close": source_row.get("closePrice"),
                    "volume": source_row.get("volume"),
                    "vwap": source_row.get("vwap"),
                    "change": source_row.get("change"),
                    "change_percent": None,
                    "transactions": source_row.get("numberOfTrade"),
                    "transactions_value": source_row.get("tradeValue"),
                },
            )
            for source_row in source_rows
        )


__all__ = [
    "CalendarEarningsFetcher",
    "CalendarEarningsQuery",
    "CompanyNewsFetcher",
    "CompanyNewsQuery",
    "EquityGainersFetcher",
    "EquityGainersQuery",
    "EtfHistoricalFetcher",
    "EtfHistoricalQuery",
    "InsiderTradingFetcher",
    "InsiderTradingQuery",
]
