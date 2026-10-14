"""Build a fail-closed evidence index for the fixed Iteration 2 model inventory.

The index reads only the explicitly supplied inventory and claims JSON. It never
walks evidence directories or infers acceptance from prose, filenames, pytest
totals, or a claim's ``PASS``/``READY`` label.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Final, TypedDict

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
EXPECTED_MODEL_COUNT: Final = 350
EXPECTED_IDENTITY_SHA256: Final = "5168e1e723999312b0978bf9b1a8999c0b4960cdb12013e43e1e59e349e21e65"
SCHEMA_VERSION: Final = 1
CLAIM_RECEIPT_SCHEMA_VERSION: Final = 1
REQUIRED_PHASES: Final = (
    "implementation",
    "direct",
    "service",
    "contract",
    "counterfact",
)
PHASE_EVIDENCE_KIND: Final = {
    "implementation": "implementation_check",
    "direct": "direct_fetcher",
    "service": "provider_model_query",
    "contract": "typed_contract",
    "counterfact": "adversarial_counterfact",
}
CLAIM_STATUSES: Final = {"PASS", "FAIL", "BLOCKED", "UNREVIEWED"}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceIndexError(ValueError):
    """Raised when the fixed inventory itself is malformed or replaced."""


JsonObject = dict[str, object]


class InventoryRow(TypedDict):
    """Pinned provider/model identity and source task key."""

    task_id: str
    provider: str
    upstream_model: str


class Inventory(TypedDict):
    """Verified identity inventory and its file-level fingerprints."""

    rows: list[InventoryRow]
    csv_sha256: str
    identity_set_sha256: str
    identity_count: int
    path: str


class ClaimValidation(TypedDict):
    """Typed internal result of validating one phase claim."""

    valid: bool
    stale: bool
    issues: list[str]
    phase: object
    status: object
    evidence_kind: object
    receipt_path: str | None
    counterfact: JsonObject | None


class PrimaryReviewValidation(TypedDict):
    """Typed internal result of validating one external review."""

    valid: bool
    stale: bool
    decision: object
    reviewer_type: object
    reviewer_id: object
    reviewer_model: object
    reviewer_role: object
    evidence_snapshot_status: str | None
    issues: list[str]


class PhaseResult(TypedDict, total=False):
    """One phase's report fields, present according to its validation path."""

    claim_count: int
    status: str
    issues: list[str]
    receipts: list[object]
    claim_status: object
    evidence_kind: object
    receipt: str | None
    counterfact: JsonObject | None


class PrimaryReviewResult(TypedDict):
    """Public report fields for the identity's external review."""

    decision: object
    reviewer_type: object
    reviewer_id: object
    reviewer_model: object
    reviewer_role: object
    evidence_snapshot_status: object
    status: str
    issues: list[str]


class ModelResult(TypedDict):
    """Public report entry for one fixed provider/model identity."""

    task_id: str
    provider: str
    upstream_model: str
    model_identity: str
    status: str
    phases: dict[str, PhaseResult]
    missing_phases: list[str]
    primary_review: PrimaryReviewResult
    issues: list[str]
    missing_material: list[str]


class InventoryReport(TypedDict):
    """Pinned inventory metadata embedded in the report."""

    path: str
    csv_sha256: str
    identity_count: int
    identity_set_sha256: str
    expected_identity_count: int
    expected_identity_set_sha256: str
    integrity: str


class ClaimsInput(TypedDict):
    """Path and digest of the explicitly supplied claims document."""

    path: str
    sha256: str


class ModelEvidenceSummary(TypedDict):
    """Counts summarized across all fixed model identities."""

    identity_count: int
    claim_count: int
    primary_review_count: int
    status_counts: dict[str, int]
    offline_acceptance_count: int


class ModelEvidenceReport(TypedDict):
    """Stable JSON report shape emitted by the evidence indexer."""

    schema_version: int
    acceptance_rule: str
    inventory: InventoryReport
    claims_input: ClaimsInput
    claims_status_source: str
    global_issues: list[str]
    invalid_claims: list[JsonObject]
    summary: ModelEvidenceSummary
    models: list[ModelResult]


