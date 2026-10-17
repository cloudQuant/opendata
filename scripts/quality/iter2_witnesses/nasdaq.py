"""Offline witness configuration for the ``nasdaq`` provider.

Nasdaq owns its request shape: every leg of all nine fetchers goes through
:mod:`opendata.data.providers.nasdaq.models._common` ``NasdaqFetcher._get``, which calls
``self.http_transport or _http_get_json`` and **always** passes ``decoder=``. The engine's
``FixedResponseTransport.__call__`` (:mod:`opendata.data.providers._engine.testing`) accepts
``(url, params, *, timeout, source, headers)`` and no ``decoder``, and the repository's own nasdaq
tests pin that keyword as part of the transport contract
(:func:`tests.test_nasdaq_provider._transport` declares ``decoder: str`` and asserts
``decoder == "delimited"``), so widening it is a shared-module edit this wave does not make.
The shipped alternative is the other mechanism: :func:`_http_get_json` resolves
``get_shared_http_client`` **inside its own body** on every call
(:mod:`opendata.data.providers._engine.http_json`), so each witness installs the repository's fake
governed client at :mod:`opendata.data.http_client` and the fetch then runs the shipped decoder,
the shipped status classification and the shipped shape guards. ``EquitySearch`` is the proof this
matters: its ``decoder="delimited"`` branch is real shipped code here, not a fixture that pretends
to be a parsed document. No leg touches a socket -- the runner refuses socket use for the whole
process -- and the URL allowlist, the header set, the row contracts under test are the shipped ones.

The bodies are **hand-authored fixture bodies, not live captures**: the dates are 2026 January
weekdays chosen for the fixture, and no claim is made that api.nasdaq.com, www.nasdaqtrader.com or
data.nasdaq.com published these values. They exist to prove the mapping and the gates, and each is
published in the envelope shape that route actually returns -- ``{"data": {...}}`` for the calendar
and quote routes, ``{"data": {"table": ...}}` with a ``totalrecords`` count for the screener,
``{"datatable": {"meta": ..., "columns": [...], "data": [[...]...]}}`` positional rows for the
Data Link table, and a pipe-delimited text file for the trader directory.

Every refusal is the engine's own ``ProviderEngineError`` carrying ``NASDAQ_SHAPE_INVALID``. Each
tamper breaks a cell that a *required* contract column is read from. Seven of them are code-only,
and that is what ships: ``required_text``, ``required_date``, ``required_float``, ``optional_date``
and ``parse_gmt_datetime`` all raise the bare code, so no cell name exists for the witness to
declare. Two carry attribution because the shipped mapper does attribute them: ``EquitySearch``
names ``Symbol`` at its own raise site, and ``HistoricalDividends`` reaches ``_typed``, whose
validation error reports the omitted contract field. Both declare that detail from the column
itself, never from a captured run -- which is what
:func:`iter2_claims_probe_runner._guard_against_copied_refusal` enforces, by requiring every
declared face entry to name the tampered cell.

What this module does not cover: the IPO ``upcoming`` branch (its row table lives one level deeper,
``data.upcoming.upcomingTable.rows``, and the same refusal is reachable from the ``priced`` branch
this module does witness); ``CalendarDividend``'s out-of-window discard path, which produces no
refusal at all; the pagination-arithmetic refusals ``NASDAQ_PAGINATION_INCONSISTENT`` and
``NASDAQ_INCOMPLETE`` (a single-page seam that answers every request identically cannot reach a
second page that disagrees with the first); and the credential and transport refusals
``NASDAQ_CREDENTIAL_MISSING`` / ``NASDAQ_HTTP_ERROR`` / ``NASDAQ_RATE_LIMITED`` (a seam that already
answers the request cannot withhold the answer).
"""

from __future__ import annotations

from typing import Any, Final

#: The one attribute the engine resolves per request; ``_http_get_json`` imports it inside its body,
#: so patching it on the owning module is what the shipped fetch path reads on the next call.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.http_client"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/nasdaq/models/_common.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/nasdaq.py",
    "opendata/data/domains.yaml",
]

#: One request day, shared by the three calendar routes and the Data Link table. It is a Monday, so
#: the economic-calendar weekday filter keeps it rather than discarding the fixture as empty.
_DAY: Final[str] = "2026-01-05"
_WINDOW: Final[dict[str, str]] = {"start_date": _DAY, "end_date": _DAY}
_SHAPE_REFUSAL: Final[dict[str, Any]] = {
    "refusal_class": "ProviderEngineError",
    "refusal_code": "NASDAQ_SHAPE_INVALID",
}


def _envelope(body: Any, needle: str) -> dict[str, Any]:  # noqa: ANN401  # one route's document
    """Wrap one body in the seam envelope, answered only for the route the fetcher builds."""
    return {"responses": [{"url_contains": needle, "body": body}]}


