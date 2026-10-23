"""congress_gov's 8 upstream rows measured against the engine, not hand-written around them.

The denominator is the pinned upstream provider dictionary
(``providers/congress_gov/openbb_congress_gov/__init__.py:23-31``,
ledger ids ``OBB2-congress_gov-*``).
Every row was read in that tree and judged against what the shared engine actually performs
at query time (:mod:`opendata.data.providers._engine.http_json`): one GET whose body decodes as
JSON, a record list that *is* a JSON array at the declared pointer (``resolve_rows``,
http_json.py:410-435 -- a pointer landing on a mapping is ``*_SHAPE_INVALID``, not one row), one
column copying one record key (``normalize_record``, http_json.py:489-520), and parameters that go
into the query string or into one fixed path template whose segments are provably single
(``encode_query``/``render_path``, http_json.py:223, :251-285).

Measured result: none of the 8 rows has that shape, so :data:`DECLARED_MODELS` is empty and
:data:`NOT_DECLARABLE` accounts for all 8, each with the capability id that blocks it. Seven ids are
absent from the engine and no declaration can reach them by re-spelling:

* **flow.branch** -- the addressed path is decided by which parameters the caller set
  (``bill`` / ``bill/{congress}`` / ``bill/{congress}/{bill_type}``, congress_bills.py:268-277).
  ``path`` is one template; an optional, non-defaulted placeholder is refused when the record is
  built (spec.py:706-709), so a branch is not a declaration but a program.
* **flow.fan_out** -- a response publishes more URLs (``pagination.count``/``pagination.next``,
  helpers.py:286-287) or each listed item carries its own detail URL (congress_amendments.py:280).
  ``fetch_pages`` builds its url once (http_json.py:765) and re-sends it with a larger offset, so a
  URL that arrives in a body can never be followed; ``cursor`` paging moves one key, not a path.
* **flow.join** -- a second request's payload supplies columns of the first's rows
  (bill_info.py:111-189). One document is what normalization sees.
* **flow.preflight_lookup** -- the request address itself is derived (here: from caller text,
  bill_text.py:79-85), not from a declared template.
* **path.assemble_single_row** -- the answer is one object assembled from several places
  (bill_info.py:107 reads ``response["bill"]``, a mapping), while the engine's rows must be a list.
* **decode.html_text** and **decode.opaque_bytes** -- bodies that are HTML to be converted, or PDF
  bytes to be handed on as base64 (bill_info.py:215, bill_text.py:106-108). ``DecoderSpec.kind`` is
  the closed set ``json``/``csv``/``tsv``/``zip_csv`` (spec.py:304-305); anything else is refused
  when the declaration is constructed.
* **outside.sdk**, **outside.stateful**, **outside.anti_bot** -- an external client library, a local
  cache that outlives the request, and a rate-limit retry loop (committees.py:1120-1135, :116-131).
  The capability registry marks all three ``engine_work: false``: no engine facet reaches them,
  so no wording of a declaration can either.

Three ids the registry marks as shipped do **not** carry the rows that name them, and this package
records that rather than counting them as coverage: ``rows.order_limit`` orders on one record key
while both list models sort on a nested value with a fallback (``latestAction.actionDate`` or
``updateDate``, congress_bills.py:321-325) and ``order_by`` is refused together with any
``pagination`` (spec.py:829); ``columns.derive`` and ``rows.filter`` cover the derived-URL and
congress-matched halves of CongressCommitteeDocuments only as pieces of a pipeline that is still
branched, fanned out and joined.

Credential face, recorded because it is the first question a future declaration asks: the upstream
dictionary declares ``credentials=["api_key"]`` (``__init__.py:22``) and every upstream request
appends the key to the URL text itself (``&api_key=`` , congress_bills.py:293). The engine can carry
that: ``credential`` plus ``credential_query_key`` puts the token in the query dict
(http_json.py:762-763). Nothing about the key blocks a declaration here -- the blockers are the
document shapes and request counts above.

Data rights: there is **no reviewed clause basis for Congress.gov in this repo**.
The data-rights registry records only a pending source identity, not permission for storage,
redistribution or commercial use; no reviewed clause evidence exists in the C65 review.
"""

from __future__ import annotations

#: The 8 model identities upstream binds for congress_gov, in dictionary order, from
#: ``providers/congress_gov/openbb_congress_gov/__init__.py:23-31``. Same order as the
#: ``OBB2-congress_gov-*`` rows of the iteration-2 ledger.
UPSTREAM_ROWS: tuple[str, ...] = (
    "CongressBills",
    "CongressBillInfo",
    "CongressBillText",
    "CongressAmendments",
    "CongressAmendmentInfo",
    "CongressAmendmentText",
    "CongressCommitteeInfo",
    "CongressCommitteeDocuments",
)

#: Models this package declares as ``ModelSpec`` records. Empty, by measurement: see the docstring.
#: A name added here becomes discoverable through ``catalog.engine_declared_models()`` as soon as
#: ``provider.py`` binds the fetcher its declaration generates -- no other wiring is missing.
DECLARED_MODELS: tuple[str, ...] = ()

