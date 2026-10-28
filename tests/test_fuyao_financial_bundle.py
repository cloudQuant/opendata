"""Synthetic-only tests for the read-only Fuyao configuration bundle."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
from opendata.data.providers.ths.endpoints import financial_statement_items
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    ThsFinancialNativeUnitProfile,
    canonical_native_unit_mapping_sha256,
)
from opendata.pipeline import fuyao_financial_bundle as bundle_module
from opendata.pipeline.fuyao_financial_bundle import (
    FuyaoFinancialBundle,
    FuyaoFinancialBundleError,
    load_fuyao_financial_bundle,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_SYNTHETIC_INSTRUMENT = "999999.BJ"
_SYNTHETIC_DATE = date(2026, 10, 28)
_STATEMENTS = ("income", "balance", "cashflow")


def _registry(
    *,
    instrument_id: str = _SYNTHETIC_INSTRUMENT,
    status: LifecycleStatus = LifecycleStatus.ACTIVE,
    extra_lifecycle: bool = False,
    with_alias: bool = False,
) -> InstrumentReferenceRegistry:
    """Build synthetic identity evidence without claiming a real listing."""
    lifecycles = [
        InstrumentLifecycle(
            dataset_id="financial_statement",
            source_id="ths",
            instrument_id=instrument_id,
            lifecycle_version="synthetic-lifecycle-v1",
            status=status,
            status_as_of=_SYNTHETIC_DATE,
            status_evidence_ref="synthetic:test-only-status",
        )
    ]
    if extra_lifecycle:
        lifecycles.append(
            InstrumentLifecycle(
                dataset_id="financial_statement",
                source_id="ths",
                instrument_id="999998.BJ",
                lifecycle_version="synthetic-lifecycle-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=_SYNTHETIC_DATE,
                status_evidence_ref="synthetic:test-only-status",
            )
        )
    aliases: tuple[InstrumentAlias, ...] = ()
    if with_alias:
        aliases = (
            InstrumentAlias(
                dataset_id="financial_statement",
                source_id="ths",
                alias="SYNTHETIC_ALIAS",
                instrument_id=instrument_id,
                alias_version="synthetic-alias-v1",
                evidence_ref="synthetic:test-only-alias",
            ),
        )
    return InstrumentReferenceRegistry(
        registry_version="synthetic-registry-v1",
        evidence_ref="synthetic:test-only-registry",
        lifecycles=tuple(lifecycles),
        aliases=aliases,
    )


def _unit_profiles() -> dict[str, dict[str, object]]:
    """Build complete, explicitly synthetic operator declarations."""
    profiles: dict[str, dict[str, object]] = {}
    for statement in _STATEMENTS:
        native_units = dict.fromkeys(financial_statement_items(statement), "synthetic-test-unit")
        version = "synthetic-profile-v1"
        profiles[statement] = {
            "statement_type": statement,
            "version": version,
            "native_units": native_units,
            "mapping_sha256_declared": canonical_native_unit_mapping_sha256(
                statement_type=statement,
                version=version,
                native_units=native_units,
            ),
            "evidence_ref": "synthetic:test-only-unit-declaration",
        }
    return profiles


def _unit_profile_objects() -> dict[str, ThsFinancialNativeUnitProfile]:
    """Build complete synthetic profile objects for direct-constructor checks."""
    profiles: dict[str, ThsFinancialNativeUnitProfile] = {}
    for statement, profile in _unit_profiles().items():
        version = profile["version"]
        native_units_value = profile["native_units"]
        declared_digest = profile["mapping_sha256_declared"]
        evidence_ref = profile["evidence_ref"]
        assert isinstance(version, str)
        assert isinstance(native_units_value, dict)
        assert isinstance(declared_digest, str)
        assert isinstance(evidence_ref, str)
        native_units: dict[str, str] = {}
        for item, unit in native_units_value.items():
            assert isinstance(item, str)
            assert isinstance(unit, str)
            native_units[item] = unit
        profiles[statement] = ThsFinancialNativeUnitProfile(
            statement_type=statement,
            version=version,
            native_units=native_units,
            mapping_sha256_declared=declared_digest,
            evidence_ref=evidence_ref,
        )
    return profiles


def _document(
    *,
    registry: InstrumentReferenceRegistry | None = None,
    instrument_id: str = _SYNTHETIC_INSTRUMENT,
) -> dict[str, object]:
    """Return one synthetic bundle document for structural tests."""
    snapshot = encode_instrument_reference_snapshot(registry or _registry())
    return {
        "schema_version": 1,
        "scope": {"instrument_id": instrument_id},
        "instrument_reference_snapshot": json.loads(snapshot.decode("utf-8")),
        "unit_profiles": _unit_profiles(),
    }


def _encoded(document: object) -> bytes:
    """Serialize synthetic JSON with stable bytes for digest-pinned cases."""
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_bundle(path: Path, raw: bytes) -> str:
    """Write synthetic bytes with service-owned private permissions."""
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _load_bytes(tmp_path: Path, raw: bytes) -> object:
    """Load one synthetic byte sequence through the public pinned entry point."""
    path = tmp_path / "bundle.json"
    pin = _write_bundle(path, raw)
    return load_fuyao_financial_bundle(path=path, expected_sha256=pin)


def test_loads_pinned_single_instrument_and_freezes_profiles(tmp_path: Path) -> None:
    """The loader validates the snapshot and all statement profiles offline."""
    bundle = _load_bytes(tmp_path, _encoded(_document()))

    assert bundle.instrument_id == _SYNTHETIC_INSTRUMENT
    assert bundle.instrument_registry.lifecycles[0].instrument_id == _SYNTHETIC_INSTRUMENT
    assert set(bundle.unit_profiles) == set(_STATEMENTS)
    assert len(bundle.bundle_sha256) == 64
    with pytest.raises(TypeError):
        bundle.unit_profiles["income"] = bundle.unit_profiles["income"]  # type: ignore[index]


@pytest.mark.parametrize(
    "registry",
    [
        _registry(extra_lifecycle=True),
        _registry(with_alias=True),
        _registry(status=LifecycleStatus.UNKNOWN),
        _registry(instrument_id="999998.BJ"),
    ],
)
def test_direct_bundle_constructor_revalidates_registry_scope(
    registry: InstrumentReferenceRegistry,
) -> None:
    """Direct construction cannot bypass the loader's one-known-instrument policy."""
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=registry,
            unit_profiles=_unit_profile_objects(),
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_registry_scope_invalid"


