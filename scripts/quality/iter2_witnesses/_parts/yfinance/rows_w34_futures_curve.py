"""Wave 34 shard: the offline witness for ``yfinance::FuturesCurve``.

Same governed seam as every landed yfinance row (:data:`_SEAM`, kind ``governed_sdk_session``):
the runner replaces ``opendata.data.providers.yfinance.models._sdk.governed_yfinance_session`` --
the attribute consumed as ``with attr(sdk) as session:`` at ``_sdk.py:78`` -- with a real
``curl_cffi.requests.Session`` subclass, and the installed 0.2.66 SDK builds its own cookie/crumb
orchestration, query strings and JSON parsing against that fake.  No socket is open; the fixture
bodies are hand-authored wire-shaped envelopes (``{"quoteSummary": {"result": [...], "error":
null}}`` and ``{"quoteResponse": {"result": [...], "error": null}}``), not live captures, and no
claim is made that finance.yahoo.com published these values.

Endpoint and field evidence (the OpenBB tree is an output-contract reference only in this
clean-room repo; nothing was copied from it):

``GET {base}/v10/finance/quoteSummary/{ROOT}%3DF?modules=futuresChain``
    upstream ``openbb_yfinance/utils/helpers.py:243-247`` -- ``:243`` the ``%3D``-encoded root,
    ``:244`` the ``v10/finance/quoteSummary`` path, ``:245`` ``params = {"modules":
    "futuresChain"}``, ``:247`` the ``YfData.get_raw_json`` transport this leg uses.  The module
    is published by the installed SDK at ``yfinance/const.py:159``
    (``quote_summary_valid_modules``) and the transport at ``yfinance/data.py:379``.
    It yields the chain's expiry symbol list -- nothing else: no price, no date.
``GET {base}/v7/finance/quote?symbols=<joined expiries>``
    upstream ``helpers.py:281-285`` (``get_futures_quotes`` issues exactly one joined quote
    request for that same list) and is the only surface that carries a mark.

Field-by-field, from the two legs:

``expiration``  chain symbol's contract-month code, decoded the way upstream decodes it at
                ``helpers.py:236-240`` and applied at ``helpers.py:291``.  Upstream's own column
                is that year-month *string* (``standard_models/futures_curve.py:62``), while this
                repo's contract declares ``expiration: date``, so the mapper resolves the month to
                its first day.
``price``       ``round((bid + ask) / 2, 2)``, falling back to the last price then the previous
                close -- the derivation at ``helpers.py:288-290``, including its null fill at
                ``:290``.  A quote row carrying no mark yields ``None``, which is exactly what the
                contract's required-but-nullable column means.
``date``        ``regularMarketTime`` of that expiry's quote row (the as-of stamp the source
                published for the mark).  Upstream's current branch drops this column at
                ``helpers.py:293`` and leaves it nullable in
                ``standard_models/futures_curve.py:59-61``; the source cell that answers this
                contract's required ``date`` is that stamp, and nothing is stamped from the clock.
``symbol``      the request echo (4th field, declared by the contract, labelled by no source cell;
                ``helpers.py:243`` upper-cases the root it asked for and the frame returns it).

Nothing here was retargeted to a richer-but-different surface to make a phase pass: produced
fields == declared fields == 4, ``price`` is the only nullable one and it is nullable in the
contract, and the two skips the mapper makes (a chain code without a decodable contract month, a
chain entry the quote leg does not return) exist because the *required* columns cannot be answered
for those entries -- they drop rows, never columns, and a melt with no row refuses with
``YFINANCE_FUTURES_CURVE_EMPTY`` instead of publishing an empty curve as a success.
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_ROOT: Final[str] = "ES"
_CRUMB: Final[str] = "fixture-crumb"

#: The chain leg's expiry codes.  ``ES=F`` is the continuous root: no contract-month code, so no
#: ``expiration`` can be decoded for it and the mapper drops it.  ``ESM27.CME`` is the honest
#: "expiry with no mark" case: quoted, dated, and price-less.
_CHAIN: Final[list[str]] = ["ES=F", "ESZ26.CME", "ESH27.CME", "ESM27.CME"]

#: Two distinct as-of stamps, so a reader can see ``date`` is read per quote row and not stamped
#: from the run: 2026-10-16T14:30:00Z and 2026-10-15T15:00:00Z.
_STAMP_A: Final[int] = 1792161000
_STAMP_B: Final[int] = 1792076400

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/futures_curve_iter2.py",
    "opendata/data/providers/yfinance/models/market_iter2.py",
    "opendata/data/providers/yfinance/models/iter2.py",
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
    "opendata/data/providers/yfinance/models/stock_daily.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]


def _chain_document() -> dict[str, Any]:
    """The two-leg envelope, in the order the SDK asks for it."""
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                # helpers.py:243-245 -- the one futuresChain quoteSummary leg.
                "url_contains": "modules=futuresChain",
                "body": {
                    "quoteSummary": {
                        "result": [{"futuresChain": {"futures": list(_CHAIN)}}],
                        "error": None,
                    }
                },
            },
            {
                # helpers.py:281-285 -- one joined quote leg for that expiry list.
                "url_contains": "v7/finance/quote",
                "body": {
                    "quoteResponse": {
                        "result": [
                            {
                                "symbol": "ESZ26.CME",
                                "exchange": "CME",
                                "quoteType": "FUTURE",
                                "bid": 6210.5,
                                "ask": 6211.5,
                                "lastMarket": 6211.0,
                                "regularMarketPrice": 6211.0,
                                "previousClose": 6205.25,
                                "regularMarketTime": _STAMP_A,
                            },
                            {
                                # No bid/ask: the price falls back to the last/regular mark.
                                "symbol": "ESH27.CME",
                                "exchange": "CME",
                                "quoteType": "FUTURE",
                                "regularMarketPrice": 6345.75,
                                "previousClose": 6340.0,
                                "regularMarketTime": _STAMP_B,
                            },
                            {
                                # Dated but unmarked: the contract's nullable price cell.
                                "symbol": "ESM27.CME",
                                "exchange": "CME",
                                "quoteType": "FUTURE",
                                "regularMarketTime": _STAMP_B,
                            },
                        ],
                        "error": None,
                    }
                },
            },
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "FuturesCurve": {
        "fixture_document": _chain_document,
        "client_seam": _SEAM,
        "query": {"symbol": _ROOT},
        "tamper": {
            "op": "set",
            "path": ["responses", 2, "body", "quoteSummary", "result"],
            "value": [],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_FUTURES_CHAIN_EMPTY",
        "tamper_reason": (
            "an empty quoteSummary.result keeps the futuresChain envelope well-formed at every "
            "layer the SDK reads -- the cookie and crumb legs, the encoded root "
            "ES%3DF in the path, the modules=futuresChain query string and the "
            "response.json() that YfData.get_raw_json hands back -- and only the expiry list "
            "that both columns of the curve are read from disappears, so the leg never reaches "
            "the v7/finance/quote request and no expiration, date or price can be produced; the "
            "traceback ends at the `if not result: raise "
            "YfinanceProviderError('YFINANCE_FUTURES_CHAIN_EMPTY')` guard in _chain_symbols in "
            "opendata/data/providers/yfinance/models/futures_curve_iter2.py, the site the "
            "product reaches before melting, rather than publishing a curve the source never "
            "returned"
        ),
        "source_files": list(_SOURCE_FILES),
        "contract_files": list(_CONTRACT_FILES),
    },
}
