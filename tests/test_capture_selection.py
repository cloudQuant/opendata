"""Behavior tests for the reserved instrument/capture selector profile."""

from __future__ import annotations

import json
from typing import Literal

import pytest
from pydantic import ValidationError

from opendata.data.capture_selection import (
    INSTRUMENT_CAPTURE_FILTER_FIELDS,
    InstrumentCaptureSelection,
    InstrumentCaptureSelectionError,
    parse_instrument_capture_selection,
    selection_profile,
    validate_selector_pair,
)
from opendata.data.platform_contracts import (
    DatasetContract,
    FieldContract,
    SourceIdentity,
    canonical_sha256,
)
from opendata.pipeline.contract_publication import ContractPublicationError, _validate_record
from opendata.pipeline.run_service import NormalizedRecord

CAPTURE_ID = "a" * 64
INSTRUMENT_ID = "SSE:600000"
RecordKind = Literal["time_series", "snapshot", "event", "table"]
TemporalSemantics = Literal["business_date", "event_time", "observation_time", "none"]
PitPolicy = Literal["supported", "required", "unsupported"]


def _contract(
    *,
    filter_fields: tuple[str, ...] = INSTRUMENT_CAPTURE_FILTER_FIELDS,
    record_kind: RecordKind = "table",
    temporal_semantics: TemporalSemantics = "none",
    time_field: str | None = None,
    pit_policy: PitPolicy = "unsupported",
    capture_nullable: bool = False,
) -> DatasetContract:
    """Build a small valid contract for selector-profile boundary tests."""
    return DatasetContract(
        dataset_id="sina_fzb_observations",
        schema_version="source-native-report-v1",
        semantic_version="sina-fzb-source-native-v1",
        asset_class="financial",
        frequency="irregular",
        record_kind=record_kind,
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(
                name="capture_id",
                data_type="string",
                nullable=capture_nullable,
            ),
            FieldContract(name="statement_code", data_type="string"),
            FieldContract(name="report_period", data_type="date"),
        ),
        business_key=("instrument_id", "capture_id", "statement_code", "report_period"),
        ordering=("instrument_id", "capture_id", "statement_code", "report_period"),
        filter_fields=filter_fields,
        time_field=time_field,
        temporal_semantics=temporal_semantics,
        source=SourceIdentity(source_id="sina-fzb", source="sina"),
        adjustment_basis="not_applicable",
        timezone="UTC",
        pit_policy=pit_policy,
    )


def test_exact_reserved_profile_does_not_mutate_or_rewrite_contract() -> None:
    contract = _contract()
    before_document = contract.model_dump(mode="json")
    before_hash = canonical_sha256(contract)

    assert selection_profile(contract) == "instrument_capture_v1"

    assert contract.model_dump(mode="json") == before_document
    assert canonical_sha256(contract) == before_hash


@pytest.mark.parametrize(
    "filter_fields",
    [(), ("instrument_id",), ("capture_id",), ("capture_id", "instrument_id")],
)
def test_nonreserved_filter_tuple_keeps_dataset_profile(
    filter_fields: tuple[str, ...],
) -> None:
    contract = _contract(filter_fields=filter_fields)

    assert selection_profile(contract) == "dataset"
    assert (
        parse_instrument_capture_selection(
            contract,
            instrument_id=None,
            capture_id=None,
            required=True,
        )
        is None
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"record_kind": "event"},
        {"temporal_semantics": "observation_time", "time_field": "capture_id"},
        {"pit_policy": "supported"},
        {"business_key": ("instrument_id", "statement_code", "report_period")},
        {
            "fields": (
                _contract().fields[0],
                FieldContract(name="capture_id", data_type="integer"),
                *_contract().fields[2:],
            )
        },
    ],
)
def test_model_copy_cannot_bypass_reserved_contract_shape(updates: dict[str, object]) -> None:
    malformed = _contract().model_copy(update=updates)

    with pytest.raises(InstrumentCaptureSelectionError) as raised:
        selection_profile(malformed)

    assert str(raised.value) in {
        "dataset contract is invalid",
        "reserved selection contract is invalid",
    }


def test_nested_mutation_that_invalidates_contract_fails_closed() -> None:
    contract = _contract()
    contract.field_units["undeclared-field"] = "share"

    with pytest.raises(InstrumentCaptureSelectionError) as raised:
        selection_profile(contract)

    assert str(raised.value) == "dataset contract is invalid"


@pytest.mark.parametrize("field_name", ["filter_fields", "fields", "business_key", "ordering"])
def test_model_copy_list_bypasses_are_rejected_before_profile_classification(
    field_name: str,
) -> None:
    contract = _contract()
    malformed = contract.model_copy(
        update={field_name: list(getattr(contract, field_name))},
    )

    with pytest.raises(InstrumentCaptureSelectionError) as raised:
        selection_profile(malformed)

    assert str(raised.value) == "dataset contract is invalid"


def test_model_copy_with_invalid_nested_field_model_fails_closed() -> None:
    contract = _contract()
    invalid_capture_field = contract.fields[1].model_copy(update={"nullable": "invalid"})
    malformed = contract.model_copy(
        update={
            "fields": (
                contract.fields[0],
                invalid_capture_field,
                *contract.fields[2:],
            )
        }
    )

    with pytest.raises(InstrumentCaptureSelectionError) as raised:
        selection_profile(malformed)

    assert str(raised.value) == "dataset contract is invalid"


