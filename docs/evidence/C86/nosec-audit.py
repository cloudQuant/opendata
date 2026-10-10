#!/usr/bin/env python3
r"""Do this round's instruments' ``# nosec`` exemptions actually suppress something?

An exemption is only honest while the scanner fires where it sits. Two ways it rots: an id that
names nothing (decoration, and it hides the day the real violation goes away), and a violation that
no exemption covers -- that one cannot hide, it fails the gate. So both directions are measured here
against bandit's own answer, using the repo's own exemption pattern: ``NOSEC_PAT`` is imported from
the shipped probe rather than retyped, because retyping it is where this instrument first went
wrong. ``B[\d.\s,]+`` stops at the ``B`` of the *second* id, so ``# nosec B603 B607`` parsed as one
declared id and bandit's ``B607`` read as ``undeemed`` -- a false finding about my own tool.

Three frames, and the gate uses the middle one:

* pooled over the population: the per-id presence census plus bandit's own ``skipped_tests``
  arithmetic;
* per file, against that file's own ``skipped_tests``: ``sites - skipped`` is the scanner saying how
  many consumptions this file's exemptions bought. One over-declared site is invisible to the pooled
  presence census -- the same id fires in a different file, so widening the population from 5 typed
  files to every instrument in the directory printed ``decorative=[]`` beside ``surplus=1``, and the
  file that carried it was found only by hand;
* per reported line, kept printed and NOT in the gate. Bandit suppresses a finding anywhere inside
  the statement an exemption sits on, so a ``# nosec`` on the line a multi-line call opens is live
  even though the reported ``line_number`` names a line further down. Keyed on lines this census
  called 14 sites dead; the scanner had consumed every one of them. ``SHAPE_CONTROL`` is in the
  population as the witness: comments at 24/51/79/130 against findings at 24/56/84/135, with
  ``surviving=[]`` and ``skipped_tests=4``. Its line census reads 3 dead while its surplus reads 0;
  the day those two agree, the frame swap is unproven, and that is gated as its own face.

Faces and three controls, all printed:

* the firing multiset with ``--ignore-nosec``, the declared multiset, and what survives nosec, tied
  to bandit's arithmetic (``fired == skipped + surviving``, per file and for the pool, and the parts
  summing to the whole);
* per-file table of ``sites / skipped / surplus / surviving_rows / dead_ids``;
* the census shape itself: comment-token sites next to raw-line sites, since this file's own prose
  and control literals are exactly what a grep-shaped census would over-count;
* an arm that adds an id the file never fires -- that file's surplus must go 0 -> 1 and name it;
* an arm that deletes one id from a two-id exemption -- that finding must come back, the exit code
  must go red, which is the proof that the exemption is load-bearing rather than decorative;
* an arm that puts this round's real over-declaration back -- the pooled presence census must stay
  silent on it while the per-file frame names it, so the silence is on the record as blindness.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import pathlib
import platform
import re
import shutil
import subprocess  # nosec B404  # literal argv: this interpreter runs bandit, never a shell string
import sys
from typing import Any, Final, NamedTuple

REPO = pathlib.Path(__file__).resolve().parents[3]
DIR: Final = pathlib.Path("docs/evidence/C86")
SELF: Final = "docs/evidence/C86/nosec-audit.py"
ARCHIVE: Final = "docs/evidence/C86/nosec-audit.txt"
TITLE: Final = (
    "# C86 —— 本轮仪器的 nosec 例外是否真压住违例：逐文件对 bandit 自己的 skipped_tests 记账"
    "（三条反例臂）"
)
CONFIG: Final = "bandit.yaml"
SCRATCH: Final = "docs/evidence/C86/.scratch_nosec"
DIGEST_LEN: Final = 16
RULE_ID: Final = re.compile(r"B\d+")
TWO_ID_PROBE: Final = "docs/evidence/C86/gap-reason-staleness.py"
PROSE_FREE: Final = "docs/evidence/C86/staleness-carrier.py"
# The carrier this round found over-declared, then repaired: an exemption naming ``B603`` on a call
# whose argv arrives as a tuple, so no partial path exists for ``B607`` to name. Never write the
# literal word followed by an id inside a comment here -- bandit reads comments, so prose that names
# an exemption becomes one, decorative, and this audit would then be pointing at itself.
BLIND_VICTIM: Final = "docs/evidence/C86/probe-seven-carrier.py"
BLIND_SEED: Final = "# nosec B603  # argv is a tuple, no literal path"
BLIND_FORGE: Final = "# nosec B603 B607  # argv is a tuple, no literal path"
# An older archive whose exemptions sit on the line the call *opens*, while bandit reports the
# finding further down the same statement. It is the witness that a line-keyed census over-reports.
SHAPE_CONTROL: Final = "docs/evidence/C29/real-tree-probe.py"


def population() -> list[str]:
    """Every instrument in this round's evidence directory, plus the shape control.

    A typed list is a face that decays: the day a sixth instrument lands with its own exemptions,
    the audit still prints PASS over a population that no longer includes it. ``glob`` cannot fail
    that way, and the named control files are asserted to be inside the enumeration, so an emptied
    or narrowed glob fails loudly instead of quietly auditing less.
    """
    names = sorted(str(DIR / path.name) for path in (REPO / DIR).glob("*.py"))
    for must in (SELF, TWO_ID_PROBE, PROSE_FREE, BLIND_VICTIM):
        if must not in names:
            raise SystemExit(f"NOSEC_AUDIT_CHECK FAIL: enumerated population lacks {must}")
    if not (REPO / SHAPE_CONTROL).is_file():
        raise SystemExit(f"NOSEC_AUDIT_CHECK FAIL: shape control {SHAPE_CONTROL} is gone")
    return [*names, SHAPE_CONTROL]


# The shape this file first shipped with: a character class that cannot cross the second id's ``B``.
RETIRED_SHAPE: Final = re.compile(r"#\s*.*?nosec(?:\s+(B[\d.\s,]+))?")
OUT: list[str] = []


def emit(line: str = "") -> None:
    """Print a face line and keep it for the archive, so the two can never diverge."""
    OUT.append(line)
    print(line)


def blind_id() -> str:
    """The id the forged comment adds, solved from the two literals rather than typed here."""
    added: set[str] = set(RULE_ID.findall(BLIND_FORGE)) - set(RULE_ID.findall(BLIND_SEED))
    if len(added) != 1:
        raise SystemExit(f"NOSEC_AUDIT_CHECK FAIL: forged comment adds {sorted(added)}")
    return added.pop()


BLIND_ID: Final = blind_id()


class Scan(NamedTuple):
    """One bandit run: exit code, per-id totals, its own ``skipped_tests``, and fired triples."""

    rc: int
    fired: collections.Counter[str]
    skipped: int
    stderr: str
    rows: list[tuple[str, int, str]]


def norm(path: str) -> str:
    """Bandit reports paths relative to cwd with a leading ``./``; the site keys do not have one."""
    return path[2:] if path.startswith("./") else path


def bandit(extra: list[str], paths: list[str]) -> Scan:
    """Run bandit the way the gate's member 8 does, and read its ids off stdout JSON only.

    ``skipped`` is bandit's own ``metrics._totals.skipped_tests`` -- the number of findings its
    exemptions actually consumed. That scanner-side term is what witnesses a decorative exemption:
    bandit 1.9.4 prints nothing to stderr for one, so an arm gated on a warning string measures
    nothing. ``rows`` carry the line number because the line is what the exemption is keyed on.
    """
    sys.path.insert(0, str(REPO))
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv: -m bandit with fixed config
        [sys.executable, "-m", "bandit", "-c", CONFIG, "-f", "json", "-q", *extra, *paths],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict[str, Any] = json.loads(done.stdout or "{}")
    results = payload.get("results")
    if not isinstance(results, list):
        raise SystemExit(f"bandit stdout carries no results list; keys={sorted(payload)}")
    fired: collections.Counter[str] = collections.Counter()
    rows: list[tuple[str, int, str]] = []
    for item in results:
        tid = str(item.get("test_id"))
        fired[tid] += 1
        rel = norm(str(item.get("filename") or ""))
        rows.append((rel, int(item.get("line_number") or -1), tid))
    metrics = payload.get("metrics")
    totals = metrics.get("_totals", {}) if isinstance(metrics, dict) else {}
    skipped = int(totals.get("skipped_tests", -1)) if isinstance(totals, dict) else -1
    return Scan(done.returncode, fired, skipped, done.stderr.strip(), rows)


def site_map(rel: str, text: str) -> dict[tuple[str, int], list[str]]:
    """The exemptions a scanner can read, keyed by the file and line bandit keys them on."""
    return {(rel, line): ids for line, ids in nosec_sites(text)}


def fired_lines(rows: list[tuple[str, int, str]]) -> dict[tuple[str, int], set[str]]:
    """Group the scanner's findings by the line they landed on."""
    out: dict[tuple[str, int], set[str]] = collections.defaultdict(set)
    for rel, line, tid in rows:
        out[(rel, line)].add(tid)
    return dict(out)


