"""Upstream sync tool tests (AC-12 / C2, design §12).

The tool runs git against a real repository, so the tests build a tiny
two-commit repository and verify the inventory, the signature diff and
the lock verification against it - no network, no akshare.
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.codemod.fetch_upstream import (
    FileChange,
    UpstreamSyncError,
    changed_files,
    function_diff,
    function_signatures,
    load_lock,
    verify_lock,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        ["git", *args],  # noqa: S607  # nosec B607  # resolved on PATH by the tool under test
        cwd=repo,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    )


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """A two-commit git repository with one file changed between them."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _write(
        repo,
        "akshare/mod.py",
        "def fetch(symbol):\n    return symbol\n\n\ndef legacy():\n    return 1\n",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git_out(repo, "rev-parse", "HEAD").strip()
    _write(
        repo,
        "akshare/mod.py",
        "def fetch(symbol, timeout=30):\n    return symbol\n\n\n"
        "def brand_new(symbol):\n    return symbol\n",
    )
    _write(repo, "akshare/new_module.py", "def created():\n    return 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "move")
    head = _git_out(repo, "rev-parse", "HEAD").strip()
    return repo, base, head


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        ["git", *args],  # noqa: S607  # nosec B607  # resolved on PATH by the tool under test
        cwd=repo,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    ).stdout


class TestChangedFiles:
    def test_inventory_lists_add_modify_delete(self, repo):
        upstream, base, head = repo

        changes = changed_files(upstream, base, head)

        by_path = {change.path: change for change in changes}
        assert by_path["akshare/mod.py"].status == "M"
        assert by_path["akshare/new_module.py"].status == "A"

    def test_sorted_and_deduped(self, repo):
        upstream, base, head = repo
        changes = changed_files(upstream, base, head)
        paths = [change.path for change in changes]

        assert paths == sorted(paths)


class TestFunctionSignatures:
    def test_signatures_include_methods_and_defaults_excluded(self):
        source = """
def fetch(symbol, timeout=30):
    ...

class Client:
    def run(self, ctx, *args, **kwargs):
        ...

async def pull():
    ...
"""
        signatures = function_signatures(source)

        assert signatures["fetch"] == ("symbol", "timeout")
        assert signatures["Client.run"] == ("self", "ctx", "*args", "**kwargs")
        assert signatures["pull"] == ()

    def test_function_diff_reports_added_removed_changed(self, repo):
        upstream, base, head = repo

        diff = function_diff(upstream, base, head, "akshare/mod.py")

        assert diff["added"] == {"brand_new": ("symbol",)}
        assert diff["removed"] == {"legacy": ()}
        assert diff["changed"] == [
            {"function": "fetch", "before": ("symbol",), "after": ("symbol", "timeout")}
        ]


class TestLockVerification:
    def test_lock_that_matches_reports_nothing(self, repo):
        upstream, base, _ = repo
        blob = _git_out(upstream, "show", f"{base}:akshare/mod.py")
        lock = {
            "upstream": {"commit": base},
            "files": [
                {
                    "path": "mod.py",
                    "upstream_path": "akshare/mod.py",
                    "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
                    "manual_edits": False,
                }
            ],
        }

        assert verify_lock(upstream, lock) == []

    def test_drifted_hash_is_reported(self, repo):
        upstream, base, _ = repo
        lock = {
            "upstream": {"commit": base},
            "files": [
                {
                    "path": "mod.py",
                    "upstream_path": "akshare/mod.py",
                    "sha256": "0" * 64,
                    "manual_edits": False,
                }
            ],
        }

        drifted = verify_lock(upstream, lock)

        assert len(drifted) == 1
        assert "akshare/mod.py" in drifted[0]


class TestLoadLock:
    def test_missing_lock_fails_closed(self, tmp_path):
        with pytest.raises(UpstreamSyncError, match="not found"):
            load_lock(tmp_path / "nope.json")

    def test_broken_lock_fails_closed(self, tmp_path):
        path = tmp_path / "lock.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(UpstreamSyncError, match="not valid JSON"):
            load_lock(path)

    def test_missing_sections_fail_closed(self, tmp_path):
        path = tmp_path / "lock.json"
        path.write_text('{"upstream": {}}', encoding="utf-8")

        with pytest.raises(UpstreamSyncError, match="missing required"):
            load_lock(path)


def test_file_change_shape():
    change = FileChange(status="A", path="akshare/x.py")

    assert change.status == "A"
    assert change.path == "akshare/x.py"
