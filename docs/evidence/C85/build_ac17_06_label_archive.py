"""Build the C85 archive for the AC-17|06 counterfact label rename and the pin that followed.

Every figure in the archive is read out of a captured body or out of git, so nothing in the
published text is typed from memory. One body is one-shot -- the write that moved the pin, which
consumed the state it reported -- and is transcribed from its tracked carrier; every other arm is
re-run here, in this generation, against the revision the header names.

Run from the repository root::

    python docs/evidence/C85/build_ac17_06_label_archive.py

It refuses to write unless every control it re-measures reads the way the gate needs it to, and the
refusal prints the faces it saw instead of producing a softer archive.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
OUT = Path("docs/evidence/C85/ac17-06-counterfact-label-arms.txt")
POLICY = Path("docs/quality/akshare-reference-allowlist.json")
PROBE = Path("scripts/quality/acceptance_item_probe.py")
FACES = Path("docs/quality/acceptance-probe-faces.json")
REPIN = Path("docs/evidence/C85/reference-policy-repin.py")
ARMS = Path("docs/evidence/C85/ac17-06-counterfact-arms.py")
STAMP = Path("docs/evidence/C85/coverage-final-stamp.json")
ITEM = "AC-17|06"
MARKER = "C85 primary 复核 sha 漂移"
CAPTURED_WRITE = Path("docs/evidence/C85/ac17-06-repin-write-capture.txt")
FACE_JSON = Path("docs/evidence/C85/ac17-06-item-face.json")

#: The two revisions this round names: the last commit that moved the probe module before the label
#: rename, and the rename itself. They are the inputs to the git commands below, and each is checked
#: against a face the re-pin instrument measured without being told them.
PREVIOUS = "af4c90f"
MOVED_BY = "0ee5839"


def run(argv: list[str]) -> tuple[int, str]:
    """Run one command with argv, capture both streams, and return the code with the body."""
    process = subprocess.run(  # nosec B603  # noqa: S603 - no shell, argv is literal below
        [str(arg) for arg in argv],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    body = process.stdout.decode("utf-8", "replace") + process.stderr.decode("utf-8", "replace")
    return process.returncode, body


def one(pattern: str, text: str, group: int = 0, occ: int = 0) -> str:
    """Read one match out of a body, and fail loudly if the body does not carry it."""
    matches = re.findall(pattern, text)
    if len(matches) <= occ:
        raise SystemExit(f"FAIL: pattern {pattern!r} found {len(matches)} match(es), wanted {occ}")
    found = matches[occ]
    if isinstance(found, tuple):
        return str(found[group])
    return str(found)


def digest(path: Path) -> str:
    """Return the sha256 of the bytes on disk, resolved against the repository root."""
    return hashlib.sha256((REPO / path).read_bytes()).hexdigest()


def size_of(path: Path) -> int:
    """Return the byte count of one repository-relative path."""
    return len((REPO / path).read_bytes())


def git_text(*args: str) -> str:
    """Run git and return stdout as text."""
    code, body = run([shutil.which("git") or "git", *args])
    if code:
        raise SystemExit(f"FAIL: git {' '.join(args)} exited {code}: {body}")
    return body


def policy_entries() -> list[dict[str, Any]]:
    """Return every entry the reference policy registers."""
    raw: dict[str, Any] = json.loads((REPO / POLICY).read_text(encoding="utf-8"))
    return [dict(entry) for entry in raw["entries"]]


def policy_entry(path: str) -> dict[str, Any]:
    """Return the registered policy entry for one path."""
    hits = [e for e in policy_entries() if str(e["path"]) == path]
    if len(hits) != 1:
        raise SystemExit(f"FAIL: {path} registered {len(hits)} time(s) in the policy")
    entry: dict[str, Any] = hits[0]
    return entry


def region(label: str, body: str) -> str:
    """Wrap one captured body in markers and state its own line, byte and digest claims."""
    data = body.encode("utf-8")
    claims = f"{label}: {len(body.splitlines())} line(s), {len(data)} bytes, sha256 "
    return f"<<<BEGIN {label}\n{body}<<<END {label}\n{claims}{_sha(data)}\n"


def _sha(data: bytes) -> str:
    """Return the hex digest of bytes, for the claim line that follows a region."""
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    """Re-measure the controls, embed the one-shot write, and publish the archive."""
    branch = git_text("rev-parse", "--abbrev-ref", "HEAD").strip()
    head = git_text("rev-parse", "HEAD").strip()
    numstat = git_text("diff", "--numstat", PREVIOUS, MOVED_BY, "--", str(PROBE)).split()
    git_added, git_removed = numstat[0], numstat[1]
    drift_revs = int(git_text("rev-list", "--count", f"{PREVIOUS}..{MOVED_BY}", "--", str(PROBE)))
    at_previous = hashlib.sha256(
        git_text("show", f"{PREVIOUS}:{PROBE}").encode("utf-8")
    ).hexdigest()

    staged = (REPO / CAPTURED_WRITE).read_bytes()
    staged_size, staged_digest = len(staged), _sha(staged)
    write_body = staged.decode("utf-8")
    old_digest = one(r"old sha256: (\w{64})", write_body)
    new_digest = one(r"new sha256: (\w{64})", write_body)
    pin_rev = one(r"pin revision: (\w+)", write_body)
    moved_by = one(r"\| moved by: (\w+)", write_body)
    newer = one(r"newer revisions: (\d+)", write_body)
    cal_added = one(r"diff 口径（difflib autojunk=False[^）]*）增(\d+) 删\d+", write_body)
    cal_removed = one(r"diff 口径（difflib autojunk=False[^）]*）增\d+ 删(\d+)", write_body)
    real_before = one(r"REAL problems: (\d+)", write_body)
    re_pinned = one(r"entries re-pinned: (\d+)", write_body)
    checker_after_write = one(r"checker exit code: (\d+)", write_body)
    walked = one(r"OUT\| OK: .*", write_body)
    hits_unchanged = one(r"registered hits unchanged: (\w+)", write_body)
    clause_written = one(r"reason: (.+)", write_body)
    brand = one(r"(akshare 出现行 \d+->\d+、出现次数 \d+->\d+（[^）]*）)", write_body)
    brand2 = one(r"(openbb 出现行 \d+->\d+、出现次数 \d+->\d+（[^）]*）)", write_body)
    load_face = one(r"加载形态行 pinned=\d+==live=\d+", write_body)
    load_pin = one(r"加载形态行 pinned=(\d+)==live=\d+", write_body)
    load_live = one(r"加载形态行 pinned=\d+==live=(\d+)", write_body)
    multiset_pin = one(r"命中多重集 pinned=(\d+) live=\d+", write_body)
    multiset_live = one(r"命中多重集 pinned=\d+ live=(\d+)", write_body)
    identical = one(r"identical=(True|False)", write_body)

    entry = policy_entry(str(PROBE))
    pinned = str(entry["sha256"])
    reviewed = str(entry["reviewed_date"])
    reviewer = str(entry["reviewer"])
    clause_on_disk = reviewer[reviewer.rindex(MARKER) :] if MARKER in reviewer else ""
    disk_digest = digest(PROBE)

    exits: dict[str, int] = {}

    def measure(name: str, argv: list[str]) -> str:
        """Run one arm, record its exit, and end its body with that exit as a face."""
        exit_code, body = run([sys.executable, *argv])
        exits[name] = exit_code
        return body + f"{name.upper()}_EXIT={exit_code}\n"

    audit_body = measure("audit", [str(REPIN)])
    drifted_after = one(r"drifted entries audited: (\d+)", audit_body)
    real_after = one(r"REAL problems: (\d+)", audit_body)

    source_body = measure("tamper_source", [str(REPIN), "--tamper-source"])
    source_problems = one(r"problems reported: (\d+)", source_body)

    prose_body = measure("tamper_prose", [str(REPIN), "--tamper-prose"])
    prose_problems = one(r"problems reported: (\d+)", prose_body)
    prose_added = one(r"added brand lines: (\d+)", prose_body)
    prose_loaded = one(r"of which load shapes: (\d+)", prose_body)

    policy_body = measure("tamper_policy", [str(REPIN), "--tamper-policy"])
    control_bit = one(r"control bit .*: (True|False)", policy_body)
    restored = one(r"policy bytes restored: (True|False)", policy_body)
    tamper_victim = one(r"TAMPER CONTROL -- (\S+):", policy_body)
    tamper_candidates = one(r"matches the bytes on disk: (\d+)", policy_body)

    face_before = digest(FACES)
    sync_body = measure("sync_faces", [str(PROBE), "--sync-faces"])
    face_after = digest(FACES)
    faces_dirty = git_text("status", "--porcelain", "--", str(FACES)).strip()

    item_body = measure("item", [str(PROBE), "--item", ITEM, "--json", str(FACE_JSON)])
    verdict = one(rf"VERDICT {re.escape(ITEM)}: (\w+)", item_body)

    arms_body = measure("arms", [str(ARMS), str(FACE_JSON)])
    live_probes = one(r"live table: probes=(\d+)", arms_body)
    live_arms = one(r"live table: probes=\d+ arms=(\d+)", arms_body)
    old_probes = one(r"table at \w+ \(imported copy\): probes=(\d+)", arms_body)
    old_arms = one(r"table at \w+ \(imported copy\): probes=\d+ arms=(\d+)", arms_body)
    arms_ref = one(r"table at (\w+) \(imported copy\)", arms_body)
    word_counts = re.findall(r"labels containing '实测': (\d+)", arms_body)
    digit_counts = re.findall(r"labels containing '实测' AND a digit: (\d+)", arms_body)
    gone = one(r"label no arm carries now: (.+)", arms_body)
    same_size = one(r"same table size across the rename: (True|False)", arms_body)
    applied = one(r"arms applied = (\d+)", arms_body)
    biting = one(r"biting = (\d+)", arms_body)
    all_bite = one(r"ALL_ARMS_BITE=(\w+)", arms_body)
    reach = one(r"REPAIR_REACHES_PROVEN=(\w+)", arms_body)
    leftovers = one(r"byte copies left in scripts/quality: (\d+)", arms_body)
    literal = one(r"(\d+\.\d+)", gone)
    live_root = one(r"live root_pct on this pass = (.+)", arms_body)
    components = [chunk.split("=", 1) for chunk in live_root.split(", ") if "=" in chunk]
    live_pipeline = one(r"pipeline=([\d.]+)", live_root)
    roots_clause = one(r"（([^（）]*?) ≥\d+%）", item_body)
    required_roots = [name.strip("`") for name in roots_clause.split("、")]
    floor = one(r" ≥(\d+)%）", item_body)
    forged = re.findall(r"forges \[root_pct=[^\]]*\]", arms_body)
    forged_literal = [line for line in forged if literal in line]
    stamp_commit = str(json.loads((REPO / STAMP).read_text(encoding="utf-8"))["commit"])
    payload: dict[str, Any] = json.loads((REPO / FACE_JSON).read_text(encoding="utf-8"))
    face_json_items = [str(record["item"]) for record in payload["items"]]

    # Recomputed from disk, so the instrument's victim count is not taken on trust.
    repinned_here = sorted(
        str(entry["path"])
        for entry in policy_entries()
        if MARKER in str(entry["reviewer"])
        and str(entry["sha256"]) == digest(Path(str(entry["path"])))
    )

    checks = {
        "drifted_after==0": drifted_after == "0",
        "real_after==0": real_after == "0",
        "real_before==0": real_before == "0",
        "tamper_source_bites": int(source_problems) > 0,
        "tamper_prose_silent": prose_problems == "0" and prose_loaded == "0",
        "tamper_prose_added_one_line": prose_added == "1",
        "tamper_policy_control": control_bit == "True" and restored == "True",
        "tamper_victim_is_reproducible": bool(repinned_here)
        and (
            tamper_victim == repinned_here[-1]
            and len(repinned_here) == int(tamper_candidates)
            and str(PROBE) in repinned_here
        ),
        "checker_zero_after_write": checker_after_write == "0",
        "exactly_one_entry_repinned": re_pinned == "1",
        "policy_pin==write_new_digest": pinned == new_digest,
        "disk_bytes==write_new_digest": disk_digest == new_digest,
        "replaced_pin_was_pre_rename_bytes": old_digest == at_previous,
        "write_names_the_same_revs_as_git": (
            pin_rev.startswith(PREVIOUS) and moved_by.startswith(MOVED_BY)
        ),
        "clause_on_disk==clause_written": clause_on_disk == clause_written,
        "item_verdict_proven": verdict == "proven",
        "face_json_carries_only_this_item": face_json_items == [ITEM],
        "every_arm_bites": all_bite == "yes" and applied == biting,
        "repair_reaches_proven": reach == "yes",
        "no_byte_copy_left": int(leftovers) == 0,
        "faces_book_unchanged": face_before == face_after and faces_dirty == "",
        "instrument_diff==git_numstat": (cal_added, cal_removed) == (git_added, git_removed),
        "audit_revisions==git_rev_list": drift_revs == int(newer),
        "content_faces_unchanged": (
            hits_unchanged == "yes"
            and identical == "True"
            and multiset_pin == multiset_live
            and load_pin == load_live
        ),
        "census_printed_twice": len(digit_counts) == 2 and len(word_counts) == 2,
        "census_can_differ": digit_counts[0] != digit_counts[1],
        "live_digit_measured_labels_zero": digit_counts[0] == "0",
        "table_size_unchanged": same_size == "True"
        and (live_probes, live_arms)
        == (
            old_probes,
            old_arms,
        ),
        "census_population_is_the_named_revision": arms_ref == PREVIOUS,
        "literal_is_a_forged_value_not_a_reading": (
            len(forged_literal) == 1 and one(r"pipeline=([\d.]+)", forged_literal[0]) == literal
        ),
        "forged_literal_is_below_the_floor": float(literal) < float(floor),
        "live_face_clears_the_floor": float(live_pipeline) >= float(floor),
        "composite_carries_every_root_the_criterion_names": [name for name, _value in components]
        == required_roots
        and live_root in item_body,
        "two_arms_forge_the_composite_face": len(forged) == 2,
        "re_runs_all_exited_zero": all(exit_code == 0 for exit_code in exits.values()),
        "embedded_capture_has_a_tracked_carrier": bool(
            git_text("ls-files", "--", str(CAPTURED_WRITE)).strip()
        ),
    }
    broken = [name for name, held in checks.items() if not held]
    if broken:
        print(f"REFUSING to write {OUT}: {broken}")
        print(
            f"faces read: drifted_after={drifted_after} real_after={real_after} "
            f"real_before={real_before} source_problems={source_problems} "
            f"prose_problems={prose_problems} prose_added={prose_added} "
            f"prose_loaded={prose_loaded} "
            f"control_bit={control_bit} restored={restored} checker={checker_after_write} "
            f"re_pinned={re_pinned} pinned={pinned[:12]}… new={new_digest[:12]}… "
            f"old={old_digest[:12]}… at_{PREVIOUS}={at_previous[:12]}… "
            f"verdict={verdict} biting={biting}/{applied} leftovers={leftovers} "
            f"faces_stable={face_before == face_after} faces_dirty={faces_dirty!r} "
            f"cal={(cal_added, cal_removed)} git={(git_added, git_removed)} "
            f"revs={drift_revs}/{newer} digit_counts={digit_counts} "
            f"arms_ref={arms_ref} live_root={live_root} literal={literal} "
            f"live_pipeline={live_pipeline} floor={floor} roots={required_roots} "
            f"forged={forged} forged_literal={forged_literal} "
            f"victim={tamper_victim} candidates={tamper_candidates}/{len(repinned_here)} "
            f"face_json_items={face_json_items} exits={exits} "
            f"clause_equal={clause_on_disk == clause_written}"
        )
        return 1

    parts: list[str] = []
    parts.append(
        f"""C85 re-pin of the acceptance probe module after one counterfact label was renamed, with