def _calendar_earnings_document() -> dict[str, Any]:
    """Two earnings rows on the day the calendar route was asked for."""
    return _envelope(
        {
            "data": {
                "asOf": _DAY,
                "rows": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "lastYearEPS": 1.4,
                        "epsForecast": 1.59,
                        "noOfEsts": 9,
                        "fiscalQuarterEnding": "2025-12-31",
                        "lastYearRptDt": "2025-01-31",
                        "time": "time-after-hours",
                        "marketCap": 3400000000000,
                    },
                    {
                        "symbol": "MSFT",
                        "name": "Microsoft Corporation",
                        "lastYearEPS": 2.93,
                        "epsForecast": 3.04,
                        "noOfEsts": 12,
                        "fiscalQuarterEnding": "2025-12-31",
                        "lastYearRptDt": "2025-01-30",
                        "time": "time-not-supplied",
                        "marketCap": 2900000000000,
                    },
                ],
            }
        },
        "/api/calendar/earnings",
    )


def _calendar_dividend_document() -> dict[str, Any]:
    """Two dividend announcements whose ex-date is the requested day."""
    return _envelope(
        {
            "data": {
                "calendar": {
                    "rows": [
                        {
                            "symbol": "KO",
                            "companyName": "Coca-Cola Company (The)",
                            "dividend_Ex_Date": _DAY,
                            "dividend_Rate": 0.51,
                            "record_Date": "2026-01-06",
                            "payment_Date": "2026-03-15",
                            "indicated_Annual_Dividend": 2.04,
                        },
                        {
                            "symbol": "PEP",
                            "companyName": "Pepsico, Inc.",
                            "dividend_Ex_Date": _DAY,
                            "dividend_Rate": 1.3,
                            "record_Date": "2026-01-06",
                            "payment_Date": "2026-04-01",
                            "indicated_Annual_Dividend": 5.2,
                        },
                    ]
                }
            }
        },
        "/api/calendar/dividends",
    )


def _calendar_ipo_document() -> dict[str, Any]:
    """Two priced IPOs under the status branch the query selected."""
    return _envelope(
        {
            "data": {
                "priced": {
                    "rows": [
                        {
                            "symbol": "ACME",
                            "companyName": "Acme Instruments",
                            "pricedDate": _DAY,
                            "expectedPriceDate": "2026-01-02",
                            "dollarValueOfSharesOffered": 240.5,
                            "sharesOffered": 12000000,
                        },
                        {
                            "symbol": "BLTE",
                            "companyName": "Blue Triton Energy",
                            "pricedDate": _DAY,
                            "filedDate": "2025-12-18",
                            "dollarValueOfSharesOffered": 98.0,
                            "sharesOffered": 7000000,
                        },
                    ]
                }
            }
        },
        "/api/ipo/calendar",
    )


def _company_filings_document() -> dict[str, Any]:
    """One page of two filings whose declared total equals the page it returns."""
    return _envelope(
        {
            "data": {
                "totalRecords": 2,
                "asOf": _DAY,
                "rows": [
                    {
                        "filed": _DAY,
                        "formType": "10-K",
                        "period": "2025-12-31",
                        "companyName": "Apple Inc.",
                        "view": {
                            "htmlLink": "https://www.nasdaq.com/htm/000032019325000001.htm",
                            "pdfLink": "https://www.nasdaq.com/pdf/000032019325000001.pdf",
                        },
                    },
                    {
                        "filed": "2026-01-02",
                        "formType": "8-K",
                        "period": "2025-12-31",
                        "companyName": "Apple Inc.",
                        "reportingOwner": "A. Officer",
                        "view": {"docLink": "https://www.nasdaq.com/doc/000032019325000002.htm"},
                    },
                ],
            }
        },
        "/api/company/AAPL/sec-filings",
    )


def _economic_calendar_document() -> dict[str, Any]:
    """Two events with published GMT times, one of them a country the query keeps."""
    return _envelope(
        {
            "data": {
                "rows": [
                    {
                        "gmt": "10:00",
                        "country": "US",
                        "eventName": "Durable Goods Orders",
                        "consensus": "1.4%",
                        "previous": "-0.7%",
                        "actual": "1.1%",
                        "description": "<p>Month-over-month durable goods.</p>",
                    },
                    {
                        "gmt": "14:30",
                        "country": "CA",
                        "eventName": "Bank of Canada Rate Decision",
                        "consensus": 2.25,
                        "previous": "2.25",
                        "actual": None,
                        "description": None,
                    },
                ]
            }
        },
        "/api/calendar/economicevents",
    )


