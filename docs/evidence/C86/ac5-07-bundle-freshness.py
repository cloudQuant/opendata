#!/usr/bin/env python3
"""AC-5|07's security bundle: rebuild it this round, then prove the freshness judge is still live.

The cell was red because the C74 bundle aged past ``MAX_EVIDENCE_AGE``. The repair is to re-*build*
it -- bandit really runs again and new bytes land -- never to re-age a stored file, and the proof
has to answer both ways. Three arms, all through the repo's own judge (the validator exposes a
``now`` seam, so no file is edited to travel in time):

* ``now=None`` -- the reading the probe takes today. After a rebuild it must carry no issue.
* ``now=generated_at + MAX_EVIDENCE_AGE + 1h`` -- the same bytes judged a day later. Must turn
  ``scan-stale`` and ``triage-stale``, or the freshness rule is decoration.
* ``now=generated_at - 1h`` -- a bundle dated after the clock. Must turn the two
  ``*-generated-at-invalid`` codes, which is the arm showing the judge reads the timestamps.

The builder runs with a literal argv so the archive also carries its refusal surface: if today's
finding identity no longer matches the prior triage it prints ``BUILD_REFUSED`` and exits 2 instead
of quietly certifying. The validator's fact dict is dumped whole, because a hand-picked key that
misses prints a convincing empty.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import platform
import subprocess  # nosec B404  # literal argv: this repo's builder and git, never a shell string
import sys
from typing import Any, Final

REPO = pathlib.Path(__file__).resolve().parents[3]
SELF: Final = "docs/evidence/C86/ac5-07-bundle-freshness.py"
ARCHIVE: Final = "docs/evidence/C86/ac5-07-bundle-freshness.txt"
BUILDER: Final = "scripts/quality/ported_security_evidence_build.py"
TITLE: Final = "# C86 —— AC-5|07 安全证据 bundle：本轮重建 + 三条时钟臂证明新鲜度判据仍活"
DIGEST_LEN: Final = 16
EXTRA_HOURS: Final = 1
OUT: list[str] = []


def emit(line: str = "") -> None:
    """Print a face line and keep it for the archive, so the two can never diverge."""
    OUT.append(line)
    print(line)


def argv_build() -> dict[str, Any]:
    """Run the repo's own bundle builder with ``--force`` and capture its refusal surface."""
    done = subprocess.run(  # noqa: S603  # nosec B603  # literal argv: this repo's builder
        [sys.executable, str(REPO / BUILDER), "--force"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = dict(json.loads(done.stdout))
    except json.JSONDecodeError:
        payload = {"built": False, "reason": f"unparsable stdout: {done.stdout[:200]}"}
    return {
        "rc": done.returncode,
        "built": bool(payload.get("built")),
        "facts": {str(k): v for k, v in payload.items() if str(k) != "generated_at"},
        "stderr": done.stderr.strip()[:400],
    }


def born_at() -> dt.datetime | None:
    """The bundle's own recorded timestamp, read from the scan document."""
    sys.path.insert(0, str(REPO))
    from scripts.quality.ported_security_evidence import SCAN_REL

    try:
        raw = json.loads((REPO / SCAN_REL).read_text(encoding="utf-8")).get("generated_at")
        return dt.datetime.fromisoformat(str(raw))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def clock_arms(born: dt.datetime | None) -> list[tuple[str, Any]]:
    """Judge the same bytes at three clock readings through the validator's own ``now`` seam."""
    sys.path.insert(0, str(REPO))
    from scripts.quality.ported_security_evidence import MAX_EVIDENCE_AGE, validate

    arms: list[tuple[str, Any]] = [("live(now=None)", validate(REPO))]
    if born is not None:
        delta = MAX_EVIDENCE_AGE + dt.timedelta(hours=EXTRA_HOURS)
        arms.append((f"aged(now=generated+{delta})", validate(REPO, now=born + delta)))
        arms.append(
            (
                f"future-dated(now=generated-{dt.timedelta(hours=EXTRA_HOURS)})",
                validate(REPO, now=born - dt.timedelta(hours=EXTRA_HOURS)),
            )
        )
    return arms


def bundle_files() -> None:
    """Digest, byte count and mtime of each bundle file, so the rebuild's new bytes are visible."""
    sys.path.insert(0, str(REPO))
    from scripts.quality.ported_security_evidence import EVIDENCE_DIR, SCAN_REL, TRIAGE_REL

    for label, rel in (("scan", SCAN_REL), ("triage", TRIAGE_REL)):
        path = REPO / str(rel)
        raw = path.read_bytes()
        modified = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        emit(
            f"  {label} {rel} bytes={len(raw)}"
            f" sha256[:{DIGEST_LEN}]={hashlib.sha256(raw).hexdigest()[:DIGEST_LEN]}"
            f" mtime={modified:%Y-%m-%dT%H:%M:%S%z}"
        )
    emit(f"  evidence_dir={EVIDENCE_DIR}")


def main() -> int:
    """Rebuild, judge at three clocks, print every face, and gate on arms that answer both ways."""
    build = argv_build()
    emit(f"[1] builder {BUILDER} --force rc={build['rc']} built={build['built']}")
    for key in sorted(build["facts"]):
        emit(f"    {key}={build['facts'][key]}")
    if build["stderr"]:
        emit(f"    stderr={build['stderr']}")
    emit("[2] bundle bytes as rewritten by this run")
    bundle_files()
    born = born_at()
    emit(f"[3] recorded generated_at={born} -- three clock readings of the same bytes")
    arms = clock_arms(born)
    codes: dict[str, list[str]] = {}
    for arm, result in arms:
        codes[arm] = [str(issue.code) for issue in result.issues]
        emit(
            f"  {arm:<46} valid={result.valid}"
            f" issues={len(result.issues)} codes={','.join(codes[arm]) or 'none'}"
        )
    emit("[4] every fact the validator reports on the live arm")
    facts = dict(arms[0][1].facts)
    for key in sorted(facts):
        emit(f"    FACT {key}={facts[key]}")
    live_ok = not codes["live(now=None)"]
    aged_ok = any(
        arm.startswith("aged") and {"scan-stale", "triage-stale"} <= set(codes[arm])
        for arm, _ in arms
    )
    future_ok = any(
        arm.startswith("future")
        and {"scan-generated-at-invalid", "triage-generated-at-invalid"} <= set(codes[arm])
        for arm, _ in arms
    )
    emit(
        f"[5] arms live_clear={live_ok} aged_names_stale={aged_ok}"
        f" future_dated_names_invalid={future_ok} arms={len(arms)}"
    )
    ok = build["rc"] == 0 and build["built"] and born is not None and len(arms) == 3
    ok = ok and live_ok and aged_ok and future_ok
    verdict = (
        f"BUNDLE_FRESHNESS_CHECK {'PASS' if ok else 'FAIL'}\n"
        f"BUILDER_RC={build['rc']}\nINSTRUMENT_RC={0 if ok else 1}\n"
    )
    emit(verdict.strip())
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    lines = [
        TITLE,
        f"# 采集时间: {stamp}",
        f"# {head_ref()}",
        f"# python: {platform.python_version()} @ {sys.executable}",
        f"# 仪器 sha256[:{DIGEST_LEN}]: {digest_self()}",
        f"# command: python {SELF}",
        "# 副作用: 本仪器跑 `python scripts/quality/ported_security_evidence_build.py --force`，"
        "它会重写 AC-5|07 的 bundle",
        "#",
        "",
    ]
    (REPO / ARCHIVE).write_text("\n".join(lines) + "\n".join(OUT) + "\n", encoding="utf-8")
    return 0 if ok else 1


def git_stdout(*args: str) -> str:
    """One read-only git command, stdout stripped. One claim per call, never a mixed argv."""
    done = subprocess.run(  # noqa: S603  # nosec B603 B607  # read-only git, exec on PATH
        ["git", *args],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip()


def head_ref() -> str:
    """Branch and short revision, from two calls.

    ``rev-parse --abbrev-ref HEAD --short HEAD`` prints the branch twice and no commit, so the
    single-argv version of this header pinned nothing -- the provenance face was itself the bug.
    """
    return (
        f"分支: {git_stdout('rev-parse', '--abbrev-ref', 'HEAD')}"
        f"  HEAD: {git_stdout('rev-parse', '--short', 'HEAD')}"
    )


def digest_self() -> str:
    """sha256[:16] of this instrument's bytes."""
    return hashlib.sha256((REPO / SELF).read_bytes()).hexdigest()[:DIGEST_LEN]


if __name__ == "__main__":
    sys.exit(main())
