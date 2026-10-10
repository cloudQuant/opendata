"""Re-audit every drifted C85 reference-policy pin, then re-pin it with recorded evidence.

The ``zero-dep-check`` member refused to start: 18 of the 87 registered entries no longer hash
to the bytes on disk, so the walker aborted on an unusable policy and never printed its walked
line -- which is what makes AC2-07 read ``walker_count=unparsed``.

A digest re-pin is only honest if the *approval content* is unchanged, so every figure printed
here is measured, not asserted:

1. the pinned bytes are recovered by walking each path's own git history and hashing every
   revision until one equals the pinned ``sha256`` (no match is a finding, not a warning);
2. the drift is that revision to HEAD for that path, classified line by line (added, removed,
   brand-carrying, added-line-is-a-removed-line-plus-a-nosec-tail -- the C81 migration that
   moved 156 bandit exemptions onto line-level ``# nosec`` comments);
3. the registered hits are re-measured with the repository's own scanner (``scan_source`` ->
   ``(module, kind)`` multiset, then ``filter_metadata_findings``) on the pinned bytes and on
   the disk bytes, next to the load-shape face a re-pin must never move. Non-Python
   registrations (``bandit.yaml``) have no AST arm and say so.

The brand-line and brand-occurrence counts are disclosed rather than gated *when the AST arm
applies*, and gated when it does not. The reason is measured, not assumed: the reviewer prose this
file appends records the faces holding (``akshare 出现行 pinned=124 live=124``) at the C75 and C78
re-pins of ``scripts/quality/acceptance_item_probe.py``, and this round's audit moved them
(124 -> 126) the moment a counterfact reading named sources in its fact overlay. Demanding the
count stay frozen would be satisfiable only by deleting prose, and deleting prose is not what the
policy forbids -- loading the upstream package is. So a moved brand face has to come with a
classification: every brand-carrying added line is tested against the load-shape regex, and one hit
there is a finding. Together with the two faces that stay hard-gated (the scanner multiset and
``load_shape_lines``) this is a narrower claim than the count equality, not a weaker one.

A clean zero is only worth printing next to an arm that bites, so two controls run in the same
process: ``--tamper-policy`` flips one hex character of a re-pinned entry's ``sha256`` in the
policy file, runs the real checker and requires it to name exactly that path, then restores the
bytes; ``--tamper-source`` feeds the comparison the pinned bytes plus one smuggled
``import akshare`` and requires it to report the moved multiset; ``--tamper-prose`` adds only a
brand-naming ``#`` comment to the pinned bytes and requires the gate to stay silent, which is the
arm that says the brand-face loosening is a measurement change rather than a hole.

Run: python3 docs/evidence/C85/reference-policy-repin.py             (audit only, writes nothing)
     python3 docs/evidence/C85/reference-policy-repin.py --write     (re-pin the drifted entries)
     python3 docs/evidence/C85/reference-policy-repin.py --tamper-policy
     python3 docs/evidence/C85/reference-policy-repin.py --tamper-source
     python3 docs/evidence/C85/reference-policy-repin.py --tamper-prose
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import types

ROOT = Path(__file__).resolve().parents[3]
POLICY = "docs/quality/akshare-reference-allowlist.json"
VERIFIER = ROOT / "scripts/codemod/verify_no_akshare.py"
REVIEW_DATE = "2026-10-10"
ROUNDS = ("akshare", "openbb")  # the scanner's own FORBIDDEN_ROOTS
BRAND_RE = re.compile("akshare|openbb", re.IGNORECASE)
LOAD_SHAPE = re.compile(
    r"(?:import\s+(?:akshare|openbb)|from\s+(?:akshare|openbb)|"
    r"(?:import_module|__import__)\(\s*['\"](?:akshare|openbb))",
    re.IGNORECASE,
)
BRAND_FACES = tuple(f"{root}_{field}" for root in ROUNDS for field in ("lines", "occurrences"))
#: Hard-gated in every case: a line that loads the upstream package by name is what the policy
#: forbids, and no amount of prose editing can restore this face the way it can restore a count.
LOAD_FACE = "load_shape_lines"
MULTISET = Counter[tuple[str, str]]


def git(*args: str) -> bytes:
    """Run git on stdout only, with the repo as cwd and a fixed executable."""
    process = subprocess.run(  # nosec B603  # noqa: S603 - no shell, argv is repo paths
        [shutil.which("git") or "git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return process.stdout


def sha256(data: bytes) -> str:
    """Return the hex digest of the bytes."""
    return hashlib.sha256(data).hexdigest()


def load_scanner() -> types.ModuleType:
    """Import the repository's own reference scanner by path."""
    spec = importlib.util.spec_from_file_location("vna", VERIFIER)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the repository scanner")
    module = importlib.util.module_from_spec(spec)
    sys.modules["vna"] = module
    spec.loader.exec_module(module)
    return module


