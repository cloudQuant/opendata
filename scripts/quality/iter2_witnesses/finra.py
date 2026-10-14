"""Offline witness configuration for the ``finra`` provider.

FinRA's Query API is a multi-leg flow (OAuth token exchange, then a metadata GET, then a
paged data POST whose row count travels in the ``Record-Total`` response *header*). The
repository's offline seam answers every leg with one JSON document and no headers, so the
fixture publishes a table keyed by request path in which each entry names that leg's body and
the headers it returns; ``_client._seam_response`` selects the entry for the URL being asked.
The credential exchange itself still runs for real, against fixture credentials that are never
sent anywhere.

Both refusals are FinRA's own stable codes (``FinraProviderError``, the engine's attributable
refusal type for this source) and are code-only: a corrupt source value is dropped before any
contract column is assembled, so there is no cell for the refusal to attribute. The ``column``
key names the contract field the tampered source key maps onto.
"""

from __future__ import annotations

from typing import Any, Final

_OAUTH_PATH: Final = "/fip/rest/ews/oauth2/access_token"
_PARTITIONS_PATH: Final = "/partitions/group/otcmarket/name/weeklysummary"
_WEEKLY_SUMMARY_PATH: Final = "/data/group/otcmarket/name/weeklysummary"
_SHORT_INTEREST_PATH: Final = "/data/group/otcmarket/name/consolidatedshortinterest"

_TOKEN_ENTRY: Final[dict[str, Any]] = {
    "body": {
        "access_token": "fixture-token-never-sent",  # nosec B105  # offline fixture body, never sent
        "token_type": "Bearer",  # nosec B105  # OAuth body shape fixture, not a credential
        "expires_in": 1800,
    }
}

_WEEKLY_SUMMARY_ROWS: Final[list[dict[str, Any]]] = [
    {
        "issueSymbolIdentifier": "FIXEX",
        "totalWeeklyShareQuantity": 125000.0,
        "totalWeeklyTradeCount": 340.0,
        "lastUpdateDate": "2026-01-09",
    },
    {
        "issueSymbolIdentifier": "FIXEY",
        "totalWeeklyShareQuantity": 9800.0,
        "totalWeeklyTradeCount": 52.0,
        "lastUpdateDate": "2026-01-09",
    },
]

_SHORT_INTEREST_ROWS: Final[list[dict[str, Any]]] = [
    {
        "settlementDate": "2026-01-08",
        "symbolCode": "FIXEX",
        "issueName": "Fixture Ex Corp",
        "marketClassCode": "C",
        "currentShortPositionQuantity": 150000.0,
        "previousShortPositionQuantity": 148000.0,
        "averageDailyVolumeQuantity": None,
        "daysToCoverQuantity": 3.5,
        "changePreviousNumber": 2000.0,
        "changePercent": 1.35,
    },
    {
        "settlementDate": "2026-01-22",
        "symbolCode": "FIXEX",
        "issueName": "Fixture Ex Corp",
        "marketClassCode": "C",
        "currentShortPositionQuantity": 142500.0,
        "previousShortPositionQuantity": 150000.0,
        "averageDailyVolumeQuantity": 45000.0,
        "daysToCoverQuantity": 3.17,
        "changePreviousNumber": -7500.0,
        "changePercent": -5.0,
    },
]


def _finra_weekly_summary_table() -> dict[str, Any]:
    """Token, partition metadata and the weekly summary page for one T1/ATS request."""
    return {
        _OAUTH_PATH: dict(_TOKEN_ENTRY),
        _PARTITIONS_PATH: {
            "body": {
                "partitionFields": ["weekStartDate", "tierIdentifier"],
                "availablePartitions": [
                    {"partitions": ["2025-12-29", "T1"]},
                    {"partitions": ["2026-01-05", "T1"]},
                    {"partitions": ["2026-01-05", "OTCE"]},
                ],
            }
        },
        _WEEKLY_SUMMARY_PATH: {
            "body": [dict(row) for row in _WEEKLY_SUMMARY_ROWS],
            "headers": {"Record-Total": str(len(_WEEKLY_SUMMARY_ROWS))},
        },
    }


def _finra_short_interest_table() -> dict[str, Any]:
    """Token plus one symbol's consolidated short-interest settlement history."""
    return {
        _OAUTH_PATH: dict(_TOKEN_ENTRY),
        _SHORT_INTEREST_PATH: {
            "body": [dict(row) for row in _SHORT_INTEREST_ROWS],
            "headers": {"Record-Total": str(len(_SHORT_INTEREST_ROWS))},
        },
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.finra.registration",
    "credential_env": {
        "FINRA_API_CLIENT_ID": "fixture-client-id-never-sent",
        "FINRA_API_CLIENT_SECRET": "fixture-client-secret-never-sent",  # nosec B105  # offline placeholder, never sent
    },
    "allowed_hosts": ["ews.fip.finra.org", "api.finra.org"],
    "models": {
        "OTCAggregate": {
            "fixture_document": _finra_weekly_summary_table,
            "query": {"tier": "T1", "is_ats": True},
            "tamper": {
                "op": "set",
                "path": [_WEEKLY_SUMMARY_PATH, "body", 0, "totalWeeklyShareQuantity"],
                "value": "125k shares",
                "column": "share_quantity",
            },
            "refusal_class": "FinraProviderError",
            "refusal_code": "FINRA_OTC_AGGREGATE_BAD_ROW",
            "tamper_reason": (
                "publishing text where the weekly summary documents a share quantity makes the "
                "strict OTCAggregate row refuse the record, so the adapter must surface "
                "FINRA_OTC_AGGREGATE_BAD_ROW rather than store an unparsable quantity"
            ),
            "source_files": [
                "opendata/data/providers/finra/models/_client.py",
                "opendata/data/providers/finra/models/otc_aggregate.py",
            ],
            "contract_files": [
                "opendata/data/models/finra.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquityShortInterest": {
            "fixture_document": _finra_short_interest_table,
            "query": {"symbol": "FIXEX"},
            "tamper": {
                "op": "set",
                "path": [_SHORT_INTEREST_PATH, "body", 1, "currentShortPositionQuantity"],
                "value": "142.5K",
                "column": "current_short_position",
            },
            "refusal_class": "FinraProviderError",
            "refusal_code": "FINRA_SHORT_INTEREST_BAD_ROW",
            "tamper_reason": (
                "publishing the text cell '142.5K' where the settlement record documents a "
                "numeric short-position quantity makes the strict EquityShortInterest row refuse "
                "the record, so the adapter surfaces FINRA_SHORT_INTEREST_BAD_ROW for the page "
                "rather than dropping the settlement or inventing a quantity"
            ),
            "source_files": [
                "opendata/data/providers/finra/models/_client.py",
                "opendata/data/providers/finra/models/equity_short_interest.py",
            ],
            "contract_files": [
                "opendata/data/models/finra.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
