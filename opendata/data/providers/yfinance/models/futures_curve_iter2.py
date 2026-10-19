"""Wave-34 yfinance ``FuturesCurve`` row: current curve for one contract root.

Field provenance (produced, not invented) -- every declared column is traced to a response
cell of the two legs this row runs through the governed session seam:

``symbol``
    The request echo.  Upstream's declared output contract of
    ``openbb_yfinance/models/futures_curve.py`` (spec sheet sha256
    ``5c3eefd5820916661a73f9c41aa5bb0db3253f06659698811e5dfc441674e52b``) carries no symbol
    column, and the chain
    frame labels nothing, so the mapper stamps the normalized root it asked for.
``expiration``
    Decoded from the chain symbol's contract-month code -- the same decode upstream performs in
    ``openbb_yfinance/utils/helpers.py:236-240`` (``get_expiration_month``) and applies at
    ``helpers.py:291``.  Upstream's own column is the year-month *string* that function returns
    (``standard_models/futures_curve.py:62``, ``expiration: str``); this repo's contract declares
    ``expiration: date``, so the mapper resolves the month to its first day and says so here.
``date``
    ``regularMarketTime`` of that expiry's quote row, i.e. the as-of stamp the source itself
    published for the mark.  Upstream's current-curve branch drops the column entirely
    (``helpers.py:293`` returns only ``["expiration","price"]``, and
    ``standard_models/futures_curve.py:59-61`` leaves ``date`` nullable), so this is the source
    cell that answers this contract's required ``date``; nothing is stamped from the wall clock.
``price``
    Mid of ``bid``/``ask``, falling back to the last/previous close, exactly the derivation
    upstream performs at ``helpers.py:288-290``.  Required-but-nullable: a quote row that carries
    no mark at all yields ``None`` rather than a dropped expiry or an invented zero.

Two legs, one seam.  ``get_raw_json`` is the transport upstream itself uses for this row
(``helpers.py:247``), and 0.2.66 publishes both modules and the method: ``futuresChain`` is in
``yfinance/const.py:159`` (``quote_summary_valid_modules``) and ``YfData.get_raw_json`` is
``yfinance/data.py:379``.  Leg 1 is
``<base>/v10/finance/quoteSummary/<ROOT>%3DF?modules=futuresChain``
(``helpers.py:242-245`` declares the root encoding, the path and the module) and yields the
expiry symbol list; leg 2 is ``<base>/v7/finance/quote?symbols=<joined expiries>`` -- upstream's
``get_futures_quotes`` (``helpers.py:281-285``) issues exactly one joined quote request for the
same list -- and yields the mark and the as-of stamp per expiry.  Both legs go out on the one
``YfData`` bound to :func:`opendata.data.providers.yfinance.models._sdk.governed_yfinance_session`
(the ``governed_sdk_session`` seam this provider already runs for its landed rows), the SDK builds
its own cookie/crumb and query strings, and no socket is opened by this module.

Measured limits, stated instead of papered over.  A chain entry whose symbol carries no
contract-month code (a continuous root such as ``ES=F``) cannot answer this contract's required
``expiration``, and a chain entry the quote leg does not return cannot answer its required
``date``; both are skipped rather than published with a substituted value, and a melt that
produces no row at all refuses with ``YFINANCE_FUTURES_CURVE_EMPTY``.  Nothing here claims to have
widened or narrowed what the row means: the four columns are the four the domain declares.

The dated branch is a separate problem and is *not* claimed here: upstream's historical branch
walks per-expiry chart legs (``helpers.py:296-314``), so the query validator refuses a ``date``
instead of quietly answering a different question.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date as date_type  # pydantic resolves these at runtime
from datetime import datetime, timezone
from numbers import Real
from typing import Any, ClassVar, Final, TypeVar

from pydantic import ConfigDict, model_validator

from opendata.data.capability import Capability
from opendata.data.domains import contract_model
from opendata.data.models.base import ContractModel
from opendata.data.models.yfinance_iter2 import YfinanceFuturesCurve
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.providers.yfinance.models import _sdk
from opendata.data.providers.yfinance.models.iter2 import YfinanceSymbolQuery
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.request_budget import RequestBudgetError

_ContractT = TypeVar("_ContractT", bound=ContractModel)

#: The quoteSummary module 0.2.66 publishes the chain under (``yfinance/const.py:159``).
_CHAIN_MODULE: Final[str] = "futuresChain"
#: Declared endpoint paths, both taken from upstream's own declaration of this row's legs.
_CHAIN_PATH: Final[str] = "/v10/finance/quoteSummary"
_QUOTES_PATH: Final[str] = "/v7/finance/quote"

#: Contract-month codes (the decode upstream applies at ``helpers.py:236-240``).
_MONTH_CODES: Final[dict[str, int]] = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}

#: (chain expiry symbols, quote rows keyed by the symbol the source echoed them under).
FuturesCurveRaw = tuple[list[str], Mapping[str, Mapping[str, object]]]


def _row_model(domain: str, model: type[_ContractT]) -> type[_ContractT]:
    """Bind the domain's contract class, refusing a mismatch at import time."""
    resolved = contract_model(domain)
    if resolved is not model:
        raise TypeError(f"{domain}: contract resolves to {resolved.__name__}, not {model.__name__}")
    return model


