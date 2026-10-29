"""Behavioral and adversarial tests for offline temporal identity staging."""

from __future__ import annotations

import hashlib
import json
from datetime import date

import pytest

from opendata.api.temporal_identity import TemporalIdentityPin as ApiTemporalIdentityPin
from opendata.api.temporal_identity_runtime import _decode_pin_registry
from opendata.data.temporal_identity_pins import (
    MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES,
    TemporalIdentityPin,
    TemporalIdentityPinRegistry,
    decode_temporal_identity_pin_registry,
)
from opendata.data.temporal_identity_staging import (
    TemporalIdentityStagingError,
    TemporalIdentityStagingResult,
    stage_temporal_identity_pin,
)
from opendata.data.temporal_instrument_identity import (
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    encode_temporal_identity_snapshot,
)


def _snapshot(*, source_id: str = "akshare", version: str = "2026-10-28") -> bytes:
    """Build an explicit small snapshot with fixed source and version values."""
    registry = TemporalIdentityRegistry(
        reference_version=version,
        evidence_ref="caller-supplied-evidence-reference",
        assignments=(
            TemporalIdentityAssignment(
                assignment_id="assign-1",
                instrument_id="instrument-1",
                exchange="XSHG",
                symbol="600000",
                valid_from=date(2024, 1, 2),
                valid_to=None,
                source_id=source_id,
                source_version="source-version-1",
                evidence_ref="caller-supplied-row-reference",
                source_sha256="a" * 64,
            ),
        ),
    )
    return encode_temporal_identity_snapshot(registry)


def _pin(
    dataset_id: str,
    *,
    source_id: str = "akshare",
    authority_id: str = "caller-authority",
    registry_id: str = "caller-registry",
    reference_version: str = "2026-10-28",
    snapshot_sha256: str = "b" * 64,
) -> dict[str, str]:
    """Return one strict runtime pin mapping for existing-document fixtures."""
    return {
        "dataset_id": dataset_id,
        "source_id": source_id,
        "authority_id": authority_id,
        "registry_id": registry_id,
        "reference_version": reference_version,
        "snapshot_sha256": snapshot_sha256,
    }