def test_direct_bundle_constructor_revalidates_provider_item_coverage() -> None:
    """A self-consistent digest does not make an incomplete profile acceptable."""
    profiles = _unit_profile_objects()
    native_units = {"synthetic_partial_item": "synthetic-test-unit"}
    version = "synthetic-profile-v1"
    profiles["income"] = ThsFinancialNativeUnitProfile(
        statement_type="income",
        version=version,
        native_units=native_units,
        mapping_sha256_declared=canonical_native_unit_mapping_sha256(
            statement_type="income",
            version=version,
            native_units=native_units,
        ),
        evidence_ref="synthetic:test-only-unit-declaration",
    )

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=_registry(),
            unit_profiles=profiles,
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_unit_profile_invalid"


def test_direct_bundle_constructor_rejects_invalid_instrument_id() -> None:
    """Direct callers must satisfy the same canonical instrument token format."""
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id="not-a-security",
            instrument_registry=_registry(),
            unit_profiles=_unit_profile_objects(),
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_instrument_scope_invalid"


def test_direct_bundle_constructor_rejects_wrong_registry_type() -> None:
    """A registry-shaped claim cannot be substituted for the validated model."""
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=object(),  # type: ignore[arg-type]
            unit_profiles=_unit_profile_objects(),
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_registry_invalid"


