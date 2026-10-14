"""Clean-room typed adapters for the eight Congress.gov Iteration 2 models."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import Field, StrictInt, field_validator, model_validator

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet

from opendata.data.capability import Capability
from opendata.data.models.congress_cboe_iter2 import (
    CongressAmendment,
    CongressAmendmentInfo,
    CongressAmendmentInfoRawData,
    CongressAmendmentText,
    CongressBill,
    CongressBillInfo,
    CongressBillInfoRawData,
    CongressBillReference,
    CongressBillSubject,
    CongressBillSummary,
    CongressBillText,
    CongressCommitteeDocument,
    CongressCommitteeHistory,
    CongressCommitteeInfo,
    CongressCommitteeInfoRawData,
    CongressCommitteeRef,
    CongressLatestAction,
    CongressSponsor,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.congress_gov.models import _client

BillType = Literal["hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres"]
AmendmentType = Literal["hamdt", "samdt", "suamdt"]
Chamber = Literal["house", "senate", "joint"]
BillComponent = Literal[
    "cosponsors", "subjects", "summaries", "committees", "actions", "relatedbills", "titles"
]
AmendmentComponent = Literal["cosponsors", "actions", "text"]
DocumentCategory = Literal["bills", "reports", "nominations", "communications", "meeting_documents"]
DocumentFormat = Literal["PDF", "XML", "HTML", "HTM", "TXT"]

_API_BASE = _client.API_ROOT
_PAGE_LIMIT = 250
_PAGE_OFFSET_MAX = 10_000_000
_BILL_COMPONENTS: tuple[BillComponent, ...] = (
    "cosponsors",
    "subjects",
    "summaries",
    "committees",
    "actions",
    "relatedbills",
    "titles",
)
_BILL_TYPES: frozenset[str] = frozenset(
    {"hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres"}
)
_AMENDMENT_TYPES: frozenset[str] = frozenset({"hamdt", "samdt", "suamdt"})
_CHAMBERS: frozenset[str] = frozenset({"house", "senate", "joint"})
_COMPONENT_KEYS: dict[BillComponent, tuple[str, str]] = {
    "cosponsors": ("cosponsors", "cosponsor"),
    "subjects": ("subjects", "item"),
    "summaries": ("summaries", "summary"),
    "committees": ("committees", "item"),
    "actions": ("actions", "item"),
    "relatedbills": ("relatedBills", "bill"),
    "titles": ("titles", "item"),
}


class _CongressQuery(QueryParams):
    source: Literal["congress_gov"] = "congress_gov"
    market: Literal["us"] = "us"


class _PageQuery(_CongressQuery):
    limit: StrictInt = Field(default=20, ge=1, le=_PAGE_LIMIT)
    offset: StrictInt = Field(default=0, ge=0, le=_PAGE_OFFSET_MAX)

    @model_validator(mode="after")
    def _validate_dates(self) -> _PageQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class CongressBillsQuery(_PageQuery):
    """Filters for one paged Congress.gov bill listing."""

    congress: StrictInt | None = Field(default=None, ge=1, le=999)
    bill_type: BillType | None = None

    @field_validator("bill_type", mode="before")
    @classmethod
    def _normalize_bill_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_shape(self) -> CongressBillsQuery:
        _reject_symbol(self)
        if self.bill_type is not None and self.congress is None:
            raise ValueError("bill_type filtering requires congress")
        return self


class CongressBillInfoQuery(_CongressQuery):
    """Identifies one bill and selected bounded components."""

    congress: StrictInt = Field(ge=1, le=999)
    bill_type: BillType
    bill_number: StrictInt = Field(ge=1, le=10_000_000)
    bill_url: str | None = None
    include_components: tuple[BillComponent, ...] = _BILL_COMPONENTS
    component_limit: StrictInt = Field(default=100, ge=1, le=_PAGE_LIMIT)

    @field_validator("bill_type", mode="before")
    @classmethod
    def _normalize_bill_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @field_validator("include_components", mode="before")
    @classmethod
    def _normalize_components(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _parse_bill_url(cls, value: object) -> object:
        return _merge_url_identity(value, "bill_url", _parse_bill_identity_url)

    @model_validator(mode="after")
    def _check_shape(self) -> CongressBillInfoQuery:
        _reject_common_window(self)
        if len(set(self.include_components)) != len(self.include_components):
            raise ValueError("include_components must not contain duplicates")
        return self


class _TextQuery(_CongressQuery):
    version_code: str | None = Field(default=None, min_length=1, max_length=24)
    document_format: DocumentFormat | None = None
    max_documents: StrictInt = Field(default=10, ge=1, le=20)

    @field_validator("version_code")
    @classmethod
    def _clean_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized or not re.fullmatch(r"[A-Z0-9_-]{1,24}", normalized):
            raise ValueError("version_code must be a short API text version identifier")
        return normalized

    @field_validator("document_format", mode="before")
    @classmethod
    def _normalize_format(cls, value: object) -> object:
        return value.upper().strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_shape(self) -> _TextQuery:
        _reject_common_window(self)
        return self


class CongressBillTextQuery(_TextQuery):
    """Selects text versions and formats for one bill."""

    congress: StrictInt = Field(ge=1, le=999)
    bill_type: BillType
    bill_number: StrictInt = Field(ge=1, le=10_000_000)
    bill_url: str | None = None

    @field_validator("bill_type", mode="before")
    @classmethod
    def _normalize_bill_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _parse_bill_url(cls, value: object) -> object:
        return _merge_url_identity(value, "bill_url", _parse_bill_identity_url)


class CongressAmendmentsQuery(_PageQuery):
    """Filters for one paged Congress.gov amendment listing."""

    congress: StrictInt | None = Field(default=None, ge=1, le=999)
    amendment_type: AmendmentType | None = None

    @field_validator("amendment_type", mode="before")
    @classmethod
    def _normalize_amendment_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_shape(self) -> CongressAmendmentsQuery:
        _reject_symbol(self)
        if self.amendment_type is not None and self.congress is None:
            raise ValueError("amendment_type filtering requires congress")
        return self


class CongressAmendmentInfoQuery(_CongressQuery):
    """Identifies one amendment and selected components."""

    congress: StrictInt = Field(ge=1, le=999)
    amendment_type: AmendmentType
    amendment_number: StrictInt = Field(ge=1, le=10_000_000)
    amendment_url: str | None = None
    include_components: tuple[AmendmentComponent, ...] = ("cosponsors", "actions", "text")
    component_limit: StrictInt = Field(default=100, ge=1, le=_PAGE_LIMIT)

    @field_validator("amendment_type", mode="before")
    @classmethod
    def _normalize_amendment_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @field_validator("include_components", mode="before")
    @classmethod
    def _normalize_components(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _parse_amendment_url(cls, value: object) -> object:
        return _merge_url_identity(value, "amendment_url", _parse_amendment_identity_url)

    @model_validator(mode="after")
    def _check_shape(self) -> CongressAmendmentInfoQuery:
        _reject_common_window(self)
        if len(set(self.include_components)) != len(self.include_components):
            raise ValueError("include_components must not contain duplicates")
        return self


class CongressAmendmentTextQuery(_TextQuery):
    """Selects text versions and formats for one amendment."""

    congress: StrictInt = Field(ge=1, le=999)
    amendment_type: AmendmentType
    amendment_number: StrictInt = Field(ge=1, le=10_000_000)
    amendment_url: str | None = None

    @field_validator("amendment_type", mode="before")
    @classmethod
    def _normalize_amendment_type(cls, value: object) -> object:
        return value.lower().strip() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _parse_amendment_url(cls, value: object) -> object:
        return _merge_url_identity(value, "amendment_url", _parse_amendment_identity_url)


class CongressCommitteeInfoQuery(_CongressQuery):
    """Identifies a committee by chamber and system code."""

    chamber: Chamber
    committee_code: str
    committee_url: str | None = None

    @field_validator("committee_code")
    @classmethod
    def _validate_committee_code(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]{4,10}", normalized):
            raise ValueError("committee_code must be a 4-10 character API system code")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _parse_committee_url(cls, value: object) -> object:
        return _merge_url_identity(value, "committee_url", _parse_committee_identity_url)

    @model_validator(mode="after")
    def _check_shape(self) -> CongressCommitteeInfoQuery:
        _reject_common_window(self)
        return self


class CongressCommitteeDocumentsQuery(_PageQuery):
    """Filters one paged committee document category."""

    chamber: Chamber
    committee_code: str
    document_category: DocumentCategory
    congress: StrictInt | None = Field(default=None, ge=1, le=999)
    event_id: StrictInt | None = Field(default=None, ge=1, le=10_000_000)
    committee_url: str | None = None

    @field_validator("committee_code")
    @classmethod
    def _validate_committee_code(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]{4,10}", normalized):
            raise ValueError("committee_code must be a 4-10 character API system code")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def _parse_committee_url(cls, value: object) -> object:
        return _merge_url_identity(value, "committee_url", _parse_committee_document_url)

    @model_validator(mode="after")
    def _check_shape(self) -> CongressCommitteeDocumentsQuery:
        _reject_common_window(self)
        if self.document_category == "nominations" and self.chamber != "senate":
            raise ValueError("nominations are available only for Senate committees")
        if self.document_category == "communications" and self.chamber == "joint":
            raise ValueError("committee communications require House or Senate")
        if self.document_category == "meeting_documents":
            if self.congress is None or self.event_id is None:
                raise ValueError("meeting_documents requires congress and event_id")
        elif self.event_id is not None:
            raise ValueError("event_id is only valid for meeting_documents")
        return self


_BILL_SITE_TYPES = {
    "house-bill": "hr",
    "senate-bill": "s",
    "house-joint-resolution": "hjres",
    "senate-joint-resolution": "sjres",
    "house-concurrent-resolution": "hconres",
    "senate-concurrent-resolution": "sconres",
    "house-resolution": "hres",
    "senate-resolution": "sres",
}
_AMENDMENT_SITE_TYPES = {"house-amendment": "hamdt", "senate-amendment": "samdt"}
_BILL_SITE_SECTIONS = frozenset(
    {
        "all-info",
        "actions",
        "amendments",
        "committees",
        "cosponsors",
        "related-bills",
        "subjects",
        "summaries",
        "text",
        "titles",
    }
)
_AMENDMENT_SITE_SECTIONS = frozenset({"all-info", "actions", "text"})


def _merge_url_identity(
    value: object,
    parameter: str,
    parser: Callable[[object], dict[str, object]],
) -> object:
    """Resolve a documented URL alias into structured IDs without fetching that URL."""
    if not isinstance(value, Mapping):
        return value
    data = dict(value)
    url = data.get(parameter)
    if url is None:
        return data
    parsed = parser(url)
    for field, expected in parsed.items():
        actual = data.get(field)
        if actual is not None and not _same_identity_value(actual, expected):
            raise ValueError(f"{parameter} conflicts with {field}")
        data[field] = expected
    return data


def _same_identity_value(actual: object, expected: object) -> bool:
    if isinstance(expected, str):
        return isinstance(actual, str) and actual.strip().lower() == expected
    if type(actual) is int:
        return actual == expected
    return isinstance(actual, str) and actual.isdecimal() and int(actual) == expected


def _official_path(value: object) -> tuple[str, str]:
    """Validate an official API/site URL; do not accept query, fragment, or userinfo."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("official URL must be a non-empty, whitespace-free string")
    if "%" in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("official URL contains an unsafe path character")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("official URL is malformed") from None
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or "?" in value
        or "#" in value
        or parts.query
        or parts.fragment
    ):
        raise ValueError("official URL must be HTTPS without credentials, query, or fragment")
    host = parts.hostname.lower()
    if host not in {"api.congress.gov", "www.congress.gov", "congress.gov"}:
        raise ValueError("official URL host is not allowlisted")
    return host, parts.path


