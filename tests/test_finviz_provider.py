"""Offline hand-authored witnesses for Finviz public-page parsing and contracts."""

from __future__ import annotations

import asyncio
import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.models.finviz import (
    FinvizEquityInfo,
    FinvizEquityScreener,
    FinvizEtfPricePerformance,
    FinvizGroupComparison,
    FinvizKeyMetrics,
    FinvizPricePerformance,
    FinvizPriceTarget,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.finviz import _source
from opendata.data.providers.finviz._html import (
    parse_equity_info,
    parse_equity_screener,
    parse_etf_price_performance,
    parse_group_comparison,
    parse_html,
    parse_key_metrics,
    parse_price_performance,
    parse_price_targets,
)
from opendata.data.providers.finviz._source import (
    FinvizRateLimitError,
    FinvizSourceError,
)
from opendata.data.providers.finviz.models.fetchers import (
    FinvizCompareGroupsFetcher,
    FinvizEquityInfoFetcher,
    FinvizEquityScreenerFetcher,
    FinvizEtfPricePerformanceFetcher,
    FinvizGroupQuery,
    FinvizKeyMetricsFetcher,
    FinvizPricePerformanceFetcher,
    FinvizPriceTargetFetcher,
    FinvizPriceTargetQuery,
)
from opendata.data.providers.finviz.provider import PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def _html_table(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> bytes:
    """Build only the table skeleton shown in the manual official-shape fixtures."""
    header_html = "".join(f"<th>{header}</th>" for header in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    html_text = (
        f"<!doctype html><html><body><table><tr>{header_html}</tr>{rows_html}</table></body></html>"
    )
    return html_text.encode()


def _headerless_table(rows: tuple[tuple[str, ...], ...]) -> bytes:
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<!doctype html><html><body><table>{rows_html}</table></body></html>".encode()


def _authorized_context(fetcher: Fetcher) -> FetchContext:
    """Build exact fixture-only authorization for one provider-model query."""
    assert fetcher.canonical_model is not None
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="finviz",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture-only-no-live-requests",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=("finviz.com",),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    context = FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=1,
            source_attempts=1,
            grants=(grant,),
            profile=profile,
        ),
    )
    return context


def _authorized_fetch(fetcher: Fetcher, **query: object) -> object:
    """Run one fetcher with fixture-only exact scope authorization."""
    return fetcher.fetch(ctx=_authorized_context(fetcher), **query)


GROUP_OVERVIEW = _html_table(
    (
        "No.",
        "Name",
        "Stocks",
        "Market Cap",
        "Dividend",
        "P/E",
        "Fwd P/E",
        "PEG",
        "LTDebt/Eq",
        "Debt/Eq",
        "Float Short",
        "Recom",
        "Change %",
        "Volume",
    ),
    (
        (
            "1",
            "Basic Materials",
            "294",
            "2841.80B",
            "1.93%",
            "20.31",
            "14.56",
            "1.13",
            "0.46",
            "0.53",
            "3.72%",
            "2.06",
            "1.52%",
            "603.58M",
        ),
    ),
)
GROUP_PERFORMANCE = _html_table(
    (
        "No.",
        "Name",
        "Perf Week",
        "Perf Month",
        "Perf Quart",
        "Perf Half",
        "Perf Year",
        "Perf YTD",
        "Avg Volume",
        "Rel Volume",
        "Change %",
        "Volume",
    ),
    (
        (
            "1",
            "Basic Materials",
            "1.11%",
            "3.21%",
            "4.12%",
            "8.02%",
            "18.19%",
            "14.02%",
            "127.21M",
            "1.03",
            "0.42%",
            "603.58M",
        ),
    ),
)
ETF_PERFORMANCE = _html_table(
    (
        "No.",
        "Ticker",
        "Holdings",
        "AUM",
        "Flows% 1M",
        "Flows% 3M",
        "YTD Return%",
        "1Y Return%",
        "3Y Return%",
        "5Y Expense",
        "Price",
        "Change %",
    ),
    (
        (
            "1",
            "A",
            "SPY",
            "503",
            "621.45B",
            "2.20%",
            "5.12%",
            "8.42%",
            "14.50%",
            "10.22%",
            "0.09%",
            "670.12",
            "0.26%",
        ),
    ),
)
PRICE_PERFORMANCE = _html_table(
    (
        "No.",
        "Ticker",
        "Perf Week",
        "Perf Month",
        "Perf Quart",
        "Perf Half",
        "Perf YTD",
        "Perf Year",
        "Perf 3Y",
        "Perf 5Y",
        "Perf 10Y",
        "Volatility W",
        "Volatility M",
        "Avg Volume",
        "Rel Volume",
        "Price",
        "Change %",
        "Volume",
    ),
    (
        (
            "1",
            "A",
            "AAPL",
            "0.88%",
            "3.08%",
            "6.09%",
            "29.24%",
            "23.83%",
            "32.51%",
            "88.08%",
            "135.58%",
            "1080.57%",
            "1.67%",
            "1.85%",
            "45.24M",
            "0.84",
            "336.64",
            "-1.11%",
            "37,881,199",
        ),
    ),
)
EQUITY_SCREENER = _html_table(
    (
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
    ),
    (
        (
            "21",
            "A",
            "AAON",
            "AAON Inc",
            "Industrials",
            "Building Products & Equipment",
            "USA",
            "7.10B",
            "45.07",
            "86.16",
            "0.24%",
            "521,790",
        ),
    ),
)
STOCK_PAGE = (
    "<!doctype html><html><head>"
    "<title>AAPL - Apple Inc Stock Price and Quote (Finviz)</title>"
    "</head><body><h1>AAPL</h1>"
    '<a href="/screener?f=sec_technology">Technology</a>'
    '<a href="/screener?f=ind_consumer_electronics">Consumer Electronics</a>'
    '<a href="/screener?f=geo_usa">USA</a>'
    "<table><tr><td>Market Cap</td><td>4912.98B</td><td>Employees</td><td>166000</td></tr>"
    "<tr><td>IPO</td><td>Dec 12, 1980</td><td>P/E</td><td>38.59</td></tr>"
    "<tr><td>Enterprise Value</td><td>4934.93B</td><td>Income</td><td>128.93B</td></tr>"
    "<tr><td>Sales</td><td>466.82B</td><td>Book/sh</td><td>7.36</td></tr>"
    "<tr><td>Cash/sh</td><td>4.28</td><td>Forward P/E</td><td>35.02</td></tr>"
    "<tr><td>EPS next Y</td><td>9.61</td><td>EPS next Y</td><td>8.80%</td></tr>"
    "<tr><td>EPS past 3/5Y</td><td>6.89% 17.91%</td>"
    "<td>Dividend Est.</td><td>1.10 (0.33%)</td></tr>"
    "<tr><td>Dividend Gr. 3/5Y</td><td>4.26% 4.98%</td>"
    "<td>Volatility</td><td>1.67% 1.85%</td></tr>"
    "<tr><td>52W High</td><td>345.34 -2.52%</td><td>52W Low</td><td>243.42 38.30%</td></tr>"
    "<tr><td>Perf Week</td><td>0.88%</td><td>Target Price</td><td>335.75</td></tr></table>"
    "<table><tr><th>Date</th><th>Action</th><th>Analyst</th><th>Rating Change</th>"
    "<th>Price Target Change</th></tr>"
    "<tr><td>Oct-01-26</td><td>Reiterated</td><td>Morgan Stanley</td>"
    "<td>Overweight → Underweight</td><td>$360 → $355</td></tr>"
    "<tr><td>Sep-18-26</td><td>Reiterated</td><td>Evercore ISI</td>"
    "<td>Outperform → Outperform</td><td>$365 → $380</td></tr>"
    "<tr><td>Sep-01-26</td><td>Upgrade</td><td>Rothschild &amp; Co Redburn</td>"
    "<td>Neutral → Buy</td><td>$400</td></tr></table></body></html>"
).encode()

_FINVIZ_FIXTURES = Path(__file__).resolve().parents[1] / "docs/evidence/C100/finviz/fixtures"
_GROUP_EXCERPT = json.loads((_FINVIZ_FIXTURES / "groups-performance-excerpt.json").read_text())
_GROUP_SOURCE_ROW = tuple(_GROUP_EXCERPT["independent_html"][0]["first_row_cells"])
CURRENT_GROUP_HEADERLESS_ROW = (
    b"<!doctype html><html><head><title>Group Screener - Performance Sector Name</title>"
    b"</head><body>" + _headerless_table((_GROUP_SOURCE_ROW,)) + b"</body></html>"
)
CURRENT_QUOTE_METRICS_EXCERPT = (_FINVIZ_FIXTURES / "quote-metrics-excerpt.txt").read_text().strip()
CURRENT_ANALYST_EVENTS_EXCERPT = (
    (_FINVIZ_FIXTURES / "analyst-events-excerpt.txt").read_text().strip()
)
CURRENT_QUOTE_PAGE = (
    "<!doctype html><html><head><title>AAPL - Apple Inc Stock Price and Quote</title></head>"
    "<body><h1>AAPL</h1><h2>Apple Inc</h2><h2>Upgrade your Finviz experience</h2>"
    '<a href="/screener?f=sec_technology">Technology</a>'
    '<a href="/screener?f=ind_consumer_electronics">Consumer Electronics</a>'
    '<a href="/screener?f=geo_usa">USA</a><table><tr><td>'
    f"{html.escape(CURRENT_QUOTE_METRICS_EXCERPT + ' ' + CURRENT_ANALYST_EVENTS_EXCERPT)}"
    "</td></tr></table></body></html>"
).encode()

# The screener receipt retained only its upgrade/filter surface, not result rows.
CURRENT_SCREENING_UPGRADE_UI = b"""<!doctype html><html><head>
<title>Stock Screener - Overview</title></head><body>
<h2>Upgrade your Finviz experience</h2>
<table><tr><td>screeners: [] hasUser: false presetsUrl=v=111</td></tr></table>
</body></html>"""

# A short synthetic quote snippet is a negative case; it is not a retained source body.
CURRENT_SHORT_QUOTE_SNIPPET = b"""<!doctype html><html><head>
<title>AAPL - Apple Inc Stock Price and Quote</title></head><body>
<h1>AAPL</h1><a href="/screener?f=sec_technology">Technology</a>
<a href="/screener?f=ind_consumer_electronics">Consumer Electronics</a>
<a href="/screener?f=geo_usa">USA</a>
<table><tr><td>Market Cap4912.98B Enterprise Value4934.93B
EPS (ttm)8.72 Price336.64 Change %-1.11%</td></tr></table>
</body></html>"""


def test_provider_descriptor_binds_every_census_model() -> None:
    census_models = {
        "CompareGroups",
        "EtfPricePerformance",
        "PricePerformance",
        "EquityInfo",
        "KeyMetrics",
        "PriceTarget",
        "EquityScreener",
    }
    actual = {
        model_id
        for binding in PROVIDER.fetcher_bindings
        for model_id in (binding.canonical_model_ids or (binding.model_key,))
    }
    assert actual == census_models
    assert PROVIDER.credentials == ()


@pytest.mark.parametrize(
    ("model_id", "fetcher_type", "contract", "query", "body"),
    (
        (
            "CompareGroups",
            FinvizCompareGroupsFetcher,
            FinvizGroupComparison,
            {"group": "sector", "metric": "performance"},
            GROUP_PERFORMANCE,
        ),
        (
            "EtfPricePerformance",
            FinvizEtfPricePerformanceFetcher,
            FinvizEtfPricePerformance,
            {"page": 1},
            ETF_PERFORMANCE,
        ),
        (
            "PricePerformance",
            FinvizPricePerformanceFetcher,
            FinvizPricePerformance,
            {"page": 1},
            PRICE_PERFORMANCE,
        ),
        (
            "EquityInfo",
            FinvizEquityInfoFetcher,
            FinvizEquityInfo,
            {"symbol": "AAPL"},
            CURRENT_QUOTE_PAGE,
        ),
        (
            "KeyMetrics",
            FinvizKeyMetricsFetcher,
            FinvizKeyMetrics,
            {"symbol": "AAPL"},
            CURRENT_QUOTE_PAGE,
        ),
        (
            "PriceTarget",
            FinvizPriceTargetFetcher,
            FinvizPriceTarget,
            {"symbol": "AAPL", "start_date": date(2026, 10, 1), "end_date": date(2026, 10, 1)},
            CURRENT_QUOTE_PAGE,
        ),
        (
            "EquityScreener",
            FinvizEquityScreenerFetcher,
            FinvizEquityScreener,
            {"page": 1},
            EQUITY_SCREENER,
        ),
    ),
)
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    model_id: str,
    fetcher_type: type[Fetcher],
    contract: type,
    query: dict[str, object],
    body: bytes,
) -> None:
    fetcher = fetcher_type()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="finviz",
        model=model_id,
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    captured_identity: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_transform_query(**kwargs: object) -> object:
        captured_identity.update(source=kwargs.get("source"), market=kwargs.get("market"))
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_transform_query)
    monkeypatch.setattr(_source, "request_html", lambda *_args, **_kwargs: body)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )
    principal = SimpleNamespace(allows_domain=lambda domain: domain == capability.domain)

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="finviz",
            model=model_id,
            query=query,
            principal=principal,
            ctx=_authorized_context(fetcher),
        )
    )

    params = fetcher.query_type.model_validate({**query, "source": "finviz", "market": "us"})
    normalized = fetcher.transform_data(body, params)
    expected_rows = normalized if isinstance(normalized, tuple) else (normalized,)
    assert captured_identity == {"source": "finviz", "market": "us"}
    assert response["source"] == "finviz"
    assert response["model"] == model_id
    assert response["domain"] == capability.domain
    assert response["results"] == [row.model_dump(mode="json") for row in expected_rows]


