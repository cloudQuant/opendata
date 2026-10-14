"""Offline witness configuration for the ``sec`` provider.

Every SEC leg here is a hand-written adapter over the governed HTTP client, so each witness
installs the repository's ``FixedResponseTransport`` on ``http_transport`` and the client's seam
branch reads the response off it. EDGAR publishes two carriers: JSON documents (the ticker
directory, the Submissions API, companyfacts XBRL) are the fixture's own keys, and text
resources (the SIC table, an RSS feed, an XBRL schema, a daily ``.idx`` master index, an archived
HTML document) travel as the list of their source lines, which is how the seam carries a body it
has no bytes slot for.

The refusals are SEC's own stable codes (``SecProviderError``, the attributable engine refusal
for this source) and they are code-only: SEC parsing rejects a whole published record -- a
companyfacts observation whose value is not a number, a submissions row whose accession is not an
accession -- before any contract column is addressed, so there is no column for the refusal to
attribute.
"""

from __future__ import annotations

from typing import Any, Final

# --- Identity directories ---------------------------------------------------------------------

_CIK_MAP: Final[dict[str, Any]] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BERKSHIRE", "title": "Berkshire Hathaway Inc."},
}

_EXCHANGE_DIRECTORY: Final[dict[str, Any]] = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [
        [320193, "Apple Inc.", "AAPL", "Nasdaq Global Select Market"],
        [1067983, "Berkshire Hathaway Inc.", "BRK-A", "NYSE"],
    ],
}

_SIC_PAGE: Final[list[str]] = [
    "<html><body>",
    '<table class="sic">',
    "<tr><th>SIC</th><th>Office</th><th>Industry</th></tr>",
    "<tr><td>1000</td><td>Office of Energy &amp; Transportation</td><td>Coal Mining</td></tr>",
    "<tr><td>1010</td><td>Office of Energy &amp; Transportation</td>"
    "<td>Bituminous Lignite Coal Mining</td></tr>",
    "</table></body></html>",
]

# --- XBRL companyfacts ------------------------------------------------------------------------

_ANNUAL_INSTANT_FACTS: Final[dict[str, Any]] = {
    "cik": 320193,
    "entities": {"320193": {"name": "Apple Inc.", "tickers": ["AAPL"]}},
    "facts": {
        "us-gaap": {
            "Assets": {
                "label": "Assets",
                "description": "Financial Position, Period End Value [Concept, Period]",
                "units": {
                    "USD": [
                        {
                            "end": "2023-12-30",
                            "start": None,
                            "val": 383285000000,
                            "accn": "0000320193-24-000100",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-01-26",
                            "frame": "CY2023",
                        }
                    ]
                },
            },
            "StockholdersEquity": {
                "label": "Stockholders Equity Attributable to Parent",
                "units": {
                    "USD": [
                        {
                            "end": "2023-12-30",
                            "val": 69186000000,
                            "accn": "0000320193-24-000100",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-01-26",
                        }
                    ]
                },
            },
        }
    },
}

_ANNUAL_DURATION_FACTS: Final[dict[str, Any]] = {
    "cik": 320193,
    "entities": {"320193": {"name": "Apple Inc.", "tickers": ["AAPL"]}},
    "facts": {
        "us-gaap": {
            "NetIncomeLoss": {
                "label": "Net Income (Loss) Attributable to Parent",
                "units": {
                    "USD": [
                        {
                            "start": "2022-09-25",
                            "end": "2023-09-30",
                            "val": 96996000000,
                            "accn": "0000320193-23-004750",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-02",
                        }
                    ]
                },
            },
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "start": "2022-09-25",
                            "end": "2023-09-30",
                            "val": 383285000000,
                            "accn": "0000320193-23-004750",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-02",
                        }
                    ]
                },
            },
        }
    },
}

