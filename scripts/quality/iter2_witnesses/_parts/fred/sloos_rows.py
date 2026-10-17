"""Wave 20 shard of the ``fred`` offline witness configuration: the SLOOS category-map row.

This file publishes ``MODELS`` for one fred inventory row, ``SeniorLoanOfficerSurvey``, and
``iter2_claims_fixtures`` merges it into ``scripts/quality/iter2_witnesses/fred.py``'s
``CONFIG["models"]`` (a duplicated key fails closed). Nothing here re-declares the 35 rows the
base module and wave-8 shard already witness.

Seam (never a socket, never ``OPENDATA_ALLOW_LIVE_E2E``): the bound fetcher is
``FredSeniorLoanOfficerSurveyFetcher`` (``models/sloos.py:170``), which the registry resolves for
this canonical model instead of the bulk-release ``SeniorLoanOfficerSurveyFetcher`` in
``iter2_macro.py:1744``. It declares no ``http_transport``, so every leg leaves through
``_client._http_get`` -> ``get_shared_http_client()`` (``models/_client.py:115``), and the entry
declares ``client_seam`` on that module. ``_http_get`` reads the attribute at request time, so
patching it is what keeps the governed client out of the process.

Fan-out and the page header: ``extract_data`` walks ``series_for_category(params.category)`` and
asks for one page per series (``sloos.py:209-233``). The fake client matches on ``url_contains``
only -- FRED carries ``series_id`` as a query parameter, never in the path -- so the carrier holds
ONE entry whose ``sequence`` carries exactly one page per leg, in map order. Each page echoes its
own ``series_id``, and the per-observation echo check in ``transform_data`` (``sloos.py:267-272``)
refuses a page that names another series.

The decreasing ``limit`` is the trap this shard exists to document. In the page fetcher,
``_fetch_single_observation_page``, the echoed ``limit`` is compared with the page size of the
leg that asked (``sloos.py:148``), and ``extract_data`` computes that size from the records
*already consumed*: ``remaining_records = max_records - records_used`` and then
``min(_OBSERVATION_PAGE_LIMIT, remaining_records)`` (``sloos.py:216,230``). Six legs of two
observations each therefore demand ``10000, 9998, 9996, 9994, 9992, 9990`` -- not the constant
``_MACRO_PAGE_LIMIT`` the generic pager accepts. The builder below simulates that accounting
instead of hardcoding the six numbers, so a map that grows or a query bound that changes moves
the header with it rather than failing silently.

Observation dates are quarter-ends inside every one of the six series' published windows
(``1996-03-31 -> 2026-09-30`` is the narrowest of the six per ``sloos_series.py``), and the query
passes the same window so the ``before observation_start`` / ``after observation_end`` guards
(``sloos.py:276-279``) are exercised rather than vacuous. ``count`` always equals the number of
observations in its own page, so ``FRED_PAGINATION_ERROR`` (``sloos.py:165``) stays a live guard.

Provenance, stated plainly: FRED requires an ``api_key``, and no key is configured here, so the
replayed pages are shaped to the documented observation-page contract
(``opendata/data/models/fred_sloos.py`` plus the header checks above) rather than cut from a
retained official capture. That is the same provenance class as the 35 already-accepted fred rows;
the ``credential_env`` token in the base module is a fixture value and is never sent anywhere.

Refusal: the adapter raises ``FredProviderError`` with a code only (``models/_client.py:29``), so
this entry declares no ``expected_missing`` / ``expected_rejected`` / ``expected_invalid`` face --
the empty attribution faces are what the contract derives, not what a run showed. The tamper writes
prose into the one source token that feeds the row's required ``value`` float column
(``FredSeniorLoanOfficerSurvey.value``), which is the cell the declared ``FRED_BAD_OBSERVATION``
covers (``sloos.py:305-306``).
"""

from __future__ import annotations

from typing import Any, Final

_SOURCE_REFUSAL_CLASS: Final[str] = "FredProviderError"
_OBSERVATIONS_URL: Final = "/fred/series/observations"
_REALTIME_START: Final = "2026-01-06"
_REALTIME_END: Final = "2026-01-06"

#: ``FredSeniorLoanOfficerSurveyQuery.max_records`` default (``models/fred_sloos.py``).
_MAX_RECORDS: Final = 10_000
#: ``_OBSERVATION_PAGE_LIMIT`` in ``models/sloos.py:28``.
_PAGE_CEILING: Final = 100_000
#: Two quarter-end observations per leg, so the twelve-row page is 6 legs x 2 cells.
_OBSERVATIONS_PER_LEG: Final = 2

