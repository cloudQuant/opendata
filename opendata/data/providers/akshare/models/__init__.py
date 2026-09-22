"""Per-domain fetcher modules for the akshare source (design §7.1).

One module per domain: ``Params`` + ``Fetcher``. Imports stay lazy in
``extract_data`` - the ported tree loads only when a fetch actually
routes here.
"""

from opendata.data.providers.akshare.models.financial_indicator import (
    AkshareFinancialIndicatorFetcher,
    FinancialIndicatorQuery,
)
from opendata.data.providers.akshare.models.financial_statement import (
    AkshareFinancialStatementFetcher,
    FinancialStatementQuery,
)
from opendata.data.providers.akshare.models.index_constituent import (
    AkshareIndexConstituentFetcher,
    IndexConstituentQuery,
)
from opendata.data.providers.akshare.models.stock_action import (
    AkshareStockActionFetcher,
    StockActionQuery,
)
from opendata.data.providers.akshare.models.stock_daily import (
    AkshareStockDailyFetcher,
    StockDailyQuery,
)

__all__ = [
    "AkshareFinancialIndicatorFetcher",
    "AkshareFinancialStatementFetcher",
    "AkshareIndexConstituentFetcher",
    "AkshareStockActionFetcher",
    "AkshareStockDailyFetcher",
    "FinancialIndicatorQuery",
    "FinancialStatementQuery",
    "IndexConstituentQuery",
    "StockActionQuery",
    "StockDailyQuery",
]
