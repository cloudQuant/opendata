"""Offline regressions for the C74 port-scope inventory generator.

``scripts/quality/port_scope_inventory.py`` re-measures a locked ported tree against
``upstream.lock``, ``manifest.json``, the on-disk bytes and the previous round's archive, and it
refuses to write anything when one of them disagrees. Every fixture here is synthetic and lives
in tmp_path: pristine upstream sources, the ported bytes (produced with the same deterministic
codemod the instrument replays), the lock, the manifest, and a C65-shaped carried inventory. No
real upstream clone is read and no git process is spawned -- ``port_module._git`` is faked at
that boundary, so the locked-commit arm still runs the real ``verify_upstream`` comparison.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

import pytest

from scripts.codemod import port_module
from scripts.quality import port_scope, source_layout
from scripts.quality import port_scope_inventory as psi

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://github.com/akfamily/akshare.git"
COMMIT = port_scope.EXPECTED_UPSTREAM_COMMIT
VENDOR_REL = Path(source_layout.VENDOR_ROOT)
ROOT_FACADE = "__init__.py"
FILLER_MODULE = "b12_on_demand"
RESOURCE_PATHS = ("file_fold/calendar.json", "stock_feature/ths.js")
B1_1_COUNTS: dict[str, int] = {
    **port_scope.B1_1_PLAN_MODULE_FILE_COUNTS,
    "futures_derivative": port_scope.B1_1_FUTURES_DERIVATIVE_FILE_COUNT,
}
_BATCH_MODULES = frozenset(B1_1_COUNTS)
# The root facade is replayed by ``vendor_init_facade`` and carries no rewrite counts, so
# only the remaining ported modules contribute the codemod's import/string rewrite totals.
MODULE_ROWS = port_scope.EXPECTED_PYTHON_FILES - 1
MANUAL_EDIT_ROWS = ("datasets.py", "stock/cons.py")


def _is_python(path: str) -> bool:
    """Whether a locked path is a ported module rather than a verbatim resource copy."""
    return PurePosixPath(path).suffix == ".py"


def _python_paths() -> list[str]:
    """Every locked Python path: the fixed scope records plus generated filler up to 325."""
    paths = [
        ROOT_FACADE,
        "_version.py",
        "exceptions.py",
        "request.py",
        "datasets.py",
        "file_fold/__init__.py",
    ]
    a2_1 = sorted(set(port_scope.A2_1_FULL_PATHS) - {ROOT_FACADE})
    paths += [p for p in a2_1 if PurePosixPath(p).parts[0] not in B1_1_COUNTS]
    for module in sorted(B1_1_COUNTS):
        owned = [p for p in a2_1 if PurePosixPath(p).parts[0] == module]
        paths.extend(owned)
        paths.extend(
            f"{module}/mod_{index:03d}.py"
            for index in range(len(owned) + 1, B1_1_COUNTS[module] + 1)
        )
    filler = 0
    while len(paths) < port_scope.EXPECTED_PYTHON_FILES:
        filler += 1
        paths.append(f"{FILLER_MODULE}/misc_{filler:03d}.py")
    assert len(paths) == port_scope.EXPECTED_PYTHON_FILES
    assert len(set(paths)) == len(paths)
    return paths


LOCKED_PYTHON_PATHS = _python_paths()
LOCKED_PATHS = (*LOCKED_PYTHON_PATHS, *RESOURCE_PATHS)
FILLER_TAIL = LOCKED_PYTHON_PATHS[-1]


def _pristine(upstream_path: str) -> str:
    """One deterministic synthetic upstream source for ``upstream_path``."""
    if upstream_path == port_module._INIT_UPSTREAM_PATH:
        return (
            '"""Upstream root export table."""\n'
            "from akshare.utils import token_process\n"
            "from akshare.stock import cons\n"
            "from akshare.not_ported_at_all import anything\n"
        )
    stem = PurePosixPath(upstream_path).stem
    return (
        f'"""Upstream {stem} module."""\n'
        "import akshare\n"
        "from akshare.utils import token_process\n"
        'MODULE_PATH = "akshare.utils.demjson"\n'
        f"def {stem}_value() -> str:\n"
        f"    return akshare.__version__ + token_process.NAME + MODULE_PATH\n"
    )


def _pristine_resource(upstream_path: str) -> bytes:
    """One deterministic synthetic upstream resource payload."""
    return f"// synthetic upstream payload for {upstream_path}\n".encode()


def _lock_row(
    path: str, upstream_path: str, pristine_sha: str, manual_edits: bool
) -> dict[str, Any]:
    """One ``upstream.lock`` record for a locked path."""
    return {
        "path": path,
        "upstream_path": upstream_path,
        "sha256": pristine_sha,
        "manual_edits": manual_edits,
    }


def _manifest_row(
    path: str, upstream_path: str, ported_sha: str, lines: int | None, manual_edits: bool
) -> dict[str, Any]:
    """One ``manifest.json`` record; only ported modules carry a frozen line count."""
    row: dict[str, Any] = {
        "path": path,
        "upstream_path": upstream_path,
        "sha256": ported_sha,
        "manual_edits": manual_edits,
    }
    if lines is not None:
        row["lines"] = lines
    return row


def _carried_row(path: str) -> dict[str, Any]:
    """One carried-forward C65-style scope claim whose batch is the derived one."""
    kind = "python" if _is_python(path) else "resource"
    derived = sorted(port_scope._expected_batches(path, kind, _BATCH_MODULES))
    return {
        "path": path,
        "batch": derived,
        "reason": f"carried scope claim for {path}: frozen with the previous round",
        "existing_recorded_cases": [f"case-{path}"] if path == "stock/cons.py" else [],
        "existing_recorded_public_functions": (
            ["stock_zh_a_hist"] if path == "stock_feature/stock_hist_em.py" else []
        ),
    }


def _scope_basis() -> dict[str, Any]:
    """A validator-clean scope record built from the fixed scope constants."""
    closure = sorted(port_scope.A2_1_DEFINITION_IMPORT_CLOSURE)
    return {
        "a2_1_definition_import_closure_files": closure,
        "a2_1_full_utils_and_public_package_path_files": sorted(port_scope.A2_1_FULL_PATHS),
        "a2_1_module_roots": sorted(port_scope.A2_1_MODULE_ROOTS),
        "a2_1_function_entrypoints": [
            {"public_function": name, "upstream_path": path}
            for name, path in port_scope.A2_1_FUNCTION_ENTRYPOINTS
        ],
        "a2_1_source_file_count": len(port_scope.A2_1_FULL_PATHS),
        "b1_1_groups": [
            {"module": module, "python_file_count": count}
            for module, count in sorted(B1_1_COUNTS.items())
        ],
        "b1_1_group_count": len(_BATCH_MODULES),
        "b1_1_python_file_count": port_scope.B1_1_PYTHON_FILE_COUNT,
        "b1_1_resource_paths": sorted(port_scope.B1_1_RESOURCE_PATHS),
        "b1_1_artifact_count_including_resources": port_scope.B1_1_ARTIFACT_COUNT,
        "d9_nonfinancial_modules_absent_from_frozen_port": sorted(
            port_scope.EXPECTED_D9_EXCLUDED_MODULES
        ),
        "b1_2_on_demand_reason": "carried: on-demand modules outside the nine B1.1 directories",
        "requirements": list(port_scope.EXPECTED_SCOPE_REQUIREMENTS),
    }


AC6_02: dict[str, Any] = {
    "offline_recorded_cases": 3,
    "public_functions_with_recorded_cases": 2,
    "note": "carried verbatim by path",
}

IMPLEMENTATION_PLAN = (
    "| item | scope | files |\n"
    "| B1.1 | batch port (stock_feature/stock_fundamental/futures/index/fund/option/bond/"
    "economic) | 245 |\n"
)
REQUIREMENTS = (
    "搬运范围（D9）除八个计划目录外显式点名扩展（stock_feature、stock_fundamental、futures、"
    "index、fund、option、bond、economic、futures_derivative）\n"
)


def _dump_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON document deterministically."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    """Read one JSON object document."""
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return cast("dict[str, Any]", loaded)


def _edit_rows(path: Path, section: str, edit: Callable[[list[dict[str, Any]]], None]) -> None:
    """Apply one in-place edit to a JSON record list and persist it back."""
    payload = _load_json(path)
    rows = payload[section]
    assert isinstance(rows, list)
    edit(cast("list[dict[str, Any]]", rows))
    _dump_json(path, payload)


def _drop(path: str) -> Callable[[list[dict[str, Any]]], None]:
    """Build an edit that removes one record by its ``path`` field."""

    def _edit(rows: list[dict[str, Any]]) -> None:
        index = next(row_index for row_index, row in enumerate(rows) if row.get("path") == path)
        rows.pop(index)

    return _edit


def _set_field(path: str, key: str, value: Any) -> Callable[[list[dict[str, Any]]], None]:
    """Build an edit that overwrites one field of one record."""

    def _edit(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row.get("path") == path:
                row[key] = value

    return _edit


def _del_field(path: str, key: str) -> Callable[[list[dict[str, Any]]], None]:
    """Build an edit that removes one field from one record."""

    def _edit(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if row.get("path") == path:
                row.pop(key, None)

    return _edit


@dataclass
class FakeGit:
    """The answers ``verify_upstream`` reads from git, without a process or a clone."""

    head: str = COMMIT
    dirty: str = ""
    calls: list[tuple[str, ...]] = field(default_factory=list)


@pytest.fixture(autouse=True)
def fake_git(monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    """Answer the two git queries the instrument makes so no subprocess is ever spawned."""
    state = FakeGit()

    def _git(repo: Path, *args: str) -> str:
        state.calls.append((str(repo), *args))
        if args[:2] == ("status", "--porcelain"):
            return state.dirty
        if args[:2] == ("rev-parse", "HEAD"):
            return state.head
        raise AssertionError(f"unexpected git invocation: {args}")

    monkeypatch.setattr(port_module, "_git", _git)
    return state


@dataclass(frozen=True)
class Tree:
    """One synthetic round: ported tree, pristine upstream checkout, previous archive."""

    root: Path
    ported: Path
    upstream: Path
    carried: Path

    @property
    def output(self) -> Path:
        """The inventory path the CLI is pointed at."""
        return self.root / "planned" / "port-scope-manifest.json"

    @property
    def lock_path(self) -> Path:
        """The synthetic ``upstream.lock``."""
        return self.ported / psi.LOCK_REL

    @property
    def manifest_path(self) -> Path:
        """The synthetic ported ``manifest.json``."""
        return self.ported / psi.MANIFEST_REL

    def build(self) -> dict[str, object]:
        """Run the generator's library entry point."""
        return psi.build_inventory(
            ported_root=self.ported,
            upstream_repo=self.upstream,
            carried_from=self.carried,
        )

    def main(self, *extra: str) -> int:
        """Run the generator's CLI against this tree."""
        return psi.main(
            [
                "-o",
                str(self.output),
                "--ported-root",
                str(self.ported),
                "--upstream-repo",
                str(self.upstream),
                "--carried-from",
                str(self.carried),
                *extra,
            ]
        )

    def refuse(
        self,
        needle: str,
        error: type[BaseException] = psi.InventoryError,
    ) -> BaseException:
        """Assert the documented claim: it refused, and it left no partial file behind."""
        with pytest.raises(error) as caught:
            self.build()
        message = str(caught.value)
        assert needle in message, message
        assert self.main() == 1
        assert not self.output.exists()
        assert not self.output.parent.exists()
        return caught.value

    def inputs(self, payload: dict[str, object]) -> port_scope.PortScopeInputs:
        """Wrap the generated inventory with the inputs the validator reads from disk."""
        return port_scope.PortScopeInputs(
            inventory=payload,
            upstream_lock=_load_json(self.lock_path),
            port_manifest=_load_json(self.manifest_path),
            implementation_plan=IMPLEMENTATION_PLAN,
            requirements=REQUIREMENTS,
            inventory_rel="planned/port-scope-manifest.json",
        )


