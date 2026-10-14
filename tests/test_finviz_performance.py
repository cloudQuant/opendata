"""Offline source-capture tests for the new Finviz performance adapters."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from opendata.data.models.finviz_performance import (
    FinvizEtfPricePerformance,
    FinvizPricePerformance,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.finviz import _source
from opendata.data.providers.finviz.models.price_performance import (
    FinvizEtfPricePerformanceFetcher,
    FinvizPricePerformanceFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)

FIXTURES = Path(__file__).resolve().parents[1] / "docs/evidence/C100/finviz-performance/fixtures"
QQQM = (FIXTURES / "qqqm.html").read_bytes()
AAPL = (FIXTURES / "aapl.html").read_bytes()
INVALID = (FIXTURES / "invalid.html").read_bytes()
PUBLIC_FIELDS = {
    "symbol",
    "one_day",
    "wtd",
    "one_week",
    "mtd",
    "one_month",
    "qtd",
    "three_month",
    "six_month",
    "ytd",
    "one_year",
    "two_year",
    "three_year",
    "four_year",
    "five_year",
    "ten_year",
    "max",
    "volatility_week",
    "volatility_month",
    "price",
    "volume",
    "average_volume",
    "relative_volume",
    "analyst_recommendation",
}


def _stub_request(
    monkeypatch: pytest.MonkeyPatch,
    pages: dict[str, bytes] | None = None,
    *,
    error_symbol: str | None = None,
    error: Exception | None = None,
) -> list[tuple[str, dict[str, str], float | None]]:
    calls: list[tuple[str, dict[str, str], float | None]] = []
    response_pages = pages or {"QQQM": QQQM, "AAPL": AAPL}

    def request_html(
        path: str,
        params: dict[str, str],
        *,
        timeout: float | None = None,
    ) -> bytes:
        calls.append((path, dict(params), timeout))
        if params["t"] == error_symbol and error is not None:
            raise error
        return response_pages[params["t"]]

    monkeypatch.setattr(_source, "request_html", request_html)
    return calls


def _limited_context(model: str, *, task_limit: int, source_limit: int) -> FetchContext:
    grant = RequestGrant(
        source="finviz",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline fixture request-budget test",
        task_attempts=task_limit,
        source_attempts=source_limit,
        allowed_hosts={"finviz.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        request_budget=RequestBudget(
            task_attempts=task_limit,
            source_attempts=source_limit,
            grants=(grant,),
        )
    )


def _authorized_context(model: str, attempts: int = 10) -> FetchContext:
    return _limited_context(
        model,
        task_limit=min(attempts, 10),
        source_limit=min(attempts, 3),
    )


def test_price_performance_maps_only_supported_price_return_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(monkeypatch)
    rows = FinvizPricePerformanceFetcher().fetch(
        ctx=_authorized_context("PricePerformance"),
        symbol="qqqm",
        source="finviz",
        market="us",
    )

    assert len(calls) == 1
    assert calls[0][0:2] == ("/stock", {"t": "QQQM"})
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, FinvizPricePerformance)
    assert row.model_dump()["symbol"] == "QQQM"
    assert row.one_day == pytest.approx(-0.0072)
    assert row.one_week == pytest.approx(-0.0137)
    assert row.one_month == pytest.approx(0.0433)
    assert row.three_month == pytest.approx(0.0366)
    assert row.six_month == pytest.approx(0.2084)
    assert row.ytd == pytest.approx(0.2144)
    assert row.one_year == pytest.approx(0.2657)
    assert row.three_year == pytest.approx(1.0186)
    assert row.five_year == pytest.approx(1.092)
    assert row.ten_year is None
    for field in PUBLIC_FIELDS.difference(
        {
            "symbol",
            "one_day",
            "one_week",
            "one_month",
            "three_month",
            "six_month",
            "ytd",
            "one_year",
            "three_year",
            "five_year",
            "ten_year",
        }
    ):
        assert getattr(row, field) is None
    assert row.provenance.requested_symbol == "QQQM"
    assert row.provenance.returned_symbol == "QQQM"
    assert row.provenance.source_url == "https://finviz.com/stock?t=QQQM"
    assert row.provenance.quote_timestamp_raw == "Oct 12 • 12:57 PM ET"
    assert row.provenance.retrieved_at_utc.tzinfo is not None
    assert row.provenance.retrieved_at_utc.utcoffset() == timedelta(0)
    assert row.provenance.source_labels["one_day"] == "Change %"
    assert row.provenance.source_labels["one_year"] == "Perf Year"
    assert row.provenance.raw_source_values["Perf Year"] == "26.57%"
    assert row.provenance.dividends_included is False
    assert row.partial_supported_fields == (
        "one_day",
        "one_week",
        "one_month",
        "three_month",
        "six_month",
        "ytd",
        "one_year",
        "three_year",
        "five_year",
        "ten_year",
    )
    serialized = json.loads(row.model_dump_json())
    assert set(serialized).difference({"provenance", "partial_supported_fields"}) == PUBLIC_FIELDS
    assert serialized["one_year"] == pytest.approx(0.2657)
    assert serialized["provenance"]["quote_timestamp_raw"] == "Oct 12 • 12:57 PM ET"


def test_etf_performance_requires_the_exact_etf_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_request(monkeypatch)
    row = FinvizEtfPricePerformanceFetcher().fetch(
        ctx=_authorized_context("EtfPricePerformance"), symbol="QQQM"
    )[0]
    assert isinstance(row, FinvizEtfPricePerformance)
    assert row.symbol == "QQQM"
    assert row.one_year == pytest.approx(0.2657)

    calls = _stub_request(monkeypatch, {"AAPL": AAPL})
    with pytest.raises(_source.FinvizSourceError, match="FINVIZ_NOT_ETF"):
        FinvizEtfPricePerformanceFetcher().fetch(
            ctx=_authorized_context("EtfPricePerformance"), symbol="AAPL"
        )
    assert calls == [("/stock", {"t": "AAPL"}, None)]


def test_equity_capture_supplies_a_non_etf_performance_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(monkeypatch, {"AAPL": AAPL})
    row = FinvizPricePerformanceFetcher().fetch(
        ctx=_authorized_context("PricePerformance"), symbol="AAPL"
    )[0]
    assert calls == [("/stock", {"t": "AAPL"}, None)]
    assert row.symbol == "AAPL"
    assert row.one_day == pytest.approx(0.009)
    assert row.one_year == pytest.approx(0.3849)
    assert row.provenance.quote_timestamp_raw == "Oct 12 • 1:11 PM ET"


@pytest.mark.parametrize(
    "query",
    [
        {"symbol": ""},
        {"symbol": "  "},
        {"symbol": []},
        {"symbol": ["QQQM", " "]},
        {"symbol": ["QQQM", 3]},
        {"symbol": ("QQQM", "AAPL")},
        {"symbol": "QQQM", "return_type": "calendar"},
        {"symbol": "QQQM", "adjustment": "splits_only"},
        {"symbol": "QQQM", "start_date": "2026-01-01"},
        {"symbol": "QQQM", "source": "fmp"},
        {"symbol": "QQQM", "market": "global"},
    ],
)
def test_query_rejects_invalid_or_unimplemented_arguments_before_io(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    calls = _stub_request(monkeypatch)
    with pytest.raises(ValidationError):
        FinvizPricePerformanceFetcher().fetch(**query)
    assert calls == []


def test_symbol_lists_are_sequential_and_preserve_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(monkeypatch)
    rows = FinvizPricePerformanceFetcher().fetch(
        ctx=_authorized_context("PricePerformance", attempts=2),
        symbol=["aapl", "qqqm"],
    )
    assert [call[1]["t"] for call in calls] == ["AAPL", "QQQM"]
    assert [row.symbol for row in rows] == ["AAPL", "QQQM"]
    assert [row.one_day for row in rows] == pytest.approx([0.009, -0.0072])


def test_batch_budget_is_preflighted_before_any_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(monkeypatch)
    with pytest.raises(RequestAttemptLimitError, match="remaining governed request budget"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_limited_context("PricePerformance", task_limit=1, source_limit=1),
            symbol=["QQQM", "AAPL"],
        )
    assert calls == []

    with pytest.raises(RequestAttemptLimitError, match="local per-call request ceiling"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance", attempts=11),
            symbol=[f"T{index}" for index in range(11)],
        )
    assert calls == []


def test_batch_failure_does_not_return_a_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(
        monkeypatch,
        error_symbol="AAPL",
        error=_source.FinvizRateLimitError("FINVIZ_HTTP_429"),
    )
    with pytest.raises(_source.FinvizRateLimitError, match="FINVIZ_HTTP_429"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance", attempts=2),
            symbol=["QQQM", "AAPL"],
        )
    assert [call[1]["t"] for call in calls] == ["QQQM", "AAPL"]


def test_wrong_page_identity_is_not_relabelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_request(monkeypatch, {"QQQM": AAPL})
    with pytest.raises(_source.FinvizSourceError, match="FINVIZ_STOCK_IDENTITY_MISMATCH"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance"), symbol="QQQM"
        )

    _stub_request(monkeypatch, {"ZZZZC100NO": INVALID})
    with pytest.raises(_source.FinvizSourceError, match="FINVIZ_STOCK_IDENTITY_MISSING"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance"), symbol="ZZZZC100NO"
        )


def test_invalid_ticker_capture_never_becomes_an_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(monkeypatch, {"ZZZZC100NO": INVALID})
    with pytest.raises(_source.FinvizSourceError, match="FINVIZ_STOCK_IDENTITY_MISSING"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance"), symbol="ZZZZC100NO"
        )
    assert calls == [("/stock", {"t": "ZZZZC100NO"}, None)]


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        pytest.param(b"", "FINVIZ_EMPTY_RESPONSE", id="empty-response"),
        pytest.param(b"no html", "FINVIZ_INVALID_HTML", id="non-html"),
        pytest.param(
            QQQM.replace(b"-1.37%", b"--1%", 1),
            "FINVIZ_INVALID_NUMBER",
            id="malformed-number",
        ),
        pytest.param(
            QQQM.replace(
                b"Performance (Week, Excl. Dividends)",
                b"Performance (Week, Annualized)",
                1,
            ),
            "FINVIZ_PERFORMANCE_TOOLTIP_MISMATCH",
            id="annualized-tooltip-decoy",
        ),
        pytest.param(
            QQQM.replace(
                b'<div class="snapshot-td-label">Perf 5Y</div>',
                b'<div class="snapshot-td-label">Perf Five Year</div>',
                1,
            ),
            "FINVIZ_PERFORMANCE_FIELDS_MISSING",
            id="unknown-performance-label",
        ),
    ],
)
def test_markup_drift_and_malformed_numbers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    error_code: str,
) -> None:
    _stub_request(monkeypatch, {"QQQM": body})
    with pytest.raises(_source.FinvizSourceError, match=error_code):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance"), symbol="QQQM"
        )


def test_public_nullable_schema_is_complete_without_return_tab_controls() -> None:
    expected = PUBLIC_FIELDS | {"provenance", "partial_supported_fields"}
    assert set(FinvizPricePerformance.model_fields) == expected
    assert set(FinvizEtfPricePerformance.model_fields) == expected


def test_page_request_failure_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_request(
        monkeypatch,
        error_symbol="QQQM",
        error=_source.FinvizSourceError("FINVIZ_HTTP_404"),
    )
    with pytest.raises(_source.FinvizSourceError, match="FINVIZ_HTTP_404"):
        FinvizPricePerformanceFetcher().fetch(
            ctx=_authorized_context("PricePerformance"), symbol="QQQM"
        )
    assert len(calls) == 1
