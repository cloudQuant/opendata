"""C36: prove the SDMX dataflow-key allowlist in .gitleaks.toml has teeth.

Why this file exists
--------------------
The history scan went from `leaks found: 5` to `no leaks found` by adding an
allowlist. An allowlist that exempts too much is worse than a red CI: it turns a real
leak into a clean run. So the claim under test is not "the scan is quiet" - it is a
*differential*: against the pre-C36 config, exactly the public dataflow shapes must go
silent and nothing else may.

The probe copies the four real files that produced the findings rather than
reconstructing them: an earlier version of this script rewrote the lines from their
masked views and only tripped 2 of the 5 rules, which would have let a *weaker* allowlist
look like a working one. Copying keeps the trigger content authentic and keeps the
values out of this transcript (`--redact` is on, and nothing is printed but file names).

The planted credentials are synthetic and reassembled from fragments at runtime, so
they never appear whole in this file - this file lives in docs/evidence/, i.e. inside
the git history the scanner walks, and a literal copy would make the history scan
report our own probe.

Run: python docs/evidence/C36/gitleaks-counterfactual.py
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
NEW_CONFIG = REPO / ".gitleaks.toml"

# The files whose lines the history scan reported (C14/C16 fixtures and the patrol
# probe table). `.env` is deliberately not here: it is gitignored, so it is not part of
# the version library the acceptance item is about - and `--no-git` would report it.
REAL_HITS = (
    "scripts/ops/macro_authority_cross_check.py",
    "opendata/pipeline/patrol.py",
    "tests/test_oecd_provider.py",
    "tests/test_ecb_provider.py",
)

EXPECTED_REAL_HITS = 4


def _fake(*parts: str) -> str:
    """Reassemble a synthetic credential from fragments at runtime."""
    return "".join(parts)


def _assignment(name: str, value: str) -> str:
    """One ``NAME = "value"`` line, so the shape below stays readable."""
    return f'{name} = "{value}"\n'


def _exe(name: str) -> str:
    """Absolute path for a helper binary.

    Partial paths would be a bandit finding (B607) *and* a real difference: the probe
    must run the same gitleaks the gate pins, not whatever a PATH edit points at.
    """
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"{name} is not on PATH")
    return found


# Long enough and shaped so that gitleaks' own rules fire; the control run below prints
# how many actually did, so this is measured rather than assumed.
PLANTED = {
    "aws_access_key.py": _assignment(
        "AWS_ACCESS_KEY_ID", _fake("AKIA", "Z4MRQ", "7VSTX", "EGHJ2", "L") + "X"
    ),
    "github_pat.py": _assignment(
        "GITHUB_TOKEN", _fake("ghp_", "9kM2x", "VbQ7r", "T4wZn", "8Hd3", "JfLc5", "Ys1A")
    ),
    "generic_api_key.py": _assignment(
        "FUYAO_API_KEY", _fake("sk_liv", "e7Kd9Q", "z2VbN4", "xRt8Wm", "1Zc6Jh", "3Lp0Yr")
    ),
    "bearer_in_url.py": _assignment(
        "URL",
        "https://api.example.com/v1/q?access_token="
        + _fake("a1b2c3", "d4e5f6", "g7h8i9", "j0k1l2"),
    ),
}

# The copies land in one directory, so these prefixes identify "a real file is still
# being reported" in the mixed-tree case.
REAL_PREFIXES = ("opendata__", "scripts__", "tests__")

# The exemption is shape-based, so an all-uppercase dotted value can slip through it.
# Measured and printed instead of left implicit (README "已知边界").
BOUNDARY = _assignment("API_KEY", _fake("ABCDEFGH.", "IJKLMN0P.", "QRSTUVW.", "XYZ0123"))


def scan(source: Path, config: Path) -> tuple[int, list[str]]:
    """Run gitleaks the way CI does (same flags, redacted) and return exit + findings."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report = Path(handle.name)
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute binary, no shell
        [
            _exe("gitleaks"),
            "detect",
            "--no-git",
            "--source",
            str(source),
            "--config",
            str(config),
            "--redact",
            "-f",
            "json",
            "-r",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        log = (result.stdout + result.stderr).strip()
        raise SystemExit(f"gitleaks wrote no report (exit {result.returncode}):\n{log[-800:]}")
    payload = report.read_text(encoding="utf-8").strip()
    entries = json.loads(payload) if payload else []
    findings = sorted(f"{Path(e['File']).name}:{e['StartLine']}[{e['RuleID']}]" for e in entries)
    report.unlink(missing_ok=True)
    return result.returncode, findings


def main() -> int:
    """Differential run: pre-C36 config vs the allowlist, over four trees.

    Returns 1 when the allowlist exempts anything beyond the real dataflow shapes;
    the printed sections are the readings, and every claim in the README is one of them.
    """
    failures: list[str] = []
    control_text = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, no shell
        [_exe("git"), "show", "HEAD:.gitleaks.toml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    with tempfile.TemporaryDirectory(prefix="c36-gitleaks-") as raw:
        root = Path(raw)
        control = root / "control.toml"
        control.write_text(control_text, encoding="utf-8")
        if control_text == NEW_CONFIG.read_text(encoding="utf-8"):
            raise SystemExit(
                "HEAD's config already carries this round's allowlist - the control is gone"
            )

        print("=== [0] 本脚本自身（含分段拼接的假凭证）过新配置 -> 期望 0 命中 ===")
        code, findings = scan(Path(__file__).parent, NEW_CONFIG)
        print(f"exit={code} findings={findings}")
        if code != 0:
            failures.append("the counterfactual trips its own scanner")

        real = root / "real"
        real.mkdir()
        for rel in REAL_HITS:
            dest = real / rel.replace("/", "__")
            shutil.copy2(REPO / rel, dest)
        _, before = scan(real, control)
        code_after, after = scan(real, NEW_CONFIG)
        print("\n=== [1] 真实文件（历史命中的那 4 个）：旧配置 vs 新配置 ===")
        print(f"旧配置命中 {len(before)} 条: {before}")
        print(f"新配置 exit={code_after} 命中 {len(after)} 条: {after}")
        if len(before) != EXPECTED_REAL_HITS:
            failures.append(f"expected {EXPECTED_REAL_HITS} authentic findings, got {before}")
        if code_after != 0 or after:
            failures.append(f"the allowlist did not cover every real finding: {after}")

        planted_dir = root / "planted"
        planted_dir.mkdir()
        for name, body in PLANTED.items():
            (planted_dir / name).write_text(body, encoding="utf-8")
        _, before = scan(planted_dir, control)
        code_after, after = scan(planted_dir, NEW_CONFIG)
        print("\n=== [2] 假凭证：旧配置命中集合必须与新配置逐条相同（不得连带吞掉）===")
        print(f"旧配置命中 {len(before)} 条: {before}")
        print(f"新配置 exit={code_after} 命中 {len(after)} 条: {after}")
        if len(before) < 3:
            failures.append(
                f"the planted probe only trips {len(before)} rules - too weak to conclude"
            )
        if code_after == 0 or before != after:
            failures.append(f"collateral exemption: before={before} after={after}")

        mixed = root / "mixed"
        mixed.mkdir()
        for src in sorted(real.iterdir()):
            shutil.copy2(src, mixed / src.name)
        for name, body in PLANTED.items():
            (mixed / name).write_text(body, encoding="utf-8")
        code_after, after = scan(mixed, NEW_CONFIG)
        print("\n=== [3] 同一棵树混放 -> 只剩假凭证，真实文件不得出现 ===")
        print(f"exit={code_after} 命中 {after}")
        real_still_reported = [f for f in after if any(p in f for p in REAL_PREFIXES)]
        if code_after == 0 or real_still_reported:
            failures.append(f"mixed tree misread: {after}")

        boundary = root / "boundary"
        boundary.mkdir()
        (boundary / "boundary.py").write_text(BOUNDARY, encoding="utf-8")
        _, before = scan(boundary, control)
        code_after, after = scan(boundary, NEW_CONFIG)
        print("\n=== [4] 已知边界：全大写带点的 4 段值 ===")
        print(f"旧配置命中 {before} / 新配置 exit={code_after} 命中 {after}")
        print("     -> 新配置会放过这种形状；登记为允许面的代价，见 README「已知边界」")

    print()
    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: 允许面只吞掉真实文件里的 SDMX 形状，假凭证一条不少地仍然报出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
