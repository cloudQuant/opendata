"""A first-party file may not vanish from a static plane because of its *name*
(C35, AC-2 / AC-17).

Milestone A2 renamed the vendored top-level ``akshare/`` to ``opendata_http/``
and left four exclusion configs pointing at the old name. Three of those configs
match by path *segment* or substring rather than by root directory, so the
provider package ``opendata/data/providers/akshare/`` - 15 modules of first-party
code that happen to sit under a directory named after the vendor - was invisible
to:

  * ``ruff check opendata`` (walked 176 of the 191 files under ``opendata/``);
  * ``mypy opendata/`` (a deliberate ``x: int = "s"`` in the package produced no
    line of output);
  * bandit, *including the leg a2-check runs on files it is handed* (the same
    marker line was reported as B404 when the file was copied out of the repo,
    and reported nowhere once it sat in the package);
  * and ``a2_check._is_a2_candidate``, which dropped all 15 from the A2 set
    before any tool was even called.

The debt that was hiding was small (6 ``I001``, 5 files needing ``ruff format``,
0 mypy errors) - which is the point: an exclusion nobody can see is not
"cheap", it is unmeasured. These cases pin the *census*, not the debt, so the
hole cannot quietly reopen: if a name-based exclusion swallows anything under a
first-party tree again, the counts stop matching and a case reddens.

Each class is written to fail in both directions - the package must be visible
*and* the ported/legacy roots it is contrasted with must stay excluded - because
a guard that only checks half of that passes when someone deletes the exclusion
list entirely.
"""

import re
import subprocess  # literal argv, shell disabled
import sys
from pathlib import Path

import pytest
import tomllib
import yaml

from scripts.quality import a2_check as guard

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Trees this project owns and therefore expects to be fully visible.
FIRST_PARTY_TREES = ("opendata", "scripts", "tests")

#: The package C35 found dark: a first-party provider package that shares its
#: name with the vendored tree that used to sit at the repo root.
COLLISION_PACKAGE = "opendata/data/providers/akshare"

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
BANDIT_CONFIG = yaml.safe_load((REPO_ROOT / "bandit.yaml").read_text(encoding="utf-8"))


