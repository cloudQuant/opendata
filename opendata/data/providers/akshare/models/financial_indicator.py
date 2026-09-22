"""Financial indicator fetcher (domain ``financial_indicator``, contract ``FinancialIndicator``).

Wraps ``opendata_http.stock_financial_analysis_indicator_em``
(eastmoney F10 main-finance dataset). The upstream JSON field names
become passthrough ``indicator`` codes - the contract explicitly
carries source codes through until normalized codes land (design
§4.1) - and ``NOTICE_DATE`` supplies the required announce date
that sina's indicator page lacks.
"""

from typing import ClassVar

import pandas as pd

from opendata.data.capability import Capability
from opendata.data.models import FinancialIndicator
from opendata.data.protocol import FetchContext, FetchResult, Fetcher, QueryParams
from opendata.data.providers.akshare._source import SOURCE
from opendata.data.providers.akshare.models._normalize import as_date, em_symbol, plain_symbol

#: Dimension columns consumed as row keys, never melted into items.
_DIMENSION_COLUMNS = frozenset({"REPORT_DATE", "NOTICE_DATE", "SECUCODE"})


class FinancialIndicatorQuery(QueryParams):
    """Validated query for the financial indicator domain."""

    symbol: str


class AkshareFinancialIndicatorFetcher(
    Fetcher[FinancialIndicatorQuery, pd.DataFrame]
):
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

    def extract_data(
        self, params: FinancialIndicatorQuery, ctx: FetchContext
    ) -> pd.DataFrame:
        """Fetch the eastmoney F10 dataset (the fetch_raw stage).

        Args:
            params: Validated query.
            ctx: Per-call context (unused: the em endpoint has no
                timeout parameter of its own).

        Returns:
            The raw dataset frame (uppercase em field columns).

        Raises:
            ValueError: If the dataset lacks the NOTICE_DATE
                column required by the contract.
        """
        import opendata_http  # lazy: load the ported tree on routing only

        frame = opendata_http.stock_financial_analysis_indicator_em(
            symbol=em_symbol(params.symbol)
        )
        if "NOTICE_DATE" not in frame.columns:
            raise ValueError(
                "upstream indicator dataset lacks NOTICE_DATE; "
                "the contract requires an announce date (fail closed)"
            )
        return frame

    def transform_data(
        self, raw: pd.DataFrame, params: FinancialIndicatorQuery
    ) -> FetchResult:
        """Melt the dataset into long contract rows.

        Args:
            raw: Raw upstream frame.
            params: The validated query.

        Returns:
            ``FinancialIndicator`` rows; string metadata columns
            drop out naturally via numeric coercion, and rows
            without report period or announce date are dropped.
        """
        indicators: list[FinancialIndicator] = []
        symbol = plain_symbol(params.symbol)
        for record in raw.to_dict("records"):
            report_period = as_date(record.get("REPORT_DATE"))
            announce_date = as_date(record.get("NOTICE_DATE"))
            if report_period is None or announce_date is None:
                continue
            for field_name, value in record.items():
                if field_name in _DIMENSION_COLUMNS or not isinstance(field_name, str):
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
