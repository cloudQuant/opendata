"""C105: judge the one file the AST witness refuses to clear at the level of its rendered product.

``document_builder.py`` was rewritten during the A2 probe repair -- a dead ``grab`` regex call was
removed and four README literals re-split to fit the 100-column rule -- so its string-constant
multiset moved and ``ast_witness.py`` correctly answers ``moved``. A literal multiset is not
what the record consumes, though: this builder's product is six markdown documents, and what a
reader of ``docs/evidence/C103/<family>/README.md`` asks is whether its claims moved.

Four runs of a real builder over the archived carriers:

* ``head``     -- the staged (pre-repair) blob, taken with ``git show :PATH``, run from a temp dir;
* ``live``     -- the repaired builder in the worktree;
* ``prose arm`` -- the live source with one printed literal perturbed, which must move a paragraph;
* ``claim arm`` -- the live source with a literal that renders inside a code span perturbed, which
  must move the code-span/number multiset.

Each content comparison therefore needs a positive control, and a control only counts when the
perturbed builder still runs clean: candidates whose run exits non-zero, writes a directory that was
not there before, or leaves the judged level untouched are skipped and named, and the arm keeps
trying later literals. An arm that cannot arm is refused rather than quietly counted as passing,
which is what happened the first time this gate was written -- the first token-bearing literal is a
family name, so perturbing it changed an output path and the builder died. The claim arm is handed
the prose arm's literal as a skip list, so the block level and the token level are armed by two
separate changes.

Every document is judged at three levels: raw bytes, paragraph blocks with inner whitespace
collapsed (the level at which markdown says the same thing at a different column), and the
multiset of code spans and numbers. Bytes are reported and allowed to move; both content levels
must hold for all six documents. Each document is finally written back with the bytes the probe
found on disk, so running this gate leaves the repository as it was.

Usage: run this file with the gate interpreter (``envs/py313``).
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import re
import shutil
import subprocess  # nosec B404  # each arm runs the builder as its own process
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUILDER = "docs/evidence/C103/probes/document_builder.py"
EVID = REPO / "docs/evidence/C103"
FAMILIES = ("fmp-fetch-array-arity", "batch5-source-pins", "batch5-scenario-census")
TAMPER_MARK = "!"
DOC_PATHS = [EVID / family / suffix for family in FAMILIES for suffix in ("README.md", "face.md")]


def digest(payload: bytes) -> str:
    """The sha256 of a document's bytes, at the width the carriers print."""
    return hashlib.sha256(payload).hexdigest()[:16]


def doc_map() -> dict[str, bytes]:
    """Every generated document, keyed by its repo-relative path, as bytes."""
    return {str(path.relative_to(REPO)): path.read_bytes() for path in DOC_PATHS}


def normalize_blocks(text: str) -> list[str]:
    """Every blank-line-separated block, its inner whitespace collapsed to single spaces.

    This is the level at which a markdown paragraph is what it says: re-wrapping a sentence at a
    different column is not a change of claim, while a block that splits, merges or moves is.
    """
    return [" ".join(block.split()) for block in re.split(r"\n[ \t]*\n", text.strip())]


def claim_tokens(joined: str) -> Counter[str]:
    """The multiset of inline literals and numbers in a block-joined document.

    Double backticks are the builder's own inline-literal style; they are folded to one first,
    because pairing single backticks over a `` ``x`` `` run yields spans of the prose between them.
    """
    single = joined.replace("``", "`")
    return Counter(re.findall(r"`[^`]+`|-?\b\d[\d.,()%]*\b", single))


def levels(joined: str) -> tuple[list[str], Counter[str]]:
    """The two judged views of a document: its paragraph blocks and its claim tokens."""
    return normalize_blocks(joined), claim_tokens(joined)


def join(text: str) -> str:
    """The document as one line per paragraph, the form both content comparisons read."""
    return " ".join(normalize_blocks(text))


