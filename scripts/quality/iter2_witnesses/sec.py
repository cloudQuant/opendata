"""Offline witness configuration for the ``sec`` provider.

Every SEC leg here is a hand-written adapter over the governed HTTP client, so each witness
installs the repository's ``FixedResponseTransport`` on ``http_transport`` and the client's seam
branch reads the response off it. EDGAR publishes two carriers: JSON documents (the ticker
directory, the Submissions API, companyfacts XBRL) are the fixture's own keys, and text
resources (the SIC table, an RSS feed, an XBRL schema, a daily ``.idx`` master index, an archived
HTML document) travel as the list of their source lines, which is how the seam carries a body it
has no bytes slot for.

Two legs are archive discovery rather than a single JSON or text document -- the fails-to-deliver
half-month ZIPs and the quarterly Form 13F data-set ZIP -- so those fixtures carry the archive as
the base64 of a deterministically built ZIP, which is the same bytes the source URL would have
published. The archive parsers themselves are the code under test.

The refusals are SEC's own stable codes (``SecProviderError``, the attributable engine refusal
for this source) and they are code-only: SEC parsing rejects a whole published record -- a
companyfacts observation whose value is not a number, a submissions row whose accession is not an
accession -- before any contract column is addressed, so there is no column for the refusal to
attribute.
"""

from __future__ import annotations

import base64
import io
import zipfile
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


# --- Governed-client seam: archive discovery and filing-package legs ----------------------------

_SEC_COMMON_MODULE: Final[str] = "opendata.data.providers.sec.models._common"
#: Every SEC leg in this repository reaches the network through ``_common``'s governed client, so
#: patching that one module attribute is what makes these witnesses offline: the archive
#: discovery, ZIP and XML parsers that ship are still the code that runs.
_SEC_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": [_SEC_COMMON_MODULE],
    "attr": "get_shared_http_client",
}


def _seam_json(url_contains: str, document: dict[str, Any]) -> dict[str, Any]:
    """One JSON answer at the URL fragment the fetcher is documented to request."""
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "application/json",
        "body": document,
    }


def _seam_text(url_contains: str, lines: list[str]) -> dict[str, Any]:
    """One text resource carried as its own source lines, as the seam encodes them."""
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "text/html",
        "body": list(lines),
    }


def _seam_zip(url_contains: str, members: dict[str, str]) -> dict[str, Any]:
    """One ZIP archive, built to fixed timestamps so the base64 carrier replays byte-identically."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            info = zipfile.ZipInfo(name, date_time=(2024, 1, 31, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, text)
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "application/zip",
        "body_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def _seam_envelope(*responses: dict[str, Any]) -> dict[str, Any]:
    """The envelope the runner's fake governed client routes on, first needle wins."""
    return {"responses": list(responses)}


_FTD_PAGE: Final[list[str]] = [
    "<html><body>",
    '<a href="/files/data/fails-deliver-data/cnsfails202312a.zip">December 2023, first half</a>',
    '<a href="/files/data/fails-deliver-data/cnsfails202312b.zip">December 2023, second half</a>',
    '<a href="/files/data/fails-deliver-data/cnsfails202401a.zip">January 2024, first half</a>',
    "</body></html>",
]

_FTD_TABLE: Final[str] = (
    "Settlement Date|Cusip|Symbol|Shares Delivered|Issuing Depository|Price\n"
    "20240131|037833100|AAPL|4719|APPLE INC|185.50\n"
    "20240130|037833100|AAPL|1204|APPLE INC|186.00\n"
)


def _ftd_document() -> dict[str, Any]:
    """The FTD index page and the newest half-month archive it links."""
    return _seam_envelope(
        _seam_text("data-research/sec-markets-data/fails-deliver-data", _FTD_PAGE),
        _seam_zip(
            "files/data/fails-deliver-data/cnsfails202401a.zip",
            {"cnsfails202401a.txt": _FTD_TABLE},
        ),
    )


