"""Offline regression tests for the current Bandit and triage evidence bundle."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest

from scripts.quality.ported_security_evidence import SCAN_REL, TRIAGE_REL, validate

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
GENERATED_AT = "2026-10-01T11:30:00+00:00"
SOURCE_HASH_ALGORITHM = "sorted relative POSIX path UTF-8 + NUL + file bytes + NUL"
RULE_DISPOSITIONS = {
    "B301": "RETAIN_DESERIALIZATION_RCE",
    "B307": "RETAIN_REMOTE_EVAL_RCE",
    "B403": "RETAIN_DESERIALIZATION_IMPORT",
    "B501": "RETAIN_TLS_VALIDATION_RISK",
}


def write_json(path: Path, document: object) -> None:
    """Write deterministic JSON evidence for a fixture repository."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    """Read one fixture object for a focused counterfactual."""
    return json.loads(path.read_text(encoding="utf-8"))


def source_digest(root: Path, paths: list[str]) -> tuple[dict[str, str], str]:
    """Return per-file and sorted path/content source digests."""
    file_hashes: dict[str, str] = {}
    source_hasher = hashlib.sha256()
    for relative in sorted(paths):
        content = (root / relative).read_bytes()
        file_hashes[relative] = hashlib.sha256(content).hexdigest()
        source_hasher.update(relative.encode("utf-8"))
        source_hasher.update(b"\0")
        source_hasher.update(content)
        source_hasher.update(b"\0")
    return file_hashes, source_hasher.hexdigest()


def refresh_scan_binding(root: Path, scan: dict[str, Any], triage: dict[str, Any]) -> None:
    """Recompute the byte-level triage binding after a deliberate scan mutation."""
    write_json(root / SCAN_REL, scan)
    triage["scan_sha256"] = hashlib.sha256((root / SCAN_REL).read_bytes()).hexdigest()
    write_json(root / TRIAGE_REL, triage)


def valid_bundle(root: Path) -> None:
    """Create an internally consistent scan and triage without external tools."""
    source_paths = [
        "opendata_http/serialization.py",
        "opendata_http/remote.py",
        "opendata_http/tls.py",
    ]
    (root / source_paths[0]).parent.mkdir(parents=True, exist_ok=True)
    (root / source_paths[1]).write_text("def remote():\n    return eval('1')\n", encoding="utf-8")
    (root / source_paths[2]).write_text("def tls():\n    return True\n", encoding="utf-8")
    (root / source_paths[0]).write_text(
        "def load():\n    return pickle.loads(b'x')\n", encoding="utf-8"
    )
    file_hashes, source_hash = source_digest(root, source_paths)

    findings = [
        ("B301", source_paths[0], 2, "MEDIUM"),
        ("B307", source_paths[1], 2, "MEDIUM"),
        ("B403", source_paths[0], 1, "LOW"),
        ("B501", source_paths[2], 2, "HIGH"),
    ]
    scan_findings = [
        {
            "filename": filename,
            "test_id": rule,
            "test_name": f"Test {rule}",
            "issue_severity": severity,
            "issue_confidence": "HIGH",
            "line_number": line,
            "line_range": [line],
        }
        for rule, filename, line, severity in findings
    ]
    counts: dict[str, int] = {}
    triage_findings: list[dict[str, Any]] = []
    for rule, filename, line, severity in findings:
        counts[rule] = counts.get(rule, 0) + 1
        triage_findings.append(
            {
                "test_id": rule,
                "filename": filename,
                "line_number": line,
                "issue_severity": severity,
                "disposition": RULE_DISPOSITIONS[rule],
                "basis_rule": rule,
            }
        )
    scan: dict[str, Any] = {
        "archive_round": "C65",
        "generated_at": GENERATED_AT,
        "produced_by": "bandit -r opendata_http",
        "scanner_version": "1.9.4",
        "python_version": "3.11.8",
        "scanner_exit": 1,
        "source_hash_algorithm": SOURCE_HASH_ALGORITHM,
        "source_sha256": source_hash,
        "source_files_sha256": file_hashes,
        "python_files": len(source_paths),
        "scan": {"errors": [], "results": scan_findings},
        "raw_content_redacted": ["code", "issue_text", "more_info"],
        "rule_counts": counts,
    }
    triage: dict[str, Any] = {
        "archive_round": "C65",
        "generated_at": GENERATED_AT,
        "reviewer": {
            "name": "Codex primary",
            "kind": "AI",
            "authorization": "用户直接回复：你帮我直接审阅",
            "requester": "cloudQuant",
        },
        "review_scope": "Grouped review of all current Bandit findings.",
        "review_complete": True,
        "security_fixed": False,
        "unresolved_risks": True,
        "source_sha256": source_hash,
        "scan_sha256": "",
        "python_files": len(source_paths),
        "finding_count": len(findings),
        "high_count": sum(severity == "HIGH" for _, _, _, severity in findings),
        "rules": {
            rule: {
                "count": count,
                "disposition": RULE_DISPOSITIONS[rule],
                "basis": f"Reviewed the current {rule} finding.",
                "follow_up": "Track remediation separately from this review.",
            }
            for rule, count in counts.items()
        },
        "findings": triage_findings,
    }
    refresh_scan_binding(root, scan, triage)


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    """A fresh local evidence repository with a source tree and full review."""
    valid_bundle(tmp_path)
    return tmp_path


def test_current_source_scan_and_risk_review_validate(evidence_root: Path) -> None:
    result = validate(evidence_root, now=NOW)

    assert result.valid is True
    assert result.facts["python_files"] == 3
    assert result.facts["finding_count"] == 4
    assert result.facts["high_count"] == 1
    assert result.facts["untriaged_findings"] == 0
    assert result.facts["deser_reviewed"] is True
    assert result.facts["rce_findings_retained"] is True
    assert result.facts["security_fixed"] is False
    assert result.facts["unresolved_risks"] is True


def test_stale_source_bytes_and_digest_are_rejected(evidence_root: Path) -> None:
    (evidence_root / "opendata_http/serialization.py").write_text(
        "def load():\n    return pickle.loads(b'changed')\n", encoding="utf-8"
    )

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert {issue.code for issue in result.issues} >= {
        "source-file-hash-mismatch",
        "source-hash-mismatch",
    }


def test_missing_triage_finding_breaks_exact_coverage(evidence_root: Path) -> None:
    triage = read_json(evidence_root / TRIAGE_REL)
    triage["findings"].pop()
    write_json(evidence_root / TRIAGE_REL, triage)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "triage-finding-set-mismatch" in {issue.code for issue in result.issues}
    assert result.facts["untriaged_findings"] == 1


def test_untriaged_high_finding_is_rejected(evidence_root: Path) -> None:
    triage = read_json(evidence_root / TRIAGE_REL)
    triage["findings"] = [row for row in triage["findings"] if row["issue_severity"] != "HIGH"]
    write_json(evidence_root / TRIAGE_REL, triage)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert result.facts["untriaged_high"] == 1
    assert "triage-finding-set-mismatch" in {issue.code for issue in result.issues}


def test_unauthorized_ai_review_is_rejected(evidence_root: Path) -> None:
    triage = read_json(evidence_root / TRIAGE_REL)
    triage["reviewer"]["authorization"] = "not authorized"
    write_json(evidence_root / TRIAGE_REL, triage)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "reviewer-authorization-invalid" in {issue.code for issue in result.issues}


def test_blank_rule_basis_is_rejected(evidence_root: Path) -> None:
    triage = read_json(evidence_root / TRIAGE_REL)
    triage["rules"]["B501"]["basis"] = "  "
    write_json(evidence_root / TRIAGE_REL, triage)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "triage-rule-basis-blank" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("missing_rule", ["B301", "B307"])
def test_zeroing_a_required_rce_class_is_rejected(evidence_root: Path, missing_rule: str) -> None:
    scan = read_json(evidence_root / SCAN_REL)
    triage = read_json(evidence_root / TRIAGE_REL)
    scan["scan"]["results"] = [
        row for row in scan["scan"]["results"] if row["test_id"] != missing_rule
    ]
    scan["rule_counts"].pop(missing_rule)
    triage["findings"] = [row for row in triage["findings"] if row["test_id"] != missing_rule]
    triage["rules"].pop(missing_rule)
    triage["finding_count"] = len(triage["findings"])
    refresh_scan_binding(evidence_root, scan, triage)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert result.facts["rce_findings_retained"] is False
    assert "critical-rce-rule-missing" in {issue.code for issue in result.issues}


def test_triage_must_bind_exact_scan_bytes(evidence_root: Path) -> None:
    scan = read_json(evidence_root / SCAN_REL)
    scan["produced_by"] = "changed after review"
    write_json(evidence_root / SCAN_REL, scan)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "scan-triage-sha-mismatch" in {issue.code for issue in result.issues}
