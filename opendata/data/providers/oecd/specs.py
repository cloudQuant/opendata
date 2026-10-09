"""OECD's nine ledger rows measured against the declarative engine.

The denominator is the ledger rows ``OBB2-oecd-<model>`` of ``模型级任务清单.csv`` and the nine
``provider == "oecd"`` entries of ``census-imf-oecd-famafrench-misc.json``, both under
``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/``. Every one is recorded as ``shape=get_csv``,
``http_method=GET``, ``response_format=csv``, ``row_extraction=flat``, ``paged=false``,
``join_needed=false``, needing ``sdmx_dotted_key_path`` + ``csv_decoder``.

Measured result: **none of the nine is declarable**, so :data:`DECLARED_MODELS` is empty and
:data:`NOT_DECLARABLE` accounts for all nine with the capability that blocks each one. The blocker
is neither the CSV body (``decode.delimited`` ships it) nor the paging (none is declared) -- it is
that every pinned model builds the request URL out of *mapped* parameter values and then rewrites
the records it read.

This package declared two of them last round, ``ConsumerPriceIndex`` and ``Unemployment``, and
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
hand-written fetchers that served them stay exactly as they were.

What the six missing capabilities are, measured against the engine's own code and named the same
way in every blocker. :data:`UPSTREAM_ROOT` is the pinned tree each cite is read from.

* ``params.value_map`` -- ``_encode_value`` (http_json.py:239-254) renders a validated
  str/int/float/bool/date verbatim and joins a list with ``,``. It cannot look a value up in a code
  table, uppercase a first letter, turn a bool into one of two literals, or send the empty string
  the upstream models use for their ``all`` choice.
* ``path.list_segment`` -- ``render_path`` (http_json.py:219-236) accepts a placeholder value only
  when it matches ``_PATH_SEGMENT`` (http_json.py:58), whose character class excludes ``+`` -- the
  character six of the nine models join several country codes with.
* ``columns.compose`` -- ``normalize_record`` (http_json.py:298-341) copies one source key per
  declared column and casts it; it cannot reverse-map a code back to a name, convert ``2024-03`` to
  a date, or rescale a published figure, all of which every pinned model does after the body
  arrives.
* ``row_filter.comparison`` -- ``RowFilterOp`` (spec.py:31) is ``contains``/``equals``/
  ``not_equals``/``in``/``prefix``: membership and text only. A model whose date window is a client
  side ``>=``/``<=`` over the converted dates has no declaration for it.
* ``fixed query keys`` -- eight of the nine append ``dimensionAtObservation=TIME_PERIOD`` and
  ``detail=dataonly`` inside their url literal after a ``?`` (``ConsumerPriceIndex`` sends no query
  string and windows the rows client-side), and four also append ``format=csvfile``.
  ``ModelSpec.path`` forbids a query string (spec.py:393-394), so such a key can only be sent as a
  declared parameter -- and rule 2 of the declaration contract requires every declared parameter to
  be a field of the upstream query-params class, which none of these three are. ``startPeriod`` and
  ``endPeriod`` are reachable that way (a ``ParamSpec`` maps ``start_date`` onto any
  ``query_key``); the three fixed keys are not. ``format`` was one of the measured
  ``PARAM_UNBACKED`` findings against last round's declarations.
* ``more than one request`` -- a declaration drives one path template (:attr:`ModelSpec.path`) and
  one paging strategy (:class:`~opendata.data.providers._engine.spec.PaginationSpec`), and
  ``fetch_pages`` can only repeat that template with new page keys. Nothing in the record can branch
  on a status and re-fetch a *rewritten* url, which is what ``house_price_index.py:132-137`` does
  when the monthly body comes back 404.

Two claims this module made earlier are refuted and stay recorded as refuted, because the refusals
below have to be true rather than convenient: the ``Accept: application/vnd.sdmx.data+csv`` header
is **declarable** (``ModelSpec.static_headers``, spec.py:425-437, validated as an RFC 7230 field
name), and a dataflow id ``FLOW,DSD@DF_X,version`` **can** ride in ``path`` as a literal --
``__post_init__`` only forbids ``?``, ``#`` and malformed braces. Neither is a blocker. What cannot
be declared is the mapped, ``+``-joined dimension key that follows the literal, and the derived
columns.

On the preamble question the lead asked: ``decode.delimited`` reads the first non-blank row of a
body as the header and can skip nothing else. That reading is what ``_client.fetch_observations``
does against the live endpoint -- it hands the whole body to ``csv.DictReader`` and requires the
*first* record to carry ``REF_AREA``/``TIME_PERIOD``/``OBS_VALUE`` -- so the engine's reading of a
delimited body is right for that reader. It is not evidence about the nine pinned rows: no body of
any of them has ever been captured here, so their preamble status is unverified, and with no
declaration left standing, nothing in this package exercises the question. It is recorded as a
caveat, not as the blocker; the blocker for all nine is the composed key and the derived columns.
"""

