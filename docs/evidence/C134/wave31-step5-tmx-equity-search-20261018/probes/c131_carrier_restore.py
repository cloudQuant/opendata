r"""Restore the C131 step-0 plane-2 carrier that this wave's chain aged, and prove the loss is nil.

Why this instrument exists: ``docs/evidence/C131/wave31-plane2-face-20261018/probes/
plane2_dev_done_axis_census.py`` writes its archive to one module-level constant (line 46,
``CARRIER = BUNDLE / "runs/wave31-step0-plane2-axis-census.txt"``, written by ``_emit`` at 486) and
takes no path argument, so running it as a verification leg of this wave rewrote step 0's final
carrier in place. Same line count (141), same length (14079 B), different digest: HEAD's face reads
``accepted=285 residue=65`` and the overwrite read ``accepted=291 residue=59``. Both are true of
their own generation, so the write is not a corruption -- it is an *aging*, and C131's face.md line
27 pins the old digest (``141 行 / 14079 B / 022bd5186aa69c81``), which is exactly the claim this
restore makes true again.

So the instrument moves the aged bytes rather than deleting them: they are archived beside this
wave, which is the generation that produced them, and then HEAD's bytes go back into C131. That
also makes this probe re-runnable -- every gate is priced off the archived copy and off HEAD, never
off "the worktree happens to be dirty right now", so a second run reads the same verdict.

Nothing here touches the census instrument itself: it is tracked, this wave ran it read-only apart
from its own fixed output path, and arm C prices that the ``.py`` is still HEAD's bytes, so the
restore cannot be silently undoing an instrument edit.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROBES = Path(__file__).resolve().parent
if str(PROBES) not in sys.path:
    sys.path.insert(0, str(PROBES))

from landed_bytes_face import ruff_codes  # noqa: E402
from w31s5_run_chain import PY, REPO, RUNS, carrier  # noqa: E402

C131 = REPO / "docs/evidence/C131/wave31-plane2-face-20261018"
AGED = C131 / "runs/wave31-step0-plane2-axis-census.txt"
AGED_COPY = RUNS / "wave31-step5-c131_plane2_face_aged_by_this_chain.txt"
CENSUS_PROBE = C131 / "probes/plane2_dev_done_axis_census.py"
C131_FACE = C131 / "face.md"
THIS_WAVE_CARRIER = RUNS / "wave31-step5-plane2_dev_done_axis_census.txt"
COUNTERS = re.compile(r"identities=(\d+) accepted=(\d+) residue=(\d+)")


def sha(data: bytes) -> str:
    """The 16-hex digest form the pinning prose uses."""
    return hashlib.sha256(data).hexdigest()[:16]


def git_bytes(*args: str) -> tuple[bytes, int]:
    """Run a read-only git plumbing command against the repo."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed list argv, no shell
        ["git", *args],  # noqa: S607  # nosec B607  # a PATH lookup, never input
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.stdout, proc.returncode