_REVENUE_COMPARISON: Final[dict[str, Any]] = {
    "fields": ["cik", "name", "ticker"],
    "data": [[320193, "Apple Inc.", "AAPL"]],
    "cik": 320193,
    "name": "Apple Inc.",
    "facts": {
        "us-gaap": {
            "Revenues": {
                "label": "Revenues",
                "units": {
                    "USD": [
                        {
                            "end": "2023-09-30",
                            "val": 383285000000,
                            "accn": "0000320193-23-004750",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2023-11-02",
                        }
                    ]
                },
            }
        }
    },
}

# --- Submissions API --------------------------------------------------------------------------

_SUBMISSIONS: Final[dict[str, Any]] = {
    "cik": 320193,
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "sic": "3571",
    "sicDescription": "Computers & Mainframe Manufacturing",
    "fiscalYearEnd": {"month": "09", "day": "30"},
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000320193-24-000555",
                "0000320193-23-004750",
            ],
            "filingDate": ["2024-05-03", "2023-11-02"],
            "reportDate": ["", "2023-09-30"],
            "acceptanceDateTime": [
                "2024-05-03T20:15:00.000Z",
                "2023-11-02T18:04:19.000Z",
            ],
            "form": ["DEF 14A", "10-K"],
            "fileNumber": ["001-36743", "001-36743"],
            "filmNumber": ["241901203", "231362817"],
            "items": ["", ""],
            "primaryDocument": ["a20230503a_def14a.htm", "aapl-20230930.htm"],
            "primaryDocDescription": ["DEF 14A", "10-K"],
            "isInlineXBRL": [0, 1],
            "isXBRL": [0, 1],
            "size": [105882, 1328669],
        },
        "files": [],
    },
}

_FILING_INDEX: Final[dict[str, Any]] = {
    "directory": {
        "name": "0000320193-23-004750-index.htm",
        "item": [
            {
                "name": "aapl-20230930.htm",
                "type": "10-K",
                "description": "ANNUAL REPORT",
                "size": "1328669",
            },
            {
                "name": "index.json",
                "type": "INDEX",
                "description": "User Defined, Interactive Data Files",
                "size": "2687",
            },
        ],
    }
}


def _copy(value: Any) -> Any:  # noqa: ANN401 - the fixture documents are dynamic JSON
    import copy

    return copy.deepcopy(value)


def _cik_map_document() -> dict[str, Any]:
    """``company_tickers.json``: the SEC's CIK/ticker/name association file."""
    return _copy(_CIK_MAP)


def _symbol_map_document() -> dict[str, Any]:
    """The same association file, read for the reverse ticker lookup."""
    return _copy(_CIK_MAP)


def _equity_search_document() -> dict[str, Any]:
    """``company_tickers_exchange.json``: ticker, CIK, name and exchange columns."""
    return _copy(_EXCHANGE_DIRECTORY)


def _sic_page() -> list[str]:
    """The official SIC-code table page, line for line."""
    return list(_SIC_PAGE)


def _instant_companyfacts_document() -> dict[str, Any]:
    """A companyfacts document carrying instant (balance-sheet) US-GAAP observations."""
    return _copy(_ANNUAL_INSTANT_FACTS)


def _duration_companyfacts_document() -> dict[str, Any]:
    """A companyfacts document carrying duration (flow-statement) US-GAAP observations."""
    return _copy(_ANNUAL_DURATION_FACTS)


def _comparison_companyfacts_document() -> dict[str, Any]:
    """One document that answers both legs: the ticker directory, then that issuer's facts."""
    return _copy(_REVENUE_COMPARISON)


def _submissions_document() -> dict[str, Any]:
    """The Submissions API answer for one issuer's filing history."""
    return _copy(_SUBMISSIONS)


def _filing_package_document() -> dict[str, Any]:
    """Submissions metadata plus the accession's ``index.json`` directory, in one document."""
    package = _copy(_SUBMISSIONS)
    package.update(_copy(_FILING_INDEX))
    return package


_HTM_DOCUMENT: Final[list[str]] = [
    "<html><head><title>Apple Inc. Form 10-K</title></head>"
    '<body><div style="margin:0pt auto;max-width:845pt;">'
    "<p>Consolidated Statements of Operations</p>"
    "<table><tr><td>Net sales</td><td>383,285</td></tr></table>"
    "</div></body></html>"
]