its controls.  branch={branch}  HEAD={head}

Each body between the BEGIN/END markers is verbatim captured stdout+stderr of the command printed
above it. The body marked "embedded capture" ran once, in the state that write consumed, and is
transcribed from the tracked carrier {CAPTURED_WRITE}, which holds {staged_size} B at sha256
{staged_digest}, so the transcription can be diffed against its carrier as well as read here. That
write is one-shot: its body comes back only by rolling the pin forward again, which is a command in
this file, not a sentence about it. Everything else was re-run by
docs/evidence/C85/build_ac17_06_label_archive.py at the HEAD named above, and no figure here was
typed -- each one is read out of a body below or out of git. The two revision names this file hands
to git are checked against the write body's own "pin revision"/"moved by" faces before anything is
written, so a stale constant here cannot silently retitle the population.

Digest convention: a region holds the captured bytes exactly, including its final newline, so
sha256(region) equals sha256(captured file). It attests that this archive was not edited after the
digests were computed; it does not attest that the commands ran -- those are re-runnable against the
named revision, which is the stronger claim and is why the argv is printed above every body.

Files this run read or wrote, with the bytes as they stood at capture time:
  {PROBE}  {size_of(PROBE)} B  sha256 {disk_digest}
  {POLICY}  {size_of(POLICY)} B  sha256 {digest(POLICY)}
  {ARMS}  {size_of(ARMS)} B  sha256 {digest(ARMS)}
  {REPIN}  {size_of(REPIN)} B  sha256 {digest(REPIN)}
  {FACES}  {size_of(FACES)} B  sha256 {face_after}
  {FACE_JSON}  {size_of(FACE_JSON)} B  sha256 {digest(FACE_JSON)}  (written by the item arm, read
      by the arms arm; it holds the measured faces both of them judge against)

