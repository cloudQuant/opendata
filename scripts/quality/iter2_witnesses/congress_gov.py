"""Offline witness configuration for the ``congress_gov`` provider.

Every Congress.gov v3 leg answers with a JSON document, and all eight fetchers pass
``self.http_transport`` into the source client, so each witness installs the repository's
``FixedResponseTransport`` and the client's seam branch re-encodes that document to the bytes a
JSON response would have carried. The parsers, the bounds and the mappers under test are the ones
that ship.

The refusals here are Congress.gov's own stable codes (``CongressGovProviderError``, the engine's
attributable refusal type for this source) and they are deliberately code-only: a bill row whose
``url`` is not an API document reference, an amendment published without a number, or a
pagination count that is negative is refused before the mapper can address a contract column, so
no cell exists for the refusal to attribute.

What these six witnesses cover, and what they do not: the repository's offline transport seam
answers every call of one fetch with the same body, so each detail witness is run with
``include_components=()`` and exercises the base detail leg only. The component joins (cosponsors,
subjects, summaries, committees, actions, related bills, titles) and the two text-download legs,
which make a second ``get_document`` call per version, are not witnessed by this shard and stay
claimed as uncovered.
"""

from __future__ import annotations

from typing import Any, Final

_API_BASE: Final[str] = "https://api.congress.gov/v3"

#: Two 119th-Congress House resolutions as ``/bill/119/hr`` publishes the first page.
_BILLS_PAGE: Final[dict[str, Any]] = {
    "bills": {
        "bill": [
            {
                "congress": 119,
                "billType": "HR",
                "billNumber": 1,
                "originChamber": "House",
                "originChamberCode": "H",
                "title": (
                    "To amend title 5, United States Code, to require the President to establish "
                    "a working group to review the Federal response to disasters."
                ),
                "updateDate": "2025-01-03",
                "updateDateIncludingText": "2025-01-03T10:02:51Z",
                "latestAction": {
                    "actionDate": "2025-01-03",
                    "text": "Referred to the Committee on Transportation and Infrastructure.",
                },
                "url": f"{_API_BASE}/bill/119/hr/1?format=json",
            },
            {
                "congress": 119,
                "billType": "HR",
                "billNumber": 8,
                "originChamber": "House",
                "originChamberCode": "H",
                "title": "To prohibit federal funding of abortion.",
                "updateDate": "2025-02-04",
                "updateDateIncludingText": "2025-02-04T16:26:14Z",
                "latestAction": {
                    "actionDate": "2025-02-04",
                    "text": "Referred to the Committees on the Judiciary, and on Ways and Means.",
                },
                "url": f"{_API_BASE}/bill/119/hr/8?format=json",
            },
        ]
    },
    "pagination": {"count": 2},
}

#: Two 118th-Congress House amendments as ``/amendment/118/hamdt`` publishes the first page.
_AMENDMENTS_PAGE: Final[dict[str, Any]] = {
    "amendments": {
        "amendment": [
            {
                "congress": 118,
                "amendmentType": "HAMDT",
                "amendmentNumber": 1,
                "description": (
                    "Amends H.R. 1. Makes modifications to the rules governing debate."
                ),
                "purpose": "To modify the rules governing debate.",
                "amendedBill": {
                    "congress": 118,
                    "type": "HR",
                    "originChamber": "House",
                    "number": 1,
                    "title": "Making amendments to the Rules of the House of Representatives.",
                    "url": f"{_API_BASE}/bill/118/hr/1?format=json",
                },
                "latestAction": {
                    "actionDate": "2023-01-10",
                    "text": "Amendment agreed to by House by a recorded vote.",
                },
                "submittedDate": "2023-01-09",
                "updateDate": "2023-01-11",
                "url": f"{_API_BASE}/amendment/118/hamdt/1?format=json",
            },
            {
                "congress": 118,
                "amendmentType": "HAMDT",
                "amendmentNumber": 2,
                "description": "Amends H.R. 1. Strikes sections 3 and 4.",
                "purpose": "To strike sections 3 and 4.",
                "amendedBill": {
                    "congress": 118,
                    "type": "HR",
                    "originChamber": "House",
                    "number": 1,
                    "title": "Making amendments to the Rules of the House of Representatives.",
                    "url": f"{_API_BASE}/bill/118/hr/1?format=json",
                },
                "latestAction": {
                    "actionDate": "2023-01-10",
                    "text": "Amendment not agreed to by House by a recorded vote.",
                },
                "submittedDate": "2023-01-09",
                "updateDate": "2023-01-11",
                "url": f"{_API_BASE}/amendment/118/hamdt/2?format=json",
            },
        ]
    },
    "pagination": {"count": 2},
}