def dead_sites(
    sites: dict[tuple[str, int], list[str]], fired: dict[tuple[str, int], set[str]]
) -> list[str]:
    """Line-keyed guess at dead exemptions -- kept as a bias face, because it is not the scanner.

    Bandit reports a finding *inside* the statement it flags, so a ``# nosec`` that opens a
    multi-line call sits on a line the reported finding never names. Keyed on lines alone this
    census called 14 older-archive sites dead; the scanner had consumed every one of them
    (``surviving=[]``, ``skipped_tests=4``). The gate therefore uses :func:`per_file`, and this
    stays printed so the disagreement is on the record rather than re-invented next round.
    """
    out: list[str] = []
    for key in sorted(sites):
        dead = sorted(tid for tid in sites[key] if tid not in fired.get(key, set()))
        if dead:
            rel, line = key
            out.append(
                f"{rel}:{line} dead={','.join(dead)} declared={','.join(sorted(sites[key]))}"
            )
    return out


def per_file(rel: str, text: str) -> dict[str, Any]:
    """Ask the scanner about one file: what it fires, what its exemptions consume, what survives.

    One file at a time is the frame that matches bandit's own arithmetic. The two other frames are
    wrong in opposite directions. Pooled over a population, an over-declared id hides behind the
    same id firing in a different file, so ``decorative=[]`` printed next to ``surplus=1``. Keyed
    on the reported finding line, a live exemption reads dead (see :func:`dead_sites`). Nothing
    here needs the line: ``sites - skipped`` is the scanner saying how many of its own
    consumptions this file's exemptions bought.
    """
    scan = bandit(["--ignore-nosec"], [rel])
    again = bandit([], [rel])
    dec = declared(text)
    dead = dec - scan.fired
    extra = scan.fired - dec
    return {
        "sites": sum(dec.values()),
        "skipped": again.skipped,
        "surplus": sum(dec.values()) - again.skipped,
        "fired": dict(sorted(scan.fired.items())),
        "surviving": dict(sorted(again.fired.items())),
        "surviving_rows": len(again.rows),
        "dead_ids": dict(sorted(dead.items())),
        "undeemed_ids": dict(sorted(extra.items())),
        "additive": sum(scan.fired.values()) == again.skipped + sum(again.fired.values()),
        "rc": again.rc,
        "line_dead": dead_sites(site_map(rel, text), fired_lines(scan.rows)),
    }


