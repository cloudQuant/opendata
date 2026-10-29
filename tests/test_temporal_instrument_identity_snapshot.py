"""Golden and adversarial coverage for the temporal identity JSON codec."""

from __future__ import annotations

from datetime import date
from typing import cast

import pytest

import opendata.data.temporal_instrument_identity_snapshot as codec
from opendata.data.temporal_instrument_identity import (
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)

GOLDEN_PAYLOAD = (
    b'{"assignments":[{"assignment_id":"a1","evidence_ref":"e1","exchange":"XSHG",'
    b'"instrument_id":"i1","source_id":"s1","source_sha256":null,"source_version":"v1",'
    b'"symbol":"600000","valid_from":"2024-01-02","valid_to":null}],'
    b'"evidence_ref":"registry-evidence","reference_version":"registry-v1",'
    b'"schema_version":1,"snapshot_type":"temporal_identity_registry"}'
)
GOLDEN_DOCUMENT = (
    b'{"assignments":[{"assignment_id":"a1","evidence_ref":"e1","exchange":"XSHG",'
    b'"instrument_id":"i1","source_id":"s1","source_sha256":null,"source_version":"v1",'
    b'"symbol":"600000","valid_from":"2024-01-02","valid_to":null}],'
    b'"content_sha256":"b2a527ef3cc93553b8857ca21ee23392f3bddd78b53560eab659c657b8df53c2",'
    b'"evidence_ref":"registry-evidence","reference_version":"registry-v1",'
    b'"schema_version":1,"snapshot_type":"temporal_identity_registry"}'
)
OVERLAPPING_DOCUMENT = (
    b'{"assignments":[{"assignment_id":"a1","evidence_ref":"e1","exchange":"XSHG",'
    b'"instrument_id":"i1","source_id":"s1","source_sha256":null,"source_version":"v1",'
    b'"symbol":"600000","valid_from":"2024-01-02","valid_to":"2024-03-01"},'
    b'{"assignment_id":"a2","evidence_ref":"e2","exchange":"XSHG","instrument_id":"i1",'
    b'"source_id":"s1","source_sha256":null,"source_version":"v1","symbol":"600000",'
    b'"valid_from":"2024-02-02","valid_to":null}],'
    b'"content_sha256":"6b9bc6e0c65d83601ecebc7e726dd9ff41b5f7b710df1c033cf671db7ccbd4dc",'
    b'"evidence_ref":"registry-evidence","reference_version":"registry-v1",'
    b'"schema_version":1,"snapshot_type":"temporal_identity_registry"}'
)


def _assignment(
    assignment_id: str,
    instrument_id: str,
    *,
    exchange: str = "XSHG",
    symbol: str = "600000",
    valid_from: date = date(2024, 1, 2),
    valid_to: date | None = None,
    source_sha256: str | None = None,
    evidence_ref: str | None = None,
) -> TemporalIdentityAssignment:
    """Build a small explicit assignment for deterministic codec cases."""
    return TemporalIdentityAssignment(
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        exchange=exchange,
        symbol=symbol,
        valid_from=valid_from,
        valid_to=valid_to,
        source_id="s1",
        source_version="v1",
        evidence_ref=evidence_ref or f"evidence-{assignment_id}",
        source_sha256=source_sha256,
    )


def _registry(*assignments: TemporalIdentityAssignment) -> TemporalIdentityRegistry:
    """Build a caller-supplied registry with fixed version metadata."""
    return TemporalIdentityRegistry(
        reference_version="registry-v1",
        evidence_ref="registry-evidence",
        assignments=assignments,
    )


def test_golden_vector_encodes_and_decodes_exact_canonical_bytes() -> None:
    """Pin the schema, field set, row metadata, nulls, digest, and wire bytes."""
    registry = _registry(_assignment("a1", "i1", evidence_ref="e1"))
    assert codec.encode_temporal_identity_snapshot(registry) == GOLDEN_DOCUMENT
    decoded = codec.decode_temporal_identity_snapshot(GOLDEN_DOCUMENT)
    assert decoded == registry
    assert decoded.assignments[0].valid_from == date(2024, 1, 2)
    assert decoded.assignments[0].valid_to is None


