"""Finviz witnesses whose bodies are verbatim slices of the retained live captures.

This shard adds the three rows the provider's first witness module left unexecuted. Unlike those
four, every fixture line here is copied byte-for-byte out of a pinned capture, so the markup under
test is the markup finviz.com actually serves -- the snapshot table opens a ``<tr>`` on one line,
puts the cells of that row on the next and closes with a ``</tr>`` line (14 such closer
lines survive in the equity subset) -- rather than a rendering the witness invented:

* ``PricePerformance`` from
  ``docs/evidence/C100/finviz-performance/fixtures/aapl.html``
  (sha256 6c6236116eefe388, captured 2026-10-12T17:11:24Z by ``curl --disable`` against
  ``https://finviz.com/stock?t=AAPL``, HTTP 200, 275167 bytes);
* ``EtfPricePerformance`` from
  ``docs/evidence/C100/remaining-protocol-review/market-six/sources/finviz-page-qqqm.html``
  (sha256 b4bc4cc88c019a00, same client on ``https://finviz.com/stock?t=QQQM``, 272242 bytes);
* ``EquityScreener`` from
  ``docs/evidence/C100/remaining-protocol-review/market-six/sources/finviz-screener-overview.html``
  (sha256 8544cb5071b9a066) from ``https://finviz.com/screener?v=111``, the shipped fetcher's own
  ``request_params``).

Each ``sha256`` above is the first 16 hex characters of the full digest recorded for that capture.

Each fixture is an ascending sequence of contiguous source-line slices: the ``<title>``, the
``div.ticker-wrapper[data-ticker]`` and ``h1.js-recent-quote-ticker`` that carry the identity, the
price and quote-date spans, the ``data-testid="stock-data-content"`` element, the category anchors
(5 on the equity page, 4 on the ETF page), and the performance snapshot table
from its ``<table`` line through its ``</table>``. The screener keeps the results table through
its eleven ``<th>`` header cells, its first two ranked rows and the line that closes the table.
Between the identity slices and the snapshot table the perf pages drop navigation, scripts and
CSS, which no gate in the code under test reads. On the screener the 17 skipped
lines are the ranked rows between the kept ones -- real data, deliberately left out to keep the
fixture small, and the only claim this shard makes about them is that the kept rows are not them.
So the retained evidence is a *subset* of the page, never a rewritten one:
no kept line is edited and every cell keeps Finviz's own value. The closing line is not a bare tag
-- this page prints its last ranked row on the same line that closes the table, so that row
travels with it, and the shipped parser returns 3 records ranked [1, 2, 20] (symbols
``A``, ``AA``, ``AAMI``) from these 20 kept lines.

Two of the three counterfacts are provider gates the first module could not reach: dropping the
ETF category anchor must refuse an ETF identity, and a Market Cap cell with a doubled magnitude
suffix must refuse. The third drops a displayed percent sign from a performance cell.
"""

from typing import Any, Final

#: ``_parts/<provider>/`` shards publish ``MODELS``; the runner merges them into the provider
#: config, and ``witness_module_rels`` pins this file for every finviz claim.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.finviz._source"],
    "attr": "shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/finviz/_source.py",
    "opendata/data/providers/finviz/_html.py",
]

_PERF_SOURCE_FILES: Final[list[str]] = [
    *_SOURCE_FILES,
    "opendata/data/providers/finviz/models/price_performance.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/finviz.py",
    "opendata/data/domains.yaml",
]

