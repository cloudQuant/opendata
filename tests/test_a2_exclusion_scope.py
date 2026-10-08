"""A first-party file may not vanish from a static plane because of its *name*
(C35, AC-2 / AC-17).

The vendored AkShare package now lives at the exact nested prefix
``opendata/data/providers/akshare/_vendor/``. Its first-party sibling adapter
must remain visible, while the nested upstream package receives a separate
ported audit. The vendor exclusion therefore must match the full component
prefix and not merely the word ``akshare`` or ``_vendor``. The adapter package
contains 15+ first-party modules and is checked by:

  * ``ruff check opendata`` (excluding only the nested vendor prefix);
  * ``mypy opendata/`` (a deliberate ``x: int = "s"`` in the package produced no
    line of output);
  * bandit, *including the leg a2-check runs on files it is handed* (the same
    marker line was reported as B404 when the file was copied out of the repo,
    and reported nowhere once it sat in the package);
  * and ``a2_check._is_a2_candidate``, which must keep all adapter files in A2
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

C45 extends the same census to ``alembic/`` and ``alembic_data/``. Both roots
were excluded from every plane on a rationale that does not survive reading the
code ("tool-generated migration env"): the modules under ``versions/`` are
hand-written and only *import* their DDL text from ``opendata/pipeline/ddl``.
Ten touched first-party files were invisible to a2-check, the ratchet, ruff's
walk and mypy's walk at the same time, and ``a2_check --files alembic/env.py``
answered "OK: no A2 files changed" - so an explicit request to look at one of
them read exactly like a pass.
"""

import re
import subprocess  # literal argv, shell disabled
import sys
from pathlib import Path

import pytest
import tomllib
import yaml

from scripts.quality import a2_check as guard
from scripts.quality.source_layout import FIRST_PARTY, VENDOR_ROOT, classify_path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Trees this project owns and therefore expects to be fully visible.
#: C45: the two alembic envs join - they are first-party Python, not
#: regenerable artifacts (only the SQL strings inside them are generated).
FIRST_PARTY_TREES = ("opendata", "scripts", "tests", "alembic", "alembic_data")

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


def files_in_layer(tree: str, layer: str) -> list[str]:
    """Return Python paths classified into one exact source layer."""
    return [name for name in tree_files(tree) if classify_path(name) == layer]


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
        assert len(files_in_layer(COLLISION_PACKAGE, FIRST_PARTY)) >= 15

    @pytest.mark.parametrize("rel_path", files_in_layer(COLLISION_PACKAGE, FIRST_PARTY))
    def test_a2_gate_accepts_each_file(self, rel_path: str) -> None:
        assert guard._is_a2_candidate(rel_path) is True

    def test_the_a2_set_computed_from_git_contains_the_package(self) -> None:
        """The predicate alone is not the gate: check the real resolved set."""
        resolved = guard.resolve_files(None)
        assert resolved is not None, "A2 baseline missing - the gate is inactive"
        missing = [
            name
            for name in files_in_layer(COLLISION_PACKAGE, FIRST_PARTY)
            if name not in set(resolved)
        ]
        assert missing == [], f"dropped from the zero-tolerance set: {missing}"

    def test_no_first_party_file_is_dropped_from_the_a2_set(self) -> None:
        offenders = [
            name
            for tree in FIRST_PARTY_TREES
            for name in tree_files(tree)
            if classify_path(name) == FIRST_PARTY and guard._is_a2_candidate(name) is False
        ]
        assert offenders == [], f"silently excluded by name: {offenders}"


class TestPortedTreeStaysSeparate:
    """The other half: ported code must stay outside first-party A2 checks."""

    @pytest.mark.parametrize("rel_path", [f"{VENDOR_ROOT}/__init__.py"])
    def test_nested_vendor_files_are_not_a2_candidates(self, rel_path: str) -> None:
        assert (REPO_ROOT / rel_path).is_file(), f"{rel_path} moved - this case proves nothing"
        assert guard._is_a2_candidate(rel_path) is False

    @pytest.mark.parametrize(
        "rel_path",
        ["alembic/env.py", "alembic_data/env.py"],
    )
    def test_the_migration_envs_are_candidates_now(self, rel_path: str) -> None:
        """C45: the roots left the exclusion set, so the predicate must accept them.

        Pinned on files that exist today, so the case cannot go vacuous the way
        ``alembic/x.py`` would have.
        """
        assert (REPO_ROOT / rel_path).is_file(), f"{rel_path} moved - this case proves nothing"
        assert guard._is_a2_candidate(rel_path) is True

    def test_migration_modules_touched_since_the_baseline_are_in_the_a2_set(self) -> None:
        """The A2 set must really carry them, not just accept the predicate."""
        resolved = guard.resolve_files(None)
        assert resolved is not None, "A2 baseline missing - the gate is inactive"
        on_disk = {name for tree in ("alembic", "alembic_data") for name in tree_files(tree)}
        dark = sorted(name for name in on_disk if name not in set(resolved))
        # alembic/env.py predates the A0 baseline and is unmodified since, so it
        # is legitimately outside the "touched since A0" set; nothing else is.
        assert dark == ["alembic/env.py"], f"migration files missing from the A2 set: {dark}"

    def test_a_walk_from_the_repo_root_still_skips_the_ported_tree(self) -> None:
        """``[tool.ruff].exclude`` patterns match the path *relative to the walk
        start*, so only a root walk shows the exclusion working. That is the
        invocation ``ruff check .`` and ``make lint`` use, and the one whose
        silence C35 exploited.
        """
        from_root = ruff_walked(".")
        expected_first_party = set(files_in_layer("opendata", FIRST_PARTY))
        assert expected_first_party <= from_root
        ported = {name for name in tree_files(VENDOR_ROOT) if name in from_root}
        assert ported == set(), f"ported files linted as first-party: {sorted(ported)[:5]}"


