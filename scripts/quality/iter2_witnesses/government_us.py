"""Offline witness configuration for the ``government_us`` provider.

GovernmentUS owns its transport: every leg of both fetchers here goes through
:func:`opendata.data.providers.government_us.models._client.get_json`, which reads the module
attribute ``get_shared_http_client`` on each call. Each witness therefore installs the
repository's fake governed client at that one attribute
(:mod:`iter2_claims_probe_runner._client_seam`), so the seam hands the shipped fetcher the bytes a
real answer would have carried. No leg touches a socket -- the runner refuses socket use for the
whole process -- and the URL allowlist, the HTML/bad-JSON guards, the page-size and ``total-count``
checks in ``array_rows`` and the row contracts under test are the shipped ones.

Both bodies are transcribed character for character from the two receipt fixtures this provider's
evidence root retains (``docs/evidence/C100/gov-finra/fixtures/``: ``treasury_auctions.json``,
446 bytes, sha256 ``67a7d1fc94aa6fb2``; ``nws_bulletin.json``, 332 bytes, sha256
``b8a46ff9062a2620``). They are **hand-authored fixture bodies, not live captures** -- the bulletin
id still carries its ``TEST-`` marker -- so they prove the mapping, the JSON:API unwrap and the
gates, not that FiscalData or the NWS API holds these values today. Each is published as the source
publishes it: FiscalData's ``data`` array plus its ``meta.total-count`` string, and one NWS
JSON-LD Product object with both ``id`` and ``@id`` present.

The refusals are the provider's own stable codes (``GovernmentUSProviderError``, which carries
``code`` and ``status``). Both are deliberately code-only, and both carry ``status=None`` because
each is a post-response judgement about a body the source did serve: a row whose ``cusip`` is not
the cusip that was filtered for, and a product with no ``issuanceTime`` at all. Neither refusal
reaches a contract column, so no cell exists for the witness to attribute.

What this module does not cover: the three other bound rows. ``CommodityPsdData`` and
``CommodityPsdReport`` refuse with ``USDA_FAS_API_KEY_MISSING_OR_INVALID`` before any transport, so
a witness for them would be a witness of credential plumbing and their prior central run is
principal-denied; ``WeatherBulletinDownload`` maps bytes to base64 and has no row-level refusal --
its only rejections are the ``GOV_US_*`` transport codes -- so its counterfact would prove status
plumbing, not a contract. ``TreasuryPrices`` is reserved and binds no fetcher at all. Page
truncation (``TREASURY_AUCTION_BAD_PAGE``, ``TREASURY_AUCTION_PAGE_REQUIRED``) is unexercised
because one declared page of one row cannot reach either bound.
"""

from __future__ import annotations

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


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.government_us.registration",
    "allowed_hosts": ["api.fiscaldata.treasury.gov", "api.weather.gov"],
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
    },
}