_AAPL_PERF_LINES: Final[tuple[str, ...]] = (
    "<title>AAPL - Apple Inc Stock Price and Quote</title>",
    (
        '          </script><div id="js-feature-discovery-root"></div><div class="content "><div '
        'class="relative" aria-hidden="true"><div class="pointer-events-none absolute inset-x-0 t'
        'op-0 -z-10 h-215 bg-linear-to-b from-bg-secondary to-transparent"></div></div><div class'
        '="ticker-wrapper" data-ticker="AAPL">'
    ),
    (
        '                    <h1 class="js-recent-quote-ticker quote-header_ticker-wrapper_ticker'
        '" data-ticker="AAPL">AAPL</h1>\r'
    ),
    '        <strong class="quote-price_price">339.68</strong>\r',
    '        <div class="quote-price_info">\r',
    (
        '            <span class="quote-price_date">Oct 12<span class="text-muted-3">&nbsp;•&nbsp'
        ";</span>1:11 PM ET</span>\r"
    ),
    (
        '                    <a href="screener?v=111&f=sec_technology" class="quote-header_catego'
        'ry">Technology</a>\r'
    ),
    (
        '                    <a href="screener?v=111&f=ind_consumerelectronics" class="quote-head'
        'er_category" title="Consumer Electronics"><span class="min-w-0 truncate">Consumer Electr'
        "onics</span></a>\r"
    ),
    (
        '                    <a href="screener?v=111&f=geo_usa" class="quote-header_category">USA'
        "</a>\r"
    ),
    (
        '                        <a href="screener?v=111&f=cap_mega" class="quote-header_category'
        '">Mega</a>\r'
    ),
    (
        '                    <a href="screener?v=111&f=exch_nasd" class="quote-header_category">N'
        "ASD</a>\r"
    ),
    (
        '<div class="content" data-testid="stock-data-content"><table style="table-layout:fixed" '
        'width="100%"><tr>'
    ),
    (
        '<table width="100%" cellpadding="3" cellspacing="0" border="0" class="js-snapshot-table '
        'snapshot-table2 screener_snapshot-table-body max-xl:table-fixed xl:table-auto">'
    ),
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Week, Excl. Dividends)"><div class="snapshot-td-label">Perf Week</div'
        '></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
        '<div class="snapshot-td-content"><b><span class="color-text is-positive">2.04%</span></b'
        "></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Month, Excl. Dividends)"><div class="snapshot-td-label">Perf Month</d'
        'iv></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="'
        '"><div class="snapshot-td-content"><b><span class="color-text is-positive">2.23%</span><'
        "/b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Quarter, Excl. Dividends)"><div class="snapshot-td-label">Perf Quarte'
        'r</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" sty'
        'le=""><div class="snapshot-td-content"><b><span class="color-text is-positive">7.88%</sp'
        "an></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Half Year, Excl. Dividends)"><div class="snapshot-td-label">Perf Half'
        ' Y</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" st'
        'yle=""><div class="snapshot-td-content"><b><span class="color-text is-positive">31.05%</'
        "span></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Year To Date, Excl. Dividends)"><div class="snapshot-td-label">Perf Y'
        'TD</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" st'
        'yle=""><div class="snapshot-td-content"><b><span class="color-text is-positive">24.95%</'
        "span></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Year, Excl. Dividends)"><div class="snapshot-td-label">Perf Year</div'
        '></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
        '<div class="snapshot-td-content"><b><span class="color-text is-positive">38.49%</span></'
        "b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (3 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 3Y</di'
        'v></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""'
        '><div class="snapshot-td-content"><b><span class="color-text is-positive">87.97%</span><'
        "/b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (5 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 5Y</di'
        'v></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""'
        '><div class="snapshot-td-content"><b><span class="color-text is-positive">140.04%</span>'
        "</b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (10 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 10Y</'
        'div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style='
        '""><div class="snapshot-td-content"><b><span class="color-text is-positive">1057.93%</sp'
        "an></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Analysts&#39; mean recommendation (1=Buy 5=Sell)"><div class="snapshot-td-label"><'
        'a href="stock?t=AAPL&ta=1&p=d&ty=fc" class="hover:underline">Recom</a></div></td><td cla'
        'ss="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="s'
        'napshot-td-content"><a href="stock?t=AAPL&ta=1&p=d&ty=fc" class="hover:underline"><b>2.1'
        "6</b></a></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Analysts&#39; mean target price"><div class="snapshot-td-label"><a href="stock?t=A'
        'APL&ta=1&p=d&ty=fc" class="hover:underline">Target Price</a></div></td><td class="snapsh'
        'ot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snapshot-td'
        '-content"><a href="stock?t=AAPL&ta=1&p=d&ty=fc" class="hover:underline"><b><span class="'
        'color-text is-negative">337.18</span></b></a></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Previous close"><div class="snapshot-td-label">Prev Close</div></td><td class="sna'
        'pshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snapshot'
        '-td-content"><b>336.64</b></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Current stock price"><div class="snapshot-td-label">Price</div></td><td class="sna'
        'pshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snapshot'
        '-td-content"><b>339.68</b></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (today)"><div class="snapshot-td-label">Change %</div></td><td class="'
        'snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snaps'
        'hot-td-content"><b><span class="color-text is-positive">0.90%</span></b></div></td>'
    ),
    "</tr>",
    "</table>",
)

