"""Offline witness configuration for the ``tmx`` provider.

TMX owns its request shape: every fetcher goes through
:func:`opendata.data.providers.tmx.models.client.graphql`, which posts one GraphQL envelope to
``https://app-money.tmx.com/graphql`` through ``get_shared_http_client().request("POST", ...)`` (the
call at ``models/client.py:132``). The engine's ``FixedResponseTransport`` is not the seam for this
provider -- no leg reads ``http_transport`` -- so ``client_seam`` names
:mod:`opendata.data.providers.tmx.models.client`: ``client.py:8`` binds the factory name at import
time, so the shipped call site resolves it in *its own* module globals, and a witness that patched
the owning ``opendata.data.http_client`` module instead would leave the real client in place while
still reporting a clean run. Every entry declares ``method: "POST"``, so the fake's widened surface
cannot turn a POST-shaped fixture into a free GET.

Serving this seam is cheaper than it was for deribit, and the reason is worth recording because it
is the difference between the two providers: a GraphQL reply carries no request id, so nothing here
needs ``echo_request_id``. ``graphql()`` reads only ``data`` (and refuses a document that carries
``errors``), so one ``{"data": {root: ...}}`` body answers the leg faithfully. Each of the seven
entries therefore declares exactly one response -- and ``_carrier_sends`` does *not* then hand that
carrier back as the budget: it returns ``max(_DEFAULT_SENDS, answerable)`` with the shipped floor at
three, so a one-response carrier is granted three sends and the carrier only sets the budget once it
can answer more than the floor. The step-2 prevalidation probe
(``docs/evidence/C131/wave31-step2-tmx-dividends-constituents-20261018/probes/``) prints both
numbers per carrier rather than asserting their relation: 1 against 3 for every shipped entry here,
4 against 4 for the four-page dividend sequence that probe builds to show the pagination loop is
live. The root field is what the operation alias publishes, not what the operation is called:
``getCompanyFilings`` returns under ``filings`` and ``getIndexConstituents`` under
``constituents`` (``client.py:78`` and ``:99``), so a fixture that keyed the body by the operation
name would refuse at ``TMX_BAD_ENVELOPE`` before any mapper ran.
EquityQuote and EquityInfo are two witnesses over the *same* operation -- both read
``getQuoteBySymbol`` -- and they are kept as separate fixtures because each row class reads a
different column set of that one reply.

The bodies are **hand-authored fixture bodies, not live captures**: the four snapshot bodies were
authored on 2026-10-12 and the history, dividend and constituent bodies on 2026-10-18, and no claim
is made that app-money.tmx.com published these values. They carry the field names TMX's public UI
query selections actually ask for (``client.py:40-117``) and the shapes the shipped mappers read,
and they exist to prove the mapping and the gates.

Every refusal is the provider's own ``TmxProviderError`` carrying a ``TMX_*`` code. That class
carries ``code`` and ``status`` but no field attribution, so ``rejected``/``missing``/``invalid``
stay empty in the observed face and this module declares no ``expected_*`` detail at all: the
counterfact gate is the code plus the cell the tamper broke, and each ``tamper_reason`` names that
cell from the contract rather than from a captured run.

Scope of this module, stated as narrowly as the measurement allows. It witnesses seven rows. Four of
them are the rows wave 30 step 0 priced as ``declare_only`` -- EquityQuote, EquityInfo,
CompanyFilings, PriceTargetConsensus -- each of which had already been building its own contract
class inside ``transform_data`` and only lacked the ``row_model`` declaration the service leg
demands. The fifth, EquityHistorical, is a different kind of landing: it needed a guard as well as a
declaration, and the guard applies a ruling this package already makes rather than a new one. Both
the filings and the history fetchers publish a ``limit`` to the source and read the reply through
``list_rows``; the filings fetcher refused a reply longer than the limit it asked for and the
history fetcher returned it, so its caller could not tell an over-delivering source from a longer
window. Wave 30 ruled that adding that guard "is a product ruling about what the mapper refuses, not
wave work", and that is still true -- what changed is that the ruling has already been written once
inside this provider, so landing the row means applying it a second time, not inventing it.

The last two, HistoricalDividends and IndexConstituents, retract a prediction this module made about
them. Wave 30's action-map probe
(``docs/evidence/C130/plane1-action-map-rebase-20261018/probes/retarget_batch_prediction.py``) read
them as not landable because the shipped residue ``_verdict`` scores HistoricalDividends
``QUERY_ONLY`` (its only ``raise`` statements are in ``extract_data``) and IndexConstituents
``NO_CODE_SITE`` (its ``transform_data`` carries no ``raise`` at all), and that reading is *still*
what the instrument prints -- the step-2 probe re-runs it and shows it side by side with the runtime
face. What the prediction got wrong is that ``_verdict`` counts raises lexically inside the mapper
methods, while TMX's row-level refusal lives in the shared ``_row`` helper at
``models/fetchers.py:190``, which every mapper in this provider calls. Both rows therefore already
refused a broken contract cell with their own typed, code-bearing error before anything was written
here: the only missing thing was the declaration the runner reads at
``iter2_claims_probe_runner.py:424``, and deleting that one ClassVar in memory is enough to make the
shipped direct leg refuse with ``AttributeError`` at that line. No guard was invented for either
row, and neither row's entry tampers a page-level or transport cell -- those refuse the whole leg
rather than one contract cell, and the exclusions below say which of them a fixture can reach at
all.

What else this module does not cover, and why: the envelope and decode refusals
``TMX_BAD_JSON``, ``TMX_GRAPHQL_ERROR``, ``TMX_BAD_ENVELOPE`` (each entry carries exactly one tamper
and the seven spend theirs on row-level refusals -- four at the ``_row`` helper, one at each of the
two count guards, one at the analysts' nested-object guard -- while an envelope refusal rejects the
whole leg rather than one contract cell, so no entry can witness one); the HTTP-status refusals
``TMX_HTTP_ERROR``, ``TMX_RATE_LIMITED``, ``TMX_ACCESS_ERROR``, ``TMX_UPSTREAM_ERROR`` and
``TMX_RESPONSE_TOO_LARGE`` (a seam that already answers 200 with a small body cannot withhold the
answer or publish an oversized one); ``TMX_OPERATION_NOT_ALLOWED`` (refused before the seam is
touched, so a fixture cannot observe it); ``TMX_BAD_ROWS`` from ``list_rows`` (it needs more than
``MAX_RESPONSE_ROWS`` = 10 000 source rows, which no fixture that answers every call identically can
carry -- the ``TMX_BAD_ROWS`` tampered here is a *different* site on each of the two rows, the
fetcher's own over-limit guard); ``TmxProviderError("TMX_BAD_ROW")`` from ``_row`` on a non-dict
source (reachable, but it refuses the row before any column is read, so it would witness the same
cell twice); and EquitySearch's projection (its row class is the contract but its mapper publishes
two of the four source columns it validates, which is a mapping ruling, not a witness).

``TMX_TRANSPORT_ERROR`` was listed above with the refusals a seam cannot produce, and that was
wrong. It *is* reachable: ask the seam for a second dividend page the carrier does not carry and the
harness's own ``WitnessFailure`` is swallowed by ``graphql``'s bare ``except Exception`` and
re-raised as ``TmxProviderError("TMX_TRANSPORT_ERROR")`` at ``client.py:153``, ``from None``. The
step-2 probe measures that with a one-page carrier whose single page reports ``hasNextPage: true``,
because that -- not a true flag on the shipped terminal carrier -- is the shape that keeps the
pagination loop asking. It still cannot be an entry's tamper, for a reason the measurement also
shows: the code names no contract cell and its cause is discarded at the raise, so a witness of it
would certify that the harness withheld an answer rather than that the mapper refuses a value TMX
published. The diagnosability cost of that ``from None`` is a product finding, not wave work.
"""

