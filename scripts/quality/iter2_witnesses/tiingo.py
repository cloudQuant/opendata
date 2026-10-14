"""Offline witness configuration for the ``tiingo`` provider.

Six of tiingo's seven rows are hand-written bounded adapters in
``opendata/data/providers/tiingo/models/iter2.py``: their shape refusal is the adapter's own
code-only ``TIINGO_SHAPE_INVALID`` raised from ``transform_data`` around a failed strict row
build, so those entries declare no attributed detail. The seventh, ``CryptoHistorical``, is the
declaration engine's ``CRYPTO_HISTORICAL`` ModelSpec, whose numeric guard attributes the refused
column -- that entry therefore declares ``expected_rejected`` from the contract's own column
name, never from a captured run.

Witnesses run exclusively through the offline seam (``http_transport`` / the engine's
``transport``) served by ``FixedResponseTransport``. No leg touches the network and no
``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.
"""

from __future__ import annotations

from typing import Any, Final


def _daily_bar(index: int) -> dict[str, Any]:
    """One Tiingo daily/IEX bar in the source keys ``map_record`` reads."""
    day = 2 + index
    return {
        "date": f"2026-01-{day:02d}T00:00:00.000Z",
        "open": 180.5 + index,
        "high": 182.25 + index,
        "low": 179.5 + index,
        "close": 181.75 + index,
        "volume": 45_000_000 + index,
        "adjOpen": 180.5 + index,
        "adjHigh": 182.25 + index,
        "adjLow": 179.5 + index,
        "adjClose": 181.75 + index,
        "adjVolume": 45_000_000 + index,
        "divCash": 0.25,
        "splitFactor": 1.0,
    }


def _tiingo_equity_historical_document() -> list[dict[str, Any]]:
    """Two daily bars for the one requested symbol."""
    return [_daily_bar(0), _daily_bar(1)]


def _tiingo_etf_historical_document() -> list[dict[str, Any]]:
    """Two ETF bars on the same documented EOD shape."""
    return [_daily_bar(0), _daily_bar(1)]


def _fx_bar(index: int) -> dict[str, Any]:
    """One FX observation: a timestamped OHLC cell, never a calendar-date-only bar."""
    day = 2 + index
    return {
        "date": f"2026-01-{day:02d}T17:00:00.000Z",
        "open": 0.8512 + index / 1000,
        "high": 0.8601 + index / 1000,
        "low": 0.849 + index / 1000,
        "close": 0.8555 + index / 1000,
    }


def _tiingo_currency_historical_document() -> list[dict[str, Any]]:
    """Two FX bars for the requested currency pair."""
    return [_fx_bar(0), _fx_bar(1)]


def _tiingo_trailing_dividend_yield_document() -> list[dict[str, Any]]:
    """The single distribution-yield observation the corporate-actions route publishes.

    ``trailingDiv1Y`` is requested through ``columns=trailingDiv1Y`` and arrives as source text,
    which the adapter parses -- a JSON number there is outside the documented shape.
    """
    return [{"date": "2026-01-02T17:00:00.000Z", "trailingDiv1Y": "1.25"}]


def _news_record(item_id: int) -> dict[str, Any]:
    """One news item in the keys ``_TiingoNewsFetcher.map_record`` reads."""
    return {
        "id": item_id,
        "title": "A fixture market headline",
        "url": "https://www.tiingo.com/fixtures/story",
        "description": "Fixture description text",
        "publishedDate": "2026-01-02T09:30:00Z",
        "crawlDate": "2026-01-02T09:31:00Z",
        "source": "Fixture Wire",
        "tickers": ["AAPL", "MSFT"],
        "tags": ["earnings", "fixture"],
    }


def _tiingo_company_news_document() -> list[dict[str, Any]]:
    """Two news items filtered to the requested ticker."""
    return [_news_record(501), _news_record(502)]


def _tiingo_world_news_document() -> list[dict[str, Any]]:
    """Two items from the unfiltered global feed."""
    return [_news_record(601), _news_record(602)]


def _crypto_price_item(index: int) -> dict[str, Any]:
    """One ``priceData[]`` cell in the spellings ``CRYPTO_PRICE_FIELDS`` declares."""
    day = 2 + index
    return {
        "date": f"2026-01-{day:02d}T00:00:00.000Z",
        "open": 100_250.5 + index,
        "high": 101_000.25 + index,
        "low": 99_800.0 + index,
        "close": 100_500.75 + index,
        "volume": 1_250.5 + index,
        "tradesDone": 30_000 + index,
        "volumeNotional": 125_000_000.5 + index,
    }