def run_builder(source: str, workdir: Path, tag: str) -> tuple[int, str]:
    """Write one builder variant into the temp dir under its own name and run it from the repo."""
    script = workdir / f"builder_{tag}.py"
    script.write_text(source, encoding="utf-8")
    proc = subprocess.run(  # noqa: S603  # nosec B603  # sys.executable plus a literal argv, no shell
        [sys.executable, str(script)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def head_source() -> str:
    """The staged copy of the builder, which is the file as it stood before the repair."""
    proc = subprocess.run(  # noqa: S603  # nosec B603 B607  # literal git argv, git resolves on PATH
        ["git", "show", f":{BUILDER}"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git show :{BUILDER} exit={proc.returncode} tail={proc.stderr[-200:]}")
    return proc.stdout


def docstring_ids(tree: ast.Module) -> set[int]:
    """The ids of every docstring constant, so a tamper cannot land on a line that never renders."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        first = body[0] if body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            out.add(id(first.value))
    return out


def literal_candidates(
    source: str, generated: dict[str, bytes], eligible: Callable[[str, str], bool]
) -> list[tuple[int, str, str]]:
    """Every printed literal that shows up in a live document, as (walk index, value, document).

    The walk index is the handle used to perturb the same constant in a freshly parsed tree;
    ``ast.walk`` is deterministic for one source, so no node has to be kept alive between the two.
    """
    tree = ast.parse(source)
    skip = docstring_ids(tree)
    out: list[tuple[int, str, str]] = []
    for index, node in enumerate(ast.walk(tree)):
        if id(node) in skip or not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        value = node.value
        if len(value) < 8 or TAMPER_MARK in value:
            continue
        hit = next(
            (rel for rel, payload in generated.items() if eligible(value, payload.decode())), None
        )
        if hit is None:
            continue
        out.append((index, value, hit))
    return out


def tamper_at(source: str, index: int, value: str) -> str:
    """Append the tamper mark to one printed constant and return the rewritten source."""
    tree = ast.parse(source)
    node = list(ast.walk(tree))[index]
    if not isinstance(node, ast.Constant) or node.value != value:
        raise RuntimeError(f"walk index {index} is not the constant {value[:24]!r} any more")
    node.value = f"{value}{TAMPER_MARK}"
    return ast.unparse(tree)


def stray_children(snapshot: set[str]) -> list[Path]:
    """Directories under the evidence root that appeared since the snapshot and carry the mark.

    A perturbed family name becomes an output directory, so an arm that wrote one would not be a
    valid run. Only a name containing the tamper mark is returned, so this probe can never hand back
    a path it did not create.
    """
    fresh = {p.name for p in EVID.iterdir() if p.is_dir()} - snapshot
    return [EVID / name for name in sorted(fresh) if TAMPER_MARK in name]


def prose_eligible(value: str, document: str) -> bool:
    """True when the literal renders somewhere in the document's text."""
    return value in join(document)


def claim_eligible(value: str, document: str) -> bool:
    """True when the literal renders inside an inline literal or carries a digit inside one."""
    joined = join(document)
    return f"`{value}`" in joined.replace("``", "`") or bool(
        re.search(rf"`[^`]*{re.escape(value)}", joined)
    )


def arm_level(
    live_src: str,
    live_map: dict[str, bytes],
    workdir: Path,
    tag: str,
    eligible: Callable[[str, str], bool],
    view: Callable[[str], object],
    snapshot: set[str],
    skip_values: frozenset[str] = frozenset(),
    limit: int = 64,
) -> tuple[int, str, str, int, list[str]]:
    """Find the first printed literal whose perturbation, in a clean builder run, moves a level.

    Returns ``(index, value, document, documents_moved, skipped)``; ``index`` is -1 when no
    candidate armed the level. Each skipped candidate is named with the reason it does not count.
    ``skip_values`` holds the literals an earlier arm already used, so two arms cannot be armed by
    the same change.
    """
    skipped: list[str] = []
    chosen = [
        (index, value, rel)
        for index, value, rel in literal_candidates(live_src, live_map, eligible)
        if value not in skip_values
    ][:limit]
    for index, value, rel in chosen:
        exit_code, _ = run_builder(tamper_at(live_src, index, value), workdir, f"{tag}{index}")
        if exit_code != 0:
            skipped.append(f"#{index} {value[:22]!r} exit={exit_code}")
            continue
        strays = stray_children(snapshot)
        if strays:
            for path in strays:
                shutil.rmtree(path)
            skipped.append(f"#{index} {value[:22]!r} wrote {len(strays)} stray dir(s), removed")
            continue
        made = doc_map()
        moved = sum(
            1
            for rel_key in live_map
            if view(made[rel_key].decode()) != view(live_map[rel_key].decode())
        )
        if moved == 0:
            skipped.append(f"#{index} {value[:22]!r} level unchanged")
            continue
        return index, value, rel, moved, skipped
    return -1, "", "", 0, skipped


def first_moved_pair(head: list[str], live: list[str]) -> tuple[str, str]:
    """The first block pair that differs, for a refusal face that names what moved."""
    return next(
        ((x, y) for x, y in itertools.zip_longest(head, live, fillvalue="") if x != y),
        ("", ""),
    )


def _written_line(tail: str) -> str:
    """Quote the builder's own ``documents_written`` reading, minus its nested ``face:``."""
    line = next((x for x in tail.splitlines() if "documents_written" in x), "(absent)")
    return line.removeprefix("face: ")


def main() -> int:
    """Run the four arms, judge the documents at three levels, then leave the tree as found."""
    original = doc_map()
    missing = [str(path.relative_to(REPO)) for path in DOC_PATHS if not path.is_file()]
    for rel in missing:
        print(f"face: MISSING_DOCUMENT {rel}")
    if missing:
        print("verdict: REFUSED - a document the builder writes is not on disk")
        return 1
    snapshot = {p.name for p in EVID.iterdir() if p.is_dir()}
    live_src = (REPO / BUILDER).read_text(encoding="utf-8")
    print(
        f"face: documents_in_scope={len(DOC_PATHS)} builder_lines={len(live_src.splitlines())}"
        f" evidence_dirs_in_snapshot={len(snapshot)}"
    )

    refusals: list[str] = []
    with tempfile.TemporaryDirectory(prefix="c105-docs-reidentity-") as tmp:
        workdir = Path(tmp)
        head_exit, head_tail = run_builder(head_source(), workdir, "head")
        head_map = doc_map()
        live_exit, live_tail = run_builder(live_src, workdir, "live")
        live_map = doc_map()
        if (head_exit, live_exit) != (0, 0):
            refusals.append(f"an arm exited non-zero (head={head_exit} live={live_exit})")
        prose_index, prose_value, prose_rel, prose_moved, prose_skipped = arm_level(
            live_src, live_map, workdir, "prose", prose_eligible, normalize_blocks, snapshot
        )
        claim_index, claim_value, claim_rel, claim_moved, claim_skipped = arm_level(
            live_src,
            live_map,
            workdir,
            "claim",
            claim_eligible,
            claim_tokens,
            snapshot,
            frozenset({prose_value}),
        )
        if prose_moved == 0:
            refusals.append("no clean builder run moved a paragraph block, so blocks are unjudged")
        if claim_moved == 0:
            refusals.append("no clean builder run moved a claim token, so tokens are unjudged")
        if prose_moved and claim_moved and prose_index == claim_index:
            refusals.append("both arms landed on the same literal, so one change armed two levels")

    for rel, payload in original.items():
        (REPO / rel).write_bytes(payload)
    restored = doc_map() == original
    strays = stray_children(snapshot)
    for path in strays:
        print(f"face: STRAY_DIR {path.relative_to(REPO)}")

    print(
        f"face: arm_head_exit={head_exit} arm_live_exit={live_exit}"
        f" arm_prose_armed={prose_index >= 0} arm_claim_armed={claim_index >= 0}"
    )
    print(
        "face: head_written="
        + str(_written_line(head_tail))
        + " live_written="
        + str(_written_line(live_tail))
    )

    bytes_same = blocks_same = tokens_same = 0
    for rel in sorted(live_map):
        head_text, live_text = head_map[rel].decode(), live_map[rel].decode()
        head_blocks, head_tokens = levels(join(head_text))
        live_blocks, live_tokens = levels(join(live_text))
        doc_blocks_same = head_blocks == live_blocks
        doc_tokens_same = head_tokens == live_tokens
        bytes_same += head_map[rel] == live_map[rel]
        blocks_same += doc_blocks_same
        tokens_same += doc_tokens_same
        print(
            f"identity: {rel} bytes_same={head_map[rel] == live_map[rel]}"
            f" blocks_same={doc_blocks_same} tokens_same={doc_tokens_same}"
            f" byte_delta={len(live_map[rel]) - len(head_map[rel])}"
            f" head_sha={digest(head_map[rel])} live_sha={digest(live_map[rel])}"
        )
        if not doc_blocks_same:
            pair = first_moved_pair(head_blocks, live_blocks)
            print(f"    block_head={pair[0][:150]!r}")
            print(f"    block_live={pair[1][:150]!r}")
        if not doc_tokens_same:
            lost = head_tokens - live_tokens
            gained = live_tokens - head_tokens
            print(f"    tokens_only_in_head={dict(list(lost.items())[:4])}")
            print(f"    tokens_only_in_live={dict(list(gained.items())[:4])}")

    print(
        f"face: candidate_pool_prose={len(literal_candidates(live_src, live_map, prose_eligible))}"
        f" candidate_pool_claim={len(literal_candidates(live_src, live_map, claim_eligible))}"
    )
    print(
        f"face: control_prose_index={prose_index} literal={prose_value[:40]!r} into={prose_rel}"
        f" blocks_moved={prose_moved} candidates_skipped={len(prose_skipped)}"
    )
    for note in prose_skipped:
        print(f"    prose_skip {note}")
    print(
        f"face: control_claim_index={claim_index} literal={claim_value[:40]!r} into={claim_rel}"
        f" tokens_moved={claim_moved} candidates_skipped={len(claim_skipped)}"
    )
    for note in claim_skipped:
        print(f"    claim_skip {note}")
    print(
        f"face: control_arms_distinct={prose_index != claim_index}"
        f" levels_armed={int(prose_moved > 0) + int(claim_moved > 0)}"
    )
    print(f"face: repo_restored={restored} stray_dirs_left={len(strays)}")

    failures = list(refusals)
    if blocks_same != len(live_map):
        failures.append(
            f"{len(live_map) - blocks_same} document(s) moved a paragraph block, not only its wrap"
        )
    if tokens_same != len(live_map):
        failures.append(f"{len(live_map) - tokens_same} document(s) moved a code span or a number")
    if not restored:
        failures.append("the probe did not leave the six documents as it found them")
    if strays:
        failures.append(f"{len(strays)} stray director(ies) were written and are not cleaned up")
    for reason in failures:
        print(f"refusal: {reason}")
    if failures:
        print("verdict: REFUSED - the literal move in document_builder.py is not content-neutral")
        return 1
    print(
        f"verdict: PASS - bytes moved in {len(live_map) - bytes_same} document(s) by re-wrapping"
        " only; every paragraph block and every claim token is intact, and each level is armed by"
        " its own clean builder run"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
