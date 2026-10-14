"""Guarded offline checks for the Finviz anonymous EquityScreener first page."""

from __future__ import annotations

import html
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opendata.data.protocol import FetchContext

GUARD_DIR = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/C100/acceptance-federal-reserve-clean-extra"
)
_GUARD_REQUIRED = os.environ.get("OPENDATA_REQUIRE_FINVIZ_PREIMPORT_GUARD") == "1"


class _HardIOGuard(Protocol):
    """Describe only the audited guard state used by this fixture suite."""

    blocked_attempts: Counter[str]
    saved_original_dispatches: Counter[str]

    def install(self) -> None: ...

    def summary(self) -> dict[str, object]: ...


IO_GUARD: _HardIOGuard | None = None
_BOOTSTRAP_DBAPI_MODULES: object | None = None
if _GUARD_REQUIRED:
    os.environ["TESTING"] = "true"
    sys.path.insert(0, str(GUARD_DIR))
    # Keep this import conditional: ordinary suite collection must not install
    # process-wide I/O patches. The isolated command sets the required flag.
    from hard_io_guard import HardIOGuard

    IO_GUARD = HardIOGuard()
    IO_GUARD.install()
    _BOOTSTRAP_SUMMARY = IO_GUARD.summary()
    if _BOOTSTRAP_SUMMARY["installed"] is not True:
        raise RuntimeError("the required Finviz I/O guard did not install")
    if _BOOTSTRAP_SUMMARY["opendata_modules_imported"] != []:
        raise RuntimeError("opendata modules were imported before the Finviz I/O guard")
    if not isinstance(_BOOTSTRAP_SUMMARY["loaded_dbapi_modules"], list):
        raise RuntimeError("the required Finviz I/O guard did not report its DBAPI baseline")
    _BOOTSTRAP_DBAPI_MODULES = _BOOTSTRAP_SUMMARY["loaded_dbapi_modules"]
    if "opendata.main" in sys.modules:
        raise RuntimeError("opendata.main was imported before the Finviz I/O guard")

OFFICIAL_CAPTURE = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/C100/remaining-protocol-review/market-six/sources"
    / "finviz-screener-overview.html"
)
EQUITY_INFO_CAPTURES = (
    pytest.param(
        Path(__file__).resolve().parents[1]
        / (
            "docs/evidence/C100/remaining-protocol-review/market-six/"
            "captures-20261012T171107Z/aapl.html"
        ),
        "AAPL",
        "Apple Inc",
        True,
        id="aapl",
    ),
    pytest.param(
        Path(__file__).resolve().parents[1]
        / "docs/evidence/C100/remaining-protocol-review/market-six/sources/finviz-page-qqqm.html",
        "QQQM",
        "Invesco NASDAQ 100 ETF",
        False,
        id="qqqm",
    ),
)
SCREEN_HEADERS = (
    "No.",
    "Ticker",
    "Company",
    "Sector",
    "Industry",
    "Country",
    "Market Cap",
    "P/E",
    "Price",
    "Change %",
    "Volume",
)


def _context(canonical_model: str) -> FetchContext:
    """Create one fixture-only authorization for an offline Finviz call."""
    from opendata.data.protocol import FetchContext
    from opendata.data.request_budget import (
        GrantDecision,
        RequestBudget,
        RequestBudgetProfile,
        RequestGrant,
        RequestOperation,
    )

    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="finviz",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-first-page-fixture-only",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=("finviz.com",),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    return FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(grant,),
            profile=profile,
        ),
    )


def _one_row_table(row: tuple[str, ...]) -> bytes:
    """Create one local source-shaped row for parser refusal counterfacts."""
    header_html = "".join(f"<th>{html.escape(value)}</th>" for value in SCREEN_HEADERS)
    row_html = "".join(f"<td>{html.escape(value)}</td>" for value in row)
    document = f"<html><body><table><tr>{header_html}</tr><tr>{row_html}</tr></table></body></html>"
    return document.encode()


