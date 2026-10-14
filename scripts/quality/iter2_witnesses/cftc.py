"""Offline witness configuration for the ``cftc`` provider.

Both CFTC legs are hand-written adapters over the Socrata JSON API, so the fixture is exactly the
document ``_client.get_json`` returns: a JSON array of one report's rows in the source's own
column names. The refusals are CFTC's stable codes (``CFTCProviderError``, the engine's
attributable refusal type here) and stay code-only, because the source value is refused while the
record is still being read -- before any contract column is assembled. ``column`` names the
contract field the tampered source key maps onto through ``cot._FIELD_SOURCE``.
"""

from __future__ import annotations

from typing import Any, Final

_LEGACY_ROW: Final[dict[str, Any]] = {
    "cftc_contract_market_code": "001001",
    "commodity_name": "S&P 500 E-MINI",
    "market_and_exchange_names": "CME",
    "report_date_as_yyyy_mm_dd": "2026-01-06T00:00:00.000",
    "yyyy_report_week_ww": "01",
    "year": "2026",
    "week": "1",
    "contract_units_of_measure": "CONTRACTS",
    "currency": "USD",
    "exchange_id": "132",
    "noncomm_positions_long_all": 951000,
    "noncomm_positions_short_all": 402000,
    "noncomm_postions_spread_all": 61000,
    "comm_positions_long_all": 512000,
    "comm_positions_short_all": 989000,
    "tot_rept_positions_long_all": 1463000,
    "tot_rept_positions_short_all": 1391000,
    "nonrept_positions_long_all": 21000,
    "nonrept_positions_short_all": 93000,
    "open_interest_all": 1484000,
}

_DIRECTORY_ROWS: Final[list[dict[str, Any]]] = [
    {
        "cftc_contract_market_code": "001001",
        "commodity_name": "S&P 500 E-MINI",
        "market_and_exchange_names": "CME",
        "report_date_as_yyyy_mm_dd": "2026-01-06T00:00:00.000",
        "commodity_group_name": "STOCK INDEX",
        "commodity_subgroup_name": "MAJOR STOCK INDEXES",
        "contract_units": "1 CONTRACT = $50 MULTIPLIED BY INDEX",
    },
    {
        "cftc_contract_market_code": "002001",
        "commodity_name": "NASDAQ-100 E-MINI",
        "market_and_exchange_names": "CME",
        "report_date_as_yyyy_mm_dd": "2026-01-06T00:00:00.000",
        "commodity_group_name": "STOCK INDEX",
        "commodity_subgroup_name": "MINOR STOCK INDEXES",
        "contract_units": "1 CONTRACT = $20 MULTIPLIED BY INDEX",
    },
]


def _cftc_legacy_report_rows() -> list[dict[str, Any]]:
    """Two weekly legacy COT rows for one six-character contract market code."""
    previous = {**_LEGACY_ROW, "report_date_as_yyyy_mm_dd": "2025-12-30T00:00:00.000"}
    return [dict(_LEGACY_ROW), previous]


def _cftc_market_directory_rows() -> list[dict[str, Any]]:
    """The current-week PRE directory page the market-code search reads."""
    return [dict(row) for row in _DIRECTORY_ROWS]


_CONFIG_FILES: Final[list[str]] = ["opendata/data/providers/cftc/models/_client.py"]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.cftc.registration",
    "credential_env": {},
    "allowed_hosts": ["publicreporting.cftc.gov"],
    "models": {
        "COT": {
            "fixture_document": _cftc_legacy_report_rows,
            "query": {"code": "001001", "limit": 100},
            "tamper": {
                "op": "set",
                "path": [0, "report_date_as_yyyy_mm_dd"],
                "value": "2026-01-06T12:30:00.000",
                "column": "date",
            },
            "refusal_class": "CFTCProviderError",
            "refusal_code": "CFTC_BAD_DATE",
            "tamper_reason": (
                "publishing a report date carrying an intraday time breaks the documented weekly "
                "calendar-date contract, so the reader must refuse with CFTC_BAD_DATE instead of "
                "silently truncating the timestamp into a date the COT row cannot trace back to a "
                "report week"
            ),
            "source_files": [
                *_CONFIG_FILES,
                "opendata/data/providers/cftc/models/cot.py",
            ],
            "contract_files": [
                "opendata/data/models/cftc.py",
                "opendata/data/domains.yaml",
            ],
        },
        "COTSearch": {
            "fixture_document": _cftc_market_directory_rows,
            "query": {"report_type": "legacy", "futures_only": False},
            "tamper": {"op": "drop", "path": [1, "commodity_name"], "column": "name"},
            "refusal_class": "CFTCProviderError",
            "refusal_code": "CFTC_BAD_DIRECTORY_ROW",
            "tamper_reason": (
                "dropping 'commodity_name' from a directory record leaves the required 'name' "
                "column unnameable, so the search must refuse with CFTC_BAD_DIRECTORY_ROW rather "
                "than publish an unidentifiable contract market"
            ),
            "source_files": [
                *_CONFIG_FILES,
                "opendata/data/providers/cftc/models/cot_search.py",
                "opendata/data/providers/cftc/models/cot.py",
            ],
            "contract_files": [
                "opendata/data/models/cftc.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