def _write_tree(root: Path) -> Tree:
    """Materialize a synthetic 327-path round whose ported bytes the codemod itself produced."""
    ported = root / VENDOR_REL
    upstream = root / "upstream"
    ported.mkdir(parents=True)
    lock_rows: list[dict[str, Any]] = []
    manifest_files: list[dict[str, Any]] = []
    manifest_resources: list[dict[str, Any]] = []
    carried_rows: list[dict[str, Any]] = []
    facade_target: Path | None = None
    for path in LOCKED_PATHS:
        upstream_path = f"akshare/{path}"
        source = upstream / PurePosixPath(upstream_path)
        source.parent.mkdir(parents=True, exist_ok=True)
        target = ported / PurePosixPath(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        carried_rows.append(_carried_row(path))
        if _is_python(path):
            pristine = _pristine(upstream_path)
            source.write_text(pristine, encoding="utf-8")
            if upstream_path == port_module._INIT_UPSTREAM_PATH:
                facade_target = target
                continue
            text, result = port_module.port_source(pristine, upstream_path, URL, COMMIT)
            data = text.encode("utf-8")
            target.write_bytes(data)
            lock_rows.append(
                _lock_row(path, upstream_path, result.upstream_sha256, result.manual_edits)
            )
            manifest_files.append(
                _manifest_row(
                    path,
                    upstream_path,
                    port_module.sha256_bytes(data),
                    data.decode("utf-8").count("\n"),
                    result.manual_edits,
                )
            )
            continue
        data = _pristine_resource(upstream_path)
        source.write_bytes(data)
        target.write_bytes(data)
        lock_rows.append(_lock_row(path, upstream_path, port_module.sha256_bytes(data), False))
        manifest_resources.append(
            _manifest_row(path, upstream_path, port_module.sha256_bytes(data), None, False)
        )
    assert facade_target is not None
    # Every other ported module now exists, which is what the facade replay prunes against.
    pristine_root = upstream / PurePosixPath(port_module._INIT_UPSTREAM_PATH)
    facade = port_module.vendor_init_facade(pristine_root, ported, URL, COMMIT)
    facade_target.write_bytes(facade)
    lock_rows.append(
        _lock_row(
            ROOT_FACADE,
            port_module._INIT_UPSTREAM_PATH,
            port_module.sha256_text(_pristine(port_module._INIT_UPSTREAM_PATH)),
            False,
        )
    )
    manifest_files.append(
        _manifest_row(
            ROOT_FACADE,
            port_module._INIT_UPSTREAM_PATH,
            port_module.sha256_bytes(facade),
            facade.decode("utf-8").count("\n"),
            False,
        )
    )
    _dump_json(
        ported / psi.LOCK_REL,
        {
            "version": 1,
            "upstream": {"url": URL, "commit": COMMIT},
            "files": sorted(lock_rows, key=lambda row: str(row["path"])),
        },
    )
    _dump_json(
        ported / psi.MANIFEST_REL,
        {
            "version": 1,
            "upstream": {"url": URL, "commit": COMMIT},
            "files": sorted(manifest_files, key=lambda row: str(row["path"])),
            "resources": sorted(manifest_resources, key=lambda row: str(row["path"])),
        },
    )
    carried = root / "c65" / port_scope.SCOPE_INVENTORY_NAME
    carried.parent.mkdir(parents=True)
    _dump_json(
        carried,
        {
            "schema_version": 1,
            "as_of": "2026-09-01",
            "upstream_commit": COMMIT,
            "scope_basis": _scope_basis(),
            "ac6_02_existing_fixture_inventory": AC6_02,
            "files": sorted(carried_rows, key=lambda row: str(row["path"])),
        },
    )
    assert len(lock_rows) == port_scope.EXPECTED_TOTAL_FILES
    return Tree(root=root, ported=ported, upstream=upstream, carried=carried)


@pytest.fixture(scope="module")
def base_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the synthetic round once; each test mutates an isolated copy of it."""
    return _write_tree(tmp_path_factory.mktemp("port-scope-inventory")).root


@pytest.fixture
def tree(base_root: Path, tmp_path: Path) -> Tree:
    """A private copy of the synthetic round, so every arm starts from a clean tree."""
    root = tmp_path / "repo"
    shutil.copytree(base_root, root)
    return Tree(
        root=root,
        ported=root / VENDOR_REL,
        upstream=root / "upstream",
        carried=root / "c65" / port_scope.SCOPE_INVENTORY_NAME,
    )


def _rows(payload: dict[str, object]) -> list[dict[str, Any]]:
    """The generated inventory's row list."""
    rows = payload["files"]
    assert isinstance(rows, list)
    return cast("list[dict[str, Any]]", rows)


def _row(payload: dict[str, object], path: str) -> dict[str, Any]:
    return next(row for row in _rows(payload) if row.get("path") == path)


def _sub(row: dict[str, Any], key: str) -> dict[str, Any]:
    value = row[key]
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _reconciliation(payload: dict[str, object]) -> dict[str, Any]:
    value = payload["reconciliation"]
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def test_synthetic_tree_is_the_locked_denominator(tree: Tree) -> None:
    """The fixture carries the very path sets the instrument reconciles, or it proves less."""
    lock = _load_json(tree.lock_path)
    rows = cast("list[dict[str, Any]]", lock["files"])
    paths = {str(row["path"]) for row in rows}
    python = sum(1 for path in paths if _is_python(path))
    assert len(paths) == port_scope.EXPECTED_TOTAL_FILES == 327
    assert python == port_scope.EXPECTED_PYTHON_FILES == 325
    assert len(paths) - python == port_scope.EXPECTED_RESOURCE_FILES == 2
    assert paths == set(LOCKED_PATHS)
    assert {row["upstream_path"] for row in rows} == {f"akshare/{path}" for path in paths}


def test_happy_path_writes_a_327_row_inventory_with_the_locked_denominator(tree: Tree) -> None:
    """The clean path is reachable: the counters add up and every replay reproduces the disk."""
    payload = tree.build()
    assert payload["schema_version"] == 1
    assert payload["as_of"] == psi.AS_OF
    assert payload["upstream_commit"] == COMMIT
    rows = _rows(payload)
    recon = _reconciliation(payload)
    total = recon["lock_file_records"]
    python = recon["lock_python_records"]
    resources = recon["lock_resource_records"]
    assert (total, python, resources) == (327, 325, 2)
    assert total == python + resources
    assert len(rows) == total
    assert python == sum(1 for row in rows if row["kind"] == "python")
    assert resources == sum(1 for row in rows if row["kind"] == "resource")
    assert [row["path"] for row in rows] == sorted(LOCKED_PATHS)
    assert recon["manifest_file_records"] == python
    assert recon["manifest_resource_records"] == resources
    assert recon["port_report_rows"] == total
    assert {row["module"] for row in rows} >= {"<root>", "utils", "stock", FILLER_MODULE}


def test_happy_path_recomputes_every_counter_from_the_live_bytes(tree: Tree) -> None:
    """Nothing in the reconciliation block is asserted: each counter is re-measured here."""
    payload = tree.build()
    recon = _reconciliation(payload)
    rows = _rows(payload)
    total = port_scope.EXPECTED_TOTAL_FILES
    assert recon["lock_manifest_path_set_equal"] is True
    assert recon["port_report_path_set_equal_lock"] is True
    assert recon["lock_manifest_upstream_path_and_manual_metadata_match"] is True
    assert recon["upstream_checkout_sha_match_count"] == total
    assert recon["manifest_snapshot_sha_matches_current_port_count"] == total
    assert recon["port_report_recorded_replay_pass_rows"] == total
    assert recon["port_report_upstream_path_and_manual_metadata_match_count"] == total
    assert (
        recon["manifest_python_line_count_matches_current_count"]
        == port_scope.EXPECTED_PYTHON_FILES
    )
    assert recon["port_report_import_rewrite_total"] == 2 * MODULE_ROWS
    assert recon["port_report_string_rewrite_total"] == MODULE_ROWS
    assert recon["port_report_import_rewrite_total"] == sum(
        int(row["port_report"]["import_rewrites"]) for row in rows
    )
    assert recon["port_report_string_rewrite_total"] == sum(
        int(row["port_report"]["string_rewrites"]) for row in rows
    )
    assert recon["manual_edits_true_rows"] == len(MANUAL_EDIT_ROWS)
    for row in rows:
        digest = _sub(row, "sha256")
        assert digest["upstream_lock_matches_current"] is True
        assert digest["manifest_matches_current_port_tree"] is True
        assert _sub(row, "port_report")["recorded_replay_status"] == psi.REPLAY_MATCH


def test_happy_path_is_accepted_by_the_scope_validator(tree: Tree) -> None:
    """The strongest claim: the generator's own output passes the module that validates it."""
    payload = tree.build()
    audit = port_scope.validate_port_scope(tree.root, tree.inputs(payload))
    assert audit.problems == ()
    assert audit.valid is True
    assert audit.checked_paths == port_scope.EXPECTED_TOTAL_FILES == 327
    assert audit.python_paths == port_scope.EXPECTED_PYTHON_FILES == 325
    assert audit.resource_paths == port_scope.EXPECTED_RESOURCE_FILES == 2
    assert audit.batch_field == "planned/port-scope-manifest.json#files.batch"


def test_happy_path_carries_scope_claims_verbatim_and_rewrites_nothing(tree: Tree) -> None:
    """Reason, batch and the recorded-fixture fields come from the archive; the bytes do not."""
    payload = tree.build()
    assert payload["scope_basis"] == _scope_basis()
    assert payload["ac6_02_existing_fixture_inventory"] == AC6_02
    carried = _row(_load_json(tree.carried), "stock/cons.py")
    cons = _row(payload, "stock/cons.py")
    assert cons["reason"] == carried["reason"]
    assert cons["batch"] == carried["batch"]
    assert cons["existing_recorded_cases"] == ["case-stock/cons.py"]
    assert cons["existing_recorded_public_functions"] == []
    assert cons["manual_edits"] is True
    assert _sub(cons, "manifest")["section"] == "files"
    facade = _row(payload, ROOT_FACADE)
    assert facade["module"] == "<root>"
    assert facade["batch"] == ["A1.6_SHARED_FACADE", "A2.1"]
    assert _sub(facade, "port_report")["import_rewrites"] == 0
    calendar = _row(payload, "file_fold/calendar.json")
    assert calendar["kind"] == "resource"
    assert _sub(calendar, "manifest")["section"] == "resources"
    assert _sub(calendar, "manifest")["snapshot_lines"] is None
    assert _sub(calendar, "sha256")["manifest_matches_current_port_tree"] is True


def test_cli_writes_the_inventory_and_refuses_to_overwrite_without_force(
    tree: Tree, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented CLI arm: -o writes the archive, and an existing file is defended."""
    assert tree.main() == 0
    assert "OK: wrote" in capsys.readouterr().out
    written = _load_json(tree.output)
    assert written == tree.build()
    assert tree.main() == 1
    assert "already exists; pass --force" in capsys.readouterr().err
    assert _load_json(tree.output) == written
    assert tree.main("--force") == 0
    assert _load_json(tree.output) == written


def test_upstream_checkout_at_another_commit_is_refused(tree: Tree, fake_git: FakeGit) -> None:
    """Fail-closed: a checkout that is not the locked commit never reaches the walk."""
    fake_git.head = "0" * 40
    caught = tree.refuse("does not match locked commit", RuntimeError)
    assert not isinstance(caught, psi.InventoryError)
    assert COMMIT in str(caught)
    assert fake_git.calls[0][1:] == ("status", "--porcelain")
    assert fake_git.calls[1][1:] == ("rev-parse", "HEAD")


def test_dirty_upstream_checkout_is_refused(tree: Tree, fake_git: FakeGit) -> None:
    """Fail-closed: a dirty upstream tree is refused before any path is reconciled."""
    fake_git.dirty = " M akshare/utils/func.py"
    caught = tree.refuse("is dirty; refusing to port", RuntimeError)
    assert not isinstance(caught, psi.InventoryError)


def test_manifest_path_missing_for_a_locked_record_is_refused(tree: Tree) -> None:
    """Fail-closed: a locked record with no manifest row is refused as a missing path."""
    _edit_rows(tree.manifest_path, "files", _drop("bond/mod_007.py"))
    message = str(tree.refuse("locked paths missing from"))
    assert str(tree.manifest_path) in message


def test_carried_archive_path_missing_for_a_locked_record_is_refused(tree: Tree) -> None:
    """Fail-closed: the previous round's archive must describe every locked record."""
    _edit_rows(tree.carried, "files", _drop("index/mod_003.py"))
    message = str(tree.refuse("locked paths missing from"))
    assert str(tree.carried) in message


def test_disk_path_missing_for_a_locked_record_is_refused(tree: Tree) -> None:
    """Fail-closed: a locked record with no file on disk is refused, not archived as a gap."""
    (tree.ported / "utils/func.py").unlink()
    tree.refuse("locked paths missing from the ported tree on disk")


def test_disk_file_absent_from_the_lock_is_refused(tree: Tree) -> None:
    """Fail-closed: the ported tree may not smuggle a path the lock never carried."""
    (tree.ported / "utils").joinpath("unported_note.py").write_text("", encoding="utf-8")
    tree.refuse("carries paths absent from upstream.lock")


def test_upstream_source_missing_for_a_locked_record_is_refused(tree: Tree) -> None:
    """Fail-closed: a locked record whose pristine source is gone cannot be replayed."""
    (tree.upstream / "akshare/utils/func.py").unlink()
    tree.refuse("is missing from the upstream checkout")


def test_pristine_hash_that_left_the_lock_is_refused(tree: Tree) -> None:
    """Fail-closed: drifted upstream bytes are named by both hashes, never archived."""
    source = tree.upstream / "akshare/utils/func.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# drifted\n", encoding="utf-8")
    message = str(tree.refuse("pristine hash left the lock"))
    assert "lock=" in message and "checkout=" in message


def test_replayed_module_that_is_not_the_disk_bytes_is_refused(tree: Tree) -> None:
    """Fail-closed: an unregistered hand edit of a ported module stops the archive."""
    disk = tree.ported / "utils/func.py"
    disk.write_text(disk.read_text(encoding="utf-8") + "\n# unregistered\n", encoding="utf-8")
    tree.refuse("differs from the deterministic codemod replay (unregistered)")


def test_carried_batch_the_derivation_rejects_is_refused(tree: Tree) -> None:
    """Fail-closed: a carried batch label must agree with the derived scope."""
    _edit_rows(tree.carried, "files", _set_field("bond/mod_003.py", "batch", ["B1.1", "A2.1"]))
    message = str(tree.refuse("carried batch disagrees with the derivation"))
    assert "carried=['A2.1', 'B1.1']" in message
    assert "derived=['B1.1']" in message


def test_counts_other_than_327_total_325_python_2_resources_are_refused(tree: Tree) -> None:
    """Fail-closed: even a fully covered tree of the wrong denominator is refused."""
    dropped = FILLER_TAIL
    _edit_rows(tree.lock_path, "files", _drop(dropped))
    _edit_rows(tree.manifest_path, "files", _drop(dropped))
    _edit_rows(tree.carried, "files", _drop(dropped))
    (tree.ported / PurePosixPath(dropped)).unlink()
    tree.refuse("locked paths are 326 total / 324 python / 2 resources; expected 327 / 325 / 2")


def test_carried_archive_without_the_top_level_scope_claims_is_refused(tree: Tree) -> None:
    """Fail-closed: scope_basis and the AC-6|02 inventory are required inputs, not optional."""
    payload = _load_json(tree.carried)
    for key in sorted(psi.CARRIED_TOP_LEVEL_KEYS):
        payload.pop(key)
    _dump_json(tree.carried, payload)
    message = str(tree.refuse("is missing"))
    assert "scope_basis" in message and "ac6_02_existing_fixture_inventory" in message


def test_carried_row_missing_a_forwarded_field_is_refused(tree: Tree) -> None:
    """Fail-closed: every row must carry the whole forwarded key set."""
    key = sorted(psi.CARRIED_ROW_KEYS)[0]
    _edit_rows(tree.carried, "files", _del_field("stock/cons.py", key))
    tree.refuse(f"row for stock/cons.py is missing ['{key}']")


def test_carried_row_without_a_rationale_is_refused(tree: Tree) -> None:
    """Fail-closed: a blank reason is a missing reason."""
    _edit_rows(tree.carried, "files", _set_field("stock/cons.py", "reason", "   "))
    tree.refuse("row for stock/cons.py has no rationale")


def test_manifest_record_without_an_integer_line_count_is_refused(tree: Tree) -> None:
    """Fail-closed: the frozen snapshot line count must be there to be compared."""
    _edit_rows(tree.manifest_path, "files", _set_field("utils/func.py", "lines", "3"))
    tree.refuse("utils/func.py: manifest.json records no integer line count")


@pytest.mark.parametrize(
    ("break_input", "needle", "error"),
    [
        ("lock", "is missing; run with --init", RuntimeError),
        ("manifest", "could not be read", psi.InventoryError),
        ("carried", "could not be read", psi.InventoryError),
        ("lock-malformed", "is malformed", RuntimeError),
        ("manifest-not-list", "#files is not a list", psi.InventoryError),
        ("carried-row-unindexed", "has a row without a string path", psi.InventoryError),
    ],
)
def test_unreadable_input_is_refused_by_naming_its_source(
    tree: Tree, break_input: str, needle: str, error: type[BaseException]
) -> None:
    """Fail-closed: an unreadable or malformed input is refused, naming where it came from."""
    if break_input == "lock":
        tree.lock_path.unlink()
    elif break_input == "manifest":
        tree.manifest_path.unlink()
    elif break_input == "carried":
        tree.carried.unlink()
    elif break_input == "lock-malformed":
        tree.lock_path.write_text("[1, 2]", encoding="utf-8")
    elif break_input == "manifest-not-list":
        _dump_json(tree.manifest_path, {"version": 1, "files": {}, "resources": []})
    else:
        _edit_rows(tree.carried, "files", _set_field("utils/func.py", "path", 42))
    tree.refuse(needle, error)
