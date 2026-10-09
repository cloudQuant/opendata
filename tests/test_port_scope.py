"""Offline regressions for the per-path vendor batch-scope reconciliation."""

from __future__ import annotations

import copy
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.quality import port_scope

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def inputs() -> port_scope.PortScopeInputs:
    """Read one frozen set of inputs so each test can mutate an isolated copy."""
    return port_scope.load_port_scope_inputs(REPO_ROOT)


def _inventory_copy(inputs: port_scope.PortScopeInputs) -> dict[str, object]:
    inventory = copy.deepcopy(inputs.inventory)
    assert isinstance(inventory, dict)
    return inventory


def _rows(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = payload["files"]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) for row in rows)
    return rows


def _scope_basis(payload: dict[str, object]) -> dict[str, object]:
    basis = payload["scope_basis"]
    assert isinstance(basis, dict)
    return basis


def _row(payload: dict[str, object], path: str) -> dict[str, object]:
    return next(row for row in _rows(payload) if row.get("path") == path)


def test_current_scope_inventory_is_clean_and_names_its_own_batch_field(
    inputs: port_scope.PortScopeInputs,
) -> None:
    audit = port_scope.validate_port_scope(REPO_ROOT, inputs)

    assert audit.checked_paths == 327
    assert audit.python_paths == 325
    assert audit.resource_paths == 2
    assert audit.valid is True
    assert audit.problems == ()
    # The batch field has to trace to the archive this run resolved, not to a frozen round.
    assert audit.inventory_rel == port_scope.scope_inventory_rel(REPO_ROOT)
    assert (REPO_ROOT / str(audit.inventory_rel)).is_file()
    assert audit.batch_field == f"{audit.inventory_rel}#files.batch"


def test_missing_inventory_path_fails_closed(inputs: port_scope.PortScopeInputs) -> None:
    inventory = _inventory_copy(inputs)
    _rows(inventory).pop()
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))

    assert audit.valid is False
    assert audit.batch_field == "-"
    assert any("path sets differ" in problem for problem in audit.problems)


def test_stale_inventory_sha_is_rejected_even_when_claimed_flags_are_true(
    inputs: port_scope.PortScopeInputs,
) -> None:
    inventory = _inventory_copy(inputs)
    sha = _row(inventory, "bond/bond_cb_sina.py")["sha256"]
    assert isinstance(sha, dict)
    sha["manifest_ported_snapshot"] = "0" * 64
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))

    assert audit.valid is False
    assert any("ported hash differs" in problem for problem in audit.problems)
    assert any("disk hash differs" in problem for problem in audit.problems)


def test_batch_label_is_derived_from_path_group(inputs: port_scope.PortScopeInputs) -> None:
    inventory = _inventory_copy(inputs)
    _row(inventory, "futures_derivative/__init__.py")["batch"] = ["B1.1"]
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))

    assert audit.valid is False
    assert any("batch differs" in problem for problem in audit.problems)


@pytest.mark.parametrize("missing_group", [False, True], ids=["extra-directory", "missing-group"])
def test_b1_1_group_set_must_match_plan_and_d9(
    inputs: port_scope.PortScopeInputs, missing_group: bool
) -> None:
    inventory = _inventory_copy(inputs)
    groups = _scope_basis(inventory)["b1_1_groups"]
    assert isinstance(groups, list)
    if missing_group:
        groups.remove(next(group for group in groups if group["module"] == "futures_derivative"))
    else:
        groups.append({"module": "unlisted_extra", "python_file_count": 1})
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))

    assert audit.valid is False
    assert any("B1.1 group" in problem for problem in audit.problems)


def test_empty_per_path_rationale_is_rejected(inputs: port_scope.PortScopeInputs) -> None:
    inventory = _inventory_copy(inputs)
    _row(inventory, "bond/bond_cb_sina.py")["reason"] = "  "
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))

    assert audit.valid is False
    assert any("rationale is empty" in problem for problem in audit.problems)


def test_inventory_reconciliation_booleans_are_not_judgment_inputs(
    inputs: port_scope.PortScopeInputs,
) -> None:
    inventory = _inventory_copy(inputs)
    reconciliation = inventory["reconciliation"]
    assert isinstance(reconciliation, dict)
    # Name the keys before overwriting them: a drifted key would add a field and test nothing.
    assert "lock_manifest_path_set_equal" in reconciliation
    assert "manifest_snapshot_sha_matches_current_port_count" in reconciliation
    reconciliation["lock_manifest_path_set_equal"] = False
    reconciliation["manifest_snapshot_sha_matches_current_port_count"] = 0
    audit = port_scope.validate_port_scope(REPO_ROOT, replace(inputs, inventory=inventory))
    baseline = port_scope.validate_port_scope(REPO_ROOT, inputs)

    assert baseline.valid is True
    assert audit.valid == baseline.valid
    assert audit.problems == baseline.problems


def test_extra_disk_directory_is_rejected(
    inputs: port_scope.PortScopeInputs, tmp_path: Path
) -> None:
    copied_port_root = port_scope.current_port_root(tmp_path)
    copied_port_root.parent.mkdir(parents=True)
    shutil.copytree(port_scope.current_port_root(REPO_ROOT), copied_port_root)
    extra = port_scope.current_port_root(tmp_path) / "unlisted_extra" / "new_module.py"
    extra.parent.mkdir()
    extra.write_text("value = 1\n", encoding="utf-8")

    audit = port_scope.validate_port_scope(tmp_path, inputs)

    assert audit.valid is False
    assert any("disk tree/upstream.lock path sets differ" in problem for problem in audit.problems)
    assert any("unlisted_extra/new_module.py" in problem for problem in audit.problems)


def test_disk_sha_is_recomputed_instead_of_trusting_manifest_flags(
    inputs: port_scope.PortScopeInputs, tmp_path: Path
) -> None:
    copied_port_root = port_scope.current_port_root(tmp_path)
    copied_port_root.parent.mkdir(parents=True)
    shutil.copytree(port_scope.current_port_root(REPO_ROOT), copied_port_root)
    changed = port_scope.current_port_root(tmp_path) / "stock_feature" / "ths.js"
    changed.write_bytes(changed.read_bytes() + b"\n")

    audit = port_scope.validate_port_scope(tmp_path, inputs)

    assert audit.valid is False
    assert any("disk hash differs" in problem for problem in audit.problems)


def test_missing_canonical_root_does_not_fall_back_to_historical_decoys(tmp_path: Path) -> None:
    historical_root = tmp_path / port_scope.PORT_ROOT
    historical_root.mkdir(parents=True)
    shutil.copyfile(
        port_scope.current_port_root(REPO_ROOT) / "manifest.json",
        historical_root / "manifest.json",
    )
    shutil.copyfile(
        port_scope.current_port_root(REPO_ROOT) / "upstream.lock",
        historical_root / "upstream.lock",
    )

    audit = port_scope.audit_port_scope(tmp_path)

    assert audit.valid is False
    assert audit.checked_paths == 0
    assert len(audit.problems) == 1
    assert "opendata_http/upstream.lock" in audit.problems[0]
    assert "opendata/data/providers/akshare/_vendor/upstream.lock" in audit.problems[0]


def test_unknown_and_escaping_current_identities_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(port_scope.PortScopePathError, match="cannot map historical path"):
        port_scope._current_source_path(tmp_path, "opendata_fuyao/not-mapped.py")

    layout = port_scope._source_layout_module()
    monkeypatch.setattr(layout, "historical_identity", lambda _path: "../outside")
    with pytest.raises(port_scope.PortScopePathError, match="unsafe current identity"):
        port_scope.current_port_root(tmp_path)