def counters(text: str) -> tuple[int, int, int]:
    """Read the cross-plane join line's three counters, or (-1, -1, -1) when the line is absent."""
    found = COUNTERS.search(text)
    if found is None:
        return (-1, -1, -1)
    return (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def main(argv: list[str]) -> int:
    """Archive the aged generation, restore HEAD's bytes, and price both halves."""
    del argv
    own_path = Path(__file__).resolve()
    out: list[str] = []

    rel_aged = str(AGED.relative_to(REPO))
    rel_probe = str(CENSUS_PROBE.relative_to(REPO))
    head_bytes, head_rc = git_bytes("show", f"HEAD:{rel_aged}")
    head_text = head_bytes.decode("utf-8")
    head_lines = head_text.splitlines()
    pinned = sha(head_bytes)

    live_bytes = AGED.read_bytes()
    if sha(live_bytes) != pinned:
        shutil.copyfile(AGED, AGED_COPY)
    aged_bytes = AGED_COPY.read_bytes() if AGED_COPY.is_file() else live_bytes
    aged_text = aged_bytes.decode("utf-8")
    aged_lines = aged_text.splitlines()

    carrier_text = THIS_WAVE_CARRIER.read_text(encoding="utf-8")
    carrier_set = set(carrier_text.splitlines())
    absent = [line for line in aged_lines if line not in carrier_set]

    probe_text = CENSUS_PROBE.read_text(encoding="utf-8")
    probe_head, probe_rc = git_bytes("show", f"HEAD:{rel_probe}")
    slices_argv = "sys.argv" in probe_text or "argparse" in probe_text
    flagless = "--output" not in probe_text and not slices_argv
    echo_emit = "CARRIER.write_text" in probe_text and "for line in lines" in probe_text

    pin_line = next(
        (line for line in C131_FACE.read_text(encoding="utf-8").splitlines() if AGED.name in line),
        "",
    )
    pin_digests = re.findall(r"\b[0-9a-f]{16}\b", pin_line)
    pin_numbers = [int(n) for n in re.findall(r"(\d+)\s*(?:行|B)", pin_line)]

    out.append(
        f"A_aged_face path={rel_aged} head={pinned} head_exit={head_rc} "
        f"archived_digest={sha(aged_bytes)} archived_bytes={len(aged_bytes)} "
        f"head_bytes={len(head_bytes)} archived_lines={len(aged_lines)} "
        f"head_lines={len(head_lines)} "
        f"different={sha(aged_bytes) != pinned} "
        f"reading=same length, different digest: the census leg rewrote step 0's archive in place, "
        f"and the bytes it wrote are archived here rather than deleted"
    )
    out.append(
        f"B_generations archived_aged={counters(aged_text)} head_face={counters(head_text)} "
        f"this_wave_carrier={counters(carrier_text)} "
        f"reading=HEAD is step 0's generation and the archived copy is this chain's; the two agree "
        f"with this wave's own leg carrier, so both are true of their own report"
    )
    out.append(
        f"C_census_probe_untouched disk={sha(CENSUS_PROBE.read_bytes())} head={sha(probe_head)} "
        f"head_exit={probe_rc} moved={sha(CENSUS_PROBE.read_bytes()) != sha(probe_head)} "
        f"fixed_carrier_const={'CARRIER = BUNDLE' in probe_text} slices_argv={slices_argv} "
        f"output_flag_present={'--output' in probe_text} flagless={flagless} "
        f"emits_then_echoes={echo_emit} "
        f"reading=the instrument is HEAD's bytes and takes no path argument, which is why the leg "
        f"aged its archive instead of writing beside this wave"
    )
    out.append(
        f"D_no_loss archived_lines={len(aged_lines)} "
        f"absent_from_this_wave_carrier={len(absent)} absent={absent[:3] or '[]'} "
        f"reading=every line the aged file held is carried by this wave's own leg archive, so the "
        f"restore below discards no reading this wave has not already committed"
    )
    out.append(
        f"E_pin_priced face_md={pin_line.strip()[:110]} digests_named={pin_digests} "
        f"numbers_named={pin_numbers} head_digest={pinned} head_lines={len(head_lines)} "
        f"head_bytes={len(head_bytes)} "
        f"reading=every number C131 face.md declares for this file is HEAD's, not the aged copy's, "
        f"so restoring HEAD is what makes that prose true"
    )

    AGED.write_bytes(head_bytes)
    restored = AGED.read_bytes()
    status_out, status_rc = git_bytes("status", "--porcelain", rel_aged)
    out.append(
        f"F_restored disk_after={sha(restored)} equals_head={sha(restored) == pinned} "
        f"bytes={len(restored)} lines={len(restored.decode('utf-8').splitlines())} "
        f"git_status_bytes={len(status_out)} git_status_exit={status_rc} "
        f"clean={not status_out.strip()} "
        f"reading=the file is HEAD's bytes again and reads clean to git, so it cannot enter this "
        f"wave's commit payload"
    )

    self_codes = ruff_codes(own_path)
    out.append(
        f"G_self_lint bytes={own_path.stat().st_size} codes={dict(sorted(self_codes.items()))} "
        f"hits={sum(self_codes.values())}"
    )

    head_counters = counters(head_text)
    aged_counters = counters(aged_text)
    carrier_counters = counters(carrier_text)
    gates = {
        "A_aged_is_a_different_generation": sha(aged_bytes) != pinned and head_rc == 0,
        "A_same_shape_different_bytes": len(aged_lines) == len(head_lines)
        and len(aged_bytes) == len(head_bytes),
        "B_three_generations_counted": all(
            all(v >= 0 for v in trio) for trio in (head_counters, aged_counters, carrier_counters)
        ),
        "B_aged_agrees_with_this_wave_carrier": aged_counters == carrier_counters,
        "B_head_is_the_other_generation": head_counters != aged_counters,
        "C_probe_untouched_and_flagless": sha(CENSUS_PROBE.read_bytes()) == sha(probe_head)
        and probe_rc == 0
        and flagless,
        "D_no_line_lost": not absent and len(aged_lines) == len(head_lines),
        "E_pin_digest_is_head_bytes": pinned in pin_digests,
        "E_pin_numbers_match_head": pin_numbers == [len(head_lines), len(head_bytes)],
        "F_restored_and_clean": sha(restored) == pinned and not status_out.strip(),
        "G_probe_itself_rule_clean": not self_codes,
    }
    all_pass = all(gates.values())
    out.append(
        f"restore_face=arms={len(gates)} arms_passed={sum(gates.values())} gates={gates} "
        f"all_pass={all_pass}"
    )

    body = "\n".join(out)
    exit_code = 0 if all_pass else 1
    path = RUNS / "wave31-step5-c131_carrier_restore.txt"
    path.write_text(
        carrier(
            {
                "name": "c131_carrier_restore",
                "argv": [PY, str(own_path)],
                "pythonpath": ".",
                "exit": exit_code,
                "stderr": "",
                "stdout": body,
                "note": "the plane-2 census leg aged C131 step 0's archive because that instrument "
                "hardcodes its output path; the aged bytes are archived in this wave, every line "
                "shown to survive in the leg carrier, and HEAD's bytes are restored and priced",
            }
        ),
        encoding="utf-8",
    )
    print(body, flush=True)
    print(f"carrier_written bytes={path.stat().st_size} path={path.relative_to(REPO)}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