def test_governed_source_uses_default_hook_free_session_and_local_rate_budget() -> None:
    client = _source._HTTP_CLIENT
    assert client._session_override is None
    assert client._config.max_attempts == 1
    assert client._config.rate_limit_per_host == 1.0
    assert client._config.rate_burst == 1
    # This is the same admission check applied to the lazy session for scoped GETs.
    client._validate_scoped_session(client._get_session())


def test_request_html_preserves_response_bytes_and_exact_request_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "<html><title>AAPL – Apple</title></html>".encode()
    calls: list[tuple[str, dict[str, str], float, str]] = []

    class FakeClient:
        def get(
            self,
            url: str,
            *,
            params: dict[str, str],
            timeout: float,
            source: str,
        ) -> SimpleNamespace:
            calls.append((url, params, timeout, source))
            return SimpleNamespace(
                headers={"Content-Type": "text/html; charset=UTF-8"},
                content=body,
            )

    monkeypatch.setattr(_source, "_HTTP_CLIENT", FakeClient())
    assert _source.request_html("/stock", {"t": "AAPL"}, timeout=4.0) == body
    assert calls == [("https://finviz.com/stock", {"t": "AAPL"}, 4.0, "finviz")]


def test_request_html_maps_429_without_retrying_or_custom_response_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeClient:
        def get(self, *_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise HttpFetchError(
                FailureCategory.RATE_LIMITED,
                url="https://finviz.com/stock",
                host="finviz.com",
                attempts=1,
                status=429,
            )

    monkeypatch.setattr(_source, "_HTTP_CLIENT", FakeClient())
    with pytest.raises(FinvizRateLimitError, match="FINVIZ_HTTP_429"):
        _source.request_html("/stock", {"t": "AAPL"})
    assert calls == 1


def test_html_decoder_preserves_utf8_and_strips_optional_bom() -> None:
    body = "\ufeff<html><head><title>AAPL – Apple</title></head></html>".encode()
    snapshot = parse_html(body)
    assert snapshot.title == "AAPL – Apple"


def test_current_public_shape_parses_group_and_rejects_missing_screener_rows() -> None:
    group = parse_group_comparison(CURRENT_GROUP_HEADERLESS_ROW, metric="performance")
    assert len(group) == 1
    assert group[0].name == "Basic Materials"
    assert group[0].performance_week_pct == 0.93
    assert group[0].performance_month_pct == -3.85
    assert group[0].average_volume_shares == 659_150_000
    assert group[0].relative_volume == 0.92

    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_equity_screener(CURRENT_SCREENING_UPGRADE_UI)
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_etf_price_performance(CURRENT_SCREENING_UPGRADE_UI)
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_price_performance(CURRENT_SCREENING_UPGRADE_UI)
    with pytest.raises(FinvizSourceError, match="KEY_METRICS_MISSING"):
        parse_key_metrics(CURRENT_SHORT_QUOTE_SNIPPET, symbol="AAPL")
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_price_targets(
            CURRENT_SHORT_QUOTE_SNIPPET,
            symbol="AAPL",
            start_date=None,
            end_date=None,
        )

    profile = parse_equity_info(CURRENT_SHORT_QUOTE_SNIPPET, symbol="AAPL")
    assert (profile.symbol, profile.company_name) == ("AAPL", "Apple Inc")
    assert (profile.sector, profile.industry, profile.country) == (
        "Technology",
        "Consumer Electronics",
        "USA",
    )
    assert profile.market_cap_usd == 4_912_980_000_000
    assert (profile.employees, profile.ipo_date) == (None, None)


def test_compare_groups_overview_and_performance_positive_contracts() -> None:
    overview = parse_group_comparison(GROUP_OVERVIEW, metric="overview")
    performance = parse_group_comparison(GROUP_PERFORMANCE, metric="performance")
    assert isinstance(overview[0], FinvizGroupComparison)
    assert overview[0].name == "Basic Materials"
    assert overview[0].market_cap_usd == pytest.approx(2_841_800_000_000)
    assert overview[0].dividend_yield_pct == 1.93
    assert overview[0].volume_shares == 603_580_000
    assert performance[0].performance_year_pct == 18.19
    assert performance[0].average_volume_shares == 127_210_000


def test_compare_groups_rejects_wrong_columns_and_unimplemented_view() -> None:
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_group_comparison(
            _html_table(("No.", "Name"), (("1", "Technology"),)),
            metric="overview",
        )
    with pytest.raises(ValueError):
        FinvizGroupQuery(metric="valuation")


def test_headerless_group_fallback_is_limited_to_typed_sector_rows() -> None:
    wrong_width = _headerless_table((_GROUP_SOURCE_ROW[:-1],))
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_group_comparison(
            b"<title>Group Screener - Performance Sector Name</title>" + wrong_width,
            metric="performance",
        )
    unknown_sector = list(_GROUP_SOURCE_ROW)
    unknown_sector[1] = "Unknown Sector"
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_group_comparison(
            b"<title>Group Screener - Performance Sector Name</title>"
            + _headerless_table((tuple(unknown_sector),)),
            metric="performance",
        )
    invalid_percent = list(_GROUP_SOURCE_ROW)
    invalid_percent[2] = "not a percent"
    with pytest.raises(FinvizSourceError, match="INVALID_PERCENT"):
        parse_group_comparison(
            b"<title>Group Screener - Performance Sector Name</title>"
            + _headerless_table((tuple(invalid_percent),)),
            metric="performance",
        )


def test_etf_performance_parses_display_units_and_rejects_bad_percent() -> None:
    row = parse_etf_price_performance(ETF_PERFORMANCE)[0]
    assert isinstance(row, FinvizEtfPricePerformance)
    assert row.symbol == "SPY"
    assert row.holdings == 503
    assert row.assets_under_management_usd == 621_450_000_000
    assert row.flows_3m_pct == 5.12
    assert row.expense_5y_pct == 0.09
    malformed = ETF_PERFORMANCE.replace(b"0.09%", b"0.09")
    with pytest.raises(FinvizSourceError, match="INVALID_PERCENT"):
        parse_etf_price_performance(malformed)


def test_price_performance_parses_all_published_periods_and_rejects_bad_cell() -> None:
    row = parse_price_performance(PRICE_PERFORMANCE)[0]
    assert isinstance(row, FinvizPricePerformance)
    assert row.symbol == "AAPL"
    assert row.performance_10y_pct == 1080.57
    assert row.volatility_week_pct == 1.67
    assert row.average_volume_shares == 45_240_000
    assert row.volume_shares == 37_881_199
    malformed = PRICE_PERFORMANCE.replace(b"0.88%", b"many")
    with pytest.raises(FinvizSourceError, match="INVALID_PERCENT"):
        parse_price_performance(malformed)


def test_equity_info_parses_public_identity_and_rejects_missing_identity() -> None:
    row = parse_equity_info(STOCK_PAGE, symbol="AAPL")
    assert isinstance(row, FinvizEquityInfo)
    assert (row.symbol, row.company_name) == ("AAPL", "Apple Inc")
    assert (row.sector, row.industry, row.country) == (
        "Technology",
        "Consumer Electronics",
        "USA",
    )
    assert row.market_cap_usd == 4_912_980_000_000
    assert row.employees == 166000
    assert row.ipo_date == date(1980, 12, 12)
    with pytest.raises(FinvizSourceError, match="STOCK_IDENTITY_MISSING"):
        parse_equity_info(STOCK_PAGE, symbol="MSFT")
    mismatched_title = STOCK_PAGE.replace(b"AAPL - Apple Inc", b"MSFT - Microsoft Corp")
    with pytest.raises(FinvizSourceError, match="STOCK_IDENTITY_MISSING"):
        parse_equity_info(mismatched_title, symbol="AAPL")


def test_receipt_derived_flat_quote_facts_fill_public_equity_profile() -> None:
    row = parse_equity_info(CURRENT_QUOTE_PAGE, symbol="AAPL")
    assert row.market_cap_usd == 4_912_980_000_000
    assert row.employees == 166000
    assert row.ipo_date == date(1980, 12, 12)
    assert (row.sector, row.industry, row.country) == (
        "Technology",
        "Consumer Electronics",
        "USA",
    )


def test_key_metrics_splits_compound_cells_and_rejects_missing_metric_shape() -> None:
    row = parse_key_metrics(STOCK_PAGE, symbol="AAPL")
    assert isinstance(row, FinvizKeyMetrics)
    assert row.market_cap_usd == 4_912_980_000_000
    assert row.eps_next_year_usd == 9.61
    assert row.eps_growth_next_year_pct == 8.80
    assert row.eps_growth_past_3y_pct == 6.89
    assert row.eps_growth_past_5y_pct == 17.91
    assert row.dividend_estimate_per_share_usd == 1.10
    assert row.dividend_estimate_yield_pct == 0.33
    assert row.dividend_growth_3y_pct == 4.26
    assert row.volatility_month_pct == 1.85
    assert row.high_52w_usd == 345.34
    assert row.high_52w_change_pct == -2.52
    assert row.low_52w_usd == 243.42
    assert row.low_52w_change_pct == 38.30
    empty_metrics = (
        b"<html><head><title>AAPL - Apple Inc Stock Price and Quote</title></head>"
        b"<body><h1>AAPL</h1></body></html>"
    )
    with pytest.raises(FinvizSourceError, match="KEY_METRICS_MISSING"):
        parse_key_metrics(empty_metrics, symbol="AAPL")


def test_receipt_derived_flat_quote_metrics_split_known_source_labels() -> None:
    row = parse_key_metrics(CURRENT_QUOTE_PAGE, symbol="AAPL")
    assert row.market_cap_usd == 4_912_980_000_000
    assert row.enterprise_value_usd == 4_934_930_000_000
    assert row.dividend_estimate_per_share_usd == 1.10
    assert row.dividend_estimate_yield_pct == 0.33
    assert row.eps_next_year_usd == 9.61
    assert row.eps_growth_next_year_pct == 8.80
    assert row.sma_20_pct == 0.37
    assert row.sma_50_pct == 4.42
    assert row.sma_200_pct == 15.79
    assert row.high_52w_change_pct == -2.52
    assert row.low_52w_change_pct == 38.30
    assert row.performance_10y_pct == 1080.57
    assert row.price_usd == 336.64
    assert row.change_pct == -1.11

    malformed = CURRENT_QUOTE_PAGE.replace(b"Market Cap4912.98B", b"Market Capnot-a-number")
    with pytest.raises(FinvizSourceError, match="INVALID_AMOUNT"):
        parse_key_metrics(malformed, symbol="AAPL")


def test_price_target_keeps_visible_rating_events_and_applies_inclusive_dates() -> None:
    rows = parse_price_targets(
        STOCK_PAGE,
        symbol="AAPL",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 18),
    )
    assert len(rows) == 2
    assert isinstance(rows[0], FinvizPriceTarget)
    assert rows[0].date == date(2026, 9, 18)
    assert rows[0].price_target_previous_usd == 365
    assert rows[0].price_target_usd == 380
    assert rows[1].rating_change == "Neutral → Buy"
    assert rows[1].price_target_previous_usd is None
    assert rows[1].price_target_usd == 400
    assert (
        parse_price_targets(
            STOCK_PAGE,
            symbol="AAPL",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
        )
        == ()
    )
    with pytest.raises(FinvizSourceError, match="STOCK_IDENTITY_MISSING"):
        parse_price_targets(STOCK_PAGE, symbol="MSFT", start_date=None, end_date=None)