from __future__ import annotations

from typing import Any, Final

#: The one attribute the transport resolves per request; ``models/client.py`` binds it at import
#: time, so patching it on that module is what the shipped call site reads on the next request.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.tmx.models.client"],
    "attr": "get_shared_http_client",
}

#: The single host every TMX leg is allowed to name, and the needle the seam matches bodies with.
_GQL_HOST: Final = "app-money.tmx.com"
_GQL_ROUTE: Final = f"{_GQL_HOST}/graphql"

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/tmx/models/client.py",
    "opendata/data/providers/tmx/provider.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/tmx.py",
    "opendata/data/domains.yaml",
]

#: The symbol every row this module queries, and the two days the history window is built from. No
#: TMX column in the four snapshot contracts carries a date, so there the values are documentation
#: of the fixture date; ``EquityHistorical`` is the exception -- its ``date`` is a required contract
#: column and its window is what the fetcher publishes to the source as ``limit``.
_SYMBOL: Final = "SHOP"
_HISTORY_DAY: Final = "2026-10-09"
_PRIOR_DAY: Final = "2026-10-08"

#: The index the constituent fixture queries. Any TMX-shaped identifier would do: the bodies here
#: are hand-authored (see the module docstring), so this value asserts nothing about which index
#: TMX publishes under it, only that it passes ``_validate_symbol`` and reaches the mapper.
_INDEX_SYMBOL: Final = "CUX"


