"""Offline witness configuration for the ``government_us`` provider.

GovernmentUS owns its transport: every leg of the fetchers witnessed here goes through
:mod:`opendata.data.providers.government_us.models._client` -- :func:`get_json` for the JSON pages,
:func:`get_bytes` for the circular -- and each of those reads the module attribute
``get_shared_http_client`` on call. Each witness therefore installs the repository's fake governed
client at that one attribute
(:mod:`iter2_claims_probe_runner._client_seam`), so the seam hands the shipped fetcher the bytes a
real answer would have carried. No leg touches a socket -- the runner refuses socket use for the
whole process -- and the URL allowlist, the HTML/bad-JSON guards, the page-size and ``total-count``
checks in ``array_rows`` and the row contracts under test are the shipped ones.

The two rows that came first here are transcribed character for character from the two receipt
fixtures this provider's evidence root retains (``docs/evidence/C100/gov-finra/fixtures/``:
``treasury_auctions.json``, 446 bytes, sha256 ``67a7d1fc94aa6fb2``; ``nws_bulletin.json``,
332 bytes, sha256 ``b8a46ff9062a2620``). They are **hand-authored fixture bodies, not live
captures** -- the bulletin id still carries its ``TEST-`` marker -- so they prove the mapping, the
JSON:API unwrap and the gates, not that FiscalData or the NWS API holds these values today. Each is
published as the source publishes it: FiscalData's ``data`` array plus its ``meta.total-count``
string, and one NWS JSON-LD Product object with both ``id`` and ``@id`` present.

The two PSD bodies are synthetic in the same way and carry no retained fixture behind them, because
this provider's evidence root holds none for FAS. They are shaped as FAS publishes: the data row and
its four lookup pages are bare JSON arrays (the shape ``array_rows`` accepts without a ``data``
envelope), country/region/commodity/attribute resolved by exactly one row each, and the circular is
a byte string whose first six bytes are the PDF magic, carried through the seam as ``body_b64``
because ``get_bytes`` hands back bytes, not text. They prove the lookup resolution, the
region-versus-country projection, the typed ``value`` guard and the magic-byte gate -- not any USDA
number.

The refusals are the provider's own stable codes (``GovernmentUSProviderError``, which carries
``code`` and ``status``). All four are deliberately code-only, and all four carry ``status=None``
because each is a post-response judgement about a body the source did serve: a row whose ``cusip``
is not the cusip that was filtered for, a product with no ``issuanceTime`` at all, a ``Value`` cell
that is neither a number nor null, and a circular whose first bytes are a gateway page's doctype
opener. None of the four reaches a contract column the witness could attribute a cell to, so no cell
is pinned.

What this module does not cover: ``WeatherBulletinDownload`` declares no ``row_model`` and has no
post-response guard at all. Measured against ``models/weather_bulletin_download.py``, its nine
``raise`` sites (:33, :36, :51, :54, :63, :70, :75, :77, :112) are all bare ``ValueError`` with an
``NWS_*`` message, and every one of them is a query/URL-shape refusal that fires before transport.
The shipped counterfact mechanism tampers the *fixture document* (``tamper.path`` always starts at
``responses``), so no tamper can reach a refusal that the query itself must trip; a witness for that
row would need a query-tamper op the runner does not have. ``TreasuryPrices`` is reserved and binds
no fetcher at all. Page truncation (``TREASURY_AUCTION_BAD_PAGE``,
``TREASURY_AUCTION_PAGE_REQUIRED``) is unexercised because one declared page of one row cannot reach
either bound.

Correction on the record: an earlier revision of this docstring also excluded the two PSD rows,
reasoning that they refuse with ``USDA_FAS_API_KEY_MISSING_OR_INVALID`` before any transport, so a
witness would be a witness of credential plumbing. That reasoning conflated "the credential guard
runs first" with "nothing downstream is exercisable". ``extract_data`` reads
``os.environ["USDA_FAS_API_KEY"]`` (``models/commodity_psd_data.py:112-114``) and the runner injects
a fixture-only value through ``credential_env``, exactly as the eia witness does, so the guard is
satisfied without a real key and the legs continue to the shipped row-level guards. Both PSD rows
do have those guards, and both are what the tampers below trip:
``CommodityPsdDataFetcher.transform_data`` refuses a row whose ``Value`` is neither a number nor
null (``models/commodity_psd_data.py:181-185``, ``USDA_BAD_COMMODITY_ROW``), and
``CommodityPsdReportFetcher.extract_data`` refuses a body that is not a PDF (
``models/commodity_psd_report.py:102-103``, ``USDA_REPORT_NOT_PDF``).
"""

