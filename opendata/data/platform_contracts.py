"""Versioned REST contracts for stable, semantically explicit data reads.

These models describe API payloads only. They do not persist publications or
snapshots, authorize a caller, or certify completeness or source rights.
Consumers should pin a publication or snapshot when a multi-page read must
remain stable while new data is published.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
TimeBound: TypeAlias = date | datetime

AssetClass = Literal[
    "equity",
    "fund",
    "bond",
    "future",
    "option",
    "fx",
    "crypto",
    "index",
    "financial",
    "macro",
    "other",
]
Frequency = Literal[
    "tick",
    "1s",
    "1m",
    "5m",
    "15m",
    "30m",
    "60m",
    "1d",
    "1w",
    "1mo",
    "irregular",
    "snapshot",
]
RecordKind = Literal["time_series", "snapshot", "event", "table"]
TemporalSemantics = Literal["business_date", "event_time", "observation_time", "none"]
AdjustmentBasis = Literal[
    "raw",
    "split_adjusted",
    "total_return_adjusted",
    "back_adjusted",
    "forward_adjusted",
    "not_applicable",
    "unknown",
]
PitPolicy = Literal["supported", "required", "unsupported"]
IdentityKeyEncoding = Literal["canonical-json-array-v1"]
StorageComparison = Literal["native", "binary_lexicographic"]
CoverageStatus = Literal["complete", "partial", "source_limited", "unknown", "not_applicable"]
CoverageScope = Literal[
    "business_history",
    "archived_snapshot_observations",
    "current_snapshot",
    "point_in_time_records",
    "not_applicable",
    "unknown",
]


class _ContractModel(BaseModel):
    """Immutable, strict base configuration for public platform payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


class TimeRange(_ContractModel):
    """A non-empty half-open time interval ``[start, end)``."""

    start: TimeBound
    end: TimeBound

    @field_validator("start", "end")
    @classmethod
    def normalize_time_bound(cls, value: TimeBound) -> TimeBound:
        """Require aware timestamps and normalize them to UTC."""
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("datetime bounds must include a timezone")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> TimeRange:
        """Require matching bound types and a strictly increasing interval."""
        if type(self.start) is not type(self.end):
            raise ValueError("range bounds must both be dates or both be datetimes")
        if self.end <= self.start:
            raise ValueError("range uses [start, end) and requires end > start")
        return self


class SourceIdentity(_ContractModel):
    """Stable source/provider identity attached to a dataset and its result."""

    source_id: Identifier
    source: Identifier


class FieldContract(_ContractModel):
    """One field's stable name, native value type, and nullability."""

    name: Identifier
    data_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "object",
        "array",
    ]
    nullable: bool = False


class StorageOrderTerm(_ContractModel):
    """Describe one physical storage sort term and how values compare."""

    field: Identifier
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] | None = None
    comparison: StorageComparison

    @model_validator(mode="after")
    def validate_comparison_nulls(self) -> StorageOrderTerm:
        """Binary lexical keys do not have SQL NULL placement semantics."""
        if self.comparison == "binary_lexicographic" and self.nulls is not None:
            raise ValueError("binary lexicographic storage terms cannot declare null placement")
        return self


