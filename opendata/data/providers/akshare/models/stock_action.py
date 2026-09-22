"""Corporate action fetcher (domain ``stock_action``, contract ``CorporateAction``).

Wraps ``opendata_http.stock_history_dividend_detail`` (sina dividend
and rights pages). Unit conversion: sina's ``送股`` / ``转增`` /
``派息`` are per-10-share quantities, the contract stores per-share
values, so ``normalize()`` divides by 10; the rights ratio is parsed
from the ``配股方案`` text (``10配3`` -> 0.3 shares per share).
"""

import re
from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import CorporateAction
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol

_RIGHTS_PLAN = re.compile(r"10\s*配\s*([0-9.]+)")


class StockActionQuery(QueryParams):
    """Validated query for the corporate action domain."""

    symbol: str


class AkshareStockActionFetcher(Fetcher[StockActionQuery, pd.DataFrame]):
    """Dividend and rights events for one A-share symbol."""

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="stock_action",
        period="event",
        market="cn",
        source=SOURCE,
        verified=False,
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
        import opendata_http  # lazy: load the ported tree on routing only

        symbol = plain_symbol(params.symbol)
        dividends = opendata_http.stock_history_dividend_detail(symbol=symbol, indicator="分红")
        dividends = dividends.assign(indicator="分红")
        rights = opendata_http.stock_history_dividend_detail(symbol=symbol, indicator="配股")
        rights = rights.assign(indicator="配股")
        return pd.concat([dividends, rights], ignore_index=True)

    def transform_data(
        self, raw: pd.DataFrame, params: StockActionQuery
    ) -> FetchResult:
        """Normalize the pages into ``CorporateAction`` rows.

        Args:
            raw: Combined upstream frame with the ``indicator``
                marker column.
            params: The validated query.

        Returns:
            ``CorporateAction`` rows; rows without an ex-date or
            with all-zero economics are dropped (contract rule).
        """
        actions: list[CorporateAction] = []
        symbol = plain_symbol(params.symbol)
        for record in raw.to_dict("records"):
            if record.get("indicator") == "分红":
                ex_date = as_date(record.get("除权除息日"))
                if ex_date is None:
                    continue
                cash = _per_share(record.get("派息"))
                stock = _per_share(record.get("送股")) + _per_share(record.get("转增"))
                if cash == 0.0 and stock == 0.0:
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
                ex_date = as_date(record.get("除权日"))
                if ex_date is None:
                    continue
                rights_shares = _parse_rights_plan(record.get("配股方案"))
                rights_price = _per_share(record.get("配股价格"))
                if rights_shares == 0.0 and rights_price == 0.0:
                    continue
                actions.append(
                    CorporateAction(
                        symbol=symbol,
                        ex_date=ex_date,
                        rights_shares=rights_shares,
                        rights_price=rights_price,
                    )
                )
        return actions


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


def _parse_rights_plan(plan: object) -> float:
    """Parse the rights ratio text into per-share shares.

    Args:
        plan: The ``配股方案`` cell, e.g. ``"10配3"``.

    Returns:
        Shares per share, 0.0 when the plan does not parse.
    """
    if not isinstance(plan, str):
        return 0.0
    match = _RIGHTS_PLAN.search(plan)
    if match is None:
        return 0.0
    return float(match.group(1)) / 10
