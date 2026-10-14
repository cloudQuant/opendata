"""Declarative Cboe index models and the remaining protocol evidence boundary.

The four index/directory ModelSpecs below remain executable through the declaration engine.
Handwritten typed adapters in ``models.all_access`` additionally implement EquityHistorical,
EtfHistorical, EquityQuote, EquitySearch, and OptionsChains from Cboe's documented All Access API.
``NOT_DECLARABLE`` names only the two fixed identities without a documented source protocol:
FuturesCurve and IndexSnapshots. A separate handwritten adapter handles the limited public
volatility-index subset of IndexHistorical.

The ``EQUITY_SEARCH`` ModelSpec is retained as an engine-format fixture for the existing delimited
decoder, but is no longer the provider binding. Runtime EquitySearch uses the documented All
Access symbol reference API and does not download the website CSV.
"""

from __future__ import annotations

from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    ParamSpec,
    RowFilterSpec,
)

#: Upstream base for the public ``cdn.cboe.com`` JSON API. No model sends a credential.
CBOE_BASE_URL = "https://cdn.cboe.com"

#: Upstream base for the ``www.cboe.com`` downloads. ``EquitySearch`` reads a CSV off this origin,
#: and no model sends a credential.
CBOE_WEBSITE_BASE_URL = "https://www.cboe.com"

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
        ColumnSpec("currency", "str"),
        ColumnSpec("description", "str"),
        ColumnSpec("data_delay", "int", source_key="mkt_data_delay", units="minutes"),
        ColumnSpec("open_time", "time", source_key="calc_start_time"),
        ColumnSpec("close_time", "time", source_key="calc_end_time"),
        ColumnSpec("time_zone", "str"),
        ColumnSpec("tick_days", "str"),
        ColumnSpec("tick_frequency", "str"),
        ColumnSpec("tick_period", "str"),
        ColumnSpec("exchange", "str"),
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
        "The directory row contract publishes data_delay as integer minutes and session bounds as "
        "datetime.time; its typed contract parses the fixture's minute-count text and local clock "
        "strings while preserving nulls. Cboe's public pages identify the feed but do not "
        "document these JSON fields' wire encodings; those source encodings remain unverified. "
        "Output-to-source key names follow the upstream public contract mapping and are not yet "
        "SOURCE_VERIFIED against an official raw response example. The columns are declared in the "
        "order ``CboeAvailableIndex`` publishes them -- ``exchange`` is that subclass's own field, "
        "so it is last -- because the row class IS the reviewed contract and its field order is "
        "part of the published shape. ``open_time``/``close_time`` are declared with the ``time`` "
        "kind because the row field is ``datetime.time`` and the contract parses local clock text "
        "(``08:30``) into it; a calendar-date kind is not what a session bound is, so the clock "
        "kind is the spelling the declaration now carries. A body publishing non-clock text for "
        "them is refused rather than guessed at."
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
        ColumnSpec(
            "last_trade_time",
            "date",
            units="ISO datetime text; official raw encoding unverified",
        ),
        ColumnSpec("asset_type", "str", source_key="type"),
    ),
    scenario="成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
    error_prefix="CBOE",
    row_envelope=("symbol", "name", "currency"),
    notes=(
        "The local contract parses last_trade_time to datetime and preserves explicit null, so the "
        "column is declared with the temporal kind rather than the text kind it carried before: "
        "the published ISO text is read as a moment, and benzinga's "
        "``ColumnSpec('last_updated', 'date', source_key='updated')`` over a ``datetime`` row "
        "field is this repo's precedent for that spelling. An unparseable published timestamp "
        "stays the refusal the pinned cboe provider test names it. "
        "The public Cboe contract describes change_percent as a normalized percentage, but Cboe's "
        "official public source material reviewed here does not establish the raw "
        "price_change_percent scale. This adapter therefore preserves the numeric source value "
        "without an unsupported scale conversion; percentage parity remains unverified. "
        "``symbol``/``name``/``currency`` are "
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
        ColumnSpec("data_delay", "int", source_key="mkt_data_delay", units="minutes"),
        ColumnSpec("open_time", "time", source_key="calc_start_time"),
        ColumnSpec("close_time", "time", source_key="calc_end_time"),
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
        "below match the document is likewise NOT yet checked against an official raw response "
        "example; the output value types follow the published public contract. The typed row "
        "parser converts a supported minute-count spelling into an integer and local session "
        "clock text into datetime.time; unsupported encodings fail closed. Two upstream facts are "
        "deliberately not declared: the "
        "``source == 'morningstar'`` records upstream discards (a filter on a key most rows do not "
        "publish would discard every row, and the published document is the source fact), and "
        "``use_cache``, which no declaration can express. ``query`` and ``is_symbol`` are "
        "client-side selection knobs, but the engine encodes every declared parameter into the "
        "request, so they ride the query string of a static JSON file that ignores them. The "
        "domain is registered on ``CboeSearchedIndex``, whose 11 fields are exactly the columns "
        "the pinned search model publishes -- it does not publish the directory's ``exchange``."
    ),
)