_QQQM_PERF_LINES: Final[tuple[str, ...]] = (
    "<title>QQQM - Invesco NASDAQ 100 ETF Stock Price and Quote</title>",
    (
        '          </script><div id="js-feature-discovery-root"></div><div class="content "><div '
        'class="relative" aria-hidden="true"><div class="pointer-events-none absolute inset-x-0 t'
        'op-0 -z-10 h-215 bg-linear-to-b from-bg-secondary to-transparent"></div></div><div class'
        '="ticker-wrapper" data-ticker="QQQM">'
    ),
    (
        '                    <h1 class="js-recent-quote-ticker quote-header_ticker-wrapper_ticker'
        '" data-ticker="QQQM">QQQM</h1>\r'
    ),
    '        <strong class="quote-price_price">307.15</strong>\r',
    '        <div class="quote-price_info">\r',
    (
        '            <span class="quote-price_date">Oct 12<span class="text-muted-3">&nbsp;•&nbsp'
        ";</span>12:57 PM ET</span>\r"
    ),
    (
        '                    <a href="screener?v=111&f=sec_financial" class="quote-header_categor'
        'y">Financial</a>\r'
    ),
    (
        '                    <a href="screener?v=111&f=ind_exchangetradedfund" class="quote-heade'
        'r_category" title="Exchange Traded Fund"><span class="min-w-0 truncate">Exchange Traded '
        "Fund</span></a>\r"
    ),
    (
        '                    <a href="screener?v=111&f=geo_usa" class="quote-header_category">USA'
        "</a>\r"
    ),
    "                    \r",
    (
        '                    <a href="screener?v=111&f=exch_nasd" class="quote-header_category">N'
        "ASD</a>\r"
    ),
    (
        '<div class="content" data-testid="stock-data-content"><table style="table-layout:fixed" '
        'width="100%"><tr>'
    ),
    (
        '<table width="100%" cellpadding="3" cellspacing="0" border="0" class="js-snapshot-table '
        'snapshot-table2 screener_snapshot-table-body max-xl:table-fixed xl:table-auto">'
    ),
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Week, Excl. Dividends)"><div class="snapshot-td-label">Perf Week</div'
        '></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
        '<div class="snapshot-td-content"><b><span class="color-text is-negative">-1.37%</span></'
        "b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Month, Excl. Dividends)"><div class="snapshot-td-label">Perf Month</d'
        'iv></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="'
        '"><div class="snapshot-td-content"><b><span class="color-text is-positive">4.33%</span><'
        "/b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Quarter, Excl. Dividends)"><div class="snapshot-td-label">Perf Quarte'
        'r</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" sty'
        'le=""><div class="snapshot-td-content"><b><span class="color-text is-positive">3.66%</sp'
        "an></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Half Year, Excl. Dividends)"><div class="snapshot-td-label">Perf Half'
        ' Y</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" st'
        'yle=""><div class="snapshot-td-content"><b><span class="color-text is-positive">20.84%</'
        "span></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Year To Date, Excl. Dividends)"><div class="snapshot-td-label">Perf Y'
        'TD</div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" st'
        'yle=""><div class="snapshot-td-content"><b><span class="color-text is-positive">21.44%</'
        "span></b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (Year, Excl. Dividends)"><div class="snapshot-td-label">Perf Year</div'
        '></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
        '<div class="snapshot-td-content"><b><span class="color-text is-positive">26.57%</span></'
        "b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (3 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 3Y</di'
        'v></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""'
        '><div class="snapshot-td-content"><b><span class="color-text is-positive">101.86%</span>'
        "</b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (5 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 5Y</di'
        'v></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""'
        '><div class="snapshot-td-content"><b><span class="color-text is-positive">109.20%</span>'
        "</b></div></td>"
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (10 Years, Excl. Dividends)"><div class="snapshot-td-label">Perf 10Y</'
        'div></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style='
        '""><div class="snapshot-td-content"><b>-</b></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Previous close"><div class="snapshot-td-label">Prev Close</div></td><td class="sna'
        'pshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snapshot'
        '-td-content"><b>309.39</b></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Current stock price"><div class="snapshot-td-label">Price</div></td><td class="sna'
        'pshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snapshot'
        '-td-content"><b>307.15</b></div></td>'
    ),
    "</tr>",
    '<tr class="table-dark-row">',
    (
        '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
        'html="Performance (today)"><div class="snapshot-td-label">Change %</div></td><td class="'
        'snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style=""><div class="snaps'
        'hot-td-content"><b><span class="color-text is-negative">-0.72%</span></b></div></td>'
    ),
    "</tr>",
    "</table>",
)