def test_direct_bundle_constructor_rejects_invalid_pin_and_profile_container() -> None:
    """Direct objects still reject malformed digest and non-mapping profiles."""
    with pytest.raises(FuyaoFinancialBundleError) as pin_error:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=_registry(),
            unit_profiles=_unit_profile_objects(),
            bundle_sha256="not-a-sha256",
        )
    assert pin_error.value.code == "bundle_pin_invalid"

    with pytest.raises(FuyaoFinancialBundleError) as profiles_error:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=_registry(),
            unit_profiles=None,  # type: ignore[arg-type]
            bundle_sha256="a" * 64,
        )
    assert profiles_error.value.code == "bundle_unit_profiles_invalid"


def test_direct_bundle_constructor_rejects_wrong_profile_type() -> None:
    """All three direct-construction entries must be actual THS profiles."""
    profiles: dict[str, object] = dict.fromkeys(_STATEMENTS, object())

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=_registry(),
            unit_profiles=profiles,  # type: ignore[arg-type]
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_unit_profile_invalid"


def test_direct_bundle_constructor_rejects_missing_statement_profile() -> None:
    """A direct bundle must contain all three statement families."""
    profiles = _unit_profile_objects()
    del profiles["cashflow"]

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        FuyaoFinancialBundle(
            instrument_id=_SYNTHETIC_INSTRUMENT,
            instrument_registry=_registry(),
            unit_profiles=profiles,
            bundle_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_unit_profiles_invalid"


@pytest.mark.parametrize("surrogate_location", ["unit_evidence", "registry_evidence", "unit_value"])
def test_rejects_escaped_lone_surrogates_in_json_strings(
    tmp_path: Path, surrogate_location: str
) -> None:
    """Every decoded JSON string must be valid UTF-8 scalar text."""
    document = _document()
    if surrogate_location == "unit_evidence":
        profiles = document["unit_profiles"]
        assert isinstance(profiles, dict)
        income = profiles["income"]
        assert isinstance(income, dict)
        income["evidence_ref"] = "synthetic:\ud800"
    elif surrogate_location == "registry_evidence":
        snapshot = document["instrument_reference_snapshot"]
        assert isinstance(snapshot, dict)
        snapshot["evidence_ref"] = "synthetic:\ud800"
    else:
        profiles = document["unit_profiles"]
        assert isinstance(profiles, dict)
        income = profiles["income"]
        assert isinstance(income, dict)
        native_units = income["native_units"]
        assert isinstance(native_units, dict)
        item = next(iter(native_units))
        native_units[item] = "synthetic:\ud800"

    raw = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, raw)

    assert caught.value.code == "bundle_json_unicode_invalid"


def test_rejects_mismatched_pin_without_echoing_bundle_bytes(tmp_path: Path) -> None:
    """A deployment pin mismatch yields a fixed error without file contents."""
    marker = "SYNTHETIC_PRIVATE_CONTENT_MARKER"
    document = _document()
    document["scope"] = {"instrument_id": _SYNTHETIC_INSTRUMENT, "note": marker}
    raw = _encoded(document)
    path = tmp_path / "bundle.json"
    _write_bundle(path, raw)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=hashlib.sha256(b"other").hexdigest())

    assert caught.value.code == "bundle_pin_mismatch"
    assert marker not in str(caught.value)
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize(
    ("path_value", "pin"),
    [
        ("relative/bundle.json", "0" * 64),
        (Path("/tmp/not-read.json"), "not-a-digest"),
    ],
)
def test_rejects_non_deployment_path_or_malformed_pin(path_value: str | Path, pin: str) -> None:
    """Path and pin validation fails before opening a file."""
    with pytest.raises(FuyaoFinancialBundleError):
        load_fuyao_financial_bundle(path=path_value, expected_sha256=pin)


