"""Shard entry: the four yfinance screen rows that ride the screener SDK surface.

EquityGainers, EquityLosers, EquityActive and EquityScreener all fetch through
``_sdk.require_screen`` -> ``yfinance.screen`` (0.2.66), which is NOT the chart/quoteSummary
surface ``EquityQuote`` landed on. Two things differ and are reflected in every entry below:

* the SDK builds one crumb-parameterized **POST** to ``v1/finance/screener`` (Yahoo's fixed
  ``corsDomain/formatted/lang/region`` query string plus the screen itself in the JSON body),
  preceded by the same cookie/crumb bootstrap legs ``EquityQuote`` declares. The POST rather than
  the saved-screen GET is not a choice this repo makes: ``screener/screener.py:138`` diverts a
  *predefined* name into the custom POST path whenever ``offset`` is not None, and
  ``require_screen`` always passes ``offset`` (0 on the first page) -- measured, and mirrored in
  the fixture needle plus the entry's declared ``method``;
* the refusal is the screener-shape guard the shipped path actually reaches. Each row's tamper
  breaks ``finance.result[0].quotes``, which the SDK unwraps and hands to ``require_screen``,
  whose list check at ``_sdk.py:435`` refuses it with ``YfinanceProviderError`` /
  ``YFINANCE_SCREEN_SHAPE_INVALID`` and ``status=None``. That site is proven, not assumed: the
  traceback tail of every tampered leg of these four rows is ``_sdk.py:81 in wrapped`` then
  ``_sdk.py:435 in require_screen``. ``_fetch_screen_pages`` (``market_iter2.py:1189``) and the
  ``transform_data`` guards (``market_iter2.py:1247``/``:1388``) carry the same class and code,
  but they are downstream backstops -- they only ever see a page ``require_screen`` already
  accepted -- so this witness names the raising site it measured.

The fixtures are hand-authored envelope shapes, not live captures: the quotes payloads carry the
symbols each screen is asked for, at exactly the page size the shared ``limit`` negotiates, and no
claim is made that finance.yahoo.com published these values.
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_CRUMB: Final[str] = "fixture-crumb"

#: Measured effective URL of the SDK's single screen POST: the base endpoint plus its sorted,
#: encoded query parameters (the seam fake matches URL + urlencode(sorted(params))).
_SCREEN_NEEDLE: Final[str] = (
    "v1/finance/screener?corsDomain=finance.yahoo.com&crumb=fixture-crumb"
    "&formatted=false&lang=en-US&region=US"
)

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/market_iter2.py",
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]


def _quote(symbol: str, name: str, exchange: str, change: float, volume: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "exchange": exchange,
        "exchDisp": exchange,
        "sector": "Technology",
        "industry": "Software",
        "country": "United States",
        "currency": "USD",
        "chgb": change,
        "percentchange": change,
        "price": round(20.0 + volume / 1_000_000, 2),
        "lastmkttrade": 1_760_000_000,
        "dayvolume": volume,
    }


def _screen_document(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The envelope one screen fetch consumes: crumb bootstrap plus the screener POST body.

    ``yfinance.screen`` returns ``json()["finance"]["result"][0]`` and ``require_screen`` maps
    that page object's ``quotes`` list onto the row stream, so the canned body is wire-shaped:
    the result page carries offset/size/total and the quote mappings Yahoo's screener publishes.
    """
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                "url_contains": _SCREEN_NEEDLE,
                "method": "POST",
                "body": {
                    "finance": {
                        "result": [
                            {
                                "offset": 0,
                                "size": len(rows),
                                "total": len(rows),
                                "quotes": rows,
                            }
                        ],
                        "error": None,
                    }
                },
            },
        ]
    }


def _tamper(forged: str) -> dict[str, Any]:
    """Forge this row's own screener page: ``quotes`` becomes a comma-joined symbol string."""
    return {
        "op": "set",
        "path": ["responses", 2, "body", "finance", "result", 0, "quotes"],
        "value": forged,
    }


