"""Declarable cboe provider models.

Two of cboe's eleven upstream models are single-GET, JSON, path-addressed endpoints and are
declared here as data (:class:`~opendata.data.providers._engine.spec.ModelSpec`). The other nine
need composition the declaration format cannot express -- a pre-flight directory lookup, a
four-request join, CSV rather than JSON, delegation to another fetcher, or regex parsing of an
option contract symbol -- and are recorded in ``NOT_DECLARABLE`` below rather than silently
dropped from the task count.
"""

from __future__ import annotations

from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec

#: Upstream base for the public ``cdn.cboe.com`` JSON API. No model sends a credential.
CBOE_BASE_URL = "https://cdn.cboe.com"

#: The index directory, which is also the pre-flight other cboe models consult.
AVAILABLE_INDICES = ModelSpec(
    model="AvailableIndices",
    domain="cboe_index_catalog",
    asset_class="index",
    period="snapshot",
    market="us",
    base_url=CBOE_BASE_URL,
    path="/api/global/us_indices/definitions/all_indices.json",
    rows_pointer="",
    params=(),
    columns=(
        ColumnSpec("symbol", "str", source_key="index_symbol", required=True, units=""),
        ColumnSpec("name", "str", source_key="index_name", required=True),
        ColumnSpec("exchange", "str"),
        ColumnSpec("currency", "str"),
        ColumnSpec("description", "str"),
        ColumnSpec("data_delay", "str", source_key="mkt_data_delay"),
        ColumnSpec("open_time", "str", source_key="calc_start_time"),
        ColumnSpec("close_time", "str", source_key="calc_end_time"),
        ColumnSpec("time_zone", "str"),
        ColumnSpec("tick_days", "str"),
        ColumnSpec("tick_frequency", "str"),
        ColumnSpec("tick_period", "str"),
    ),
    scenario="指数目录发现：列出 cboe 发布的全部指数及其计算时段与延迟",
    error_prefix="CBOE",
    notes=(
        "The whole response document is the record list, so rows_pointer is empty. Upstream "
        "additionally drops every record whose ``source`` equals ``morningstar`` and pops "
        "``featured``/``featured_order``/``display`` before publishing; the ``source`` filter is a "
        "client-side selection and belongs to a bespoke fetcher, so this declaration publishes "
        "what the endpoint returns. Output-to-source key names follow the upstream "
        "``__alias_dict__`` direction and are not yet SOURCE_VERIFIED against a live response."
    ),
)

#: One European index's constituent quotes, addressed by symbol in the path.
INDEX_CONSTITUENTS = ModelSpec(
    model="IndexConstituents",
    domain="cboe_index_constituents",
    asset_class="index",
    period="snapshot",
    market="eu",
    base_url=CBOE_BASE_URL,
    path="/api/global/european_indices/constituent_quotes/{symbol}.json",
    rows_pointer="data",
    params=(
        ParamSpec(
            "symbol",
            "str",
            required=True,
            note="Constituent index id, addressed in the path; upstream restricts it to its "
            "European index list, which this declaration does not claim to enumerate.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", required=True),
        ColumnSpec("name", "str"),
        ColumnSpec("currency", "str"),
        ColumnSpec("security_type", "str"),
        ColumnSpec("last_price", "float", source_key="current_price", units="currency"),
        ColumnSpec("open", "float", units="currency"),
        ColumnSpec("high", "float", units="currency"),
        ColumnSpec("low", "float", units="currency"),
        ColumnSpec("close", "float", units="currency"),
        ColumnSpec("volume", "int", units="shares"),
        ColumnSpec("prev_close", "float", source_key="prev_day_close", units="currency"),
        ColumnSpec("change", "float", source_key="price_change", units="currency"),
        ColumnSpec(
            "change_percent",
            "float",
            source_key="price_change_percent",
            units="percent",
        ),
        ColumnSpec("tick", "str"),
        ColumnSpec("last_trade_time", "str", units="source format %Y-%m-%dT%H:%M:%S"),
        ColumnSpec("asset_type", "str", source_key="type"),
    ),
    scenario="成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
    error_prefix="CBOE",
    row_envelope=("symbol", "name", "currency"),
    notes=(
        "Upstream divides ``price_change_percent`` by 100 after the call, so this "
        "declaration keeps the published percent scale and lets the consumer decide. A "
        "value the source publishes as 0 is turned into a null upstream; the engine "
        "reports 0, which is the source fact. ``symbol``/``name``/``currency`` are "
        "response-level facts, used only where the constituent record publishes none."
    ),
)

#: Every upstream cboe model, and why the remaining nine are not declared here.
NOT_DECLARABLE: dict[str, str] = {
    "EquityHistorical": "pre-flight index directory decides the URL; date window filtered "
    "client-side after a full download",
    "EtfHistorical": "alias of EquityHistorical in the upstream package",
    "EquityQuote": "four requests joined on symbol; percent columns scaled after the call",
    "EquitySearch": "CSV download, not JSON",
    "FuturesCurve": "delegates to the historical and quote fetchers for a VX symbol list",
    "IndexHistorical": "interval branch selects between two URL roots and drops volume",
    "IndexSearch": "client-side substring search over the index directory",
    "IndexSnapshots": "region branch addresses two unrelated paths and drops different columns",
    "OptionsChains": "contract symbol parsed by regex, strike scaled by 1/1000, result published "
    "columnar rather than as rows",
}
