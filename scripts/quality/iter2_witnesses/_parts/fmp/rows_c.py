"""Wave 9 shard: the sixteen FMP disclosure rows the base witness module leaves unwitnessed.

Every leg here goes through ``opendata.data.providers.fmp.models._client.fetch_array(endpoint,
request, timeout)`` with no ``transport`` argument -- ``iter2_disclosures.py`` never passes the
engine seam -- so the governed ``get_shared_http_client()`` is the only thing that can answer, and
the runner's socket circuit breaker would refuse it. Each entry therefore declares a
``client_seam`` over ``models/_client.py`` (the one module that holds that name, imported at
``_client.py:12``) and publishes a ``{"responses": [...]}`` envelope keyed by the endpoint path.
GovernmentTrades is the two-route case that motivates the envelope:
``_read_two_chamber_trades`` reads ``house-trades`` and ``senate-trades`` and tags each row with
the chamber that answered, so one fixed body could not tell the two endpoints apart.

The refusal family is the disclosure contract's own: ``_DisclosureFetcher.transform_data`` funnels
``ValidationError``/``TypeError``/``ValueError`` into ``FMPProviderError("FMP_BAD_SHAPE")`` and the
requested-symbol cross-check into ``FMP_WRONG_SYMBOL``. Neither carries an HTTP status nor a column
attribution, so no entry below declares ``expected_missing``/``expected_invalid`` faces; the cell
each tamper breaks is named in ``tamper_reason`` instead. The shape mechanisms do differ row by
row, and each entry exercises the one its own contract declares -- strict ``int`` counts, finite
JSON numbers, ISO dates with no time, ISO timestamps with a time, strict ``bool`` flags, text-typed
source amounts, and the shared non-blank text validator.

    $ PYTHONPATH=. python -c "from opendata.data.models.fmp_disclosures import *"  (row classes read
      from each fetcher's ``row_model``, listed in the MODELS keys below)
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"
_SOURCE_DISCLOSURE = ["opendata/data/providers/fmp/models/iter2_disclosures.py", _FMP_SOURCE]
_CONTRACTS_DISCLOSURE = ["opendata/data/models/fmp_disclosures.py", "opendata/data/domains.yaml"]

_SEAM: Final = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.fmp.models._client"],
    "attr": "get_shared_http_client",
}


def _envelope(*, route: str, body: Any) -> dict[str, Any]:  # noqa: ANN401  # one route's JSON body
    """Serve one documented FMP stable route from the offline client seam."""
    return {"responses": [{"url_contains": f"/stable/{route}", "status": 200, "body": body}]}


def _news_row(symbol: str | None, title: str, url: str, published: str) -> dict[str, Any]:
    """One news record as the stock/general news routes publish it."""
    row: dict[str, Any] = {
        "title": title,
        "image": "https://example.com/wire/thumb.png",
        "publishedDate": published,
        "url": url,
        "publisher": "Example Wire",
        "site": "finance",
        "text": (
            "The company reported results this session and said it expects the current "
            "product cycle to continue through the next fiscal year."
        ),
    }
    if symbol is not None:
        row["symbol"] = symbol
    return row


def _company_news_document() -> dict[str, Any]:
    """Two stock-news pages-worth of rows for one ticker (the page stops when short)."""
    return _envelope(
        route="news/stock",
        body=[
            _news_row(
                "AAPL",
                "Apple supplier lifts component order for the spring cycle",
                "https://example.com/wire/apple-supplier-order",
                "2026-01-15T14:32:00Z",
            ),
            _news_row(
                "AAPL",
                "Apple buys stake in silicon photonics startup",
                "https://example.com/wire/apple-photonics-stake",
                "2026-01-14T09:05:00Z",
            ),
        ],
    )


def _world_news_document() -> dict[str, Any]:
    """Two general-news rows; the route carries no ticker identity of its own."""
    return _envelope(
        route="news/general-latest",
        body=[
            _news_row(
                None,
                "Fed minutes leave the January rate decision open",
                "https://example.com/wire/fed-minutes-january",
                "2026-01-15T21:00:00Z",
            ),
            _news_row(
                None,
                "Container freight rates fall for a fourth week",
                "https://example.com/wire/freight-rates-weekly",
                "2026-01-15T06:40:00Z",
            ),
        ],
    )


def _company_filings_document() -> dict[str, Any]:
    """Two SEC filing records inside the requested 31-day window."""
    return _envelope(
        route="sec-filings-search/symbol",
        body=[
            {
                "symbol": "AAPL",
                "cik": "0000320193",
                "formType": "8-K",
                "filingDate": "2026-01-09 17:41:12",
                "acceptedDate": "2026-01-09 17:42:03",
                "link": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000009/",
                "finalLink": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000009/"
                    "0000320193-26-000009-index.htm"
                ),
                "hasFinancials": False,
            },
            {
                "symbol": "AAPL",
                "cik": "0000320193",
                "formType": "10-Q",
                "filingDate": "2026-01-30 06:12:44",
                "acceptedDate": "2026-01-30 06:14:02",
                "link": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000030/",
                "finalLink": (
                    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000030/"
                    "0000320193-26-000030-index.htm"
                ),
                "hasFinancials": True,
            },
        ],
    )


def _historical_dividends_document() -> dict[str, Any]:
    """Three dividend records, including the empty-string date the route sends pre-declaration."""
    return _envelope(
        route="dividends",
        body=[
            {
                "symbol": "AAPL",
                "date": "2025-11-10",
                "recordDate": "2025-11-14",
                "paymentDate": "2025-11-16",
                "declarationDate": "2025-10-30",
                "dividend": 0.25,
                "adjDividend": 0.25,
                "yield": 0.42,
                "frequency": "quarterly",
            },
            {
                "symbol": "AAPL",
                "date": "2026-02-09",
                "recordDate": "2026-02-13",
                "paymentDate": "2026-02-19",
                "declarationDate": "",
                "dividend": 0.26,
                "adjDividend": 0.26,
                "yield": 0.41,
                "frequency": "quarterly",
            },
        ],
    )


def _historical_splits_document() -> dict[str, Any]:
    """One 4-for-5 split record and one reverse split, as the route spells the ratio cells."""
    return _envelope(
        route="splits",
        body=[
            {
                "symbol": "NVDA",
                "date": "2024-06-07",
                "numerator": 1,
                "denominator": 10,
                "splitType": "Reverse Split",
            },
            {
                "symbol": "NVDA",
                "date": "2021-07-20",
                "numerator": 4,
                "denominator": 5,
                "splitType": "Split",
            },
        ],
    )


def _historical_eps_document() -> dict[str, Any]:
    """Two earnings records for the requested ticker."""
    return _envelope(
        route="earnings",
        body=[
            {
                "symbol": "AAPL",
                "date": "2025-10-30",
                "epsActual": 1.64,
                "epsEstimated": 1.57,
                "revenueActual": 124_300_000_000,
                "revenueEstimated": 121_900_000_000,
                "lastUpdated": "2026-01-12",
            },
            {
                "symbol": "AAPL",
                "date": "2026-01-29",
                "epsActual": 2.11,
                "epsEstimated": 1.94,
                "revenueActual": 136_800_000_000,
                "revenueEstimated": 134_200_000_000,
                "lastUpdated": "2026-01-30",
            },
        ],
    )


def _historical_employees_document() -> dict[str, Any]:
    """One annual headcount record with its filing lineage."""
    return _envelope(
        route="historical-employee-count",
        body=[
            {
                "symbol": "AAPL",
                "cik": "0000320193",
                "companyName": "Apple Inc.",
                "formType": "10-K",
                "filingDate": "2025-11-01",
                "periodOfReport": "2025-09-27",
                "acceptanceTime": "2025-11-01 18:15:43",
                "employeeCount": 161_000,
                "source": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000011/",
            }
        ],
    )


def _historical_market_cap_document() -> dict[str, Any]:
    """Three daily market-capitalization points for the requested ticker."""
    return _envelope(
        route="historical-market-capitalization",
        body=[
            {"symbol": "AAPL", "date": "2026-01-13", "marketCap": 3_741_000_000_000},
            {"symbol": "AAPL", "date": "2026-01-14", "marketCap": 3_765_000_000_000},
            {"symbol": "AAPL", "date": "2026-01-15", "marketCap": 3_702_000_000_000},
        ],
    )


def _price_target_document() -> dict[str, Any]:
    """Two analyst price-target stories for the requested ticker."""
    return _envelope(
        route="price-target-news",
        body=[
            {
                "symbol": "NVDA",
                "publishedDate": "2026-01-15T12:05:00Z",
                "analystName": "Morgan Stanley",
                "priceTarget": 215.0,
                "adjPriceTarget": 215.0,
                "priceWhenPosted": 174.32,
                "newsPublisher": "MarketWire",
                "newsURL": "https://example.com/targets/nvda-morgan-stanley",
                "newsBaseURL": "https://example.com/targets",
                "newsTitle": "Morgan Stanley lifts NVDA target on data-center demand",
                "analystCompany": "Morgan Stanley",
            },
            {
                "symbol": "NVDA",
                "publishedDate": "2026-01-08T09:41:00Z",
                "analystName": "Piper Sandler",
                "priceTarget": 198.0,
                "adjPriceTarget": 198.0,
                "priceWhenPosted": 168.9,
                "newsPublisher": "StreetNews",
                "newsURL": "https://example.com/targets/nvda-piper-sandler",
                "newsBaseURL": "https://example.com/targets",
                "newsTitle": "Piper Sandler keeps NVDA at Overweight",
                "analystCompany": "Piper Sandler",
            },
        ],
    )


def _price_target_consensus_document() -> dict[str, Any]:
    """The single consensus row the route returns for one ticker."""
    return _envelope(
        route="price-target-consensus",
        body=[
            {
                "symbol": "MSFT",
                "targetHigh": 585.0,
                "targetLow": 410.0,
                "targetConsensus": 512.4,
                "targetMedian": 505.0,
            }
        ],
    )


def _key_executives_document() -> dict[str, Any]:
    """Two executive records; the route is not required to echo the ticker."""
    return _envelope(
        route="key-executives",
        body=[
            {
                "title": "Chief Executive Officer",
                "name": "Timothy D. Cook",
                "pay": 17_351_027,
                "currencyPay": "USD",
                "gender": "M",
                "yearBorn": 1960,
                "titleSince": "2011-08-24",
                "active": True,
            },
            {
                "title": "Senior Vice President, Chief Financial Officer",
                "name": "Kevan Parekh",
                "pay": 4_629_013,
                "currencyPay": "USD",
                "gender": "M",
                "yearBorn": 1975,
                "titleSince": "2025-01-01",
                "active": True,
            },
        ],
    )


def _executive_compensation_document() -> dict[str, Any]:
    """One compensation record per executive for the requested company."""
    return _envelope(
        route="governance-executive-compensation",
        body=[
            {
                "cik": "0000320193",
                "symbol": "AAPL",
                "companyName": "Apple Inc.",
                "nameAndPosition": "Timothy D. Cook, Chief Executive Officer",
                "year": 2025,
                "salary": 1_000_000,
                "bonus": 6_090_560,
                "stockAward": 9_424_066,
                "optionAward": 0,
                "incentivePlanCompensation": 0,
                "allOtherCompensation": 836_401,
                "total": 17_351_027,
                "filingDate": "2025-11-01",
                "acceptedDate": "2025-11-01 18:15:43",
                "link": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000011/",
            },
            {
                "cik": "0000320193",
                "symbol": "AAPL",
                "companyName": "Apple Inc.",
                "nameAndPosition": "Kevan Parekh, Senior Vice President, CFO",
                "year": 2025,
                "salary": 754_492,
                "bonus": 1_994_077,
                "stockAward": 1_733_353,
                "optionAward": 0,
                "incentivePlanCompensation": 0,
                "allOtherCompensation": 147_091,
                "total": 4_629_013,
                "filingDate": "2025-11-01",
                "acceptedDate": "2025-11-01 18:15:43",
                "link": "https://www.sec.gov/Archives/edgar/data/320193/000032019325000011/",
            },
        ],
    )


def _trade_row(
    symbol: str,
    member_id: str,
    first: str,
    last: str,
    office: str,
    district: str,
    day: str,
    asset: str,
    trade_type: str,
    amount: str,
    link: str,
) -> dict[str, Any]:
    """One disclosure record as a chamber endpoint publishes it (no chamber cell of its own)."""
    return {
        "symbol": symbol,
        "senateID": member_id,
        "firstName": first,
        "lastName": last,
        "office": office,
        "district": district,
        "disclosureDate": day,
        "transactionDate": day,
        "owner": "Joint",
        "assetDescription": asset,
        "assetType": "stock",
        "type": trade_type,
        "amount": amount,
        "comment": "Purchase of a listed equity security reported on the chamber form.",
        "capitalGainsOver200USD": "Yes",
        "link": link,
    }


def _government_trades_document() -> dict[str, Any]:
    """The two chamber routes, each answering with a short page for the requested ticker."""
    return {
        "responses": [
            {
                "url_contains": "/stable/house-trades",
                "status": 200,
                "body": [
                    _trade_row(
                        "AAPL",
                        "H000123",
                        "Sam",
                        "Aurrent",
                        "Rep.",
                        "ME-01",
                        "2026-01-09",
                        "Apple Inc.",
                        "purchase",
                        "$10001 - $15000",
                        "https://www.clerk.house.gov/trades/2026/000123",
                    )
                ],
            },
            {
                "url_contains": "/stable/senate-trades",
                "status": 200,
                "body": [
                    _trade_row(
                        "AAPL",
                        "S000775",
                        "Mark",
                        "Kelly",
                        "Sen.",
                        "AZ",
                        "2026-01-12",
                        "Apple Inc.",
                        "sale",
                        "$100001 - $250000",
                        "https://www.senate.gov/financial/2026/000775",
                    )
                ],
            },
        ]
    }


def _insider_trading_document() -> dict[str, Any]:
    """Two Form 4 records for the requested ticker on the paged search route."""
    return _envelope(
        route="insider-trading/search",
        body=[
            {
                "symbol": "NVDA",
                "securityName": "NVDA",
                "filingDate": "2026-01-13",
                "transactionDate": "2026-01-09",
                "reportingName": "JENSEN HUANG",
                "reportingCik": "0001045779",
                "companyCik": "0001045810",
                "transactionType": "M",
                "securitiesOwned": 734_802_651,
                "securitiesTransacted": 200_000,
                "price": 121.34,
                "typeOfOwner": "D",
                "acquisitionOrDisposition": "A",
                "directOrIndirect": "D",
                "formType": "4",
                "url": "https://www.sec.gov/Archives/edgar/data/1045810/000104577926000009/",
            },
            {
                "symbol": "NVDA",
                "securityName": "NVDA",
                "filingDate": "2026-01-14",
                "transactionDate": "2026-01-10",
                "reportingName": "COLETTE KRESS",
                "reportingCik": "0001573166",
                "companyCik": "0001045810",
                "transactionType": "S",
                "securitiesOwned": 1_120_000,
                "securitiesTransacted": 40_000,
                "price": 119.8,
                "typeOfOwner": "D",
                "acquisitionOrDisposition": "D",
                "directOrIndirect": "D",
                "formType": "4",
                "url": "https://www.sec.gov/Archives/edgar/data/1045810/000157316626000010/",
            },
        ],
    )


def _institutional_ownership_document() -> dict[str, Any]:
    """The one quarterly symbol-position summary row the route returns."""
    return _envelope(
        route="institutional-ownership/symbol-positions-summary",
        body=[
            {
                "symbol": "AAPL",
                "cik": "0000320193",
                "date": "2025-12-31",
                "investorsHolding": 8_412,
                "lastInvestorsHolding": 8_274,
                "investorsHoldingChange": 138,
                "numberOf13Fshares": 7_912_400_000,
                "lastNumberOf13Fshares": 7_801_300_000,
                "numberOf13FsharesChange": 111_100_000,
                "totalInvested": 3_512_400_000_000,
                "lastTotalInvested": 3_401_900_000_000,
                "totalInvestedChange": 110_500_000_000,
                "ownershipPercent": 48.12,
                "lastOwnershipPercent": 47.55,
                "ownershipPercentChange": 0.57,
                "newPositions": 112,
                "lastNewPositions": 98,
                "newPositionsChange": 14,
                "increasedPositions": 1_204,
                "lastIncreasedPositions": 1_166,
                "increasedPositionsChange": 38,
                "closedPositions": 74,
                "lastClosedPositions": 81,
                "closedPositionsChange": -7,
                "reducedPositions": 618,
                "lastReducedPositions": 640,
                "reducedPositionsChange": -22,
                "totalCalls": 12_400_000,
                "lastTotalCalls": 11_900_000,
                "totalCallsChange": 500_000,
                "totalPuts": 8_300_000,
                "lastTotalPuts": 8_150_000,
                "totalPutsChange": 150_000,
                "putCallRatio": 0.67,
                "lastPutCallRatio": 0.68,
                "putCallRatioChange": -0.01,
            }
        ],
    )


def _equity_ownership_document() -> dict[str, Any]:
    """Two Schedule 13D/G records; every numeric-looking cell is source text here."""
    return _envelope(
        route="acquisition-of-beneficial-ownership",
        body=[
            {
                "cik": "0001067983",
                "symbol": "AAPL",
                "cusip": "037833100",
                "filingDate": "2026-01-08",
                "acceptedDate": "2026-01-08",
                "nameOfReportingPerson": "Berkshire Hathaway Inc.",
                "citizenshipOrPlaceOfOrganization": "DE",
                "soleVotingPower": "420,000,000",
                "sharedVotingPower": "0",
                "soleDispositivePower": "420,000,000",
                "sharedDispositivePower": "0",
                "amountBeneficiallyOwned": "420,000,000",
                "percentOfClass": "2.85",
                "typeOfReportingPerson": "CI",
                "url": "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000004/",
            },
            {
                "cik": "0001067983",
                "symbol": "AAPL",
                "cusip": "037833100",
                "filingDate": "2026-01-16",
                "acceptedDate": "2026-01-16",
                "nameOfReportingPerson": "Berkshire Hathaway Inc.",
                "citizenshipOrPlaceOfOrganization": "DE",
                "soleVotingPower": "418,500,000",
                "sharedVotingPower": "0",
                "soleDispositivePower": "418,500,000",
                "sharedDispositivePower": "0",
                "amountBeneficiallyOwned": "418,500,000",
                "percentOfClass": "2.84",
                "typeOfReportingPerson": "CI",
                "url": "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000011/",
            },
        ],
    )


def _entry(
    *,
    fixture: Any,  # noqa: ANN401  # fixture builder callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    code: str,
    reason: str,
    responses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble one disclosure-row witness entry over the governed-client seam."""
    document = fixture() if responses is None else fixture
    return {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": (
            fixture if responses is None else lambda _r=responses: {"responses": _r}
        ),
        "query": query,
        "tamper": tamper,
        "refusal_code": code,
        "tamper_reason": reason,
        "source_files": _SOURCE_DISCLOSURE,
        "contract_files": _CONTRACTS_DISCLOSURE,
    }


