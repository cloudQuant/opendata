"""C73: prove the evidence-manifest digest allowlist in .gitleaks.toml has teeth.

Why this file exists
--------------------
The history scan reported 441 findings since commit 5080639 and the claim under triage is
that every one of them is a ``"<repo/path>.<ext>": "<sha256>"`` integrity pair inside an
archived gate manifest, not a credential. The scanner's own failure message prescribes an
allowlist as the remedy, and an allowlist that exempts more than it should is worse than a
red gate: this config's header records the accident of a wide ``^docs/`` entry once hiding a
real token literal in docs/evidence/.

So the claim tested here is a *differential*, never "the scan went quiet":

* [1] the real repo, pre-edit config vs post-edit config: the exemption must silence only
  findings that sit on a path->digest pair line, and must leave none of those;
* [2] a credential planted in ``docs/evidence/`` must survive the exemption -- the arm that
  fails loudly if anyone re-adds a ``paths`` condition, because gitleaks OR-s an allowlist's
  conditions together instead of AND-ing them;
* [3] a 64-hex value under a non-path key (``"api_key"``) must survive;
* [4] the known cost, measured: a path->64-hex pair outside docs/evidence also goes quiet;
* [5] the C36 SDMX exemption must still bite after this edit;
* [6] the rejected variant is re-measured, not merely recounted: an allowlist that adds
  ``paths = ['''^docs/evidence/''']`` to this shape swallows the planted token as well,
  which is the only proof of *why* path scoping stays out of the shipped config.

Arms [2]-[6] run in a throwaway git repo scanned in history mode, because the gate scans
history: ``--no-git`` hands gitleaks absolute file paths, and a scan over absolute paths
cannot show what a path condition would have done to the real run. An earlier revision of
this probe did use ``--no-git``, and its "path condition binds" arm passed on a fixture
digest that was 48 hex chars long - a line nothing could ever have exempted.

The probe copies the real SDMX fixture instead of reconstructing it, and its planted
credentials are reassembled from fragments at runtime -- this file sits inside the history
the scanner walks, so a literal copy would make the scan report our own probe.

Run: python docs/evidence/C73/gitleaks-counterfactual.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parents[3]
NEW_CONFIG = REPO / ".gitleaks.toml"


def line_hex_allowlist(config_text: str) -> str | None:
    """The global allowlist regex that exempts path->sha256 pairs, read out of a config.

    The probe measures the shipped string rather than a re-typed copy of it: an earlier
    revision carried its own transcription, and the two drifted apart (`*` against `{2,160}?`)
    without any arm noticing - only comparing against the file itself surfaced the gap.
    """
    parsed = tomllib.loads(config_text)
    hits = [
        regex
        for entry in parsed.get("allowlists", [])
        for regex in entry.get("regexes", [])
        if entry.get("regexTarget") == "line" and "[0-9a-f]{64}" in regex
    ]
    if len(hits) > 1:
        raise SystemExit(f"more than one digest-shaped line allowlist: {hits}")
    return hits[0] if hits else None


# The shape under test, taken from the config the gate actually runs. An absent allowlist is
# a hard stop, not an empty pattern: `re.compile("")` matches every line and would let the
# probe report "the exemption silences exactly the digest lines" against a config that has no
# exemption at all.
_SHIPPED_PAIR = line_hex_allowlist(NEW_CONFIG.read_text(encoding="utf-8"))
if _SHIPPED_PAIR is None:
    raise SystemExit(".gitleaks.toml carries no line-target 64-hex allowlist - nothing to measure")
PAIR = re.compile(_SHIPPED_PAIR)

# A file whose history findings are SDMX dataflow keys, for the regression arm.
SDMX_HIT = "tests/test_ecb_provider.py"

# The last commit whose .gitleaks.toml carries no digest allowlist. Pinned rather than read
# from HEAD, because HEAD moves past this round the moment the allowlist lands and the
# control would silently disappear from every later run.
CONTROL_REF = "5a23239"

# Tags the probe plants, as ``basename:line[rule]``, so each arm names one exact reading.
PLANTED_PAIR = "manifest.json:2[generic-api-key]"
PLANTED_SK = "manifest.json:3[generic-api-key]"
PLANTED_PAT = "leaky.py:1[generic-api-key]"
NON_PATH_HEX = "config.json:2[generic-api-key]"
PAIR_OUTSIDE = "integrity.json:2[generic-api-key]"


def _fake(*parts: str) -> str:
    """Reassemble a synthetic credential from fragments at runtime."""
    return "".join(parts)


def _pair(key: str, value: str) -> str:
    """One ``"key": "value"`` JSON line, so the shapes below stay readable."""
    return f'  "{key}": "{value}"\n'


def _exe(name: str) -> str:
    """Absolute path for a helper binary.

    A bare name would be a bandit finding (B607) and a real difference: the probe must run
    the same gitleaks the gate pins, not whatever a PATH edit points at.
    """
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"{name} is not on PATH")
    return found


def git(args: list[str], cwd: Path) -> str:
    """Run git in ``cwd`` and return stdout, failing loudly rather than continuing blind."""
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute binary, no shell
        [_exe("git"), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()[-400:]}")
    return result.stdout


def scan_history(source: Path, config: Path) -> tuple[int, list[dict[str, object]]]:
    """Scan ``source``'s git history the way the gate does and return exit plus findings."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        report = Path(handle.name)
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute binary, no shell
        [
            _exe("gitleaks"),
            "detect",
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
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        if result.returncode == 0:
            return 0, []
        log = (result.stdout + result.stderr).strip()
        raise SystemExit(f"gitleaks wrote no report (exit {result.returncode}):\n{log[-600:]}")
    payload = report.read_text(encoding="utf-8").strip()
    entries: list[dict[str, object]] = json.loads(payload) if payload else []
    report.unlink(missing_ok=True)
    return result.returncode, entries


def tags(entries: list[dict[str, object]]) -> set[str]:
    """Reduce findings to ``basename:line[rule]`` so two configs can be compared."""
    return {f"{Path(str(e['File'])).name}:{e['StartLine']}[{e['RuleID']}]" for e in entries}


def untriaged(source: Path, entries: list[dict[str, object]]) -> list[str]:
    """List findings whose reported line is not a path->digest pair, with the line text."""
    out: list[str] = []
    for e in entries:
        rel = str(e["File"])
        line_no = e.get("StartLine")
        path = Path(rel)
        if not path.is_absolute():
            path = source / rel
        text = ""
        if isinstance(line_no, int) and path.is_file():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            text = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        if not PAIR.search(text):
            out.append(f"{Path(rel).name}:{line_no} {text[:60]}")
    return out


def _stays(label: str, tag: str, before: set[str], after: set[str], failures: list[str]) -> None:
    """Assert one arm: the control must report ``tag`` and so must the allowlisted config."""
    print(f"  {label} {tag}: 旧={tag in before} 新={tag in after}")
    if tag not in before:
        failures.append(f"{label}: control does not report {tag} either, so the arm cannot differ")
    if tag not in after:
        failures.append(f"{label}: {tag} was exempted")


def main() -> int:
    """Differential run: pre-C73 config vs the digest allowlist, over six arms.

    Returns 1 when the exemption reaches anything beyond a path->digest pair line, or when
    an arm cannot differ; every printed section is one claim of the config comment.
    """
    failures: list[str] = []
    control_text = git(["show", f"{CONTROL_REF}:.gitleaks.toml"], REPO)
    shipped = NEW_CONFIG.read_text(encoding="utf-8")
    if control_text == shipped:
        raise SystemExit(f"control {CONTROL_REF} is identical to .gitleaks.toml - no control")
    if line_hex_allowlist(control_text) is not None:
        raise SystemExit(f"control {CONTROL_REF} exempts digest pairs - arm [1] cannot differ")

    print(f"[0] shape under test (read from .gitleaks.toml) = {PAIR.pattern}")

    # Exactly 64 lowercase hex: a shorter stand-in would not match the allowlist shape at
    # all, so the arms below would "pass" by reporting a line nothing could have exempted.
    digest64 = _fake(
        "9f3c1a7b",
        "2d4e05c8",
        "f61139ab",
        "cd7e5240",
        "f8b16a2c",
        "93e7d0f4",
        "5b8c1e3a",
        "70d2f964",
    )
    if not re.fullmatch(r"[0-9a-f]{64}", digest64):
        raise SystemExit(f"fixture digest must be 64 lowercase hex, got {len(digest64)} chars")

    with tempfile.TemporaryDirectory(prefix="c73-gitleaks-") as raw:
        root = Path(raw)
        control = root / "control.toml"
        control.write_text(control_text, encoding="utf-8")

        print("=== [1] 真实仓库全历史：旧配置命中须全部落在 path->摘要 行上，新配置须为 0 ===")
        code_before, before = scan_history(REPO, control)
        code_after, after = scan_history(REPO, NEW_CONFIG)
        b_tags, a_tags = tags(before), tags(after)
        offenders = untriaged(REPO, before)
        print(f"旧配置 exit={code_before} 命中={len(b_tags)}")
        print(f"  不在 path->摘要 行上的={len(offenders)}  样例={offenders[:3]}")
        print(f"新配置 exit={code_after} 命中={len(a_tags)}  样例={sorted(a_tags)[:3]}")
        print(f"允许面吞掉的命中数={len(b_tags) - len(a_tags)}")
        if not b_tags:
            failures.append("arm [1] cannot differ: the control reports nothing in history mode")
        if offenders:
            failures.append(f"findings that are not digest pairs are untriaged: {offenders[:3]}")
        if code_after != 0 or a_tags:
            failures.append(f"the allowlist leaves findings behind: {sorted(a_tags)[:3]}")

        probe = root / "probe"
        evidence = probe / "docs" / "evidence" / "C99"
        evidence.mkdir(parents=True)
        (probe / "src").mkdir(parents=True)
        (evidence / "manifest.json").write_text(
            "{\n"
            + _pair("opendata/services/api_key_service.py", digest64)
            + _pair("OPENAI_API_KEY", _fake("sk-proj-", "Q7kM2x", "VbQ7rT", "4wZn8H", "d3JfLc"))
            + "}\n",
            encoding="utf-8",
        )
        (evidence / "leaky.py").write_text(
            _pair("GITHUB_TOKEN", _fake("ghp_", "9kM2xVbQ7rT4wZn8Hd3JfLc5")), encoding="utf-8"
        )
        (evidence / "config.json").write_text(
            "{\n" + _pair("api_key", digest64) + "}\n", encoding="utf-8"
        )
        (probe / "src" / "integrity.json").write_text(
            "{\n" + _pair("tests/test_key_health.py", digest64) + "}\n", encoding="utf-8"
        )
        shutil.copy2(REPO / SDMX_HIT, probe / SDMX_HIT.replace("/", "__"))
        git(["init", "-q", "."], probe)
        git(["config", "user.email", "probe@example.invalid"], probe)
        git(["config", "user.name", "probe"], probe)
        git(["add", "-A"], probe)
        git(["commit", "-qm", "digest pairs plus planted credentials"], probe)
        _, pb = scan_history(probe, control)
        code_after, pa = scan_history(probe, NEW_CONFIG)
        b_tags, a_tags = tags(pb), tags(pa)
        print(f"\n探针仓库：旧配置命中={sorted(b_tags)}")
        print(f"探针仓库：新配置 exit={code_after} 命中={sorted(a_tags)}")

        print("\n=== [2] docs/evidence 里植入的真凭证必须仍然报出（paths 条件会在此失效）===")
        _stays("植入凭证", PLANTED_SK, b_tags, a_tags, failures)
        _stays("植入令牌", PLANTED_PAT, b_tags, a_tags, failures)
        print(
            f"  同档案内的摘要对 {PLANTED_PAIR}: 旧={PLANTED_PAIR in b_tags} "
            f"新={PLANTED_PAIR in a_tags}"
        )
        if PLANTED_PAIR not in b_tags:
            failures.append("arm [2] cannot show the pair being silenced: control misses it too")
        if PLANTED_PAIR in a_tags:
            failures.append(f"the digest pair beside them is still reported: {PLANTED_PAIR}")

        print("\n=== [3] 非路径键名下的 64 位十六进制必须仍然报出 ===")
        _stays("非路径键", NON_PATH_HEX, b_tags, a_tags, failures)

        print("\n=== [4] 已知代价：docs/evidence 之外的 path->摘要 也会被放过 ===")
        print(f"  {PAIR_OUTSIDE}: 旧={PAIR_OUTSIDE in b_tags} 新={PAIR_OUTSIDE in a_tags}")
        if PAIR_OUTSIDE not in b_tags:
            failures.append("arm [4] cannot differ: the control does not report it either")
        if PAIR_OUTSIDE in a_tags:
            print("     -> 允许面比登记的更窄，请同步更新 .gitleaks.toml 的已知边界说明")
        else:
            print("     -> 登记为允许面的代价（见 .gitleaks.toml 已知边界与本档）")

        print("\n=== [5] C36 的 SDMX 允许面必须不受本次改动影响 ===")
        sdmx = {t for t in a_tags if "ecb" in t}
        print(f"新配置对 SDMX 文件的命中={sorted(sdmx)}")
        if sdmx:
            failures.append(f"the C36 exemption regressed: {sorted(sdmx)}")

        print("\n=== [6] 被否决的写法：paths + 形状，证明 gitleaks 的允许面条件是「或」 ===")
        rejected = root / "rejected.toml"
        # The shape is taken from PAIR.pattern rather than retyped, so this arm measures
        # the shipped exemption plus a path condition and nothing else.
        rejected.write_text(
            control_text + "\n[[allowlists]]\n"
            'description = "rejected variant: path OR shape (measured, not shipped)"\n'
            "paths = ['''^docs/evidence/''']\n"
            'regexTarget = "line"\n'
            f"regexes = ['''{PAIR.pattern}''']\n",
            encoding="utf-8",
        )
        _, r_entries = scan_history(probe, rejected)
        r_tags = tags(r_entries)
        swallowed = sorted(b_tags - r_tags)
        print(f"paths 版命中={sorted(r_tags)}")
        print(f"被 paths 版吞掉、而形状版保住的={swallowed}")
        if PLANTED_PAT in r_tags:
            failures.append("arm [6] expected the paths variant to swallow the planted token")
        if not [t for t in swallowed if t not in (PAIR_OUTSIDE, PLANTED_PAIR)]:
            failures.append("the paths variant swallowed no credential - arm cannot differ")
        print("     -> 所以 .gitleaks.toml 不用 paths 收口；形状版的三类凭证仍全部报出（[2]/[3]）")

    print()
    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("OK: 允许面只吞掉 path->摘要 行；植入凭证仍报出，SDMX 允许面保持生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