def nosec_sites(text: str) -> list[tuple[int, list[str]]]:
    """Return (line, ids) for every exemption bandit would actually read -- comment tokens only.

    A raw-line census here would be the AC-17|02 defect re-imported: this file writes
    ``"# nosec B608"`` inside a string literal to forge its own control, and names the pattern in
    prose, so a grep-shaped scan counts exemptions the scanner never saw. ``_comment_lines`` is the
    probe's own mirror of ``bandit/core/manager.py``, and ``line_shaped`` below keeps the retired
    shape on the record next to it.
    """
    from scripts.quality.acceptance_item_probe import NOSEC_PAT, _comment_lines

    lines = text.splitlines()
    clines, readable = _comment_lines(text)
    if not readable:
        raise SystemExit("untokenizable file: bandit honors no exemption in one of these")
    out: list[tuple[int, list[str]]] = []
    for i in sorted(clines):
        hit = NOSEC_PAT.search(lines[i - 1])
        if hit:
            ids = sorted(set(RULE_ID.findall(hit.group(1) or "")))
            if ids:
                out.append((i, ids))
    return out


def line_shaped(text: str) -> int:
    """The retired grep shape: count ``# nosec`` wherever it appears, string literals included."""
    from scripts.quality.acceptance_item_probe import NOSEC_PAT

    return sum(1 for line in text.splitlines() if NOSEC_PAT.search(line))