_13F_MANAGER_CIK: Final[str] = "0001067983"
_13F_HR_ACCESSION: Final[str] = "0001067983-24-000858"
_13F_HR_FOLDER: Final[str] = "000106798324000858"
_13F_HR_TABLE: Final[str] = "form13fInfoTable.xml"

_13F_HR_SUBMISSIONS: Final[dict[str, Any]] = {
    "cik": 1067983,
    "name": "Berkshire Hathaway Inc.",
    "filings": {
        "recent": {
            "accessionNumber": [_13F_HR_ACCESSION, "0001067983-24-000742"],
            "filingDate": ["2024-11-14", "2024-05-15"],
            "reportDate": ["2024-09-30", "2024-03-31"],
            "form": ["13F-HR", "13F-HR"],
            "primaryDocument": [_13F_HR_TABLE, "form13fInfoTable2024Q1.xml"],
            "size": [3519, 3120],
        },
        "files": [],
    },
}

_13F_HR_INDEX: Final[dict[str, Any]] = {
    "directory": {
        "item": [
            {
                "name": _13F_HR_TABLE,
                "type": "INFORMATION TABLE",
                "description": "INFORMATION TABLE OF RECEIPTS AND DELIVERIES",
                "size": "3519",
            },
            {"name": "index.json", "type": "INDEX", "description": "JSON", "size": "2687"},
        ]
    }
}

_13F_HR_XML: Final[list[str]] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<informationTable xmlns="http://www.sec.gov/xml/filings/edgar/form13f">',
    "<infoTable>",
    "<nameOfIssuer>APPLE INC</nameOfIssuer>",
    "<titleOfClass>COM</titleOfClass>",
    "<cusip>037833100</cusip>",
    "<value>8474954</value>",
    "<shrsOrPrnAmt>",
    "<sshPrnamt>45614320</sshPrnamt>",
    "<sshPrnamtType>SH</sshPrnamtType>",
    "</shrsOrPrnAmt>",
    "<investmentDiscretion>SOLE</investmentDiscretion>",
    "<votingAuthority>",
    "<Sole>45614320</Sole>",
    "<Shared>0</Shared>",
    "<None>0</None>",
    "</votingAuthority>",
    "</infoTable>",
    "<infoTable>",
    "<nameOfIssuer>BANK OF AMERICA CORP /DE/</nameOfIssuer>",
    "<titleOfClass>COM</titleOfClass>",
    "<cusip>060505100</cusip>",
    "<value>2952358</value>",
    "<shrsOrPrnAmt>",
    "<sshPrnamt>88361245</sshPrnamt>",
    "<sshPrnamtType>SH</sshPrnamtType>",
    "</shrsOrPrnAmt>",
    "<investmentDiscretion>SOLE</investmentDiscretion>",
    "<votingAuthority>",
    "<Sole>88361245</Sole>",
    "<Shared>0</Shared>",
    "<None>0</None>",
    "</votingAuthority>",
    "</infoTable>",
    "</informationTable>",
]


def _form_13f_hr_document() -> dict[str, Any]:
    """The manager's Submissions answer, its filing index, and the information-table XML."""
    return _seam_envelope(
        _seam_json(f"submissions/CIK{_13F_MANAGER_CIK}.json", _13F_HR_SUBMISSIONS),
        _seam_json(f"{_13F_HR_FOLDER}/index.json", _13F_HR_INDEX),
        _seam_text(f"{_13F_HR_FOLDER}/{_13F_HR_TABLE}", _13F_HR_XML),
    )


_13F_BATCH_PATH: Final[str] = "/files/datastandardsinnovation/data/form-13f-data-sets/"


def _13f_batch_anchor(window: str) -> str:
    """One quarterly batch anchor as the data-set listing publishes it."""
    return f'<a href="{_13F_BATCH_PATH}{window}_form13f.zip">{window}</a>'