def test_receipt_derived_flat_analyst_events_stop_at_visible_history_boundary() -> None:
    rows = parse_price_targets(
        CURRENT_QUOTE_PAGE,
        symbol="AAPL",
        start_date=None,
        end_date=None,
    )
    assert len(rows) == 20
    assert rows[0].date == date(2026, 10, 1)
    assert rows[0].analyst == "Morgan Stanley"
    assert rows[0].rating_change == "Overweight"
    assert rows[0].price_target_previous_usd == 360
    assert rows[0].price_target_usd == 355
    assert rows[3].action == "Upgrade"
    assert rows[3].analyst == "Rothschild & Co Redburn"
    assert rows[3].rating_change == "Neutral → Buy"

    filtered = parse_price_targets(
        CURRENT_QUOTE_PAGE,
        symbol="AAPL",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 18),
    )
    assert [row.date for row in filtered] == [date(2026, 9, 18), date(2026, 9, 1)]
    outside = parse_price_targets(
        CURRENT_QUOTE_PAGE,
        symbol="AAPL",
        start_date=date(2020, 1, 1),
        end_date=date(2020, 12, 31),
    )
    assert outside == ()

    unknown_rating = CURRENT_QUOTE_PAGE.replace(
        b"Morgan Stanley Overweight",
        b"Morgan Stanley Unknown",
    )
    with pytest.raises(FinvizSourceError, match="UNKNOWN_ANALYST_RATING"):
        parse_price_targets(
            unknown_rating,
            symbol="AAPL",
            start_date=None,
            end_date=None,
        )