#: Every upstream congress_gov row that the declaration format cannot express, and the capability
#: ids that block it. Reasons name the mechanism, not the effort: "not written yet" is no blocker.
NOT_DECLARABLE: dict[str, str] = {
    "CongressBills": "flow.branch, flow.fan_out: the path is chosen by which params are set -- "
    "bill, bill/{congress} or bill/{congress}/{bill_type} (congress_bills.py:268-277), and a "
    "nested latestAction is popped for the sort (:321); limit=0 diverts to an offset-URL fan-out "
    "over pagination.count/next (helpers.py:234)",
    "CongressBillInfo": "flow.join, path.assemble_single_row, decode.html_text: one base GET "
    "(bill_info.py:99) then up to 7 conditional sub-resource GETs whose URLs are read out of that "
    "payload (:111-189) and merged back into a single dict; the answer is one markdown_content "
    "string, not a record list (:257)",
    "CongressBillText": "flow.preflight_lookup, flow.fan_out, decode.opaque_bytes: the request "
    "addresses are caller text normalized into a list (:79-83) and requested one by one "
    "(bill_text.py:101); .pdf bodies become base64 bytes (:106-108) and the rest raw response "
    "text (:121), so no JSON document and no declared path template is involved",
    "CongressAmendments": "flow.branch, flow.fan_out, flow.join: amendment / {congress} / "
    "{congress}/{type} branch (congress_amendments.py:239-245), then each listed item's own detail "
    "url is requested and merged back (:280-308), and limit=0 diverts to a next-URL fan-out "
    "(helpers.py:685, :716-717)",
    "CongressAmendmentInfo": "flow.join, path.assemble_single_row: base amendment GET "
    "(amendment_info.py:103) plus conditional cosponsors/actions/textVersions GETs read from that "
    "payload (:117-133); the output is one assembled markdown_content record (:156) over a body "
    "that is a mapping, not rows",
    "CongressAmendmentText": "flow.preflight_lookup, flow.fan_out, decode.opaque_bytes: caller "
    "URL list normalized (:71-75) and looped (:78), one request per URL (amendment_text.py:91); "
    ".pdf -> base64 bytes (:95-97), otherwise response text (:111); a host check writes error rows "
    "inline (:80-88)",
    "CongressCommitteeInfo": "flow.join, path.assemble_single_row: two unrelated sources gathered "
    "at once (congress_committee_info.py:141-145) -- v3 committee detail plus a static "
    "unitedstates.github.io membership file keyed by a code-derived id (committees.py:16, "
    ":57-62) -- merged into one markdown record (:264)",
    "CongressCommitteeDocuments": "flow.branch, flow.fan_out, flow.join, outside.sdk, "
    "outside.stateful, outside.anti_bot: doc_type-branched pipelines run in parallel "
    "(committees.py:1149-1194) over next-URL paging, PDF URLs concatenated from citation fields "
    "(:249, :788-790), a SQLite response cache (:1120-1135) and rate-limit backoff (:116-131)",
}

#: The capability ids that block each row, spelled the way the roadmap registry spells them, so a
#: re-measurement of the census can be machine-checked against :data:`NOT_DECLARABLE` instead of
#: re-read out of prose. Keys mirror :data:`UPSTREAM_ROWS`.
BLOCKING_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "CongressBills": ("flow.branch", "flow.fan_out"),
    "CongressBillInfo": ("flow.join", "path.assemble_single_row", "decode.html_text"),
    "CongressBillText": ("flow.preflight_lookup", "flow.fan_out", "decode.opaque_bytes"),
    "CongressAmendments": ("flow.branch", "flow.fan_out", "flow.join"),
    "CongressAmendmentInfo": ("flow.join", "path.assemble_single_row"),
    "CongressAmendmentText": ("flow.preflight_lookup", "flow.fan_out", "decode.opaque_bytes"),
    "CongressCommitteeInfo": ("flow.join", "path.assemble_single_row"),
    "CongressCommitteeDocuments": (
        "flow.branch",
        "flow.fan_out",
        "flow.join",
        "outside.sdk",
        "outside.stateful",
        "outside.anti_bot",
    ),
}

#: The half of each row's need set that the engine already ships, recorded because naming it is what
#: keeps a blocker honest: a row is refused for the ids that are missing, not for these. Rows not
#: listed here need nothing that exists today.
SHIPPED_NEEDS_WITHIN: dict[str, tuple[str, ...]] = {
    "CongressBills": ("rows.order_limit",),
    "CongressAmendments": ("rows.order_limit",),
    "CongressCommitteeDocuments": ("columns.derive", "rows.filter"),
}

#: ``file:line`` evidence in the pinned upstream tree for the row named by the key, so a reviewer
#: can re-measure a blocker without re-reading the whole package. Paths are relative to
#: ``providers/congress_gov/openbb_congress_gov/``; shared machinery lives under ``utils/``.
UPSTREAM_EVIDENCE: dict[str, str] = {
    "CongressBills": "models/congress_bills.py:268,312 + utils/helpers.py:234,286",
    "CongressBillInfo": "models/bill_info.py:99,111-189,257",
    "CongressBillText": "models/bill_text.py:79,101,106,121",
    "CongressAmendments": "models/congress_amendments.py:239,275,280 + utils/helpers.py:685,716",
    "CongressAmendmentInfo": "models/amendment_info.py:103,117-133,156",
    "CongressAmendmentText": "models/amendment_text.py:71,91,95,111",
    "CongressCommitteeInfo": "models/congress_committee_info.py:141,264 + utils/committees.py:16",
    "CongressCommitteeDocuments": "utils/committees.py:1097,1149 + utils/congress_search.py:16",
}

#: Recorded, never declared: the credential spelling the upstream dictionary publishes
#: (``__init__.py:22``, ``credentials=["api_key"]``), which ``catalog._credential`` turns into the
#: ``CONGRESS_GOV_API_KEY`` environment variable this package's descriptor declares.
UPSTREAM_CREDENTIAL_NAME = "api_key"