#: Two Senate Banking Committee reports as ``/committee/senate/ssbk00/reports`` publishes them.
_COMMITTEE_REPORTS_PAGE: Final[dict[str, Any]] = {
    "reports": {
        "item": [
            {
                "congress": 118,
                "reportNumber": "118-150",
                "title": "MAKING APPROPRIATIONS FOR THE DEPARTMENT OF HOMELAND SECURITY",
                "committee": {
                    "name": "Banking, Housing, and Urban Affairs",
                    "code": "SSBK00",
                    "chamber": "Senate",
                },
                "actionDate": "2024-05-15",
                "documentType": "Executive Report",
                "url": "https://www.congress.gov/committee-report/118th-congress/senate-report/150",
            },
            {
                "congress": 118,
                "reportNumber": "118-201",
                "title": "NOMINATIONS HEARING REPORT",
                "committee": {
                    "name": "Banking, Housing, and Urban Affairs",
                    "code": "SSBK00",
                    "chamber": "Senate",
                },
                "actionDate": "2024-09-19",
                "documentType": "Printed Hearings",
                "url": "https://www.congress.gov/committee-report/118th-congress/senate-report/201",
            },
        ]
    },
    "pagination": {"count": 2},
}

#: ``/bill/119/hr/1`` answered with no component join, which is what one transport body can carry.
_BILL_DETAIL: Final[dict[str, Any]] = {
    "bill": {
        "congress": 119,
        "billType": "HR",
        "billNumber": 1,
        "originChamber": "House",
        "originChamberCode": "H",
        "title": (
            "To amend title 5, United States Code, to require the President to establish "
            "a working group to review the Federal response to disasters."
        ),
        "introducedDate": "2025-01-03",
        "updateDate": "2025-01-03",
        "updateDateIncludingText": "2025-01-03T10:02:51Z",
        "latestAction": {
            "actionDate": "2025-01-03",
            "text": "Referred to the Committee on Transportation and Infrastructure.",
        },
        "sponsors": {
            "sponsor": {
                "_bioguideId": "M000133",
                "fullName": "Mr. Mast, John",
                "firstName": "John",
                "lastName": "Mast",
                "party": "Republican",
                "state": "FL",
                "type": "Primary Sponsor",
            }
        },
        "url": f"{_API_BASE}/bill/119/hr/1?format=json",
    }
}

#: ``/amendment/118/hamdt/1`` answered with no component join.
_AMENDMENT_DETAIL: Final[dict[str, Any]] = {
    "amendment": {
        "congress": 118,
        "amendmentType": "HAMDT",
        "amendmentNumber": 1,
        "description": "Amends H.R. 1. Makes modifications to the rules governing debate.",
        "purpose": "To modify the rules governing debate.",
        "submittedDate": "2023-01-09",
        "updateDate": "2023-01-11",
        "latestAction": {
            "actionDate": "2023-01-10",
            "text": "Amendment agreed to by House by a recorded vote.",
        },
        "amendedBill": {
            "congress": 118,
            "billType": "HR",
            "billNumber": 1,
            "title": "Making amendments to the Rules of the House of Representatives.",
            "url": f"{_API_BASE}/bill/118/hr/1?format=json",
        },
        "url": f"{_API_BASE}/amendment/118/hamdt/1?format=json",
    }
}