def _bill_identity_from_path(path: str, *, api: bool) -> dict[str, object]:
    segments = path.split("/")
    if api:
        if len(segments) != 6 or segments[:3] != ["", "v3", "bill"]:
            raise ValueError("bill URL must identify exactly one Congress.gov API bill")
        congress_text, bill_type, number_text = segments[3:]
    else:
        if len(segments) not in {5, 6} or segments[1] != "bill":
            raise ValueError("bill URL must identify exactly one Congress.gov bill page")
        congress_page, bill_section, number_text = segments[2:5]
        congress_match = re.fullmatch(r"([1-9][0-9]{0,2})th-congress", congress_page)
        if congress_match is None:
            raise ValueError("Congress.gov bill page has an invalid Congress segment")
        congress_text = congress_match.group(1)
        bill_type = _BILL_SITE_TYPES.get(bill_section, "")
        if len(segments) == 6 and segments[5] not in _BILL_SITE_SECTIONS:
            raise ValueError("Congress.gov bill page has an unsupported detail path")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", congress_text):
        raise ValueError("bill URL has an invalid Congress number")
    if bill_type not in _BILL_TYPES:
        raise ValueError("bill URL has an unsupported bill type")
    if not re.fullmatch(r"[1-9][0-9]{0,7}", number_text):
        raise ValueError("bill URL has an invalid bill number")
    return {
        "congress": int(congress_text),
        "bill_type": bill_type,
        "bill_number": int(number_text),
    }


def _parse_bill_identity_url(value: object) -> dict[str, object]:
    """Parse a public Congress.gov bill URL or its documented compact identity."""
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,2}/[a-zA-Z]+/[1-9][0-9]{0,7}", value):
        path = "/v3/bill/" + value
        return _bill_identity_from_path(path, api=True)
    host, path = _official_path(value)
    if host == "api.congress.gov":
        return _bill_identity_from_path(path, api=True)
    return _bill_identity_from_path(path, api=False)