def tree_files(tree: str) -> list[str]:
    """Return repo-relative POSIX ``.py`` paths under *tree*, minus tool caches."""
    return sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def ruff_walked(tree: str) -> set[str]:
    """Return the files ``ruff check <tree>`` actually walks.

    ``--show-files`` reports ruff's own decision after ``[tool.ruff].exclude``
    has been applied, so this measures the plane instead of re-implementing its
    pattern semantics here.
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ruff", "check", tree, "--show-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {str(Path(line).relative_to(REPO_ROOT)) for line in result.stdout.split() if line}


def mypy_excludes(name: str) -> bool:
    """True when a path would be dropped by ``[tool.mypy].exclude``."""
    return any(re.search(pattern, name) for pattern in PYPROJECT["tool"]["mypy"]["exclude"])


class TestTheCollidingPackageIsInvisibleToNoPlane:
    """opendata/data/providers/akshare/ is first-party and is checked as such."""

    def test_the_package_is_still_there_to_lose(self) -> None:
        """Keep the sweeps below from going vacuous if the tree is ever moved."""
        assert len(tree_files(COLLISION_PACKAGE)) >= 15

    @pytest.mark.parametrize("rel_path", tree_files(COLLISION_PACKAGE))
    def test_a2_gate_accepts_each_file(self, rel_path: str) -> None:
        assert guard._is_a2_candidate(rel_path) is True

    def test_the_a2_set_computed_from_git_contains_the_package(self) -> None:
        """The predicate alone is not the gate: check the real resolved set."""
        resolved = guard.resolve_files(None)
        assert resolved is not None, "A2 baseline missing - the gate is inactive"
        missing = [name for name in tree_files(COLLISION_PACKAGE) if name not in set(resolved)]
        assert missing == [], f"dropped from the zero-tolerance set: {missing}"

    def test_no_first_party_file_is_dropped_from_the_a2_set(self) -> None:
        offenders = [
            name
            for tree in FIRST_PARTY_TREES
            for name in tree_files(tree)
            if guard._is_a2_candidate(name) is False
        ]
        assert offenders == [], f"silently excluded by name: {offenders}"


class TestRootTreesStayExcluded:
    """The other half: the ported/legacy layers must not be pulled in by name."""

    @pytest.mark.parametrize(
        "rel_path",
        ["opendata_http/__init__.py", "alembic/env.py", "alembic_data/env.py"],
    )
    def test_existing_root_tree_files_are_not_a2_candidates(self, rel_path: str) -> None:
        assert (REPO_ROOT / rel_path).is_file(), f"{rel_path} moved - this case proves nothing"
        assert guard._is_a2_candidate(rel_path) is False

    def test_a_walk_from_the_repo_root_still_skips_the_ported_tree(self) -> None:
        """``[tool.ruff].exclude`` patterns match the path *relative to the walk
        start*, so only a root walk shows the exclusion working. That is the
        invocation ``ruff check .`` and ``make lint`` use, and the one whose
        silence C35 exploited.
        """
        from_root = ruff_walked(".")
        # Non-vacuity: the root walk really does carry the first-party trees, so
        # an empty `ported` below is an exclusion working and not an empty scan.
        assert set(tree_files("opendata")) <= from_root
        ported = {name for name in tree_files("opendata_http") if name in from_root}
        assert ported == set(), f"ported files linted as first-party: {sorted(ported)[:5]}"


class TestRuffWalkCoversEveryFirstPartyFile:
    """``[tool.ruff].exclude`` may only name root trees, at the root."""

    @pytest.mark.parametrize("tree", FIRST_PARTY_TREES)
    def test_walk_census_equals_tree_census(self, tree: str) -> None:
        on_disk = set(tree_files(tree))
        walked = ruff_walked(tree)
        assert walked == on_disk, (
            f"ruff walks {len(walked)} of {len(on_disk)} files under {tree}/; "
            f"unseen={sorted(on_disk - walked)[:20]} extra={sorted(walked - on_disk)[:20]}"
        )

    def test_exclude_entries_that_could_match_a_nested_path_are_gone(self) -> None:
        """A slashless ruff exclude pattern matches any directory basename.

        Pinned structurally so the fix cannot be half-applied: every entry here
        must carry a slash (a root-relative form) except the tool caches, which
        are not project paths.
        """
        allowed_bare = {".venv", "htmlcov", ".git", "__pycache__", "node_modules"}
        offenders = [
            pattern
            for pattern in PYPROJECT["tool"]["ruff"]["exclude"]
            if "/" not in pattern and pattern not in allowed_bare
        ]
        assert offenders == [], f"basename-at-any-depth exclude patterns: {offenders}"


class TestMypyExcludesOnlyWhatItSays:
    """``[tool.mypy].exclude`` is a ``re.search`` over the path - so it anchors."""

    def test_only_the_legacy_data_fetch_subtree_is_dark_under_opendata(self) -> None:
        dark = [name for name in tree_files("opendata") if mypy_excludes(name)]
        assert set(dark) == set(tree_files("opendata/data_fetch"))

    def test_the_package_that_collided_is_not_dark(self) -> None:
        sample = tree_files(f"{COLLISION_PACKAGE}/models")
        assert sample, "provider models moved - this case proves nothing"
        assert [name for name in sample if mypy_excludes(name)] == []

    @pytest.mark.parametrize(
        "rel_path",
        ["opendata_http/x.py", "tests/x.py", "alembic/x.py", "alembic_data/x.py"],
    )
    def test_documented_root_trees_stay_out(self, rel_path: str) -> None:
        assert mypy_excludes(rel_path) is True


class TestBanditExcludesNoFirstPartyName:
    """bandit matches ``exclude_dirs`` anywhere in the path - even for files."""

    def test_no_entry_is_a_substring_of_a_first_party_path(self) -> None:
        entries = BANDIT_CONFIG["exclude_dirs"]
        assert entries, "exclude_dirs emptied - this case would prove nothing"
        first_party = {name for tree in FIRST_PARTY_TREES for name in tree_files(tree)}
        hits = sorted(
            f"{entry} matched {name}" for entry in entries for name in first_party if entry in name
        )
        assert hits == [], f"bandit never scans these: {hits[:10]}"

    def test_the_ported_tree_is_still_excluded(self) -> None:
        ported = tree_files("opendata_http")
        assert ported, "opendata_http/ moved - this case proves nothing"
        assert any(entry in ported[0] for entry in BANDIT_CONFIG["exclude_dirs"])


class TestDeveloperViewTargetsExist:
    """``make lint``'s ported-tree command named a directory that no longer does."""

    @staticmethod
    def _make_var(name: str) -> list[str]:
        text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        match = re.search(rf"^{name} := (\S.*?)$", text, re.M)
        assert match, f"{name} is no longer defined in the Makefile"
        return match.group(1).split()

    @pytest.mark.parametrize("name", ["PY_SELFDEV", "PY_PORTED"])
    def test_every_named_tree_exists_and_holds_python(self, name: str) -> None:
        for target in self._make_var(name):
            tree = REPO_ROOT / target
            assert tree.is_dir(), f"{name} names {target}/, which does not exist"
            assert tree_files(target), f"{name} names {target}/, which holds no .py files"
