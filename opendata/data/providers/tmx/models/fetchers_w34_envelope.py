"""Wave-34 TMX envelope-family rows: ``OptionsChains`` and ``IndexSnapshots``.

This module is the contract **and** the transport for two TMX rows.  It keeps the declarations the
scaffold landed -- ``canonical_model``, ``domain``, ``capability``, ``query_type`` and a
``row_model`` bound through :func:`_row_model`, which refuses a drift at import time -- and fills
the bodies the scaffold deliberately left refusing.

Why these are envelope rows (the wave-33 veto, retracted on measurement).  The shipped fake
(``scripts/quality/iter2_claims_probe_runner.py``, ``_FakeSharedClient._respond``) builds a
``requests.Response`` and fills it with ``response._content = body`` plus
``Content-Type`` from the spec, so an HTML table and a CSV file are ordinary recorded envelopes:
bytes and a media type.  And a fixture spec is a **list** of responses matched by ``url_contains``
+ ``method``, so a row may declare several legs across several hosts -- precedented inside this
provider by ``fetchers_w33_etf_loaders.ALLOWED_DIRECTORY_HOSTS``.

The two request/parse families, with the upstream shape each is derived from (the OpenBB checkout
is an output-contract reference only; no code was copied):

``OptionsChains`` -- two GETs, one host (``www.m-x.ca``)
    * ``…/en/trading/data/quotes?symbol=`` -- the listed chain as an HTML table (tmx
      ``utils/helpers.py:515`` for the URL, ``:530`` ``read_html(...)[0]`` for the parse, ``:533``
      and ``:538`` for the ``Expiry date``/``Strike`` columns, ``:537``/``:544`` for the ``Calls``
      and ``Puts`` blocks, ``:517-526`` for the nine columns the block yields and ``:553-559`` for
      the casts that prove those cells travel as plain numeric text, and ``:569-581`` for the
      contract-symbol derivation).
    * ``…/en/trading/data/historical?symbol=…&dnld=1#quotes`` -- the EOD chain file, comma-separated
      (tmx ``utils/helpers.py:618`` and ``:631`` for the URL, ``:647`` ``read_csv``, ``:653-659``
      for the six raw cells it drops, ``:655`` ``Symbol`` -> ``contract_symbol``, ``:657``
      ``Call/Put`` 0/1 -> ``call``/``put``, ``:669-690`` for the 21-cell positional rename table
      (snake-cased at ``:712``), ``:695-699`` for ``dte`` and the volatility scale and ``:703`` for
      the zero-strike class row).
    A contract row is one listed contract: the quotes block publishes the live cells and the EOD
    file publishes the last session's statistics for the same ``(expiration, strike, option_type)``
    key -- the very index both of upstream's own loaders set
    (``utils/helpers.py:541``, ``:548``, ``:701``).

``IndexSnapshots`` -- one GET and one POST, two hosts
    * GET ``tmxinfoservices.com/files/indices/sptsx-indices.json`` -- the regional index document
      (tmx ``models/index_snapshots.py:166``; the per-index cells read at ``:174-196``, the
      ``(USD)`` currency rule at ``:186-190``, the symbol collection at ``:198-201``; the alias
      table that names every source cell at ``:31-49``).
    * POST ``app-money.tmx.com/graphql`` -- ``getQuoteForSymbols`` for exactly those symbols
      (tmx ``models/index_snapshots.py:203-219`` for the ask and ``:232-241`` for the merge by
      ``symbol``; the eight selected cells at ``utils/gql.py:472-483``).  ``client.py``'s operation
      table does not declare ``getQuoteForSymbols``, and that file is not this wave's to edit, so
      the document is declared **here** and posted through the same governed client, on the same
      precedent as ``fetchers_w33_etf_loaders.py:1-20``.

What this transport refuses instead of inventing.  ``TmxOptionsChains`` declares 55 columns and
``TmxIndexSnapshots`` declares 26 (spec sheet ``/tmp/w34_tmx_specs.json``, sha256
``dcb533c0de26bfdd5c3ee39cdb9560a0d4e953dcf91968376a1d2cd347be6d0a`` and
``6bff37ba7cda7c7336cf83d545495a1098348f948369214c31209c53253c869b``); neither envelope
family publishes a cell for the rest, and no leg was fabricated to fill them.  The
witness shard names every unsourced declared column with the line that shows it.  Where the two
legs *do* publish a cell, its type is enforced rather than coerced: text where the contract reads
a number, a date that is not an exact ISO calendar date, and a chain longer than the caller's
``limit`` all refuse with the package's own ``TmxProviderError``.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from html.parser import HTMLParser
from io import StringIO
from typing import TYPE_CHECKING, ClassVar, Final, Literal, TypeVar, cast

from pydantic import Field, StrictInt, StrictStr

from opendata.data.capability import Capability
from opendata.data.domains import contract_model
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.base import ContractModel
from opendata.data.models.tmx import TmxIndexSnapshots, TmxOptionsChains
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.tmx.models.client import (
    DEFAULT_TIMEOUT_SECONDS,
    GRAPHQL_URL,
    MAX_RESPONSE_BYTES,
    MAX_RESPONSE_ROWS,
    TmxProviderError,
    _http_code,
)
from opendata.data.providers.tmx.models.fetchers import _SymbolQuery, _TmxQuery
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

_QueryT = TypeVar("_QueryT", bound=QueryParams)
_ContractT = TypeVar("_ContractT", bound=ContractModel)
_Date = date

#: The host set these two rows name.  ``app-money.tmx.com`` is the host this provider already
#: grants (``iter2_witnesses/tmx.py:117``); ``www.m-x.ca`` and ``tmxinfoservices.com`` are the
#: envelope hosts upstream reads.  The parent witness module's ``allowed_hosts`` still lists only
#: the first, and that file is scaffold-owned, so the gap is reported rather than edited here --
#: exactly as ``fetchers_w33_etf_loaders.ALLOWED_DIRECTORY_HOSTS`` reported its three.
REQUIRED_HOSTS: Final[tuple[str, ...]] = (
    "app-money.tmx.com",
    "www.m-x.ca",
    "tmxinfoservices.com",
)

#: The chain file and the quotes page, one host, two GETs (tmx ``utils/helpers.py:515``, ``:618``).
_MX_QUOTES_URL: Final = "https://www.m-x.ca/en/trading/data/quotes?symbol="
_MX_EOD_URL: Final = "https://www.m-x.ca/en/trading/data/historical?symbol="

#: The regional index document (tmx ``models/index_snapshots.py:166``).
_INDEX_DIRECTORY_URL: Final = "https://tmxinfoservices.com/files/indices/sptsx-indices.json"

#: ``getQuoteForSymbols`` selects exactly eight cells (tmx ``utils/gql.py:472-483``).  The root
#: field is the operation's own name: unlike ``getNewsAndEvents`` it carries no alias
#: (``models/client.py:40-171`` holds the aliased ones).
_QUOTE_PANEL_ROOT: Final = "getQuoteForSymbols"
_QUOTE_PANEL_QUERY: Final = """query getQuoteForSymbols($symbols: [String]) {
  getQuoteForSymbols(symbols: $symbols) {
    symbol longname price prevClose priceChange percentChange weeks52high weeks52low
  }
}"""

#: The nine columns the quotes page's Calls/Puts blocks yield: the block's own cells in the order
#: the rename table publishes them (tmx ``utils/helpers.py:517-526``; the block contributes six and
#: the two key columns are concatenated ahead of it at ``:537``/``:544``).
_QUOTE_BLOCK_CELLS: Final = ("bid", "ask", "last_trade_price", "change", "open_interest", "volume")
_EXPIRY_HEADER: Final = "Expiry date"  # tmx ``utils/helpers.py:533``
_STRIKE_HEADER: Final = "Strike"  # tmx ``utils/helpers.py:538``

#: The nineteen raw cells the EOD file publishes that upstream keeps, in its own positional order,
#: snake-cased the way ``utils/helpers.py:712`` snake-cases the rename table at ``:669-690``.  The
#: twentieth and twenty-first published cells are derived from ``Symbol`` (``:655``) and
#: ``Call/Put`` (``:657``).
_EOD_RAW_CELLS: Final = (
    "eod_date",
    "strike",
    "expiration",
    "close_bid",
    "close_ask",
    "close_bid_size",
    "close_ask_size",
    "last_trade_price",
    "volume",
    "prev_close",
    "change",
    "open",
    "high",
    "low",
    "total_value",
    "transactions",
    "settlement_price",
    "open_interest",
    "implied_volatility",
)
_EOD_SYMBOL_HEADER: Final = "Symbol"  # tmx ``utils/helpers.py:655``
_EOD_PUT_HEADER: Final = "Call/Put"  # tmx ``utils/helpers.py:657``
_EOD_CALL: Final = "0"
_EOD_PUT: Final = "1"

#: The index document's cells that are not already contract column names (tmx
#: ``models/index_snapshots.py:31-48`` -- the provider's own alias table, read verbatim).
_INDEX_ALIASES: Final = {
    "prevClose": "prev_close",
    "priceChange": "change",
    "previousday": "change_percent",
    "weeks52high": "year_high",
    "weeks52low": "year_low",
    "monthtodate": "return_mtd",
    "quartertodate": "return_qtd",
    "yeartodate": "return_ytd",
    "total": "total_market_value",
    "average": "constituent_average_market_value",
    "median": "constituent_median_market_value",
    "sumtop10": "constituent_top10_market_value",
    "largest": "constituent_largest_market_value",
    "largestweight": "constituent_largest_weight",
    "smallest": "constituent_smallest_market_value",
    "smallestweight": "constituent_smallest_weight",
}

#: Percent cells published by the source at percent scale and declared by the contract as
#: normalized percent (tmx ``models/index_snapshots.py:110-123``).
_INDEX_PERCENT_CELLS: Final = (
    "change_percent",
    "return_mtd",
    "return_qtd",
    "return_ytd",
    "constituent_largest_weight",
    "constituent_smallest_weight",
)

#: Price-scale cells the source rounds to two decimals (tmx ``models/index_snapshots.py:125-137``).
_INDEX_ROUNDED_CELLS: Final = ("price", "prev_close", "change", "year_high", "year_low")

#: The declared cells neither envelope leg of this row publishes; kept named in code so the
#: witness entry can price them instead of implying them (see the shard's IndexSnapshots entry):
#: ``open``/``high``/``low``/``close``/``volume`` are outside the eight-cell quote selection
#: (tmx ``utils/gql.py:472-483``) and outside the alias table (``models/index_snapshots.py:31-49``),
#: and ``number_of_constituents`` matches no directory cell the alias table renames.
_INDEX_UNDECLARED_SOURCE: Final = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "number_of_constituents",
)

#: The three panel cells that are labels rather than numbers.
_INDEX_TEXT_CELLS: Final = ("symbol", "name", "currency")

#: The one region whose two-leg shape is evidenced in this repo (see ``_index_region_screen``).
_SUPPORTED_REGION: Final = "ca"


def _capability(domain: str, period: str, model: str, asset_class: str) -> Capability:
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period=period,
        market="CA",
        source="tmx",
        verified=False,
        notes=f"{model}: wave-34 TMX envelope family; hand-authored fixtures only, no live capture",
    )


def _row_model(domain: str, model: type[_ContractT]) -> type[_ContractT]:
    """Bind the domain's contract class, refusing a mismatch at import time."""
    resolved = contract_model(domain)
    if resolved is not model:
        raise TypeError(f"{domain}: contract resolves to {resolved.__name__}, not {model.__name__}")
    return model