def _htm_document_page() -> list[str]:
    """One archived filing document, exactly as its URL publishes it."""
    return list(_HTM_DOCUMENT)


_RSS_FEED: Final[list[str]] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
    "<channel>",
    "<title>Litigation Releases</title>",
    "<link>https://www.sec.gov/enforcement-litigation/litigation-releases</link>",
    "<item>",
    "<title>In the Matter of Example Entity</title>",
    "<pubDate>Fri, 09 Feb 2024 16:00:00 GMT</pubDate>",
    "<link>https://www.sec.gov/enforcement-litigation/litigation-releases/lr-26400</link>",
    '<guid isPermaLink="true">https://www.sec.gov/lr-26400</guid>',
    "<description>The Commission charged the respondent with disclosure violations.</description>",
    "</item>",
    "</channel>",
    "</rss>",
]


def _rss_feed_page() -> list[str]:
    """The public litigation-release RSS feed, line for line."""
    return list(_RSS_FEED)


_SCHEMA_XSD: Final[list[str]] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<schema xmlns="http://www.w3.org/2001/XMLSchema" xmlns:xbrli="http://www.xbrl.org/2003/instance"'
    ' xmlns:dei="https://xbrl.sec.gov/dei/2024" targetNamespace="https://xbrl.sec.gov/dei/2024"'
    ' elementFormDefault="unqualified" attributeFormDefault="unqualified">',
    "<annotation><documentation>SEC Data Exchange Taxonomy 2024</documentation></annotation>",
    '<element id="dei_EntityInformation" name="EntityInformation" type="dei:EntityInformation"'
    ' substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="duration"/>',
    '<element id="dei_CentralIndexKey" name="EntityCentralIndexKey" type="xbrli:stringItemType"'
    ' substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="duration"/>',
    '<element id="dei_DocumentFiscalYearFocus" name="DocumentFiscalYearFocus"'
    ' type="xbrli:gYearItemType" substitutionGroup="xbrli:item" nillable="true"'
    ' xbrli:periodType="instant"/>',
    "</schema>",
]


def _schema_xsd_page() -> list[str]:
    """One published XBRL taxonomy schema, line for line."""
    return list(_SCHEMA_XSD)


_MASTER_INDEX: Final[list[str]] = [
    "CIK|Company Name|Form Type|Date Filed|Filename",
    "0000320193|Apple Inc.|10-K|2024-01-26|edgar/data/320193/0000320193-24-000100.txt",
    "0001018724|DOW INC.|8-K|2024-01-26|edgar/data/1018724/0001018724-24-000025.txt",
]


def _master_index_page() -> list[str]:
    """The EDGAR daily master index for one filing date, line for line."""
    return list(_MASTER_INDEX)