#: ``series_for_category("spreads")`` in declaration order (``fred/sloos_series.py``). The order is
#: the witness's claim about the map: if the map gains a leg the sequence runs out and the runner
#: refuses, rather than replaying the last page a seventh time.
_SPREADS_SERIES: Final[tuple[str, ...]] = (
    "DRISCFLM",
    "DRISCFS",
    "SUBLPDCLATSNQ",
    "SUBLPDCLCTSNQ",
    "SUBLPDCLXTSNQ",
    "SUBLPFCITSNQ",
)

#: Net percentages of domestic/foreign banks reporting tighter terms, one pair per leg in the map's
#: own order. Values are distinct per series so a page replayed onto the wrong leg shows up in the
#: row digests the direct and service phases print.
_SPREADS_TOKENS: Final[dict[str, tuple[str, str]]] = {
    "DRISCFLM": ("32.5", "28.0"),
    "DRISCFS": ("22.0", "18.5"),
    "SUBLPDCLATSNQ": ("12.5", "9.0"),
    "SUBLPDCLCTSNQ": ("15.0", "11.5"),
    "SUBLPDCLXTSNQ": ("20.0", "16.5"),
    "SUBLPFCITSNQ": ("5.0", "8.5"),
}

_WINDOW_START: Final = "2026-03-31"
_WINDOW_END: Final = "2026-06-30"


def _observation(day: str, series_id: str, token: str) -> dict[str, str]:
    """One FRED observation row, echoing the series its own leg asked for.

    ``series_id`` inside the observation is the field ``transform_data`` cross-checks against the
    requested series, so echoing it makes a mis-sequenced page refuse instead of mislabelling rows.
    """
    return {
        "date": day,
        "series_id": series_id,
        "value": token,
        "realtime_start": _REALTIME_START,
        "realtime_end": _REALTIME_END,
    }


def _observation_page(*, series_id: str, limit: int) -> dict[str, Any]:
    """One complete page for one leg, carrying the ``limit`` that leg actually asked with."""
    values = _SPREADS_TOKENS[series_id]
    return {
        "realtime_start": _REALTIME_START,
        "realtime_end": _REALTIME_END,
        "observation_start": _WINDOW_START,
        "observation_end": _WINDOW_END,
        "limit": limit,
        "offset": 0,
        "count": len(values),
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "units": "lin",
        "output_type": 1,
        "series_id": series_id,
        "observations": [
            _observation(day, series_id, token)
            for day, token in ((_WINDOW_START, values[0]), (_WINDOW_END, values[1]))
        ],
    }


def _fred_senior_loan_officer_survey_document() -> dict[str, Any]:
    """Six SLOOS spread pages whose headers echo the record accounting ``extract_data`` performs."""
    pages: list[dict[str, Any]] = []
    consumed = 0
    for series_id in _SPREADS_SERIES:
        remaining = _MAX_RECORDS - consumed
        page_size = min(_PAGE_CEILING, remaining)
        pages.append(_observation_page(series_id=series_id, limit=page_size))
        consumed += _OBSERVATIONS_PER_LEG
    return {"responses": [{"url_contains": _OBSERVATIONS_URL, "sequence": pages}]}


MODELS: Final[dict[str, dict[str, Any]]] = {
    "SeniorLoanOfficerSurvey": {
        "fixture_document": _fred_senior_loan_officer_survey_document,
        "query": {"category": "spreads", "start_date": _WINDOW_START, "end_date": _WINDOW_END},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 3, "observations", 1, "value"],
            "value": "not-a-percentage",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "publishing a non-numeric 'value' token in one leg's page breaks the documented "
            "source-token float conversion every SLOOS spread observation goes through, so the "
            "adapter must refuse the whole selection with FRED_BAD_OBSERVATION surfaced as a "
            "ProviderEngineError-shaped FredProviderError instead of coercing the cell or "
            "silently dropping the observation"
        ),
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models._client"],
            "attr": "get_shared_http_client",
        },
        "source_files": [
            "opendata/data/providers/fred/models/_client.py",
            "opendata/data/providers/fred/models/sloos.py",
            "opendata/data/providers/fred/sloos_series.py",
            "opendata/data/request_budget.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_sloos.py",
            "opendata/data/domains.yaml",
        ],
    },
}
