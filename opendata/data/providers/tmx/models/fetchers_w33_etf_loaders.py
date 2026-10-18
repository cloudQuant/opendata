"""Wave-33 TMX shared-loader fetchers (ETF directory, IIROC bonds, index services).

Nine rows ride three *public directory* loaders.  The wave's scoping note
(``docs/evidence/C138/wave33-implement-missing-model-20261018/notes/per_row_scoping.md``)
placed these loaders in ``client.py``; that file is not this shard's to edit and carries only
the GraphQL operation table, so all three are implemented **here**, bound to the same governed
client the accepted TMX rows use.  The consequence for witnessing is the one that matters: this
module imports ``get_shared_http_client`` itself (``client.py:8`` does the same), so the witness
seam patches the attribute in *this* module's globals -- and the shipped call site below
resolves it there, which is what makes a mis-declared seam fail loudly instead of silently
fetching.

The three transport families, with the upstream shape each is derived from (the OpenBB checkout
is read as an output-contract reference only; no code was copied):

* ``_ETF_DIRECTORY_URL`` -- one GET of ``…/etfs/etfs.json`` answers ``EtfInfo``, ``EtfSearch``,
  ``EtfSectors``, ``EtfCountries`` and ``EtfHoldings`` (tmx ``utils/helpers.py:389``; raw record
  keys from the rename table at ``utils/helpers.py:21-77``, the per-ETF nested lists at
  ``models/etf_sectors.py:70-79``, ``models/etf_countries.py:73-78``,
  ``models/etf_holdings.py:112-129``, the ``altData`` derivations at ``utils/helpers.py:421-424``
  and the keyword/div-frequency predicates at ``models/etf_search.py:231-246``).  The caveat that
  ``EtfHoldings`` "may need a per-ETF page beyond ``etfs.json``" is **not** borne out: upstream
  reads ``holdings_top10`` out of the same directory record (``models/etf_holdings.py:112``).
* ``_BONDS_LIST_URL`` -- ``…/debtip/designatedbonds/list``, one listing answering both
  ``BondPrices`` and ``TreasuryPrices`` (tmx ``utils/helpers.py:1121``; source keys from the
  provider alias tables at ``models/bond_prices.py:57-68`` and ``models/treasury_prices.py:62-73``
  plus the ``N/A``-to-null and integer/float casts at ``utils/helpers.py:1127-1150``).
* ``_INDEX_DIRECTORY_URL`` -- ``…/files/indices/sptsx-indices.json`` for ``AvailableIndices`` and
  ``IndexSectors`` (tmx ``models/available_indices.py:56``, ``models/index_sectors.py:59``; the
  ``indices`` object, the ``name_en`` column and the "(USD)" currency rule are read at
  ``models/available_indices.py:82-104``; sectors at ``models/index_sectors.py:78-88``).

The row ceiling is measured, not assumed: the shipped ceiling in this package is
``client.MAX_RESPONSE_ROWS`` = 10 000 (``client.py:17``), while the IIROC listing is documented
upstream as "approximately 70-100K securities" (``utils/helpers.py:1110``) -- an upstream
docstring that nothing in this repo has ever measured.  So no leg here materializes an unbounded
frame: ``_bounded_matches`` streams the source records through a raw predicate and holds at most
``MAX_DIRECTORY_ROWS`` + 1 matches, refusing with ``TMX_BAD_ROWS`` only when even the *filtered*
set is over the ceiling.  A caller who asks for the whole listing gets a typed refusal; a caller
who filters gets an answer.

Refusals are the package's own ``TmxProviderError``, so they carry ``code`` and ``status``:
``TMX_BAD_ROW`` at the contract construction site (:func:`_contract_row`, and the one
name-typed screen in :func:`_available_indices_rows`), ``TMX_BAD_ROWS`` at the directory shape
and ceiling screens (:func:`_directory_rows`, :func:`_directory_object`,
:func:`_bounded_matches`), ``TMX_BAD_FILTER`` at filters whose source column this package cannot
evidence (:func:`_unsupported_bond_filters`, :func:`_etf_search_rows`), ``TMX_BAD_SORT`` for an
ordering that is not a total order, plus the transport, status, size and decode refusals reused
from ``client.py``'s own mapping.
"""

from __future__ import annotations

import itertools
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Final, Literal, TypeVar, cast

from opendata.data.capability import Capability
from opendata.data.domains import contract_model
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.base import ContractModel
from opendata.data.models.tmx import (
    TmxAvailableIndices,
    TmxBondPrices,
    TmxEtfCountries,
    TmxEtfHoldings,
    TmxEtfInfo,
    TmxEtfSearch,
    TmxEtfSectors,
    TmxIndexSectors,
    TmxTreasuryPrices,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.tmx.models.client import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    TmxProviderError,
    _http_code,
)
from opendata.data.providers.tmx.models.fetchers import _TmxQuery
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

_Date = date
_QueryT = TypeVar("_QueryT", bound=QueryParams)
_ContractT = TypeVar("_ContractT", bound=ContractModel)

#: One GET answers the five ETF rows (tmx ``utils/helpers.py:389``).
_ETF_DIRECTORY_URL: Final = "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json"
#: One listing answers both IIROC debt rows (tmx ``utils/helpers.py:1121``).
_BONDS_LIST_URL: Final = "https://bondtradedata.iiroc.ca/debtip/designatedbonds/list"
#: One document answers both index rows (tmx ``models/available_indices.py:56``).
_INDEX_DIRECTORY_URL: Final = "https://tmxinfoservices.com/files/indices/sptsx-indices.json"