def policy_text(path: str = POLICY) -> str:
    """Read the policy file as text."""
    return (ROOT / path).read_text(encoding="utf-8")


def policy_document(text: str) -> dict[str, object]:
    """Decode policy JSON text."""
    parsed: dict[str, object] = json.loads(text)
    return parsed


def entry_map(raw: dict[str, object]) -> dict[str, dict[str, object]]:
    """Map registered path -> policy entry."""
    entries = cast("list[dict[str, object]]", raw["entries"])
    return {str(entry["path"]): entry for entry in entries}


def dump(raw: dict[str, object]) -> str:
    """Serialize a policy document the way the committed file is serialized."""
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def hit_multisets(
    scanner: types.ModuleType, path: str, source: str
) -> tuple[MULTISET | None, MULTISET | None]:
    """Return (raw, policy-filtered) (module, kind) multisets, or (None, None) if not Python."""
    try:
        findings = scanner.scan_source(source, path)
    except SyntaxError:
        return None, None
    raw = Counter((finding.module, finding.kind) for finding in findings)
    kept = scanner.filter_metadata_findings(path, source, findings, None)
    return raw, Counter((finding.module, finding.kind) for finding in kept)


def faces(source: str) -> dict[str, int]:
    """Count the faces a re-pin has to leave untouched, plus the line count."""
    lines = source.splitlines()
    measured = {
        "lines": len(lines),
        "load_shape_lines": sum(1 for line in lines if LOAD_SHAPE.search(line)),
    }
    for root in ROUNDS:
        measured[f"{root}_lines"] = sum(1 for line in lines if root in line.lower())
        measured[f"{root}_occurrences"] = source.lower().count(root)
    return measured


def diff_counts(old_source: str, live_source: str) -> tuple[int, int, int]:
    """Count added and removed lines the way a diff counts them, not the way a set does.

    ``autojunk=False`` is set explicitly rather than left to the default. difflib's junk heuristic
    demotes frequently repeated lines (a lone closing brace, blank lines) to non-anchors on long
    inputs, which can turn a pure append into a chain of replaces. On this round's drift the flag
    changes nothing (both arms measured 401 added / 0 removed, equal to ``git diff --numstat``), so
    it is recorded here as a guard against a heuristic, not as the cause of a difference.

    The third figure is how many of the diff-added lines carry text that already occurs somewhere
    in the pinned bytes. That is the population which explains the gap between this count and the
    deduplicated one in :func:`changed_lines`, so :func:`reason` can measure the cause instead of
    asserting it: a gap equal to it is fully explained by de-duplication, and a gap larger than it
    is published as unexplained rather than attributed to a mechanism that did not run.
    """
    old_lines = old_source.splitlines()
    live_lines = live_source.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, live_lines, autojunk=False)
    old_set = set(old_lines)
    added = removed = dup_added = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            block = live_lines[j1:j2]
            added += len(block)
            dup_added += sum(1 for line in block if line in old_set)
        if tag in ("delete", "replace"):
            removed += i2 - i1
    return added, removed, dup_added