def test_registry_input_order_does_not_change_canonical_bytes() -> None:
    """Canonical assignments use the registry's stable pair and date ordering."""
    earlier = _assignment("a1", "i1", valid_from=date(2024, 1, 2), valid_to=date(2024, 2, 1))
    later = _assignment("a2", "i2", valid_from=date(2024, 2, 1))
    other_pair = _assignment(
        "b1",
        "i3",
        exchange="XSHE",
        symbol="000001",
        valid_from=date(2023, 1, 1),
    )
    first = _registry(later, other_pair, earlier)
    second = _registry(earlier, later, other_pair)
    encoded = codec.encode_temporal_identity_snapshot(first)
    assert encoded == codec.encode_temporal_identity_snapshot(second)
    assert codec.decode_temporal_identity_snapshot(encoded).assignments == first.assignments


def test_caller_declared_source_hash_is_preserved_without_reinterpretation() -> None:
    """A well-formed source hash round-trips as metadata in the content digest."""
    declared = "a" * 64
    registry = _registry(_assignment("a1", "i1", source_sha256=declared))
    restored = codec.decode_temporal_identity_snapshot(
        codec.encode_temporal_identity_snapshot(registry)
    )
    assert restored.assignments[0].source_sha256 == declared


def test_decoder_rejects_duplicate_object_keys() -> None:
    """Repeated JSON members fail before schema or digest interpretation."""
    duplicate = GOLDEN_DOCUMENT.replace(
        b'"snapshot_type":"temporal_identity_registry"}',
        b'"snapshot_type":"temporal_identity_registry",'
        b'"snapshot_type":"temporal_identity_registry"}',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="duplicate"):
        codec.decode_temporal_identity_snapshot(duplicate)


def test_decoder_rejects_noncanonical_json_bytes() -> None:
    """Whitespace changes are not accepted as a second encoding of the same value."""
    spaced = GOLDEN_DOCUMENT[:-1] + b" }"
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="canonical"):
        codec.decode_temporal_identity_snapshot(spaced)


def test_decoder_rejects_changed_content_even_when_schema_remains_valid() -> None:
    """A semantically valid payload edit cannot retain the old content digest."""
    changed = GOLDEN_DOCUMENT.replace(b"registry-evidence", b"registry-evidencf")
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="does not match"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_boolean_schema_version() -> None:
    """JSON booleans are not accepted as integer schema versions."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"schema_version":1,"snapshot_type":',
        b'"schema_version":true,"snapshot_type":',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="integer 1"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_unknown_snapshot_type() -> None:
    """A fixed discriminator prevents another protocol's document being accepted."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"snapshot_type":"temporal_identity_registry"',
        b'"snapshot_type":"other_registry"',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="snapshot_type"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_oversized_integer_without_converting_it() -> None:
    """The JSON parser callback refuses long integer tokens before int conversion."""
    huge_integer = b"9" * 100_000
    changed = GOLDEN_DOCUMENT.replace(
        b'"schema_version":1,',
        b'"schema_version":' + huge_integer + b",",
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="integer 1"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_noncanonical_or_invalid_dates() -> None:
    """Dates must be real canonical calendar dates before digest validation."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"valid_from":"2024-01-02"',
        b'"valid_from":"2024-02-30"',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="valid ISO date"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_reuses_domain_overlap_validation() -> None:
    """Overlapping half-open pair assignments fail domain registry construction."""
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="identity validation"):
        codec.decode_temporal_identity_snapshot(OVERLAPPING_DOCUMENT)


def test_decoder_reuses_domain_duplicate_assignment_id_validation() -> None:
    """Duplicate assignment identifiers are rejected by the domain registry."""
    non_overlapping = OVERLAPPING_DOCUMENT.replace(
        b'"valid_from":"2024-02-02","valid_to":null}',
        b'"valid_from":"2024-03-01","valid_to":null}',
    )
    duplicate = non_overlapping.replace(
        b'"assignment_id":"a2"',
        b'"assignment_id":"a1"',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="identity validation"):
        codec.decode_temporal_identity_snapshot(duplicate)


def test_decoder_rejects_unknown_fields() -> None:
    """The versioned wire schema does not silently accept added root fields."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"content_sha256":"b2a5',
        b'"unexpected":"extension","content_sha256":"b2a5',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="fields do not match"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_non_bytes_and_empty_input() -> None:
    """The public decoder requires a nonempty immutable byte string."""
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="must be bytes"):
        codec.decode_temporal_identity_snapshot(cast("bytes", bytearray(GOLDEN_DOCUMENT)))
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="must not be empty"):
        codec.decode_temporal_identity_snapshot(b"")


