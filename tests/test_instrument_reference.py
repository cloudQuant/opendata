"""Behavioral checks for reviewed, versioned instrument identity evidence."""

from datetime import date

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceError,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)


def d(value: str) -> date:
    """Build literal test dates."""
    return date.fromisoformat(value)


def life(instrument_id: str = "XNYS:ABC", **overrides: object) -> InstrumentLifecycle:
    """Build a reviewed active life with an independent listing boundary."""
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": instrument_id,
        "lifecycle_version": "life-2024-01",
        "status": LifecycleStatus.ACTIVE,
        "status_as_of": d("2024-01-10"),
        "status_evidence_ref": "review:active-state",
        "known_start_date": d("2024-01-01"),
        "start_boundary_unknown": False,
        "start_evidence_ref": "exchange:listing-notice",
    }
    values.update(overrides)
    return InstrumentLifecycle(**values)


def alias(
    value: str,
    instrument_id: str,
    start: str | None,
    end: str | None,
    version: str,
) -> InstrumentAlias:
    """Build one reviewed effective-dated source alias."""
    return InstrumentAlias(
        dataset_id="equity_daily",
        source_id="exchange_archive",
        alias=value,
        instrument_id=instrument_id,
        alias_version=version,
        evidence_ref=f"review:{version}",
        valid_from=d(start) if start else None,
        valid_through=d(end) if end else None,
    )


def registry(
    *lifecycles: InstrumentLifecycle, aliases: tuple[InstrumentAlias, ...] = ()
) -> InstrumentReferenceRegistry:
    """Build an immutable reviewed reference snapshot."""
    return InstrumentReferenceRegistry(
        registry_version="reference-2024.1",
        evidence_ref="review:instrument-reference",
        lifecycles=lifecycles,
        aliases=aliases,
    )


def test_canonical_resolution_returns_pinned_registry_and_lifecycle() -> None:
    result = registry(life()).resolve("equity_daily", "exchange_archive", "XNYS:ABC")

    assert result.instrument_id == "XNYS:ABC"
    assert result.registry_version == "reference-2024.1"
    assert result.registry_evidence_ref == "review:instrument-reference"
    assert result.alias_version is None
    assert result.lifecycle.known_start_date == d("2024-01-01")


def test_alias_resolution_returns_its_exact_evidence_and_canonical_identity() -> None:
    mapping = alias("ABC", "XNYS:ABC", "2020-01-01", None, "ticker-v2")
    result = registry(life(), aliases=(mapping,)).resolve(
        "equity_daily", "exchange_archive", "ABC", as_of=d("2024-01-01")
    )

    assert result.input_identifier == "ABC"
    assert result.instrument_id == "XNYS:ABC"
    assert result.alias_version == "ticker-v2"
    assert result.alias_evidence_ref == "review:ticker-v2"


def test_single_alias_version_can_resolve_without_as_of() -> None:
    result = registry(life(), aliases=(alias("ABC", "XNYS:ABC", None, None, "v1"),)).resolve(
        "equity_daily", "exchange_archive", "ABC"
    )

    assert result.instrument_id == "XNYS:ABC"


def test_single_time_scoped_alias_requires_explicit_as_of() -> None:
    snapshot = registry(
        life(), aliases=(alias("ABC", "XNYS:ABC", "2024-01-02", "2024-01-02", "v1"),)
    )

    with pytest.raises(InstrumentReferenceError, match="time-scoped alias"):
        snapshot.resolve("equity_daily", "exchange_archive", "ABC")

    resolved = snapshot.resolve("equity_daily", "exchange_archive", "ABC", as_of=d("2024-01-02"))
    assert resolved.instrument_id == "XNYS:ABC"
    assert resolved.alias_valid_from == d("2024-01-02")
    assert resolved.alias_valid_through == d("2024-01-02")
    assert resolved.alias_resolved_as_of == d("2024-01-02")
    with pytest.raises(InstrumentReferenceError, match="requested as_of"):
        snapshot.resolve("equity_daily", "exchange_archive", "ABC", as_of=d("2024-01-03"))


