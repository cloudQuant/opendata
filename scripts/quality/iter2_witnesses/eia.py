"""Offline witness configuration for the ``eia`` provider.

EIA owns its transport: every leg of both fetchers goes through
:mod:`opendata.data.providers.eia.models._client`, which reads the module attribute
``get_shared_http_client`` on each call. Each witness therefore installs the repository's fake
governed client at that one attribute (:mod:`iter2_claims_probe_runner._client_seam`), so the seam
hands the shipped fetcher the bytes a real answer would have carried. No leg touches a socket --
the runner refuses socket use for the whole process -- and the URL allowlist
(``api.eia.gov`` / ``ir.eia.gov`` / ``www.eia.gov``), the CSV and JSON shape guards, the page-total
and frequency checks and the row contracts under test are the shipped ones.

Both bodies are transcribed from the receipt fixtures this provider's evidence history retains
(``docs/evidence/C100/acceptance-congress-cftc-eia/fixtures/``: ``eia_psr_table1.csv``, 80 bytes,
sha256 ``bd6401d7e9192346``; ``eia_steo_response.json``, 475 bytes, sha256 ``55980df9a1a93211``).
They are **hand-authored fixture bodies, not live captures** -- the dates are 2025 week and month
ends chosen for the fixture, not a claim that WPSR table 1 or the STEO API holds these values --
so they prove the mapping and the gates. Each is published as the source publishes it: the WPSR
CSV as the two official lines (``STUB_n`` header columns plus two ISO date columns), and the API
answer wrapped in its ``{"response": ...}`` envelope with the ``total`` string and ``frequency``
label the v2 endpoint itself returns. The CSV adapter ignores the returned ``Content-Type``, so the
seam's ``text/html`` default for a list of lines is not load-bearing for that leg.

The refusals are the provider's own stable codes (``EIAProviderError``, which carries ``code`` and
``status``). Both are deliberately code-only and both carry ``status=None`` because each is a
post-response judgement about a body the source did serve: a stock cell that is neither a number
nor one of EIA's undisclosed-value tokens, and a page that answers a different series than the one
filtered for. Neither refusal reaches a contract column, so no cell exists for the witness to
attribute.

What this module does not cover: STEO's archived-workbook branch (``symbol=None`` or a
``release_date`` selector) needs an archive HTML page and a real ``.xlsx`` behind it, so its
counterfact would prove openpyxl plumbing rather than a contract row; the WPSR categories other than
``balance_sheet`` and ``weekly_estimates`` publish no ``_VERIFIED_TABLES_BY_CATEGORY`` entry, so
their query validator refuses any ``table`` selector before a witness could reach the parser; and
the remaining EIA guard codes (``EIA_URL_REJECTED``, ``EIA_HTTP_ERROR``, ``EIA_API_KEY_MISSING``,
``EIA_BAD_TOTAL``, ``EIA_TRUNCATED_PAGE``, ``EIA_STEO_*``) are transport or credential shapes that a
seam which already answers the request cannot reach with a one-page fixture.
"""

from __future__ import annotations

import copy
from typing import Any, Final

#: The one attribute both fetchers' transport reads per request; the runner patches it in
#: ``models/_client.py`` only.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.eia.models._client"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/eia/models/_client.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/eia.py",
    "opendata/data/domains.yaml",
]

#: One WPSR balance-sheet table exactly as the retained CSV fixture publishes it: three stub
#: hierarchy columns, two dated observation columns, and one stocks row.
_PSR_LINES: Final[list[str]] = [
    "STUB_1,STUB_2,STUB_3,2025-03-05,2025-02-26",
    "Crude oil,Ending stocks,,430.2,428.7",
]

#: One STEO API page as the retained JSON fixture publishes it, including the source's own
#: string-typed ``total`` and the ``w`` withheld marker on the second observation.
_STEO_SERIES: Final[str] = "PET.RWTC.D"
_STEO_BODY: Final[dict[str, Any]] = {
    "response": {
        "total": "2",
        "frequency": "monthly",
        "data": [
            {
                "period": "2025-01",
                "seriesId": _STEO_SERIES,
                "value": "0",
                "value-units": "dollars per barrel",
                "seriesName": "World crude oil price",
            },
            {
                "period": "2025-02",
                "seriesId": _STEO_SERIES,
                "value": "w",
                "value-units": "dollars per barrel",
                "seriesName": "World crude oil price",
            },
        ],
    }
}


def _psr_document() -> dict[str, Any]:
    """The table 1 CSV the offline seam answers the WPSR request with."""
    return {
        "responses": [
            {"url_contains": "wpsr/table1.csv", "body": list(_PSR_LINES)},
        ]
    }


def _steo_document() -> dict[str, Any]:
    """The STEO series page the offline seam answers the API request with."""
    return {
        "responses": [
            {"url_contains": "/v2/steo/data/", "body": copy.deepcopy(_STEO_BODY)},
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.eia.provider",
    "allowed_hosts": ["ir.eia.gov", "api.eia.gov"],
    "credential_env": {"EIA_API_KEY": "fixture-key-never-sent"},
    "models": {
        "PetroleumStatusReport": {
            "fixture_document": _psr_document,
            "client_seam": _SEAM,
            "query": {"category": "balance_sheet"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", 1],
                "value": "Crude oil,Ending stocks,,43O.2,428.7",
            },
            "refusal_class": "EIAProviderError",
            "refusal_code": "EIA_BAD_NUMBER",
            "tamper_reason": (
                "a stock cell written 43O.2 keeps the row's stub path, its table title and both "
                "dated columns intact and still parses as CSV, but it is neither a number nor one "
                "of EIA's documented undisclosed-value tokens, and value is a required contract "
                "column, so PetroleumStatusReportFetcher.transform_data must refuse the table "
                "rather than coerce the cell to 0, drop the row silently, or invent a unit for it"
            ),
            "source_files": [
                "opendata/data/providers/eia/models/petroleum_status_report.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "ShortTermEnergyOutlook": {
            "fixture_document": _steo_document,
            "client_seam": _SEAM,
            "query": {"symbol": _STEO_SERIES, "frequency": "month"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "response", "data", 0, "seriesId"],
                "value": "PET.RWTE.D",
            },
            "refusal_class": "EIAProviderError",
            "refusal_code": "EIA_BAD_SERIES_ID",
            "tamper_reason": (
                "answering PET.RWTE.D on a request filtered for PET.RWTC.D leaves the page total, "
                "the monthly frequency label and every value well-formed, so series identity is "
                "the only thing that can catch it: ShortTermEnergyOutlookFetcher must refuse the "
                "row in extract_data and again in transform_data rather than publish another "
                "series' observation under the series the caller asked for"
            ),
            "source_files": [
                "opendata/data/providers/eia/models/short_term_energy_outlook.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
