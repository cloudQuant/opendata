"""Wave-33 shard: offline witnesses for the nine TMX shared-directory loader rows.

Transport, measured rather than assumed.  TMX's SDK is not installed here and none of these
three legs is a GraphQL envelope, so every row is served through the recorded-envelope
``client_seam``: the runner replaces ``get_shared_http_client`` in
``opendata.data.providers.tmx.models.fetchers_w33_etf_loaders`` — the module that *calls* it,
the same reason the parent module names ``models/client.py`` (``iter2_witnesses/tmx.py:10``
explains the import-time binding) — and every entry declares ``method: "GET"`` so the fake's
widened surface cannot turn a directory GET into a free POST.  The three families found in the
shipped code are one GET each:

* ``dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json`` for EtfInfo, EtfSearch, EtfSectors,
  EtfCountries, EtfHoldings — upstream tmx ``utils/helpers.py:389`` (one request answers all
  five; ``models/etf_holdings.py:112`` reads holdings out of the same record, so the scoping
  note's "may need a per-ETF page" caveat did not reproduce);
* ``bondtradedata.iiroc.ca/debtip/designatedbonds/list`` for BondPrices and TreasuryPrices —
  upstream ``utils/helpers.py:1121``;
* ``tmxinfoservices.com/files/indices/sptsx-indices.json`` for AvailableIndices and
  IndexSectors — upstream ``models/available_indices.py:56`` and ``models/index_sectors.py:59``.

Row-level guards, which is what each entry tampers.  Nine rows, nine distinct required cells,
and the refusal is the package's own ``TmxProviderError`` raised at the site named in
``tamper_reason`` (line numbers below are this shard's landing state of
``opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py``).  Wave 32 learned that a
guard no input can reach is worth nothing, so each of these is reached by the *only* value that
cell can take in the fixture: quote a number the contract reads as a JSON number, put a number
where ``StrictStr`` lives, break an ISO date, or strip a string the mapper type-tests.  The
counterfact phase runs the tampered leg in a fresh child, and that leg must put at least one
request through the carrier built from its own document — which the ``use_cache`` field cannot
defeat, because ``_directory_document`` keeps no module-level cache (nothing in this module
remembers a previous answer).

Two things this shard does **not** claim.  (1) ``allowed_hosts`` in the parent module still
names only ``app-money.tmx.com`` (``iter2_witnesses/tmx.py:117``); the three directory hosts are
not in it, and that list is scaffold-owned, so it is reported rather than edited here.  The
seam's own ``url_contains`` needles are what bind these fixtures to their routes, and the
grant's host set is not consulted by the fake, so the leg still cannot reach a socket.
(2) The bodies are **hand-authored fixture bodies, not live captures**: no claim is made that
cloudfront, iiroc.ca or tmxinfoservices.com published these values.  They carry the key spellings
the upstream selections and rename tables publish (cited per entry) and the shapes the shipped
mappers read.  Cells whose source spelling is *not* evidenced are said so in the entry that
prices them rather than implied by a passing row: ``TmxEtfInfo.domicile`` (no directory key),
``TmxAvailableIndices.exchange`` (the document has no exchange cell; upstream derives
``category`` instead), and the seven treasury cells the provider's alias table leaves alone
(``security_type``/``bid``/``offer``/``eod_price``/``call_date``/``cusip``/``isin``).

Not witnessed by any entry, because no fixture can reach them without rejecting the whole leg
instead of one contract cell: ``TMX_TRANSPORT_ERROR``, the status refusals, ``TMX_BAD_JSON``,
``TMX_RESPONSE_TOO_LARGE``; ``TMX_BAD_ROWS`` from ``_directory_rows``/``_directory_object`` (a
body that is not a JSON array/object — it is one call, so a tamper there and a tamper here would
witness the same thing twice, and the ceiling arm needs a 10 001-record carrier);
``TMX_BAD_FILTER`` (a query-shape refusal, not a reply-shape one); and the zero-row branch
every directory row takes when the requested symbol is absent from the directory.
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.tmx.models.fetchers_w33_etf_loaders"],
    "attr": "get_shared_http_client",
}

_ETF_ROUTE: Final = "dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json"
_BONDS_ROUTE: Final = "bondtradedata.iiroc.ca/debtip/designatedbonds/list"
_INDEX_ROUTE: Final = "tmxinfoservices.com/files/indices/sptsx-indices.json"

_SYMBOL: Final = "XYZ"
_OTHER_SYMBOL: Final = "QDF"
_INDEX_SYMBOL: Final = "CUX"
#: How many designated-bond records the debt fixture publishes (measured against
#: ``MAX_DIRECTORY_ROWS`` = 10 000 in the printed face; the upstream docstring's 70-100 K
#: figure was never measured and this carrier is nowhere near the ceiling).
_BOND_RECORDS: Final = 120


def _document(route: str, payload: Any) -> dict[str, Any]:  # noqa: ANN401  # fixture JSON payload
    """One recorded GET for one directory route."""
    return {
        "responses": [
            {"url_contains": route, "method": "GET", "body": payload},
        ]
    }


# --------------------------------------------------------------------------- ETF directory


def _holding(index: int, *, weighted: float | str = 2.4) -> dict[str, Any]:
    """One ``top10holdings`` cell, keyed as tmx ``models/etf_holdings.py:114-129`` renames it."""
    return {
        "symbol": ["SHOP", "AFR.TO", "CNQ"][index % 3],
        "securityname": f"Holding {index + 1}",
        "weighting": weighted if index == 0 else 1.1 + index,
        "numberofshares": 10_000 + index,
        "marketvalue": 1_250_000.5 + index if index else "N/A",
        "currency": "CAD",
        "sharepercentage": 0.24,
        "sharechange": "N/A" if index == 1 else -1.5,
        "country": "Canada",
        "excode": "TOR",
        "detailholdingtypeid": "EQ",
        "fundid": "FUND-1",
    }


def _etf_record(symbol: str, longname: str, *, monthly: bool) -> dict[str, Any]:
    """One record of the ETF directory, keyed by the raw cells ``helpers.py:21-77`` renames."""
    return {
        "symbol": symbol,
        "shortname": symbol[:3],
        "longname": longname,
        "fundfamily": "Family House",
        "currency": "CAD",
        "inceptiondate": "2012-03-15",
        "investmentstyle": "Growth" if monthly else "Balanced",
        "esg": monthly,
        "unitprice": 21.44,
        "close": 21.4,
        "prevClose": 21.32,
        "totalreturn1month": 1.2 if monthly else -0.4,
        "totalreturn3month": 3.4,
        "totalreturn6month": 6.1,
        "totalreturnytd": 9.8,
        "totalreturn1year": 12.5,
        "totalreturn3year": 30.2,
        "totalreturn5year": 55.1,
        "totalreturn10year": 120.4,
        "totalreturnsinceinception": 210.9,
        "beta1y": 0.91,
        "beta3y": 0.95,
        "beta5y": 1.02,
        "beta10y": 1.05,
        "beta15y": 1.08,
        "avgdailyvolume": 184_000,
        "avgvol30days": 176_000,
        "aum": 812_500_000.0,
        "pricetoearnings": 18.9,
        "pricetobook": 2.4,
        "managementfee": 0.2,
        "distributionyeld": 1.7,
        "dividendfrequency": "Monthly" if monthly else "Quarterly",
        "prospectobjective": f"{longname} seeks long-term growth.",
        "sectors": [
            {"name": "Financials", "percent": 60.5},
            {"name": "Technology", "percent": 39.5},
        ],
        "regions": [
            {"name": "Canada", "percent": 88.1},
            {"name": "United States", "percent": 11.9},
        ],
        "top10holdings": [_holding(0), _holding(1), _holding(2)],
        "altData": {
            "fundfamilyen": "Family House Funds",
            "websitefactsheeten": f"https://www.tmx.example/factsheet/{symbol}",
            "mer": 0.58,
        },
    }


def _etf_directory() -> list[dict[str, Any]]:
    """Three ETFs: the requested one, a second keyword hit, and one that never matches."""
    return [
        _etf_record(_SYMBOL, "Growth Portfolio ETF", monthly=True),
        _etf_record("ABC", "Balanced Canada ETF", monthly=False),
        _etf_record(_OTHER_SYMBOL, "Growth Income ETF", monthly=False),
    ]


def _etf_directory_document() -> dict[str, Any]:
    """The one GET that answers all five ETF rows."""
    return _document(_ETF_ROUTE, _etf_directory())


# --------------------------------------------------------------------------- IIROC debt


def _bond_record(index: int) -> dict[str, Any]:
    """One designated-bond record, keyed by the aliases at ``models/bond_prices.py:57-68``."""
    buckets = ("Federal", "Provincial", "Municipal")
    is_treasury = index % 2 == 0
    return {
        "isin": f"CA9{index:07d}X0",
        "lei": None if index % 7 else f"LEI{index:06d}",
        "figi": "N/A" if index % 5 == 0 else f"BB000{index:05d}",
        "cusip": f"12345{index % 100:02d}10",
        "couponRate": 1.5 + (index % 9) * 0.25,
        "lastYield": 3.4 + (index % 5) * 0.1,
        "lastPrice": 98.5 + (index % 7) * 0.05,
        "highestPrice": 101.2,
        "lowestPrice": "N/A" if index % 11 == 0 else 95.4,
        "totalTrades": 4 + index % 13,
        "lastTradedDate": "2026-10-15",
        "maturityDate": f"20{27 + index % 20}-0{(index % 9) + 1}-28",
        "originalIssueDate": f"20{10 + index % 16}-0{(index % 9) + 1}-15",
        "call_date": None,
        "bid": None if is_treasury else 99.1,
        "offer": None if is_treasury else 99.6,
        "eod_price": 99.3,
        "security_type": "Note" if index % 3 else "Bond",
        "issuer": (
            f"{buckets[index % 3]} Government of Canada Debt"
            if is_treasury
            else f"Corporation Bond {index % 40}"
        ),
        "bondType": "DEBT",
        "securityId": index,
        "secKey": 10_000 + index,
    }


def _bond_listing() -> list[dict[str, Any]]:
    """The whole debt listing this fixture publishes: ``_BOND_RECORDS`` records, one GET."""
    return [_bond_record(index) for index in range(_BOND_RECORDS)]


def _bond_listing_document() -> dict[str, Any]:
    """The one GET that answers both debt rows."""
    return _document(_BONDS_ROUTE, _bond_listing())


# --------------------------------------------------------------------------- index services


def _index_directory() -> dict[str, Any]:
    """The information-services document: an ``indices`` object plus the ``groups`` map over it."""
    return {
        "groups": {"COMPOSITE": [_INDEX_SYMBOL], "SECTOR": ["SPT"], "ESG": [_OTHER_SYMBOL]},
        "indices": {
            _INDEX_SYMBOL: {
                "name_en": "S&P/TSX Composite Index",
                "overview_en": "<p>The benchmark for Canadian large caps.</p>",
                "quotedmarketvalue": {"total": 2_400_000_000_000.0},
                "sectors": [
                    {"name": "Financials", "weight": 32.4},
                    {"name": "Industrials", "weight": 17.1},
                    {"name": "Technology", "weight": 14.8},
                ],
            },
            "SPT": {
                "name_en": "S&P/TSX Capped Energy Index (USD)",
                "sectors": [{"name": "Energy", "weight": 100.0}],
            },
            _OTHER_SYMBOL: {
                "name_en": "S&P/TSX Growth Income Index",
                "sectors": [{"name": "Financials", "weight": 41.0}],
            },
        },
    }


def _index_directory_document() -> dict[str, Any]:
    """The one GET that answers both index rows."""
    return _document(_INDEX_ROUTE, _index_directory())


# --------------------------------------------------------------------------- entries


_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py",
    "opendata/data/providers/tmx/models/client.py",
    "opendata/data/providers/tmx/provider.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/tmx.py",
    "opendata/data/domains.yaml",
]


def _entry(
    *,
    builder: Any,  # noqa: ANN401  # the fixture document callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    refusal_code: str,
    tamper_reason: str,
) -> dict[str, Any]:  # the witness entry shape
    return {
        "fixture_document": builder,
        "client_seam": _SEAM,
        "query": query,
        "tamper": tamper,
        "refusal_class": "TmxProviderError",
        "refusal_code": refusal_code,
        "tamper_reason": tamper_reason,
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    }


_BODY: Final = ["responses", 0, "body"]

MODELS: Final[dict[str, dict[str, Any]]] = {  # witness entries are dynamic JSON
    "EtfInfo": _entry(
        builder=_etf_directory_document,
        query={"symbol": _SYMBOL},
        tamper={"op": "set", "path": [*_BODY, 0, "avgdailyvolume"], "value": "184000"},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the profile row is built for exactly one directory record, and avg_volume is one of "
            "the two StrictInt columns of TmxEtfInfo: quoting avgdailyvolume breaks that cell "
            "while every price, return and ratio in the same record stays whole, so the refusal "
            "is about one column. The traceback ends at _contract_row in "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:238 "
            "(TmxProviderError TMX_BAD_ROW, raised from the model_validate at :236) — the "
            "profile mapper must refuse rather than coerce a text volume into an int it never "
            "parsed, because a consumer that averages volumes across ETFs would either crash "
            "later or read a null the directory never published"
        ),
    ),
    "EtfSearch": _entry(
        builder=_etf_directory_document,
        query={"query": "growth"},
        tamper={"op": "set", "path": [*_BODY, 0, "totalreturn1month"], "value": "1.2"},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the keyword screen is upstream's five-column one (tmx models/etf_search.py:233-237) "
            "and two records match, so the tamper lands on a return cell of the first hit only: "
            "return_1m of TmxEtfSearch carries the _number validator of "
            "opendata/data/models/tmx.py, which refuses a JSON string on purpose. The traceback "
            "ends at _contract_row in "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:238 — the search "
            "mapper must refuse the hit rather than publish a fund whose one-month return is "
            "text, because the other 33 columns of that row would still validate and the "
            "result frame would look clean"
        ),
    ),
    "EtfSectors": _entry(
        builder=_etf_directory_document,
        query={"symbol": _SYMBOL},
        tamper={
            "op": "set",
            "path": [*_BODY, 0, "sectors", 0, "percent"],
            "value": "60.5",
        },
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the row is one entry of the ETF record's own sectors list, mapped name->sector and "
            "percent->weight exactly as tmx models/etf_sectors.py:74 renames them, and weight is "
            "the one required non-null column of TmxEtfSectors (its _number validator). Quoting "
            "that one percent leaves the sector label, the second member and the echoed symbol "
            "intact. The traceback ends at _contract_row in "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:238 (reached through "
            "_etf_breakdown_rows at :558) — a weight a consumer cannot sum is not a weight"
        ),
    ),
    "EtfCountries": _entry(
        builder=_etf_directory_document,
        query={"symbol": _SYMBOL},
        tamper={"op": "set", "path": [*_BODY, 0, "regions", 0, "name"], "value": 5},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "country is the required StrictStr column of TmxEtfCountries, read from the regions "
            "list's name cell the way tmx models/etf_countries.py:78 renames it; putting a JSON "
            "number there breaks one cell and leaves both weights and the second region whole, "
            "so the refusal prices the label and not the arithmetic. The traceback ends at "
            "_contract_row in opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:238 "
            "(through _etf_breakdown_rows at :558) — a numeric country name would be published "
            "as a row whose key a consumer cannot join on"
        ),
    ),
    "EtfHoldings": _entry(
        builder=_etf_directory_document,
        query={"symbol": "xyz"},
        tamper={"op": "set", "path": [*_BODY, 0, "top10holdings", 0, "weighting"], "value": "2.4"},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the holdings list travels inside the same directory record (tmx "
            "models/etf_holdings.py:112), which is why the scoping note's per-ETF-page caveat did "
            "not reproduce and why the query symbol here is lowercase: _normalize_symbol at "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:241 is the shipped "
            "normalization from tmx models/etf_sectors.py:68 and the leg still found the record. "
            "weight is a required-by-validator column of TmxEtfHoldings whose _number validator "
            "refuses text; quoting the first holding's weighting leaves its share count, market "
            "value, currency and the other two holdings whole. The traceback ends at _contract_row "
            "in the same module at :238 (through _etf_holdings_rows at :583)"
        ),
    ),
    "BondPrices": _entry(
        builder=_bond_listing_document,
        query={"issuer_name": "Corporation"},
        tamper={"op": "set", "path": [*_BODY, 1, "couponRate"], "value": "1.75"},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the issuer screen runs on raw records before any row exists (_bond_raw_predicate "
            "at opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:758, the "
            "issuer.str.contains shape tmx uses at models/bond_prices.py:193), so the record "
            "index the tamper breaks is the first *surviving* one — index 1 of the listing, whose "
            "coupon is published as text. coupon_rate of TmxBondPrices carries the _number "
            "validator, and the same record's isin, yield, prices and dates stay intact. The "
            "traceback ends at _contract_row in that module at :238 (through _bond_prices_rows at "
            ":816). Scale note, said plainly instead of implied: this mapper publishes couponRate "
            "as the source number and does NOT copy upstream's /100 percent normalization "
            "(models/bond_prices.py:120-127), because no evidence in this repo fixes the listing's "
            "scale — the fixture therefore carries the source-scale value 1.75"
        ),
    ),
    "TreasuryPrices": _entry(
        builder=_bond_listing_document,
        query={"govt_type": "federal"},
        tamper={"op": "set", "path": [*_BODY, 0, "maturityDate"], "value": "28/01/2027"},
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the government-issuer predicate is tmx's own "
            "issuer.str.contains(govt_type) (models/treasury_prices.py:144, reimplemented at "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:768), so the federal "
            "selection starts at record 0 of the listing and the tamper breaks that record's "
            "maturity cell. maturity_date of TmxTreasuryPrices is parsed by the contract's "
            "_iso_date, and this mapper parses the raw cell itself, at the _exact_date raise "
            "(fetchers_w33_etf_loaders.py:264), so the traceback ends there: "
            "28/01/2027 is a date the debt row cannot be filed under, and neither the coupon, "
            "the yield nor the bid/offer/eod_price/call_date/security_type/cusip/isin cells are "
            "touched by the tamper (those seven are read under the contract's own names because "
            "the provider's alias table at models/treasury_prices.py:62-73 leaves them alone -- "
            "their raw spelling is not independently evidenced)"
        ),
    ),
    "AvailableIndices": _entry(
        builder=_index_directory_document,
        query={},
        tamper={
            "op": "set",
            "path": [*_BODY, "indices", _INDEX_SYMBOL, "name_en"],
            "value": 5,
        },
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the row set is the document's indices object, one row per symbol, and the currency "
            "cell is derived from this same name string by tmx's own rule "
            '"USD" if "(USD)" in name_en else "CAD" (models/available_indices.py:99-102), '
            "reimplemented as the name type-test whose raise is at "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:926 — that screen is "
            "where the traceback ends, with TMX_BAD_ROW, before _contract_row is ever reached. A "
            "numeric name means the mapper cannot state the currency at all, so it must refuse "
            "the directory rather than publish a symbol whose currency was invented; the other "
            "two index entries, the sectors lists and the groups map are untouched by the tamper"
        ),
    ),
    "IndexSectors": _entry(
        builder=_index_directory_document,
        query={"symbol": _INDEX_SYMBOL},
        tamper={
            "op": "set",
            "path": [*_BODY, "indices", _INDEX_SYMBOL, "sectors", 0, "weight"],
            "value": "32.4",
        },
        refusal_code="TMX_BAD_ROW",
        tamper_reason=(
            "the domain declares only sector and weight for this row -- no symbol column, unlike "
            "TmxEtfSectors -- so the only cells a sector entry can break are those two, and the "
            "mapper reads them as name/weight straight from the index document (tmx "
            "models/index_sectors.py:84-87). weight is the required float of TmxIndexSectors and "
            "its _number validator refuses text; the sector label, the other two members and "
            "their weights are intact. The traceback ends at _contract_row in "
            "opendata/data/providers/tmx/models/fetchers_w33_etf_loaders.py:238 (through "
            "_index_sectors_rows at :952)"
        ),
    ),
}

__all__ = ["MODELS"]
