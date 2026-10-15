"""Wave 9 shard: the eight FMP ETF rows the base witness module leaves unwitnessed.

Each of these fetchers owns a second leg that the base module's single-body engine seam cannot
serve. ``_SymbolEtfFetcher.extract_data``, ``EtfPricePerformanceFetcher`` and
``EtfHistoricalFetcher``
first call ``_require_listed_etf``, which reads the ``etf-list`` directory through
``fetch_array(..., transport=None)`` -- the governed ``get_shared_http_client()`` -- and then read
the data route. One fixed body would answer the directory with weight rows and the guard would
raise ``FMP_SYMBOL_NOT_ETF``. So every entry here declares a ``client_seam`` over the two modules
that hold that name (``models/_client.py:12`` and ``models/iter2_etf.py:14``) and publishes the
fixture as a ``{"responses": [...]}`` envelope keyed by the endpoint path, so the directory leg and
the data leg are served separately while the shipped parsing, normalization and refusal code is
still the code under test.

The refusal faces are code-only, and that is the shape the ETF contracts ship. Every typed refusal
here comes from ``_validated``/``_require_source_keys``/the identity guards in
``opendata/data/providers/fmp/models/iter2_etf.py``, which raise ``FMPProviderError`` with a code
and, except on the legacy history leg, no HTTP status and no column attribution. The tamper below
therefore names the cell it broke in ``tamper_reason`` instead of declaring ``expected_missing`` or
``expected_invalid`` faces the provider never publishes.

The ``row_model`` each entry witnesses was read from the fetcher class, not assumed:

    EtfInfo             -> opendata.data.models.fmp_etf.FmpEtfInfo        (etf/info)
    EtfCountries        -> opendata.data.models.fmp_etf.FmpEtfCountry     (etf/country-weightings)
    EtfSectors          -> opendata.data.models.fmp_etf.FmpEtfSector      (etf/sector-weightings)
    EtfHoldings         -> opendata.data.models.fmp_etf.FmpEtfHolding     (etf/holdings)
    EtfEquityExposure   -> opendata.data.models.fmp_etf.FmpEtfEquityExposure (etf/asset-exposure)
    EtfPricePerformance -> opendata.data.models.fmp_etf.FmpEtfPricePerformance (batch-etf-quotes)
    EtfSearch           -> opendata.data.models.fmp_etf.FmpEtfSearch      (etf-list)
    EtfHistorical       -> opendata.data.models.fmp_etf.FmpEtfHistorical
                          (api/v3/historical-price-full)
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"
_SOURCE_ETF = ["opendata/data/providers/fmp/models/iter2_etf.py", _FMP_SOURCE]
_CONTRACTS_ETF = ["opendata/data/models/fmp_etf.py", "opendata/data/domains.yaml"]

#: The ETF guards refuse with a code only -- no HTTP status, no column attribution face. Every
#: entry below therefore declares ``refusal_class``/``refusal_code`` and no expected faces.
_SEAM: Final = {
    "kind": "shared_http_client",
    "modules": [
        "opendata.data.providers.fmp.models._client",
        "opendata.data.providers.fmp.models.iter2_etf",
    ],
    "attr": "get_shared_http_client",
}

_DIRECTORY_URL: Final = "/stable/etf-list"

#: The directory is FmpEtfSearch (extra="forbid"), so a directory row carries exactly these
#: two cells and nothing else.
_QQQ = {"symbol": "QQQ", "name": "Invesco QQQ Trust, Series 1"}
_IVV = {"symbol": "IVV", "name": "iShares Core S&P 500 ETF"}
_SPY = {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"}


def _envelope(*, directory: list[dict[str, Any]], url: str, body: Any) -> dict[str, Any]:  # noqa: ANN401
    """Serve the ETF directory and one data route as two separate fixture responses."""
    return {
        "responses": [
            {"url_contains": _DIRECTORY_URL, "status": 200, "body": directory},
            {"url_contains": url, "status": 200, "body": body},
        ]
    }


def _etf_info_document() -> dict[str, Any]:
    """One ``etf/info`` record for the requested fund."""
    return _envelope(
        directory=[_QQQ],
        url="/stable/etf/info",
        body=[
            {
                "symbol": "QQQ",
                "name": "Invesco QQQ Trust, Series 1",
                "description": (
                    "The Fund generally seeks to provide investment results that correspond, "
                    "before expenses, generally to the price and yield performance of the "
                    "Nasdaq-100 Index."
                ),
                "isin": "US46132F1056",
                "assetClass": "Equity",
                "domicile": "US",
                "etfCompany": "Invesco",
                "expenseRatio": 0.2,
                "assetsUnderManagement": 351_800_000_000,
                "avgVolume": 44_128_900,
                "inceptionDate": "1999-03-10",
                "nav": 512.34,
                "navCurrency": "USD",
                "holdingsCount": 101,
                "isActivelyTrading": True,
                "updatedAt": "2026-01-16",
            }
        ],
    )


def _etf_countries_document() -> dict[str, Any]:
    """Two ``etf/country-weightings`` rows, as the route publishes them."""
    return _envelope(
        directory=[_QQQ],
        url="/stable/etf/country-weightings",
        body=[
            {"symbol": "QQQ", "country": "United States", "weightPercentage": 97.41},
            {"symbol": "QQQ", "country": "Netherlands", "weightPercentage": 1.87},
            {"symbol": "QQQ", "country": "Ireland", "weightPercentage": 0.72},
        ],
    )


def _etf_sectors_document() -> dict[str, Any]:
    """Two ``etf/sector-weightings`` rows for one fund."""
    return _envelope(
        directory=[_QQQ],
        url="/stable/etf/sector-weightings",
        body=[
            {"symbol": "QQQ", "sector": "Technology", "weightPercentage": 61.34},
            {"symbol": "QQQ", "sector": "Consumer Cyclical", "weightPercentage": 15.02},
            {"symbol": "QQQ", "sector": "Communication Services", "weightPercentage": 22.41},
        ],
    )


def _etf_holdings_document() -> dict[str, Any]:
    """Three ``etf/holdings`` rows, keeping the source's own weight units."""
    return _envelope(
        directory=[_QQQ],
        url="/stable/etf/holdings",
        body=[
            {
                "symbol": "QQQ",
                "asset": "AAPL",
                "name": "Apple Inc.",
                "isin": "US0378331005",
                "sharesNumber": 191_456_700,
                "weightPercentage": 8.42,
                "marketValue": 48_312_000_000,
                "updatedAt": "2025-12-31",
            },
            {
                "symbol": "QQQ",
                "asset": "MSFT",
                "name": "Microsoft Corporation",
                "isin": "US5949181045",
                "sharesNumber": 105_238_100,
                "weightPercentage": 8.06,
                "marketValue": 46_271_000_000,
                "updatedAt": "2025-12-31",
            },
            {
                "symbol": "QQQ",
                "asset": "NVDA",
                "name": "NVIDIA Corp.",
                "isin": "US67066G1040",
                "sharesNumber": 148_204_500,
                "weightPercentage": 7.94,
                "marketValue": 45_592_000_000,
                "updatedAt": "2025-12-31",
            },
        ],
    )