def _document(root: str, payload: object) -> dict[str, Any]:
    """One GraphQL reply for one operation, served only to the verb and route TMX actually uses.

    ``root`` is the *alias* the operation selects under, which for two of TMX's operations differs
    from its own name; see the module docstring for why that distinction is the difference between
    a fixture that maps and one that refuses at ``TMX_BAD_ENVELOPE``.
    """
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {"data": {root: payload}},
            }
        ]
    }


def _quote_payload() -> dict[str, object]:
    """Hand-author one ``getQuoteBySymbol`` reply, with the price fields as JSON numbers."""
    return {
        "symbol": _SYMBOL,
        "name": "Shopify Inc.",
        "price": 121.44,
        "priceChange": -1.32,
        "percentChange": -1.0753,
        "exchangeName": "Toronto Stock Exchange",
        "exchangeCode": "TOR",
        "sector": "Technology",
        "industry": "Retail (Internet)",
        "volume": 8_129_433,
        "openPrice": 122.05,
        "dayHigh": 123.18,
        "dayLow": 120.62,
        "MarketCap": 158_632_000_000.0,
        "MarketCapAllClasses": 158_632_000_000.0,
        "peRatio": 63.25,
        "prevClose": 122.76,
        "dividendFrequency": "ANNUAL",
        "dividendYield": None,
        "dividendAmount": None,
        "dividendCurrency": "CAD",
        "beta": 1.4129,
        "eps": 1.92,
        "exDividendDate": None,
        "longDescription": (
            "Shopify Inc. engages in powering commerce for entrepreneurs and enterprises."
        ),
        "website": "https://www.shopify.com",
        "email": "investor@shopify.com",
        "phoneNumber": "+1 613 111 1111",
        "fullAddress": "808 Queen Street, Ottawa, ON K1P 6S9, Canada",
        "employees": 12_800,
        "shareOutStanding": 400_000_000,
        "totalDebtToEquity": 23.94,
        "totalSharesOutStanding": 1_302_000_000,
        "sharesESCROW": 0,
        "issueType": "Common Ordinary",
        "vwap": 121.903,
        "dividendPayDate": None,
        "weeks52high": 139.42,
        "weeks52low": 60.61,
        "currency": "CAD",
    }


def _equity_quote_document() -> dict[str, Any]:
    """The quote reply, projected into :class:`opendata.data.models.tmx.TmxEquityQuote`."""
    return _document("getQuoteBySymbol", _quote_payload())


def _equity_info_document() -> dict[str, Any]:
    """The same operation witnessed for the profile contract, which reads a different column set."""
    return _document("getQuoteBySymbol", _quote_payload())


def _filing(index: int) -> dict[str, object]:
    """One ``filings`` record in the shape the operation's selection publishes."""
    return {
        "size": "1.4M",
        "filingDate": f"2026-0{index + 1}-15",
        "description": f"Interim report {index + 1}",
        "urlToPdf": f"https://www.sedarplus.example/fixture/filing-{index + 1}.pdf",
    }


def _company_filings_document() -> dict[str, Any]:
    """One filing row under a ``limit`` of one, the only pair that makes the over-limit gate bite.

    The fetcher asks the source for ``limit`` records and refuses when more come back; a fixture
    that already published more rows than its own query asked for would never reach that guard on
    the untampered control, so the control carries exactly one and the tamper adds a second.
    """
    return _document("filings", [_filing(0)])


def _history_row(day: str) -> dict[str, object]:
    """One ``getCompanyPriceHistory`` record, carrying exactly the eleven selected columns."""
    return {
        "datetime": day,
        "openPrice": 121.05,
        "closePrice": 121.44,
        "high": 123.18,
        "low": 120.62,
        "volume": 8_129_433,
        "tradeValue": 990_547_102.5,
        "numberOfTrade": 18_504,
        "change": -1.32,
        "changePercent": -0.010753,
        "vwap": 121.903,
    }


def _equity_historical_document() -> dict[str, Any]:
    """One day of history under a one-day window, so the reply count equals the published ``limit``.

    The same pair CompanyFilings is witnessed with: a single-day window makes ``limit`` one, the
    control therefore carries exactly one row, and the guard only has room to bite on the tamper.
    """
    return _document("getCompanyPriceHistory", [_history_row(_HISTORY_DAY)])


