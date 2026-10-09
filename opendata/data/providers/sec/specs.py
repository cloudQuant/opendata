"""sec's 24 upstream rows measured against the declarative engine, not hand-written around it.

The sec denominator is the pinned upstream provider dictionary
(``providers/sec/openbb_sec/__init__.py:36-59``, ledger ids ``OBB2-sec-<model>``). Every row was
read in that tree and judged against what the shared engine actually performs at query time
(:mod:`opendata.data.providers._engine.http_json`): one GET whose body decodes as JSON, a record
list that *is* a JSON array (``resolve_rows`` raises ``*_SHAPE_INVALID`` for anything else,
http_json.py:218), output columns that are keys of each record or of the enclosing document
(``normalize_record``, http_json.py:236-263), and parameters that go into the query string or into
one fixed path template (``encode_query``/``render_path``, http_json.py:142-178).

Measured result: none of the 24 rows has that shape, so :data:`DECLARED_MODELS` is empty and
:data:`NOT_DECLARABLE` accounts for all 24 with the capability that blocks each one. The blockers
are not stylistic; four engine capabilities would have to exist first:

* **non-row documents** - ``company_tickers.json`` is an object keyed by row ordinal
  (helpers.py:57), ``company_tickers_mf.json`` is an array of bare arrays named by a ``fields``
  list (helpers.py:123), and ``submissions`` filings are column-oriented parallel arrays
  (company_filings.py:233). Nothing in the engine turns any of those into records.
* **derived columns** - several published columns are built after the response arrives, by string
  concatenation (company_filings.py:319-331) or by a join against a second request
  (frames.py:176-181). The engine reads a value out of the document or it emits a null.
* **more than one request** - a pre-flight decides the URL (equity_ftd.py:74), a second request
  fills columns (nport_disclosure.py:255), or a document is re-read per filing
  (form_13FHR.py:77, latest_financial_reports.py:165).
* **other formats** - Form 4 and NPORT XML (form4.py:295, nport_disclosure.py:326), an RSS feed
  (rss_litigation.py:73), HTML tables (sic_search.py:96) and HTML-to-markdown bodies
  (sec_filing.py:542, htm_file.py:97) are not JSON documents.

SEC fair-access headers, recorded because it is the first question any future sec declaration asks:
upstream's ``User-Agent`` is a **literal default**, not a value composed from a caller-supplied
email - ``openbb_sec/utils/definitions.py:9-13`` and ``:17-20`` both send
``"my real company name definitelynot@fakecompany.com"`` (``latest_financial_reports.py:117-118``
repeats it inline; ``utils/form4.py:8-9`` sends a different literal). So ``static_headers`` *can*
carry it verbatim; nothing about the header blocks a declaration. The upstream literal is a
placeholder contact address, which is why it is recorded here rather than wired into a production
declaration. One caveat for whoever declares first: ``SEC_HEADERS`` also pins a ``Host`` field to
``www.sec.gov`` (definitions.py:12), and upstream sends the ``HEADERS`` variant without it to
``data.sec.gov`` (company_filings.py:225) because, as its own comment says, some endpoints do not
like that field -- so the pair a declaration sends has to be chosen per host, not per provider.

Re-check of the three rows the roadmap lists as already covered (iteration-2 round):
``capability-roadmap.json`` puts ``CashFlowStatement``, ``IncomeStatement`` and
``InstitutionsSearch`` in ``roadmap.shipped_cover_rows``, each with a single need that names a
capability the engine does ship (``columns.select`` for the two statements, ``decode.delimited``
for the filer list). That list is a label-to-capability mapping over the *engine-work residue*,
and it is not a re-measurement: the
census rows themselves, re-read this round from ``census-sec-tmx-fed-gov-finra.json``, still carry
``expressible_today: false`` and name the need that blocks them -- ``xbrl_tag_assembly`` and
``tag_column_selection`` for the statements, ``delimited_rows_without_published_header`` for the
filer list. The engine's shipped ``decode.delimited`` reads a delimited body only when its first row
publishes a header naming a declared column (:meth:`decoders._rows_from_text`, which raises
``HEADER_MISMATCH`` otherwise), and a column copies exactly one fixed source key
(``normalize_record``), so neither need is met by what exists. Independently of the capability
question, no endpoint, column name or JSON pointer for these three rows is traceable in this repo:
no ``sec`` module is vendored under any ``opendata/data/providers/*/_vendor`` tree (the repo has
exactly one such tree today), this package has never held a hand-written fetcher, and the only
``sec.gov`` strings in the tree are the provider
website and the prose cites above. Rule 2 of the declaration contract -- every URL and column must
trace to a recorded fact -- therefore refuses all three by itself. So ``DECLARED_MODELS`` is still
empty, ``provider.py`` still binds nothing, and :data:`RECHECKED_THIS_ROUND` names the three rows
with the census record that settles each one.
"""

from __future__ import annotations