class DatasetContract(_ContractModel):
    """Versioned dataset schema and financial/time semantics."""

    dataset_id: Identifier
    schema_version: Identifier
    semantic_version: Identifier
    asset_class: AssetClass
    frequency: Frequency
    record_kind: RecordKind
    fields: tuple[FieldContract, ...]
    business_key: tuple[Identifier, ...]
    ordering: tuple[Identifier, ...]
    filter_fields: tuple[Identifier, ...] = ()
    time_field: Identifier | None = None
    temporal_semantics: TemporalSemantics
    source_available_at_field: Identifier | None = None
    source: SourceIdentity
    field_units: dict[Identifier, Identifier] = Field(default_factory=dict)
    field_currencies: dict[Identifier, Identifier] = Field(default_factory=dict)
    currency: Identifier | None = None
    adjustment_basis: AdjustmentBasis
    field_adjustments: dict[Identifier, AdjustmentBasis] = Field(default_factory=dict)
    calendar_id: Identifier | None = None
    timezone: Identifier
    pit_policy: PitPolicy

    @model_validator(mode="after")
    def validate_contract_semantics(self) -> DatasetContract:
        """Reject incomplete identity, temporal, and stable-order declarations."""
        field_names = tuple(field.name for field in self.fields)
        if not field_names or len(set(field_names)) != len(field_names):
            raise ValueError("fields must contain unique names")
        known_fields = set(field_names)
        if not self.business_key or len(set(self.business_key)) != len(self.business_key):
            raise ValueError("business_key must contain unique fields")
        if not set(self.business_key).issubset(known_fields):
            raise ValueError("business_key fields must be declared in fields")
        if not self.ordering or len(set(self.ordering)) != len(self.ordering):
            raise ValueError("ordering must contain unique fields")
        if not set(self.ordering).issubset(known_fields):
            raise ValueError("ordering fields must be declared in fields")
        if not set(self.business_key).issubset(self.ordering):
            raise ValueError(
                "ordering must include every business_key field as a stable tie-breaker"
            )
        if (
            self.temporal_semantics == "business_date"
            and self.time_field is not None
            and self.time_field in known_fields
        ):
            expected_ordering = tuple(dict.fromkeys((self.time_field, *self.business_key)))
            if self.ordering != expected_ordering:
                raise ValueError("business_date ordering must follow time_field then business_key")
        field_by_name = {field.name: field for field in self.fields}
        if any(field_by_name[name].nullable for name in self.business_key):
            raise ValueError("business_key fields must be non-nullable")
        if len(set(self.filter_fields)) != len(self.filter_fields):
            raise ValueError("filter_fields must not contain duplicates")
        if not set(self.filter_fields).issubset(known_fields):
            raise ValueError("filter_fields must be declared in fields")
        if self.temporal_semantics == "none":
            if self.time_field is not None:
                raise ValueError("temporal_semantics='none' requires time_field=null")
        elif self.time_field is None:
            raise ValueError("temporal datasets require an explicit time_field")
        elif self.time_field not in known_fields:
            raise ValueError("time_field must be declared in fields")
        elif (
            self.temporal_semantics == "business_date"
            and field_by_name[self.time_field].data_type != "date"
        ):
            raise ValueError("business_date time_field must have data_type='date'")
        elif (
            self.temporal_semantics in {"event_time", "observation_time"}
            and field_by_name[self.time_field].data_type != "datetime"
        ):
            raise ValueError("timestamp semantics require a datetime time_field")
        if self.source_available_at_field is not None:
            if self.source_available_at_field not in known_fields:
                raise ValueError("source_available_at_field must be declared in fields")
            if field_by_name[self.source_available_at_field].data_type != "datetime":
                raise ValueError("source_available_at_field must have data_type='datetime'")
        for mapping_name, mapping in (
            ("field_units", self.field_units),
            ("field_currencies", self.field_currencies),
            ("field_adjustments", self.field_adjustments),
        ):
            if not set(mapping).issubset(known_fields):
                raise ValueError(f"{mapping_name} keys must be declared in fields")
        if self.record_kind == "snapshot":
            if self.temporal_semantics != "observation_time":
                raise ValueError(
                    "snapshot datasets describe observation_time, never inferred history"
                )
            if self.frequency != "snapshot":
                raise ValueError("snapshot record_kind requires frequency='snapshot'")
        if self.record_kind == "time_series" and self.temporal_semantics not in {
            "business_date",
            "event_time",
        }:
            raise ValueError("time_series requires business_date or event_time semantics")
        if self.temporal_semantics == "business_date" and self.frequency in {
            "tick",
            "1s",
            "1m",
            "5m",
            "15m",
            "30m",
            "60m",
        }:
            raise ValueError("intraday frequencies require timestamp semantics")
        return self

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Require an installed IANA timezone rather than an arbitrary label."""
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


def canonical_sha256(contract: DatasetContract) -> str:
    """Return the stable SHA-256 of the normalized native dataset contract JSON."""
    encoded = json.dumps(
        contract.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class DataCoverage(_ContractModel):
    """Coverage status plus the scope to which a completeness claim applies.

    ``archived_snapshot_observations`` counts only observations actually
    archived. It does not imply that every business date has a historical
    value; that claim requires ``business_history`` scope and independent
    boundary/calendar evidence.
    """

    status: CoverageStatus
    scope: CoverageScope
    requested: TimeRange | None = None
    observed: TimeRange | None = None
    missing_ranges: tuple[TimeRange, ...] = ()
    evidence_refs: tuple[Identifier, ...] = ()
    limitation: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_coverage_claim(self) -> DataCoverage:
        """Require evidence and gaps to match the declared coverage state."""
        if self.status == "complete" and not self.evidence_refs:
            raise ValueError("complete coverage requires evidence_refs")
        if self.status == "partial" and not self.missing_ranges:
            raise ValueError("partial coverage requires at least one missing range")
        if self.status == "source_limited" and not self.limitation:
            raise ValueError("source_limited coverage requires a limitation")
        if self.scope == "current_snapshot" and self.status == "complete":
            raise ValueError("a current snapshot cannot certify historical completeness")
        if self.requested is not None and self.observed is not None:
            if type(self.requested.start) is not type(self.observed.start):
                raise ValueError("requested and observed ranges must use the same time type")
            if self.observed.start < self.requested.start or self.observed.end > self.requested.end:
                raise ValueError("observed range must be contained in requested range")
        if self.requested is not None:
            for missing_range in self.missing_ranges:
                if type(self.requested.start) is not type(missing_range.start):
                    raise ValueError("missing ranges must use the requested range time type")
                if (
                    missing_range.start < self.requested.start
                    or missing_range.end > self.requested.end
                ):
                    raise ValueError("missing ranges must be contained in requested range")
        return self


class Publication(_ContractModel):
    """Immutable dataset publication identity, quality, and coverage receipt.

    Field-specific units and adjustment bases live in ``DatasetContract``;
    the publication does not claim one unit describes every output column.
    """

    publication_id: Identifier
    dataset_id: Identifier
    source: SourceIdentity
    schema_version: Identifier
    semantic_version: Identifier
    sequence: int = Field(ge=1)
    published_at: datetime
    record_count: int = Field(ge=0)
    content_sha256: Sha256
    quality_status: Literal["passed", "warning", "rejected", "unknown"]
    quality_report_id: Identifier | None = None
    quality_issue_count: int = Field(default=0, ge=0)
    provenance: dict[str, JsonValue]
    provenance_sha256: Sha256 | None = None
    watermark: TimeRange | None = None
    coverage: DataCoverage

    @field_validator("provenance", mode="before")
    @classmethod
    def validate_provenance(cls, value: object) -> object:
        """Require bounded finite JSON provenance bound to a dataset contract."""
        _validate_json(value, path="provenance", depth=0, budget=[0])
        if not isinstance(value, dict):
            raise ValueError("provenance must be an object")
        contract_hash = value.get("contract_sha256")
        if (
            not isinstance(contract_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", contract_hash) is None
        ):
            raise ValueError("provenance.contract_sha256 must be a lowercase SHA-256 digest")
        try:
            serialized = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("provenance must contain only finite JSON values") from exc
        if len(serialized.encode("utf-8")) > 65_536:
            raise ValueError("provenance exceeds the 65536-byte limit")
        return value

    @field_validator("published_at")
    @classmethod
    def normalize_published_at(cls, value: datetime) -> datetime:
        """Require an aware publication instant and normalize it to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value.astimezone(timezone.utc)