def test_price_target_rejects_bad_table_date_or_target() -> None:
    wrong_columns = (
        b"<html><head><title>AAPL - Apple Inc Stock Price and Quote</title></head>"
        b"<body><h1>AAPL</h1>"
        + _html_table(("Date", "Action"), (("Oct-01-26", "Reiterated"),))
        + b"</body></html>"
    )
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_price_targets(wrong_columns, symbol="AAPL", start_date=None, end_date=None)
    invalid_target = STOCK_PAGE.replace(b"$360 &#8594; $355", b"not a target")
    invalid_target = invalid_target.replace("$360 → $355".encode(), b"not a target")
    with pytest.raises(FinvizSourceError, match="INVALID_PRICE_TARGET"):
        parse_price_targets(invalid_target, symbol="AAPL", start_date=None, end_date=None)
    with pytest.raises(ValueError):
        FinvizPriceTargetQuery(symbol="AAPL", start_date="2026-10-12T00:00:00")
    with pytest.raises(ValueError):
        FinvizPriceTargetQuery(
            symbol="AAPL",
            start_date=date(2026, 10, 13),
            end_date=date(2026, 10, 12),
        )


def test_equity_screener_handles_official_letter_group_cell_and_units() -> None:
    row = parse_equity_screener(EQUITY_SCREENER)[0]
    assert isinstance(row, FinvizEquityScreener)
    assert row.rank == 21
    assert row.symbol == "AAON"
    assert row.market_cap_usd == 7_100_000_000
    assert row.change_pct == 0.24
    assert row.volume_shares == 521_790


