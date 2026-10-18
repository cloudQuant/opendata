"""Offline witness configuration for the ``government_us`` provider.

GovernmentUS owns its transport: every leg of the fetchers witnessed here goes through
:mod:`opendata.data.providers.government_us.models._client` -- :func:`get_json` for the JSON pages,
:func:`get_bytes` for the two document rows -- and each of those reads the module attribute
``get_shared_http_client`` on call. Each witness therefore installs the repository's fake governed
client at that one attribute
(:mod:`iter2_claims_probe_runner._client_seam`), so the seam hands the shipped fetcher the bytes a
real answer would have carried. No leg touches a socket -- the runner refuses socket use for the
whole process -- and the URL allowlist, the HTML/bad-JSON guards, the page-size and ``total-count``
checks in ``array_rows`` and the row contracts under test are the shipped ones.

The two rows that came first here are transcribed character for character from the two receipt
fixtures kept beside this provider's history (``docs/evidence/C100/gov-finra/fixtures/``:
``treasury_auctions.json``, 446 bytes, sha256 ``67a7d1fc94aa6fb2``; ``nws_bulletin.json``,
332 bytes, sha256 ``b8a46ff9062a2620``) -- a root ``.gitignore`` covers, so these bodies are inline
here and are never read from there. They are **hand-authored fixture bodies, not live
captures** -- the bulletin id still carries its ``TEST-`` marker -- so they prove the mapping, the
JSON:API unwrap and the gates, not that FiscalData or the NWS API holds these values today. Each is
published as the source publishes it: FiscalData's ``data`` array plus its ``meta.total-count``
string, and one NWS JSON-LD Product object with both ``id`` and ``@id`` present.

The two PSD bodies are synthetic in the same way, and they transcribe neither of the two FAS
receipts that root does hold on this machine: ``usda_psd.json`` (1,237 bytes, sha256
``55db6e5c6e8e2911``) keeps its lookups and its data rows as keys of one merged object and codes
them in a stub namespace (``CommodityCode`` 1, ``CountryCode`` ``BR``) rather than the mutually
consistent numeric codes this witness carries, and ``usda_report.pdf`` is a 51-byte magic-only
stub (sha256 ``904636248025ad20``). Both bodies here follow the shape the fetcher reads off the
wire: the data row and its four lookup pages are bare JSON arrays (the shape ``array_rows``
accepts without a ``data`` envelope), country/region/commodity/attribute resolved by exactly one
row each, and the circular is a byte string whose first six bytes are the PDF magic, carried
through the seam as ``body_b64`` because ``get_bytes`` hands back bytes, not text. They prove
the lookup resolution, the region-versus-country projection, the typed ``value`` guard and the
magic-byte gate -- not any USDA number. An earlier revision of this paragraph asserted that the
evidence root holds no FAS receipt at all, which the two filenames above refute; the claim that
survives is the narrower one, that these bodies are not transcriptions of them.

The bulletin document is carried as ``body_b64`` for the same reason the circular is:
``get_bytes`` returns bytes. Its payload is a transcription, byte for byte, of the third NWS
receipt the machine holds -- ``docs/evidence/C100/gov-finra/fixtures/nws_document.txt``, 33
bytes, sha256 ``e81fd18a79047014``, measured equal to the ``productText`` of
``nws_bulletin.json`` above (``TEST`` marker included) and, once base64-encoded, equal to the
literal ``tests/test_government_us_iter2.py:551`` asserts this fetcher returns. That fixture root
is gitignored (``.gitignore``'s ``/docs/evidence/C100/**`` rule), and the provider test opens it on
lines 59, 258 and 522 with no skip marker anywhere in the file, so seven of its nine tests --
including the download test just cited -- do not run from a clean clone. This module reads nothing
at all: the 33 bytes are inline as ``_BULLETIN_TEXT_BYTES``, so the landed row rests on the clone
even where the provider's own suite does not. The census is the wave's
``probes/c100_fixture_dependency_census.py``. The URL this document is served under is not a
receipt: the query carries one official ``api.weather.gov`` product URL composed from that fixture
id, and ``text/plain`` is the type the fetcher's own ``Accept`` header asks for. So the row proves
the media-type gate and the base64 document mapping over bytes this file carries, and -- like the
four rows ahead of it, and for the same reason -- it does not prove what the NWS archive serves
today.

The refusals are the provider's own stable codes (``GovernmentUSProviderError``, which carries
``code`` and ``status``). All five are deliberately code-only, and all five carry ``status=None``
because each is a post-response judgement about a body the source did serve: a row whose ``cusip``
is not the cusip that was filtered for, a product with no ``issuanceTime`` at all, a ``Value`` cell
that is neither a number nor null, a circular whose first bytes are a gateway page's doctype opener,
and a bulletin document announced as ``text/html``. None of the five reaches a contract column the
witness could attribute a cell to, so no cell is pinned.

What this module does not cover: ``TreasuryPrices`` is reserved and binds no fetcher at all. Page
truncation (``TREASURY_AUCTION_BAD_PAGE``, ``TREASURY_AUCTION_PAGE_REQUIRED``) is unexercised
because one declared page of one row cannot reach either bound. ``NWS_DOCUMENT_EMPTY_BODY``
(``models/weather_bulletin_download.py:134-135``) is the download row's other post-response
guard and is not the tamper published here because the runner takes exactly one ``tamper`` per
row (``entry["tamper"]`` is a single dict with a single ``path``), and the content-type guard is
the one that prices the failure an archive actually produces; the empty-body arm is measured in
the wave's prevalidation probe instead.

Correction on the record: an earlier revision of this docstring also excluded
``WeatherBulletinDownload``, stating that it declared no ``row_model`` and had no post-response
guard at all, that all of its ``raise`` sites were bare ``ValueError`` with an ``NWS_*`` message
fired before transport, and that the shipped counterfact could therefore never reach it. That
was a faithful reading of the module as it stood then and is not a reading of the module as it
stands: the row was landed by wave-31 step 3, which declared the fetcher's
``row_model`` as ``WeatherBulletinDownload`` (``models/weather_bulletin_download.py:96``) and added
the two guards at :135 and :137, both after the governed ``get_bytes`` call at :125. The claim that
survived is the one about the mechanism, not the one about the row: the query-shape refusals (:37,
:40, :55, :58, :67, :74, :79, :81, and the pre-transport URL narrowing at :122) are still bare
``ValueError`` and still fire before any seam is consulted, so a ``tamper.path`` that starts at
``responses`` cannot reach any of them, and none of them is witnessed.

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


#: The document this row downloads: the same retained bulletin text the ``WeatherBulletin`` row
#: witnesses, served here as the bytes behind the product's own URL rather than as a JSON-LD object.
_BULLETIN_TEXT_BYTES: Final[bytes] = _NWS_BODY["productText"].encode("utf-8")

#: The URL shape the query validator accepts for this host: HTTPS, an allowlisted host, no query
#: string and no fragment. The path carries the product id the retained fixture publishes.
_BULLETIN_DOC_URL: Final[str] = f"/products/{_NWS_BODY['id']}/text"


def _download_document() -> dict[str, Any]:
    """The document bytes the offline seam answers the bulletin download request with."""
    return {
        "responses": [
            {
                "url_contains": _BULLETIN_DOC_URL,
                "content_type": "text/plain",
                "body_b64": base64.b64encode(_BULLETIN_TEXT_BYTES).decode("ascii"),
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
        "WeatherBulletinDownload": {
            "fixture_document": _download_document,
            "client_seam": _SEAM,
            "query": {"urls": f"https://api.weather.gov{_BULLETIN_DOC_URL}"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "content_type"],
                "value": "text/html",
            },
            "refusal_class": "GovernmentUSProviderError",
            "refusal_code": "NWS_DOCUMENT_BAD_CONTENT_TYPE",
            "tamper_reason": (
                "the tampered answer keeps the same URL, the same byte length and a body that "
                "base64-encodes into a perfectly well-shaped content string, but it announces "
                "text/html, which is what an interstitial or gateway page announces and is none of "
                "the three media types this fetcher's own Accept header asked for. The published "
                "row would carry that announced type verbatim in data_format.media_type, so "
                "WeatherBulletinDownloadFetcher.extract_data must refuse the answer rather than "
                "ship a document the contract claims is HTML"
            ),
            "source_files": [
                "opendata/data/providers/government_us/models/weather_bulletin_download.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
