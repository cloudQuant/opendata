"""Offline witness configuration for the ``finviz`` provider.

Finviz serves public HTML pages, not JSON, and its fetchers own their transport: every page model
routes through :func:`opendata.data.providers.finviz._source.request_html`, which reads the module
attribute ``shared_http_client`` on each call. Each witness therefore installs the repository's
fake governed client at that one attribute (:mod:`iter2_claims_probe_runner._client_seam`), so the
seam hands the shipped ``_html.parse_*`` functions the bytes a real answer would have carried. No
leg touches a socket -- the runner refuses socket use for the whole process -- and the path,
content-type, size and empty-body gates under test in ``_source.py`` are the shipped ones.

Three of the four bodies are re-published from the receipts this provider's evidence root retains
(``docs/evidence/C100/finviz/fixtures/``): the sector performance row with its twelve displayed
cells, the quote-metrics excerpt, and the analyst-event excerpt. Their labels and values are the
source's own; they are separated onto one HTML line per cell only so that a counterfact can target
exactly one displayed cell, which is not how finviz.com emits markup. They prove the reading and
the gates, not that the site still serves these numbers today.

The refusals are Finviz's own stable codes. ``FinvizSourceError`` is this provider's attributable
refusal type and now carries ``code`` and ``status`` alongside the message, so a witness names the
code instead of matching on prose. All four are deliberately code-only: a percentage cell printed
without its percent sign, a page whose ``<title>`` names another ticker, a two-value 52-week cell
printed with one number, and an analyst date that is not a real day are all refused before any
contract column is addressed, so no cell exists for the refusal to attribute.

What these four witnesses do not cover: ``EtfPricePerformance`` publishes capability domain
``finviz_etf_performance`` (``opendata/data/providers/finviz/models/fetchers.py:226``) while
``opendata/data/domains.yaml`` registers ``finviz_etf_price_performance``, so its
``implementation`` phase would compare a capability against an unregistered domain and is filed as
a gap instead of being witnessed here; ``PricePerformance`` and ``EquityScreener`` fetch through
``models/price_performance.py`` with their own decode path and are not witnessed in this wave.
"""

from __future__ import annotations

from typing import Any, Final

#: The page furniture the source really serves around a quote table: title, ticker heading and
#: the three sector/industry/country filter anchors that ``_profile_tags`` reads.
_QUOTE_HEAD: Final[list[str]] = [
    "<!doctype html><html><head>",
    "<title>AAPL - Apple Inc Stock Price and Quote</title>",
    "</head><body>",
    "<h1>AAPL</h1>",
    '<a href="/screener?f=sec_technology">Technology</a>',
    '<a href="/screener?f=ind_consumer_electronics">Consumer Electronics</a>',
    '<a href="/screener?f=geo_usa">USA</a>',
    "<table>",
]

#: Label/value cells exactly as the retained quote-metrics excerpt displays them, two pairs per
#: row the way the source lays the quote table out.
_QUOTE_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("Market Cap", "4912.98B"),
    ("Enterprise Value", "4934.93B"),
    ("Income", "128.93B"),
    ("Sales", "466.82B"),
    ("Book/sh", "7.36"),
    ("Cash/sh", "4.28"),
    ("Dividend Est.", "1.10 (0.33%)"),
    ("Dividend TTM", "1.06 (0.31%)"),
    ("Dividend Gr. 3/5Y", "4.26% 4.98%"),
    ("Employees", "166000"),
    ("IPO", "Dec 12, 1980"),
    ("P/E", "38.59"),
    ("Forward P/E", "35.02"),
    ("PEG", "2.75"),
    ("P/S", "10.52"),
    ("P/B", "45.74"),
    ("EPS (ttm)", "8.72"),
    ("EPS next Y", "9.61"),
    ("EPS next Y", "8.80%"),
    ("EPS past 3/5Y", "6.89% 17.91%"),
    ("Sales past 3/5Y", "1.81% 8.71%"),
    ("Volatility", "1.67% 1.85%"),
    ("52W High", "345.34 -2.52%"),
    ("52W Low", "243.42 38.30%"),
    ("Rel Volume", "0.84"),
    ("Avg Volume", "45.24M"),
    ("Volume", "37,881,199"),
    ("Perf Week", "0.88%"),
    ("Recom", "2.16"),
    ("Target Price", "335.75"),
    ("Price", "336.64"),
    ("Change %", "-1.11%"),
)

