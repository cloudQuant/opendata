"""Validate individually pinned historical evidence before A2 static partitioning.

This module never discovers exemptions by directory or filename pattern. The
only candidate identities come from the pinned C100 B-class classification,
and a candidate is omitted from static remediation only after its source bytes,
receipt provenance, active source relationship, and independently pinned review
artifact all agree. Secret scanning is separate and continues to inspect the
candidate files themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess  # nosec B404  # pinned gitleaks argv; no shell
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

CLASSIFICATION_PATH = (
    "docs/evidence/C100/quality-scope-audit/immutable-wave9-exact5055/classification.json"
)
CLASSIFICATION_SHA256 = "2f28046d002f37764d80e2cdc1ef80cfff8998a1ff575b75e0a5530d1cfb8ffe"
CLASSIFICATION_RUN_ID = "20261013T135220.000000Z"
MANIFEST_PATH = "docs/quality/immutable-evidence.json"
# Updated only after a separately reviewed manifest is approved. Pending rows
# cannot be activated by editing JSON alone.
MANIFEST_SHA256_PIN = "6edd0786c62b1e7aace510032623cf8f302cbd6553530847de2fb7d37710ee21"
APPROVAL_ARTIFACT_SHA256_PIN: str | None = (
    "5cbafa2ec91976ea7123d7bad7695c3ed9873d04ea6e21fc7c7cda5617eb8e23"
)
SCAN_DEVICE_REL_PATH = "docs/quality/secret-scan.json"
SCAN_CONFIG_REL_PATH = ".gitleaks.toml"
MANIFEST_SCHEMA_VERSION = 1
APPROVAL_SCHEMA_VERSION = 1
IMMUTABLE_DECISION = "approve_exact_immutable_static_remediation_exemption"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "classification",
    "active_entrypoints",
    "historical_diagnostics",
    "approval",
    "candidates",
}
_CLASSIFICATION_KEYS = {"path", "sha256", "run_id", "eligible_category"}
_CANDIDATE_KEYS = {
    "path",
    "sha256",
    "classification_category",
    "active_entrypoint",
    "source_path",
    "archival_reason",
    "receipt",
    "review",
}
_RECEIPT_KEYS = {
    "path",
    "sha256",
    "source_sha256_pointer",
    "command_pointer",
    "executed_at_utc_pointer",
    "missing_execution_time_reason",
    "installed_directory_pointer",
    "source_relative_path_pointer",
}
_REVIEW_KEYS = {"status", "reviewer", "reviewed_at_utc", "decision", "note"}
_APPROVAL_KEYS = {"status", "artifact_path", "artifact_sha256"}
_APPROVAL_ARTIFACT_KEYS = {"schema_version", "decision", "items"}
_APPROVAL_ITEM_KEYS = {
    "path",
    "sha256",
    "receipt_path",
    "receipt_sha256",
    "source_sha256_pointer",
    "decision",
    "reviewer",
    "reviewed_at_utc",
}


@dataclass(frozen=True)
class EvidenceValidation:
    """Validated candidate partitions and reasons that still block A2."""

    errors: tuple[str, ...]
    blocked_paths: tuple[str, ...]
    blocked_details: tuple[tuple[str, tuple[str, ...]], ...]
    excluded_paths: tuple[str, ...]
    snapshot_paths: tuple[str, ...]
    active_entrypoints: tuple[str, ...]
    historical_ruff_count: int

    @property
    def blocked(self) -> bool:
        """Whether the candidate manifest or any candidate remains unapproved."""
        return bool(self.errors or self.blocked_paths)


@dataclass(frozen=True)
class SecretScanResult:
    """Outcome of scanning exact immutable evidence files with pinned Gitleaks."""

    errors: tuple[str, ...]
    scanned_paths: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether every requested file was scanned without findings or tool drift."""
        return not self.errors