from __future__ import annotations

#: The pinned upstream tree the cites below are read from.
UPSTREAM_ROOT = "providers/oecd/openbb_oecd/models"

#: ``ConsumerPriceIndex`` and ``Unemployment`` were declared last round and retracted this one;
#: see the module docstring for the measured findings that refused them.
DECLARED_MODELS: tuple[str, ...] = ()

#: Every ledger row, and the capability that blocks it. A cite that repeats no file name is a line
#: of the file named just before it.
#:
#: Read row by row, the nine share one request shape the declaration format has no way to spell: the
#: caller's parameters are mapped through code tables (and, for multi-country requests, joined with
#: ``+``) into a single dotted dimension key appended after the dataflow literal, and the records
#: are renamed, code-mapped back to names, date-converted and rescaled after the body arrives.
NOT_DECLARABLE: dict[str, str] = {
    "CompositeLeadingIndicator": "params.value_map and path.list_segment: every country is looked "
    "up in COUNTRIES and joined with '+' (composite_leading_indicator.py:176) into one dotted key "
    "with empty positions (:181); fixed query keys (:183); renamed (:202) and divided by 100 "
    "(:214) = columns.compose",
    "CountryInterestRates": "params.value_map and path.list_segment: COUNTRY_TO_CODE_IR codes "
    "joined with '+' (country_interest_rates.py:126) into the key, whose duration position is a "
    "DURATION_DICT lookup (:134); fixed query keys (:136); renamed (:147), /100 (:152) and "
    "set-indexed before to_dict (:155-160) = columns.compose",
    "GdpNominal": "params.value_map/path.list_segment: '+'-joined COUNTRY_TO_CODE_GDP codes "
    "(gdp_nominal.py:149) into a key with empty positions (:155), its dataflow literal rewritten "
    "per unit (:153-154, :160); columns.compose: apply_map (:183), date conversion (:184), x1e6 "
    "(:187); row_filter.comparison for the date window (:185)",
    "GdpReal": "same composition as GdpNominal: '+'-joined code map (gdp_real.py:125) into the key "
    "(:131); fixed query keys (:133); columns.compose: apply_map/str.replace (:157), date "
    "conversion (:158), x1e6 scaling (:160); row_filter.comparison for the date window (:159)",
    "GdpForecast": "params.value_map: measure_dict[query.units] (gdp_forecast.py:171-178) and "
    "'+'-joined codes (:192) compose the key (:203); fixed query keys (:205); columns.compose: "
    "renamed (:222), int cast or /100 on a units-dependent branch (:236-240); rows dropped by "
    "value comparisons (:233, :242) = row_filter.comparison",
    "HousePriceIndex": "params.value_map/path.list_segment: frequency_dict and transform_dict "
    "lookups (house_price_index.py:114-115) and '+'-joined codes (:121) in the key (:128); fixed "
    "query keys (:130); more than one request: a 404 is retried on a rewritten url (:132-137), "
    "beyond one path template per page; country code map back (:151)",
    "SharePriceIndex": "params.value_map: frequency_dict lookup (share_price_index.py:113) and "
    "'+'-joined COUNTRY_TO_CODE_RGDP codes (:119) compose the key (:126); fixed query keys (:128); "
    "renamed (:139) then the codes mapped back to country names (:148) = columns.compose",
    "ConsumerPriceIndex": "params.value_map: harmonized->methodology "
    "(consumer_price_index.py:172), frequency[0].upper() (:179), units dict "
    "(:180-184), '' for all expenditure (:185-187) and '+'-joined codes "
    "(:189-193) in one segment (:203-205); columns.compose names/dates/scale "
    "(:242-244, :249-250); row_filter.comparison window (:246-248)",
    "Unemployment": "params.value_map/path.list_segment: '+'-joined COUNTRY_TO_CODE_UNEMPLOYMENT "
    "codes (unemployment.py:139-143) and dict-mapped sex/age/seasonal positions in one segment "
    "(:150-152); fixed query keys (:154); columns.compose: renamed (:165), /100 (:168), codes back "
    "to country names (:169), date conversion (:170)",
}

#: The nine ledger rows this iteration asked about, so a refusal cannot silently drop one.
LEDGER_ROWS: tuple[str, ...] = (
    "CompositeLeadingIndicator",
    "ConsumerPriceIndex",
    "CountryInterestRates",
    "GdpNominal",
    "GdpReal",
    "GdpForecast",
    "HousePriceIndex",
    "SharePriceIndex",
    "Unemployment",
)
