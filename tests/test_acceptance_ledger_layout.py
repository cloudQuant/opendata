"""Historical ledger evidence paths resolve to current tracked source identities."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from scripts.quality import acceptance_ledger_check as ledger_check

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

REPO_ROOT = ledger_check.REPO_ROOT

HISTORICAL_EVIDENCE_MAPS = (
    (
        "opendata_fuyao/endpoints.py",
        "opendata/data/providers/ths/endpoints.py",
    ),
    (
        "opendata_fuyao/error_messages.yaml",
        "opendata/data/providers/ths/transport/error_messages.yaml",
    ),
    (
        "opendata_http/datasets.py",
        "opendata/data/providers/akshare/_vendor/datasets.py",
    ),
    (
        "opendata_http/manifest.json",
        "opendata/data/providers/akshare/_vendor/manifest.json",
    ),
    (
        "opendata_http/upstream.lock",
        "opendata/data/providers/akshare/_vendor/upstream.lock",
    ),
)

HISTORY_HASHES = {
    "docs/evidence/A3/fuyao-live-smoke.txt": (
        "fbecd370bb1bb4de4bb3b93d06e767fa2526a3e46b532e7d87faded17ce0e9d7"
    ),
    "docs/evidence/A3/fuyao-dump-import.txt": (
        "9190990949ce01112121f6ae713e4f1c1549fbce45fb2a2a6496ebe595fcba21"
    ),
    "docs/evidence/B2/fuyao-endpoint-inventory.txt": (
        "4fce07850a4fecaf7c48b82d240dc332f107ac733d1685b52d8fb2837db7957e"
    ),
}


@pytest.fixture
def temp_git_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give each case its own index so tests never mutate the working repository index."""
    git = ledger_check.GIT
    assert git is not None
    subprocess.run(  # noqa: S603  # fixed git executable, literal argv, shell disabled
        [git, "init", "--quiet"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
        text=True,
    )
    source_layout = REPO_ROOT / "scripts/quality/source_layout.py"
    target_layout = tmp_path / "scripts/quality/source_layout.py"
    target_layout.parent.mkdir(parents=True)
    shutil.copyfile(source_layout, target_layout)
    monkeypatch.setattr(ledger_check, "REPO_ROOT", tmp_path)
    ledger_check._tracked.cache_clear()
    ledger_check._source_layout_module.cache_clear()
    yield tmp_path
    ledger_check._tracked.cache_clear()
    ledger_check._source_layout_module.cache_clear()


def _entry(path: str) -> dict[str, object]:
    return {
        "command": "offline fixture validation",
        "round": "I2",
        "date": "2026-10-08",
        "evidence": [path],
    }


def _write(root: Path, relative: str, content: str = "fixture evidence\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_add(root: Path, *paths: str) -> None:
    git = ledger_check.GIT
    assert git is not None
    subprocess.run(  # noqa: S603  # fixed git executable, literal argv, shell disabled
        [git, "add", "--", *paths],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )


@pytest.mark.parametrize(("historical", "canonical"), HISTORICAL_EVIDENCE_MAPS)
def test_canonical_file_exists_and_is_tracked(
    historical: str, canonical: str, temp_git_root: Path
) -> None:
    _write(temp_git_root, canonical)
    _git_add(temp_git_root, canonical)

    assert ledger_check._entry_problems(_entry(historical), "fixture|01") == []


@pytest.mark.parametrize(("historical", "canonical"), HISTORICAL_EVIDENCE_MAPS)
def test_missing_canonical_file_fails_even_when_old_decoy_is_tracked(
    historical: str, canonical: str, temp_git_root: Path
) -> None:
    _write(temp_git_root, historical)
    _git_add(temp_git_root, historical)

    problems = ledger_check._entry_problems(_entry(historical), "fixture|02")

    assert problems == [
        f"fixture|02: evidence path does not exist: {historical} (canonical: {canonical})"
    ]


@pytest.mark.parametrize(("historical", "canonical"), HISTORICAL_EVIDENCE_MAPS)
def test_canonical_file_must_be_tracked_even_when_it_exists(
    historical: str, canonical: str, temp_git_root: Path
) -> None:
    _write(temp_git_root, canonical)

    problems = ledger_check._entry_problems(_entry(historical), "fixture|03")

    assert problems == [
        f"fixture|03: evidence path is not tracked by git: {historical} (canonical: {canonical})"
    ]


@pytest.mark.parametrize(("historical", "canonical"), HISTORICAL_EVIDENCE_MAPS)
def test_old_tracked_identity_cannot_substitute_for_untracked_canonical_file(
    historical: str, canonical: str, temp_git_root: Path
) -> None:
    _write(temp_git_root, historical)
    _write(temp_git_root, canonical)
    _git_add(temp_git_root, historical)

    problems = ledger_check._entry_problems(_entry(historical), "fixture|04")

    assert problems == [
        f"fixture|04: evidence path is not tracked by git: {historical} (canonical: {canonical})"
    ]


@pytest.mark.parametrize("path", ("opendata_fuyao/not-mapped.yaml", "../outside.txt"))
def test_unknown_or_unsafe_identity_fails_closed(path: str, temp_git_root: Path) -> None:
    problems = ledger_check._entry_problems(_entry(path), "fixture|05")

    assert len(problems) == 1
    assert "evidence path has no safe current identity" in problems[0]
    assert path in problems[0]


def test_historical_a3_b2_and_current_docs_evidence_paths_and_bytes_are_unchanged(
    temp_git_root: Path,
) -> None:
    layout = ledger_check._source_layout_module(temp_git_root)
    preserved_paths = (
        *HISTORY_HASHES,
        "docs/evidence/B2/gate.txt",
        "docs/evidence/C65/current-primary-20261008/model-domain-joint-final-primary-gate.txt",
    )

    for path in preserved_paths:
        assert layout.historical_identity(path) == path
    for path, expected_hash in HISTORY_HASHES.items():
        actual_hash = hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
        assert actual_hash == expected_hash, path
