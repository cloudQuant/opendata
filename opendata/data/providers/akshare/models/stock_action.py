"""Corporate action fetcher (domain ``stock_action``, contract ``CorporateAction``).

Wraps ``opendata_http.stock_history_dividend_detail`` (sina dividend
and rights pages). Unit conversion: sina's ``送股`` / ``转增`` /
``派息`` and ``配股方案`` are per-10-share quantities, the contract
stores per-share values, so ``normalize()`` divides them by 10;
``配股价格`` is already a per-share price and is passed through.
Rows sina leaves undated are dropped and reported (see
``_report_drops``), because an undated amount can belong to a dated
ex-date.
"""

import re
from typing import ClassVar, cast

import pandas as pd
from loguru import logger

from opendata.data.capability import Capability
from opendata.data.models import CorporateAction
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import (
    as_date,
    plain_symbol,
    within_window,
)

_RIGHTS_PLAN = re.compile(r"10\s*配\s*([0-9.]+)")


class StockActionQuery(QueryParams):
    """Validated query for the corporate action domain."""

    symbol: str


class AkshareStockActionFetcher(Fetcher[StockActionQuery, pd.DataFrame]):
    """Dividend and rights events for one A-share symbol."""

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_action",
        # ``1D`` and not a private ``event`` spelling: ``authority.json`` ranks
        # this leg behind the ths one, and ``resolve()`` matches ``period`` -
        # two legs of one domain that spell the period differently can never
        # take over for each other, so the published fallback would be
        # unreachable (measured, docs/evidence/C24/fallback-generality-sweep.txt).
        period="1D",
        market="cn",
        source=SOURCE,
        verified=False,
        notes=(
            "sina leaves pending plans (进度=预案) undated and drops them here, so cash on a "
            "dated ex-date can under-report (measured 601318 2018-06-07: 1.0 returned vs 1.2 "
            "published by ths); rights ratio is nominal per 10 shares, not the subscribed "
            "amount (C26)"
        ),
    )

    def transform_query(self, **kwargs: object) -> StockActionQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required).

        Returns:
            The validated query.
        """
        return StockActionQuery.model_validate(kwargs)

    def extract_data(self, params: StockActionQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch the sina dividend and rights pages (fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context (unused: sina pages have no
                timeout parameter).

        Returns:
            Frame with an extra ``indicator`` column marking the
            source page (``分红`` / ``配股``).
        """
        # lazy: load the ported tree on routing only
        import opendata.data.providers.akshare._vendor as opendata_http

        symbol = plain_symbol(params.symbol)
        dividends = opendata_http.stock_history_dividend_detail(symbol=symbol, indicator="分红")
        dividends = dividends.assign(indicator="分红")
        rights = opendata_http.stock_history_dividend_detail(symbol=symbol, indicator="配股")
        rights = rights.assign(indicator="配股")
        return cast("pd.DataFrame", pd.concat([dividends, rights], ignore_index=True))

    def transform_data(self, raw: pd.DataFrame, params: StockActionQuery) -> FetchResult:
        """Normalize the pages into ``CorporateAction`` rows.

        Args:
            raw: Combined upstream frame with the ``indicator``
                marker column.
            params: The validated query.

        Returns:
            ``CorporateAction`` rows inside the requested window; rows
            without an ex-date or with all-zero economics are dropped
            (contract rule). Sina's pages carry the whole dividend
            history, so the window is applied here. A dropped row that
            still carried economics is reported, because dropping it
            silently would answer "nothing was paid" when sina only
            failed to date it.
        """
        actions: list[CorporateAction] = []
        symbol = plain_symbol(params.symbol)
        undated_dividends: list[str] = []
        undated_rights: list[str] = []
        outside_window = 0
        all_zero = 0
        for record in raw.to_dict("records"):
            if record.get("indicator") == "分红":
                cash = _per_share(record.get("派息"))
                stock = _per_share(record.get("送股")) + _per_share(record.get("转增"))
                ex_date = as_date(record.get("除权除息日"))
                if ex_date is None:
                    if cash != 0.0 or stock != 0.0:
                        undated_dividends.append(f"{record.get('公告日期')}:{cash}+{stock}")
                    continue
                if not within_window(ex_date, params.start_date, params.end_date):
                    outside_window += 1
                    continue
                if cash == 0.0 and stock == 0.0:
                    all_zero += 1
                    continue
                actions.append(
                    CorporateAction(
                        symbol=symbol,
                        ex_date=ex_date,
                        cash_dividend=cash,
                        stock_dividend=stock,
                    )
                )
            else:
                rights_shares = _parse_rights_plan(record.get("配股方案"))
                rights_price = _price(record.get("配股价格"))
                ex_date = as_date(record.get("除权日"))
                if ex_date is None:
                    if rights_shares != 0.0 or rights_price != 0.0:
                        undated_rights.append(
                            f"{record.get('公告日期')}:{rights_shares}@{rights_price}"
                        )
                    continue
                if not within_window(ex_date, params.start_date, params.end_date):
                    outside_window += 1
                    continue
                if rights_shares == 0.0 and rights_price == 0.0:
                    all_zero += 1
                    continue
                actions.append(
                    CorporateAction(
                        symbol=symbol,
                        ex_date=ex_date,
                        rights_shares=rights_shares,
                        rights_price=rights_price,
                    )
                )
        _report_drops(symbol, undated_dividends, undated_rights, outside_window, all_zero)
        return actions