from __future__ import annotations

import base64
import copy
from typing import Any, Final

#: The one attribute both fetchers' transport reads per request; the runner patches it in
#: ``models/_client.py`` only.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.government_us.models._client"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/government_us/models/_client.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/government_us.py",
    "opendata/data/domains.yaml",
]

#: One 10-year note auction row exactly as the retained FiscalData fixture publishes it, including
#: the source's own string-typed numbers and its ``Z``-suffixed update stamp.
_TREASURY_BODY: Final[dict[str, Any]] = {
    "data": [
        {
            "cusip": "91282CJL6",
            "issue_date": "2025-02-18",
            "security_type": "Note",
            "security_term": "10-Year",
            "maturity_date": "2035-02-15",
            "auction_date": "2025-02-12",
            "interest_rate": "4.50",
            "high_yield": "4.63",
            "bid_to_cover_ratio": "2.54",
            "reopening": "No",
            "updated_timestamp": "2025-02-12T13:00:00Z",
        }
    ],
    "meta": {"total-count": "1", "count": 1},
}

_CUSIP: Final[str] = "91282CJL6"

#: One NWS Text Product object as the retained bulletin fixture publishes it.
_NWS_BODY: Final[dict[str, Any]] = {
    "@id": "https://api.weather.gov/products/TEST/2026-10-12T12:00:00+00:00",
    "id": "TEST-20261012T1200Z",
    "wmoCollectiveId": "ABCD01",
    "issuingOffice": "KOUN",
    "issuanceTime": "2026-10-12T12:00:00+00:00",
    "productCode": "TUV",
    "productName": "Weather Bulletin",
    "productText": "NOUS43 KOUN 121200\nTEST BULLETIN\n",
}


def _row_index(body: dict[str, Any], column: str, value: str) -> int:
    """Resolve one counterfact target to its fixture row, refusing a duplicated key."""
    hits = [index for index, row in enumerate(body["data"]) if row.get(column) == value]
    if len(hits) != 1:
        raise AssertionError(
            f"counterfact target {column}={value!r} matches {len(hits)} fixture row(s), "
            "not exactly one"
        )
    return hits[0]


#: The fixture row the CUSIP counterfact targets, resolved by key rather than by hand-typed index.
_TREASURY_ROW: Final[int] = _row_index(_TREASURY_BODY, "cusip", _CUSIP)


def _envelope(url_contains: str, body: dict[str, Any]) -> dict[str, Any]:
    """Wrap one body as the single JSON answer that fetch is served."""
    return {
        "responses": [
            {
                "url_contains": url_contains,
                "content_type": "application/json",
                "body": copy.deepcopy(body),
            }
        ]
    }


def _treasury_document() -> dict[str, Any]:
    """The FiscalData page the offline seam answers the auction request with."""
    return _envelope("v1/accounting/od/auctions_query", _TREASURY_BODY)


def _bulletin_document() -> dict[str, Any]:
    """The NWS JSON-LD product the offline seam answers the latest-product request with."""
    return _envelope("/products/types/TUV/locations/KOUN/latest", _NWS_BODY)


