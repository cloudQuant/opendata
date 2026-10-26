"""Golden and rejection tests for the offline instrument-reference publisher."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from opendata.data.dataset_contract_registry import canonical_sha256
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_publication import (
    InstrumentPresentation,
    build_instrument_reference_contract,
    build_instrument_reference_records,
)

_DATASET_ID = "fixture.instrument-reference.stock"
_SOURCE_ID = "fixture.reference"
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "afi204" / "instrument_reference_v1.json"
_FIXTURE_SHA256 = "bc891b8abaa1f332bf6f1c08ee4737bdada68e10a848f30866dd43dbbe215f72"


def _date(value: str) -> date:
    """Build an explicit test date from its ISO representation."""
    return date.fromisoformat(value)


def _beta_lifecycle() -> InstrumentLifecycle:
    """Return the synthetic Beta lifecycle with its start boundary unknown."""
    return InstrumentLifecycle(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        instrument_id="TEST_XSHE_B",
        lifecycle_version="fixture-lifecycle-1",
        status=LifecycleStatus.UNKNOWN,
        status_as_of=_date("2026-10-22"),
        status_evidence_ref="TEST-204-STATUS",
        known_start_date=None,
        start_boundary_unknown=True,
        start_evidence_ref=None,
        delisted_on=None,
        delist_evidence_ref=None,
    )


def _alpha_lifecycle() -> InstrumentLifecycle:
    """Return the synthetic Alpha lifecycle with an independent start date."""
    return InstrumentLifecycle(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        instrument_id="TEST_XSHG_A",
        lifecycle_version="fixture-lifecycle-1",
        status=LifecycleStatus.ACTIVE,
        status_as_of=_date("2026-10-22"),
        status_evidence_ref="TEST-204-STATUS",
        known_start_date=_date("1991-01-01"),
        start_boundary_unknown=False,
        start_evidence_ref="TEST-204-START",
        delisted_on=None,
        delist_evidence_ref=None,
    )


def _base_aliases() -> tuple[InstrumentAlias, ...]:
    """Return the two adjacent, inclusive synthetic alias intervals."""
    return (
        InstrumentAlias(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            alias="HIST",
            instrument_id="TEST_XSHE_B",
            alias_version="fixture-alias-1",
            evidence_ref="TEST-204-ALIAS",
            valid_from=_date("2020-01-01"),
            valid_through=None,
        ),
        InstrumentAlias(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            alias="HIST",
            instrument_id="TEST_XSHG_A",
            alias_version="fixture-alias-1",
            evidence_ref="TEST-204-ALIAS",
            valid_from=_date("2010-01-01"),
            valid_through=_date("2019-12-31"),
        ),
    )


def _registry(
    *,
    lifecycles: tuple[InstrumentLifecycle, ...] | None = None,
    aliases: tuple[InstrumentAlias, ...] | None = None,
    dataset_id: str = _DATASET_ID,
    source_id: str = _SOURCE_ID,
    evidence_ref: str = "TEST-204-REGISTRY",
) -> InstrumentReferenceRegistry:
    """Build a complete explicit registry fixture for publisher behavior tests."""
    return InstrumentReferenceRegistry(
        registry_version="fixture-registry-1",
        evidence_ref=evidence_ref,
        lifecycles=((_beta_lifecycle(), _alpha_lifecycle()) if lifecycles is None else lifecycles),
        aliases=_base_aliases() if aliases is None else aliases,
    )


def _presentations() -> tuple[InstrumentPresentation, ...]:
    """Return reviewed synthetic CN stock symbols with explicit exchanges."""
    return (
        InstrumentPresentation(
            instrument_id="TEST_XSHE_B",
            symbol="000001",
            name="SYNTHETIC Beta",
            market="CN",
            exchange="XSHE",
        ),
        InstrumentPresentation(
            instrument_id="TEST_XSHG_A",
            symbol="600001",
            name="SYNTHETIC Alpha",
            market="CN",
            exchange="XSHG",
        ),
    )


def _fixture() -> dict[str, object]:
    """Load the shared, hash-pinned synthetic protocol fixture."""
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _records(
    registry: InstrumentReferenceRegistry | None = None,
    presentations: tuple[InstrumentPresentation, ...] | None = None,
):
    """Build the test publication from explicit fixture models."""
    return build_instrument_reference_records(
        registry=_registry() if registry is None else registry,
        presentations=_presentations() if presentations is None else presentations,
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
    )


def test_shared_protocol_fixture_is_the_frozen_synthetic_input() -> None:
    """The checked-in fixture must remain the root-frozen synthetic contract."""
    assert sha256(_FIXTURE_PATH.read_bytes()).hexdigest() == _FIXTURE_SHA256
    assert _fixture()["fixture_kind"] == (
        "SYNTHETIC_PROTOCOL; not live/source/rights/production evidence"
    )


def test_contract_matches_every_fixture_field_and_the_golden_hash() -> None:
    """The builder returns the exact 19-field table/none contract and frozen hash."""
    fixture = _fixture()
    contract = build_instrument_reference_contract(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        source="synthetic-fixture",
    )

    assert contract.model_dump(mode="json") == fixture["contract"]
    assert canonical_sha256(contract) == fixture["contract_sha256"]
    assert contract.record_kind == "table"
    assert contract.temporal_semantics == "none"
    assert contract.time_field is None
    assert contract.pit_policy == "unsupported"
    assert len(contract.fields) == 19


def test_records_match_each_golden_business_key_payload_and_null_time_field() -> None:
    """The explicit registry projects to the fixture rows without invented time."""
    fixture = _fixture()
    contract = build_instrument_reference_contract(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        source="synthetic-fixture",
    )
    records = _records()
    actual = [
        {
            "dataset_id": contract.dataset_id,
            "business_key": record.business_key,
            "business_date": record.business_date.isoformat()
            if record.business_date is not None
            else None,
            "event_at": record.event_at,
            "observed_at": record.observed_at,
            "source_published_at": record.source_published_at,
            "available_at": record.available_at,
            "payload": dict(record.payload),
        }
        for record in records
    ]
    expected = [
        {
            field: row[field]
            for field in (
                "dataset_id",
                "business_key",
                "business_date",
                "event_at",
                "observed_at",
                "source_published_at",
                "available_at",
                "payload",
            )
        }
        for row in fixture["rows"]
    ]

    assert actual == expected
    assert [record.business_key for record in records] == [
        '["TEST_XSHE_B"]',
        '["TEST_XSHG_A"]',
    ]
    assert all(record.business_date is None for record in records)
    assert all(record.event_at is None for record in records)
    assert all(record.observed_at is None for record in records)
    assert all(record.source_published_at is None for record in records)
    assert all(record.available_at is None for record in records)


def test_record_order_is_stable_when_registry_alias_and_presentation_order_changes() -> None:
    """The output order and alias arrays do not depend on iterable input order."""
    original_registry = _registry()
    reversed_registry = _registry(
        lifecycles=tuple(reversed(original_registry.lifecycles)),
        aliases=tuple(reversed(original_registry.aliases)),
    )

    assert _records(reversed_registry, tuple(reversed(_presentations()))) == _records(
        original_registry, _presentations()
    )


def test_delisted_lifecycle_and_independent_evidence_are_preserved() -> None:
    """Delisted evidence is published literally without inventing a date field."""
    lifecycle = InstrumentLifecycle(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        instrument_id="TEST_XSHE_DELISTED",
        lifecycle_version="fixture-lifecycle-delisted",
        status=LifecycleStatus.DELISTED,
        status_as_of=_date("2026-10-22"),
        status_evidence_ref="TEST-204-STATUS-DELISTED",
        known_start_date=_date("2001-01-01"),
        start_boundary_unknown=False,
        start_evidence_ref="TEST-204-START-DELISTED",
        delisted_on=_date("2025-01-02"),
        delist_evidence_ref="TEST-204-DELISTED",
    )
    registry = _registry(lifecycles=(lifecycle,), aliases=())
    presentation = InstrumentPresentation(
        instrument_id="TEST_XSHE_DELISTED",
        symbol="000002",
        name="SYNTHETIC Delisted",
        market="CN",
        exchange="XSHE",
    )

    record = _records(registry, (presentation,))[0]

    assert record.payload["status"] == "delisted"
    assert record.payload["status_evidence_ref"] == "TEST-204-STATUS-DELISTED"
    assert record.payload["known_start_date"] == "2001-01-01"
    assert record.payload["start_evidence_ref"] == "TEST-204-START-DELISTED"
    assert record.payload["delisted_on"] == "2025-01-02"
    assert record.payload["delist_evidence_ref"] == "TEST-204-DELISTED"
    assert record.business_date is None


def test_alias_model_keeps_inclusive_adjacent_date_boundaries() -> None:
    """The source alias model resolves each inclusive side of the version change."""
    registry = _registry()

    before = registry.resolve(_DATASET_ID, _SOURCE_ID, "HIST", as_of=_date("2019-12-31"))
    after = registry.resolve(_DATASET_ID, _SOURCE_ID, "HIST", as_of=_date("2020-01-01"))

    assert before.instrument_id == "TEST_XSHG_A"
    assert before.alias_valid_through == _date("2019-12-31")
    assert after.instrument_id == "TEST_XSHE_B"
    assert after.alias_valid_from == _date("2020-01-01")


def test_builder_rejects_lifecycle_rows_outside_the_requested_scope() -> None:
    """A valid registry cannot be projected across dataset or source boundaries."""
    wrong_scope = _registry(
        lifecycles=(replace(_beta_lifecycle(), source_id="fixture.other"),),
        aliases=(),
    )

    with pytest.raises(ValueError, match="lifecycle.*scope"):
        _records(wrong_scope, (_presentations()[0],))


def test_builder_requires_exactly_one_presentation_for_each_lifecycle() -> None:
    """Missing and extra AFI presentation rows fail closed."""
    beta = _presentations()[0]
    extra = InstrumentPresentation(
        instrument_id="TEST_EXTRA",
        symbol="999999",
        name="SYNTHETIC Extra",
        market="CN",
        exchange="XSHE",
    )

    with pytest.raises(ValueError, match="exactly"):
        _records(presentations=(beta,))
    with pytest.raises(ValueError, match="exactly"):
        _records(presentations=(*_presentations(), extra))


def test_presentation_requires_six_digit_symbol_cn_market_and_explicit_exchange() -> None:
    """The publication never derives exchange or accepts a non-CN stock symbol."""
    with pytest.raises(ValueError, match="six decimal digits"):
        InstrumentPresentation("TEST_XSHG_A", "60001", "Alpha", "CN", "XSHG")
    with pytest.raises(ValueError, match="market"):
        InstrumentPresentation("TEST_XSHG_A", "600001", "Alpha", "SH", "XSHG")
    with pytest.raises(ValueError, match="exchange"):
        InstrumentPresentation("TEST_XSHG_A", "600001", "Alpha", "CN", "")


def test_presentation_accepts_xbse_as_an_explicit_exchange() -> None:
    """The frozen CN profile permits XBSE without inferring it from a symbol."""
    presentation = InstrumentPresentation("TEST_XBSE_A", "600001", "SYNTHETIC XBSE", "CN", "XBSE")

    assert presentation.exchange == "XBSE"


def test_presentation_name_accepts_128_characters_and_rejects_129() -> None:
    """The frozen presentation contract caps printable names at 128 characters."""
    accepted = InstrumentPresentation(
        "TEST_NAME_BOUNDARY",
        "600001",
        "N" * 128,
        "CN",
        "XSHG",
    )

    assert len(accepted.name) == 128
    with pytest.raises(ValueError, match="name must be"):
        InstrumentPresentation("TEST_NAME_BOUNDARY", "600001", "N" * 129, "CN", "XSHG")


def test_builder_rejects_duplicate_or_cross_target_presentation_symbols() -> None:
    """Symbols remain unique and cannot shadow another canonical identity."""
    beta, alpha = _presentations()
    duplicate = replace(alpha, symbol=beta.symbol)
    with pytest.raises(ValueError, match="symbols must be unique"):
        _records(presentations=(beta, duplicate))

    numeric_lifecycle = InstrumentLifecycle(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        instrument_id="000001",
        lifecycle_version="fixture-lifecycle-1",
        status=LifecycleStatus.UNKNOWN,
        status_as_of=_date("2026-10-22"),
        status_evidence_ref="TEST-204-STATUS",
    )
    numeric_registry = _registry(
        lifecycles=(numeric_lifecycle, _alpha_lifecycle()),
        aliases=(),
    )
    with pytest.raises(ValueError, match="another canonical ID"):
        _records(
            numeric_registry,
            (
                InstrumentPresentation("000001", "000002", "SYNTHETIC Numeric", "CN", "XSHE"),
                InstrumentPresentation("TEST_XSHG_A", "000001", "SYNTHETIC Alpha", "CN", "XSHG"),
            ),
        )


def test_builder_rejects_alias_that_collides_with_another_instrument_symbol() -> None:
    """An alias cannot make one AFI query symbol resolve to two canonical IDs."""
    conflicting = InstrumentAlias(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        alias="600001",
        instrument_id="TEST_XSHE_B",
        alias_version="fixture-alias-2",
        evidence_ref="TEST-204-ALIAS-2",
    )
    registry = _registry(aliases=(*_base_aliases(), conflicting))

    with pytest.raises(ValueError, match="conflicts with another instrument"):
        _records(registry)


def test_alias_count_is_bounded_at_sixteen_per_instrument() -> None:
    """Sixteen aliases publish; a seventeenth alias is rejected."""
    extras = tuple(
        InstrumentAlias(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            alias=f"ALT{index:02d}",
            instrument_id="TEST_XSHE_B",
            alias_version=f"fixture-alias-{index + 2}",
            evidence_ref=f"TEST-204-ALIAS-{index + 2}",
        )
        for index in range(16)
    )
    accepted_registry = _registry(aliases=(*_base_aliases(), *extras[:15]))
    rejected_registry = _registry(aliases=(*_base_aliases(), *extras))

    accepted = _records(accepted_registry)
    assert len(accepted[0].payload["aliases"]) == 16
    with pytest.raises(ValueError, match="more than 16 aliases"):
        _records(rejected_registry)


def test_builder_rejects_nonopaque_or_nonascii_evidence_references() -> None:
    """Evidence fields accept opaque IDs, never source URLs or free-form text."""
    url_reference = _registry(evidence_ref="https://source.example/reference")
    nonascii_lifecycle = replace(_alpha_lifecycle(), status_evidence_ref="状态证据")
    nonascii_reference = _registry(
        lifecycles=(_beta_lifecycle(), nonascii_lifecycle),
    )

    with pytest.raises(ValueError, match="opaque ASCII"):
        _records(url_reference)
    with pytest.raises(ValueError, match="opaque ASCII"):
        _records(nonascii_reference)


def test_registry_rejects_invalid_lifecycle_and_overlapping_alias_evidence() -> None:
    """Contradictory lifecycle claims and inclusive alias overlaps cannot publish."""
    with pytest.raises(ValueError, match="unknown start"):
        InstrumentLifecycle(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            instrument_id="TEST_INVALID",
            lifecycle_version="fixture-lifecycle-1",
            status=LifecycleStatus.UNKNOWN,
            status_as_of=_date("2026-10-22"),
            status_evidence_ref="TEST-204-STATUS",
            known_start_date=_date("1991-01-01"),
            start_boundary_unknown=True,
        )

    overlap = InstrumentAlias(
        dataset_id=_DATASET_ID,
        source_id=_SOURCE_ID,
        alias="HIST",
        instrument_id="TEST_XSHE_B",
        alias_version="fixture-alias-overlap",
        evidence_ref="TEST-204-ALIAS-OVERLAP",
        valid_from=_date("2019-12-31"),
        valid_through=None,
    )
    with pytest.raises(ValueError, match="must not overlap"):
        _registry(aliases=(*_base_aliases(), overlap))


def test_builder_enforces_the_twenty_thousand_lifecycle_limit() -> None:
    """The profile cap is checked before presentation rows are materialized."""
    lifecycles = tuple(
        InstrumentLifecycle(
            dataset_id=_DATASET_ID,
            source_id=_SOURCE_ID,
            instrument_id=f"TEST:{index:05d}",
            lifecycle_version="fixture-lifecycle-1",
            status=LifecycleStatus.UNKNOWN,
            status_as_of=_date("2026-10-22"),
            status_evidence_ref="TEST-204-STATUS",
        )
        for index in range(20_001)
    )
    registry = _registry(lifecycles=lifecycles, aliases=())

    with pytest.raises(ValueError, match="20,000-row profile limit"):
        _records(registry, ())
