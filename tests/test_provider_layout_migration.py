from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.codemod import migrate_provider_layout as migration
from scripts.codemod.migrate_provider_layout import (
    FUYAO_MOVE_MAP,
    MigrationConfig,
    MigrationError,
    migrate_repository,
)

AKSHARE_SOURCE_HEADER = (
    "# Ported from akshare (https://github.com/cloudQuant/akshare.git) @ fixture-commit\n"
    "# Copyright (c) 2019-2026 Albert King — MIT License (see THIRD_PARTY_NOTICES.md)\n"
).encode()


def _write(root: Path, relative: str, content: str | bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def _synthetic_repo(root: Path) -> MigrationConfig:
    _write(root, "LICENSE-AKSHARE", "MIT License\nsynthetic fixture\n")
    _write(
        root,
        "legacy_ak/__init__.py",
        AKSHARE_SOURCE_HEADER
        + b"from legacy_ak.sample import exported\n"
        + b"from legacy_ak._version import __version__, get_version\n",
    )
    _write(
        root,
        "legacy_ak/sample.py",
        "def exported(value=1):\n    return value\n",
    )
    _write(
        root,
        "legacy_ak/_version.py",
        '__version__ = "1.2.3-fixture"\n\ndef get_version():\n    return __version__\n',
    )
    _write(root, "legacy_ak/file_fold/calendar.json", b'{"calendar": []}\n')
    _write(root, "legacy_ak/stock_feature/ths.js", b"window.fixture = true;\n")
    _write(
        root,
        "legacy_ak/manifest.json",
        json.dumps(
            {
                "upstream": {"commit": "fixture-commit"},
                "files": [{"path": "sample.py", "sha256": "fixture-upstream-hash"}],
            }
        )
        + "\n",
    )
    _write(
        root,
        "legacy_ak/upstream.lock",
        json.dumps(
            {
                "upstream": {"commit": "fixture-commit"},
                "files": [{"path": "sample.py", "sha256": "fixture-upstream-hash"}],
            }
        )
        + "\n",
    )

    fuyao_sources = {
        "__init__.py": ("from .credentials import Token\nfrom .endpoints import endpoint\n"),
        "credentials.py": "class Token:\n    pass\n",
        "envelope.py": "class Envelope:\n    pass\n",
        "errors.py": "class TransportError(Exception):\n    pass\n",
        "http_client.py": "class HttpClient:\n    pass\n",
        "rate_limiter.py": "class RateLimiter:\n    pass\n",
        "error_messages.yaml": "errors: {}\n",
        "endpoints.py": "def endpoint():\n    return '/v1/data'\n",
        "dumps.py": "def dump():\n    return None\n",
        "endpoint_map.py": "def load_map():\n    return {}\n",
        "endpoint_map.yaml": "endpoints: []\n",
    }
    for relative, content in fuyao_sources.items():
        _write(root, f"legacy_fuyao/{relative}", content)

    _write(
        root,
        "consumer.py",
        """\
import legacy_ak as ak_alias
import legacy_ak.sample
import legacy_ak.sample as sample_alias
from legacy_ak.sample import exported as imported_export
import legacy_fuyao as fuyao_alias
import legacy_fuyao.endpoints
from legacy_fuyao import endpoints, Token as API_TOKEN
from importlib import import_module as load
from importlib.resources import files as resource_files, path as resource_path
from importlib.util import find_spec as locate
import builtins as builtin_alias
from builtins import __import__ as raw_import

loaded_module = load("legacy_ak.sample")
loaded_by_builtin_alias = builtin_alias.__import__("legacy_ak.sample")
loaded_by_builtin_import = raw_import("legacy_ak.sample")
located_spec = locate("legacy_ak.sample")
calendar = resource_files("legacy_ak").joinpath("file_fold/calendar.json")
errors = resource_path("legacy_fuyao", "error_messages.yaml")
endpoint_map = resource_files("legacy_fuyao").joinpath("endpoint_map.yaml")
""",
    )
    _write(root, "docs/history.md", "import legacy_ak.sample\n")
    return MigrationConfig(
        repo_root=root,
        akshare_source=Path("legacy_ak"),
        akshare_target=Path("newpkg/akshare/_vendor"),
        fuyao_source=Path("legacy_fuyao"),
        ths_target=Path("newpkg/ths"),
        akshare_old_namespace="legacy_ak",
        akshare_new_namespace="newpkg.akshare._vendor",
        fuyao_old_namespace="legacy_fuyao",
        ths_new_namespace="newpkg.ths",
        transport_new_namespace="newpkg.ths.transport",
        expected_akshare_file_count=None,
        expected_akshare_suffix_counts=None,
        expected_source_imports=None,
    )


def test_dynamic_module_arguments_skip_non_literal_values() -> None:
    tree = ast.parse("import importlib\nimportlib.import_module(resolve_module())")

    assert migration._dynamic_module_arguments(tree) == []


def test_plan_rewrites_import_forms_and_dynamic_resource_packages(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    original_consumer = (tmp_path / "consumer.py").read_text()

    report = migrate_repository(config)

    assert (tmp_path / "consumer.py").read_text() == original_consumer
    assert report["inventory"]["akshare"]["non_cache_file_count"] == 7
    assert report["inventory"]["fuyao"]["non_cache_file_count"] == 11
    assert (
        report["manifest_and_lock_provenance"]["manifest.json"]["source_file_sha256"]
        == report["manifest_and_lock_provenance"]["manifest.json"]["migrated_file_sha256"]
    )
    assert (
        report["manifest_and_lock_provenance"]["upstream.lock"]["embedded_provenance"][
            "declared_upstream_commit"
        ]
        == "fixture-commit"
    )
    assert report["lazy_vendor_facade"]["export_count"] == 3
    assert report["lazy_vendor_facade"]["preserved_source_header_line_count"] == 2
    assert (
        bytes.fromhex(report["lazy_vendor_facade"]["preserved_source_header_bytes_hex"])
        == AKSHARE_SOURCE_HEADER
    )
    assert report["lazy_vendor_facade"]["preserved_source_header_sha256"] == (
        hashlib.sha256(AKSHARE_SOURCE_HEADER).hexdigest()
    )
    coverage = report["coverage_identity_mapping"]
    assert coverage["pre_migration_first_party_baseline"] == {
        "total_python_file_identities": 213,
        "opendata": 204,
        "fuyao": 9,
        "ported": 0,
    }
    assert len(coverage["fuyao_first_party_preserved"]) == 9
    assert len(coverage["akshare_vendor_separate_audit_excluded_from_first_party"]) == 3
    assert all(
        row["coverage_scope"] == "first_party_provider"
        for row in coverage["fuyao_first_party_preserved"]
    )
    assert all(
        row["coverage_scope"] == "vendored_dependency_separate_audit"
        for row in coverage["akshare_vendor_separate_audit_excluded_from_first_party"]
    )
    assert any(
        row["kind"] == "ImportFrom" and len(row["modules_after"]) == 2
        for row in report["rewrites"]["ast_import_nodes"]
    )
    dynamic_targets = {
        (row["before"], row["after"], row["resource_name"])
        for row in report["rewrites"]["dynamic_module_strings"]
    }
    assert (
        "legacy_fuyao",
        "newpkg.ths.transport",
        "error_messages.yaml",
    ) in dynamic_targets
    assert (
        "legacy_fuyao",
        "newpkg.ths",
        "endpoint_map.yaml",
    ) in dynamic_targets
    assert (tmp_path / "docs/history.md").read_text() == "import legacy_ak.sample\n"


def test_frozen_akshare_root_header_and_version_export_are_preserved(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    frozen_init = repo_root / "tests/fixtures/provider-layout/opendata-http-init.txt"
    source_bytes = frozen_init.read_bytes()
    old_init = tmp_path / "opendata_http/__init__.py"
    old_init.parent.mkdir(parents=True)
    old_init.write_bytes(source_bytes)
    monkeypatch.setattr(migration, "_module_path_exists", lambda *_args: True)
    source_header = b"".join(source_bytes.splitlines(keepends=True)[:2])

    generated, report = migration._lazy_vendor_init(old_init, MigrationConfig(repo_root=tmp_path))

    assert generated.startswith(source_header)
    assert b"c4f6a631c259783dbc2507b6b27d179b3e88079d" in source_header
    assert "Copyright (c) 2019-2026 Albert King — MIT License" in source_header.decode()
    assert report["preserved_source_header_bytes_hex"] == source_header.hex()
    assert report["exports"]["__version__"] == {
        "module": "opendata.data.providers.akshare._vendor._version",
        "symbol": "__version__",
        "optional": False,
    }


def test_apply_is_lazy_hash_reported_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    config = _synthetic_repo(tmp_path)
    report_path = tmp_path.parent / f"{tmp_path.name}-migration.json"
    license_before = (tmp_path / "LICENSE-AKSHARE").read_bytes()
    sentinels = {
        "newpkg/ths/provider.py": 'SENTINEL = "provider"\n',
        "newpkg/ths/models/model.py": 'SENTINEL = "model"\n',
        "newpkg/ths/registration.py": 'SENTINEL = "registration"\n',
    }
    for relative, content in sentinels.items():
        _write(tmp_path, relative, content)

    first = migrate_repository(config, apply=True, report_path=report_path)

    assert first["status"] == "complete"
    assert first["legacy_imports_after"] == {}
    assert len(first["moves"]) == 18
    assert not (tmp_path / "legacy_ak").exists()
    assert not (tmp_path / "legacy_fuyao").exists()
    assert (
        tmp_path / "newpkg/akshare/_vendor/file_fold/calendar.json"
    ).read_bytes() == b'{"calendar": []}\n'
    assert (tmp_path / "newpkg/ths/transport/error_messages.yaml").read_text() == "errors: {}\n"
    assert (tmp_path / "newpkg/ths/endpoint_map.yaml").read_text() == "endpoints: []\n"
    generated_facade = (tmp_path / "newpkg/akshare/_vendor/__init__.py").read_bytes()
    assert generated_facade.startswith(AKSHARE_SOURCE_HEADER)
    assert b"# Migration note: lazy compatibility facade;" in generated_facade
    assert (tmp_path / "LICENSE-AKSHARE").read_bytes() == license_before
    for relative, content in sentinels.items():
        assert (tmp_path / relative).read_text() == content

    rewritten = (tmp_path / "consumer.py").read_text()
    assert "legacy_ak.sample" not in rewritten
    assert "newpkg.akshare._vendor.sample" in rewritten
    assert 'load("newpkg.akshare._vendor.sample")' in rewritten
    assert 'builtin_alias.__import__("newpkg.akshare._vendor.sample")' in rewritten
    assert 'raw_import("newpkg.akshare._vendor.sample")' in rewritten
    assert 'locate("newpkg.akshare._vendor.sample")' in rewritten
    assert 'resource_path("newpkg.ths.transport", "error_messages.yaml")' in rewritten
    assert "from newpkg.ths.transport import Token as API_TOKEN" in rewritten
    assert "import newpkg.ths.transport as legacy_fuyao" in rewritten

    monkeypatch.syspath_prepend(str(tmp_path))
    facade = importlib.import_module("newpkg.akshare._vendor")
    assert "newpkg.akshare._vendor.sample" not in sys.modules
    assert "newpkg.akshare._vendor._version" not in sys.modules
    exported = facade.exported
    assert exported(7) == 7
    assert exported is facade.exported
    assert exported.__module__ == "newpkg.akshare._vendor.sample"
    assert str(inspect.signature(exported)) == "(value=1)"
    assert "newpkg.akshare._vendor.sample" in sys.modules
    assert facade.__version__ == "1.2.3-fixture"
    version_module = importlib.import_module("newpkg.akshare._vendor._version")
    assert facade.__version__ is version_module.__version__
    version_getter = facade.get_version
    assert version_getter is version_module.get_version
    assert str(inspect.signature(version_getter)) == "()"
    assert version_getter() == facade.__version__

    second = migrate_repository(config, apply=True, report_path=report_path)
    assert second["idempotent"] is True
    assert second["moves"] == first["moves"]


def test_plan_fails_closed_on_destination_collision(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    _write(tmp_path, "newpkg/ths/endpoints.py", "already owned\n")

    with pytest.raises(MigrationError, match="collision"):
        migrate_repository(config)


def test_plan_fails_closed_on_vendor_root_collision(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    _write(
        tmp_path,
        "newpkg/akshare/_vendor/owned.py",
        "owned by another package\n",
    )

    with pytest.raises(MigrationError, match="AKShare destination already exists"):
        migrate_repository(config)


def test_plan_fails_closed_when_a_resource_is_missing(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    (tmp_path / "legacy_ak/stock_feature/ths.js").unlink()

    with pytest.raises(MigrationError, match="resources are missing"):
        migrate_repository(config)


def test_plan_fails_closed_when_dynamic_fuyao_resource_target_is_unknown(
    tmp_path: Path,
) -> None:
    config = _synthetic_repo(tmp_path)
    with (tmp_path / "consumer.py").open("a") as consumer:
        consumer.write('\nunknown_root = resource_files("legacy_fuyao")\n')

    with pytest.raises(MigrationError, match="cannot determine Fuyao resource destination"):
        migrate_repository(config)


def test_plan_fails_closed_on_incomplete_source_tree(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    (tmp_path / "legacy_fuyao/http_client.py").unlink()

    with pytest.raises(MigrationError, match="inventory mismatch"):
        migrate_repository(config)


def test_plan_fails_closed_when_old_root_is_absent_and_target_is_partial(
    tmp_path: Path,
) -> None:
    config = _synthetic_repo(tmp_path)
    shutil.rmtree(tmp_path / "legacy_ak")
    _write(
        tmp_path,
        "newpkg/akshare/_vendor/sample.py",
        "def partial():\n    return None\n",
    )

    with pytest.raises(MigrationError, match="required source directory is missing"):
        migrate_repository(config)


def test_apply_fails_closed_on_an_interrupted_prior_report(tmp_path: Path) -> None:
    config = _synthetic_repo(tmp_path)
    report_path = tmp_path.parent / f"{tmp_path.name}-interrupted.json"
    report_path.write_text(json.dumps({"status": "interrupted"}))

    with pytest.raises(MigrationError, match="earlier report exists"):
        migrate_repository(config, apply=True, report_path=report_path)


def test_apply_preserves_vendor_root_that_appears_after_preflight(
    tmp_path: Path, monkeypatch
) -> None:
    config = _synthetic_repo(tmp_path)
    report_path = tmp_path.parent / f"{tmp_path.name}-appeared.json"
    destination = tmp_path / "newpkg/akshare/_vendor"
    build_plan = migration._build_plan

    def create_destination_after_preflight(config):
        report, plan = build_plan(config)
        _write(tmp_path, "newpkg/akshare/_vendor/concurrent.txt", "concurrent work\n")
        return report, plan

    monkeypatch.setattr(migration, "_build_plan", create_destination_after_preflight)
    with pytest.raises(MigrationError, match="destination appeared during apply"):
        migrate_repository(config, apply=True, report_path=report_path)

    assert (destination / "concurrent.txt").read_text() == "concurrent work\n"
    assert (tmp_path / "legacy_ak/sample.py").is_file()
    assert json.loads(report_path.read_text())["status"] == "interrupted"


def test_transport_destination_path_is_injectable(tmp_path: Path) -> None:
    config = replace(
        _synthetic_repo(tmp_path),
        transport_target=Path("newpkg/custom_transport"),
        transport_new_namespace="newpkg.custom_transport",
    )

    report = migrate_repository(config)

    credential_move = next(
        row for row in report["moves"] if row["source"].endswith("/credentials.py")
    )
    assert credential_move["destination"] == "newpkg/custom_transport/credentials.py"
    assert any(
        row["after"] == "newpkg.custom_transport" and row["resource_name"] == "error_messages.yaml"
        for row in report["rewrites"]["dynamic_module_strings"]
    )


def test_fuyao_move_table_is_complete_and_fixed() -> None:
    assert len(FUYAO_MOVE_MAP) == 11
    assert FUYAO_MOVE_MAP["credentials.py"] == "transport/credentials.py"
    assert FUYAO_MOVE_MAP["error_messages.yaml"] == "transport/error_messages.yaml"
    assert FUYAO_MOVE_MAP["endpoints.py"] == "endpoints.py"
    assert FUYAO_MOVE_MAP["endpoint_map.yaml"] == "endpoint_map.yaml"


def test_ths_root_transport_reexports_are_lazy_and_preserve_identity() -> None:
    root = importlib.import_module("opendata.data.providers.ths")
    transport = importlib.import_module("opendata.data.providers.ths.transport")

    assert root.__all__ == ["FETCHERS", "register", *transport.__all__]
    for name in transport.__all__:
        exported = getattr(root, name)
        assert exported is getattr(transport, name)
        assert exported is getattr(root, name)
    unknown_name = "unknown_transport_export"
    with pytest.raises(AttributeError):
        getattr(root, unknown_name)


def test_ths_root_cold_import_does_not_load_transport_or_provider_runtime() -> None:
    probe = """
import importlib
import socket
import sys


def reject_network(*args, **kwargs):
    raise AssertionError("network access during cold THS package import")


socket.socket.connect = reject_network
socket.create_connection = reject_network

blocked_prefixes = (
    "opendata.core.config",
    "opendata.data.providers.catalog",
    "opendata.data.providers.ths.models",
    "opendata.data.providers.ths.transport",
)


class ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if any(
            fullname == prefix or fullname.startswith(prefix + ".")
            for prefix in blocked_prefixes
        ):
            raise AssertionError("unexpected cold-import dependency: " + fullname)
        if fullname == "settings" or fullname.endswith(".settings"):
            raise AssertionError("settings imported during cold THS package import: " + fullname)
        return None


sys.meta_path.insert(0, ImportBlocker())
module_name = "opendata.data.providers.ths"
root = importlib.import_module(module_name)
assert root.__all__[:2] == ["FETCHERS", "register"]
assert not any(
    name == "settings" or name.endswith(".settings") for name in sys.modules
)
assert not any(
    name == "httpx" or name.startswith("httpx.") for name in sys.modules
)
print("cold-import-ok")
"""
    # S603: this uses sys.executable and a literal probe; no shell or user input.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert completed.stdout.strip() == "cold-import-ok"
