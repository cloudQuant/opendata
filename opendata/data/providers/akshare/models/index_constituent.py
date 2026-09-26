"""Index constituent fetcher (domain ``index_constituent``, contract ``IndexConstituent``).

Wraps ``opendata_http.index_stock_cons_weight_csindex`` (the CSI
index close-weight file). ``weight`` is in percent, passed through
as the contract specifies; the exchange disambiguation (Shanghai /
Shenzhen / inter-bank code columns) is already resolved upstream.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import IndexConstituent
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol


class IndexConstituentQuery(QueryParams):
    """Validated query for the index constituent domain."""

    symbol: str


class AkshareIndexConstituentFetcher(
    Fetcher[IndexConstituentQuery, pd.DataFrame]
):
    """Constituents with weights for one CSI index code.

    ``as_of`` is the contract's snapshot date in the strict sense: the CSI
    close-weight file carries the date its own list belongs to, so this leg
    publishes a list date where the ths leg can only publish the observation
    day. It is **not** ``verified``: C34 swept the 21 index codes both sides
    could be asked about and the membership agreed symbol for symbol on the
    nine semiannual families (沪深 300 / 上证 50 / 中证 500 / 中证 1000 / 上证 180
    and four 行业 families) but differed on every 科创板 code, in an equal number
    of members per direction (科创 50: 5 in / 5 out) - the two lists belong to
    two dates, and for a quarterly-reconstituted index that gap is the answer
    itself, not metadata. See ``docs/evidence/C34/``; a flip needs a re-run of
    that sweep with no mismatch left.
    """

    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="index_constituent",
        period="snapshot",
        market="cn",
        source=SOURCE,
        verified=False,
        notes="CSI close-weight file; as_of is the list's own data date",
    )

    def transform_query(self, **kwargs: object) -> IndexConstituentQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required, the index code, e.g.
                ``000300``).

        Returns:
            The validated query.
        """
        return IndexConstituentQuery.model_validate(kwargs)

    def extract_data(
        self, params: IndexConstituentQuery, ctx: FetchContext
    ) -> pd.DataFrame:
        """Fetch the CSI close-weight file (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context (unused: the weight file uses a
                fixed upstream timeout).

        Returns:
            The upstream frame with resolved constituent codes.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.index_stock_cons_weight_csindex(
            symbol=plain_symbol(params.symbol)
        )

    def transform_data(
        self, raw: pd.DataFrame, params: IndexConstituentQuery
    ) -> FetchResult:
        """Normalize the weight file into contract rows.

        Args:
            raw: Upstream frame with the columns ``日期`` /
                ``指数代码`` / ``成分券代码`` / ``权重``.
            params: The validated query.

        Returns:
            ``IndexConstituent`` rows; rows without an as-of date
            or a constituent code are dropped.
        """
        constituents: list[IndexConstituent] = []
        index_symbol = plain_symbol(params.symbol)
        for record in raw.to_dict("records"):
            as_of = as_date(record.get("日期"))
            code = record.get("成分券代码")
            if as_of is None or code is None or (isinstance(code, float) and pd.isna(code)):
                continue
            constituents.append(
                IndexConstituent(
                    index_symbol=str(record.get("指数代码") or index_symbol),
                    symbol=plain_symbol(str(code)),
                    as_of=as_of,
                    weight=_numeric(record.get("权重")),
                )
            )
        return constituents


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