def declared(text: str) -> collections.Counter[str]:
    """Count exemption ids per site the way the shipped comment-token census does."""
    out: collections.Counter[str] = collections.Counter()
    for _i, ids in nosec_sites(text):
        for rule in ids:
            out[rule] += 1
    return out


def parse_shapes(text: str) -> tuple[list[list[str]], list[list[str]], int]:
    """Show the retired and shipped patterns disagreeing on a real two-id line from this repo."""
    retired: list[list[str]] = []
    shipped: list[list[str]] = []
    lines = text.splitlines()
    for i, ids in nosec_sites(text):
        if len(ids) < 2:
            continue
        shipped.append(ids)
        old = RETIRED_SHAPE.search(lines[i - 1])
        retired.append(RULE_ID.findall((old.group(1) or "") if old else ""))
    return retired, shipped, len(shipped)


def diff(
    fired: collections.Counter[str], dec: collections.Counter[str]
) -> tuple[list[str], list[str]]:
    """Return (declared but never fired, fired but never declared) over one file set."""
    decorative = sorted(tid for tid in dec if fired.get(tid, 0) == 0)
    undeemed = sorted(tid for tid in fired if dec.get(tid, 0) == 0)
    return decorative, undeemed


def write_scratch(name: str, text: str) -> str:
    """Put a tampered copy inside the repo tree so bandit's config sees the same shape it gates.

    The path is returned repo-relative for the same reason the population keys are: bandit reports
    findings relative to its cwd, and an absolute argv would put a machine path in the archive.
    """
    target = REPO / SCRATCH / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return f"{SCRATCH}/{name}"


def control_decorative(clean: dict[str, Any]) -> bool:
    """Append an id this file never fires and charge it to the scanner, not to a line.

    ``B608`` (try/except/pass) has no shape in the probe, so bandit cannot consume it. Expected on
    the per-file frame: one more site, the same consumptions, surplus going 0 -> 1, ``dead_ids``
    naming B608, and no new finding anywhere in the file.
    """
    text = (REPO / TWO_ID_PROBE).read_text(encoding="utf-8")
    tampered_text = f"{text}\n# nosec B608\n"
    path = write_scratch(f"deco_{pathlib.Path(TWO_ID_PROBE).stem}.py", tampered_text)
    tampered = per_file(path, tampered_text)
    emit(f"  CONTROL-DECORATIVE file={TWO_ID_PROBE} +1 id the scanner never fires on this file")
    for label, reading in (("clean", clean), ("tampered", tampered)):
        emit(
            f"    {label:<9} sites={reading['sites']} skipped={reading['skipped']}"
            f" surplus={reading['surplus']} fired={reading['fired']}"
            f" surviving={reading['surviving']} dead_ids={reading['dead_ids']}"
            f" line_dead={len(reading['line_dead'])}"
        )
    emit("    expected: surplus 0 -> 1, dead_ids gains {'B608': 1}, nothing new fires")
    return (
        clean["surplus"] == 0
        and not clean["dead_ids"]
        and tampered["surplus"] == 1
        and tampered["sites"] == clean["sites"] + 1
        and tampered["skipped"] == clean["skipped"]
        and tampered["dead_ids"] == {"B608": 1}
        and tampered["fired"] == clean["fired"]
        and not tampered["surviving"]
    )


