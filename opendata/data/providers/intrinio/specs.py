"""Declarable intrinio provider models.

Thirty-eight intrinio rows were censused; four are declared here and all four survive the
declaration plane plus the runtime witness. They are single-GET, nested-key JSON endpoints, so
they are expressed as data (:class:`~opendata.data.providers._engine.spec.ModelSpec`). Their
capability needs are exactly the shipped vocabulary: ``columns.rename`` (a
``ColumnSpec.source_key``), ``rows.filter`` (the substring search on the pair directory, a
:class:`RowFilterSpec`), the parameter aliasing the census records as ``param.value_mapping``
(``limit`` sent as ``page_size``, a ``ParamSpec.query_key``), ``flow.url_template`` (the adjustment
feed's ``{symbol}`` path segment), ``columns.derive`` for the one column whose value no record
publishes (:data:`HISTORICAL_DIVIDENDS` stamps the queried symbol with a
:class:`~opendata.data.providers._engine.value_transform.DerivePlan`), and ``path.explode`` for
:data:`CALENDAR_IPO`, whose published scalars sit inside per-record ``company`` and ``security``
objects and are projected out by a
:class:`~opendata.data.providers._engine.row_expansion.RecordRepointPlan`.

``HistoricalDividends`` was withheld for one round for exactly that last reason. Upstream builds
every row as ``{"symbol": query.symbol, **item}`` (historical_dividends.py:102-104), so the value a
row must carry is a *query parameter*, and the engine's only carriers for a non-record value were
``row_envelope`` (spec.py:423, "Response keys copied onto every row as source facts" -- keys of the
enclosing document, unverified here because no body of this endpoint has ever been captured in this
repo) and the path placeholder, which addresses the request and never reaches the rows. Publishing
the column over those carriers would have made the offline witness certify ``symbol=None`` as the
contract. ``column_derives`` is the carrier that was missing, so the declaration is back.

Every interface fact below is taken from the cited upstream contract file as output-contract
comparison only: the host, path, row key, parameter names and output columns are restated as our
own declarations, and no upstream code, docstring or comment was copied.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.row_expansion import FieldMapping, RecordRepointPlan
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    ParamSpec,
    RowFilterSpec,
    ValueKind,
)
from opendata.data.providers._engine.value_transform import DerivePlan

#: Upstream base for the Intrinio Web API v2 JSON endpoints (:61 of the currency-pairs contract).
INTRINIO_BASE_URL = "https://api-v2.intrinio.com"

#: Environment variable holding the Web API key; every intrinio endpoint takes it as ``api_key``
#: in the query string (currency_pairs.py:62, equity_search.py:81, historical_dividends.py:93).
INTRINIO_CREDENTIAL = "INTRINIO_API_KEY"

#: The full forex pair directory, narrowed client-side by a free-text needle.
CURRENCY_PAIRS = ModelSpec(
    model="CurrencyPairs",
    domain="intrinio_currency_pairs",
    asset_class="currency",
    period="snapshot",
    market="all",
    base_url=INTRINIO_BASE_URL,
    path="/forex/pairs",
    rows_pointer="pairs",
    params=(
        ParamSpec(
            "query",
            "str",
            note="Free-text needle. Upstream never sends it (the endpoint is fetched with only "
            "the key, currency_pairs.py:62) and filters the rows client-side on "
            "code/base_currency/quote_currency substrings (:76-81); the engine encodes every "
            "declared parameter into the request, so it rides the query string of an endpoint "
            "that ignores it.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="code", required=True),
        ColumnSpec("name", "str"),
        ColumnSpec("base_currency", "str", required=True),
        ColumnSpec("quote_currency", "str", required=True),
    ),
    credential=INTRINIO_CREDENTIAL,
    credential_query_key="api_key",
    row_filters=(
        RowFilterSpec(
            op="contains",
            columns=("code", "base_currency", "quote_currency"),
            value_param="query",
            ignore_case=True,
            match="any",
        ),
    ),
    scenario="外汇对目录：列出 intrinio 发布的全部可交易货币对并按币种代码子串筛选",
    error_prefix="INTRINIO",
    notes=(
        "openbb_intrinio/models/currency_pairs.py:61-63,76-81,25,28-33 (contract only). One GET "
        "of /forex/pairs; rows are the 'pairs' list of the document (:63). The published symbol is "
        "renamed from the upstream code key (:25), and base_currency/quote_currency are required "
        "upstream fields (:28-33), so both are required here. name is published by the inherited "
        "standard model (standard_models/currency_pairs.py:21, str|None default None) and the "
        "provider alias dict renames only symbol<-code, "
        "so name keeps its record spelling; a record "
        "without it publishes null the same way upstream's "
        "own optional field does, which is why it "
        "is declared neither required nor renamed. The filter reads the raw record "
        "keys, hence the 'code' spelling. Upstream raises EmptyDataError when the document is "
        "empty; this declaration publishes the empty list the engine already allows, and refuses "
        "a document without the 'pairs' key as a shape failure."
    ),
)

#: The company search: one GET, rows under 'companies', output keys renamed.
EQUITY_SEARCH = ModelSpec(
    model="EquitySearch",
    domain="intrinio_equity_search",
    asset_class="equity",
    period="snapshot",
    market="us",
    base_url=INTRINIO_BASE_URL,
    path="/companies/search",
    rows_pointer="companies",
    params=(
        ParamSpec(
            "query",
            "str",
            note="Search needle, sent upstream: the request query string is the dumped query "
            "minus is_symbol (equity_search.py:79-81).",
        ),
        ParamSpec(
            "active",
            "bool",
            default=True,
            note="Upstream default: only companies actively traded in the past 14 days "
            "(equity_search.py:31-35).",
        ),
        ParamSpec(
            "limit",
            "int",
            query_key="page_size",
            default=10000,
            note="The census 'param.value_mapping' fact: the query limit is aliased to "
            "page_size with a default of 10000 (equity_search.py:26-38).",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="ticker", required=True),
        ColumnSpec("name", "str"),
        ColumnSpec("intrinio_id", "str", source_key="id", required=True),
        ColumnSpec("cik", "str"),
        ColumnSpec("lei", "str"),
    ),
    credential=INTRINIO_CREDENTIAL,
    credential_query_key="api_key",
    scenario="公司检索：在 intrinio 美股公司库中检索并返回改名后的 symbol 与 intrinio_id",
    error_prefix="INTRINIO",
    notes=(
        "openbb_intrinio/models/equity_search.py:43-46,50-54,79-82,90 (contract only). One GET of "
        "/companies/search, rows under 'companies' (:82,:90); no paging loop. The output renames "
        "id->intrinio_id and ticker->symbol (:43-46), which is the columns.rename face; name is "
        "published by the inherited standard model (standard_models/equity_search.py:24, str|None "
        "default None) and the provider alias dict "
        "(:45-48) renames only intrinio_id and symbol, so "
        "name keeps its record spelling and its optional null. is_symbol "
        "is excluded from the upstream querystring (:79) and is deliberately not declared here: "
        "the engine encodes every declared parameter into the request, so declaring it would send "
        "a parameter the reviewed contract does not, and a caller that passes it is refused at the "
        "query stage instead. cik/lei are optional upstream fields (:50-54)."
    ),
)


#: The dividend/split adjustment feed for one security: rows under 'stock_price_adjustments', with
#: the queried symbol stamped onto every row by a declared derive plan.
HISTORICAL_DIVIDENDS = ModelSpec(
    model="HistoricalDividends",
    domain="intrinio_historical_dividends",
    asset_class="equity",
    period="d",
    market="us",
    base_url=INTRINIO_BASE_URL,
    path="/securities/{symbol}/prices/adjustments",
    rows_pointer="stock_price_adjustments",
    params=(
        ParamSpec(
            "symbol",
            "str",
            required=True,
            note="Addresses the security in the path (:93); upstream excludes it from the query "
            "string (historical_dividends.py:92), which is what the engine's path-consumed rule "
            "does. required because a placeholder with no value cannot address a resource "
            "(spec.py:630-637).",
        ),
        ParamSpec(
            "start_date",
            "date",
            note="Window start, sent when the caller names it. Upstream fills a one-year default "
            "window in transform_query (historical_dividends.py:74-78) with a relative date this "
            "declaration vocabulary has no carrier for, so an omitted bound is simply not sent.",
        ),
        ParamSpec(
            "end_date",
            "date",
            note="Window end; same relative-default divergence as start_date.",
        ),
        ParamSpec(
            "limit",
            "int",
            query_key="page_size",
            default=100,
            note="param.value_mapping face: limit aliased to page_size with a default of 100 "
            "(historical_dividends.py:25-30).",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", required=True),
        ColumnSpec("ex_dividend_date", "date", source_key="date", required=True),
        ColumnSpec("amount", "float", source_key="dividend", required=True),
        ColumnSpec("currency", "str", source_key="dividend_currency"),
        ColumnSpec("factor", "float"),
        ColumnSpec("split_ratio", "float"),
    ),
    column_derives=(
        DerivePlan(
            column="symbol",
            source_kind="parameter",
            source="symbol",
            note="The record list carries no symbol key: upstream prepends the query's symbol to "
            "every item (historical_dividends.py:102-104), so this column is produced from the "
            "validated parameter rather than copied out of the record.",
        ),
    ),
    credential=INTRINIO_CREDENTIAL,
    credential_query_key="api_key",
    scenario="分红拆股：按证券代码取回该证券的除息日分红与拆股调整序列",
    error_prefix="INTRINIO",
    notes=(
        "openbb_intrinio/models/historical_dividends.py:91-95,102-104,36-40,25-30,42-56 (contract "
        "only). One GET of /securities/{symbol}/prices/adjustments; rows are the document's "
        "'stock_price_adjustments' list (:95) and there is no paging loop. The three renames "
        "date->ex_dividend_date, dividend->amount, dividend_currency->currency are the provider's "
        "own alias dict (:36-40). ex_dividend_date and amount are required in both readings -- the "
        "standard model declares them non-optional (standard_models/historical_dividends.py:38-41) "
        "and this declaration's first offline witness "
        "refused a record with no 'dividend' key, which "
        "is the same ValidationError upstream's own "
        "model_validate would raise on that record. So a "
        "split-only row is NOT a shape this contract accepts; whether the endpoint publishes a "
        "dividend value on every adjustment kind is "
        "unverified, because no body of this endpoint has "
        "ever been captured in this repo. currency/factor/split_ratio are the provider's optional "
        "fields (:42-56). symbol is the derived column (column_derives) and is declared required "
        "because the parameter is required: a stamp of "
        "None means no symbol was asked for, and that "
        "request is refused before any send. "
        "DIVERGENCE, recorded rather than papered over: upstream case-folds the queried symbol "
        "(standard_models/historical_dividends.py:25-29, "
        "a to_upper field validator) and this engine "
        "has no parameter-normalization carrier, so a "
        "caller who passes 'aapl' addresses a lowercase "
        "path where upstream addresses 'AAPL'. That is "
        "param.value_transform work, not a rename; the "
        "column value it stamps is the caller's own spelling either way."
    ),
)


#: ``(output column, kind, record pointer)`` triples for the IPO calendar. The first twenty are
#: the item's own keys -- ``symbol``/``ipo_date`` read the raw ``ticker``/``date`` the provider
#: alias dict renames (calendar_ipo.py:48) -- and the rest descend into the per-record ``company``
#: and ``security`` objects (:149-155, whose scalar keys are IntrinioCompany and IntrinioSecurity
#: in openbb_intrinio/utils/references.py). A pointer with a dot is re-resolved once per record by
#: the repoint kernel, which is the whole ``path.explode`` need of census row
#: OBB2-intrinio-CalendarIpo.
CALENDAR_IPO_FIELDS: Final[tuple[tuple[str, ValueKind, str], ...]] = (
    ("symbol", "str", "ticker"),
    ("ipo_date", "date", "date"),
    ("status", "str", "status"),
    ("exchange", "str", "exchange"),
    ("offer_amount", "float", "offer_amount"),
    ("share_price", "float", "share_price"),
    ("share_price_lowest", "float", "share_price_lowest"),
    ("share_price_highest", "float", "share_price_highest"),
    ("share_count", "int", "share_count"),
    ("share_count_lowest", "int", "share_count_lowest"),
    ("share_count_highest", "int", "share_count_highest"),
    ("announcement_url", "str", "announcement_url"),
    ("sec_report_url", "str", "sec_report_url"),
    ("open_price", "float", "open_price"),
    ("close_price", "float", "close_price"),
    ("volume", "int", "volume"),
    ("day_change", "float", "day_change"),
    ("week_change", "float", "week_change"),
    ("month_change", "float", "month_change"),
    ("id", "str", "id"),
    ("company_id", "str", "company.id"),
    ("company_ticker", "str", "company.ticker"),
    ("company_name", "str", "company.name"),
    ("company_lei", "str", "company.lei"),
    ("company_cik", "str", "company.cik"),
    ("security_id", "str", "security.id"),
    ("security_name", "str", "security.name"),
    ("security_code", "str", "security.code"),
    ("security_currency", "str", "security.currency"),
    ("security_ticker", "str", "security.ticker"),
    ("security_composite_ticker", "str", "security.composite_ticker"),
    ("security_figi", "str", "security.figi"),
    ("security_composite_figi", "str", "security.composite_figi"),
    ("security_share_class_figi", "str", "security.share_class_figi"),
    ("security_primary_listing", "bool", "security.primary_listing"),
)

#: The IPO calendar: one GET, rows under 'initial_public_offerings', every published scalar
#: projected through a per-record pointer so the nested company/security facts reach the row flat.
CALENDAR_IPO = ModelSpec(
    model="CalendarIpo",
    domain="intrinio_calendar_ipo",
    asset_class="equity",
    period="d",
    market="us",
    base_url=INTRINIO_BASE_URL,
    path="/companies/ipos",
    rows_pointer="initial_public_offerings",
    params=(
        ParamSpec(
            "symbol",
            "str",
            query_key="ticker",
            note="Alias fact of the census row: the search symbol is sent as ticker "
            "(calendar_ipo.py:26).",
        ),
        ParamSpec(
            "status",
            "enum",
            enum=("upcoming", "priced", "withdrawn"),
            note="IPO status; upstream spells the three values verbatim on the wire "
            "(calendar_ipo.py:32-34), so no value_map restatement is declared.",
        ),
        ParamSpec("start_date", "date", note="Window start, sent verbatim (:178)."),
        ParamSpec("end_date", "date", note="Window end, sent verbatim (:178)."),
        ParamSpec(
            "min_value",
            "int",
            query_key="offer_amount_greater_than",
            note="Alias: offer floor (calendar_ipo.py:28).",
        ),
        ParamSpec(
            "max_value",
            "int",
            query_key="offer_amount_less_than",
            note="Alias: offer ceiling (calendar_ipo.py:29).",
        ),
        ParamSpec(
            "limit",
            "int",
            query_key="page_size",
            default=100,
            note="Alias: limit sent as page_size; page_size only caps the page, there is no "
            "next-page loop (calendar_ipo.py:27,181-183). Default 100 is the inherited standard "
            "model's own (standard_models/calendar_ipo.py:27-30).",
        ),
    ),
    columns=tuple(ColumnSpec(name, kind) for name, kind, _ in CALENDAR_IPO_FIELDS),
    row_expansion=(
        RecordRepointPlan(
            records_pointer="",
            fields=tuple(
                FieldMapping(name, pointer, required=False)
                for name, _, pointer in CALENDAR_IPO_FIELDS
            ),
        ),
    ),
    credential=INTRINIO_CREDENTIAL,
    credential_query_key="api_key",
    scenario="IPO日历：按状态与发行规模取回新股发行日历并展平嵌套公司证券字段",
    error_prefix="INTRINIO",
    notes=(
        "openbb_intrinio/models/calendar_ipo.py:177-183,25-30,48,149-155 and "
        "openbb_intrinio/utils/references.py IntrinioCompany/IntrinioSecurity (contract only). One "
        "GET of /companies/ipos; rows are the document's initial_public_offerings list (:183). "
        "Every repoint field is required=False because every upstream field is optional with "
        "default None -- including symbol/ipo_date (standard_models/calendar_ipo.py:36-45) -- so a "
        "record that publishes no company object answers with null company columns, mirroring the "
        "Optional[IntrinioCompany] field. security.company_id is not declared: it repeats "
        "company_id one level down. DIVERGENCE, recorded: upstream's contract carries company and "
        "security as nested objects, and this package's row model flattens their scalars -- that "
        "projection is the census row's path.explode need, and no object-valued column kind exists "
        "to keep. Upstream raises EmptyDataError on an empty list (:190-191); the repoint kernel "
        "refuses the same answer with ROW_EXPANSION_EMPTY_RESULT, so this model does NOT publish "
        "the empty list the CurrencyPairs declaration allows -- the refusal is the closer reading "
        "of the cited contract. The domain intrinio_calendar_ipo is not yet in domains.yaml, which "
        "this package may not edit, so build_row_model synthesizes CalendarIpoRow rather than "
        "publishing a reviewed contract class."
    ),
)


#: The census rows intrinio's own package puts forward and the declaration plane refuses, each with
#: the capability that blocks it. Empty this round: the one row it carried, ``HistoricalDividends``,
#: was blocked on ``columns.derive``, that capability landed with ``ModelSpec.column_derives``, and
#: the model is now declared above. Kept as a ledger rather than deleted so a future refusal has a
#: documented home and the row count of withheld models stays measurable.
NOT_DECLARABLE: dict[str, str] = {}
