"""Validate current Bandit and triage evidence for the source-preserved ported tree."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.quality.source_layout import VENDOR_ROOT

EVIDENCE_DIR = Path("docs/evidence/C74")
SCAN_REL = EVIDENCE_DIR / "ported-bandit-scan.json"
TRIAGE_REL = EVIDENCE_DIR / "ported-security-triage.json"
#: The root the bundle is read in. C65 wrote its scan under the retired ``opendata_http`` name, and
#: every clause below reads paths in that identity space. A relocation of the ported tree -- or a
#: bundle older than ``MAX_EVIDENCE_AGE`` -- has to be answered by re-running
#: ``scripts/quality/ported_security_evidence_build.py`` for the new round, never by loosening a
#: check.
PORT_ROOT = VENDOR_ROOT
EXPECTED_ROUND = "C74"
EXPECTED_SCANNER_VERSION = "1.9.4"
EXPECTED_SOURCE_HASH_ALGORITHM = "sorted relative POSIX path UTF-8 + NUL + file bytes + NUL"
EXPECTED_REVIEW_AUTHORIZATION = "用户直接回复：你帮我直接审阅"
MAX_EVIDENCE_AGE = timedelta(hours=24)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RULE_RE = re.compile(r"^B\d{3}$")
SCAN_FINDING_KEYS = {
    "filename",
    "test_id",
    "test_name",
    "issue_severity",
    "issue_confidence",
    "line_number",
    "line_range",
}
TRIAGE_FINDING_KEYS = {
    "test_id",
    "filename",
    "line_number",
    "issue_severity",
    "disposition",
    "basis_rule",
}
CRITICAL_DISPOSITIONS = {
    "B301": "RETAIN_DESERIALIZATION_RCE",
    "B307": "RETAIN_REMOTE_EVAL_RCE",
}
DESERIALIZATION_DISPOSITIONS = {
    "B301": "RETAIN_DESERIALIZATION_RCE",
    "B403": "RETAIN_DESERIALIZATION_IMPORT",
}


@dataclass(frozen=True)
class Issue:
    """A safe, bounded finding from one evidence surface."""

    code: str
    message: str


@dataclass
class ValidationResult:
    """Measured scan/triage facts and independent validation findings."""

    facts: dict[str, Any]
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        """Whether all current source, scan, and triage bindings validate."""
        return not self.issues


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _safe_source_path(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or not value.startswith(f"{PORT_ROOT}/"):
        return None
    if any(part in {"", ".", ".."} for part in value.split("/")) or path.suffix != ".py":
        return None
    return path.as_posix()


def _identity(record: dict[str, Any]) -> tuple[str, str, int] | None:
    path = _safe_source_path(record.get("filename"))
    rule = record.get("test_id")
    line = record.get("line_number")
    if path is None or not isinstance(rule, str) or RULE_RE.fullmatch(rule) is None:
        return None
    if type(line) is not int or line < 1:
        return None
    return path, rule, line


def _add_issue(issues: list[Issue], code: str, message: str) -> None:
    issues.append(Issue(code, message))


def _load_document(root: Path, relative: Path, label: str, issues: list[Issue]) -> object | None:
    path = root / relative
    if not path.is_file():
        _add_issue(issues, f"{label}-missing", f"{label} evidence file is missing")
        return None
    try:
        return _read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _add_issue(issues, f"{label}-invalid", f"{label} evidence file is unreadable or invalid")
        return None


def _validate_freshness(
    scan: dict[str, Any], triage: dict[str, Any], now: datetime, issues: list[Issue]
) -> tuple[str, str]:
    generated: list[datetime | None] = []
    for label, payload in (("scan", scan), ("triage", triage)):
        value = payload.get("generated_at")
        timestamp = _parse_datetime(value)
        generated.append(timestamp)
        if timestamp is None or timestamp > now + timedelta(minutes=2):
            _add_issue(issues, f"{label}-generated-at-invalid", f"{label} generated_at is invalid")
        elif now - timestamp > MAX_EVIDENCE_AGE:
            _add_issue(
                issues,
                f"{label}-stale",
                f"{label} evidence is older than the current review window",
            )
    if generated[0] is None or generated[1] is None or generated[0] != generated[1]:
        _add_issue(issues, "generated-at-mismatch", "scan and triage were not generated together")
    return (
        str(scan.get("generated_at", "")),
        str(triage.get("generated_at", "")),
    )


def _validate_current_source(
    root: Path, scan: dict[str, Any], triage: dict[str, Any], issues: list[Issue]
) -> tuple[dict[str, str], str | None, int, int]:
    port_root = root / PORT_ROOT
    if not port_root.is_dir():
        _add_issue(issues, "source-tree-missing", f"ported source tree is missing: {PORT_ROOT}")
        return {}, None, 0, 0
    actual_paths = sorted(
        path.relative_to(root).as_posix()
        for path in port_root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    current_hashes: dict[str, str] = {}
    unreadable = 0
    source_hasher = hashlib.sha256()
    for relative in actual_paths:
        path = root / relative
        try:
            content = path.read_bytes()
        except OSError:
            unreadable += 1
            _add_issue(
                issues, "source-file-unreadable", f"ported source file cannot be read: {relative}"
            )
            continue
        current_hashes[relative] = hashlib.sha256(content).hexdigest()
        source_hasher.update(relative.encode("utf-8"))
        source_hasher.update(b"\0")
        source_hasher.update(content)
        source_hasher.update(b"\0")
    current_source_hash = source_hasher.hexdigest() if unreadable == 0 else None

    saved_hashes = scan.get("source_files_sha256")
    if not isinstance(saved_hashes, dict):
        _add_issue(issues, "source-file-map-invalid", "scan source_files_sha256 map is missing")
        saved_hashes = {}
    normalized_hashes: dict[str, str] = {}
    for raw_path, digest in saved_hashes.items():
        normalized_path = _safe_source_path(raw_path)
        if (
            normalized_path is None
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            _add_issue(
                issues, "source-file-map-invalid", "scan source file map contains an invalid entry"
            )
            continue
        if normalized_path in normalized_hashes:
            _add_issue(
                issues, "source-file-map-duplicate", "scan source file map contains duplicate paths"
            )
            continue
        normalized_hashes[normalized_path] = digest
    if set(normalized_hashes) != set(current_hashes):
        _add_issue(
            issues,
            "source-file-set-mismatch",
            "scan source manifest differs from the current Python file set",
        )
    for relative in sorted(set(normalized_hashes) & set(current_hashes)):
        if normalized_hashes[relative] != current_hashes[relative]:
            _add_issue(
                issues, "source-file-hash-mismatch", f"ported source bytes changed: {relative}"
            )

    expected_algorithm = scan.get("source_hash_algorithm")
    if expected_algorithm != EXPECTED_SOURCE_HASH_ALGORITHM:
        _add_issue(
            issues,
            "source-hash-algorithm-invalid",
            "scan source hash algorithm is not the declared path/NUL/content/NUL scheme",
        )
    scan_source_hash = scan.get("source_sha256")
    if (
        current_source_hash is None
        or not isinstance(scan_source_hash, str)
        or SHA256_RE.fullmatch(scan_source_hash) is None
        or scan_source_hash != current_source_hash
    ):
        _add_issue(
            issues,
            "source-hash-mismatch",
            "scan source hash does not match current ported source bytes",
        )
    if triage.get("source_sha256") != scan_source_hash:
        _add_issue(
            issues,
            "triage-source-binding-mismatch",
            "triage source hash differs from the scan source hash",
        )

    file_count = len(actual_paths)
    for label, payload in (("scan", scan), ("triage", triage)):
        value = payload.get("python_files")
        if type(value) is not int or value != file_count:
            _add_issue(
                issues,
                f"{label}-python-file-count-mismatch",
                f"{label} Python file count differs from the current tree",
            )
    return current_hashes, current_source_hash, file_count, len(normalized_hashes)


def _validated_scan_findings(
    scan: dict[str, Any], current_paths: set[str], issues: list[Issue]
) -> tuple[list[dict[str, Any]], Counter[str], int, int]:
    scan_payload = scan.get("scan")
    if not isinstance(scan_payload, dict):
        _add_issue(issues, "scan-payload-invalid", "Bandit scan result object is missing")
        return [], Counter(), 0, 0
    errors = scan_payload.get("errors")
    if not isinstance(errors, list) or errors:
        _add_issue(
            issues, "scan-errors-present", "Bandit scan contains errors or an invalid errors field"
        )
    raw_results = scan_payload.get("results")
    if not isinstance(raw_results, list):
        _add_issue(issues, "scan-results-invalid", "Bandit scan results list is missing")
        raw_results = []

    findings: list[dict[str, Any]] = []
    identities: Counter[tuple[str, str, int]] = Counter()
    counts: Counter[str] = Counter()
    high_count = 0
    for value in raw_results:
        if not isinstance(value, dict) or set(value) != SCAN_FINDING_KEYS:
            _add_issue(
                issues,
                "scan-finding-shape-invalid",
                "Bandit finding has an unexpected sanitized shape",
            )
            continue
        identity = _identity(value)
        severity = value.get("issue_severity")
        confidence = value.get("issue_confidence")
        test_name = value.get("test_name")
        line_range = value.get("line_range")
        if identity is None:
            _add_issue(
                issues, "scan-finding-identity-invalid", "Bandit finding identity is invalid"
            )
            continue
        relative, rule, line = identity
        if relative not in current_paths:
            _add_issue(
                issues,
                "scan-finding-path-invalid",
                "Bandit finding is outside the current source file set",
            )
        if not isinstance(severity, str) or severity not in {"LOW", "MEDIUM", "HIGH"}:
            _add_issue(
                issues, "scan-finding-severity-invalid", "Bandit finding has an invalid severity"
            )
        if not isinstance(confidence, str) or confidence not in {"LOW", "MEDIUM", "HIGH"}:
            _add_issue(
                issues,
                "scan-finding-confidence-invalid",
                "Bandit finding has an invalid confidence",
            )
        if not isinstance(test_name, str) or not test_name.strip():
            _add_issue(issues, "scan-finding-name-invalid", "Bandit finding has no test name")
        if (
            not isinstance(line_range, list)
            or not line_range
            or any(type(position) is not int or position < 1 for position in line_range)
            or line not in line_range
        ):
            _add_issue(
                issues, "scan-finding-line-range-invalid", "Bandit finding line range is invalid"
            )
        identities[identity] += 1
        counts[rule] += 1
        high_count += severity == "HIGH"
        findings.append(value)
    if any(count > 1 for count in identities.values()):
        _add_issue(
            issues, "scan-finding-identity-duplicate", "Bandit scan repeats a finding identity"
        )
    saved_counts = scan.get("rule_counts")
    if not isinstance(saved_counts, dict):
        _add_issue(issues, "scan-rule-counts-invalid", "Bandit rule count map is missing")
        saved_counts = {}
    normalized_counts: dict[str, int] = {}
    for rule, count in saved_counts.items():
        if not isinstance(rule, str) or RULE_RE.fullmatch(rule) is None or type(count) is not int:
            _add_issue(
                issues,
                "scan-rule-counts-invalid",
                "Bandit rule count map contains an invalid entry",
            )
            continue
        normalized_counts[rule] = count
    if normalized_counts != dict(counts):
        _add_issue(
            issues, "scan-rule-count-mismatch", "Bandit rule counts differ from finding rows"
        )
    scanner_exit = scan.get("scanner_exit")
    expected_exit = 1 if findings else 0
    if type(scanner_exit) is not int or scanner_exit != expected_exit:
        _add_issue(
            issues,
            "scanner-exit-inconsistent",
            "Bandit exit code is not consistent with findings present",
        )
    return findings, counts, high_count, len(raw_results)


def _validate_triage(
    scan: dict[str, Any],
    triage: dict[str, Any],
    scan_findings: list[dict[str, Any]],
    scan_counts: Counter[str],
    high_count: int,
    issues: list[Issue],
) -> tuple[int, int, int, int, bool, bool, bool]:
    raw_triage = triage.get("findings")
    if not isinstance(raw_triage, list):
        _add_issue(issues, "triage-findings-invalid", "triage findings list is missing")
        raw_triage = []
    triage_by_identity: dict[tuple[str, str, int], dict[str, Any]] = {}
    for entry in raw_triage:
        if not isinstance(entry, dict) or set(entry) != TRIAGE_FINDING_KEYS:
            _add_issue(
                issues,
                "triage-finding-shape-invalid",
                "triage finding has an unexpected identity/disposition shape",
            )
            continue
        identity = _identity(entry)
        if identity is None:
            _add_issue(
                issues, "triage-finding-identity-invalid", "triage finding identity is invalid"
            )
            continue
        if identity in triage_by_identity:
            _add_issue(
                issues, "triage-finding-identity-duplicate", "triage repeats a finding identity"
            )
            continue
        triage_by_identity[identity] = entry

    scan_by_identity: dict[tuple[str, str, int], dict[str, Any]] = {}
    for entry in scan_findings:
        identity = _identity(entry)
        if identity is not None:
            scan_by_identity[identity] = entry
    missing = sorted(set(scan_by_identity) - set(triage_by_identity))
    extra = sorted(set(triage_by_identity) - set(scan_by_identity))
    if missing or extra:
        _add_issue(
            issues,
            "triage-finding-set-mismatch",
            "triage findings do not exactly cover current scan identities",
        )

    missing_high = 0
    disposition_by_rule: dict[str, str] = {}
    rules = triage.get("rules")
    if not isinstance(rules, dict):
        _add_issue(issues, "triage-rules-invalid", "triage rule summary is missing")
        rules = {}
    if set(rules) != set(scan_counts):
        _add_issue(
            issues,
            "triage-rule-set-mismatch",
            "triage rule summaries do not match rules found in scan",
        )
    for rule, count in scan_counts.items():
        summary = rules.get(rule)
        if not isinstance(summary, dict) or set(summary) != {
            "count",
            "disposition",
            "basis",
            "follow_up",
        }:
            _add_issue(
                issues, "triage-rule-summary-invalid", f"triage summary for {rule} is incomplete"
            )
            continue
        if type(summary.get("count")) is not int or summary.get("count") != count:
            _add_issue(
                issues, "triage-rule-count-mismatch", f"triage count for {rule} differs from scan"
            )
        disposition = summary.get("disposition")
        basis = summary.get("basis")
        follow_up = summary.get("follow_up")
        if not isinstance(disposition, str) or not disposition.strip():
            _add_issue(
                issues, "triage-rule-disposition-blank", f"triage disposition for {rule} is blank"
            )
        else:
            disposition_by_rule[rule] = disposition
        if not isinstance(basis, str) or not basis.strip():
            _add_issue(issues, "triage-rule-basis-blank", f"triage basis for {rule} is blank")
        if not isinstance(follow_up, str) or not follow_up.strip():
            _add_issue(
                issues, "triage-rule-follow-up-blank", f"triage follow-up for {rule} is blank"
            )

    for identity, scan_entry in scan_by_identity.items():
        triage_entry = triage_by_identity.get(identity)
        if triage_entry is None:
            if scan_entry.get("issue_severity") == "HIGH":
                missing_high += 1
            continue
        rule = identity[1]
        if triage_entry.get("issue_severity") != scan_entry.get("issue_severity"):
            _add_issue(
                issues, "triage-severity-mismatch", "triage severity differs from its scan finding"
            )
        if triage_entry.get("basis_rule") != rule:
            _add_issue(
                issues,
                "triage-basis-rule-mismatch",
                "triage finding basis does not reference its scan rule",
            )
        disposition = triage_entry.get("disposition")
        if not isinstance(disposition, str) or not disposition.strip():
            _add_issue(
                issues, "triage-finding-disposition-blank", "triage finding disposition is blank"
            )
        elif rule in disposition_by_rule and disposition != disposition_by_rule[rule]:
            _add_issue(
                issues,
                "triage-finding-disposition-mismatch",
                "triage finding disposition differs from its rule review",
            )
        if scan_entry.get("issue_severity") == "HIGH" and (
            not isinstance(disposition, str) or not disposition.startswith("RETAIN_")
        ):
            _add_issue(
                issues,
                "triage-high-not-retained",
                "HIGH finding is not explicitly retained as reviewed risk",
            )

    for rule, expected in CRITICAL_DISPOSITIONS.items():
        if scan_counts.get(rule, 0) <= 0:
            _add_issue(
                issues,
                "critical-rce-rule-missing",
                f"required reviewed risk rule {rule} is absent from scan",
            )
        if disposition_by_rule.get(rule) != expected:
            _add_issue(
                issues,
                "critical-rce-not-retained",
                f"required RCE rule {rule} is not retained as risk",
            )
    for rule, expected in DESERIALIZATION_DISPOSITIONS.items():
        if scan_counts.get(rule, 0) <= 0 or disposition_by_rule.get(rule) != expected:
            _add_issue(
                issues,
                "deserialization-not-reviewed",
                f"deserialization finding group {rule} is absent or not reviewed as risk",
            )

    triage_findings = triage.get("finding_count")
    if type(triage_findings) is not int or triage_findings != len(scan_findings):
        _add_issue(
            issues,
            "triage-finding-count-mismatch",
            "triage finding count differs from the current scan",
        )
    triage_high = triage.get("high_count")
    if type(triage_high) is not int or triage_high != high_count:
        _add_issue(
            issues, "triage-high-count-mismatch", "triage HIGH count differs from the current scan"
        )

    reviewer = triage.get("reviewer")
    reviewer_authorized = (
        isinstance(reviewer, dict)
        and reviewer.get("kind") == "AI"
        and isinstance(reviewer.get("name"), str)
        and bool(reviewer.get("name", "").strip())
        and reviewer.get("requester") == "cloudQuant"
        and reviewer.get("authorization") == EXPECTED_REVIEW_AUTHORIZATION
    )
    if not reviewer_authorized:
        _add_issue(
            issues,
            "reviewer-authorization-invalid",
            "triage lacks the current direct user authorization for AI review",
        )
    if triage.get("review_complete") is not True:
        _add_issue(issues, "review-incomplete", "triage review_complete is not true")
    if not isinstance(triage.get("review_scope"), str) or not triage["review_scope"].strip():
        _add_issue(issues, "review-scope-blank", "triage review scope is blank")
    if triage.get("security_fixed") is not False:
        _add_issue(
            issues,
            "security-fixed-overclaim",
            "triage must preserve that reviewed source risks are not fixed",
        )
    if triage.get("unresolved_risks") is not True:
        _add_issue(
            issues, "unresolved-risk-overclaim", "triage must state that risks remain unresolved"
        )

    covered_count = len(set(scan_by_identity) & set(triage_by_identity))
    high_reviewed = (
        high_count > 0
        and missing_high == 0
        and not any(issue.code == "triage-high-not-retained" for issue in issues)
    )
    deser_reviewed = all(
        scan_counts.get(rule, 0) > 0 and disposition_by_rule.get(rule) == expected
        for rule, expected in DESERIALIZATION_DISPOSITIONS.items()
    )
    rce_findings_retained = all(
        scan_counts.get(rule, 0) > 0 and disposition_by_rule.get(rule) == expected
        for rule, expected in CRITICAL_DISPOSITIONS.items()
    )
    return (
        covered_count,
        len(missing),
        missing_high,
        len(raw_triage),
        high_reviewed,
        deser_reviewed,
        rce_findings_retained,
    )


def validate(root: Path, *, now: datetime | None = None) -> ValidationResult:
    """Validate ``EXPECTED_ROUND``'s scan and triage against the tree at ``PORT_ROOT``."""
    current = _now_utc(now)
    issues: list[Issue] = []
    scan_path = root / SCAN_REL
    try:
        scan_sha256 = (
            hashlib.sha256(scan_path.read_bytes()).hexdigest() if scan_path.is_file() else ""
        )
    except OSError:
        scan_sha256 = ""
    raw_scan = _load_document(root, SCAN_REL, "scan", issues)
    raw_triage = _load_document(root, TRIAGE_REL, "triage", issues)
    if not isinstance(raw_scan, dict) or not isinstance(raw_triage, dict):
        return ValidationResult(
            facts={"valid": False, "issue_count": len(issues), "scan_sha256": scan_sha256},
            issues=issues,
        )
    scan = raw_scan
    triage = raw_triage

    generated_scan, generated_triage = _validate_freshness(scan, triage, current, issues)
    for label, payload in (("scan", scan), ("triage", triage)):
        if payload.get("archive_round") != EXPECTED_ROUND:
            _add_issue(
                issues,
                f"{label}-round-invalid",
                f"{label} evidence is not archived as the current round",
            )

    if scan.get("scanner_version") != EXPECTED_SCANNER_VERSION:
        _add_issue(
            issues, "scanner-version-invalid", "current evidence was not produced by Bandit 1.9.4"
        )
    if not isinstance(scan.get("python_version"), str) or not scan["python_version"].strip():
        _add_issue(issues, "scanner-python-version-invalid", "scan Python version is missing")
    if not isinstance(scan.get("produced_by"), str) or not scan["produced_by"].strip():
        _add_issue(issues, "scanner-command-missing", "scan command provenance is missing")
    redacted = scan.get("raw_content_redacted")
    if (
        not isinstance(redacted, list)
        or not all(isinstance(item, str) for item in redacted)
        or not {"code", "issue_text", "more_info"}.issubset(redacted)
    ):
        _add_issue(
            issues,
            "scan-redaction-invalid",
            "scan artifact does not declare source-context redaction",
        )
    if triage.get("scan_sha256") != scan_sha256 or SHA256_RE.fullmatch(scan_sha256) is None:
        _add_issue(
            issues,
            "scan-triage-sha-mismatch",
            "triage is not bound to the exact current scan JSON bytes",
        )

    current_hashes, current_source_hash, python_files, saved_source_files = (
        _validate_current_source(root, scan, triage, issues)
    )
    source_paths = set(current_hashes)
    findings, rule_counts, high_count, raw_finding_count = _validated_scan_findings(
        scan, source_paths, issues
    )
    if scan.get("source_sha256") != current_source_hash:
        _add_issue(
            issues,
            "scan-source-current-mismatch",
            "scan source digest is not the current path/content digest",
        )
    (
        triaged_count,
        untriaged_count,
        untriaged_high,
        triage_finding_count,
        high_reviewed,
        deser_reviewed,
        rce_findings_retained,
    ) = _validate_triage(scan, triage, findings, rule_counts, high_count, issues)
    if triage.get("source_sha256") != current_source_hash:
        _add_issue(
            issues,
            "triage-source-current-mismatch",
            "triage source digest is not the current path/content digest",
        )

    rules_found = len(rule_counts)
    all_rules_bandit = bool(rule_counts) and all(RULE_RE.fullmatch(rule) for rule in rule_counts)
    if raw_finding_count != len(findings):
        _add_issue(issues, "scan-finding-count-invalid", "some scan finding rows failed validation")
    issues_by_code = Counter(issue.code for issue in issues)
    rule_review_issue_codes = {
        "triage-rules-invalid",
        "triage-rule-set-mismatch",
        "triage-rule-summary-invalid",
        "triage-rule-count-mismatch",
        "triage-rule-disposition-blank",
        "triage-rule-basis-blank",
        "triage-rule-follow-up-blank",
        "triage-finding-disposition-blank",
        "triage-finding-disposition-mismatch",
        "triage-basis-rule-mismatch",
        "triage-high-not-retained",
    }
    facts: dict[str, Any] = {
        "valid": False,
        "python_files": python_files,
        "source_files_saved": saved_source_files,
        "source_files_verified": len(current_hashes),
        "source_sha256": current_source_hash or "",
        "scan_sha256": scan_sha256,
        "source_tree_valid": not any(
            issue.code.startswith("source-")
            or issue.code.startswith("scan-source-")
            or issue.code.startswith("triage-source-")
            for issue in issues
        ),
        "scanner_version": str(scan.get("scanner_version", "")),
        "scanner_exit": scan.get("scanner_exit", "unknown"),
        "scanner_exit_consistent": not any(
            issue.code == "scanner-exit-inconsistent" for issue in issues
        ),
        "scan_errors": len(scan.get("scan", {}).get("errors", []))
        if isinstance(scan.get("scan"), dict) and isinstance(scan["scan"].get("errors"), list)
        else "invalid",
        "finding_count": len(findings),
        "scan_files": len(
            {identity[0] for identity in (_identity(item) for item in findings) if identity}
        ),
        "rule_count": rules_found,
        "rule_names": ", ".join(sorted(rule_counts)) or "-",
        "rule_counts_match": not any(issue.code == "scan-rule-count-mismatch" for issue in issues),
        "high_count": high_count,
        "triaged_findings": triaged_count,
        "triage_findings": triage_finding_count,
        "untriaged_findings": untriaged_count,
        "untriaged_high": untriaged_high,
        "finding_coverage": not any(
            issue.code in {"triage-finding-set-mismatch", "triage-finding-count-mismatch"}
            for issue in issues
        ),
        "rule_review_valid": not any(issue.code in rule_review_issue_codes for issue in issues),
        "high_reviewed": high_reviewed,
        "deser_reviewed": deser_reviewed,
        "rce_findings_retained": rce_findings_retained,
        "reviewer_authorized": not any(
            issue.code == "reviewer-authorization-invalid" for issue in issues
        ),
        "review_complete": triage.get("review_complete") is True,
        "security_fixed": triage.get("security_fixed") is True,
        "unresolved_risks": triage.get("unresolved_risks") is True,
        "generated_at": generated_scan,
        "triage_generated_at": generated_triage,
        "issue_count": len(issues),
        "issue_codes": sorted(issues_by_code),
        "all_rules_bandit": all_rules_bandit,
    }
    facts["valid"] = not issues
    return ValidationResult(facts=facts, issues=issues)