#: The three directory hosts these rows name.  The provider's parent witness module still
#: allow-lists only ``app-money.tmx.com`` (``iter2_witnesses/tmx.py:117``), which is a scaffold
#: gap this shard reports instead of editing around.
ALLOWED_DIRECTORY_HOSTS: Final = [
    "dgr53wu9i7rmp.cloudfront.net",
    "bondtradedata.iiroc.ca",
    "tmxinfoservices.com",
]

#: The row ceiling a shared directory loader may hand to a frame in one request.
MAX_DIRECTORY_ROWS: Final = 10_000

#: The government-issuer tokens ``TreasuryPrices`` selects on (tmx ``models/treasury_prices.py:27``
#: and the ``issuer.str.contains`` predicate at ``:144``).
_GOVT_TOKENS: Final = ("federal", "provincial", "municipal")

#: The five columns TMX's ETF search screens (tmx ``models/etf_search.py:233-237``), whose
#: frame columns ``name``/``short_name``/``investment_objectives`` are renames of these raw keys.
_SEARCH_RAW_KEYS: Final = (
    "longname",
    "shortname",
    "investmentstyle",
    "prospectobjective",
    "symbol",
)

#: Columns a caller may order the search result by; anything else is refused, not ignored.
_SEARCH_SORT_COLUMNS: Final = frozenset(TmxEtfSearch.model_fields)


def _capability(domain: str, period: str, model: str, asset_class: str) -> Capability:
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period=period,
        market="CA",
        source="tmx",
        verified=False,
        notes=f"{model}: wave-33 TMX shared-directory loader; synthetic fixtures only",
    )


def _row_model(domain: str, model: type[_ContractT]) -> type[_ContractT]:
    """Bind the domain's contract class, refusing a mismatch at import time."""
    resolved = contract_model(domain)
    if resolved is not model:
        raise TypeError(f"{domain}: contract resolves to {resolved.__name__}, not {model.__name__}")
    return model


# --------------------------------------------------------------------------- transport