def changed_lines(old_source: str, live_source: str) -> tuple[int, int, int, int, int, list[str]]:
    """Classify the lines that moved between the pinned bytes and the live ones.

    The counts here are a *text-deduplicated* population: an added line whose exact text already
    occurred anywhere in the pinned bytes counts as zero, so ``added`` reads below a git numstat of
    the same drift. That is deliberate -- this population supports the two classification clauses
    (nosec tails, brand-carrying added lines) -- but it is not a diff count, so :func:`compare`
    measures :func:`diff_counts` next to it and :func:`reason` publishes both counts side by side.
    A duplicated brand-carrying *load* cannot hide in either: ``load_shape_lines`` and the scanner
    multiset count every occurrence, so both are measured over the whole file and gated as totals.

    Returns:
        Added and removed counts, the brand-carrying counts on each side, how many added lines are
        a removed line plus a ``# nosec`` tail, and the brand-carrying added lines themselves --
        which :func:`compare` has to classify one by one before a brand face may move.
    """
    old_lines, live_lines = old_source.splitlines(), live_source.splitlines()
    old_set, live_set = set(old_lines), set(live_lines)
    added = [line for line in live_lines if line not in old_set]
    removed = [line for line in old_lines if line not in live_set]
    removed_bases = {line.rstrip() for line in removed}

    def is_nosec_tail(line: str) -> bool:
        head, marker, _tail = line.partition("# nosec")
        if not marker:
            return False
        return head.rstrip() in removed_bases

    brand_added = [line for line in added if BRAND_RE.search(line)]
    return (
        len(added),
        len(removed),
        len(brand_added),
        sum(1 for line in removed if BRAND_RE.search(line)),
        sum(1 for line in added if is_nosec_tail(line)),
        brand_added,
    )


def compare(
    scanner: types.ModuleType, path: str, old_source: str, live_source: str
) -> tuple[list[str], dict[str, object]]:
    """Report every way the approval content moved between the pinned and the live bytes."""
    problems: list[str] = []
    old_raw, old_kept = hit_multisets(scanner, path, old_source)
    live_raw, live_kept = hit_multisets(scanner, path, live_source)
    hits_same = old_raw == live_raw and old_kept == live_kept
    if old_raw is None or live_raw is None or old_kept is None or live_kept is None:
        ast_note = "AST 臂不适用（该注册不是可解析的 Python 模块）：命中面按品牌行与加载形态行度量"
    else:
        ast_note = (
            f"扫描器 (module, kind) 命中多重集 pinned={sum(old_raw.values())} "
            f"live={sum(live_raw.values())}、策略过滤后 pinned={sum(old_kept.values())} "
            f"live={sum(live_kept.values())}，identical={hits_same}"
        )
    if not hits_same:
        problems.append(f"{path}: finding multiset {dict(old_raw or {})} -> {dict(live_raw or {})}")
    old_faces, live_faces = faces(old_source), faces(live_source)
    problems.extend(
        f"{path}: face {key} {old_faces[key]} -> {live_faces[key]}"
        for key in (LOAD_FACE,)
        if old_faces[key] != live_faces[key]
    )
    moved_brand_faces = {
        key: (old_faces[key], live_faces[key])
        for key in BRAND_FACES
        if old_faces[key] != live_faces[key]
    }
    added, removed, added_brand, removed_brand, nosec_tails, brand_added = changed_lines(
        old_source, live_source
    )
    diff_added, diff_removed, dup_added = diff_counts(old_source, live_source)
    ast_applies = old_raw is not None and live_raw is not None
    loaded_added = [line for line in brand_added if LOAD_SHAPE.search(line)]
    if loaded_added:
        problems.append(
            f"{path}: {len(loaded_added)} brand-carrying added line(s) are themselves load "
            f"shapes: {'；'.join(line.strip()[:90] for line in loaded_added)}"
        )
    elif not ast_applies and (moved_brand_faces or added_brand or removed_brand):
        problems.append(
            f"{path}: brand faces moved {moved_brand_faces} with +{added_brand}/-{removed_brand}"
            " brand lines, and this registration has no AST arm to say whether the brand is "
            "loaded -- a count equality is the only judge available here, so it stays a finding"
        )
    measured: dict[str, object] = {
        "ast_note": ast_note,
        "ast_applies": ast_applies,
        "hits_same": hits_same,
        "hits_pinned": 0 if old_raw is None else sum(old_raw.values()),
        "hits_live": 0 if live_raw is None else sum(live_raw.values()),
        "faces_pinned": old_faces,
        "faces_live": live_faces,
        "faces": live_faces,
        "moved_brand_faces": moved_brand_faces,
        "brand_added_lines": [line.strip()[:110] for line in brand_added],
        "loaded_added": len(loaded_added),
        "added": added,
        "removed": removed,
        "diff_added": diff_added,
        "diff_removed": diff_removed,
        "diff_dup_in_pin": dup_added,
        "added_brand": added_brand,
        "removed_brand": removed_brand,
        "nosec_tails": nosec_tails,
    }
    return problems, measured


