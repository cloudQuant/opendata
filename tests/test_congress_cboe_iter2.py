"""Hand-authored Congress.gov Iteration 2 fixtures and service-path checks."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from requests.structures import CaseInsensitiveDict

from opendata.data.models.congress_cboe_iter2 import (
    CongressAmendment,
    CongressAmendmentInfo,
    CongressAmendmentText,
    CongressBill,
    CongressBillInfo,
    CongressBillText,
    CongressCommitteeDocument,
    CongressCommitteeInfo,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.congress_gov.models import _client
from opendata.data.providers.congress_gov.models.iter2 import (
    CongressAmendmentInfoFetcher,
    CongressAmendmentsFetcher,
    CongressAmendmentTextFetcher,
    CongressBillInfoFetcher,
    CongressBillsFetcher,
    CongressBillTextFetcher,
    CongressCommitteeDocumentsFetcher,
    CongressCommitteeInfoFetcher,
)
from opendata.data.providers.congress_gov.provider import PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class _Response:
    def __init__(self, payload: object, *, content_type: str | None = None) -> None:
        self.status_code = 200
        if isinstance(payload, bytes):
            self.content = payload
            resolved_content_type = content_type or "application/octet-stream"
        else:
            self.content = json.dumps(payload).encode("utf-8")
            resolved_content_type = content_type or "application/json"
        self.headers = {"Content-Type": resolved_content_type}


class _Transport:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str,
        **_: object,
    ) -> _Response:
        self.requests.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "timeout": timeout,
                "source": source,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra HTTP request")
        return self.responses.pop(0)


class _Principal:
    def allows_domain(self, domain: str) -> bool:
        return domain.startswith("congress_")


_BILL = {
    "congress": 118,
    "type": "hr",
    "number": 1,
    "title": "A Test Bill",
    "introducedDate": "2023-01-03",
    "updateDate": "2024-02-02",
    "updateDateIncludingText": "2024-02-02T14:00:00Z",
    "originChamber": "House",
    "originChamberCode": "H",
    "latestAction": {"actionDate": "2024-02-01", "text": "Became Public Law", "type": "Floor"},
    "url": "https://api.congress.gov/v3/bill/118/hr/1",
}
_AMENDMENT = {
    "congress": 118,
    "type": "samdt",
    "number": 42,
    "amendedBill": {"congress": 118, "type": "hr", "number": 1},
    "amendedBillTitle": "A Test Bill",
    "description": "A test amendment",
    "purpose": "For test purposes",
    "submittedDate": "2024-01-31",
    "updateDate": "2024-02-02T14:00:00Z",
    "latestAction": {
        "actionDate": "2024-02-01",
        "actionTime": "14:00:00",
        "text": "Agreed to",
        "type": "Floor",
    },
    "sponsors": {"item": [{"member": {"fullName": "Jane Doe"}}]},
    "url": "https://api.congress.gov/v3/amendment/118/samdt/42",
}


_CASES: tuple[dict[str, Any], ...] = (
    {
        "model": "CongressBills",
        "fetcher": CongressBillsFetcher,
        "contract": CongressBill,
        "query": {
            "congress": 118,
            "bill_type": "HR",
            "limit": 20,
            "offset": 0,
            "start_date": "2024-02-02",
            "end_date": "2024-02-02",
        },
        "responses": ({"bills": [_BILL], "pagination": {"count": 1, "next": None}},),
        "expected": {
            "update_date": "2024-02-02",
            "latest_action_date": "2024-02-01",
            "bill_url": "https://api.congress.gov/v3/bill/118/hr/1",
            "congress": 118,
            "bill_number": 1,
            "origin_chamber": "House",
            "origin_chamber_code": "H",
            "bill_type": "HR",
            "title": "A Test Bill",
            "latest_action": "Became Public Law",
            "update_date_including_text": "2024-02-02T14:00:00Z",
        },
        "bad_query": {"congress": 118, "bill_type": "HR", "limit": 20, "offset": 0},
        "bad_response": {"bills": "one bill", "pagination": {"count": 1, "next": None}},
    },
    {
        "model": "CongressBillInfo",
        "fetcher": CongressBillInfoFetcher,
        "contract": CongressBillInfo,
        "query": {"congress": 118, "bill_type": "hr", "bill_number": 1, "include_components": []},
        "responses": ({"bill": _BILL},),
        "expected": {
            "markdown_content": (
                "# A Test Bill\n\n- Congress: 118\n- Bill: HR 1\n"
                "- Update date: 2024-02-02\n- Source: https://api.congress.gov/v3/bill/118/hr/1\n\n"
                "## Latest action\n- Date: 2024-02-01\n- Text: Became Public Law"
            ),
            "source_url": "https://api.congress.gov/v3/bill/118/hr/1",
            "raw_data": {
                "congress": 118,
                "bill_type": "hr",
                "bill_number": 1,
                "title": "A Test Bill",
                "update_date": "2024-02-02",
                "update_date_including_text": "2024-02-02T14:00:00Z",
                "introduced_date": "2023-01-03",
                "latest_action": {
                    "action_date": "2024-02-01",
                    "text": "Became Public Law",
                    "action_type": "Floor",
                },
                "sponsors": [],
                "cosponsors": [],
                "subjects": [],
                "summaries": [],
                "committees": [],
                "actions": [],
                "related_bills": [],
                "titles": [],
                "source_url": "https://api.congress.gov/v3/bill/118/hr/1",
                "included_components": [],
            },
        },
        "bad_query": {
            "congress": 118,
            "bill_type": "hr",
            "bill_number": 1,
            "include_components": [],
        },
        "bad_response": {"bill": "not an object"},
    },
    {
        "model": "CongressBillText",
        "fetcher": CongressBillTextFetcher,
        "contract": CongressBillText,
        "query": {"congress": 118, "bill_type": "hr", "bill_number": 1, "document_format": "xml"},
        "responses": (
            {
                "textVersions": [
                    {
                        "type": "Enrolled",
                        "date": "2024-02-01",
                        "formats": {
                            "item": [
                                {
                                    "type": "XML",
                                    "url": "https://www.congress.gov/118/bills/hr1/BILLS-118hr1enr.xml",
                                }
                            ]
                        },
                    }
                ],
                "pagination": {"count": 1},
            },
            b'<bill congress="118"/>',
        ),
        "expected": {
            "congress": 118,
            "version_code": "Enrolled",
            "version_date": "2024-02-01",
            "document_format": "XML",
            "content_base64": "PGJpbGwgY29uZ3Jlc3M9IjExOCIvPg==",
            "media_type": "application/octet-stream",
            "source_url": "https://www.congress.gov/118/bills/hr1/BILLS-118hr1enr.xml",
            "bill_type": "hr",
            "bill_number": 1,
        },
        "bad_query": {
            "congress": 118,
            "bill_type": "hr",
            "bill_number": 1,
            "document_format": "XML",
        },
        "bad_response": {
            "textVersions": [
                {
                    "type": "Enrolled",
                    "date": "2024-02-01",
                    "formats": {"item": [{"type": "XML", "url": "https://evil.example/bill.xml"}]},
                }
            ]
        },
    },
    {
        "model": "CongressAmendments",
        "fetcher": CongressAmendmentsFetcher,
        "contract": CongressAmendment,
        "query": {
            "congress": 118,
            "amendment_type": "SAMDT",
            "limit": 20,
            "offset": 5,
            "start_date": "2024-02-01",
            "end_date": "2024-02-02",
        },
        "responses": ({"amendments": [_AMENDMENT], "pagination": {"count": 6, "next": None}},),
        "expected": {
            "congress": 118,
            "amendment_type": "SAMDT",
            "number": 42,
            "amended_bill": "HR 1",
            "amended_bill_title": "A Test Bill",
            "description": "A test amendment",
            "purpose": "For test purposes",
            "latest_action_date": "2024-02-01",
            "latest_action": "Agreed to",
            "latest_action_time": "14:00:00",
            "sponsor": "Jane Doe",
            "submitted_date": "2024-01-31",
            "update_date": "2024-02-02",
            "amendment_url": "https://api.congress.gov/v3/amendment/118/samdt/42",
        },
        "bad_query": {"congress": 118, "amendment_type": "SAMDT", "limit": 20, "offset": 0},
        "bad_response": {"amendments": ["not an amendment"], "pagination": {"count": 1}},
    },
    {
        "model": "CongressAmendmentInfo",
        "fetcher": CongressAmendmentInfoFetcher,
        "contract": CongressAmendmentInfo,
        "query": {
            "congress": 118,
            "amendment_type": "samdt",
            "amendment_number": 42,
            "include_components": [],
        },
        "responses": ({"amendment": _AMENDMENT},),
        "expected": {
            "markdown_content": (
                "# SAMDT 42\n\n- Congress: 118\n- Description: A test amendment\n"
                "- Purpose: For test purposes\n- Update date: 2024-02-02\n"
                "- Source: https://api.congress.gov/v3/amendment/118/samdt/42\n\n"
                "## Latest action\n- Date: 2024-02-01\n- Text: Agreed to\n\n## Sponsor\nJane Doe"
            ),
            "source_url": "https://api.congress.gov/v3/amendment/118/samdt/42",
            "raw_data": {
                "congress": 118,
                "amendment_type": "samdt",
                "amendment_number": 42,
                "description": "A test amendment",
                "purpose": "For test purposes",
                "update_date": "2024-02-02",
                "latest_action": {
                    "action_date": "2024-02-01",
                    "text": "Agreed to",
                    "action_type": "Floor",
                },
                "sponsors": [
                    {
                        "bioguide_id": None,
                        "name": "Jane Doe",
                        "first_name": None,
                        "last_name": None,
                        "party": None,
                        "state": None,
                        "district": None,
                        "sponsorship_date": None,
                        "is_original_cosponsor": None,
                    }
                ],
                "cosponsors": [],
                "actions": [],
                "text_versions": [],
                "source_url": "https://api.congress.gov/v3/amendment/118/samdt/42",
                "included_components": [],
            },
        },
        "bad_query": {
            "congress": 118,
            "amendment_type": "samdt",
            "amendment_number": 42,
            "include_components": [],
        },
        "bad_response": {"amendment": []},
    },
    {
        "model": "CongressAmendmentText",
        "fetcher": CongressAmendmentTextFetcher,
        "contract": CongressAmendmentText,
        "query": {
            "congress": 118,
            "amendment_type": "samdt",
            "amendment_number": 42,
            "document_format": "xml",
        },
        "responses": (
            {
                "textVersions": [
                    {
                        "type": "Submitted",
                        "date": "2024-02-01",
                        "formats": {
                            "item": [
                                {
                                    "type": "XML",
                                    "url": "https://www.congress.gov/118/amendments/samdt42/BILLS-118samdt42.xml",
                                }
                            ]
                        },
                    }
                ]
            },
            b"<amendment/>",
        ),
        "expected": {
            "congress": 118,
            "version_code": "Submitted",
            "version_date": "2024-02-01",
            "document_format": "XML",
            "content_base64": "PGFtZW5kbWVudC8+",
            "media_type": "application/octet-stream",
            "source_url": "https://www.congress.gov/118/amendments/samdt42/BILLS-118samdt42.xml",
            "amendment_type": "samdt",
            "amendment_number": 42,
        },
        "bad_query": {
            "congress": 118,
            "amendment_type": "samdt",
            "amendment_number": 42,
            "document_format": "xml",
        },
        "bad_response": {
            "textVersions": [
                {
                    "type": "Submitted",
                    "date": "2024-02-01",
                    "formats": {
                        "item": [{"type": "XML", "url": "https://evil.example/amendment.xml"}]
                    },
                }
            ]
        },
    },
    {
        "model": "CongressCommitteeInfo",
        "fetcher": CongressCommitteeInfoFetcher,
        "contract": CongressCommitteeInfo,
        "query": {"chamber": "house", "committee_code": "hspw00"},
        "responses": (
            {
                "committee": {
                    "systemCode": "hspw00",
                    "name": "Transportation and Infrastructure Committee",
                    "chamber": "House",
                    "committeeTypeCode": "Standing",
                    "committeeWebsiteUrl": "https://transportation.house.gov/",
                    "isCurrent": True,
                    "bills": {"count": 22},
                    "reports": {"count": 3},
                    "history": {
                        "item": [
                            {
                                "officialName": "Transportation and Infrastructure Committee",
                                "startDate": "1995-01-04",
                            }
                        ]
                    },
                }
            },
        ),
        "expected": {
            "markdown_content": (
                "# Transportation and Infrastructure Committee\n\n- Chamber: House\n"
                "- System code: hspw00\n- Committee type: Standing\n"
                "- Website: https://transportation.house.gov/\n"
                "- Membership roster included: false\n"
                "- Source: https://api.congress.gov/v3/committee/house/hspw00\n\n"
                "## Name history\nPeriods: 1\n\n## Activity counts\n- Bills: 22\n- Reports: 3"
            ),
            "source_url": "https://api.congress.gov/v3/committee/house/hspw00",
            "raw_data": {
                "system_code": "hspw00",
                "name": "Transportation and Infrastructure Committee",
                "chamber": "House",
                "committee_type": "Standing",
                "website_url": "https://transportation.house.gov/",
                "is_current": True,
                "parent": None,
                "subcommittees": [],
                "history": [
                    {
                        "official_name": "Transportation and Infrastructure Committee",
                        "library_of_congress_name": None,
                        "start_date": "1995-01-04",
                        "end_date": None,
                        "committee_type": None,
                        "establishing_authority": None,
                    }
                ],
                "bill_count": 22,
                "report_count": 3,
                "nomination_count": None,
                "communication_count": None,
                "source_url": "https://api.congress.gov/v3/committee/house/hspw00",
                "membership_roster_included": False,
            },
        },
        "bad_query": {"chamber": "house", "committee_code": "hspw00"},
        "bad_response": {"committee": "not an object"},
    },
    {
        "model": "CongressCommitteeDocuments",
        "fetcher": CongressCommitteeDocumentsFetcher,
        "contract": CongressCommitteeDocument,
        "query": {
            "chamber": "house",
            "committee_code": "hspw00",
            "document_category": "bills",
            "limit": 20,
        },
        "responses": (
            {
                "bills": [
                    {
                        "congress": 118,
                        "billType": "HR",
                        "billNumber": 100,
                        "title": "A Committee Bill",
                        "relationshipType": "Referred to",
                        "actionDate": "2024-01-05",
                        "url": "https://api.congress.gov/v3/bill/118/hr/100",
                    }
                ],
                "pagination": {"count": 1},
            },
        ),
        "expected": {
            "document_category": "bills",
            "committee_code": "hspw00",
            "chamber": "house",
            "congress": 118,
            "document_id": "100",
            "title": "A Committee Bill",
            "description": None,
            "document_type": "HR",
            "event_date": "2024-01-05",
            "document_url": None,
            "api_url": "https://api.congress.gov/v3/bill/118/hr/100",
        },
        "bad_query": {
            "chamber": "house",
            "committee_code": "hspw00",
            "document_category": "bills",
            "limit": 20,
        },
        "bad_response": {"bills": "not a list", "pagination": {"count": 1}},
    },
)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, responses: tuple[object, ...]
) -> _Transport:
    transport = _Transport(*(_Response(value) for value in responses))
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "fixture-api-key")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)
    return transport


def test_document_content_type_accepts_requests_case_insensitive_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the media type from the same Mapping implementation used by requests."""
    response = _Response(b"<bill/>")
    response.headers = CaseInsensitiveDict({"cOnTeNt-TyPe": "Application/XML; charset=utf-8"})
    transport = _Transport(response)
    monkeypatch.setenv("CONGRESS_GOV_API_KEY", "fixture-api-key")
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: transport)

    body, media_type = _client.get_document(
        "https://www.congress.gov/118/bills/hr1/BILLS-118hr1enr.xml",
        context=_fixture_context("CongressBillText"),
    )

    assert body == b"<bill/>"
    assert media_type == "application/xml"
    assert transport.requests[0]["source"] == "congress_gov"