def _directory_document(url: str, *, timeout: float, use_cache: bool) -> object:
    """Fetch one public directory document through the governed client, bounded and decoded.

    The status and transport refusals are this package's own mapping (``client.py:150-166``), so a
    directory leg that answers 429, 500 or a truncated body refuses exactly like an accepted
    GraphQL row.  Nothing is cached in this module: every call asks the seam, which is what lets a
    counterfact leg that was handed a tampered document prove it read it.
    """
    try:
        response = get_shared_http_client().get(
            url,
            headers={"Accept": "application/json"},
            timeout=min(timeout, DEFAULT_TIMEOUT_SECONDS),
            source="tmx",
            use_cache=use_cache,
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
        document: object = response.json()
    except Exception:
        raise TmxProviderError("TMX_BAD_JSON", status=status) from None
    return document


def _directory_rows(document: object) -> list[Mapping[str, object]]:
    """Require a JSON array of objects; a directory that is not one is not answerable."""
    if not isinstance(document, list):
        raise TmxProviderError("TMX_BAD_ROWS")
    if any(not isinstance(row, dict) for row in document):
        raise TmxProviderError("TMX_BAD_ROWS")
    return [cast("Mapping[str, object]", row) for row in document]


def _directory_object(document: object, key: str) -> Mapping[str, object]:
    """Require one JSON object section of a directory document (the index service shape)."""
    if not isinstance(document, dict):
        raise TmxProviderError("TMX_BAD_ROWS")
    section = cast("Mapping[str, object]", document).get(key)
    if not isinstance(section, dict):
        raise TmxProviderError("TMX_BAD_ROWS")
    return cast("Mapping[str, object]", section)


def _directory_row(value: object) -> Mapping[str, object]:
    """One nested directory record (a sector, a country, a holding, an index's metadata)."""
    if not isinstance(value, dict):
        raise TmxProviderError("TMX_BAD_ROWS")
    return cast("Mapping[str, object]", value)


def _nested_list(value: object) -> list[object]:
    """Read one nested list field; absent publishes no rows, wrong-typed is a shape refusal."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise TmxProviderError("TMX_BAD_ROWS")
    return value


def _bounded_matches(
    rows: Iterable[Mapping[str, object]],
    predicate: Callable[[Mapping[str, object]], bool],
) -> list[Mapping[str, object]]:
    """Filter raw records before building anything, refusing past the ceiling."""
    kept = (row for row in rows if predicate(row))
    matches = list(itertools.islice(kept, MAX_DIRECTORY_ROWS + 1))
    if len(matches) > MAX_DIRECTORY_ROWS:
        raise TmxProviderError("TMX_BAD_ROWS")
    return matches


def _contract_row(model: type[_ContractT], mapped: Mapping[str, object]) -> _ContractT:
    """Build one frozen contract row, refusing a cell the contract cannot hold."""
    try:
        return model.model_validate(dict(mapped))
    except Exception:
        raise TmxProviderError("TMX_BAD_ROW") from None


def _normalize_symbol(symbol: str) -> str:
    """TMX's own ETF symbol normalization (tmx ``models/etf_sectors.py:68``)."""
    return symbol.upper().replace("-", ".").replace(".TO", "").replace(".TSX", "")


def _na_to_none(value: object) -> object:
    """TMX publishes ``N/A``/``NA``/``-`` cells for debt records (tmx ``utils/helpers.py:1127``)."""
    cleaned = value.strip() if isinstance(value, str) else value
    return None if isinstance(cleaned, str) and cleaned in {"N/A", "NA", "-"} else value


def _exact_date(value: object) -> _Date | None:
    """Parse a raw source date only as an exact ISO calendar date, or refuse."""
    cleaned = _na_to_none(value)
    if cleaned is None:
        return None
    if isinstance(cleaned, _Date):
        return cleaned
    if not isinstance(cleaned, str):
        raise TmxProviderError("TMX_BAD_ROW")
    try:
        return _Date.fromisoformat(cleaned)
    except ValueError:
        raise TmxProviderError("TMX_BAD_ROW") from None


def _contains(haystack: object, needle: str) -> bool:
    """Case-insensitive substring test on a raw cell, the way TMX's own filters read it."""
    return isinstance(haystack, str) and needle.casefold() in haystack.casefold()


def _pick_etf(records: list[Mapping[str, object]], symbol: str) -> Mapping[str, object] | None:
    """The one directory record whose normalized symbol is the one asked for."""
    target = _normalize_symbol(symbol)
    for record in records:
        if _normalize_symbol(str(record.get("symbol") or "")) == target:
            return record
    return None


def _alt_data(record: Mapping[str, object]) -> Mapping[str, object]:
    """TMX's ``altData`` block: issuer, website and MER come from it (``helpers.py:421-424``)."""
    raw = record.get("altData")
    return {} if raw is None else _directory_row(raw)


# --------------------------------------------------------------------------- query shapes


class EtfInfoQuery(_TmxQuery):
    """ETF profile request against the shared directory."""

    symbol: str
    use_cache: bool = False


class EtfSearchQuery(_TmxQuery):
    """Keyword search over the shared ETF directory."""

    query: str
    div_freq: Literal["", "M", "Q", "S", "A", "O"] = ""
    sort_by: str | None = None
    use_cache: bool = False


class EtfBreakdownQuery(_TmxQuery):
    """Sector or country weight request; the two rows share one directory leg."""

    symbol: str
    use_cache: bool = False


class EtfHoldingsQuery(_TmxQuery):
    """Holdings request against the shared ETF directory."""

    symbol: str
    use_cache: bool = False


class BondPricesQuery(_TmxQuery):
    """Designated-bond listing filters, all optional and bounded upstream."""

    country: str | None = None
    issuer_name: str | None = None
    isin: str | None = None
    lei: str | None = None
    currency: str | None = None
    coupon_min: float | None = None
    coupon_max: float | None = None
    issued_amount_min: float | None = None
    issued_amount_max: float | None = None
    maturity_date_min: _Date | None = None
    maturity_date_max: _Date | None = None
    issue_date_min: _Date | None = None
    issue_date_max: _Date | None = None
    last_traded_min: _Date | None = None
    use_cache: bool = False


class TreasuryPricesQuery(_TmxQuery):
    """Treasury listing filters over the same IIROC debt endpoint.

    ``govt_type`` is the TMX query parameter, declared ``federal``/``provincial``/``municipal``
    (tmx ``models/treasury_prices.py:27``); an unset value selects any government issuer.
    """

    govt_type: Literal["federal", "provincial", "municipal"] | None = None
    issue_date_min: _Date | None = None
    issue_date_max: _Date | None = None
    maturity_date_min: _Date | None = None
    maturity_date_max: _Date | None = None
    last_traded_min: _Date | None = None
    use_cache: bool = False


class AvailableIndicesQuery(_TmxQuery):
    """The TMX information-services index directory; no symbol is required."""

    use_cache: bool = False


class IndexSectorsQuery(_TmxQuery):
    """Sector weights for one index symbol from the information-services directory."""

    symbol: str
    use_cache: bool = False


# --------------------------------------------------------------------------- shared fetch shape


class _DirectoryFetcher(Fetcher[_QueryT, object]):
    """Shared shape for rows served by one of the three directory loaders."""

    async_mode = "bounded_thread"
    domain: ClassVar[str]
    query_type: ClassVar[type[_QueryT]]
    directory_url: ClassVar[str]

    @staticmethod
    def _timeout(ctx: FetchContext) -> float:
        return (
            DEFAULT_TIMEOUT_SECONDS
            if ctx.timeout is None
            else min(ctx.timeout, DEFAULT_TIMEOUT_SECONDS)
        )

    def transform_query(self, **kwargs: object) -> _QueryT:
        """Validate the request against this row's declared query type."""
        return self.query_type.model_validate(kwargs)

    def extract_data(self, params: _QueryT, ctx: FetchContext) -> object:
        """Read this row's directory leg through the governed client."""
        use_cache = getattr(params, "use_cache", False)
        return _directory_document(
            self.directory_url,
            timeout=self._timeout(ctx),
            use_cache=bool(use_cache),
        )


# --------------------------------------------------------------------------- ETF directory rows


def _etf_info_mapped(record: Mapping[str, object]) -> dict[str, object]:
    """Project one directory record into the 31 columns of :class:`TmxEtfInfo`."""
    alt = _alt_data(record)
    return {
        "symbol": record.get("symbol"),
        "name": record.get("longname"),
        # ``issuer`` is the fund family read out of altData, not the ``fundfamily`` cell
        # (tmx ``utils/helpers.py:422`` and ``models/etf_info.py:32``).
        "issuer": alt.get("fundfamilyen"),
        # No ETF-directory key is evidenced for a fund's domicile; published as null.
        "domicile": None,
        "website": alt.get("websitefactsheeten"),
        "description": record.get("prospectobjective"),
        "inception_date": record.get("inceptiondate"),
        "investment_style": record.get("investmentstyle"),
        "esg": record.get("esg"),
        "currency": record.get("currency"),
        "unit_price": record.get("unitprice"),
        "close": record.get("close"),
        "prev_close": record.get("prevClose"),
        "return_1m": record.get("totalreturn1month"),
        "return_3m": record.get("totalreturn3month"),
        "return_6m": record.get("totalreturn6month"),
        "return_ytd": record.get("totalreturnytd"),
        "return_1y": record.get("totalreturn1year"),
        "return_3y": record.get("totalreturn3year"),
        "return_5y": record.get("totalreturn5year"),
        "return_10y": record.get("totalreturn10year"),
        "return_from_inception": record.get("totalreturnsinceinception"),
        "avg_volume": record.get("avgdailyvolume"),
        "avg_volume_30d": record.get("avgvol30days"),
        "aum": record.get("aum"),
        "pe_ratio": record.get("pricetoearnings"),
        "pb_ratio": record.get("pricetobook"),
        "management_fee": record.get("managementfee"),
        "mer": alt.get("mer"),
        "distribution_yield": record.get("distributionyeld"),
        "dividend_frequency": record.get("dividendfrequency"),
    }


def _etf_search_mapped(record: Mapping[str, object]) -> dict[str, object]:
    """Project one directory record into the 34 columns of :class:`TmxEtfSearch`."""
    mapped = _etf_info_mapped(record)
    return {
        "symbol": mapped["symbol"],
        "name": mapped["name"],
        "short_name": record.get("shortname"),
        # The search contract keeps the inception cell as the source string; only
        # ``TmxEtfInfo`` parses it to a date.
        "inception_date": record.get("inceptiondate"),
        "issuer": mapped["issuer"],
        "investment_style": mapped["investment_style"],
        "esg": mapped["esg"],
        "currency": mapped["currency"],
        "unit_price": mapped["unit_price"],
        "close": mapped["close"],
        "prev_close": mapped["prev_close"],
        "return_1m": mapped["return_1m"],
        "return_3m": mapped["return_3m"],
        "return_6m": mapped["return_6m"],
        "return_ytd": mapped["return_ytd"],
        "return_1y": mapped["return_1y"],
        "beta_1y": record.get("beta1y"),
        "return_3y": mapped["return_3y"],
        "beta_3y": record.get("beta3y"),
        "return_5y": mapped["return_5y"],
        "beta_5y": record.get("beta5y"),
        "return_10y": mapped["return_10y"],
        "beta_10y": record.get("beta10y"),
        "beta_15y": record.get("beta15y"),
        "return_from_inception": mapped["return_from_inception"],
        "avg_volume": mapped["avg_volume"],
        "avg_volume_30d": mapped["avg_volume_30d"],
        "aum": mapped["aum"],
        "pe_ratio": mapped["pe_ratio"],
        "pb_ratio": mapped["pb_ratio"],
        "management_fee": mapped["management_fee"],
        "mer": mapped["mer"],
        "distribution_yield": mapped["distribution_yield"],
        "dividend_frequency": mapped["dividend_frequency"],
    }


def _etf_keyword_predicate(needle: str) -> Callable[[Mapping[str, object]], bool]:
    """TMX's five search columns, case-insensitively (tmx ``models/etf_search.py:233-237``)."""

    def matches(record: Mapping[str, object]) -> bool:
        return any(_contains(record.get(key), needle) for key in _SEARCH_RAW_KEYS)

    return matches


def _sort_key(column: str, row: TmxEtfSearch) -> tuple[int, float | str]:
    """Order one contract column with nulls last; refuse a column with no total order."""
    value = getattr(row, column)
    if value is None:
        return (1, 0.0)
    if isinstance(value, bool):
        return (0, float(int(value)))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, str):
        return (0, value)
    return (0, str(value))