def control_undeemed(clean: dict[str, Any]) -> bool:
    """Delete one id from a two-id exemption: that finding has to come back through the gap.

    Which id and which line are read from the site rather than typed, so the arm cannot quietly
    target a line that does not exist. This is the proof that the exemption is load-bearing.
    """
    text = (REPO / TWO_ID_PROBE).read_text(encoding="utf-8")
    sites = dict(nosec_sites(text))
    victim = next((line for line, ids in sites.items() if len(ids) > 1), None)
    if victim is None:
        emit(f"  CONTROL-UNDEEMED file={TWO_ID_PROBE} no two-id exemption -> cannot differ")
        return False
    drop = sites[victim][-1]
    lines = text.splitlines(keepends=True)
    tampered_lines = list(lines)
    tampered_lines[victim - 1] = tampered_lines[victim - 1].replace(f" {drop}", "", 1)
    tampered_text = "".join(tampered_lines)
    path = write_scratch(f"und_{pathlib.Path(TWO_ID_PROBE).stem}.py", tampered_text)
    tampered = per_file(path, tampered_text)
    emit(f"  CONTROL-UNDEEMED file={TWO_ID_PROBE} line={victim} dropped={drop}")
    for label, reading in (("clean", clean), ("tampered", tampered)):
        emit(
            f"    {label:<9} sites={reading['sites']} skipped={reading['skipped']}"
            f" surplus={reading['surplus']} fired={reading['fired']}"
            f" surviving={reading['surviving']} rc={reading['rc']}"
        )
    emit(f"    expected: {drop} back as a surviving finding, rc red, one fewer consumption")
    return bool(
        tampered["surviving"] == {drop: 1}
        and tampered["surviving_rows"] == 1
        and tampered["rc"] != 0
        and tampered["surplus"] == 0
        and tampered["additive"]
        and tampered["skipped"] == clean["skipped"] - 1
        and sum(tampered["fired"].values()) == sum(clean["fired"].values())
    )


def control_blind(per: dict[str, dict[str, Any]]) -> bool:
    """Put this round's real over-declaration back and show which frame can see it.

    ``probe-seven-carrier.py`` carried ``# nosec B603 B607`` on a call whose argv arrives as a
    tuple, so bandit never fired a B607 there -- yet the population-wide per-id census read
    ``decorative=[]`` the whole time, because B607 does fire in other files of the pool. That
    silence is the finding, not a pass, and the arm expects both halves: the pooled presence census
    stays empty on the forged pool, while the per-file frame names the id with ``surplus=1`` on the
    same copy.
    """
    text = (REPO / BLIND_VICTIM).read_text(encoding="utf-8")
    if text.count(BLIND_SEED) != 1:
        emit(
            f"  CONTROL-BLIND victim={BLIND_VICTIM} seed_sites={text.count(BLIND_SEED)}"
            " -> cannot differ"
        )
        return False
    forged = text.replace(BLIND_SEED, BLIND_FORGE, 1)
    path = write_scratch(f"blind_{pathlib.Path(BLIND_VICTIM).name}", forged)
    clean, tampered = per[BLIND_VICTIM], per_file(path, forged)
    pool_paths: list[str] = []
    pool_texts: dict[str, str] = {}
    for rel in sorted(per):
        target, body = (
            (path, forged)
            if rel == BLIND_VICTIM
            else (rel, (REPO / rel).read_text(encoding="utf-8"))
        )
        pool_paths.append(target)
        pool_texts[target] = body
    pool_scan = bandit(["--ignore-nosec"], pool_paths)
    pool_again = bandit([], pool_paths)
    pool_dec: collections.Counter[str] = collections.Counter()
    for body in pool_texts.values():
        pool_dec.update(declared(body))
    deco_pool, und_pool = diff(pool_scan.fired, pool_dec)
    fired_b607 = pool_scan.fired.get(BLIND_ID, 0)
    emit(f"  CONTROL-BLIND victim={BLIND_VICTIM} re-forged the id this round removed")
    for label, reading in (("clean", clean), ("tampered", tampered)):
        emit(
            f"    per-file {label:<9} sites={reading['sites']} skipped={reading['skipped']}"
            f" surplus={reading['surplus']} dead_ids={reading['dead_ids']}"
        )
    emit(
        f"    pooled on the forged population: decorative={deco_pool} undeemed={und_pool}"
        f" | {BLIND_ID}: declared={pool_dec.get(BLIND_ID, 0)} fired={fired_b607}"
        f" | pooled_surplus={sum(pool_dec.values()) - pool_again.skipped}"
    )
    emit("    expected: pooled presence census stays silent, per-file frame names the extra id")
    return (
        fired_b607 > 0
        and pool_dec.get(BLIND_ID, 0) == fired_b607 + 1
        and not deco_pool
        and not und_pool
        and clean["surplus"] == 0
        and not clean["dead_ids"]
        and tampered["surplus"] == 1
        and tampered["dead_ids"] == {BLIND_ID: 1}
        and tampered["fired"] == clean["fired"]
    )


