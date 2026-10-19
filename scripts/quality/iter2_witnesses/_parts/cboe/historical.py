"""Wave 8 shard: offline witnesses for cboe's three unwitnessed historical rows.

EquityHistorical and EtfHistorical are the hand-written All Access adapters: their fetch leg is
ONE trading-day list request followed by one ``/market/underlying-quotes`` page per listed day,
so the fixture is a ``client_seam`` envelope whose quotes entry uses the seam's ``sequence``
capability -- each matching call consumes the next page, and the runner fails loudly if the
source asks for more pages than declared. ``_all_access_client._live_json`` reads
``get_shared_http_client`` at request time and the fetchers already thread ``transport=``
through ``get_json`` (it stays None here, so the patched shared client is what runs), which is
the brief's Branch-B late resolution already in place -- no provider-local change was needed.

IndexHistorical is the public Cboe CDN daily-quote CSV download. Its fetcher calls
``get_shared_http_client()`` inside ``_fetch_csv`` at request time (module attribute lookup),
so the seam patches that module; the CSV carrier rides the seam's list-of-lines body form.
The one provider-local change this shard required is declaring ``row_model`` on
CboeIndexHistoricalFetcher, which the implementation phase reads; the class it names is the
verified resolution of the capability domain:
``contract_model('cboe_index_historical') ->
opendata.data.models.cboe_index_historical.CboeIndexHistorical``.

No expected_* face here was copied from a captured run: EquityHistorical and EtfHistorical
refuse through the All Access numeric/required guards (code-only, attribution faces empty as
branch B requires), and IndexHistorical refuses through its own strict CSV numeric guard
``CboeIndexHistoricalError`` with a code and a status face.

FuturesCurve is deliberately absent: cboe binds no fetcher for that fixed identity
(``opendata/data/providers/cboe/specs.py`` NOT_DECLARABLE), so witnessing it would require a
catalog/domains/models change this wave freezes. It is reported as a GAP. IndexSnapshots was
refused the same way when this shard was authored and left ``NOT_DECLARABLE`` in wave 37, once the
official panel was captured; it is witnessed by the sibling ``_parts/cboe/snapshots.py`` shard.
"""

from __future__ import annotations

from typing import Any, Final


def _daily_bar_page(
    symbol: str,
    open_value: float,
    high: float,
    low: float,
    close: float,
    volume: float,
) -> list[dict[str, Any]]:
    """One ``/market/underlying-quotes`` answer page carrying exactly one bar for ``symbol``.

    The route answers with an array keyed by ``symbol``; ``_fetch_daily_bars`` refuses the whole
    page if ANY record carries a different symbol, so every declared page is a one-record list.
    """
    return [
        {
            "symbol": symbol,
            "underlying_open": open_value,
            "underlying_high": high,
            "underlying_low": low,
            "underlying_close": close,
            "underlying_volume": volume,
        }
    ]


def _cboe_equity_historical_envelope() -> dict[str, Any]:
    """Two trading days in January 2026, then one AAPL quote page per day.

    The trading-days answer is a JSON array of ISO strings; the seam's string-body form is what
    ``_live_json``'s ``json.loads`` reads back as that array. The quotes entry sequences exactly
    the two pages the two listed trading days will request.
    """
    return {
        "responses": [
            {
                "url_contains": "reference/trading-days",
                "body": '["2026-01-02", "2026-01-05"]',
            },
            {
                "url_contains": "market/underlying-quotes",
                "sequence": [
                    _daily_bar_page("AAPL", 180.0, 182.25, 179.5, 181.25, 45_000_000.0),
                    _daily_bar_page("AAPL", 181.0, 183.5, 180.75, 183.0, 51_000_000.0),
                ],
            },
        ]
    }


def _cboe_etf_historical_envelope() -> dict[str, Any]:
    """Two trading days on the same documented route, answering with SPY ETF bars."""
    return {
        "responses": [
            {
                "url_contains": "reference/trading-days",
                "body": '["2026-01-06", "2026-01-07"]',
            },
            {
                "url_contains": "market/underlying-quotes",
                "sequence": [
                    _daily_bar_page("SPY", 587.0, 589.25, 586.5, 588.75, 62_000_000.0),
                    _daily_bar_page("SPY", 588.5, 590.0, 587.75, 589.5, 58_000_000.0),
                ],
            },
        ]
    }