def _dividend_rows() -> list[dict[str, object]]:
    """The two ``dividends`` records one page publishes, in the selection's own column names."""
    return [
        {
            "exDate": "2026-06-14",
            "amount": 0.08,
            "currency": "CAD",
            "payableDate": "2026-06-28",
            "declarationDate": "2026-05-20",
            "recordDate": "2026-06-15",
        },
        {
            "exDate": "2026-09-14",
            "amount": 0.10,
            "currency": "CAD",
            "payableDate": "2026-09-30",
            "declarationDate": "2026-08-19",
            "recordDate": "2026-09-15",
        },
    ]


def _historical_dividends_document() -> dict[str, Any]:
    """One *terminal* dividend page, the only shape the pagination loop stops on.

    ``hasNextPage: false`` ends ``extract_data`` after one seam call. It is *not* the shape that
    shows the loop works: a carrier that stops after one send is equally consistent with a loop
    that never runs, so the step-2 probe serves a four-page ``sequence`` carrier beside this one
    and reads eight rows over four calls (``CONTROL_DIVIDEND_PAGINATION_LIVE``). Note that a
    non-terminal flag on this one-response carrier does not reach ``TMX_PAGE_LIMIT`` either -- the
    loop's second ask is answered by the same body, whose ``pageNumber`` disagrees with the page
    asked for, so it refuses at the second send (``TMX_BAD_PAGE``, ``models/fetchers.py:485``)
    while the cap would need a hundred non-terminal pages no carrier here can supply.
    Both page-level guards refuse the whole leg rather than one contract cell, so no entry tampers
    them; the entry tampers the record-level ``exDate``, which the mapper compares against the
    requested bounds (``models/fetchers.py:515`` and ``:517``) and refuses through ``_row``.
    """
    return _document(
        "dividendHistory",
        {"pageNumber": 1, "hasNextPage": False, "dividends": _dividend_rows()},
    )


def _constituent(symbol: str, name: str, weight: float) -> dict[str, object]:
    """One ``constituents`` record, keyed by the columns the index selection asks for.

    Both members carry the same ``quotedMarketValue`` literal this module's quote body already
    uses: it is one of the four optional columns, the mapper only requires it to pass ``_number``,
    and nothing compares it across rows. ``symbol`` is the column the contract requires and the one
    the entry tampers.
    """
    return {
        "symbol": symbol,
        "longName": name,
        "exchange": "TOR",
        "quotedMarketValue": 158_632_000_000.0,
        "weight": weight,
    }


def _index_constituents_document() -> dict[str, Any]:
    """Two index members, projected into two ``TmxIndexConstituent`` contract rows."""
    return _document(
        "constituents",
        [
            _constituent("SHOP", "Shopify Inc.", 24.7),
            _constituent("LUN.TO", "Restaurant Brands International Limited", 3.14),
        ],
    )


def _analysts_payload() -> dict[str, object]:
    """Hand-author one ``getCompanyAnalysts`` reply with both nested objects the mapper requires."""
    return {
        "totalAnalysts": 23,
        "priceTarget": {
            "lowPriceTarget": 74.0,
            "highPriceTarget": 190.0,
            "priceTarget": 131.25,
            "priceTargetUpside": 8.07,
        },
        "consensusAnalysts": {
            "consensus": "buy",
            "buy": 18,
            "sell": 2,
            "hold": 3,
        },
    }