#: The US options company directory, published as a CSV download and searched client-side.
EQUITY_SEARCH = ModelSpec(
    model="EquitySearch",
    domain="cboe_equity_search",
    asset_class="equity",
    period="snapshot",
    market="us",
    base_url=CBOE_WEBSITE_BASE_URL,
    path="/us/options/symboldir/equity_index_options/",
    rows_pointer="",
    params=(
        ParamSpec(
            "query",
            "str",
            note="Search needle. Declared without a default so a query that omits it sends no "
            "needle at all; the declared filters then have nothing to select on, which is what "
            "upstream's empty default publishes -- the whole directory.",
        ),
        ParamSpec(
            "is_symbol",
            "bool",
            default=False,
            note="Which leg to take: True searches the listed symbols, False -- upstream's "
            "default -- searches the company names.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="Stock Symbol", required=True, units=""),
        ColumnSpec("name", "str", source_key="Company Name"),
        ColumnSpec("dpm_name", "str", source_key="DPM Name"),
        ColumnSpec("post_station", "str", source_key="Post/Station"),
    ),
    scenario="股票目录检索：下载 cboe 期权公司目录 CSV 并按代码或公司名筛选上市公司",
    error_prefix="CBOE",
    static_query=(("download", "csv"),),
    decoder=DecoderSpec(kind="csv"),
    row_filters=(
        RowFilterSpec(
            op="contains",
            columns=("symbol", "Stock Symbol"),
            value_param="query",
            when_param="is_symbol",
            when_value="True",
            ignore_case=True,
        ),
        RowFilterSpec(
            op="contains",
            columns=("name", "Company Name"),
            value_param="query",
            when_param="is_symbol",
            when_value="False",
            ignore_case=True,
        ),
    ),
    notes=(
        "The CSV body *is* the record list, so rows_pointer is empty and the declared decoder kind "
        "is csv. The path carries no query string: upstream's ``?download=csv`` is the payload "
        "format the page itself asks for, so it is declared as ``static_query`` -- a constant of "
        "the upstream code rather than part of its accepted query model -- and a caller cannot "
        "re-point this reviewed model at the HTML page by passing one. The two record keys each "
        "filter leg tests are published in BOTH spellings because the filter reads the record the "
        "decoder hands back while the normalized name is what this package publishes; a record "
        "that carries neither is a non-match, never a shape failure. ``symbol`` is required "
        "because upstream keys the whole directory on it, and the other three are its optional "
        "published columns. Three upstream facts are deliberately not declared: the "
        "``astype(str)`` cast and the NaN-to-null cleanup applied after the read -- the engine "
        "publishes the text the body carries -- and ``use_cache``, which no declaration can "
        "express. The declared source keys are the TRIMMED header spellings, not upstream's rename "
        "keys: ``_header_names`` strips every header cell and ``_row`` strips every cell, so "
        "upstream's leading-space keys -- ``' Stock Symbol'``, ``' DPM Name'``, "
        "``' Post/Station'`` -- are what pandas sees while the trimmed forms are what this engine "
        "sees, and a declaration copying upstream's spelling matches no column at all. Neither "
        "spelling is yet SOURCE_VERIFIED against a live body."
    ),
)

#: Fixed Cboe identities without a documented source protocol in this implementation.
NOT_DECLARABLE: dict[str, str] = {
    "FuturesCurve": "The reviewed official All Access docs do not establish the fixed model's "
    "underlying list, quote/history join, or curve output protocol.",
    "IndexSnapshots": "No official endpoint/response mapping for the fixed regional snapshot "
    "identity was evidenced in this slice.",
}
