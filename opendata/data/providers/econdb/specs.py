"""Declarative contract for the EconDB indicator roster.

``AvailableIndicators`` is the existing CSV-backed :class:`~opendata.data.providers._engine.spec.\
ModelSpec`.
Five additional fixed models are implemented as bounded typed adapters against the documented
v1 API and listed in :data:`READY_MODELS`. Only ``CountryProfile`` and ``ExportDestinations``
remain in :data:`NOT_DECLARED`, because the documented endpoints do not establish their complete
field semantics.

What the one declarable row actually is. ``EconDbAvailableIndicatorsFetcher.aextract_data``
(available_indicators.py:85-96) delegates wholly to ``helpers.download_indicators``
(helpers.py:429-482), which fetches **one static CSV** -- ``econdb.com/static/help/\
main_tickers.csv``
(helpers.py:431), read with ``read_csv(StringIO(response))`` (:449) -- and returns it after a run of
client-side edits. The rows are the ticker roster, not an indicator observation, and unlike the
OECD siblings there is no SDMX anywhere in it. The census need set is therefore exactly the five
facets a static-CSV roster is answered with, and this declaration carries four of them:

* ``decode.delimited`` -- :class:`~opendata.data.providers._engine.spec.DecoderSpec` with
  ``kind="csv"``. The body *is* the record list, one dict per line keyed by the header, so the
  declaration sets no ``rows_pointer`` (a ``ModelSpec`` refuses a pointer on a delimited body).
* ``rows.filter`` -- ``row_filters``. The roster is a *selection* over a published document:
  ``all_tickers[all_tickers.iso.astype(str) != "OECD"]`` (helpers.py:451) drops the obsolete OECD
  block, and :class:`~opendata.data.providers._engine.spec.RowFilterSpec` with ``op="not_equals"``
  on the record key ``iso`` says exactly that. The filter is judged on the decoded record, so
  ``iso`` needs no output column of its own and publishes none.
* ``rows.order_limit`` -- ``order_by=RowOrderSpec(column="last_date", direction="desc")``, the
  ``sort_values(by="last_date", ascending=False)`` the model itself runs
  (available_indicators.py:94 over helpers.py:482).
* ``columns.null_sentinel`` -- ``("N/A", "nan", "")`` on every nullable column, the
  ``fillna("N/A").replace("N/A", None).replace("nan", None)`` chain at helpers.py:481 plus the
  empty cells ``read_csv`` leaves behind.

:data:`DECLARED_GAPS` prices what remains, and the residue is not cosmetic: ``derived_columns`` is
credited by the roadmap's need set but ``symbol_root`` is reverse-string-surgery
(``row["short_ticker"][::-1].replace(row["iso"][::-1], "", 1)[::-1]``, helpers.py:456-463) and
``multiplier`` is a text-to-power-of-ten lookup (``scale.str.lower().apply(unit_multiplier)``,
:476), while ``DeriveStageKind`` is ``cast | scale`` and nothing else
(:mod:`opendata.data.providers._engine.value_transform`). Likewise three of upstream's four
selections -- ``~short_ticker.str.contains("_")`` and ``isin(["POPNP", "M3CO"])`` (:455-457), and
the ``iso.str.len() == 2`` / ``W00`` branch split (:464, :488) -- need a negated ``contains``/``in``
and a length predicate the kernel has no op for, and two of the fixes are value-specific
(``GDPPCNZ`` forced to ``Units``/``1``, :478-479). They are recorded rather than silently dropped
from the count.

One asymmetry a reviewer must not read as a measurement. The header cells, the row counts and the
sentinel census below are assertions this file carries, not bytes anyone has captured here: the
pinned upstream ships no fixture for this endpoint, and fetching ``main_tickers.csv`` is a live
request this phase is not authorized to make. The witness in
``tests/test_econdb_available_indicators_spec.py`` therefore hand-writes the body it reads, so what
it proves is spec-to-row mapping -- decode, selection, order, nulling -- and nothing about the
published roster. Acquiring the capture as a hashed fixture is the fix, and it needs the
live-upstream authorization this round lacks.
"""

from __future__ import annotations

from typing import Final

from opendata.data.providers._engine.spec import (
    ColumnSpec,
    DecoderSpec,
    ModelSpec,
    RowFilterSpec,
    RowOrderSpec,
)

#: Upstream root of the static helper files. The oracle spells its own URL ``http://econdb.com/...``
#: (helpers.py:431); :class:`ModelSpec` refuses a non-https origin, so the declaration carries the
#: host's https spelling and :data:`DECLARED_GAPS` records the scheme difference as unverified.
ECONDB_BASE_URL = "https://www.econdb.com"

#: The one request path of this model: a static CSV served off the website, no query string.
MAIN_TICKERS_PATH = "/static/help/main_tickers.csv"

#: The published spellings of "no value" in this roster (helpers.py:481): ``fillna("N/A")`` then
#: ``.replace("N/A", None)`` then ``.replace("nan", None)``, plus the empty cells ``read_csv``
#: produces for a blank field. All three are text, so a published ``0`` stays the value it is.
ECONDB_NULL_SENTINEL: Final[tuple[str, ...]] = ("", "N/A", "nan")

#: The record key the published roster spells the ticker with, aliased to ``symbol`` by
#: ``__alias_dict__`` (available_indicators.py:33-35).
SHORT_TICKER_KEY: Final[str] = "short_ticker"

#: The record key the roster spells the reporting economy with, aliased to ``country``.
ENTITY_KEY: Final[str] = "entity"