def _cboe_index_historical_envelope() -> dict[str, Any]:
    """One VIX daily-history CSV exactly in the captured ``_OHLC_HEADER`` spelling.

    ``_parse_csv`` compares the header tuple verbatim (DATE,OPEN,HIGH,LOW,CLOSE) and reads dates
    as MM/DD/YYYY; the two rows sit inside the local window so both survive the date filter.
    """
    return {
        "responses": [
            {
                "url_contains": "VIX_History.csv",
                "content_type": "text/csv",
                "body": [
                    "DATE,OPEN,HIGH,LOW,CLOSE",
                    "01/02/2026,14.5,15.2,14.1,14.8",
                    "01/05/2026,15.0,15.6,14.7,15.3",
                ],
            }
        ]
    }


_ALL_ACCESS_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.cboe.models._all_access_client"],
    "attr": "get_shared_http_client",
}

MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityHistorical": {
        "client_seam": _ALL_ACCESS_SEAM,
        "fixture_document": _cboe_equity_historical_envelope,
        "query": {"symbol": "AAPL", "start_date": "2026-01-02", "end_date": "2026-01-05"},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "sequence", 0, 0, "underlying_open"],
            "value": True,
            "column": "open",
        },
        "refusal_class": "CboeAllAccessError",
        "refusal_code": "CBOE_LIVEVOL_BAD_NUMERIC_FIELD",
        "tamper_reason": (
            "the route documents underlying_open as a quote number and the CboeEquityHistorical "
            "'open' cell is read through _required_number; publishing a boolean where the first "
            "trading day's bar should be is the adapter's numeric guard (pydantic would widen "
            "True to 1.0), so the adapter must refuse the page with "
            "CBOE_LIVEVOL_BAD_NUMERIC_FIELD rather than emit a bar whose open is a flag"
        ),
        "source_files": [
            "opendata/data/providers/cboe/models/all_access.py",
            "opendata/data/providers/cboe/models/_all_access_client.py",
        ],
        "contract_files": [
            "opendata/data/models/congress_cboe_iter2.py",
            "opendata/data/domains.yaml",
        ],
    },
    "EtfHistorical": {
        "client_seam": _ALL_ACCESS_SEAM,
        "fixture_document": _cboe_etf_historical_envelope,
        "query": {"symbol": "SPY", "start_date": "2026-01-06", "end_date": "2026-01-07"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 1, "sequence", 1, 0, "underlying_close"],
            "column": "close",
        },
        "refusal_class": "CboeAllAccessError",
        "refusal_code": "CBOE_LIVEVOL_BAD_REQUIRED_FIELD",
        "tamper_reason": (
            "CboeEtfHistorical declares 'close' as a required daily-bar cell and the route "
            "publishes underlying_close for every trading day; a quote page whose record omits "
            "it is not a bar, so _required_number must refuse the second listed day with "
            "CBOE_LIVEVOL_BAD_REQUIRED_FIELD rather than invent a closing print"
        ),
        "source_files": [
            "opendata/data/providers/cboe/models/all_access.py",
            "opendata/data/providers/cboe/models/_all_access_client.py",
        ],
        "contract_files": [
            "opendata/data/models/congress_cboe_iter2.py",
            "opendata/data/domains.yaml",
        ],
    },
    "IndexHistorical": {
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.cboe.models.index_historical_public"],
            "attr": "get_shared_http_client",
        },
        "fixture_document": _cboe_index_historical_envelope,
        "query": {"symbol": "VIX", "start_date": "2026-01-02", "end_date": "2026-01-06"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 1],
            "value": "01/02/2026,14.5,not-a-number,14.1,14.8",
            "column": "high",
        },
        "refusal_class": "CboeIndexHistoricalError",
        "refusal_code": "CBOE_INDEX_HISTORICAL_BAD_CSV",
        "tamper_reason": (
            "the VIX history file publishes an OHLC number per cell and _source_number only "
            "accepts finite decimal text; a HIGH cell spelled 'not-a-number' is a corrupt body, "
            "so the CSV guard must refuse with CBOE_INDEX_HISTORICAL_BAD_CSV rather than "
            "publish a volatility-index bar with an invented high"
        ),
        "source_files": [
            "opendata/data/providers/cboe/models/index_historical_public.py",
        ],
        "contract_files": [
            "opendata/data/models/cboe_index_historical.py",
            "opendata/data/domains.yaml",
        ],
    },
}
