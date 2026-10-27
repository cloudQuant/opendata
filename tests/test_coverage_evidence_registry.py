"""Strict operator evidence loading for publication coverage."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest

from opendata.data.coverage_evidence_registry import (
    MAX_COVERAGE_CALENDAR_DATES,
    MAX_COVERAGE_EVIDENCE_ENTRIES,
    MAX_COVERAGE_EVIDENCE_FILE_BYTES,
    VISIBLE_RECORD_SET_ALGORITHM,
    CoverageEvidenceConfigurationError,
    DatasetCoverageEvidence,
    DatasetCoverageEvidenceRegistry,
    load_dataset_coverage_evidence_registry,
)
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentReferenceRegistry,
)

if TYPE_CHECKING:
    from pathlib import Path


def _document() -> dict[str, object]:
    """Build a synthetic evidence bundle for one allowed index publication."""
    return {
        "schema_version": 1,
        "entries": [
            {
                "dataset_id": "cn.csi300.index.daily",
                "source_id": "ths.index_daily",
                "publication_id": "a" * 32,
                "instrument_registry": {
                    "registry_version": "index-reference-1",
                    "evidence_ref": "docs/evidence/coverage/index-registry.json",
                    "lifecycle": {
                        "lifecycle_version": "csi300-life-1",
                        "status": "active",
                        "status_as_of": "2026-10-22",
                        "status_evidence_ref": "docs/evidence/coverage/status.json",
                        "known_start_date": "2026-10-21",
                        "start_boundary_unknown": False,
                        "start_evidence_ref": "docs/evidence/coverage/listing.json",
                        "delisted_on": None,
                        "delist_evidence_ref": None,
                    },
                },
                "calendar": {
                    "calendar_id": "SSE",
                    "calendar_version": "calendar-1",
                    "evidence_ref": "docs/evidence/coverage/calendar.json",
                    "covered_from": "2026-10-21",
                    "covered_through": "2026-10-22",
                    "open_dates": ["2026-10-21", "2026-10-22"],
                    "daily_classification_complete": True,
                },
                "publication_scope": {
                    "start": "2026-10-21",
                    "end_exclusive": "2026-10-23",
                    "evidence_ref": "docs/evidence/coverage/publication-scan.json",
                },
            }
        ],
    }


def _schema2_document() -> dict[str, object]:
    """Build an operator document with a prior full-visible-set content pin."""
    document = _document()
    document["schema_version"] = 2
    document["entries"][0]["visible_record_set"] = {
        "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
        "sha256": "b" * 64,
        "record_count": 2,
        "publication_sequence": 7,
        "contract_sha256": "c" * 64,
    }
    return document


def _write_document(
    tmp_path: Path, document: dict[str, object], *, name: str = "coverage.json"
) -> Path:
    """Write a synthetic operator document inside the test's private directory."""
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _loaded_evidence(tmp_path: Path) -> DatasetCoverageEvidence:
    """Build the canonical typed fixture through the public file loader."""
    registry = load_dataset_coverage_evidence_registry(_write_document(tmp_path, _document()))
    evidence = registry.resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert evidence is not None
    return evidence


def test_registry_resolves_only_exact_dataset_source_publication_pin(tmp_path: Path) -> None:
    """A valid operator file resolves the exact immutable triple and nothing adjacent."""
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")

    registry = load_dataset_coverage_evidence_registry(path)

    evidence = registry.resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert evidence is not None
    assert len(evidence.evidence_sha256) == 64
    assert all(character in "0123456789abcdef" for character in evidence.evidence_sha256)
    assert registry.resolve("cn.csi300.index.daily", "ths.index_daily", "b" * 32) is None
    assert registry.resolve("cn.csi500.index.daily", "ths.index_daily", "a" * 32) is None
    assert registry.resolve("cn.csi300.index.daily", "other", "a" * 32) is None

    changed_document = _document()
    changed_document["entries"][0]["calendar"]["calendar_version"] = "calendar-2"
    changed_path = tmp_path / "changed-coverage.json"
    changed_path.write_text(json.dumps(changed_document), encoding="utf-8")
    changed = load_dataset_coverage_evidence_registry(changed_path).resolve(
        "cn.csi300.index.daily", "ths.index_daily", "a" * 32
    )
    assert changed is not None
    assert changed.evidence_sha256 != evidence.evidence_sha256