_13F_DATASETS_PAGE: Final[list[str]] = [
    "<html><body>",
    _13f_batch_anchor("01Jul2024-31Aug2024"),
    _13f_batch_anchor("01Oct2024-31Dec2024"),
    "</body></html>",
]

_13F_DATASET_SUBMISSIONS_TSV: Final[str] = (
    "ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT\n"
    "0001067983-24-000858\t14-Nov-2024\t13F-HR\t1067983\t30-Sep-2024\n"
    "0000320193-24-000941\t14-Nov-2024\t13F-HR\t320193\t30-Sep-2024\n"
)

_13F_DATASET_COVERPAGE_TSV: Final[str] = (
    "ACCESSION_NUMBER\tREPORTCALENDARORQUARTER\tFILINGMANAGER_NAME\n"
    "0001067983-24-000858\t30-Sep-2024\tBerkshire Hathaway Inc\n"
    "0000320193-24-000941\t30-Sep-2024\tApple Inc\n"
)


def _institutions_document() -> dict[str, Any]:
    """The 13F data-set listing and the newest quarterly batch archive."""
    return _seam_envelope(
        _seam_text("data-research/sec-markets-data/form-13f-data-sets", _13F_DATASETS_PAGE),
        _seam_zip(
            "01Oct2024-31Dec2024_form13f.zip",
            {
                "submission.tsv": _13F_DATASET_SUBMISSIONS_TSV,
                "coverpage.tsv": _13F_DATASET_COVERPAGE_TSV,
            },
        ),
    )


_MDA_ISSUER_CIK: Final[str] = "0000789019"
_MDA_ACCESSION: Final[str] = "0000950170-23-046672"
_MDA_FOLDER: Final[str] = "000095017023046672"
_MDA_PRIMARY_DOCUMENT: Final[str] = "msft-20230930.htm"

_MDA_TICKERS: Final[dict[str, Any]] = {
    "0": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corporation"}
}

_MDA_SUBMISSIONS: Final[dict[str, Any]] = {
    "cik": 789019,
    "name": "Microsoft Corporation",
    "filings": {
        "recent": {
            "accessionNumber": [_MDA_ACCESSION, "0000950170-23-028615"],
            "filingDate": ["2023-10-24", "2023-04-25"],
            "reportDate": ["2023-09-30", "2023-03-31"],
            "form": ["10-Q", "10-Q"],
            "primaryDocument": [_MDA_PRIMARY_DOCUMENT, "msft-20230331.htm"],
            "size": [1883342, 1739944],
        },
        "files": [],
    },
}

_MDA_HTML: Final[list[str]] = [
    "<html><body>",
    "<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>",
    "<p>FORM 10-Q FOR THE QUARTERLY PERIOD ENDED SEPTEMBER 30, 2023</p>",
    "<h2>PART I Financial Information</h2>",
    "<h3>Item 1. Financial Statements</h3>",
    "<p>Condensed Consolidated Balance Sheets</p>",
    "<h3>Item 2. Management's Discussion and Analysis of Financial Condition and Results of "
    "Operations</h3>",
    "<p>Revenue increased on growth across each of our reporting segments, with Microsoft Cloud "
    "revenue up year over year and commercial remaining performance obligations continuing to "
    "grow.</p>",
    "<h3>Item 3. Quantitative and Qualitative Disclosures About Market Risk</h3>",
    "<p>Our market risk exposures relate to interest rate and foreign currency risk.</p>",
    "</body></html>",
]


def _management_discussion_document() -> dict[str, Any]:
    """Ticker association, the issuer's Submissions answer, then the filing document."""
    return _seam_envelope(
        _seam_json("files/company_tickers.json", _MDA_TICKERS),
        _seam_json(f"submissions/CIK{_MDA_ISSUER_CIK}.json", _MDA_SUBMISSIONS),
        _seam_text(f"{_MDA_FOLDER}/{_MDA_PRIMARY_DOCUMENT}", _MDA_HTML),
    )