def _fixture_context(model: str) -> FetchContext:
    """Authorize only fake-transport API and official-document hosts for one model."""
    grant = RequestGrant(
        source="congress_gov",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=10,
        source_attempts=10,
        allowed_hosts=frozenset({"api.congress.gov", "www.congress.gov", "congress.gov"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=10,
        source_attempts=10,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(timeout=2.0, request_budget=budget)


def _descriptor(fetcher: object, model: str) -> SimpleNamespace:
    capability = fetcher.capability  # type: ignore[attr-defined]
    return SimpleNamespace(
        source=capability.source,
        model=model,
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: str(case["model"]))
def test_provider_model_service_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    """Each model's source fixture passes through provider_model_query and JSON serialization."""
    fetcher_type = case["fetcher"]
    fetcher = fetcher_type()
    model = case["model"]
    contract = case["contract"]
    expected = contract.model_validate(case["expected"]).model_dump(mode="json")
    transport = _install_transport(monkeypatch, case["responses"])
    descriptor = _descriptor(fetcher, model)
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="congress_gov",
            model=model,
            query=case["query"],
            principal=_Principal(),
            ctx=_fixture_context(model),
        )
    )

    assert response["source"] == "congress_gov"
    assert response["model"] == model
    assert response["domain"] == fetcher.capability.domain
    assert response["results"] == [expected]
    assert transport.requests[0]["source"] == "congress_gov"
    if "api.congress.gov" in transport.requests[0]["url"]:
        assert transport.requests[0]["params"]["api_key"] == "fixture-api-key"
        assert transport.requests[0]["params"]["format"] == "json"
    if model == "CongressBills":
        assert transport.requests[0]["params"]["fromDateTime"] == "2024-02-02T00:00:00Z"
        assert transport.requests[0]["params"]["toDateTime"] == "2024-02-02T23:59:59Z"
        assert transport.requests[0]["params"]["limit"] == "20"
        assert transport.requests[0]["params"]["offset"] == "0"
    if model == "CongressAmendments":
        assert transport.requests[0]["params"]["fromDateTime"] == "2024-02-01T00:00:00Z"
        assert transport.requests[0]["params"]["toDateTime"] == "2024-02-02T23:59:59Z"
        assert transport.requests[0]["params"]["offset"] == "5"
    assert transport.responses == []


@pytest.mark.parametrize("case", _CASES, ids=lambda case: str(case["model"]))
def test_each_model_rejects_a_hand_authored_destructive_fixture(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]
) -> None:
    """Malformed source records and unsafe document references fail closed per model."""
    fetcher = case["fetcher"]()
    transport = _install_transport(monkeypatch, (case["bad_response"],))
    with pytest.raises(_client.CongressGovProviderError):
        fetcher.fetch(ctx=_fixture_context(case["model"]), **case["bad_query"])
    assert len(transport.requests) == 1
    assert transport.responses == []


