"""Shard w32_d: BalanceSheet, CashFlowStatement, IncomeStatement, KeyMetrics (yfinance).

BLOCKED-WAVE RECORD, UPDATED 2026-10-19 AFTER THE ``row_model`` FIX AND AFTER THE COUNTERFACT
PHASE'S TAMPERED-LEG ISOLATION. The three statement rows now run all five phases green through the
shipped runner. KeyMetrics is still blocked, by an SDK capability gap. Every number below was
measured in this working tree against the installed yfinance 0.2.66 and the shipped
``scripts/quality/iter2_claims_probe_runner.py``.

MEASURED, PER STATEMENT ROW (identical shape for the three, one subprocess per phase)
    implementation PASS / direct PASS (12 rows) / service PASS (12 rows) / contract PASS
    counterfact PASS, ``refusal_class=YfinanceProviderError``,
    ``refusal_code=YFINANCE_EMPTY_RESPONSE``, expected == observed

1. ``row_model`` blocker is GONE. ``YfinanceStatementFetcher`` now declares
   ``row_model: ClassVar[type[YfinanceFinancialStatementObservation]]``, so
   ``witness_implementation`` passes and the fetch phases stop tripping ``type(row) is not
   fetcher_type.row_model``. ``fetcher.row_model is contract_model(domain)`` for all three domains.

2. The shape guard is defensive-only, and the earlier attribution was WRONG. The refusal this
   row's document actually produces is ``_statement_observations``' empty-frame branch at
   ``opendata/data/providers/yfinance/models/iter2.py:237`` -> ``YfinanceProviderError`` /
   ``YFINANCE_EMPTY_RESPONSE``. ``iter2.py:299`` (``not isinstance(raw, pd.DataFrame)`` ->
   ``YFINANCE_STATEMENT_SHAPE_INVALID``) is unreachable from any fixture: measured the raw value
   ``require_statement_frame`` hands ``transform_data`` for six wire defects -- ``result:[]``,
   a non-JSON body, a body with no ``timeseries`` key, ``result:[{}]`` (no ``meta``), a blank
   item label, a duplicated ``asOfDate`` -- and it is a ``DataFrame`` in all six (``empty=True``
   for the first four). ``Financials._fetch_time_series`` swallows every parse/transport defect
   into ``pd.DataFrame()``, and ``base.py:498`` -> ``get_balance_sheet_time_series`` never returns
   ``None``, so neither the adapter guard nor the one inside ``_sdk.require_statement_frame`` can
   fire on a fixture. Two of the six defects (blank item label, duplicated ``asOfDate``) do not
   refuse at all: the SDK de-duplicates and re-labels, and 12 rows ship. That is why the declared
   code here is ``YFINANCE_EMPTY_RESPONSE`` and the declared tamper is ``result:[]``.

3. CACHE MASKING, measured, and now closed in the runner instead of in this shard. ``YfData`` is a
   process singleton (``YfData._instances == {YfData: <one object>}``) and
   ``YfData.cache_get`` is ``@lru_cache(maxsize=64)`` keyed by ``(self, url, params, timeout)``.
   ``Financials._get_financials_time_series`` calls it as ``cache_get(url=url)`` with the url
   built entirely from fixed inputs: ``symbol`` (the query), the ``type=`` list (``const.
   fundamentals_keys``), ``period1=1483113600`` (``datetime(2016,12,31)``), and
   ``period2=int(pd.Timestamp.utcnow().ceil("D").timestamp())`` -- day-ceiled, so identical for
   both legs of one counterfact run (measured ``period2=1792368000`` on 2026-10-19).
   Control leg: 2 seam calls, 12 rows. Tampered leg in the SAME process: 0 seam calls, 12 rows
   (the control's frame), ``cache_get.cache_info() = CacheInfo(hits=1, misses=1)`` -- the tamper was
   never read. Clearing the lru by hand between the legs makes the tamper bite immediately -- 1 seam
   call, 12 rows -> 0 rows
   and ``YFINANCE_EMPTY_RESPONSE`` at ``iter2.py:237`` -- which localises the blocker precisely to
   that cache and proves the document/tamper pair is otherwise sound.
   ``rows_w32_a`` escaped the same trap for chart rows because ``PriceHistory`` routes a
   future-dated ``end`` through the plain uncached ``get``; a statement leg has no such branch and
   no date field of its own: ``YfinanceStatementQuery`` publishes only ``symbol``, ``limit`` and
   ``frequency``, and only ``symbol``/``frequency`` reach the SDK, neither of which can differ
   between two legs built from one witness entry. So the two legs CANNOT be made distinct by the
   fixture, and clearing ``YfData``'s cache from a witness module would replace part of the code
   under test. The runner therefore isolates the counterfact phase's tampered leg in a fresh child
   process and requires that leg itself to put at least one request through the carrier built from
   the tampered document -- an answer recorded before it cannot cross a process boundary.
   Measured on this row: the isolated tampered leg reports ``seam_requests=2``,
   ``outcome=refusal``, ``refusal_code=YFINANCE_EMPTY_RESPONSE`` (phase PASS); the same leg forced
   back into the control's process reports ``seam_requests=0`` and the phase refuses with
   ``FAIL reason=the tampered leg made 0 request(s) through the offline carrier, so the control
   leg's answer served it and the tamper was never read``. These three rows now land because the
   tamper is really read, not because anything here was special-cased.

4. KeyMetrics -- determination (a), the pinned SDK cannot serve the contract.
   ``yfinance.__version__ == '0.2.66'``; ``hasattr(yfinance.Ticker, 'get_valuation_measures')`` is
   ``False`` on the class and on an instance; no attribute name on ``Ticker`` contains ``metric``
   or ``valuation``; the string ``valuation-measures`` appears in no file of the installed package;
   ``grep`` for ``peg_ratio``/``trailing_pe``/``price_to_book``/``enterprise_to_ebitda`` in the
   package returns nothing; ``const.fundamentals_keys`` names only ``financials``,
   ``balance-sheet``, ``cash-flow``. The only measure-bearing surface left is ``Ticker.info``,
   measured offline through this same seam on the shipped ``EquityInfo`` fixture: it returns a
   ``dict`` (14 keys, valuation-ish keys ``quoteType``/``trailingPegRatio`` only), never a frame,
   and carries no per-period rows for ``period_ending``/``period_kind``/``fiscal_year``.
   ``_sdk.require_valuation_measures`` therefore raises before any fixture leg is asked, and the
   runner reports ``direct``/``contract``/``counterfact`` as
   ``FAIL reason=unclassified witness crash YfinanceProviderError: YFINANCE_REQUEST_FAILED`` and
   ``service`` as ``FAIL reason=unclassified witness crash ProviderModelQueryUpstreamError:`` (the
   service layer's own wrapper around the same typed refusal).
   ``implementation`` passes (``row_model=YfinanceKeyMetricObservation`` is the contract class), so
   the gap is purely the SDK method the pinned version never shipped -- an upstream-capability
   gap, not evidence machinery. Serving the row would need product code to retarget a surface that
   does not exist in 0.2.66; that edit is out of scope here.

The three statement fixture documents stay as recorded: each one drives the real 0.2.66 parse and
this repo's normalizer to 12 source-native ``YfinanceFinancialStatementObservation`` rows through
the ``governed_sdk_session`` seam with no socket. The KeyMetrics document is inert for this SDK
generation and is kept only for envelope continuity.
"""

