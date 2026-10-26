"""Reviewed, versioned instrument identity, alias, and lifecycle evidence.

This module is deliberately storage- and provider-independent. Callers inject
the reviewed registry they loaded; it never guesses an inception date from
published rows or reaches a database, provider, or environment variable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_MAX_EVIDENCE_CHARS = 512
_MAX_REFERENCE_ROWS = 100_000


class InstrumentReferenceError(ValueError):
    """Raised when a reviewed instrument reference cannot safely be resolved."""


class LifecycleStatus(str, Enum):
    """Reviewed lifecycle state in a versioned instrument registry."""

    ACTIVE = "active"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


def _identifier(value: str, name: str) -> str:
    """Validate a stable identifier without silently normalizing it."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a 1-128 character stable identifier")
    return value


def _evidence_ref(value: str, name: str) -> str:
    """Validate a bounded, nonempty reference to a separately retained review artifact."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_EVIDENCE_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be a bounded nonempty evidence reference")
    return value


def _strict_date(value: date | None, name: str) -> date | None:
    """Accept date values while rejecting timestamps and coercion."""
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a datetime.date")
    return value


def _schema_version(value: int) -> None:
    """Require this module's frozen schema version without accepting bool."""
    if type(value) is not int or value != 1:
        raise ValueError("schema_version must be the integer 1")


def _bounded_tuple(values: Iterable[object], name: str) -> tuple[object, ...]:
    """Materialize a bounded iterable once and reject scalar pseudo-iterables."""
    if isinstance(values, (str, bytes, bytearray, dict)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be a bounded iterable")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a bounded iterable") from exc
    result = tuple(islice(iterator, _MAX_REFERENCE_ROWS + 1))
    if len(result) > _MAX_REFERENCE_ROWS:
        raise ValueError(f"{name} exceeds the {_MAX_REFERENCE_ROWS:,}-row limit")
    return result


