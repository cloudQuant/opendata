"""Offline witness configuration for the ``multpl`` provider.

Multpl publishes its S&P 500 valuation history as a visible HTML ``Date``/``Value`` table, so
the fixture is that page in exactly the carrier ``SP500MultiplesFetcher._get_html`` reads: the
transport's body, losslessly, as the list of the page's own source lines. The refusal here is
the source's own stable shape vocabulary (``MultplProviderError``, which is the engine's
attributable refusal type), and the code is deliberately bare -- the page has no column key for
a refusal to attribute.
"""

from __future__ import annotations

from typing import Any, Final

# One synthetic monthly table for the ``pe_ratio`` page, in the source's own markup.
_PE_RATIO_PAGE: Final[list[str]] = [
    "<html><body>",
    '<table class="table">',
    "<tr><th>Date</th><th>Value</th></tr>",
    "<tr><td>Oct 9, 2026</td><td>26.2</td></tr>",
    "<tr><td>Sep 1, 2026</td><td>25.87</td></tr>",
    "<tr><td>Aug 1, 2026</td><td>24.1&dagger;</td></tr>",
    "</table></body></html>",
]


def _multpl_pe_ratio_page() -> list[str]:
    """The ``/s-p-500-pe-ratio/table/by-month`` body the fetcher parses."""
    return list(_PE_RATIO_PAGE)


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.multpl.registration",
    "credential_env": {},
    "allowed_hosts": ["www.multpl.com"],
    "models": {
        "SP500Multiples": {
            "fixture_document": _multpl_pe_ratio_page,
            "query": {"metric": "pe_ratio"},
            "tamper": {
                "op": "set",
                "path": [3],
                "value": "<tr><td>Oct 9, 2026</td><td>NaN</td></tr>",
            },
            "refusal_class": "MultplProviderError",
            "refusal_code": "MULTPL_BAD_VALUE",
            "tamper_reason": (
                "publishing a Value cell of 'NaN' where the contract declares a finite decimal "
                "ratio must make the table parser refuse the page with MULTPL_BAD_VALUE rather "
                "than emit an observation the strict SP500Multiples row (allow_inf_nan=False) "
                "could never hold"
            ),
            "source_files": [
                "opendata/data/providers/multpl/models/sp500_multiples.py",
                "opendata/data/providers/multpl/specs.py",
            ],
            "contract_files": [
                "opendata/data/models/multpl.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
