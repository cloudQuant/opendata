"""Regression coverage for current MIT-vendor source and import isolation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.quality import vendor_independence as boundary

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = REPOSITORY_ROOT / boundary.VENDOR_REL


def _fixture_sources() -> dict[str, str]:
    return {
        "__init__.py": (
            "_EXPORTS = {'entry': "
            "('opendata.data.providers.akshare._vendor.pkg.alpha', 'run', False), "
            "'optional': ('akqmt', 'xt_api', True)}\n"
        ),
        "helper.py": "def help_value():\n    return 1\n",
        "pkg/__init__.py": "from .alpha import run\n",
        "pkg/alpha.py": (
            "from . import beta\n"
            "from .. import helper\n"
            "import json\n"
            "from requests import Session\n"
            "\n"
            "def run():\n"
            "    return beta.VALUE + helper.help_value()\n"
        ),
        "pkg/beta.py": "VALUE = 1\n",
    }


def _fixture_resources() -> dict[str, str]:
    return {"resources/schema.json": '{"version": 1}\n'}


def _write_manifest(vendor_root: Path) -> None:
    rows = []
    for path in sorted(vendor_root.rglob("*.py")):
        relative = path.relative_to(vendor_root).as_posix()
        rows.append(
            {
                "path": relative,
                "upstream_path": f"akshare/{relative}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    resource_rows = []
    for path in sorted(vendor_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == "manifest.json":
            continue
        if (
            path.suffix == ".py"
            or "__pycache__" in path.parts
            or path.name
            in {
                "upstream.lock",
                "LICENSE-AKSHARE",
            }
        ):
            continue
        relative = path.relative_to(vendor_root).as_posix()
        resource_rows.append(
            {
                "path": relative,
                "upstream_path": f"akshare/{relative}",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    (vendor_root / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "upstream": {"commit": "fixture"},
                "files": rows,
                "resources": resource_rows,
                "counts": {
                    "py_files": len(rows),
                    "resource_files": len(resource_rows),
                    "total_files": len(rows) + len(resource_rows),
                },
            }
        ),
        encoding="utf-8",
    )


def _make_vendor(vendor_root: Path, sources: dict[str, str] | None = None) -> Path:
    for relative, source in (sources or _fixture_sources()).items():
        path = vendor_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    for relative, content in _fixture_resources().items():
        path = vendor_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _write_manifest(vendor_root)
    return vendor_root


def test_fixture_allows_relative_vendor_and_third_party_imports(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")

    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )

    assert audit.valid is True, audit.issues
    assert audit.actual_python_files == len(_fixture_sources())
    assert audit.actual_resource_files == len(_fixture_resources())
    assert audit.parsed_python_files == len(_fixture_sources())
    assert audit.vendor_imports >= 2
    assert audit.third_party_imports == 2
    assert audit.facade_module_paths == 2
    assert audit.facade_vendor_module_paths == 1
    assert audit.facade_third_party_module_paths == 1
    assert audit.facade_third_party_targets == ["akqmt"]
    assert audit.ast_crossings == []


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("from opendata.core.database import connect\n", "opendata.core.database"),
        ("import opendata.data.providers.bls\n", "opendata.data.providers.bls"),
        ("import opendata_http\n", "opendata_http"),
        ("import opendata_client.api\n", "opendata_client.api"),
        ("import opendata_fuyao.api\n", "opendata_fuyao.api"),
        ("import akshare.stock\n", "akshare.stock"),
        ("import openbb_core.api\n", "openbb_core.api"),
    ],
    ids=[
        "core",
        "sibling-provider",
        "old-root",
        "client-root",
        "fuyao-root",
        "akshare-root",
        "openbb-root",
    ],
)
def test_bsl_and_legacy_imports_fail_closed(tmp_path: Path, source: str, target: str) -> None:
    sources = _fixture_sources()
    sources["pkg/alpha.py"] = source
    vendor_root = _make_vendor(tmp_path / "_vendor", sources)

    audit = boundary.audit_vendor_sources(vendor_root, expected_python_files=len(sources))

    assert audit.valid is False
    assert any(crossing.target == target for crossing in audit.ast_crossings)


def test_manifest_missing_file_identity_fails_closed(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")
    manifest_path = vendor_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].pop()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )

    assert audit.valid is False
    assert any("manifest/disk Python identities differ" in issue for issue in audit.issues)


def test_manifest_duplicate_identity_fails_closed(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")
    manifest_path = vendor_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(manifest["files"][0].copy())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )

    assert audit.valid is False
    assert any("repeats Python identity" in issue for issue in audit.issues)


def test_manifest_duplicate_upstream_identity_fails_closed(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")
    manifest_path = vendor_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][1]["upstream_path"] = manifest["files"][0]["upstream_path"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )

    assert audit.valid is False
    assert any("repeats upstream identity" in issue for issue in audit.issues)


@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "duplicate", "duplicate_upstream", "invalid_sha"]
)
def test_resource_inventory_must_match_and_have_unique_valid_hashes(
    tmp_path: Path, mutation: str
) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")
    manifest_path = vendor_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        manifest["resources"].pop()
    elif mutation == "extra":
        (vendor_root / "extra.bin").write_bytes(b"extra")
    elif mutation == "duplicate":
        manifest["resources"].append(manifest["resources"][0].copy())
    elif mutation == "duplicate_upstream":
        duplicate = manifest["resources"][0].copy()
        duplicate["path"] = "resources/alias.json"
        (vendor_root / duplicate["path"]).write_bytes(
            (vendor_root / manifest["resources"][0]["path"]).read_bytes()
        )
        manifest["resources"].append(duplicate)
        manifest["counts"]["resource_files"] = 2
        manifest["counts"]["total_files"] += 1
    else:
        manifest["resources"][0]["sha256"] = "not-a-sha256"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )

    assert audit.valid is False
    if mutation in {"missing", "extra"}:
        assert any("manifest/disk resource identities differ" in issue for issue in audit.issues)
    elif mutation == "duplicate":
        assert any("repeats resource identity" in issue for issue in audit.issues)
    elif mutation == "duplicate_upstream":
        assert any("repeats upstream identity" in issue for issue in audit.issues)
    else:
        assert any("resources[0] has an invalid path or sha256" in issue for issue in audit.issues)


def test_vendor_import_target_must_resolve_to_manifest(tmp_path: Path) -> None:
    sources = _fixture_sources()
    sources["pkg/alpha.py"] = "import opendata.data.providers.akshare._vendor.missing_module\n"
    vendor_root = _make_vendor(tmp_path / "_vendor", sources)

    audit = boundary.audit_vendor_sources(vendor_root, expected_python_files=len(sources))

    assert audit.valid is False
    assert any(
        "vendor import target is not in the manifest" in issue
        and issue.endswith("opendata.data.providers.akshare._vendor.missing_module")
        for issue in audit.issues
    )


def test_extra_and_unparseable_python_files_fail_closed(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "_vendor")
    (vendor_root / "extra.py").write_text("value = 1\n", encoding="utf-8")
    audit = boundary.audit_vendor_sources(
        vendor_root,
        expected_python_files=len(_fixture_sources()),
    )
    assert audit.valid is False
    assert any("manifest/disk Python identities differ" in issue for issue in audit.issues)

    bad_vendor = _make_vendor(tmp_path / "bad_vendor")
    bad_source = bad_vendor / "pkg" / "beta.py"
    bad_source.write_text("def (\n", encoding="utf-8")
    _write_manifest(bad_vendor)
    bad_audit = boundary.audit_vendor_sources(
        bad_vendor,
        expected_python_files=len(_fixture_sources()),
    )
    assert bad_audit.valid is False
    assert any("cannot be parsed" in issue for issue in bad_audit.issues)


def test_relative_import_that_escapes_vendor_fails_closed(tmp_path: Path) -> None:
    sources = _fixture_sources()
    sources["pkg/alpha.py"] = "from ......core.database import connect\n"
    vendor_root = _make_vendor(tmp_path / "_vendor", sources)

    audit = boundary.audit_vendor_sources(vendor_root, expected_python_files=len(sources))

    assert audit.valid is False
    assert any(crossing.target == "opendata.core.database" for crossing in audit.ast_crossings)


def test_facade_module_path_cannot_bypass_into_bsl(tmp_path: Path) -> None:
    sources = _fixture_sources()
    sources["__init__.py"] = "_EXPORTS = {'bad': ('opendata.core.database', 'connect', True)}\n"
    vendor_root = _make_vendor(tmp_path / "_vendor", sources)

    audit = boundary.audit_vendor_sources(vendor_root, expected_python_files=len(sources))

    assert audit.valid is False
    assert any(
        crossing.kind == "facade-export" and crossing.target == "opendata.core.database"
        for crossing in audit.ast_crossings
    )


def test_isolation_copy_uses_namespace_ancestors_without_bsl_init(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    vendor_root = checkout / boundary.VENDOR_REL
    _make_vendor(vendor_root)
    for ancestor in boundary.NAMESPACE_ANCESTORS:
        relative = Path(*ancestor.split("."))
        init_path = checkout / relative / "__init__.py"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        init_path.write_text("# BSL ancestor marker\n", encoding="utf-8")
    resource = vendor_root / "stock_feature" / "ths.js"
    resource.parent.mkdir(parents=True)
    resource.write_text("// vendor resource\n", encoding="utf-8")

    isolation_root = tmp_path / "isolated"
    copied_vendor = boundary.copy_vendor_subtree(vendor_root, isolation_root)

    assert (copied_vendor / "__init__.py").is_file()
    assert (copied_vendor / "stock_feature" / "ths.js").read_text(encoding="utf-8") == (
        "// vendor resource\n"
    )
    for ancestor in boundary.NAMESPACE_ANCESTORS:
        copied_ancestor = isolation_root.joinpath(*ancestor.split("."))
        assert not (copied_ancestor / "__init__.py").exists()
    assert boundary.validate_isolation_namespace_tree(isolation_root) == []


def test_isolation_namespace_preflight_rejects_bsl_python(tmp_path: Path) -> None:
    vendor_root = _make_vendor(tmp_path / "checkout" / boundary.VENDOR_REL)
    isolation_root = tmp_path / "isolated"
    boundary.copy_vendor_subtree(vendor_root, isolation_root)
    ancestor = isolation_root / "opendata"
    ancestor_init = ancestor / "__init__.py"
    ancestor_init.write_text("# unexpected BSL package\n", encoding="utf-8")
    non_vendor_source = ancestor / "foreign.py"
    non_vendor_source.write_text("# unexpected source\n", encoding="utf-8")

    issues = boundary.validate_isolation_namespace_tree(isolation_root)

    assert any("contains BSL __init__.py" in issue for issue in issues)
    assert any("contains non-vendor Python" in issue for issue in issues)


def test_isolation_run_stops_before_child_on_namespace_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vendor_root = _make_vendor(tmp_path / "checkout" / boundary.VENDOR_REL)
    original_copy = boundary.copy_vendor_subtree

    def contaminated_copy(source: Path, isolation_root: Path) -> Path:
        copied = original_copy(source, isolation_root)
        (isolation_root / "opendata" / "__init__.py").write_text(
            "# unexpected ancestor package\n",
            encoding="utf-8",
        )
        return copied

    def unexpected_child(*args: object, **kwargs: object) -> None:
        pytest.fail("isolated interpreter must not start with a contaminated namespace")

    monkeypatch.setattr(boundary, "copy_vendor_subtree", contaminated_copy)
    monkeypatch.setattr(boundary.subprocess, "run", unexpected_child)

    audit = boundary.run_isolated_import_check(vendor_root, REPOSITORY_ROOT)

    assert audit.valid is False
    assert audit.status == "invalid_isolation_namespace_tree"
    assert any("contains BSL __init__.py" in issue for issue in audit.issues)


def test_isolation_rejects_a_negative_control_that_imports(tmp_path: Path) -> None:
    isolation_root = (tmp_path / "copy").resolve()
    vendor_root = isolation_root.joinpath(*boundary.VENDOR_PREFIX.split("."))
    locations = {
        name: str(isolation_root.joinpath(*name.split(".")))
        for name in boundary.NAMESPACE_ANCESTORS
    }
    entry_paths = {
        name: vendor_root / f"{index}.py" for index, name in enumerate(boundary.ENTRY_IMPORTS)
    }
    report = {
        "valid": True,
        "issues": [],
        "imports": {
            name: {
                "status": "imported",
                "file": str(entry_paths[name]),
                "origin": str(entry_paths[name]),
            }
            for name in boundary.ENTRY_IMPORTS
        },
        "controls": {
            "core_database": {"status": "imported"},
            "sibling_provider": {"status": "blocked_import_error"},
            "client_root": {
                "status": "blocked_import_error",
                "error_type": "ImportError",
                "blocked_module": "opendata_client",
            },
        },
        "ancestors": {
            name: {
                "file": None,
                "origin": None,
                "search_locations": [location],
                "expected_location": location,
            }
            for name, location in locations.items()
        },
        "loaded_opendata": {
            **{
                name: {
                    "file": None,
                    "origin": None,
                    "search_locations": [location],
                }
                for name, location in locations.items()
            },
            **{
                name: {
                    "file": str(entry_paths[name]),
                    "origin": str(entry_paths[name]),
                    "search_locations": [],
                }
                for name in boundary.ENTRY_IMPORTS
            },
        },
        "network_attempts": [],
        "repository_path_leaks": [],
        "cwd": str(isolation_root),
        "sys_path": [str(isolation_root), "/python/lib"],
        "baseline_sys_path": ["/python/lib"],
    }

    issues = boundary.validate_isolation_report(report, REPOSITORY_ROOT)

    assert any("negative control" in issue for issue in issues)


def test_current_vendor_manifest_and_three_isolated_imports_pass() -> None:
    tree = boundary.audit_vendor_sources(
        VENDOR_ROOT,
        expected_python_files=325,
        expected_resource_files=2,
    )
    assert tree.valid is True, tree.issues
    assert tree.actual_python_files == 325
    assert tree.manifest_python_files == 325
    assert tree.actual_resource_files == 2
    assert tree.manifest_resource_files == 2
    assert tree.parsed_python_files == 325
    assert tree.facade_vendor_module_paths == 986
    assert tree.facade_third_party_module_paths == 1
    assert tree.facade_third_party_targets == ["akqmt"]
    assert tree.source_pins["upstream_commit"] == "c4f6a631c259783dbc2507b6b27d179b3e88079d"

    isolation = boundary.run_isolated_import_check(VENDOR_ROOT, REPOSITORY_ROOT)

    assert isolation.valid is True, isolation.issues
    assert set(isolation.imports) == set(boundary.ENTRY_IMPORTS)
    assert all(item["status"] == "imported" for item in isolation.imports.values())
    assert set(isolation.controls) == set(boundary.CONTROL_IMPORTS)
    assert all(item["status"] == "blocked_import_error" for item in isolation.controls.values())
    assert isolation.controls["client_root"]["blocked_module"] == "opendata_client"
    assert set(isolation.ancestors) == set(boundary.NAMESPACE_ANCESTORS)
    assert isolation.sys_path == [str(Path(isolation.cwd)), *isolation.baseline_sys_path]
    assert isolation.repository_path_leaks == []
    assert isolation.network_attempts == []
