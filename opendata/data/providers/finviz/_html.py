"""Strict parsers for the public Finviz table and stock-page shapes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from opendata.data.models.finviz import (
    FinvizEquityInfo,
    FinvizEquityScreener,
    FinvizEtfPricePerformance,
    FinvizGroupComparison,
    FinvizKeyMetrics,
    FinvizPricePerformance,
    FinvizPriceTarget,
)
from opendata.data.providers.finviz._source import FinvizSourceError

_NUMBER = re.compile(r"[+-]?(?:\d+(?:,\d{3})*(?:\.\d*)?|\.\d+)\Z")
_AMOUNT = re.compile(r"([+-]?(?:\d+(?:,\d{3})*(?:\.\d*)?|\.\d+))\s*([KMBT])?\Z", re.I)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_EMPTY = frozenset({"", "-", "—", "–", "n/a", "na", "none"})


@dataclass(frozen=True)
class HTMLTable:
    """One table's direct rows as source-rendered text cells."""

    rows: tuple[tuple[str, ...], ...]


@dataclass
class _TableBuilder:
    """Mutable parse state for one table's direct cells and rows."""

    rows: list[tuple[str, ...]]
    row: list[str] | None = None
    cell: list[str] | None = None


@dataclass(frozen=True)
class HTMLSnapshot:
    """Parsed text landmarks used on a Finviz stock page."""

    tables: tuple[HTMLTable, ...]
    title: str
    headings: tuple[str, ...]
    anchors: tuple[tuple[str, str], ...]


class _PageParser(HTMLParser):
    """Collect visible table cells, page title, headings, and anchor labels."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[HTMLTable] = []
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._table_stack: list[_TableBuilder] = []
        self._in_title = False
        self._heading: list[str] | None = None
        self._anchor_href: str | None = None
        self._anchor_text: list[str] | None = None
        self._anchor_skips_cell_text: list[bool] = []
        self._skip_cell_text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            self._table_stack.append(_TableBuilder(rows=[]))
        elif tag == "tr" and self._table_stack:
            table = self._table_stack[-1]
            if table.row is not None:
                self._finish_row(table)
            table.row = []
        elif tag in {"td", "th"} and self._table_stack:
            row = self._table_stack[-1].row
            if row is not None:
                self._table_stack[-1].cell = []
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._heading = []
        elif tag == "a":
            skip_cell_text = "company-ticker" in (attributes.get("class") or "").split()
            self._anchor_skips_cell_text.append(skip_cell_text)
            if skip_cell_text:
                self._skip_cell_text_depth += 1
            self._anchor_href = attributes.get("href") or ""
            self._anchor_text = []
        elif tag in {"br", "p", "div"} and self._skip_cell_text_depth == 0:
            cell = self._active_cell()
            if cell is not None:
                cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._table_stack:
            table = self._table_stack[-1]
            if table.row is not None:
                if table.cell is not None:
                    table.row.append(_clean_text("".join(table.cell)))
                table.cell = None
        elif tag == "tr" and self._table_stack:
            self._finish_row(self._table_stack[-1])
        elif tag in {"thead", "tbody", "tfoot"} and self._table_stack:
            table = self._table_stack[-1]
            if table.row is not None:
                self._finish_row(table)
        elif tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table.row is not None:
                self._finish_row(table)
            if table.rows or not self._table_stack:
                self.tables.append(HTMLTable(tuple(table.rows)))
        elif tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3"} and self._heading is not None:
            heading = _clean_text("".join(self._heading))
            if heading:
                self.headings.append(heading)
            self._heading = None
        elif tag == "a":
            if self._anchor_skips_cell_text and self._anchor_skips_cell_text.pop():
                self._skip_cell_text_depth -= 1
            if self._anchor_text is not None:
                self.anchors.append(
                    (self._anchor_href or "", _clean_text("".join(self._anchor_text)))
                )
                self._anchor_href = None
                self._anchor_text = None

    @staticmethod
    def _finish_row(table: _TableBuilder) -> None:
        if table.row:
            table.rows.append(tuple(table.row))
        table.row = None
        table.cell = None

    def handle_data(self, data: str) -> None:
        if self._skip_cell_text_depth == 0:
            cell = self._active_cell()
            if cell is not None:
                cell.append(data)
        if self._in_title:
            self.title_parts.append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._anchor_text is not None:
            self._anchor_text.append(data)

    def _active_cell(self) -> list[str] | None:
        """Return only the innermost table's active cell, excluding nested text."""
        if not self._table_stack:
            return None
        return self._table_stack[-1].cell


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def parse_html(body: bytes) -> HTMLSnapshot:
    """Decode and parse a non-empty Finviz HTML document."""
    if not isinstance(body, bytes) or not body:
        raise FinvizSourceError("FINVIZ_EMPTY_RESPONSE")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise FinvizSourceError("FINVIZ_INVALID_ENCODING") from error
    if "<" not in text or ">" not in text:
        raise FinvizSourceError("FINVIZ_INVALID_HTML")
    parser = _PageParser()
    try:
        parser.feed(text)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise FinvizSourceError("FINVIZ_INVALID_HTML") from error
    if not parser.tables and not parser.title_parts and not parser.headings:
        raise FinvizSourceError("FINVIZ_INVALID_HTML")
    return HTMLSnapshot(
        tables=tuple(parser.tables),
        title=_clean_text("".join(parser.title_parts)),
        headings=tuple(parser.headings),
        anchors=tuple(parser.anchors),
    )


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _matching_table(body: bytes, expected_headers: tuple[str, ...]) -> tuple[HTMLTable, int]:
    snapshot = parse_html(body)
    expected = tuple(_header_key(header) for header in expected_headers)
    matches: list[tuple[HTMLTable, int]] = []
    for table in snapshot.tables:
        for index, row in enumerate(table.rows):
            if tuple(_header_key(cell) for cell in row) == expected:
                matches.append((table, index))
    if not matches:
        raise FinvizSourceError("FINVIZ_EXPECTED_COLUMNS_MISSING")
    if len(matches) > 1:
        raise FinvizSourceError("FINVIZ_AMBIGUOUS_TABLE")
    return matches[0]


