"""Price the four files this wave edited as bytes, so ``face.md`` owes no recalled digest.

Why this instrument exists: the first draft of ``face.md`` §1 named three sha256:16 digests written
from recall, and two of the three were wrong. A digest is only evidence if something on disk prints
it, so each one here comes from three sources that must line up -- the bytes on disk, the bytes
``git show HEAD:<path>`` returns, and the ``after_sha256`` the producer wrote into the shard pins.

Arms:

``A_landed_bytes``
    one ``LAND`` line per landed file (disk digest, HEAD digest, pin set, agreement), plus the
    shard's distinct pinned-path denominator.
``B_all_four_moved``
    every landed file differs HEAD-versus-worktree, so the wave's own diff is what moved them.
``C_pin_membership_walked``
    which of the four the claims document pins at all, read off the same walk the producer's pins
    are made of -- this is what says a re-based test file owes no re-sign.
``D_pinned_agree_with_disk``
    for every one of the four that IS pinned, pin == disk, i.e. the re-sign tax is paid.
``E_denominator_agrees_with_the_drift_carrier``
    this probe's pinned-path count equals the ``pinned_files=`` the post-chain pin-drift carrier
    printed -- two instruments, one population, and either could read zero by mis-shape.
``F_shard_part_agrees_with_both_drift_carriers``
    the pre-landing carrier's ``STALE ... disk=`` value for the shard part equals today's disk
    digest (I edited nothing there after producing), and the rerun carrier reads ``stale_files=0``.
``G_probe_itself_rule_clean``
    the shipped ruff channel charges this probe zero codes.
``H_control_untouched_file``
    a file this wave did not edit must read HEAD == disk; without it, B's all-different is a
    predicate that cannot read False and therefore proves nothing.
``I_head_plane_self_consistent``
    for every landed file the committed claims document pins, the pin equals the digest of that
    same file's HEAD bytes -- this is the clean-clone reading, and it is the one the pre-landing
    drift carrier cannot give because it read a worktree document.
``J_intermediate_generation_named``
    the ``pinned=`` digest the pre-landing drift carrier printed is neither HEAD's bytes nor
    today's, so that carrier's baseline is a generation this wave's own first run produced and then
    overwrote. Recording which generation a face really compared is part of pricing it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

PROBES = Path(__file__).resolve().parent
if str(PROBES) not in sys.path:
    sys.path.insert(0, str(PROBES))

from take1_lint_preexistence import ruff_over  # noqa: E402
from w31s4_run_chain import (  # noqa: E402
    FETCHER,
    HANDOFF_TEST,
    PY,
    REPO,
    RUNS,
    SHARD,
    SHARD_PART,
    WITNESS,
    carrier,
)

PIN_GROUPS: tuple[str, ...] = ("source", "contract", "fixture", "probe")
LANDED_FILES: tuple[str, ...] = (FETCHER, SHARD_PART, WITNESS, HANDOFF_TEST)
CONTROL_UNTOUCHED = "scripts/quality/iter2_claims_producer.py"
POPULATION: tuple[str, ...] = (*LANDED_FILES, CONTROL_UNTOUCHED)
PRE_CARRIER = RUNS / "wave31-step4-pin_drift_pre_production.txt"
RERUN_CARRIER = RUNS / "wave31-step4-pin_drift_pre_production_rerun.txt"
STALE_LINE = re.compile(r"^STALE (\S+) pinned=([0-9a-f]{16}) disk=([0-9a-f]{16})$")
DRIFT_COUNT = re.compile(r"pinned_files=(\d+) stale_files=(\d+)")


def short(data: bytes) -> str:
    """The same 16-hex digest prefix the shard pins record, so the two are comparable."""
    return hashlib.sha256(data).hexdigest()[:16]


def git_show_bytes(path: str) -> tuple[bytes, int]:
    """Return HEAD's bytes for ``path``; binary mode, because a digest is about bytes."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # read-only git plumbing, fixed list argv
        ["git", "show", f"HEAD:{path}"],  # noqa: S607  # nosec B607  # a PATH lookup, never input
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.stdout, proc.returncode


