"""Wave-37 shard: offline witness for cboe's IndexSnapshots row.

The row was refused in ``opendata/data/providers/cboe/specs.py`` ``NOT_DECLARABLE`` with the reason
"No official endpoint/response mapping for the fixed regional snapshot identity was evidenced in
this slice." That reason is now refuted by an artifact rather than by prose: one unauthenticated GET
of Cboe's own panel document answered ``HTTP/2 200`` with 810 index rows on 2026-10-19, captured at
``docs/evidence/C140/wave37-unauthenticated-access-20261019/sources/cboe-all-us-indices.json``
(358,262 bytes, body sha256 ``9554ad964bcee6ac…``). The two records below are that capture's
``^VIX`` and ``^SPX`` cells verbatim, including the source's own key spellings, so the fixture is
the source's shape and not a paraphrase of it.

The adapter reads the panel through ``get_shared_http_client()`` inside ``_fetch_panel``, so this
witness declares the same ``shared_http_client`` seam the sibling ``historical`` shard declares for
the CSV route: one document per query, because the source publishes the whole panel in a single
file and offers no server-side symbol filter. Selection by symbol and the row limit are local.

The refusal is the adapter's own numeric guard, ``CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD``. It is
attributed by code rather than by a typed column because the envelope publishes ``null`` for cells
it does not carry -- every zero in this document is a *reported* absence the contract wants as null
-- so "not a number" and "no number" are different failures and only the first may land as data.
``name``, ``currency`` and ``status`` are declared on ``CboeIndexSnapshots`` but have no cell in
this envelope, and the adapter never fills them.
"""

from __future__ import annotations

from typing import Any, Final

from opendata.data.providers.cboe.models.index_snapshots_public import SNAPSHOT_URL


def _panel_record_vix() -> dict[str, Any]:
    """The capture's ``^VIX`` cell, verbatim including the envelope's own key names."""
    return {
        "symbol": "^VIX",
        "security_type": "index",
        "exchange_id": 5,
        "current_price": 19.99,
        "price_change": 3.53,
        "price_change_percent": 17.6588,
        "bid": 0.0,
        "ask": 0.0,
        "bid_size": 0,
        "ask_size": 0,
        "open": 15.81,
        "high": 22.31,
        "low": 15.72,
        "close": 19.99,
        "prev_day_close": 19.99,
        "volume": 0,
        "iv30": 97.896,
        "iv30_change": 0.0,
        "iv30_change_percent": 0.0,
        "seqno": 0,
        "last_trade_time": "2026-10-16T16:15:01",
        "tick": "up",
    }


def _panel_record_spx() -> dict[str, Any]:
    """The capture's ``^SPX`` cell, so a symbol selection has more than one candidate to refuse."""
    return {
        "symbol": "^SPX",
        "security_type": "index",
        "exchange_id": 5,
        "current_price": 7659.4399,
        "price_change": -84.6201,
        "price_change_percent": -1.1048,
        "bid": 7610.6699,
        "ask": 7694.9199,
        "bid_size": 1,
        "ask_size": 1,
        "open": 7745.3198,
        "high": 7756.8398,
        "low": 7631.77,
        "close": 7659.44,
        "prev_day_close": 7659.4399,
        "volume": 0,
        "iv30": 13.964,
        "iv30_change": 0.0,
        "iv30_change_percent": 0.0,
        "seqno": 0,
        "last_trade_time": "2026-10-16T16:14:59",
        "tick": "down",
    }


def _cboe_index_snapshots_envelope() -> dict[str, Any]:
    """The panel document as the source publishes it: an envelope name plus the row list."""
    return {
        "responses": [
            {
                "url_contains": SNAPSHOT_URL.rsplit("/", 1)[-1],
                "content_type": "application/json",
                "body": {
                    "symbol": "all_us_indices",
                    "timestamp": "2026-10-19 03:44:36",
                    "data": [_panel_record_vix(), _panel_record_spx()],
                },
            }
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "IndexSnapshots": {
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.cboe.models.index_snapshots_public"],
            "attr": "get_shared_http_client",
        },
        "fixture_document": _cboe_index_snapshots_envelope,
        "query": {"symbol": "VIX"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", 0, "current_price"],
            "value": True,
            "column": "price",
        },
        "refusal_class": "CboeIndexSnapshotsError",
        "refusal_code": "CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD",
        "tamper_reason": (
            "the panel publishes current_price as a quote number and _number only nulls a genuine "
            "JSON null; a boolean in that cell is a corrupt body, and because pydantic would widen "
            "True to a price of 1.0 the adapter must refuse the whole panel with "
            "CBOE_INDEX_SNAPSHOTS_BAD_NUMERIC_FIELD rather than emit an index snapshot priced at 1"
        ),
        "source_files": [
            "opendata/data/providers/cboe/models/index_snapshots_public.py",
        ],
        "contract_files": [
            "opendata/data/models/cboe_index_snapshots.py",
            "opendata/data/domains.yaml",
        ],
    },
}