Why this run existed

  An arm of {ITEM} carried the label

      「{gone}」

  The figure inside it is the value the judge has to reject, not a report reading, and reading
  「实测」 literally sends a reviewer to close a gap that does not exist -- which is what happened
  this round. Two measurements say which of the two readings is live. The forged side:
  {len(forged_literal)} of the item's {len(forged)} root_pct arms forge {literal} as the pipeline
  component of the {", ".join(required_roots)} composite, which is under the {floor}% floor the
  criterion names:

      {forged_literal[0]}

  The live side, recomputed by the same pass out of the archived report:

      root_pct={live_root}

  whose pipeline component reads {live_pipeline}, at or over that floor. The census below measures
  the shape (the word 实测 next to a digit) over the whole arm table at this tree and at
  {arms_ref[:7]}, so the published zero comes with an arm that returns one.

  Renaming a label moves the bytes of a path the reference policy registers ({POLICY}), so the pin
  had to move.
  The drift is {cal_added} added / {cal_removed} removed lines by the instrument, and git reports
  the same pair as {git_added}/{git_removed} for {pin_rev[:7]}..{moved_by[:7]} -- {drift_revs}
  revision(s) of the path, which equals the audit's "newer revisions: {newer}". No content face
  moved: {brand}; {brand2};
  {load_face}; scanner (module, kind) multiset pinned={multiset_pin} live={multiset_live},
  identical={identical}; registered hits unchanged: {hits_unchanged}. So this is a digest re-pin of
  the same approved content, not a new approval.

