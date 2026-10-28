"""Behavioral checks for the strict reviewed instrument snapshot codec."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

import pytest

import opendata.data.instrument_reference_snapshot as snapshot_codec
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import (
    InstrumentReferenceSnapshotError,
    decode_instrument_reference_snapshot,
    encode_instrument_reference_snapshot,
)

_DATASET_ID = "equity_daily"
_SOURCE_ID = "fixture_source"


def _life(
    instrument_id: str,
    *,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    known_start: str | None = "2020-01-02",
    delisted_on: str | None = None,
) -> InstrumentLifecycle:
    """Build one explicitly synthetic lifecycle for codec tests."""
    start_date = date.fromisoformat(known_start) if known_start is not None else None
    delist_date = date.fromisoformat(delisted_on) if delisted_on is not None else None
    return InstrumentLifecycle(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        instrument_id=instrument_id,
        lifecycle_version=f"fixture-life-{instrument_id}",
        status=status,
        status_as_of=date.fromisoformat("2026-10-28"),
        status_evidence_ref=f"TEST-STATUS-{instrument_id}",
        known_start_date=start_date,
        start_boundary_unknown=known_start is None,
        start_evidence_ref=f"TEST-START-{instrument_id}" if start_date is not None else None,
        delisted_on=delist_date,
        delist_evidence_ref=f"TEST-DELIST-{instrument_id}" if delist_date is not None else None,
    )


def _alias(
    name: str,
    instrument_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> InstrumentAlias:
    """Build one synthetic source alias with explicit optional date bounds."""
    return InstrumentAlias(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        alias=name,
        instrument_id=instrument_id,
        alias_version=f"fixture-alias-{name}-{instrument_id}",
        evidence_ref=f"TEST-ALIAS-{name}-{instrument_id}",
        valid_from=date.fromisoformat(start) if start is not None else None,
        valid_through=date.fromisoformat(end) if end is not None else None,
    )


def _registry(*, reversed_rows: bool = False) -> InstrumentReferenceRegistry:
    """Build a registry with unknown, known, delisted, and time-scoped cases."""
    lifecycles: tuple[InstrumentLifecycle, ...] = (
        _life("TEST:UNKNOWN", status=LifecycleStatus.UNKNOWN, known_start=None),
        _life("TEST:KNOWN"),
        _life("TEST:DELISTED", status=LifecycleStatus.DELISTED, delisted_on="2024-06-30"),
    )
    aliases: tuple[InstrumentAlias, ...] = (
        _alias("OLD-KNOWN", "TEST:KNOWN", start="2010-01-01", end="2019-12-31"),
        _alias("OLD-UNKNOWN", "TEST:UNKNOWN", start="2020-01-01"),
    )
    if reversed_rows:
        lifecycles = tuple(reversed(lifecycles))
        aliases = tuple(reversed(aliases))
    return InstrumentReferenceRegistry(
        registry_version="fixture-registry-v1",
        evidence_ref="TEST-REGISTRY-REVIEW",
        lifecycles=lifecycles,
        aliases=aliases,
    )


def _canonical_document(document: dict[str, Any]) -> bytes:
    """Serialize a mutated test document with the codec's canonical envelope."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _rehash(document: dict[str, Any]) -> bytes:
    """Recompute the content digest to reach schema and domain rejection paths."""
    unsigned = dict(document)
    unsigned.pop("content_sha256", None)
    digest = hashlib.sha256(_canonical_document(unsigned)).hexdigest()
    return _canonical_document({**unsigned, "content_sha256": digest})