def git_face(*args: str) -> str:
    """One read-only git command, stdout stripped. Two claims need two calls, not one argv."""
    done = subprocess.run(  # noqa: S603  # nosec B603 B607  # read-only git, exec on PATH
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def main() -> int:
    """Audit every exemption in this round's instruments per file, then arm three directions."""
    sys.path.insert(0, str(REPO))
    # A crashed run leaves its tampered copies behind inside docs/evidence/**, where the gate's
    # member 5 would charge them as untracked and the repo-wide exemption census would count them.
    shutil.rmtree(REPO / SCRATCH, ignore_errors=True)
    files = population()
    texts = {rel: (REPO / rel).read_text(encoding="utf-8") for rel in files}
    per = {rel: per_file(rel, texts[rel]) for rel in files}
    scan = bandit(["--ignore-nosec"], files)
    again = bandit([], files)
    sites: dict[tuple[str, int], list[str]] = {}
    for rel in files:
        sites.update(site_map(rel, texts[rel]))
    dead = dead_sites(sites, fired_lines(scan.rows))
    dec: collections.Counter[str] = collections.Counter()
    for text in texts.values():
        dec.update(declared(text))
    decorative, undeemed = diff(scan.fired, dec)
    fired_total = sum(scan.fired.values())
    remaining = sum(again.fired.values())
    sites_total = sum(dec.values())
    additivity = fired_total == again.skipped + remaining
    surplus = sites_total - again.skipped
    parts_skipped = sum(per[rel]["skipped"] for rel in files)
    parts_sites = sum(per[rel]["sites"] for rel in files)
    parts_surplus = sum(per[rel]["surplus"] for rel in files)
    with_sites = [rel for rel in files if per[rel]["sites"]]
    emit(
        f"[1] files={len(files)} (enumerated, not typed) files_with_exemptions={len(with_sites)}"
        f" exemption_sites={sites_total} config={CONFIG}"
    )
    for rel in files:
        emit(f"    {rel} nosec_sites={per[rel]['sites']}")
    emit(f"    bandit --ignore-nosec rc={scan.rc} fired={dict(sorted(scan.fired.items()))}")
    if scan.stderr:
        emit(f"    bandit stderr={scan.stderr[:200]}")
    emit(
        f"[2] declared={dict(sorted(dec.items()))}"
        f" surviving_after_nosec={dict(sorted(again.fired.items()))}"
    )
    emit(
        f"[3] pooled per-id presence census: decorative={decorative} undeemed={undeemed}"
        "  <- blind across files: an extra id hides behind the same id firing in another file"
    )
    emit("[3b] per-file frame (sites charged against that file's own skipped_tests)")
    for rel in files:
        reading = per[rel]
        emit(
            f"    {rel} sites={reading['sites']} skipped={reading['skipped']}"
            f" surplus={reading['surplus']} surviving_rows={reading['surviving_rows']}"
            f" fired={reading['fired']} surviving={reading['surviving']}"
            f" dead_ids={reading['dead_ids']} undeemed_ids={reading['undeemed_ids']}"
            f" additive={reading['additive']} rc={reading['rc']}"
            f" line_dead={len(reading['line_dead'])}"
        )
    shape = per[SHAPE_CONTROL]
    emit(
        f"[3c] parts tie to the whole: sum(per-file skipped)={parts_skipped} pool={again.skipped}"
        f" | sum(per-file sites)={parts_sites} pool={sites_total}"
        f" | sum(per-file surplus)={parts_surplus} pool={surplus}"
    )
    emit(
        f"[3d] required disagreement {SHAPE_CONTROL}: line_dead={len(shape['line_dead'])}"
        f" surplus={shape['surplus']} surviving_rows={shape['surviving_rows']}"
        f" skipped={shape['skipped']} fired={shape['fired']}"
    )
    emit(
        "     the line frame calls these exemptions dead while the scanner consumed every one;"
        " if the two frames ever agree here the frame swap is unproven"
    )
    for row in dead:
        emit(f"    LINE-DEAD(bias, not in gate) {row}")
    for rel, line, tid in sorted(again.rows):
        emit(f"    SURVIVING {rel}:{line} {tid}")
    emit(
        f"[3e] bandit's own arithmetic: fired={fired_total} skipped={again.skipped}"
        f" surviving={remaining}"
        f" -> additivity={additivity}; declared_sites={sites_total} consumed={again.skipped}"
        f" surplus={surplus}"
    )
    retired, shipped, two_id_lines = parse_shapes(texts[TWO_ID_PROBE])
    emit(f"[4] two-id exemption lines in {TWO_ID_PROBE}={two_id_lines}")
    emit(f"    shipped pattern ids={shipped}")
    emit(f"    retired pattern ids={retired}  <- the parse that fabricated 'undeemed B607'")
    shapes = {
        rel: (len(nosec_sites(texts[rel])), line_shaped(texts[rel])) for rel in (SELF, PROSE_FREE)
    }
    self_wider = shapes[SELF][0] < shapes[SELF][1]
    control_equal = shapes[PROSE_FREE][0] == shapes[PROSE_FREE][1]
    emit(
        f"[4b] comment-token sites vs raw-line sites: {SELF}={shapes[SELF]} "
        f"(prose/literals only the grep shape sees), {PROSE_FREE}={shapes[PROSE_FREE]}"
    )
    emit(f"    expected: self_wider={self_wider} control_file_equal={control_equal}")
    emit("[5] controls")
    deco_ok = control_decorative(per[TWO_ID_PROBE])
    und_ok = control_undeemed(per[TWO_ID_PROBE])
    blind_ok = control_blind(per)
    # The tampered copies are only evidence while this run holds them. Left behind under
    # docs/evidence/** they would be counted by the repo-wide exemption census and charged as
    # untracked by the gate's member 5, so the cleanup is measured here rather than claimed below.
    shutil.rmtree(REPO / SCRATCH, ignore_errors=True)
    scratch_gone = not (REPO / SCRATCH).exists()
    clean_ok = (
        not decorative
        and not undeemed
        and not again.rows
        and additivity
        and surplus == 0
        and again.skipped >= 0
        and parts_skipped == again.skipped
        and parts_sites == sites_total
        and all(per[rel]["surplus"] == 0 for rel in files)
        and all(per[rel]["surviving_rows"] == 0 for rel in files)
        and all(per[rel]["additive"] for rel in files)
        and all(not per[rel]["undeemed_ids"] for rel in files)
    )
    parse_ok = two_id_lines > 0 and shipped != retired
    shape_ok = self_wider and control_equal
    disagree_ok = shape["surplus"] == 0 and len(shape["line_dead"]) > 0
    ok = (
        clean_ok
        and parse_ok
        and shape_ok
        and disagree_ok
        and deco_ok
        and und_ok
        and blind_ok
        and scratch_gone
    )
    emit(
        f"[6] clean={clean_ok} parse_differs={parse_ok} shapes_agree_with_scanner={shape_ok}"
        f" line_frame_over_reports={disagree_ok} decorative_detected={deco_ok}"
        f" undeemed_detected={und_ok} pooled_blindness_shown={blind_ok}"
        f" scratch_cleared={scratch_gone}"
    )
    verdict = f"NOSEC_AUDIT_CHECK {'PASS' if ok else 'FAIL'}\nINSTRUMENT_RC={0 if ok else 1}"
    emit(verdict)
    when = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp = f"sha256[:{DIGEST_LEN}]"
    lines = [
        TITLE,
        f"# 采集时间: {when}",
        f"# 分支: {git_face('rev-parse', '--abbrev-ref', 'HEAD')}"
        f"  HEAD: {git_face('rev-parse', '--short', 'HEAD')}",
        f"# python: {platform.python_version()} @ {sys.executable}",
        f"# 仪器 {stamp}: {digest_self()}",
        f"# command: python {SELF}",
        f"# 副作用: 反例臂在 {SCRATCH}/ 写被篡改的临时副本，出判定前删除（scratch_cleared 面实测）",
        "# git status --porcelain（跑时，含本仪器自己的未提交项）:",
        git_face("status", "--porcelain"),
        "#",
        "",
    ]
    (REPO / ARCHIVE).write_text("\n".join(lines) + "\n".join(OUT) + "\n", encoding="utf-8")
    return 0 if ok else 1


def digest_self() -> str:
    """sha256[:16] of this instrument's bytes."""
    return hashlib.sha256((REPO / SELF).read_bytes()).hexdigest()[:DIGEST_LEN]


if __name__ == "__main__":
    sys.exit(main())