_COMMON_FILES: Final[list[str]] = [
    "opendata/data/providers/sec/models/_common.py",
]
_DOCUMENTS_FILES: Final[list[str]] = [
    *_COMMON_FILES,
    "opendata/data/providers/sec/models/iter2_documents.py",
]
_DIRECTORY_FILES: Final[list[str]] = [
    *_COMMON_FILES,
    "opendata/data/providers/sec/models/iter2_directory.py",
]
_FINANCIAL_FILES: Final[list[str]] = [
    *_COMMON_FILES,
    "opendata/data/providers/sec/models/iter2_financial.py",
]
_DOCUMENT_CONTRACT: Final[list[str]] = [
    "opendata/data/models/sec_documents.py",
    "opendata/data/domains.yaml",
]
_ITER2_CONTRACT: Final[list[str]] = [
    "opendata/data/models/sec_iter2.py",
    "opendata/data/domains.yaml",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.sec.registration",
    # EDGAR requires a product/contact value on every request; this one is only ever sent to a
    # fake transport that records the URL it was asked for.
    "credential_env": {"SEC_USER_AGENT": "Iter2 Offline Witness witness@example.com"},
    "allowed_hosts": ["www.sec.gov", "data.sec.gov", "xbrl.sec.gov"],
    "models": {
        "CikMap": {
            "fixture_document": _cik_map_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "drop", "path": ["0", "ticker"]},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_TICKER_DIRECTORY_BAD_SHAPE",
            "tamper_reason": (
                "the CIK map contract needs the ticker it resolves a symbol by, so a directory "
                "record that publishes no ticker is refused rather than matched on a blank key"
            ),
            "source_files": _DIRECTORY_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "SymbolMap": {
            "fixture_document": _symbol_map_document,
            "query": {"query": "apple"},
            "tamper": {"op": "set", "path": ["0", "cik_str"], "value": "not-a-cik"},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_BAD_CIK",
            "tamper_reason": (
                "cik is a declared contract column and the SEC's own CIK is one to ten digits, so "
                "a directory record publishing a non-numeric CIK must be refused outright"
            ),
            "source_files": _DIRECTORY_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "EquitySearch": {
            "fixture_document": _equity_search_document,
            "query": {"query": "AAPL", "is_symbol": True},
            "tamper": {"op": "set", "path": ["data", 0, 3], "value": 100},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_TICKER_DIRECTORY_BAD_SHAPE",
            "tamper_reason": (
                "the exchange-ticker file documents exchange as a text column, and the row carries "
                "a number in that cell, so the directory parser must reject the whole file rather "
                "than publish an exchange the SecEquitySearchRecord contract cannot hold"
            ),
            "source_files": _DIRECTORY_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "SicSearch": {
            "fixture_document": _sic_page,
            "query": {"query": "coal"},
            "tamper": {
                "op": "set",
                "path": [4],
                "value": "<tr><td>12345</td><td>Office of Technology</td><td>Computers</td></tr>",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_SIC_BAD_ROW",
            "tamper_reason": (
                "the contract's sic column is a SEC Standard Industrial Classification code, which "
                "is one to four digits, so a published five-digit code is a corrupt row and not a "
                "searchable classification"
            ),
            "source_files": _DIRECTORY_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "BalanceSheet": {
            "fixture_document": _instant_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "Assets", "units", "USD", 0, "val"],
                "value": True,
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "the instant Assets observation backs the declared total_assets amount, and a "
                "published boolean is not a monetary value the strict "
                "SecBalanceSheetStatement row can hold"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "BalanceSheetGrowth": {
            "fixture_document": _instant_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "Assets", "units", "USD", 0, "filed"],
                "value": "2024-13-01",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "filing_date is a declared contract column and the companyfacts API publishes "
                "filed as an ISO calendar date, so an impossible month must refuse the "
                "observation instead of becoming a null or guessed filing date"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "CashFlowStatement": {
            "fixture_document": _duration_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "NetIncomeLoss", "units", "USD", 0, "val"],
                "value": "not-a-number",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "the duration NetIncomeLoss observation is the contract's net_income amount, and "
                "a non-numeric published value must be refused rather than defaulted to zero"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "CashFlowStatementGrowth": {
            "fixture_document": _duration_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "drop",
                "path": ["facts", "us-gaap", "NetIncomeLoss", "units", "USD", 0, "accn"],
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "accession is a declared contract column that identifies the filing vintage the "
                "growth comparison is drawn from, so an observation published without its accn "
                "cannot be attributed to a filing and must be refused"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "IncomeStatement": {
            "fixture_document": _duration_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "Revenues", "units", "USD", 0, "end"],
                "value": "not-an-epoch",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "period_ending is a declared contract column read from the observation's end, and "
                "the companyfacts API publishes it as an ISO date, so a non-date value must refuse "
                "the observation rather than emit an unanchored statement period"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "IncomeStatementGrowth": {
            "fixture_document": _duration_companyfacts_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "Revenues", "units", "USD", 0, "start"],
                "value": "2023-09-30",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "a duration fact's period_beginning must precede its period_ending for the "
                "declared statement period to mean anything, so a published start equal to the end "
                "is refused as a malformed observation"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "CompareCompanyFacts": {
            "fixture_document": _comparison_companyfacts_document,
            "query": {"symbol": "AAPL", "fact": "Revenues"},
            "tamper": {
                "op": "set",
                "path": ["facts", "us-gaap", "Revenues", "units", "USD", 0, "form"],
                "value": "",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_XBRL_BAD_FACT",
            "tamper_reason": (
                "form is a declared contract column naming the filing that reported the fact, so "
                "an observation published with an empty form must be refused rather than "
                "attributed to an unknown report type"
            ),
            "source_files": _FINANCIAL_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "CompanyFilings": {
            "fixture_document": _submissions_document,
            "query": {"cik": "0000320193"},
            "tamper": {
                "op": "set",
                "path": ["filings", "recent", "accessionNumber", 0],
                "value": "0320193-24-555",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_SUBMISSIONS_BAD_ROW",
            "tamper_reason": (
                "accession_number is a declared contract column and the SEC documents it as "
                "nnnnnnnnnn-nn-nnnnnn, so a row whose accession is not an accession must be "
                "refused instead of losing the filing's identity"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "Filings": {
            "fixture_document": _submissions_document,
            "query": {"cik": "0000320193"},
            "tamper": {"op": "drop", "path": ["filings", "recent", "filingDate"]},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_SUBMISSIONS_BAD_SHAPE",
            "tamper_reason": (
                "the Submissions API documents filings.recent as parallel arrays and filing_date "
                "is a required contract column, so a payload that omits the filingDate array "
                "publishes an answer whose rows cannot be dated"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "SecFiling": {
            "fixture_document": _filing_package_document,
            "query": {"cik": "0000320193", "accession_number": "0000320193-23-004750"},
            "tamper": {"op": "set", "path": ["directory", "item", 0, "name"], "value": ""},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_FILING_INDEX_BAD_NAME",
            "tamper_reason": (
                "documents is a declared contract column of filing document names, and the "
                "index.json listing publishes a name for every member, so an item with no name "
                "cannot address a document and must be refused"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "SecHtmFile": {
            "fixture_document": _htm_document_page,
            "query": {
                "url": "https://www.sec.gov/Archives/edgar/data/320193"
                "/000032019323004750/aapl-20230930.htm"
            },
            "tamper": {"op": "set", "path": [0], "value": "   "},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_HTML_CONTRACT_INVALID",
            "tamper_reason": (
                "the contract publishes one archived document's content, and a filing URL that "
                "answers with blank whitespace carries no document at all, so the row contract "
                "rejects it instead of handing back an empty filing"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "RssLitigation": {
            "fixture_document": _rss_feed_page,
            "query": {"limit": 5},
            "tamper": {"op": "set", "path": [7], "value": "<pubDate>not-a-date</pubDate>"},
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_RSS_BAD_DATE",
            "tamper_reason": (
                "published is a required contract column carrying the release date, so an item "
                "whose pubDate cannot be parsed as an RFC 2822 or ISO date must be refused rather "
                "than dated from the fetch"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "SchemaFiles": {
            "fixture_document": _schema_xsd_page,
            "query": {"taxonomy": "dei", "year": 2024, "limit": 20},
            "tamper": {
                "op": "set",
                "path": [5],
                "value": '<element id="dei_CentralIndexKey" name="EntityCentralIndexKey"'
                ' type="xbrli:stringItemType" xbrli:periodType="instantaneous"/>',
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_SCHEMA_BAD_PERIOD_TYPE",
            "tamper_reason": (
                "period_type is a declared contract column that the XBRL 2.1 specification allows "
                "only as instant or duration, so a taxonomy element published with any other value "
                "is refused rather than stored as an unparseable concept"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "LatestFinancialReports": {
            "fixture_document": _master_index_page,
            "query": {"filing_date": "2024-01-26"},
            "tamper": {
                "op": "set",
                "path": [1],
                "value": "0000320193|Apple Inc.|10-K|2024-01-26"
                "|edgar/data/320193/0000320193-24-000100.txt|extra",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_DAILY_INDEX_BAD_ROW",
            "tamper_reason": (
                "the daily master index documents exactly five pipe-delimited columns per entry, "
                "so a published line that does not split into five cannot be mapped onto the "
                "declared CIK, name, form type, date and filename"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
    },
}