#: The 24 model identities upstream binds for sec, in dictionary order, from
#: ``providers/sec/openbb_sec/__init__.py:36-59``. ``Filings`` is a second key on the
#: ``CompanyFilings`` fetcher and ``SecHtmFile``/``SecFiling`` keep their upstream prefixes.
UPSTREAM_ROWS: tuple[str, ...] = (
    "BalanceSheet",
    "BalanceSheetGrowth",
    "CashFlowStatement",
    "CashFlowStatementGrowth",
    "CikMap",
    "CompanyFilings",
    "CompareCompanyFacts",
    "EquityFTD",
    "EquitySearch",
    "Filings",
    "Form13FHR",
    "SecHtmFile",
    "IncomeStatement",
    "IncomeStatementGrowth",
    "InsiderTrading",
    "InstitutionsSearch",
    "LatestFinancialReports",
    "ManagementDiscussionAnalysis",
    "NportDisclosure",
    "RssLitigation",
    "SchemaFiles",
    "SecFiling",
    "SicSearch",
    "SymbolMap",
)

#: Models this package declares as ``ModelSpec`` records. Empty, by measurement: see the docstring.
#: Re-measured this round by the three-row re-check below, which refused all three; this module
#: still constructs no ``ModelSpec``.
#: A name added here becomes discoverable through ``catalog.engine_declared_models()`` as soon as
#: ``provider.py`` binds the fetcher its declaration generates - no other wiring is missing.
DECLARED_MODELS: tuple[str, ...] = ()

#: The three rows ``capability-roadmap.json`` lists under ``roadmap.shipped_cover_rows`` against a
#: capability the engine already ships (``columns.select`` for the statements, ``decode.delimited``
#: for the filer list), each mapped to the census record that re-measured it this round. All three
#: stayed in :data:`NOT_DECLARABLE`: that roadmap list maps need *labels* over the engine-work
#: residue and re-runs no measurement -- its own ``method_note`` says "no engine code was executed,
#: no recorded fixture was replayed" -- while the census row still carries the blocking need
#: (``xbrl_tag_assembly``/``tag_column_selection``, ``delimited_rows_without_published_header``) and
#: the ``expressible_today: false`` a declaration has to obey.
RECHECKED_THIS_ROUND: dict[str, str] = {
    "CashFlowStatement": "OBB2-sec-CashFlowStatement",
    "IncomeStatement": "OBB2-sec-IncomeStatement",
    "InstitutionsSearch": "OBB2-sec-InstitutionsSearch",
}

#: Every upstream sec row that the declaration format cannot express, and the capability that
#: blocks it. Reasons name the mechanism, not the effort: "not written yet" is not a blocker.
NOT_DECLARABLE: dict[str, str] = {
    "BalanceSheet": "no record list exists: companyfacts JSON is a facts/taxonomy/tag/units "
    "nesting and one row is assembled per XBRL tag (utils/company_facts.py:272, :430)",
    "BalanceSheetGrowth": "per-tag assembly plus a prior-period lookup the engine cannot express "
    "(utils/company_facts.py:335-345)",
    "CashFlowStatement": "needs xbrl_tag_assembly + tag_column_selection (census "
    "OBB2-sec-CashFlowStatement: expressible_today=false, join_needed=true over 2 endpoints, "
    "evidence http_json.py:296 - one column copies one fixed source key); upstream picks each "
    "value by XBRL tag (models/cash_flow.py:431)",
    "CashFlowStatementGrowth": "per-tag assembly plus prior-period growth "
    "(utils/company_facts.py:345, models/cash_flow_growth.py:427)",
    "IncomeStatement": "needs xbrl_tag_assembly + tag_column_selection (census "
    "OBB2-sec-IncomeStatement: expressible_today=false, join_needed=true over 2 endpoints, "
    "evidence http_json.py:296 - one column copies one fixed source key); upstream builds each "
    "row from tag nesting (models/income_statement.py:596)",
    "IncomeStatementGrowth": "per-tag assembly plus prior-period growth "
    "(models/income_statement_growth.py:569)",
    "CikMap": "one value chosen by a pandas filter over company_tickers.json, whose document is an "
    "object keyed by row ordinal (utils/helpers.py:57, :140-144)",
    "SymbolMap": "reverse of CikMap: the symbol is picked out of the same dict-of-records with "
    "``.iloc[0]`` (utils/helpers.py:167-175)",
    "EquitySearch": "client-side substring filter over the ticker dictionary, and the ``is_fund`` "
    "branch addresses a second document whose rows are bare arrays named by ``fields`` "
    "(models/equity_search.py:66-84, utils/helpers.py:123)",
    "InstitutionsSearch": "needs delimited_rows_without_published_header (census "
    "OBB2-sec-InstitutionsSearch: shape=binary_or_other, expressible_today=false, evidence "
    "decoders.py:283 - a delimited body's header must name a declared column, else "
    "HEADER_MISMATCH); upstream splits a colon-delimited text file (utils/helpers.py:69, :89)",
    "SicSearch": "an HTML page parsed by ``read_html``, then filtered client-side "
    "(models/sic_search.py:77, :96-106)",
    "CompanyFilings": "``filings.recent`` is column-oriented parallel arrays; three published "
    "columns are URLs concatenated after the call and past-1000 filings fetches ``filings.files`` "
    "(models/company_filings.py:233, :319-331, :251-267)",
    "Filings": "alias of CompanyFilings in the upstream dictionary "
    "(``__init__.py:45``), so it inherits the same blocker",
    "CompareCompanyFacts": "the ``symbol`` column is joined from a second request and the frame "
    "URL branches on ``instantaneous``/``fiscal_period`` and on computed defaults "
    "(utils/frames.py:128-136, :176-181; models/compare_company_facts.py:148-170)",
    "EquityFTD": "a pre-flight of https://www.sec.gov/data.json decides the URLs and the bodies "
    "are zipped pipe-delimited CSV (models/equity_ftd.py:74, utils/helpers.py:274, :226)",
    "Form13FHR": "each filing is fetched from a submissions preflight and parsed as XML "
    "(models/form_13FHR.py:60-84, utils/parse_13f.py:65-68)",
    "InsiderTrading": "Form 4 XML documents, one request per filing, remapped through a field "
    "dictionary (models/insider_trading.py:204, utils/form4.py:282-297)",
    "LatestFinancialReports": "records sit under a nested ``_source`` per Elasticsearch hit, the "
    "published columns are derived from them, and index headers are fetched per filing "
    "(models/latest_financial_reports.py:153-171, :200-230)",
    "ManagementDiscussionAnalysis": "runs the CompanyFilings fetcher, then reads HTML filing "
    "indexes and converts the document body to markdown "
    "(models/management_discussion_analysis.py:74-75, :603)",
    "NportDisclosure": "an efts search plus a fund-map join decide the filing, whose body is "
    "parsed as XML (models/nport_disclosure.py:255, :326)",
    "RssLitigation": "the endpoint is an RSS XML feed; columns come from renamed DataFrame fields "
    "(models/rss_litigation.py:61, :73-78)",
    "SchemaFiles": "three progressive modes read XBRL taxonomy XML/HTML through an object "
    "graph and flatten it (models/schema_files.py:241-295)",
    "SecFiling": "one row assembled from the filing index JSON, a company concept and an HTML "
    "cover page (models/sec_filing.py:351-372, :542)",
    "SecHtmFile": "the request is an .htm file converted to markdown text, not JSON "
    "(models/htm_file.py:71-72, :97)",
}