def _finite(value: object) -> float | None:
    """A real, finite number or ``None`` -- never a bool, a string or an NaN dressed up as one."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _chain_symbol(root: str) -> str:
    """Yahoo keys the chain on the contract root with its ``=`` percent-encoded."""
    return f"{root.upper()}%3DF"


def _expiration_of(symbol: str) -> date_type | None:
    """First day of the expiration month encoded in one chain symbol's contract-month code."""
    head = symbol.split(".")[0]
    if len(head) < 3:
        return None
    month = _MONTH_CODES.get(head[-3])
    year_digits = head[-2:]
    if month is None or not year_digits.isdigit():
        return None
    return date_type(2000 + int(year_digits), month, 1)


def _base_url(sdk: Any) -> str:  # noqa: ANN401  # untyped optional SDK module
    """The SDK's own query host, so this row never hardcodes a vendor origin."""
    base = getattr(getattr(sdk, "const", None), "_BASE_URL_", None)
    if not isinstance(base, str) or not base.startswith("https://"):
        raise YfinanceProviderError("YFINANCE_SDK_SURFACE_MISSING")
    return base


def _chain_symbols(response: object) -> list[str]:
    """Read the expiry list out of the ``futuresChain`` envelope the SDK parsed."""
    if not isinstance(response, Mapping):
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_SHAPE_INVALID")
    summary = response.get("quoteSummary")
    if not isinstance(summary, Mapping):
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_SHAPE_INVALID")
    result = summary.get("result")
    if not isinstance(result, list):
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_SHAPE_INVALID")
    if not result:
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_EMPTY")
    chain = result[0].get("futuresChain") if isinstance(result[0], Mapping) else None
    if not isinstance(chain, Mapping):
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_SHAPE_INVALID")
    futures = chain.get("futures")
    if not isinstance(futures, list) or any(not isinstance(item, str) for item in futures):
        # Upstream joins this list straight into a symbol string (``helpers.py:284``), so the
        # cells it documents are symbol codes; anything else is not this row's source.
        raise YfinanceProviderError("YFINANCE_FUTURES_CHAIN_SHAPE_INVALID")
    return [item for item in futures if item]


def _quote_rows(response: object) -> dict[str, Mapping[str, object]]:
    """Index the joined quote leg by the symbol the source echoed each row under."""
    if not isinstance(response, Mapping):
        raise YfinanceProviderError("YFINANCE_FUTURES_QUOTES_SHAPE_INVALID")
    payload = response.get("quoteResponse")
    if not isinstance(payload, Mapping):
        raise YfinanceProviderError("YFINANCE_FUTURES_QUOTES_SHAPE_INVALID")
    result = payload.get("result")
    if not isinstance(result, list) or any(not isinstance(item, Mapping) for item in result):
        raise YfinanceProviderError("YFINANCE_FUTURES_QUOTES_SHAPE_INVALID")
    rows: dict[str, Mapping[str, object]] = {}
    for item in result:
        symbol = item.get("symbol")
        if isinstance(symbol, str) and symbol:
            rows[symbol] = item
    return rows


def require_futures_curve(root: str) -> FuturesCurveRaw:
    """Run both curve legs on the one governed session and hand back what each answered.

    Raises:
        YfinanceProviderError: The SDK is missing, a leg failed, or the chain envelope does not
            carry the expiry list this row's contract is read from.
    """
    sdk = _sdk._require_sdk()  # the package's single documented SDK access point
    try:
        with _sdk.governed_yfinance_session(sdk) as session:
            yfdata = sdk.data.YfData(session=session)  # untyped optional SDK
            base = _base_url(sdk)
            chain_response: object = yfdata.get_raw_json(
                url=f"{base}{_CHAIN_PATH}/{_chain_symbol(root)}",
                params={"modules": _CHAIN_MODULE},
            )
            symbols = _chain_symbols(chain_response)
            quotes_response: object = yfdata.get_raw_json(
                url=f"{base}{_QUOTES_PATH}",
                params={"symbols": ",".join(symbols)},
            )
            return symbols, _quote_rows(quotes_response)
    except RequestBudgetError:
        raise
    except YfinanceProviderError:
        raise
    except Exception as exc:  # the SDK re-raises transport errors as its own/curl types
        raise YfinanceProviderError("YFINANCE_REQUEST_FAILED") from exc