#: How many samples one drop report carries.
_DROP_SAMPLES = 3


def _report_drops(
    symbol: str,
    undated_dividends: list[str],
    undated_rights: list[str],
    outside_window: int,
    all_zero: int,
) -> None:
    """Say out loud which upstream rows never became events.

    An undated plan is the loss that matters: sina lists a plan and its
    implementation as separate rows, and the undated row's amount can
    belong to the dated ex-date. Measured on 601318 2018-06-07 - fuyao
    publishes ``dividend_per_share`` 1.2 for that day while this leg's two
    source rows read 10.0/10 (dated) and 2.0/10 (``进度=预案``, no date), so
    the returned event carries 1.0 and the extra 0.2 is invisible unless it
    is reported (C26, ``docs/evidence/C26/silent-drop-measure.txt``).

    Args:
        symbol: Plain code the rows were dropped for.
        undated_dividends: Samples of dropped 分红 rows that still carried
            amounts.
        undated_rights: Samples of dropped 配股 rows that still carried
            economics.
        outside_window: Rows cut by the caller's window (contract-required,
            counted only).
        all_zero: Rows with no economics at all (contract-required,
            counted only).
    """
    if undated_dividends or undated_rights:
        logger.warning(
            f"stock_action {symbol}: dropped {len(undated_dividends)} undated dividend "
            f"plan(s) {undated_dividends[:_DROP_SAMPLES]} and {len(undated_rights)} "
            f"undated rights plan(s) {undated_rights[:_DROP_SAMPLES]} - a cash amount on "
            f"an undated row may belong to a dated ex-date, so the events returned for "
            f"that day can under-report"
        )
    logger.debug(
        f"stock_action {symbol}: {outside_window} row(s) outside the requested window, "
        f"{all_zero} all-zero row(s)"
    )


def _numeric(value: object) -> float | None:
    """Coerce a cell to float, None when not numeric.

    Args:
        value: Upstream cell.

    Returns:
        The float value, or None.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _per_share(value: object) -> float:
    """Convert a per-10-share cell to per-share, 0.0 when missing.

    Args:
        value: Upstream cell (per-10-share quantity or None/NaN).

    Returns:
        The per-share value.
    """
    numeric = _numeric(value)
    return numeric / 10 if numeric is not None else 0.0


def _price(value: object) -> float:
    """Read a money cell as published, 0.0 when missing.

    Args:
        value: The ``配股价格`` cell. Sina states prices per share, not per
            ten shares: C26 measured six dated allotments whose cells matched
            东方财富's ``配股价`` verbatim, with zero rows read as divided by
            ten (``docs/evidence/C26/``).

    Returns:
        The price per share.
    """
    numeric = _numeric(value)
    return numeric if numeric is not None else 0.0


def _parse_rights_plan(plan: object) -> float:
    """Parse the rights ratio into shares per share.

    Args:
        plan: The ``配股方案`` cell. The ported page coerces it with
            ``pd.to_numeric``, so the live shape is a bare number meaning
            shares per ten shares (``1.5`` = 每10股配1.5股); the page's own
            detail schema labels the column ``配股比例（10配）`` and
            东方财富 still spells the same value ``"10配1.5"``. Both shapes
            are accepted because the ratio is per ten shares either way
            (C26 measured 6 events, none of them parsing to 0).

    Returns:
        Shares per share, 0.0 when the plan does not parse.
    """
    if isinstance(plan, str):
        match = _RIGHTS_PLAN.search(plan)
        return float(match.group(1)) / 10 if match else 0.0
    return _per_share(plan)
