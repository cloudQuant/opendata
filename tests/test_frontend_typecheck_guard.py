"""``make frontend-typecheck`` must keep reading source files (C31).

The item used to run ``npx vue-tsc --noEmit`` with no ``-p``/``-b``. That reads
``frontend/tsconfig.json``, which is a solution file (``files: []`` plus
references), so TypeScript opened **zero** files and exited 0 — a planted
``const n: number = 'x'`` still passed the gate. Measured in
``docs/evidence/C31/typecheck-vacuity.txt``; the same class of silence covers the
e2e specs, which no project used to include at all.

A gate item that cannot fail is not a gate item, and the green of the run that
removed the silence cannot protect it from rotting back. So the rules the fix
relies on — the recipe's shape and the project graph that decides which files
are read — are asserted here, against the shipped config and against planted
regressions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
FRONTEND = REPO_ROOT / "frontend"
SOLUTION = "tsconfig.json"

# The planes a frontend type check has to cover, and one pattern each that
# proves the plane is inside the project. ``e2e`` is the one that was missing
# before C31: four spec files were run by playwright and read by nobody.
REQUIRED_PROJECTS = {
    "tsconfig.app.json": ("src/**/*.ts", "src/**/*.vue"),
    "tsconfig.e2e.json": ("e2e/**/*.ts", "playwright.config.ts"),
}


def strip_jsonc(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments without touching string contents.

    tsconfig files are JSONC, and a naive regex would eat the ``/*`` inside
    ``./src/*``. A quote tracker is three lines, so there is no excuse.
    """
    out: list[str] = []
    in_string = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\":
                index += 1
                if index < length:
                    out.append(text[index])
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def read_tsconfig(path: Path) -> dict[str, Any]:
    loaded: object = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    assert isinstance(loaded, dict)
    return loaded


def recipe_for(makefile_text: str, target: str) -> list[str]:
    """The tab-indented command lines of a make target, in order."""
    lines = makefile_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    except StopIteration:
        return []
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("\t"):
            recipe.append(line.strip())
        elif line.strip() == "":
            continue
        else:
            break
    return recipe


def judge_recipe(recipe: Sequence[str]) -> tuple[str, ...]:
    """Reasons why this recipe could report green without checking anything."""
    reasons: list[str] = []
    tsc = [line for line in recipe if "vue-tsc" in line or "tsc" in line]
    if not tsc:
        return ("NO_TSC_INVOCATION: the recipe never runs vue-tsc",)
    for line in tsc:
        tokens = line.split()
        selects_project = any(
            token in {"-b", "--build", "-p", "--project"} or token.startswith("--project=")
            for token in tokens
        )
        if not selects_project:
            reasons.append(
                f"NO_PROJECT_SELECTED: {line!r} reads the solution file, which has "
                "files: [] and checks zero files"
            )
            continue
        builds = any(token in {"-b", "--build"} for token in tokens)
        forces = "--force" in tokens
        if builds and not forces:
            reasons.append(
                f"STALE_BUILD_INFO: {line!r} builds without --force, so the "
                "*.tsbuildinfo a previous gate run leaves behind can make it a no-op"
            )
    return tuple(reasons)


def judge_graph(
    solution: Mapping[str, Any], projects: Mapping[str, Mapping[str, Any]]
) -> tuple[str, ...]:
    """Reasons why the project graph leaves a frontend plane unchecked."""
    reasons: list[str] = []
    references = {str(ref.get("path", "")).lstrip("./") for ref in solution.get("references", [])}
    for project, patterns in REQUIRED_PROJECTS.items():
        if project not in references:
            reasons.append(f"UNREFERENCED_PROJECT: {SOLUTION} does not reference {project}")
            continue
        include = list(projects.get(project, {}).get("include", []))
        if not include:
            reasons.append(f"EMPTY_INCLUDE: {project} includes no files")
        reasons.extend(
            f"UNCOVERED_PLANE: {project} does not include {pattern!r}"
            for pattern in patterns
            if not any(pattern in entry for entry in include)
        )
    return tuple(reasons)