def test_schema2_registry_parses_and_fingerprints_full_visible_set_pin(tmp_path: Path) -> None:
    """Schema 2 binds the independently supplied full visible set and contract."""
    registry = load_dataset_coverage_evidence_registry(
        _write_document(tmp_path, _schema2_document())
    )
    evidence = registry.resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert evidence is not None
    assert evidence.visible_record_set is not None
    assert evidence.visible_record_set.algorithm == VISIBLE_RECORD_SET_ALGORITHM
    assert evidence.visible_record_set.sha256 == "b" * 64
    assert evidence.visible_record_set.record_count == 2
    assert evidence.visible_record_set.publication_sequence == 7
    assert evidence.visible_record_set.contract_sha256 == "c" * 64

    changed_document = _schema2_document()
    changed_document["entries"][0]["visible_record_set"]["sha256"] = "d" * 64
    changed = load_dataset_coverage_evidence_registry(
        _write_document(tmp_path, changed_document, name="changed-schema2.json")
    ).resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert changed is not None
    assert changed.evidence_sha256 != evidence.evidence_sha256


@pytest.mark.parametrize(
    "pin",
    (
        {
            "algorithm": "other-v1",
            "sha256": "b" * 64,
            "record_count": 2,
            "publication_sequence": 7,
            "contract_sha256": "c" * 64,
        },
        {
            "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
            "sha256": "B" * 64,
            "record_count": 2,
            "publication_sequence": 7,
            "contract_sha256": "c" * 64,
        },
        {
            "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
            "sha256": "b" * 64,
            "record_count": True,
            "publication_sequence": 7,
            "contract_sha256": "c" * 64,
        },
        {
            "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
            "sha256": "b" * 64,
            "record_count": 2,
            "publication_sequence": 0,
            "contract_sha256": "c" * 64,
        },
        {
            "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
            "sha256": "b" * 64,
            "record_count": 2,
            "publication_sequence": 7,
            "contract_sha256": "C" * 64,
        },
        {
            "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
            "sha256": "b" * 64,
            "record_count": 2,
            "publication_sequence": 7,
            "contract_sha256": "c" * 64,
            "extra": "ignored",
        },
    ),
)
def test_schema2_rejects_invalid_visible_record_set_pins(
    tmp_path: Path, pin: dict[str, object]
) -> None:
    """Unsupported algorithms, coercions, malformed hashes, and extra fields fail closed."""
    document = _schema2_document()
    document["entries"][0]["visible_record_set"] = pin
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


@pytest.mark.parametrize(
    "mutate",
    (
        lambda doc: doc["entries"][0].update(publication_id="A" * 32),
        lambda doc: doc["entries"][0].update(dataset_id="cn.equity.daily"),
        lambda doc: doc["entries"][0]["calendar"].update(daily_classification_complete=1),
        lambda doc: doc["entries"][0]["publication_scope"].update(evidence_ref="reviewed"),
        lambda doc: doc["entries"][0].update(unreviewed_status="approved"),
    ),
)
def test_registry_rejects_unbound_or_coerced_evidence_fields(
    tmp_path: Path, mutate: object
) -> None:
    """Malformed pins, weak references, coercions, and unknown claims fail closed."""
    document = _document()
    mutate(document)  # type: ignore[operator]
    path = tmp_path / "invalid-coverage.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(path)


