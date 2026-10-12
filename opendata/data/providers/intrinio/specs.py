"""Declarable intrinio provider models.

Thirty-eight intrinio rows were censused; five are declared here. Four survive the declaration plane
plus the engine's synthetic witness; the fifth, :data:`INSIDER_TRADING`, survives both planes, a
hand-written offline witness of its own body and, since C98, the engine's synthetic witness too --
child-array explosion is now a vocabulary :mod:`opendata.data.providers._engine.testing` scripts,
which its notes record as a closed fixture gap rather than a declaration gap.
They are single-GET, nested-key JSON endpoints, so
they are expressed as data (:class:`~opendata.data.providers._engine.spec.ModelSpec`). Their
capability needs are exactly the shipped vocabulary: ``columns.rename`` (a
``ColumnSpec.source_key``), ``rows.filter`` (the substring search on the pair directory, a
:class:`RowFilterSpec`), the parameter aliasing the census records as ``param.value_mapping``
(``limit`` sent as ``page_size``, a ``ParamSpec.query_key``), ``flow.url_template`` (the adjustment
feed's ``{symbol}`` path segment), ``columns.derive`` for the one column whose value no record
publishes (:data:`HISTORICAL_DIVIDENDS` stamps the queried symbol with a
:class:`~opendata.data.providers._engine.value_transform.DerivePlan`), ``path.explode`` twice over
-- :data:`CALENDAR_IPO` projects its published scalars out of each record's ``company`` and
``security`` object with a
:class:`~opendata.data.providers._engine.row_expansion.RecordRepointPlan`, and
:data:`INSIDER_TRADING` explodes each filing's ``transactions`` array into one row per transaction
with the filing-level scalars copied down by a
:class:`~opendata.data.providers._engine.row_expansion.NestedArrayExpansion` ``parent_fields`` --
``columns.null_sentinel`` for the ``''`` spellings :data:`INSIDER_TRADING` nulls on 28 of its 29
columns (the kernel C97 shipped), and
``param.relative_default`` for the window bounds the two declarations fill from the request date
when the caller names none (``RelativeDefault``, since C96).

The remaining 33 rows were read upstream, endpoint by endpoint, and re-measured against the same
two planes -- the ``ModelSpec`` field list and what ``http_json`` performs at query time. None of
them is declarable today, so this round adds one module and one binding; :data:`NOT_DECLARABLE`
carries each refusal with the canonical capability that blocks it, the mechanism and the line that
shows it. Counted by the capability a refusal names first: ``flow.fan_out`` 14 (a request per
comma-split symbol, per tag, or per id read off an earlier response), ``flow.branch`` 8 (a path,
rows key or sort column chosen by an optional parameter), ``param.value_transform`` 3,
``flow.preflight_lookup`` 3 (a response publishes the URL the rows live at), ``flow.join`` 2,
``derive.conditional`` 1, ``rows.cap_source_order`` 1 and 1 row whose
need -- de-duplicating across pages, then ordering and capping the assembled set -- no canonical
names.
Counted by every capability a refusal mentions, ``flow.fan_out`` appears 23 times and
``flow.branch`` 8 and ``columns.null_sentinel`` 7 times each. The two kernels that would clear most
of it, ``_engine/multi_request.py`` and ``_engine/request_body.py``, exist as pure modules with no
``ModelSpec`` field pointing at them, so a declaration cannot reach them from here.

``InsiderTrading`` was refused for one round on ``columns.null_sentinel`` alone, and that refusal
named the rest of the row as declarable. C97 shipped the sentinel facet, so the row was re-judged
against the two planes and declared: 28 of its 29 columns carry ``('',)``, matched on the published
text before any cast, which is what the upstream ``model_validator`` does (insider_trading.py:
109-117) and what keeps a genuine ``0`` or ``False`` a value. The one column that cannot carry it,
``company_name``, is the provider's only non-optional field, and a sentinel on a non-nullable column
is refused by the declaration plane (spec.py:393-397); the divergence that leaves is recorded in the
declaration's notes rather than papered over.

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

from opendata.data.providers._engine.row_expansion import (
    FieldMapping,
    NestedArrayExpansion,
    RecordRepointPlan,
)
from opendata.data.providers._engine.spec import (
    ColumnSpec,
    ModelSpec,
    ParamSpec,
    RelativeDefault,
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
            relative_default=RelativeDefault(years=-1),
            note="Window start, sent when the caller names it and filled with a one-year lookback "
            "when they do not: upstream computes ``now - relativedelta(year=1)`` in "
            "transform_query (historical_dividends.py:74-76), and RelativeDefault(years=-1) is the "
            "carrier for that bound. Before this field existed the omission sent no window at "
            "all, which answered wider than upstream.",
        ),
        ParamSpec(
            "end_date",
            "date",
            relative_default=RelativeDefault(),
            note="Window end: upstream's bound is the request date itself (historical_dividends.py"
            ":77-78), which is the all-zero RelativeDefault -- a bound of zero offset, not an "
            "absent one. Both bounds are resolved against one clock reading per fetch, so the "
            "window cannot straddle midnight and disagree with itself.",
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
        "column value it stamps is the caller's own spelling either way. The window half of the "
        "row's needs is closed: both bounds carry a RelativeDefault now, so an omitted start_date "
        "sends the request date minus a year and an omitted end_date sends the request date, which "
        "is what the cited transform_query sends."
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
        "of the cited contract. The domain intrinio_calendar_ipo has been registered in "
        "domains.yaml since 083b7e4 with declared semantics (event / time_field=ipo_date / "
        "natural_key=[symbol, ipo_date] / filter_dims=[symbol, ipo_date, status] / "
        "storage_mode=transient / permissions=[query]) and its reviewed contract "
        "IntrinioPublicOffering, so build_row_model publishes that class, not a CalendarIpoRow "
        "look-alike -- measured on this spec: 35 contract fields against 35 declared columns, "
        "empty symmetric difference in both directions. The sentence this note carried until C98 "
        "claimed the opposite (that the domain was absent from domains.yaml and the row model was "
        "synthesized), and it had been false since the commit that shipped the row itself."
    ),
)


#: The seven scalars one insider-trading filing publishes at its own level, as
#: ``(row key, record pointer)`` pairs. Upstream copies each of them onto every transaction row it
#: explodes (insider_trading.py:162-179), indexing them directly -- a filing that omits one raises
#: there -- so every one is read as required here. ``owner_cik``/``owner_name`` sit inside the
#: filing's ``owner`` object and reach the row through the dotted pointer grammar the kernel walks.
INSIDER_TRADING_FILING_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("issuer_ticker", "issuer_ticker"),
    ("issuer_cik", "issuer_cik"),
    ("issuer_company", "issuer_company"),
    ("filing_date", "filing_date"),
    ("filing_url", "filing_url"),
    ("owner_cik", "owner.owner_cik"),
    ("owner_name", "owner.owner_name"),
)

#: The keys one ``transactions[]`` item publishes for the columns this model carries. Upstream
#: spreads the whole sub-item onto the row (:168) and every field its model declares is optional
#: with a ``None`` default (standard_models/insider_trading.py:39-107, insider_trading.py:60-107),
#: so each is read required=False: an item that omits a key answers null, not refusal.
INSIDER_TRADING_TRANSACTION_FIELDS: Final[tuple[str, ...]] = (
    "transaction_date",
    "ownership_type",
    "officer_title",
    "transaction_type_code",
    "acquisition_disposition_code",
    "security_title",
    "total_shares_owned",
    "amount_of_shares",
    "transaction_price",
    "conversion_exercise_price",
    "deemed_execution_date",
    "exercise_date",
    "expiration_date",
    "underlying_security_title",
    "underlying_shares",
    "nature_of_ownership",
    "director",
    "officer",
    "ten_percent_owner",
    "other_relation",
    "derivative_transaction",
    "report_line_number",
)

#: The one spelling this model's before-validator reads as no value: the empty string
#: (insider_trading.py:109-117 rewrites ``v == ""`` to ``None`` for every key of the assembled row).
EMPTY_TEXT: Final[tuple[str, ...]] = ("",)

#: The insider-trading filings of one company: one GET, the document's ``transaction_filings`` list
#: exploded per ``transactions[]`` item, the filing's own scalars copied onto every child row and
#: the six renamed transaction keys read out of the record spelling the provider aliases.
INSIDER_TRADING = ModelSpec(
    model="InsiderTrading",
    domain="intrinio_insider_trading",
    asset_class="equity",
    period="d",
    market="us",
    base_url=INTRINIO_BASE_URL,
    path="/companies/{symbol}/insider_transaction_filings",
    rows_pointer="transaction_filings",
    params=(
        ParamSpec(
            "symbol",
            "str",
            required=True,
            note="Addresses the company in the path (insider_trading.py:153-155); upstream drops "
            "it from the query string (:152), which is the engine's path-consumed rule.",
        ),
        ParamSpec(
            "start_date",
            "date",
            relative_default=RelativeDefault(years=-5),
            note="Window start: upstream fills an omitted bound with ``now - relativedelta("
            "years=5)`` (:133-137), the five-year default this ledger named as declarable once a "
            "``RelativeDefault`` resolved one.",
        ),
        ParamSpec(
            "end_date",
            "date",
            relative_default=RelativeDefault(),
            note="Window end: the request date itself (:136-137), the all-zero RelativeDefault.",
        ),
        ParamSpec(
            "ownership_type",
            "enum",
            enum=("D", "I"),
            note="Direct versus indirect ownership; upstream spells the two letters as-is on the "
            "wire (:37-40), so no value_map restatement is declared.",
        ),
        ParamSpec(
            "sort_by",
            "enum",
            enum=("filing_date", "updated_on"),
            default="updated_on",
            note="Upstream's own default (:41-44); the sort happens on the server, and this "
            "declaration asks for no client-side ``order_by`` over the exploded rows.",
        ),
        ParamSpec(
            "limit",
            "int",
            query_key="page_size",
            default=500,
            note="param.value_mapping face: limit aliased to page_size (:29), and upstream sends "
            "500 when the caller names none (:151). page_size caps the page; there is no next-page "
            "loop to declare, so ``pagination`` stays at its none default.",
        ),
    ),
    columns=(
        ColumnSpec(
            "symbol",
            "str",
            source_key="issuer_ticker",
            required=True,
            null_sentinel=EMPTY_TEXT,
        ),
        ColumnSpec(
            "company_cik",
            "str",
            source_key="issuer_cik",
            required=True,
            null_sentinel=EMPTY_TEXT,
        ),
        # The one non-optional field of the provider's own model (:59), so it carries no sentinel:
        # rewriting ``''`` to None would produce exactly the null the row model refuses.
        ColumnSpec(
            "company_name", "str", source_key="issuer_company", required=True, nullable=False
        ),
        ColumnSpec("filing_date", "date", required=True, null_sentinel=EMPTY_TEXT),
        ColumnSpec("filing_url", "str", required=True, null_sentinel=EMPTY_TEXT),
        ColumnSpec("owner_cik", "str", required=True, null_sentinel=EMPTY_TEXT),
        ColumnSpec("owner_name", "str", required=True, null_sentinel=EMPTY_TEXT),
        ColumnSpec("transaction_date", "date", null_sentinel=EMPTY_TEXT),
        ColumnSpec("ownership_type", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("owner_title", "str", source_key="officer_title", null_sentinel=EMPTY_TEXT),
        ColumnSpec(
            "transaction_type",
            "str",
            source_key="transaction_type_code",
            null_sentinel=EMPTY_TEXT,
        ),
        ColumnSpec(
            "acquisition_or_disposition",
            "str",
            source_key="acquisition_disposition_code",
            null_sentinel=EMPTY_TEXT,
        ),
        ColumnSpec("security_type", "str", source_key="security_title", null_sentinel=EMPTY_TEXT),
        ColumnSpec(
            "securities_owned", "float", source_key="total_shares_owned", null_sentinel=EMPTY_TEXT
        ),
        ColumnSpec(
            "securities_transacted",
            "float",
            source_key="amount_of_shares",
            null_sentinel=EMPTY_TEXT,
        ),
        ColumnSpec("transaction_price", "float", null_sentinel=EMPTY_TEXT, units="usd"),
        ColumnSpec("conversion_exercise_price", "float", null_sentinel=EMPTY_TEXT, units="usd"),
        ColumnSpec("deemed_execution_date", "date", null_sentinel=EMPTY_TEXT),
        ColumnSpec("exercise_date", "date", null_sentinel=EMPTY_TEXT),
        ColumnSpec("expiration_date", "date", null_sentinel=EMPTY_TEXT),
        ColumnSpec("underlying_security_title", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("underlying_shares", "float", null_sentinel=EMPTY_TEXT),
        ColumnSpec("nature_of_ownership", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("director", "bool", null_sentinel=EMPTY_TEXT),
        ColumnSpec("officer", "bool", null_sentinel=EMPTY_TEXT),
        ColumnSpec("ten_percent_owner", "bool", null_sentinel=EMPTY_TEXT),
        ColumnSpec("other_relation", "bool", null_sentinel=EMPTY_TEXT),
        ColumnSpec("derivative_transaction", "bool", null_sentinel=EMPTY_TEXT),
        ColumnSpec("report_line_number", "int", null_sentinel=EMPTY_TEXT),
    ),
    row_expansion=(
        NestedArrayExpansion(
            records_pointer="",
            child_array_pointer="transactions",
            child_fields=tuple(
                FieldMapping(key, key, required=False) for key in INSIDER_TRADING_TRANSACTION_FIELDS
            ),
            parent_fields=tuple(
                FieldMapping(key, pointer) for key, pointer in INSIDER_TRADING_FILING_FIELDS
            ),
        ),
    ),
    credential=INTRINIO_CREDENTIAL,
    credential_query_key="api_key",
    scenario="内部人交易：按公司代码取回内幕交易申报，并把每笔申报的交易逐条展开成行",
    error_prefix="INTRINIO",
    notes=(
        "openbb_intrinio/models/insider_trading.py:29,31-44,50-57,59-107,109-117,133-137,150-155,"
        "162-181 (contract only). One GET of /companies/{symbol}/insider_transaction_filings; rows "
        "are the document's transaction_filings list (:155) and there is no paging loop -- "
        "page_size only caps the page (:29,:151). Each filing's transactions array is exploded "
        "into one row per transaction and the filing-level scalars are copied onto every sub-row, "
        "which is nested_array_explode and is carried by the NestedArrayExpansion plan: its "
        "parent_fields are read once per filing and re-published on each child row "
        "(row_expansion.py:420-435), and its dotted pointers are how owner.owner_cik and "
        "owner.owner_name reach the flat row (transform_data :174-175). The six renames "
        "officer_title->owner_title, transaction_type_code->transaction_type, "
        "acquisition_disposition_code->acquisition_or_disposition, security_title->security_type, "
        "total_shares_owned->securities_owned and amount_of_shares->securities_transacted are the "
        "provider's own alias dict (:50-57); symbol, company_cik and company_name are renamed off "
        "the filing (:171-173). columns.null_sentinel is the blocker this ledger carried for one "
        "round and C97 shipped: the model_validator at :109-117 rewrites every '' of the assembled "
        "row to None before any coercion, so 28 of the 29 columns -- every column but the "
        "non-optional company_name -- declare ('',) and are matched on the published text ahead of "
        "the cast (http_json.py:553-575,:628-630). A published 0 or False is not that text, so "
        "securities_transacted=0 and derivative_transaction=False stay themselves. DIVERGENCE, "
        "recorded: company_name is non-nullable, so an issuer_company published as '' reaches the "
        "row as the empty string where upstream's validator turns it into None and pydantic turns "
        "that into a refusal; a sentinel on a non-nullable column is refused by the declaration "
        "plane itself (spec.py:393-397) and this package weakens neither reading to close the gap. "
        "Upstream also spreads every other key of the sub-item onto the row (:168); the child "
        "fields name only the columns this contract publishes, so an undeclared transaction key is "
        "dropped rather than reaching the row, which is what the engine's extra-forbid row model "
        "refuses anyway. The standard model coerces numbers to strings for company_cik and admits "
        "int|str for owner_cik (standard_models/insider_trading.py:46-49,"
        "insider_trading.py:62-64), and this engine's str column is a strict str, so a numeric CIK "
        "is a shape failure here where upstream would stringify it. C98 registered the domain "
        "intrinio_insider_trading in domains.yaml with all six semantics fields (event / "
        "time_field=filing_date / natural_key=[symbol, filing_date, owner_cik, "
        "report_line_number] / filter_dims=[symbol, company_cik, owner_cik, filing_date] / "
        "storage_mode=transient / permissions=[query]) and bound the reviewed contract "
        "IntrinioInsiderTransaction, so build_row_model publishes that class instead of "
        "synthesizing an InsiderTradingRow look-alike -- measured on this spec: the contract's 29 "
        "fields against the 29 declared columns leave an empty symmetric difference in both "
        "directions. No body of this endpoint has ever been captured in this repo, so the row keys "
        "rest on the cited contract, exactly as HISTORICAL_DIVIDENDS' do. "
        "GAP CLOSED, C98, by measurement rather than by a relaxed arm: "
        "witness.nested_array_body_synthesis was a fixture gap and the shared offline witness "
        "now speaks the explode. _engine/testing.py reads a plan's child_array_pointer: raw_key "
        "spells a child column as ``transactions.<pointer>`` off "
        "NestedArrayExpansion.child_fields and a parent column as its own pointer, "
        "synthetic_record scripts one child item per filing record, damage_record_field walks the "
        "same prefix so a falsifier edits the cell the expansion actually reads, and "
        "absent_key_reads_as_null tells a required pointer (published or refused) from an "
        "optional one (absent reads as a null). The judge's half was the refusal layer: a required "
        "child or parent key is refused by expand_nested_array as ROW_EXPANSION_MISSING_POINTER "
        "naming the POINTER, while an optional one survives the expansion and is refused by "
        "normalize_record as INTRINIO_SHAPE_INVALID naming the COLUMN, so the contract suite's two "
        "parallel if-chains became one predicate (expansion_refuses_before_the_normalizer) that "
        "chooses code and named face together. Face, measured three times on this row: pytest "
        "tests/test_provider_model_contracts.py -k InsiderTrading was 24 failed, 5 passed, "
        "6 skipped, moved to 13 failed, 16 passed, 6 skipped when valid_query_kwargs learned its "
        "enum branch, and is now 29 passed, 6 skipped; suite-wide 542 passed, 165 skipped. "
        "Invariance, measured on the same edit: over the 37 "
        "required-column-with-a-published-key cells the two cases judge across 18 models, exactly "
        "7 change face and all 7 are this row's (symbol, company_cik, company_name, filing_date, "
        "filing_url, owner_cik, owner_name); the count outside this model is 0, and the only "
        "models with any kernel-refused cell are this one (7) and "
        "federal_reserve::SvenssonYieldCurve (1, unchanged). Nothing in this declaration, its "
        "column keys or its sentinels moved to reach that face, and the hand-written two-filing "
        "witness tests/test_intrinio_insider_trading_spec.py still witnesses that the spec plane "
        "expresses the explode, the sentinel, the renames and the window and that the runtime "
        "reads them. C98's registration also moved this row's catalog face: it no longer lands in "
        "the metadata-only branch (data_query.py:316) but in the provider_query branch carrying "
        "reason=transient_model (:358-364), because a declared DomainSpec is what puts a domain in "
        "``provider_query_domains`` (:271-275). The parameter face is the other half of the same "
        "round: the catalog reads ``get_type_hints(type(fetcher).transform_query)['return']`` "
        "(:473), which advertised nothing for any engine-generated model until C98 put the "
        "generated query model on that annotation (http_json.py:1303). Measured here, "
        "``InsiderTradingQuery`` publishes the six declared params (symbol, start_date, end_date, "
        "ownership_type, sort_by, limit)."
    ),
)


#: The 38 intrinio model identities the iteration-2 census records (ledger ids
#: ``OBB2-intrinio-<model>``, ``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/
#: census-intrinio-{1,2,3}of3.json``), in dictionary order. ``EtfHistorical`` is a second key on
#: ``IntrinioEquityHistoricalFetcher`` (openbb_intrinio/__init__.py:83), so it counts as its own row
#: and inherits that fetcher's blockers.
UPSTREAM_ROWS: Final[tuple[str, ...]] = (
    "BalanceSheet",
    "CalendarIpo",
    "CashFlowStatement",
    "CompanyFilings",
    "CompanyNews",
    "CurrencyPairs",
    "EquityHistorical",
    "EquityInfo",
    "EquityQuote",
    "EquitySearch",
    "EtfHistorical",
    "EtfHoldings",
    "EtfInfo",
    "EtfPricePerformance",
    "EtfSearch",
    "FinancialRatios",
    "ForwardEbitdaEstimates",
    "ForwardEpsEstimates",
    "ForwardPeEstimates",
    "ForwardSalesEstimates",
    "FredSeries",
    "HistoricalAttributes",
    "HistoricalDividends",
    "HistoricalMarketCap",
    "IncomeStatement",
    "IndexHistorical",
    "InsiderTrading",
    "KeyMetrics",
    "LatestAttributes",
    "MarketSnapshots",
    "OptionsChains",
    "OptionsSnapshots",
    "OptionsUnusual",
    "PriceTargetConsensus",
    "ReportedFinancials",
    "SearchAttributes",
    "ShareStatistics",
    "WorldNews",
)

#: The five rows this package declares, i.e. :data:`UPSTREAM_ROWS` minus :data:`NOT_DECLARABLE`.
#: Read the model id off the declarations themselves so the tuple cannot drift from them.
DECLARED_MODELS: Final[tuple[str, ...]] = tuple(
    sorted(
        {
            spec.model
            for spec in (
                CURRENCY_PAIRS,
                EQUITY_SEARCH,
                HISTORICAL_DIVIDENDS,
                CALENDAR_IPO,
                INSIDER_TRADING,
            )
        }
    )
)

#: The census rows intrinio's own package puts forward and the declaration plane refuses, each with
#: the canonical capability that blocks it, the mechanism that needs it and the upstream line read
#: this round. Re-measured row by row against :mod:`opendata.data.providers._engine.spec` (the only
#: spec-plane vocabulary) and :mod:`opendata.data.providers._engine.http_json` (what it performs at
#: query time), not against the census' label mapping: every one of these 33 rows needs at least one
#: capability that ``ModelSpec`` has no field for. Two kernels for two of them --
#: ``_engine/multi_request.py`` (fan-out + join) and ``_engine/request_body.py`` (POST body + body
#: paging) -- exist as pure modules today and are reachable by no declaration, because adding the
#: fields that would wire them is the engine owner's call, not this package's.
#:
#: ``param.relative_default`` is deliberately absent from the refusals below. ``RelativeDefault``
#: (spec.py:117-195) landed mid-round and ``http_json`` does perform it at query time
#: (:229-245), so the two rows that were withheld on it a round ago are now recorded on the
#: blocker that survives the new field: ``CompanyFilings`` on ``derive.conditional`` -- a
#: conditional default gates on another parameter and this one cannot. ``InsiderTrading`` was
#: withheld on ``columns.null_sentinel`` for exactly that reason and is no longer here: C97 shipped
#: the sentinel facet, the row was re-judged, and it is declared above.
#:
#: The census mis-maps one need and this ledger does not repeat it. Its instrument has no key
#: for "the pages come from a cursor the response publishes", so it folds that need onto
#: ``request.body_pagination`` for nine intrinio rows (EtfSearch, OptionsUnusual, FredSeries,
#: HistoricalAttributes, PriceTargetConsensus, WorldNews and the Ebitda/Eps/Sales estimates),
#: yet the engine DOES read a cursor out of a response -- ``PaginationSpec.cursor_field``
#: (spec.py:240,263-264) -- and a plain ``next_page`` loop is declarable
#: (etf_holdings.py:177-182, company_filings.py:140-141). Those nine are recorded below on
#: their real blockers -- a URL branch, a caller-gated page count, a cross-page dedupe/order or
#: a per-symbol fan-out -- never on paging.
#:
#: Reasons name the mechanism, never the effort: "not written yet" is not a blocker.
NOT_DECLARABLE: dict[str, str] = {
    "BalanceSheet": "flow.fan_out + columns.response_dependent_selection + columns.null_sentinel: "
    "one fundamentals GET lists fiscal periods, then one standardized_financials URL per period is "
    "fanned out (balance_sheet.py:444-465); each row is assembled from a variable tag/value list, "
    "not fixed keys (:471-492), 0 is nulled (:395-403).",
    "CashFlowStatement": "flow.branch + flow.fan_out + columns.null_sentinel: ``period`` maps to "
    "FY/QTR/TTM/YTD and anything else raises (cash_flow.py:262-267); a second URL per fiscal "
    "period is fanned out from the first response (:280-306) and zeros are nulled per row "
    "(:231-236, :325).",
    "CompanyFilings": "derive.conditional: the ``today-1y``/``today`` window is filled only when "
    "``form_type`` is absent (company_filings.py:96-100), and a ``RelativeDefault`` "
    "(spec.py:117-195) has no gate conditioning a bound on another parameter; the ``company`` "
    "object upstream attaches also has no carrier (:138, :161-173).",
    "CompanyNews": "flow.fan_out + post-assembly order/cap: one task per comma-split symbol "
    "(company_news.py:253-269), each with its own ``next_page`` loop (:238-248); rows are de-duped "
    "by ``url`` and sorted across pages before the cap (:260-265), and ``topics`` is an array of "
    "objects folded into a string (:160-167).",
    "EquityHistorical": "flow.branch: ``_interval_size`` versus ``_frequency`` picks a different "
    "path, a different rows key and a different sort column (equity_historical.py:226-231, "
    ":266-273); the ``next_page`` loop (:243-249) is declarable, the branch that chooses the shape "
    "it pages is not.",
    "EquityInfo": "flow.fan_out: one ``/companies/{symbol}/profile`` request per comma-split "
    "symbol is gathered together (equity_info.py:63-68), and a declaration renders one path "
    "template per request.",
    "EquityQuote": "flow.fan_out + columns.compose + columns.null_sentinel: one realtime URL per "
    "symbol (equity_quote.py:129-133); ``symbol`` is lifted out of the nested ``security`` object "
    "(:123), ``sales_conditions`` is regex-stripped then '' nulled (:74-82) and a non-200 answers "
    "``{}`` so the row silently vanishes (:119-120).",
    "EtfHistorical": "flow.branch: ``__init__.py:83`` binds this row to "
    "IntrinioEquityHistoricalFetcher, so it inherits that fetcher's interval-versus-frequency "
    "branch over path, rows key and sort column (equity_historical.py:226-231, :266-273).",
    "EtfHoldings": "param.value_transform + columns.null_sentinel: ``:US`` is appended when the "
    "symbol has no venue (etf_holdings.py:162) and every 0 becomes None (:133-141). The cursor "
    "loop (:177-182) and the weighting sort (:204) look declarable, but that sort runs over the "
    "whole assembled list while the engine orders per page.",
    "EtfInfo": "flow.fan_out + param.value_transform + rows.cap_source_order: per-symbol fan-out "
    "with a ``:US`` suffix (etf_info.py:621-638), then the rows are re-sorted back into the "
    "caller's own symbol order after the gather (:643-646).",
    "EtfPricePerformance": "flow.join + flow.fan_out: per symbol a ``stats`` row is fetched and an "
    "``analytics`` response is merged into that same row (etf_price_performance.py:161-200), while "
    "columns are dropped by substring tests against two parameter-derived needles (:177-181).",
    "EtfSearch": "flow.branch: three mutually exclusive URL shapes -- index search, issuer-less "
    "search and the full list -- chosen by which optional parameters arrived "
    "(etf_search.py:92-97); one ``path`` template cannot answer for all three.",
    "FinancialRatios": "flow.fan_out + columns.response_dependent_selection: ids from a first "
    "fundamentals call fan out one standardized_financials request each "
    "(financial_ratios.py:190-205), and each row names its columns by XBRL tag and nulls its zeros "
    "(:214-235, :312).",
    "ForwardEbitdaEstimates": "flow.fan_out: one ``identifier={symbol}`` request per comma-split "
    "symbol is gathered (forward_ebitda_estimates.py:110-128); the ``next_page`` cursor loop "
    "inside it (:148-150) is the declarable part.",
    "ForwardEpsEstimates": "flow.fan_out: ``get_one`` builds one zacks/eps_estimates URL per "
    "symbol and gathers them (forward_eps_estimates.py:145-161); the ``next_page`` loop over the "
    "merged list (:177-180) is declarable, the fan-out is not.",
    "ForwardPeEstimates": "flow.branch + flow.fan_out: with symbols it gathers one "
    "``/zacks/forward_pe/{symbol}`` request each (forward_pe_estimates.py:86-115), without them "
    "one ``?page_size=10000`` request (:141-142) -- two request shapes from one parameter -- then "
    "re-sorts rows into caller order (:156-162).",
    "ForwardSalesEstimates": "flow.fan_out: one ``zacks/sales_estimates`` URL per comma-split "
    "symbol is gathered into one list (forward_sales_estimates.py:164-175).",
    "FredSeries": "param.value_transform + a parameter-gated page count: ``$`` is stripped from "
    "the series id before it addresses the path (fred_series.py:85) and the loop keeps fetching "
    "only while the caller's ``all_pages`` flag is set (:98-108), a bound no ``PaginationSpec`` "
    "field expresses.",
    "HistoricalAttributes": "flow.fan_out + request.caller_url: the Cartesian product of symbol x "
    "tag URLs goes through amake_requests (historical_attributes.py:77-81) and each row's "
    "``symbol``/``tag`` are read back off that request's URL path parts (:97-101), a value no "
    "derive stage can see.",
    "HistoricalMarketCap": "flow.branch + flow.fan_out + columns.null_sentinel: ``interval`` "
    "chooses the frequency path (historical_market_cap.py:86), one request per symbol is gathered "
    "(:91-102) and published zeros are nulled.",
    "IncomeStatement": "flow.join + flow.fan_out + columns.response_dependent_selection: a second "
    "calculations response is concatenated into the same row's financials list "
    "(income_statement.py:455-464) and one URL per fiscal period is fanned out (:466-472); columns "
    "are named by XBRL tag and 0 becomes None (:484-498).",
    "IndexHistorical": "param.value_transform + flow.fan_out: ``$``/``^`` are stripped from the "
    "symbol before it addresses the path (index_historical.py:71) and one URL per symbol is "
    "gathered (:74-77), with null-valued rows dropped (:84).",
    "KeyMetrics": "flow.fan_out + path.object_keys: 39 hardcoded data-point tags become 39 URLs "
    "per symbol (key_metrics.py:259-318) and one row is assembled by keying each response on the "
    "last path segment of the URL that produced it (:313-315).",
    "LatestAttributes": "flow.fan_out + request.caller_url: the symbol x tag Cartesian product is "
    "fetched through amake_requests (latest_attributes.py:84-90) and each row's ``symbol``/``tag`` "
    "come from the response's request URL parts (:79-82), never from the document.",
    "MarketSnapshots": "flow.preflight_lookup + flow.fan_out + columns.null_sentinel: a JSON "
    "directory response publishes gzip file URLs (market_snapshots.py:168-177), each a second "
    "request decoded as gzipped CSV (:185-196), and ``Max``/``Min``/0/NaN are nulled after the "
    "frame lands (:241-247).",
    "OptionsChains": "flow.branch + flow.fan_out + flow.join: ``delay`` picks the eod versus "
    "realtime path (options_chains.py:230-235), one chain URL fans out per expiration read from "
    "that response (:236-245), each row merges four sub-objects (:340, :352-357) and the "
    "underlying price is delegated to two other fetchers (:299-313).",
    "OptionsSnapshots": "flow.preflight_lookup + shape.columnar: the JSON response publishes a "
    "file URL whose gzip bytes are parsed as CSV (options_snapshots.py:196-217), four columns are "
    "sliced out of ``contract_symbol`` by regex (:272-291) and the answer is validated as columnar "
    "``orient=list`` rows (:337).",
    "OptionsUnusual": "flow.branch + columns.null_sentinel: symbol presence chooses between "
    "``/{symbol}/intraday`` and ``/intraday`` (options_unusual.py:237-242), dates are snapped to a "
    "trading day in a validator (:88-136), several columns are rewritten and 0 nulled (:177-208) "
    "and the cap is applied to the sorted whole list (:246-289).",
    "PriceTargetConsensus": "flow.fan_out: one ``get_one`` consensus request per comma-split "
    "symbol is gathered (price_target_consensus.py:103-111).",
    "ReportedFinancials": "flow.preflight_lookup + flow.fan_out + "
    "columns.response_dependent_selection: ``statement_type`` picks a statement_code "
    "(reported_financials.py:81-84), the ids are filtered and capped client-side in a frame "
    "(:93-101), then one URL per id is fanned out (:116-121) and its tag/value pairs become "
    "dynamic columns (:131-145).",
    "SearchAttributes": "rows.cap_source_order: the cap is ``matched_attributes[: query.limit]`` "
    "over the source's own order, with no comparison and no key column (search_attributes.py:59, "
    ":64); ``RowOrderSpec.column`` is mandatory, so a pure source-order truncation has no carrier.",
    "ShareStatistics": "flow.fan_out + path.assemble_single_row: three tag URLs are fetched in one "
    "fan-out (share_statistics.py:76-79), each scalar is keyed by a URL path part (:81-86) and all "
    "three, plus ``symbol`` and today's date, become ONE row (:68, :88).",
    "WorldNews": "no canonical names it (nearest: rows.order_limit): rows are de-duped by ``url`` "
    "while pages are still arriving and the loop stops on the deduped count versus the limit "
    "(world_news.py:216-230), then the assembled set is sorted and truncated; the engine orders "
    "per page and has no dedupe facet.",
}