class TestShippedConfig:
    def test_the_recipe_selects_a_project_and_forces_a_rebuild(self) -> None:
        recipe = recipe_for(MAKEFILE.read_text(encoding="utf-8"), "frontend-typecheck")

        assert recipe
        assert judge_recipe(recipe) == ()

    def test_the_project_graph_covers_src_and_the_playwright_specs(self) -> None:
        solution = read_tsconfig(FRONTEND / SOLUTION)
        names = {str(ref.get("path", "")).lstrip("./") for ref in solution.get("references", [])}
        projects = {name: read_tsconfig(FRONTEND / name) for name in sorted(names)}

        assert judge_graph(solution, projects) == ()

    def test_the_solution_file_itself_checks_nothing(self) -> None:
        # Not a defect — the reason the recipe must not point at it alone.
        solution = read_tsconfig(FRONTEND / SOLUTION)

        assert solution.get("files") == []
        assert judge_recipe(["cd frontend && npx vue-tsc --noEmit"]) != ()

    def test_build_state_stays_out_of_the_working_tree(self) -> None:
        # ``-b`` writes *.tsbuildinfo; a gate run must not leave it dirty.
        gitignore = (FRONTEND / ".gitignore").read_text(encoding="utf-8")

        assert "*.tsbuildinfo" in gitignore.split()


class TestGuardIsFalsifiable:
    """Each rule has to reject the regression it exists for."""

    def test_a_recipe_without_a_project_is_named(self) -> None:
        reasons = judge_recipe(["cd frontend && npx vue-tsc --noEmit"])

        assert len(reasons) == 1
        assert reasons[0].startswith("NO_PROJECT_SELECTED")

    def test_a_recipe_that_drops_force_is_named(self) -> None:
        reasons = judge_recipe(["cd frontend && npx vue-tsc -b"])

        assert reasons == (
            "STALE_BUILD_INFO: 'cd frontend && npx vue-tsc -b' builds without --force, "
            "so the *.tsbuildinfo a previous gate run leaves behind can make it a no-op",
        )

    def test_a_recipe_that_hid_the_check_entirely_is_named(self) -> None:
        assert judge_recipe(["cd frontend && echo skipping"]).__len__() == 1
        assert judge_recipe([]) == ("NO_TSC_INVOCATION: the recipe never runs vue-tsc",)

    def test_losing_the_e2e_project_reference_is_named(self) -> None:
        solution = {
            "files": [],
            "references": [{"path": "./tsconfig.node.json"}, {"path": "./tsconfig.app.json"}],
        }
        projects = {
            "tsconfig.node.json": {"include": ["vite.config.ts"]},
            "tsconfig.app.json": {"include": ["src/**/*.ts", "src/**/*.vue"]},
        }

        reasons = judge_graph(solution, projects)

        assert reasons == (
            "UNREFERENCED_PROJECT: tsconfig.json does not reference tsconfig.e2e.json",
        )

    def test_an_e2e_project_that_includes_nothing_is_named(self) -> None:
        solution = {
            "files": [],
            "references": [{"path": "./tsconfig.app.json"}, {"path": "./tsconfig.e2e.json"}],
        }
        projects: dict[str, dict[str, Any]] = {
            "tsconfig.app.json": {"include": ["src/**/*.ts", "src/**/*.vue"]},
            "tsconfig.e2e.json": {"include": []},
        }

        reasons = judge_graph(solution, projects)

        assert "EMPTY_INCLUDE: tsconfig.e2e.json includes no files" in reasons
        assert any("UNCOVERED_PLANE" in r and "e2e/**/*.ts" in r for r in reasons)

    def test_a_src_plane_left_out_of_the_app_project_is_named(self) -> None:
        solution = {
            "files": [],
            "references": [{"path": "./tsconfig.app.json"}, {"path": "./tsconfig.e2e.json"}],
        }
        projects = {
            "tsconfig.app.json": {"include": ["src/**/*.ts"]},
            "tsconfig.e2e.json": {"include": ["playwright.config.ts", "e2e/**/*.ts"]},
        }

        reasons = judge_graph(solution, projects)

        assert reasons == ("UNCOVERED_PLANE: tsconfig.app.json does not include 'src/**/*.vue'",)

    def test_the_jsonc_stripper_leaves_path_globs_intact(self) -> None:
        parsed = json.loads(
            strip_jsonc(
                '{\n  /* Bundler mode */\n  "paths": {"@/*": ["./src/*"]},\n'
                '  "include": ["src/**/*.vue"]  // trailing comment\n}\n'
            )
        )

        assert parsed == {"paths": {"@/*": ["./src/*"]}, "include": ["src/**/*.vue"]}