#: One archived FAS circular. The body is the shipped guard's own subject: ``extract_data`` reads
#: the first six bytes, so this is a real ``%PDF-`` header followed by the smallest object table the
#: fetcher can carry through unparsed.
_PDF_BYTES: Final[bytes] = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
)

#: The same slot filled with the HTML an upstream gateway page really answers with.
_NOT_PDF_BYTES: Final[bytes] = b"<!DOCTYPE html><html><body>504 Gateway Timeout</body></html>\n"

_PSD_COMMODITY: Final[str] = "corn"
_PSD_COMMODITY_CODE: Final[str] = "10402"
_PSD_MARKET_YEAR: Final[int] = 2024
_PSD_COUNTRY_CODE: Final[str] = "2750"
_PSD_REGION_CODE: Final[str] = "51000"

#: The four FAS lookup pages, in the order ``extract_data`` asks for them
#: (``models/commodity_psd_data.py:116-119``). Each carries exactly one row that the shipped
#: resolvers accept: ``_resolve_code`` and ``_resolve_countries`` both refuse a set of matches
#: smaller or larger than one, so a second Corn or China row would change which guard the witness
#: is exercising rather than exercise it.
_PSD_LOOKUPS: Final[list[tuple[str, list[dict[str, Any]]]]] = [
    (
        "commodities",
        [
            {
                "CommodityCode": _PSD_COMMODITY_CODE,
                "CommodityName": "Corn",
                "CommodityDescription": "Corn",
            }
        ],
    ),
    (
        "countries",
        [
            {
                "CountryCode": _PSD_COUNTRY_CODE,
                "CountryName": "China",
                "RegionCode": _PSD_REGION_CODE,
            }
        ],
    ),
    ("regions", [{"RegionCode": _PSD_REGION_CODE, "RegionName": "China (Mainland)"}]),
    ("commodityAttributes", [{"AttributeId": "10", "AttributeName": "Production"}]),
]

#: One PSD commodity page row in the source keys ``_map_source_row`` reads. The ``value`` is a
#: number here because the contract column is ``float | int | None``; the counterfact below is the
#: only place it is not.
_PSD_ROW: Final[dict[str, Any]] = {
    "CountryCode": _PSD_COUNTRY_CODE,
    "CommodityCode": _PSD_COMMODITY_CODE,
    "CommodityDescription": "Corn",
    "AttributeDescription": "Production",
    "MarketYear": _PSD_MARKET_YEAR,
    "UnitDescription": "Metric Ton",
    "Value": 265.0,
}

#: The data page follows the lookups, so its envelope index is derived from the lookup list rather
#: than typed -- a fifth lookup would move the tamper target along with it.
_PSD_DATA_INDEX: Final[int] = len(_PSD_LOOKUPS)
_PSD_DATA_URL: Final[str] = (
    f"/api/psd/commodity/{_PSD_COMMODITY_CODE}/country/all/year/{_PSD_MARKET_YEAR}"
)
_PSD_REPORT_URL: Final[str] = "/PSDOnline/Circulars/2022/05/Grain.pdf"


def _psd_document() -> dict[str, Any]:
    """The five JSON pages the offline seam answers one PSD query with."""
    responses: list[dict[str, Any]] = [
        {"url_contains": f"/api/psd/{name}", "body": copy.deepcopy(rows)}
        for name, rows in _PSD_LOOKUPS
    ]
    responses.append({"url_contains": _PSD_DATA_URL, "body": [copy.deepcopy(_PSD_ROW)]})
    return {"responses": responses}