def locate_pin(path: str, pinned: str) -> tuple[str | None, list[str], int]:
    """Find the newest revision whose blob hashes to the pinned digest.

    Returns (pin revision, revisions newer than it, how many revisions match it).
    """
    revs = git("log", "--format=%H", "--", path).decode().split()
    matches = [rev for rev in revs if sha256(git("show", f"{rev}:{path}")) == pinned]
    if not matches:
        return None, revs, 0
    newest = min(revs.index(match) for match in matches)
    return revs[newest], revs[:newest], len(matches)


def reason(
    measured: dict[str, object],
    pin_rev: str,
    mover: str,
    newer: int,
    problems: list[str],
) -> str:
    """One honest sentence about what moved and what did not, from measured numbers.

    Args:
        measured: The faces, hit multisets and line classifications from :func:`compare`.
        pin_rev: The revision whose blob equals the pinned digest.
        mover: The newest revision of the path after the pin.
        newer: How many revisions of the path are newer than the pin.
        problems: What :func:`compare` refused for this entry; the closing clause is only allowed
            to call the drift a re-pin when that list is empty, because this sentence is written
            into the policy's ``reviewer`` field and a reader will believe it.

    Returns:
        The attribution string, naming the mover revision so two re-pins in one round read apart.
    """
    pinned_faces: dict[str, int] = measured["faces_pinned"]  # type: ignore[assignment]
    live_faces: dict[str, int] = measured["faces_live"]  # type: ignore[assignment]
    moved: dict[str, tuple[int, int]] = measured["moved_brand_faces"]  # type: ignore[assignment]
    diff_added = int(str(measured["diff_added"]))
    diff_removed = int(str(measured["diff_removed"]))
    dedup_added = int(str(measured["added"]))
    dup = int(str(measured["diff_dup_in_pin"]))
    brand_lines = cast("list[str]", measured["brand_added_lines"])
    brand_face = "；".join(
        f"{root} 出现行 {pinned_faces[f'{root}_lines']}->"
        f"{live_faces[f'{root}_lines']}、出现次数 "
        f"{pinned_faces[f'{root}_occurrences']}->{live_faces[f'{root}_occurrences']}"
        + ("（未移动）" if not any(key.startswith(root) for key in moved) else "（移动）")
        for root in ROUNDS
    )
    nosec_clause = (
        f"其中 {measured['nosec_tails']}/{measured['added']} 条去重口径新增行是某条被删行的同文加 "
        f"# nosec 尾注释（C81 把 bandit 例外从全局 skips 收到行级）"
        if int(str(measured["nosec_tails"]))
        else f"去重口径新增行里没有一条是被删行的同文加 # nosec 尾注释（0/{measured['added']}）"
    )
    brand_class = (
        f"带品牌新增 {measured['added_brand']} 条 / 删 {measured['removed_brand']} 条，"
        f"逐行按加载形态正则分类后命中 {measured['loaded_added']} 条；原文："
        + (" | ".join(brand_lines) if brand_lines else "（无）")
        if int(str(measured["added_brand"])) or int(str(measured["removed_brand"]))
        else "带品牌的增删行为 0/0"
    )
    verdict = (
        "没有任何一处用品牌名去加载外部包，所以这是同一批准内容下的摘要重钉，不是新的批准"
        if not problems
        else f"但本条已记 {len(problems)} 项问题，因此不据此重钉（clean=NO）"
    )
    gap = diff_added - dedup_added
    dedup_clause = (
        f"两口径差 {gap} 条，de-dupe 命中 {dup} 条（新增行的文本在 pin 字节里已出现），"
        f"余 {gap - dup} 条未被该机制解释"
        if gap
        else f"两口径相同（差 0 条，de-dupe 命中 {dup} 条）"
    )
    return (
        f"C85 primary 复核 sha 漂移（被 {mover[:7]} 改动）：pin 字节属 rev {pin_rev[:7]}，"
        f"pin 之后该路径共 {newer} 个 revision；{brand_face}；"
        f"加载形态行 pinned={pinned_faces['load_shape_lines']}=="
        f"live={live_faces['load_shape_lines']}；变动行 diff 口径（difflib autojunk=False）"
        f"增{diff_added} 删{diff_removed}，按文本去重口径 增{dedup_added} 删{measured['removed']}，"
        f"{dedup_clause}；{nosec_clause}；{brand_class}；"
        f"{measured['ast_note']}；{verdict}"
    )