def _etf_equity_exposure_document() -> dict[str, Any]:
    """``etf/asset-exposure`` rows naming one equity across three directory-verified funds."""
    return _envelope(
        directory=[_QQQ, _SPY],
        url="/stable/etf/asset-exposure",
        body=[
            {
                "symbol": "QQQ",
                "asset": "AAPL",
                "sharesNumber": 191_456_700,
                "weightPercentage": 8.42,
                "marketValue": 48_312_000_000,
            },
            {
                "symbol": "SPY",
                "asset": "AAPL",
                "sharesNumber": 152_340_000,
                "weightPercentage": 7.01,
                "marketValue": 40_118_000_000,
            },
        ],
    )


def _quote_row(symbol: str, price: float, change_percentage: float) -> dict[str, Any]:
    """One ``batch-etf-quotes`` record with the columns the ETF quote contract declares."""
    return {
        "symbol": symbol,
        "name": f"{symbol} exchange-traded fund",
        "price": price,
        "changePercentage": change_percentage,
        "change": round(price * change_percentage / 100, 4),
        "volume": 41_285_000,
        "dayLow": round(price * 0.988, 4),
        "dayHigh": round(price * 1.011, 4),
        "exchange": "NASDAQ",
        "open": round(price * 0.994, 4),
        "previousClose": round(price / (1 + change_percentage / 100), 4),
        "timestamp": 1_768_736_400,
    }


def _etf_price_performance_document() -> dict[str, Any]:
    """A quote batch in which exactly one row is the requested fund."""
    return _envelope(
        directory=[_QQQ, _IVV],
        url="/stable/batch-etf-quotes",
        body=[
            _quote_row("QQQ", 512.34, 1.32),
            _quote_row("IVV", 617.55, -0.41),
        ],
    )


def _etf_search_document() -> dict[str, Any]:
    """The ETF directory the search route reads with no pagination of its own."""
    return {"responses": [{"url_contains": _DIRECTORY_URL, "status": 200, "body": [_QQQ, _IVV]}]}


def _history_row(day: str, close: float) -> dict[str, Any]:
    """One legacy ``historical-price-full`` record, published with all thirteen source cells."""
    return {
        "date": day,
        "open": round(close * 0.996, 4),
        "high": round(close * 1.008, 4),
        "low": round(close * 0.989, 4),
        "close": close,
        "volume": 38_412_000,
        "unadjustedVolume": 38_412_000,
        "change": round(close * 0.0031, 4),
        "changePercent": 0.31,
        "vwap": round(close * 0.9995, 4),
        "label": "0.31%",
        "changeOverTime": 1.94,
    }