#: ``file:line`` evidence in the pinned upstream tree for the row named by the key, so a reviewer
#: can re-measure a blocker without re-reading the whole package. Paths are relative to
#: ``providers/sec/openbb_sec/``; shared machinery lives under ``utils/``.
UPSTREAM_EVIDENCE: dict[str, str] = {
    "BalanceSheet": "models/balance_sheet.py:665 + utils/company_facts.py:573,272",
    "BalanceSheetGrowth": "models/balance_sheet_growth.py:617 + utils/company_facts.py:335-345",
    "CashFlowStatement": "models/cash_flow.py:431 + utils/company_facts.py:573",
    "CashFlowStatementGrowth": "models/cash_flow_growth.py:427 + utils/company_facts.py:345",
    "IncomeStatement": "models/income_statement.py:596 + utils/company_facts.py:573",
    "IncomeStatementGrowth": "models/income_statement_growth.py:569 + utils/company_facts.py:345",
    "CikMap": "models/cik_map.py:50 + utils/helpers.py:37,57,140",
    "SymbolMap": "models/symbol_map.py:49 + utils/helpers.py:167-175",
    "EquitySearch": "models/equity_search.py:66-84 + utils/helpers.py:37,108",
    "InstitutionsSearch": "models/institutions_search.py:66-70 + utils/helpers.py:69,89",
    "SicSearch": "models/sic_search.py:77,96",
    "CompanyFilings": "models/company_filings.py:216,233,319",
    "Filings": "__init__.py:45 (same fetcher as CompanyFilings)",
    "CompareCompanyFacts": "models/compare_company_facts.py:148 + utils/frames.py:128,176",
    "EquityFTD": "models/equity_ftd.py:74 + utils/helpers.py:274,226",
    "Form13FHR": "models/form_13FHR.py:60-84 + utils/parse_13f.py:65",
    "InsiderTrading": "models/insider_trading.py:204 + utils/form4.py:282",
    "LatestFinancialReports": "models/latest_financial_reports.py:136,165,200",
    "ManagementDiscussionAnalysis": "models/management_discussion_analysis.py:74,603",
    "NportDisclosure": "models/nport_disclosure.py:255,326",
    "RssLitigation": "models/rss_litigation.py:61,73",
    "SchemaFiles": "models/schema_files.py:241,293",
    "SecFiling": "models/sec_filing.py:351,542",
    "SecHtmFile": "models/htm_file.py:71,97",
}

#: Recorded, never declared: the literal ``User-Agent`` upstream sends on every sec request
#: (``utils/definitions.py:10``). It is a placeholder contact address, so it belongs in a
#: declaration only once a real filer address is supplied by configuration.
UPSTREAM_USER_AGENT = "my real company name definitelynot@fakecompany.com"