def test_registration_lists_exactly_eight_congress_models() -> None:
    assert tuple(PROVIDER.fetcher_dict) == (
        "CongressBills",
        "CongressBillInfo",
        "CongressBillText",
        "CongressAmendments",
        "CongressAmendmentInfo",
        "CongressAmendmentText",
        "CongressCommitteeInfo",
        "CongressCommitteeDocuments",
    )
    assert len(PROVIDER.fetcher_bindings) == 8
    assert all(not fetcher.capability.verified for fetcher in PROVIDER.fetchers)


@pytest.mark.parametrize(
    ("fetcher_type", "query", "expected"),
    (
        (
            CongressBillInfoFetcher,
            {"bill_url": "https://api.congress.gov/v3/bill/119/s/1947"},
            {"congress": 119, "bill_type": "s", "bill_number": 1947},
        ),
        (
            CongressBillTextFetcher,
            {"bill_url": ("https://www.congress.gov/bill/118th-congress/house-bill/1/text")},
            {"congress": 118, "bill_type": "hr", "bill_number": 1},
        ),
        (
            CongressAmendmentInfoFetcher,
            {"amendment_url": "https://api.congress.gov/v3/amendment/118/samdt/42"},
            {"congress": 118, "amendment_type": "samdt", "amendment_number": 42},
        ),
        (
            CongressAmendmentTextFetcher,
            {
                "amendment_url": (
                    "https://www.congress.gov/amendment/118th-congress/"
                    "senate-amendment/3309/all-info"
                )
            },
            {"congress": 118, "amendment_type": "samdt", "amendment_number": 3309},
        ),
        (
            CongressCommitteeInfoFetcher,
            {"committee_url": "https://api.congress.gov/v3/committee/house/hspw00"},
            {"chamber": "house", "committee_code": "hspw00"},
        ),
        (
            CongressCommitteeDocumentsFetcher,
            {"committee_url": ("https://api.congress.gov/v3/committee/house/hspw00/reports")},
            {
                "chamber": "house",
                "committee_code": "hspw00",
                "document_category": "reports",
            },
        ),
        (
            CongressCommitteeDocumentsFetcher,
            {
                "committee_url": ("https://api.congress.gov/v3/committee-meeting/118/house/99"),
                "committee_code": "HSPW00",
            },
            {
                "chamber": "house",
                "committee_code": "hspw00",
                "document_category": "meeting_documents",
                "congress": 118,
                "event_id": 99,
            },
        ),
    ),
    ids=(
        "bill-api",
        "bill-public-page",
        "amendment-api",
        "amendment-public-page",
        "committee-api",
        "committee-reports-api",
        "committee-meeting-api",
    ),
)
def test_official_resource_urls_resolve_to_structured_ids(
    fetcher_type: type,
    query: dict[str, object],
    expected: dict[str, object],
) -> None:
    """Documented source URLs are parsed locally, then validated as structured query IDs."""
    parsed = fetcher_type().transform_query(**query)
    assert {field: getattr(parsed, field) for field in expected} == expected
    serialized = parsed.model_dump(mode="json")
    assert serialized["source"] == "congress_gov"
    assert serialized["market"] == "us"


