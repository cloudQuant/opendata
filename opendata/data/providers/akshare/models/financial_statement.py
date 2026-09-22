"""Financial statement fetcher (domain ``financial_statement``, contract ``FinancialStatement``).

Wraps ``opendata_http.stock_financial_report_sina`` (sina's three
statements). The upstream frame is wide (rows = report periods,
columns = line items plus page metadata); ``normalize()`` melts it
into long ``item`` / ``value`` rows and takes the announce date from
the per-row ``公告日期`` column.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import FinancialStatement
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, plain_symbol, sina_symbol

#: Columns of the wide frame that are page metadata, not line items.
_METADATA_COLUMNS = frozenset(
    {"数据源", "是否审计", "公告日期", "币种", "类型", "更新日期", "报告日"}
)


class FinancialStatementQuery(QueryParams):
    """Validated query for the financial statement domain."""

    symbol: str
    statement_type: str = "资产负债表"


class AkshareFinancialStatementFetcher(
    Fetcher[FinancialStatementQuery, pd.DataFrame]
):
    """One of sina's three statements for one A-share symbol."""

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="financial_statement",
        period="Q",
        market="cn",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FinancialStatementQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required) and ``statement_type``
                in ``{"资产负债表", "利润表", "现金流量表"}``.

        Returns:
            The validated query.
        """
        return FinancialStatementQuery.model_validate(kwargs)

    def extract_data(
        self, params: FinancialStatementQuery, ctx: FetchContext
    ) -> pd.DataFrame:
        """Fetch the sina statement page (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context (unused: sina has no timeout).

        Returns:
            The wide upstream frame.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        return opendata_http.stock_financial_report_sina(
            stock=sina_symbol(params.symbol), symbol=params.statement_type
        )

    def transform_data(
        self, raw: pd.DataFrame, params: FinancialStatementQuery
    ) -> FetchResult:
        """Melt the wide statement into long contract rows.

        Args:
            raw: Wide upstream frame (rows = report periods).
            params: The validated query.

        Returns:
            ``FinancialStatement`` rows; rows without a report
            period, announce date or numeric value are dropped.
        """
        items = [name for name in raw.columns if name not in _METADATA_COLUMNS]
        statements: list[FinancialStatement] = []
        symbol = plain_symbol(params.symbol)
        for record in raw.to_dict("records"):
            report_period = as_date(record.get("报告日"))
            announce_date = as_date(record.get("公告日期"))
            if report_period is None or announce_date is None:
                continue
            for item in items:
                value = _numeric(record.get(item))
                if value is None:
                    continue
                statements.append(
                    FinancialStatement(
                        symbol=symbol,
                        statement_type=params.statement_type,
                        report_period=report_period,
                        announce_date=announce_date,
                        item=str(item),
                        value=value,
                    )
                )
        return statements


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