def _pin_document(*pins: dict[str, str]) -> bytes:
    """Build a compact runtime-compatible existing pin document."""
    return json.dumps(
        {"schema_version": 1, "pins": list(pins)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _stage(
    snapshot_bytes: bytes | bytearray | object,
    existing: object | None = None,
    **overrides: object,
) -> TemporalIdentityStagingResult:
    """Invoke staging with one fixed and inspectable declaration set."""
    arguments: dict[str, object] = {
        "dataset_id": "stock_daily",
        "source_id": "akshare",
        "authority_id": "caller-authority",
        "registry_id": "caller-registry",
        "reference_version": "2026-10-28",
    }
    arguments.update(overrides)
    return stage_temporal_identity_pin(snapshot_bytes, existing, **arguments)  # type: ignore[arg-type]


def _decode_shared_and_runtime(raw: bytes, digest: str) -> TemporalIdentityPinRegistry:
    """Check the public parser and compatibility runtime wrapper agree."""
    shared = decode_temporal_identity_pin_registry(raw, digest)
    runtime = _decode_pin_registry(raw, digest)
    assert runtime == shared
    return shared


def test_api_keeps_reexporting_the_shared_pin_model_object() -> None:
    """Legacy API imports resolve to the exact low-level model class."""
    assert ApiTemporalIdentityPin is TemporalIdentityPin


def test_stage_emits_runtime_compatible_canonical_bytes_and_complete_digests() -> None:
    """Generated pins bind exact snapshot bytes and pass the production parser."""
    snapshot_bytes = _snapshot()

    result = _stage(snapshot_bytes)

    assert result.snapshot_bytes == snapshot_bytes
    assert type(result.snapshot_bytes) is bytes
    assert result.snapshot_sha256 == hashlib.sha256(snapshot_bytes).hexdigest()
    assert result.pin_file_sha256 == hashlib.sha256(result.pin_document_bytes).hexdigest()
    assert result.assignment_count == 1
    assert result.existing_pin_count == 0
    assert result.pin_count == 1
    assert result.verification_status == "caller_declared_unverified"
    parsed = _decode_shared_and_runtime(result.pin_document_bytes, result.pin_file_sha256)
    pin = parsed.get("stock_daily")
    assert pin is not None
    assert result.candidate_pin == pin
    assert pin.snapshot_sha256 == result.snapshot_sha256
    assert pin.source_id == "akshare"
    assert result.pin_document_bytes == json.dumps(
        {
            "schema_version": 1,
            "pins": [
                {
                    "dataset_id": "stock_daily",
                    "source_id": "akshare",
                    "authority_id": "caller-authority",
                    "registry_id": "caller-registry",
                    "reference_version": "2026-10-28",
                    "snapshot_sha256": result.snapshot_sha256,
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_stage_result_repr_does_not_include_snapshot_or_pin_document_bytes() -> None:
    """Large caller-provided snapshot and generated pin bytes stay out of logs."""
    result = _stage(_snapshot())

    rendered = repr(result)

    assert repr(result.snapshot_bytes) not in rendered
    assert repr(result.pin_document_bytes) not in rendered
    assert result.candidate_pin is not None
    assert result.candidate_pin.dataset_id == "stock_daily"


def test_stage_preserves_all_other_dataset_pins_and_sorts_deterministically() -> None:
    """Adding a dataset retains every existing pin, regardless of input order."""
    existing = _pin_document(_pin("z_last"), _pin("a_first"))

    result = _stage(_snapshot(), existing)

    decoded = _decode_shared_and_runtime(result.pin_document_bytes, result.pin_file_sha256)
    assert {key: decoded.get(key) is not None for key in ("a_first", "stock_daily", "z_last")} == {
        "a_first": True,
        "stock_daily": True,
        "z_last": True,
    }
    root = json.loads(result.pin_document_bytes)
    assert [pin["dataset_id"] for pin in root["pins"]] == [
        "a_first",
        "stock_daily",
        "z_last",
    ]
    assert result.existing_pin_count == 2
    assert result.pin_count == 3


def test_existing_identical_dataset_pin_is_idempotent_without_replacement_flag() -> None:
    """Replaying the exact same declaration does not require replacement intent."""
    snapshot_bytes = _snapshot()
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    existing = _pin_document(_pin("stock_daily", snapshot_sha256=snapshot_digest))

    result = _stage(snapshot_bytes, existing)

    assert result.pin_count == 1
    assert result.existing_pin_count == 1
    assert (
        _decode_shared_and_runtime(result.pin_document_bytes, result.pin_file_sha256).get(
            "stock_daily"
        )
        is not None
    )


def test_changed_existing_dataset_pin_requires_explicit_replacement() -> None:
    """A pre-existing target cannot be silently overwritten by staging."""
    existing = _pin_document(_pin("stock_daily", authority_id="older-authority"))

    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing)

    assert exc.value.code == "replacement_confirmation_required"


def test_explicit_replacement_changes_only_the_named_dataset_pin() -> None:
    """Authorized replacement retains every unrelated pin byte-for-byte in meaning."""
    snapshot_bytes = _snapshot()
    existing = _pin_document(
        _pin("stock_daily", authority_id="older-authority"),
        _pin("fund_daily", authority_id="fund-authority"),
    )

    result = _stage(snapshot_bytes, existing, replace_dataset_id="stock_daily")

    decoded = _decode_shared_and_runtime(result.pin_document_bytes, result.pin_file_sha256)
    stock_pin = decoded.get("stock_daily")
    fund_pin = decoded.get("fund_daily")
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    assert stock_pin is not None and stock_pin.snapshot_sha256 == snapshot_digest
    expected_fund_pin = TemporalIdentityPin.model_validate(
        _pin("fund_daily", authority_id="fund-authority")
    )
    assert fund_pin == expected_fund_pin
    assert result.pin_count == 2


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"reference_version": "other-version"}, "reference_version_mismatch"),
        ({"source_id": "different-source"}, "source_id_mismatch"),
        ({"dataset_id": "bad id"}, "pin_metadata_invalid"),
        ({"authority_id": ""}, "pin_metadata_invalid"),
    ],
)
def test_stage_rejects_inconsistent_or_invalid_caller_declarations(
    overrides: dict[str, str], expected_code: str
) -> None:
    """Version/source relationships and strict route identifiers fail closed."""
    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), **overrides)

    assert exc.value.code == expected_code