#: ``/committee/senate/ssbk00`` as the single endpoint answer the committee detail parses.
_COMMITTEE_DETAIL: Final[dict[str, Any]] = {
    "committee": {
        "systemCode": "SSBK00",
        "name": "Banking, Housing, and Urban Affairs",
        "chamber": "Senate",
        "committeeTypeCode": "S",
        "isCurrent": True,
        "websiteUrl": "https://www.banking.senate.gov",
        "subcommittees": {
            "item": [
                {
                    "systemCode": "SHHS",
                    "name": "Housing, Insurance, and Community Development",
                    "chamber": "Senate",
                }
            ]
        },
        "history": {
            "item": [
                {
                    "officialName": "Committee on Banking, Housing, and Urban Affairs",
                    "libraryOfCongressName": (
                        "United States. Congress. Senate. Committee on Banking, Housing, "
                        "and Urban Affairs."
                    ),
                    "startDate": "1970-01-01",
                }
            ]
        },
        "bills": {"count": 4312},
        "reports": {"count": 1180},
        "nominations": {"count": 612},
        "communications": {"count": 94},
    }
}


def _bills_page() -> dict[str, Any]:
    """The ``/bill/119/hr`` first page the bills listing parses."""
    return {
        "bills": {"bill": [dict(row) for row in _BILLS_PAGE["bills"]["bill"]]},
        "pagination": dict(_BILLS_PAGE["pagination"]),
    }


def _amendments_page() -> dict[str, Any]:
    """The ``/amendment/118/hamdt`` first page the amendments listing parses."""
    return {
        "amendments": {
            "amendment": [dict(row) for row in _AMENDMENTS_PAGE["amendments"]["amendment"]]
        },
        "pagination": dict(_AMENDMENTS_PAGE["pagination"]),
    }


def _committee_reports_page() -> dict[str, Any]:
    """The ``/committee/senate/ssbk00/reports`` page the committee document listing parses."""
    return {
        "reports": {"item": [dict(row) for row in _COMMITTEE_REPORTS_PAGE["reports"]["item"]]},
        "pagination": dict(_COMMITTEE_REPORTS_PAGE["pagination"]),
    }


def _bill_detail() -> dict[str, Any]:
    """``/bill/119/hr/1`` with no component joins requested, so one call answers the fetch."""
    return {"bill": dict(_BILL_DETAIL["bill"])}


def _amendment_detail() -> dict[str, Any]:
    """``/amendment/118/hamdt/1`` with no component joins requested."""
    return {"amendment": dict(_AMENDMENT_DETAIL["amendment"])}


def _committee_detail() -> dict[str, Any]:
    """``/committee/senate/ssbk00`` as the single committee endpoint answer."""
    return {"committee": dict(_COMMITTEE_DETAIL["committee"])}


_CONFIG_FILES: Final[list[str]] = [
    "opendata/data/providers/congress_gov/models/_client.py",
]

