"""Declarable cboe provider models.

Three of cboe's eleven upstream models are single-GET, JSON, path-addressed endpoints and are
declared here as data (:class:`~opendata.data.providers._engine.spec.ModelSpec`). The other eight
need composition the declaration format cannot express -- a pre-flight directory lookup, a
four-request join, CSV rather than JSON, delegation to another fetcher, or regex parsing of an
option contract symbol -- and are recorded in ``NOT_DECLARABLE`` below rather than silently
dropped from the task count.

The third one is a *search*: it reads the same directory document ``AvailableIndices`` reads and
keeps the rows whose symbol, name or description contains the caller's needle. That selection is
declared with :class:`~opendata.data.providers._engine.spec.RowFilterSpec`, so the engine no longer
has to hand-write a fetcher for it.
"""

from __future__ import annotations

from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    ParamSpec,
    RowFilterSpec,
)

#: Upstream base for the public ``cdn.cboe.com`` JSON API. No model sends a credential.
CBOE_BASE_URL = "https://cdn.cboe.com"

#: The index directory, which is also the pre-flight other cboe models consult.
AVAILABLE_INDICES = ModelSpec(
    model="AvailableIndices",
    domain="cboe_available_indices",
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
        "``featured``/``featured_order``/``display`` before publishing. The engine can declare "
        "such a client-side selection now (:class:`RowFilterSpec`, and ``IndexSearch`` below does "
        "declare one), but this catalogue publishes what the endpoint returns, and a filter on a "
        "``source`` key the document does not always carry would discard the rows that omit it. "
        "Output-to-source key names follow the upstream ``__alias_dict__`` direction and are not "
        "yet SOURCE_VERIFIED against a live response."
    ),
)

#: One European index's constituent quotes, addressed by symbol in the path.
INDEX_CONSTITUENTS = ModelSpec(
    model="IndexConstituents",
    domain="cboe_index_constituent_quotes",
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

#: The same index directory, searched: one GET of the published document plus the two client-side
#: selections upstream applies to it. This is the model that needed a filter predicate the
#: declaration format could not express.
INDEX_SEARCH = ModelSpec(
    model="IndexSearch",
    domain="cboe_index_search",
    asset_class="index",
    period="snapshot",
    market="us",
    base_url=CBOE_BASE_URL,
    path="/api/global/us_indices/definitions/all_indices.json",
    rows_pointer="",
    params=(
        ParamSpec(
            "query",
            "str",
            note="Search needle. Declared without a default so a query that omits it sends no "
            "needle at all; the engine then reads the filter as having nothing to select on and "
            "publishes the whole directory, which is what upstream's empty default does.",
        ),
        ParamSpec(
            "is_symbol",
            "bool",
            default=False,
            note="Which branch to take: True searches index symbols only, False -- upstream's "
            "default -- searches name, symbol and description.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="index_symbol", required=True, units=""),
        ColumnSpec("name", "str", source_key="index_name", required=True),
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
    scenario="指数检索：在 cboe 美国指数目录中按代码或名称与描述筛选指数",
    error_prefix="CBOE",
    row_filters=(
        RowFilterSpec(
            op="contains",
            columns=("index_symbol",),
            value_param="query",
            when_param="is_symbol",
            when_value="True",
            ignore_case=True,
        ),
        RowFilterSpec(
            op="contains",
            columns=("name", "index_name", "index_symbol", "description"),
            value_param="query",
            when_param="is_symbol",
            when_value="False",
            ignore_case=True,
            match="any",
        ),
    ),
    notes=(
        "Upstream reads this same document, drops its ``source`` column and then keeps either the "
        "rows whose ``index_symbol`` contains the needle (``is_symbol`` true) or the rows whose "
        "name, ``index_symbol`` or ``description`` contains it -- case-insensitively in both "
        "branches, which is what ``ignore_case=True`` declares here. The two ``columns`` tuples "
        "are the RECORD keys tested, read before normalization, so they are not the output names: "
        "a row whose tested key is absent is a non-match, never a shape failure. The name leg is "
        "declared in BOTH spellings the sources use -- upstream's ``else`` branch reads ``name``, "
        "while this package's ``AvailableIndices`` maps ``index_name`` -- because no live response "
        "has settled which key the directory publishes, and a search that could not match on an "
        "index's name would not be a search. Whether the published column names and scales "
        "below match the document is likewise NOT yet checked against a live response; they "
        "follow the upstream source reading. Two upstream facts are deliberately not declared: the "
        "``source == 'morningstar'`` records upstream discards (a filter on a key most rows do not "
        "publish would discard every row, and the published document is the source fact), and "
        "``use_cache``, which no declaration can express. ``query`` and ``is_symbol`` are "
        "client-side selection knobs, but the engine encodes every declared parameter into the "
        "request, so they ride the query string of a static JSON file that ignores them. The "
        "domain is registered on ``CboeSearchedIndex``, whose 11 fields are exactly the columns "
        "the pinned search model publishes -- it does not publish the directory's ``exchange``."
    ),
)

#: Every upstream cboe model, and why the remaining eight are not declared here.
NOT_DECLARABLE: dict[str, str] = {
    "EquityHistorical": "pre-flight index directory decides the URL; date window filtered "
    "client-side after a full download",
    "EtfHistorical": "alias of EquityHistorical in the upstream package",
    "EquityQuote": "four requests joined on symbol; percent columns scaled after the call",
    "EquitySearch": "CSV download, not JSON",
    "FuturesCurve": "delegates to the historical and quote fetchers for a VX symbol list",
    "IndexHistorical": "interval branch selects between two URL roots and drops volume",
    "IndexSnapshots": "region branch addresses two unrelated paths and drops different columns",
    "OptionsChains": "contract symbol parsed by regex, strike scaled by 1/1000, result published "
    "columnar rather than as rows",
}
