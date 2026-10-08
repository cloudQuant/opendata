"""Port module codemod tests (A1.5 / FR-5).

Covers the rewrite chain (header, imports, strings, manual edits),
idempotence, resource copying, the lock lifecycle and CLI behaviour -
all against synthetic trees in tmp_path, never the real upstream.
"""

import json
import shutil
from pathlib import Path

import pytest

from scripts.codemod import port_module
from scripts.codemod.port_module import (
    MANUAL_EDITS,
    UpstreamLock,
    apply_manual_edits,
    init_lock,
    port_source,
    port_submodule,
    prepend_porting_header,
    rewrite_imports,
    rewrite_strings,
    sha256_bytes,
    sha256_text,
)

URL = "https://github.com/cloudQuant/akshare.git"
COMMIT = "c4f6a63" * 5 + "1" * 2  # 42 chars, hex-ish

SAMPLE = '''#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Desc: sample module
"""

from akshare.utils import demjson
from akshare.utils.tqdm import get_tqdm
import akshare.cons as cons

MODULE = "akshare.file_fold.calendar"
LABEL = "akshare"


def load(name="akshare.datasets.data"):
    """See akshare docs for details."""
    return name
'''


class TestRewrites:
    def test_import_rewrites_all_four_forms(self):
        source, count = rewrite_imports(SAMPLE)
        assert count == 3
        assert "from opendata.data.providers.akshare._vendor.utils import demjson" in source
        assert "from opendata.data.providers.akshare._vendor.utils.tqdm import get_tqdm" in source
        assert "import opendata.data.providers.akshare._vendor.cons as cons" in source

    def test_docstring_mentions_untouched(self):
        source, _ = rewrite_imports(SAMPLE)
        assert "akshare docs" in source

    def test_string_rewrites_only_module_paths(self):
        source, count = rewrite_strings(SAMPLE)
        assert count == 2
        assert 'MODULE = "opendata.data.providers.akshare._vendor.file_fold.calendar"' in source
        assert 'load(name="opendata.data.providers.akshare._vendor.datasets.data")' in source
        assert 'LABEL = "akshare"' in source

    def test_bare_label_string_untouched(self):
        source = 'print("akshare")'
        assert rewrite_strings(source) == (source, 0)

    def test_header_inserted_after_shebang_and_coding(self):
        source = prepend_porting_header(SAMPLE, URL, COMMIT)
        lines = source.splitlines()
        assert lines[0].startswith("#!")
        assert "coding" in lines[1]
        assert lines[2] == f"# Ported from akshare ({URL}) @ {COMMIT}"
        assert lines[3].startswith("# Copyright (c) 2019-2026")
        assert lines[4] == '"""'

    def test_header_inserted_at_top_without_shebang(self):
        source = prepend_porting_header('"""Doc."""\n', URL, COMMIT)
        assert source.splitlines()[0] == f"# Ported from akshare ({URL}) @ {COMMIT}"

    def test_header_idempotent(self):
        once = prepend_porting_header(SAMPLE, URL, COMMIT)
        assert prepend_porting_header(once, URL, COMMIT) == once


class TestManualEdits:
    def test_credential_replay_removes_secret(self):
        source = 'xq_a_token = "supersecretvalue"\nprint(x)\n'
        result, todos = apply_manual_edits(source, "akshare/stock/cons.py")
        assert todos == []
        assert 'os.environ.get("XQ_A_TOKEN", "")' in result
        assert "supersecretvalue" not in result
        assert "import os\n" in result

    def test_import_os_not_duplicated(self):
        source = 'import os\nxq_a_token = "s"\n'
        result, _ = apply_manual_edits(source, "akshare/stock/cons.py")
        assert result.count("import os") == 1

    def test_pattern_mismatch_becomes_todo(self):
        source = "xq_a_token = None\n"
        _, todos = apply_manual_edits(source, "akshare/stock/cons.py")
        assert len(todos) == 1
        assert "no longer matches" in todos[0]

    def test_unregistered_file_untouched(self):
        source = "token = 'x'\n"
        result, todos = apply_manual_edits(source, "akshare/other/mod.py")
        assert (result, todos) == (source, [])

    def test_fail_closed_edits_replay_without_unused_os(self):
        source = (
            "from opendata.data.providers.akshare._vendor.utils.func import fetch_paginated_data\n"
            "print(1)\n"
        )
        result, _ = apply_manual_edits(source, "akshare/index/index_zh_em.py")
        assert (
            "from opendata.data.providers.akshare._vendor.utils.request import request_eastmoney"
            in result
        )
        assert "import os" not in result

    def test_silent_empty_fixes_are_registered_for_replay(self):
        registered = {edit.upstream_path for edit in MANUAL_EDITS}
        assert {
            "akshare/index/index_zh_em.py",
            "akshare/index/index_cons.py",
        } <= registered

    def test_codemod_source_is_secret_free(self):
        # The transforms must never embed credential literals.
        text = Path(port_module.__file__).read_text(encoding="utf-8")
        assert "9492bad942" not in text
        assert "13718729810" not in text
        assert "fsXkFu6Ef25vWNc" not in text