_NPORT_FUND_CIK: Final[str] = "0001234567"
_NPORT_ACCESSION: Final[str] = "0001234567-24-000012"
_NPORT_FOLDER: Final[str] = "000123456724000012"
_NPORT_PRIMARY_DOCUMENT: Final[str] = "primary_document.xml"

_NPORT_SUBMISSIONS: Final[dict[str, Any]] = {
    "cik": 1234567,
    # A synthetic fund identity: N-PORT holdings are joined on the filer CIK, and nothing in the
    # witness reads or writes a live fund's record.
    "name": "Offline Witness Growth Fund",
    "filings": {
        "recent": {
            "accessionNumber": [_NPORT_ACCESSION],
            "filingDate": ["2024-05-27"],
            "reportDate": ["2024-03-31"],
            "acceptanceDateTime": ["2024-05-27T18:04:19.000Z"],
            "form": ["NPORT-P"],
            "primaryDocument": [_NPORT_PRIMARY_DOCUMENT],
            "size": [8123],
        },
        "files": [],
    },
}

_NPORT_INDEX: Final[dict[str, Any]] = {
    "directory": {
        "item": [
            {
                "name": _NPORT_PRIMARY_DOCUMENT,
                "type": "NPORT-P",
                "description": "Fund Portfolio Holdings File",
                "size": "8123",
            },
            {"name": "index.json", "type": "INDEX", "description": "JSON", "size": "2687"},
        ]
    }
}

_NPORT_XML: Final[list[str]] = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<NPORT-P xmlns="http://www.sec.gov/xml/filings/edgar/formnport">',
    "<periodOfReport>2024-03-31</periodOfReport>",
    "<regStateCY>DE</regStateCY>",
    "<invstOrSec>",
    "<name>APPLE INC</name>",
    "<title>APPLE INC</title>",
    "<cusip>037833100</cusip>",
    "<identifiers>",
    '<ticker idType="https://xbrl.sec.gov/dei/2023">AAPL</ticker>',
    "</identifiers>",
    "<fairValLevel>2</fairValLevel>",
    "<securityCategory>equityContract</securityCategory>",
    "<payoffProfile>long</payoffProfile>",
    "<assetCat>equity</assetCat>",
    "<issuerCat>corp</issuerCat>",
    "<invCountry>US</invCountry>",
    "<balance>1250000</balance>",
    "<unit>sh</unit>",
    "<currency>USD</currency>",
    "<valUSD>231875000</valUSD>",
    "<pctVal>0.5234</pctVal>",
    "</invstOrSec>",
    "<invstOrSec>",
    "<name>MICROSOFT CORP</name>",
    "<title>MICROSOFT CORP</title>",
    "<cusip>594918104</cusip>",
    "<fairValLevel>2</fairValLevel>",
    "<securityCategory>equityContract</securityCategory>",
    "<payoffProfile>long</payoffProfile>",
    "<assetCat>equity</assetCat>",
    "<issuerCat>corp</issuerCat>",
    "<invCountry>US</invCountry>",
    "<balance>420000</balance>",
    "<unit>sh</unit>",
    "<currency>USD</currency>",
    "<valUSD>151947600</valUSD>",
    "<pctVal>0.3430</pctVal>",
    "</invstOrSec>",
    "</NPORT-P>",
]