@pytest.mark.parametrize("path_value", [None, Path("/")])
def test_rejects_missing_or_root_bundle_path(path_value: object) -> None:
    """Missing paths and filesystem-root paths fail before a file is opened."""
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path_value, expected_sha256="a" * 64)  # type: ignore[arg-type]

    assert caught.value.code == "bundle_path_invalid"


def test_rejects_platform_without_no_follow_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader refuses platforms unable to enforce its no-symlink policy."""
    monkeypatch.delattr(bundle_module.os, "O_NOFOLLOW")

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(
            path=tmp_path / "bundle.json",
            expected_sha256="a" * 64,
        )

    assert caught.value.code == "bundle_platform_unsupported"


def test_rejects_final_symlink(tmp_path: Path) -> None:
    """The final bundle filename is opened without following a symlink."""
    target = tmp_path / "target.json"
    pin = _write_bundle(target, _encoded(_document()))
    alias = tmp_path / "bundle.json"
    alias.symlink_to(target)

    with pytest.raises(FuyaoFinancialBundleError):
        load_fuyao_financial_bundle(path=alias, expected_sha256=pin)


def test_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    """Every directory component is traversed with no-follow semantics."""
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    target = real_directory / "bundle.json"
    pin = _write_bundle(target, _encoded(_document()))
    alias_directory = tmp_path / "alias"
    alias_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(FuyaoFinancialBundleError):
        load_fuyao_financial_bundle(path=alias_directory / "bundle.json", expected_sha256=pin)


def test_rejects_group_or_world_writable_file(tmp_path: Path) -> None:
    """A correct pin does not override unsafe file write permissions."""
    path = tmp_path / "bundle.json"
    pin = _write_bundle(path, _encoded(_document()))
    path.chmod(0o620)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=pin)

    assert caught.value.code == "bundle_permissions_invalid"


def test_rejects_group_or_world_writable_parent(tmp_path: Path) -> None:
    """The immediate config directory must be private to the service owner."""
    parent = tmp_path / "unsafe"
    parent.mkdir()
    path = parent / "bundle.json"
    pin = _write_bundle(path, _encoded(_document()))
    parent.chmod(0o770)
    try:
        with pytest.raises(FuyaoFinancialBundleError) as caught:
            load_fuyao_financial_bundle(path=path, expected_sha256=pin)
        assert caught.value.code == "bundle_permissions_invalid"
    finally:
        parent.chmod(0o700)


def test_rejects_hard_linked_configuration(tmp_path: Path) -> None:
    """The config must be a single-link regular file for atomic replacement."""
    path = tmp_path / "bundle.json"
    pin = _write_bundle(path, _encoded(_document()))
    (tmp_path / "second-name.json").hardlink_to(path)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=pin)

    assert caught.value.code == "bundle_file_type_invalid"


def test_rejects_empty_and_oversized_files(tmp_path: Path) -> None:
    """The loader bounds input size before decoding JSON."""
    empty = tmp_path / "empty.json"
    empty_pin = _write_bundle(empty, b"")
    with pytest.raises(FuyaoFinancialBundleError) as empty_error:
        load_fuyao_financial_bundle(path=empty, expected_sha256=empty_pin)
    assert empty_error.value.code == "bundle_file_size_invalid"

    oversized = tmp_path / "large.json"
    large_bytes = b"x" * (bundle_module.MAX_FUYAO_FINANCIAL_BUNDLE_BYTES + 1)
    large_pin = _write_bundle(oversized, large_bytes)
    with pytest.raises(FuyaoFinancialBundleError) as large_error:
        load_fuyao_financial_bundle(path=oversized, expected_sha256=large_pin)
    assert large_error.value.code == "bundle_file_size_invalid"


def test_detects_path_replacement_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path must still name the exact opened file after its bytes are read."""
    path = tmp_path / "bundle.json"
    original = _encoded(_document())
    replacement = tmp_path / "replacement.json"
    _write_bundle(replacement, _encoded(_document(instrument_id="999997.BJ")))
    pin = _write_bundle(path, original)
    real_read = os.read
    replaced = False

    def read_then_replace(file_descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = real_read(file_descriptor, size)
        if not replaced:
            os.replace(replacement, path)
            replaced = True
        return content

    monkeypatch.setattr(bundle_module.os, "read", read_then_replace)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=pin)

    assert caught.value.code == "bundle_path_changed_during_read"