@dataclass(frozen=True, slots=True)
class InstrumentLifecycle:
    """One reviewed lifecycle row for a dataset/source/canonical instrument.

    ``known_start_date`` is an independently evidenced business boundary. A
    latest publication row is never a substitute. ``status_as_of`` limits how
    far an active-state attestation can support a coverage assessment.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    lifecycle_version: str
    status: LifecycleStatus | str
    status_as_of: date
    status_evidence_ref: str
    known_start_date: date | None = None
    start_boundary_unknown: bool = True
    start_evidence_ref: str | None = None
    delisted_on: date | None = None
    delist_evidence_ref: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate lifecycle boundaries, status, and evidence references."""
        _schema_version(self.schema_version)
        for name in ("dataset_id", "source_id", "instrument_id", "lifecycle_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        try:
            status = LifecycleStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("status must be active, delisted, or unknown") from exc
        object.__setattr__(self, "status", status)
        status_as_of = _strict_date(self.status_as_of, "status_as_of")
        if status_as_of is None:
            raise ValueError("status_as_of is required")
        object.__setattr__(self, "status_as_of", status_as_of)
        object.__setattr__(
            self,
            "status_evidence_ref",
            _evidence_ref(self.status_evidence_ref, "status_evidence_ref"),
        )

        if type(self.start_boundary_unknown) is not bool:
            raise ValueError("start_boundary_unknown must be a bool")
        known_start = _strict_date(self.known_start_date, "known_start_date")
        object.__setattr__(self, "known_start_date", known_start)
        if self.start_boundary_unknown:
            if known_start is not None or self.start_evidence_ref is not None:
                raise ValueError("unknown start cannot carry a date or start evidence")
        else:
            if known_start is None or self.start_evidence_ref is None:
                raise ValueError("known start requires a date and independent evidence")
            object.__setattr__(
                self,
                "start_evidence_ref",
                _evidence_ref(self.start_evidence_ref, "start_evidence_ref"),
            )

        delisted_on = _strict_date(self.delisted_on, "delisted_on")
        object.__setattr__(self, "delisted_on", delisted_on)
        if status is LifecycleStatus.DELISTED:
            if delisted_on is None or self.delist_evidence_ref is None:
                raise ValueError("delisted status requires delisted_on and delist_evidence_ref")
            if status_as_of < delisted_on:
                raise ValueError("status_as_of cannot precede delisted_on")
            object.__setattr__(
                self,
                "delist_evidence_ref",
                _evidence_ref(self.delist_evidence_ref, "delist_evidence_ref"),
            )
        elif delisted_on is not None or self.delist_evidence_ref is not None:
            raise ValueError("delist evidence is valid only for delisted status")
        if known_start is not None and delisted_on is not None and known_start > delisted_on:
            raise ValueError("known_start_date cannot follow delisted_on")


@dataclass(frozen=True, slots=True)
class InstrumentAlias:
    """A source- and dataset-scoped alias valid over an inclusive date interval."""

    dataset_id: str
    source_id: str
    alias: str
    instrument_id: str
    alias_version: str
    evidence_ref: str
    valid_from: date | None = None
    valid_through: date | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate alias scope, version, effective interval, and provenance."""
        _schema_version(self.schema_version)
        for name in ("dataset_id", "source_id", "alias", "instrument_id", "alias_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "evidence_ref", _evidence_ref(self.evidence_ref, "evidence_ref"))
        valid_from = _strict_date(self.valid_from, "valid_from")
        valid_through = _strict_date(self.valid_through, "valid_through")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_through", valid_through)
        if valid_from is not None and valid_through is not None and valid_from > valid_through:
            raise ValueError("valid_from must be on or before valid_through")


@dataclass(frozen=True, slots=True)
class ResolvedInstrument:
    """A canonical identity plus the exact registry and alias evidence used."""

    input_identifier: str
    dataset_id: str
    source_id: str
    instrument_id: str
    registry_version: str
    registry_evidence_ref: str
    alias_version: str | None
    alias_evidence_ref: str | None
    alias_valid_from: date | None
    alias_valid_through: date | None
    alias_resolved_as_of: date | None
    lifecycle: InstrumentLifecycle


@dataclass(frozen=True, slots=True)
class InstrumentReferenceRegistry:
    """Immutable reviewed registry used to resolve identifiers and lifecycle.

    The registry is injected into consumers. This class does not load files,
    query the warehouse, or promote unreviewed observations into evidence.
    """

    registry_version: str
    evidence_ref: str
    lifecycles: tuple[InstrumentLifecycle, ...]
    aliases: tuple[InstrumentAlias, ...] = ()
    schema_version: int = 1
    _lifecycles_by_key: Mapping[tuple[str, str, str], InstrumentLifecycle] = field(
        init=False, repr=False, compare=False
    )
    _aliases_by_key: Mapping[tuple[str, str, str], tuple[InstrumentAlias, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Freeze bounded registry rows and reject ambiguous or dangling aliases."""
        _schema_version(self.schema_version)
        object.__setattr__(
            self,
            "registry_version",
            _identifier(self.registry_version, "registry_version"),
        )
        object.__setattr__(self, "evidence_ref", _evidence_ref(self.evidence_ref, "evidence_ref"))
        lifecycle_objects = _bounded_tuple(self.lifecycles, "lifecycles")
        alias_objects = _bounded_tuple(self.aliases, "aliases")
        if any(not isinstance(row, InstrumentLifecycle) for row in lifecycle_objects):
            raise ValueError("lifecycles must contain InstrumentLifecycle rows")
        if any(not isinstance(row, InstrumentAlias) for row in alias_objects):
            raise ValueError("aliases must contain InstrumentAlias rows")
        lifecycle_rows: tuple[InstrumentLifecycle, ...] = tuple(
            row for row in lifecycle_objects if isinstance(row, InstrumentLifecycle)
        )
        alias_rows: tuple[InstrumentAlias, ...] = tuple(
            row for row in alias_objects if isinstance(row, InstrumentAlias)
        )
        lifecycle_keys = [
            (row.dataset_id, row.source_id, row.instrument_id) for row in lifecycle_rows
        ]
        if len(lifecycle_keys) != len(set(lifecycle_keys)):
            raise ValueError("registry contains duplicate lifecycle identities")
        lifecycle_by_key = {
            (row.dataset_id, row.source_id, row.instrument_id): row for row in lifecycle_rows
        }
        canonical_ids_by_scope: dict[tuple[str, str], set[str]] = {}
        for row in lifecycle_rows:
            scope = (row.dataset_id, row.source_id)
            canonical_ids_by_scope.setdefault(scope, set()).add(row.instrument_id)

        aliases_by_key: dict[tuple[str, str, str], list[InstrumentAlias]] = {}
        for alias_row in alias_rows:
            lifecycle_key = (alias_row.dataset_id, alias_row.source_id, alias_row.instrument_id)
            if lifecycle_key not in lifecycle_by_key:
                raise ValueError("alias points to a lifecycle absent from this registry")
            scope = (alias_row.dataset_id, alias_row.source_id)
            canonical_ids = canonical_ids_by_scope[scope]
            if alias_row.alias in canonical_ids and alias_row.alias != alias_row.instrument_id:
                raise ValueError("alias cannot shadow another canonical instrument identifier")
            alias_key = (alias_row.dataset_id, alias_row.source_id, alias_row.alias)
            aliases_by_key.setdefault(alias_key, []).append(alias_row)
        self._validate_alias_intervals(alias_rows)
        object.__setattr__(self, "lifecycles", tuple(lifecycle_rows))
        object.__setattr__(self, "aliases", tuple(alias_rows))
        object.__setattr__(self, "_lifecycles_by_key", MappingProxyType(lifecycle_by_key))
        object.__setattr__(
            self,
            "_aliases_by_key",
            MappingProxyType({key: tuple(rows) for key, rows in aliases_by_key.items()}),
        )

    @staticmethod
    def _validate_alias_intervals(aliases: tuple[InstrumentAlias, ...]) -> None:
        """Reject same-scope alias intervals that could resolve to two identities."""
        grouped: dict[tuple[str, str, str], list[InstrumentAlias]] = {}
        for item in aliases:
            key = (item.dataset_id, item.source_id, item.alias)
            grouped.setdefault(key, []).append(item)
        for rows in grouped.values():
            ordered = sorted(rows, key=lambda item: item.valid_from or date.min)
            for left, right in zip(ordered, ordered[1:], strict=False):
                if left.valid_through is None or right.valid_from is None:
                    overlaps = True
                else:
                    overlaps = left.valid_through >= right.valid_from
                if overlaps:
                    raise ValueError("same-scope alias validity intervals must not overlap")

    def resolve(
        self,
        dataset_id: str,
        source_id: str,
        identifier: str,
        *,
        as_of: date | None = None,
    ) -> ResolvedInstrument:
        """Resolve a canonical identity or effective-dated alias.

        Args:
            dataset_id: Dataset whose provider-specific identity is requested.
            source_id: Source whose identifiers the input uses.
            identifier: Canonical instrument ID or reviewed alias.
            as_of: Required for time-scoped or reused aliases. It selects the
                canonical identity at this point in time; it does not restrict
                the canonical instrument's historical query interval.

        Returns:
            The canonical instrument and exact registry evidence references.

        Raises:
            InstrumentReferenceError: When no unique reviewed identity exists.
        """
        dataset = _identifier(dataset_id, "dataset_id")
        source = _identifier(source_id, "source_id")
        supplied = _identifier(identifier, "identifier")
        effective = _strict_date(as_of, "as_of")
        lifecycle = self._lifecycles_by_key.get((dataset, source, supplied))
        if lifecycle is not None:
            return ResolvedInstrument(
                supplied,
                dataset,
                source,
                lifecycle.instrument_id,
                self.registry_version,
                self.evidence_ref,
                None,
                None,
                None,
                None,
                None,
                lifecycle,
            )

        alias_rows = self._aliases_by_key.get((dataset, source, supplied), ())
        if not alias_rows:
            raise InstrumentReferenceError("instrument identifier has no reviewed registry match")
        if effective is None:
            if len(alias_rows) > 1:
                raise InstrumentReferenceError("reused alias requires an explicit as_of date")
            alias = alias_rows[0]
            if alias.valid_from is not None or alias.valid_through is not None:
                raise InstrumentReferenceError("time-scoped alias requires an explicit as_of date")
        else:
            candidates = [
                row
                for row in alias_rows
                if (row.valid_from is None or effective >= row.valid_from)
                and (row.valid_through is None or effective <= row.valid_through)
            ]
            if not candidates:
                raise InstrumentReferenceError(
                    "instrument identifier has no reviewed registry match at the requested as_of"
                )
            if len(candidates) != 1:
                raise InstrumentReferenceError("reused alias requires an explicit as_of date")
            alias = candidates[0]
        lifecycle = self._lifecycles_by_key[(dataset, source, alias.instrument_id)]
        return ResolvedInstrument(
            supplied,
            dataset,
            source,
            lifecycle.instrument_id,
            self.registry_version,
            self.evidence_ref,
            alias.alias_version,
            alias.evidence_ref,
            alias.valid_from,
            alias.valid_through,
            effective,
            lifecycle,
        )


__all__ = [
    "InstrumentAlias",
    "InstrumentLifecycle",
    "InstrumentReferenceError",
    "InstrumentReferenceRegistry",
    "LifecycleStatus",
    "ResolvedInstrument",
]
