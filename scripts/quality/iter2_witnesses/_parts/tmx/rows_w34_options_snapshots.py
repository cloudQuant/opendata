"""Wave-34 TMX shard: the two envelope-family rows, witnessed offline.

Transport, measured rather than assumed.  TMX's SDK is not installed here, so both rows are served
through the recorded-envelope ``client_seam``: the runner replaces ``get_shared_http_client`` in
``opendata.data.providers/tmx/models/fetchers_w34_envelope`` -- the module that *calls* it, the same
reason the parent module names ``models/client.py`` (``iter2_witnesses/tmx.py:8-12`` explains the
import-time binding) -- and every leg declares its own ``method``, so the fake's widened surface
cannot turn a directory GET into a free POST or a GraphQL POST into a free GET.

The shipped fake fills ``response._content = body`` and sets ``Content-Type`` from the spec
(``iter2_claims_probe_runner.py:344-356``), which is why the wave-33 "no envelope shape" veto did
not survive re-measurement: an HTML table and a CSV file are bytes plus a media type, and both of
this shard's bodies are declared as **lists of lines** (which the fake encodes as text and which a
tamper can therefore address by line index).  Multi-leg fixtures are the shipped shape too -- one
spec carries a ``responses`` list, each entry matched by ``url_contains`` (``:321-347``) -- and the
send budget is read off that list (``_carrier_sends``, ``:115-137``), not declared by this shard.

Legs and hosts, with the upstream shape each leg is derived from (the OpenBB checkout is an
output-contract reference only; no code was copied):

``OptionsChains`` -- 2 legs, 1 host (``www.m-x.ca``)
    1. GET ``…/en/trading/data/quotes?symbol=`` -- the listed chain as one HTML table: URL at tmx
       ``utils/helpers.py:515``, ``read_html(...)[0]`` at ``:530``, the ``Expiry date`` column at
       ``:533`` (whose cells may carry the ``(Weekly)`` marker, stripped at ``:535``), the
       ``Strike`` column at ``:538``, the ``Calls``/``Puts`` blocks at ``:537`` and ``:544``, the
       six quoted cells per block in the order the nine-column rename table states at ``:517-526``,
       the ``astype(float)``/``astype("int64")`` casts at ``:553-559`` that prove those cells travel
       as plain numeric text, and the contract-symbol derivation at ``:569-581``.
    2. GET ``…/en/trading/data/historical?symbol=…&dnld=1#quotes`` -- the EOD chain file:
       ``utils/helpers.py:618`` and ``:631`` for the URL, ``:647`` for ``read_csv``, ``:655`` for
       ``Symbol`` -> ``contract_symbol``, ``:657`` for ``Call/Put`` 0/1 -> ``call``/``put``,
       ``:653-659`` for the six raw cells upstream drops, ``:669-690`` for the 21-cell rename table
       (snake-cased at ``:712``), ``:695-697`` for ``dte``, ``:699`` for the volatility scale and
       ``:703`` for the zero-strike class row.
    The two legs are joined on ``(expiration, strike, option_type)`` -- the index both of
    upstream's own loaders set (``:541``, ``:548``, ``:701``) -- with the quotes page's six live
    cells winning and the file contributing only the cells the page does not publish plus its own
    ``Symbol`` key.  Required cells: ``contract_symbol`` (``:655`` from the file, ``:577`` derived
    for a contract the file does not list), ``expiration`` (``:533``/``:671``) and ``strike``
    (``:538``/``:670``); the domain names ``contract_symbol`` + ``expiration`` as its natural key
    and ``expiration`` as its ``time_field`` (``opendata/data/domains.yaml:1056-1059``).

``IndexSnapshots`` -- 2 legs, 2 hosts (``tmxinfoservices.com``, ``app-money.tmx.com``)
    1. GET ``…/files/indices/sptsx-indices.json`` -- tmx ``models/index_snapshots.py:166``; the
       per-index assembly at ``:174-196`` (``name_en`` at ``:181``, the ``(USD)`` currency rule at
       ``:186-190``, the ``performance``/``quotedmarketvalue`` spread at ``:176-195``, the symbol
       collection at ``:198-201``) and the alias table that names every source cell at ``:31-49``.
    2. POST ``app-money.tmx.com/graphql`` ``getQuoteForSymbols`` -- the ask at tmx
       ``models/index_snapshots.py:203-219``, the merge by ``symbol`` at ``:232-241``, the two cells
       dropped before it (``longname``, ``percentChange``) at ``:226-231``, and the eight selected
       cells at ``utils/gql.py:472-483`` (``symbol longname price prevClose priceChange
       percentChange weeks52high weeks52low``).  ``client._OPERATIONS`` does not carry this
       operation and ``models/client.py`` is not this shard's to edit, so the document is declared
       in the fetcher module and posted through the same governed client, on the precedent
       ``fetchers_w33_etf_loaders.py:1-20`` set for the three directory loaders.

Produced versus declared.  ``OptionsChains`` maps **25 of the 55** declared columns and
``IndexSnapshots`` **20 of the 26** (spec sheet ``/tmp/w34_tmx_specs.json``, sha256
``dcb533c0de26bfdd5c3ee39cdb9560a0d4e953dcf91968376a1d2cd347be6d0a`` and
``6bff37ba7cda7c7336cf83d545495a1098348f948369214c31209c53253c869b``, 55 and 26 output columns,
``column_basis=declared``).  Every remaining declared column is nullable in the contract and is
left null here because neither envelope family publishes a cell for it -- named in full in each
entry below rather than implied by a passing row.

Row-level guards, which is what each entry tampers, and where each traceback actually ends.
``OptionsChains`` breaks the EOD file's own ``expiration`` cell for the first listed contract
(28/10/2026): the refusal ends at the ``_exact_date`` raise in
``opendata/data/providers/tmx/models/fetchers_w34_envelope.py:392``, before any row is built.
``IndexSnapshots`` quotes the price cell of the GraphQL leg: the refusal ends at the
``_json_number`` raise in the same module at ``:347``, reached through ``_number_cell`` (:746)
from ``_index_rows`` (:715).  Neither site is an envelope, HTTP-status or shape refusal -- those
reject the whole leg rather than one contract cell and are excluded below.

The bodies are **hand-authored fixture bodies, not live captures**: no claim is made that m-x.ca,
tmxinfoservices.com or app-money.tmx.com published these values.  They carry the cell spellings the
upstream rename tables and selections publish (cited per leg above) and the shapes the shipped
mappers read.  Two cells whose raw spelling is *not* evidenced are said so instead of implied: the
19 leading EOD CSV header labels (upstream overwrites the header positionally at
``utils/helpers.py:691``, so only ``Symbol`` and ``Call/Put`` are evidenced raw cells at ``:655``
and ``:657`` -- this fixture therefore names those 19 columns with the rename table's own names and
the mapper reads them by position, requiring only the last two by header text), and the quotes
table's two-row grouped header (``read_html`` yields it as a MultiIndex, which is why upstream can
address ``data["Calls"]`` and ``data["Unnamed: 7_level_0"]`` at ``:537-538``).

Not witnessed by any entry, because no fixture can reach them without rejecting the whole leg:
``TMX_TRANSPORT_ERROR``, ``TMX_HTTP_ERROR``/``TMX_RATE_LIMITED``/``TMX_ACCESS_ERROR``/
``TMX_UPSTREAM_ERROR``, ``TMX_RESPONSE_TOO_LARGE``, ``TMX_BAD_JSON``, ``TMX_GRAPHQL_ERROR``,
``TMX_BAD_ENVELOPE`` (all from ``_envelope_text``/``_quote_panel``, ``:247-333`` -- a seam that
already answers 200 with a small, well-formed body cannot withhold the answer), ``TMX_BAD_FILTER``
for an unsupported region (a query-shape refusal at ``:781``, not a reply-shape one), and
``TMX_BAD_ROWS`` from the count guards at ``:895``/``:940`` (the ceiling arm needs 10 001 records
no carrier here can hold).
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.tmx.models.fetchers_w34_envelope"],
    "attr": "get_shared_http_client",
}

#: The needles that bind each leg to its route.  The two m-x.ca needles share a prefix, so each one
#: carries its own query string -- the fake matches the first spec whose needle is in the URL.
_MX_QUOTES_ROUTE: Final = "m-x.ca/en/trading/data/quotes?symbol="
_MX_EOD_ROUTE: Final = "m-x.ca/en/trading/data/historical?symbol="
_INDEX_ROUTE: Final = "tmxinfoservices.com/files/indices/sptsx-indices.json"
_GQL_ROUTE: Final = "app-money.tmx.com/graphql"

#: The underlying whose chain this shard lists, the two expirations it publishes and the EOD
#: session date the chain file is stamped with.
_SYMBOL: Final = "SHOP"
_EXPIRY_NEAR: Final = "2026-10-16"
_EXPIRY_FAR: Final = "2026-11-20"
_EOD_DATE: Final = "2026-10-09"

#: The index whose panel row is complete, the one published in USD, and the one the quote leg never
#: answers -- the last is what makes the row-existence rule at tmx ``models/index_snapshots.py:278``
#: observable instead of merely declared.
_INDEX_SYMBOL: Final = "CUX"
_USD_INDEX: Final = "SPT"
_UNQUOTED_INDEX: Final = "HGL"

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/tmx/models/fetchers_w34_envelope.py",
    "opendata/data/providers/tmx/models/client.py",
    "opendata/data/providers/tmx/provider.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/tmx.py",
    "opendata/data/domains.yaml",
]


# --------------------------------------------------------------------------- OptionsChains

# The quotes page, as the two-row grouped header ``read_html`` yields (tmx
# ``utils/helpers.py:530-548``).  Column order: the two key cells upstream concatenates ahead of
# each block (``Expiry date`` at ``:533``, ``Strike`` at ``:538``), then the six ``Calls`` cells
# and the six ``Puts`` cells in the rename table's own order (``:517-526``).
_QUOTES_HEADER = (
    "<html><body><table>"
    '<tr><th>Instrument</th><th>Instrument</th><th colspan="6">Calls</th>'
    '<th colspan="6">Puts</th></tr>'
    "<tr><th>Expiry date</th><th>Strike</th>"
    "<th>Bid</th><th>Ask</th><th>Last</th><th>Change</th><th>Open interest</th><th>Volume</th>"
    "<th>Bid</th><th>Ask</th><th>Last</th><th>Change</th><th>Open interest</th><th>Volume</th>"
    "</tr>"
)

_QUOTES_ROWS: Final[list[tuple[str, str, tuple[str, ...], tuple[str, ...]]]] = [
    (
        f"{_EXPIRY_NEAR}(Weekly)",
        "120.00",
        ("10.50", "11.00", "10.80", "0.90", "3210", "45"),
        ("9.80", "10.40", "10.10", "-0.40", "1805", "12"),
    ),
    (
        _EXPIRY_FAR,
        "125.00",
        ("14.20", "14.90", "14.55", "1.15", "640", "9"),
        ("13.10", "13.80", "N/A", "N/A", "410", "0"),
    ),
]


def _quotes_table() -> list[str]:
    """The chain page as one table per line, so a tamper can address a row by index."""
    lines = [_QUOTES_HEADER]
    for expiry, strike, calls, puts in _QUOTES_ROWS:
        cells = [expiry, strike, *calls, *puts]
        lines.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    lines.append("</table></body></html>")
    return lines


# The EOD chain file.  The first 19 columns are the leading cells of upstream's positional rename
# table (tmx ``utils/helpers.py:669-690``); the last two are the two raw cells the loader names
# (``Symbol`` at ``:655``, ``Call/Put`` at ``:657``).  Order per row: eod_date, strike, expiration,
# closeBid, closeAsk, closeBidSize, closeAskSize, lastTradePrice, volume, prevClose, change, open,
# high, low, totalValue, transactions, settlementPrice, openInterest, impliedVolatility.
_EOD_HEADER: Final = (
    "eod_date,strike,expiration,closeBid,closeAsk,closeBidSize,closeAskSize,lastTradePrice,"
    "volume,prevClose,change,open,high,low,totalValue,transactions,settlementPrice,openInterest,"
    "impliedVolatility,Symbol,Call/Put"
)

_EOD_ROWS: Final[list[list[str]]] = [
    [
        "2026-10-09",
        "120.000",
        _EXPIRY_NEAR,
        "10.40",
        "11.10",
        "12",
        "20",
        "10.80",
        "45",
        "9.90",
        "0.90",
        "10.10",
        "11.20",
        "9.80",
        "48600",
        "12",
        "10.75",
        "3210",
        "34.2",
        "SHOP  261016C00120000",
        "0",
    ],
    [
        "2026-10-09",
        "120.000",
        _EXPIRY_NEAR,
        "9.90",
        "10.50",
        "8",
        "15",
        "10.10",
        "12",
        "10.50",
        "-0.40",
        "10.60",
        "10.90",
        "9.70",
        "12120",
        "5",
        "10.20",
        "1805",
        "35.8",
        "SHOP  261016P00120000",
        "1",
    ],
    [
        "2026-10-09",
        "125.000",
        _EXPIRY_FAR,
        "14.10",
        "14.95",
        "25",
        "10",
        "14.55",
        "9",
        "13.40",
        "1.15",
        "13.90",
        "14.80",
        "13.70",
        "13095",
        "4",
        "14.30",
        "640",
        "31.5",
        "SHOP  261120C00125000",
        "0",
    ],
    [
        "2026-10-09",
        "125.000",
        _EXPIRY_FAR,
        "13.00",
        "13.90",
        "20",
        "6",
        "13.20",
        "0",
        "12.80",
        "0.40",
        "12.90",
        "13.60",
        "12.70",
        "0",
        "0",
        "13.10",
        "410",
        "33.1",
        "SHOP  261120P00125000",
        "1",
    ],
    # A contract the live page no longer lists: present in the file, published by no chain row.
    [
        "2026-10-09",
        "115.000",
        "2026-09-18",
        "4.20",
        "4.90",
        "5",
        "5",
        "4.55",
        "3",
        "4.10",
        "0.45",
        "4.20",
        "4.80",
        "4.00",
        "1365",
        "1",
        "4.50",
        "288",
        "41.0",
        "SHOP  260918C00115000",
        "0",
    ],
    # The class/root row, dropped by the same zero-strike test upstream applies (``:703``).
    [
        "2026-10-09",
        "0.000",
        _EXPIRY_NEAR,
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "SHOP  ",
        "0",
    ],
]


def _eod_line(fields: list[str], **overrides: str) -> str:
    """One chain-file row, with named cells replaceable so a tamper states its own defect."""
    replacements = {"expiration": 2, "strike": 1, "impliedVolatility": 18}
    cells = list(fields)
    for name, value in overrides.items():
        cells[replacements[name]] = value
    return ",".join(cells)


def _eod_file() -> list[str]:
    return [_EOD_HEADER] + [_eod_line(row) for row in _EOD_ROWS]


def _options_chains_document() -> dict[str, Any]:
    """Two GETs on one host: the live chain table and the EOD chain file it is joined to."""
    return {
        "responses": [
            {
                "url_contains": _MX_QUOTES_ROUTE,
                "method": "GET",
                "content_type": "text/html",
                "body": _quotes_table(),
            },
            {
                "url_contains": _MX_EOD_ROUTE,
                "method": "GET",
                "content_type": "text/csv",
                "body": _eod_file(),
            },
        ]
    }


# --------------------------------------------------------------------------- IndexSnapshots


def _index_directory_body() -> dict[str, Any]:
    """The information-services document: an ``indices`` object with the two sections the row reads.

    The percent cells are carried at the source scale the alias table expects
    (``yeartodate: 8.6`` means 8.6 %, which the contract's own normalization rule divides by 100 --
    tmx ``models/index_snapshots.py:110-123``), and ``weeks52high``/``weeks52low`` sit in the
    document as well as in the quote leg so the merge order is what decides the published cell
    (``:232-241``).
    """
    return {
        "groups": {"COMPOSITE": [_INDEX_SYMBOL], "SECTOR": [_USD_INDEX], "ESG": [_UNQUOTED_INDEX]},
        "indices": {
            _INDEX_SYMBOL: {
                "name_en": "S&P/TSX Composite Index",
                "performance": {
                    "previousday": 0.82,
                    "monthtodate": 1.9,
                    "quartertodate": 3.4,
                    "yeartodate": 8.6,
                    "weeks52high": 24914.62,
                    "weeks52low": 18999.16,
                },
                "quotedmarketvalue": {
                    "total": 2_400_000_000_000.0,
                    "average": 8_100_000_000.0,
                    "median": 3_200_000_000.0,
                    "sumtop10": 1_512_000_000_000.0,
                    "largest": 310_000_000_000.0,
                    "largestweight": 12.9,
                    "smallest": 11_000_000.0,
                    "smallestweight": 0.01,
                },
            },
            _USD_INDEX: {
                "name_en": "S&P/TSX Capped Energy Index (USD)",
                "performance": {
                    "previousday": -0.44,
                    "monthtodate": -1.2,
                    "quartertodate": 2.6,
                    "yeartodate": 14.3,
                    "weeks52high": 1980.4,
                    "weeks52low": 1420.9,
                },
                "quotedmarketvalue": {
                    "total": 96_400_000_000.0,
                    "average": 3_100_000_000.0,
                    "median": 2_050_000_000.0,
                    "sumtop10": 71_000_000_000.0,
                    "largest": 14_200_000_000.0,
                    "largestweight": 14.73,
                    "smallest": 210_000_000.0,
                    "smallestweight": 0.22,
                },
            },
            _UNQUOTED_INDEX: {
                "name_en": "S&P/TSX Health Care Index",
                "performance": {"previousday": 0.31, "yeartodate": 5.4},
                "quotedmarketvalue": {"total": 41_000_000_000.0},
            },
        },
    }


def _index_quote_leg() -> dict[str, Any]:
    """``getQuoteForSymbols`` for the directory's own symbols, carrying the eight selected cells.

    ``longname`` and ``percentChange`` are present because the source publishes them and the
    provider drops both before merging (tmx ``models/index_snapshots.py:226-231``): the name and
    the day percent belong to the directory's ``name_en`` and ``previousday`` cells.  No index in
    this list is missing a cell another one has, and ``HGL`` -- which the directory does publish --
    is absent, which is what makes the price-existence rule observable.
    """
    return {
        "data": {
            "getQuoteForSymbols": [
                {
                    "symbol": _INDEX_SYMBOL,
                    "longname": "S&P/TSX Composite Index (quoted)",
                    "price": 24017.41,
                    "prevClose": 23840.02,
                    "priceChange": 177.39,
                    "percentChange": 0.744,
                    "weeks52high": 24914.62,
                    "weeks52low": 18999.16,
                },
                {
                    "symbol": _USD_INDEX,
                    "longname": "S&P/TSX Capped Energy Index (USD)",
                    "price": 1874.226,
                    "prevClose": 1882.5,
                    "priceChange": -8.27,
                    "percentChange": -0.439,
                    "weeks52high": 1980.4,
                    "weeks52low": 1420.9,
                },
            ]
        }
    }


def _index_snapshots_document() -> dict[str, Any]:
    """One GET of the index document and one POST that quotes the symbols it names."""
    return {
        "responses": [
            {
                "url_contains": _INDEX_ROUTE,
                "method": "GET",
                "body": _index_directory_body(),
            },
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": _index_quote_leg(),
            },
        ]
    }


# --------------------------------------------------------------------------- entries


def _entry(
    *,
    builder: Any,  # noqa: ANN401  # the fixture document callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    tamper_reason: str,
) -> dict[str, Any]:  # the witness entry shape
    return {
        "fixture_document": builder,
        "client_seam": _SEAM,
        "query": query,
        "tamper": tamper,
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROW",
        "tamper_reason": tamper_reason,
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    }


_MODELS: Final[dict[str, dict[str, Any]]] = {
    "OptionsChains": _entry(
        builder=_options_chains_document,
        query={"symbol": _SYMBOL},
        tamper={
            "op": "set",
            "path": ["responses", 1, "body", 1],
            "value": _eod_line(_EOD_ROWS[0], expiration="28/10/2026"),
        },
        tamper_reason=(
            "the EOD chain file's own expiration cell for the first listed contract is restated "
            "as 28/10/2026, a date the row cannot be filed under: _exact_date parses ISO calendar "
            "dates only, exactly as the file states them (tmx utils/helpers.py:671 and :697 apply "
            "format=%Y-%m-%d), and expiration is one of the three required cells of "
            "TmxOptionsChains, the domain's time_field and half of its natural key "
            "(opendata/data/domains.yaml:1056-1059). The traceback ends at the _exact_date raise "
            "in opendata/data/providers/tmx/models/fetchers_w34_envelope.py:392, inside "
            "_eod_rows, before any contract row is built: the chain mapper must refuse the file "
            "rather than invent a date out of a slashed cell or re-key the contract onto the live "
            "page's copy of the same cell -- the page does publish 2026-10-16(Weekly) for this "
            "contract, so a silent fall-back would still 'work' while making the file's own key "
            "unauditable. Nothing else in either leg is touched: the quotes table, the other four "
            "chain rows and the zero-strike class row are all intact"
        ),
    ),
    "IndexSnapshots": _entry(
        builder=_index_snapshots_document,
        query={"region": "ca"},
        tamper={
            "op": "set",
            "path": ["responses", 1, "body", "data", "getQuoteForSymbols", 0, "price"],
            "value": "24017.41",
        },
        tamper_reason=(
            "price is the cell the whole row is gated on -- tmx models/index_snapshots.py:278 "
            "publishes an index snapshot only when the quote leg answered one -- and it is "
            "declared as a float on TmxIndexSnapshots whose numeric cells run the package's own "
            "_number validator. Quoting it leaves the other seven selected cells of that reply "
            "(prevClose, priceChange, weeks52high/low and the rest of the panel) and every cell of "
            "the index document intact, and the second row is untouched, so the refusal is about "
            "one cell. The traceback ends at the _json_number raise in "
            "opendata/data/providers/tmx/models/fetchers_w34_envelope.py:347, reached through "
            "_number_cell at :743 from _index_rows at :712: the panel mapper must refuse rather "
            "than coerce text into the index level, because a snapshot whose price was laundered "
            "from a string cannot be told from one the source never quoted -- and dropping the row "
            "on a present-but-unreadable price would hide the defect behind the legitimate HGL "
            "drop this very fixture proves"
        ),
    ),
}

#: The declared columns each row leaves unsourced, stated as data so the entry text is checkable.
#: OptionsChains: the 30 columns neither the quotes table's nine cells (tmx
#: ``utils/helpers.py:517-526``) nor the EOD file's 21-cell rename table (``:669-690``) publishes --
#: ``underlying_price`` (upstream takes it from a third GraphQL quote call,
#: ``models/options_chains.py:95-100``, and on the dated path from the chain's own last-trade cell,
#: ``utils/helpers.py:701``, which is not an underlying price), ``change_percent``,
#: ``contract_size``, ``theoretical_price``, ``tick``, ``mark``, ``bid_size``, ``bid_time``,
#: ``bid_exchange``, ``ask_size``, ``ask_time``, ``ask_exchange``, ``open_bid``, ``open_ask``,
#: ``bid_high``, ``ask_high``, ``bid_low``, ``ask_low``, ``close``, ``close_size``, ``close_time``,
#: ``close_bid_time``, ``close_ask_time``, ``last_trade_size``, ``last_trade_time``, ``delta``,
#: ``gamma``, ``theta``, ``vega``, ``rho`` (no greek or size/time cell is read by either loader).
#: IndexSnapshots: the six named in ``_INDEX_UNDECLARED_SOURCE`` -- ``open``, ``high``, ``low``,
#: ``close``, ``volume`` (outside the eight-cell ``getQuoteForSymbols`` selection at tmx
#: ``utils/gql.py:472-483`` and outside the alias table at ``models/index_snapshots.py:31-49``) and
#: ``number_of_constituents`` (declared at ``:75-78`` with no alias entry for any directory cell).
UNSOURCED_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "OptionsChains": (
        "underlying_price",
        "change_percent",
        "contract_size",
        "theoretical_price",
        "tick",
        "mark",
        "bid_size",
        "bid_time",
        "bid_exchange",
        "ask_size",
        "ask_time",
        "ask_exchange",
        "open_bid",
        "open_ask",
        "bid_high",
        "ask_high",
        "bid_low",
        "ask_low",
        "close",
        "close_size",
        "close_time",
        "close_bid_time",
        "close_ask_time",
        "last_trade_size",
        "last_trade_time",
        "delta",
        "gamma",
        "theta",
        "vega",
        "rho",
    ),
    "IndexSnapshots": (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "number_of_constituents",
    ),
}

MODELS: Final[dict[str, dict[str, Any]]] = _MODELS

__all__ = ["MODELS", "UNSOURCED_FIELDS"]