# --------------------------------------------------------------------------- transport


def _timeout_for(ctx: FetchContext) -> float:
    return (
        DEFAULT_TIMEOUT_SECONDS
        if ctx.timeout is None
        else min(ctx.timeout, DEFAULT_TIMEOUT_SECONDS)
    )


def _envelope_text(url: str, *, accept: str, timeout: float) -> str:
    """One governed GET whose body is a text envelope -- HTML or CSV, not JSON.

    The status, transport and size refusals are this package's own mapping
    (``models/client.py:150-166``), so a directory page that answers 429 or truncates refuses
    exactly like an accepted GraphQL row.  Nothing is cached here: every call asks the seam, which
    is what lets a counterfact leg prove it read the document it was handed.
    """
    try:
        response = get_shared_http_client().get(
            url,
            headers={"Accept": accept},
            timeout=min(timeout, DEFAULT_TIMEOUT_SECONDS),
            source="tmx",
            use_cache=False,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise TmxProviderError(_http_code(exc.status), status=exc.status) from None
    except Exception:
        raise TmxProviderError("TMX_TRANSPORT_ERROR") from None

    status = int(response.status_code)
    if not 200 <= status < 300:
        raise TmxProviderError(_http_code(status), status=status)
    body = response.content
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise TmxProviderError("TMX_RESPONSE_TOO_LARGE", status=status)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        # Not TMX_BAD_JSON: this envelope carries no JSON at all.  An undecodable body is a reply
        # with no rows in it, which is the shape refusal this package already owns.
        raise TmxProviderError("TMX_BAD_ROWS", status=status) from None


def _quote_panel(symbols: list[str], *, timeout: float) -> list[Mapping[str, object]]:
    """Post ``getQuoteForSymbols`` for the directory's own symbols and read its reply rows.

    Same envelope discipline as ``client.graphql`` (``models/client.py:174-226``): a non-2xx
    answer, an oversized body, a document with ``errors``, or a data section without the root field
    each refuse with the package's code.  Only the operation is declared locally, because
    ``client._OPERATIONS`` does not carry it and that table is not this wave's to edit.
    """
    try:
        response = get_shared_http_client().request(
            "POST",
            GRAPHQL_URL,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=min(timeout, DEFAULT_TIMEOUT_SECONDS),
            source="tmx",
            json_body={
                "operationName": _QUOTE_PANEL_ROOT,
                "query": _QUOTE_PANEL_QUERY,
                "variables": {"symbols": symbols},
            },
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise TmxProviderError(_http_code(exc.status), status=exc.status) from None
    except Exception:
        raise TmxProviderError("TMX_TRANSPORT_ERROR") from None

    status = int(response.status_code)
    if not 200 <= status < 300:
        raise TmxProviderError(_http_code(status), status=status)
    body = response.content
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise TmxProviderError("TMX_RESPONSE_TOO_LARGE", status=status)
    try:
        document: object = json.loads(body.decode("utf-8"))
    except Exception:
        raise TmxProviderError("TMX_BAD_JSON", status=status) from None
    if not isinstance(document, dict) or "errors" in document:
        raise TmxProviderError("TMX_GRAPHQL_ERROR", status=status)
    data = document.get("data")
    if not isinstance(data, dict) or _QUOTE_PANEL_ROOT not in data:
        raise TmxProviderError("TMX_BAD_ENVELOPE", status=status)
    rows = data.get(_QUOTE_PANEL_ROOT)
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > MAX_RESPONSE_ROWS:
        raise TmxProviderError("TMX_BAD_ROWS", status=status)
    return [cast("Mapping[str, object]", row) for row in rows if isinstance(row, dict)]


# --------------------------------------------------------------------------- cell readers


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _json_number(value: object) -> float | None:
    """Read one JSON numeric cell; text is refused, never coerced (the landed TMX ruling)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TmxProviderError("TMX_BAD_ROW")
    return float(value)


def _cell_number(value: object) -> float | None:
    """Read one HTML/CSV cell as a number, the way TMX's own page publishes it.

    Upstream casts these blocks straight with ``astype(float)``/``astype("int64")``
    (tmx ``utils/helpers.py:553-559``), which establishes that the cells travel as plain numeric
    text: no thousands separator, no currency sign.  A cell that is anything else is refused here
    rather than laundered, because a coerced cell is a value the source never published.
    """
    cleaned = _text(value)
    if cleaned in {"", "N/A", "NA", "-", "--"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        raise TmxProviderError("TMX_BAD_ROW") from None


def _cell_integer(value: object) -> int | None:
    """Read a count cell: ``StrictInt`` columns cannot be built from a fractional text cell."""
    number = _cell_number(value)
    if number is None:
        return None
    if not float(number).is_integer():
        raise TmxProviderError("TMX_BAD_ROW")
    return int(number)


def _exact_date(value: object) -> _Date | None:
    """Parse a raw source date only as an exact ISO calendar date, or refuse.

    Both chain files state their dates as ``%Y-%m-%d`` (tmx ``utils/helpers.py:697-698``, and the
    quotes block's expiry cells are cast through a datetime index at ``:540``), and the contract's
    ``expiration`` is a required ``date``; nothing in either envelope publishes a zone, so a cell
    carrying a time or a slashed date is refused here rather than guessed at.
    """
    cleaned = _text(value)
    if cleaned in {"", "N/A", "NA", "-", "--"}:
        return None
    try:
        parsed = _Date.fromisoformat(cleaned)
    except ValueError:
        raise TmxProviderError("TMX_BAD_ROW") from None
    return parsed


def _normalize_symbol(symbol: str) -> str:
    """TMX's own option-ticker normalization (tmx ``utils/helpers.py:611-613``)."""
    return symbol.upper().replace("-", ".").replace(".TO", "").replace(".TSX", "")


def _strike_key(value: object) -> str:
    """One strike as a fixed three-decimal key.

    The two legs state the same strike with different precision (``120.00`` on the page, ``120`` in
    the file), and upstream keys both blocks on the strike itself
    (``utils/helpers.py:541``, ``:548``, ``:701``); three decimals is the width the contract-symbol
    derivation pads it to (``:570-575``).
    """
    number = _cell_number(value) if isinstance(value, str) else _json_number(value)
    if number is None:
        raise TmxProviderError("TMX_BAD_ROW")
    return f"{number:.3f}"


def _mx_contract_symbol(underlying: str, expiration: _Date, option_type: str, strike: float) -> str:
    """The standardized contract symbol the quotes page itself builds.

    tmx ``utils/helpers.py:569-581``: the (space-padded to six) underlying, ``%y%m%d``, ``C``/``P``
    and the strike padded to five whole and three fractional digits.  Used only for a contract the
    EOD file does not list; when the file does list it, its own ``Symbol`` cell wins (``:655``).
    """
    whole, _, fraction = f"{strike:.3f}".partition(".")
    padded = f"{whole.zfill(5)}{fraction[:3].ljust(3, '0')}"
    letter = "C" if option_type == "call" else "P"
    return f"{underlying:<6}{expiration:%y%m%d}{letter}{padded}"


def _percent(value: float | None) -> float | None:
    """Normalize a source percent the way the upstream model declares it (``:110-123``)."""
    if not value:
        return None
    return round(value / 100.0, 6)


# --------------------------------------------------------------------------- quotes table


class _FirstTable(HTMLParser):
    """The first ``<table>`` of a document, as rows of ``(tag, colspan, text)`` cells.

    ``read_html`` returns a list of frames and upstream takes ``[0]`` (tmx
    ``utils/helpers.py:530``); this is the same screen implemented on the standard library, because
    the governed client hands us bytes and a media type and nothing else.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, str]]] = []
        self._depth = 0
        self._cell: tuple[str, int] | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            return
        if not self._depth:
            return
        if tag == "tr":
            self.rows.append([])
        elif tag in {"td", "th"}:
            colspan = 1
            for name, value in attrs:
                if name == "colspan" and value is not None and value.isdigit():
                    colspan = max(1, int(value))
            self._cell = (tag, colspan)
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth = max(0, self._depth - 1)
            return
        if tag in {"td", "th"} and self._cell is not None and self.rows:
            text = " ".join(" ".join(self._buffer).split())
            self.rows[-1].append((self._cell[0], self._cell[1], text))
            self._cell = None


def _quotes_rows(html: str, underlying: str) -> list[dict[str, object]]:
    """Parse the m-x.ca quotes table into one record per listed contract and option side.

    The structure read is the one upstream reads: a two-row header whose first row groups the
    ``Calls`` and ``Puts`` blocks (``utils/helpers.py:537``, ``:544``), an ``Expiry date`` column
    (``:533``) whose cells may carry the ``(Weekly)`` marker (``:535``), a ``Strike`` column
    (``:538``), and six quoted cells per block in the order the nine-column rename table states
    (``:517-526``).  Each block becomes its own row tagged ``call``/``put``, exactly as upstream
    concatenates them (``:542``, ``:547``).
    """
    parser = _FirstTable()
    parser.feed(html)
    parser.close()
    rows = parser.rows
    header = [row for row in rows[:3] if row and all(cell[0] == "th" for cell in row)]
    if len(header) < 2:
        raise TmxProviderError("TMX_BAD_ROWS")
    group_cells, column_cells = header[0], header[1]
    groups: list[str] = []
    for _, colspan, text in group_cells:
        groups.extend([text for _ in range(colspan)])
    columns = [text for _, _, text in column_cells]
    if len(groups) != len(columns) or not columns:
        raise TmxProviderError("TMX_BAD_ROWS")
    if _EXPIRY_HEADER not in columns or _STRIKE_HEADER not in columns:
        raise TmxProviderError("TMX_BAD_ROWS")
    expiry_at = columns.index(_EXPIRY_HEADER)
    strike_at = columns.index(_STRIKE_HEADER)
    blocks: dict[str, list[int]] = {"Calls": [], "Puts": []}
    for index, group in enumerate(groups):
        if group in blocks:
            blocks[group].append(index)
    if any(len(indices) != len(_QUOTE_BLOCK_CELLS) for indices in blocks.values()):
        raise TmxProviderError("TMX_BAD_ROWS")

    records: list[dict[str, object]] = []
    for row in rows[len(header) :]:
        cells = [text for _, _, text in row]
        if len(cells) != len(columns) or not any(cells):
            raise TmxProviderError("TMX_BAD_ROWS")
        expiration = _exact_date(cells[expiry_at].replace("(Weekly)", "").strip())
        strike = _cell_number(cells[strike_at])
        if expiration is None or strike is None:
            raise TmxProviderError("TMX_BAD_ROW")
        option_type = "call"
        for group, indices in blocks.items():
            option_type = "call" if group == "Calls" else "put"
            mapped = dict(zip(_QUOTE_BLOCK_CELLS, indices, strict=True))
            record: dict[str, object] = {
                "expiration": expiration,
                "strike": strike,
                "option_type": option_type,
                "contract_symbol": _mx_contract_symbol(underlying, expiration, option_type, strike),
            }
            for cell, index in mapped.items():
                record[cell] = (
                    _cell_integer(cells[index])
                    if cell in {"open_interest", "volume"}
                    else _cell_number(cells[index])
                )
            records.append(record)
    records.sort(
        key=lambda item: (
            str(item["expiration"]),
            _strike_key(item["strike"]),
            str(item["option_type"]),
        )
    )
    return records


# --------------------------------------------------------------------------- EOD chain file


def _eod_rows(text: str) -> list[dict[str, object]]:
    """Parse one m-x.ca EOD chain file into the cells upstream's rename table publishes.

    ``read_csv`` reads the header and ``data.columns = cols`` replaces it positionally (tmx
    ``utils/helpers.py:647`` and ``:691``), so the file's own header spelling is not evidence
    for anything except the two cells the loader names: ``Symbol`` (``:655``) and ``Call/Put``
    (``:657``).  This mapper therefore reads positions 0-18 under the 19 leading names of the
    rename table and requires those two cells to be the last two columns of the file it was handed.
    """
    lines = [line for line in StringIO(text).read().splitlines() if line.strip()]
    if not lines:
        raise TmxProviderError("TMX_BAD_ROWS")
    reader = csv.reader(StringIO("\n".join(lines)))
    header = next(reader, None)
    if header is None or len(header) != len(_EOD_RAW_CELLS) + 2:
        raise TmxProviderError("TMX_BAD_ROWS")
    if _text(header[-2]) != _EOD_SYMBOL_HEADER or _text(header[-1]) != _EOD_PUT_HEADER:
        raise TmxProviderError("TMX_BAD_ROWS")
    width = len(header)
    records: list[dict[str, object]] = []
    for row in reader:
        if len(row) != width:
            raise TmxProviderError("TMX_BAD_ROWS")
        cells = dict(zip(_EOD_RAW_CELLS, row[: len(_EOD_RAW_CELLS)], strict=True))
        expiration = _exact_date(cells["expiration"])
        eod_date = _exact_date(cells["eod_date"])
        strike = _cell_number(cells["strike"])
        if strike is None:
            raise TmxProviderError("TMX_BAD_ROW")
        if strike == 0:
            # The class/root row of the file, dropped by the same test upstream applies
            # (tmx ``utils/helpers.py:703``).
            continue
        option_flag = _text(row[width - 1])
        if option_flag == _EOD_CALL:
            option_type = "call"
        elif option_flag == _EOD_PUT:
            option_type = "put"
        elif option_flag in {"call", "put"}:
            option_type = option_flag
        else:
            raise TmxProviderError("TMX_BAD_ROW")
        volume = _cell_integer(cells["volume"])
        record: dict[str, object] = {
            "contract_symbol": _text(row[width - 2]),
            "option_type": option_type,
            "expiration": expiration,
            "eod_date": eod_date,
            "strike": strike,
            "close_bid": _cell_number(cells["close_bid"]),
            "close_ask": _cell_number(cells["close_ask"]),
            "close_bid_size": _cell_integer(cells["close_bid_size"]),
            "close_ask_size": _cell_integer(cells["close_ask_size"]),
            "last_trade_price": _cell_number(cells["last_trade_price"]),
            "volume": volume,
            "prev_close": _cell_number(cells["prev_close"]),
            "change": _cell_number(cells["change"]),
            "open": _cell_number(cells["open"]),
            "high": _cell_number(cells["high"]),
            "low": _cell_number(cells["low"]),
            "transactions": _cell_integer(cells["transactions"]),
            # Both cells are in the file's own rename table (tmx ``utils/helpers.py:684`` and
            # ``:686``) and both are declared by the contract (``opendata/data/models/tmx.py:820``
            # and ``:821``), so they are published rather than dropped.
            "total_value": _cell_number(cells["total_value"]),
            "settlement_price": _cell_number(cells["settlement_price"]),
            "open_interest": _cell_integer(cells["open_interest"]),
            "implied_volatility": _volatility(cells["implied_volatility"]),
        }
        if expiration is None or eod_date is None:
            raise TmxProviderError("TMX_BAD_ROW")
        # dte is the calendar distance between the two dates the file itself states
        # (tmx ``utils/helpers.py:695-697``); the current-chain rule (``:565-567``) subtracts
        # wall-clock today, which would make this row a function of the day it was read, so it is
        # not used.
        record["dte"] = (expiration - eod_date).days
        records.append(record)
    return records


def _volatility(value: object) -> float | None:
    """TMX's EOD file states implied volatility in percent; upstream scales it (``:699``)."""
    number = _cell_number(value)
    return None if number is None else number * 0.01


# --------------------------------------------------------------------------- index panel


def _index_directory(body: str) -> Mapping[str, object]:
    """Decode the index document and require its ``indices`` section."""
    try:
        document: object = json.loads(body)
    except Exception:
        raise TmxProviderError("TMX_BAD_JSON") from None
    if not isinstance(document, dict):
        raise TmxProviderError("TMX_BAD_ROWS")
    indices = document.get("indices")
    if not isinstance(indices, dict):
        raise TmxProviderError("TMX_BAD_ROWS")
    return cast("Mapping[str, object]", indices)


def _index_records(indices: Mapping[str, object]) -> list[dict[str, object]]:
    """Assemble one record per index from the cells the document publishes.

    The shape is tmx ``models/index_snapshots.py:174-196``.
    """
    records: list[dict[str, object]] = []
    for symbol in indices:
        raw = indices[symbol]
        if not isinstance(raw, dict):
            raise TmxProviderError("TMX_BAD_ROWS")
        name = _text(raw.get("name_en"))
        if not name:
            # The currency cell is derived from this very string upstream (``:186-190``); a record
            # without a name cannot state one, so the directory is refused rather than guessed.
            raise TmxProviderError("TMX_BAD_ROW")
        record: dict[str, object] = {
            "symbol": symbol,
            "name": name,
            "currency": "USD" if "(USD)" in name else "CAD",
        }
        for section in ("performance", "quotedmarketvalue"):
            cells = raw.get(section)
            if cells is None:
                continue
            if not isinstance(cells, dict):
                raise TmxProviderError("TMX_BAD_ROWS")
            for key, value in cells.items():
                record[_INDEX_ALIASES.get(str(key), str(key))] = value
        records.append(record)
    return records


def _merge_quotes(records: list[dict[str, object]], panel: list[Mapping[str, object]]) -> None:
    """Overlay the quote leg on each index record, keyed by ``symbol`` (tmx ``:232-241``).

    Two cells of that reply are dropped before the merge for the ``ca`` panel -- ``longname`` and
    ``percentChange`` (``:226-231``) -- because the name and the day percent belong to the
    directory's own cells (``previousday`` is the source of ``change_percent``, ``:35``).
    """
    by_symbol: dict[str, Mapping[str, object]] = {}
    for row in panel:
        symbol = _text(row.get("symbol"))
        if symbol:
            by_symbol.setdefault(symbol, row)
    for record in records:
        quote = by_symbol.get(_text(record.get("symbol")))
        if quote is None:
            continue
        for key, value in quote.items():
            name = str(key)
            if name in {"symbol", "longname", "percentChange"}:
                continue
            record[_INDEX_ALIASES.get(name, name)] = value


def _index_rows(records: list[dict[str, object]]) -> list[TmxIndexSnapshots]:
    """Validate every panel record into a frozen contract row.

    A record the quote leg never answered publishes no price, and upstream drops exactly those
    rows (tmx ``models/index_snapshots.py:278-279``) -- an index level nobody quoted is not a
    snapshot.  A price that is present but not a number is refused instead: ``open``, ``high``,
    ``low``, ``close``, ``volume`` and ``number_of_constituents`` are declared nullable by
    ``TmxIndexSnapshots`` because neither leg of this row publishes them
    (``utils/gql.py:472-483`` selects none of them and ``models/index_snapshots.py:31-49`` aliases
    none of them), and the witness entry says so rather than implying it.
    """
    rows: list[TmxIndexSnapshots] = []
    for record in records:
        price = record.get("price")
        if price is None or (isinstance(price, str) and not _text(price)):
            continue
        mapped: dict[str, object] = {}
        for key, value in record.items():
            if key not in TmxIndexSnapshots.model_fields:
                # A directory cell the contract declares no column for.  Publishing it would fail
                # the strict round-trip the contract phase runs, so it is dropped here and named.
                continue
            if isinstance(value, str) and not _text(value):
                continue
            mapped[key] = value if key in _INDEX_TEXT_CELLS else _number_cell(key, value)
        rows.append(_contract_row(TmxIndexSnapshots, mapped))
    if not rows:
        raise TmxProviderError("TMX_BAD_ROWS")
    return rows


def _number_cell(key: str, value: object) -> object:
    """Read one panel cell under the scale its own contract column declares."""
    if key in _INDEX_PERCENT_CELLS:
        return _percent(_json_number(value))
    number = _json_number(value)
    if number is None:
        return None
    if key in _INDEX_ROUNDED_CELLS:
        return round(number, 2)
    if key in {"volume", "number_of_constituents"}:
        if not number.is_integer():
            raise TmxProviderError("TMX_BAD_ROW")
        return int(number)
    return number


def _contract_row(model: type[_ContractT], mapped: Mapping[str, object]) -> _ContractT:
    """Build one frozen contract row, refusing a cell the contract cannot hold."""
    try:
        return model.model_validate(dict(mapped))
    except Exception:
        raise TmxProviderError("TMX_BAD_ROW") from None


def _region_screen(region: str) -> None:
    """Refuse a region whose source shape this repo cannot evidence.

    Upstream's own query type is ``Literal["ca", "us"]`` (tmx ``models/index_snapshots.py:19``) and
    only the ``ca`` branch is the two-envelope shape this row reads; the ``us`` branch is keyed by
    the ``NASDAQ_GIDS`` constant (``:152``, ``:244-246`` and ``utils/helpers.py:81``) whose member
    list this repo holds no evidenced copy of.  Serving the Canadian panel under a ``us`` or ``eu``
    label would be a wrong answer, so the request is refused with the code this package already
    uses for a filter whose source column cannot be evidenced.
    """
    if region != _SUPPORTED_REGION:
        raise TmxProviderError("TMX_BAD_FILTER")


# --------------------------------------------------------------------------- fetchers


class OptionsChainsQuery(_SymbolQuery):
    """One underlying's option chain, optionally pinned to one expiration."""

    expiration: StrictStr | None = None
    limit: StrictInt = Field(default=1_000, ge=1, le=10_000)


class IndexSnapshotsQuery(_TmxQuery):
    """The TMX index panel for one declared region."""

    region: Literal["us", "eu", "ca"] = "ca"
    limit: StrictInt = Field(default=100, ge=1, le=1_000)


class _EnvelopeFetcher(Fetcher[_QueryT, object]):
    """Shared base: ``bounded_thread`` like every other TMX fetcher, one governed client."""

    async_mode: ClassVar[str] = "bounded_thread"

    domain: ClassVar[str]
    query_type: ClassVar[type[_QueryT]]

    def transform_query(self, **kwargs: object) -> _QueryT:
        """Validate the request against this row's declared query type."""
        return self.query_type.model_validate(kwargs)


class OptionsChainsFetcher(_EnvelopeFetcher[OptionsChainsQuery]):
    """One underlying's listed option chain from the m-x.ca quotes table and EOD file."""

    canonical_model: ClassVar[str | None] = "OptionsChains"
    domain: ClassVar[str] = "tmx_options_chains"
    query_type: ClassVar[type[OptionsChainsQuery]] = OptionsChainsQuery
    row_model: ClassVar[type[TmxOptionsChains]] = _row_model("tmx_options_chains", TmxOptionsChains)
    capability: ClassVar[Capability] = _capability(
        "tmx_options_chains", "snapshot", "OptionsChains", "option"
    )

    def extract_data(self, params: OptionsChainsQuery, ctx: FetchContext) -> object:
        """Read the listed chain and its EOD file: two GETs, one host, both text envelopes."""
        underlying = _normalize_symbol(params.symbol)
        timeout = _timeout_for(ctx)
        return {
            "underlying": underlying,
            "quotes": _envelope_text(
                f"{_MX_QUOTES_URL}{underlying}",
                accept="text/html,application/xhtml+xml",
                timeout=timeout,
            ),
            "eod": _envelope_text(
                f"{_MX_EOD_URL}{underlying}&dnld=1#quotes",
                accept="text/csv,application/octet-stream",
                timeout=timeout,
            ),
        }

    def transform_data(self, raw: object, params: OptionsChainsQuery) -> FetchResult:
        """Join the two envelopes on the contract key and validate every row."""
        if not isinstance(raw, dict):
            raise TmxProviderError("TMX_BAD_ROWS")
        payload = cast("Mapping[str, object]", raw)
        quotes = _text(payload.get("quotes"))
        eod = _text(payload.get("eod"))
        underlying = _text(payload.get("underlying"))
        if not underlying or not quotes or not eod:
            raise TmxProviderError("TMX_BAD_ROWS")
        listed = _quotes_rows(quotes, underlying)
        sessions = {
            (_text(row["expiration"]), _strike_key(row["strike"]), _text(row["option_type"])): row
            for row in _eod_rows(eod)
        }
        wanted = ""
        if params.expiration is not None:
            pinned = _exact_date(params.expiration)
            if pinned is None:
                raise TmxProviderError("TMX_BAD_ROW")
            wanted = pinned.isoformat()
        rows: list[TmxOptionsChains] = []
        for record in listed:
            key = (
                _text(record["expiration"]),
                _strike_key(record["strike"]),
                _text(record["option_type"]),
            )
            if wanted and key[0] != wanted:
                continue
            cells = dict(record)
            session = sessions.get(key)
            if session is not None:
                # The EOD file is the base and the quotes page's six live cells win over it, so a
                # row never mixes a cell between sessions: the live block is what a chain snapshot
                # is asked for, and the file contributes only the cells the page does not publish
                # (its own rename table at ``utils/helpers.py:669-690``) plus the ``Symbol`` cell
                # that is this contract's own key (``:655``).
                merged = dict(session)
                merged.update({cell: record[cell] for cell in _QUOTE_BLOCK_CELLS})
                merged["expiration"] = record["expiration"]
                merged["strike"] = record["strike"]
                merged["option_type"] = record["option_type"]
                symbol_cell = _text(session["contract_symbol"])
                merged["contract_symbol"] = symbol_cell or record["contract_symbol"]
                cells = merged
            cells["underlying_symbol"] = f"{underlying}:CA"
            # ``underlying_price`` is published by neither envelope leg: upstream takes it from a
            # third GraphQL quote call (models/options_chains.py:95-100) and, on the dated path,
            # from the chain's own last-trade cell (utils/helpers.py:701), which is not an
            # underlying price.  The declared cell is left null instead.
            rows.append(_contract_row(TmxOptionsChains, cells))
        if len(rows) > params.limit:
            # The landed ruling in this package: a reply longer than the caller asked for is
            # refused, not truncated (see models/fetchers.py and the wave-33 filings guard).
            raise TmxProviderError("TMX_BAD_ROWS")
        if not rows:
            raise TmxProviderError("TMX_BAD_ROWS")
        return rows


class IndexSnapshotsFetcher(_EnvelopeFetcher[IndexSnapshotsQuery]):
    """The regional index snapshot panel published across TMX's quote and directory legs."""

    canonical_model: ClassVar[str | None] = "IndexSnapshots"
    domain: ClassVar[str] = "tmx_index_snapshots"
    query_type: ClassVar[type[IndexSnapshotsQuery]] = IndexSnapshotsQuery
    row_model: ClassVar[type[TmxIndexSnapshots]] = _row_model(
        "tmx_index_snapshots", TmxIndexSnapshots
    )
    capability: ClassVar[Capability] = _capability(
        "tmx_index_snapshots", "snapshot", "IndexSnapshots", "index"
    )

    def extract_data(self, params: IndexSnapshotsQuery, ctx: FetchContext) -> object:
        """Read the index document, then quote its own symbols through the GraphQL leg."""
        _region_screen(params.region)
        timeout = _timeout_for(ctx)
        body = _envelope_text(_INDEX_DIRECTORY_URL, accept="application/json", timeout=timeout)
        records = _index_records(_index_directory(body))
        symbols = [_text(record["symbol"]) for record in records]
        panel = _quote_panel(symbols, timeout=timeout)
        return {"records": records, "panel": panel}

    def transform_data(self, raw: object, params: IndexSnapshotsQuery) -> FetchResult:
        """Merge the two legs by symbol and validate the panel into contract rows."""
        if not isinstance(raw, dict):
            raise TmxProviderError("TMX_BAD_ROWS")
        payload = cast("Mapping[str, object]", raw)
        records_object = payload.get("records")
        panel_object = payload.get("panel")
        if not isinstance(records_object, list) or not isinstance(panel_object, list):
            raise TmxProviderError("TMX_BAD_ROWS")
        records = [cast("dict[str, object]", item) for item in records_object]
        panel = [cast("Mapping[str, object]", item) for item in panel_object]
        _merge_quotes(records, panel)
        rows = _index_rows(records)
        if len(rows) > params.limit:
            raise TmxProviderError("TMX_BAD_ROWS")
        return rows


__all__ = [
    "REQUIRED_HOSTS",
    "IndexSnapshotsFetcher",
    "IndexSnapshotsQuery",
    "OptionsChainsFetcher",
    "OptionsChainsQuery",
]