def _amendment_identity_from_path(path: str, *, api: bool) -> dict[str, object]:
    segments = path.split("/")
    if api:
        if len(segments) != 6 or segments[:3] != ["", "v3", "amendment"]:
            raise ValueError("amendment URL must identify exactly one Congress.gov API amendment")
        congress_text, amendment_type, number_text = segments[3:]
    else:
        if len(segments) not in {5, 6} or segments[1] != "amendment":
            raise ValueError("amendment URL must identify exactly one Congress.gov amendment page")
        congress_page, amendment_section, number_text = segments[2:5]
        congress_match = re.fullmatch(r"([1-9][0-9]{0,2})th-congress", congress_page)
        if congress_match is None:
            raise ValueError("Congress.gov amendment page has an invalid Congress segment")
        congress_text = congress_match.group(1)
        amendment_type = _AMENDMENT_SITE_TYPES.get(amendment_section, "")
        if len(segments) == 6 and segments[5] not in _AMENDMENT_SITE_SECTIONS:
            raise ValueError("Congress.gov amendment page has an unsupported detail path")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", congress_text):
        raise ValueError("amendment URL has an invalid Congress number")
    if amendment_type not in _AMENDMENT_TYPES:
        raise ValueError("amendment URL has an unsupported amendment type")
    if not re.fullmatch(r"[1-9][0-9]{0,7}", number_text):
        raise ValueError("amendment URL has an invalid amendment number")
    return {
        "congress": int(congress_text),
        "amendment_type": amendment_type,
        "amendment_number": int(number_text),
    }


def _parse_amendment_identity_url(value: object) -> dict[str, object]:
    """Parse a public Congress.gov amendment URL or its documented compact identity."""
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,2}/[a-zA-Z]+/[1-9][0-9]{0,7}", value):
        path = "/v3/amendment/" + value
        return _amendment_identity_from_path(path, api=True)
    host, path = _official_path(value)
    if host == "api.congress.gov":
        return _amendment_identity_from_path(path, api=True)
    return _amendment_identity_from_path(path, api=False)


def _parse_committee_identity_url(value: object) -> dict[str, object]:
    """Parse one official Congress.gov committee detail API URL."""
    host, path = _official_path(value)
    segments = path.split("/")
    if host != "api.congress.gov" or len(segments) != 5:
        raise ValueError("committee URL must identify one Congress.gov API committee")
    if segments[:3] != ["", "v3", "committee"]:
        raise ValueError("committee URL must use the documented committee API path")
    chamber, committee_code = segments[3:]
    if chamber not in _CHAMBERS or not re.fullmatch(r"[a-zA-Z0-9]{4,10}", committee_code):
        raise ValueError("committee URL has an invalid chamber or system code")
    return {"chamber": chamber, "committee_code": committee_code.lower()}


def _parse_committee_document_url(value: object) -> dict[str, object]:
    """Parse documented committee collection or meeting API paths into query IDs."""
    host, path = _official_path(value)
    if host != "api.congress.gov":
        raise ValueError("committee document URL must use the Congress.gov API host")
    segments = path.split("/")
    if len(segments) == 6 and segments[:3] == ["", "v3", "committee"]:
        chamber, committee_code, endpoint = segments[3:]
        if chamber not in _CHAMBERS or not re.fullmatch(r"[a-zA-Z0-9]{4,10}", committee_code):
            raise ValueError("committee document URL has an invalid committee identity")
        categories = {
            "bills": "bills",
            "reports": "reports",
            "nominations": "nominations",
            "house-communication": "communications",
            "senate-communication": "communications",
        }
        if endpoint not in categories:
            raise ValueError("committee document URL has an unsupported collection path")
        endpoint_chamber = "house" if endpoint == "house-communication" else "senate"
        if (
            endpoint in {"house-communication", "senate-communication"}
            and chamber != endpoint_chamber
        ):
            raise ValueError("committee communication path conflicts with its chamber")
        return {
            "chamber": chamber,
            "committee_code": committee_code.lower(),
            "document_category": categories[endpoint],
        }
    if len(segments) == 6 and segments[:3] == ["", "v3", "committee-meeting"]:
        congress_text, chamber, event_text = segments[3:]
        if not re.fullmatch(r"[1-9][0-9]{0,2}", congress_text):
            raise ValueError("committee meeting URL has an invalid Congress number")
        if chamber not in _CHAMBERS or not re.fullmatch(r"[1-9][0-9]{0,7}", event_text):
            raise ValueError("committee meeting URL has an invalid chamber or event id")
        return {
            "chamber": chamber,
            "document_category": "meeting_documents",
            "congress": int(congress_text),
            "event_id": int(event_text),
        }
    raise ValueError("committee document URL has an unsupported API path")


class CongressBillsFetcher(Fetcher[CongressBillsQuery, list[dict[str, Any]]]):
    """Fetch one explicitly paged bill-list slice."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressBills"
    row_model: ClassVar[type[ContractModel]] = CongressBill
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_bills",
        period="snapshot",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Official API contract; live access and rights evidence remain unverified.",
    )

    def transform_query(self, **kwargs: object) -> CongressBillsQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressBillsQuery.model_validate(kwargs)

    def extract_data(self, params: CongressBillsQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch and validate source data for the supplied query."""
        path = "/bill"
        if params.congress is not None:
            path += f"/{params.congress}"
            if params.bill_type is not None:
                path += f"/{params.bill_type}"
        query = {
            "limit": str(params.limit),
            "offset": str(params.offset),
            **_date_range_params(params),
        }
        document = _required_document(
            _client.get_json(path, params=query, context=ctx, transport=self.http_transport)
        )
        return _bounded_collection(document, "bills", "bill", params.limit)

    def transform_data(self, raw: list[dict[str, Any]], params: CongressBillsQuery) -> FetchResult:
        """Convert fetched data into typed model results."""
        del params
        return tuple(_map_bill(row) for row in raw)


