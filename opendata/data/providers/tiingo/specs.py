"""Declarative specification for Tiingo's CryptoHistorical model.

The remaining six census rows use typed adapters in ``models.iter2``; this module retains the
engine declaration for ``OBB2-tiingo-CryptoHistorical``
(``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/census-tiingo.json``, upstream contract
``providers/tiingo/openbb_tiingo/models/crypto_historical.py``, cited below as
``crypto_historical.py``). The row's need set is ``nested_array_explode``,
``client_side_sort``, ``param_value_mapping``, ``date_format`` and ``relative_date_default``, and
the census records the row in the shipped_cover set rather than ``expressible_today``: every facet
it needs is one the engine already publishes, so what was missing was this package, not a kernel.

The model identity ledger at the bottom is projected from the provider's full binding set.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.row_expansion import (
    FieldMapping,
    NestedArrayExpansion,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    ParamSpec,
    RelativeDefault,
    RowOrderSpec,
)

#: Tiingo's API root, the same origin every reviewed tiingo model addresses
#: (crypto_historical.py:125 spells ``https://api.tiingo.com/tiingo/crypto/prices``).
TIINGO_BASE_URL: Final = "https://api.tiingo.com"

#: Environment variable holding the access token (``credentials["tiingo_token"]``,
#: crypto_historical.py:121), sent as the ``token`` query key (:138).
TIINGO_CREDENTIAL: Final = "TIINGO_TOKEN"

#: The interval spellings the model publishes and the ``resampleFreq`` spellings the endpoint takes.
#: Upstream derives the second from the first by suffix substitution (:128-135): ``*m`` becomes
#: ``<n>min``, ``*h`` ``<n>hour``, ``*d`` ``<n>day``, and the fallback arm sends ``1day``.
CRYPTO_INTERVAL_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("1m", "1min"),
    ("5m", "5min"),
    ("15m", "15min"),
    ("30m", "30min"),
    ("90m", "90min"),
    ("1h", "1hour"),
    ("2h", "2hour"),
    ("4h", "4hour"),
    ("1d", "1day"),
    ("7d", "7day"),
    ("30d", "30day"),
)

#: The keys one ``priceData[]`` item publishes for the columns this model carries, as the cited
#: response shape (:160-181) and the provider's own alias dict (``transactions`` -> ``tradesDone``,
#: ``volume_notional`` -> ``volumeNotional``, :71-75) spell them.
CRYPTO_PRICE_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("date", "date"),
    ("open", "open"),
    ("high", "high"),
    ("low", "low"),
    ("close", "close"),
    ("volume", "volume"),
    ("transactions", "tradesDone"),
    ("volume_notional", "volumeNotional"),
)

#: The crypto history of one or more tickers: ONE GET for every symbol (the tickers query param is
#: the comma-separated list, aliased at :50), each response object's nested ``priceData`` array
#: exploded into rows carrying their own ticker, and the exploded rows re-ordered client-side by
#: date (:183). No paging.
CRYPTO_HISTORICAL = ModelSpec(
    model="CryptoHistorical",
    domain="tiingo_crypto_historical",
    asset_class="crypto",
    period="d",
    market="all",
    base_url=TIINGO_BASE_URL,
    path="/tiingo/crypto/prices",
    rows_pointer="",
    params=(
        ParamSpec(
            "symbol",
            "str",
            query_key="tickers",
            required=True,
            note="One request carries every ticker: the model aliases symbol->tickers (:50) and "
            "the caller's comma-separated list goes on the wire whole (get_querystring at :126), "
            "which is why this row needs no symbol_fan_out the equity rows need.",
        ),
        ParamSpec(
            "start_date",
            "date",
            query_key="startDate",
            relative_default=RelativeDefault(years=-1),
            note="Window start: an omitted bound is filled with ``now - relativedelta(years=1)`` "
            "(:103-105) -- the one-year offset the census re-read at C96, NOT a 90-day window.",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="endDate",
            relative_default=RelativeDefault(),
            note="Window end: the request date itself (:107-108), the all-zero RelativeDefault.",
        ),
        ParamSpec(
            "interval",
            "enum",
            query_key="resampleFreq",
            enum=tuple(caller for caller, _ in CRYPTO_INTERVAL_MAP),
            default="1d",
            value_map=CRYPTO_INTERVAL_MAP,
            note="param_value_mapping face: the caller's spelling is remapped to the endpoint's "
            "resampleFreq (:128-135), with ``1d`` as upstream's own default (:51-53). An "
            "unmapped interval is a refusal at encode time, where upstream's :135 fallback arm "
            "silently sends ``1day`` -- recorded as a divergence, not papered over.",
        ),
        ParamSpec(
            "exchanges",
            "str_list",
            note="Exchange subset the endpoint filters on (:36-42, multiple_items_allowed at :47); "
            "the model publishes no alias, so the caller's name is the wire key.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="ticker", required=True, nullable=False),
        ColumnSpec("date", "date", required=True, nullable=False, units="iso8601"),
        ColumnSpec("open", "float", units="usd"),
        ColumnSpec("high", "float", units="usd"),
        ColumnSpec("low", "float", units="usd"),
        ColumnSpec("close", "float", required=True, units="usd"),
        ColumnSpec("volume", "float", units="base_currency"),
        ColumnSpec("transactions", "int", source_key="tradesDone"),
        ColumnSpec("volume_notional", "float", source_key="volumeNotional", units="quote_currency"),
    ),
    row_expansion=(
        NestedArrayExpansion(
            records_pointer="",
            child_array_pointer="priceData",
            child_fields=tuple(
                FieldMapping(pointer, pointer, required=pointer in ("date", "close"))
                for _column, pointer in CRYPTO_PRICE_FIELDS
            ),
            parent_fields=(FieldMapping("ticker", "ticker"),),
        ),
    ),
    order_by=RowOrderSpec(column="date", direction="asc"),
    credential=TIINGO_CREDENTIAL,
    credential_query_key="token",
    scenario="加密货币历史价格：一次请求取回多个 ticker 的嵌套 priceData 并按日期升序展开成行",
    error_prefix="TIINGO",
    notes=(
        "providers/tiingo/openbb_tiingo/models/crypto_historical.py:36-53,71-75,103-108,121-138,"
        "160-183 (contract only; clean-room, no upstream code ported). Shape is get_json_single, "
        "one endpoint, no paging, row_extraction=nested_key: the document is a bare array of "
        "ticker objects, so rows_pointer is empty and the explode is the per-object priceData "
        "array "
        "(nested_array_explode, carried by NestedArrayExpansion at :165-181). DIVERGENCE, "
        "recorded: upstream stamps the symbol column only when the response returned more than one "
        "ticker (:191-193, the equity-family rule), while the expansion's parent_fields copy the "
        "ticker onto every row unconditionally -- a single-ticker answer therefore carries a "
        "symbol where upstream leaves the field unset. The date face is the standard model's own "
        "coercion (standard_models/crypto_historical.py:44-52: an ISO string with a colon "
        "parses to "
        "a datetime, otherwise to a date), which this declaration carries as one ``date`` column "
        "rather than the two branches of :228-233 the equity row uses. ``vwap`` is a field of the "
        "standard model the tiingo body never publishes, so it is not declared here. client_side_"
        "sort is order_by over the exploded records with no cap (:183 sorts by date and returns "
        "everything), and the engine judges it on the record key the ``date`` column reads, before "
        "normalization. Upstream warns (does not raise) for a requested ticker the response lacks "
        "(:163-167) and for an object whose priceData is empty (:172-175); this declaration is "
        "silent on both -- a missing priceData key is the kernel's MISSING_POINTER refusal and an "
        "empty array contributes no rows, which the kernel documents as a published answer of none."
    ),
)

#: The tiingo model identities the iteration-2 census records for this provider.
UPSTREAM_ROWS: Final[tuple[str, ...]] = (
    "CompanyNews",
    "CryptoHistorical",
    "CurrencyHistorical",
    "EquityHistorical",
    "EtfHistorical",
    "TrailingDividendYield",
    "WorldNews",
)

#: Model identities with provider bindings. ``CryptoHistorical`` is the declarative model above;
#: the other six are hand-written adapters because their output/URL shapes need bounded transforms.
DECLARED_MODELS: Final[tuple[str, ...]] = UPSTREAM_ROWS

#: There are no unbound Tiingo census identities in this provider slice.
NOT_DECLARABLE: Final[dict[str, str]] = {}