MODELS: Final[dict[str, Any]] = {
    "CompanyNews": _entry(
        fixture=_company_news_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "publishedDate"],
            "value": "2026-01-15",
            "column": "published_date",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the stock-news route stamps every story with a publication instant and the contract "
            "declares published_date as a datetime; a bare date carries no time, so the shared "
            "timestamp parser must refuse the story with FMP_BAD_SHAPE rather than invent a "
            "midnight publish time"
        ),
    ),
    "WorldNews": _entry(
        fixture=_world_news_document,
        query={},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 1, "title"],
            "value": "   ",
            "column": "title",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "title is a required headline cell and the shared text validator rejects whitespace-only "
            "source text rather than trimming it into an empty string; a blank headline is not a "
            "story the feed can name, so the row must be refused with FMP_BAD_SHAPE"
        ),
    ),
    "CompanyFilings": _entry(
        fixture=_company_filings_document,
        query={"symbol": "AAPL", "start_date": "2026-01-01", "end_date": "2026-01-31"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 1, "hasFinancials"],
            "value": "true",
            "column": "has_financials",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the filing route states hasFinancials as a JSON boolean and the contract keeps it "
            "strictly bool; the text 'true' is not the flag, so the validator must refuse with "
            "FMP_BAD_SHAPE instead of letting a truthy string decide whether a filing carries "
            "financial statements"
        ),
    ),
    "HistoricalDividends": _entry(
        fixture=_historical_dividends_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 1, "dividend"],
            "value": "0.26",
            "column": "dividend",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "dividend is a per-share amount the contract reads as a finite JSON number; a quoted "
            "decimal string is a different cell, and accepting it would let a formatted amount "
            "stand in for the declared one, so the row must be refused with FMP_BAD_SHAPE"
        ),
    ),
    "HistoricalSplits": _entry(
        fixture=_historical_splits_document,
        query={"symbol": "NVDA"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 1, "numerator"],
            "value": 4.0,
            "column": "numerator",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "a split ratio is a pair of integer cells and the contract declares numerator as a "
            "strict int (type(value) is int); 4.0 is a float, so the shared integer guard must "
            "refuse the ratio with FMP_BAD_SHAPE rather than round it into a split"
        ),
    ),
    "HistoricalEps": _entry(
        fixture=_historical_eps_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "date"],
            "value": "2025-10-30T00:00:00",
            "column": "date",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the earnings route dates each print with a calendar day and the contract refuses a "
            "time component on a date field; a timestamped cell is not the ISO date the natural "
            "key is built from, so the parser must raise FMP_BAD_SHAPE"
        ),
    ),
    "HistoricalEmployees": _entry(
        fixture=_historical_employees_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "drop",
            "path": ["responses", 0, "body", 0, "employeeCount"],
            "column": "employee_count",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the whole point of the headcount route is employeeCount, declared as a required "
            "strict int; a filing record without it cannot state a period headcount, so validation "
            "must refuse the row with FMP_BAD_SHAPE instead of publishing a blank count"
        ),
    ),
    "HistoricalMarketCap": _entry(
        fixture=_historical_market_cap_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 2, "symbol"],
            "value": "MSFT",
            "column": "symbol",
        },
        code="FMP_WRONG_SYMBOL",
        reason=(
            "historical-market-capitalization is asked for one ticker, and the disclosure "
            "transformer compares every row's symbol against the request before validating; a row "
            "answering MSFT inside an AAPL request would silently join another issuer's capitaliza"
            "tion series, so it must be refused with FMP_WRONG_SYMBOL"
        ),
    ),
    "PriceTarget": _entry(
        fixture=_price_target_document,
        query={"symbol": "NVDA"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "priceTarget"],
            "value": True,
            "column": "price_target",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "priceTarget is a stated target price in the contract's finite-number cells, and the "
            "shared numeric guard refuses a boolean outright rather than letting True become 1; a "
            "flag where an amount belongs must raise FMP_BAD_SHAPE"
        ),
    ),
    "PriceTargetConsensus": _entry(
        fixture=_price_target_consensus_document,
        query={"symbol": "MSFT"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "targetMedian"],
            "value": "505",
            "column": "target_median",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the consensus route publishes the four target statistics as JSON numbers and the "
            "contract declares target_median among its finite-number cells; a quoted numeral is a "
            "different cell type, so the row must be refused with FMP_BAD_SHAPE"
        ),
    ),
    "KeyExecutives": _entry(
        fixture=_key_executives_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "symbol"],
            "value": "MSFT",
            "column": "symbol",
        },
        code="FMP_WRONG_SYMBOL",
        reason=(
            "key-executives answers without a ticker, so KeyExecutivesFetcher.transform_data "
            "(iter2_disclosures.py:755-770) first rejects any row that names a different issuer and "
            "only then stamps the requested symbol on the officer list; a row already carrying MSFT "
            "would have Berkshire's officers republished under AAPL, which the guard refuses with "
            "FMP_WRONG_SYMBOL before the contract ever sees the cell"
        ),
    ),
    "ExecutiveCompensation": _entry(
        fixture=_executive_compensation_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "year"],
            "value": "2025",
            "column": "year",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the compensation summary is organized by a required strict-int fiscal year, and the "
            "shared integer guard checks type(value) is int; the text '2025' is not that integer, "
            "so the row must be refused with FMP_BAD_SHAPE rather than coerced into a period"
        ),
    ),
    "GovernmentTrades": _entry(
        fixture=_government_trades_document,
        query={"symbol": "AAPL"},
        responses=_government_trades_document()["responses"],
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "disclosureDate"],
            "value": "2026/01/09",
            "column": "disclosure_date",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "each chamber route dates its disclosure with an ISO calendar day and the contract "
            "requires disclosure_date as such; a slash-formatted cell is not an ISO date, so the "
            "row must be refused with FMP_BAD_SHAPE instead of guessing the field order"
        ),
    ),
    "InsiderTrading": _entry(
        fixture=_insider_trading_document,
        query={"symbol": "NVDA"},
        tamper={
            "op": "drop",
            "path": ["responses", 0, "body", 1, "reportingCik"],
            "column": "reporting_cik",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "a Form 4 row is filed by an identified insider, and reporting_cik is a required text "
            "cell the contract uses to tell two same-named filers apart; without it the row cannot "
            "be attributed, so validation must refuse it with FMP_BAD_SHAPE"
        ),
    ),
    "InstitutionalOwnership": _entry(
        fixture=_institutional_ownership_document,
        query={"symbol": "AAPL", "year": 2025, "quarter": 4},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "investorsHolding"],
            "value": 8412.5,
            "column": "investors_holding",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "investorsHolding counts the 13F filers that held the ticker, declared as a required "
            "strict int; a fractional count is not a headcount of investors, so the integer guard "
            "must refuse the summary with FMP_BAD_SHAPE rather than truncate it"
        ),
    ),
    "EquityOwnership": _entry(
        fixture=_equity_ownership_document,
        query={"symbol": "AAPL"},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "percentOfClass"],
            "value": 2.85,
            "column": "percent_of_class",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "the beneficial-ownership route publishes percentOfClass as source text with its own "
            "formatting, and the contract declares it as required str; a bare JSON number is a "
            "different cell than the text the filing states, so validation must refuse the row "
            "with FMP_BAD_SHAPE rather than re-format it"
        ),
    ),
}
