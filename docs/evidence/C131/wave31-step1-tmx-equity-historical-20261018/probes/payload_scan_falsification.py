r"""Prove the pre-commit payload scan can bite, so its zero is a measurement.

``staged_payload_scan.py`` prints ``payload_scan_findings=0`` for the wave's staged set. A zero
from a screen that cannot report anything is not evidence, so this probe scans throwaway trees
built from *this* wave's staged bytes with fabricated credential-shaped literals planted in them
and requires a non-zero finding count. Three arms, each with its expected value stated first:

``A_planted_high_entropy_token_reports``
    a fabricated ``ghp_``-shaped token whose body is an LCG sequence, plus an ``AKIA``-shaped id,
    in one fixture file, must be reported. Wave 30's first attempt at this arm used 40 identical
    characters and reported nothing: the shipped ruleset's entropy check, not any rule's regex, is
    what made a silent control -- which is why the entropy of every body is printed, not assumed.
``B_clean_copy_stays_clean``
    the same tree with no fixture must report 0, or arm A proves only that the tree is dirty
    rather than that the plant is what moved the counter.
``C_assembled_literal_stays_quiet``
    the same secret assembled with ``+`` from four nine-character literals must report 0. That is
    the shape ``.gitleaks.toml``:128-129 records this repo having to learn: encoding is not a
    carrier (gitleaks decodes one base64 layer) but assembly is, so this arm names the boundary of
    the screen instead of flattering it. If it ever stops reading 0, the boundary has moved.

Nothing here writes into the repo: the trees are ``mktemp`` copies of working-tree bytes, the
plants come from an LCG, and no planted literal is printed or stored -- only rule ids, file names,
entropy and counts.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
CONFIG = REPO_ROOT / ".gitleaks.toml"
CARRIER = (
    Path(__file__).resolve().parent.parent / "runs" / "wave31-step1-payload_scan_falsification.txt"
)


def _entropy(text: str) -> float:
    """Shannon entropy in bits per character, so arm A's premise is visible."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _fake_body(length: int) -> str:
    """High-entropy filler from an LCG: a fabricated shape, never a real secret."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    out: list[str] = []
    state = 20261018
    for _ in range(length):
        state = (1103515245 * state + 12345) % (1 << 31)
        out.append(alphabet[state % len(alphabet)])
    return "".join(out)


def _rule_ids(findings: list[dict]) -> list[str]:
    return sorted({str(item.get("RuleID")) for item in findings})


def _scan(files: list[Path], stage: Path, report: Path) -> tuple[list[dict] | None, int, str]:
    """Copy a file set into one throwaway tree, scan it, return (findings, rc, stderr tail).

    ``findings`` is ``None`` -- never an empty list -- when gitleaks produced no report, so a broken
    premise (missing config, bad argv) cannot be read by any arm as the zero it is contrasting.
    """
    for path in files:
        target = stage / path.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    if report.exists():
        report.unlink()
    result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, shell disabled
        [
            shutil.which("gitleaks") or "gitleaks",
            "detect",
            "--no-git",
            "--source",
            str(stage),
            "--config",
            str(CONFIG),
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    tail = " ".join(result.stderr.split())[-160:]
    if not report.exists():
        return None, result.returncode, tail
    return json.loads(report.read_text(encoding="utf-8")), result.returncode, tail


def _subject_files(limit: int) -> list[Path]:
    """Take working-tree bytes of the first ``limit`` staged paths as the scanned set."""
    git = shutil.which("git") or "git"
    listed = subprocess.run(  # noqa: S603  # nosec B603  # resolved absolute path, fixed argv
        [git, "-c", "core.quotepath=off", "diff", "--cached", "--name-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    names = [n for n in listed.stdout.splitlines() if n][:limit]
    return [REPO_ROOT / n for n in names if (REPO_ROOT / n).is_file()]


ARMS = (
    ("A_planted_high_entropy_token_reports", "plant", 1),
    ("B_clean_copy_stays_clean", "clean", 0),
    ("C_assembled_literal_stays_quiet", "assembled", 0),
)


def main() -> int:
    """Run the three arms and gate each on the expected value declared in the docstring."""
    if shutil.which("gitleaks") is None:
        print("falsification=SKIPPED reason=gitleaks-not-on-path")
        return 0
    hosts = _subject_files(3)
    body = _fake_body(36)
    aws_body = _fake_body(16)
    lines: list[str] = [
        "wave-31 step 1 -- does the pre-commit payload scan bite on THIS commit's staged bytes?",
        f"argv: PYTHONPATH= $PY {Path(__file__).resolve().relative_to(REPO_ROOT)}",
        "hosts: the first staged payload paths copied into an mktemp tree; the repo tree is never "
        "written. Planted literals come from an LCG and are not printed anywhere in this file.",
        f"repo_root={REPO_ROOT} config={CONFIG} config_exists={CONFIG.is_file()} "
        f"hosts={len(hosts)} host_tails={','.join(p.name for p in hosts)}",
        f"planted_body_entropy ghp={_entropy(body):.4f} akia={_entropy(aws_body):.4f} "
        "flat40=0.0000 -- arm A's premise is the entropy gate, not a rule regex.",
    ]
    if not CONFIG.is_file() or not hosts:
        lines.append(
            "premise_broken=1 reason=config_missing_or_no_staged_hosts "
            "-- no arm ran, because an arm that cannot reach a report reads as a false green."
        )
        lines.append("falsification_face arms=0 arms_passed=0 arms_failed=0 PASS=False")
        lines.append("falsification_exit=1")
        CARRIER.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for line in lines:
            print(line)
        print(f"carrier_written bytes={CARRIER.stat().st_size} lines={len(lines)}")
        return 1
    verdicts: list[bool] = []
    for name, kind, expected in ARMS:
        stage = Path(tempfile.mkdtemp(prefix=f"w31s1-{name[:5]}-"))
        report = stage.parent / f"w31s1-report-{name[:5]}.json"
        fixture = stage / "planted-fixture.py"
        try:
            if kind == "plant":
                fixture.write_text(
                    f'token = "ghp_{body}"\nsecondary = "AKIA{aws_body}"\n',
                    encoding="utf-8",
                )
            elif kind == "assembled":
                parts = " + ".join(f'"{body[i : i + 9]}"' for i in range(0, 36, 9))
                fixture.write_text(f'token = "ghp_" + {parts}\n', encoding="utf-8")
            findings, rc, tail = _scan(hosts, stage, report)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            report.unlink(missing_ok=True)
        want = f">={expected}" if expected else "0"
        if findings is None:
            lines.append(
                f"{name}: kind={kind} scanned={len(hosts) + (1 if kind != 'clean' else 0)} "
                f"findings=NO_REPORT gitleaks_rc={rc} expected={want} -> False "
                f"stderr_tail={tail or '(empty)'}"
            )
            verdicts.append(False)
            continue
        ok = len(findings) >= expected if expected else len(findings) == 0
        lines.append(
            f"{name}: kind={kind} scanned={len(hosts) + (1 if kind != 'clean' else 0)} "
            f"findings={len(findings)} rules={_rule_ids(findings) or '[]'} gitleaks_rc={rc} "
            f"expected={want} -> {ok}"
        )
        verdicts.append(ok)

    passed = sum(1 for ok in verdicts if ok)
    lines.append(
        f"falsification_face arms={len(verdicts)} arms_passed={passed} "
        f"arms_failed={len(verdicts) - passed} PASS={passed == len(verdicts)}"
    )
    lines.append("falsification_exit=0" if passed == len(verdicts) else "falsification_exit=1")
    CARRIER.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in lines:
        print(line)
    print(f"carrier_written bytes={CARRIER.stat().st_size} lines={len(lines)}")
    return 0 if passed == len(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