#: The trader directory header exactly as Nasdaq Trader publishes it, in its own column order.
_DIRECTORY_HEADER: Final[str] = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size"
    "|ETF|NextShares|CQS Symbol|NASDAQ Symbol|Nasdaq Traded|Listing Exchange"
)


def _equity_search_document() -> dict[str, Any]:
    """The pipe-delimited symbol directory, delivered as the text file it is."""
    return _envelope(
        [
            _DIRECTORY_HEADER,
            "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|AAPL||AAPL|Y|N",
            "AAPW|SPDR Weekly Risk ETF|G|N|N|100|Y|||AAPW|N|N",
            "ZZZX|Zephyr Holdings|Q|Y|I|100|N|ZZZX||ZZZX|Y|N",
            "File Creation Time: 010520260900|||||||||||",
        ],
        "nasdaqtraded.txt",
    )


def _equity_screener_document() -> dict[str, Any]:
    """One screener page of two rows, with the source's own percentage-point strings."""
    return _envelope(
        {
            "data": {
                "totalrecords": 2,
                "table": {
                    "symbol": [],
                    "rows": [
                        {
                            "symbol": "NVDA",
                            "name": "NVIDIA Corporation Common Stock",
                            "lastsale": "142.38",
                            "netchange": "3.12",
                            "pctchange": "2.25%",
                            "marketCap": "3500000000000",
                        },
                        {
                            "symbol": "AMD",
                            "name": "Advanced Micro Devices, Inc. Common Stock",
                            "lastsale": "168.4",
                            "netchange": "-1.2",
                            "pctchange": "(0.72%)",
                            "marketCap": "27100000000",
                        },
                    ],
                },
            }
        },
        "/api/screener/stocks",
    )


def _historical_dividends_document() -> dict[str, Any]:
    """Two paid dividends for the requested symbol inside the requested window."""
    return _envelope(
        {
            "data": {
                "symbol": "AAPL",
                "dividends": {
                    "rows": [
                        {
                            "exOrEffDate": _DAY,
                            "amount": 0.25,
                            "type": "Cash Dividend",
                            "currency": "USD",
                            "recordDate": "2026-01-06",
                            "paymentDate": "2026-02-12",
                            "declarationDate": "2025-12-04",
                        },
                        {
                            "exOrEffDate": "2026-01-02",
                            "amount": "0.24",
                            "type": "Cash Dividend",
                            "currency": "USD",
                            "recordDate": "2026-01-03",
                            "paymentDate": "2026-02-10",
                        },
                    ]
                },
            }
        },
        "/api/quote/AAPL/dividends",
    )