class DataSnapshot(_ContractModel):
    """Immutable read point pinned to one publication and selection digest.

    ``as_of`` is the platform publication visibility cutoff. It is not a source
    ``available_at`` instant; source availability belongs in a declared row
    field such as ``source_available_at_field`` on the dataset contract.
    """

    snapshot_id: Identifier
    dataset_id: Identifier
    publication_id: Identifier
    publication_sequence: int = Field(ge=1)
    schema_version: Identifier
    semantic_version: Identifier
    created_at: datetime
    as_of: datetime
    selection_sha256: Sha256
    content_sha256: Sha256

    @field_validator("created_at", "as_of")
    @classmethod
    def normalize_snapshot_instants(cls, value: datetime) -> datetime:
        """Require aware snapshot instants and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamps must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_snapshot_order(self) -> DataSnapshot:
        """Reject snapshots created before their declared point-in-time cutoff."""
        if self.as_of > self.created_at:
            raise ValueError("as_of must not be later than snapshot creation")
        return self


class DataQuery(_ContractModel):
    """Read request whose cursor is opaque and fixed-order for a pinned view.

    ``as_of`` selects the platform publication visibility cutoff. It must not be
    interpreted as source availability or disclosure time.
    """

    dataset_id: Identifier
    schema_version: Identifier | None = None
    semantic_version: Identifier | None = None
    start: TimeBound | None = None
    end: TimeBound | None = None
    filters: dict[str, JsonValue] = Field(default_factory=dict)
    publication_id: Identifier | None = None
    snapshot_id: Identifier | None = None
    as_of: datetime | None = None
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    page_size: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("start", "end")
    @classmethod
    def normalize_bounds(cls, value: TimeBound | None) -> TimeBound | None:
        """Require aware datetime bounds and normalize them to UTC."""
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("datetime bounds must include a timezone")
            return value.astimezone(timezone.utc)
        return value

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime | None) -> datetime | None:
        """Require an aware point-in-time instant and normalize it to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("filters", mode="before")
    @classmethod
    def validate_filters(cls, value: object) -> object:
        """Require bounded JSON filter data with finite numbers and safe keys."""
        _validate_json(value, path="filters", depth=0, budget=[0])
        if not isinstance(value, dict):
            raise ValueError("filters must be an object keyed by declared filter fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("filter names must be non-empty strings")
            if isinstance(item, dict):
                raise ValueError("nested filter objects are not supported")
            if isinstance(item, list) and any(
                nested is None or isinstance(nested, (dict, list)) for nested in item
            ):
                raise ValueError("filter arrays may contain only scalar JSON values")
        try:
            serialized = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("filters must contain only finite JSON values") from exc
        if len(serialized.encode("utf-8")) > 65_536:
            raise ValueError("filters exceed the 65536-byte limit")
        return value

    @model_validator(mode="after")
    def validate_query_shape(self) -> DataQuery:
        """Enforce a complete half-open range and one fixed-view selector."""
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must be supplied together")
        if self.start is not None and self.end is not None:
            if type(self.start) is not type(self.end):
                raise ValueError("range bounds must both be dates or both be datetimes")
            if self.end <= self.start:
                raise ValueError("range uses [start, end) and requires end > start")
        selector_count = sum(
            value is not None for value in (self.publication_id, self.snapshot_id, self.as_of)
        )
        if selector_count > 1:
            raise ValueError("choose at most one of publication_id, snapshot_id, and as_of")
        if self.cursor is not None and all(
            value is None for value in (self.publication_id, self.snapshot_id, self.as_of)
        ):
            # First-page reads may select the current view, but a returned
            # cursor must be replayed with the same pinned view on next pages.
            raise ValueError("cursor pagination requires publication_id, snapshot_id, or as_of")
        return self

    def validate_for(self, contract: DatasetContract) -> DataQuery:
        """Validate dataset-specific time and point-in-time request semantics."""
        if self.dataset_id != contract.dataset_id:
            raise ValueError("query dataset_id does not match dataset contract")
        if self.schema_version is not None and self.schema_version != contract.schema_version:
            raise ValueError("query schema_version does not match dataset contract")
        if self.semantic_version is not None and self.semantic_version != contract.semantic_version:
            raise ValueError("query semantic_version does not match dataset contract")
        unknown_filters = set(self.filters) - set(contract.filter_fields)
        if unknown_filters:
            raise ValueError(f"filters are not allowed for fields: {sorted(unknown_filters)}")
        if contract.temporal_semantics == "business_date":
            if self.start is None:
                raise ValueError("business_date datasets require a half-open date range")
            if (
                not isinstance(self.start, date)
                or isinstance(self.start, datetime)
                or not isinstance(self.end, date)
                or isinstance(self.end, datetime)
            ):
                raise ValueError("business_date datasets require date bounds")
        elif contract.temporal_semantics in {"event_time", "observation_time"}:
            if self.start is None:
                raise ValueError("timestamp datasets require a half-open datetime range")
            if not isinstance(self.start, datetime) or not isinstance(self.end, datetime):
                raise ValueError("timestamp datasets require timezone-aware datetime bounds")
        elif self.start is not None:
            raise ValueError("non-temporal datasets do not accept time bounds")
        if contract.pit_policy == "unsupported" and self.as_of is not None:
            raise ValueError("dataset contract does not support point-in-time queries")
        if contract.pit_policy == "required" and self.as_of is None and self.snapshot_id is None:
            raise ValueError("dataset contract requires as_of or a fixed snapshot")
        return self


class DataPageMetadata(_ContractModel):
    """Semantic, logical order, physical order, and publication metadata.

    ``ordering_fields`` references contract payload fields. ``storage_order``
    states the actual persisted comparison sequence; an encoded composite key
    is lexicographic and does not claim natural numeric ordering.
    """

    dataset_id: Identifier
    schema_version: Identifier
    semantic_version: Identifier
    contract_sha256: Sha256
    publication_id: Identifier
    snapshot_id: Identifier | None
    source: SourceIdentity
    temporal_semantics: TemporalSemantics
    field_units: dict[Identifier, Identifier]
    field_currencies: dict[Identifier, Identifier]
    field_adjustments: dict[Identifier, AdjustmentBasis]
    currency: Identifier | None
    adjustment_basis: AdjustmentBasis
    ordering_fields: tuple[Identifier, ...]
    storage_order: tuple[StorageOrderTerm, ...]
    identity_key_encoding: IdentityKeyEncoding
    timezone: Identifier
    pit_policy: PitPolicy
    as_of: datetime | None
    coverage: DataCoverage
    page_size: int = Field(ge=1, le=10_000)
    returned_count: int = Field(ge=0, le=10_000)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    next_cursor: str | None = Field(default=None, min_length=1, max_length=4096)
    has_more: bool

    @field_validator("as_of")
    @classmethod
    def normalize_metadata_as_of(cls, value: datetime | None) -> datetime | None:
        """Require aware page cutoff timestamps and normalize them to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("metadata as_of must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_pagination_metadata(self) -> DataPageMetadata:
        """Keep continuation metadata internally consistent."""
        if not self.ordering_fields or len(set(self.ordering_fields)) != len(self.ordering_fields):
            raise ValueError("ordering_fields must be non-empty and unique")
        if not self.storage_order or len({term.field for term in self.storage_order}) != len(
            self.storage_order
        ):
            raise ValueError("storage_order must contain unique fields")
        if not any(
            term.field == "business_key" and term.comparison == "binary_lexicographic"
            for term in self.storage_order
        ):
            raise ValueError("storage_order must declare the encoded business_key comparison")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more must exactly match whether next_cursor is present")
        if self.returned_count > self.page_size:
            raise ValueError("returned_count cannot exceed page_size")
        return self


class DataPage(_ContractModel):
    """One bounded page from a single publication or immutable snapshot."""

    request: DataQuery
    rows: tuple[dict[str, JsonValue], ...]
    meta: DataPageMetadata

    @field_validator("rows", mode="before")
    @classmethod
    def validate_rows(cls, value: object) -> object:
        """Reject non-JSON rows, non-finite numbers, and oversized page payloads."""
        if not isinstance(value, (list, tuple)):
            raise ValueError("rows must be an array of objects")
        _validate_json(value, path="rows", depth=0, budget=[0])
        try:
            serialized = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("rows must contain only finite JSON values") from exc
        if len(serialized.encode("utf-8")) > 8 * 1024 * 1024:
            raise ValueError("page rows exceed the 8 MiB payload limit")
        if any(not isinstance(row, dict) for row in value):
            raise ValueError("every row must be an object")
        return value

    @model_validator(mode="after")
    def validate_page_matches_request(self) -> DataPage:
        """Bind page metadata to its request and verify the actual row count."""
        if self.meta.returned_count != len(self.rows):
            raise ValueError("returned_count must equal the number of rows")
        if self.request.dataset_id != self.meta.dataset_id:
            raise ValueError("page dataset_id must match the request")
        if (
            self.request.schema_version is not None
            and self.request.schema_version != self.meta.schema_version
        ):
            raise ValueError("page schema_version must match the pinned request")
        if (
            self.request.semantic_version is not None
            and self.request.semantic_version != self.meta.semantic_version
        ):
            raise ValueError("page semantic_version must match the pinned request")
        if self.request.cursor != self.meta.cursor:
            raise ValueError("page cursor must echo the request cursor")
        if self.request.page_size != self.meta.page_size:
            raise ValueError("page_size must echo the request")
        if (
            self.request.publication_id is not None
            and self.meta.publication_id != self.request.publication_id
        ):
            raise ValueError("page publication_id must match the pinned request")
        if (
            self.request.snapshot_id is not None
            and self.meta.snapshot_id != self.request.snapshot_id
        ):
            raise ValueError("page snapshot_id must match the pinned request")
        if self.request.as_of is not None and self.meta.as_of != self.request.as_of:
            raise ValueError("page as_of must match the requested point in time")
        if self.request.cursor is not None and all(
            value is None
            for value in (self.request.publication_id, self.request.snapshot_id, self.request.as_of)
        ):
            raise ValueError("a continuation page must remain pinned to the same view")
        return self

    def validate_for(self, contract: DatasetContract) -> DataPage:
        """Validate the query and response semantics against a dataset contract."""
        self.request.validate_for(contract)
        if self.meta.schema_version != contract.schema_version:
            raise ValueError("page schema_version does not match dataset contract")
        if self.meta.semantic_version != contract.semantic_version:
            raise ValueError("page semantic_version does not match dataset contract")
        if self.meta.contract_sha256 != canonical_sha256(contract):
            raise ValueError("page contract_sha256 does not match dataset contract")
        if self.meta.source != contract.source:
            raise ValueError("page source does not match dataset contract")
        if self.meta.temporal_semantics != contract.temporal_semantics:
            raise ValueError("page temporal semantics do not match dataset contract")
        if (
            self.meta.field_units != contract.field_units
            or self.meta.field_currencies != contract.field_currencies
            or self.meta.currency != contract.currency
        ):
            raise ValueError("page field units/currencies do not match dataset contract")
        if (
            self.meta.adjustment_basis != contract.adjustment_basis
            or self.meta.field_adjustments != contract.field_adjustments
        ):
            raise ValueError("page adjustment basis does not match dataset contract")
        if self.meta.timezone != contract.timezone or self.meta.pit_policy != contract.pit_policy:
            raise ValueError("page time zone/PIT policy do not match dataset contract")
        if self.meta.ordering_fields != contract.ordering:
            raise ValueError("page ordering_fields do not match dataset contract")
        if contract.temporal_semantics == "business_date":
            expected_storage_order = (
                StorageOrderTerm(
                    field="business_date",
                    direction="asc",
                    nulls="last",
                    comparison="native",
                ),
                StorageOrderTerm(
                    field="business_key",
                    direction="asc",
                    comparison="binary_lexicographic",
                ),
            )
            if self.meta.storage_order != expected_storage_order:
                raise ValueError("business-date storage_order does not match persisted sort")
        field_by_name = {field.name: field for field in contract.fields}
        if any(
            field_by_name[name].data_type in {"integer", "number"} for name in contract.business_key
        ) and any(
            term.field == "business_key" and term.comparison == "binary_lexicographic"
            for term in self.meta.storage_order
        ):
            raise ValueError("numeric business keys cannot use binary lexicographic storage order")
        if contract.record_kind == "snapshot" and self.meta.coverage.scope not in {
            "archived_snapshot_observations",
            "current_snapshot",
            "unknown",
        }:
            raise ValueError("snapshot datasets cannot claim business-history coverage")
        for row_number, row in enumerate(self.rows):
            unknown = set(row) - set(field_by_name)
            missing = {
                name
                for name, field in field_by_name.items()
                if not field.nullable and name not in row
            }
            if unknown or missing:
                raise ValueError(
                    f"row {row_number} fields mismatch contract; "
                    f"unknown={sorted(unknown)}, missing={sorted(missing)}"
                )
            for name, value in row.items():
                if value is None:
                    if not field_by_name[name].nullable:
                        raise ValueError(f"row {row_number} field {name!r} must not be null")
                    continue
                if not _field_value_matches(field_by_name[name].data_type, value):
                    raise ValueError(
                        f"row {row_number} field {name!r} does not match "
                        f"data_type={field_by_name[name].data_type!r}"
                    )
        return self


def content_sha256(payload: object) -> str:
    """Return SHA-256 for deterministic finite JSON using UTF-8 and sorted keys."""
    _validate_json(payload, path="$", depth=0, budget=[0])
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain only finite JSON values") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_json(value: object, *, path: str, depth: int, budget: list[int]) -> None:
    """Validate bounded JSON-native values before hashing or retaining them."""
    budget[0] += 1
    if budget[0] > 100_000:
        raise ValueError(f"{path} exceeds the maximum JSON node count")
    if depth > 32:
        raise ValueError(f"{path} exceeds the maximum JSON nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json(item, path=f"{path}[{index}]", depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            _validate_json(key, path=f"{path}.<key>", depth=depth + 1, budget=budget)
            _validate_json(item, path=f"{path}.{key}", depth=depth + 1, budget=budget)
        return
    raise ValueError(f"{path} contains a non-JSON value")


def _field_value_matches(data_type: str, value: JsonValue) -> bool:
    """Return whether one JSON value has the declared field shape."""
    if data_type == "string":
        return isinstance(value, str)
    if data_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if data_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "object":
        return isinstance(value, dict)
    if data_type == "array":
        return isinstance(value, list)
    if data_type == "date":
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    if data_type == "datetime":
        if not isinstance(value, str):
            return False
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return False
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    return False


__all__ = [
    "AdjustmentBasis",
    "AssetClass",
    "CoverageScope",
    "CoverageStatus",
    "DataCoverage",
    "DataPage",
    "DataPageMetadata",
    "DataQuery",
    "DataSnapshot",
    "DatasetContract",
    "FieldContract",
    "Frequency",
    "IdentityKeyEncoding",
    "PitPolicy",
    "Publication",
    "RecordKind",
    "SourceIdentity",
    "StorageComparison",
    "StorageOrderTerm",
    "TemporalSemantics",
    "TimeBound",
    "TimeRange",
    "content_sha256",
    "canonical_sha256",
]
