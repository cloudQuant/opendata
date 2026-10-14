"""Unit checks for the AC2-20 independent SDK package probe."""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from typing import TYPE_CHECKING
from zipfile import ZipFile

from scripts.quality import sdk_package_probe as probe

if TYPE_CHECKING:
    from pathlib import Path


def _minimal_sdk_tree(root: Path) -> None:
    for name in probe.SOURCE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n", encoding="utf-8")
    package = root / probe.PACKAGE_DIRECTORY
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""fixture package"""\n', encoding="utf-8")
    (package / "client.py").write_text("class Client: pass\n", encoding="utf-8")


def _metadata() -> str:
    return """Metadata-Version: 2.1
Name: opendata-client
Version: 0.1.0
Requires-Python: >=3.10
Provides-Extra: frames
Provides-Extra: ws
Requires-Dist: httpx>=0.27
Requires-Dist: loguru>=0.7
Requires-Dist: pandas>=2.0; extra == "frames"
Requires-Dist: websockets>=11.0; extra == "ws"
"""


def test_source_manifest_binds_relative_paths_and_content(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _minimal_sdk_tree(source)

    original = probe._source_manifest(source)
    assert probe._source_manifest(source) == original

    client_file = source / probe.PACKAGE_DIRECTORY / "client.py"
    client_file.write_text("class Client: pass  # updated\n", encoding="utf-8")
    changed = probe._source_manifest(source)
    assert changed["fingerprint"] != original["fingerprint"]

    client_file.rename(client_file.with_name("renamed.py"))
    renamed = probe._source_manifest(source)
    assert renamed["fingerprint"] != changed["fingerprint"]


def test_source_snapshot_excludes_caches_and_unrelated_checkout_files(tmp_path: Path) -> None:
    source = tmp_path / "checkout" / "opendata_client"
    source.mkdir(parents=True)
    _minimal_sdk_tree(source)
    (source / "unrelated.txt").write_text("not part of SDK", encoding="utf-8")
    cache = source / probe.PACKAGE_DIRECTORY / "__pycache__"
    cache.mkdir()
    (cache / "old.pyc").write_bytes(b"cache")

    snapshot = tmp_path / "external" / "snapshot"
    snapshot.parent.mkdir()
    probe._copy_source_snapshot(source, snapshot)

    assert (
        probe._source_manifest(source)["fingerprint"]
        == probe._source_manifest(snapshot)["fingerprint"]
    )
    assert not (snapshot / "unrelated.txt").exists()
    assert not (snapshot / probe.PACKAGE_DIRECTORY / "__pycache__").exists()


def test_metadata_contract_accepts_exact_runtime_and_optional_requirements() -> None:
    assert probe._metadata_contract_issues(_metadata()) == []


def test_metadata_contract_rejects_missing_dependency_and_wrong_optional_marker() -> None:
    missing_loguru = _metadata().replace("Requires-Dist: loguru>=0.7\n", "")
    issues = probe._metadata_contract_issues(missing_loguru)
    assert any("Requires-Dist mismatch" in issue for issue in issues)

    wrong_frames_marker = _metadata().replace(
        'pandas>=2.0; extra == "frames"', 'pandas>=2.0; extra == "ws"'
    )
    assert any(
        "Requires-Dist mismatch" in issue
        for issue in probe._metadata_contract_issues(wrong_frames_marker)
    )


def test_check_results_are_split_between_positives_and_synthetic_controls() -> None:
    state = {
        "positive_assertions": [],
        "synthetic_controls": [],
        "artifact_mutations": [],
    }

    probe._add_fact(state, "snapshot_matches", True, observed="same", expected="same")
    probe._add_synthetic_control(
        state,
        "metadata_string_rejected",
        True,
        mutation={"kind": "in_memory_text_edit"},
        observed=["missing dependency"],
        expected="checker rejects missing dependency",
    )

    assert [item["name"] for item in state["positive_assertions"]] == ["snapshot_matches"]
    assert [item["name"] for item in state["synthetic_controls"]] == ["metadata_string_rejected"]
    assert state["artifact_mutations"] == []


def test_sdist_metadata_reader_selects_root_pkg_info(tmp_path: Path) -> None:
    sdist = tmp_path / "opendata_client-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, content in (
            ("opendata_client-0.1.0/PKG-INFO", _metadata()),
            ("opendata_client-0.1.0/opendata_client.egg-info/PKG-INFO", "generated duplicate"),
        ):
            payload = content.encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    path, metadata = probe._metadata_from_archive(sdist, "sdist")

    assert path == "opendata_client-0.1.0/PKG-INFO"
    assert metadata == _metadata()


def test_missing_loguru_mutation_uses_a_copy_of_the_built_wheel(tmp_path: Path) -> None:
    source = tmp_path / "source-wheel.whl"
    mutated = tmp_path / "mutated-wheel.whl"
    metadata_name = "opendata_client-0.1.0.dist-info/METADATA"
    record_name = "opendata_client-0.1.0.dist-info/RECORD"
    record = f"{metadata_name},,\n{record_name},,\n"
    with ZipFile(source, "w") as archive:
        archive.writestr(metadata_name, _metadata())
        archive.writestr(record_name, record)

    members = probe._mutate_wheel_missing_loguru(source, mutated)

    _, original_metadata = probe._metadata_from_archive(source, "wheel")
    _, mutated_metadata = probe._metadata_from_archive(mutated, "wheel")
    with ZipFile(mutated) as archive:
        rows = archive.read(record_name).decode("utf-8").splitlines()
    metadata_record = next(row for row in rows if row.startswith(metadata_name + ","))
    digest = base64.urlsafe_b64encode(hashlib.sha256(mutated_metadata.encode()).digest())
    assert members == {"metadata_member": metadata_name, "record_member": record_name}
    assert probe._metadata_contract_issues(original_metadata) == []
    assert any(
        "loguru>=0.7" in issue for issue in probe._metadata_contract_issues(mutated_metadata)
    )
    assert metadata_record.split(",")[1] == "sha256=" + digest.decode().rstrip("=")
    assert metadata_record.split(",")[2] == str(len(mutated_metadata.encode()))
    assert probe._sha256_file(source) != probe._sha256_file(mutated)


def test_artifact_archive_preserves_same_basename_for_distinct_roles(tmp_path: Path) -> None:
    source_wheel = tmp_path / "source-build" / "opendata_client.whl"
    rebuilt_wheel = tmp_path / "sdist-rebuild" / source_wheel.name
    source_wheel.parent.mkdir()
    rebuilt_wheel.parent.mkdir()
    source_wheel.write_bytes(b"source-built wheel")
    rebuilt_wheel.write_bytes(b"wheel rebuilt from sdist")
    run_dir = tmp_path / "run"

    source_artifact = probe._artifact_copy(source_wheel, run_dir, "source-build-wheel")
    rebuilt_artifact = probe._artifact_copy(rebuilt_wheel, run_dir, "sdist-rebuilt-wheel")

    assert source_artifact["filename"] == "source-build-wheel--opendata_client.whl"
    assert rebuilt_artifact["filename"] == "sdist-rebuilt-wheel--opendata_client.whl"
    assert source_artifact["source_filename"] == rebuilt_artifact["source_filename"]
    assert source_artifact["path"] != rebuilt_artifact["path"]
    assert (run_dir / source_artifact["path"]).read_bytes() == b"source-built wheel"
    assert (run_dir / rebuilt_artifact["path"]).read_bytes() == b"wheel rebuilt from sdist"
    assert source_artifact["sha256"] != rebuilt_artifact["sha256"]


def test_source_leak_detector_rejects_checkout_and_snapshot_paths(tmp_path: Path) -> None:
    checkout = tmp_path / "repo"
    snapshot = tmp_path / "snapshot"
    installed = tmp_path / "venv" / "site-packages"

    leaks = probe._source_path_leaks(
        [str(installed), str(checkout / "opendata_client"), str(snapshot)],
        [checkout, snapshot],
    )

    assert leaks == [str(checkout / "opendata_client"), str(snapshot)]


def test_install_targets_base_pip_at_isolated_python(tmp_path: Path) -> None:
    venv_python = tmp_path / "target" / "bin" / "python"
    command = probe._pip_command(venv_python, "check")
    assert command == [
        probe.sys.executable,
        "-m",
        "pip",
        "--python",
        str(venv_python),
        "check",
    ]


def test_optional_requirement_uses_sdk_wheel_direct_url_and_extra(tmp_path: Path) -> None:
    wheel = tmp_path / "opendata_client-0.1.0-py3-none-any.whl"
    assert probe._pip_requirement(wheel, "ws") == (
        f"opendata-client[ws] @ {wheel.resolve().as_uri()}"
    )
    assert probe._pip_requirement(wheel, None) == str(wheel)


def test_clean_environment_removes_checkout_and_private_pip_overrides(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    monkeypatch.setenv("PYTHONHOME", "/python")
    monkeypatch.setenv("PIP_TARGET", "/site-packages")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:secret@example.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://other.invalid/simple")
    monkeypatch.setenv("COV_CORE_SOURCE", "opendata_client")

    environment = probe._clean_environment(isolated_pip=True)

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "PIP_TARGET" not in environment
    assert "COV_CORE_SOURCE" not in environment
    assert "secret" not in environment["PIP_INDEX_URL"]
    assert environment["PIP_INDEX_URL"] == "https://pypi.org/simple"
    assert "PIP_EXTRA_INDEX_URL" not in environment


def test_loopback_environment_bypasses_http_and_websocket_proxies(monkeypatch) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "WS_PROXY",
        "WSS_PROXY",
        "ws_proxy",
        "wss_proxy",
    ):
        monkeypatch.setenv(name, "http://proxy.example.invalid")

    environment = probe._loopback_environment()

    assert all(
        name not in environment
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "WS_PROXY",
            "WSS_PROXY",
            "ws_proxy",
            "wss_proxy",
        )
    )
    assert environment["NO_PROXY"] == "*"
    assert environment["no_proxy"] == "*"


def test_smoke_contract_contains_real_local_http_and_websocket_calls() -> None:
    smoke = probe._smoke_code()
    compile(smoke, "<sdk-package-smoke>", "exec")
    assert "ThreadingHTTPServer" in smoke
    assert "client.stock_daily_all" in smoke
    assert "page=2, page_size=2" in smoke
    assert "client.query_provider_model" in smoke
    assert "client.subscribe(" in smoke
    assert "Sec-WebSocket-Accept" in smoke


def test_probe_cli_rejects_nonpositive_timeout(capsys) -> None:
    assert probe.main(["--timeout-seconds", "0"]) == 2
    assert "must be positive" in capsys.readouterr().err