def _table_records(
    body: bytes,
    expected_headers: tuple[str, ...],
    *,
    grouped_letter: bool = False,
) -> tuple[tuple[str, ...], ...]:
    table, header_index = _matching_table(body, expected_headers)
    width = len(expected_headers)
    rows: list[tuple[str, ...]] = []
    for source_row in table.rows[header_index + 1 :]:
        row = source_row
        if not row or all(not value for value in row):
            continue
        if (
            grouped_letter
            and len(row) == width + 1
            and _SYMBOL.fullmatch(row[1].upper())
            and len(row[1]) == 1
            and row[1].isalpha()
            and _SYMBOL.fullmatch(row[2].upper())
        ):
            row = row[:1] + row[2:]
        if len(row) != width:
            raise FinvizSourceError("FINVIZ_ROW_WIDTH_MISMATCH")
        rows.append(row)
    if not rows:
        raise FinvizSourceError("FINVIZ_EMPTY_TABLE")
    return tuple(rows)


def _record(header: tuple[str, ...], row: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(header, row, strict=True))


def _text(record: dict[str, str], key: str) -> str | None:
    value = record[key].strip()
    return None if value.casefold() in _EMPTY else value


def _number(value: str | None) -> float | None:
    if value is None or value.strip().casefold() in _EMPTY:
        return None
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not _NUMBER.fullmatch(cleaned):
        raise FinvizSourceError("FINVIZ_INVALID_NUMBER")
    parsed = float(cleaned)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise FinvizSourceError("FINVIZ_INVALID_NUMBER")
    return parsed


def _percent(value: str | None) -> float | None:
    if value is None or value.strip().casefold() in _EMPTY:
        return None
    text = value.strip().replace("−", "-")
    if not text.endswith("%"):
        raise FinvizSourceError("FINVIZ_INVALID_PERCENT")
    return _number(text[:-1])


def _amount(value: str | None) -> float | None:
    if value is None or value.strip().casefold() in _EMPTY:
        return None
    text = value.strip().replace("$", "").replace(",", "")
    match = _AMOUNT.fullmatch(text)
    if match is None:
        raise FinvizSourceError("FINVIZ_INVALID_AMOUNT")
    number = float(match.group(1))
    suffix = match.group(2).upper() if match.group(2) else None
    multiplier = {None: 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suffix]
    parsed = number * multiplier
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        raise FinvizSourceError("FINVIZ_INVALID_AMOUNT")
    return parsed


def _integer(value: str | None) -> int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    if not parsed.is_integer():
        raise FinvizSourceError("FINVIZ_INVALID_INTEGER")
    return int(parsed)