def _etf_historical_document() -> dict[str, Any]:
    """The legacy ETF history envelope: the document, not the row, owns the symbol."""
    return _envelope(
        directory=[_QQQ],
        url="/api/v3/historical-price-full/QQQ",
        body={
            "symbol": "QQQ",
            "historical": [
                _history_row("2026-01-05", 508.12),
                _history_row("2026-01-06", 511.44),
                _history_row("2026-01-07", 512.34),
            ],
        },
    )


MODELS: Final[dict[str, Any]] = {
    "EtfInfo": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_info_document,
        "query": {"symbol": "QQQ"},
        "tamper": {"op": "drop", "path": ["responses", 1, "body", 0, "symbol"], "column": "symbol"},
        "refusal_code": "FMP_WRONG_SYMBOL",
        "tamper_reason": (
            "etf/info answers for the fund that was asked for, and the fetcher refuses a row that "
            "cannot be attributed to the requested ticker rather than inventing the identity; the "
            "contract declares symbol as required trimmed text, so a row without it must raise "
            "FMP_WRONG_SYMBOL instead of publishing an ownerless fund profile"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfCountries": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_countries_document,
        "query": {"symbol": "QQQ"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 1, "body", 0, "weightPercentage"],
            "column": "weight",
        },
        "refusal_code": "FMP_BAD_SHAPE",
        "tamper_reason": (
            "a country allocation is the pair (country, weightPercentage) and the contract "
            "declares weight as a required finite JSON number; the source key is the cell the "
            "row's weight is read from, so a weighting row that carries no weight must raise "
            "FMP_BAD_SHAPE rather than enter a 0% exposure the route never stated"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfSectors": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_sectors_document,
        "query": {"symbol": "QQQ"},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", 1, "sector"],
            "value": " Consumer Cyclical ",
            "column": "sector",
        },
        "refusal_code": "FMP_BAD_SHAPE",
        "tamper_reason": (
            "the sector-weightings route publishes the sector name as trimmed text and the "
            "contract rejects leading or trailing whitespace rather than trimming it into a "
            "different string; the padded cell would split one sector across two keys, so the "
            "guard must raise FMP_BAD_SHAPE"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfHoldings": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_holdings_document,
        "query": {"symbol": "QQQ"},
        "tamper": {"op": "drop", "path": ["responses", 1, "body", 2, "asset"], "column": "asset"},
        "refusal_code": "FMP_BAD_SHAPE",
        "tamper_reason": (
            "a holding is a position in something, and the contract declares asset as required "
            "text alongside symbol; a holdings row with no asset cell cannot state what the fund "
            "owns, so _validated must raise FMP_BAD_SHAPE instead of publishing a blank position"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfEquityExposure": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_equity_exposure_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", 0, "asset"],
            "value": "MSFT",
            "column": "equity_symbol",
        },
        "refusal_code": "FMP_WRONG_EQUITY_SYMBOL",
        "tamper_reason": (
            "etf/asset-exposure is asked for one equity, so every returned row must name that "
            "ticker in its asset cell; a row that answers MSFT when AAPL was requested would "
            "silently widen the exposure board to another security, so the fetcher must refuse "
            "with FMP_WRONG_EQUITY_SYMBOL"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfPricePerformance": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_price_performance_document,
        "query": {"symbol": "QQQ"},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", 0, "changePercentage"],
            "value": "+1.32%",
            "column": "one_day",
        },
        "refusal_code": "FMP_BAD_SHAPE",
        "tamper_reason": (
            "the batch quote states the one-day change as a JSON number in percentage points and "
            "the row's one_day is exactly that cell; a sign-and-unit formatted string is a "
            "different unit than the contract declares, so _require_source_keys validation must "
            "raise FMP_BAD_SHAPE rather than parse a guess"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfSearch": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_search_document,
        "query": {"query": "s&p 500"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 1, "symbol"],
            "value": "QQQ",
            "column": "symbol",
        },
        "refusal_code": "FMP_DUPLICATE_ETF_SYMBOL",
        "tamper_reason": (
            "the directory is the identity list for ETF-only search, so one ticker may appear "
            "once; the tamper makes the second row repeat QQQ, which the dedupe guard must refuse "
            "with FMP_DUPLICATE_ETF_SYMBOL instead of letting two rows claim one fund symbol"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
    "EtfHistorical": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "fixture_document": _etf_historical_document,
        "query": {"symbol": "QQQ", "start_date": "2026-01-05", "end_date": "2026-01-07"},
        "tamper": {
            "op": "set",
            "path": ["responses", 1, "body", "historical", 2, "date"],
            "value": "2026-02-11",
            "column": "date",
        },
        "refusal_code": "FMP_OUT_OF_WINDOW_ROW",
        "tamper_reason": (
            "the legacy history page is asked for 2026-01-05..2026-01-07, and a row dated outside "
            "the requested window is not evidence for it; the fetcher must refuse with "
            "FMP_OUT_OF_WINDOW_ROW rather than pad the series with a session the caller never "
            "bounded"
        ),
        "source_files": _SOURCE_ETF,
        "contract_files": _CONTRACTS_ETF,
    },
}
