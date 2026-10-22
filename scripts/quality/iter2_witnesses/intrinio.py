"""Offline witness configuration for the intrinio provider models.

Intrinio's five declarative engine rows are witnessed here exactly as :mod:`cboe` witnesses its
engine rows: the refusal the probe must observe is the engine's own attributed failure
(``missing=(column,)`` for a required cell the record never published, ``rejected=(column,)`` for
a published boolean in a numeric column), so every ``expected_*`` list below is declared from
``opendata/data/providers/intrinio/specs.py`` — from the column the tampered source key feeds —
and never from a captured run.

The cursor-paged declarations (``HistoricalDividends``, ``CalendarIpo``, ``InsiderTrading``) carry
an explicit ``next_page: null`` in their fixture because that key is itself part of the contract
these endpoints document, and the tamper is aimed at a *cell*, never at the cursor: a
``RowExpansionError`` from the repoint/explode kernels is a :class:`RuntimeError`, not a typed
provider refusal, so a tamper that broke a required parent pointer would be an unattributed crash
rather than the witness this matrix claims.

Intrinio's seven hand-written news/attributes rows are witnessed the way :mod:`benzinga` witnesses
its hand-written adapters: those adapters refuse a bad upstream body with a stable code and status
and no per-cell detail, so their entries declare no ``expected_missing``/``expected_rejected``/
``expected_invalid`` list and no ``column`` — an invented attribution would claim a refusal this
provider's code does not publish. Their fixtures publish ``next_page: null`` too, because the
adapter reads that key on every paged page and refuses a page that omits it.
"""

from __future__ import annotations

from typing import Any, Final

_ENGINE_SOURCE_FILES: Final[tuple[str, ...]] = (
    "opendata/data/providers/intrinio/specs.py",
    "opendata/data/providers/_engine/http_json.py",
)
_CONTRACT_FILES: Final[tuple[str, ...]] = (
    "opendata/data/models/intrinio.py",
    "opendata/data/domains.yaml",
)
_REFUSAL_CLASS: Final = "ProviderEngineError"
_SHAPE_CODE: Final = "INTRINIO_SHAPE_INVALID"


def _currency_pair(code: str, base: str, quote: str) -> dict[str, Any]:
    """One forex-pair record in the keys ``CURRENCY_PAIRS`` reads (:57-61 of the declaration)."""
    return {
        "code": code,
        "name": f"{base}/{quote}",
        "base_currency": base,
        "quote_currency": quote,
    }


def _intrinio_currency_pairs_document() -> dict[str, Any]:
    """The parameterless ``/forex/pairs`` directory under its declared ``pairs`` pointer."""
    return {
        "pairs": [
            _currency_pair("USDCHF", "USD", "CHF"),
            _currency_pair("NZDUSD", "NZD", "USD"),
        ]
    }


def _intrinio_equity_search_document() -> dict[str, Any]:
    """Two ``/companies/search`` hits under the declared ``companies`` pointer."""
    return {
        "companies": [
            {
                "ticker": "AAPL",
                "name": "Apple Inc.",
                "id": "A3269",
                "cik": "0000320193",
                "lei": "X0ZZ0WYF7QIF98IC0L06",
            },
            {
                "ticker": "MSFT",
                "name": "Microsoft Corporation",
                "id": "S3279",
                "cik": "0000789019",
            },
        ]
    }


def _intrinio_historical_dividends_document() -> dict[str, Any]:
    """Two adjustments for one security plus the terminal cursor the contract documents."""
    return {
        "stock_price_adjustments": [
            {
                "date": "2024-02-09",
                "dividend": 0.24,
                "dividend_currency": "USD",
                "factor": 1.0,
                "split_ratio": None,
            },
            {
                "date": "2024-05-10",
                "dividend": 0.25,
                "dividend_currency": "USD",
                "factor": 1.0,
                "split_ratio": None,
            },
        ],
        "next_page": None,
    }