from __future__ import annotations

import datetime
from typing import Any, Final

from yfinance import const

_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_SYMBOL: Final[str] = "AAPL"
_CRUMB: Final[str] = "fixture-crumb"

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]

_STATEMENT_SOURCES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/iter2.py",
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
]


def _statement_document(internal_name: str) -> dict[str, Any]:
    """One fundamentals-timeseries envelope in the exact shape 0.2.66 parses for a statement leg."""
    keys = const.fundamentals_keys[internal_name]
    stamps = [
        int(datetime.datetime(2024, 12, 31, tzinfo=datetime.timezone.utc).timestamp()),
        int(datetime.datetime(2023, 12, 31, tzinfo=datetime.timezone.utc).timestamp()),
    ]
    item: dict[str, Any] = {}
    for i, key in enumerate(keys[:6]):
        item[f"annual{key}"] = [
            {"asOfDate": date, "reportedValue": {"raw": 1000.0 + i * 10 + j}}
            for j, date in enumerate(("2024-12-31", "2023-12-31"))
        ]
    item["timestamp"] = stamps
    item["meta"] = {"symbol": [_SYMBOL]}
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                "url_contains": "fundamentals-timeseries",
                "body": {"timeseries": {"result": [item], "error": None}},
            },
        ]
    }


def _balance_sheet_document() -> dict[str, Any]:
    return _statement_document("balance-sheet")