def test_noncontract_and_contract_subclass_are_rejected() -> None:
    class DerivedContract(DatasetContract):
        pass

    contract = _contract()
    derived = DerivedContract.model_validate(contract.model_dump(mode="python"))

    for candidate in (object(), derived):
        with pytest.raises(InstrumentCaptureSelectionError) as raised:
            selection_profile(candidate)  # type: ignore[arg-type]  # Runtime type guard is under test.
        assert str(raised.value) == "dataset contract is invalid"


def test_selector_pair_accepts_only_canonical_exact_strings() -> None:
    selection = validate_selector_pair(
        instrument_id=INSTRUMENT_ID,
        capture_id=CAPTURE_ID,
    )

    assert selection == InstrumentCaptureSelection(INSTRUMENT_ID, CAPTURE_ID)
    assert selection is not None
    assert selection.instrument_id == INSTRUMENT_ID
    assert selection.capture_id == CAPTURE_ID
    assert validate_selector_pair(instrument_id=None, capture_id=None) is None


@pytest.mark.parametrize(
    ("instrument_id", "capture_id"),
    [
        (None, CAPTURE_ID),
        (INSTRUMENT_ID, None),
        ("", CAPTURE_ID),
        ("含有中文", CAPTURE_ID),
        ("A" * 129, CAPTURE_ID),
        ("SSE:600000\n", CAPTURE_ID),
        (INSTRUMENT_ID, "A" * 64),
        (INSTRUMENT_ID, "a" * 63),
        (INSTRUMENT_ID, "a" * 65),
        (INSTRUMENT_ID, 7),
    ],
)
def test_selector_pair_rejects_partial_or_noncanonical_values_without_echo(
    instrument_id: object | None,
    capture_id: object | None,
) -> None:
    with pytest.raises(InstrumentCaptureSelectionError) as raised:
        validate_selector_pair(instrument_id=instrument_id, capture_id=capture_id)

    assert str(raised.value) in {
        "instrument_id and capture_id are required together",
        "instrument_id is invalid",
        "capture_id is invalid",
    }
    assert "含有中文" not in str(raised.value)


def test_selector_pair_rejects_str_subclasses_and_boolean_values() -> None:
    class StringSubclass(str):
        pass

    for instrument_id, capture_id in (
        (StringSubclass(INSTRUMENT_ID), CAPTURE_ID),
        (INSTRUMENT_ID, StringSubclass(CAPTURE_ID)),
        (True, CAPTURE_ID),
        (INSTRUMENT_ID, False),
    ):
        with pytest.raises(InstrumentCaptureSelectionError):
            validate_selector_pair(
                instrument_id=instrument_id,
                capture_id=capture_id,
            )


def test_parse_requires_pair_only_for_reserved_profile_and_rejects_dataset_pair() -> None:
    reserved = _contract()
    dataset = _contract(filter_fields=("instrument_id",))

    assert (
        parse_instrument_capture_selection(
            reserved,
            instrument_id=None,
            capture_id=None,
            required=False,
        )
        is None
    )
    with pytest.raises(InstrumentCaptureSelectionError):
        parse_instrument_capture_selection(
            reserved,
            instrument_id=None,
            capture_id=None,
            required=True,
        )
    with pytest.raises(InstrumentCaptureSelectionError):
        parse_instrument_capture_selection(
            dataset,
            instrument_id=INSTRUMENT_ID,
            capture_id=CAPTURE_ID,
            required=False,
        )
    with pytest.raises(InstrumentCaptureSelectionError):
        parse_instrument_capture_selection(
            reserved,
            instrument_id=INSTRUMENT_ID,
            capture_id=CAPTURE_ID,
            required=1,  # type: ignore[arg-type]
        )


def test_contract_constructor_rejects_nullable_selector_business_key() -> None:
    with pytest.raises(ValidationError):
        _contract(capture_nullable=True)


def _normalized_record(payload: dict[str, str]) -> NormalizedRecord:
    """Create a record with the contract's canonical four-part key."""
    key = json.dumps(
        [
            payload["instrument_id"],
            payload["capture_id"],
            payload["statement_code"],
            payload["report_period"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return NormalizedRecord(business_key=key, payload=payload)


def _payload(*, capture_id: str = CAPTURE_ID) -> dict[str, str]:
    """Return a complete non-temporal source-native report payload."""
    return {
        "instrument_id": INSTRUMENT_ID,
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": "2026-06-30",
    }


def test_reserved_writer_populates_explicit_selection_metadata() -> None:
    record = _validate_record(_normalized_record(_payload()), _contract())

    assert record.instrument_id == INSTRUMENT_ID
    assert record.capture_id == CAPTURE_ID


def test_legacy_writer_keeps_selector_metadata_null_for_similar_payload_fields() -> None:
    contract = _contract(filter_fields=("instrument_id",))

    record = _validate_record(_normalized_record(_payload()), contract)

    assert record.instrument_id is None
    assert record.capture_id is None


def test_reserved_writer_rejects_noncanonical_capture_id_without_echo() -> None:
    invalid_capture_id = "A" * 64

    with pytest.raises(ContractPublicationError) as raised:
        _validate_record(_normalized_record(_payload(capture_id=invalid_capture_id)), _contract())

    assert str(raised.value) == "record selector values are invalid"
    assert invalid_capture_id not in str(raised.value)


def test_writer_fails_closed_on_malformed_reserved_contract() -> None:
    malformed = _contract().model_copy(update={"filter_fields": ["instrument_id", "capture_id"]})

    with pytest.raises(ContractPublicationError) as raised:
        _validate_record(_normalized_record(_payload()), malformed)

    assert str(raised.value) == "dataset selection profile is invalid"