def test_decoder_translates_invalid_utf8_and_json_syntax() -> None:
    """Invalid UTF-8 and malformed but balanced JSON fail as codec errors."""
    for raw in (b"\xff", b'{"field":,}'):
        with pytest.raises(codec.TemporalIdentitySnapshotError, match="strict UTF-8 JSON"):
            codec.decode_temporal_identity_snapshot(raw)


def test_decoder_rejects_invalid_content_digest_shape() -> None:
    """Digest input must be exactly 64 lowercase hexadecimal characters."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"content_sha256":"b2a5',
        b'"content_sha256":"B2a5',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="lowercase SHA-256"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_non_array_assignments() -> None:
    """Assignments must be an explicitly present JSON array."""
    raw = (
        b'{"assignments":null,"content_sha256":"'
        + b"0" * 64
        + b'","evidence_ref":"registry-evidence","reference_version":"registry-v1",'
        b'"schema_version":1,"snapshot_type":"temporal_identity_registry"}'
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="assignments must be an array"):
        codec.decode_temporal_identity_snapshot(raw)


def test_decoder_rejects_incomplete_root_schema() -> None:
    """Missing required root members fail the exact field-set check."""
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="fields do not match"):
        codec.decode_temporal_identity_snapshot(b"{}")


def test_decoder_rejects_non_string_registry_metadata() -> None:
    """Registry metadata fields remain strings and do not accept JSON booleans."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"reference_version":"registry-v1"',
        b'"reference_version":true',
    )
    with pytest.raises(
        codec.TemporalIdentitySnapshotError, match="reference_version must be a string"
    ):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_malformed_date_shape() -> None:
    """The lexical date form requires two-digit month and day components."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"valid_from":"2024-01-02"',
        b'"valid_from":"2024-1-02"',
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="ISO date string"):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_rejects_escaped_surrogate_that_cannot_be_canonicalized() -> None:
    """Escaped lone surrogates fail when canonical UTF-8 bytes are produced."""
    changed = GOLDEN_DOCUMENT.replace(
        b'"evidence_ref":"registry-evidence"',
        b'"evidence_ref":"\\ud800"',
    )
    with pytest.raises(
        codec.TemporalIdentitySnapshotError,
        match="cannot be canonically encoded",
    ):
        codec.decode_temporal_identity_snapshot(changed)


def test_decoder_preflight_handles_json_string_escapes() -> None:
    """The lexical preflight skips escaped quotes and backslashes inside strings."""
    escaped_value = b'"registry' + bytes((92, 34)) + b"evidence" + bytes((92, 92)) + b'path"'
    changed = GOLDEN_DOCUMENT.replace(b'"registry-evidence"', escaped_value)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="does not match"):
        codec.decode_temporal_identity_snapshot(changed)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"NaN", "nonstandard number"),
        (b"1.0", "unsupported number"),
        (b"]", "mismatched containers"),
        (b'{"x":]', "mismatched containers"),
        (b'"unterminated', "unterminated string"),
        (b"{", "unterminated containers"),
    ],
)
def test_decoder_rejects_nonstandard_or_malformed_json_tokens(raw: bytes, message: str) -> None:
    """Nonstandard constants, floats, and unbalanced JSON fail with explicit errors."""
    with pytest.raises(codec.TemporalIdentitySnapshotError, match=message):
        codec.decode_temporal_identity_snapshot(raw)


def test_decoder_preflight_enforces_object_count_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nested object count is bounded independently of array item count."""
    monkeypatch.setattr(codec, "_MAX_ASSIGNMENTS", 0)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="100000-assignment limit"):
        codec.decode_temporal_identity_snapshot(b'{"x":{}}')


def test_encoder_rejects_registry_substitute_type() -> None:
    """Encoding accepts the exact immutable domain registry class only."""
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="TemporalIdentityRegistry"):
        codec.encode_temporal_identity_snapshot(cast("TemporalIdentityRegistry", object()))


