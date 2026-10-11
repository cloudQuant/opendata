"""OECD's nine ledger rows, re-measured against this checkout's engine (C89).

The denominator is the ledger rows ``OBB2-oecd-<model>`` of ``模型级任务清单.csv`` and the nine
``provider == "oecd"`` entries of ``census-imf-oecd-famafrench-misc.json``, both under
``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/``. Every one reads ``shape=get_csv``,
``http_method=GET``, ``response_format=csv``, ``row_extraction=flat``, ``paged=false``,
``join_needed=false``. The need sets are no longer the two labels C75 left them with
(``sdmx_dotted_key_path`` + ``csv_decoder``): C89 appended the blockers the per-row notes below
actually name, and the census still marks all nine ``expressible_today: false``.

Measured result: **none of the nine is declarable**, so :data:`DECLARED_MODELS` is empty and
:data:`NOT_DECLARABLE` accounts for all nine. That repeats C75's verdict, not its reasoning: four of
the six capabilities C75 named have since shipped, and the need sets in the census remain incomplete
for a different reason. Measured against the code, not against either round's prose.

This package declared two of them, ``ConsumerPriceIndex`` and ``Unemployment``, and
``scripts/quality/declaration_provenance.py`` refused them with measured findings rather than
opinion: ``COLUMN_MISSING``/``COLUMN_EXTRA`` (the pinned models publish ``country``/``date``/
``value`` plus ``expenditure`` for CPI, the declarations published the SDMX dimension names),
``PARAM_UNBACKED`` (neither ``series_key`` nor ``format`` is a field of the upstream query-params
class) and ``PATH_UNBACKED`` (the HICP dataflow literal occurs in no upstream file). The
declarations described :mod:`opendata.data.providers.oecd.models._client` -- the clean-room reader
this repository ships, whose own docstring records it as ``self-written; only the OECD SDMX API's
public interface is referenced`` -- under OpenBB's ledger identity. That is a mislabeled port, not
a smaller one: declaring the same reader under a different model id would keep the invention while
dodging the audit, so both ModelSpecs and their generated fetchers are retracted, and the two
hand-written fetchers that served them stay exactly as they were. That retraction stands: nothing
below re-opens it, and a re-declaration would have to answer the same three findings.

Shipped since C75, and therefore no longer a blocker (each probed in
``tests/test_oecd_engine_provider.py``):

* :attr:`~opendata.data.providers._engine.spec.ParamSpec.value_map` maps a caller's spelling to the
  upstream token, in the query string and inside a composite position alike
  (``http_json.py:349-366``, ``http_json.py:311-312``).
* :attr:`~opendata.data.providers._engine.spec.ModelSpec.static_query` sends
  ``dimensionAtObservation=TIME_PERIOD``, ``detail=dataonly`` and ``format=csvfile`` as
  caller-immutable constants (``spec.py:426-431``, ``http_json.py:233``).
* :attr:`~opendata.data.providers._engine.spec.ModelSpec.composite_keys` composes the dotted SDMX
  key, literal positions included, and a position whose parameter is absent takes a declared
  wildcard default -- the empty string is legal (``key_composition.py:141-143``,
  ``key_composition.py:204-213``).
* :attr:`~opendata.data.providers._engine.spec.ModelSpec.column_derives` casts and rescales: the
  ``/100`` a percent-valued ``OBS_VALUE`` gets and the ``x1_000_000`` GDP levels get are declarable
  (``value_transform.py:513``, ``value_transform.py:357-365``).
* The CSV body, the BOM, the header row and the ``REF_AREA``/``TIME_PERIOD``/``OBS_VALUE`` select
  and rename are declarable (``DecoderSpec``, ``ColumnSpec.source_key``, ``spec.py:66-79``).

What is still missing, each name used by a blocker below and defined by the engine line that shows
its absence. :data:`UPSTREAM_ROOT` is the pinned tree every cite is read from.

* ``path.list_segment`` -- one key position carrying several country codes joined with ``+``
  (six of the nine build that string with ``"+".join(...)``). ``CompositeKeySpec`` joins whole
  positions on ONE separator (``key_composition.py:254-255``) and the ``sdmx_code`` charset refuses
  both ``+`` and a rendered list (``key_composition.py:62-66``, measured as ``charset_invalid``).
  The same grammar also refuses the models' ``all`` sentinel, which upstream spells as an EMPTY
  position: a ``value_map`` may not carry a blank target (``spec.py:158-171``), and a bool parameter
  cannot be mapped to ``Y``/``N`` at all (``spec.py:154-157``).
* ``columns.compose`` -- the published ``country`` is the REF_AREA code looked up in a
  ``CODE_TO_COUNTRY_*`` table and then rewritten (``_`` to spaces, ``.title()``, ``Oecd``,
  ``Asia5``), and CPI rewrites ``expenditure`` through a second table. A derive plan runs the
  kernel's two stages and nothing else -- ``STAGE_SEQUENCE = ("cast", "scale")``
  (``value_transform.py:513``, refusal at ``value_transform.py:537-541``) -- so no declaration can
  look a value up.
* ``columns.period_cast`` -- ``oecd_date_to_python_date`` branches on the published text's width:
  ``YYYY``, ``YYYY-MM``, ``YYYY-Qn`` (``utils/helpers.py:253-261``). ``CastSpec`` declares ONE
  ``input_format`` and refuses a value that does not match it (``value_transform.py:141-146``),
  which is enough for the fixed-monthly CLI key and not enough for any model whose frequency
  position comes from the caller.
* ``derive.conditional`` -- a cast, a rescale or a key position chosen by ANOTHER parameter's value:
  ``value // 100`` only for percent transforms, ``int`` only for levels, ``B1GQ`` rewritten to
  ``B1GQ_POP`` for capita, annual forced where a units/frequency pair has no quarterly series, the
  dataflow literal itself varying with ``units``. A :class:`DerivePlan` binds a column, a source and
  stages and carries no gate (``value_transform.py:569-597``), and a
  :class:`~opendata.data.providers._engine.key_composition.KeyComponent` reads one value
  (``key_composition.py:297-333``).
* ``row_filter.comparison`` -- ``RowFilterOp`` is ``contains``/``equals``/``not_equals``/``in``/
  ``prefix`` (``spec.py:40``, ``spec.py:55``): membership and text only. Six rows drop rows on a
  value comparison (``value.notnull()``, ``value > 0``) and three window the converted dates with
  ``>=``/``<=`` client-side; CPI's selection is a nested boolean expression over five dimension
  columns, which one op over ``match="any"|"all"`` cannot spell (``spec.py:341-401``).
* ``more than one request`` -- ``HousePriceIndex`` retries a 404 monthly body on the same url with
  ``.M.`` rewritten to ``.Q.``. A record drives one :attr:`ModelSpec.path` template and one
  :class:`~opendata.data.providers._engine.spec.PaginationSpec`, and nothing can branch on a status
  and re-address a rewritten url.

Two C75 claims stay refuted, because a refusal has to be true rather than convenient: the
``Accept: application/vnd.sdmx.data+csv`` header is declarable (``ModelSpec.static_headers``) and a
``FLOW,DSD@DF_X,version`` dataflow rides in ``path`` as a literal -- ``__post_init__`` forbids only
``?``, ``#`` and malformed braces (``spec.py:534-535``). No body of any pinned model has ever been
captured here, so their preamble status remains unverified; it is not what blocks these nine, since
none of them reaches the row stage a preamble could corrupt.
"""