def test_equity_screener_refuses_bad_columns_missing_ticker_and_bad_html() -> None:
    bad_columns = _html_table(("No.", "Ticker", "Company"), (("1", "A", "Apple"),))
    with pytest.raises(FinvizSourceError, match="EXPECTED_COLUMNS_MISSING"):
        parse_equity_screener(bad_columns)
    missing_ticker = _html_table(
        (
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
        ),
        (("1", "A", "", "Technology", "Consumer Electronics", "USA", "1B", "10", "1", "1%", "1"),),
    )
    with pytest.raises(FinvizSourceError, match="REQUIRED_SCREENER_CELL_MISSING"):
        parse_equity_screener(missing_ticker)
    with pytest.raises(FinvizSourceError, match="INVALID_HTML"):
        parse_equity_screener(b"403 Forbidden")


@pytest.mark.parametrize(
    ("fetcher_type", "view"),
    (
        (FinvizEtfPricePerformanceFetcher, "191"),
        (FinvizPricePerformanceFetcher, "141"),
    ),
)
def test_paged_screener_fetchers_use_verified_20_row_offset(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type,
    view: str,
) -> None:
    calls: list[tuple[str, dict[str, str], float | None]] = []

    def fake_request(path: str, params: dict[str, str], *, timeout: float | None = None) -> bytes:
        calls.append((path, params, timeout))
        return {
            "111": EQUITY_SCREENER,
            "191": ETF_PERFORMANCE,
            "141": PRICE_PERFORMANCE,
        }[view]

    monkeypatch.setattr(_source, "request_html", fake_request)
    fetcher = fetcher_type()
    rows = _authorized_fetch(fetcher, page=2)
    assert rows
    assert calls == [("/screener", {"v": view, "r": "21"}, 5.0)]
    with pytest.raises(ValidationError):
        _authorized_fetch(fetcher, symbol="AAPL")