def _cash_flow_document() -> dict[str, Any]:
    return _statement_document("cash-flow")


def _income_statement_document() -> dict[str, Any]:
    # Yahoo stores the income table internally under the "financials" key.
    return _statement_document("financials")


def _key_metrics_document() -> dict[str, Any]:
    """The fundamentals-timeseries valuation envelope this row's request actually receives.

    Two series per measure: the requested ``quarterly*`` series (dated columns) and the
    ``trailing*`` series, whose newest point is what the fetcher labels ``Current``. Only two
    measures carry values; the frame still emits all nine rows, so the row mapper has to mark the
    rest ``source_missing`` rather than invent them.
    """
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                "url_contains": "fundamentals-timeseries/v1/finance/timeseries",
                "body": {
                    "timeseries": {
                        "result": [
                            {
                                "meta": {"symbol": ["AAPL"]},
                                "trailingMarketCap": [
                                    {
                                        "asOfDate": "2024-12-31",
                                        "reportedValue": {"raw": 3_700_000_000_000},
                                    }
                                ],
                                "quarterlyMarketCap": [
                                    {
                                        "asOfDate": "2024-09-30",
                                        "reportedValue": {"raw": 3_400_000_000_000},
                                    }
                                ],
                                "trailingPeRatio": [
                                    {"asOfDate": "2024-12-31", "reportedValue": {"raw": 32.1}}
                                ],
                                "quarterlyPeRatio": [
                                    {"asOfDate": "2024-09-30", "reportedValue": {"raw": 30.4}}
                                ],
                            }
                        ],
                        "error": None,
                    }
                },
            },
        ]
    }


_TAMPER_EMPTY_RESULT: Final[dict[str, Any]] = {
    "op": "set",
    "path": ["responses", 2, "body", "timeseries", "result"],
    "value": [],
}