def _intrinio_calendar_ipo_document() -> dict[str, Any]:
    """One IPO calendar item with the nested company/security objects the repoint plan reads."""
    return {
        "initial_public_offerings": [
            {
                "ticker": "ABCV",
                "date": "2026-03-04",
                "status": "upcoming",
                "exchange": "NASDAQ",
                "offer_amount": 120000000.0,
                "share_price": 16.0,
                "share_price_lowest": 14.0,
                "share_price_highest": 18.0,
                "share_count": 7500000,
                "share_count_lowest": 6666667,
                "share_count_highest": 8571429,
                "announcement_url": "https://www.sec.gov/Archives/abcv-424b4.html",
                "sec_report_url": "https://www.sec.gov/Archives/abcv-s1.html",
                "open_price": None,
                "close_price": None,
                "volume": None,
                "day_change": None,
                "week_change": None,
                "month_change": None,
                "id": "IPO7712",
                "company": {
                    "id": "C8812",
                    "ticker": "ABCV",
                    "name": "Alpha Beta Capital Ventures Inc.",
                    "lei": "549300XKZ0AV8V0J1V77",
                    "cik": "0001987654",
                },
                "security": {
                    "id": "S9902",
                    "name": "Alpha Beta Capital Ventures Common Stock",
                    "code": "ABCV",
                    "currency": "USD",
                    "ticker": "ABCV",
                    "composite_ticker": "NASDAQ:ABCV",
                    "figi": "BBG000XYZ1V7",
                    "composite_figi": "BBG000XYZ1V8",
                    "share_class_figi": "BBG000XYZ1V9",
                    "primary_listing": True,
                },
            }
        ],
        "next_page": None,
    }


def _intrinio_insider_trading_document() -> dict[str, Any]:
    """One filing with one transaction, in the spellings the explode plan and aliases read."""
    return {
        "transaction_filings": [
            {
                "issuer_ticker": "NVDA",
                "issuer_cik": "0001045810",
                "issuer_company": "NVIDIA Corp",
                "filing_date": "2026-02-19",
                "filing_url": "https://www.sec.gov/Archives/edgar/data/1045810/r00012.htm",
                "owner": {
                    "owner_cik": "0001585131",
                    "owner_name": "COOPER REbecca A",
                },
                "transactions": [
                    {
                        "transaction_date": "2026-02-17",
                        "ownership_type": "D",
                        "officer_title": "Chief Executive Officer",
                        "transaction_type_code": "S",
                        "acquisition_disposition_code": "D",
                        "security_title": "Common Stock",
                        "total_shares_owned": 21000000.0,
                        "amount_of_shares": 500000.0,
                        "transaction_price": 120.5,
                        "conversion_exercise_price": None,
                        "deemed_execution_date": "2026-02-17",
                        "exercise_date": None,
                        "expiration_date": None,
                        "underlying_security_title": None,
                        "underlying_shares": None,
                        "nature_of_ownership": "See footnote",
                        "director": False,
                        "officer": True,
                        "ten_percent_owner": False,
                        "other_relation": False,
                        "derivative_transaction": False,
                        "report_line_number": 1,
                    }
                ],
            }
        ],
        "next_page": None,
    }


_NEWS_SOURCE_FILES: Final[tuple[str, ...]] = (
    "opendata/data/providers/intrinio/models/iter2_news_attributes.py",
)
_NEWS_CONTRACT_FILES: Final[tuple[str, ...]] = (
    "opendata/data/models/intrinio_news_attributes.py",
    "opendata/data/domains.yaml",
)
_SHAPE_REFUSAL: Final = "IntrinioShapeError"


def _news_item(item_id: str, title: str, *, ticker: str) -> dict[str, Any]:
    """One ``news[]`` record, in exactly the keys the news adapter reads (:456-528)."""
    return {
        "id": item_id,
        "publication_date": "2026-02-17T14:30:00Z",
        "title": title,
        "author": "R. A. Reporter",
        "summary": f"Summary of {title}.",
        "url": f"https://news.intrinio.example/{item_id}",
        "source": "Bloomberg",
        "topics": [{"id": "T1", "name": "earnings"}, {"id": "T2", "name": "guidance"}],
        "word_count": 640,
        "business_relevance": 0.86,
        "article_sentiment": "positive",
        "article_sentiment_confidence": 0.91,
        "language": "english",
        "spam": False,
        "copyright": "intrinio",
        "security": {
            "id": f"SEC-{item_id}",
            "company_id": f"COMP-{item_id}",
            "ticker": ticker,
            "primary_listing": True,
        },
    }


def _intrinio_company_news_document() -> dict[str, Any]:
    """The company news feed for one identifier, with the terminal cursor the adapter reads."""
    return {
        "news": [
            _news_item("N1001", "Apple lifts guidance for the quarter", ticker="AAPL"),
            _news_item("N1002", "Apple reports record services revenue", ticker="AAPL"),
        ],
        "next_page": None,
    }