def parse_equity_screener(body: bytes) -> tuple[FinvizEquityScreener, ...]:
    """Parse the public Screener Overview's exact eleven-column table."""
    headers = (
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
    rows = _table_records(body, headers, grouped_letter=True)
    result: list[FinvizEquityScreener] = []
    for row in rows:
        values = _record(headers, row)
        rank = _integer(values["No."])
        symbol = _text(values, "Ticker")
        company = _text(values, "Company")
        if rank is None or symbol is None or company is None:
            raise FinvizSourceError("FINVIZ_REQUIRED_SCREENER_CELL_MISSING")
        result.append(
            FinvizEquityScreener(
                rank=rank,
                symbol=symbol,
                company_name=company,
                sector=_text(values, "Sector"),
                industry=_text(values, "Industry"),
                country=_text(values, "Country"),
                market_cap_usd=_amount(values["Market Cap"]),
                pe_ratio=_number(values["P/E"]),
                price_usd=_number(values["Price"]),
                change_pct=_percent(values["Change %"]),
                volume_shares=_amount(values["Volume"]),
            )
        )
    return tuple(result)


def parse_etf_price_performance(body: bytes) -> tuple[FinvizEtfPricePerformance, ...]:
    """Parse the public Screener ETF Perf table."""
    headers = (
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
    )
    rows = _table_records(body, headers, grouped_letter=True)
    result: list[FinvizEtfPricePerformance] = []
    for row in rows:
        values = _record(headers, row)
        symbol = _text(values, "Ticker")
        if symbol is None:
            raise FinvizSourceError("FINVIZ_REQUIRED_SCREENER_CELL_MISSING")
        result.append(
            FinvizEtfPricePerformance(
                symbol=symbol,
                holdings=_integer(values["Holdings"]),
                assets_under_management_usd=_amount(values["AUM"]),
                flows_1m_pct=_percent(values["Flows% 1M"]),
                flows_3m_pct=_percent(values["Flows% 3M"]),
                ytd_return_pct=_percent(values["YTD Return%"]),
                return_1y_pct=_percent(values["1Y Return%"]),
                return_3y_pct=_percent(values["3Y Return%"]),
                expense_5y_pct=_percent(values["5Y Expense"]),
                price_usd=_number(values["Price"]),
                change_pct=_percent(values["Change %"]),
            )
        )
    return tuple(result)


def parse_price_performance(body: bytes) -> tuple[FinvizPricePerformance, ...]:
    """Parse the public Screener Performance table."""
    headers = (
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
    )
    rows = _table_records(body, headers, grouped_letter=True)
    result: list[FinvizPricePerformance] = []
    for row in rows:
        values = _record(headers, row)
        symbol = _text(values, "Ticker")
        if symbol is None:
            raise FinvizSourceError("FINVIZ_REQUIRED_SCREENER_CELL_MISSING")
        result.append(
            FinvizPricePerformance(
                symbol=symbol,
                performance_week_pct=_percent(values["Perf Week"]),
                performance_month_pct=_percent(values["Perf Month"]),
                performance_quarter_pct=_percent(values["Perf Quart"]),
                performance_half_year_pct=_percent(values["Perf Half"]),
                performance_ytd_pct=_percent(values["Perf YTD"]),
                performance_year_pct=_percent(values["Perf Year"]),
                performance_3y_pct=_percent(values["Perf 3Y"]),
                performance_5y_pct=_percent(values["Perf 5Y"]),
                performance_10y_pct=_percent(values["Perf 10Y"]),
                volatility_week_pct=_percent(values["Volatility W"]),
                volatility_month_pct=_percent(values["Volatility M"]),
                average_volume_shares=_amount(values["Avg Volume"]),
                relative_volume=_number(values["Rel Volume"]),
                price_usd=_number(values["Price"]),
                change_pct=_percent(values["Change %"]),
                volume_shares=_amount(values["Volume"]),
            )
        )
    return tuple(result)


def parse_group_comparison(body: bytes, *, metric: str) -> tuple[FinvizGroupComparison, ...]:
    """Parse sector rows from the documented Finviz Groups view selection."""
    if metric == "overview":
        headers = (
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
        )
        rows = _table_records(body, headers)
        result: list[FinvizGroupComparison] = []
        for row in rows:
            values = _record(headers, row)
            name = _text(values, "Name")
            if name is None:
                raise FinvizSourceError("FINVIZ_REQUIRED_GROUP_CELL_MISSING")
            result.append(
                FinvizGroupComparison(
                    group="sector",
                    name=name,
                    stocks=_integer(values["Stocks"]),
                    market_cap_usd=_amount(values["Market Cap"]),
                    dividend_yield_pct=_percent(values["Dividend"]),
                    pe_ratio=_number(values["P/E"]),
                    forward_pe_ratio=_number(values["Fwd P/E"]),
                    peg_ratio=_number(values["PEG"]),
                    long_term_debt_to_equity=_number(values["LTDebt/Eq"]),
                    debt_to_equity=_number(values["Debt/Eq"]),
                    short_float_pct=_percent(values["Float Short"]),
                    recommendation=_number(values["Recom"]),
                    change_pct=_percent(values["Change %"]),
                    volume_shares=_amount(values["Volume"]),
                )
            )
        return tuple(result)
    if metric == "performance":
        performance_headers = (
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
        )
        try:
            rows = _table_records(body, performance_headers)
        except FinvizSourceError as error:
            if str(error) != "FINVIZ_EXPECTED_COLUMNS_MISSING":
                raise
            rows = _headerless_group_performance_rows(body)
        result = []
        seen_ranks: set[int] = set()
        seen_names: set[str] = set()
        for row in rows:
            if len(row) != len(performance_headers):
                raise FinvizSourceError("FINVIZ_ROW_WIDTH_MISMATCH")
            values = _record(performance_headers, row)
            rank = _integer(values["No."])
            name = _text(values, "Name")
            if rank is None or rank < 1 or name is None:
                raise FinvizSourceError("FINVIZ_REQUIRED_GROUP_CELL_MISSING")
            if rank in seen_ranks or name in seen_names:
                raise FinvizSourceError("FINVIZ_DUPLICATE_GROUP_ROW")
            seen_ranks.add(rank)
            seen_names.add(name)
            result.append(
                FinvizGroupComparison(
                    group="sector",
                    name=name,
                    performance_week_pct=_percent(values["Perf Week"]),
                    performance_month_pct=_percent(values["Perf Month"]),
                    performance_quarter_pct=_percent(values["Perf Quart"]),
                    performance_half_year_pct=_percent(values["Perf Half"]),
                    performance_year_pct=_percent(values["Perf Year"]),
                    performance_ytd_pct=_percent(values["Perf YTD"]),
                    average_volume_shares=_amount(values["Avg Volume"]),
                    relative_volume=_number(values["Rel Volume"]),
                    change_pct=_percent(values["Change %"]),
                    volume_shares=_amount(values["Volume"]),
                )
            )
        return tuple(result)
    raise FinvizSourceError("FINVIZ_UNSUPPORTED_GROUP_VIEW")


_SECTOR_NAMES = frozenset(
    {
        "Basic Materials",
        "Communication Services",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Financial",
        "Healthcare",
        "Industrials",
        "Real Estate",
        "Technology",
        "Utilities",
    }
)


def _headerless_group_performance_rows(body: bytes) -> tuple[tuple[str, ...], ...]:
    """Parse the observed headerless 12-cell sector performance rows, never arbitrary tables."""
    snapshot = parse_html(body)
    title = snapshot.title.casefold()
    if "group screener" not in title or "performance" not in title or "sector" not in title:
        raise FinvizSourceError("FINVIZ_EXPECTED_COLUMNS_MISSING")
    candidates = []
    for table in snapshot.tables:
        rows = tuple(row for row in table.rows if row and any(value for value in row))
        if not rows or len(rows[0]) != 12:
            continue
        if not _NUMBER.fullmatch(rows[0][0]) or rows[0][1] not in _SECTOR_NAMES:
            continue
        candidates.append(rows)
    if not candidates:
        raise FinvizSourceError("FINVIZ_EXPECTED_COLUMNS_MISSING")
    if len(candidates) > 1:
        raise FinvizSourceError("FINVIZ_AMBIGUOUS_TABLE")
    return candidates[0]


_METRIC_ALIASES: dict[str, str] = {
    "marketcap": "market_cap_usd",
    "enterprisevalue": "enterprise_value_usd",
    "income": "net_income_usd",
    "sales": "sales_usd",
    "booksh": "book_value_per_share_usd",
    "cashsh": "cash_per_share_usd",
    "pe": "pe_ratio",
    "forwardpe": "forward_pe_ratio",
    "peg": "peg_ratio",
    "ps": "price_to_sales",
    "pb": "price_to_book",
    "pc": "price_to_cash",
    "pfcf": "price_to_free_cash_flow",
    "evebitda": "enterprise_value_to_ebitda",
    "evsales": "enterprise_value_to_sales",
    "quickratio": "quick_ratio",
    "currentratio": "current_ratio",
    "debteq": "debt_to_equity",
    "ltdebteq": "long_term_debt_to_equity",
    "epsttm": "eps_ttm_usd",
    "epsnextq": "eps_next_quarter_usd",
    "epsthisy": "eps_growth_this_year_pct",
    "epsnexty": "eps_growth_next_year_pct",
    "epsnext5y": "eps_growth_next_5y_pct",
    "epspast3y": "eps_growth_past_3y_pct",
    "epspast5y": "eps_growth_past_5y_pct",
    "salespast3y": "sales_growth_past_3y_pct",
    "salespast5y": "sales_growth_past_5y_pct",
    "epsyyttm": "eps_growth_ttm_yoy_pct",
    "salesyyttm": "sales_growth_ttm_yoy_pct",
    "epsqq": "eps_growth_qoq_pct",
    "salesqq": "sales_growth_qoq_pct",
    "insiderown": "insider_ownership_pct",
    "insidertrans": "insider_transactions_pct",
    "instown": "institutional_ownership_pct",
    "insttrans": "institutional_transactions_pct",
    "roa": "roa_pct",
    "roe": "roe_pct",
    "roic": "roic_pct",
    "grossmargin": "gross_margin_pct",
    "opermargin": "operating_margin_pct",
    "profitmargin": "profit_margin_pct",
    "sma20": "sma_20_pct",
    "sma50": "sma_50_pct",
    "sma200": "sma_200_pct",
    "shsoutstand": "shares_outstanding",
    "shsfloat": "float_shares",
    "shortfloat": "short_float_pct",
    "shortratio": "short_ratio",
    "shortinterest": "short_interest_shares",
    "52whigh": "high_52w_usd",
    "52wlow": "low_52w_usd",
    "atr14": "average_true_range_14_usd",
    "rsi14": "rsi_14",
    "beta": "beta",
    "relvolume": "relative_volume",
    "avgvolume": "average_volume_shares",
    "volume": "volume_shares",
    "perfweek": "performance_week_pct",
    "perfmonth": "performance_month_pct",
    "perfquarter": "performance_quarter_pct",
    "perfhalfy": "performance_half_year_pct",
    "perfytd": "performance_ytd_pct",
    "perfyear": "performance_year_pct",
    "perf3y": "performance_3y_pct",
    "perf5y": "performance_5y_pct",
    "perf10y": "performance_10y_pct",
    "recom": "recommendation",
    "targetprice": "target_price_usd",
    "prevclose": "previous_close_usd",
    "price": "price_usd",
    "change": "change_pct",
}
_PERCENT_METRICS = frozenset(
    {
        "dividend_estimate_yield_pct",
        "dividend_ttm_yield_pct",
        "dividend_growth_3y_pct",
        "dividend_growth_5y_pct",
        "payout_pct",
        "eps_growth_this_year_pct",
        "eps_growth_next_year_pct",
        "eps_growth_next_5y_pct",
        "eps_growth_past_3y_pct",
        "eps_growth_past_5y_pct",
        "sales_growth_past_3y_pct",
        "sales_growth_past_5y_pct",
        "eps_growth_ttm_yoy_pct",
        "sales_growth_ttm_yoy_pct",
        "eps_growth_qoq_pct",
        "sales_growth_qoq_pct",
        "insider_ownership_pct",
        "insider_transactions_pct",
        "institutional_ownership_pct",
        "institutional_transactions_pct",
        "roa_pct",
        "roe_pct",
        "roic_pct",
        "gross_margin_pct",
        "operating_margin_pct",
        "profit_margin_pct",
        "sma_20_pct",
        "sma_50_pct",
        "sma_200_pct",
        "short_float_pct",
        "volatility_week_pct",
        "volatility_month_pct",
        "performance_week_pct",
        "performance_month_pct",
        "performance_quarter_pct",
        "performance_half_year_pct",
        "performance_ytd_pct",
        "performance_year_pct",
        "performance_3y_pct",
        "performance_5y_pct",
        "performance_10y_pct",
        "change_pct",
    }
)
_AMOUNT_METRICS = frozenset(
    {
        "market_cap_usd",
        "enterprise_value_usd",
        "net_income_usd",
        "sales_usd",
        "shares_outstanding",
        "float_shares",
        "short_interest_shares",
        "average_volume_shares",
        "volume_shares",
    }
)


def _metric_pairs(snapshot: HTMLSnapshot) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for table in snapshot.tables:
        for row in table.rows:
            if len(row) >= 2 and len(row) % 2 == 0:
                pairs.extend((row[index], row[index + 1]) for index in range(0, len(row), 2))
    return tuple((label, value) for label, value in pairs if _clean_text(label))


_FLAT_METRIC_LABELS = (
    "Market Cap",
    "Enterprise Value",
    "Income",
    "Sales",
    "Book/sh",
    "Cash/sh",
    "Dividend Est.",
    "Dividend TTM",
    "Dividend Ex-Date",
    "Dividend Gr. 3/5Y",
    "Payout",
    "Employees",
    "IPO",
    "Forward P/E",
    "P/E",
    "PEG",
    "P/S",
    "P/B",
    "P/C",
    "P/FCF",
    "EV/EBITDA",
    "EV/Sales",
    "Quick Ratio",
    "Current Ratio",
    "LT Debt/Eq",
    "Debt/Eq",
    "Option/Short",
    "EPS (ttm)",
    "EPS next Y",
    "EPS next Q",
    "EPS this Y",
    "EPS next 5Y",
    "EPS past 3/5Y",
    "Sales past 3/5Y",
    "EPS Y/Y TTM",
    "Sales Y/Y TTM",
    "EPS Q/Q",
    "Sales Q/Q",
    "Earnings",
    "EPS/Sales Surpr.",
    "Insider Own",
    "Insider Trans",
    "Inst Own",
    "Inst Trans",
    "ROA",
    "ROE",
    "ROIC",
    "Gross Margin",
    "Oper. Margin",
    "Profit Margin",
    "Trades",
    "Shs Outstand",
    "Shs Float",
    "Short Float",
    "Short Ratio",
    "Short Interest",
    "52W High",
    "52W Low",
    "Volatility",
    "ATR (14)",
    "RSI (14)",
    "Beta",
    "Rel Volume",
    "Avg Volume",
    "Volume",
    "Perf Week",
    "Perf Month",
    "Perf Quarter",
    "Perf Half Y",
    "Perf YTD",
    "Perf Year",
    "Perf 3Y",
    "Perf 5Y",
    "Perf 10Y",
    "Recom",
    "Target Price",
    "Prev Close",
    "Price",
    "Change %",
)
_FLAT_METRIC_LABEL_PATTERN = re.compile(
    "|".join(re.escape(label) for label in sorted(_FLAT_METRIC_LABELS, key=len, reverse=True))
)
_SMA_VALUE = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)%"
_FLAT_SMA_BLOCK = re.compile(
    rf"SMA20(?P<sma20>{_SMA_VALUE})\s*"
    rf"SMA50(?P<sma50>{_SMA_VALUE})\s*"
    rf"SMA200(?P<sma200>{_SMA_VALUE})"
)