class TestRuffWalkCoversEveryFirstPartyFile:
    """``[tool.ruff].exclude`` may only name root trees, at the root."""

    @pytest.mark.parametrize("tree", FIRST_PARTY_TREES)
    def test_walk_census_equals_tree_census(self, tree: str) -> None:
        on_disk = set(files_in_layer(tree, FIRST_PARTY))
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
        dark = [name for name in files_in_layer("opendata", FIRST_PARTY) if mypy_excludes(name)]
        assert set(dark) == set(tree_files("opendata/data_fetch"))
        assert set(tree_files(VENDOR_ROOT)) <= {
            name for name in tree_files("opendata") if mypy_excludes(name)
        }

    def test_the_package_that_collided_is_not_dark(self) -> None:
        sample = tree_files(f"{COLLISION_PACKAGE}/models")
        assert sample, "provider models moved - this case proves nothing"
        assert [name for name in sample if mypy_excludes(name)] == []

    @pytest.mark.parametrize(
        "rel_path",
        [f"{VENDOR_ROOT}/x.py", "tests/x.py"],
    )
    def test_documented_root_trees_stay_out(self, rel_path: str) -> None:
        assert mypy_excludes(rel_path) is True

    @pytest.mark.parametrize(
        "rel_path",
        [
            "opendata/data/providers/akshare/adapter.py",
            "opendata/data/providers/akshare/_vendorish/adapter.py",
            "other/_vendor/adapter.py",
        ],
    )
    def test_vendor_exclusion_is_component_bounded(self, rel_path: str) -> None:
        assert mypy_excludes(rel_path) is False

    @pytest.mark.parametrize("rel_path", ["alembic/x.py", "alembic_data/x.py"])
    def test_the_migration_roots_are_not_mypy_dark(self, rel_path: str) -> None:
        """C45: ``^alembic/`` and ``^alembic_data/`` were dropped with the ruff entries."""
        assert mypy_excludes(rel_path) is False

    def test_mypy_walks_both_migration_envs(self) -> None:
        """A directory mypy refuses to walk is a dark plane, not a clean one.

        Before C45 ``mypy alembic`` exited 2 with "There are no .py[i] files in
        directory", which reads as an invocation error from the outside and as
        "nothing to complain about" to anything that only greps for ``error:``.
        """
        result = subprocess.run(
            [sys.executable, "-m", "mypy", "--no-color-output", "alembic", "alembic_data"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (result.stdout + result.stderr)[-800:]
        for tree in ("alembic", "alembic_data"):
            assert tree_files(tree), f"{tree}/ moved - this case proves nothing"


class TestBanditExcludesNoFirstPartyName:
    """bandit matches ``exclude_dirs`` anywhere in the path - even for files."""

    def test_no_entry_is_a_substring_of_a_first_party_path(self) -> None:
        entries = BANDIT_CONFIG["exclude_dirs"]
        assert entries, "exclude_dirs emptied - this case would prove nothing"
        first_party = {
            name for tree in FIRST_PARTY_TREES for name in files_in_layer(tree, FIRST_PARTY)
        }
        hits = sorted(
            f"{entry} matched {name}" for entry in entries for name in first_party if entry in name
        )
        assert hits == [], f"bandit never scans these: {hits[:10]}"

    def test_the_nested_ported_tree_is_still_excluded(self) -> None:
        ported = tree_files(VENDOR_ROOT)
        assert ported, f"{VENDOR_ROOT}/ moved - this case proves nothing"
        assert all(any(entry in name for entry in BANDIT_CONFIG["exclude_dirs"]) for name in ported)

    @pytest.mark.parametrize(
        "rel_path",
        [
            "opendata/data/providers/akshare/adapter.py",
            "opendata/data/providers/akshare/_vendorish/adapter.py",
            "other/_vendor/adapter.py",
        ],
    )
    def test_vendor_exclusion_does_not_match_similar_names(self, rel_path: str) -> None:
        assert not any(entry in rel_path for entry in BANDIT_CONFIG["exclude_dirs"])


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
