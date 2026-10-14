from __future__ import annotations

import json
import tarfile
from io import BytesIO
from typing import TYPE_CHECKING, Any
from zipfile import ZIP_DEFLATED, ZipFile

from scripts.quality import runtime_package_probe as probe

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _minimal_source_tree(root: Path) -> None:
    for filename in probe.SOURCE_FILES:
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{filename}\n", encoding="utf-8")
    (root / "opendata" / "data").mkdir(parents=True)
    (root / "opendata" / "__init__.py").write_text('"""test package"""\n', encoding="utf-8")
    (root / "opendata" / "data" / "domains.yaml").write_text("domains: []\n", encoding="utf-8")


def _wheel_with_required_members(tmp_path: Path) -> tuple[Path, Path]:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    root_license = (
        b"Business Source License 1.1\n\n"
        b"This is the complete fixture root license text.\n"
        b"Additional legal terms are included here so the vendor scan must match\n"
        b"the whole file and cannot reject a short mention of BSL-1.1.\n"
    )
    (snapshot / "LICENSE").write_bytes(root_license)
    (snapshot / "LICENSE-AKSHARE").write_bytes(b"AKShare license\n")
    (snapshot / "pyproject.toml").write_text(
        """[project]
name = "opendata-fixture"
version = "0.1.0"
dependencies = [
    "requests>=2.31.0",
    "uvicorn[standard]>=0.32.0; python_version >= '3.10'",
]

[project.optional-dependencies]
dev = ["ruff==0.15.20"]
""",
        encoding="utf-8",
    )
    vendor_license = snapshot / "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE"
    vendor_license.parent.mkdir(parents=True)
    vendor_license.write_bytes(b"AKShare license\n")
    (vendor_license.parent / "LEGAL-NOTICE.txt").write_text(
        "The root project uses BSL-1.1; this text is not a copied license.\n",
        encoding="utf-8",
    )

    artifact = tmp_path / "opendata_fixture-0.1.0-py3-none-any.whl"
    dist_info = "opendata_fixture-0.1.0.dist-info"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            """Metadata-Version: 2.1
Name: opendata-fixture
Version: 0.1.0
Requires-Dist: requests>=2.31.0
Requires-Dist: uvicorn[standard]>=0.32.0; python_version >= "3.10"
Provides-Extra: dev
Requires-Dist: ruff==0.15.20; extra == "dev"

""",
        )
        archive.writestr(f"{dist_info}/licenses/LICENSE", root_license)
        archive.writestr(f"{dist_info}/licenses/LICENSE-AKSHARE", b"AKShare license\n")
        archive.writestr(
            "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
            b"AKShare license\n",
        )
        archive.writestr(
            "opendata/data/providers/akshare/_vendor/LEGAL-NOTICE.txt",
            "The root project uses BSL-1.1; this text is not a copied license.\n",
        )
        for relative in probe.REQUIRED_PACKAGE_RESOURCES:
            path = snapshot / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"resource:{relative}\n".encode()
            path.write_bytes(payload)
            archive.writestr(relative, payload)
    return artifact, snapshot