_SOURCE_FILES: Final[list[str]] = ["opendata/data/providers/congress_gov/models/iter2.py"]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/congress_cboe_iter2.py",
    "opendata/data/domains.yaml",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.congress_gov.registration",
    "credential_env": {"CONGRESS_GOV_API_KEY": "fixture-key-never-sent"},
    "allowed_hosts": ["api.congress.gov", "www.congress.gov", "congress.gov"],
    "models": {
        "CongressBills": {
            "fixture_document": _bills_page,
            "query": {"congress": 119, "bill_type": "hr", "limit": 2, "offset": 0},
            "tamper": {
                "op": "set",
                "path": ["bills", "bill", 0, "url"],
                "value": "https://www.congress.gov/bill/119th-congress/house-bill/1",
            },
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_BILL_ROW",
            "tamper_reason": (
                "publishing the human-facing www.congress.gov page in the row's ``url`` where the "
                "API documents an ``api.congress.gov/v3`` resource reference must make the mapper "
                "refuse the row with CONGRESS_GOV_BAD_BILL_ROW, because ``bill_url`` is a required "
                "cell of ``CongressBill`` and ``_safe_api_url`` returns None for any host or path "
                "outside the API's own document namespace"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
        "CongressAmendments": {
            "fixture_document": _amendments_page,
            "query": {"congress": 118, "amendment_type": "hamdt", "limit": 2, "offset": 0},
            "tamper": {
                "op": "drop",
                "path": ["amendments", "amendment", 0, "amendmentNumber"],
            },
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_AMENDMENT_ROW",
            "tamper_reason": (
                "publishing an amendment record with no number under either of the keys the mapper "
                "reads (``amendmentNumber``, ``number``) must make it refuse with "
                "CONGRESS_GOV_BAD_AMENDMENT_ROW rather than emit a ``CongressAmendment`` whose "
                "required ``number`` cell holds None"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
        "CongressCommitteeDocuments": {
            "fixture_document": _committee_reports_page,
            "query": {
                "chamber": "senate",
                "committee_code": "ssbk00",
                "document_category": "reports",
                "limit": 2,
                "offset": 0,
            },
            "tamper": {
                "op": "set",
                "path": ["pagination", "count"],
                "value": -1,
            },
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_PAGINATION",
            "tamper_reason": (
                "publishing a ``pagination`` object whose ``count`` is negative is an internally "
                "impossible page: the bounded-collection guard must refuse it with "
                "CONGRESS_GOV_BAD_PAGINATION before any page of the collection is sliced, rather "
                "than quietly return rows whose stated total contradicts the page"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
        "CongressBillInfo": {
            "fixture_document": _bill_detail,
            "query": {
                "congress": 119,
                "bill_type": "hr",
                "bill_number": 1,
                "include_components": (),
            },
            "tamper": {"op": "drop", "path": ["bill", "title"]},
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_BILL_INFO",
            "tamper_reason": (
                "publishing a bill detail with no ``title`` must make the detail mapper refuse "
                "with CONGRESS_GOV_BAD_BILL_INFO: ``title`` is a required cell of the "
                "``CongressBillInfoRawData`` document that rides under ``raw_data``, so an "
                "untitled detail cannot be turned into a contract row by filling the cell from "
                "the query"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
        "CongressAmendmentInfo": {
            "fixture_document": _amendment_detail,
            "query": {
                "congress": 118,
                "amendment_type": "hamdt",
                "amendment_number": 1,
                "include_components": (),
            },
            "tamper": {"op": "drop", "path": ["amendment", "amendmentNumber"]},
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_AMENDMENT_INFO",
            "tamper_reason": (
                "publishing an amendment detail whose number is absent under both keys the mapper "
                "reads must refuse with CONGRESS_GOV_BAD_AMENDMENT_INFO rather than let "
                "``amendment_number``, a required cell of the retained "
                "``CongressAmendmentInfoRawData``, fall back to the number the caller asked for"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
        "CongressCommitteeInfo": {
            "fixture_document": _committee_detail,
            "query": {"chamber": "senate", "committee_code": "ssbk00"},
            "tamper": {"op": "drop", "path": ["committee", "name"]},
            "refusal_class": "CongressGovProviderError",
            "refusal_code": "CONGRESS_GOV_BAD_COMMITTEE_INFO",
            "tamper_reason": (
                "publishing a committee endpoint answer with no ``name`` must refuse with "
                "CONGRESS_GOV_BAD_COMMITTEE_INFO, because ``name`` is a required cell of "
                "``CongressCommitteeInfoRawData`` and the mapper has no other published title to "
                "derive it from"
            ),
            "source_files": _SOURCE_FILES,
            "contract_files": _CONTRACT_FILES + _CONFIG_FILES,
        },
    },
}