def _price_target_consensus_document() -> dict[str, Any]:
    """The analyst reply, projected into the TmxPriceTargetConsensus contract row."""
    return _document("getCompanyAnalysts", _analysts_payload())


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.tmx.provider",
    "allowed_hosts": [_GQL_HOST],
    "credential_env": {},
    "models": {
        "EquityQuote": {
            "fixture_document": _equity_quote_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "getQuoteBySymbol", "price"],
                "value": "121.44",
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROW",
            "tamper_reason": (
                "quoting the price leaves every other cell of the snapshot well-formed, and "
                "last_price is the contract column of TmxEquityQuote that TMX publishes as a JSON "
                "number: the quote mapper must refuse the snapshot rather than coerce a numeric "
                "string into a float it never parsed, because _number reads the TMX numeric fields "
                "as numbers only and a coerced last_price would silently carry a percent and a "
                "market cap derived from a cell whose type the source never published"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "EquityInfo": {
            "fixture_document": _equity_info_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "getQuoteBySymbol", "employees"],
                "value": "12800",
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROW",
            "tamper_reason": (
                "employees is the one profile column of TmxEquityInfo typed StrictInt, so quoting "
                "it breaks exactly that cell while the description, address and share counts stay "
                "whole: the profile mapper must refuse rather than publish a company whose "
                "headcount is a string, because the contract row is what a consumer sums across "
                "companies and a text headcount either fails their arithmetic later or reads as a "
                "null the fixture never produced"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "CompanyFilings": {
            "fixture_document": _company_filings_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL, "limit": 1},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "filings"],
                "value": [_filing(0), _filing(1)],
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROWS",
            "tamper_reason": (
                "both records are individually well-formed, so nothing about either filing is "
                "wrong and only their count disagrees with the request: the caller asked for "
                "limit 1, filing_date is a required column of TmxCompanyFiling whose row the "
                "caller did not ask for, and the filings mapper must refuse the over-long reply "
                "rather than truncate it, because silently dropping the tail is indistinguishable "
                "from the source having no more filings to publish"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "PriceTargetConsensus": {
            "fixture_document": _price_target_consensus_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "getCompanyAnalysts", "consensusAnalysts"],
                "value": "18 buy / 2 sell / 3 hold",
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROW",
            "tamper_reason": (
                "the rating block is where this contract lives: buy_ratings, sell_ratings and "
                "hold_ratings are the three columns of TmxPriceTargetConsensus that only exist "
                "inside consensusAnalysts, so flattening that object to a label string leaves the "
                "price targets and totalAnalysts untouched and makes the three counts unreadable "
                "-- the consensus mapper must refuse rather than publish a row whose rating split "
                "is three nulls a consumer cannot tell apart from zero analyst coverage"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "EquityHistorical": {
            "fixture_document": _equity_historical_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL, "start_date": _HISTORY_DAY, "end_date": _HISTORY_DAY},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "getCompanyPriceHistory"],
                "value": [_history_row(_HISTORY_DAY), _history_row(_PRIOR_DAY)],
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROWS",
            "tamper_reason": (
                "a one-day window is published to the source as limit 1, so the added record is a "
                "day the caller never asked for: both rows carry all eleven selected columns and "
                "each of them maps, so nothing about either record is wrong and only their count "
                "disagrees with the request. date is the required column of TmxEquityHistorical "
                "that the extra row is filed under, and the history mapper must refuse the "
                "over-long reply rather than return it, because a consumer that asked for one "
                "day and received two cannot tell an over-delivering source from a window it "
                "mis-stated -- the ruling the filings mapper in this same module already makes"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "HistoricalDividends": {
            "fixture_document": _historical_dividends_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL},
            "tamper": {
                "op": "set",
                "path": [
                    "responses",
                    0,
                    "body",
                    "data",
                    "dividendHistory",
                    "dividends",
                    0,
                    "exDate",
                ],
                "value": "2026-06-14T00:00:00",
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROW",
            "tamper_reason": (
                "ex_dividend_date is the column every dividend row is filed under, the time_field "
                "the domain declares, and the second of the four columns its natural key names. "
                "transform_data compares it against the requested window: a value carrying a time "
                "is exactly what _iso_date refuses on purpose, because it parses calendar dates "
                "only. The dividend mapper must refuse the record rather than truncate the cell -- "
                "a date invented out of a timestamp moves which dividends the window keeps, and no "
                "other column of either record is touched by the tamper"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "IndexConstituents": {
            "fixture_document": _index_constituents_document,
            "client_seam": _SEAM,
            "query": {"symbol": _INDEX_SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "data", "constituents", 0, "symbol"],
                "value": None,
            },
            "refusal_class": "TmxProviderError",
            "refusal_code": "TMX_BAD_ROW",
            "tamper_reason": (
                "symbol is the whole natural key of the index-constituent domain and the only "
                "required column of TmxIndexConstituent: nulling it leaves that member's name, "
                "exchange, market value and weight intact and leaves the second member complete, "
                "so the refusal is about one cell. What the key buys this mapper is *presence*, "
                "not uniqueness: the same probe shows two members carrying one symbol still serve "
                "two rows, so the mapper must refuse a null-keyed member -- a row a consumer "
                "cannot address -- and de-duplication stays the storage layer's job, not a guard "
                "this witness could claim"
            ),
            "source_files": [
                "opendata/data/providers/tmx/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