def _etf_info_rows(raw: object, params: EtfInfoQuery) -> FetchResult:
    """One ETF profile row for the requested symbol, or none when the directory omits it."""
    record = _pick_etf(_directory_rows(raw), params.symbol)
    if record is None:
        return ()
    return (_contract_row(TmxEtfInfo, _etf_info_mapped(record)),)


def _etf_search_rows(raw: object, params: EtfSearchQuery) -> FetchResult:
    """Directory rows whose five searchable columns contain the keyword."""
    needle = params.query.strip()
    if not needle:
        raise TmxProviderError("TMX_BAD_FILTER")
    if params.sort_by is not None and params.sort_by not in _SEARCH_SORT_COLUMNS:
        # A name this contract does not publish cannot be ordered, and silently dropping
        # the caller's ``sort_by`` would ship a frame in the source's own order.
        raise TmxProviderError("TMX_BAD_FILTER")
    matches = _bounded_matches(_directory_rows(raw), _etf_keyword_predicate(needle))
    rows = [_contract_row(TmxEtfSearch, _etf_search_mapped(record)) for record in matches]
    if params.div_freq:
        # TMX stores the frequency capitalized (tmx ``models/etf_search.py:243``).
        wanted = params.div_freq.capitalize()
        rows = [row for row in rows if row.dividend_frequency == wanted]
    if params.sort_by is not None:
        try:
            rows.sort(key=lambda row: _sort_key(str(params.sort_by), row), reverse=True)
        except TypeError:
            raise TmxProviderError("TMX_BAD_SORT") from None
    return tuple(rows)


