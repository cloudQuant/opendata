"""Wave-33 shard: ``government_us::TreasuryPrices``.

Published through ``_parts/<provider>/*.py`` so this row's claims live beside the row and the
parent ``iter2_witnesses/government_us.py`` module -- which pins the five accepted rows and the
provider's ``allowed_hosts`` list -- is not touched.

**Transport, seam and shape.** ``TreasuryPricesFetcher.extract_data`` posts to
``https://treasurydirect.gov/GA-FI/FedInvest/securityPriceDetail``
(``opendata/data/providers/government_us/models/treasury_prices.py:68``, transcribed from the
upstream declaration ``openbb_platform/providers/government_us/openbb_government_us/models/
treasury_prices.py:66``).  It reads the governed client off the module attribute the parent
module already names -- ``opendata.data.providers.government_us.models._client.
get_shared_http_client`` (``_client.py:9``, read at ``treasury_prices.py:210``) -- so the same
``shared_http_client`` seam the five accepted rows use is what hands this leg its bytes.  The
fixture spec declares ``method: "POST"``: the fake refuses to serve a POST-shaped spec under a
GET, so the verb the row claims is the verb the fetcher pays.

**The deviation this witness cannot price, stated rather than buried.** Upstream sends the four
date selectors plus ``fileType=csv&csv=CSV+FORMAT`` as an ``x-www-form-urlencoded`` **body**
(upstream :78-86).  The governed transport in this repository publishes no form-body verb at all:
``GovernedHttpClient.request`` accepts ``params``/``headers``/``json_body`` only
(``opendata/data/http_client.py:426-438``, and :463-465 refuses a body on GET/HEAD), and this
provider's own ``_client`` wraps GET (:42-79).  Both files are outside this wave's editable set,
so the shipped leg sends the same names and the same values through ``params``.  An offline
witness answers a request by URL substring and never inspects a body, so **no phase here can
show that TreasuryDirect takes the selectors from the query string**; the five greens below
price the CSV parsing, the type projection, the date echo and the refusal codes, and nothing
more.  The same gap has a second half: ``treasurydirect.gov`` is absent from the parent
CONFIG's ``allowed_hosts`` (``iter2_witnesses/government_us.py:311-316``, which the runner copies
into every entry at ``iter2_claims_probe_runner.py:1039``), so a live leg also needs the host in
the source-use grant (``opendata/data/source_policy.py:105``).  Landing either half is the
harness/policy owner's edit, not this shard's.

**Field reconciliation.** The source publishes eight columns -- ``cusip``, ``security_type``,
``rate``, ``maturity_date``, ``call_date``, ``bid``, ``offer``, ``eod_price`` -- in the order the
upstream mapper renames them into (upstream :112-123), and the as-of date is echoed from the
query because ``TreasuryPrice.date`` is required and no column carries it (upstream :124,
``opendata/data/models/government_us.py:175``).  So the mapper writes nine keys, and the witnessed
row comes out with **8** of the contract's **18** declared fields non-null: ``call_date`` is blank
on the listing for a bill, so it stays ``None``, and the other nine (``issuer_name``, ``isin``,
``issue_date``, ``last_traded_date``, ``total_trades``, ``last_price``, ``highest_price``,
``lowest_price``, ``ytm``) are published by no source this wave could evidence.  The scaffold's
``column_basis: built`` reading of "8 published" is right about the source columns and wrong about
what they price: eight of eighteen populated, ten null, every populated one a named contract field
and none invented.  Upstream's ``read_csv(header=0)`` then renames (upstream :111-123) consumes the
**first data line** as a header; this mapper reads positionally and drops a leading line only when
its first cell is not a nine-character CUSIP, so no security is silently lost.

**The body is a transcription, not a capture.** The three CSV lines below are byte-for-byte the
opening rows of the upstream recording kept on this machine
(``openbb_platform/providers/government_us/tests/record/http/test_government_us_fetchers/
test_government_us_treasury_prices_fetcher_urllib3_v2.yaml``, the 2024-06-26 listing).  They are
served under the query's own as-of date -- the fetcher defaults it to the last business day,
which is why the witness query carries no ``date`` and stays re-measurable next month -- so they
prove the mapping over the shape the source really publishes and do **not** prove that
TreasuryDirect holds these quotes today.  The recording's ``Content-Type`` was not transcribable
from the cassette's response head, so the spec declares ``text/csv``, the type the request asks
for.

**Refusal.** ``GovernmentUSProviderError`` is this provider's own typed, code-bearing error
(``_client.py:18-25``, standing required by ``iter2_claims_probe_runner.py:603-620``).  The
tamper breaks one numeric cell of the requested security's own line, and the shipped mapper
refuses it in ``_number`` at ``treasury_prices.py:327`` -- ``TREASURY_PRICE_BAD_VALUE`` -- before
a contract row exists.  The traceback ends inside ``transform_data``'s projection loop, so the
guard is reachable by document tamper, not merely present in the AST; a string in a float column
would otherwise be coerced, dropped, or shipped as text.  Deliberately code-only, like the five
accepted rows ahead of it: this provider's typed error carries no rejected/missing/invalid face
(``iter2_claims_probe_runner.py:651-658``), so declaring one would be a copied refusal.
"""