def test_encoder_rejects_assignment_count_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry row limit is checked before sorting or serialization."""
    registry = _registry(_assignment("a1", "i1"))
    monkeypatch.setattr(codec, "_MAX_ASSIGNMENTS", 0)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="assignment limit"):
        codec.encode_temporal_identity_snapshot(registry)


def test_encoder_rejects_node_budget_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """The calculated JSON node budget is checked before row mapping creation."""
    registry = _registry(_assignment("a1", "i1"))
    monkeypatch.setattr(codec, "_MAX_JSON_NODES", 13)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="node limit"):
        codec.encode_temporal_identity_snapshot(registry)


def test_encoder_rejects_unpaired_surrogate_in_registry_text() -> None:
    """Domain-valid text that cannot be UTF-8 encoded fails as a codec error."""
    registry = _registry(_assignment("a1", "i1", evidence_ref="\ud800"))
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="not canonically encodable"):
        codec.encode_temporal_identity_snapshot(registry)


def test_decoder_rejects_input_over_byte_limit_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Input above the byte cap is rejected before invoking the JSON parser."""
    monkeypatch.setattr(codec, "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES", 8)

    def unexpected_loads(*args: object, **kwargs: object) -> object:
        """Fail if parsing starts before the input size check."""
        raise AssertionError("json.loads must not run")

    monkeypatch.setattr(codec.json, "loads", unexpected_loads)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="exceeds"):
        codec.decode_temporal_identity_snapshot(GOLDEN_DOCUMENT)


def test_encoder_enforces_output_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming encoder stops when the final document exceeds its cap."""
    registry = _registry(_assignment("a1", "i1", evidence_ref="e1"))
    monkeypatch.setattr(
        codec,
        "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES",
        len(GOLDEN_PAYLOAD) + 32,
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="exceeds 64 MiB"):
        codec.encode_temporal_identity_snapshot(registry)


def test_encoder_enforces_digest_input_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonical digest input is bounded before final document serialization."""
    registry = _registry(_assignment("a1", "i1", evidence_ref="e1"))
    monkeypatch.setattr(
        codec,
        "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES",
        len(GOLDEN_PAYLOAD) - 1,
    )
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="exceeds 64 MiB"):
        codec.encode_temporal_identity_snapshot(registry)


def test_decoder_preflight_bounds_array_rows_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lexical pass rejects an oversized row array before JSON parsing."""
    monkeypatch.setattr(codec, "_MAX_JSON_ARRAY_ITEMS", 1)

    def unexpected_loads(*args: object, **kwargs: object) -> object:
        """Fail if parsing starts before the array-size check."""
        raise AssertionError("json.loads must not run")

    monkeypatch.setattr(codec.json, "loads", unexpected_loads)
    raw = b'{"assignments":[{},{}]}'
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="array item limit"):
        codec.decode_temporal_identity_snapshot(raw)


def test_decoder_preflight_rejects_excess_nodes_before_json_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The iterative node bound runs before standard-library object creation."""

    def unexpected_loads(*args: object, **kwargs: object) -> object:
        """Fail if parsing begins before node preflight succeeds."""
        raise AssertionError("json.loads must not run")

    monkeypatch.setattr(codec, "_MAX_JSON_NODES", 1)
    monkeypatch.setattr(codec.json, "loads", unexpected_loads)
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="node limit"):
        codec.decode_temporal_identity_snapshot(GOLDEN_DOCUMENT)


def test_decoder_preflight_bounds_object_members() -> None:
    """An oversized object is rejected before duplicate-key bookkeeping."""
    fields = b",".join(f'"key{index}":0'.encode("ascii") for index in range(11))
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="member limit"):
        codec.decode_temporal_identity_snapshot(b"{" + fields + b"}")


def test_decoder_preflight_bounds_nesting_before_json_materialization() -> None:
    """Excessive nesting is rejected by the iterative scanner."""
    raw = b"[" * 9 + b"0" + b"]" * 9
    with pytest.raises(codec.TemporalIdentitySnapshotError, match="depth limit"):
        codec.decode_temporal_identity_snapshot(raw)


def test_domain_rejects_empty_half_open_interval() -> None:
    """The codec registry uses the domain rule that valid_to must be later."""
    with pytest.raises(TemporalInstrumentIdentityError, match="after valid_from"):
        _assignment(
            "a1",
            "i1",
            valid_from=date(2024, 1, 2),
            valid_to=date(2024, 1, 2),
        )