def test_registry_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Duplicate keys cannot shadow the publication identity in strict JSON."""
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":1,"entries":[],"schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("dataset_id", "cn.unreviewed.index.daily", ValueError),
        ("source_id", "unreviewed.provider", ValueError),
        ("publication_id", None, ValueError),
        ("publication_id", "A" * 32, ValueError),
        ("instrument_identifier", "000905.SH", ValueError),
        ("instrument_registry", object(), TypeError),
        ("calendar", object(), TypeError),
        ("publication_scope_start", datetime(2026, 10, 21), ValueError),
        ("publication_scope_end_exclusive", datetime(2026, 10, 23), ValueError),
        ("publication_scope_start", date(2026, 10, 23), ValueError),
        ("publication_scope_evidence_ref", "operator-reviewed", ValueError),
    ),
)
def test_typed_evidence_constructor_rejects_unbound_or_invalid_pins(
    tmp_path: Path, field: str, value: object, error: type[Exception]
) -> None:
    """Programmatic callers cannot bypass identity, date, or evidence-reference checks."""
    with pytest.raises(error):
        replace(_loaded_evidence(tmp_path), **{field: value})


def test_typed_evidence_constructor_rejects_empty_or_aliased_instrument_registries(
    tmp_path: Path,
) -> None:
    """Coverage evidence requires one canonical lifecycle and no aliases."""
    evidence = _loaded_evidence(tmp_path)
    reference = evidence.instrument_registry
    empty_registry = InstrumentReferenceRegistry(
        registry_version=reference.registry_version,
        evidence_ref=reference.evidence_ref,
        lifecycles=(),
    )
    with pytest.raises(ValueError, match="exactly one canonical lifecycle"):
        replace(evidence, instrument_registry=empty_registry)

    alias = InstrumentAlias(
        dataset_id=evidence.dataset_id,
        source_id=evidence.source_id,
        alias="CSI300",
        instrument_id=evidence.instrument_identifier,
        alias_version="alias-1",
        evidence_ref="docs/evidence/coverage/alias.json",
    )
    aliased_registry = replace(reference, aliases=(alias,))
    with pytest.raises(ValueError, match="exactly one canonical lifecycle"):
        replace(evidence, instrument_registry=aliased_registry)


def test_typed_evidence_constructor_rejects_calendar_bound_to_another_index(
    tmp_path: Path,
) -> None:
    """Calendar evidence is tied to the same exact index symbol as its pin."""
    evidence = _loaded_evidence(tmp_path)
    other_calendar = replace(evidence.calendar, instrument_id="000905.SH")
    with pytest.raises(ValueError, match="calendar is not bound"):
        replace(evidence, calendar=other_calendar)


def test_typed_evidence_constructor_rejects_registry_subclasses_and_lifecycle_drift(
    tmp_path: Path,
) -> None:
    """A stateful resolver subclass cannot change lifecycle after fingerprinting."""
    evidence = _loaded_evidence(tmp_path)
    original = evidence.instrument_registry
    resolve_calls: list[int] = []

    class WrongResolvedInstrumentRegistry(InstrumentReferenceRegistry):
        def resolve(self, dataset_id: str, source_id: str, identifier: str, **kwargs: object):
            resolved = super().resolve(dataset_id, source_id, identifier, **kwargs)
            return replace(resolved, instrument_id="000905.SH")

    class WrongResolvedLifecycleRegistry(InstrumentReferenceRegistry):
        def resolve(self, dataset_id: str, source_id: str, identifier: str, **kwargs: object):
            resolved = super().resolve(dataset_id, source_id, identifier, **kwargs)
            return replace(
                resolved,
                lifecycle=replace(resolved.lifecycle, dataset_id="cn.csi500.index.daily"),
            )

    class AlteredSameIdentityLifecycleRegistry(InstrumentReferenceRegistry):
        def resolve(self, dataset_id: str, source_id: str, identifier: str, **kwargs: object):
            resolved = super().resolve(dataset_id, source_id, identifier, **kwargs)
            return replace(
                resolved,
                lifecycle=replace(resolved.lifecycle, status_as_of=date(2026, 10, 23)),
            )

    class StatefulLifecycleRegistry(InstrumentReferenceRegistry):
        def resolve(self, dataset_id: str, source_id: str, identifier: str, **kwargs: object):
            resolve_calls.append(len(resolve_calls))
            resolved = super().resolve(dataset_id, source_id, identifier, **kwargs)
            if len(resolve_calls) > 1:
                return replace(
                    resolved,
                    lifecycle=replace(resolved.lifecycle, status_as_of=date(2026, 10, 24)),
                )
            return resolved

    def subclass_registry(
        registry_type: type[InstrumentReferenceRegistry],
    ) -> InstrumentReferenceRegistry:
        return registry_type(
            registry_version=original.registry_version,
            evidence_ref=original.evidence_ref,
            lifecycles=original.lifecycles,
            aliases=original.aliases,
            schema_version=original.schema_version,
        )

    for registry_type in (
        WrongResolvedInstrumentRegistry,
        WrongResolvedLifecycleRegistry,
        AlteredSameIdentityLifecycleRegistry,
        StatefulLifecycleRegistry,
    ):
        with pytest.raises(ValueError, match="subclasses are not accepted"):
            replace(evidence, instrument_registry=subclass_registry(registry_type))
    assert resolve_calls == []


def test_registry_constructor_rejects_bad_rows_duplicates_and_oversized_sequences(
    tmp_path: Path,
) -> None:
    """Direct registry construction preserves exact pins and the bounded input contract."""
    evidence = _loaded_evidence(tmp_path)
    with pytest.raises(CoverageEvidenceConfigurationError):
        DatasetCoverageEvidenceRegistry("not-a-sequence")
    with pytest.raises(CoverageEvidenceConfigurationError):
        DatasetCoverageEvidenceRegistry((object(),))  # type: ignore[arg-type]
    with pytest.raises(CoverageEvidenceConfigurationError):
        DatasetCoverageEvidenceRegistry((evidence, evidence))
    with pytest.raises(CoverageEvidenceConfigurationError):
        DatasetCoverageEvidenceRegistry((evidence,) * (MAX_COVERAGE_EVIDENCE_ENTRIES + 1))

    registry = DatasetCoverageEvidenceRegistry((evidence,))
    with pytest.raises(TypeError):
        registry._entries[("other", "other", "other")] = evidence  # type: ignore[index]
    assert DatasetCoverageEvidenceRegistry.empty().resolve("x", "y", "z") is None


@pytest.mark.parametrize("schema_version", (True, 3))
def test_loader_rejects_unsupported_schema_versions(tmp_path: Path, schema_version: object) -> None:
    """Schema version validation rejects bool coercion and unknown versions."""
    document = {"schema_version": schema_version, "entries": []}
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


@pytest.mark.parametrize("entries", (None, "not-an-array"))
def test_loader_rejects_non_array_entries(tmp_path: Path, entries: object) -> None:
    """The top-level entries field must remain a JSON array."""
    document = {"schema_version": 1, "entries": entries}
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


def test_loader_rejects_entry_count_above_configured_limit(tmp_path: Path) -> None:
    """Row-count limits are checked before parsing or materializing evidence objects."""
    document = {
        "schema_version": 1,
        "entries": [None] * (MAX_COVERAGE_EVIDENCE_ENTRIES + 1),
    }
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


def test_loader_rejects_file_above_byte_limit(tmp_path: Path) -> None:
    """Oversized input is refused after reading at most one byte beyond its limit."""
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (MAX_COVERAGE_EVIDENCE_FILE_BYTES + 1))
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(path)


@pytest.mark.parametrize(
    "raw",
    (
        b"{not-json",
        b'{"schema_version":NaN,"entries":[]}',
        b"\xff",
    ),
)
def test_loader_wraps_invalid_json_constants_and_encoding(tmp_path: Path, raw: bytes) -> None:
    """Syntax errors, non-standard constants, and invalid UTF-8 fail as one safe error."""
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(path)


def test_loader_wraps_missing_file_errors(tmp_path: Path) -> None:
    """Missing operator configuration is reported through the public config error."""
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "reference",
    (
        "",
        " docs/evidence/coverage/review.json",
        "docs/evidence/../private.json",
        "docs/evidence/bad\\name.json",
        "x" * 513,
        "bad\nreference",
    ),
)
def test_loader_rejects_empty_unbounded_or_unsafe_evidence_references(
    tmp_path: Path, reference: str
) -> None:
    """Evidence references must be bounded, canonical, and confined to evidence docs."""
    document = _document()
    document["entries"][0]["publication_scope"]["evidence_ref"] = reference
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


def test_loader_accepts_explicit_review_ticket_as_an_evidence_anchor(tmp_path: Path) -> None:
    """An uppercase review ticket is a supported immutable evidence reference."""
    document = _document()
    document["entries"][0]["publication_scope"]["evidence_ref"] = "COV-204"
    registry = load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))
    evidence = registry.resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert evidence is not None
    assert evidence.publication_scope_evidence_ref == "COV-204"


def test_loader_preserves_explicitly_unknown_inception_boundary(tmp_path: Path) -> None:
    """A declared unknown start remains unknown instead of using observed rows."""
    document = _document()
    lifecycle = document["entries"][0]["instrument_registry"]["lifecycle"]
    lifecycle.update(
        known_start_date=None,
        start_boundary_unknown=True,
        start_evidence_ref=None,
    )
    registry = load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))
    evidence = registry.resolve("cn.csi300.index.daily", "ths.index_daily", "a" * 32)
    assert evidence is not None
    resolved = evidence.instrument_registry.resolve(
        evidence.dataset_id, evidence.source_id, evidence.instrument_identifier
    )
    assert resolved.lifecycle.known_start_date is None
    assert resolved.lifecycle.start_boundary_unknown is True
    assert resolved.lifecycle.start_evidence_ref is None


@pytest.mark.parametrize(
    "mutate",
    (
        lambda document: document["entries"][0]["calendar"].update(open_dates="not-a-list"),
        lambda document: document["entries"][0]["calendar"].update(
            open_dates=["2026-10-21", "2026-10-21"]
        ),
        lambda document: document["entries"][0]["calendar"].update(open_dates=["2026-02-30"]),
        lambda document: document["entries"][0]["instrument_registry"]["lifecycle"].update(
            status_as_of="20261021"
        ),
        lambda document: document["entries"][0]["publication_scope"].update(start="2026-10-23"),
        lambda document: document["entries"][0]["instrument_registry"]["lifecycle"].update(
            status_as_of="2026-10-21T00:00:00"
        ),
    ),
)
def test_loader_rejects_invalid_calendar_and_date_semantics(tmp_path: Path, mutate: object) -> None:
    """Calendar lists and date-only half-open evidence retain strict semantics."""
    document = _document()
    mutate(document)  # type: ignore[operator]
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(_write_document(tmp_path, document))


def test_calendar_date_limit_is_enforced_before_parsing_elements(tmp_path: Path) -> None:
    """The public loader checks its date cap before parsing any calendar value."""
    document = _document()
    document["entries"][0]["calendar"]["open_dates"] = [""] * (MAX_COVERAGE_CALENDAR_DATES + 1)
    path = tmp_path / "too-many-calendar-dates.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CoverageEvidenceConfigurationError):
        load_dataset_coverage_evidence_registry(path)