def test_detects_in_place_file_mutation_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged pathname cannot conceal mutation of the opened file inode."""
    path = tmp_path / "bundle.json"
    raw = _encoded(_document())
    pin = _write_bundle(path, raw)
    real_read = os.read
    changed = False

    def read_then_touch_open_inode(file_descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = real_read(file_descriptor, size)
        if not changed:
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
            changed = True
        return content

    monkeypatch.setattr(bundle_module.os, "read", read_then_touch_open_inode)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=pin)

    assert caught.value.code == "bundle_file_changed_during_read"


def test_detects_parent_permission_change_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-read checks catch permission changes even when the inode is unchanged."""
    path = tmp_path / "bundle.json"
    raw = _encoded(_document())
    pin = _write_bundle(path, raw)
    real_read = os.read
    original_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    changed = False

    def read_then_change_permissions(file_descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = real_read(file_descriptor, size)
        if not changed:
            tmp_path.chmod(0o770)
            changed = True
        return content

    monkeypatch.setattr(bundle_module.os, "read", read_then_change_permissions)
    try:
        with pytest.raises(FuyaoFinancialBundleError) as caught:
            load_fuyao_financial_bundle(path=path, expected_sha256=pin)
        assert caught.value.code == "bundle_permissions_invalid"
    finally:
        tmp_path.chmod(original_mode)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda document: document.update(extra="synthetic"), "bundle_schema_invalid"),
        (lambda document: document.update(schema_version=True), "bundle_schema_invalid"),
        (
            lambda document: document.update(scope={"instrument_id": "bad-id"}),
            "bundle_instrument_scope_invalid",
        ),
        (
            lambda document: document.update(scope={"instrument_id": "999999.BJ", "extra": 1}),
            "bundle_scope_schema_invalid",
        ),
    ],
)
def test_rejects_invalid_root_and_scope_shapes(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
    expected_code: str,
) -> None:
    """Unknown fields and invalid one-instrument scope fail closed."""
    document = _document()
    mutation(document)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, _encoded(document))

    assert caught.value.code == expected_code


def test_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    """Duplicate keys are rejected even when the deployment pin matches."""
    raw = b'{"schema_version":1,"schema_version":1}'
    path = tmp_path / "bundle.json"
    pin = _write_bundle(path, raw)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        load_fuyao_financial_bundle(path=path, expected_sha256=pin)

    assert caught.value.code == "bundle_json_duplicate_key"


def test_rejects_invalid_utf8_json(tmp_path: Path) -> None:
    """Malformed UTF-8 is translated to a stable parser error."""
    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, b"\xff")

    assert caught.value.code == "bundle_json_invalid"


def test_rejects_nonstandard_json_constant_and_deep_json(tmp_path: Path) -> None:
    """NaN and deeply nested documents cannot reach schema parsing."""
    raw_constant = b'{"schema_version":NaN}'
    with pytest.raises(FuyaoFinancialBundleError) as constant_error:
        _load_bytes(tmp_path, raw_constant)
    assert constant_error.value.code == "bundle_json_invalid"

    nested: object = None
    for _ in range(20):
        nested = [nested]
    with pytest.raises(FuyaoFinancialBundleError) as depth_error:
        _load_bytes(tmp_path, _encoded(nested))
    assert depth_error.value.code == "bundle_json_bounds_invalid"