#: The analyst events the retained excerpt keeps, in the source's own five columns. The date is
#: the page's ``Oct-01-26`` display, not an ISO date.
_ANALYST_HEADER: Final[tuple[str, ...]] = (
    "Date",
    "Action",
    "Analyst",
    "Rating Change",
    "Price Target Change",
)
_ANALYST_ROWS: Final[tuple[tuple[str, ...], ...]] = (
    ("Oct-01-26", "Reiterated", "Morgan Stanley", "Overweight", "$360 → $355"),
    ("Sep-18-26", "Reiterated", "Evercore ISI", "Outperform", "$365 → $380"),
    ("Aug-17-26", "Upgrade", "Rothschild & Co Redburn", "Neutral → Buy", "$400"),
)

#: One sector row exactly as the retained groups receipt published its twelve cells.
_SECTOR_ROW: Final[tuple[str, ...]] = (
    "1",
    "Basic Materials",
    "0.93%",
    "-3.85%",
    "5.11%",
    "-5.62%",
    "16.89%",
    "11.78%",
    "659.15M",
    "0.92",
    "1.52%",
    "603.58M",
)

_QUOTE_TAIL: Final[list[str]] = ["</table>", "</body></html>"]


def _row(cells: tuple[str, ...], *, tag: str = "td") -> list[str]:
    """Publish one table row as one source line per cell so a counterfact can target a cell."""
    opening = "<tr>"
    closing = "</tr>"
    return [opening, *(f"<{tag}>{cell}</{tag}>" for cell in cells), closing]


def _stock_lines(*, with_analyst_table: bool) -> list[str]:
    """Lay out one AAPL stock page: the quote table, plus the analyst table when requested."""
    lines = list(_QUOTE_HEAD)
    pairs = list(_QUOTE_PAIRS)
    for index in range(0, len(pairs), 2):
        cell_pair: tuple[str, ...] = (pairs[index][0], pairs[index][1])
        if index + 1 < len(pairs):
            cell_pair += (pairs[index + 1][0], pairs[index + 1][1])
        lines.extend(_row(cell_pair))
    lines.append("</table>")
    if with_analyst_table:
        lines.append("<table>")
        lines.extend(_row(_ANALYST_HEADER, tag="th"))
        for event in _ANALYST_ROWS:
            lines.extend(_row(event))
        lines.append("</table>")
    lines.extend(_QUOTE_TAIL)
    return lines


def _group_lines() -> list[str]:
    """Publish the headerless sector performance table the retained receipt actually observed."""
    lines = [
        "<!doctype html><html><head>",
        "<title>Group Screener - Performance Sector Name</title>",
        "</head><body>",
        "<table>",
    ]
    lines.extend(_row(_SECTOR_ROW))
    lines.extend(["</table>", "</body></html>"])
    return lines


def _line_index(lines: list[str], needle: str) -> int:
    """Resolve one counterfact target to its fixture line, refusing an ambiguous needle."""
    hits = [index for index, line in enumerate(lines) if needle in line]
    if len(hits) != 1:
        raise AssertionError(
            f"counterfact target {needle!r} matches {len(hits)} fixture line(s), not exactly one"
        )
    return hits[0]


def _envelope(path: str, lines: list[str]) -> dict[str, Any]:
    """Wrap HTML source lines as the one response a page fetch is served."""
    return {"responses": [{"url_contains": path, "body": lines}]}


