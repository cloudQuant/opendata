"""Benzinga's declarative PriceTarget model plus the full identity ledger.

The benzinga census rows four models; exactly one of them -- ``OBB2-benzinga-PriceTarget`` -- is
covered end to end by the shipped capability vocabulary, and this package declares that one row:
``columns.cast`` (the epoch-*seconds* ``updated`` integer becoming a UTC calendar date through the
derive plan's :class:`~opendata.data.providers._engine.value_transform.CastSpec`, the C95 census
correction that :155/:158 publish seconds and :253 reads them back with no /1000),
``columns.null_sentinel`` (the ``""`` spellings upstream's before-validator nulls across every key
of the assembled row, price_target.py:257-261), ``columns.select`` (the published ``url_calendar``
key is a duplicate of ``url`` and is dropped simply by not being a declared column -- upstream
``transform_data`` pops it at :319), and ``param.value_mapping`` (the caller's ``action`` needle is
re-spelled to the wire's ``COVERAGE_DICT`` value, :143-147 and the data model's own literals at
:192-207). It is a single-GET, nested-key JSON endpoint, so it rides the shipped declarative engine
as a :class:`~opendata.data.providers._engine.spec.ModelSpec`.

The other three rows use typed hand-written adapters in ``models.iter2`` because their response
shapes require bounded pagination or a nested typed payload.

Every interface fact below is taken from the cited upstream contract file as output-contract
comparison only (openbb_platform/providers/benzinga/openbb_benzinga/models/price_target.py): the
host, path, row key, parameter names and output columns are restated as our own declarations, and
no upstream code, docstring or comment was copied.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.spec import ColumnSpec, ModelSpec, ParamSpec
from opendata.data.providers._engine.value_transform import CastSpec, DerivePlan, DeriveStage

#: Base of the Benzinga v2.1 calendar REST API (price_target.py:289).
BENZINGA_BASE_URL = "https://api.benzinga.com"

#: Environment variable holding the access token; upstream sends it as the ``token`` query key
#: (aextract_data, price_target.py:301-305).
BENZINGA_CREDENTIAL = "BENZINGA_API_KEY"

#: The one spelling this model's rows read as no value: the empty string, nulled per key before
#: any coercion (price_target.py:257-261 model_validator).
EMPTY_TEXT: Final[tuple[str, ...]] = ("",)

#: ``param.value_mapping`` in full: the caller's enum value and the published wire spelling of the
#: ``action`` column it becomes (COVERAGE_DICT, price_target.py:22-35; the same literals are the
#: ``action`` choices of the data model, :192-207).
ACTION_SPELLINGS: Final[tuple[tuple[str, str], ...]] = (
    ("downgrades", "Downgrades"),
    ("maintains", "Maintains"),
    ("reinstates", "Reinstates"),
    ("reiterates", "Reiterates"),
    ("upgrades", "Upgrades"),
    ("assumes", "Assumes"),
    ("initiates", "Initiates Coverage On"),
    ("terminates", "Terminates Coverage On"),
    ("removes", "Removes"),
    ("suspends", "Suspends"),
    ("firm_dissolved", "Firm Dissolved"),
)

#: The analyst price-target calendar: one GET of /api/v2.1/calendar/ratings, rows under
#: ``ratings``, every published key renamed to the contract's column name.
PRICE_TARGET = ModelSpec(
    model="PriceTarget",
    domain="benzinga_price_target",
    asset_class="equity",
    period="d",
    market="us",
    base_url=BENZINGA_BASE_URL,
    path="/api/v2.1/calendar/ratings",
    rows_pointer="ratings",
    static_headers=(("accept", "application/json"),),
    params=(
        ParamSpec(
            "symbol",
            "str",
            query_key="parameters[tickers]",
            note="The searched ticker, sent under the bracketed alias key "
            "(price_target.py:44-55); no preflight lookup addresses it, so join_needed is false.",
        ),
        ParamSpec(
            "action",
            "enum",
            query_key="parameters[action]",
            enum=tuple(origin for origin, _ in ACTION_SPELLINGS),
            value_map=ACTION_SPELLINGS,
            note="The census param.value_mapping face: the caller's lowercase needle is re-spelled "
            "through COVERAGE_DICT before the wire (price_target.py:143-147); the map is total for "
            "the enum and an unlisted needle is refused at encode, never passthrough.",
        ),
        ParamSpec(
            "date",
            "date",
            query_key="parameters[date]",
            note="Calendar-day shorthand for the window (price_target.py:84-89).",
        ),
        ParamSpec(
            "start_date",
            "date",
            query_key="parameters[date_from]",
            note="Window start (:90-94).",
        ),
        ParamSpec(
            "end_date",
            "date",
            query_key="parameters[date_to]",
            note="Window end (:95-99).",
        ),
        ParamSpec(
            "limit",
            "int",
            query_key="pagesize",
            default=200,
            note="Row cap aliased to pagesize; upstream fills 200 when the caller names none "
            "(:292). pagesize only caps the page -- the fetcher issues exactly one request and no "
            "paging loop exists, so pagination stays at its none default.",
        ),
    ),
    columns=(
        ColumnSpec("symbol", "str", source_key="ticker", null_sentinel=EMPTY_TEXT),
        ColumnSpec("published_date", "date", source_key="date", null_sentinel=EMPTY_TEXT),
        ColumnSpec("published_time", "str", source_key="time", null_sentinel=EMPTY_TEXT),
        ColumnSpec("analyst_firm", "str", source_key="analyst", null_sentinel=EMPTY_TEXT),
        ColumnSpec("analyst_id", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("company_name", "str", source_key="name", null_sentinel=EMPTY_TEXT),
        ColumnSpec("action", "str", source_key="action_company", null_sentinel=EMPTY_TEXT),
        ColumnSpec("action_change", "str", source_key="action_pt", null_sentinel=EMPTY_TEXT),
        ColumnSpec("rating_previous", "str", source_key="rating_prior", null_sentinel=EMPTY_TEXT),
        ColumnSpec("importance", "int", null_sentinel=EMPTY_TEXT),
        ColumnSpec(
            "price_target",
            "float",
            source_key="pt_current",
            null_sentinel=EMPTY_TEXT,
            units="usd",
        ),
        ColumnSpec(
            "price_target_previous",
            "float",
            source_key="pt_prior",
            null_sentinel=EMPTY_TEXT,
            units="usd",
        ),
        ColumnSpec(
            "adj_price_target",
            "float",
            source_key="adjusted_pt_current",
            null_sentinel=EMPTY_TEXT,
            units="usd",
        ),
        ColumnSpec(
            "previous_adj_price_target",
            "float",
            "adjusted_pt_prior",
            null_sentinel=EMPTY_TEXT,
            units="usd",
        ),
        ColumnSpec("notes", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("url_news", "str", null_sentinel=EMPTY_TEXT),
        ColumnSpec("url_analyst", "str", source_key="url", null_sentinel=EMPTY_TEXT),
        ColumnSpec("id", "str", null_sentinel=EMPTY_TEXT),
        # Published as an epoch SECONDS integer (price_target.py:155/:158 emit
        # int(v.timestamp()); :253 reads safe_fromtimestamp(v) with no /1000) and cast to the UTC
        # calendar day. DIVERGENCE, recorded: upstream keeps a datetime when the instant is not
        # midnight (:254) and the engine's ValueKind has no datetime domain, so this contract
        # publishes the UTC date for every instant; the census's C95 correction is what names the
        # unit here.
        ColumnSpec("last_updated", "date", source_key="updated", null_sentinel=EMPTY_TEXT),
    ),
    column_derives=(
        DerivePlan(
            column="last_updated",
            source_kind="record",
            stages=(
                DeriveStage(
                    kind="cast",
                    cast=CastSpec(kind="epoch_s_to_date", timezone="UTC"),
                ),
            ),
            note="The columns.cast face: the record's own 'updated' key (source left blank) is an "
            "epoch-seconds integer and becomes the UTC calendar day; the '' sentinel is matched "
            "on the published text ahead of this stage, and a null after the sentinel stays null.",
        ),
    ),
    credential=BENZINGA_CREDENTIAL,
    credential_query_key="token",
    scenario="目标价评级：按代码与评级动作取回 benzinga 分析师目标价日历单页",
    error_prefix="BENZINGA",
    notes=(
        "openbb_benzinga/models/price_target.py:22-35,44-55,143-147,192-207,250-261,289-320 "
        "(contract only). One GET; rows are the document's 'ratings' list (:307). The seventeen "
        "renames are the provider's own alias dict (:181-197) restated as source keys. "
        "columns.select is the drop face: the published url_calendar key duplicates url and is "
        "not a declared column, so it never reaches the row (upstream pops it, :319), and the "
        "row model is extra-forbid anyway. Upstream raises EmptyDataError on a document without a "
        "non-empty 'ratings' list (:303-310); the engine refuses a document without the rows key "
        "as a shape failure and publishes the empty list the declaration allows. DIVERGENCE, "
        "recorded: page, updated, importance, analyst_ids, firm_ids and fields are accepted "
        "upstream parameters this declaration does not carry, so a caller passing them is refused "
        "at the query stage instead of silently widening the reviewed contract; the ''-spellings "
        "validator (:257-261) is what makes every column here optional with a null default."
    ),
)

#: Model identities with provider bindings; PriceTarget remains the declarative row in this module.
DECLARED_MODELS: Final[tuple[str, ...]] = (
    "AnalystSearch",
    "CompanyNews",
    "WorldNews",
    PRICE_TARGET.model,
)