@pytest.mark.parametrize(
    "registry",
    [
        _registry(extra_lifecycle=True),
        _registry(with_alias=True),
        _registry(status=LifecycleStatus.UNKNOWN),
        _registry(instrument_id="999998.BJ"),
    ],
)
def test_rejects_registry_outside_single_known_scope(
    tmp_path: Path, registry: InstrumentReferenceRegistry
) -> None:
    """Additional instruments, aliases, unknown lifecycles, and mismatches fail."""
    document = _document(registry=registry)

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, _encoded(document))

    assert caught.value.code == "bundle_registry_scope_invalid"


def test_rejects_missing_or_unexpected_unit_profile(tmp_path: Path) -> None:
    """All and only three statement profiles are required."""
    document = _document()
    document["unit_profiles"] = {"income": _unit_profiles()["income"]}

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, _encoded(document))

    assert caught.value.code == "bundle_unit_profiles_invalid"

    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    profiles["unexpected"] = copy.deepcopy(profiles["income"])
    with pytest.raises(FuyaoFinancialBundleError) as extra_error:
        _load_bytes(tmp_path, _encoded(document))
    assert extra_error.value.code == "bundle_unit_profiles_invalid"


def test_rejects_unit_digest_or_item_set_mismatch(tmp_path: Path) -> None:
    """The canonical mapping digest and provider item set must both match."""
    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    income = profiles["income"]
    assert isinstance(income, dict)
    income["mapping_sha256_declared"] = "0" * 64
    with pytest.raises(FuyaoFinancialBundleError) as digest_error:
        _load_bytes(tmp_path, _encoded(document))
    assert digest_error.value.code == "bundle_unit_profile_invalid"

    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    income = profiles["income"]
    assert isinstance(income, dict)
    native_units = income["native_units"]
    assert isinstance(native_units, dict)
    native_units["unexpected_synthetic_item"] = "synthetic-test-unit"
    version = income["version"]
    assert isinstance(version, str)
    income["mapping_sha256_declared"] = canonical_native_unit_mapping_sha256(
        statement_type="income",
        version=version,
        native_units=native_units,
    )
    with pytest.raises(FuyaoFinancialBundleError) as items_error:
        _load_bytes(tmp_path, _encoded(document))
    assert items_error.value.code == "bundle_unit_profile_invalid"


def test_rejects_unknown_unit_profile_fields_and_statement_mismatch(tmp_path: Path) -> None:
    """Profile shapes bind each mapping to its exact statement family."""
    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    income = profiles["income"]
    assert isinstance(income, dict)
    income["extra"] = "synthetic"
    with pytest.raises(FuyaoFinancialBundleError) as extra_error:
        _load_bytes(tmp_path, _encoded(document))
    assert extra_error.value.code == "bundle_unit_profile_schema_invalid"

    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    income = profiles["income"]
    assert isinstance(income, dict)
    income["statement_type"] = "balance"
    with pytest.raises(FuyaoFinancialBundleError) as statement_error:
        _load_bytes(tmp_path, _encoded(document))
    assert statement_error.value.code == "bundle_unit_profile_invalid"


def test_rejects_non_string_unit_profile_declaration_fields(tmp_path: Path) -> None:
    """Unit profile text fields cannot be coerced from JSON numbers or nulls."""
    document = _document()
    profiles = document["unit_profiles"]
    assert isinstance(profiles, dict)
    income = profiles["income"]
    assert isinstance(income, dict)
    income["version"] = 1

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, _encoded(document))

    assert caught.value.code == "bundle_unit_profile_invalid"


@pytest.mark.parametrize("snapshot_value", [[], {}])
def test_rejects_invalid_registry_snapshot_shapes(tmp_path: Path, snapshot_value: object) -> None:
    """The embedded registry must be an independently valid strict snapshot."""
    document = _document()
    document["instrument_reference_snapshot"] = snapshot_value

    with pytest.raises(FuyaoFinancialBundleError) as caught:
        _load_bytes(tmp_path, _encoded(document))

    assert caught.value.code == "bundle_registry_invalid"