from __future__ import annotations

import copy
from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.government_us.models._client"],
    "attr": "get_shared_http_client",
}

#: The requested security: the first line of the transcribed upstream listing.
_CUSIP: Final[str] = "912796Y45"

#: The eight published columns, in wire order (upstream ``treasury_prices.py:112-123``).
_COLUMNS: Final[tuple[str, ...]] = (
    "cusip",
    "security_type",
    "rate",
    "maturity_date",
    "call_date",
    "bid",
    "offer",
    "eod_price",
)

#: Three rows of the 2024-06-26 listing, transcribed from the pinned upstream recording.
_LISTING_LINES: Final[list[str]] = [
    "912796Y45,MARKET BASED BILL,0.0,06/27/2024,,0.000000,99.985500,0.000000",
    "912797KH9,MARKET BASED BILL,0.0,07/02/2024,,0.000000,99.912833,0.000000",
    "912796Y52,MARKET BASED BILL,0.0,07/05/2024,,99.869125,99.868750,0.000000",
]

#: The requested security's line, and the same line with its ``bid`` cell broken the way a
#: gateway or a truncated export writes it.
_REQUESTED_ROW: Final[int] = _LISTING_LINES.index(
    next(line for line in _LISTING_LINES if line.startswith(_CUSIP))
)
_BAD_BID_ROW: Final[str] = "912796Y45,MARKET BASED BILL,0.0,06/27/2024,,99.985500x,99.985500,0"


def _document() -> dict[str, Any]:
    """The one CSV listing the offline seam answers the POST with."""
    return {
        "responses": [
            {
                "url_contains": "/GA-FI/FedInvest/securityPriceDetail",
                "method": "POST",
                "content_type": "text/csv",
                "body": copy.deepcopy(_LISTING_LINES),
            }
        ]
    }


MODELS: Final[dict[str, Any]] = {
    "TreasuryPrices": {
        "fixture_document": _document,
        "client_seam": _SEAM,
        "query": {"cusip": _CUSIP},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", _REQUESTED_ROW],
            "column": "bid",
            "value": _BAD_BID_ROW,
        },
        "refusal_class": "GovernmentUSProviderError",
        "refusal_code": "TREASURY_PRICE_BAD_VALUE",
        "tamper_reason": (
            "the tampered line keeps the requested CUSIP, the security type, both date cells "
            "and the eight-column shape, and is the same length as the row it replaces, but its "
            "bid cell reads 99.985500x, which is neither a number nor empty. bid is typed "
            "float | None on the reserved contract, so TreasuryPricesFetcher.transform_data must "
            "refuse the listing rather than coerce the cell to 0, drop the security silently, or "
            "ship a string in a numeric price column"
        ),
        "source_files": [
            "opendata/data/providers/government_us/models/treasury_prices.py",
            "opendata/data/providers/government_us/models/_client.py",
        ],
        "contract_files": [
            "opendata/data/models/government_us.py",
            "opendata/data/domains.yaml",
        ],
    },
}