def _entry(
    document: dict[str, Any], query: dict[str, Any], forged: str, reason: str
) -> dict[str, Any]:
    return {
        "fixture_document": lambda snapshot=document: snapshot,
        "client_seam": _SEAM,
        "query": query,
        "tamper": _tamper(forged),
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SCREEN_SHAPE_INVALID",
        "tamper_reason": reason,
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    }


MODELS: Final[dict[str, Any]] = {
    "EquityGainers": _entry(
        _screen_document(
            [
                _quote("UPST", "Upstart Holdings", "NMS", 41.6, 9_800_000),
                _quote("QUBH", "Quantum-Bound Holding", "NYQ", 33.2, 4_100_000),
            ]
        ),
        {"limit": 2},
        "UPST,QUBH",
        (
            "a real upstream defect on this row's document is Yahoo's day_gainers screen answer "
            "changing the result page's quotes field from per-quote mappings into a comma-joined "
            "symbol string: every priced column the row keeps (percentchange, dayvolume, last "
            "trade) lives inside those quote mappings, and the SDK unwraps exactly that page and "
            "hands it to require_screen, whose list check at _sdk.py:435 refuses the string with "
            "YFINANCE_SCREEN_SHAPE_INVALID rather than let YfinanceEquityGainersFetcher publish "
            "gainers whose quote payload the source never returned as structure"
        ),
    ),
    "EquityLosers": _entry(
        _screen_document(
            [
                _quote("KND", "Kinderpain Energy", "NYQ", -28.4, 7_600_000),
                _quote("SONW", "Snowline Devices", "NMS", -19.9, 2_900_000),
            ]
        ),
        {"limit": 2},
        "KND,SONW",
        (
            "the day-losers screen publishes the losers by the same quotes list the gainers "
            "screen uses, so a wire-shape regression that serialises this row's quotes page into "
            "a symbol string destroys exactly the negative percentchange payloads this row is "
            "ordered by; the page reaches this repo only through require_screen, so it is the "
            "list check at _sdk.py:435 that refuses with YFINANCE_SCREEN_SHAPE_INVALID before an "
            "empty-or-string loser stream can become rows the source never quoted"
        ),
    ),
    "EquityActive": _entry(
        _screen_document(
            [
                _quote("NVDA", "NVIDIA Corporation", "NMS", 2.7, 41_000_000),
                _quote("TSLA", "Tesla, Inc.", "NMS", -1.4, 33_500_000),
            ]
        ),
        {"limit": 2},
        "NVDA,TSLA",
        (
            "the most-active screen sorts on dayvolume inside each quote mapping, so when this "
            "row's result page arrives with quotes as a flattened symbol string the activity "
            "ranks the row exists to report are gone; the SDK hands that page to require_screen, "
            "whose list check at _sdk.py:435 fails closed with YFINANCE_SCREEN_SHAPE_INVALID "
            "rather than let a string masquerade as the ranked quote list -- the same-code "
            "transform_data guard at market_iter2.py:1247 is a backstop this tamper never reaches"
        ),
    ),
    "EquityScreener": _entry(
        _screen_document(
            [
                _quote("AAPL", "Apple Inc.", "NMS", 0.9, 52_000_000),
                _quote("MSFT", "Microsoft Corporation", "NMS", 1.3, 27_000_000),
            ]
        ),
        {"limit": 2, "price_min": 10.0},
        "AAPL,MSFT",
        (
            "the custom EquityQuery screen answers the user's typed filters with the same "
            "quotes-list contract as the predefined screens, so a defect that returns this "
            "row's matched quotes as a comma-joined string invalidates the filter semantics the "
            "result claims to honour; require_screen receives the unwrapped page before "
            "YfinanceEquityScreenerFetcher does, and its list check at _sdk.py:435 is the site "
            "that refuses with YFINANCE_SCREEN_SHAPE_INVALID rather than publish rows whose "
            "filter matches the source never structurally returned"
        ),
    ),
}