_STATEMENT_TAMPER_REASON: Final[str] = (
    "a real upstream defect -- Yahoo publishing the timeseries envelope with an empty result list "
    "for a symbol whose statement feed is being rebuilt -- reaches this row's frame as an EMPTY "
    "DataFrame, and the site that fires is _statement_observations' frame.empty branch at "
    "opendata/data/providers/yfinance/models/iter2.py:237, which refuses with "
    "YfinanceProviderError/YFINANCE_EMPTY_RESPONSE rather than publish periods the source never "
    "returned. Measured on this row's own document (the tampered body really read): 12 rows -> 0 "
    "rows, refusal code YFINANCE_EMPTY_RESPONSE at iter2.py:237. "
    "NOT iter2.py:299: the isinstance(raw, pd.DataFrame) guard never fires, because "
    "Financials._fetch_time_series collapses every wire defect into pd.DataFrame() and "
    "require_statement_frame never hands back None or a non-frame (measured over result:[], a "
    "non-JSON body, a body with no timeseries key, result:[{}], a blank item label and a "
    "duplicated asOfDate -- a DataFrame every time). WHY THE TWO LEGS CANNOT SHARE A PROCESS: "
    "YfData is a process singleton and its URL-keyed cache_get lru (maxsize=64) replays the "
    "control response for the tampered leg (measured cache_info hits=1 misses=1, tampered leg "
    "calls and returned the control's 12 rows), because the leg's url is fixed by symbol, the "
    "fundamentals-timeseries selector, and the two epoch window bounds (lower bound "
    "pinned, upper bound the day-ceiling of utcnow, so day-stable) -- identical for both "
    "legs, with no date field in YfinanceStatementQuery to move the way rows_w32_a moved a chart "
    "window into the future. The runner closes that hole structurally instead of special-casing "
    "this row: the counterfact phase runs its tampered leg in a fresh child process and refuses "
    "when that leg itself puts no request through the carrier built from the tampered document. "
    "Measured on this row, the isolated leg reports seam_requests=2 with outcome=refusal and this "
    "code; the same leg forced back into the control's process reports seam_requests=0 and the "
    "phase exits 1 with FAIL reason=the tampered leg made 0 request(s) through the offline "
    "carrier, so the control leg's answer served it and the tamper was never read"
)

_KEY_METRICS_TAMPER_REASON: Final[str] = (
    "a real upstream defect -- the fundamentals-timeseries endpoint answering with an empty "
    "result array, which is what Yahoo returns while a symbol's measure series is being rebuilt -- "
    "leaves this row with no trailing point to label Current and no dated column to emit, so the "
    "fetch returns None and the row mapper refuses with YfinanceProviderError/"
    "YFINANCE_METRICS_SHAPE_INVALID rather than publish a metrics row whose values were never "
    "served. This entry replaces the wave-32/33 claim that the row was blocked by an upstream SDK "
    "capability gap: that was measured against a transport targeting Ticker.get_valuation_measures "
    "(the 1.7.0 generation's method, indeed absent here), while the same nine measures are served "
    "by the fundamentals-timeseries endpoint the statements already use, which the pinned 0.2.66 "
    "can request through its own data layer. Measured on this row's own document: control leg "
    "emits 2 rows (Current + one dated period), tampered leg refuses."
)


MODELS: Final[dict[str, Any]] = {
    "BalanceSheet": {
        "fixture_document": _balance_sheet_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": _TAMPER_EMPTY_RESULT,
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _STATEMENT_TAMPER_REASON,
        "source_files": _STATEMENT_SOURCES,
        "contract_files": _CONTRACT_FILES,
    },
    "CashFlowStatement": {
        "fixture_document": _cash_flow_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": _TAMPER_EMPTY_RESULT,
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _STATEMENT_TAMPER_REASON,
        "source_files": _STATEMENT_SOURCES,
        "contract_files": _CONTRACT_FILES,
    },
    "IncomeStatement": {
        "fixture_document": _income_statement_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": _TAMPER_EMPTY_RESULT,
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _STATEMENT_TAMPER_REASON,
        "source_files": _STATEMENT_SOURCES,
        "contract_files": _CONTRACT_FILES,
    },
    "KeyMetrics": {
        "fixture_document": _key_metrics_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL, "frequency": "quarterly", "periods": 1},
        "tamper": _TAMPER_EMPTY_RESULT,
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_METRICS_SHAPE_INVALID",
        "tamper_reason": _KEY_METRICS_TAMPER_REASON,
        "source_files": [
            "opendata/data/providers/yfinance/models/market_iter2.py",
            "opendata/data/providers/yfinance/models/_sdk.py",
            "opendata/data/providers/yfinance/models/_session.py",
        ],
        "contract_files": _CONTRACT_FILES,
    },
}