def _top_retail_document() -> dict[str, Any]:
    """The RTAT10 datatable exactly as Data Link publishes it: named columns, positional rows."""
    return _envelope(
        {
            "datatable": {
                "meta": {"datatable_id": 9246, "next_cursor_id": None},
                "columns": [
                    {"name": "date"},
                    {"name": "ticker"},
                    {"name": "activity"},
                    {"name": "sentiment"},
                ],
                "data": [
                    ["2026-01-05", "OPEN", 18412345.0, 0.62],
                    ["2026-01-05", "MSTR", 12094551.0, -0.31],
                    ["2026-01-02", "NVDA", 9988776.0, 0.11],
                ],
            }
        },
        "NDAQ/RTAT10.json",
    )


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.nasdaq.provider",
    "allowed_hosts": ["api.nasdaq.com", "www.nasdaqtrader.com", "data.nasdaq.com"],
    "credential_env": {"NASDAQ_DATA_LINK_API_KEY": "fixture-key-never-sent"},
    "models": {
        "CalendarEarnings": {
            "fixture_document": _calendar_earnings_document,
            "client_seam": _SEAM,
            "query": dict(_WINDOW),
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "rows", 0, "symbol"],
                "value": "N/A",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "a row whose symbol is Nasdaq's own withheld token still has the as-of day the "
                "request asked for and every numeric cell intact, so nothing else in the document "
                "disagrees: symbol is a required contract column of NasdaqCalendarEarnings, and "
                "CalendarEarningsFetcher must refuse the calendar rather than publish a row about "
                "no ticker or invent one from the name"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/calendar_earnings.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CalendarDividend": {
            "fixture_document": _calendar_dividend_document,
            "client_seam": _SEAM,
            "query": dict(_WINDOW),
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "calendar", "rows", 0, "dividend_Rate"],
                "value": "0,5O1",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "0,5O1 is neither a decimal nor one of Nasdaq's blank-or-N/A cells: the ex-date, "
                "symbol and both record/payment dates still parse, and the row survives every"
                "shape "
                "guard except the one that reads the rate. amount is a contract column of "
                "NasdaqCalendarDividend that a fetcher may coerce to 0.0 or drop silently, and "
                "CalendarDividendFetcher must refuse instead of publishing a wrong dividend"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/calendar_dividend.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CalendarIpo": {
            "fixture_document": _calendar_ipo_document,
            "client_seam": _SEAM,
            "query": {**_WINDOW, "status": "priced"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "priced", "rows", 0, "pricedDate"],
                "value": "13/45/2026",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "13/45/2026 keeps the ticker, the company name and the share counts well-formed"
                "and "
                "is the cell the priced branch is sorted and window-filtered by: pricedDate is the "
                "IPO event date of NasdaqCalendarIpo, and CalendarIpoFetcher must refuse the month "
                "page rather than drop the row out of the window or publish an impossible deal date"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/calendar_ipo.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CompanyFilings": {
            "fixture_document": _company_filings_document,
            "client_seam": _SEAM,
            "query": {"symbol": "AAPL", "end_date": _DAY, "limit": 2},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "rows", 0, "filed"],
                "value": "not-filed",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "one unparseable filed cell leaves totalRecords equal to the page it returns, so"
                "the "
                "pagination loop stops after exactly the send the fixture declares and reaches the "
                "row mapper; filing_date is required by NasdaqCompanyFiling, and CompanyFilings-"
                "Fetcher must refuse the page rather than silently drop the filing that limits say "
                "is there"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/company_filings.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "EconomicCalendar": {
            "fixture_document": _economic_calendar_document,
            "client_seam": _SEAM,
            "query": dict(_WINDOW),
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "rows", 0, "gmt"],
                "value": "25:99",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "25:99 is not any of the three GMT spellings the route publishes, while the event "
                "name, country and all three figures stay well-formed: date is the ordering column "
                "of NasdaqEconomicEvent and the day is what the weekday filter already vouched"
                "for, "
                "so EconomicCalendarFetcher must refuse the day rather than synthesize a midnight "
                "timestamp the source never announced"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/economic_calendar.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "EquitySearch": {
            "fixture_document": _equity_search_document,
            "client_seam": _SEAM,
            "query": {"query": "aapl"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "N/A|Apple Inc. - Common Stock|Q|N|N|100|N|AAPL||AAPL|Y|N",
                "column": "Symbol",
            },
            "expected_missing": ["Symbol"],
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "a directory line whose Symbol cell is Nasdaq's own N/A token still matches the "
                "requested term on its security name and carries valid market-category, ETF and "
                "test-issue flags, so every local filter admits it: pipe_file_rows keeps the line "
                "because the cell is not empty, while optional_text folds N/A to nothing, and "
                "NasdaqEquitySearch has no row without a symbol -- EquitySearchFetcher must refuse "
                "the file rather than emit a nameless listing or shift every later column"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/equity_search.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "EquityScreener": {
            "fixture_document": _equity_screener_document,
            "client_seam": _SEAM,
            "query": {"limit": 2},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "table", "rows", 0, "lastsale"],
                "value": "142,3B",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "142,3B keeps totalrecords equal to the rows on the page, so the offset loop stops "
                "after one send, and the symbol, name, change and percentage all still parse: "
                "last_price is the one required numeric of NasdaqEquityScreener, and "
                "EquityScreenerFetcher must refuse the screen rather than publish a screen row"
                "whose "
                "price is a placeholder"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/equity_screener.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "HistoricalDividends": {
            "fixture_document": _historical_dividends_document,
            "client_seam": _SEAM,
            "query": {"symbol": "AAPL", "start_date": "2026-01-01", "end_date": _DAY},
            "tamper": {
                "op": "drop",
                "path": ["responses", 0, "body", "data", "dividends", "rows", 0, "amount"],
            },
            "expected_invalid": ["amount"],
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "a payment row that publishes no amount cell at all still carries an ex-date"
                "inside "
                "the requested window, a dividend type, a currency and three well-formed dates,"
                "so it "
                "passes every shape guard on its way to the row mapper: amount is a required"
                "float of "
                "NasdaqHistoricalDividend, so HistoricalDividendsFetcher must refuse the history "
                "rather than invent a zero dividend or drop the payment the source declared"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/historical_dividends.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "TopRetail": {
            "fixture_document": _top_retail_document,
            "client_seam": _SEAM,
            "query": dict(_WINDOW),
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "datatable", "data", 0, 2],
                "value": "1841234O.0",
            },
            **_SHAPE_REFUSAL,
            "tamper_reason": (
                "one corrupted activity cell leaves the column names, the row width and the ticker "
                "and sentiment cells exactly as the table declares them, and it is the cell the "
                "latest-day selection and the descending sort are both computed from: activity is"
                "a "
                "required float of NasdaqTopRetail, so TopRetailFetcher must refuse the table"
                "rather "
                "than rank the top-ten list on a number it could not read"
            ),
            "source_files": [
                "opendata/data/providers/nasdaq/models/top_retail.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
