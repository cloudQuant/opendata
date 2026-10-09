"""Rebuild the ported Bandit scan and rule-review bundle against the tree as it ships now.

``ported_security_evidence.validate`` reads a bundle in the identity space it was written in, and
its freshness window is 24 hours, so any acceptance round older than that window -- or any
relocation of the ported root -- invalidates the bundle by construction. This tool re-scans the
shipped ported tree with Bandit and carries a prior round's rule-level review forward only when the
current findings are the same objects that review classified:

* every rule's count equals the prior review's count, so the numbers embedded in the review basis
  prose stay literally true;
* no rule appears that the prior round never reviewed;
* every ``path.py:12`` style locator inside the carried prose resolves in the current tree, and a
  rule's locators resolve to findings of that same rule.

Anything else raises ``BuildError`` instead of writing a bundle. The review itself is not re-done
here: dispositions, bases and follow-ups are carried verbatim from the prior round, and the
reviewer block (including the user's direct-review authorization) is copied, never rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404  # literal argv, shell disabled
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.quality import ported_security_evidence as evidence  # noqa: E402

#: Bundles of earlier rounds are the record of what was reviewed then; this tool may read them but
#: never writes into them.
PROTECTED_REL: Final = ("docs/evidence/C65", "docs/evidence/C73", "docs/evidence/A2")

#: ``path/to/file.py:12``, ``file.py:22,29`` and ``file.py:149/196/249`` all appear in the
#: carried review prose, so every one of them has to resolve in the current tree.
CITATION_RE: Final = re.compile(r"([A-Za-z0-9_./-]+\.py):(\d{1,6}(?:\s*(?:/|,|、)\s*\d{1,6})*)")
SEPARATORS_RE: Final = re.compile(r"\s*[/,、]\s*")


class BuildError(RuntimeError):
    """Raised for any condition that would make a rebuilt bundle overstate the review."""


def _dump(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _relative_posix(root: Path, path: Path) -> str:
    return PurePosixPath(path.relative_to(root).as_posix()).as_posix()


def _bandit_identity(root: Path, target_rel: str) -> tuple[int, list[dict[str, Any]], str, str]:
    """Run Bandit exactly as the shipped tree is scanned today and return its raw findings."""
    with tempfile.TemporaryDirectory(prefix="ported-bandit-") as tmp:
        raw_path = Path(tmp) / "bandit-raw.json"
        argv = [
            sys.executable,
            "-m",
            "bandit",
            "-q",
            "-r",
            target_rel,
            "-f",
            "json",
            "-o",
            str(raw_path),
        ]
        completed = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            argv,
            cwd=str(root),
            shell=False,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        if not raw_path.is_file():
            tail = [line for line in completed.stderr.splitlines() if line.strip()]
            raise BuildError(
                f"bandit wrote no JSON (exit {completed.returncode}): "
                f"{tail[-1] if tail else '(no stderr)'}"
            )
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    version = subprocess.run(  # nosec B603  # same fixed argv family
        [sys.executable, "-m", "bandit", "--version"],
        cwd=str(root),
        shell=False,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout
    scanner_version = version.splitlines()[0].strip().split()[-1] if version.strip() else ""
    python_version = ".".join(str(part) for part in sys.version_info[:3])
    errors = payload.get("errors")
    if not isinstance(errors, list) or errors:
        raise BuildError(f"bandit reported {len(errors or [])} scan errors; nothing to certify")
    results = payload.get("results")
    if not isinstance(results, list):
        raise BuildError("bandit JSON has no results list")
    return completed.returncode, results, scanner_version, python_version


def _sanitize(root: Path, target_rel: str, results: list[Any]) -> list[dict[str, Any]]:
    """Keep only the declared redacted keys and rewrite filenames into repo-relative POSIX."""
    findings: list[dict[str, Any]] = []
    for value in results:
        if not isinstance(value, dict):
            raise BuildError("bandit result row is not an object")
        raw_name = value.get("filename")
        if not isinstance(raw_name, str):
            raise BuildError("bandit result row has no filename")
        candidate = PurePosixPath(raw_name)
        if candidate.is_absolute():
            try:
                relative = _relative_posix(root, Path(candidate))
            except ValueError as error:
                raise BuildError(f"bandit filename outside the repository: {raw_name}") from error
        else:
            relative = candidate.as_posix()
        if not relative.startswith(f"{target_rel}/"):
            raise BuildError(f"bandit finding is outside the ported root: {relative}")
        findings.append(
            {
                "filename": relative,
                "test_id": value.get("test_id"),
                "test_name": value.get("test_name"),
                "issue_severity": value.get("issue_severity"),
                "issue_confidence": value.get("issue_confidence"),
                "line_number": value.get("line_number"),
                "line_range": value.get("line_range"),
            }
        )
    return findings


def _source_manifest(root: Path, target_rel: str) -> tuple[dict[str, str], str, int]:
    """Digest the ported tree the way the validator does: sorted path, NUL, bytes, NUL."""
    target = root / target_rel
    if not target.is_dir():
        raise BuildError(f"ported root is missing: {target_rel}")
    paths = sorted(
        _relative_posix(root, path)
        for path in target.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    hashes: dict[str, str] = {}
    hasher = hashlib.sha256()
    for relative in paths:
        content = (root / relative).read_bytes()
        hashes[relative] = hashlib.sha256(content).hexdigest()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(content)
        hasher.update(b"\0")
    return hashes, hasher.hexdigest(), len(paths)


def _cited_lines(raw: str) -> list[int]:
    return [int(part.strip()) for part in SEPARATORS_RE.split(raw) if part.strip()]


def _resolve_citations(
    root: Path, target_rel: str, text: str, *, allowed_lines: set[tuple[str, int]] | None
) -> tuple[list[str], list[str]]:
    """Check every prose locator against the current tree; return (resolved, rejected)."""
    resolved: list[str] = []
    rejected: list[str] = []
    for file_rel, numbers in CITATION_RE.findall(text):
        suffix = file_rel.split(f"{target_rel}/", 1)[-1]
        matches = sorted(
            path
            for path in (root / target_rel).rglob(Path(suffix).name)
            if _relative_posix(root, path).endswith(suffix)
        )
        if len(matches) != 1:
            rejected.append(f"{file_rel} (no unique file under {target_rel})")
            continue
        line_count = len(matches[0].read_text(encoding="utf-8").splitlines())
        for line in _cited_lines(numbers):
            label = f"{suffix}:{line}"
            if line > line_count:
                rejected.append(f"{label} (file has {line_count} lines)")
            elif allowed_lines is not None and (suffix, line) not in allowed_lines:
                rejected.append(f"{label} (no finding of this rule there)")
            else:
                resolved.append(label)
    return resolved, rejected


def _dropped_root(paths: set[str], depth: int) -> str:
    heads = {"/".join(path.split("/")[:depth]) for path in paths}
    return heads.pop() if len(heads) == 1 else "mixed"


def _align_roots(prior_paths: set[str], current_paths: set[str]) -> tuple[int, str]:
    """Find how many leading components the carried root had.

    A relocation preserves every path below the ported root, so exactly one drop-depth should make
    the carried file structure equal to the current one. Matching on path *tails* instead would let
    ``request.py`` resolve against both ``request.py`` and ``utils/request.py``.
    """
    strip = len(evidence.PORT_ROOT.split("/"))
    relative_current = {"/".join(path.split("/")[strip:]) for path in current_paths}
    max_depth = min(len(path.split("/")) for path in prior_paths) - 1
    for depth in range(1, max_depth + 1):
        relative_prior = {"/".join(path.split("/")[depth:]) for path in prior_paths}
        if relative_prior == relative_current:
            return depth, _dropped_root(prior_paths, depth)
    raise BuildError(
        "the carried bundle's paths do not line up with the current ported tree at any root depth"
    )


def _identity(rule: object, line: object, label: str) -> tuple[str, str, int]:
    if not isinstance(line, int) or line < 1:
        raise BuildError(f"finding row has no usable line number: {label}:{line!r}")
    return label, str(rule), line


def _verify_identity_coverage(
    prior: dict[str, Any], findings: list[dict[str, Any]]
) -> dict[str, Any]:
    """Require the current findings to be the same objects the carried review classified."""
    prior_findings = prior.get("findings")
    if not isinstance(prior_findings, list) or not prior_findings:
        raise BuildError("prior triage has no finding rows to carry")
    prior_rows = [row for row in prior_findings if isinstance(row, dict)]
    prior_paths = {str(row.get("filename", "")) for row in prior_rows}
    if any(not path for path in prior_paths):
        raise BuildError("prior triage has a finding row without a filename")
    current_paths = {str(row["filename"]) for row in findings}
    depth, root_label = _align_roots(prior_paths, current_paths)
    strip = len(evidence.PORT_ROOT.split("/"))
    wanted = {
        _identity(
            row.get("test_id"),
            row.get("line_number"),
            "/".join(str(row["filename"]).split("/")[depth:]),
        )
        for row in prior_rows
    }
    actual = {
        _identity(
            row.get("test_id"),
            row.get("line_number"),
            "/".join(str(row["filename"]).split("/")[strip:]),
        )
        for row in findings
    }
    only_prior = sorted(wanted - actual)
    only_actual = sorted(actual - wanted)
    if only_prior or only_actual:
        raise BuildError(
            f"current findings are not the objects the prior round reviewed (dropped root "
            f"{root_label}): dropped={only_prior[:5]} added={only_actual[:5]} "
            f"(prior {len(wanted)}, current {len(actual)})"
        )
    return {
        "prior_root": root_label,
        "prior_rows": len(prior_rows),
        "current_rows": len(findings),
        "identities": len(actual),
    }


def _carry_rule_reviews(
    root: Path,
    prior: dict[str, Any],
    fresh_counts: dict[str, int],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Carry each reviewed rule group forward only when its counts and locators still hold."""
    prior_rules = prior.get("rules")
    if not isinstance(prior_rules, dict):
        raise BuildError("prior triage has no rules map to carry")
    unreviewed = sorted(set(fresh_counts) - set(prior_rules))
    if unreviewed:
        raise BuildError(f"rules with no prior review: {', '.join(unreviewed)}")
    dropped = sorted(set(prior_rules) - set(fresh_counts))
    if dropped:
        raise BuildError(
            f"prior review covers rules absent from the current scan: {', '.join(dropped)}"
        )
    carried: dict[str, Any] = {}
    citation_log: list[str] = []
    for rule, fresh_count in sorted(fresh_counts.items()):
        summary = prior_rules[rule]
        if not isinstance(summary, dict):
            raise BuildError(f"prior review for {rule} is not an object")
        if summary.get("count") != fresh_count:
            raise BuildError(
                f"{rule}: prior review classified {summary.get('count')} findings, "
                f"the current tree has {fresh_count}; the basis prose quotes that number"
            )
        basis = str(summary.get("basis", ""))
        allowed = {
            (finding["filename"].split(f"{evidence.PORT_ROOT}/", 1)[-1], finding["line_number"])
            for finding in findings
            if finding["test_id"] == rule
        }
        resolved, rejected = _resolve_citations(
            root, evidence.PORT_ROOT, basis, allowed_lines=allowed
        )
        if rejected:
            raise BuildError(
                f"{rule}: basis prose cites locations the scan disagrees with: {rejected}"
            )
        citation_log.extend(f"{rule} {item}" for item in resolved)
        carried[rule] = {
            "count": fresh_count,
            "disposition": summary.get("disposition"),
            "basis": basis,
            "follow_up": str(summary.get("follow_up", "")),
        }
    return carried, citation_log


