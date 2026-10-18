"""Scan the exact staged commit payload for credentials, three ways, with a biting control.

Why this instrument exists: the wave record intends to claim "the staged payload was pre-scanned for
secrets". That claim is worth nothing as prose, and one green `gitleaks` run is worth little either
-- this repo's `.gitleaks.toml` is mostly allowlist text, so a zero can mean "nothing secret" or
"the allowlist swallowed it". This probe runs the shipped config, the same rules with
every allowlist table removed, and the built-in ruleset, over blobs pulled out of the
*index* (not the worktree), and
it plants a runtime-generated fake key into a throwaway copy of the same tree to show the channel
moves from 0 to 1. The planted literal is never printed, so this carrier cannot become the next
finding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROBES = Path(__file__).resolve().parent
if str(PROBES) not in sys.path:
    sys.path.insert(0, str(PROBES))

from take1_lint_preexistence import ruff_over  # noqa: E402
from w31s4_run_chain import PY, REPO, RUNS, carrier  # noqa: E402

CODE_FILES = [
    "opendata/data/providers/federal_reserve/models/fomc_documents.py",
    "scripts/quality/iter2_witnesses/_parts/federal_reserve/rows_a.py",
    "scripts/quality/iter2_witnesses/federal_reserve.py",
    "tests/test_federal_reserve_iter2_models.py",
]
ALLOWLIST_HEADER = re.compile(r"^\[\[?allowlist|^\[rules\.allowlist")
SECTION_HEADER = re.compile(r"^\[")


def git_bytes(*args: str) -> tuple[bytes, int]:
    """Run a read-only git plumbing command and hand back raw bytes, never decoded text."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed list argv, no shell
        ["git", *args],  # noqa: S607  # nosec B607  # a PATH lookup, never input
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    return proc.stdout, proc.returncode


def short(data: bytes) -> str:
    """Render the same 16-hex digest form the shard pins use."""
    return hashlib.sha256(data).hexdigest()[:16]


def staged_paths() -> list[str]:
    """Read the index-versus-HEAD payload exactly as ``git commit`` would take it."""
    out, rc = git_bytes("-c", "core.quotepath=off", "diff", "--cached", "--name-only")
    paths = sorted(line for line in out.decode("utf-8").splitlines() if line)
    print(f"staged_roster exit={rc} files={len(paths)}", flush=True)
    return paths


def committed_paths() -> list[str]:
    """Read the payload a commit already took, since after one the index is empty."""
    out, rc = git_bytes(
        "-c", "core.quotepath=off", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
    )
    paths = sorted(line for line in out.decode("utf-8").splitlines() if line)
    print(f"committed_roster exit={rc} files={len(paths)}", flush=True)
    return paths


def materialize(paths: list[str], dest: Path, blob_prefix: str) -> int:
    """Write every blob out of the chosen git object, so the scan reads commit-bound bytes."""
    written = 0
    for rel in paths:
        data, rc = git_bytes("show", f"{blob_prefix}{rel}")
        if rc != 0:
            print(f"MATERIALIZE_FAIL {rel} exit={rc}", flush=True)
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
    return written


def strip_allowlists(text: str) -> tuple[str, int]:
    """Drop every allowlist table while keeping the rule definitions intact."""
    kept: list[str] = []
    skipping = False
    dropped = 0
    for line in text.splitlines():
        if ALLOWLIST_HEADER.match(line):
            skipping = True
            continue
        if SECTION_HEADER.match(line):
            skipping = False
        if skipping:
            dropped += 1
            continue
        kept.append(line)
    return "\n".join(kept) + "\n", dropped


def run_gitleaks(label: str, source: Path, config: Path | None, report: Path) -> dict[str, object]:
    """Scan one tree and return only counts, rule ids and file names -- never the match text."""
    argv = [
        GITLEAKS or "gitleaks",
        "detect",
        "--no-git",
        "--source",
        str(source),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--exit-code",
        "0",
    ]
    if config is not None:
        argv += ["--config", str(config)]
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed list argv, no shell
        argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    findings = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else []
    rules = sorted({str(item.get("RuleID")) for item in findings})
    files = sorted({str(item.get("File", "")).split("/")[-1] for item in findings})
    shown = str(config).split("/")[-1] if config is not None else "builtin-ruleset"
    print(
        f"SCAN {label} config={shown} exit={proc.returncode} findings={len(findings)} "
        f"rules={rules} files={files} stderr_tail={last_stderr_line(proc.stderr)}",
        flush=True,
    )
    return {"findings": len(findings), "rules": rules, "files": files, "exit": proc.returncode}


def gitleaks_version() -> str:
    """Report the scanner build, since a findings count means nothing without its ruleset."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # fixed list argv
        [GITLEAKS or "gitleaks", "version"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def last_stderr_line(text: str) -> str:
    """Read gitleaks' summary line without crashing when it writes nothing."""
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1][6:] if lines else ""


GITLEAKS = shutil.which("gitleaks")


