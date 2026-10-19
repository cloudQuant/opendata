"""Wave-39 shard: offline witness for cboe's FuturesCurve row.

The row was refused in ``opendata.data.providers.cboe.specs.NOT_DECLARABLE`` with the reason "The
reviewed official All Access docs do not establish the fixed model's underlying list, quote/history
join, or curve output protocol." Three captured artifacts answer that refusal, all of them
unauthenticated GETs of Cboe's own site recorded in
``docs/evidence/C144/wave39-unauthenticated-access-20261019/sources/``:

* ``cboe-futures-settlement-csv.csv`` -- ``HTTP/2 200``, ``text/csv``, 1,767 bytes, body sha256
  ``2715f48481217cf7…``, header ``Product,Symbol,Expiration Date,Price``, 56 rows over 12 products
  with 14 VX expiries. That is the curve output protocol.
* the same route asked with ``?dt=2026-10-09`` / ``-15`` / ``-16`` / ``-17`` returns four distinct
  bodies, and every response's ``content-disposition`` filename names the day it served
  (``FuturesSettlements_2026-10-09.csv`` and so on), with the non-settlement Saturday returning the
  header row alone. That is the history half -- and it corrects this round's first take, which
  asked only for the day the route was already serving and therefore could not have detected that
  ``dt=`` works.
* ``cboe-futures-roots-json`` -- ``HTTP/2 307`` then ``200`` on the public symbol book. That is the
  underlying list.

The fixture below is the 2026-10-16 capture's own lines, verbatim: header, all fourteen VX rows, and
two VXM rows. The VXM rows are kept on purpose -- the adapter filters the one published table by
``Product`` locally, and a fixture holding only VX would let a broken filter pass by accident.
``_seam_body_bytes`` joins a list body with LF and this capture arrived with CRLF; both are handled
by the same decoder, and the byte-level difference is recorded rather than smoothed over.

The refusal is the adapter's own price guard, ``CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD``, reached by
replacing one VX line's price cell with text. It is attributed by code rather than by a typed column
because the table has no null-price rows to lose: 56 of 56 cells in the capture carry a number, and
a day with no settlements returns no rows at all rather than empty ones, so a non-number here is a
corrupt body and not an untraded expiry. ``date`` is the request echo and is never stamped from a
clock, because the settlement day lives in a response header this seam cannot deliver.
"""

from __future__ import annotations

from typing import Any, Final

from opendata.data.providers.cboe.models.futures_settlement_public import (
    SETTLEMENT_PATH,
)

#: The needle the seam matches against the URL string the adapter builds. It carries the query
#: marker, so an adapter that stopped forwarding the settlement day would match no fixture entry.
DAY_NEEDLE = f"{SETTLEMENT_PATH}?dt="

_VX_TABLE_2026_10_16: tuple[str, ...] = (
    "Product,Symbol,Expiration Date,Price",
    "VX,VX/V6,2026-10-21,19.9075",
    "VX,VX43/V6,2026-10-28,19.9075",
    "VX,VX44/X6,2026-11-04,19.9075",
    "VX,VX45/X6,2026-11-11,19.2382",
    "VX,VX/X6,2026-11-18,19.2382",
    "VX,VX47/X6,2026-11-24,19.2382",
    "VX,VX48/Z6,2026-12-01,19.2382",
    "VX,VX/Z6,2026-12-16,18.9485",
    "VX,VX/F7,2027-01-20,19.5239",
    "VX,VX/G7,2027-02-17,19.8542",
    "VX,VX/H7,2027-03-17,20.0296",
    "VX,VX/J7,2027-04-21,20.3473",
    "VX,VX/K7,2027-05-18,20.4356",
    "VX,VX/M7,2027-06-16,20.625",
    "VXM,VXM/V6,2026-10-21,19.9075",
    "VXM,VXM/X6,2026-11-18,19.2382",
)


def _cboe_futures_curve_envelope() -> dict[str, Any]:
    """One settlement day of Cboe's own table, as the source publishes it."""
    return {
        "responses": [
            {
                "url_contains": DAY_NEEDLE,
                "content_type": "text/csv",
                "body": list(_VX_TABLE_2026_10_16),
            }
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "FuturesCurve": {
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.cboe.models.futures_settlement_public"],
            "attr": "get_shared_http_client",
        },
        "fixture_document": _cboe_futures_curve_envelope,
        "query": {"symbol": "VX", "date": "2026-10-16"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 4],
            "value": "VX,VX45/X6,2026-11-11,not-a-settlement-price",
            "column": "price",
        },
        "refusal_class": "CboeFuturesCurveError",
        "refusal_code": "CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD",
        "tamper_reason": (
            "the settlement table publishes Price as a number in 56 of 56 captured rows and "
            "returns no rows at all on a day with no settlements, so a text cell in that column is "
            "a corrupt body rather than an untraded expiry; _price refuses with "
            "CBOE_FUTURES_CURVE_BAD_NUMERIC_FIELD instead of letting StrictFloat widen a "
            "bogus cell into a curve point"
        ),
        "source_files": [
            "opendata/data/providers/cboe/models/futures_settlement_public.py",
        ],
        "contract_files": [
            "opendata/data/models/cboe_futures_curve.py",
            "opendata/data/domains.yaml",
        ],
    },
}