#: Built once so every tamper index is resolved against the bytes the seam actually serves.
_QUOTE_LINES: Final[list[str]] = _stock_lines(with_analyst_table=False)
_ANALYST_LINES: Final[list[str]] = _stock_lines(with_analyst_table=True)
_GROUP_LINES: Final[list[str]] = _group_lines()


def _quote_document() -> dict[str, Any]:
    """The stock-page body served for a profile or key-metrics request."""
    return _envelope("/stock", list(_QUOTE_LINES))


def _analyst_document() -> dict[str, Any]:
    """The stock-page body served for an analyst-event request."""
    return _envelope("/stock", list(_ANALYST_LINES))


def _group_document() -> dict[str, Any]:
    """The sector performance body served for a group comparison request."""
    return _envelope("/groups", list(_GROUP_LINES))


_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.finviz._source"],
    "attr": "shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/finviz/_source.py",
    "opendata/data/providers/finviz/_html.py",
    "opendata/data/providers/finviz/models/fetchers.py",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.finviz.registration",
    "allowed_hosts": ["finviz.com"],
    "models": {
        "CompareGroups": {
            "fixture_document": _group_document,
            "client_seam": _SEAM,
            "query": {"group": "sector", "metric": "performance"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", _line_index(_GROUP_LINES, "<td>16.89%</td>")],
                "value": "<td>16.89</td>",
            },
            "refusal_class": "FinvizSourceError",
            "refusal_code": "FINVIZ_INVALID_PERCENT",
            "tamper_reason": (
                "printing the Perf Year cell as 16.89 without the percent sign keeps the row "
                "twelve cells wide and the sector name legitimate, but the contract column is a "
                "percentage, so parse_group_comparison must refuse the cell rather than read "
                "16.89 as either a percent or a ratio"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": [
                "opendata/data/models/finviz.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquityInfo": {
            "fixture_document": _quote_document,
            "client_seam": _SEAM,
            "query": {"symbol": "AAPL"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", _line_index(_QUOTE_LINES, "<title>AAPL - ")],
                "value": "<title>MSFT - Apple Inc Stock Price and Quote</title>",
            },
            "refusal_class": "FinvizSourceError",
            "refusal_code": "FINVIZ_STOCK_IDENTITY_MISSING",
            "tamper_reason": (
                "a page whose title carries MSFT while the caller asked for AAPL is a redirect or "
                "a stale cache entry, not a profile for the requested symbol, so _stock_identity "
                "must refuse the page instead of publishing Apple Inc's facts under MSFT or AAPL"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": [
                "opendata/data/models/finviz.py",
                "opendata/data/domains.yaml",
            ],
        },
        "KeyMetrics": {
            "fixture_document": _quote_document,
            "client_seam": _SEAM,
            "query": {"symbol": "AAPL"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", _line_index(_QUOTE_LINES, "<td>345.34 ")],
                "value": "<td>345.34</td>",
            },
            "refusal_class": "FinvizSourceError",
            "refusal_code": "FINVIZ_INVALID_COMPOUND_METRIC",
            "tamper_reason": (
                "the 52W High cell is a displayed pair, price and distance from the high, so "
                "printing only 345.34 leaves one number where the contract expects two and "
                "parse_key_metrics must refuse rather than guess a zero change percentage"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": [
                "opendata/data/models/finviz.py",
                "opendata/data/domains.yaml",
            ],
        },
        "PriceTarget": {
            "fixture_document": _analyst_document,
            "client_seam": _SEAM,
            "query": {"symbol": "AAPL", "start_date": "2026-08-01", "end_date": "2026-10-31"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", _line_index(_ANALYST_LINES, "<td>Oct-01-26<")],
                "value": "<td>Oct-85-26</td>",
            },
            "refusal_class": "FinvizSourceError",
            "refusal_code": "FINVIZ_INVALID_ANALYST_DATE",
            "tamper_reason": (
                "Oct-85-26 keeps the page's three-part display shape but is not a day of any "
                "month, so _analyst_date must refuse the event instead of silently dropping the "
                "row and shortening the caller's analyst history"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": [
                "opendata/data/models/finviz.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
