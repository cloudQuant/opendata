"""Strict, operator-owned evidence pins for published history coverage.

This registry intentionally supports only the separately published THS index
datasets whose contracts and symbol identities are on the repository's exact
single-series allowlist. It never derives lifecycle, calendar, or publication
scope evidence from warehouse rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from opendata.data.instrument_reference import (
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.providers.ths.index_daily_publication import (
    INDEX_SOURCE_ID,
    INDEX_SYMBOL_BY_DATASET_ID,
)
from opendata.pipeline.coverage_service import CalendarEvidence

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_COVERAGE_EVIDENCE_FILE_BYTES = 1_048_576
MAX_COVERAGE_EVIDENCE_ENTRIES = 2_048
MAX_COVERAGE_CALENDAR_DATES = 100_000
_PUBLICATION_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$", re.ASCII)
_DOCS_PATH_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$", re.ASCII)
VISIBLE_RECORD_SET_ALGORITHM = "visible-record-set-v1"


class CoverageEvidenceConfigurationError(ValueError):
    """Raised when an operator coverage-evidence file is invalid."""


@dataclass(frozen=True, slots=True)
class VisibleRecordSetPin:
    """Prior operator pin for one verified complete publication record set."""

    algorithm: str
    sha256: str
    record_count: int
    publication_sequence: int
    contract_sha256: str

    def __post_init__(self) -> None:
        """Reject pins with unsupported algorithms, hashes, or coerced counts."""
        if self.algorithm != VISIBLE_RECORD_SET_ALGORITHM:
            raise ValueError("visible record-set algorithm is unsupported")
        if not isinstance(self.sha256, str) or _SHA256_RE.fullmatch(self.sha256) is None:
            raise ValueError("visible record-set sha256 must be lowercase hexadecimal")
        if type(self.record_count) is not int or self.record_count < 0:
            raise ValueError("visible record-set record_count must be a nonnegative integer")
        if type(self.publication_sequence) is not int or self.publication_sequence < 1:
            raise ValueError("publication_sequence must be a positive integer")
        if (
            not isinstance(self.contract_sha256, str)
            or _SHA256_RE.fullmatch(self.contract_sha256) is None
        ):
            raise ValueError("contract_sha256 must be lowercase hexadecimal")


@dataclass(frozen=True, slots=True)
class DatasetCoverageEvidence:
    """Evidence bundle pinned to one dataset, source, and immutable publication."""

    dataset_id: str
    source_id: str
    publication_id: str
    instrument_identifier: str
    instrument_registry: InstrumentReferenceRegistry
    calendar: CalendarEvidence
    publication_scope_start: date
    publication_scope_end_exclusive: date
    publication_scope_evidence_ref: str
    visible_record_set: VisibleRecordSetPin | None = None

    def __post_init__(self) -> None:
        """Reject a bundle whose identity or temporal evidence is not exact."""
        if self.dataset_id not in INDEX_SYMBOL_BY_DATASET_ID:
            raise ValueError("coverage evidence is limited to the trusted index dataset allowlist")
        if self.source_id != INDEX_SOURCE_ID:
            raise ValueError("coverage evidence source does not match the trusted index source")
        if not isinstance(self.publication_id, str) or not _PUBLICATION_ID_RE.fullmatch(
            self.publication_id
        ):
            raise ValueError("publication_id must be 32 lowercase hexadecimal characters")
        expected_instrument = INDEX_SYMBOL_BY_DATASET_ID[self.dataset_id]
        if self.instrument_identifier != expected_instrument:
            raise ValueError("instrument identifier does not match the trusted dataset allowlist")
        if not isinstance(self.instrument_registry, InstrumentReferenceRegistry):
            raise TypeError("instrument_registry must be an InstrumentReferenceRegistry")
        if type(self.instrument_registry) is not InstrumentReferenceRegistry:
            raise ValueError(
                "instrument_registry subclasses are not accepted for coverage evidence"
            )
        if len(self.instrument_registry.lifecycles) != 1 or self.instrument_registry.aliases:
            raise ValueError(
                "coverage evidence requires exactly one canonical lifecycle and no aliases"
            )
        if not isinstance(self.calendar, CalendarEvidence):
            raise TypeError("calendar must be CalendarEvidence")
        if (self.calendar.dataset_id, self.calendar.source_id, self.calendar.instrument_id) != (
            self.dataset_id,
            self.source_id,
            self.instrument_identifier,
        ):
            raise ValueError("calendar is not bound to this exact dataset/source/instrument")
        resolved = self.instrument_registry.resolve(
            self.dataset_id,
            self.source_id,
            self.instrument_identifier,
        )
        if resolved.instrument_id != self.instrument_identifier:
            raise ValueError("instrument registry resolves to a different canonical instrument")
        if (
            resolved.lifecycle.dataset_id,
            resolved.lifecycle.source_id,
            resolved.lifecycle.instrument_id,
        ) != (self.dataset_id, self.source_id, self.instrument_identifier):
            raise ValueError("instrument lifecycle is not bound to this evidence pin")
        if resolved.lifecycle != self.instrument_registry.lifecycles[0]:
            raise ValueError("resolved lifecycle differs from the uniquely registered lifecycle")
        if any(
            isinstance(bound, datetime) or not isinstance(bound, date)
            for bound in (self.publication_scope_start, self.publication_scope_end_exclusive)
        ):
            raise ValueError("publication scope bounds must be date-only")
        if self.publication_scope_start >= self.publication_scope_end_exclusive:
            raise ValueError("publication scope must be a nonempty half-open interval")
        _require_evidence_ref(self.publication_scope_evidence_ref, "publication scope evidence")
        if self.visible_record_set is not None and not isinstance(
            self.visible_record_set, VisibleRecordSetPin
        ):
            raise TypeError("visible_record_set must be a VisibleRecordSetPin")

    @property
    def evidence_sha256(self) -> str:
        """Return a stable fingerprint of this exact operator-attested evidence bundle."""
        lifecycle = self.instrument_registry.lifecycles[0]
        material = {
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "publication_id": self.publication_id,
            "instrument_identifier": self.instrument_identifier,
            "instrument_registry": {
                "schema_version": self.instrument_registry.schema_version,
                "registry_version": self.instrument_registry.registry_version,
                "evidence_ref": self.instrument_registry.evidence_ref,
                "lifecycle": {
                    "schema_version": lifecycle.schema_version,
                    "lifecycle_version": lifecycle.lifecycle_version,
                    "status": LifecycleStatus(lifecycle.status).value,
                    "status_as_of": lifecycle.status_as_of.isoformat(),
                    "status_evidence_ref": lifecycle.status_evidence_ref,
                    "known_start_date": (
                        lifecycle.known_start_date.isoformat()
                        if lifecycle.known_start_date is not None
                        else None
                    ),
                    "start_boundary_unknown": lifecycle.start_boundary_unknown,
                    "start_evidence_ref": lifecycle.start_evidence_ref,
                    "delisted_on": (
                        lifecycle.delisted_on.isoformat()
                        if lifecycle.delisted_on is not None
                        else None
                    ),
                    "delist_evidence_ref": lifecycle.delist_evidence_ref,
                },
            },
            "calendar": {
                "schema_version": self.calendar.schema_version,
                "calendar_id": self.calendar.calendar_id,
                "calendar_version": self.calendar.calendar_version,
                "evidence_ref": self.calendar.evidence_ref,
                "covered_from": self.calendar.covered_from.isoformat(),
                "covered_through": self.calendar.covered_through.isoformat(),
                "open_dates": [day.isoformat() for day in sorted(self.calendar.open_dates)],
                "daily_classification_complete": self.calendar.daily_classification_complete,
            },
            "publication_scope": {
                "start": self.publication_scope_start.isoformat(),
                "end_exclusive": self.publication_scope_end_exclusive.isoformat(),
                "evidence_ref": self.publication_scope_evidence_ref,
            },
            "visible_record_set": (
                {
                    "algorithm": self.visible_record_set.algorithm,
                    "sha256": self.visible_record_set.sha256,
                    "record_count": self.visible_record_set.record_count,
                    "publication_sequence": self.visible_record_set.publication_sequence,
                    "contract_sha256": self.visible_record_set.contract_sha256,
                }
                if self.visible_record_set is not None
                else None
            ),
        }
        canonical = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, init=False)
class DatasetCoverageEvidenceRegistry:
    """Immutable map that resolves evidence by its exact publication pin."""

    _entries: Mapping[tuple[str, str, str], DatasetCoverageEvidence]

    def __init__(self, entries: Sequence[DatasetCoverageEvidence]) -> None:
        """Build an immutable registry while rejecting duplicate evidence pins."""
        if isinstance(entries, (str, bytes)) or len(entries) > MAX_COVERAGE_EVIDENCE_ENTRIES:
            raise CoverageEvidenceConfigurationError
        indexed: dict[tuple[str, str, str], DatasetCoverageEvidence] = {}
        for entry in entries:
            if not isinstance(entry, DatasetCoverageEvidence):
                raise CoverageEvidenceConfigurationError
            key = (entry.dataset_id, entry.source_id, entry.publication_id)
            if key in indexed:
                raise CoverageEvidenceConfigurationError
            indexed[key] = entry
        object.__setattr__(self, "_entries", MappingProxyType(indexed))

    @classmethod
    def empty(cls) -> DatasetCoverageEvidenceRegistry:
        """Return an empty registry so missing optional evidence stays UNKNOWN."""
        return cls(())

    def resolve(
        self, dataset_id: str, source_id: str, publication_id: str
    ) -> DatasetCoverageEvidence | None:
        """Return evidence only for an exact dataset/source/publication triple."""
        return self._entries.get((dataset_id, source_id, publication_id))


def load_dataset_coverage_evidence_registry(
    path: str | Path,
) -> DatasetCoverageEvidenceRegistry:
    """Load a bounded, strict evidence file from an explicit operator-owned path.

    The path must be selected by deployment configuration. The file cannot
    grant API access; the route still checks the separate ``coverage.read``
    authorization before resolving any evidence or reading publication facts.
    """
    try:
        with Path(path).open("rb") as config_file:
            raw = config_file.read(MAX_COVERAGE_EVIDENCE_FILE_BYTES + 1)
        if len(raw) > MAX_COVERAGE_EVIDENCE_FILE_BYTES:
            raise CoverageEvidenceConfigurationError
        decoded = raw.decode("utf-8", errors="strict")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        root = _object(document, {"schema_version", "entries"}, "root")
        if type(root["schema_version"]) is not int or root["schema_version"] not in (1, 2):
            raise ValueError("unsupported coverage evidence schema")
        rows = root["entries"]
        if not isinstance(rows, list) or len(rows) > MAX_COVERAGE_EVIDENCE_ENTRIES:
            raise ValueError("coverage evidence entries are invalid or exceed the row limit")
        entries = tuple(_parse_entry(row, schema_version=root["schema_version"]) for row in rows)
        return DatasetCoverageEvidenceRegistry(entries)
    except CoverageEvidenceConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise CoverageEvidenceConfigurationError from exc


def _parse_entry(raw: object, *, schema_version: int) -> DatasetCoverageEvidence:
    """Parse one exact-pinned index evidence bundle and reject unknown fields."""
    entry_fields = {
        "dataset_id",
        "source_id",
        "publication_id",
        "instrument_registry",
        "calendar",
        "publication_scope",
    }
    if schema_version == 2:
        entry_fields.add("visible_record_set")
    row = _object(
        raw,
        entry_fields,
        "entry",
    )
    dataset_id = _string(row["dataset_id"], "dataset_id", maximum=128)
    source_id = _string(row["source_id"], "source_id", maximum=128)
    publication_id = _string(row["publication_id"], "publication_id", maximum=32)
    if dataset_id not in INDEX_SYMBOL_BY_DATASET_ID or source_id != INDEX_SOURCE_ID:
        raise ValueError("coverage evidence is outside the trusted index source/dataset allowlist")
    if _PUBLICATION_ID_RE.fullmatch(publication_id) is None:
        raise ValueError("publication_id is invalid")
    instrument_id = INDEX_SYMBOL_BY_DATASET_ID[dataset_id]
    registry = _parse_instrument_registry(
        row["instrument_registry"], dataset_id, source_id, instrument_id
    )
    calendar = _parse_calendar(row["calendar"], dataset_id, source_id, instrument_id)
    publication_scope = _object(
        row["publication_scope"],
        {"start", "end_exclusive", "evidence_ref"},
        "publication_scope",
    )
    scope_start = _date(publication_scope["start"], "publication_scope.start")
    scope_end = _date(publication_scope["end_exclusive"], "publication_scope.end_exclusive")
    scope_evidence_ref = _evidence_reference(
        publication_scope["evidence_ref"], "publication_scope.evidence_ref"
    )
    record_set_pin = (
        _parse_visible_record_set(row["visible_record_set"]) if schema_version == 2 else None
    )
    return DatasetCoverageEvidence(
        dataset_id=dataset_id,
        source_id=source_id,
        publication_id=publication_id,
        instrument_identifier=instrument_id,
        instrument_registry=registry,
        calendar=calendar,
        publication_scope_start=scope_start,
        publication_scope_end_exclusive=scope_end,
        publication_scope_evidence_ref=scope_evidence_ref,
        visible_record_set=record_set_pin,
    )


def _parse_visible_record_set(raw: object) -> VisibleRecordSetPin:
    """Parse the exact algorithm and publication/contract pin required by schema 2."""
    row = _object(
        raw,
        {"algorithm", "sha256", "record_count", "publication_sequence", "contract_sha256"},
        "visible_record_set",
    )
    record_count = row["record_count"]
    publication_sequence = row["publication_sequence"]
    if type(record_count) is not int or record_count < 0:
        raise ValueError("visible_record_set.record_count must be a nonnegative integer")
    if type(publication_sequence) is not int or publication_sequence < 1:
        raise ValueError("visible_record_set.publication_sequence must be a positive integer")
    return VisibleRecordSetPin(
        algorithm=_string(row["algorithm"], "visible_record_set.algorithm", maximum=64),
        sha256=_string(row["sha256"], "visible_record_set.sha256", maximum=64),
        record_count=record_count,
        publication_sequence=publication_sequence,
        contract_sha256=_string(
            row["contract_sha256"], "visible_record_set.contract_sha256", maximum=64
        ),
    )


def _parse_instrument_registry(
    raw: object, dataset_id: str, source_id: str, instrument_id: str
) -> InstrumentReferenceRegistry:
    """Construct the single whitelisted lifecycle without accepting free scopes."""
    row = _object(raw, {"registry_version", "evidence_ref", "lifecycle"}, "instrument_registry")
    registry_version = _string(row["registry_version"], "registry_version", maximum=128)
    registry_evidence = _evidence_reference(row["evidence_ref"], "instrument_registry.evidence_ref")
    lifecycle_row = _object(
        row["lifecycle"],
        {
            "lifecycle_version",
            "status",
            "status_as_of",
            "status_evidence_ref",
            "known_start_date",
            "start_boundary_unknown",
            "start_evidence_ref",
            "delisted_on",
            "delist_evidence_ref",
        },
        "instrument_registry.lifecycle",
    )
    known_start = _optional_date(lifecycle_row["known_start_date"], "known_start_date")
    delisted_on = _optional_date(lifecycle_row["delisted_on"], "delisted_on")
    lifecycle = InstrumentLifecycle(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=instrument_id,
        lifecycle_version=_string(
            lifecycle_row["lifecycle_version"], "lifecycle_version", maximum=128
        ),
        status=_string(lifecycle_row["status"], "status", maximum=16),
        status_as_of=_date(lifecycle_row["status_as_of"], "status_as_of"),
        status_evidence_ref=_evidence_reference(
            lifecycle_row["status_evidence_ref"], "status_evidence_ref"
        ),
        known_start_date=known_start,
        start_boundary_unknown=_boolean(
            lifecycle_row["start_boundary_unknown"], "start_boundary_unknown"
        ),
        start_evidence_ref=_optional_evidence_reference(
            lifecycle_row["start_evidence_ref"], "start_evidence_ref"
        ),
        delisted_on=delisted_on,
        delist_evidence_ref=_optional_evidence_reference(
            lifecycle_row["delist_evidence_ref"], "delist_evidence_ref"
        ),
    )
    return InstrumentReferenceRegistry(
        registry_version=registry_version,
        evidence_ref=registry_evidence,
        lifecycles=(lifecycle,),
    )


def _parse_calendar(
    raw: object, dataset_id: str, source_id: str, instrument_id: str
) -> CalendarEvidence:
    """Construct one bounded, exact-scoped daily calendar slice."""
    row = _object(
        raw,
        {
            "calendar_id",
            "calendar_version",
            "evidence_ref",
            "covered_from",
            "covered_through",
            "open_dates",
            "daily_classification_complete",
        },
        "calendar",
    )
    raw_dates = row["open_dates"]
    if not isinstance(raw_dates, list) or len(raw_dates) > MAX_COVERAGE_CALENDAR_DATES:
        raise ValueError("calendar.open_dates is invalid or exceeds the date limit")
    dates = tuple(_date(value, "calendar.open_dates[]") for value in raw_dates)
    if len(set(dates)) != len(dates):
        raise ValueError("calendar.open_dates contains duplicates")
    return CalendarEvidence(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=instrument_id,
        calendar_id=_string(row["calendar_id"], "calendar_id", maximum=128),
        calendar_version=_string(row["calendar_version"], "calendar_version", maximum=128),
        evidence_ref=_evidence_reference(row["evidence_ref"], "calendar.evidence_ref"),
        covered_from=_date(row["covered_from"], "calendar.covered_from"),
        covered_through=_date(row["covered_through"], "calendar.covered_through"),
        open_dates=dates,
        daily_classification_complete=_boolean(
            row["daily_classification_complete"], "daily_classification_complete"
        ),
    )


def _object(value: object, required_keys: set[str], name: str) -> dict[str, object]:
    """Return one JSON object only when its key set exactly matches the schema."""
    if not isinstance(value, dict) or set(value) != required_keys:
        raise ValueError(f"{name} must have the exact declared field set")
    return value


def _string(value: object, name: str, *, maximum: int) -> str:
    """Validate a nonempty, unnormalized, bounded string."""
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be a bounded nonempty string")
    return value


def _date(value: object, name: str) -> date:
    """Parse one canonical ISO date without timestamp or coercion acceptance."""
    raw = _string(value, name, maximum=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return parsed


def _optional_date(value: object, name: str) -> date | None:
    """Parse an optional strict ISO date."""
    return None if value is None else _date(value, name)


def _boolean(value: object, name: str) -> bool:
    """Reject integer truth values and other coercions."""
    if type(value) is not bool:
        raise ValueError(f"{name} must be a JSON boolean")
    return value


def _evidence_reference(value: object, name: str) -> str:
    """Require a path-like evidence anchor or an explicit review ticket."""
    reference = _string(value, name, maximum=512)
    if _TICKET_RE.fullmatch(reference):
        return reference
    if reference.startswith("docs/evidence/"):
        parts = reference.split("/")[1:]
        if (
            parts
            and all(part not in {"", ".", ".."} for part in parts)
            and all(_DOCS_PATH_PART_RE.fullmatch(part) is not None for part in parts)
        ):
            return reference
    raise ValueError(f"{name} must point to docs/evidence or a review ticket")


def _optional_evidence_reference(value: object, name: str) -> str | None:
    """Parse an optional evidence anchor."""
    return None if value is None else _evidence_reference(value, name)


def _require_evidence_ref(value: str, name: str) -> None:
    """Apply the same auditable-reference rule to programmatically injected entries."""
    _evidence_reference(value, name)


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys rather than silently accepting the last value."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject non-standard JSON numeric constants."""
    raise ValueError(f"invalid JSON constant: {value}")


__all__ = [
    "CoverageEvidenceConfigurationError",
    "DatasetCoverageEvidence",
    "DatasetCoverageEvidenceRegistry",
    "MAX_COVERAGE_CALENDAR_DATES",
    "MAX_COVERAGE_EVIDENCE_ENTRIES",
    "load_dataset_coverage_evidence_registry",
]