def _json_object(value: object) -> JsonObject | None:
    """Return a string-keyed view of an object without accepting malformed keys."""
    if not isinstance(value, dict):
        return None
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _json_array(value: object) -> list[object] | None:
    """Return a JSON array as opaque objects for explicit per-entry validation."""
    if not isinstance(value, list):
        return None
    return list(value)


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest of a byte sequence."""
    return hashlib.sha256(value).hexdigest()


def identity_projection_sha256(identities: list[tuple[str, str]]) -> str:
    """Digest the canonical sorted provider/model identity projection."""
    projection = json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256_bytes(projection)


def load_inventory(
    path: Path,
    *,
    expected_count: int = EXPECTED_MODEL_COUNT,
    expected_identity_sha256: str = EXPECTED_IDENTITY_SHA256,
) -> Inventory:
    """Read and verify the pinned CSV identity inventory."""
    raw = path.read_bytes()
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EvidenceIndexError("inventory is not UTF-8 CSV") from exc

    reader = csv.DictReader(io.StringIO(decoded, newline=""))
    if (
        not reader.fieldnames
        or "provider" not in reader.fieldnames
        or "upstream_model" not in reader.fieldnames
    ):
        raise EvidenceIndexError("inventory must have provider and upstream_model headers")
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise EvidenceIndexError("inventory contains duplicate column names")

    rows: list[InventoryRow] = []
    identities: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, source_row in enumerate(reader, start=2):
        provider = (source_row.get("provider") or "").strip()
        upstream_model = (source_row.get("upstream_model") or "").strip()
        if not provider or not upstream_model:
            raise EvidenceIndexError(f"inventory row {line_number} has an empty identity")
        identity = (provider, upstream_model)
        if identity in seen:
            raise EvidenceIndexError(
                f"inventory row {line_number} duplicates {provider}::{upstream_model}"
            )
        seen.add(identity)
        identities.append(identity)
        rows.append(
            {
                "provider": provider,
                "upstream_model": upstream_model,
                "task_id": (source_row.get("task_id") or "").strip(),
            }
        )

    actual_digest = identity_projection_sha256(identities)
    if len(rows) != expected_count:
        raise EvidenceIndexError(
            f"inventory identity count changed: expected {expected_count}, got {len(rows)}"
        )
    if actual_digest != expected_identity_sha256:
        raise EvidenceIndexError(
            "inventory identity set changed: "
            f"expected {expected_identity_sha256}, got {actual_digest}"
        )

    return {
        "rows": rows,
        "csv_sha256": sha256_bytes(raw),
        "identity_set_sha256": actual_digest,
        "identity_count": len(rows),
        "path": path.as_posix(),
    }


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(timezone.utc)


def _utc_timestamp(value: object) -> bool:
    return _parse_utc_timestamp(value) is not None


def _safe_repo_path(repo_root: Path, relative_path: object) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    pure_path = PurePosixPath(relative_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        return None
    root = repo_root.resolve()
    candidate = (root / Path(*pure_path.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _file_sha256(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _receipt_model_value_status(value: object, provider: str, upstream_model: str) -> str:
    expected = f"{provider}::{upstream_model}"
    if value == expected:
        return "match"
    if value == upstream_model:
        return "bare"
    if isinstance(value, str) and value.endswith(f"::{upstream_model}"):
        return "provider_collision"
    if isinstance(value, str):
        return "other"
    return "invalid"


def _nested_receipt_model_status(
    value: dict[str, object],
    provider: str,
    upstream_model: str,
    *,
    allow_other_models: bool,
) -> tuple[bool, bool]:
    """Return whether a nested model pair matches and whether it conflicts."""
    fields = [key for key in ("model_identity", "upstream_model", "model") if key in value]
    statuses = [_receipt_model_value_status(value[key], provider, upstream_model) for key in fields]
    nested_match = "match" in statuses
    bare_match = "bare" in statuses
    other_model = "other" in statuses
    conflict = "provider_collision" in statuses or "invalid" in statuses

    if "model_identity" in fields:
        declared_status = _receipt_model_value_status(
            value["model_identity"], provider, upstream_model
        )
        if declared_status != "match" and not (allow_other_models and declared_status == "other"):
            conflict = True
    if nested_match or bare_match:
        conflict = conflict or other_model
    elif other_model and not allow_other_models:
        conflict = True

    provider_present = "provider" in value
    nested_provider = value.get("provider")
    if provider_present and nested_provider != provider:
        conflict = conflict or not allow_other_models or nested_match or bare_match
    elif provider_present and nested_provider == provider and bare_match:
        nested_match = True
    return nested_match, conflict


def _receipt_top_level_status(
    receipt_json: dict[str, object], provider: str, upstream_model: str
) -> tuple[bool, bool]:
    matched = False
    conflict = False
    if "model_identity" in receipt_json:
        matched = receipt_json["model_identity"] == f"{provider}::{upstream_model}"
        conflict = not matched

    provider_present = "provider" in receipt_json
    if provider_present and receipt_json["provider"] != provider:
        conflict = True
    if "upstream_model" in receipt_json:
        if receipt_json["upstream_model"] != upstream_model:
            conflict = True
        elif provider_present and receipt_json["provider"] == provider:
            matched = True
    return matched, conflict


def _receipt_single_model_status(
    value: object, provider: str, upstream_model: str
) -> tuple[bool, bool, bool]:
    if isinstance(value, dict):
        matched, conflict = _nested_receipt_model_status(
            value, provider, upstream_model, allow_other_models=False
        )
        return matched, False, conflict
    status = _receipt_model_value_status(value, provider, upstream_model)
    return status == "match", status == "bare", status not in {"match", "bare"}


def _receipt_models_list_status(
    models: object, provider: str, upstream_model: str
) -> tuple[bool, bool, bool]:
    if not isinstance(models, list) or not models:
        return False, False, True
    matched = False
    bare_model_seen = False
    conflict = False
    target_seen = False
    for model in models:
        if isinstance(model, dict):
            model_match, model_conflict = _nested_receipt_model_status(
                model, provider, upstream_model, allow_other_models=True
            )
            matched = matched or model_match
            conflict = conflict or model_conflict
            target_seen = target_seen or model_match or model_conflict
            continue
        if not isinstance(model, str):
            conflict = True
            continue
        status = _receipt_model_value_status(model, provider, upstream_model)
        matched = matched or status == "match"
        bare_model_seen = bare_model_seen or status == "bare"
        conflict = conflict or status == "provider_collision"
        target_seen = target_seen or status in {"match", "bare", "provider_collision"}
    return matched, bare_model_seen, conflict or not target_seen


def _receipt_identity_issues(receipt_json: object, provider: str, upstream_model: str) -> list[str]:
    if not isinstance(receipt_json, dict):
        return ["receipt_not_json_object"]

    matched, conflict = _receipt_top_level_status(receipt_json, provider, upstream_model)
    bare_model_seen = False
    if "model" in receipt_json:
        model_match, model_bare, model_conflict = _receipt_single_model_status(
            receipt_json["model"], provider, upstream_model
        )
        matched = matched or model_match
        bare_model_seen = bare_model_seen or model_bare
        conflict = conflict or model_conflict
    if "models" in receipt_json:
        list_match, list_bare, list_conflict = _receipt_models_list_status(
            receipt_json["models"], provider, upstream_model
        )
        matched = matched or list_match
        bare_model_seen = bare_model_seen or list_bare
        conflict = conflict or list_conflict

    # A provider paired with a bare model name is an identity. A bare name by
    # itself is deliberately insufficient because other providers may reuse it.
    if receipt_json.get("provider") == provider and bare_model_seen:
        matched = True
    if conflict:
        return ["wrong_model_receipt"]
    if not matched:
        return ["receipt_has_no_machine_readable_model_identity"]
    return []


def _validate_receipt(
    receipt: object,
    *,
    provider: str,
    upstream_model: str,
    repo_root: Path,
    label: str,
    execution_claim: dict[str, object] | None = None,
) -> tuple[list[str], bool, str | None]:
    issues: list[str] = []
    stale = False
    if not isinstance(receipt, dict):
        return [f"{label}_receipt_missing"], False, None
    path_value = receipt.get("path")
    expected_sha = receipt.get("sha256")
    if receipt.get("model_identity") != f"{provider}::{upstream_model}":
        issues.append(f"{label}_receipt_model_identity_mismatch")
    if not isinstance(expected_sha, str) or not HEX_SHA256.fullmatch(expected_sha):
        issues.append(f"{label}_receipt_sha256_missing_or_invalid")
    path = _safe_repo_path(repo_root, path_value)
    if path is None:
        issues.append(f"{label}_receipt_path_invalid")
        return issues, stale, None
    actual_sha = _file_sha256(path)
    if actual_sha is None:
        issues.append(f"{label}_receipt_missing_on_disk")
        stale = True
    elif isinstance(expected_sha, str) and actual_sha != expected_sha:
        issues.append(f"{label}_receipt_hash_drift")
        stale = True
    else:
        try:
            receipt_json = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            issues.append(f"{label}_receipt_not_parseable_json")
        else:
            issues.extend(_receipt_identity_issues(receipt_json, provider, upstream_model))
            if execution_claim is not None and isinstance(receipt_json, dict):
                issues.extend(_receipt_execution_issues(receipt_json, execution_claim))
    return issues, stale, path_value if isinstance(path_value, str) else None


def _receipt_execution_issues(
    receipt_json: dict[str, object], claim: dict[str, object]
) -> list[str]:
    issues: list[str] = []
    if receipt_json.get("receipt_schema_version") != CLAIM_RECEIPT_SCHEMA_VERSION:
        issues.append("claim_receipt_schema_version_unsupported")
    issues.extend(
        f"claim_receipt_{field}_mismatch"
        for field in ("phase", "status")
        if receipt_json.get(field) != claim.get(field)
    )

    execution = receipt_json.get("machine_execution")
    if not isinstance(execution, dict):
        issue = (
            "source_review_machine_execution_missing"
            if claim.get("execution_mode") == "source_review"
            else "claim_receipt_machine_execution_missing"
        )
        return [*issues, issue]
    if execution.get("record_type") != "machine_execution":
        issues.append("claim_receipt_machine_execution_invalid")
    if execution.get("status") != claim.get("status"):
        issues.append("claim_receipt_execution_status_mismatch")
    if execution.get("actual_command") != claim.get("actual_command"):
        issues.append("claim_receipt_command_mismatch")
    claim_exit = claim.get("exit_code")
    receipt_exit = execution.get("exit_code")
    if (
        not isinstance(receipt_exit, int)
        or isinstance(receipt_exit, bool)
        or receipt_exit != claim_exit
    ):
        issues.append("claim_receipt_exit_code_mismatch")

    claim_time = _parse_utc_timestamp(claim.get("executed_at_utc"))
    receipt_time = _parse_utc_timestamp(execution.get("executed_at_utc"))
    if claim_time is None or receipt_time is None:
        issues.append("claim_receipt_executed_at_utc_invalid")
    elif claim_time != receipt_time:
        issues.append("claim_receipt_executed_at_utc_mismatch")

    claim_pins = _normalized_pin_snapshot(claim.get("pins"))
    receipt_pins = _normalized_pin_snapshot(receipt_json.get("pins"))
    if claim_pins is None or receipt_pins is None:
        issues.append("claim_receipt_pins_missing_or_invalid")
    elif claim_pins != receipt_pins:
        issues.append("claim_receipt_pins_mismatch")
    return issues


def _normalized_pin_snapshot(pins: object) -> tuple[object, ...] | None:
    """Return a strict, order-independent snapshot of all evidence pins."""
    if not isinstance(pins, dict):
        return None
    normalized_groups: list[tuple[str, tuple[tuple[str, str, str], ...]]] = []
    for group in ("source", "fixture", "probe", "contract"):
        entries = pins.get(group)
        if not isinstance(entries, list) or not entries:
            return None
        normalized_entries: list[tuple[str, str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            path = entry.get("path")
            before = entry.get("before_sha256")
            after = entry.get("after_sha256")
            if (
                not isinstance(path, str)
                or not path.strip()
                or not isinstance(before, str)
                or not HEX_SHA256.fullmatch(before)
                or not isinstance(after, str)
                or not HEX_SHA256.fullmatch(after)
            ):
                return None
            normalized_entries.append((path, before, after))
        normalized_groups.append((group, tuple(sorted(normalized_entries))))

    contract_version = pins.get("contract_version")
    if not isinstance(contract_version, str) or not contract_version.strip():
        return None
    return (*normalized_groups, ("contract_version", contract_version))


def _validate_pin_groups(
    pins: object,
    *,
    repo_root: Path,
    label: str,
) -> tuple[list[str], bool]:
    issues: list[str] = []
    stale = False
    if not isinstance(pins, dict):
        return ["pins_missing"], False
    for group in ("source", "fixture", "probe", "contract"):
        entries = pins.get(group)
        if not isinstance(entries, list) or not entries:
            issues.append(f"{group}_pins_missing")
            continue
        for index, entry in enumerate(entries):
            pin_label = f"{group}_pin_{index + 1}"
            if not isinstance(entry, dict):
                issues.append(f"{pin_label}_invalid")
                continue
            before = entry.get("before_sha256")
            after = entry.get("after_sha256")
            if not isinstance(before, str) or not HEX_SHA256.fullmatch(before):
                issues.append(f"{pin_label}_pre_hash_missing_or_invalid")
            if not isinstance(after, str) or not HEX_SHA256.fullmatch(after):
                issues.append(f"{pin_label}_post_hash_missing_or_invalid")
            if isinstance(before, str) and isinstance(after, str) and before != after:
                issues.append(f"{pin_label}_pre_post_hash_drift")
                stale = True
            path = _safe_repo_path(repo_root, entry.get("path"))
            if path is None:
                issues.append(f"{pin_label}_path_invalid")
                continue
            current = _file_sha256(path)
            if current is None:
                issues.append(f"{pin_label}_file_missing")
                stale = True
            elif isinstance(after, str) and HEX_SHA256.fullmatch(after) and current != after:
                issues.append(f"{pin_label}_current_hash_drift")
                stale = True
        if group == "contract":
            contract_version = pins.get("contract_version")
            if not isinstance(contract_version, str) or not contract_version.strip():
                issues.append("contract_version_missing")
            elif entries and isinstance(entries[0], dict):
                first_after = entries[0].get("after_sha256")
                if contract_version != f"sha256:{first_after}":
                    issues.append("contract_version_pin_mismatch")
    return issues, stale


def _validate_claim(
    claim: object,
    *,
    identity: tuple[str, str],
    repo_root: Path,
) -> ClaimValidation:
    provider, upstream_model = identity
    issues: list[str] = []
    stale = False
    claim_object = _json_object(claim)
    if claim_object is None:
        return {
            "valid": False,
            "stale": False,
            "issues": ["claim_not_object"],
            "phase": None,
            "status": None,
            "evidence_kind": None,
            "receipt_path": None,
            "counterfact": None,
        }
    claim = claim_object

    phase = claim.get("phase")
    if phase not in REQUIRED_PHASES:
        issues.append("phase_unknown")
    if claim.get("provider") != provider or claim.get("upstream_model") != upstream_model:
        issues.append("claim_identity_mismatch")

    status = claim.get("status")
    if not isinstance(status, str) or status not in CLAIM_STATUSES:
        issues.append("claim_status_unrecognized")
    elif status != "PASS":
        issues.append(f"claim_status_{str(status).lower()}")

    expected_kind = PHASE_EVIDENCE_KIND.get(phase) if isinstance(phase, str) else None
    if claim.get("evidence_kind") != expected_kind:
        issues.append("evidence_kind_mismatch")
    execution_mode = claim.get("execution_mode")
    if not isinstance(execution_mode, str) or execution_mode not in {
        "offline_fixture",
        "source_probe",
        "source_review",
    }:
        issues.append("execution_mode_unrecognized")

    command = claim.get("actual_command")
    if not isinstance(command, str) or not command.strip():
        issues.append("actual_command_missing")
    else:
        normalized_command = command.strip().lower()
        if normalized_command in {"ready", "pass", "console", "self-test", "self test"} or (
            "console" in normalized_command
            and any(
                marker in normalized_command for marker in ("self-test", "self test", "self_test")
            )
        ):
            issues.append("status_or_console_is_not_a_command")

    exit_code = claim.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        issues.append("exit_code_missing_or_invalid")
    elif exit_code != 0:
        issues.append("command_exit_nonzero")
    if not _utc_timestamp(claim.get("executed_at_utc")):
        issues.append("executed_at_utc_missing_or_invalid")

    receipt_issues, receipt_stale, receipt_path = _validate_receipt(
        claim.get("receipt"),
        provider=provider,
        upstream_model=upstream_model,
        repo_root=repo_root,
        label="claim",
        execution_claim=claim,
    )
    issues.extend(receipt_issues)
    stale = stale or receipt_stale

    counterfact: dict[str, object] | None = None
    if phase == "counterfact":
        counterfact_issues, counterfact = _counterfact_receipt_details(
            claim.get("receipt"), repo_root=repo_root
        )
        issues.extend(counterfact_issues)

    pin_issues, pins_stale = _validate_pin_groups(
        claim.get("pins"), repo_root=repo_root, label=f"{provider}::{upstream_model}:{phase}"
    )
    issues.extend(pin_issues)
    stale = stale or pins_stale

    return {
        "valid": not issues and not stale,
        "stale": stale,
        "issues": sorted(set(issues)),
        "phase": phase,
        "status": status,
        "evidence_kind": claim.get("evidence_kind"),
        "receipt_path": receipt_path,
        "counterfact": counterfact,
    }


def _counterfact_receipt_details(
    receipt: object, *, repo_root: Path
) -> tuple[list[str], JsonObject | None]:
    receipt_object = _json_object(receipt)
    if receipt_object is None:
        return ["counterfact_receipt_missing"], None
    path = _safe_repo_path(repo_root, receipt_object.get("path"))
    if path is None:
        return ["counterfact_receipt_path_invalid"], None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["counterfact_receipt_not_parseable_json"], None
    document_object = _json_object(document)
    if document_object is None:
        return ["counterfact_receipt_not_json_object"], None
    detail = _json_object(document_object.get("counterfact"))
    if detail is None:
        return ["counterfact_details_missing"], None

    issues: list[str] = []
    expected = detail.get("expected")
    observed = detail.get("observed")
    reason = detail.get("reason")
    if not isinstance(expected, dict) or not expected:
        issues.append("counterfact_expected_missing")
    if not isinstance(observed, dict) or not observed:
        issues.append("counterfact_observed_missing")
    if not isinstance(reason, str) or not reason.strip():
        issues.append("counterfact_reason_missing")
    if isinstance(expected, dict) and isinstance(observed, dict) and expected != observed:
        issues.append("counterfact_observation_mismatch")
    return issues, detail


def _validate_primary_review(
    review: object,
    *,
    identity: tuple[str, str],
    repo_root: Path,
    phase_claims: dict[str, object],
    phase_evidence_valid: bool,
) -> PrimaryReviewValidation:
    provider, upstream_model = identity
    review_object = _json_object(review)
    if review_object is None:
        return {
            "valid": False,
            "stale": False,
            "decision": None,
            "reviewer_type": None,
            "reviewer_id": None,
            "reviewer_model": None,
            "reviewer_role": None,
            "evidence_snapshot_status": None,
            "issues": ["primary_review_missing"],
        }
    review = review_object
    issues: list[str] = []
    if review.get("provider") != provider or review.get("upstream_model") != upstream_model:
        issues.append("primary_review_identity_mismatch")
    reviewer_type = review.get("reviewer_type")
    if not isinstance(reviewer_type, str) or reviewer_type not in {"primary_model", "human"}:
        issues.append("primary_review_reviewer_type_unrecognized")
    reviewer_id = review.get("reviewer_id")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        issues.append("primary_review_reviewer_id_missing")
    reviewer_model = review.get("reviewer_model")
    if reviewer_type == "primary_model":
        if not isinstance(reviewer_model, str) or not reviewer_model.strip():
            issues.append("primary_review_model_missing")
    elif reviewer_type == "human" and reviewer_model not in (None, ""):
        issues.append("primary_review_human_model_must_be_empty")
    reviewer_role = review.get("reviewer_role")
    if not isinstance(reviewer_role, str) or not reviewer_role.strip():
        issues.append("primary_review_role_missing")
    decision = review.get("decision")
    if not isinstance(decision, str) or decision not in {"ACCEPT", "REJECT"}:
        issues.append("primary_review_decision_missing_or_invalid")
    if not _utc_timestamp(review.get("reviewed_at_utc")):
        issues.append("primary_review_timestamp_missing_or_invalid")
    receipt_issues, stale, receipt_relative_path = _validate_receipt(
        review.get("receipt"),
        provider=provider,
        upstream_model=upstream_model,
        repo_root=repo_root,
        label="primary_review",
    )
    issues.extend(receipt_issues)
    receipt_path = (
        _safe_repo_path(repo_root, receipt_relative_path)
        if receipt_relative_path is not None
        else None
    )
    evidence_snapshot_status = "MISSING_OR_INVALID"
    if receipt_path is not None:
        try:
            receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            receipt_document = None
        receipt_object = _json_object(receipt_document)
        if receipt_object is not None:
            issues.extend(
                f"primary_review_receipt_{field}_mismatch"
                for field in (
                    "reviewer_type",
                    "reviewer_id",
                    "reviewer_model",
                    "reviewer_role",
                    "decision",
                    "reviewed_at_utc",
                )
                if receipt_object.get(field) != review.get(field)
            )
            expected_snapshot = _claim_evidence_snapshot(phase_claims)
            actual_snapshot = _normalize_review_evidence_snapshot(
                receipt_object.get("evidence_snapshot")
            )
            if expected_snapshot is None:
                issues.append("primary_review_evidence_snapshot_current_claims_missing_or_invalid")
            elif actual_snapshot is None:
                issues.append("primary_review_evidence_snapshot_missing_or_invalid")
            elif actual_snapshot != expected_snapshot:
                issues.append("primary_review_evidence_snapshot_mismatch")
                stale = phase_evidence_valid
                evidence_snapshot_status = "MISMATCH"
            else:
                evidence_snapshot_status = "MATCHED"
    else:
        issues.append("primary_review_receipt_not_readable")
    return {
        "valid": not issues and not stale,
        "stale": stale,
        "decision": decision,
        "reviewer_type": reviewer_type,
        "reviewer_id": reviewer_id,
        "reviewer_model": reviewer_model,
        "reviewer_role": reviewer_role,
        "evidence_snapshot_status": evidence_snapshot_status,
        "issues": sorted(set(issues)),
    }


def _normalize_review_pin_entries(value: object) -> tuple[tuple[str, str, str], ...] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized: list[tuple[str, str, str]] = []
    required_keys = {"path", "before_sha256", "after_sha256"}
    for item in value:
        if not isinstance(item, dict) or set(item) != required_keys:
            return None
        path = item.get("path")
        before = item.get("before_sha256")
        after = item.get("after_sha256")
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(before, str)
            or not HEX_SHA256.fullmatch(before)
            or not isinstance(after, str)
            or not HEX_SHA256.fullmatch(after)
        ):
            return None
        normalized.append((path, before, after))
    return tuple(sorted(normalized))


def _normalize_review_evidence_snapshot(snapshot: object) -> tuple[object, ...] | None:
    if not isinstance(snapshot, dict) or set(snapshot) != {"phases"}:
        return None
    phases = snapshot.get("phases")
    if not isinstance(phases, dict) or set(phases) != set(REQUIRED_PHASES):
        return None

    normalized_phases: list[tuple[object, ...]] = []
    for phase in REQUIRED_PHASES:
        phase_snapshot = phases.get(phase)
        if not isinstance(phase_snapshot, dict) or set(phase_snapshot) != {
            "receipt",
            "source_pins",
            "contract_pins",
            "contract_version",
        }:
            return None
        receipt = phase_snapshot.get("receipt")
        if not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}:
            return None
        receipt_path = receipt.get("path")
        receipt_sha = receipt.get("sha256")
        if (
            not isinstance(receipt_path, str)
            or not receipt_path.strip()
            or not isinstance(receipt_sha, str)
            or not HEX_SHA256.fullmatch(receipt_sha)
        ):
            return None
        source_pins = _normalize_review_pin_entries(phase_snapshot.get("source_pins"))
        contract_pins = _normalize_review_pin_entries(phase_snapshot.get("contract_pins"))
        contract_version = phase_snapshot.get("contract_version")
        if (
            source_pins is None
            or contract_pins is None
            or not isinstance(contract_version, str)
            or not contract_version.strip()
            or contract_version not in {f"sha256:{pin[2]}" for pin in contract_pins}
        ):
            return None
        normalized_phases.append(
            (
                phase,
                receipt_path,
                receipt_sha,
                source_pins,
                contract_pins,
                contract_version,
            )
        )
    return tuple(normalized_phases)


def _claim_evidence_snapshot(
    phase_claims: dict[str, object],
) -> tuple[object, ...] | None:
    if not isinstance(phase_claims, dict) or set(phase_claims) != set(REQUIRED_PHASES):
        return None
    phase_snapshots: dict[str, JsonObject] = {}
    for phase in REQUIRED_PHASES:
        claim = _json_object(phase_claims.get(phase))
        if claim is None:
            return None
        receipt = _json_object(claim.get("receipt"))
        pins = _json_object(claim.get("pins"))
        if receipt is None or pins is None:
            return None
        phase_snapshots[phase] = {
            "receipt": {
                "path": receipt.get("path"),
                "sha256": receipt.get("sha256"),
            },
            "source_pins": pins.get("source"),
            "contract_pins": pins.get("contract"),
            "contract_version": pins.get("contract_version"),
        }
    return _normalize_review_evidence_snapshot({"phases": phase_snapshots})


def build_report(
    inventory: Inventory,
    claims_document: object,
    *,
    claims_sha256: str,
    claims_path: str,
    repo_root: Path = REPO_ROOT,
) -> ModelEvidenceReport:
    """Build an auditable report from the fixed inventory and explicit claims."""
    claims_object = _json_object(claims_document)
    if claims_object is None:
        claims_object = {}
    claims_document = claims_object
    global_issues: list[str] = []
    if claims_document.get("schema_version") != SCHEMA_VERSION:
        global_issues.append("claims_schema_version_missing_or_unsupported")
    if claims_document.get("identity_set_sha256") != inventory["identity_set_sha256"]:
        global_issues.append("claims_identity_set_pin_mismatch")

    if "claims" not in claims_document:
        global_issues.append("claims_array_missing")
    raw_claims = _json_array(claims_document.get("claims", []))
    if raw_claims is None:
        raw_claims = []
        global_issues.append("claims_must_be_an_explicit_array")
    if "primary_reviews" not in claims_document:
        global_issues.append("primary_reviews_array_missing")
    raw_reviews = _json_array(claims_document.get("primary_reviews", []))
    if raw_reviews is None:
        raw_reviews = []
        global_issues.append("primary_reviews_must_be_an_explicit_array")

    inventory_lookup = {(row["provider"], row["upstream_model"]): row for row in inventory["rows"]}
    claims_by_identity_phase: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    invalid_claims: list[dict[str, object]] = []
    for position, claim in enumerate(raw_claims):
        claim_object = _json_object(claim)
        if claim_object is None:
            invalid_claims.append({"index": position, "issues": ["claim_not_object"]})
            continue
        claim = claim_object
        provider, upstream_model, phase = (
            claim.get("provider"),
            claim.get("upstream_model"),
            claim.get("phase"),
        )
        if not isinstance(provider, str) or not isinstance(upstream_model, str):
            invalid_claims.append(
                {"index": position, "issues": ["claim_identity_missing_or_invalid"]}
            )
            continue
        identity = (provider, upstream_model)
        if identity not in inventory_lookup:
            invalid_claims.append(
                {
                    "index": position,
                    "provider": provider,
                    "upstream_model": upstream_model,
                    "phase": phase,
                    "issues": ["unknown_inventory_identity"],
                }
            )
            continue
        if phase not in REQUIRED_PHASES:
            invalid_claims.append(
                {
                    "index": position,
                    "provider": provider,
                    "upstream_model": upstream_model,
                    "phase": phase,
                    "issues": ["phase_unknown"],
                }
            )
            continue
        key = (provider, upstream_model, phase)
        claims_by_identity_phase.setdefault(key, []).append(claim)

    reviews_by_identity: dict[tuple[str, str], list[object]] = {}
    for review in raw_reviews:
        review_object = _json_object(review)
        if review_object is None:
            invalid_claims.append({"kind": "primary_review", "issues": ["review_not_object"]})
            continue
        review = review_object
        provider = review.get("provider")
        upstream_model = review.get("upstream_model")
        if not isinstance(provider, str) or not isinstance(upstream_model, str):
            invalid_claims.append(
                {"kind": "primary_review", "issues": ["review_identity_missing_or_invalid"]}
            )
            continue
        identity = (provider, upstream_model)
        if identity not in inventory_lookup:
            invalid_claims.append(
                {
                    "kind": "primary_review",
                    "provider": identity[0],
                    "upstream_model": identity[1],
                    "issues": ["unknown_inventory_identity"],
                }
            )
            continue
        reviews_by_identity.setdefault(identity, []).append(review)

    model_results: list[ModelResult] = []
    for row in inventory["rows"]:
        identity = (row["provider"], row["upstream_model"])
        phase_results: dict[str, PhaseResult] = {}
        row_stale = False
        row_has_claims = False
        missing_phases: list[str] = []
        issues: list[str] = []
        review_phase_claims: dict[str, object] = {}
        for phase in REQUIRED_PHASES:
            candidates = claims_by_identity_phase.get((*identity, phase), [])
            if not candidates:
                missing_phases.append(phase)
                continue
            row_has_claims = True
            if len(candidates) != 1:
                duplicate_issue = "duplicate_identity_phase_claim"
                issues.append(duplicate_issue)
                phase_results[phase] = {
                    "claim_count": len(candidates),
                    "status": "STALE",
                    "issues": [duplicate_issue],
                    "receipts": [
                        receipt.get("path")
                        for c in candidates
                        for receipt in [_json_object(c.get("receipt"))]
                        if receipt is not None
                    ],
                }
                row_stale = True
                continue
            review_phase_claims[phase] = candidates[0]
            validated = _validate_claim(candidates[0], identity=identity, repo_root=repo_root)
            phase_results[phase] = {
                "claim_count": 1,
                "status": "STALE"
                if validated["stale"]
                else ("CANDIDATE" if validated["valid"] else "INCOMPLETE"),
                "claim_status": validated.get("status"),
                "evidence_kind": validated.get("evidence_kind"),
                "receipt": validated.get("receipt_path"),
                "counterfact": validated.get("counterfact"),
                "issues": validated["issues"],
            }
            row_stale = row_stale or validated["stale"]
            issues.extend(validated["issues"])

        review_candidates = reviews_by_identity.get(identity, [])
        phase_evidence_valid = not missing_phases and all(
            phase_results.get(phase, {}).get("status") == "CANDIDATE" for phase in REQUIRED_PHASES
        )
        review_result: PrimaryReviewValidation
        if len(review_candidates) > 1:
            review_result = {
                "valid": False,
                "stale": True,
                "decision": None,
                "reviewer_type": None,
                "reviewer_id": None,
                "reviewer_model": None,
                "reviewer_role": None,
                "evidence_snapshot_status": None,
                "issues": ["duplicate_primary_review"],
            }
            row_stale = True
        elif review_candidates:
            review_result = _validate_primary_review(
                review_candidates[0],
                identity=identity,
                repo_root=repo_root,
                phase_claims=review_phase_claims,
                phase_evidence_valid=phase_evidence_valid,
            )
            row_stale = row_stale or review_result["stale"]
        else:
            review_result = {
                "valid": False,
                "stale": False,
                "decision": None,
                "reviewer_type": None,
                "reviewer_id": None,
                "reviewer_model": None,
                "reviewer_role": None,
                "evidence_snapshot_status": None,
                "issues": ["primary_review_missing"],
            }

        if row_stale:
            status = "STALE"
        elif not row_has_claims:
            status = "UNREVIEWED"
        elif (
            missing_phases
            or any(
                phase_results.get(phase, {}).get("status") != "CANDIDATE"
                for phase in REQUIRED_PHASES
            )
            or global_issues
            or invalid_claims
            or not review_result["valid"]
        ):
            status = "INCOMPLETE"
        elif review_result["decision"] == "REJECT":
            status = "REJECTED"
        elif review_result["decision"] == "ACCEPT":
            status = "OFFLINE_ACCEPTED"
        else:
            status = "INCOMPLETE"

        missing_material = [f"add {phase} claim" for phase in missing_phases]
        for phase, detail in phase_results.items():
            if detail.get("status") in {"STALE", "INCOMPLETE"}:
                issue_codes = ", ".join(detail["issues"]) or "evidence validation failed"
                missing_material.append(f"repair {phase} evidence: {issue_codes}")
        if not review_result["valid"]:
            missing_material.append("add externally recorded primary-model or human review receipt")
        model_results.append(
            {
                **row,
                "model_identity": f"{row['provider']}::{row['upstream_model']}",
                "status": status,
                "phases": phase_results,
                "missing_phases": missing_phases,
                "primary_review": {
                    "decision": review_result["decision"],
                    "reviewer_type": review_result.get("reviewer_type"),
                    "reviewer_id": review_result.get("reviewer_id"),
                    "reviewer_model": review_result.get("reviewer_model"),
                    "reviewer_role": review_result.get("reviewer_role"),
                    "evidence_snapshot_status": review_result.get("evidence_snapshot_status"),
                    "status": "VALID" if review_result["valid"] else "MISSING_OR_INVALID",
                    "issues": review_result["issues"],
                },
                "issues": sorted(set(issues + review_result["issues"])),
                "missing_material": missing_material,
            }
        )

    counts: dict[str, int] = {}
    for model in model_results:
        counts[model["status"]] = counts.get(model["status"], 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "acceptance_rule": (
            "all five phase claims plus an explicit valid primary-model or human "
            "ACCEPT receipt; claim PASS alone is candidate only"
        ),
        "inventory": {
            "path": inventory["path"],
            "csv_sha256": inventory["csv_sha256"],
            "identity_count": inventory["identity_count"],
            "identity_set_sha256": inventory["identity_set_sha256"],
            "expected_identity_count": EXPECTED_MODEL_COUNT,
            "expected_identity_set_sha256": EXPECTED_IDENTITY_SHA256,
            "integrity": "PINNED_EXACT_SET",
        },
        "claims_input": {"path": claims_path, "sha256": claims_sha256},
        "claims_status_source": (
            "explicit JSON claims only; no directory globbing or markdown status parsing"
        ),
        "global_issues": sorted(set(global_issues)),
        "invalid_claims": invalid_claims,
        "summary": {
            "identity_count": len(model_results),
            "claim_count": len(raw_claims),
            "primary_review_count": len(raw_reviews),
            "status_counts": dict(sorted(counts.items())),
            "offline_acceptance_count": counts.get("OFFLINE_ACCEPTED", 0),
        },
        "models": model_results,
    }


def render_markdown(report: ModelEvidenceReport) -> str:
    """Render a compact Markdown view of an already-built evidence report."""
    inventory = report["inventory"]
    summary = report["summary"]
    status_counts = json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True)
    lines = [
        "# Iteration 2 model evidence index",
        "",
        (
            "This is an evidence completeness index, not a provider, production, or "
            "release acceptance report."
        ),
        (
            "A claim status of `PASS` is only a candidate. Offline acceptance requires all "
            "five phase claims and an explicit valid external human or primary-model "
            "`ACCEPT` review receipt."
        ),
        "",
        (
            f"- Fixed identities: **{inventory['identity_count']}** "
            f"(expected **{inventory['expected_identity_count']}**)."
        ),
        f"- Identity projection SHA-256: `{inventory['identity_set_sha256']}`.",
        f"- Input CSV SHA-256: `{inventory['csv_sha256']}`.",
        (
            f"- Claims input: `{report['claims_input']['path']}` "
            f"(SHA-256 `{report['claims_input']['sha256']}`)."
        ),
        f"- Offline accepted: **{summary['offline_acceptance_count']}**.",
        f"- Status counts: `{status_counts}`.",
        "",
        (
            "| Provider | Upstream model | Status | Recorded phase claims | Missing "
            "phases | Missing evidence / follow-up | Receipt links |"
        ),
        "|---|---|---|---|---|---|---|",
    ]
    for model in report["models"]:
        recorded_phases = [
            f"{phase}:{detail.get('status')}" for phase, detail in model["phases"].items()
        ]
        receipts: list[str] = []
        for detail in model["phases"].values():
            path_value = detail.get("receipt")
            if isinstance(path_value, str):
                target = PurePosixPath(path_value)
                rel = (
                    PurePosixPath("..").joinpath(*target.parts[3:])
                    if target.parts[:3] == ("docs", "evidence", "C100")
                    else target
                )
                receipts.append(f"[{target.name}]({rel.as_posix()})")
        missing = list(model["missing_material"])
        if not missing:
            missing = ["none"]
        lines.append(
            (
                "| {provider} | {upstream} | **{status}** | {candidate} | "
                "{missing_phases} | {material} | {receipts} |"
            ).format(
                provider=model["provider"],
                upstream=model["upstream_model"],
                status=model["status"],
                candidate=", ".join(recorded_phases) or "—",
                missing_phases=", ".join(model["missing_phases"]) or "—",
                material="; ".join(missing),
                receipts="; ".join(receipts) or "—",
            )
        )
    lines.extend(
        [
            "",
            (
                "`STALE` means a receipt or pinned source/fixture/probe/contract hash no "
                "longer matches, or pre/post hashes drifted. Time passing alone does not "
                "expire an offline snapshot."
            ),
            (
                "`UNREVIEWED` means there are no claims for that identity. `INCOMPLETE` "
                "means claims exist but evidence phases, pins, command outcome, or "
                "external primary review are missing or invalid."
            ),
            "",
            (
                "The exact claim schema and CLI are documented in [README.md](README.md). "
                "The indexer does not create primary reviews, alter the task CSV, run "
                "provider/database/network checks, or infer status from pytest totals."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceIndexError(f"cannot read JSON input {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments, build the index, and write JSON plus Markdown."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, help="fixed model task CSV")
    parser.add_argument("--claims", required=True, help="explicit model evidence claims JSON")
    parser.add_argument(
        "--output", required=True, help="output JSON path; Markdown is written beside it"
    )
    args = parser.parse_args(argv)

    inventory_path = Path(args.inventory)
    claims_path = Path(args.claims)
    output_path = Path(args.output)
    try:
        inventory = load_inventory(inventory_path)
        claims_bytes = claims_path.read_bytes()
        claims_document = json.loads(claims_bytes.decode("utf-8"))
        report = build_report(
            inventory,
            claims_document,
            claims_sha256=sha256_bytes(claims_bytes),
            claims_path=claims_path.as_posix(),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, EvidenceIndexError) as exc:
        print(f"model evidence index failed closed: {exc}", file=sys.stderr)
        return 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = output_path.with_suffix(".md")
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(
        f"wrote {output_path} and {markdown_path}: "
        f"{report['summary']['identity_count']} identities, "
        f"{report['summary']['offline_acceptance_count']} offline accepted"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