def _sha256(data: bytes) -> str:
    """Return the lowercase SHA-256 digest for bytes."""
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: object, label: str) -> str:
    """Validate a canonical, root-relative POSIX path string."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{label} is not a canonical root-relative path: {value!r}")
    return value


def _regular_file(root: Path, relative: object, label: str) -> tuple[Path, bytes]:
    """Read a root-contained regular file while rejecting symlinks in every component."""
    name = _relative_path(relative, label)
    parts = PurePosixPath(name).parts
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except OSError as error:
            raise ValueError(f"{label} is missing or unreadable: {name}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"{label} traverses a symlink: {name}")
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"{label} has a non-directory parent: {name}")
        if index == len(parts) - 1 and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} is not a regular file: {name}")
    resolved = cursor.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} escapes repository root: {name}") from error
    return cursor, cursor.read_bytes()


def _strict_object(value: object, expected: set[str], label: str) -> dict[str, Any]:
    """Require a JSON object with exactly the expected keys."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"{label} schema mismatch; missing={missing}, unknown={unknown}")
    return value


def _json_document(
    root: Path,
    path_value: object,
    label: str,
    document_cache: dict[tuple[str, str], dict[str, object]] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Read a JSON object and return its normalized path, digest, and value.

    The caller may reuse a parsed object only after this call has revalidated the
    path, read the file, and computed its current digest. Cache entries are scoped
    to one manifest validation and keyed by both canonical path and observed SHA.
    """
    path_name = _relative_path(path_value, f"{label} path")
    _, raw = _regular_file(root, path_name, label)
    digest = _sha256(raw)
    cache_key = (path_name, digest)
    if document_cache is not None and cache_key in document_cache:
        return path_name, digest, document_cache[cache_key]
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON: {path_name}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} JSON root must be an object: {path_name}")
    if document_cache is not None:
        document_cache[cache_key] = value
    return path_name, digest, value


def _json_pointer(document: object, pointer: object, label: str) -> object:
    """Resolve a strict RFC 6901 JSON pointer against a decoded value."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError(f"{label} must be a non-root JSON pointer")
    current = document
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise ValueError(f"{label} contains an invalid JSON Pointer escape")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"{label} does not resolve: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdecimal() or (len(token) > 1 and token.startswith("0")):
                raise ValueError(f"{label} has an invalid array index: {pointer}")
            index = int(token)
            if index >= len(current):
                raise ValueError(f"{label} does not resolve: {pointer}")
            current = current[index]
        else:
            raise ValueError(f"{label} cannot traverse scalar JSON data: {pointer}")
    return current


def _is_utc_timestamp(value: object) -> bool:
    """Return whether a value is an explicit ISO-8601 UTC timestamp."""
    if not isinstance(value, str) or not value:
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() == timezone.utc.utcoffset(
        timestamp
    )


def _classification_rows(
    root: Path,
    expected_sha256: str,
    document_cache: dict[tuple[str, str], dict[str, object]],
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Load the pinned classification and return its exact B rows plus diagnostics."""
    errors: list[str] = []
    snapshots: list[str] = []
    try:
        classification_path, digest, document = _json_document(
            root, CLASSIFICATION_PATH, "pinned classification", document_cache
        )
    except ValueError as error:
        return {}, snapshots, [str(error)]
    if classification_path != CLASSIFICATION_PATH:
        errors.append("classification path differs from the code-pinned path")
    if digest != expected_sha256:
        errors.append(f"classification SHA-256 mismatch: expected {expected_sha256}, got {digest}")
    if document.get("generated_at_utc") is None or not isinstance(document.get("files"), list):
        errors.append("classification does not carry generated_at_utc and files")
        return {}, snapshots, errors

    rows: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(document["files"]):
        if not isinstance(item, dict) or item.get("category") != "B":
            continue
        try:
            path_name = _relative_path(item.get("path"), f"classification B row {index} path")
        except ValueError as error:
            errors.append(str(error))
            continue
        if path_name in rows:
            errors.append(f"duplicate classification B path: {path_name}")
            continue
        digest_value = item.get("captured_source_sha256")
        if not isinstance(digest_value, str) or SHA256_PATTERN.fullmatch(digest_value) is None:
            errors.append(f"invalid captured source SHA-256 in classification: {path_name}")
            continue
        finding_count = item.get("ruff_finding_count")
        if (
            not isinstance(finding_count, int)
            or isinstance(finding_count, bool)
            or finding_count < 0
        ):
            errors.append(
                "pinned classification B row ruff_finding_count must be a "
                f"non-negative integer: {path_name}"
            )
        rows[path_name] = item
        try:
            _regular_file(root, path_name, f"classified snapshot {path_name}")
        except ValueError as error:
            errors.append(str(error))
            continue
        snapshots.append(path_name)
    return rows, sorted(snapshots), errors


def _review_artifact_items(
    root: Path,
    manifest: dict[str, Any],
    expected_sha256: str | None,
    document_cache: dict[tuple[str, str], dict[str, object]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Load a separately pinned approval artifact; pending state yields no approvals."""
    errors: list[str] = []
    approval = _strict_object(manifest["approval"], _APPROVAL_KEYS, "approval")
    status = approval.get("status")
    if status == "pending_review":
        if approval.get("artifact_path") is not None or approval.get("artifact_sha256") is not None:
            errors.append("pending review must not carry an approval artifact")
        if expected_sha256 is not None:
            errors.append("approval artifact pin exists while the manifest is still pending")
        return {}, errors
    if status != "approved" or not isinstance(expected_sha256, str):
        errors.append("approval is not backed by a trusted code-pinned review artifact")
        return {}, errors

    try:
        artifact_path, digest, artifact = _json_document(
            root, approval.get("artifact_path"), "approval artifact", document_cache
        )
    except ValueError as error:
        return {}, [str(error)]
    if digest != expected_sha256 or approval.get("artifact_sha256") != expected_sha256:
        errors.append(
            "approval artifact SHA-256 is not code-pinned: "
            f"expected {expected_sha256}, got {digest}"
        )
    if artifact_path != approval.get("artifact_path"):
        errors.append("approval artifact path is not canonical")
    try:
        artifact = _strict_object(artifact, _APPROVAL_ARTIFACT_KEYS, "approval artifact")
    except ValueError as error:
        return {}, errors + [str(error)]
    if artifact.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        errors.append("approval artifact schema_version is unsupported")
    if artifact.get("decision") != "approve_exact_manifest_rows":
        errors.append("approval artifact decision is not the exact-row approval decision")
    items = artifact.get("items")
    if not isinstance(items, list):
        errors.append("approval artifact items must be a list")
        return {}, errors
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        try:
            row = _strict_object(item, _APPROVAL_ITEM_KEYS, f"approval item {index}")
            path_name = _relative_path(row.get("path"), f"approval item {index} path")
        except ValueError as error:
            errors.append(str(error))
            continue
        if path_name in result:
            errors.append(f"duplicate approval item path: {path_name}")
            continue
        result[path_name] = row
    return result, errors


def validate_manifest(
    root: Path,
    *,
    expected_manifest_sha256: str | None = MANIFEST_SHA256_PIN,
    expected_classification_sha256: str = CLASSIFICATION_SHA256,
    expected_approval_sha256: str | None = APPROVAL_ARTIFACT_SHA256_PIN,
) -> EvidenceValidation:
    """Validate the pinned manifest and return exact paths safe for static partitioning.

    A missing pin, unreviewed row, incomplete provenance fact, or mismatch never
    produces an exclusion. The full source resolver remains owned by a2_check
    and is not changed by this module.
    """
    repository = root.resolve(strict=True)
    fatal: list[str] = []
    blocked: dict[str, list[str]] = {}
    document_cache: dict[tuple[str, str], dict[str, object]] = {}
    classification_rows, snapshot_paths, classification_errors = _classification_rows(
        repository, expected_classification_sha256, document_cache
    )
    fatal.extend(classification_errors)
    try:
        _, raw = _regular_file(repository, MANIFEST_PATH, "immutable evidence manifest")
        manifest_path = MANIFEST_PATH
        manifest_digest = _sha256(raw)
        try:
            loaded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"manifest is not valid UTF-8 JSON: {error}") from error
        if not isinstance(loaded, dict):
            raise ValueError("manifest JSON root must be an object")
        manifest = _strict_object(loaded, _TOP_LEVEL_KEYS, "manifest")
    except ValueError as error:
        return EvidenceValidation(
            errors=tuple(fatal + [str(error)]),
            blocked_paths=tuple(sorted(classification_rows)),
            blocked_details=tuple(
                (path_name, ("manifest could not be validated",))
                for path_name in sorted(classification_rows)
            ),
            excluded_paths=(),
            snapshot_paths=tuple(snapshot_paths),
            active_entrypoints=(),
            historical_ruff_count=0,
        )

    if manifest_path != MANIFEST_PATH:
        fatal.append("manifest path differs from the code-pinned path")
    if expected_manifest_sha256 is None:
        fatal.append("immutable evidence manifest has no trusted code-level SHA-256 pin")
    elif manifest_digest != expected_manifest_sha256:
        fatal.append(
            f"manifest SHA-256 mismatch: expected {expected_manifest_sha256}, got {manifest_digest}"
        )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        fatal.append("manifest schema_version is unsupported")

    try:
        classification = _strict_object(
            manifest.get("classification"), _CLASSIFICATION_KEYS, "manifest classification"
        )
    except ValueError as error:
        fatal.append(str(error))
        classification = {}
    if classification:
        if classification.get("path") != CLASSIFICATION_PATH:
            fatal.append("manifest classification path differs from the code-pinned path")
        if classification.get("sha256") != expected_classification_sha256:
            fatal.append("manifest classification SHA-256 differs from the code pin")
        if classification.get("run_id") != CLASSIFICATION_RUN_ID:
            fatal.append("manifest classification run_id is not the reviewed inventory")
        if classification.get("eligible_category") != "B":
            fatal.append("manifest classification category must be exactly B")

    active_value = manifest.get("active_entrypoints")
    if not isinstance(active_value, list) or not all(
        isinstance(item, str) for item in active_value
    ):
        fatal.append("active_entrypoints must be a list of root-relative paths")
        active_paths: list[str] = []
    else:
        active_paths = []
        for index, item in enumerate(active_value):
            try:
                active_path = _relative_path(item, f"active_entrypoints[{index}]")
                _regular_file(repository, active_path, f"active entrypoint {active_path}")
            except ValueError as error:
                fatal.append(str(error))
                continue
            if active_path in active_paths:
                fatal.append(f"duplicate active entrypoint: {active_path}")
            active_paths.append(active_path)

    historical = manifest.get("historical_diagnostics")
    historical_count = 0
    try:
        historical = _strict_object(
            historical,
            {"ruff_count", "classification_sha256", "status"},
            "historical_diagnostics",
        )
        raw_historical_count = historical.get("ruff_count")
        if (
            not isinstance(raw_historical_count, int)
            or isinstance(raw_historical_count, bool)
            or raw_historical_count < 0
        ):
            fatal.append("historical_diagnostics.ruff_count must be a non-negative integer")
            historical_count = 0
        else:
            historical_count = raw_historical_count
        if historical.get("classification_sha256") != expected_classification_sha256:
            fatal.append("historical diagnostic count is not bound to the pinned classification")
        if historical.get("status") != "NOT_GREEN_HISTORICAL_FINDINGS":
            fatal.append("historical Ruff findings must remain explicitly non-green")
        row_finding_counts = [
            item.get("ruff_finding_count") for item in classification_rows.values()
        ]
        if (
            row_finding_counts
            and all(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0
                for count in row_finding_counts
            )
            and isinstance(raw_historical_count, int)
            and not isinstance(raw_historical_count, bool)
            and raw_historical_count >= 0
            and historical.get("classification_sha256") == expected_classification_sha256
        ):
            typed_row_finding_counts: list[int] = [
                count
                for count in row_finding_counts
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0
            ]
            row_finding_total = sum(typed_row_finding_counts)
            if historical_count != row_finding_total:
                fatal.append(
                    "historical_diagnostics.ruff_count must equal the sum of pinned "
                    f"classification B-row ruff_finding_count values ({row_finding_total})"
                )
    except ValueError as error:
        fatal.append(str(error))

    rows_value = manifest.get("candidates")
    if not isinstance(rows_value, list):
        fatal.append("manifest candidates must be a list")
        rows: list[object] = []
    else:
        rows = rows_value
    manifest_rows: dict[str, dict[str, Any]] = {}
    for index, raw_row in enumerate(rows):
        try:
            row = _strict_object(raw_row, _CANDIDATE_KEYS, f"candidate {index}")
            path_name = _relative_path(row.get("path"), f"candidate {index} path")
        except ValueError as error:
            fatal.append(str(error))
            continue
        if path_name in manifest_rows:
            fatal.append(f"duplicate candidate path: {path_name}")
            continue
        manifest_rows[path_name] = row

    if set(manifest_rows) != set(classification_rows):
        missing = sorted(set(classification_rows) - set(manifest_rows))
        unknown = sorted(set(manifest_rows) - set(classification_rows))
        fatal.append(
            "candidate set differs from pinned B classification; "
            f"missing={missing}, unknown={unknown}"
        )

    approval_items: dict[str, dict[str, Any]] = {}
    try:
        approval_items, approval_errors = _review_artifact_items(
            repository, manifest, expected_approval_sha256, document_cache
        )
        fatal.extend(approval_errors)
    except ValueError as error:
        fatal.append(str(error))

    candidate_facts: dict[str, dict[str, Any]] = {}
    for path_name, row in manifest_rows.items():
        row_errors: list[str] = []
        blocked[path_name] = row_errors
        classified = classification_rows.get(path_name)
        if classified is None:
            row_errors.append("path is not a B-class row in the pinned classification")
        digest = row.get("sha256")
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            row_errors.append("source sha256 is malformed")
        elif classified is not None and digest != classified.get("captured_source_sha256"):
            row_errors.append("source sha256 differs from the pinned classification")
        try:
            _, source_bytes = _regular_file(repository, path_name, f"candidate {path_name}")
            actual_source_digest = _sha256(source_bytes)
            if digest != actual_source_digest:
                row_errors.append(
                    f"source hash mismatch: manifest={digest}, observed={actual_source_digest}"
                )
        except ValueError as error:
            row_errors.append(str(error))
            actual_source_digest = ""
        if row.get("classification_category") != "B":
            row_errors.append("candidate classification_category must be B")

        active_entrypoint = row.get("active_entrypoint")
        if not isinstance(active_entrypoint, str) or active_entrypoint not in active_paths:
            row_errors.append("candidate does not point to a listed active entrypoint")
        if active_entrypoint == path_name:
            row_errors.append("active entrypoint cannot be registered as an immutable snapshot")

        source_path = row.get("source_path")
        if source_path is not None:
            try:
                source_path = _relative_path(source_path, f"source path for {path_name}")
                if source_path == path_name:
                    row_errors.append("installation/source path must differ from the snapshot path")
                _regular_file(repository, source_path, f"repository source for {path_name}")
                if source_path != active_entrypoint:
                    row_errors.append(
                        "installation source_path must be the declared active entrypoint"
                    )
            except ValueError as error:
                row_errors.append(str(error))

        reason = row.get("archival_reason")
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            row_errors.append("archival_reason must state a specific preservation reason")

        try:
            receipt = _strict_object(row.get("receipt"), _RECEIPT_KEYS, f"receipt for {path_name}")
        except ValueError as error:
            row_errors.append(str(error))
            receipt = {}
        receipt_document: dict[str, Any] = {}
        receipt_path: str | None = None
        receipt_digest: str | None = None
        source_pointer: str | None = None
        if receipt:
            try:
                receipt_path, receipt_digest, receipt_document = _json_document(
                    repository,
                    receipt.get("path"),
                    f"receipt for {path_name}",
                    document_cache,
                )
                if receipt.get("sha256") != receipt_digest:
                    row_errors.append(
                        f"receipt hash mismatch for {receipt_path}: "
                        f"manifest={receipt.get('sha256')}, observed={receipt_digest}"
                    )
                source_pointer = receipt.get("source_sha256_pointer")
                pointed_digest = _json_pointer(
                    receipt_document, source_pointer, f"source hash pointer for {path_name}"
                )
                if pointed_digest != digest:
                    row_errors.append(
                        "receipt source hash pointer resolves to "
                        f"{pointed_digest!r}, expected {digest!r}"
                    )
                command_pointer = receipt.get("command_pointer")
                if command_pointer is None:
                    row_errors.append("original execution command is missing")
                else:
                    command = _json_pointer(
                        receipt_document,
                        command_pointer,
                        f"execution command pointer for {path_name}",
                    )
                    if not (
                        isinstance(command, str)
                        and command.strip()
                        or isinstance(command, list)
                        and command
                        and all(isinstance(part, str) and part for part in command)
                    ):
                        row_errors.append("receipt execution command is empty or malformed")
                timestamp_pointer = receipt.get("executed_at_utc_pointer")
                missing_time_reason = receipt.get("missing_execution_time_reason")
                if timestamp_pointer is None:
                    if not isinstance(missing_time_reason, str) or not missing_time_reason.strip():
                        row_errors.append("missing UTC execution time must be stated truthfully")
                    row_errors.append("original UTC execution timestamp is missing")
                else:
                    timestamp = _json_pointer(
                        receipt_document,
                        timestamp_pointer,
                        f"execution time pointer for {path_name}",
                    )
                    if not _is_utc_timestamp(timestamp):
                        row_errors.append(
                            "receipt execution timestamp is not explicit UTC ISO-8601"
                        )
                installed_pointer = receipt.get("installed_directory_pointer")
                if installed_pointer is not None:
                    installed_directory = _json_pointer(
                        receipt_document,
                        installed_pointer,
                        f"installation target pointer for {path_name}",
                    )
                    if not isinstance(installed_directory, str):
                        row_errors.append("installation target pointer does not resolve to a path")
                    else:
                        target = Path(installed_directory).resolve(strict=False)
                        installed_file_parent = (
                            (repository / path_name).resolve(strict=True).parents[1]
                        )
                        if target != installed_file_parent:
                            row_errors.append(
                                "receipt installation target differs from the snapshot location"
                            )
                relative_source_pointer = receipt.get("source_relative_path_pointer")
                if relative_source_pointer is not None:
                    relative_source = _json_pointer(
                        receipt_document,
                        relative_source_pointer,
                        f"source-relative path pointer for {path_name}",
                    )
                    if not isinstance(relative_source, str) or not isinstance(source_path, str):
                        row_errors.append(
                            "receipt source path does not resolve to the declared repository source"
                        )
                    elif not Path(source_path).as_posix().endswith(relative_source):
                        row_errors.append(
                            "receipt source path differs from the declared repository source"
                        )
            except ValueError as error:
                row_errors.append(str(error))

        try:
            review = _strict_object(row.get("review"), _REVIEW_KEYS, f"review for {path_name}")
        except ValueError as error:
            row_errors.append(str(error))
            review = {}
        if review:
            if review.get("status") not in {"pending", "approved"}:
                row_errors.append("review status must be pending or approved")
            if review.get("status") == "pending":
                if any(review.get(field) is not None for field in ("reviewer", "reviewed_at_utc")):
                    row_errors.append("pending review cannot claim reviewer or reviewed timestamp")
                if review.get("decision") is not None:
                    row_errors.append("pending review cannot claim a decision")
                row_errors.append("candidate awaits independent review")
            else:
                reviewer = review.get("reviewer")
                reviewed_at = review.get("reviewed_at_utc")
                if not isinstance(reviewer, str) or not reviewer.strip():
                    row_errors.append("approved review requires a reviewer")
                if not _is_utc_timestamp(reviewed_at):
                    row_errors.append("approved review requires an explicit UTC timestamp")
                if review.get("decision") != IMMUTABLE_DECISION:
                    row_errors.append("approved review has an unsupported decision")
                approval_item = approval_items.get(path_name)
                if approval_item is None:
                    row_errors.append(
                        "approved row is absent from the independently pinned review artifact"
                    )
                elif (
                    approval_item.get("sha256") != digest
                    or approval_item.get("receipt_path") != receipt_path
                    or approval_item.get("receipt_sha256") != receipt_digest
                    or approval_item.get("source_sha256_pointer") != source_pointer
                    or approval_item.get("decision") != IMMUTABLE_DECISION
                    or approval_item.get("reviewer") != reviewer
                    or approval_item.get("reviewed_at_utc") != reviewed_at
                ):
                    row_errors.append(
                        "review artifact does not bind the exact candidate provenance"
                    )
        candidate_facts[path_name] = {
            "sha256": digest,
            "receipt_path": receipt_path,
            "receipt_sha256": receipt_digest,
            "source_sha256_pointer": source_pointer,
            "review": review,
            "errors": row_errors,
        }

    if len(approval_items) > 0:
        unknown_approvals = sorted(set(approval_items) - set(manifest_rows))
        if unknown_approvals:
            fatal.append(f"approval artifact contains unknown candidate paths: {unknown_approvals}")

    excluded: list[str] = []
    if not fatal:
        for path_name, facts in candidate_facts.items():
            if not facts["errors"]:
                excluded.append(path_name)
            else:
                blocked[path_name] = facts["errors"]
    if fatal:
        # A global trust/schema failure invalidates every exemption.
        blocked = {
            path_name: ["manifest-level trust or schema failure"] for path_name in manifest_rows
        }
        excluded = []

    return EvidenceValidation(
        errors=tuple(fatal),
        blocked_paths=tuple(sorted(path for path, reasons in blocked.items() if reasons)),
        blocked_details=tuple(
            (path_name, tuple(reasons)) for path_name, reasons in sorted(blocked.items()) if reasons
        ),
        excluded_paths=tuple(sorted(excluded)),
        snapshot_paths=tuple(snapshot_paths),
        active_entrypoints=tuple(sorted(set(active_paths))),
        historical_ruff_count=historical_count,
    )


def partition_static_files(
    files: list[str], validation: EvidenceValidation
) -> tuple[list[str], list[str]]:
    """Partition an unchanged A2 resolver result into static targets and exclusions."""
    excluded = set(validation.excluded_paths)
    active = set(validation.active_entrypoints)
    if excluded & active:
        raise ValueError(f"active A2 entrypoints cannot be excluded: {sorted(excluded & active)}")
    source_set = set(files)
    removed_active = (source_set & active) - (source_set - excluded)
    if removed_active:
        raise ValueError(
            f"active A2 entrypoints were removed from static checks: {sorted(removed_active)}"
        )
    tool_files = [name for name in files if name not in excluded]
    return tool_files, sorted(source_set & excluded)


def _pinned_gitleaks(root: Path) -> tuple[str, str]:
    """Read the existing secret-scan pin and confirm the executable matches it."""
    _, raw = _regular_file(root, SCAN_DEVICE_REL_PATH, "pinned secret-scan manifest")
    try:
        device = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"secret-scan manifest is not valid JSON: {error}") from error
    if not isinstance(device, dict):
        raise ValueError("secret-scan manifest root must be an object")
    version = device.get("gitleaks_version")
    argv = device.get("scan_argv")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise ValueError("secret-scan manifest has no pinned Gitleaks version")
    if not isinstance(argv, list) or not argv or argv[0] != "gitleaks":
        raise ValueError("secret-scan manifest does not pin the Gitleaks executable")
    if "--config" not in argv:
        raise ValueError("secret-scan manifest does not pin its Gitleaks config")
    config_index = argv.index("--config") + 1
    if config_index >= len(argv) or argv[config_index] != SCAN_CONFIG_REL_PATH:
        raise ValueError("secret-scan manifest config differs from the existing pinned config")
    tool = shutil.which(argv[0])
    if tool is None:
        raise ValueError("pinned Gitleaks executable is missing")
    version_result = subprocess.run(  # noqa: S603  # nosec B603  # tool from pinned manifest via PATH
        [tool, "version"], cwd=root, capture_output=True, text=True, check=False
    )
    observed = re.search(r"\d+\.\d+\.\d+", version_result.stdout + version_result.stderr)
    if version_result.returncode != 0 or observed is None or observed.group(0) != version:
        raise ValueError(f"Gitleaks version drift: expected {version}, observed output differs")
    return tool, SCAN_CONFIG_REL_PATH


def scan_snapshot_bytes(root: Path, validation: EvidenceValidation) -> SecretScanResult:
    """Run pinned Gitleaks directory scans over each exact classified snapshot file."""
    repository = root.resolve(strict=True)
    if not validation.snapshot_paths:
        return SecretScanResult(
            errors=("no trusted snapshot paths are available for the required file scan",),
            scanned_paths=(),
        )
    try:
        tool, config = _pinned_gitleaks(repository)
    except (OSError, ValueError) as error:
        return SecretScanResult(errors=(str(error),), scanned_paths=())

    errors: list[str] = []
    scanned: list[str] = []
    for path_name in validation.snapshot_paths:
        try:
            _regular_file(repository, path_name, f"secret-scan snapshot {path_name}")
        except ValueError as error:
            errors.append(str(error))
            continue
        result = subprocess.run(  # noqa: S603  # nosec B603  # exact pinned tool/config/file argv
            [tool, "dir", "--config", config, "--redact", "--no-banner", path_name],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        scanned.append(path_name)
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip()
            errors.append(
                f"Gitleaks {path_name} returned {result.returncode}"
                + (f": {output}" if output else "")
            )
    return SecretScanResult(errors=tuple(errors), scanned_paths=tuple(scanned))