def _flat_quote_cell(snapshot: HTMLSnapshot) -> str | None:
    """Return the visible quote facts from the one-cell current-page layout."""
    candidates = [
        row[0]
        for table in snapshot.tables
        for row in table.rows
        if len(row) == 1 and "Market Cap" in row[0] and "Change %" in row[0]
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise FinvizSourceError("FINVIZ_AMBIGUOUS_FLAT_QUOTE")

    text = candidates[0]
    start = text.find("Market Cap")
    change = text.find("Change %", start)
    if start < 0 or change < 0:
        raise FinvizSourceError("FINVIZ_FLAT_QUOTE_BOUNDARY_MISSING")
    change_value = re.match(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)%", text[change + len("Change %") :])
    if change_value is None:
        raise FinvizSourceError("FINVIZ_INVALID_PERCENT")
    end = change + len("Change %") + change_value.end()
    return text[start:end].strip()


def _flat_quote_pairs(snapshot: HTMLSnapshot) -> tuple[tuple[str, str], ...]:
    """Split the observed one-cell quote text only at known Finviz metric labels."""
    text = _flat_quote_cell(snapshot)
    if text is None:
        return ()

    sma_pairs: tuple[tuple[str, str], ...] = ()
    sma_match = _FLAT_SMA_BLOCK.search(text)
    if sma_match is not None:
        sma_pairs = (
            ("SMA20", sma_match.group("sma20")),
            ("SMA50", sma_match.group("sma50")),
            ("SMA200", sma_match.group("sma200")),
        )
        text = text[: sma_match.start()] + " " + text[sma_match.end() :]

    matches = list(_FLAT_METRIC_LABEL_PATTERN.finditer(text))
    terminal_index = next(
        (index for index, match in enumerate(matches) if match.group() == "Change %"),
        None,
    )
    if terminal_index is None:
        raise FinvizSourceError("FINVIZ_FLAT_QUOTE_BOUNDARY_MISSING")
    matches = matches[: terminal_index + 1]
    pairs = [
        (match.group(), text[match.end() : matches[index + 1].start()].strip())
        for index, match in enumerate(matches[:-1])
    ]
    pairs.append((matches[-1].group(), text[matches[-1].end() :].strip()))
    return tuple(pairs) + sma_pairs


def _stock_metric_pairs(snapshot: HTMLSnapshot) -> tuple[tuple[str, str], ...]:
    """Prefer the observed flat quote facts, retaining support for paired legacy tables."""
    return _flat_quote_pairs(snapshot) or _metric_pairs(snapshot)


def _require_complete_flat_metrics(pairs: tuple[tuple[str, str], ...]) -> None:
    """Reject short UI snippets instead of presenting them as the full metrics view."""
    labels = [_header_key(label) for label, _value in pairs]
    required = (
        "marketcap",
        "enterprisevalue",
        "income",
        "sales",
        "booksh",
        "cashsh",
        "dividendest",
        "dividendttm",
        "dividendexdate",
        "dividendgr35y",
        "employees",
        "ipo",
        "pe",
        "forwardpe",
        "epsttm",
        "epsnextq",
        "epsthisy",
        "epsnext5y",
        "epspast35y",
        "salespast35y",
        "epsyyttm",
        "salesyyttm",
        "epsqq",
        "salesqq",
        "earnings",
        "epssalessurpr",
        "insiderown",
        "insidertrans",
        "instown",
        "insttrans",
        "roa",
        "roe",
        "roic",
        "grossmargin",
        "opermargin",
        "profitmargin",
        "sma20",
        "sma50",
        "sma200",
        "shsoutstand",
        "shsfloat",
        "shortfloat",
        "shortratio",
        "shortinterest",
        "52whigh",
        "52wlow",
        "volatility",
        "atr14",
        "rsi14",
        "beta",
        "relvolume",
        "avgvolume",
        "volume",
        "perfweek",
        "perfmonth",
        "perfquarter",
        "perfhalfy",
        "perfytd",
        "perfyear",
        "perf3y",
        "perf5y",
        "perf10y",
        "recom",
        "targetprice",
        "prevclose",
        "price",
        "change",
    )
    if any(label not in labels for label in required) or labels.count("epsnexty") < 2:
        raise FinvizSourceError("FINVIZ_KEY_METRICS_MISSING")


def _stock_identity(snapshot: HTMLSnapshot, requested_symbol: str) -> tuple[str, str]:
    symbol = None
    company = None
    title_symbol = None
    for heading in snapshot.headings:
        if _SYMBOL.fullmatch(heading.upper()):
            symbol = heading.upper()
            break
    title = snapshot.title
    if title:
        title_match = re.match(r"^([A-Za-z][A-Za-z0-9.\-]{0,9})\s*-\s*(.+)$", title)
        if title_match:
            title_symbol = title_match.group(1).upper()
            remainder = title_match.group(2)
            company = re.split(r"\s+Stock Price and Quote", remainder, maxsplit=1)[0].strip()
            symbol = symbol or title_symbol
    if symbol is None and requested_symbol in snapshot.headings:
        symbol = requested_symbol
    title_mismatch = title_symbol is not None and title_symbol != requested_symbol
    if symbol != requested_symbol or title_mismatch or not company:
        raise FinvizSourceError("FINVIZ_STOCK_IDENTITY_MISSING")
    return symbol, company


def _profile_tags(snapshot: HTMLSnapshot) -> tuple[str | None, str | None, str | None]:
    values: dict[str, str] = {}
    for href, text in snapshot.anchors:
        if not text:
            continue
        filters = parse_qs(urlsplit(href).query).get("f", [])
        for item in filters:
            prefix = item.casefold().split("_", maxsplit=1)[0]
            target = {"sec": "sector", "ind": "industry", "geo": "country"}.get(prefix)
            if target is not None:
                values[target] = text
    for label, value in _metric_pairs(snapshot):
        key = _header_key(label)
        target = {"sector": "sector", "industry": "industry", "country": "country"}.get(key)
        if target is not None and value.strip().casefold() not in _EMPTY:
            values[target] = value.strip()
    return values.get("sector"), values.get("industry"), values.get("country")


def parse_equity_info(body: bytes, *, symbol: str) -> FinvizEquityInfo:
    """Parse company identity and published profile fields from one stock page."""
    snapshot = parse_html(body)
    parsed_symbol, company = _stock_identity(snapshot, symbol)
    pairs = {_header_key(label): value for label, value in _stock_metric_pairs(snapshot)}
    ipo_text = pairs.get("ipo")
    ipo_date = None
    if ipo_text and ipo_text.strip().casefold() not in _EMPTY:
        for pattern in ("%b %d, %Y", "%b %d %Y"):
            try:
                ipo_date = datetime.strptime(ipo_text.strip(), pattern).date()
                break
            except ValueError:
                continue
        if ipo_date is None:
            raise FinvizSourceError("FINVIZ_INVALID_IPO_DATE")
    sector, industry, country = _profile_tags(snapshot)
    return FinvizEquityInfo(
        symbol=parsed_symbol,
        company_name=company,
        sector=sector,
        industry=industry,
        country=country,
        market_cap_usd=_amount(pairs.get("marketcap")),
        employees=_integer(pairs.get("employees")),
        ipo_date=ipo_date,
    )


def parse_key_metrics(body: bytes, *, symbol: str) -> FinvizKeyMetrics:
    """Parse recognized Finviz quote metrics without inventing absent cells."""
    snapshot = parse_html(body)
    parsed_symbol, _ = _stock_identity(snapshot, symbol)
    metric_pairs = _stock_metric_pairs(snapshot)
    if _flat_quote_cell(snapshot) is not None:
        _require_complete_flat_metrics(metric_pairs)
    recognized: dict[str, str] = {}
    for label, value in metric_pairs:
        key = _header_key(label)
        field = _METRIC_ALIASES.get(key)
        if key == "epsnexty":
            field = (
                "eps_growth_next_year_pct" if value.strip().endswith("%") else "eps_next_year_usd"
            )
        elif key in {
            "52whigh",
            "52wlow",
            "dividendest",
            "dividendttm",
            "dividendgr35y",
            "epspast35y",
            "salespast35y",
            "volatility",
        }:
            field = None
        if field is not None:
            if field in recognized:
                raise FinvizSourceError("FINVIZ_DUPLICATE_METRIC")
            recognized[field] = value
    if not recognized:
        raise FinvizSourceError("FINVIZ_KEY_METRICS_MISSING")
    result: dict[str, object] = {"symbol": parsed_symbol}
    for field, value in recognized.items():
        if field in _PERCENT_METRICS:
            result[field] = _percent(value)
        elif field in _AMOUNT_METRICS:
            result[field] = _amount(value)
        else:
            result[field] = _number(value)
    # Dividend-specific labels are a displayed two-value cell, such as ``1.10 (0.33%)``.
    for source_label, amount_field, yield_field in (
        ("dividendest", "dividend_estimate_per_share_usd", "dividend_estimate_yield_pct"),
        ("dividendttm", "dividend_ttm_per_share_usd", "dividend_ttm_yield_pct"),
    ):
        compound = next(
            (value for label, value in metric_pairs if _header_key(label) == source_label),
            None,
        )
        if compound and compound.strip().casefold() not in _EMPTY:
            match = re.fullmatch(r"\s*\$?([\d,.]+)\s*\(\s*([+-]?[\d,.]+)%\s*\)\s*", compound)
            if match is None:
                raise FinvizSourceError("FINVIZ_INVALID_DIVIDEND_METRIC")
            result[amount_field] = _number(match.group(1))
            result[yield_field] = _number(match.group(2))
    dividend_growth = next(
        (value for label, value in metric_pairs if _header_key(label) == "dividendgr35y"),
        None,
    )
    if dividend_growth and dividend_growth.strip().casefold() not in _EMPTY:
        numbers = re.findall(r"[+-]?[\d,]+(?:\.\d+)?", dividend_growth)
        if len(numbers) != 2 or "%" not in dividend_growth:
            raise FinvizSourceError("FINVIZ_INVALID_COMPOUND_METRIC")
        result["dividend_growth_3y_pct"] = _number(numbers[0])
        result["dividend_growth_5y_pct"] = _number(numbers[1])
    # Finviz splits the two horizon values into one cell for these labels.
    for label, first_field, second_field in (
        ("epspast35y", "eps_growth_past_3y_pct", "eps_growth_past_5y_pct"),
        ("salespast35y", "sales_growth_past_3y_pct", "sales_growth_past_5y_pct"),
        ("volatility", "volatility_week_pct", "volatility_month_pct"),
        ("52whigh", "high_52w_usd", "high_52w_change_pct"),
        ("52wlow", "low_52w_usd", "low_52w_change_pct"),
    ):
        compound = next(
            (value for key, value in metric_pairs if _header_key(key) == label),
            None,
        )
        if compound and compound.strip().casefold() not in _EMPTY:
            numbers = re.findall(r"[+-]?[\d,]+(?:\.\d+)?", compound)
            if len(numbers) != 2:
                raise FinvizSourceError("FINVIZ_INVALID_COMPOUND_METRIC")
            if label in {"epspast35y", "salespast35y", "volatility"}:
                result[first_field] = _percent(numbers[0] + "%")
                result[second_field] = _percent(numbers[1] + "%")
            elif label in {"52whigh", "52wlow"}:
                result[first_field] = _number(numbers[0])
                result[second_field] = _percent(numbers[1] + "%")
    return FinvizKeyMetrics.model_validate(result)


_PRICE_TARGET_HEADERS = (
    "Date",
    "Action",
    "Analyst",
    "Rating Change",
    "Price Target Change",
)
_ANALYST_EVENT_DATE = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2}-\d{2}")
_ANALYST_ACTION = re.compile(r"(Reiterated|Upgrade|Downgrade)")
_ANALYST_RATING_SUFFIXES = tuple(
    sorted(
        (
            "Neutral → Buy",
            "Hold → Underperform",
            "Buy → Hold",
            "Hold → Buy",
            "Sector Weight → Underweight",
            "Overweight",
            "Outperform",
            "Neutral",
            "Underweight",
            "Buy",
        ),
        key=len,
        reverse=True,
    )
)
_ANALYST_TARGET_SUFFIX = re.compile(
    r"(?P<target>\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s*(?:→|->)\s*\$?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)?)\s*\Z"
)