def _etf_breakdown_rows(
    raw: object,
    params: EtfBreakdownQuery,
    key: str,
    model: type[ContractModel],
    label_column: str,
) -> FetchResult:
    """Weights from one ETF's nested breakdown list (tmx etf_sectors.py:74, etf_countries.py:78)."""
    record = _pick_etf(_directory_rows(raw), params.symbol)
    if record is None:
        return ()
    rows: list[ContractModel] = []
    for item in _nested_list(record.get(key)):
        entry = _directory_row(item)
        rows.append(
            _contract_row(
                model,
                {
                    label_column: entry.get("name"),
                    "weight": entry.get("percent"),
                    "symbol": params.symbol.upper(),
                },
            )
        )
    return tuple(rows)


def _etf_holdings_rows(raw: object, params: EtfHoldingsQuery) -> FetchResult:
    """The ETF's top-ten holdings list (tmx ``models/etf_holdings.py:114-129``)."""
    record = _pick_etf(_directory_rows(raw), params.symbol)
    if record is None:
        return ()
    rows: list[TmxEtfHoldings] = []
    for item in _nested_list(record.get("top10holdings")):
        entry = _directory_row(item)
        share_change = entry.get("sharechange")
        if share_change is None:
            # TMX publishes this cell under both spellings (``models/etf_holdings.py:128-129``).
            share_change = entry.get("shareChange")
        rows.append(
            _contract_row(
                TmxEtfHoldings,
                {
                    "symbol": entry.get("symbol"),
                    "name": entry.get("securityname"),
                    "weight": entry.get("weighting"),
                    "shares": entry.get("numberofshares"),
                    "market_value": _na_to_none(entry.get("marketvalue")),
                    "currency": entry.get("currency"),
                    "share_percentage": entry.get("sharepercentage"),
                    "share_change": _na_to_none(share_change),
                    "country": entry.get("country"),
                    "exchange": entry.get("excode"),
                    "type_id": entry.get("detailholdingtypeid"),
                    "fund_id": entry.get("fundid"),
                },
            )
        )
    return tuple(rows)


class EtfInfoFetcher(_DirectoryFetcher[EtfInfoQuery]):
    """ETF profile rows from the shared cloudfront directory."""

    canonical_model: ClassVar[str | None] = "EtfInfo"
    domain: ClassVar[str] = "tmx_etf_info"
    directory_url: ClassVar[str] = _ETF_DIRECTORY_URL
    query_type: ClassVar[type[EtfInfoQuery]] = EtfInfoQuery
    row_model: ClassVar[type[TmxEtfInfo]] = _row_model("tmx_etf_info", TmxEtfInfo)
    capability: ClassVar[Capability] = _capability("tmx_etf_info", "snapshot", "EtfInfo", "etf")

    def transform_data(self, raw: object, params: EtfInfoQuery) -> FetchResult:
        """Map the selected directory record into the 31-column profile contract."""
        return _etf_info_rows(raw, params)


class EtfSearchFetcher(_DirectoryFetcher[EtfSearchQuery]):
    """ETF search rows from the shared cloudfront directory."""

    canonical_model: ClassVar[str | None] = "EtfSearch"
    domain: ClassVar[str] = "tmx_etf_search"
    directory_url: ClassVar[str] = _ETF_DIRECTORY_URL
    query_type: ClassVar[type[EtfSearchQuery]] = EtfSearchQuery
    row_model: ClassVar[type[TmxEtfSearch]] = _row_model("tmx_etf_search", TmxEtfSearch)
    capability: ClassVar[Capability] = _capability("tmx_etf_search", "snapshot", "EtfSearch", "etf")

    def transform_data(self, raw: object, params: EtfSearchQuery) -> FetchResult:
        """Screen the directory by keyword and map every hit into the 34-column row."""
        return _etf_search_rows(raw, params)


class EtfSectorsFetcher(_DirectoryFetcher[EtfBreakdownQuery]):
    """ETF sector weights from the shared cloudfront directory."""

    canonical_model: ClassVar[str | None] = "EtfSectors"
    domain: ClassVar[str] = "tmx_etf_sectors"
    directory_url: ClassVar[str] = _ETF_DIRECTORY_URL
    query_type: ClassVar[type[EtfBreakdownQuery]] = EtfBreakdownQuery
    row_model: ClassVar[type[TmxEtfSectors]] = _row_model("tmx_etf_sectors", TmxEtfSectors)
    capability: ClassVar[Capability] = _capability(
        "tmx_etf_sectors", "snapshot", "EtfSectors", "etf"
    )

    def transform_data(self, raw: object, params: EtfBreakdownQuery) -> FetchResult:
        """Project the record's ``sectors`` list into sector/weight rows."""
        return _etf_breakdown_rows(raw, params, "sectors", TmxEtfSectors, "sector")