def _tiingo_crypto_historical_document() -> list[dict[str, Any]]:
    """One ticker object carrying the nested ``priceData`` array the plan explodes."""
    return [
        {
            "ticker": "BTC-USD",
            "priceData": [_crypto_price_item(0), _crypto_price_item(1)],
        }
    ]


_ITER2_MODEL: Final[str] = "opendata/data/providers/tiingo/models/iter2.py"
_SPEC_MODEL: Final[str] = "opendata/data/providers/tiingo/models/crypto_historical.py"
_CONTRACT: Final[str] = "opendata/data/models/tiingo.py"
_DOMAINS: Final[str] = "opendata/data/domains.yaml"
_ADAPTER_SOURCE: Final[tuple[str, ...]] = (_ITER2_MODEL, "opendata/data/providers/tiingo/specs.py")
_ENGINE_SOURCE: Final[tuple[str, ...]] = (
    _SPEC_MODEL,
    "opendata/data/providers/tiingo/specs.py",
    "opendata/data/providers/_engine/http_json.py",
)


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.tiingo.registration",
    "credential_env": {"TIINGO_TOKEN": "fixture-token-never-sent"},  # nosec B105  # offline placeholder, never sent
    "allowed_hosts": ["api.tiingo.com"],
    "models": {
        "EquityHistorical": {
            "fixture_document": _tiingo_equity_historical_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "drop", "path": [0, "close"], "column": "close"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a bar without its documented 'close' cell makes the strict "
                "TiingoEquityHistorical build raise, and the adapter must surface it as "
                "TIINGO_SHAPE_INVALID rather than shipping a bar with no close"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "EtfHistorical": {
            "fixture_document": _tiingo_etf_historical_document,
            "query": {"symbol": "SPY"},
            "tamper": {"op": "set", "path": [0, "volume"], "value": True, "column": "volume"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "a boolean where the contract declares an integer share count is the numeric "
                "guard's own refusal: the adapter must surface TIINGO_SHAPE_INVALID instead of "
                "letting a flag become 1 share"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "CurrencyHistorical": {
            "fixture_document": _tiingo_currency_historical_document,
            "query": {"symbol": "EURUSD"},
            "tamper": {"op": "drop", "path": [0, "date"], "column": "date"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "dropping the required 'date' cell leaves an FX observation with no moment, so "
                "the adapter must surface TIINGO_SHAPE_INVALID rather than build a row whose "
                "non-nullable datetime is empty"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "TrailingDividendYield": {
            "fixture_document": _tiingo_trailing_dividend_yield_document,
            "query": {"symbol": "AAPL"},
            "tamper": {
                "op": "set",
                "path": [0, "trailingDiv1Y"],
                "value": 123.5,
                "column": "trailing_dividend_yield",
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "the requested 'columns=trailingDiv1Y' payload is documented as source text the "
                "adapter parses; publishing a JSON number instead is outside that shape, so the "
                "adapter must surface TIINGO_SHAPE_INVALID rather than guess at the spelling"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "CompanyNews": {
            "fixture_document": _tiingo_company_news_document,
            "query": {"symbol": "AAPL"},
            "tamper": {"op": "drop", "path": [0, "id"], "column": "id"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "dropping the required integer 'id' makes the strict TiingoCompanyNews build "
                "refuse the item, so the adapter must surface TIINGO_SHAPE_INVALID rather than "
                "publish an unidentifiable news row"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "WorldNews": {
            "fixture_document": _tiingo_world_news_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": [0, "tickers"],
                "value": "AAPL,MSFT",
                "column": "tickers",
            },
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "the contract declares 'tickers' a list of strings and the documented body "
                "publishes a JSON array; collapsing it to one comma-joined string is not that "
                "shape, so the adapter must surface TIINGO_SHAPE_INVALID"
            ),
            "source_files": list(_ADAPTER_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
        "CryptoHistorical": {
            "fixture_document": _tiingo_crypto_historical_document,
            "query": {"symbol": "BTC-USD"},
            "tamper": {
                "op": "set",
                "path": [0, "priceData", 0, "volume"],
                "value": True,
                "column": "volume",
            },
            "expected_rejected": ["volume"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "TIINGO_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a boolean where CRYPTO_HISTORICAL declares a float 'volume' column is "
                "the engine's own numeric guard, so the refusal must be TIINGO_SHAPE_INVALID "
                "attributing rejected=('volume',)"
            ),
            "source_files": list(_ENGINE_SOURCE),
            "contract_files": [_CONTRACT, _DOMAINS],
        },
    },
}
