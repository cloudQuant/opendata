"""Financial indicator fetcher (domain ``financial_indicator``, contract ``FinancialIndicator``).

Wraps ``opendata_http.stock_financial_analysis_indicator_em``
(eastmoney F10 main-finance dataset). The upstream JSON field names
become passthrough ``indicator`` codes - the contract explicitly
carries source codes through until normalized codes land (design
§4.1) - and ``NOTICE_DATE`` supplies the required announce date
that sina's indicator page lacks.

The melt's column vocabulary comes from the 口径映射表
(``mappings/akshare.yaml`` under ``financial_indicator.pivot``): which
columns are the row's report period and announce date, and which is
page metadata rather than an indicator. ``unit`` stays None because the
em dataset mixes 元, 元/股 and % per column and that has never been
measured - the table declares no unit it cannot back.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.mapping import require_pivot
from opendata.data.models import FinancialIndicator
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, em_symbol, plain_symbol


class FinancialIndicatorQuery(QueryParams):
    """Validated query for the financial indicator domain."""

    symbol: str


class AkshareFinancialIndicatorFetcher(Fetcher[FinancialIndicatorQuery, pd.DataFrame]):
    """Main-finance indicators by report period for one symbol."""

    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="financial_indicator",
        period="Q",
        market="cn",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FinancialIndicatorQuery:
        """Build and validate the query (the validate stage).

        Args:
            **kwargs: ``symbol`` (required; plain or suffixed).

        Returns:
            The validated query.
        """
        return FinancialIndicatorQuery.model_validate(kwargs)

    def extract_data(self, params: FinancialIndicatorQuery, ctx: FetchContext) -> pd.DataFrame:
        """Fetch the eastmoney F10 dataset (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context (unused: the em endpoint has no
                timeout parameter of its own).

        Returns:
            The raw dataset frame (uppercase em field columns).

        Raises:
            ValueError: If the dataset lacks the announce-date column
                the 口径映射表 names (the contract requires one).
            RuntimeError: If the 口径映射表 declares no melt for this domain.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        announce_column = require_pivot(SOURCE, "financial_indicator").row_column("announce_date")
        frame = opendata_http.stock_financial_analysis_indicator_em(symbol=em_symbol(params.symbol))
        if announce_column not in frame.columns:
            raise ValueError(
                f"upstream indicator dataset lacks {announce_column}; "
                "the contract requires an announce date (fail closed)"
            )
        return frame

    def transform_data(self, raw: pd.DataFrame, params: FinancialIndicatorQuery) -> FetchResult:
        """Melt the dataset into long contract rows.

        Args:
            raw: Raw upstream frame.
            params: The validated query.

        Returns:
            ``FinancialIndicator`` rows; string metadata columns
            drop out naturally via numeric coercion, and rows
            without report period or announce date are dropped.
        """
        pivot = require_pivot(SOURCE, "financial_indicator")
        indicators_by_column = set(pivot.line_items(raw.columns))
        period_column = pivot.row_column("report_period")
        announce_column = pivot.row_column("announce_date")
        indicators: list[FinancialIndicator] = []
        symbol = plain_symbol(params.symbol)
        for record in raw.to_dict("records"):
            report_period = as_date(record.get(period_column))
            announce_date = as_date(record.get(announce_column))
            if report_period is None or announce_date is None:
                continue
            for field_name, value in record.items():
                if field_name not in indicators_by_column:
                    continue
                numeric = _numeric(value)
                if numeric is None:
                    continue
                indicators.append(
                    FinancialIndicator(
                        symbol=symbol,
                        report_period=report_period,
                        announce_date=announce_date,
                        indicator=field_name,
                        value=numeric,
                    )
                )
        return indicators


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