def _sdist_with_contract_members(tmp_path: Path, snapshot: Path) -> Path:
    artifact = tmp_path / "opendata-fixture-0.1.0.tar.gz"
    metadata = (
        b"Metadata-Version: 2.1\n"
        b"Name: opendata-fixture\n"
        b"Version: 0.1.0\n"
        b"Requires-Dist: requests>=2.31.0\n"
        b'Requires-Dist: uvicorn[standard]>=0.32.0; python_version >= "3.10"\n'
        b"Provides-Extra: dev\n"
        b'Requires-Dist: ruff==0.15.20; extra == "dev"\n\n'
    )
    members = [
        "pyproject.toml",
        "LICENSE",
        "LICENSE-AKSHARE",
        "PKG-INFO",
        "opendata.egg-info/PKG-INFO",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/LEGAL-NOTICE.txt",
        *probe.REQUIRED_PACKAGE_RESOURCES,
    ]
    with tarfile.open(artifact, "w:gz") as archive:
        root = tarfile.TarInfo("opendata-fixture-0.1.0/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        root.mtime = 0
        archive.addfile(root)
        for relative in members:
            payload = (
                metadata if relative.endswith("PKG-INFO") else (snapshot / relative).read_bytes()
            )
            member = tarfile.TarInfo(f"opendata-fixture-0.1.0/{relative}")
            member.size = len(payload)
            member.mode = 0o644
            member.mtime = 0
            archive.addfile(member, BytesIO(payload))
    return artifact


def _synthetic_installed_package(site: Path, *, root_code: str = "") -> Path:
    package_files = {
        "opendata/__init__.py": root_code,
        "opendata/data/__init__.py": '"""synthetic installed package"""\n',
        "opendata/data/providers/__init__.py": '"""synthetic providers"""\n',
        "opendata/data/providers/akshare/__init__.py": '"""synthetic provider"""\n',
        "opendata/data/providers/akshare/_vendor/__init__.py": '"""synthetic vendor"""\n',
        "opendata/main.py": 'from pathlib import Path\nPath("main-imported").touch()\n',
        "opendata/lifespan.py": 'from pathlib import Path\nPath("lifespan-imported").touch()\n',
        "opendata-0.0.1.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: opendata\nVersion: 0.0.1\n\n"
        ),
    }
    for relative, content in package_files.items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return site