def test_stage_rejects_noncanonical_and_non_bytes_snapshot_inputs() -> None:
    """The snapshot must be exact canonical bytes before hashing or pinning."""
    snapshot_bytes = _snapshot()
    with pytest.raises(TemporalIdentityStagingError) as non_bytes:
        _stage(bytearray(snapshot_bytes))
    assert non_bytes.value.code == "snapshot_bytes_invalid"

    with pytest.raises(TemporalIdentityStagingError) as noncanonical:
        _stage(snapshot_bytes + b" ")
    assert noncanonical.value.code == "snapshot_invalid"

    with pytest.raises(TemporalIdentityStagingError) as empty:
        _stage(b"")
    assert empty.value.code == "snapshot_size_invalid"


def test_stage_rejects_snapshot_with_no_assignments_because_source_cannot_be_checked() -> None:
    """An empty registry does not provide evidence for the requested source ID."""
    raw = encode_temporal_identity_snapshot(
        TemporalIdentityRegistry(
            reference_version="2026-10-28",
            evidence_ref="empty",
            assignments=(),
        )
    )

    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(raw)

    assert exc.value.code == "snapshot_source_unverifiable"


@pytest.mark.parametrize(
    ("existing", "overrides", "expected_code"),
    [
        (
            _pin_document(_pin("stock_daily", authority_id="old")),
            {"replace_dataset_id": "fund_daily"},
            "replacement_dataset_mismatch",
        ),
        (
            _pin_document(_pin("fund_daily")),
            {"replace_dataset_id": "stock_daily"},
            "replacement_target_missing",
        ),
    ],
)
def test_stage_rejects_invalid_replacement_requests(
    existing: bytes,
    overrides: dict[str, str],
    expected_code: str,
) -> None:
    """Replacement must name exactly one existing target dataset."""
    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing, **overrides)

    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"pins":[],"pins":[]}',
        b'{"schema_version":1,"pins":[{"dataset_id":"only-one-field"}]}',
        b'{"schema_version":2,"pins":[]}',
        b'{"schema_version":1,"pins":[],"unexpected":true}',
        b'{"schema_version":1,"pins":[NaN]}',
    ],
)
def test_stage_rejects_existing_pin_documents_runtime_would_reject(raw: bytes) -> None:
    """Existing malformed, duplicate, nonstandard or unknown data never gets dropped."""
    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), raw)

    assert exc.value.code == "existing_pin_document_invalid"


@pytest.mark.parametrize("existing", [bytearray(b"{}"), "not pin bytes"])
def test_stage_rejects_non_bytes_existing_pin_documents(existing: object) -> None:
    """Existing pin input must be immutable bytes, not coercible lookalikes."""
    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing)

    assert exc.value.code == "existing_pin_bytes_invalid"


@pytest.mark.parametrize(
    "existing",
    [b"", b" " * (MAX_TEMPORAL_IDENTITY_PIN_FILE_BYTES + 1)],
)
def test_stage_rejects_empty_or_oversized_existing_pin_documents(existing: bytes) -> None:
    """Existing pin bytes are rejected before parsing when outside fixed bounds."""
    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing)

    assert exc.value.code == "existing_pin_size_invalid"


def test_stage_rejects_duplicate_existing_dataset_pins() -> None:
    """Ambiguous pre-existing datasets are rejected before any merge occurs."""
    existing = _pin_document(_pin("fund_daily"), _pin("fund_daily"))

    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing)

    assert exc.value.code == "existing_pin_document_invalid"


def test_stage_rejects_pin_count_overflow_without_truncating_existing_entries() -> None:
    """The fixed runtime count cap fails instead of emitting a partial pin set."""
    existing = _pin_document(*(_pin(f"dataset-{index:04d}") for index in range(2_048)))

    with pytest.raises(TemporalIdentityStagingError) as exc:
        _stage(_snapshot(), existing)

    assert exc.value.code == "pin_count_limit_exceeded"


def test_stage_result_does_not_claim_authority_or_coverage() -> None:
    """The returned status states that the declaration is caller-supplied only."""
    result = _stage(_snapshot())

    assert result.verification_status == "caller_declared_unverified"
    assert not hasattr(result, "authority_verified")
    assert not hasattr(result, "coverage_verified")