def test_empty_snapshot_matches_golden_canonical_json_and_digest() -> None:
    """Pin exact canonical bytes and the self-excluding payload hash contract."""
    empty = InstrumentReferenceRegistry(
        registry_version="TEST-v1",
        evidence_ref="TEST-evidence",
        lifecycles=(),
    )

    encoded = encode_instrument_reference_snapshot(empty)

    assert encoded == (
        b'{"aliases":[],"content_sha256":"'
        b"296c19d4ef9c79160fc7782b49e2b7ce6f406300422ffa699b94373c114c1e4a"
        b'","evidence_ref":"TEST-evidence","lifecycles":[],"registry_version":"TEST-v1",'
        b'"schema_version":1}'
    )
    assert decode_instrument_reference_snapshot(encoded) == empty


def test_round_trip_preserves_unknown_dates_statuses_aliases_and_is_order_stable() -> None:
    """Preserve unknown lifecycle evidence and produce stable output for reordering."""
    encoded = encode_instrument_reference_snapshot(_registry())

    assert encode_instrument_reference_snapshot(_registry(reversed_rows=True)) == encoded
    decoded = decode_instrument_reference_snapshot(encoded)
    unknown = next(row for row in decoded.lifecycles if row.instrument_id == "TEST:UNKNOWN")
    delisted = next(row for row in decoded.lifecycles if row.instrument_id == "TEST:DELISTED")
    dated_alias = next(row for row in decoded.aliases if row.alias == "OLD-KNOWN")

    assert unknown.status is LifecycleStatus.UNKNOWN
    assert unknown.status_as_of == date(2026, 10, 28)
    assert unknown.status_evidence_ref == "TEST-STATUS-TEST:UNKNOWN"
    assert unknown.known_start_date is None
    assert unknown.start_boundary_unknown is True
    assert unknown.start_evidence_ref is None
    assert delisted.delisted_on == date(2024, 6, 30)
    assert delisted.delist_evidence_ref == "TEST-DELIST-TEST:DELISTED"
    assert dated_alias.valid_from == date(2010, 1, 1)
    assert dated_alias.valid_through == date(2019, 12, 31)


def test_decoder_rejects_content_tampering_even_when_json_is_well_formed() -> None:
    """A valid JSON edit with an old digest is rejected before returning a model."""
    document = json.loads(encode_instrument_reference_snapshot(_registry()))
    document["evidence_ref"] = "TEST-TAMPERED-EVIDENCE"

    with pytest.raises(InstrumentReferenceSnapshotError, match="does not match"):
        decode_instrument_reference_snapshot(_canonical_document(document))


def test_decoder_rejects_duplicate_json_keys() -> None:
    """Duplicate keys cannot hide a second value behind parser last-wins rules."""
    encoded = encode_instrument_reference_snapshot(_registry())
    malformed = encoded.replace(
        b'"schema_version":1',
        b'"schema_version":1,"schema_version":1',
        1,
    )

    with pytest.raises(InstrumentReferenceSnapshotError, match="duplicate"):
        decode_instrument_reference_snapshot(malformed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda doc: doc["lifecycles"][0].pop("known_start_date"), "fields do not match"),
        (lambda doc: doc["lifecycles"][0].update(unreviewed="extra"), "fields do not match"),
        (lambda doc: doc.update(schema_version=True), "schema_version must be integer"),
        (
            lambda doc: doc["lifecycles"][0].update(schema_version=True),
            "schema_version must be integer",
        ),
        (lambda doc: doc["lifecycles"][0].update(start_boundary_unknown=1), "must be a boolean"),
        (lambda doc: doc["lifecycles"][0].update(status_as_of="20261028"), "ISO date string"),
        (lambda doc: doc["lifecycles"][0].update(known_start_date="2026-02-30"), "valid ISO date"),
        (lambda doc: doc["lifecycles"][0].update(status="not-a-status"), "unsupported"),
    ],
)
def test_decoder_rejects_schema_type_and_date_coercions(mutation: Any, message: str) -> None:
    """Reject omitted/extra fields, bool-as-int, bool-as-date, and loose dates."""
    document = json.loads(encode_instrument_reference_snapshot(_registry()))
    mutation(document)

    with pytest.raises(InstrumentReferenceSnapshotError, match=message):
        decode_instrument_reference_snapshot(_rehash(document))