def test_reused_alias_requires_and_obeys_as_of_boundaries() -> None:
    first = life("XNYS:ABC")
    second = life("XNYS:XYZ")
    aliases = (
        alias("OLD", "XNYS:ABC", "2020-01-01", "2022-12-31", "old-v1"),
        alias("OLD", "XNYS:XYZ", "2023-01-01", None, "new-v1"),
    )
    snapshot = registry(first, second, aliases=aliases)

    with pytest.raises(InstrumentReferenceError, match="explicit as_of"):
        snapshot.resolve("equity_daily", "exchange_archive", "OLD")
    assert (
        snapshot.resolve(
            "equity_daily", "exchange_archive", "OLD", as_of=d("2022-12-31")
        ).instrument_id
        == "XNYS:ABC"
    )
    assert (
        snapshot.resolve(
            "equity_daily", "exchange_archive", "OLD", as_of=d("2023-01-01")
        ).instrument_id
        == "XNYS:XYZ"
    )
    with pytest.raises(InstrumentReferenceError, match="no reviewed registry match"):
        snapshot.resolve("equity_daily", "exchange_archive", "OLD", as_of=d("2019-12-31"))


@pytest.mark.parametrize(
    "rows, message",
    [
        (("not-a-life",), "InstrumentLifecycle"),
        ((life(), life()), "duplicate lifecycle"),
    ],
)
def test_registry_rejects_invalid_or_duplicate_lifecycle_rows(
    rows: tuple[object, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        InstrumentReferenceRegistry("v1", "evidence", rows)  # type: ignore[arg-type]


def test_registry_rejects_dangling_alias() -> None:
    with pytest.raises(ValueError, match="absent from this registry"):
        registry(aliases=(alias("ABC", "XNYS:ABC", None, None, "v1"),))


def test_registry_rejects_overlapping_reused_alias_intervals() -> None:
    aliases = (
        alias("OLD", "XNYS:ABC", "2020-01-01", "2024-01-02", "v1"),
        alias("OLD", "XNYS:XYZ", "2024-01-02", None, "v2"),
    )
    with pytest.raises(ValueError, match="must not overlap"):
        registry(life("XNYS:ABC"), life("XNYS:XYZ"), aliases=aliases)


def test_registry_rejects_canonical_identifier_shadowing() -> None:
    with pytest.raises(ValueError, match="cannot shadow"):
        registry(
            life("XNYS:ABC"),
            life("XNYS:XYZ"),
            aliases=(alias("XNYS:ABC", "XNYS:XYZ", None, None, "v1"),),
        )


def test_registry_alias_validation_uses_bounded_comparison_work() -> None:
    """Guard alias validation against scanning every lifecycle for every alias."""

    class CountingIdentifier(str):
        """Count alias comparisons while retaining ordinary string semantics."""

        comparisons = 0

        def __eq__(self, other: object) -> bool:
            type(self).comparisons += 1
            return str.__eq__(self, other)

        __hash__ = str.__hash__

    size = 1_000
    lifecycles = tuple(life(f"XNYS:{index}") for index in range(size))
    aliases = tuple(
        alias(CountingIdentifier(f"ticker_{index}"), f"XNYS:{index}", None, None, f"v{index}")
        for index in range(size)
    )
    CountingIdentifier.comparisons = 0

    snapshot = registry(*lifecycles, aliases=aliases)

    assert len(snapshot.aliases) == size
    assert CountingIdentifier.comparisons <= size * 10


def test_registry_rejects_iterables_over_the_materialization_limit() -> None:
    """Stop registry construction at the fixed row bound before retaining inputs."""
    from itertools import repeat

    with pytest.raises(ValueError, match="exceeds the 100,000-row limit"):
        InstrumentReferenceRegistry(
            "v1",
            "evidence",
            repeat(life(), 100_001),  # type: ignore[arg-type]
        )


def test_unknown_or_wrong_scope_identifier_fails_closed() -> None:
    snapshot = registry(life())
    with pytest.raises(InstrumentReferenceError):
        snapshot.resolve("other_dataset", "exchange_archive", "XNYS:ABC")
    with pytest.raises(InstrumentReferenceError):
        snapshot.resolve("equity_daily", "other_source", "XNYS:ABC")
    with pytest.raises(InstrumentReferenceError):
        snapshot.resolve("equity_daily", "exchange_archive", "UNKNOWN")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"schema_version": True}, "schema_version"),
        ({"start_boundary_unknown": False, "known_start_date": None}, "known start"),
        ({"start_boundary_unknown": True, "known_start_date": d("2024-01-01")}, "unknown start"),
        ({"status": "delisted"}, "delisted_on"),
        ({"delisted_on": d("2024-01-05")}, "valid only for delisted"),
        ({"status_as_of": "2024-01-10"}, "status_as_of"),
        ({"known_start_date": d("2024-01-06"), "delisted_on": d("2024-01-05")}, "valid only"),
    ],
)
def test_lifecycle_rejects_unversioned_or_inconsistent_boundaries(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": "XNYS:ABC",
        "lifecycle_version": "life-v1",
        "status": "active",
        "status_as_of": d("2024-01-10"),
        "status_evidence_ref": "review:status",
        "known_start_date": d("2024-01-01"),
        "start_boundary_unknown": False,
        "start_evidence_ref": "review:start",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        InstrumentLifecycle(**values)  # type: ignore[arg-type]


def test_delisted_lifecycle_requires_boundary_evidence() -> None:
    with pytest.raises(ValueError, match="delisted_on and delist_evidence_ref"):
        life(status="delisted")
    retired = life(
        status="delisted",
        status_as_of=d("2024-01-10"),
        delisted_on=d("2024-01-05"),
        delist_evidence_ref="exchange:delisting-notice",
    )
    assert retired.status is LifecycleStatus.DELISTED


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"dataset_id": " bad-id"}, "stable identifier"),
        ({"status_evidence_ref": "review:bad\nevidence"}, "evidence reference"),
        ({"status": "suspended"}, "status must be"),
        ({"status_as_of": None}, "status_as_of is required"),
        ({"start_boundary_unknown": 1}, "must be a bool"),
        ({"status_as_of": d("2024-01-04"), "delisted_on": d("2024-01-05")}, "cannot precede"),
    ],
)
def test_lifecycle_rejects_invalid_identity_and_evidence_values(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": "XNYS:ABC",
        "lifecycle_version": "life-v1",
        "status": "active",
        "status_as_of": d("2024-01-10"),
        "status_evidence_ref": "review:status",
        "known_start_date": d("2024-01-01"),
        "start_boundary_unknown": False,
        "start_evidence_ref": "review:start",
    }
    if changes.get("delisted_on") is not None:
        values.update(
            status="delisted",
            delist_evidence_ref="exchange:delisting-notice",
        )
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        InstrumentLifecycle(**values)  # type: ignore[arg-type]


def test_alias_rejects_invalid_effective_interval() -> None:
    with pytest.raises(ValueError, match="on or before"):
        alias("ABC", "XNYS:ABC", "2024-01-02", "2024-01-01", "v1")


def test_registry_rejects_reused_alias_with_unbounded_overlapping_interval() -> None:
    mappings = (
        alias("OLD", "XNYS:ABC", None, None, "v1"),
        alias("OLD", "XNYS:XYZ", "2024-01-01", None, "v2"),
    )

    with pytest.raises(ValueError, match="must not overlap"):
        registry(life("XNYS:ABC"), life("XNYS:XYZ"), aliases=mappings)