def _parse_target_price(value: str) -> tuple[float | None, float | None]:
    text = value.strip()
    if text.casefold() in _EMPTY:
        return None, None
    number = r"\$?\s*[+-]?[\d,]+(?:\.\d+)?"
    transition = re.fullmatch(rf"({number})\s*(?:→|->)\s*({number})", text)
    if transition is not None:
        return _number(transition.group(1)), _number(transition.group(2))
    single = re.fullmatch(number, text)
    if single is not None:
        return None, _number(single.group(0))
    raise FinvizSourceError("FINVIZ_INVALID_PRICE_TARGET")


def _analyst_date(value: str) -> date:
    text = value.strip()
    if re.fullmatch(r"[A-Z][a-z]{2}-\d{2}-\d{2}", text):
        pattern = "%b-%d-%y"
    elif re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", text):
        pattern = "%b %d, %Y"
    elif re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} \d{4}", text):
        pattern = "%b %d %Y"
    else:
        raise FinvizSourceError("FINVIZ_INVALID_ANALYST_DATE")
    try:
        return datetime.strptime(text, pattern).date()
    except ValueError as error:
        raise FinvizSourceError("FINVIZ_INVALID_ANALYST_DATE") from error


def _flat_analyst_rows(snapshot: HTMLSnapshot) -> tuple[dict[str, str], ...]:
    """Extract only event rows between the visible analyst header and history boundary."""
    cells = [
        row[0]
        for table in snapshot.tables
        for row in table.rows
        if len(row) == 1
        and "Date Action Analyst Rating Change Price Target Change" in row[0]
        and "Show Previous Ratings" in row[0]
    ]
    if not cells:
        raise FinvizSourceError("FINVIZ_EXPECTED_COLUMNS_MISSING")
    if len(cells) > 1:
        raise FinvizSourceError("FINVIZ_AMBIGUOUS_ANALYST_TABLE")
    text = cells[0]
    header = "Date Action Analyst Rating Change Price Target Change"
    data_start = text.find(header) + len(header)
    boundary = text.find("Show Previous Ratings", data_start)
    event_text = text[data_start:boundary].strip()
    date_matches = list(_ANALYST_EVENT_DATE.finditer(event_text))
    if not date_matches or event_text[: date_matches[0].start()].strip():
        raise FinvizSourceError("FINVIZ_INVALID_ANALYST_ROWS")

    events: list[dict[str, str]] = []
    for index, date_match in enumerate(date_matches):
        event_end = (
            date_matches[index + 1].start() if index + 1 < len(date_matches) else len(event_text)
        )
        event = event_text[date_match.end() : event_end].strip()
        action_match = _ANALYST_ACTION.match(event)
        if action_match is None:
            raise FinvizSourceError("FINVIZ_INVALID_ANALYST_ACTION")
        remainder = event[action_match.end() :].strip()
        target_match = _ANALYST_TARGET_SUFFIX.search(remainder)
        if target_match is None:
            raise FinvizSourceError("FINVIZ_INVALID_PRICE_TARGET")
        rating_and_analyst = remainder[: target_match.start()].strip()
        rating = next(
            (suffix for suffix in _ANALYST_RATING_SUFFIXES if rating_and_analyst.endswith(suffix)),
            None,
        )
        if rating is None:
            raise FinvizSourceError("FINVIZ_UNKNOWN_ANALYST_RATING")
        analyst = rating_and_analyst[: -len(rating)].strip()
        if not analyst:
            raise FinvizSourceError("FINVIZ_REQUIRED_ANALYST_CELL_MISSING")
        events.append(
            {
                "Date": date_match.group(),
                "Action": action_match.group(),
                "Analyst": analyst,
                "Rating Change": rating,
                "Price Target Change": target_match.group("target"),
            }
        )
    return tuple(events)