The clause the write put into the policy, and the face that it is still the one on disk

  pinned sha256 = {pinned}; sha256 of the bytes on disk = {disk_digest}; reviewed_date = {reviewed}.
  The pin this write replaced read {old_digest}, which is the sha256 of git's bytes at
  {PREVIOUS} -- so the replaced value was the pre-rename module, not an unrelated drift.
  The clause below is read back off the policy file (the text after the last 「{MARKER}」 in that
  entry's reviewer chain), and it is byte-equal to the reason line the write arm printed.

"""
    )
    parts.append(region("appended-clause", clause_written + "\n"))
    parts.append("\nCaptured arms\n\n")

    arms_list: list[tuple[str, str, str, str]] = [
        ("write", f"python {REPIN} --write", write_body, "embedded capture"),
        ("audit", f"python {REPIN}", audit_body, "re-run here"),
        ("tamper-source", f"python {REPIN} --tamper-source", source_body, "re-run here"),
        ("tamper-prose", f"python {REPIN} --tamper-prose", prose_body, "re-run here"),
        ("tamper-policy", f"python {REPIN} --tamper-policy", policy_body, "re-run here"),
        ("sync-faces", f"python {PROBE} --sync-faces", sync_body, "re-run here"),
        ("item", f"python {PROBE} --item {ITEM} --json {FACE_JSON}", item_body, "re-run here"),
        ("arms", f"python {ARMS} {FACE_JSON}", arms_body, "re-run here"),
    ]
    for label, argv, body, kind in arms_list:
        parts.append(f"{label} -- {kind}\n  $ {argv}\n\n")
        parts.append(region(label, body))
        parts.append("\n")

    parts.append(
        f"""What the arms establish

  1. The pin is satisfiable and the checker passes on it: the write arm read REAL problems=
     {real_before}, entries re-pinned={re_pinned}, checker exit {checker_after_write}; the audit
     that followed reads drifted entries={drifted_after}, REAL problems={real_after}. The walker's
     own line, which is what the gate needs: {walked}
  2. The brand gate bites where it must and stays quiet where it must: a smuggled load produces
     {source_problems} problem(s), while a comment naming the brand produces {prose_problems} and is
     classified rather than trusted ({prose_added} added brand line(s), {prose_loaded} of them load
     shapes).
  3. The digest is a measurement, not a claim: flipping one hex digit of {tamper_victim} -- the last
     of the entries whose pin already equals the bytes on disk, counted {tamper_candidates} by the
     instrument and {len(repinned_here)} when this file recomputes that set -- makes the checker
     name exactly that path (control bit {control_bit}) and the bytes come back (restored
     {restored}). {PROBE} is inside that same set, so the pin this archive is about is one of the
     pins the flip test can reach, and the walker's exit code 0 above is the checker accepting it.
  4. The label census can differ: {live_probes} probes / {live_arms} arms here and {old_probes}
     probes / {old_arms} arms at {arms_ref[:7]} -- the same population -- while the
     digit-bearing-实测 count is {digit_counts[0]} now and {digit_counts[1]} then. The
     {word_counts[0]} labels that still contain 实测 carry no figure ("该包实测不一致",
     "没有一次实测取值"), which is what the digit predicate separates them on.
  5. Nothing about the judgement changed: {ITEM} still reads {verdict}, {biting} of its {applied}
     arms flip a clean reading back to a gap, its declared repair still reaches proven ({reach}),
     and the copy the census imported from {arms_ref[:7]} left {leftovers} byte copies in
     scripts/quality.
  6. The faces book did not move: {FACES} hashed {face_before[:12]}… before --sync-faces and
     {face_after[:12]}… after, and git reports it {"clean" if not faces_dirty else "dirty"}.
  7. The figure the label was named for is a forged input, not a reading: {len(forged_literal)} of
     the {len(forged)} arms that forge root_pct forge this value ({forged_literal[0]}), under the
     {floor}% floor, while the composite the same pass recomputes reads root_pct={live_root} --
     pipeline={live_pipeline}, and that composite is the one the item arm above prints.
  Every arm above exited 0: {exits}.

What this archive does not claim

  It does not claim the renamed label was the only misleading one -- only that no arm label now
  pairs 实测 with a digit, measured over {live_arms} arms. It does not restate the coverage faces:
  those live in {STAMP} at commit {stamp_commit} and are read by the probe itself. And it does not
  claim the pin is permanent -- any later commit that touches {PROBE} moves this digest again, which
  is why the audit appears here as a command rather than as a sentence.

Recompute recipe

  python3 - <<'PY'
import hashlib, pathlib, re
text = pathlib.Path("{OUT}").read_text("utf-8")
pat = (r"<<<BEGIN (\\S+)\\n(.*?)<<<END \\1\\n\\1: (\\d+) line\\(s\\), (\\d+) bytes, "
       r"sha256 (\\w{{64}})")
rows = re.findall(pat, text, re.S)
print("regions:", len(rows))
print("every region matches its own claim:", all(
    len(body.splitlines()) == int(lines)
    and len(body.encode("utf-8")) == int(nbytes)
    and hashlib.sha256(body.encode("utf-8")).hexdigest() == sha
    for _label, body, lines, nbytes, sha in rows
))
PY
"""
    )
    (REPO / OUT).write_text("".join(parts), encoding="utf-8")
    print(f"wrote {OUT} ({size_of(OUT)} bytes)")
    print(
        f"summary faces: verdict={verdict} arms_biting={biting}/{applied} "
        f"digit_census_live={digit_counts[0]} old={digit_counts[1]} re_pinned={re_pinned} "
        f"checker={checker_after_write} drifted_after={drifted_after} "
        f"checks_held={len(checks)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