def _run_synthetic_smoke(
    site: Path,
    workspace: Path,
    *,
    stage: str = "synthetic_smoke",
    state: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    snapshot = workspace / "snapshot"
    snapshot.mkdir(parents=True)
    work_dir = workspace / "work"
    work_dir.mkdir()
    run_dir = workspace / "evidence"
    run_state = (
        state
        if state is not None
        else {"commands": {}, "installations": {}, "positive_assertions": []}
    )
    report = probe._run_smoke(
        state=run_state,
        stage=stage,
        venv_site=site,
        snapshot_root=snapshot,
        work_dir=work_dir,
        resource_hashes={},
        timeout_seconds=30,
        run_dir=run_dir,
    )
    return report, run_dir


def test_snapshot_fingerprint_binds_relative_paths_and_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _minimal_source_tree(source)

    original = probe._source_manifest(source)
    same = probe._source_manifest(source)
    assert original == same

    resource = source / "opendata" / "data" / "domains.yaml"
    resource.write_text("domains: [changed]\n", encoding="utf-8")
    changed = probe._source_manifest(source)
    assert changed["fingerprint"] != original["fingerprint"]

    resource.rename(source / "opendata" / "data" / "renamed.yaml")
    renamed = probe._source_manifest(source)
    assert renamed["fingerprint"] != changed["fingerprint"]


def test_source_snapshot_copies_whitelist_and_excludes_generated_files(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    _minimal_source_tree(source)
    (source / "opendata" / "__pycache__").mkdir()
    (source / "opendata" / "__pycache__" / "stale.pyc").write_bytes(b"cache")
    (source / "unrelated.txt").write_text("not in package build context", encoding="utf-8")

    snapshot = tmp_path / "external" / "source"
    snapshot.parent.mkdir()
    probe._copy_source_snapshot(source, snapshot)

    snapshot_hash = probe._source_manifest(snapshot)["fingerprint"]
    source_hash = probe._source_manifest(source)["fingerprint"]
    assert snapshot_hash == source_hash
    assert not (snapshot / "unrelated.txt").exists()
    assert not (snapshot / "opendata" / "__pycache__").exists()


def test_build_resource_allowlist_is_exact_seven_container_contract_members() -> None:
    assert len(probe.REQUIRED_PACKAGE_RESOURCES) == 7
    assert set(probe.REQUIRED_PACKAGE_RESOURCES) == {
        "opendata/data/providers/akshare/_vendor/manifest.json",
        "opendata/data/providers/akshare/_vendor/upstream.lock",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/file_fold/calendar.json",
        "opendata/data/providers/akshare/_vendor/stock_feature/ths.js",
        "opendata/data/providers/ths/endpoint_map.yaml",
        "opendata/data/providers/ths/transport/error_messages.yaml",
    }


def test_isolated_pip_targets_venv_without_switching_base_interpreter(tmp_path: Path) -> None:
    venv_python = tmp_path / "install-target" / "bin" / "python"

    command = probe._pip_command(venv_python, "check")

    assert command[:5] == [
        probe.sys.executable,
        "-m",
        "pip",
        "--python",
        str(venv_python),
    ]
    assert command[-1] == "check"


def test_clean_install_environment_removes_source_and_private_pip_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    monkeypatch.setenv("PYTHONHOME", "/python")
    monkeypatch.setenv("PIP_TARGET", "/global/site-packages")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://other.invalid/simple")
    monkeypatch.setenv("COV_CORE_SOURCE", "opendata")

    environment = probe._clean_environment(isolated_pip=True)

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "PIP_TARGET" not in environment
    assert "COV_CORE_SOURCE" not in environment
    assert "secret" not in environment["PIP_INDEX_URL"]
    assert environment["PIP_INDEX_URL"] == "https://pypi.org/simple"
    assert "PIP_EXTRA_INDEX_URL" not in environment


def test_timeout_argument_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--timeout-seconds", "0"]) == 2
    assert "must be positive" in capsys.readouterr().err


def test_archive_checker_rejects_removed_required_resource_copy(tmp_path: Path) -> None:
    artifact, snapshot = _wheel_with_required_members(tmp_path)
    source_hash = probe._sha256_file(artifact)
    mutated = tmp_path / "counterfactual" / "missing-resource.whl"

    probe._write_wheel_copy(
        artifact,
        mutated,
        remove_member=probe.REQUIRED_PACKAGE_RESOURCES[0],
    )
    report = probe._inspect_archive(artifact=mutated, kind="wheel", snapshot_root=snapshot)

    assert report["accepted"] is False
    assert "contains_seven_required_resources" in report["rejection_reasons"]
    assert report["missing_package_resources"] == [probe.REQUIRED_PACKAGE_RESOURCES[0]]
    assert probe._sha256_file(artifact) == source_hash


def test_archive_checker_rejects_replaced_license_bytes_in_copy(tmp_path: Path) -> None:
    artifact, snapshot = _wheel_with_required_members(tmp_path)
    source_hash = probe._sha256_file(artifact)
    license_member = "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE"
    mutated = tmp_path / "counterfactual" / "license-mismatch.whl"

    probe._write_wheel_copy(
        artifact,
        mutated,
        replacements={license_member: b"different license bytes\n"},
    )
    report = probe._inspect_archive(artifact=mutated, kind="wheel", snapshot_root=snapshot)
    license_check = next(
        check
        for check in report["license_content_hash_checks"]
        if check["archive_member"] == license_member
    )

    assert report["accepted"] is False
    assert "license_files_match_source_hashes" in report["rejection_reasons"]
    assert license_check["status"] == "FAIL"
    assert probe._sha256_file(artifact) == source_hash


def test_archive_checker_compares_full_requires_dist_contract(tmp_path: Path) -> None:
    artifact, snapshot = _wheel_with_required_members(tmp_path)
    baseline = probe._inspect_archive(artifact=artifact, kind="wheel", snapshot_root=snapshot)
    assert baseline["checks"]["metadata_dependencies_match_pyproject"] is True
    assert baseline["metadata_dependency_checks"][0]["expected_provides_extra"] == ["dev"]

    metadata_name = baseline["metadata_dependency_checks"][0]["archive_member"]
    with ZipFile(artifact) as archive:
        metadata = archive.read(metadata_name)
    changed_metadata = metadata.replace(
        b'uvicorn[standard]>=0.32.0; python_version >= "3.10"',
        b'uvicorn[other]>=0.32.0; python_version >= "3.10"',
    )
    mutated = tmp_path / "counterfactual" / "wrong-dependency-extra.whl"
    probe._write_wheel_copy(artifact, mutated, replacements={metadata_name: changed_metadata})
    report = probe._inspect_archive(artifact=mutated, kind="wheel", snapshot_root=snapshot)
    requirements = report["metadata_dependency_checks"][0]

    assert report["accepted"] is False
    assert "metadata_dependencies_match_pyproject" in report["rejection_reasons"]
    assert any(
        item["name"] == "uvicorn" and item["extras"] == ["other"]
        for item in requirements["unexpected_requires_dist"]
    )
    assert any(
        item["name"] == "uvicorn" and item["extras"] == ["standard"]
        for item in requirements["missing_requires_dist"]
    )

    changed_marker = metadata.replace(b'python_version >= "3.10"', b'python_version >= "3.11"')
    marker_mutation = tmp_path / "counterfactual" / "wrong-dependency-marker.whl"
    probe._write_wheel_copy(artifact, marker_mutation, replacements={metadata_name: changed_marker})
    marker_report = probe._inspect_archive(
        artifact=marker_mutation,
        kind="wheel",
        snapshot_root=snapshot,
    )
    marker_requirements = marker_report["metadata_dependency_checks"][0]
    assert marker_report["accepted"] is False
    assert any(
        item["name"] == "uvicorn" and 'python_version >= "3.10"' in item["marker"]
        for item in marker_requirements["missing_requires_dist"]
    )
    assert any(
        item["name"] == "uvicorn" and 'python_version >= "3.11"' in item["marker"]
        for item in marker_requirements["unexpected_requires_dist"]
    )


def test_archive_checker_rejects_full_bsl_text_but_allows_a_license_reference(
    tmp_path: Path,
) -> None:
    artifact, snapshot = _wheel_with_required_members(tmp_path)
    baseline = probe._inspect_archive(artifact=artifact, kind="wheel", snapshot_root=snapshot)
    assert baseline["accepted"] is True
    assert baseline["bsl_full_text_misplacements"] == []

    member = f"{probe.MIT_VENDOR_PREFIX}BSL-LICENSE"
    mutated = tmp_path / "counterfactual" / "misplaced-bsl-license.whl"
    probe._write_wheel_copy(
        artifact,
        mutated,
        additions={member: (snapshot / "LICENSE").read_bytes()},
    )
    report = probe._inspect_archive(artifact=mutated, kind="wheel", snapshot_root=snapshot)

    assert report["accepted"] is False
    assert "has_no_full_bsl_license_text_in_mit_vendor_tree" in report["rejection_reasons"]
    assert report["bsl_full_text_misplacements"] == [
        {
            "archive_member": member,
            "sha256": probe._sha256_file(snapshot / "LICENSE"),
            "match": "complete normalized root LICENSE text",
        }
    ]


def test_sdist_checker_validates_pyproject_distribution_metadata_and_license_tree(
    tmp_path: Path,
) -> None:
    wheel, snapshot = _wheel_with_required_members(tmp_path)
    del wheel
    artifact = _sdist_with_contract_members(tmp_path, snapshot)
    baseline = probe._inspect_archive(artifact=artifact, kind="sdist", snapshot_root=snapshot)
    assert baseline["accepted"] is True
    assert baseline["checks"]["metadata_dependencies_match_pyproject"] is True
    assert baseline["checks"]["sdist_pyproject_dependencies_match_source"] is True

    with tarfile.open(artifact, "r:gz") as source:
        pyproject = source.extractfile("opendata-fixture-0.1.0/pyproject.toml")
        assert pyproject is not None
        changed_pyproject = pyproject.read().replace(b'"requests>=2.31.0",\n', b"")
    wrong_project = tmp_path / "counterfactual" / "sdist-wrong-pyproject.tar.gz"
    probe._write_sdist_copy(
        artifact,
        wrong_project,
        replacements={"pyproject.toml": changed_pyproject},
    )
    wrong_project_report = probe._inspect_archive(
        artifact=wrong_project,
        kind="sdist",
        snapshot_root=snapshot,
    )
    assert wrong_project_report["accepted"] is False
    assert "sdist_pyproject_dependencies_match_source" in wrong_project_report["rejection_reasons"]

    with tarfile.open(artifact, "r:gz") as source:
        pkg_info = source.extractfile("opendata-fixture-0.1.0/PKG-INFO")
        assert pkg_info is not None
        changed_pkg_info, removed = probe._remove_metadata_dependency(pkg_info.read(), "requests")
    assert removed == ["requests>=2.31.0"]
    wrong_metadata = tmp_path / "counterfactual" / "sdist-wrong-pkg-info.tar.gz"
    probe._write_sdist_copy(
        artifact,
        wrong_metadata,
        replacements={"PKG-INFO": changed_pkg_info},
    )
    wrong_metadata_report = probe._inspect_archive(
        artifact=wrong_metadata,
        kind="sdist",
        snapshot_root=snapshot,
    )
    assert wrong_metadata_report["accepted"] is False
    assert "metadata_dependencies_match_pyproject" in wrong_metadata_report["rejection_reasons"]

    misplaced_member = f"{probe.MIT_VENDOR_PREFIX}BSL-LICENSE"
    wrong_license = tmp_path / "counterfactual" / "sdist-misplaced-bsl.tar.gz"
    probe._write_sdist_copy(
        artifact,
        wrong_license,
        additions={misplaced_member: (snapshot / "LICENSE").read_bytes()},
    )
    wrong_license_report = probe._inspect_archive(
        artifact=wrong_license,
        kind="sdist",
        snapshot_root=snapshot,
    )
    assert wrong_license_report["accepted"] is False
    assert (
        "has_no_full_bsl_license_text_in_mit_vendor_tree"
        in wrong_license_report["rejection_reasons"]
    )


def test_installed_smoke_rejects_source_path_pollution(tmp_path: Path) -> None:
    workspace = tmp_path / "opendata-runtime-package-source-pollution"
    installed_site = workspace / "installed"
    for relative in (
        "opendata/__init__.py",
        "opendata/data/__init__.py",
        "opendata/data/providers/__init__.py",
        "opendata/data/providers/akshare/__init__.py",
        "opendata/data/providers/akshare/_vendor/__init__.py",
    ):
        path = installed_site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""test package"""\n', encoding="utf-8")
    snapshot = workspace / "source-snapshot"
    (snapshot / "opendata").mkdir(parents=True)
    (snapshot / "opendata" / "__init__.py").write_text('"""source package"""\n', encoding="utf-8")
    work_dir = workspace / "work"
    work_dir.mkdir(parents=True)
    run_dir = workspace / "evidence"
    state: dict[str, Any] = {"commands": {}, "counterfactuals": []}

    probe._run_source_path_pollution_counterfactual(
        state=state,
        stage="source_path_pollution_smoke",
        venv_site=installed_site,
        snapshot_root=snapshot,
        work_dir=work_dir,
        resource_hashes={},
        timeout_seconds=30,
        run_dir=run_dir,
    )

    record = state["counterfactuals"][0]
    assert record["status"] == "PASS"
    assert record["observed"]["command_exit_code"] != 0
    stdout_path = run_dir / record["observed"]["stdout"]["path"]
    assert "source checkout/snapshot leaked onto sys.path" in stdout_path.read_text()


def test_installed_smoke_installs_io_guard_before_synthetic_product_imports(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "opendata-runtime-package-clean-smoke"
    site = _synthetic_installed_package(workspace / "site-packages")

    report, _ = _run_synthetic_smoke(site, workspace)

    assert report["schema"] == probe.SMOKE_REPORT_SCHEMA
    assert report["product_import_names"] == list(probe.SMOKE_PRODUCT_IMPORTS)
    assert report["io_guard"]["installed_before_product_imports"] is True
    assert report["io_guard"]["product_imports_started_after_guard"] is True
    assert report["io_guard"]["attempt_counts"] == dict.fromkeys(probe.SMOKE_GUARD_COUNTERS, 0)
    assert report["io_guard"]["attempts_total"] == 0
    assert report["io_guard"]["coverage"] == probe.SMOKE_GUARD_COVERAGE
    assert set(report["product_modules"]) == {
        "opendata",
        "opendata.data",
        "opendata.data.providers",
        "opendata.data.providers.akshare",
        "opendata.data.providers.akshare._vendor",
    }
    assert report["distribution_version"] == "0.0.1"


def test_installed_smoke_fails_when_synthetic_import_swallows_guard_errors(
    tmp_path: Path,
) -> None:
    """Emit audit events only; the synthetic package performs no real I/O."""
    root_code = """
import importlib
import sys

def swallow(callback):
    try:
        callback()
    except BaseException:
        pass

for event in (
    "socket.getaddrinfo",
    "socket.connect",
    "subprocess.Popen",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.fork",
    "sqlite3.connect",
):
    swallow(lambda event=event: sys.audit(event))

swallow(lambda: importlib.import_module("sqlite3"))
swallow(lambda: importlib.import_module("opendata.main"))
swallow(lambda: importlib.import_module("opendata.lifespan"))
"""
    workspace = tmp_path / "opendata-runtime-package-guard-counterfactual"
    site = _synthetic_installed_package(workspace / "site-packages", root_code=root_code)
    state: dict[str, Any] = {
        "commands": {},
        "installations": {},
        "positive_assertions": [],
    }

    try:
        _run_synthetic_smoke(site, workspace, state=state)
    except probe.ProbeFailureError as exc:
        assert "blocked I/O attempts" in str(exc)
    else:
        raise AssertionError("smoke passed despite caught guard rejections")

    stdout_path = workspace / "evidence" / "logs" / "synthetic_smoke.stdout.txt"
    report = json.loads(stdout_path.read_text(encoding="utf-8"))
    counts = report["io_guard"]["attempt_counts"]
    assert counts["socket"] == 2
    assert counts["process"] == 5
    assert counts["database"] == 2
    assert counts["entrypoint_import"] == 2
    assert report["io_guard"]["attempts_total"] == sum(counts.values())
    assert state["commands"]["synthetic_smoke"]["exit_code"] == 1
    assert (workspace / "work" / "main-imported").exists() is False
    assert (workspace / "work" / "lifespan-imported").exists() is False


def test_run_smoke_rejects_missing_guard_record_and_noninteger_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for case, guard in (
        ("missing", None),
        (
            "noninteger",
            {
                "installed": True,
                "installed_before_product_imports": True,
                "product_imports_started_after_guard": True,
                "installation_error_type": None,
                "coverage": probe.SMOKE_GUARD_COVERAGE,
                "attempt_counts": {
                    "database": 0,
                    "entrypoint_import": 0,
                    "process": 0,
                    "socket": True,
                },
                "attempts_total": 0,
                "blocked_attempts": [],
            },
        ),
    ):
        workspace = tmp_path / f"opendata-runtime-package-{case}-guard-report"
        site = workspace / "site-packages"
        site.mkdir(parents=True)
        report: dict[str, Any] = {
            "schema": probe.SMOKE_REPORT_SCHEMA,
            "product_import_names": list(probe.SMOKE_PRODUCT_IMPORTS),
            "base_python_executable": probe.sys.executable,
            "base_python_version": "synthetic Python version",
            "isolated_site_packages": str(site.resolve()),
            "distribution_version": "0.0.1",
            "resources": {},
            "sys_path_source_leaks": [],
        }
        if guard is not None:
            report["io_guard"] = guard

        def write_smoke_report(
            *,
            state: dict[str, Any],
            name: str,
            command: list[str],
            cwd: Path,
            environment: dict[str, str],
            timeout_seconds: int,
            run_dir: Path,
            report_for_case: dict[str, Any] = report,
        ) -> probe.CommandRecord:
            del state, environment
            log_path = run_dir / "logs" / f"{name}.stdout.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stdout = json.dumps(report_for_case)
            log_path.write_text(stdout, encoding="utf-8")
            stderr_path = run_dir / "logs" / f"{name}.stderr.txt"
            stderr_path.write_text("", encoding="utf-8")
            return {
                "argv": command,
                "cwd": str(cwd),
                "started_at_utc": "synthetic",
                "finished_at_utc": "synthetic",
                "duration_seconds": 0.0,
                "timeout_seconds": timeout_seconds,
                "timed_out": False,
                "exit_code": 0,
                "stdout": {
                    "path": f"logs/{name}.stdout.txt",
                    "size_bytes": len(stdout.encode("utf-8")),
                    "sha256": probe._sha256_bytes(stdout.encode("utf-8")),
                },
                "stderr": {
                    "path": f"logs/{name}.stderr.txt",
                    "size_bytes": 0,
                    "sha256": probe._sha256_bytes(b""),
                },
            }

        monkeypatch.setattr(probe, "_run_command", write_smoke_report)
        try:
            probe._run_smoke(
                state={"commands": {}, "installations": {}, "positive_assertions": []},
                stage="synthetic_smoke",
                venv_site=site,
                snapshot_root=workspace / "snapshot",
                work_dir=workspace / "work",
                resource_hashes={},
                timeout_seconds=30,
                run_dir=workspace / "evidence",
            )
        except probe.ProbeFailureError as exc:
            expected = (
                "lacks its I/O guard" if guard is None else "counter is not a non-negative integer"
            )
            assert expected in str(exc)
        else:
            raise AssertionError(f"smoke accepted malformed {case} guard evidence")


def test_artifact_roles_keep_same_named_wheels_separate(tmp_path: Path) -> None:
    source_build = tmp_path / "source" / "opendata.whl"
    rebuilt = tmp_path / "rebuilt" / "opendata.whl"
    source_build.parent.mkdir()
    rebuilt.parent.mkdir()
    source_build.write_bytes(b"source wheel")
    rebuilt.write_bytes(b"rebuilt wheel")
    run_dir = tmp_path / "run"

    source_record = probe._record_artifact(source_build, "source-build-wheel", run_dir)
    rebuilt_record = probe._record_artifact(rebuilt, "sdist-rebuilt-wheel", run_dir)

    source_copy = run_dir / source_record["path"]
    rebuilt_copy = run_dir / rebuilt_record["path"]
    assert source_copy != rebuilt_copy
    assert source_copy.read_bytes() == b"source wheel"
    assert rebuilt_copy.read_bytes() == b"rebuilt wheel"


def test_archive_counterfactuals_record_positive_baseline_and_negative_rejections(
    tmp_path: Path,
) -> None:
    artifact, snapshot = _wheel_with_required_members(tmp_path)
    run_dir = tmp_path / "evidence"
    state: dict[str, Any] = {"positive_assertions": [], "counterfactuals": []}

    probe._run_archive_counterfactuals(
        state=state,
        artifact=artifact,
        snapshot_root=snapshot,
        run_dir=run_dir,
    )

    assert state["positive_assertions"] == [
        {
            "name": "counterfactual_baseline_wheel_is_accepted",
            "status": "PASS",
            "observed": {
                "artifact_sha256": probe._sha256_file(artifact),
                "checks": {
                    "contains_seven_required_resources": True,
                    "excludes_separate_client_and_removed_packages": True,
                    "license_files_match_source_hashes": True,
                    "has_exactly_one_metadata_member": True,
                    "metadata_dependencies_match_pyproject": True,
                    "sdist_pyproject_dependencies_match_source": True,
                    "has_no_full_bsl_license_text_in_mit_vendor_tree": True,
                },
            },
            "expected": {
                "accepted": True,
                "checks": {
                    "contains_seven_required_resources": True,
                    "excludes_separate_client_and_removed_packages": True,
                    "license_files_match_source_hashes": True,
                    "has_exactly_one_metadata_member": True,
                    "metadata_dependencies_match_pyproject": True,
                    "sdist_pyproject_dependencies_match_source": True,
                    "has_no_full_bsl_license_text_in_mit_vendor_tree": True,
                },
            },
        }
    ]
    assert [item["status"] for item in state["counterfactuals"]] == [
        "PASS",
        "PASS",
        "PASS",
        "PASS",
    ]


def test_archive_counterfactuals_include_sdist_dependency_and_license_mutations(
    tmp_path: Path,
) -> None:
    wheel, snapshot = _wheel_with_required_members(tmp_path)
    sdist = _sdist_with_contract_members(tmp_path, snapshot)
    state: dict[str, Any] = {"positive_assertions": [], "counterfactuals": []}

    probe._run_archive_counterfactuals(
        state=state,
        artifact=wheel,
        snapshot_root=snapshot,
        run_dir=tmp_path / "evidence",
        sdist_artifact=sdist,
    )

    names = {item["name"] for item in state["counterfactuals"]}
    assert "sdist_required_runtime_dependency_removed_rejected" in names
    assert "sdist_full_bsl_license_in_mit_vendor_tree_rejected" in names
    assert all(item["status"] == "PASS" for item in state["counterfactuals"])
    assert probe._inspect_archive(artifact=sdist, kind="sdist", snapshot_root=snapshot)["accepted"]