def _intrinio_world_news_document() -> dict[str, Any]:
    """The all-company feed: the same records plus the nested company the world row keeps."""
    rows = [
        _news_item("W2001", "Semiconductor demand keeps suppliers busy", ticker="NVDA"),
        _news_item("W2002", "Freight rates ease across the container trade", ticker="FDX"),
    ]
    for index, item in enumerate(rows):
        item["company"] = {
            "id": f"COMP-{item['id']}",
            "ticker": item["security"]["ticker"],
            "name": f"Company {index}",
            "lei": f"549300XKZ0AV8V0J1V{index:02d}",
            "cik": f"00010458{index:02d}",
        }
    return {"news": rows, "next_page": None}


def _intrinio_company_filings_document() -> dict[str, Any]:
    """One ``filings[]`` record for a searched company."""
    return {
        "filings": [
            {
                "id": "F3001",
                "filing_date": "2026-02-03",
                "report_type": "10-K",
                "report_url": "https://www.sec.gov/Archives/aapl-20251231.htm",
                "filing_url": "https://www.sec.gov/Archives/aapl-index.htm",
                "accepted_date": "2026-02-03T17:05:00Z",
                "period_end_date": "2025-12-31",
                "sec_unique_id": "0000320193-26-000011",
                "instance_url": "https://www.sec.gov/Archives/aapl.xml",
                "company": {
                    "id": "COMP-A3269",
                    "ticker": "AAPL",
                    "name": "Apple Inc.",
                    "cik": "0000320193",
                },
            }
        ],
        "next_page": None,
    }


def _data_tag(tag_id: str, tag: str, statement_code: str, sequence: int) -> dict[str, Any]:
    """One ``tags[]`` hit in the keys the data-tag search maps (:852-866)."""
    return {
        "id": tag_id,
        "name": tag.replace("_", " ").title(),
        "tag": tag,
        "statement_code": statement_code,
        "statement_type": "income_statement",
        "parent": "Revenues",
        "sequence": sequence,
        "factor": "1",
        "balance": "credit",
        "type": "number",
        "unit": "USD",
    }


def _intrinio_search_attributes_document() -> dict[str, Any]:
    """Two matching data tags for the ``/data_tags/search`` query."""
    return {
        "tags": [
            _data_tag("T4001", "net_income", "NETINC", 30),
            _data_tag("T4002", "net_income_continuing_operations", "NETINC", 31),
        ]
    }


def _intrinio_latest_attributes_document() -> dict[str, Any]:
    """One latest data-point answer: the adapter reads only the ``value`` cell."""
    return {"value": 96996000.0, "symbol": "AAPL", "tag": "net_income"}


def _intrinio_historical_attributes_document() -> dict[str, Any]:
    """Two annual observations for one security/tag pair."""
    return {
        "historical_data": [
            {"date": "2022-12-31", "value": 93462000.0},
            {"date": "2023-12-31", "value": 96996000.0},
        ],
        "next_page": None,
    }


def _intrinio_fred_series_document() -> dict[str, Any]:
    """Two economic-index observations under the index endpoint's ``historical_data`` pointer."""
    return {
        "historical_data": [
            {"date": "2024-01-01", "value": 118.3},
            {"date": "2024-02-01", "value": 118.9},
        ],
        "next_page": None,
    }