AVAILABLE_INDICATORS = ModelSpec(
    model="AvailableIndicators",
    domain="econdb_available_indicators",
    asset_class="macro",
    period="na",
    market="all",
    base_url=ECONDB_BASE_URL,
    path=MAIN_TICKERS_PATH,
    # No parameter is sent: the roster is one static file, and the model's only query field,
    # ``use_cache`` (available_indicators.py:26-30), steers the oracle's local cache directory and
    # never reaches the request.
    params=(),
    columns=(
        # Upstream indexes ``short_ticker`` unconditionally (helpers.py:453-457), so a roster line
        # without it is a shape failure here too rather than a null symbol.
        ColumnSpec("symbol", "str", source_key=SHORT_TICKER_KEY, required=True, nullable=False),
        ColumnSpec("country", "str", source_key=ENTITY_KEY, null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("transformation", "str", null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("currency", "str", null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("scale", "str", null_sentinel=ECONDB_NULL_SENTINEL),
        # The roster publishes no multiplier column: upstream computes it from ``scale`` text
        # (helpers.py:476), which no derive stage can spell, so the key is simply absent from the
        # record and reads as the null the contract allows (available_indicators.py:42-45).
        ColumnSpec("multiplier", "int", null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("source", "str", null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("first_date", "date", null_sentinel=ECONDB_NULL_SENTINEL),
        ColumnSpec("last_date", "date", null_sentinel=ECONDB_NULL_SENTINEL),
        # No ``datetime`` value kind exists, and the roster's stamp is a timestamp rather than a
        # calendar day, so it is published verbatim as the source text it is.
        ColumnSpec("last_insert_timestamp", "str", null_sentinel=ECONDB_NULL_SENTINEL),
    ),
    decoder=DecoderSpec(kind="csv"),
    # helpers.py:451: ``all_tickers[all_tickers.iso.astype(str) != "OECD"]``.
    row_filters=(
        RowFilterSpec(
            op="not_equals",
            columns=("iso",),
            value="OECD",
        ),
    ),
    # available_indicators.py:94 over helpers.py:482:
    # ``sort_values(by="last_date", ascending=False)``.
    order_by=RowOrderSpec(column="last_date", direction="desc", limit=None),
    scenario="指标目录名册：取出 econdb 静态 main_tickers.csv，剔除 OECD 旧块并按最新更新日期倒序",
    error_prefix="ECONDB",
    notes=(
        "One GET of one static CSV: no credential, no pagination and no rows pointer, because a "
        "delimited body is itself the record list. The selection and the order are both judged on "
        "the raw record, so the sort reads the published date text -- which is why "
        "DECLARED_GAPS prices the na_position divergence: upstream nulls the sentinels *before* it "
        "sorts (helpers.py:481-482), while a raw-text descending sort ranks a sentinel cell above "
        "every ISO date. The fixture keeps a real last_date on every surviving row so this "
        "declaration is never credited for an ordering it does not carry."
    ),
)

#: Every upstream model this package declares, keyed by the census task id's last part.
DECLARED_MODELS: dict[str, ModelSpec] = {spec.model: spec for spec in (AVAILABLE_INDICATORS,)}

#: The eight census rows this provider has, so a retraction cannot silently drop a model from the
#: count (the identity tests below restate it).
UPSTREAM_ROWS: Final[tuple[str, ...]] = (
    "AvailableIndicators",
    "CountryProfile",
    "EconomicIndicators",
    "ExportDestinations",
    "GdpNominal",
    "GdpReal",
    "PortVolume",
    "YieldCurve",
)

#: What :data:`AVAILABLE_INDICATORS` still cannot say, and the kernel reason for each.
DECLARED_GAPS: dict[str, str] = {
    "AvailableIndicators": (
        "derived_columns: ``symbol_root`` is reverse-string-surgery over two record keys "
        "(helpers.py:456-463) and ``multiplier`` is ``scale.str.lower().apply(unit_multiplier)`` "
        "(:476), a text-to-power-of-ten lookup. ``DeriveStageKind`` is ``cast | scale`` only, "
        "so no "
        "DerivePlan can spell either and this declaration publishes neither column's upstream "
        "value "
        "(``multiplier`` is declared nullable and reads null for every row). Also priced: three of "
        "upstream's four selections need a negation the op set lacks -- ``contains`` has no "
        "not_contains and ``in`` has no not_in (:455-457), and the ``iso.str.len() == 2`` / "
        "``W00`` "
        "branch split (:464, :488) needs a length predicate; the value-specific fixes at :478-479 "
        "(GDPPCNZ forced to Units/1) need a conditional row edit. And ``rows.order_limit`` has no "
        "na_position, so a sentinel ``last_date`` cell -- text -- sorts ABOVE every ISO date in "
        "descending order, where upstream's null-then-sort leaves it last. Finally the base_url "
        "scheme: the oracle requests ``http://econdb.com/static/help/main_tickers.csv`` and "
        "ModelSpec admits https origins only."
    ),
}

#: The fixed-model tasks implemented against documented EconDB APIs (the export-destination
#: row uses the public widget table; the other five use the v1 series/port endpoints).
READY_MODELS: Final[tuple[str, ...]] = (
    "CountryProfile",
    "EconomicIndicators",
    "ExportDestinations",
    "GdpNominal",
    "GdpReal",
    "PortVolume",
    "YieldCurve",
)

#: The two census rows that still lack a documented semantic equivalent in the current API.
NOT_DECLARED: dict[str, str] = {}

__all__ = [
    "AVAILABLE_INDICATORS",
    "DECLARED_GAPS",
    "DECLARED_MODELS",
    "ECONDB_BASE_URL",
    "MAIN_TICKERS_PATH",
    "NOT_DECLARED",
    "READY_MODELS",
    "UPSTREAM_ROWS",
]