def audit(
    scanner: types.ModuleType, raw: dict[str, object]
) -> tuple[list[dict[str, object]], list[str]]:
    """Audit every drifted entry and return (table rows, real problems)."""
    rows: list[dict[str, object]] = []
    real: list[str] = []
    for path, entry in sorted(entry_map(raw).items()):
        disk = (ROOT / path).read_bytes()
        disk_digest = sha256(disk)
        pinned = str(entry["sha256"])
        if pinned == disk_digest:
            continue
        pin_rev, newer, match_count = locate_pin(path, pinned)
        if pin_rev is None:
            real.append(
                f"{path}: pinned digest {pinned[:12]} matches none of the {len(newer)} "
                "revisions of this path -- the pin never corresponded to a committed state"
            )
            rows.append({"path": path, "clean": "NO PIN REVISION", "old_full": pinned})
            continue
        mover = newer[0] if newer else pin_rev
        old_source = git("show", f"{pin_rev}:{path}").decode("utf-8")
        live_source = disk.decode("utf-8")
        problems, measured = compare(scanner, path, old_source, live_source)
        real.extend(problems)
        if not newer:
            real.append(f"{path}: pin revision is already the newest of the path, digests differ")
        if sha256(git("show", f"HEAD:{path}")) != disk_digest:
            real.append(f"{path}: working tree differs from HEAD; audit HEAD bytes instead")
        rows.append(
            {
                "path": path,
                "old_full": pinned,
                "new_full": disk_digest,
                "pin_rev": pin_rev,
                "mover": mover,
                "newer": len(newer),
                "match_count": match_count,
                "clean": "yes" if not problems else "NO",
                "reason": reason(measured, pin_rev, mover, len(newer), problems),
            }
        )
    return rows, real


def print_table(rows: list[dict[str, object]]) -> None:
    """Print the audit table the evidence file is made of."""
    print(f"drifted entries audited: {len(rows)}")
    for row in rows:
        print("=" * 78)
        print("path:", row["path"])
        print("old sha256:", row["old_full"])
        print("new sha256:", row.get("new_full", "unpinnable"))
        print(
            "pin revision:",
            row.get("pin_rev", "none"),
            "| moved by:",
            row.get("mover", "none"),
            "| newer revisions:",
            row.get("newer", ""),
            "| revisions matching the pin:",
            row.get("match_count", ""),
        )
        print("registered hits unchanged:", row["clean"])
        print("reason:", row.get("reason", ""))


def write_repin(raw: dict[str, object], rows: list[dict[str, object]]) -> int:
    """Re-pin the clean entries: current digest, review date, evidence in the reviewer field."""
    entries = entry_map(raw)
    replaced = 0
    for row in rows:
        if row["clean"] != "yes":
            continue
        entry = entries[str(row["path"])]
        entry["sha256"] = row["new_full"]
        entry["reviewed_date"] = REVIEW_DATE
        entry["reviewer"] = f"{entry['reviewer']}；{row['reason']}"
        replaced += 1
    (ROOT / POLICY).write_text(dump(raw), encoding="utf-8")
    return replaced