def main(argv: list[str]) -> int:
    """Price the payload three ways and gate on the planted control, over index or HEAD bytes."""
    source = argv[argv.index("--source") + 1] if "--source" in argv else "index"
    if source not in ("index", "head"):
        print(f"PRESCAN_ABORT unknown_source={source}", flush=True)
        return 2
    blob_prefix = ":" if source == "index" else "HEAD:"
    blob_label = "index" if source == "index" else "HEAD"
    face_name = "staged_payload_scan" if source == "index" else "committed_payload_scan"
    own_path = Path(__file__).resolve()
    if GITLEAKS is None:
        print("PRESCAN_ABORT gitleaks_binary=absent", flush=True)
        return 2
    work = Path(tempfile.mkdtemp(prefix="w31s4-scan-"))
    repo_config = REPO / ".gitleaks.toml"
    paths = staged_paths() if source == "index" else committed_paths()
    out: list[str] = [
        f"TOOL gitleaks_version={gitleaks_version()} repo_config_present={repo_config.is_file()}"
    ]

    for rel in CODE_FILES:
        index_data, index_rc = git_bytes("show", f"{blob_prefix}{rel}")
        disk = (REPO / rel).read_bytes()
        out.append(
            f"CODE {Path(rel).name} {blob_label}={short(index_data)} worktree={short(disk)} "
            f"{blob_label}_exit={index_rc} same={short(index_data) == short(disk)}"
        )
    missing_code = [rel for rel in CODE_FILES if rel not in paths]

    tree = work / "payload"
    tree.mkdir()
    copied = materialize(paths, tree, blob_prefix)

    shipped = run_gitleaks("shipped", tree, repo_config, work / "shipped.json")

    raw = repo_config.read_text(encoding="utf-8")
    stripped_text, dropped = strip_allowlists(raw)
    stripped_cfg = work / "no_allowlist.toml"
    stripped_cfg.write_text(stripped_text, encoding="utf-8")
    rules_kept = len(re.findall(r"^\[\[rules\]\]", stripped_text, re.MULTILINE))
    allowlist_free = run_gitleaks("allowlist_free", tree, stripped_cfg, work / "noallow.json")
    builtin = run_gitleaks("builtin", tree, None, work / "builtin.json")

    control_tree = work / "control"
    shutil.copytree(tree, control_tree)
    planted = "AKIA" + base64.b32encode(os.urandom(10)).decode().rstrip("=")[:16]
    (control_tree / "POSITIVE_CONTROL.txt").write_text(
        f"aws_access_key_id = {planted}\n", encoding="utf-8"
    )
    control = run_gitleaks("planted_control", control_tree, stripped_cfg, work / "control.json")

    self_codes, self_hits, self_ok = ruff_over(
        own_path.read_text(encoding="utf-8"), "H_self_lint", filename=str(own_path)
    )
    shutil.rmtree(work, ignore_errors=True)

    out.append(
        f"A_{'staged' if source == 'index' else 'committed'}_roster count={len(paths)} "
        f"code_files_in_roster={not missing_code} "
        f"missing={missing_code or '[]'}"
    )
    out.append(
        f"B_blobs_from_{'index' if source == 'index' else 'HEAD'} copied={copied} of={len(paths)} "
        f"reading=the scan read {blob_label} bytes, "
        "so a worktree-only edit cannot hide behind it"
    )
    out.append(
        f"C_scan_shipped findings={shipped['findings']} exit={shipped['exit']} "
        "config=.gitleaks.toml (as shipped, allowlists active)"
    )
    out.append(
        f"D_scan_allowlist_free findings={allowlist_free['findings']} "
        f"exit={allowlist_free['exit']} raw_lines={len(raw.splitlines())} "
        f"stripped_lines={len(stripped_text.splitlines())} lines_dropped={dropped} "
        f"rules_kept={rules_kept} config_actually_changed={stripped_text != raw}"
    )
    out.append(f"E_scan_builtin findings={builtin['findings']} exit={builtin['exit']}")
    out.append(
        f"F_planted_control findings={control['findings']} files={control['files']} "
        "reading=the argv that reads 0 over the payload must read 1 once a fake key is planted"
    )
    leaked = planted in json.dumps([shipped, allowlist_free, builtin, control], default=str)
    out.append(
        f"G_planted_literal_not_printed leaked={leaked} "
        "reading=no line of this carrier can carry the planted token"
    )
    out.append(
        f"H_self_lint codes={dict(sorted(self_codes.items()))} hits={self_hits} "
        f"parser_agrees={self_ok} bytes={len(own_path.read_bytes())}"
    )

    gates = {
        "A_roster_nonempty_and_covers_code": bool(paths) and not missing_code,
        f"B_copied_equals_{'staged' if source == 'index' else 'committed'}": copied == len(paths),
        "C_shipped_clean": shipped["findings"] == 0,
        "D_strip_actually_changed_config": stripped_text != raw and rules_kept > 0,
        "D_allowlist_free_clean": allowlist_free["findings"] == 0,
        "E_builtin_clean": builtin["findings"] == 0,
        "F_control_bit": control["findings"] == 1 and control["files"] == ["POSITIVE_CONTROL.txt"],
        "G_literal_not_carried": not leaked,
        "H_probe_itself_rule_clean": not self_codes and self_ok,
    }
    all_pass = all(gates.values())
    out.append(
        f"prescan_face=arms={len(gates)} arms_passed={sum(gates.values())} gates={gates} "
        f"all_pass={all_pass}"
    )

    body = "\n".join(out)
    exit_code = 0 if all_pass else 1
    path = RUNS / f"wave31-step4-{face_name}.txt"
    argv_tail = [] if source == "index" else ["--source", source]
    path.write_text(
        carrier(
            {
                "name": face_name,
                "argv": [PY, str(own_path), *argv_tail],
                "pythonpath": ".",
                "exit": exit_code,
                "stderr": "",
                "stdout": body,
                "note": (
                    "credential pre-scan over the staged index payload: shipped config, every "
                    "allowlist table removed, and the built-in ruleset, plus a runtime-planted "
                    "fake key that must move the count from 0 to 1"
                    if source == "index"
                    else "the same three scans re-run over the bytes commit 623c83a2 took into "
                    "HEAD, because after a commit the index is empty and the index-mode face "
                    "can only read a true zero roster"
                ),
            }
        ),
        encoding="utf-8",
    )
    print(body, flush=True)
    print(f"carrier_written bytes={path.stat().st_size} path={path.relative_to(REPO)}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