@pytest.mark.parametrize(
    ("fetcher_type", "query"),
    (
        (
            CongressBillInfoFetcher,
            {"bill_url": "https://evil.example/v3/bill/118/hr/1"},
        ),
        (
            CongressBillTextFetcher,
            {"bill_url": "https://api.congress.gov/v3/bill/118/hr/1?format=json"},
        ),
        (
            CongressAmendmentInfoFetcher,
            {"amendment_url": "https://user@api.congress.gov/v3/amendment/118/samdt/42"},
        ),
        (
            CongressCommitteeInfoFetcher,
            {"committee_url": "https://api.congress.gov/v3/committee/house/hspw00#detail"},
        ),
        (
            CongressBillInfoFetcher,
            {
                "bill_url": "https://api.congress.gov/v3/bill/118/hr/1",
                "bill_number": 2,
            },
        ),
        (
            CongressAmendmentTextFetcher,
            {"amendment_url": "https://api.congress.gov/v3/amendment/118/samdt/42/actions"},
        ),
    ),
    ids=("foreign-host", "query", "userinfo", "fragment", "conflicting-id", "extra-path"),
)
def test_resource_url_alias_rejects_untrusted_or_conflicting_identity(
    fetcher_type: type, query: dict[str, object]
) -> None:
    """URL aliases fail closed on host, URL decorations, path, and ID conflicts."""
    with pytest.raises(ValueError):
        fetcher_type().transform_query(**query)


