"""Behavior tests for versioned REST data contracts and financial semantics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import ValidationError

from opendata.data.platform_contracts import (
    DataCoverage,
    DataPage,
    DataPageMetadata,
    DataQuery,
    DatasetContract,
    DataSnapshot,
    FieldContract,
    Publication,
    SourceIdentity,
    TimeRange,
    canonical_sha256,
    content_sha256,
)

UTC = timezone.utc
EQUITY_SOURCE = SourceIdentity(source_id="cn_equity_daily", source="eastmoney")
MACRO_SOURCE = SourceIdentity(source_id="fx_spot_observation", source="ecb_reference")


def equity_contract() -> DatasetContract:
    """Return a realistic Shanghai-listed daily-bar contract."""
    return DatasetContract(
        dataset_id="cn_equity_daily",
        schema_version="2.1.0",
        semantic_version="equity-bars-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="integer"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        filter_fields=("instrument_id",),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=EQUITY_SOURCE,
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
        },
        field_currencies={"open": "CNY", "high": "CNY", "low": "CNY", "close": "CNY"},
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="supported",
    )


def fx_snapshot_contract() -> DatasetContract:
    """Return a realistic provider snapshot contract with archived observations."""
    return DatasetContract(
        dataset_id="fx_spot_snapshot",
        schema_version="1.0.0",
        semantic_version="fx-observations-v1",
        asset_class="fx",
        frequency="snapshot",
        record_kind="snapshot",
        fields=(
            FieldContract(name="pair", data_type="string"),
            FieldContract(name="observation_time", data_type="datetime"),
            FieldContract(name="source_available_at", data_type="datetime"),
            FieldContract(name="rate", data_type="number"),
        ),
        business_key=("pair", "observation_time"),
        ordering=("observation_time", "pair"),
        filter_fields=("pair",),
        time_field="observation_time",
        temporal_semantics="observation_time",
        source_available_at_field="source_available_at",
        source=MACRO_SOURCE,
        field_units={"rate": "USD/EUR"},
        field_currencies={"rate": "USD"},
        currency=None,
        adjustment_basis="not_applicable",
        field_adjustments={"rate": "not_applicable"},
        calendar_id=None,
        timezone="UTC",
        pit_policy="required",
    )


def equity_page(request: DataQuery | None = None) -> DataPage:
    """Build a representative, already published daily-bar response page."""
    start = date(2026, 1, 5)
    end = date(2026, 1, 6)
    query = request or DataQuery(
        dataset_id="cn_equity_daily",
        start=start,
        end=end,
        filters={"instrument_id": "SSE:600000"},
        publication_id="cn-equity-pub-42",
        page_size=1000,
    )
    coverage = DataCoverage(
        status="complete",
        scope="business_history",
        requested=TimeRange(start=start, end=end),
        observed=TimeRange(start=start, end=end),
        evidence_refs=("calendar:SSE:v2026.01", "inception:600000:reviewed"),
    )
    return DataPage(
        request=query,
        rows=(
            {
                "instrument_id": "SSE:600000",
                "trade_date": "2026-01-05",
                "open": 10.21,
                "high": 10.38,
                "low": 10.16,
                "close": 10.34,
                "volume": 1_238_500,
            },
        ),
        meta=DataPageMetadata(
            dataset_id="cn_equity_daily",
            schema_version="2.1.0",
            semantic_version="equity-bars-v1",
            contract_sha256=canonical_sha256(equity_contract()),
            publication_id="cn-equity-pub-42",
            snapshot_id=None,
            source=EQUITY_SOURCE,
            temporal_semantics="business_date",
            field_units={
                "open": "CNY/share",
                "high": "CNY/share",
                "low": "CNY/share",
                "close": "CNY/share",
                "volume": "share",
            },
            field_currencies={
                "open": "CNY",
                "high": "CNY",
                "low": "CNY",
                "close": "CNY",
            },
            field_adjustments={
                "open": "raw",
                "high": "raw",
                "low": "raw",
                "close": "raw",
                "volume": "not_applicable",
            },
            currency="CNY",
            adjustment_basis="raw",
            timezone="Asia/Shanghai",
            pit_policy="supported",
            as_of=None,
            coverage=coverage,
            page_size=1000,
            returned_count=1,
            cursor=None,
            next_cursor=None,
            has_more=False,
        ),
    )


def fx_snapshot_page(
    *,
    coverage_scope: Literal[
        "archived_snapshot_observations", "business_history"
    ] = "archived_snapshot_observations",
) -> DataPage:
    """Build an archived provider snapshot with separate source/platform times."""
    query_start = datetime(2026, 2, 1, 10, tzinfo=UTC)
    query_end = datetime(2026, 2, 1, 11, tzinfo=UTC)
    cutoff = datetime(2026, 2, 1, 12, tzinfo=UTC)
    return DataPage(
        request=DataQuery(
            dataset_id="fx_spot_snapshot",
            start=query_start,
            end=query_end,
            filters={"pair": "EUR/USD"},
            as_of=cutoff,
        ),
        rows=(
            {
                "pair": "EUR/USD",
                "observation_time": "2026-02-01T10:30:00Z",
                "source_available_at": "2026-02-01T10:35:00+00:00",
                "rate": 1.1824,
            },
        ),
        meta=DataPageMetadata(
            dataset_id="fx_spot_snapshot",
            schema_version="1.0.0",
            semantic_version="fx-observations-v1",
            contract_sha256=canonical_sha256(fx_snapshot_contract()),
            publication_id="fx-pub-8",
            snapshot_id=None,
            source=MACRO_SOURCE,
            temporal_semantics="observation_time",
            field_units={"rate": "USD/EUR"},
            field_currencies={"rate": "USD"},
            field_adjustments={"rate": "not_applicable"},
            currency=None,
            adjustment_basis="not_applicable",
            timezone="UTC",
            pit_policy="required",
            as_of=cutoff,
            coverage=DataCoverage(
                status="partial",
                scope=coverage_scope,
                requested=TimeRange(start=query_start, end=query_end),
                observed=TimeRange(start=query_start, end=query_start + timedelta(minutes=45)),
                missing_ranges=(
                    TimeRange(start=query_start + timedelta(minutes=45), end=query_end),
                ),
                limitation="Provider endpoint only returned archived snapshots.",
            ),
            page_size=1000,
            returned_count=1,
            cursor=None,
            next_cursor=None,
            has_more=False,
        ),
    )


def test_stock_daily_page_preserves_units_source_window_and_fixed_publication() -> None:
    """A daily page exposes per-field units and binds the exclusive end date."""
    page = equity_page()

    assert page.request.start == date(2026, 1, 5)
    assert page.request.end == date(2026, 1, 6)
    assert page.meta.publication_id == "cn-equity-pub-42"
    assert page.meta.source == EQUITY_SOURCE
    assert page.meta.field_units["volume"] == "share"
    assert page.meta.field_units["close"] == "CNY/share"
    assert page.meta.coverage.scope == "business_history"
    assert page.validate_for(equity_contract()) is page


def test_snapshot_observation_keeps_platform_cutoff_separate_from_source_time() -> None:
    """Archived FX observations include source time and a distinct platform cutoff."""
    cutoff = datetime(2026, 2, 1, 12, tzinfo=UTC)
    page = fx_snapshot_page()

    assert page.rows[0]["observation_time"] == "2026-02-01T10:30:00Z"
    assert page.rows[0]["source_available_at"] == "2026-02-01T10:35:00+00:00"
    assert page.meta.as_of == cutoff  # platform publication visibility cutoff
    assert page.meta.coverage.scope == "archived_snapshot_observations"
    assert page.validate_for(fx_snapshot_contract()) is page


def test_publication_and_snapshot_hashes_are_versioned_and_timezone_aware() -> None:
    """Publication metadata rejects naive instants and snapshot pins a publication."""
    coverage = DataCoverage(
        status="unknown",
        scope="unknown",
        limitation="No independent source retention evidence is available.",
    )
    with pytest.raises(ValidationError, match="timezone"):
        Publication(
            publication_id="pub-1",
            dataset_id="cn_equity_daily",
            source=EQUITY_SOURCE,
            schema_version="2.1.0",
            semantic_version="stock-bars-v1",
            sequence=1,
            published_at=datetime(2026, 1, 6),
            record_count=0,
            content_sha256="0" * 64,
            quality_status="unknown",
            coverage=coverage,
        )
    publication = Publication(
        publication_id="pub-1",
        dataset_id="cn_equity_daily",
        source=EQUITY_SOURCE,
        schema_version="2.1.0",
        semantic_version="stock-bars-v1",
        sequence=1,
        published_at=datetime(2026, 1, 6, 9, tzinfo=timezone(timedelta(hours=8))),
        record_count=1,
        content_sha256="0" * 64,
        quality_status="warning",
        quality_report_id="dq-report-1",
        quality_issue_count=2,
        provenance={"contract_sha256": canonical_sha256(equity_contract())},
        provenance_sha256="3" * 64,
        watermark=TimeRange(start=date(2026, 1, 5), end=date(2026, 1, 6)),
        coverage=coverage,
    )
    assert publication.published_at == datetime(2026, 1, 6, 1, tzinfo=UTC)
    assert publication.quality_issue_count == 2
    assert publication.watermark is not None

    snapshot = DataSnapshot(
        snapshot_id="snap-1",
        dataset_id="cn_equity_daily",
        publication_id="cn-equity-pub-42",
        publication_sequence=42,
        schema_version="2.1.0",
        semantic_version="equity-bars-v1",
        created_at=datetime(2026, 1, 6, 9, tzinfo=timezone(timedelta(hours=8))),
        as_of=datetime(2026, 1, 6, 8, tzinfo=timezone(timedelta(hours=8))),
        selection_sha256="1" * 64,
        content_sha256="2" * 64,
    )
    assert snapshot.created_at == datetime(2026, 1, 6, 1, tzinfo=UTC)
    assert snapshot.as_of == datetime(2026, 1, 6, tzinfo=UTC)
    assert "platform publication visibility cutoff" in DataSnapshot.__doc__
    with pytest.raises(ValidationError, match="must not be later"):
        DataSnapshot(
            snapshot_id="snap-future",
            dataset_id="cn_equity_daily",
            publication_id="cn-equity-pub-42",
            publication_sequence=42,
            schema_version="2.1.0",
            semantic_version="equity-bars-v1",
            created_at=datetime(2026, 1, 6, 8, tzinfo=UTC),
            as_of=datetime(2026, 1, 6, 9, tzinfo=UTC),
            selection_sha256="1" * 64,
            content_sha256="2" * 64,
        )


def test_contract_requires_stable_order_and_declared_filter_fields() -> None:
    """The unique business key is part of sort order and filters are allowlisted."""
    contract = equity_contract()
    with pytest.raises(ValidationError, match="stable tie-breaker"):
        DatasetContract.model_validate(
            {
                **contract.model_dump(),
                "ordering": ("trade_date",),
            }
        )
    with pytest.raises(ValueError, match="not allowed"):
        DataQuery(
            dataset_id="cn_equity_daily",
            start=date(2026, 1, 5),
            end=date(2026, 1, 6),
            filters={"close": 10.0},
        ).validate_for(contract)


def test_schema_and_semantic_versions_pin_queries_and_response_hashes() -> None:
    """Version pins and canonical contract hashes bind a read to its schema semantics."""
    contract = equity_contract()
    pinned = DataQuery(
        dataset_id=contract.dataset_id,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
    )
    assert pinned.validate_for(contract) is pinned
    for version_field in ("schema_version", "semantic_version"):
        with pytest.raises(ValueError, match=f"query {version_field} does not match"):
            DataQuery.model_validate(
                {**pinned.model_dump(), version_field: "unregistered-version"}
            ).validate_for(contract)

    changed_metadata = equity_page().meta.model_copy(update={"contract_sha256": "f" * 64})
    with pytest.raises(ValueError, match="contract_sha256 does not match"):
        equity_page().model_copy(update={"meta": changed_metadata}).validate_for(contract)
    assert canonical_sha256(contract) == canonical_sha256(
        DatasetContract.model_validate(contract.model_dump())
    )
    assert canonical_sha256(contract) != canonical_sha256(
        contract.model_copy(update={"semantic_version": "equity-bars-v2"})
    )


def test_publication_requires_contract_hash_provenance() -> None:
    """Publication receipts reject a missing, malformed, or unbounded contract digest."""
    with pytest.raises(ValidationError, match="contract_sha256"):
        Publication.model_validate(
            {
                "publication_id": "pub-1",
                "dataset_id": "cn_equity_daily",
                "source": EQUITY_SOURCE,
                "schema_version": "2.1.0",
                "semantic_version": "equity-bars-v1",
                "sequence": 1,
                "published_at": datetime(2026, 1, 6, tzinfo=UTC),
                "record_count": 0,
                "content_sha256": "0" * 64,
                "quality_status": "unknown",
                "coverage": DataCoverage(status="unknown", scope="unknown"),
                "provenance": {"contract_sha256": "not-a-hash"},
            }
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"fields": ()}, "fields must contain unique names"),
        ({"business_key": ()}, "business_key must contain unique fields"),
        ({"business_key": ("missing",)}, "business_key fields must be declared"),
        ({"ordering": ()}, "ordering must contain unique fields"),
        ({"ordering": ("missing",)}, "ordering fields must be declared"),
        (
            {
                "fields": (
                    FieldContract(name="instrument_id", data_type="string", nullable=True),
                    *equity_contract().fields[1:],
                )
            },
            "business_key fields must be non-nullable",
        ),
        ({"filter_fields": ("instrument_id", "instrument_id")}, "filter_fields must not contain"),
        ({"filter_fields": ("not_declared",)}, "filter_fields must be declared"),
        ({"time_field": "not_declared"}, "time_field must be declared"),
        (
            {
                "fields": tuple(
                    FieldContract(
                        name=field.name,
                        data_type="number" if field.name == "trade_date" else field.data_type,
                    )
                    for field in equity_contract().fields
                )
            },
            "business_date time_field must have data_type='date'",
        ),
        (
            {"source_available_at_field": "not_declared"},
            "source_available_at_field must be declared",
        ),
        (
            {"source_available_at_field": "trade_date"},
            "source_available_at_field must have data_type='datetime'",
        ),
        ({"field_units": {"missing": "USD"}}, "field_units keys must be declared"),
        ({"temporal_semantics": "none"}, "temporal_semantics='none' requires time_field=null"),
        ({"time_field": None}, "temporal datasets require an explicit time_field"),
        (
            {
                "fields": tuple(
                    FieldContract(
                        name=field.name,
                        data_type="date" if field.name == "trade_date" else field.data_type,
                    )
                    for field in equity_contract().fields
                ),
                "temporal_semantics": "event_time",
            },
            "timestamp semantics require a datetime time_field",
        ),
        ({"frequency": "1m"}, "intraday frequencies require timestamp semantics"),
        ({"timezone": "Mars/Olympus"}, "timezone must be a valid IANA timezone"),
    ],
)
def test_dataset_contract_rejects_incomplete_semantics(
    changes: dict[str, object], message: str
) -> None:
    """Dataset registration fails closed on unknown or inconsistent schema declarations."""
    with pytest.raises(ValidationError, match=message):
        DatasetContract.model_validate({**equity_contract().model_dump(), **changes})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"scope": "business_history", "status": "complete", "evidence_refs": ()},
            "complete coverage requires evidence_refs",
        ),
        ({"status": "partial"}, "partial coverage requires at least one missing range"),
        (
            {"status": "source_limited", "limitation": None},
            "source_limited coverage requires a limitation",
        ),
        (
            {
                "requested": TimeRange(start=date(2026, 1, 1), end=date(2026, 2, 1)),
                "observed": TimeRange(
                    start=datetime(2026, 1, 1, tzinfo=UTC),
                    end=datetime(2026, 2, 1, tzinfo=UTC),
                ),
            },
            "requested and observed ranges must use the same time type",
        ),
        (
            {
                "requested": TimeRange(start=date(2026, 1, 1), end=date(2026, 2, 1)),
                "observed": TimeRange(start=date(2025, 12, 1), end=date(2026, 1, 5)),
            },
            "observed range must be contained in requested range",
        ),
        (
            {
                "status": "partial",
                "requested": TimeRange(start=date(2026, 1, 1), end=date(2026, 2, 1)),
                "missing_ranges": (
                    TimeRange(
                        start=datetime(2026, 1, 5, tzinfo=UTC), end=datetime(2026, 1, 6, tzinfo=UTC)
                    ),
                ),
            },
            "missing ranges must use the requested range time type",
        ),
        (
            {
                "status": "partial",
                "requested": TimeRange(start=date(2026, 1, 1), end=date(2026, 2, 1)),
                "missing_ranges": (TimeRange(start=date(2025, 12, 1), end=date(2026, 1, 2)),),
            },
            "missing ranges must be contained in requested range",
        ),
    ],
)
def test_coverage_rejects_unsupported_completeness_claims(
    changes: dict[str, object], message: str
) -> None:
    """Coverage status, evidence, and half-open ranges must describe the same scope."""
    base: dict[str, object] = {
        "status": "unknown",
        "scope": "unknown",
        "limitation": "Coverage has not been independently verified.",
    }
    with pytest.raises(ValidationError, match=message):
        DataCoverage.model_validate({**base, **changes})


@pytest.mark.parametrize(
    ("request_fields", "message"),
    [
        ({"start": date(2026, 1, 1)}, "start and end must be supplied together"),
        (
            {"publication_id": "pub-1", "snapshot_id": "snap-1"},
            "choose at most one of publication_id",
        ),
        ({"as_of": datetime(2026, 1, 1)}, "as_of must include a timezone"),
        ({"filters": []}, "filters must be an object"),
        ({"filters": {"": "bad-key"}}, "filter names must be non-empty strings"),
        (
            {"filters": {"instrument_id": ["x", {"nested": "bad"}]}},
            "filter arrays may contain only scalar",
        ),
        ({"filters": {"instrument_id": "x" * 70_000}}, "filters exceed the 65536-byte limit"),
        ({"filters": {"instrument_id": 10**5000}}, "filters must contain only finite JSON values"),
    ],
)
def test_query_shape_rejects_partial_ambiguous_or_unbounded_values(
    request_fields: dict[str, object], message: str
) -> None:
    """A query has one pinned view, paired bounds, and finite scalar filters."""
    with pytest.raises(ValidationError, match=message):
        DataQuery.model_validate({"dataset_id": "cn_equity_daily", **request_fields})


def test_query_validation_uses_dataset_time_and_pit_policy() -> None:
    """Date, timestamp, nontemporal, filter, and PIT rules follow the contract."""
    contract = equity_contract()
    with pytest.raises(ValueError, match="dataset_id does not match"):
        DataQuery(
            dataset_id="another_dataset", start=date(2026, 1, 1), end=date(2026, 1, 2)
        ).validate_for(contract)
    with pytest.raises(ValueError, match="require a half-open date range"):
        DataQuery(dataset_id="cn_equity_daily").validate_for(contract)
    with pytest.raises(ValueError, match="require date bounds"):
        DataQuery(
            dataset_id="cn_equity_daily",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        ).validate_for(contract)

    with pytest.raises(ValueError, match="require a half-open datetime range"):
        DataQuery(dataset_id="fx_spot_snapshot").validate_for(fx_snapshot_contract())
    with pytest.raises(ValueError, match="require timezone-aware datetime bounds"):
        DataQuery(
            dataset_id="fx_spot_snapshot",
            start=date(2026, 1, 1),
            end=date(2026, 1, 2),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ).validate_for(fx_snapshot_contract())

    with pytest.raises(ValueError, match="does not support point-in-time"):
        DataQuery(
            dataset_id="fx_spot_snapshot",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        ).validate_for(fx_snapshot_contract().model_copy(update={"pit_policy": "unsupported"}))

    table_contract = DatasetContract.model_validate(
        {
            **contract.model_dump(),
            "record_kind": "table",
            "temporal_semantics": "none",
            "time_field": None,
        }
    )
    with pytest.raises(ValueError, match="non-temporal datasets do not accept time bounds"):
        DataQuery(
            dataset_id="cn_equity_daily", start=date(2026, 1, 1), end=date(2026, 1, 2)
        ).validate_for(table_contract)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"temporal_semantics": "event_time"}, "snapshot datasets describe observation_time"),
        ({"frequency": "1d"}, "snapshot record_kind requires frequency='snapshot'"),
    ],
)
def test_snapshot_contract_rejects_time_series_shortcuts(
    changes: dict[str, object], message: str
) -> None:
    """Snapshot datasets keep observation-time semantics and snapshot cadence explicit."""
    with pytest.raises(ValidationError, match=message):
        DatasetContract.model_validate({**fx_snapshot_contract().model_dump(), **changes})


def test_timestamp_contract_rejects_wrong_field_type_and_wrong_record_kind() -> None:
    """Event timestamps and record kinds must agree with the declared schema."""
    contract_data = fx_snapshot_contract().model_dump()
    contract_data.update(
        {
            "dataset_id": "fx_event_feed",
            "record_kind": "time_series",
            "frequency": "irregular",
            "temporal_semantics": "event_time",
            "pit_policy": "supported",
        }
    )
    invalid_time = {
        **contract_data,
        "fields": tuple(
            FieldContract(
                name=field.name,
                data_type="date" if field.name == "observation_time" else field.data_type,
            )
            for field in fx_snapshot_contract().fields
        ),
    }
    with pytest.raises(ValidationError, match="timestamp semantics require a datetime"):
        DatasetContract.model_validate(invalid_time)

    invalid_kind = {
        **contract_data,
        "temporal_semantics": "none",
        "time_field": None,
    }
    with pytest.raises(ValidationError, match="time_series requires business_date or event_time"):
        DatasetContract.model_validate(invalid_kind)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ({"instrument_id": "SSE:600000"}, "missing=.*trade_date"),
        ({**equity_page().rows[0], "close": None}, "field 'close' must not be null"),
        ({**equity_page().rows[0], "close": "10.34"}, "field 'close' does not match"),
        ({**equity_page().rows[0], "volume": True}, "field 'volume' does not match"),
        (
            {**equity_page().rows[0], "trade_date": "2026-02-30"},
            "field 'trade_date' does not match",
        ),
    ],
)
def test_rows_must_match_required_schema_and_native_json_types(
    row: dict[str, object], message: str
) -> None:
    """Rows reject missing keys, invalid nulls, and values with wrong native types."""
    page = equity_page().model_copy(update={"rows": (row,)})
    with pytest.raises(ValueError, match=message):
        page.validate_for(equity_contract())


def test_timestamp_row_requires_an_aware_rfc3339_value() -> None:
    """A snapshot observation timestamp is distinct from a naive local wall time."""
    page = fx_snapshot_page()
    row = dict(page.rows[0], observation_time="2026-02-01T10:30:00")
    with pytest.raises(ValueError, match="field 'observation_time' does not match"):
        page.model_copy(update={"rows": (row,)}).validate_for(fx_snapshot_contract())


def test_nontemporal_table_supports_explicit_boolean_object_and_array_fields() -> None:
    """Schema validation preserves nonnumeric record types without coercion."""
    contract = DatasetContract(
        dataset_id="corporate_event_registry",
        schema_version="1.2.0",
        semantic_version="corp-events-v1",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=(
            FieldContract(name="event_id", data_type="string"),
            FieldContract(name="is_material", data_type="boolean"),
            FieldContract(name="attributes", data_type="object"),
            FieldContract(name="tags", data_type="array"),
        ),
        business_key=("event_id",),
        ordering=("event_id",),
        temporal_semantics="none",
        source=SourceIdentity(source_id="corp_events", source="exchange_notice"),
        field_units={},
        field_currencies={},
        currency=None,
        adjustment_basis="not_applicable",
        field_adjustments={},
        calendar_id=None,
        timezone="UTC",
        pit_policy="unsupported",
    )
    page = DataPage(
        request=DataQuery(dataset_id="corporate_event_registry"),
        rows=(
            {
                "event_id": "SSE:2026-0123",
                "is_material": True,
                "attributes": {"kind": "dividend", "cash_per_share": 0.25},
                "tags": ["cash", "approved"],
            },
        ),
        meta=DataPageMetadata(
            dataset_id="corporate_event_registry",
            schema_version="1.2.0",
            semantic_version="corp-events-v1",
            contract_sha256=canonical_sha256(contract),
            publication_id="events-pub-5",
            snapshot_id=None,
            source=contract.source,
            temporal_semantics="none",
            field_units={},
            field_currencies={},
            field_adjustments={},
            currency=None,
            adjustment_basis="not_applicable",
            timezone="UTC",
            pit_policy="unsupported",
            as_of=None,
            coverage=DataCoverage(status="not_applicable", scope="not_applicable"),
            page_size=1000,
            returned_count=1,
            cursor=None,
            next_cursor=None,
            has_more=False,
        ),
    )

    assert page.validate_for(contract) is page


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"schema_version": "wrong"}, "schema_version does not match"),
        (
            {"source": SourceIdentity(source_id="other", source="other")},
            "page source does not match",
        ),
        ({"temporal_semantics": "event_time"}, "page temporal semantics do not match"),
        ({"field_units": {"close": "USD/share"}}, "page field units/currencies do not match"),
        (
            {"field_adjustments": {"close": "split_adjusted"}},
            "page adjustment basis does not match",
        ),
        ({"timezone": "UTC"}, "page time zone/PIT policy do not match"),
    ],
)
def test_page_semantics_must_match_the_registered_contract(
    metadata: dict[str, object], message: str
) -> None:
    """The server cannot change source, field units, adjustment, or time semantics per page."""
    page = equity_page()
    updated_meta = page.meta.model_copy(update=metadata)
    with pytest.raises(ValueError, match=message):
        page.model_copy(update={"meta": updated_meta}).validate_for(equity_contract())


def test_page_and_pagination_metadata_reject_mismatched_echoes() -> None:
    """Every envelope count, cursor, page size, and pinned selector must echo the request."""
    base = equity_page()
    next_page_meta = base.meta.model_copy(update={"has_more": True, "next_cursor": "next"})
    with pytest.raises(ValidationError, match="number of rows"):
        DataPage.model_validate(
            {**base.model_dump(), "meta": {**next_page_meta.model_dump(), "returned_count": 2}}
        )

    with pytest.raises(ValidationError, match="has_more must exactly match"):
        DataPageMetadata.model_validate({**base.meta.model_dump(), "has_more": True})
    with pytest.raises(ValidationError, match="cannot exceed page_size"):
        DataPageMetadata.model_validate(
            {**base.meta.model_dump(), "page_size": 1, "returned_count": 2}
        )
    with pytest.raises(ValidationError, match="metadata as_of must include a timezone"):
        DataPageMetadata.model_validate({**base.meta.model_dump(), "as_of": datetime(2026, 1, 5)})

    pinned = DataQuery(
        dataset_id="cn_equity_daily",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        publication_id="cn-equity-pub-42",
        page_size=999,
    )
    mismatched_page = base.model_dump()
    mismatched_page["request"] = pinned.model_dump()
    with pytest.raises(ValidationError, match="page_size must echo"):
        DataPage.model_validate(mismatched_page)

    snapshot_request = DataQuery(
        dataset_id="cn_equity_daily",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        snapshot_id="snap-1",
    )
    mismatch_snapshot = base.model_dump()
    mismatch_snapshot["request"] = snapshot_request.model_dump()
    mismatch_snapshot["meta"]["snapshot_id"] = "snap-other"
    with pytest.raises(ValidationError, match="snapshot_id must match"):
        DataPage.model_validate(mismatch_snapshot)

    as_of = datetime(2026, 1, 6, tzinfo=UTC)
    as_of_request = DataQuery(
        dataset_id="cn_equity_daily",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        as_of=as_of,
    )
    mismatch_as_of = base.model_dump()
    mismatch_as_of["request"] = as_of_request.model_dump()
    mismatch_as_of["meta"]["as_of"] = datetime(2026, 1, 5, tzinfo=UTC)
    with pytest.raises(ValidationError, match="as_of must match"):
        DataPage.model_validate(mismatch_as_of)

    cursor_request = DataQuery(
        dataset_id="cn_equity_daily",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        publication_id="cn-equity-pub-42",
        cursor="opaque-cursor",
    )
    mismatch_cursor = base.model_dump()
    mismatch_cursor["request"] = cursor_request.model_dump()
    with pytest.raises(ValidationError, match="cursor must echo"):
        DataPage.model_validate(mismatch_cursor)

    mismatch_dataset = base.model_dump()
    mismatch_dataset["meta"]["dataset_id"] = "other_dataset"
    with pytest.raises(ValidationError, match="dataset_id must match"):
        DataPage.model_validate(mismatch_dataset)


def test_time_range_and_page_body_reject_naive_or_unbounded_payloads() -> None:
    """Shared time and response payload bounds are enforced before use."""
    with pytest.raises(ValidationError, match="datetime bounds must include a timezone"):
        TimeRange(start=datetime(2026, 1, 1), end=datetime(2026, 1, 2))
    base = equity_page().model_dump()
    with pytest.raises(ValidationError, match="rows must be an array"):
        DataPage.model_validate({**base, "rows": {"not": "an array"}})
    with pytest.raises(ValidationError, match="every row must be an object"):
        DataPage.model_validate({**base, "rows": ["not an object"]})
    oversized = dict(base["rows"][0])
    oversized["close"] = "x" * (8 * 1024 * 1024)
    with pytest.raises(ValidationError, match="8 MiB payload limit"):
        DataPage.model_validate({**base, "rows": [oversized]})
    row = dict(base["rows"][0])
    row["open"] = 10**5000
    with pytest.raises(ValidationError, match="finite JSON values"):
        DataPage.model_validate({**base, "rows": [row]})


def test_json_validator_rejects_non_json_deep_or_oversized_graphs() -> None:
    """Content hashing is bounded and accepts only canonical finite JSON values."""
    with pytest.raises(ValueError, match="non-JSON value"):
        content_sha256({"not_json": object()})
    with pytest.raises(ValueError, match="non-string object key"):
        content_sha256({1: "bad-key"})
    nested: object = "leaf"
    for _ in range(34):
        nested = [nested]
    with pytest.raises(ValueError, match="nesting depth"):
        content_sha256(nested)
    with pytest.raises(ValueError, match="node count"):
        content_sha256([None] * 100_001)
    with pytest.raises(ValueError, match="finite JSON values"):
        content_sha256({"large": 10**5000})


def test_contract_rejects_unknown_fields_and_mixed_or_invalid_ranges() -> None:
    """Unknown JSON fields, inclusive/reversed windows, and naive timestamps fail."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SourceIdentity(source_id="feed", source="vendor", secret="no")
    with pytest.raises(ValidationError, match="end > start"):
        DataQuery(dataset_id="cn_equity_daily", start=date(2026, 1, 6), end=date(2026, 1, 6))
    with pytest.raises(ValidationError, match="both be dates or both be datetimes"):
        DataQuery(
            dataset_id="cn_equity_daily",
            start=date(2026, 1, 5),
            end=datetime(2026, 1, 6, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="timezone"):
        DataQuery(
            dataset_id="fx_spot_snapshot",
            start=datetime(2026, 2, 1, 10),
            end=datetime(2026, 2, 1, 11),
        )


def test_filters_and_rows_reject_non_finite_and_nested_unbounded_values() -> None:
    """JSON payloads cannot carry NaN, arbitrary filter objects, or huge nesting."""
    with pytest.raises(ValidationError, match="non-finite"):
        DataQuery(dataset_id="cn_equity_daily", filters={"instrument_id": float("nan")})
    with pytest.raises(ValidationError, match="nested filter objects"):
        DataQuery(dataset_id="cn_equity_daily", filters={"instrument_id": {"$ne": "x"}})
    row_payload = equity_page().model_dump()
    row_payload["rows"] = ({**row_payload["rows"][0], "open": float("inf")},)
    with pytest.raises(ValidationError, match="non-finite"):
        DataPage.model_validate(row_payload)
    with pytest.raises(ValueError, match="non-finite"):
        content_sha256({"close": float("nan")})


def test_page_rejects_row_schema_drift_and_wrong_publication() -> None:
    """Response rows and semantic metadata must match the pinned contract."""
    page = equity_page()
    invalid_rows = page.model_dump()
    invalid_rows["rows"] = (dict(page.rows[0], unexpected="drift"),)
    invalid = DataPage.model_validate(invalid_rows)
    with pytest.raises(ValueError, match="unknown=.*unexpected"):
        invalid.validate_for(equity_contract())

    wrong_pin = page.model_dump()
    wrong_pin["meta"]["publication_id"] = "pub-other"
    with pytest.raises(ValidationError, match="pinned request"):
        DataPage.model_validate(wrong_pin)


def test_snapshot_coverage_cannot_be_relabelled_as_business_history() -> None:
    """A snapshot endpoint cannot claim complete current or business history."""
    with pytest.raises(ValidationError, match="historical completeness"):
        DataCoverage(
            status="complete",
            scope="current_snapshot",
            evidence_refs=("capture:2026-02-01",),
        )
    page = fx_snapshot_page(coverage_scope="business_history")
    with pytest.raises(ValueError, match="cannot claim business-history"):
        page.validate_for(fx_snapshot_contract())


def test_point_in_time_required_for_contract_and_cursor_requires_a_fixed_view() -> None:
    """Strict PIT datasets require a platform cutoff and continuation pin."""
    query = DataQuery(
        dataset_id="fx_spot_snapshot",
        start=datetime(2026, 2, 1, 10, tzinfo=UTC),
        end=datetime(2026, 2, 1, 11, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="requires as_of"):
        query.validate_for(fx_snapshot_contract())
    with pytest.raises(ValidationError, match="cursor pagination requires"):
        DataQuery(
            dataset_id="cn_equity_daily",
            start=date(2026, 1, 5),
            end=date(2026, 1, 6),
            cursor="opaque-next-page",
        )


def test_content_hash_has_stable_golden_vector_and_envelope_counts_rows() -> None:
    """Canonical JSON hashing is stable and response count cannot lie."""
    assert content_sha256({"a": 1}) == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"
    )
    payload = equity_page().model_dump()
    payload["meta"]["returned_count"] = 2
    with pytest.raises(ValidationError, match="number of rows"):
        DataPage.model_validate(payload)