class CongressBillInfoFetcher(Fetcher[CongressBillInfoQuery, dict[str, Any]]):
    """Fetch a bill detail and only the caller-selected official subresources."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressBillInfo"
    row_model: ClassVar[type[ContractModel]] = CongressBillInfo
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_bill_info",
        period="snapshot",
        market="us",
        source="congress_gov",
        verified=False,
        notes=(
            "Official API detail and optional bounded joins; live access and rights remain "
            "unverified."
        ),
    )

    def transform_query(self, **kwargs: object) -> CongressBillInfoQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressBillInfoQuery.model_validate(kwargs)

    def extract_data(self, params: CongressBillInfoQuery, ctx: FetchContext) -> dict[str, Any]:
        """Fetch and validate source data for the supplied query."""
        path = _bill_path(params.congress, params.bill_type, params.bill_number)
        base_doc = _required_document(
            _client.get_json(path, params={}, context=ctx, transport=self.http_transport)
        )
        bill = _required_object(base_doc, "bill")
        result: dict[str, Any] = {"bill": bill, "components": {}}
        for component in params.include_components:
            suffix, collection_key = _COMPONENT_KEYS[component]
            component_doc = _client.get_json(
                f"{path}/{suffix}",
                params={"limit": str(params.component_limit), "offset": "0"},
                context=ctx,
                transport=self.http_transport,
                allow_not_found=True,
            )
            if component_doc is None:
                result["components"][component] = []
                continue
            if component == "subjects":
                result["components"][component] = _subject_rows(component_doc)
            else:
                rows = _collection_rows(component_doc, collection_key[0], collection_key[1])
                _enforce_component_count(component_doc, len(rows), params.component_limit)
                result["components"][component] = rows
        result["identity"] = (params.congress, params.bill_type, params.bill_number)
        return result

    def transform_data(self, raw: dict[str, Any], params: CongressBillInfoQuery) -> FetchResult:
        """Convert fetched data into typed model results."""
        bill = raw["bill"]
        components = raw["components"]
        mapped = {
            **_map_bill_identity(bill),
            "sponsors": tuple(
                _map_sponsor(item) for item in _objects(bill.get("sponsors"), "sponsor")
            ),
            "cosponsors": tuple(_map_sponsor(item) for item in components.get("cosponsors", [])),
            "subjects": tuple(_map_subject(item) for item in components.get("subjects", [])),
            "summaries": tuple(_map_summary(item) for item in components.get("summaries", [])),
            "committees": tuple(
                _map_committee_ref(item) for item in components.get("committees", [])
            ),
            "actions": tuple(_map_action(item) for item in components.get("actions", [])),
            "related_bills": tuple(
                _map_related_bill(item) for item in components.get("relatedbills", [])
            ),
            "titles": tuple(_title_text(item) for item in components.get("titles", [])),
            "source_url": _safe_api_url(bill.get("url")) or _bill_url(params),
            "included_components": params.include_components,
        }
        try:
            raw_data = CongressBillInfoRawData.model_validate(mapped)
            markdown_content = _bill_info_markdown(raw_data)
            return (
                CongressBillInfo.model_validate(
                    {
                        "markdown_content": markdown_content,
                        "raw_data": raw_data,
                        "source_url": raw_data.source_url,
                    }
                ),
            )
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_BILL_INFO") from None


class CongressBillTextFetcher(Fetcher[CongressBillTextQuery, list[dict[str, Any]]]):
    """Download original bill text bytes from text-version links published by the API."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressBillText"
    row_model: ClassVar[type[ContractModel]] = CongressBillText
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_bill_text",
        period="event",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Downloaded official text versions remain base64 bytes with source format metadata.",
    )

    def transform_query(self, **kwargs: object) -> CongressBillTextQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressBillTextQuery.model_validate(kwargs)

    def extract_data(
        self, params: CongressBillTextQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch and validate source data for the supplied query."""
        path = f"{_bill_path(params.congress, params.bill_type, params.bill_number)}/text"
        text_doc = _required_document(
            _client.get_json(
                path,
                params={"limit": str(_PAGE_LIMIT), "offset": "0"},
                context=ctx,
                transport=self.http_transport,
            )
        )
        versions = _bounded_collection(text_doc, "textVersions", "item", _PAGE_LIMIT)
        _enforce_component_count(text_doc, len(versions), _PAGE_LIMIT)
        documents = _select_text_documents(
            versions,
            version_code=params.version_code,
            document_format=params.document_format,
            maximum=params.max_documents,
        )
        result: list[dict[str, Any]] = []
        for version, format_item in documents:
            url = _format_url(format_item)
            body, media_type = _client.get_document(url, context=ctx, transport=self.http_transport)
            result.append(
                {
                    "congress": params.congress,
                    "bill_type": params.bill_type,
                    "bill_number": params.bill_number,
                    "version_code": version.get("type"),
                    "version_date": version.get("date"),
                    "document_format": _format_type(format_item),
                    "content_base64": base64.b64encode(body).decode("ascii"),
                    "media_type": media_type,
                    "source_url": url,
                }
            )
        return result

    def transform_data(
        self, raw: list[dict[str, Any]], params: CongressBillTextQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        del params
        try:
            return tuple(CongressBillText.model_validate(row) for row in raw)
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_BILL_TEXT") from None


class CongressAmendmentsFetcher(Fetcher[CongressAmendmentsQuery, list[dict[str, Any]]]):
    """Fetch one explicitly paged amendment-list slice."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressAmendments"
    row_model: ClassVar[type[ContractModel]] = CongressAmendment
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_amendments",
        period="snapshot",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Official API contract; live access and rights evidence remain unverified.",
    )

    def transform_query(self, **kwargs: object) -> CongressAmendmentsQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressAmendmentsQuery.model_validate(kwargs)

    def extract_data(
        self, params: CongressAmendmentsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch and validate source data for the supplied query."""
        path = "/amendment"
        if params.congress is not None:
            path += f"/{params.congress}"
            if params.amendment_type is not None:
                path += f"/{params.amendment_type}"
        query = {
            "limit": str(params.limit),
            "offset": str(params.offset),
            **_date_range_params(params),
        }
        document = _required_document(
            _client.get_json(path, params=query, context=ctx, transport=self.http_transport)
        )
        return _bounded_collection(document, "amendments", "amendment", params.limit)

    def transform_data(
        self, raw: list[dict[str, Any]], params: CongressAmendmentsQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        del params
        try:
            return tuple(_map_amendment(row) for row in raw)
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDMENT_ROW") from None


class CongressAmendmentInfoFetcher(Fetcher[CongressAmendmentInfoQuery, dict[str, Any]]):
    """Fetch one amendment detail and bounded cosponsor/action/text metadata."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressAmendmentInfo"
    row_model: ClassVar[type[ContractModel]] = CongressAmendmentInfo
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_amendment_info",
        period="snapshot",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Official API detail and optional bounded subresources; rights remain unverified.",
    )

    def transform_query(self, **kwargs: object) -> CongressAmendmentInfoQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressAmendmentInfoQuery.model_validate(kwargs)

    def extract_data(self, params: CongressAmendmentInfoQuery, ctx: FetchContext) -> dict[str, Any]:
        """Fetch and validate source data for the supplied query."""
        path = _amendment_path(params.congress, params.amendment_type, params.amendment_number)
        document = _required_document(
            _client.get_json(path, params={}, context=ctx, transport=self.http_transport)
        )
        amendment = _required_object(document, "amendment")
        components: dict[str, list[dict[str, Any]]] = {}
        for component in params.include_components:
            suffix = {"cosponsors": "cosponsors", "actions": "actions", "text": "text"}[component]
            child = _client.get_json(
                f"{path}/{suffix}",
                params={"limit": str(params.component_limit), "offset": "0"},
                context=ctx,
                transport=self.http_transport,
                allow_not_found=True,
            )
            if child is None:
                components[component] = []
                continue
            collection = "textVersions" if component == "text" else component
            singular = (
                "item"
                if component == "text"
                else ("cosponsor" if component == "cosponsors" else "item")
            )
            rows = _collection_rows(child, collection, singular)
            _enforce_component_count(child, len(rows), params.component_limit)
            components[component] = rows
        return {"amendment": amendment, "components": components}

    def transform_data(
        self, raw: dict[str, Any], params: CongressAmendmentInfoQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        amendment = raw["amendment"]
        components = raw["components"]
        mapped = {
            **_map_amendment_identity(amendment),
            "sponsors": tuple(
                _map_sponsor(item) for item in _objects(amendment.get("sponsors"), "sponsor")
            ),
            "cosponsors": tuple(_map_sponsor(item) for item in components.get("cosponsors", [])),
            "actions": tuple(_map_action(item) for item in components.get("actions", [])),
            "text_versions": tuple(
                _text_version_label(item) for item in components.get("text", [])
            ),
            "source_url": _safe_api_url(amendment.get("url")) or _amendment_url(params),
            "included_components": params.include_components,
        }
        try:
            raw_data = CongressAmendmentInfoRawData.model_validate(mapped)
            markdown_content = _amendment_info_markdown(raw_data)
            return (
                CongressAmendmentInfo.model_validate(
                    {
                        "markdown_content": markdown_content,
                        "raw_data": raw_data,
                        "source_url": raw_data.source_url,
                    }
                ),
            )
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDMENT_INFO") from None


class CongressAmendmentTextFetcher(Fetcher[CongressAmendmentTextQuery, list[dict[str, Any]]]):
    """Download original amendment text bytes from documented text-version URLs."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressAmendmentText"
    row_model: ClassVar[type[ContractModel]] = CongressAmendmentText
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_amendment_text",
        period="event",
        market="us",
        source="congress_gov",
        verified=False,
        notes=(
            "Text endpoint coverage is source-dependent and original bytes remain base64 encoded."
        ),
    )

    def transform_query(self, **kwargs: object) -> CongressAmendmentTextQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressAmendmentTextQuery.model_validate(kwargs)

    def extract_data(
        self, params: CongressAmendmentTextQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch and validate source data for the supplied query."""
        path = (
            f"{_amendment_path(params.congress, params.amendment_type, params.amendment_number)}"
            "/text"
        )
        document = _required_document(
            _client.get_json(
                path,
                params={"limit": str(_PAGE_LIMIT), "offset": "0"},
                context=ctx,
                transport=self.http_transport,
            )
        )
        versions = _bounded_collection(document, "textVersions", "item", _PAGE_LIMIT)
        _enforce_component_count(document, len(versions), _PAGE_LIMIT)
        selections = _select_text_documents(
            versions,
            version_code=params.version_code,
            document_format=params.document_format,
            maximum=params.max_documents,
        )
        rows: list[dict[str, Any]] = []
        for version, format_item in selections:
            url = _format_url(format_item)
            body, media_type = _client.get_document(url, context=ctx, transport=self.http_transport)
            rows.append(
                {
                    "congress": params.congress,
                    "amendment_type": params.amendment_type,
                    "amendment_number": params.amendment_number,
                    "version_code": version.get("type"),
                    "version_date": version.get("date"),
                    "document_format": _format_type(format_item),
                    "content_base64": base64.b64encode(body).decode("ascii"),
                    "media_type": media_type,
                    "source_url": url,
                }
            )
        return rows

    def transform_data(
        self, raw: list[dict[str, Any]], params: CongressAmendmentTextQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        del params
        try:
            return tuple(CongressAmendmentText.model_validate(row) for row in raw)
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDMENT_TEXT") from None


class CongressCommitteeInfoFetcher(Fetcher[CongressCommitteeInfoQuery, dict[str, Any]]):
    """Fetch official committee identity, history, parentage, and activity counts."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressCommitteeInfo"
    row_model: ClassVar[type[ContractModel]] = CongressCommitteeInfo
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_committee_info",
        period="snapshot",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Official committee endpoint only; external membership roster is not included.",
    )

    def transform_query(self, **kwargs: object) -> CongressCommitteeInfoQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressCommitteeInfoQuery.model_validate(kwargs)

    def extract_data(self, params: CongressCommitteeInfoQuery, ctx: FetchContext) -> dict[str, Any]:
        """Fetch and validate source data for the supplied query."""
        path = f"/committee/{params.chamber}/{params.committee_code}"
        document = _required_document(
            _client.get_json(path, params={}, context=ctx, transport=self.http_transport)
        )
        return _required_object(document, "committee")

    def transform_data(
        self, raw: dict[str, Any], params: CongressCommitteeInfoQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        mapped = {
            "system_code": raw.get("systemCode"),
            "name": raw.get("name"),
            "chamber": raw.get("chamber") or params.chamber.title(),
            "committee_type": raw.get("committeeTypeCode") or raw.get("type"),
            "website_url": raw.get("committeeWebsiteUrl"),
            "is_current": _optional_bool(raw.get("isCurrent")),
            "parent": _map_parent(raw.get("parent")),
            "subcommittees": tuple(
                _map_committee_ref(row)
                for row in _collection_from_value(raw.get("subcommittees"), "item")
            ),
            "history": tuple(
                _map_committee_history(row)
                for row in _collection_from_value(raw.get("history"), "item")
            ),
            "bill_count": _nested_count(raw.get("bills")),
            "report_count": _nested_count(raw.get("reports")),
            "nomination_count": _nested_count(raw.get("nominations")),
            "communication_count": _nested_count(raw.get("communications")),
            "source_url": f"{_API_BASE}/committee/{params.chamber}/{params.committee_code}",
            "membership_roster_included": False,
        }
        try:
            raw_data = CongressCommitteeInfoRawData.model_validate(mapped)
            markdown_content = _committee_info_markdown(raw_data)
            return (
                CongressCommitteeInfo.model_validate(
                    {
                        "markdown_content": markdown_content,
                        "raw_data": raw_data,
                        "source_url": raw_data.source_url,
                    }
                ),
            )
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_COMMITTEE_INFO") from None


class CongressCommitteeDocumentsFetcher(
    Fetcher[CongressCommitteeDocumentsQuery, list[dict[str, Any]]]
):
    """Fetch typed metadata from the official committee API, without web scraping."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CongressCommitteeDocuments"
    row_model: ClassVar[type[ContractModel]] = CongressCommitteeDocument
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="government",
        domain="congress_committee_documents",
        period="event",
        market="us",
        source="congress_gov",
        verified=False,
        notes="Official API metadata and published links only; no HTML scraping or PDF collection.",
    )

    def transform_query(self, **kwargs: object) -> CongressCommitteeDocumentsQuery:
        """Validate keyword arguments as this fetcher query."""
        return CongressCommitteeDocumentsQuery.model_validate(kwargs)

    def extract_data(
        self, params: CongressCommitteeDocumentsQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch and validate source data for the supplied query."""
        if params.document_category == "meeting_documents":
            path = f"/committee-meeting/{params.congress}/{params.chamber}/{params.event_id}"
            document = _required_document(
                _client.get_json(path, params={}, context=ctx, transport=self.http_transport)
            )
            meeting = _required_object(document, "committeeMeeting")
            if not _meeting_contains_committee(meeting, params.committee_code):
                raise _client.CongressGovProviderError("CONGRESS_GOV_MEETING_COMMITTEE_MISMATCH")
            meeting_documents = _collection_from_value(meeting.get("meetingDocuments"), "item")
            page = meeting_documents[params.offset : params.offset + params.limit]
            rows = [
                _document_row(
                    row,
                    category="meeting_documents",
                    committee_code=params.committee_code,
                    chamber=params.chamber,
                    congress=params.congress,
                    default_id=f"{params.event_id}-{index}",
                    event_date=meeting.get("date") or meeting.get("meetingDate"),
                    api_url=f"{_API_BASE}{path}",
                )
                for index, row in enumerate(page, start=params.offset)
            ]
            return rows

        category_path = {
            "bills": "bills",
            "reports": "reports",
            "nominations": "nominations",
        }
        if params.document_category == "communications":
            suffix = "house-communication" if params.chamber == "house" else "senate-communication"
        else:
            suffix = category_path[params.document_category]
        path = f"/committee/{params.chamber}/{params.committee_code}/{suffix}"
        document = _required_document(
            _client.get_json(
                path,
                params={"limit": str(params.limit), "offset": str(params.offset)},
                context=ctx,
                transport=self.http_transport,
            )
        )
        collection = {
            "bills": ("bills", "bill"),
            "reports": ("reports", "item"),
            "nominations": ("nominations", "item"),
            "communications": (
                "houseCommunications" if params.chamber == "house" else "senateCommunications",
                "item",
            ),
        }[params.document_category]
        raw_rows = _bounded_collection(document, collection[0], collection[1], params.limit)
        return [
            _document_row(
                row,
                category=params.document_category,
                committee_code=params.committee_code,
                chamber=params.chamber,
                congress=_int_or_none(row.get("congress")) or params.congress,
                default_id=f"{params.offset + index}",
                event_date=row.get("actionDate")
                or row.get("referralDate")
                or row.get("receivedDate"),
                api_url=_safe_api_url(row.get("url")),
            )
            for index, row in enumerate(raw_rows)
        ]

    def transform_data(
        self, raw: list[dict[str, Any]], params: CongressCommitteeDocumentsQuery
    ) -> FetchResult:
        """Convert fetched data into typed model results."""
        del params
        try:
            return tuple(CongressCommitteeDocument.model_validate(row) for row in raw)
        except (TypeError, ValueError):
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_COMMITTEE_DOCUMENT") from None


def _reject_common_window(query: _CongressQuery) -> None:
    if query.start_date is not None or query.end_date is not None or query.symbol is not None:
        raise ValueError(
            "Congress.gov query uses source identifiers and does not accept market windows"
        )


def _reject_symbol(query: _CongressQuery) -> None:
    if query.symbol is not None:
        raise ValueError("Congress.gov query does not accept a market symbol")


def _date_range_params(query: _PageQuery) -> dict[str, str]:
    """Map inclusive calendar dates to the API's documented UTC datetime filters."""
    result: dict[str, str] = {}
    if query.start_date is not None:
        result["fromDateTime"] = f"{query.start_date.isoformat()}T00:00:00Z"
    if query.end_date is not None:
        result["toDateTime"] = f"{query.end_date.isoformat()}T23:59:59Z"
    return result


def _bill_path(congress: int, bill_type: str, number: int) -> str:
    return f"/bill/{congress}/{bill_type}/{number}"


def _bill_url(params: CongressBillInfoQuery) -> str:
    return f"{_API_BASE}{_bill_path(params.congress, params.bill_type, params.bill_number)}"


def _amendment_path(congress: int, amendment_type: str, number: int) -> str:
    return f"/amendment/{congress}/{amendment_type}/{number}"


def _amendment_url(params: CongressAmendmentInfoQuery) -> str:
    path = _amendment_path(params.congress, params.amendment_type, params.amendment_number)
    return f"{_API_BASE}{path}"


def _required_document(document: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    return document


def _required_object(document: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    return value


def _bounded_collection(
    document: Mapping[str, Any], collection_key: str, item_key: str, maximum: int
) -> list[dict[str, Any]]:
    rows = _collection_rows(document, collection_key, item_key)
    pagination = document.get("pagination")
    if isinstance(pagination, dict) and "count" in pagination:
        count = pagination["count"]
        if type(count) is not int or count < 0:
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_PAGINATION")
    if len(rows) > maximum:
        raise _client.CongressGovLimitError("CONGRESS_GOV_RESULT_LIMIT", maximum=maximum)
    return rows


def _collection_rows(
    document: Mapping[str, Any], collection_key: str, item_key: str
) -> list[dict[str, Any]]:
    if collection_key not in document:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    return _collection_from_value(document[collection_key], item_key)


def _collection_from_value(value: object, item_key: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        if item_key in value:
            rows = value[item_key]
        elif "item" in value:
            rows = value["item"]
        elif len(value) == 0:
            rows = []
        else:
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
        if rows is None:
            return []
        if isinstance(rows, dict):
            rows = [rows]
    elif value is None:
        return []
    else:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    return rows


def _enforce_component_count(document: Mapping[str, Any], rows: int, maximum: int) -> None:
    pagination = document.get("pagination")
    if not isinstance(pagination, dict) or "count" not in pagination:
        return
    count = pagination["count"]
    if type(count) is not int or count < 0:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_PAGINATION")
    if count > maximum:
        raise _client.CongressGovLimitError("CONGRESS_GOV_RESULT_LIMIT", maximum=maximum)
    if rows < count:
        raise _client.CongressGovProviderError("CONGRESS_GOV_INCOMPLETE_PAGE")


def _map_bill(row: Mapping[str, Any]) -> CongressBill:
    identity = _map_bill_identity(row)
    action = identity["latest_action"]
    mapped = {
        "update_date": identity["update_date"],
        "latest_action_date": action.action_date
        if isinstance(action, CongressLatestAction)
        else None,
        "bill_url": _safe_api_url(row.get("url")),
        "congress": identity["congress"],
        "bill_number": identity["bill_number"],
        "origin_chamber": _text_or_none(row.get("originChamber")),
        "origin_chamber_code": _text_or_none(row.get("originChamberCode")),
        "bill_type": str(identity["bill_type"]).upper(),
        "title": identity["title"],
        "latest_action": action.text if isinstance(action, CongressLatestAction) else None,
        "update_date_including_text": _datetime_or_none(row.get("updateDateIncludingText")),
    }
    try:
        return CongressBill.model_validate(mapped)
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_BILL_ROW") from None


def _map_bill_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "congress": _first(row, "congress"),
        "bill_type": str(_first(row, "billType", "type") or "").lower(),
        "bill_number": _first(row, "billNumber", "number"),
        "title": _first(row, "title", "shortTitle"),
        "update_date": _date_or_none(row.get("updateDate")),
        "update_date_including_text": _datetime_or_none(row.get("updateDateIncludingText")),
        "introduced_date": _date_or_none(row.get("introducedDate")),
        "latest_action": _map_action(row["latestAction"])
        if isinstance(row.get("latestAction"), dict)
        else None,
    }


def _map_action(row: Mapping[str, Any]) -> CongressLatestAction:
    try:
        return CongressLatestAction.model_validate(
            {
                "action_date": _date_or_none(row.get("actionDate")),
                "text": _text_or_none(row.get("text")),
                "action_type": _text_or_none(row.get("type")),
            }
        )
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_ACTION") from None


def _map_sponsor(row: Mapping[str, Any]) -> CongressSponsor:
    person = _nested_mapping_or_row(row, "member")
    name = _text_or_none(person.get("fullName")) or _join_name(
        person.get("firstName"), person.get("middleName"), person.get("lastName")
    )
    mapped = {
        "bioguide_id": _text_or_none(person.get("bioguideId")),
        "name": name,
        "first_name": _text_or_none(person.get("firstName")),
        "last_name": _text_or_none(person.get("lastName")),
        "party": _text_or_none(person.get("party")),
        "state": _text_or_none(person.get("state")),
        "district": _int_or_none(person.get("district")),
        "sponsorship_date": _date_or_none(row.get("sponsorshipDate")),
        "is_original_cosponsor": _bool_or_none(row.get("isOriginalCosponsor")),
    }
    try:
        return CongressSponsor.model_validate(mapped)
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SPONSOR") from None


def _map_subject(row: Mapping[str, Any]) -> CongressBillSubject:
    try:
        return CongressBillSubject.model_validate(
            {
                "name": _first(row, "name", "text"),
                "subject_type": _text_or_none(_first(row, "subjectType", "type")),
            }
        )
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SUBJECT") from None


def _map_summary(row: Mapping[str, Any]) -> CongressBillSummary:
    try:
        return CongressBillSummary.model_validate(
            {
                "action_date": _date_or_none(row.get("actionDate")),
                "version_code": _text_or_none(row.get("versionCode")),
                "text": _first(row, "text", "summaryText"),
                "update_date": _text_or_none(row.get("updateDate")),
            }
        )
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SUMMARY") from None


def _map_committee_ref(row: Mapping[str, Any]) -> CongressCommitteeRef:
    parent = _nested_mapping_or_row(row, "parent")
    mapped = {
        "system_code": _text_or_none(parent.get("systemCode")),
        "name": _text_or_none(parent.get("name")),
        "chamber": _text_or_none(parent.get("chamber")),
        "url": _safe_api_url(parent.get("url")),
    }
    try:
        return CongressCommitteeRef.model_validate(mapped)
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_COMMITTEE_REF") from None


def _map_related_bill(row: Mapping[str, Any]) -> CongressBillReference:
    related = _nested_mapping_or_row(row, "bill")
    try:
        return CongressBillReference.model_validate(
            {
                "congress": _first(related, "congress"),
                "bill_type": str(_first(related, "billType", "type") or "").lower(),
                "bill_number": _first(related, "billNumber", "number"),
                "title": _text_or_none(related.get("title")),
                "relationship": _text_or_none(_first(row, "relationshipType", "type")),
                "url": _safe_api_url(related.get("url")),
            }
        )
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_RELATED_BILL") from None


def _subject_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    subjects = document.get("subjects")
    if not isinstance(subjects, dict):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE")
    rows: list[dict[str, Any]] = []
    for group_key, subject_type in (
        ("legislativeSubjects", "legislative"),
        ("policyArea", "policy_area"),
    ):
        group = subjects.get(group_key)
        if group is None:
            continue
        if isinstance(group, dict) and "name" in group:
            group_rows = [group]
        else:
            group_rows = _collection_from_value(group, "item")
        rows.extend({**item, "subjectType": subject_type} for item in group_rows)
    return rows


def _title_text(row: Mapping[str, Any]) -> str:
    value = _first(row, "title", "titleType")
    if not isinstance(value, str) or not value.strip():
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_TITLE")
    return value.strip()


def _map_amendment(row: Mapping[str, Any]) -> CongressAmendment:
    amendment_number = _int_or_none(_first(row, "amendmentNumber", "number"))
    if amendment_number is None:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDMENT_ROW")
    latest_action = row.get("latestAction")
    action: Mapping[str, Any] = latest_action if isinstance(latest_action, Mapping) else {}
    mapped = {
        "congress": row.get("congress"),
        "amendment_type": str(_first(row, "amendmentType", "type") or "").upper(),
        "number": amendment_number,
        "amended_bill": _amended_bill_text(row.get("amendedBill")),
        "amended_bill_title": _text_or_none(row.get("amendedBillTitle")),
        "description": _text_or_none(row.get("description")),
        "purpose": _text_or_none(row.get("purpose")),
        "latest_action_date": _date_or_none(action.get("actionDate")),
        "latest_action": _text_or_none(action.get("text")),
        "latest_action_time": _text_or_none(action.get("actionTime")),
        "sponsor": _amendment_sponsor(row),
        "submitted_date": _date_or_none(row.get("submittedDate")),
        "update_date": _date_or_none(row.get("updateDate")),
        "amendment_url": _safe_api_url(row.get("url")),
    }
    try:
        return CongressAmendment.model_validate(mapped)
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDMENT_ROW") from None


def _map_amendment_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "congress": row.get("congress"),
        "amendment_type": str(_first(row, "amendmentType", "type") or "").lower(),
        "amendment_number": _first(row, "amendmentNumber", "number"),
        "description": _text_or_none(row.get("description")),
        "purpose": _text_or_none(row.get("purpose")),
        "update_date": _date_or_none(row.get("updateDate")),
        "latest_action": _map_action(row["latestAction"])
        if isinstance(row.get("latestAction"), dict)
        else None,
    }


def _amended_bill_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _text_or_none(value)
    if not isinstance(value, Mapping):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_AMENDED_BILL")
    bill_type = _text_or_none(_first(value, "billType", "type"))
    bill_number = _int_or_none(_first(value, "billNumber", "number"))
    if bill_type is None or bill_number is None:
        return None
    return f"{bill_type.upper()} {bill_number}"


def _amendment_sponsor(row: Mapping[str, Any]) -> str | None:
    sponsor = row.get("sponsor")
    if isinstance(sponsor, str):
        return _text_or_none(sponsor)
    if isinstance(sponsor, dict):
        mapped = _map_sponsor(sponsor)
        return mapped.name

    sponsors = row.get("sponsors")
    if sponsors is None:
        return None
    entries = _collection_from_value(sponsors, "item")
    if not entries:
        return None
    return _map_sponsor(entries[0]).name


def _bill_info_markdown(raw_data: CongressBillInfoRawData) -> str:
    lines = [
        f"# {_markdown_text(raw_data.title)}",
        "",
        f"- Congress: {raw_data.congress}",
        f"- Bill: {raw_data.bill_type.upper()} {raw_data.bill_number}",
        f"- Update date: {_markdown_date(raw_data.update_date)}",
        f"- Source: {raw_data.source_url}",
    ]
    if raw_data.latest_action is not None:
        lines.extend(
            [
                "",
                "## Latest action",
                f"- Date: {_markdown_date(raw_data.latest_action.action_date)}",
                f"- Text: {_markdown_text(raw_data.latest_action.text)}",
            ]
        )
    if raw_data.sponsors:
        names = ", ".join(
            _markdown_text(item.name) for item in raw_data.sponsors if item.name is not None
        )
        lines.extend(["", "## Sponsor", names or "—"])
    for label, rows in (
        ("Cosponsors", raw_data.cosponsors),
        ("Subjects", raw_data.subjects),
        ("Summaries", raw_data.summaries),
        ("Committees", raw_data.committees),
        ("Related bills", raw_data.related_bills),
    ):
        if rows:
            lines.extend(["", f"## {label}", f"Count: {len(rows)}"])
    return "\n".join(lines)


def _amendment_info_markdown(raw_data: CongressAmendmentInfoRawData) -> str:
    lines = [
        f"# {raw_data.amendment_type.upper()} {raw_data.amendment_number}",
        "",
        f"- Congress: {raw_data.congress}",
        f"- Description: {_markdown_text(raw_data.description)}",
        f"- Purpose: {_markdown_text(raw_data.purpose)}",
        f"- Update date: {_markdown_date(raw_data.update_date)}",
        f"- Source: {raw_data.source_url}",
    ]
    if raw_data.latest_action is not None:
        lines.extend(
            [
                "",
                "## Latest action",
                f"- Date: {_markdown_date(raw_data.latest_action.action_date)}",
                f"- Text: {_markdown_text(raw_data.latest_action.text)}",
            ]
        )
    if raw_data.sponsors:
        names = ", ".join(
            _markdown_text(item.name) for item in raw_data.sponsors if item.name is not None
        )
        lines.extend(["", "## Sponsor", names or "—"])
    for label, rows in (("Cosponsors", raw_data.cosponsors), ("Actions", raw_data.actions)):
        if rows:
            lines.extend(["", f"## {label}", f"Count: {len(rows)}"])
    return "\n".join(lines)


def _committee_info_markdown(raw_data: CongressCommitteeInfoRawData) -> str:
    lines = [
        f"# {_markdown_text(raw_data.name)}",
        "",
        f"- Chamber: {_markdown_text(raw_data.chamber)}",
        f"- System code: {raw_data.system_code}",
        f"- Committee type: {_markdown_text(raw_data.committee_type)}",
        f"- Website: {raw_data.website_url or '—'}",
        f"- Membership roster included: {str(raw_data.membership_roster_included).lower()}",
        f"- Source: {raw_data.source_url}",
    ]
    if raw_data.parent is not None:
        lines.extend(["", f"- Parent: {_markdown_text(raw_data.parent.name)}"])
    if raw_data.subcommittees:
        names = ", ".join(
            _markdown_text(item.name) for item in raw_data.subcommittees if item.name is not None
        )
        lines.extend(["", "## Subcommittees", names or "—"])
    if raw_data.history:
        lines.extend(["", "## Name history", f"Periods: {len(raw_data.history)}"])
    counts = {
        "Bills": raw_data.bill_count,
        "Reports": raw_data.report_count,
        "Nominations": raw_data.nomination_count,
        "Communications": raw_data.communication_count,
    }
    available_counts = [
        f"- {label}: {count}" for label, count in counts.items() if count is not None
    ]
    if available_counts:
        lines.extend(["", "## Activity counts", *available_counts])
    return "\n".join(lines)


def _markdown_text(value: str | None) -> str:
    if value is None:
        return "—"
    return " ".join(value.split()).replace("|", "\\|")


def _markdown_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "—"


def _text_version_label(row: Mapping[str, Any]) -> str:
    version = _text_or_none(row.get("type"))
    if version is None:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_TEXT_VERSION")
    return version


def _map_parent(value: object) -> CongressCommitteeRef | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_COMMITTEE_PARENT")
    return _map_committee_ref(value)


def _map_committee_history(row: Mapping[str, Any]) -> CongressCommitteeHistory:
    try:
        return CongressCommitteeHistory.model_validate(
            {
                "official_name": _text_or_none(row.get("officialName")),
                "library_of_congress_name": _text_or_none(row.get("libraryOfCongressName")),
                "start_date": _text_or_none(row.get("startDate")),
                "end_date": _text_or_none(row.get("endDate")),
                "committee_type": _text_or_none(row.get("committeeTypeCode")),
                "establishing_authority": _text_or_none(row.get("establishingAuthority")),
            }
        )
    except (TypeError, ValueError):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_COMMITTEE_HISTORY") from None


def _nested_count(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    return _int_or_none(value.get("count"))


def _meeting_contains_committee(meeting: Mapping[str, Any], committee_code: str) -> bool:
    for item in _collection_from_value(meeting.get("committees"), "item"):
        if item.get("systemCode") == committee_code:
            return True
    return False


def _document_row(
    row: Mapping[str, Any],
    *,
    category: str,
    committee_code: str,
    chamber: str,
    congress: int | None,
    default_id: str,
    event_date: object,
    api_url: str | None,
) -> dict[str, Any]:
    identifier = _first(
        row,
        "eventId",
        "billNumber",
        "amendmentNumber",
        "reportNumber",
        "number",
        "citation",
        "systemCode",
        "id",
    )
    url = _safe_document_url(row.get("url"))
    title = _first(row, "title", "name", "citation", "description")
    if not isinstance(title, str) or not title.strip():
        title = str(identifier or default_id)
    raw_type = _first(row, "documentType", "billType", "type", "communicationType")
    if isinstance(raw_type, dict):
        raw_type = _first(raw_type, "name", "code")
    return {
        "document_category": category,
        "committee_code": committee_code,
        "chamber": chamber,
        "congress": congress,
        "document_id": str(identifier or default_id),
        "title": title.strip(),
        "description": _text_or_none(row.get("description")),
        "document_type": _text_or_none(raw_type),
        "event_date": _date_or_none(event_date),
        "document_url": url,
        "api_url": api_url,
    }


def _select_text_documents(
    versions: list[dict[str, Any]],
    *,
    version_code: str | None,
    document_format: str | None,
    maximum: int,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for version in versions:
        version_type = _text_or_none(version.get("type"))
        if version_code is not None and (version_type or "").upper() != version_code:
            continue
        formats = _collection_from_value(version.get("formats"), "item")
        for format_item in formats:
            format_type = _format_type(format_item)
            if document_format is not None and format_type != document_format:
                continue
            selected.append((version, format_item))
            if len(selected) > maximum:
                raise _client.CongressGovLimitError("CONGRESS_GOV_DOCUMENT_LIMIT", maximum=maximum)
    return selected


def _format_type(format_item: Mapping[str, Any]) -> str:
    value = _first(format_item, "type", "format")
    if not isinstance(value, str):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_TEXT_FORMAT")
    normalized = value.strip().upper()
    if normalized not in {"PDF", "XML", "HTML", "HTM", "TXT"}:
        raise _client.CongressGovProviderError("CONGRESS_GOV_UNSUPPORTED_TEXT_FORMAT")
    return normalized


def _format_url(format_item: Mapping[str, Any]) -> str:
    value = _first(format_item, "url", "URL")
    safe = _safe_document_url(value)
    if safe is None:
        raise _client.CongressGovProviderError("CONGRESS_GOV_DOCUMENT_URL_REJECTED")
    return safe


def _safe_api_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.hostname != "api.congress.gov"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not parts.path.startswith("/v3/")
        or parts.fragment
    ):
        return None
    return f"https://api.congress.gov{parts.path}"


def _safe_document_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        _client._validate_document_url(value)
    except _client.CongressGovProviderError:
        return None
    return value


def _date_or_none(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_DATE")
    candidate = value.strip()
    if len(candidate) >= 10:
        candidate = candidate[:10]
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_DATE") from None


def _datetime_or_none(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min, tzinfo=timezone.utc)
    elif isinstance(value, str):
        candidate = value.strip()
        try:
            if len(candidate) == 10:
                result = datetime.combine(
                    date.fromisoformat(candidate), time.min, tzinfo=timezone.utc
                )
            else:
                result = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_DATETIME") from None
    else:
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_DATETIME")
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result


def _optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    return _bool_or_none(value)


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    if type(value) is bool:
        return value
    if value in ("true", "True", "1", 1):
        return True
    if value in ("false", "False", "0", 0):
        return False
    raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_BOOLEAN")


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_INTEGER")


def _text_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _client.CongressGovProviderError("CONGRESS_GOV_BAD_TEXT")
    result = value.strip()
    return result or None


def _first(row: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _nested_mapping_or_row(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return an optional nested object, preserving the established parent fallback."""
    nested = row.get(key)
    return nested if isinstance(nested, Mapping) else row


def _objects(value: object, item_key: str) -> list[dict[str, Any]]:
    return _collection_from_value(value, item_key)


def _join_name(*parts: object) -> str | None:
    names = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return " ".join(names) or None