def run_checker() -> tuple[int, list[str]]:
    """Run the real judge on the policy as it sits on disk; return (exit code, output lines)."""
    process = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(VERIFIER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return process.returncode, [f"OUT| {line}" for line in process.stdout.splitlines()] + [
        f"ERR| {line}" for line in process.stderr.splitlines()
    ]


def tamper_policy(scanner: types.ModuleType) -> int:
    """Flip one hex character of a re-pinned sha256 and require the checker to name that path."""
    before = (ROOT / POLICY).read_bytes()
    raw = policy_document(before.decode("utf-8"))
    entries = entry_map(raw)
    re_pinned = sorted(
        path
        for path, entry in entries.items()
        if "C85 primary 复核 sha 漂移" in str(entry["reviewer"])
        and str(entry["sha256"]) == sha256((ROOT / path).read_bytes())
    )
    print(f"entries carrying a C85 re-pin that matches the bytes on disk: {len(re_pinned)}")
    if not re_pinned:
        print("  nothing re-pinned to tamper with -- run --write first")
        return 1
    victim = re_pinned[-1]
    digest = str(entries[victim]["sha256"])
    flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    entries[victim]["sha256"] = flipped
    (ROOT / POLICY).write_text(dump(raw), encoding="utf-8")
    code, lines = run_checker()
    print(f"TAMPER CONTROL -- {victim}: sha256 last hex digit {digest[-1]} -> {flipped[-1]}")
    print(f"  checker exit code: {code}")
    for line in lines:
        print("  " + line)
    (ROOT / POLICY).write_bytes(before)
    restored = sha256((ROOT / POLICY).read_bytes()) == sha256(before)
    print("  policy bytes restored:", restored)
    stale_line = next((line for line in lines if "stale sha256" in line), "")
    reported = [chunk.split(";", 1)[0].strip() for chunk in stale_line.split("stale sha256:")[1:]]
    print("  stale paths reported by the checker:", reported)
    after_code, after_lines = run_checker()
    print(f"  restored policy: checker exit code {after_code}")
    for line in after_lines:
        print("  " + line)
    bites = code != 0 and reported == [victim] and restored and after_code == 0
    print(f"  control bit (the checker names exactly {victim} as stale, and nothing else):", bites)
    return 0 if bites else 1


def tamper_source(scanner: types.ModuleType) -> int:
    """Require the content comparison to see a newly smuggled-in reference."""
    victim = "opendata/core/config.py"
    raw = policy_document(policy_text())
    pinned = str(entry_map(raw)[victim]["sha256"])
    pin_rev = (
        locate_pin(victim, pinned)[0]
        or git("log", "--format=%H", "-1", "--", victim).decode().split()[0]
    )
    source = git("show", f"{pin_rev}:{victim}").decode("utf-8")
    problems, _measured = compare(scanner, victim, source, f"{source}\nimport akshare\n")
    print(
        f"SOURCE TAMPER CONTROL -- pinned bytes of {victim} (rev {pin_rev[:7]}) "
        "+ one 'import akshare':"
    )
    print(f"  problems reported: {len(problems)}")
    for problem in problems:
        print("   -", problem)
    return 0 if problems else 1


def tamper_prose(scanner: types.ModuleType) -> int:
    """Require the brand-face rule to accept a mention that is not a load.

    This is the arm on the other side of :func:`tamper_source`, and it exists because the brand
    faces are no longer a hard gate where the AST arm applies. Without it the loosening is an
    assertion: the same victim, the same pinned bytes, one added *comment* naming the brand, which
    must produce no problem at all -- and the line has to be classified, not trusted.
    """
    victim = "opendata/core/config.py"
    raw = policy_document(policy_text())
    pinned = str(entry_map(raw)[victim]["sha256"])
    pin_rev = (
        locate_pin(victim, pinned)[0]
        or git("log", "--format=%H", "-1", "--", victim).decode().split()[0]
    )
    source = git("show", f"{pin_rev}:{victim}").decode("utf-8")
    comment = "# 来源名 akshare 只出现在读数文本里，没有任何一处用它加载外部包\n"
    problems, measured = compare(scanner, victim, source, f"{source}{comment}")
    print(
        f"PROSE CONTROL -- pinned bytes of {victim} (rev {pin_rev[:7]}) + one comment naming "
        "akshare:"
    )
    print(f"  problems reported: {len(problems)}")
    for problem in problems:
        print("   -", problem)
    print(
        f"  brand faces moved: {measured['moved_brand_faces']} | added brand lines: "
        f"{measured['added_brand']} | of which load shapes: {measured['loaded_added']} | "
        f"AST arm applies: {measured['ast_applies']}"
    )
    return 0 if not problems else 1


def main(argv: list[str]) -> int:
    """Audit, print the table, and optionally re-pin or run the two-sided controls."""
    scanner = load_scanner()
    raw = policy_document(policy_text())
    print("policy entries registered:", len(entry_map(raw)))
    if "--tamper-policy" in argv:
        return tamper_policy(scanner)
    if "--tamper-source" in argv:
        return tamper_source(scanner)
    if "--tamper-prose" in argv:
        return tamper_prose(scanner)
    rows, real = audit(scanner, raw)
    print_table(rows)
    print("=" * 78)
    print(f"REAL problems: {len(real)}")
    for problem in real:
        print("  -", problem)
    if "--write" in argv:
        print("entries re-pinned:", write_repin(raw, rows))
        code, lines = run_checker()
        print("checker exit code:", code)
        for line in lines:
            print("  " + line)
    return 0 if not real else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