from __future__ import annotations

#: The pinned upstream tree the cites below are read from.
UPSTREAM_ROOT = "providers/oecd/openbb_oecd/models"

#: Nothing is declared: every row still needs ``columns.compose`` plus at least one other
#: capability above, and a model declared without them would publish the code where the pinned
#: model publishes the name. C75's two declarations remain retracted.
DECLARED_MODELS: tuple[str, ...] = ()

#: Every ledger row, and the capability that blocks it. A cite that repeats no file name is a line
#: of the file named just before it.
NOT_DECLARABLE: dict[str, str] = {
    "CompositeLeadingIndicator": "path.list_segment: '+'-joined COUNTRIES codes "
    "(composite_leading_indicator.py:176) and an adjustment position another parameter empties "
    "(:169-171); columns.compose: code looked back up to a name, two rewrites (:203-210); "
    "derive.conditional /100 only when growth_rate (:213); row_filter.comparison not-null (:217)",
    "CountryInterestRates": "columns.compose: CODE_TO_COUNTRY_IR lookup (country_interest_rates.py"
    ":150); columns.period_cast over the caller-chosen M/Q/A key (:120, :151); path.list_segment "
    "joins codes with '+' (:126); row_filter.comparison drops null values (:154); the duration "
    "lookup (:134) and the fixed keys (:136) are declarable",
    "GdpNominal": "derive.conditional: price_base rewritten for indices (gdp_nominal.py:140-141) "
    "and B1GQ read as B1GQ_POP for capita (:160), and the dataflow literal itself varies with "
    "units (:154); columns.compose (:177-183); columns.period_cast (:184); row_filter.comparison "
    "window (:185); path.list_segment (:149)",
    "GdpReal": "path.list_segment: '+'-joined COUNTRY_TO_CODE_GDP codes (gdp_real.py:125) into the "
    "composed key (:131); columns.compose: lookup, title, Oecd rewrite (:151-157); "
    "columns.period_cast over the Q/A key (:117, :158); row_filter.comparison date window (:159); "
    "the x1e6 rescale (:160) is declarable",
    "GdpForecast": "derive.conditional: annual forced by capita+quarter (gdp_forecast.py:180-184), "
    "int cast or /100 by units (:235-240); row_filter.comparison: not-null and two value>0 windows "
    "(:233, :237, :242); columns.compose: code table with replace/title (:223-231); "
    "columns.period_cast (:232); path.list_segment (:192-197)",
    "HousePriceIndex": "more than one request: a 404 monthly body is retried on a url with .M. "
    "rewritten to .Q. (house_price_index.py:134-138), past one path template; columns.compose: "
    "CODE_TO_COUNTRY_RGDP lookup (:151); columns.period_cast (:152); path.list_segment (:121); "
    "row_filter.comparison not-null (:154)",
    "SharePriceIndex": "columns.compose: REF_AREA mapped back through CODE_TO_COUNTRY_RGDP "
    "(share_price_index.py:148); columns.period_cast over the frequency_dict key (:113, :149); "
    "row_filter.comparison not-null drop (:143); path.list_segment: '+'-joined codes (:119) in the "
    "key (:126); the select and rename (:140) are declarable",
    "ConsumerPriceIndex": "row_filter.comparison: a nested expression over METHODOLOGY, "
    "UNIT_MEASURE, FREQ, REF_AREA, EXPENDITURE (consumer_price_index.py:213-228) and a date window "
    "(:245); columns.compose: two code tables (:242-243); columns.period_cast (:244); "
    "derive.conditional /100 by transform (:249); path.list_segment (:189-193)",
    "Unemployment": "columns.compose: CODE_TO_COUNTRY_UNEMPLOYMENT lookup "
    "(unemployment.py:169) and an 'all' fill (:180); columns.period_cast (:170); "
    "row_filter.comparison: not-null (:172) and the date window (:177); path.list_segment: "
    "'+'-joined codes (:139-145), and a bool position (:137) no value_map can spell",
}

#: The nine ledger rows this iteration asked about, so a refusal cannot silently drop one.
LEDGER_ROWS: tuple[str, ...] = (
    "CompositeLeadingIndicator",
    "ConsumerPriceIndex",
    "CountryInterestRates",
    "GdpForecast",
    "GdpNominal",
    "GdpReal",
    "HousePriceIndex",
    "SharePriceIndex",
    "Unemployment",
)