_SCREENER_LINES: Final[tuple[str, ...]] = (
    '<table class="styled-table-new is-rounded is-tabular-nums w-full screener_table">',
    "<thead>",
    '<tr align="center" valign="middle">',
    '<th class="table-header is-static w-9 cursor-default" align="right">No.</th>',
    (
        '<th class="table-header cursor-pointer is-selected is-ascending" align="left" onclick="w'
        'indow.location=\'screener?v=111&o=-ticker\'">Ticker<svg width="16" height="16" class="inli'
        'ne-block -mt-[5px] -mb-0.5">\r'
    ),
    '    <use href="/assets/dist-icons/icons.4ede4ca4.svg#arrowUpLong"/>\r',
    "</svg></th>",
    (
        '<th class="table-header cursor-pointer" align="left" onclick="window.location=\'screener?'
        "v=111&o=company'\">Company</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="left" onclick="window.location=\'screener?'
        "v=111&o=sector'\">Sector</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="left" onclick="window.location=\'screener?'
        "v=111&o=industry'\">Industry</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="left" onclick="window.location=\'screener?'
        "v=111&o=country'\">Country</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="right" onclick="window.location=\'screener'
        "?v=111&o=marketcap'\">Market Cap</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="right" onclick="window.location=\'screener'
        "?v=111&o=pe'\">P/E</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="right" onclick="window.location=\'screener'
        "?v=111&o=price'\">Price</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="right" onclick="window.location=\'screener'
        "?v=111&o=change'\">Change %</th>"
    ),
    (
        '<th class="table-header cursor-pointer" align="right" onclick="window.location=\'screener'
        "?v=111&o=volume'\">Volume</th>"
    ),
    (
        '<tr class="styled-row is-bordered is-rounded group is-hoverable is-striped has-color-tex'
        't" valign="top">'
    ),
    (
        '<td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">1</a></td><td height="1'
        '0" align="left"data-boxover-ticker="A" data-boxover-company="Agilent Technologies Inc" d'
        'ata-boxover-industry="Diagnostics &amp; Research" data-boxover-country="USA" data-boxove'
        'r-value="48.13B"><span class="flex items-center gap-1 pl-0.5"><a class="company-ticker" '
        'href="stock?t=A&amp;ty=c&amp;p=d&amp;b=1" style="--hover-color: #008FFE; --logo-url: url'
        '(\'https://logo.finviz.com/A.svg\')"><img src="https://logo.finviz.com/A.svg" alt="A logo"'
        ' crossorigin="anonymous" onload="this.classList.add(\'is-loaded\');this.parentElement.clas'
        'sList.add(\'is-logo-loaded\')" /><span>A</span></a><a href="stock?t=A&ty=c&p=d&b=1" class='
        '"tab-link">A</a></span></td><td height="10" align="left"><a href="stock?t=A&ty=c&p=d&b=1'
        '" ">Agilent Technologies Inc</a></td><td height="10" align="left"><a href="stock?t=A&ty='
        'c&p=d&b=1" ">Healthcare</a></td><td height="10" align="left"><a href="stock?t=A&ty=c&p=d'
        '&b=1" ">Diagnostics & Research</a></td><td height="10" align="left"><a href="stock?t=A&t'
        'y=c&p=d&b=1" ">USA</a></td><td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1'
        '" ">48.13B</a></td><td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">33.6'
        '1</a></td><td height="10" align="right"data-boxover-ticker="A" data-boxover-company="Agi'
        'lent Technologies Inc" data-boxover-industry="Diagnostics &amp; Research" data-boxover-c'
        'ountry="USA" data-boxover-value="48.13B"><a href="stock?t=A&ty=c&p=d&b=1" "><span class='
        '"color-text is-positive">170.72</span></a></td><td height="10" align="right"><a href="st'
        'ock?t=A&ty=c&p=d&b=1" "><span class="color-text is-positive">0.11%</span></a></td><td he'
        'ight="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">666,989</a></td></tr><tr clas'
        's="styled-row is-bordered is-rounded group is-hoverable is-striped has-color-text" valig'
        'n="top">'
    ),
    (
        '<td height="10" align="right"><a href="stock?t=AA&ty=c&p=d&b=1" ">2</a></td><td height="'
        '10" align="left"data-boxover-ticker="AA" data-boxover-company="Alcoa Corp" data-boxover-'
        'industry="Aluminum" data-boxover-country="USA" data-boxover-value="11.03B"><span class="'
        'flex items-center gap-1 pl-0.5"><a class="company-ticker" href="stock?t=AA&amp;ty=c&amp;'
        'p=d&amp;b=1" style="--hover-color: #1A5490; --logo-url: url(\'https://logo.finviz.com/AA.'
        'svg\')"><img src="https://logo.finviz.com/AA.svg" alt="AA logo" crossorigin="anonymous" o'
        "nload=\"this.classList.add('is-loaded');this.parentElement.classList.add('is-logo-loaded'"
        ')" /><span>A</span></a><a href="stock?t=AA&ty=c&p=d&b=1" class="tab-link">AA</a></span><'
        '/td><td height="10" align="left"><a href="stock?t=AA&ty=c&p=d&b=1" ">Alcoa Corp</a></td>'
        '<td height="10" align="left"><a href="stock?t=AA&ty=c&p=d&b=1" ">Basic Materials</a></td'
        '><td height="10" align="left"><a href="stock?t=AA&ty=c&p=d&b=1" ">Aluminum</a></td><td h'
        'eight="10" align="left"><a href="stock?t=AA&ty=c&p=d&b=1" ">USA</a></td><td height="10" '
        'align="right"><a href="stock?t=AA&ty=c&p=d&b=1" ">11.03B</a></td><td height="10" align="'
        'right"><a href="stock?t=AA&ty=c&p=d&b=1" "><span class="color-text is-positive">8.58</sp'
        'an></a></td><td height="10" align="right"data-boxover-ticker="AA" data-boxover-company="'
        'Alcoa Corp" data-boxover-industry="Aluminum" data-boxover-country="USA" data-boxover-val'
        'ue="11.03B"><a href="stock?t=AA&ty=c&p=d&b=1" "><span class="color-text is-negative">41.'
        '81</span></a></td><td height="10" align="right"><a href="stock?t=AA&ty=c&p=d&b=1" "><spa'
        'n class="color-text is-negative">-0.93%</span></a></td><td height="10" align="right"><a '
        'href="stock?t=AA&ty=c&p=d&b=1" ">1,653,304</a></td></tr><tr class="styled-row is-bordere'
        'd is-rounded group is-hoverable is-striped has-color-text" valign="top">'
    ),
    (
        '<td height="10" align="right"><a href="stock?t=AAMI&ty=c&p=d&b=1" ">20</a></td><td heigh'
        't="10" align="left"data-boxover-ticker="AAMI" data-boxover-company="Acadian Asset Manage'
        'ment Inc" data-boxover-industry="Asset Management" data-boxover-country="USA" data-boxov'
        'er-value="3.28B"><span class="flex items-center gap-1 pl-0.5"><a class="company-ticker" '
        'href="stock?t=AAMI&amp;ty=c&amp;p=d&amp;b=1" style="--hover-color: #F0F3FA; --logo-url: '
        'url(\'https://logo.finviz.com/AAMI.svg\')"><img src="https://logo.finviz.com/AAMI.svg" alt'
        '="AAMI logo" crossorigin="anonymous" onload="this.classList.add(\'is-loaded\');this.parent'
        "Element.classList.add('is-logo-loaded')\" /><span>A</span></a><a href=\"stock?t=AAMI&ty=c&"
        'p=d&b=1" class="tab-link">AAMI</a></span></td><td height="10" align="left"><a href="stoc'
        'k?t=AAMI&ty=c&p=d&b=1" ">Acadian Asset Management Inc</a></td><td height="10" align="lef'
        't"><a href="stock?t=AAMI&ty=c&p=d&b=1" ">Financial</a></td><td height="10" align="left">'
        '<a href="stock?t=AAMI&ty=c&p=d&b=1" ">Asset Management</a></td><td height="10" align="le'
        'ft"><a href="stock?t=AAMI&ty=c&p=d&b=1" ">USA</a></td><td height="10" align="right"><a h'
        'ref="stock?t=AAMI&ty=c&p=d&b=1" ">3.28B</a></td><td height="10" align="right"><a href="s'
        'tock?t=AAMI&ty=c&p=d&b=1" ">32.64</a></td><td height="10" align="right"data-boxover-tick'
        'er="AAMI" data-boxover-company="Acadian Asset Management Inc" data-boxover-industry="Ass'
        'et Management" data-boxover-country="USA" data-boxover-value="3.28B"><a href="stock?t=AA'
        'MI&ty=c&p=d&b=1" "><span class="color-text is-negative">92.51</span></a></td><td height='
        '"10" align="right"><a href="stock?t=AAMI&ty=c&p=d&b=1" "><span class="color-text is-nega'
        'tive">-1.28%</span></a></td><td height="10" align="right"><a href="stock?t=AAMI&ty=c&p=d'
        '&b=1" ">87,062</a></td></tr></table></td></tr></table>'
    ),
)