def parse_price_targets(
    body: bytes,
    *,
    symbol: str,
    start_date: date | None,
    end_date: date | None,
) -> tuple[FinvizPriceTarget, ...]:
    """Parse only analyst events actually visible in the stock page's ratings table."""
    snapshot = parse_html(body)
    parsed_symbol, _ = _stock_identity(snapshot, symbol)
    try:
        rows = _table_records(body, _PRICE_TARGET_HEADERS)
    except FinvizSourceError as error:
        if str(error) != "FINVIZ_EXPECTED_COLUMNS_MISSING":
            raise
        rows = tuple(
            tuple(event[header] for header in _PRICE_TARGET_HEADERS)
            for event in _flat_analyst_rows(snapshot)
        )
    result: list[FinvizPriceTarget] = []
    for row in rows:
        values = _record(_PRICE_TARGET_HEADERS, row)
        event_date = _analyst_date(values["Date"])
        if start_date is not None and event_date < start_date:
            continue
        if end_date is not None and event_date > end_date:
            continue
        action = _text(values, "Action")
        analyst = _text(values, "Analyst")
        if action is None or analyst is None:
            raise FinvizSourceError("FINVIZ_REQUIRED_ANALYST_CELL_MISSING")
        previous_target, current_target = _parse_target_price(values["Price Target Change"])
        result.append(
            FinvizPriceTarget(
                symbol=parsed_symbol,
                date=event_date,
                action=action,
                analyst=analyst,
                rating_change=_text(values, "Rating Change"),
                price_target_previous_usd=previous_target,
                price_target_usd=current_target,
            )
        )
    return tuple(result)