class TestPortSource:
    def test_full_chain(self):
        source, result = port_source(SAMPLE, "akshare/utils/fake.py", URL, COMMIT)
        assert result.import_rewrites == 3
        assert result.string_rewrites == 2
        assert result.manual_edits is False
        assert result.upstream_sha256 == sha256_text(SAMPLE)
        assert result.ported_sha256 == sha256_text(source)
        assert "# Ported from akshare" in source

    def test_expected_output_is_stable(self):
        _, first = port_source(SAMPLE, "akshare/utils/fake.py", URL, COMMIT)
        source, second = port_source(SAMPLE, "akshare/utils/fake.py", URL, COMMIT)
        assert first.ported_sha256 == second.ported_sha256
        assert source


def make_upstream_repo(root: Path):
    """A synthetic upstream repository with port-able submodules."""
    repo = root / "upstream"
    (repo / "akshare" / "utils").mkdir(parents=True)
    (repo / "akshare" / "__init__.py").write_text(
        "# upstream provenance\n# upstream copyright\nfrom akshare.utils import decode\n",
        encoding="utf-8",
    )
    (repo / "akshare" / "utils" / "__init__.py").write_text(
        "# -*- coding:utf-8 -*-\nfrom akshare.utils.demjson import decode\n", encoding="utf-8"
    )
    # Matches a registered manual edit path (credential disposal).
    (repo / "akshare" / "stock").mkdir(parents=True)
    (repo / "akshare" / "stock" / "cons.py").write_text(
        '# -*- coding:utf-8 -*-\nxq_a_token = "placeholder-secret"\n', encoding="utf-8"
    )
    (repo / "akshare" / "file_fold").mkdir(parents=True)
    (repo / "akshare" / "file_fold" / "calendar.json").write_bytes(b'{"20240101": 0}')
    (repo / "akshare" / "file_fold" / "opaque.dat").write_bytes(b"\x00\xff\x10")
    return repo


