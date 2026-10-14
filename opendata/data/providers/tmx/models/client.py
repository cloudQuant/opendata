"""Governed GraphQL transport for the public TMX Money web application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

GRAPHQL_URL: Final = "https://app-money.tmx.com/graphql"
DEFAULT_TIMEOUT_SECONDS: Final = 25.0
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
MAX_RESPONSE_ROWS: Final = 10_000
MAX_DIVIDEND_PAGES: Final = 100
DIVIDEND_PAGE_SIZE: Final = 10


class TmxProviderError(RuntimeError):
    """Sanitized stable failure from TMX transport or a malformed response."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Create a stable provider error with an optional HTTP status."""
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


@dataclass(frozen=True)
class GraphqlReply:
    """Data section of a validated GraphQL response."""

    data: Mapping[str, object]


_OPERATIONS: Final[dict[str, tuple[str, str]]] = {
    "findTmxCompaniesByKeywords": (
        "findTmxCompaniesByKeywords",
        """query findTmxCompaniesByKeywords($keywords: String) {
          findTmxCompaniesByKeywords(keywords: $keywords) {
            symbol name exchange instrumentType
          }
        }""",
    ),
    "getCompanyPriceHistory": (
        "getCompanyPriceHistory",
        """query getCompanyPriceHistory(
          $symbol: String!, $start: String, $end: String, $adjusted: Boolean,
          $adjustmentType: String, $unadjusted: Boolean, $limit: Int
        ) {
          getCompanyPriceHistory(
            symbol: $symbol, start: $start, end: $end, adjusted: $adjusted,
            adjustmentType: $adjustmentType, unadjusted: $unadjusted, limit: $limit
          ) {
            datetime openPrice closePrice high low volume tradeValue numberOfTrade
            change changePercent vwap
          }
        }""",
    ),
    "getQuoteBySymbol": (
        "getQuoteBySymbol",
        """query getQuoteBySymbol($symbol: String, $locale: String) {
          getQuoteBySymbol(symbol: $symbol, locale: $locale) {
            symbol name price priceChange percentChange exchangeName exchangeCode
            sector industry volume openPrice dayHigh dayLow MarketCap MarketCapAllClasses
            peRatio prevClose dividendFrequency dividendYield dividendAmount
            dividendCurrency beta eps exDividendDate longDescription website email
            phoneNumber fullAddress employees shareOutStanding totalDebtToEquity
            totalSharesOutStanding sharesESCROW issueType vwap dividendPayDate weeks52high
            weeks52low currency
          }
        }""",
    ),
    "getCompanyFilings": (
        "filings",
        """query getCompanyFilings(
          $symbol: String!, $fromDate: String, $toDate: String, $limit: Int
        ) {
          filings: getCompanyFilings(
            symbol: $symbol, fromDate: $fromDate, toDate: $toDate, limit: $limit
          ) { size filingDate description urlToPdf }
        }""",
    ),
    "getDividendsForSymbol": (
        "dividendHistory",
        """query getDividendsForSymbol($symbol: String!, $page: Int, $batch: Int) {
          dividendHistory: getDividendsForSymbol(
            symbol: $symbol, page: $page, batch: $batch
          ) {
            pageNumber hasNextPage
            dividends { exDate amount currency payableDate declarationDate recordDate }
          }
        }""",
    ),
    "getIndexConstituents": (
        "constituents",
        """query getIndexConstituents($symbol: String!) {
          constituents: getIndexConstituents(symbol: $symbol) {
            symbol longName quotedMarketValue weight exchange
          }
        }""",
    ),
    "getCompanyAnalysts": (
        "getCompanyAnalysts",
        """query getCompanyAnalysts($symbol: String!, $datatype: String) {
          getCompanyAnalysts(symbol: $symbol, datatype: $datatype) {
            totalAnalysts
            priceTarget { lowPriceTarget highPriceTarget priceTarget priceTargetUpside }
            consensusAnalysts { consensus buy sell hold }
          }
        }""",
    ),
}


def graphql(
    operation_name: str,
    variables: Mapping[str, object],
    *,
    timeout: float | None = None,
) -> GraphqlReply:
    """Run one allow-listed TMX operation using request-budget admission."""
    operation = _OPERATIONS.get(operation_name)
    if operation is None:
        raise TmxProviderError("TMX_OPERATION_NOT_ALLOWED")
    root_field, query = operation
    try:
        response = get_shared_http_client().request(
            "POST",
            GRAPHQL_URL,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=(
                DEFAULT_TIMEOUT_SECONDS
                if timeout is None
                else min(timeout, DEFAULT_TIMEOUT_SECONDS)
            ),
            source="tmx",
            json_body={
                "operationName": operation_name,
                "query": query,
                "variables": dict(variables),
            },
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise TmxProviderError(_http_code(exc.status), status=exc.status) from None
    except Exception:
        raise TmxProviderError("TMX_TRANSPORT_ERROR") from None

    status = response.status_code
    if not 200 <= status < 300:
        raise TmxProviderError(_http_code(status), status=status)
    body = response.content
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise TmxProviderError("TMX_RESPONSE_TOO_LARGE", status=status)
    try:
        document: object = response.json()
    except Exception:
        raise TmxProviderError("TMX_BAD_JSON", status=status) from None
    if not isinstance(document, dict) or "errors" in document:
        raise TmxProviderError("TMX_GRAPHQL_ERROR", status=status)
    data = document.get("data")
    if not isinstance(data, dict) or root_field not in data:
        raise TmxProviderError("TMX_BAD_ENVELOPE", status=status)
    return GraphqlReply(data=data)


def list_rows(value: object) -> list[Mapping[str, object]]:
    """Require a bounded list of JSON objects and preserve empty responses."""
    if not isinstance(value, list) or len(value) > MAX_RESPONSE_ROWS:
        raise TmxProviderError("TMX_BAD_ROWS")
    if any(not isinstance(row, dict) for row in value):
        raise TmxProviderError("TMX_BAD_ROWS")
    return value


def _http_code(status: int | None) -> str:
    if status == 429:
        return "TMX_RATE_LIMITED"
    if status in {401, 403}:
        return "TMX_ACCESS_ERROR"
    if status is not None and status >= 500:
        return "TMX_UPSTREAM_ERROR"
    return "TMX_HTTP_ERROR"
