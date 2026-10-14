"""Hand-authored contract fixtures and bounded tests for the Tradier clean-room adapter."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.models.tradier import (
    TradierEquityHistorical,
    TradierEquityQuote,
    TradierEquitySearch,
    TradierEtfHistorical,
    TradierOptionChain,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.tradier.models import _client as _tradier_client
from opendata.data.providers.tradier.models._client import TradierProviderError
from opendata.data.providers.tradier.models.equity_historical import (
    TradierEquityHistoricalFetcher,
)
from opendata.data.providers.tradier.models.equity_quote import TradierEquityQuoteFetcher
from opendata.data.providers.tradier.models.equity_search import TradierEquitySearchFetcher
from opendata.data.providers.tradier.models.etf_historical import TradierEtfHistoricalFetcher
from opendata.data.providers.tradier.models.options_chains import TradierOptionsChainsFetcher
from opendata.data.providers.tradier.provider import PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

_ROOT = Path(__file__).parents[1]
_FIXTURES = _ROOT / "docs/evidence/C100/tradier/fixtures"
_START = date(2024, 1, 2)
_END = date(2024, 1, 3)
_EXPIRATION = date(2024, 1, 19)


class _Response:
    def __init__(self, document: object, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._document = document

    def json(self) -> object:
        return self._document


class _FakeClient:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        call = {"url": url, **kwargs}
        self.calls.append(call)
        answer = self.answer(urlsplit(url).path.removeprefix("/v1"), kwargs["params"])
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADIER_TOKEN", "fixture-token-not-a-real-credential")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _client(monkeypatch: pytest.MonkeyPatch, answer: Any) -> _FakeClient:
    fake = _FakeClient(answer)
    monkeypatch.setattr(_tradier_client, "get_shared_http_client", lambda: fake)
    return fake


def _authorized_context(canonical_model: str | None) -> FetchContext:
    assert canonical_model is not None
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="tradier",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture-only-no-live-requests",
        task_attempts=256,
        source_attempts=64,
        allowed_hosts=("api.tradier.com",),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        profile=profile,
    )
    return FetchContext(
        request_budget=RequestBudget(
            task_attempts=256,
            source_attempts=64,
            grants=(grant,),
            profile=profile,
        )
    )


def _fetch(fetcher: Any, **query: Any) -> Any:
    """Run a provider call with exact fixture-only framework authorization."""
    return fetcher.fetch(ctx=_authorized_context(fetcher.canonical_model), **query)


def _lookup(symbol: str, security_type: str) -> dict[str, Any]:
    return {
        "securities": {
            "security": {
                "symbol": symbol,
                "exchange": "Q",
                "type": security_type,
                "description": f"{symbol} listing",
            }
        }
    }


def _history(symbol: str, *, singleton: bool = False) -> dict[str, Any]:
    document = _fixture("history-singleton.json" if singleton else "history.json")
    document["history"]["symbol"] = symbol
    return document


def test_stock_history_uses_type_lookup_and_bounded_history_not_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(path: str, params: dict[str, str]) -> dict[str, Any]:
        if path == "/markets/lookup":
            assert params == {"q": "AAPL", "types": "stock"}
            return _lookup("AAPL", "stock")
        if path == "/markets/history":
            assert params == {
                "symbol": "AAPL",
                "interval": "daily",
                "start": "2024-01-02",
                "end": "2024-01-03",
            }
            return _history("AAPL")
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    rows = _fetch(TradierEquityHistoricalFetcher(), symbol="aapl", start_date=_START, end_date=_END)

    assert [type(row) for row in rows] == [TradierEquityHistorical, TradierEquityHistorical]
    assert [row.model_dump() for row in rows] == [
        {
            "symbol": "AAPL",
            "date": date(2024, 1, 2),
            "interval": "daily",
            "open": 187.15,
            "high": 188.44,
            "low": 183.89,
            "close": 185.64,
            "volume": 82488700,
            "dividend_adjustment_semantics": "may_not_be_dividend_adjusted",
            "split_adjustment_semantics": "not_specified_by_tradier_docs",
        },
        {
            "symbol": "AAPL",
            "date": date(2024, 1, 3),
            "interval": "daily",
            "open": 184.22,
            "high": 185.88,
            "low": 183.43,
            "close": 184.25,
            "volume": 58414500,
            "dividend_adjustment_semantics": "may_not_be_dividend_adjusted",
            "split_adjustment_semantics": "not_specified_by_tradier_docs",
        },
    ]
    assert [urlsplit(call["url"]).path for call in client.calls] == [
        "/v1/markets/lookup",
        "/v1/markets/history",
    ]
    assert all(
        call["headers"]["Authorization"] == "Bearer fixture-token-not-a-real-credential"
        for call in client.calls
    )


def test_etf_history_accepts_singleton_shape_and_checks_etf_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(path: str, params: dict[str, str]) -> dict[str, Any]:
        if path == "/markets/lookup":
            assert params == {"q": "SPY", "types": "etf"}
            return _lookup("SPY", "etf")
        if path == "/markets/history":
            return _history("SPY", singleton=True)
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    rows = _fetch(
        TradierEtfHistoricalFetcher(),
        symbol="SPY",
        start_date=_START,
        end_date=_START,
        interval="weekly",
    )

    assert len(rows) == 1 and type(rows[0]) is TradierEtfHistorical
    assert rows[0].model_dump() == {
        "symbol": "SPY",
        "date": _START,
        "interval": "weekly",
        "open": 472.16,
        "high": 473.67,
        "low": 470.49,
        "close": 472.65,
        "volume": 12345678,
        "dividend_adjustment_semantics": "may_not_be_dividend_adjusted",
        "split_adjustment_semantics": "not_specified_by_tradier_docs",
    }
    assert [urlsplit(call["url"]).path for call in client.calls] == [
        "/v1/markets/lookup",
        "/v1/markets/history",
    ]


@pytest.mark.parametrize(
    ("fetcher_type", "security_type", "symbol", "contract_type"),
    [
        (
            TradierEquityHistoricalFetcher,
            "stock",
            "AAPL",
            TradierEquityHistorical,
        ),
        (
            TradierEtfHistoricalFetcher,
            "etf",
            "SPY",
            TradierEtfHistorical,
        ),
    ],
)
def test_documented_day_only_history_uses_requested_symbol_provenance(
    fetcher_type: Any,
    security_type: str,
    symbol: str,
    contract_type: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(path: str, _params: dict[str, str]) -> dict[str, Any]:
        if path == "/markets/lookup":
            return _lookup(symbol, security_type)
        if path == "/markets/history":
            return _fixture("history-guide-day-only.json")
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    rows = _fetch(
        fetcher_type(),
        symbol=symbol,
        start_date=date(2019, 1, 2),
        end_date=date(2019, 1, 4),
    )

    assert all(type(row) is contract_type for row in rows)
    assert [(row.symbol, row.date) for row in rows] == [
        (symbol, date(2019, 1, 2)),
        (symbol, date(2019, 1, 3)),
        (symbol, date(2019, 1, 4)),
    ]
    assert [(row.open, row.high, row.low, row.close, row.volume) for row in rows] == [
        (154.89, 158.85, 154.23, 157.92, 37039737),
        (143.98, 145.72, 142.0, 142.19, 91312195),
        (144.53, 148.5499, 143.8, 148.26, 58607070),
    ]
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("fetcher_type", "security_type", "requested_symbol", "source_symbol"),
    [
        (TradierEquityHistoricalFetcher, "stock", "AAPL", "MSFT"),
        (TradierEtfHistoricalFetcher, "etf", "SPY", "QQQ"),
    ],
)
def test_explicit_history_symbol_mismatch_is_rejected(
    fetcher_type: Any,
    security_type: str,
    requested_symbol: str,
    source_symbol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(path: str, _params: dict[str, str]) -> dict[str, Any]:
        if path == "/markets/lookup":
            return _lookup(requested_symbol, security_type)
        if path == "/markets/history":
            document = _fixture("history-singleton.json")
            document["history"]["symbol"] = source_symbol
            return document
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    with pytest.raises(TradierProviderError, match="TRADIER_HISTORY_SYMBOL_MISMATCH"):
        _fetch(
            fetcher_type(),
            symbol=requested_symbol,
            start_date=_START,
            end_date=_END,
        )
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    ("fetcher_type", "security_type", "symbols"),
    [
        (TradierEquityHistoricalFetcher, "stock", ("AAPL", "MSFT")),
        (TradierEtfHistoricalFetcher, "etf", ("SPY", "QQQ")),
    ],
)
def test_source_cannot_override_reserved_history_provenance(
    fetcher_type: Any,
    security_type: str,
    symbols: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_symbol, second_symbol = symbols

    def answer(path: str, params: dict[str, str]) -> dict[str, Any]:
        requested_symbol = params.get("q", params.get("symbol", ""))
        if path == "/markets/lookup":
            return _lookup(requested_symbol, security_type)
        if path == "/markets/history" and requested_symbol == first_symbol:
            return {
                "history": {
                    "symbol": first_symbol,
                    "day": {
                        "date": _START.isoformat(),
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.1,
                        "volume": 500,
                        "_requested_symbol": second_symbol,
                    },
                }
            }
        if path == "/markets/history":
            return {"history": {"symbol": requested_symbol, "day": None}}
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    with pytest.raises(TradierProviderError, match="TRADIER_RESERVED_FIELD_COLLISION"):
        _fetch(
            fetcher_type(),
            symbols=symbols,
            start_date=_START,
            end_date=_END,
        )
    assert [urlsplit(call["url"]).path for call in client.calls] == [
        "/v1/markets/lookup",
        "/v1/markets/lookup",
        "/v1/markets/history",
    ]


@pytest.mark.parametrize(
    "query",
    [
        {"symbol": "AAPL", "end_date": _END},
        {"symbol": "AAPL", "start_date": _END, "end_date": _START},
        {
            "symbols": ["AAPL", "MSFT"],
            "start_date": _START,
            "end_date": _END,
            "max_requests": 3,
        },
        {"symbol": "AAPL", "start_date": _START, "end_date": _END, "page": 1},
    ],
    ids=["missing-bound", "reversed-window", "fanout-budget", "unsupported-page"],
)
def test_history_invalid_parameters_are_rejected_before_io(
    query: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, lambda *_: pytest.fail("transport must not run"))
    with pytest.raises(ValidationError):
        _fetch(TradierEquityHistoricalFetcher(), **query)
    assert client.calls == []


def test_history_rejects_wrong_type_bad_shape_empty_and_out_of_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(
        monkeypatch,
        lambda path, _params: (
            _lookup("AAPL", "etf")
            if path == "/markets/lookup"
            else pytest.fail("history must not be requested after a type mismatch")
        ),
    )
    with pytest.raises(TradierProviderError, match="TRADIER_WRONG_SECURITY_TYPE"):
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)
    assert [urlsplit(call["url"]).path for call in client.calls] == ["/v1/markets/lookup"]

    client = _client(
        monkeypatch,
        lambda path, _params: (
            _lookup("AAPL", "stock")
            if path == "/markets/lookup"
            else {"history": {"symbol": "AAPL", "day": "not-an-array"}}
        ),
    )
    with pytest.raises(TradierProviderError, match="TRADIER_BAD_SHAPE"):
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)

    client = _client(
        monkeypatch,
        lambda path, _params: (
            _lookup("AAPL", "stock")
            if path == "/markets/lookup"
            else {"history": {"symbol": "AAPL", "day": None}}
        ),
    )
    assert (
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)
        == ()
    )

    client = _client(
        monkeypatch,
        lambda path, _params: (
            _lookup("AAPL", "stock")
            if path == "/markets/lookup"
            else {
                "history": {
                    "symbol": "AAPL",
                    "day": {
                        "date": "2024-01-04",
                        "open": 10.0,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10.1,
                        "volume": 500,
                    },
                }
            }
        ),
    )
    with pytest.raises(TradierProviderError, match="TRADIER_OUT_OF_WINDOW_ROW"):
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)


def test_history_symbol_fanout_has_a_complete_request_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(path: str, params: dict[str, str]) -> dict[str, Any]:
        symbol = params.get("q", params.get("symbol", ""))
        if path == "/markets/lookup":
            return _lookup(symbol, "stock")
        if path == "/markets/history":
            return _history(symbol)
        pytest.fail(f"unexpected endpoint {path}")

    client = _client(monkeypatch, answer)
    rows = _fetch(
        TradierEquityHistoricalFetcher(),
        symbols=["AAPL", "MSFT"],
        start_date=_START,
        end_date=_END,
        max_requests=4,
    )

    assert len(rows) == 4
    assert [(row.symbol, row.date) for row in rows] == [
        ("AAPL", date(2024, 1, 2)),
        ("AAPL", date(2024, 1, 3)),
        ("MSFT", date(2024, 1, 2)),
        ("MSFT", date(2024, 1, 3)),
    ]
    assert len(client.calls) == 4


def test_quote_is_a_snapshot_and_maps_a_singleton_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, lambda *_: _fixture("quotes.json"))
    rows = _fetch(TradierEquityQuoteFetcher(), symbol="AAPL", include_lot_size=True)

    assert len(rows) == 1 and type(rows[0]) is TradierEquityQuote
    assert rows[0].model_dump() == {
        "symbol": "AAPL",
        "description": "Apple Inc",
        "exchange": "Q",
        "security_type": "stock",
        "last": 190.25,
        "change": 1.15,
        "volume": 48291000,
        "open": 189.4,
        "high": 191.02,
        "low": 188.88,
        "close": 190.25,
        "bid": 190.23,
        "ask": 190.27,
        "change_percentage": 0.61,
        "average_volume": 50967638,
        "last_volume": 100,
        "trade_date": 1704229200000,
        "previous_close": 189.1,
        "week_52_high": 199.62,
        "week_52_low": 164.08,
        "bid_size": 100,
        "bid_exchange": "Q",
        "bid_date": 1704229200000,
        "ask_size": 200,
        "ask_exchange": "Q",
        "ask_date": 1704229200000,
        "lot_size": 100,
    }
    assert client.calls[0]["params"] == {"symbols": "AAPL", "includeLotSize": "true"}


@pytest.mark.parametrize(
    "query",
    [
        {"symbol": "AAPL", "start_date": _START},
        {"symbol": "AAPL", "end_date": _END},
        {"symbols": ["AAPL", "AAPL"]},
        {"symbol": "AAPL", "greeks": True},
    ],
    ids=["start-date", "end-date", "duplicate-symbol", "unsupported-greeks"],
)
def test_quote_invalid_parameters_are_rejected_before_io(
    query: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, lambda *_: pytest.fail("transport must not run"))
    with pytest.raises(ValidationError):
        _fetch(TradierEquityQuoteFetcher(), **query)
    assert client.calls == []


def test_quote_empty_and_partial_multi_symbol_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, lambda *_: {"quotes": {"quote": None}})
    assert _fetch(TradierEquityQuoteFetcher(), symbol="AAPL") == ()
    assert len(client.calls) == 1

    partial = _fixture("quotes.json")
    client = _client(monkeypatch, lambda *_: partial)
    with pytest.raises(TradierProviderError, match="TRADIER_MISSING_QUOTES"):
        _fetch(TradierEquityQuoteFetcher(), symbols=["AAPL", "MSFT"])

    client = _client(monkeypatch, lambda *_: {"quotes": {"quote": "broken"}})
    with pytest.raises(TradierProviderError, match="TRADIER_BAD_SHAPE"):
        _fetch(TradierEquityQuoteFetcher(), symbol="AAPL")


def test_search_maps_fixture_and_accepts_an_empty_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, lambda *_: _fixture("search.json"))
    rows = _fetch(TradierEquitySearchFetcher(), query="apple")
    assert [row.model_dump() for row in rows] == [
        {
            "symbol": "AAPL",
            "exchange": "Q",
            "security_type": "stock",
            "description": "Apple Inc",
        },
        {
            "symbol": "APPL",
            "exchange": "A",
            "security_type": "stock",
            "description": "Appell Petroleum Corp",
        },
    ]
    assert all(type(row) is TradierEquitySearch for row in rows)
    assert client.calls[0]["params"] == {"q": "apple", "indexes": "false"}

    client = _client(monkeypatch, lambda *_: {"securities": {"security": None}})
    assert _fetch(TradierEquitySearchFetcher(), query="no-such-security") == ()

    client = _client(monkeypatch, lambda *_: {"securities": {"security": 42}})
    with pytest.raises(TradierProviderError, match="TRADIER_BAD_SHAPE"):
        _fetch(TradierEquitySearchFetcher(), query="apple")


@pytest.mark.parametrize(
    "query",
    [
        {"query": " "},
        {"query": "apple", "start_date": _START},
        {"query": "apple", "page": 2},
        {"query": "apple", "indexes": 1},
    ],
    ids=["empty-query", "date-window", "unsupported-pagination", "non-bool-indexes"],
)
def test_search_invalid_parameters_are_rejected_before_io(
    query: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, lambda *_: pytest.fail("transport must not run"))
    with pytest.raises(ValidationError):
        _fetch(TradierEquitySearchFetcher(), **query)
    assert client.calls == []


def test_option_chain_maps_fixture_and_keeps_expiration_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, lambda *_: _fixture("options-chain.json"))
    rows = _fetch(TradierOptionsChainsFetcher(), symbol="AAPL", expiration=_EXPIRATION, greeks=True)

    assert len(rows) == 1 and type(rows[0]) is TradierOptionChain
    assert rows[0].model_dump() == {
        "symbol": "AAPL240119C00190000",
        "description": "AAPL Jan 19 2024 $190 Call",
        "exchange": "Z",
        "security_type": "option",
        "last": 4.15,
        "change": 0.35,
        "volume": 412,
        "open": 3.95,
        "high": 4.25,
        "low": 3.8,
        "close": 3.8,
        "bid": 4.1,
        "ask": 4.2,
        "change_percentage": 9.21,
        "average_volume": 389,
        "last_volume": 10,
        "trade_date": 1704229200,
        "previous_close": 3.8,
        "week_52_high": 6.2,
        "week_52_low": 0.75,
        "bid_size": 12,
        "bid_exchange": "P",
        "bid_date": 1704229190,
        "ask_size": 15,
        "ask_exchange": "P",
        "ask_date": 1704229190,
        "open_interest": 804,
        "contract_size": 100,
        "expiration_date": _EXPIRATION,
        "expiration_type": "standard",
        "option_type": "call",
        "root_symbol": "AAPL",
        "underlying": "AAPL",
        "strike": 190.0,
        "lot_size": 100,
        "greeks": {"delta": 0.51, "gamma": 0.04, "theta": -0.12, "vega": 0.08, "rho": 0.03},
    }
    assert client.calls[0]["params"] == {
        "symbol": "AAPL",
        "expiration": "2024-01-19",
        "greeks": "true",
    }


@pytest.mark.parametrize(
    "query",
    [
        {"symbol": "AAPL"},
        {"symbol": "AAPL", "expiration": _EXPIRATION, "start_date": _START},
        {"symbols": ["AAPL", "MSFT"], "expiration": _EXPIRATION, "max_requests": 1},
        {"symbol": "AAPL", "expiration": _EXPIRATION, "greeks": "true"},
    ],
    ids=["missing-expiration", "date-window", "fanout-budget", "non-bool-greeks"],
)
def test_chain_invalid_parameters_are_rejected_before_io(
    query: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(monkeypatch, lambda *_: pytest.fail("transport must not run"))
    with pytest.raises(ValidationError):
        _fetch(TradierOptionsChainsFetcher(), **query)
    assert client.calls == []


def test_chain_empty_wrong_expiration_and_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, lambda *_: {"options": {"option": None}})
    assert _fetch(TradierOptionsChainsFetcher(), symbol="AAPL", expiration=_EXPIRATION) == ()

    client = _client(monkeypatch, lambda *_: _fixture("options-chain.json"))
    with pytest.raises(TradierProviderError, match="TRADIER_WRONG_EXPIRATION"):
        _fetch(TradierOptionsChainsFetcher(), symbol="AAPL", expiration=date(2024, 1, 26))

    calls: list[str] = []

    def answer(path: str, params: dict[str, str]) -> dict[str, Any]:
        calls.append(path)
        document = _fixture("options-chain.json")
        document["options"]["option"]["underlying"] = params["symbol"]
        document["options"]["option"]["symbol"] = f"{params['symbol']}240119C00190000"
        document["options"]["option"]["root_symbol"] = params["symbol"]
        return document

    client = _client(monkeypatch, answer)
    rows = _fetch(
        TradierOptionsChainsFetcher(),
        symbols=["AAPL", "MSFT"],
        expiration=_EXPIRATION,
        max_requests=2,
    )
    assert len(rows) == 2
    assert calls == ["/markets/options/chains", "/markets/options/chains"]
    assert len(client.calls) == 2


def test_missing_key_is_rejected_before_shared_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRADIER_TOKEN")
    monkeypatch.setattr(
        _tradier_client,
        "get_shared_http_client",
        lambda: pytest.fail("shared client must not be created without a token"),
    )
    with pytest.raises(TradierProviderError, match="TRADIER_TOKEN_MISSING"):
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)


def test_rate_limit_errors_are_sanitized_and_status_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeClient(
        lambda *_: HttpFetchError(
            FailureCategory.RATE_LIMITED,
            url="https://api.tradier.com/v1/markets/search?token=never-print",
            host="api.tradier.com",
            attempts=3,
            status=429,
            retryable=True,
        )
    )
    monkeypatch.setattr(_tradier_client, "get_shared_http_client", lambda: fake)
    with pytest.raises(TradierProviderError, match="TRADIER_RATE_LIMITED") as caught:
        _fetch(TradierEquitySearchFetcher(), query="apple")
    assert "never-print" not in str(caught.value)
    assert caught.value.status == 429


def test_all_outputs_round_trip_through_json_and_dataframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = {
        "/markets/lookup": _lookup("AAPL", "stock"),
        "/markets/history": _history("AAPL"),
        "/markets/quotes": _fixture("quotes.json"),
        "/markets/search": _fixture("search.json"),
        "/markets/options/chains": _fixture("options-chain.json"),
    }
    client = _client(monkeypatch, lambda path, _params: documents[path])
    outputs = [
        _fetch(TradierEquityHistoricalFetcher(), symbol="AAPL", start_date=_START, end_date=_END)[
            0
        ],
        TradierEtfHistorical.model_validate(
            {
                "symbol": "SPY",
                "date": _START,
                "interval": "daily",
                "open": 472.16,
                "high": 473.67,
                "low": 470.49,
                "close": 472.65,
                "volume": 12345678,
            }
        ),
        _fetch(TradierEquityQuoteFetcher(), symbol="AAPL")[0],
        _fetch(TradierEquitySearchFetcher(), query="apple")[0],
        _fetch(TradierOptionsChainsFetcher(), symbol="AAPL", expiration=_EXPIRATION)[0],
    ]
    for row in outputs:
        contract = type(row)
        round_tripped = contract.model_validate_json(row.model_dump_json())
        assert round_tripped == row
        frame = contract.to_frame([row])
        assert list(frame.columns) == list(contract.model_fields)
        assert contract.from_frame(frame) == [row]
    assert len(client.calls) == 5


def test_descriptor_and_integration_manifest_cover_all_five_models() -> None:
    bindings = PROVIDER.fetcher_bindings
    assert tuple(binding.canonical_model_ids for binding in bindings) == (
        ("EquityHistorical",),
        ("EtfHistorical",),
        ("EquityQuote",),
        ("EquitySearch",),
        ("OptionsChains",),
    )
    manifest = json.loads(
        (_ROOT / "docs/evidence/C100/tradier/integration.json").read_text(encoding="utf-8")
    )
    assert manifest["descriptor_reference"] == "opendata.data.providers.tradier.provider:PROVIDER"
    assert len(manifest["bindings"]) == 5
    assert set(manifest["domains"]) == {
        "tradier_equity_historical",
        "tradier_etf_historical",
        "tradier_equity_quote",
        "tradier_equity_search",
        "tradier_options_chains",
    }