def test_bill_info_public_url_runs_through_service_and_fetches_canonical_api_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service converts the public URL to IDs and never fetches caller URL text."""
    case = next(case for case in _CASES if case["model"] == "CongressBillInfo")
    fetcher = CongressBillInfoFetcher()
    model = "CongressBillInfo"
    contract = CongressBillInfo
    transport = _install_transport(monkeypatch, case["responses"])
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
    )

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="congress_gov",
            model=model,
            query={
                "bill_url": "https://api.congress.gov/v3/bill/118/hr/1",
                "include_components": [],
            },
            principal=_Principal(),
            ctx=_fixture_context(model),
        )
    )

    assert response["results"] == [
        contract.model_validate(case["expected"]).model_dump(mode="json")
    ]
    assert transport.requests[0]["url"] == "https://api.congress.gov/v3/bill/118/hr/1"
    assert transport.requests[0]["source"] == "congress_gov"
    assert transport.requests[0]["params"]["api_key"] == "fixture-api-key"


def test_list_queries_reject_reversed_date_windows() -> None:
    with pytest.raises(ValueError, match="start_date must not be after end_date"):
        CongressBillsFetcher().transform_query(start_date="2024-02-01", end_date="2024-01-31")
    with pytest.raises(ValueError, match="start_date must not be after end_date"):
        CongressAmendmentsFetcher().transform_query(start_date="2024-02-01", end_date="2024-01-31")


def test_committee_meeting_documents_page_through_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The detail-only meeting route still applies the caller's bounded page locally."""
    fetcher = CongressCommitteeDocumentsFetcher()
    model = "CongressCommitteeDocuments"
    transport = _install_transport(
        monkeypatch,
        (
            {
                "committeeMeeting": {
                    "eventId": 99,
                    "date": "2024-02-01T14:00:00Z",
                    "committees": {"item": [{"systemCode": "hspw00"}]},
                    "meetingDocuments": {
                        "item": [
                            {
                                "name": "First",
                                "documentType": "Hearing Notice",
                                "url": "https://www.congress.gov/118/meeting/house/99/first.pdf",
                            },
                            {
                                "name": "Second",
                                "documentType": "Support Document",
                                "url": "https://www.congress.gov/118/meeting/house/99/second.pdf",
                            },
                        ]
                    },
                }
            },
        ),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (_descriptor(fetcher, model), fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, CongressCommitteeDocument),
    )

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="congress_gov",
            model=model,
            query={
                "committee_code": "hspw00",
                "committee_url": ("https://api.congress.gov/v3/committee-meeting/118/house/99"),
                "limit": 1,
                "offset": 1,
            },
            principal=_Principal(),
            ctx=_fixture_context(model),
        )
    )

    assert response["results"] == [
        {
            "document_category": "meeting_documents",
            "committee_code": "hspw00",
            "chamber": "house",
            "congress": 118,
            "document_id": "99-1",
            "title": "Second",
            "description": None,
            "document_type": "Support Document",
            "event_date": "2024-02-01",
            "document_url": "https://www.congress.gov/118/meeting/house/99/second.pdf",
            "api_url": "https://api.congress.gov/v3/committee-meeting/118/house/99",
        }
    ]
    assert len(transport.requests) == 1
    assert transport.requests[0]["params"]["api_key"] == "fixture-api-key"
