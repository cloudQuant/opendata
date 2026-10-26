"""Behavior tests for explicit, immutable, versioned dataset contract loading."""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest

from opendata.data.dataset_contract_registry import (
    DatasetContractConflictError,
    DatasetContractNotFoundError,
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
    load_dataset_contract_registry,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity

if TYPE_CHECKING:
    from pathlib import Path


def make_contract(
    *,
    schema_version: str = "1.0.0",
    semantic_version: str = "prices-v1",
    unit: str = "USD/share",
) -> DatasetContract:
    """Build a small explicitly declared dataset contract for registry tests."""
    return DatasetContract(
        dataset_id="sample_prices",
        schema_version=schema_version,
        semantic_version=semantic_version,
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        filter_fields=("instrument_id",),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id="sample_prices", source="fixture_provider"),
        field_units={"close": unit},
        field_currencies={"close": "USD"},
        currency="USD",
        adjustment_basis="raw",
        field_adjustments={"close": "raw"},
        calendar_id="NYSE",
        timezone="America/New_York",
        pit_policy="supported",
    )


def write_registry(path: Path, contracts: list[dict[str, object]]) -> None:
    """Write one canonical version-1 registry document for loader tests."""
    path.write_text(
        json.dumps(
            {"registry_version": 1, "contracts": contracts},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def test_registry_resolves_multiple_schema_and_semantic_versions() -> None:
    """A dataset can retain old semantic versions beside its current contract."""
    old = make_contract(schema_version="1.0.0", semantic_version="prices-v1")
    current = make_contract(schema_version="2.0.0", semantic_version="prices-v2")
    registry = DatasetContractRegistry((old, current))

    assert len(registry) == 2
    assert registry.resolve("sample_prices", "1.0.0", "prices-v1") == old
    assert registry.resolve("sample_prices", "2.0.0", "prices-v2") == current
    with pytest.raises(DatasetContractNotFoundError):
        registry.resolve("sample_prices", "2.0.0", "prices-v1")
    with pytest.raises(DatasetContractNotFoundError):
        registry.resolve("unregistered", "1.0.0", "prices-v1")


def test_registry_deduplicates_identical_contracts_and_rejects_conflicts() -> None:
    """Repeated identical versions are harmless but conflicting payloads fail closed."""
    contract = make_contract()
    assert len(DatasetContractRegistry((contract, contract))) == 1

    conflicting = make_contract(unit="EUR/share")
    with pytest.raises(DatasetContractConflictError):
        DatasetContractRegistry((contract, conflicting))


def test_registry_requires_explicit_contract_models_and_strict_serialization() -> None:
    """Registry construction rejects guessed string inputs and mutated invalid models."""
    with pytest.raises(DatasetContractRegistryError, match="sequence of DatasetContract"):
        DatasetContractRegistry("sample_prices")  # type: ignore[arg-type]
    with pytest.raises(DatasetContractRegistryError, match="only DatasetContract"):
        DatasetContractRegistry((object(),))  # type: ignore[arg-type]

    mutated = make_contract()
    mutated.field_units["close"] = object()  # type: ignore[assignment]
    with pytest.raises(DatasetContractRegistryError, match="strict JSON"):
        DatasetContractRegistry((mutated,))


def test_registry_detaches_input_and_returns_immutable_mapping_copies() -> None:
    """Mutating caller-owned or resolved dictionaries cannot alter registry state."""
    original = make_contract()
    registry = DatasetContractRegistry((original,))
    original.field_units["close"] = "EUR/share"

    resolved = registry.resolve("sample_prices", "1.0.0", "prices-v1")
    assert resolved.field_units["close"] == "USD/share"
    resolved.field_units["close"] = "GBP/share"
    assert (
        registry.resolve("sample_prices", "1.0.0", "prices-v1").field_units["close"] == "USD/share"
    )

    with pytest.raises(TypeError):
        registry.contracts[("new", "1", "1")] = resolved  # type: ignore[index]
    exposed = registry.contracts[("sample_prices", "1.0.0", "prices-v1")]
    exposed.field_units["close"] = "JPY/share"
    assert (
        registry.resolve("sample_prices", "1.0.0", "prices-v1").field_units["close"] == "USD/share"
    )


def test_canonical_hash_is_stable_and_tracks_any_contract_change() -> None:
    """Canonical contract hashes ignore mapping insertion order and bind semantics."""
    contract = make_contract()
    rebuilt = DatasetContract.model_validate(contract.model_dump())
    changed = contract.model_copy(update={"semantic_version": "prices-v2"})

    assert canonical_sha256(contract) == canonical_sha256(rebuilt)
    assert canonical_sha256(contract) != canonical_sha256(changed)
    with pytest.raises(TypeError, match="DatasetContract"):
        canonical_sha256({})  # type: ignore[arg-type]


def test_loader_reads_explicit_bounded_registry(tmp_path: Path) -> None:
    """The strict JSON loader preserves exact versions without choosing defaults."""
    contract = make_contract()
    path = tmp_path / "contracts.json"
    write_registry(path, [contract.model_dump(mode="json")])

    registry = load_dataset_contract_registry(path)
    assert registry.resolve("sample_prices", "1.0.0", "prices-v1") == contract
    assert canonical_sha256(registry.resolve("sample_prices", "1.0.0", "prices-v1")) == (
        canonical_sha256(contract)
    )


def test_loader_rejects_duplicate_json_properties_and_nonfinite_constants(
    tmp_path: Path,
) -> None:
    """Duplicate object keys and NaN/Infinity extensions are rejected by the parser."""
    path = tmp_path / "contracts.json"
    path.write_text('{"registry_version":1,"registry_version":1,"contracts":[]}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="duplicate key"):
        load_dataset_contract_registry(path)

    path.write_text('{"registry_version":1,"contracts":[],"extension":NaN}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="invalid number NaN"):
        load_dataset_contract_registry(path)

    path.write_text('{"registry_version":1,"contracts":[],"extension":1e999}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="non-finite"):
        load_dataset_contract_registry(path)


def test_loader_rejects_invalid_root_versions_unknown_fields_and_invalid_contracts(
    tmp_path: Path,
) -> None:
    """Unknown fields and unrecognized registry formats are not silently ignored."""
    path = tmp_path / "contracts.json"
    path.write_text('{"registry_version":true,"contracts":[]}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="version must be integer 1"):
        load_dataset_contract_registry(path)

    path.write_text('{"registry_version":1,"contracts":[],"fallback":true}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="requires only"):
        load_dataset_contract_registry(path)

    path.write_text('{"registry_version":1,"contracts":{}}', encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="must be an array"):
        load_dataset_contract_registry(path)

    invalid_contract = make_contract().model_dump(mode="json")
    invalid_contract["unrecognized"] = "must fail"
    write_registry(path, [invalid_contract])
    with pytest.raises(DatasetContractRegistryError, match="index 0 is invalid"):
        load_dataset_contract_registry(path)


def test_loader_rejects_invalid_utf8_bom_unbounded_depth_and_oversized_file(
    tmp_path: Path,
) -> None:
    """Input decoding, nesting, and file-size limits are enforced before use."""
    path = tmp_path / "contracts.json"
    path.write_bytes(b"\xff")
    with pytest.raises(DatasetContractRegistryError, match="strict UTF-8 JSON"):
        load_dataset_contract_registry(path)

    path.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(DatasetContractRegistryError, match="strict UTF-8 JSON"):
        load_dataset_contract_registry(path)

    path.write_text(
        '{"registry_version":1,"contracts":[],"extension":' + "[" * 34 + "0" + "]" * 34 + "}",
        encoding="utf-8",
    )
    with pytest.raises(DatasetContractRegistryError, match="depth 32"):
        load_dataset_contract_registry(path)

    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(DatasetContractRegistryError, match="1 MiB limit"):
        load_dataset_contract_registry(path)


def test_loader_rejects_nonobject_root_and_excessive_node_count(tmp_path: Path) -> None:
    """Parsed JSON roots and aggregate node budgets are validated before contracts."""
    path = tmp_path / "contracts.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(DatasetContractRegistryError, match="root must be an object"):
        load_dataset_contract_registry(path)

    path.write_text(
        '{"registry_version":1,"contracts":[],"extension":['
        + ",".join("null" for _ in range(100_001))
        + "]}",
        encoding="utf-8",
    )
    with pytest.raises(DatasetContractRegistryError, match="100000 nodes"):
        load_dataset_contract_registry(path)


def test_resolve_reports_only_registered_version_corruption_as_registry_error() -> None:
    """Unexpected corrupted storage fails as a platform registry error."""
    contract = make_contract()
    registry = DatasetContractRegistry((contract,))
    key = (contract.dataset_id, contract.schema_version, contract.semantic_version)
    object.__setattr__(registry, "_payloads", MappingProxyType({key: ("{}", "0" * 64)}))
    with pytest.raises(DatasetContractRegistryError, match="stored dataset contract is invalid"):
        registry.resolve(*key)


def test_loader_rejects_missing_file_and_duplicate_version_payloads(tmp_path: Path) -> None:
    """Unreadable paths and conflicting duplicate triple identities are explicit errors."""
    with pytest.raises(DatasetContractRegistryError, match="unreadable"):
        load_dataset_contract_registry(tmp_path / "missing.json")

    path = tmp_path / "contracts.json"
    write_registry(
        path,
        [
            make_contract().model_dump(mode="json"),
            make_contract(unit="EUR/share").model_dump(mode="json"),
        ],
    )
    with pytest.raises(DatasetContractConflictError):
        load_dataset_contract_registry(path)