class EtfCountriesFetcher(_DirectoryFetcher[EtfBreakdownQuery]):
    """ETF country weights from the shared cloudfront directory."""

    canonical_model: ClassVar[str | None] = "EtfCountries"
    domain: ClassVar[str] = "tmx_etf_countries"
    directory_url: ClassVar[str] = _ETF_DIRECTORY_URL
    query_type: ClassVar[type[EtfBreakdownQuery]] = EtfBreakdownQuery
    row_model: ClassVar[type[TmxEtfCountries]] = _row_model("tmx_etf_countries", TmxEtfCountries)
    capability: ClassVar[Capability] = _capability(
        "tmx_etf_countries", "snapshot", "EtfCountries", "etf"
    )

    def transform_data(self, raw: object, params: EtfBreakdownQuery) -> FetchResult:
        """Project the record's ``regions`` list into country/weight rows."""
        return _etf_breakdown_rows(raw, params, "regions", TmxEtfCountries, "country")


class EtfHoldingsFetcher(_DirectoryFetcher[EtfHoldingsQuery]):
    """ETF holding rows from the shared cloudfront directory."""

    canonical_model: ClassVar[str | None] = "EtfHoldings"
    domain: ClassVar[str] = "tmx_etf_holdings"
    directory_url: ClassVar[str] = _ETF_DIRECTORY_URL
    query_type: ClassVar[type[EtfHoldingsQuery]] = EtfHoldingsQuery
    row_model: ClassVar[type[TmxEtfHoldings]] = _row_model("tmx_etf_holdings", TmxEtfHoldings)
    capability: ClassVar[Capability] = _capability(
        "tmx_etf_holdings", "snapshot", "EtfHoldings", "etf"
    )

    def transform_data(self, raw: object, params: EtfHoldingsQuery) -> FetchResult:
        """Map the record's ``top10holdings`` list into the 12-column holding row."""
        return _etf_holdings_rows(raw, params)


# --------------------------------------------------------------------------- IIROC debt listing


def _bond_mapped(record: Mapping[str, object]) -> dict[str, object]:
    """One debt record into the 14 columns of TmxBondPrices (tmx bond_prices.py:57-68)."""
    return {
        "isin": _na_to_none(record.get("isin")),
        "lei": _na_to_none(record.get("lei")),
        "figi": _na_to_none(record.get("figi")),
        "cusip": _na_to_none(record.get("cusip")),
        # TMX publishes coupon and yield on the source's own percent scale; the in-repo
        # contract declares no unit, so this mapper does not re-scale it (unlike upstream's
        # ``/100`` validator at ``models/bond_prices.py:120-127``, which is reported as a
        # divergence rather than copied without a measured scale).
        "coupon_rate": _na_to_none(record.get("couponRate")),
        "ytm": _na_to_none(record.get("lastYield")),
        "price": _na_to_none(record.get("lastPrice")),
        "highest_price": _na_to_none(record.get("highestPrice")),
        "lowest_price": _na_to_none(record.get("lowestPrice")),
        "total_trades": _na_to_none(record.get("totalTrades")),
        "last_traded_date": _na_to_none(record.get("lastTradedDate")),
        "maturity_date": _na_to_none(record.get("maturityDate")),
        "issue_date": _na_to_none(record.get("originalIssueDate")),
        "issuer_name": _na_to_none(record.get("issuer")),
    }


def _treasury_mapped(record: Mapping[str, object]) -> dict[str, object]:
    """One debt record into the 17 columns of :class:`TmxTreasuryPrices`.

    Only the renamed columns have an evidenced source key (tmx ``models/treasury_prices.py:62-73``);
    ``security_type``, ``bid``, ``offer``, ``eod_price``, ``call_date``, ``cusip`` and ``isin`` are
    read under the contract's own names because the provider's alias table leaves them alone --
    the raw IIROC spelling of those seven cells is *not* independently evidenced.
    """
    return {
        "issuer_name": _na_to_none(record.get("issuer")),
        "cusip": _na_to_none(record.get("cusip")),
        "isin": _na_to_none(record.get("isin")),
        "security_type": _na_to_none(record.get("security_type")),
        "issue_date": _na_to_none(record.get("originalIssueDate")),
        "maturity_date": _na_to_none(record.get("maturityDate")),
        "call_date": _na_to_none(record.get("call_date")),
        "bid": _na_to_none(record.get("bid")),
        "offer": _na_to_none(record.get("offer")),
        "eod_price": _na_to_none(record.get("eod_price")),
        "last_traded_date": _na_to_none(record.get("lastTradedDate")),
        "total_trades": _na_to_none(record.get("totalTrades")),
        "last_price": _na_to_none(record.get("lastPrice")),
        "highest_price": _na_to_none(record.get("highestPrice")),
        "lowest_price": _na_to_none(record.get("lowestPrice")),
        "rate": _na_to_none(record.get("couponRate")),
        "ytm": _na_to_none(record.get("lastYield")),
    }


def _unsupported_bond_filters(params: BondPricesQuery) -> tuple[str, ...]:
    """Name every requested filter whose source column this package cannot evidence.

    Refusing is the honest half of the row ceiling: ``country``, ``currency`` and the issued
    amount bounds are TMX query parameters upstream, but no key carrying them appears in the
    listing shape this module reads (``utils/helpers.py:1121-1150``), so applying them would be
    a silent no-op and dropping them would ignore the caller.
    """
    candidates = (
        ("country", params.country),
        ("currency", params.currency),
        ("issued_amount_min", params.issued_amount_min),
        ("issued_amount_max", params.issued_amount_max),
    )
    return tuple(name for name, value in candidates if value is not None)