_TAMPER_WEEK_LINE: Final[str] = (
    '<td class="snapshot-td2 cursor-pointer max-xl:w-[4%] xl:w-px" align="left" data-boxover-'
    'html="Performance (Week, Excl. Dividends)"><div class="snapshot-td-label">Perf Week</div'
    '></td><td class="snapshot-td2 max-xl:w-[8%] xl:w-full xl:max-w-0" align="left" style="">'
    '<div class="snapshot-td-content"><b><span class="color-text is-positive">2.04</span></b>'
    "</div></td>"
)

_TAMPER_MARKET_CAP_LINE: Final[str] = (
    '<td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">1</a></td><td height="1'
    '0" align="left"data-boxover-ticker="A" data-boxover-company="Agilent Technologies Inc" d'
    'ata-boxover-industry="Diagnostics &amp; Research" data-boxover-country="USA" data-boxove'
    'r-value="48.13B"><span class="flex items-center gap-1 pl-0.5"><a class="company-ticker" '
    'href="stock?t=A&amp;ty=c&amp;p=d&amp;b=1" style="--hover-color: #008FFE; --logo-url: url'
    '(\'https://logo.finviz.com/A.svg\')"><img src="https://logo.finviz.com/A.svg" alt="A logo"'
    ' crossorigin="anonymous" onload="this.classList.add(\'is-loaded\');this.parentElement.clas'
    'sList.add(\'is-logo-loaded\')" /><span>A</span></a><a href="stock?t=A&ty=c&p=d&b=1" class='
    '"tab-link">A</a></span></td><td height="10" align="left"><a href="stock?t=A&ty=c&p=d&b=1'
    '" ">Agilent Technologies Inc</a></td><td height="10" align="left"><a href="stock?t=A&ty='
    'c&p=d&b=1" ">Healthcare</a></td><td height="10" align="left"><a href="stock?t=A&ty=c&p=d'
    '&b=1" ">Diagnostics & Research</a></td><td height="10" align="left"><a href="stock?t=A&t'
    'y=c&p=d&b=1" ">USA</a></td><td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1'
    '" ">48.13BB</a></td><td height="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">33.'
    '61</a></td><td height="10" align="right"data-boxover-ticker="A" data-boxover-company="Ag'
    'ilent Technologies Inc" data-boxover-industry="Diagnostics &amp; Research" data-boxover-'
    'country="USA" data-boxover-value="48.13B"><a href="stock?t=A&ty=c&p=d&b=1" "><span class'
    '="color-text is-positive">170.72</span></a></td><td height="10" align="right"><a href="s'
    'tock?t=A&ty=c&p=d&b=1" "><span class="color-text is-positive">0.11%</span></a></td><td h'
    'eight="10" align="right"><a href="stock?t=A&ty=c&p=d&b=1" ">666,989</a></td></tr><tr cla'
    'ss="styled-row is-bordered is-rounded group is-hoverable is-striped has-color-text" vali'
    'gn="top">'
)