def test_stock_fetchers_encode_only_valid_tickers_and_read_public_stock_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(path: str, params: dict[str, str], *, timeout: float | None = None) -> bytes:
        calls.append((path, params))
        return STOCK_PAGE

    monkeypatch.setattr(_source, "request_html", fake_request)
    row = _authorized_fetch(FinvizEquityInfoFetcher(), symbol="aapl")[0]
    assert row.symbol == "AAPL"
    metrics = _authorized_fetch(FinvizKeyMetricsFetcher(), symbol="AAPL")[0]
    assert isinstance(metrics, FinvizKeyMetrics)
    targets = _authorized_fetch(
        FinvizPriceTargetFetcher(),
        symbol="AAPL",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
    )
    assert len(targets) == 1
    assert calls == [
        ("/stock", {"t": "AAPL"}),
        ("/stock", {"t": "AAPL"}),
        ("/stock", {"t": "AAPL"}),
    ]
    with pytest.raises(ValidationError):
        _authorized_fetch(FinvizKeyMetricsFetcher(), symbol="AAPL&v=191")
    with pytest.raises(ValidationError):
        _authorized_fetch(FinvizEquityInfoFetcher(), symbol="AAPL", start_date=date(2026, 1, 1))
    assert len(calls) == 3


def test_group_fetcher_routes_only_verified_view_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(path: str, params: dict[str, str], *, timeout: float | None = None) -> bytes:
        calls.append((path, params))
        return GROUP_PERFORMANCE

    monkeypatch.setattr(_source, "request_html", fake_request)
    rows = _authorized_fetch(FinvizCompareGroupsFetcher(), group="sector", metric="performance")
    assert rows[0].name == "Basic Materials"
    assert calls == [("/groups", {"g": "sector", "o": "name", "st": "d1", "v": "140"})]
    with pytest.raises(ValidationError):
        _authorized_fetch(FinvizCompareGroupsFetcher(), group="industry")


def test_fetcher_contract_rows_are_strict() -> None:
    with pytest.raises(ValidationError):
        FinvizEquityScreener(rank=1, symbol="AAPL", company_name="Apple", invented="field")
    with pytest.raises(ValidationError):
        FinvizPriceTarget(
            symbol="AAPL",
            date=date(2026, 10, 1),
            action="Upgrade",
            analyst="Firm",
            price_target_usd=float("inf"),
        )