@pytest.fixture(autouse=True)
def _assert_no_io_attempts() -> Iterator[None]:
    """Require zero guard attempts when running under the isolated guard."""
    if IO_GUARD is None:
        yield
        return

    blocked_before = Counter(IO_GUARD.blocked_attempts)
    saved_before = sum(IO_GUARD.saved_original_dispatches.values())
    yield
    blocked_after = Counter(IO_GUARD.blocked_attempts)
    assert all(blocked_after[key] >= blocked_before[key] for key in blocked_after)
    assert all(blocked_after[key] - blocked_before[key] == 0 for key in blocked_after)
    assert sum(IO_GUARD.saved_original_dispatches.values()) == saved_before == 0
    assert IO_GUARD.summary()["loaded_dbapi_modules"] == _BOOTSTRAP_DBAPI_MODULES
    assert "opendata.main" not in sys.modules


def test_only_unfiltered_first_page_dispatches_the_exact_overview_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.data.models.finviz import FinvizEquityScreener
    from opendata.data.providers.finviz import _source
    from opendata.data.providers.finviz.models.fetchers import FinvizEquityScreenerFetcher

    calls: list[tuple[str, dict[str, str], float | None]] = []
    official_html = OFFICIAL_CAPTURE.read_bytes()

    def fake_request(
        path: str,
        params: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> bytes:
        calls.append((path, dict(params), timeout))
        return official_html

    monkeypatch.setattr(_source, "request_html", fake_request)
    fetcher = FinvizEquityScreenerFetcher()
    rows = fetcher.fetch(ctx=_context("EquityScreener"), source="finviz", market="us")

    assert calls == [("/screener", {"v": "111"}, 5.0)]
    assert isinstance(rows, tuple)
    assert len(rows) == 20
    assert [row.rank for row in rows] == list(range(1, 21))
    first = rows[0]
    assert isinstance(first, FinvizEquityScreener)
    assert first.model_dump() == {
        "rank": 1,
        "symbol": "A",
        "company_name": "Agilent Technologies Inc",
        "sector": "Healthcare",
        "industry": "Diagnostics & Research",
        "country": "USA",
        "market_cap_usd": 48_130_000_000.0,
        "pe_ratio": 33.61,
        "price_usd": 170.72,
        "change_pct": 0.11,
        "volume_shares": 666_989.0,
    }
    assert set(first.model_dump()) == {
        "rank",
        "symbol",
        "company_name",
        "sector",
        "industry",
        "country",
        "market_cap_usd",
        "pe_ratio",
        "price_usd",
        "change_pct",
        "volume_shares",
    }


def test_source_market_page_and_filters_fail_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.data.providers.finviz import _source
    from opendata.data.providers.finviz.models.fetchers import FinvizEquityScreenerFetcher

    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(
        path: str,
        params: Mapping[str, str],
        *,
        timeout: float | None = None,
    ) -> bytes:
        calls.append((path, dict(params)))
        return OFFICIAL_CAPTURE.read_bytes()

    monkeypatch.setattr(_source, "request_html", fake_request)
    fetcher = FinvizEquityScreenerFetcher()
    invalid_queries: tuple[dict[str, object], ...] = (
        {"source": "other"},
        {"market": "ca"},
        {"page": True},
        {"page": False},
        {"page": 1.0},
        {"page": "1"},
        {"page": 0},
        {"page": 2},
        {"page": None},
        {"symbol": "AAPL"},
        {"start_date": "2026-10-01"},
        {"end_date": "2026-10-01"},
        {"sector": "Technology"},
    )
    for query in invalid_queries:
        with pytest.raises(ValidationError):
            fetcher.fetch(ctx=_context("EquityScreener"), **query)
    assert calls == []


def test_screener_parser_refuses_missing_columns_ticker_bad_numbers_and_bad_shapes() -> None:
    from opendata.data.providers.finviz._html import parse_equity_screener
    from opendata.data.providers.finviz._source import FinvizSourceError

    official_html = OFFICIAL_CAPTURE.read_bytes()
    upgraded_header = official_html.replace(b">Change %</th>", b">Daily Change</th>", 1)
    with pytest.raises(FinvizSourceError, match="FINVIZ_EXPECTED_COLUMNS_MISSING"):
        parse_equity_screener(upgraded_header)

    missing_ticker = _one_row_table(
        (
            "1",
            "",
            "Agilent Technologies Inc",
            "Healthcare",
            "Diagnostics & Research",
            "USA",
            "48.13B",
            "33.61",
            "170.72",
            "0.11%",
            "666,989",
        )
    )
    with pytest.raises(FinvizSourceError, match="FINVIZ_REQUIRED_SCREENER_CELL_MISSING"):
        parse_equity_screener(missing_ticker)

    bad_number = official_html.replace(b">48.13B</a>", b">not-a-number</a>", 1)
    with pytest.raises(FinvizSourceError, match="FINVIZ_INVALID_AMOUNT"):
        parse_equity_screener(bad_number)

    truncated_row = _one_row_table(
        (
            "1",
            "A",
            "Agilent Technologies Inc",
            "Healthcare",
            "Diagnostics & Research",
            "USA",
            "48.13B",
            "33.61",
            "170.72",
            "0.11%",
        )
    )
    with pytest.raises(FinvizSourceError, match="FINVIZ_ROW_WIDTH_MISMATCH"):
        parse_equity_screener(truncated_row)


@pytest.mark.parametrize(
    ("capture_path", "symbol", "company_name", "market_cap_present"),
    EQUITY_INFO_CAPTURES,
)
def test_equity_info_parses_official_nested_quote_pages(
    capture_path: Path,
    symbol: str,
    company_name: str,
    market_cap_present: bool,
) -> None:
    from opendata.data.models.finviz import FinvizEquityInfo
    from opendata.data.providers.finviz._html import parse_equity_info

    result = parse_equity_info(capture_path.read_bytes(), symbol=symbol)

    assert isinstance(result, FinvizEquityInfo)
    assert result.symbol == symbol
    assert result.company_name == company_name
    if market_cap_present:
        assert result.market_cap_usd is not None and result.market_cap_usd > 0
    else:
        assert result.market_cap_usd is None
        assert result.ipo_date is not None


@pytest.mark.parametrize(
    "second_quote",
    (
        "Market Cap $3.0T Change % 1.0%",
        "Market Cap $2.9T Change % 2.0%",
    ),
    ids=("same-duplicate", "different-target"),
)
def test_equity_info_rejects_multiple_independent_flat_quote_tables(
    second_quote: str,
) -> None:
    from opendata.data.providers.finviz._html import parse_equity_info
    from opendata.data.providers.finviz._source import FinvizSourceError

    first_quote = "Market Cap $3.0T Change % 1.0%"
    body = (
        "<html><head><title>AAPL - Apple Inc Stock Price and Quote</title></head><body>"
        f"<table><tr><td>{first_quote}</td></tr></table>"
        f"<table><tr><td>{second_quote}</td></tr></table>"
        "</body></html>"
    ).encode()

    with pytest.raises(FinvizSourceError, match="FINVIZ_AMBIGUOUS_FLAT_QUOTE"):
        parse_equity_info(body, symbol="AAPL")


def test_performance_screener_models_keep_their_existing_page_two_query() -> None:
    from opendata.data.providers.finviz.models.fetchers import (
        FinvizEtfPricePerformanceFetcher,
        FinvizPageQuery,
        FinvizPricePerformanceFetcher,
    )

    etf_fetcher = FinvizEtfPricePerformanceFetcher()
    price_fetcher = FinvizPricePerformanceFetcher()
    etf_query = etf_fetcher.transform_query(page=2)
    price_query = price_fetcher.transform_query(page=2)

    assert isinstance(etf_query, FinvizPageQuery)
    assert isinstance(price_query, FinvizPageQuery)
    assert etf_fetcher.request_params(etf_query) == {"v": "191", "r": "21"}
    assert price_fetcher.request_params(price_query) == {"v": "141", "r": "21"}