class FuturesCurveQuery(YfinanceSymbolQuery):
    """One contract root's current futures curve.

    ``date`` is declared on the model and refused on the validator, because this row's contract is
    the current curve only.  Silently ignoring a date would answer "what is the curve now" to a
    caller who asked "what was the curve then", which is the wrong-row failure this provider's
    query validators exist to prevent.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    date: date_type | None = None

    @model_validator(mode="after")
    def _reject_dated_history(self) -> FuturesCurveQuery:
        if self.date is not None:
            raise YfinanceProviderError("YFINANCE_BAD_QUERY")
        return self


class YfinanceFuturesCurveFetcher(Fetcher[FuturesCurveQuery, FuturesCurveRaw]):
    """Current futures curve for one contract root from the SDK's ``futuresChain`` module."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FuturesCurve"
    domain: ClassVar[str] = "yfinance_futures_curve"
    query_type: ClassVar[type[FuturesCurveQuery]] = FuturesCurveQuery
    row_model: ClassVar[type[YfinanceFuturesCurve]] = _row_model(
        "yfinance_futures_curve", YfinanceFuturesCurve
    )
    capability: ClassVar[Capability] = Capability(
        asset_class="futures",
        domain="yfinance_futures_curve",
        period="snapshot",
        market="global",
        source=SOURCE,
        verified=False,
        notes="FuturesCurve: wave-34 yfinance current curve; futuresChain + quote legs, offline",
    )

    def transform_query(self, **kwargs: object) -> FuturesCurveQuery:
        """Validate the request, refusing a dated curve rather than narrowing it."""
        return FuturesCurveQuery.model_validate(kwargs)

    def extract_data(self, params: FuturesCurveQuery, ctx: FetchContext) -> FuturesCurveRaw:
        """Read the chain and its quotes through the governed SDK session."""
        query = _validated_curve_query(params)
        return require_futures_curve(query.symbol)

    def transform_data(self, raw: object, params: FuturesCurveQuery) -> FetchResult:
        """Melt the two legs into one dated row per expirable, quotable chain entry."""
        query = _validated_curve_query(params)
        if (
            not isinstance(raw, tuple)
            or len(raw) != 2
            or not isinstance(raw[0], list)
            or not isinstance(raw[1], Mapping)
        ):
            raise YfinanceProviderError("YFINANCE_FUTURES_CURVE_SHAPE_INVALID")
        symbols, rows = raw[0], raw[1]
        if any(not isinstance(item, str) for item in symbols):
            raise YfinanceProviderError("YFINANCE_FUTURES_CURVE_SHAPE_INVALID")
        melted: list[YfinanceFuturesCurve] = []
        for expiry_symbol in symbols:
            expiration = _expiration_of(expiry_symbol)
            row = rows.get(expiry_symbol)
            if expiration is None or row is None:
                # No decodable contract month, or no quote row: neither required column can be
                # answered from the source, so this expiry is not published with a substitute.
                continue
            melted.append(
                YfinanceFuturesCurve(
                    symbol=query.symbol,
                    date=_as_of_date(row),
                    expiration=expiration,
                    price=_price_of(row),
                )
            )
        if not melted:
            raise YfinanceProviderError("YFINANCE_FUTURES_CURVE_EMPTY")
        return melted


def _validated_curve_query(params: FuturesCurveQuery) -> FuturesCurveQuery:
    """Re-check the stage hand-off so a raw tuple cannot be fed past a mutated query."""
    if not isinstance(params, FuturesCurveQuery):
        raise YfinanceProviderError("YFINANCE_BAD_QUERY")
    return params


def _as_of_date(row: Mapping[str, object]) -> date_type:
    """The UTC calendar date of the quote's own ``regularMarketTime`` stamp."""
    stamp = _finite(row.get("regularMarketTime"))
    if stamp is None or stamp <= 0:
        raise YfinanceProviderError("YFINANCE_FUTURES_CURVE_AS_OF_MISSING")
    return datetime.fromtimestamp(int(stamp), tz=timezone.utc).date()


def _price_of(row: Mapping[str, object]) -> float | None:
    """Mid of bid/ask, falling back to the last price then the previous close (upstream's order)."""
    bid = _finite(row.get("bid"))
    ask = _finite(row.get("ask"))
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 2)
    for key in ("lastMarket", "regularMarketPrice", "previousClose"):
        value = _finite(row.get(key))
        if value is not None:
            return value
    return None


__all__ = [
    "FuturesCurveQuery",
    "YfinanceFuturesCurveFetcher",
    "require_futures_curve",
]