def test_decoder_rejects_domain_invalid_alias_relationships() -> None:
    """Delegate duplicate identities and dangling aliases to the domain registry."""
    document = json.loads(encode_instrument_reference_snapshot(_registry()))
    document["aliases"][0]["instrument_id"] = "TEST:MISSING"

    with pytest.raises(InstrumentReferenceSnapshotError, match="instrument reference validation"):
        decode_instrument_reference_snapshot(_rehash(document))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "x" * 8192, "bounded string"),
        ("status_as_of", "x" * 8192, "ISO date string"),
    ],
)
def test_decoder_rejects_oversized_status_and_date_without_leaking_input(
    field: str, value: str, message: str
) -> None:
    """Large invalid text fails with a bounded error and no chained input value."""
    document = json.loads(encode_instrument_reference_snapshot(_registry()))
    document["lifecycles"][0][field] = value

    with pytest.raises(InstrumentReferenceSnapshotError, match=message) as error:
        decode_instrument_reference_snapshot(_rehash(document))

    assert error.value.__cause__ is None
    assert len(str(error.value)) < 128


def test_decoder_rejects_escaped_surrogate_without_leaking_encoding_error() -> None:
    """A JSON-escaped surrogate cannot escape as UnicodeEncodeError diagnostics."""
    encoded = encode_instrument_reference_snapshot(_registry())
    malformed = encoded.replace(b'"TEST-REGISTRY-REVIEW"', b'"\\ud800"', 1)

    with pytest.raises(
        InstrumentReferenceSnapshotError,
        match="cannot be canonically encoded",
    ) as error:
        decode_instrument_reference_snapshot(malformed)

    assert error.value.__cause__ is None
    assert len(str(error.value)) < 128


@pytest.mark.parametrize(
    "raw",
    [b"", b"not-json", b"\xff", b"NaN", b"{}"],
)
def test_decoder_rejects_empty_invalid_utf8_nonstandard_and_wrong_root(raw: bytes) -> None:
    """Malformed byte streams and non-object or nonstandard values fail closed."""
    with pytest.raises(InstrumentReferenceSnapshotError):
        decode_instrument_reference_snapshot(raw)


def test_decoder_preflights_json_depth_and_node_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject nested and excessive token streams before JSON tree construction."""
    with pytest.raises(InstrumentReferenceSnapshotError, match="depth limit"):
        decode_instrument_reference_snapshot(b"[[[[[[[[[0]]]]]]]]]")

    monkeypatch.setattr(snapshot_codec, "_MAX_JSON_NODES", 2)
    with pytest.raises(InstrumentReferenceSnapshotError, match="node limit"):
        decode_instrument_reference_snapshot(b'{"x":0}')

    monkeypatch.setattr(snapshot_codec, "_MAX_JSON_NODES", 12)
    empty = InstrumentReferenceRegistry(
        registry_version="TEST-v1",
        evidence_ref="TEST-evidence",
        lifecycles=(),
    )
    with pytest.raises(InstrumentReferenceSnapshotError, match="node limit"):
        encode_instrument_reference_snapshot(empty)


def test_encoder_and_decoder_enforce_the_shared_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check both directions without allocating the production-size limit."""
    monkeypatch.setattr(snapshot_codec, "MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES", 16)
    with pytest.raises(InstrumentReferenceSnapshotError, match="exceeds 64 MiB"):
        encode_instrument_reference_snapshot(_registry())
    with pytest.raises(InstrumentReferenceSnapshotError, match="exceeds 64 MiB"):
        decode_instrument_reference_snapshot(b"x" * 17)


def test_decoder_requires_immutable_bytes_input() -> None:
    """The public decoder accepts only the bounded immutable byte contract."""
    with pytest.raises(InstrumentReferenceSnapshotError, match="must be bytes"):
        decode_instrument_reference_snapshot(bytearray(b"{}"))