def _intrinio_options_snapshots_document() -> dict[str, Any]:
    """Two option contracts: the metadata envelope, then the gzip CSV it names."""
    header = (
        "CONTRACT ID,OPEN INTEREST,TRADE PRICE,TRADE SIZE,TOTAL TRADE VOLUME,"
        "LAST TRADE TIMESTAMP,TRADE HIGH PRICE,TRADE LOW PRICE,ASK PRICE,ASK SIZE,"
        "LAST ASK TIMESTAMP,BID PRICE,BID SIZE,LAST BID TIMESTAMP,TOTAL ASK VOLUME,"
        "ASK HIGH PRICE,ASK LOW PRICE,TOTAL BID VOLUME,BID HIGH PRICE,BID LOW PRICE"
    )
    calls = (
        "AAPL_240308C00170000,1200,2.35,3,45,1709906400,2.50,2.10,2.40,10,1709906400,"
        "2.30,8,1709906400,300,2.45,2.05,500,2.35,2.15"
    )
    puts = (
        "AAPL_240308P00165000,,1.10,1,7,1709906400,1.15,1.05,1.12,4,1709906400,"
        "1.08,6,1709906400,120,1.14,1.04,200,1.10,1.02"
    )
    return {
        "responses": [
            {
                "url_contains": "/options/snapshots",
                "body": {
                    "snapshots": [
                        {"files": [{"url": "https://data.intrinio.example/options-snapshot.csv"}]}
                    ]
                },
            },
            {
                "url_contains": "options-snapshot.csv",
                "body": "\n".join((header, calls, puts)) + "\n",
                "content_type": "text/csv",
            },
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.intrinio.provider",
    "credential_env": {"INTRINIO_API_KEY": "fixture-token-never-sent"},
    "allowed_hosts": ["api-v2.intrinio.com"],
    "models": {
        "CurrencyPairs": {
            "fixture_document": _intrinio_currency_pairs_document,
            "query": {"query": "USD"},
            "tamper": {
                "op": "drop",
                "path": ["pairs", 0, "code"],
                "column": "symbol",
            },
            "expected_missing": ["symbol"],
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "the symbol column is declared required with source_key 'code', so a pair record "
                "that never publishes 'code' leaves the required cell unresolvable and the engine "
                "must refuse with INTRINIO_SHAPE_INVALID attributing missing=('symbol',) instead "
                "of publishing a nameless pair"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/currency_pairs.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": list(_CONTRACT_FILES),
        },
        "EquitySearch": {
            "fixture_document": _intrinio_equity_search_document,
            "query": {"query": "AAPL"},
            "tamper": {
                "op": "drop",
                "path": ["companies", 0, "id"],
                "column": "intrinio_id",
            },
            "expected_missing": ["intrinio_id"],
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "intrinio_id is declared required with source_key 'id', so a search hit without "
                "the upstream id cannot be addressed in the catalog and the engine must refuse "
                "with INTRINIO_SHAPE_INVALID attributing missing=('intrinio_id',)"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/equity_search.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": list(_CONTRACT_FILES),
        },
        "HistoricalDividends": {
            "fixture_document": _intrinio_historical_dividends_document,
            "query": {"symbol": "AAPL"},
            "tamper": {
                "op": "drop",
                "path": ["stock_price_adjustments", 0, "dividend"],
                "column": "amount",
            },
            "expected_missing": ["amount"],
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "amount is declared required with source_key 'dividend' and the contract row "
                "types it non-null, so an adjustment record that publishes no dividend is the "
                "split-only shape the declaration refuses to accept: INTRINIO_SHAPE_INVALID with "
                "missing=('amount',)"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/historical_dividends.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": list(_CONTRACT_FILES),
        },
        "CalendarIpo": {
            "fixture_document": _intrinio_calendar_ipo_document,
            "query": {"symbol": "ABCV", "status": "upcoming"},
            "tamper": {
                "op": "set",
                "path": ["initial_public_offerings", 0, "share_count"],
                "value": True,
                "column": "share_count",
            },
            "expected_rejected": ["share_count"],
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "share_count is declared int, and pydantic silently widens True to 1 share, so "
                "the numeric guard that refuses a published boolean must attribute the offending "
                "column: INTRINIO_SHAPE_INVALID with rejected=('share_count',)"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/calendar_ipo.py",
                "opendata/data/providers/_engine/row_expansion.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": list(_CONTRACT_FILES),
        },
        "InsiderTrading": {
            "fixture_document": _intrinio_insider_trading_document,
            "query": {"symbol": "NVDA"},
            "tamper": {
                "op": "set",
                "path": ["transaction_filings", 0, "transactions", 0, "report_line_number"],
                "value": True,
                "column": "report_line_number",
            },
            "expected_rejected": ["report_line_number"],
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "report_line_number is declared int on the exploded transaction row, so a filing "
                "that publishes a flag in the line-number cell is the boolean-in-numeric-column "
                "refusal attributed to that column: INTRINIO_SHAPE_INVALID with "
                "rejected=('report_line_number',)"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/insider_trading.py",
                "opendata/data/providers/_engine/row_expansion.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": list(_CONTRACT_FILES),
        },
        "CompanyNews": {
            "fixture_document": _intrinio_company_news_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "set", "path": ["news", 0, "publication_date"], "value": True},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "publication_date is documented as ISO-8601 date-time text and the row types it "
                "as a required datetime, so a boolean in that cell is a shape failure the adapter "
                "must refuse with INTRINIO_SHAPE_INVALID rather than stamping a story dated True"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "WorldNews": {
            "fixture_document": _intrinio_world_news_document,
            "query": {"limit": 2},
            "tamper": {"op": "drop", "path": ["news", 0, "url"]},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "url is the required locator of a news record (the row declares it non-null "
                "StrictStr), so a story the feed publishes without it is INTRINIO_SHAPE_INVALID, "
                "not an unaddressable headline row"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "CompanyFilings": {
            "fixture_document": _intrinio_company_filings_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "drop", "path": ["filings", 0, "report_url"]},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "report_url is the filing's required document address, read through "
                "_required_text, so a filing record without it is refused as "
                "INTRINIO_SHAPE_INVALID instead of a row that points at no filing"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "SearchAttributes": {
            "fixture_document": _intrinio_search_attributes_document,
            "query": {"query": "net_income"},
            "tamper": {"op": "set", "path": ["tags", 0, "sequence"], "value": True},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "sequence is the tag's integer position in its statement (_optional_integer "
                "refuses a bool outright), so a flag in that cell is INTRINIO_SHAPE_INVALID rather "
                "than a tag catalogped at line 1"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "LatestAttributes": {
            "fixture_document": _intrinio_latest_attributes_document,
            "query": {"symbol": "AAPL", "tag": "net_income"},
            "tamper": {"op": "drop", "path": ["value"]},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "the data-point endpoint's whole answer is its value cell, so a document that "
                "publishes no 'value' key is the shape this adapter refuses with "
                "INTRINIO_SHAPE_INVALID instead of inventing a null latest attribute row"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "HistoricalAttributes": {
            "fixture_document": _intrinio_historical_attributes_document,
            "query": {"symbol": "AAPL", "tag": "net_income", "frequency": "yearly", "limit": 2},
            "tamper": {"op": "set", "path": ["historical_data", 0, "value"], "value": True},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "an observation's value is a number and _optional_number refuses a boolean in a "
                "numeric field, because pydantic would widen True to 1.0 USD; the shape refusal "
                "INTRINIO_SHAPE_INVALID is the only honest answer"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "FredSeries": {
            "fixture_document": _intrinio_fred_series_document,
            "query": {"symbol": "SPCSAANDUS", "limit": 2},
            "tamper": {"op": "drop", "path": ["historical_data"]},
            "refusal_class": _SHAPE_REFUSAL,
            "refusal_code": _SHAPE_CODE,
            "tamper_reason": (
                "the index history endpoint documents its observations under the historical_data "
                "pointer, so a document without that list is INTRINIO_SHAPE_INVALID rather than an "
                "empty series that would read as a legitimate zero"
            ),
            "source_files": list(_NEWS_SOURCE_FILES),
            "contract_files": list(_NEWS_CONTRACT_FILES),
        },
        "OptionsSnapshots": {
            "client_seam": {
                "kind": "shared_http_client",
                "attr": "get_shared_http_client",
                "modules": [
                    "opendata.data.providers.intrinio.models.options_snapshots",
                ],
            },
            "fixture_document": _intrinio_options_snapshots_document,
            "query": {"date": "2024-03-08T12:15:00+0400", "only_traded": True},
            "tamper": {"op": "drop", "path": ["responses", 0, "body", "snapshots", 0, "files"]},
            "refusal_class": _REFUSAL_CLASS,
            "refusal_code": "INTRINIO_OPTIONS_SNAPSHOTS_UNAVAILABLE",
            "tamper_reason": (
                "the metadata envelope names the bulk file through snapshots[].files[].url, so an "
                "envelope that lists a snapshot without any file leaves the adapter nothing to "
                "download and it must refuse with INTRINIO_OPTIONS_SNAPSHOTS_UNAVAILABLE instead "
                "of publishing an empty option table"
            ),
            "source_files": [
                "opendata/data/providers/intrinio/models/options_snapshots.py",
                *_ENGINE_SOURCE_FILES,
            ],
            "contract_files": [
                "opendata/data/models/intrinio_options_snapshots.py",
                *_CONTRACT_FILES,
            ],
        },
    },
}