def _bond_raw_predicate(params: BondPricesQuery) -> Callable[[Mapping[str, object]], bool]:
    """Identifier/issuer screens applied to raw records before any row is built."""
    screens = (("isin", params.isin), ("lei", params.lei), ("issuer", params.issuer_name))

    def matches(record: Mapping[str, object]) -> bool:
        return all(needle is None or _contains(record.get(key), needle) for key, needle in screens)

    return matches


def _treasury_raw_predicate(params: TreasuryPricesQuery) -> Callable[[Mapping[str, object]], bool]:
    """Keep only government issuers, the ``issuer.str.contains`` rule TMX applies itself."""
    tokens = _GOVT_TOKENS if params.govt_type is None else (params.govt_type,)

    def matches(record: Mapping[str, object]) -> bool:
        issuer = record.get("issuer")
        return isinstance(issuer, str) and any(
            token.casefold() in issuer.casefold() for token in tokens
        )

    return matches


def _coupon_bound(record: Mapping[str, object], bound: float | None, *, upper: bool) -> bool:
    """Compare one raw coupon cell against a caller's bound; unreadable cells drop."""
    if bound is None:
        return True
    value = _na_to_none(record.get("couponRate"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    coupon = float(value)
    return coupon <= bound if upper else coupon >= bound


def _date_bound(row_date: _Date | None, minimum: _Date | None, maximum: _Date | None) -> bool:
    """Apply an inclusive date window to one already-parsed contract cell."""
    if row_date is None:
        return minimum is None and maximum is None
    if minimum is not None and row_date < minimum:
        return False
    return maximum is None or row_date <= maximum


def _bond_prices_rows(raw: object, params: BondPricesQuery) -> FetchResult:
    """Designated-bond rows, filtered before the frame and bounded at the ceiling."""
    if _unsupported_bond_filters(params):
        raise TmxProviderError("TMX_BAD_FILTER")
    screening = _bond_raw_predicate(params)
    records = _bounded_matches(
        _directory_rows(raw),
        lambda record: (
            screening(record)
            and _coupon_bound(record, params.coupon_min, upper=False)
            and _coupon_bound(record, params.coupon_max, upper=True)
        ),
    )
    rows: list[TmxBondPrices] = []
    for record in records:
        row = _contract_row(
            TmxBondPrices,
            {
                **_bond_mapped(record),
                "last_traded_date": _exact_date(record.get("lastTradedDate")),
                "maturity_date": _exact_date(record.get("maturityDate")),
                "issue_date": _exact_date(record.get("originalIssueDate")),
            },
        )
        if not _date_bound(row.maturity_date, params.maturity_date_min, params.maturity_date_max):
            continue
        if not _date_bound(row.issue_date, params.issue_date_min, params.issue_date_max):
            continue
        if params.last_traded_min is not None and (
            row.last_traded_date is None or row.last_traded_date < params.last_traded_min
        ):
            continue
        rows.append(row)
    return tuple(rows)


def _treasury_prices_rows(raw: object, params: TreasuryPricesQuery) -> FetchResult:
    """Treasury rows from the same listing, selected on the government-issuer predicate."""
    records = _bounded_matches(_directory_rows(raw), _treasury_raw_predicate(params))
    rows: list[TmxTreasuryPrices] = []
    for record in records:
        row = _contract_row(
            TmxTreasuryPrices,
            {
                **_treasury_mapped(record),
                "issue_date": _exact_date(record.get("originalIssueDate")),
                "maturity_date": _exact_date(record.get("maturityDate")),
                "call_date": _exact_date(record.get("call_date")),
                "last_traded_date": _exact_date(record.get("lastTradedDate")),
            },
        )
        if not _date_bound(row.issue_date, params.issue_date_min, params.issue_date_max):
            continue
        if not _date_bound(row.maturity_date, params.maturity_date_min, params.maturity_date_max):
            continue
        if params.last_traded_min is not None and (
            row.last_traded_date is None or row.last_traded_date < params.last_traded_min
        ):
            continue
        rows.append(row)
    return tuple(rows)


class BondPricesFetcher(_DirectoryFetcher[BondPricesQuery]):
    """Designated-bond rows from the IIROC debt listing."""

    canonical_model: ClassVar[str | None] = "BondPrices"
    domain: ClassVar[str] = "tmx_bond_prices"
    directory_url: ClassVar[str] = _BONDS_LIST_URL
    query_type: ClassVar[type[BondPricesQuery]] = BondPricesQuery
    row_model: ClassVar[type[TmxBondPrices]] = _row_model("tmx_bond_prices", TmxBondPrices)
    capability: ClassVar[Capability] = _capability(
        "tmx_bond_prices", "snapshot", "BondPrices", "fixed_income"
    )

    def transform_data(self, raw: object, params: BondPricesQuery) -> FetchResult:
        """Map and filter the listing into the 14-column bond contract."""
        return _bond_prices_rows(raw, params)


class TreasuryPricesFetcher(_DirectoryFetcher[TreasuryPricesQuery]):
    """Treasury bond rows from the same IIROC debt listing."""

    canonical_model: ClassVar[str | None] = "TreasuryPrices"
    domain: ClassVar[str] = "tmx_treasury_prices"
    directory_url: ClassVar[str] = _BONDS_LIST_URL
    query_type: ClassVar[type[TreasuryPricesQuery]] = TreasuryPricesQuery
    row_model: ClassVar[type[TmxTreasuryPrices]] = _row_model(
        "tmx_treasury_prices", TmxTreasuryPrices
    )
    capability: ClassVar[Capability] = _capability(
        "tmx_treasury_prices", "snapshot", "TreasuryPrices", "fixed_income"
    )

    def transform_data(self, raw: object, params: TreasuryPricesQuery) -> FetchResult:
        """Select government issuers and map them into the 17-column treasury contract."""
        return _treasury_prices_rows(raw, params)


# --------------------------------------------------------------------------- index services


def _index_records(raw: object) -> Mapping[str, object]:
    """The ``indices`` section of the index-services document (tmx available_indices.py:82)."""
    return _directory_object(raw, "indices")


def _available_indices_rows(raw: object, params: AvailableIndicesQuery) -> FetchResult:
    """One row per index the directory publishes under a name.

    ``exchange`` stays null: the document's per-index metadata carries ``name_en``,
    ``overview_en`` and ``quotedmarketvalue`` (tmx ``models/available_indices.py:88-104``) and no
    evidenced exchange cell, so publishing one would be an invention.
    """
    del params  # the directory takes no filter; the request is the whole document
    records = _index_records(raw)
    if len(records) > MAX_DIRECTORY_ROWS:
        raise TmxProviderError("TMX_BAD_ROWS")
    rows: list[TmxAvailableIndices] = []
    for symbol, value in records.items():
        meta = _directory_row(value)
        name = meta.get("name_en")
        if not isinstance(name, str):
            # TMX derives the currency from this same cell (``models/available_indices.py:100``),
            # so a directory entry whose name is not a string cannot be projected at all.
            raise TmxProviderError("TMX_BAD_ROW")
        rows.append(
            _contract_row(
                TmxAvailableIndices,
                {
                    "symbol": symbol,
                    "name": name,
                    "exchange": None,
                    "currency": "USD" if "(USD)" in name else "CAD",
                },
            )
        )
    return tuple(rows)


def _index_sectors_rows(raw: object, params: IndexSectorsQuery) -> FetchResult:
    """Sector weights for one index, same document (tmx ``models/index_sectors.py:78-88``)."""
    records = _index_records(raw)
    meta = records.get(params.symbol)
    if meta is None:
        return ()
    entry = _directory_row(meta)
    rows: list[TmxIndexSectors] = []
    for item in _nested_list(entry.get("sectors")):
        sector = _directory_row(item)
        rows.append(
            _contract_row(
                TmxIndexSectors,
                {"sector": sector.get("name"), "weight": sector.get("weight")},
            )
        )
    return tuple(rows)


class AvailableIndicesFetcher(_DirectoryFetcher[AvailableIndicesQuery]):
    """The TMX index directory from the information-services host."""

    canonical_model: ClassVar[str | None] = "AvailableIndices"
    domain: ClassVar[str] = "tmx_available_indices"
    directory_url: ClassVar[str] = _INDEX_DIRECTORY_URL
    query_type: ClassVar[type[AvailableIndicesQuery]] = AvailableIndicesQuery
    row_model: ClassVar[type[TmxAvailableIndices]] = _row_model(
        "tmx_available_indices", TmxAvailableIndices
    )
    capability: ClassVar[Capability] = _capability(
        "tmx_available_indices", "snapshot", "AvailableIndices", "index"
    )

    def transform_data(self, raw: object, params: AvailableIndicesQuery) -> FetchResult:
        """Project every named index entry into the 4-column directory row."""
        return _available_indices_rows(raw, params)


class IndexSectorsFetcher(_DirectoryFetcher[IndexSectorsQuery]):
    """Sector weights for one index from the information-services host."""

    canonical_model: ClassVar[str | None] = "IndexSectors"
    domain: ClassVar[str] = "tmx_index_sectors"
    directory_url: ClassVar[str] = _INDEX_DIRECTORY_URL
    query_type: ClassVar[type[IndexSectorsQuery]] = IndexSectorsQuery
    row_model: ClassVar[type[TmxIndexSectors]] = _row_model("tmx_index_sectors", TmxIndexSectors)
    capability: ClassVar[Capability] = _capability(
        "tmx_index_sectors", "snapshot", "IndexSectors", "index"
    )

    def transform_data(self, raw: object, params: IndexSectorsQuery) -> FetchResult:
        """Map the index's ``sectors`` list into the two-column weight row."""
        return _index_sectors_rows(raw, params)


__all__ = [
    "ALLOWED_DIRECTORY_HOSTS",
    "AvailableIndicesFetcher",
    "AvailableIndicesQuery",
    "BondPricesFetcher",
    "BondPricesQuery",
    "EtfBreakdownQuery",
    "EtfCountriesFetcher",
    "EtfHoldingsFetcher",
    "EtfHoldingsQuery",
    "EtfInfoFetcher",
    "EtfInfoQuery",
    "EtfSearchFetcher",
    "EtfSearchQuery",
    "EtfSectorsFetcher",
    "IndexSectorsFetcher",
    "IndexSectorsQuery",
    "MAX_DIRECTORY_ROWS",
    "TreasuryPricesFetcher",
    "TreasuryPricesQuery",
]