class TestSubmodulePort:
    def make_lock(self, repo):
        return UpstreamLock(url=URL, commit="fake-commit", files={})

    def test_ports_files_and_resources(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        ported_root = tmp_path / "_vendor"
        monkeypatch.setattr(port_module, "PORTED_ROOT", ported_root)
        monkeypatch.setattr(port_module, "LOCK_PATH", ported_root / "upstream.lock")
        lock = self.make_lock(repo)

        results = port_submodule("utils", upstream_repo=repo, lock=lock)
        assert [r.status for r in results] == ["ported"]
        assert (ported_root / "utils" / "__init__.py").exists()
        assert "from opendata.data.providers.akshare._vendor.utils.demjson import decode" in (
            ported_root / "utils" / "__init__.py"
        ).read_text(encoding="utf-8")

        results = port_submodule("file_fold", upstream_repo=repo, lock=lock)
        assert (ported_root / "file_fold" / "calendar.json").read_bytes() == b'{"20240101": 0}'
        opaque = (ported_root / "file_fold" / "opaque.dat").read_bytes()
        assert opaque == b"\x00\xff\x10"
        assert lock.files["file_fold/opaque.dat"]["sha256"] == sha256_bytes(opaque)
        assert all(result.import_rewrites == 0 for result in results)

    def test_root_port_generates_lazy_facade_and_preserves_source_hash(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        ported_root = tmp_path / "_vendor"
        monkeypatch.setattr(port_module, "PORTED_ROOT", ported_root)
        monkeypatch.setattr(port_module, "LOCK_PATH", ported_root / "upstream.lock")
        lock = self.make_lock(repo)
        upstream_bytes = (repo / "akshare" / "__init__.py").read_bytes()

        results = port_submodule("__init__", upstream_repo=repo, lock=lock)

        facade = (ported_root / "__init__.py").read_text(encoding="utf-8")
        assert "def __getattr__(name):" in facade
        assert "from importlib import import_module as _import_module" in facade
        assert "from opendata.data.providers.akshare._vendor.utils import decode" not in facade
        assert lock.files["__init__.py"]["sha256"] == sha256_text(upstream_bytes.decode("utf-8"))
        assert results[0].ported_sha256 == sha256_bytes((ported_root / "__init__.py").read_bytes())

    def test_manual_edit_recorded_in_lock(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        ported_root = tmp_path / "_vendor"
        monkeypatch.setattr(port_module, "PORTED_ROOT", ported_root)
        monkeypatch.setattr(port_module, "LOCK_PATH", ported_root / "upstream.lock")
        lock = self.make_lock(repo)
        port_submodule("stock", upstream_repo=repo, lock=lock)

        record = lock.files["stock/cons.py"]
        assert record["manual_edits"] is True
        ported = (ported_root / "stock" / "cons.py").read_text(encoding="utf-8")
        assert "placeholder-secret" not in ported
        assert 'os.environ.get("XQ_A_TOKEN", "")' in ported

    def test_idempotent_second_run_skips(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        ported_root = tmp_path / "_vendor"
        monkeypatch.setattr(port_module, "PORTED_ROOT", ported_root)
        monkeypatch.setattr(port_module, "LOCK_PATH", ported_root / "upstream.lock")
        lock = self.make_lock(repo)
        port_submodule("utils", upstream_repo=repo, lock=lock)
        lock.save()

        second = port_submodule("utils", upstream_repo=repo, lock=UpstreamLock.load())
        assert [r.status for r in second] == ["skipped-identical"]

    def test_unknown_submodule_raises(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        monkeypatch.setattr(port_module, "PORTED_ROOT", tmp_path / "_vendor")
        with pytest.raises(ValueError, match="unknown submodule"):
            port_submodule("nope", upstream_repo=repo, lock=self.make_lock(repo))

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        repo = make_upstream_repo(tmp_path)
        ported_root = tmp_path / "_vendor"
        monkeypatch.setattr(port_module, "PORTED_ROOT", ported_root)
        results = port_submodule(
            "utils", upstream_repo=repo, lock=self.make_lock(repo), dry_run=True
        )
        assert len(results) == 1
        assert not ported_root.exists()


class TestLock:
    def test_load_missing_lock_fails_closed(self, tmp_path):
        with pytest.raises(RuntimeError, match="missing"):
            UpstreamLock.load(tmp_path / "nope.lock")

    def test_load_malformed_lock_fails_closed(self, tmp_path):
        path = tmp_path / "bad.lock"
        path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        with pytest.raises(RuntimeError, match="malformed"):
            UpstreamLock.load(path)

    def test_round_trip(self, tmp_path):
        from scripts.codemod.port_module import PortResult

        path = tmp_path / "upstream.lock"
        lock = UpstreamLock(url=URL, commit="abc", files={})
        lock.record(
            PortResult(
                upstream_path="akshare/utils/cons.py",
                ported_path="utils/cons.py",
                status="ported",
                manual_edits=True,
                upstream_sha256="x",
            )
        )
        lock.save(path)
        loaded = UpstreamLock.load(path)
        assert loaded.files["utils/cons.py"]["manual_edits"] is True
        assert loaded.commit == "abc"


def test_manifest_keeps_migrated_and_upstream_hashes_separate(tmp_path):
    from scripts.codemod.gen_manifest import generate_manifest

    ported_root = tmp_path / "_vendor"
    ported_root.mkdir()
    migrated = b"value = 'migrated'\n"
    (ported_root / "module.py").write_bytes(migrated)
    upstream_sha = sha256_text("value = 'upstream'\n")
    lock = UpstreamLock(
        url=URL,
        commit="abc",
        files={
            "module.py": {
                "path": "module.py",
                "upstream_path": "akshare/module.py",
                "sha256": upstream_sha,
                "manual_edits": False,
            }
        },
    )

    manifest = generate_manifest(ported_root, lock)
    entry = manifest["files"][0]
    assert entry["sha256"] == sha256_bytes(migrated)
    assert entry["upstream_sha256"] == upstream_sha
    assert entry["sha256"] != entry["upstream_sha256"]


def test_report_compares_resource_hashes_as_raw_bytes(tmp_path):
    from scripts.codemod.report_port import collect_drift

    upstream_repo = tmp_path / "upstream"
    pristine = upstream_repo / "akshare" / "file_fold" / "opaque.dat"
    pristine.parent.mkdir(parents=True)
    pristine.write_bytes(b"\x00\xff\x10")
    ported_root = tmp_path / "_vendor"
    ported = ported_root / "file_fold" / "opaque.dat"
    ported.parent.mkdir(parents=True)
    ported.write_bytes(pristine.read_bytes())
    digest = sha256_bytes(pristine.read_bytes())
    lock = UpstreamLock(
        url=URL,
        commit="abc",
        files={
            "file_fold/opaque.dat": {
                "path": "file_fold/opaque.dat",
                "upstream_path": "akshare/file_fold/opaque.dat",
                "sha256": digest,
                "manual_edits": False,
            }
        },
    )

    todos, table = collect_drift(ported_root, lock, upstream_repo)
    assert todos == []
    assert table[0]["upstream_sha256"] == digest
    assert table[0]["ported_sha256"] == digest
    assert table[0]["replay_matches"] is True


def test_report_fails_closed_for_unlocked_vendor_files(tmp_path):
    from scripts.codemod.report_port import collect_unlocked_files

    ported_root = tmp_path / "_vendor"
    ported_root.mkdir()
    for name in ("upstream.lock", "manifest.json", "LICENSE-AKSHARE"):
        (ported_root / name).write_text("metadata", encoding="utf-8")
    lock = UpstreamLock(url=URL, commit="abc", files={})

    assert collect_unlocked_files(ported_root, lock) == []
    (ported_root / "untracked.py").write_text("value = 1\n", encoding="utf-8")
    assert collect_unlocked_files(ported_root, lock) == [
        "untracked.py: file exists in the vendor tree but is absent from upstream.lock"
    ]


def test_sync_lock_verification_preserves_source_and_resource_hash_semantics(tmp_path):
    import subprocess

    from scripts.codemod.fetch_upstream import _git_exe, verify_lock

    repo = tmp_path / "upstream"
    source = repo / "akshare" / "file_fold" / "source.py"
    resource = repo / "akshare" / "file_fold" / "opaque.dat"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"value = 1\r\n")
    resource_bytes = b"\x00\xff\x10"
    resource.write_bytes(resource_bytes)
    git_exe = _git_exe()
    subprocess.run(  # noqa: S603  # resolved Git executable, literal arguments
        [git_exe, "init", "-q"], cwd=repo, check=True
    )
    subprocess.run(  # noqa: S603  # resolved Git executable, literal arguments
        [git_exe, "add", "."], cwd=repo, check=True
    )
    subprocess.run(  # noqa: S603  # resolved Git executable, literal arguments
        [
            git_exe,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    commit = subprocess.run(  # noqa: S603  # resolved Git executable, literal arguments
        [git_exe, "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    lock = {
        "upstream": {"commit": commit},
        "files": [
            {
                "path": "file_fold/source.py",
                "upstream_path": "akshare/file_fold/source.py",
                "sha256": sha256_text("value = 1\n"),
            },
            {
                "path": "file_fold/opaque.dat",
                "upstream_path": "akshare/file_fold/opaque.dat",
                "sha256": sha256_bytes(resource_bytes),
            },
        ],
    }

    assert verify_lock(repo, lock) == []


def test_compare_numeric_diff_indices_preserve_tolerance_and_nan_semantics():
    import numpy as np
    import pandas as pd

    from scripts.codemod.compare_with_upstream import _compare_frames

    reference = pd.DataFrame({"close": [1.0, np.nan, 3.0]})
    ported = pd.DataFrame({"close": [1.0, np.nan, 4.0]})

    assert _compare_frames(reference, ported, {"close": "float64"}) == ["cell close[2]: 3.0 != 4.0"]


class TestInitLock:
    def _init_repo(self, repo: Path) -> None:
        import os
        import subprocess

        env = {**os.environ, "HOME": str(repo.parent)}
        git = shutil.which("git") or "git"
        subprocess.run([git, "init", "-q", str(repo)], check=True, env=env)  # noqa: S603  # nosec B603  # test fixture, literal argv
        (repo / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run([git, "-C", str(repo), "add", "."], check=True, env=env)  # noqa: S603  # nosec B603  # test fixture, literal argv
        subprocess.run(  # noqa: S603  # nosec B603  # test fixture, literal argv
            [git, "-C", str(repo), "commit", "-q", "-m", "init"],
            check=True,
            env={
                **env,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

    def test_init_lock_from_clean_repo(self, tmp_path):
        repo = tmp_path / "up"
        repo.mkdir()
        self._init_repo(repo)
        lock = init_lock(repo, URL)
        assert len(lock.commit) == 40
        assert lock.url == URL

    def test_init_lock_rejects_dirty_repo(self, tmp_path):
        repo = tmp_path / "up"
        repo.mkdir()
        self._init_repo(repo)
        (repo / "dirty.txt").write_text("y", encoding="utf-8")
        with pytest.raises(RuntimeError, match="dirty"):
            init_lock(repo, URL)