def _carry_js_review(root: Path, prior: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Re-check the supplementary JavaScript review's coordinates in the shipped tree."""
    block = prior.get("additional_javascript_review")
    if not isinstance(block, dict):
        raise BuildError("prior triage has no JavaScript review to carry")
    carried: dict[str, Any] = {}
    log: list[str] = []
    for key, value in sorted(block.items()):
        if not isinstance(value, str):
            raise BuildError(f"JavaScript review entry {key} is not text")
        if key == "decision":
            carried[key] = value
            continue
        resolved, rejected = _resolve_citations(root, evidence.PORT_ROOT, value, allowed_lines=None)
        if rejected:
            raise BuildError(f"JavaScript review entry {key} no longer resolves: {rejected}")
        carried[key] = value
        log.extend(f"{key} {item}" for item in resolved)
    carried["locator_space"] = (
        f"坐标相对 {evidence.PORT_ROOT}，本轮逐条核对过：文件存在且行号未越界"
    )
    return carried, log


def build_bundle(
    root: Path,
    *,
    round_label: str,
    out_dir_rel: str,
    prior_triage_rel: str,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Scan the shipped ported tree and write a scan+triage bundle under ``out_dir_rel``."""
    if not re.fullmatch(r"C\d+", round_label):
        raise BuildError(f"round label must look like C74, got {round_label!r}")
    relative_out = PurePosixPath(out_dir_rel).as_posix().rstrip("/")
    for protected in PROTECTED_REL:
        if relative_out.startswith(protected):
            raise BuildError(f"refusing to write into a prior round's archive: {protected}")
    prior_path = root / prior_triage_rel
    if not prior_path.is_file():
        raise BuildError(f"prior triage to carry is missing: {prior_triage_rel}")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))

    target_rel = evidence.PORT_ROOT
    generated_at = (now or datetime.now(timezone.utc)).isoformat()
    exit_code, raw_results, scanner_version, python_version = _bandit_identity(root, target_rel)
    if scanner_version != evidence.EXPECTED_SCANNER_VERSION:
        raise BuildError(
            f"bandit is {scanner_version or 'unknown'}, the judge requires "
            f"{evidence.EXPECTED_SCANNER_VERSION}"
        )
    findings = _sanitize(root, target_rel, raw_results)
    if len({(row["filename"], row["test_id"], row["line_number"]) for row in findings}) != len(
        findings
    ):
        raise BuildError("bandit repeated a finding identity; the judge would reject the bundle")
    rule_counts: dict[str, int] = {}
    for row in findings:
        rule_counts[str(row["test_id"])] = rule_counts.get(str(row["test_id"]), 0) + 1
    high_count = sum(row["issue_severity"] == "HIGH" for row in findings)
    expected_exit = 1 if findings else 0
    if exit_code != expected_exit:
        raise BuildError(
            f"bandit exited {exit_code} with {len(findings)} findings; expected {expected_exit}"
        )

    hashes, source_hash, python_files = _source_manifest(root, target_rel)
    identity = _verify_identity_coverage(prior, findings)
    carried_rules, rule_citations = _carry_rule_reviews(root, prior, rule_counts, findings)
    js_review, js_citations = _carry_js_review(root, prior)

    scan_document: dict[str, Any] = {
        "archive_round": round_label,
        "generated_at": generated_at,
        "produced_by": (
            f"{Path(sys.executable).name} -m bandit -q -r {target_rel} -f json"
            f" (bandit {scanner_version}, python {python_version})"
        ),
        "scanner_version": scanner_version,
        "python_version": python_version,
        "scanner_exit": exit_code,
        "source_hash_algorithm": evidence.EXPECTED_SOURCE_HASH_ALGORITHM,
        "source_sha256": source_hash,
        "source_files_sha256": hashes,
        "python_files": python_files,
        "scan": {"errors": [], "results": findings},
        "raw_content_redacted": ["code", "issue_text", "more_info"],
        "rule_counts": rule_counts,
        "ported_root": target_rel,
    }

    out_dir = root / relative_out
    scan_rel = out_dir / "ported-bandit-scan.json"
    triage_rel = out_dir / "ported-security-triage.json"
    if not force:
        for path in (scan_rel, triage_rel):
            if path.exists():
                raise BuildError(
                    f"{path.relative_to(root)} exists; pass --force to rewrite this round"
                )
    out_dir.mkdir(parents=True, exist_ok=True)
    scan_rel.write_text(_dump(scan_document), encoding="utf-8")
    scan_sha256 = hashlib.sha256(scan_rel.read_bytes()).hexdigest()

    b113_count = rule_counts.get("B113", 0)
    triage_document: dict[str, Any] = {
        "archive_round": round_label,
        "generated_at": generated_at,
        "reviewer": prior["reviewer"],
        "review_scope": (
            f"完整扫描结果按{len(carried_rules)}规则组分类，并核对{high_count} HIGH、pickle路径、"
            f"远端eval、JS及注册入口；不是逐行复审全部{b113_count}个HTTP调用"
        ),
        "review_complete": True,
        "security_fixed": False,
        "unresolved_risks": True,
        "source_sha256": source_hash,
        "scan_sha256": scan_sha256,
        "python_files": python_files,
        "finding_count": len(findings),
        "high_count": high_count,
        "rules": carried_rules,
        "findings": [
            {
                "test_id": row["test_id"],
                "filename": row["filename"],
                "line_number": row["line_number"],
                "issue_severity": row["issue_severity"],
                "disposition": carried_rules[str(row["test_id"])]["disposition"],
                "basis_rule": row["test_id"],
            }
            for row in findings
        ],
        "source_preservation_basis": str(prior.get("source_preservation_basis", "")),
        "additional_javascript_review": js_review,
        "carried_from": {
            "triage": prior_triage_rel,
            "prior_round": prior.get("archive_round"),
            "prior_root": identity["prior_root"],
            "prior_rows": identity["prior_rows"],
            "current_rows": identity["current_rows"],
            "identities_matched": identity["identities"],
            "identity_check": (
                f"本轮 {identity['current_rows']} 条发现与 {identity['prior_rows']} 条上一轮记录按 "
                f"(去掉 {identity['prior_root']}/ 的相对路径, 规则, 行号) 逐条比对，"
                "增减均为 0，所以按规则组的既有处置可以结转；处置文本本轮未重写"
            ),
            "rule_citations_verified": len(rule_citations),
            "javascript_citations_verified": len(js_citations),
        },
    }
    if prior.get("source_preservation_basis") is None:
        triage_document.pop("source_preservation_basis")
    triage_rel.write_text(_dump(triage_document), encoding="utf-8")

    return {
        "round": round_label,
        "scan": scan_rel.relative_to(root).as_posix(),
        "triage": triage_rel.relative_to(root).as_posix(),
        "python_files": python_files,
        "finding_count": len(findings),
        "high_count": high_count,
        "rule_count": len(rule_counts),
        "rules": ", ".join(sorted(rule_counts)),
        "scanner_version": scanner_version,
        "scanner_exit": exit_code,
        "source_sha256": source_hash,
        "scan_sha256": scan_sha256,
        "carried_from_round": prior.get("archive_round"),
        "carried_prior_rows": identity["prior_rows"],
        "identities_matched": identity["identities"],
        "rule_citations_verified": len(rule_citations),
        "javascript_citations_verified": len(js_citations),
    }


def main(argv: list[str] | None = None) -> int:
    """Scan, carry and write one round's bundle; exit 2 when the carry condition does not hold."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=_REPO_ROOT, type=Path)
    parser.add_argument("--round", dest="round_label", default="C74")
    parser.add_argument("--out-dir", default="docs/evidence/C74")
    parser.add_argument("--prior-triage", default="docs/evidence/C65/ported-security-triage.json")
    parser.add_argument("--force", action="store_true", help="rewrite this round's bundle")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        facts = build_bundle(
            root,
            round_label=args.round_label,
            out_dir_rel=args.out_dir,
            prior_triage_rel=args.prior_triage,
            force=args.force,
        )
    except BuildError as error:
        print(f"BUILD_REFUSED {error}", file=sys.stderr)
        print(json.dumps({"built": False, "reason": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"built": True, **facts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