def _report_document() -> dict[str, Any]:
    """The PDF bytes the offline seam answers the circular request with, as a base64 carrier."""
    return {
        "responses": [
            {
                "url_contains": _PSD_REPORT_URL,
                "body_b64": base64.b64encode(_PDF_BYTES).decode("ascii"),
            }
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.government_us.registration",
    "allowed_hosts": [
        "api.fiscaldata.treasury.gov",
        "api.weather.gov",
        "api.fas.usda.gov",
        "apps.fas.usda.gov",
    ],
    "credential_env": {"USDA_FAS_API_KEY": "fixture-key-never-sent"},
    "models": {
        "TreasuryAuctions": {
            "fixture_document": _treasury_document,
            "client_seam": _SEAM,
            "query": {"cusip": _CUSIP, "page_size": 10, "page_num": 1},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", _TREASURY_ROW, "cusip"],
                "value": "91282CJL7",
            },
            "refusal_class": "GovernmentUSProviderError",
            "refusal_code": "TREASURY_AUCTION_CUSIP_MISMATCH",
            "tamper_reason": (
                "serving a row for 91282CJL7 under a request filtered for 91282CJL6 keeps the "
                "page well-formed, in size and typed-clean, but answers a security the caller did "
                "not ask for, so TreasuryAuctionsFetcher.transform_data must refuse the row rather "
                "than publish another instrument's auction result under the requested CUSIP"
            ),
            "source_files": [
                "opendata/data/providers/government_us/models/treasury_auctions.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "WeatherBulletin": {
            "fixture_document": _bulletin_document,
            "client_seam": _SEAM,
            "query": {"product_type": "TUV", "location": "KOUN"},
            "tamper": {
                "op": "drop",
                "path": ["responses", 0, "body", "issuanceTime"],
            },
            "refusal_class": "GovernmentUSProviderError",
            "refusal_code": "NWS_BAD_PRODUCT_ROW",
            "tamper_reason": (
                "a product object with no issuanceTime still carries a legitimate id, office and "
                "bulletin text, but issuance_time is a required contract column and the mapping "
                "cannot invent one, so WeatherBulletinFetcher.transform_data must refuse the "
                "product rather than publish an undated bulletin"
            ),
            "source_files": [
                "opendata/data/providers/government_us/models/weather_bulletin.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CommodityPsdData": {
            "fixture_document": _psd_document,
            "client_seam": _SEAM,
            "query": {
                "commodity": _PSD_COMMODITY,
                "market": "global",
                "start_year": _PSD_MARKET_YEAR,
                "end_year": _PSD_MARKET_YEAR,
            },
            "tamper": {
                "op": "set",
                "path": ["responses", _PSD_DATA_INDEX, "body", 0, "Value"],
                "value": "265.0t",
            },
            "refusal_class": "GovernmentUSProviderError",
            "refusal_code": "USDA_BAD_COMMODITY_ROW",
            "tamper_reason": (
                "a Value cell written 265.0t keeps the country, commodity, attribute and "
                "market-year keys the mapping reads and still arrives as one well-formed JSON "
                "row, but value is typed float | int | None on the contract and 265.0t is none "
                "of those, so CommodityPsdDataFetcher.transform_data must refuse the page "
                "rather than coerce the cell to 0, drop the row silently, or ship a string in "
                "a numeric column"
            ),
            "source_files": [
                "opendata/data/providers/government_us/models/commodity_psd_data.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CommodityPsdReport": {
            "fixture_document": _report_document,
            "client_seam": _SEAM,
            "query": {"commodity": "grain", "year": 2022, "month": 5},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body_b64"],
                "value": base64.b64encode(_NOT_PDF_BYTES).decode("ascii"),
            },
            "refusal_class": "GovernmentUSProviderError",
            "refusal_code": "USDA_REPORT_NOT_PDF",
            "tamper_reason": (
                "the tampered carrier is the same length, arrives under the same URL and with "
                "the same content type, and would base64-encode into a well-shaped content "
                "string, but its first six bytes are <!DOCT instead of %PDF-, so what the "
                "archive served is a gateway page and not the circular the contract publishes. "
                "CommodityPsdReportFetcher.extract_data must refuse it before transform_data "
                "turns the bytes into a report row"
            ),
            "source_files": [
                "opendata/data/providers/government_us/models/commodity_psd_report.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