def pinned_paths() -> Mapping[str, set[str]]:
    """Walk the shard's dict-shaped pin groups into ``path -> set of pinned digest prefixes``."""
    claims = json.loads((REPO / SHARD).read_text(encoding="utf-8"))
    seen: dict[str, set[str]] = {}
    for claim in claims["claims"]:
        for group in PIN_GROUPS:
            for pin in claim["pins"].get(group) or []:
                seen.setdefault(str(pin["path"]), set()).add(str(pin["after_sha256"])[:16])
    return seen


def stale_pairs(carrier_text: str) -> dict[str, tuple[str, str]]:
    """Map each path a ``STALE`` line names to the ``(pinned, disk)`` pair it printed."""
    found = (STALE_LINE.match(line) for line in carrier_text.splitlines())
    return {m.group(1): (m.group(2), m.group(3)) for m in found if m}


def head_claims_pins() -> Mapping[str, set[str]]:
    """The same pin walk ``pinned_paths`` does, but over the claims document as HEAD holds it."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # read-only git plumbing, fixed list argv
        ["git", "show", f"HEAD:{SHARD}"],  # noqa: S607  # nosec B607  # a PATH lookup, never input
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {}
    seen: dict[str, set[str]] = {}
    for claim in json.loads(proc.stdout)["claims"]:
        for group in PIN_GROUPS:
            for pin in claim["pins"].get(group) or []:
                seen.setdefault(str(pin["path"]), set()).add(str(pin["after_sha256"])[:16])
    return seen


def base(rel: str) -> str:
    """The file name only, for the one line that has to stay narrow enough to read."""
    return rel.rsplit("/", 1)[-1]


def main(argv: list[str]) -> int:
    """Take the byte face and write it in this wave's carrier format."""
    del argv
    out: list[str] = []
    pinned = pinned_paths()
    own_path = Path(__file__).resolve()

    faces: dict[str, dict[str, Any]] = {}
    for rel in POPULATION:
        data = (REPO / rel).read_bytes()
        head_bytes, head_rc = git_show_bytes(rel)
        faces[rel] = {
            "disk": short(data),
            "head": short(head_bytes),
            "head_rc": head_rc,
            "bytes": len(data),
            "pins": sorted(pinned.get(rel, set())),
        }

    for rel in POPULATION:
        face = faces[rel]
        moved = face["disk"] != face["head"]
        pins = "/".join(face["pins"]) or "NONE"
        agrees = bool(face["pins"]) and face["disk"] in face["pins"]
        out.append(
            f"LAND {rel} disk={face['disk']} head={face['head']} head_exit={face['head_rc']} "
            f"bytes={face['bytes']} pinned={pins} pin_matches_disk={agrees} moved={moved}"
        )

    moved_files = [rel for rel in LANDED_FILES if faces[rel]["disk"] != faces[rel]["head"]]
    pinned_here = [rel for rel in LANDED_FILES if faces[rel]["pins"]]
    unpinned_here = [rel for rel in LANDED_FILES if not faces[rel]["pins"]]
    agreed = [rel for rel in pinned_here if faces[rel]["disk"] in faces[rel]["pins"]]
    at_head = head_claims_pins()
    head_pinned = [rel for rel in LANDED_FILES if rel in at_head]
    head_agreed = [rel for rel in head_pinned if faces[rel]["head"] in at_head[rel]]
    control = faces[CONTROL_UNTOUCHED]
    pre_text = PRE_CARRIER.read_text(encoding="utf-8")
    rerun_text = RERUN_CARRIER.read_text(encoding="utf-8")
    pre_stale = stale_pairs(pre_text)
    pre_pin, pre_disk = pre_stale.get(SHARD_PART, ("ABSENT", "ABSENT"))
    counts = re.search(DRIFT_COUNT, rerun_text)
    rerun_pinned = int(counts.group(1)) if counts else -1
    rerun_stale = int(counts.group(2)) if counts else -1
    today_disk = faces[SHARD_PART]["disk"]
    head_disk = faces[SHARD_PART]["head"]
    self_codes, self_hits, self_ok = ruff_over(
        own_path.read_text(encoding="utf-8"), "G_self_bytes", filename=str(own_path)
    )

    gates = {
        "A_every_git_show_returned_zero": all(faces[rel]["head_rc"] == 0 for rel in POPULATION),
        "B_all_four_moved": moved_files == list(LANDED_FILES),
        "C_pin_membership_printed": len(pinned_here) + len(unpinned_here) == len(LANDED_FILES),
        "D_pinned_agree_with_disk": bool(pinned_here) and len(agreed) == len(pinned_here),
        "E_denominator_agrees_with_drift_carrier": len(pinned) == rerun_pinned,
        "F_shard_part_agrees_with_both_carriers": pre_disk == today_disk and rerun_stale == 0,
        "G_probe_itself_rule_clean": not self_codes,
        "H_control_untouched_file": control["disk"] == control["head"],
        "I_head_plane_self_consistent": bool(head_pinned) and len(head_agreed) == len(head_pinned),
        "J_intermediate_generation_named": pre_pin not in {head_disk, today_disk},
    }

    out.append(
        f"A_landed_bytes landed={len(LANDED_FILES)} denominator_pinned_paths={len(pinned)} "
        f"shard={SHARD} pin_groups={list(PIN_GROUPS)}"
    )
    out.append(
        f"B_all_four_moved count={len(moved_files)} files={moved_files} "
        f"reading=every landed file is its own byte change; none is HEAD's bytes"
    )
    out.append(
        f"C_pin_membership pinned={pinned_here} unpinned={unpinned_here} "
        f"reading=an unpinned landed file owes the shard no re-sign; the pinned ones do"
    )
    pairs = ", ".join(f"{base(rel)}:{faces[rel]['disk']}" for rel in pinned_here)
    out.append(
        f"D_pinned_agree_with_disk agreed={len(agreed)} of={len(pinned_here)} disk={{{pairs}}}"
    )
    out.append(
        f"E_denominator this_probe={len(pinned)} drift_carrier={rerun_pinned} "
        f"carrier_stale_files={rerun_stale}"
    )
    out.append(
        f"F_carrier_agreement pre_landing_pinned={pre_pin} pre_landing_disk={pre_disk} "
        f"today_disk={today_disk} rerun_stale_files={rerun_stale} "
        f"reading=the shard part has not been edited since the producer pinned it"
    )
    out.append(
        f"G_self_lint bytes={own_path.stat().st_size} codes={dict(sorted(self_codes.items()))} "
        f"hits={self_hits} parser_agrees={self_ok}"
    )
    out.append(
        f"H_control_untouched {CONTROL_UNTOUCHED} disk={control['disk']} head={control['head']} "
        f"moved={control['disk'] != control['head']} "
        f"reading=the moved-predicate reads False for a file this wave did not edit"
    )
    head_pairs = ", ".join(f"{base(rel)}:{'/'.join(sorted(at_head[rel]))}" for rel in head_pinned)
    out.append(
        f"I_head_plane_self_consistent pinned_at_head={len(head_pinned)} of={len(LANDED_FILES)} "
        f"agree={len(head_agreed)} head_pins={{{head_pairs}}} "
        f"unpinned_at_head={[base(rel) for rel in LANDED_FILES if rel not in at_head]} "
        f"reading=for every landed file HEAD pins, that pin is HEAD's own bytes; the fetcher is "
        f"absent from HEAD's pin set because HEAD's shard held no entry for this row"
    )
    out.append(
        f"J_intermediate_generation named_by_pre_landing_carrier={pre_pin} "
        f"is_head={pre_pin == head_disk} is_today={pre_pin == today_disk} "
        f"reading=that digest belongs to neither, because the pre-landing drift carrier read a "
        f"worktree claims document this wave's own first run had already rewritten"
    )
    out.append(
        f"bytes_face=arms={len(gates)} arms_passed={sum(gates.values())} gates={gates} "
        f"all_pass={all(gates.values())}"
    )

    body = "\n".join(out)
    exit_code = 0 if all(gates.values()) else 1
    path = RUNS / "wave31-step4-landed_bytes_face.txt"
    path.write_text(
        carrier(
            {
                "name": "landed_bytes_face",
                "argv": [PY, str(own_path)],
                "pythonpath": ".",
                "exit": exit_code,
                "stderr": "",
                "stdout": body,
                "note": "the four landed files and one untouched control, priced by disk bytes, "
                "by git show HEAD bytes, and by the after_sha256 the producer wrote into the shard",
            }
        ),
        encoding="utf-8",
    )
    print(body, flush=True)
    print(f"carrier_written bytes={path.stat().st_size} path={path.relative_to(REPO)}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