def _nport_disclosure_document() -> dict[str, Any]:
    """The fund's Submissions answer, its filing index, then the N-PORT holdings XML."""
    return _seam_envelope(
        _seam_json(f"submissions/CIK{_NPORT_FUND_CIK}.json", _NPORT_SUBMISSIONS),
        _seam_json(f"{_NPORT_FOLDER}/index.json", _NPORT_INDEX),
        _seam_text(f"{_NPORT_FOLDER}/{_NPORT_PRIMARY_DOCUMENT}", _NPORT_XML),
    )


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
_FTD_FILES: Final[list[str]] = [
    *_COMMON_FILES,
    "opendata/data/providers/sec/models/iter2_ftd.py",
]
_INSTITUTIONS_FILES: Final[list[str]] = [
    *_COMMON_FILES,
    "opendata/data/providers/sec/models/iter2_institutions.py",
]
_DOCUMENT_CONTRACT: Final[list[str]] = [
    "opendata/data/models/sec_documents.py",
    "opendata/data/domains.yaml",
]
_INSTITUTIONS_CONTRACT: Final[list[str]] = [
    "opendata/data/models/sec_institutions.py",
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
        "EquityFTD": {
            "client_seam": _SEC_CLIENT_SEAM,
            "fixture_document": _ftd_document,
            "query": {"symbol": "AAPL", "limit": 1},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 4],
                "value": '<a href="/files/data/fails-deliver-data/legacy/cnsfails202312a.zip">'
                "December 2023, first half (legacy mirror)</a>",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_FTD_ARCHIVE_LINK_CONFLICT",
            "tamper_reason": (
                "the FTD fetcher maps each half-month to exactly one archive URL, so a listing "
                "that republishes December 2023 first half under a second path cannot say which "
                "report the settlement rows came from and must refuse before any download"
            ),
            "source_files": _FTD_FILES,
            "contract_files": _ITER2_CONTRACT,
        },
        "InstitutionsSearch": {
            "client_seam": _SEC_CLIENT_SEAM,
            "fixture_document": _institutions_document,
            "query": {"query": "berkshire"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 3],
                "value": _13f_batch_anchor("01Nov2024-31Dec2024"),
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_13F_DATASET_LATEST_BATCH_AMBIGUOUS",
            "tamper_reason": (
                "the witness reads the single newest filing-window batch, so a listing that "
                "publishes two batches whose windows both end 31Dec2024 has no unique latest "
                "batch and must refuse instead of picking one by link order"
            ),
            "source_files": _INSTITUTIONS_FILES,
            "contract_files": _INSTITUTIONS_CONTRACT,
        },
        "Form13FHR": {
            "client_seam": _SEC_CLIENT_SEAM,
            "fixture_document": _form_13f_hr_document,
            "query": {"cik": "0001067983"},
            "tamper": {
                "op": "set",
                "path": ["responses", 2, "body", 6],
                "value": "<value>eight-million</value>",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_13F_BAD_VALUE",
            "tamper_reason": (
                "value is a required contract column carrying the reported dollar amount, and the "
                "Form 13F information table publishes it as an integer, so a holding whose value "
                "is not a number is refused instead of entering the row as a null or a guess"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "ManagementDiscussionAnalysis": {
            "client_seam": _SEC_CLIENT_SEAM,
            "fixture_document": _management_discussion_document,
            "query": {"symbol": "MSFT", "year": 2023, "period": "Q3"},
            "tamper": {
                "op": "set",
                "path": ["responses", 2, "body", 6],
                "value": "<h3>Item 2. Overview of the Quarter</h3>",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_MDA_SECTION_NOT_FOUND",
            "tamper_reason": (
                "the contract publishes the management discussion narrative itself, and a 10-Q "
                "whose Item 2 heading no longer names management's discussion has no such section "
                "to extract, so the fetch must refuse rather than return neighbouring Item 1 text"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
        "NportDisclosure": {
            "client_seam": _SEC_CLIENT_SEAM,
            "fixture_document": _nport_disclosure_document,
            "query": {"cik": "0001234567"},
            "tamper": {
                "op": "set",
                "path": ["responses", 2, "body", 17],
                "value": "<balance>1.25M</balance>",
            },
            "refusal_class": "SecProviderError",
            "refusal_code": "SEC_NPORT_BAD_NUMBER",
            "tamper_reason": (
                "balance is the declared holdings quantity and N-PORT publishes it as a number, so "
                "a position carrying a shorthand string cannot be counted, valued or compared and "
                "must be refused rather than stored as null"
            ),
            "source_files": _DOCUMENTS_FILES,
            "contract_files": _DOCUMENT_CONTRACT,
        },
    },
}
