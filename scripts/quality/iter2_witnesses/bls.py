"""Offline witness configuration for the ``bls`` provider.

Both BLS legs are hand-written adapters over the governed HTTP client, so each witness installs
the repository's ``FixedResponseTransport`` on ``http_transport`` and the client's seam branch
reads the body off it. BLS publishes the survey catalog as a tab-delimited bulk file, which JSON
carries losslessly as the list of its own source lines; the observations leg is the official
v1 API's JSON document with its own keys.

The refusals here are BLS's own stable codes (``BlsProviderError``, the engine's attributable
refusal type for this source) and they are deliberately code-only: a corrupt bulk record or a
non-numeric observation value is refused before any column of the contract is ever addressed,
so there is no cell for the refusal to attribute.
"""

from __future__ import annotations

from typing import Any, Final

# The official ``ci.series`` header, then two catalog records, as the bulk file publishes them.
_CATALOG_TSV: Final[list[str]] = [
    "series_id\tfrequency\tperiod\tedition\tseasonal\tcategory-included\tdata_type\tdimensions"
    "\tseries_title",
    "CUUR0000SA0\tM\tM\t1\tS\t\tN\tnotseasonallyadjusted\t"
    "Consumer Price Index for All Urban Items: All Items: U.S. City Average",
    "CUURS0000SA0\tM\tM\t1\tS\t\tN\tnotseasonallyadjusted\t"
    "Consumer Price Index for All Urban Items: All Items: Northeast Region",
]

_CU_OBSERVATIONS: Final[dict[str, Any]] = {
    "status": "REQUEST_SUCCEEDED",
    "responseTime": 21,
    "message": [],
    "Results": {
        "series": [
            {
                "seriesID": "CUUR0000SA0",
                "concept": "CU",
                "dataItem": "CUUR0000SA0",
                "dimension": "S0",
                "series_title": "Consumer Price Index, U.S. City Average",
                "data": [
                    {
                        "year": "2026",
                        "period": "M01",
                        "periodName": "January",
                        "value": "324.512",
                        "footnotes": [],
                        "latest": "false",
                    },
                    {
                        "year": "2026",
                        "period": "M02",
                        "periodName": "February",
                        "value": "325.004",
                        "footnotes": [{"code": "P", "text": "preliminary"}],
                        "latest": "true",
                    },
                ],
            }
        ]
    },
}


def _bls_catalog_page() -> list[str]:
    """The ``ci/ci.series`` bulk file the catalog search reads, line for line."""
    return list(_CATALOG_TSV)


def _bls_series_document() -> dict[str, Any]:
    """One v1 time-series POST answer for the single requested series and year."""
    return {
        "status": _CU_OBSERVATIONS["status"],
        "responseTime": _CU_OBSERVATIONS["responseTime"],
        "message": [],
        "Results": {
            "series": [
                {
                    "seriesID": "CUUR0000SA0",
                    "concept": "CU",
                    "dataItem": "CUUR0000SA0",
                    "dimension": "S0",
                    "series_title": "Consumer Price Index, U.S. City Average",
                    "data": [dict(row) for row in _CU_OBSERVATIONS["Results"]["series"][0]["data"]],
                }
            ]
        },
    }


_CONFIG_FILES: Final[list[str]] = [
    "opendata/data/providers/bls/models/_client.py",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.bls.registration",
    # An empty key selects the official no-registration v1 endpoint the fixture answers.
    "credential_env": {"BLS_API_KEY": ""},
    "allowed_hosts": ["api.bls.gov", "download.bls.gov"],
    "models": {
        "BlsSearch": {
            "fixture_document": _bls_catalog_page,
            "query": {"survey": "ci"},
            "tamper": {
                "op": "set",
                "path": [1],
                "value": "\tM\tM\t1\tS\t\tN\tnotseasonallyadjusted\t"
                "Consumer Price Index for All Urban Items: All Items: U.S. City Average",
            },
            "refusal_class": "BlsProviderError",
            "refusal_code": "BLS_CATALOG_MISSING_ID",
            "expected_status": 200,
            "tamper_reason": (
                "publishing a catalog record with an empty series_id field strips the identity "
                "column the BlsCatalogItem contract requires, so the parser must refuse the file "
                "with BLS_CATALOG_MISSING_ID rather than hand back an unaddressable series"
            ),
            "source_files": [
                *_CONFIG_FILES,
                "opendata/data/providers/bls/models/search.py",
            ],
            "contract_files": [
                "opendata/data/models/period_series.py",
                "opendata/data/domains.yaml",
            ],
        },
        "BlsSeries": {
            "fixture_document": _bls_series_document,
            "query": {
                "series_ids": ["CUUR0000SA0"],
                "start_year": 2026,
                "end_year": 2026,
                "max_requests": 5,
            },
            "tamper": {
                "op": "set",
                "path": ["Results", "series", 0, "data", 0, "value"],
                "value": "n/a",
            },
            "refusal_class": "BlsProviderError",
            "refusal_code": "BLS_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing 'n/a' in the documented numeric value field makes the observation "
                "parser refuse instead of inferring a missing or zero value, so BlsSeries must "
                "raise BLS_BAD_OBSERVATION rather than emit a row the strict BlsObservation "
                "contract cannot hold"
            ),
            "source_files": [
                *_CONFIG_FILES,
                "opendata/data/providers/bls/models/series.py",
            ],
            "contract_files": [
                "opendata/data/models/period_series.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