def _line_index(lines: tuple[str, ...], needle: str) -> int:
    """Resolve one counterfact target to its fixture line, refusing an ambiguous needle."""
    hits = [index for index, line in enumerate(lines) if needle in line]
    if len(hits) != 1:
        raise AssertionError(
            f"counterfact target {needle!r} matches {len(hits)} fixture line(s), not exactly one"
        )
    return hits[0]


def _envelope(path: str, lines: tuple[str, ...]) -> dict[str, Any]:
    """Wrap the verbatim source lines as the one response a page fetch is served."""
    return {"responses": [{"url_contains": path, "body": list(lines)}]}


MODELS: Final[dict[str, Any]] = {
    "PricePerformance": {
        "fixture_document": lambda: _envelope("/stock", _AAPL_PERF_LINES),
        "client_seam": _SEAM,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", _line_index(_AAPL_PERF_LINES, "Performance (Week")],
            "value": _TAMPER_WEEK_LINE,
        },
        "refusal_class": "FinvizSourceError",
        "refusal_code": "FINVIZ_INVALID_NUMBER",
        "tamper_reason": (
            "the captured page prints the week return as 2.04%; printing it as 2.04 keeps the cell "
            "in the same markup and the same row, but a displayed percent is what the column's "
            "unit is, so _parse_percent must refuse rather than publish 2.04 as a fraction of 1 "
            "or as 204 percent"
        ),
        "source_files": _PERF_SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "EtfPricePerformance": {
        "fixture_document": lambda: _envelope("/stock", _QQQM_PERF_LINES),
        "client_seam": _SEAM,
        "query": {"symbol": "QQQM"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", _line_index(_QQQM_PERF_LINES, "Exchange Traded Fund")],
        },
        "refusal_class": "FinvizSourceError",
        "refusal_code": "FINVIZ_NOT_ETF",
        "tamper_reason": (
            "the ETF identity is published as one category anchor, so deleting that anchor leaves "
            "a page that still carries a QQQM heading, a quote timestamp and the full performance "
            "grid: the only fact that made it an ETF is gone, and _parse_page must refuse instead "
            "of returning ETF rows for an unclassified symbol"
        ),
        "source_files": _PERF_SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "EquityScreener": {
        "fixture_document": lambda: _envelope("/screener", _SCREENER_LINES),
        "client_seam": _SEAM,
        "query": {"page": 1},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", _line_index(_SCREENER_LINES, "Agilent Technologies")],
            "value": _TAMPER_MARKET_CAP_LINE,
        },
        "refusal_class": "FinvizSourceError",
        "refusal_code": "FINVIZ_INVALID_AMOUNT",
        "tamper_reason": (
            "the captured row prints Agilent's market capitalisation as 48.13B with one magnitude "
            "suffix; printing 48.13BB keeps the eleven-column row intact and the ticker legitimate "
            "but makes the suffix unreadable, so _amount must refuse the cell rather than guess "
            "whether the author meant billions twice"
        ),
        "source_files": [*_SOURCE_FILES, "opendata/data/providers/finviz/models/fetchers.py"],
        "contract_files": _CONTRACT_FILES,
    },
}
