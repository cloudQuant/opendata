"""Validate the bounded C65 provider-source review evidence bundle."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from zoneinfo import ZoneInfo

EVIDENCE_DIR = Path("docs/evidence/C65")
SOURCE_REVIEW_REL = EVIDENCE_DIR / "provider-source-review.json"
SIMILARITY_REL = EVIDENCE_DIR / "provider-similarity-inventory.json"
NEAREST_REVIEW_REL = EVIDENCE_DIR / "provider-nearest-review.json"
PROVIDER_ROOT_REL = Path("opendata/data/providers")
# The reviewed provider package set is not a literal here: it is derived from the live tree by
# ``registered_provider_packages`` below, the same discovery ``_discover_sources`` walks, so the
# instrument cannot rot when a package is registered or removed. Stored artifacts are still bound
# to that measured set (see ``review-package-set-mismatch`` and ``_compare_manifest``).
# EXPECTED_BASELINE_PROVIDERS stays frozen: the OpenBB baseline scope is a separate open question.
EXPECTED_BASELINE_PROVIDERS = frozenset({"ecb", "fred", "imf", "oecd", "yfinance"})
EXPECTED_OPENBB_COMMIT = "3e071fcc2cd9f891cac6040ae60296dba76dab46"
EXPECTED_REVIEW_AUTHORIZATION = "用户直接回复：你帮我直接审阅"
EXPECTED_PACKAGE_REVIEW_RESULT = "NO_COMPLEX_COPY_OBSERVED_IN_MATCHED_SCOPE"
EXPECTED_NEAREST_REVIEW_RESULT = "NO_SHARED_COMPLEX_IMPLEMENTATION_OBSERVED"
MAX_EVIDENCE_AGE = timedelta(hours=24)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOCAL_TIMEZONE = ZoneInfo("Europe/Madrid")


@dataclass(frozen=True)
class Issue:
    """A bounded, safe validation finding."""

    code: str
    message: str


@dataclass
class ValidationResult:
    """Current source facts and independent evidence validation findings."""

    facts: dict[str, Any]
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        """Whether all required evidence bindings and source checks passed."""
        return not self.issues


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _add_issue(issues: list[Issue], code: str, message: str) -> None:
    issues.append(Issue(code, message))


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _as_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


def _load_document(
    root: Path, relative: Path, label: str, issues: list[Issue]
) -> tuple[dict[str, Any] | None, bytes | None]:
    path = root / relative
    try:
        content = path.read_bytes()
    except OSError:
        _add_issue(issues, f"{label}-missing-or-unreadable", f"{label} evidence cannot be read")
        return None, None
    try:
        parsed = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError):
        _add_issue(issues, f"{label}-invalid", f"{label} evidence is not valid UTF-8 JSON")
        return None, content
    document = _as_dict(parsed)
    if document is None:
        _add_issue(issues, f"{label}-shape-invalid", f"{label} evidence must be a JSON object")
    return document, content


def _normalize_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_timestamp(value: object, label: str, now_utc: datetime, issues: list[Issue]) -> str:
    parsed = _parse_timestamp(value)
    if parsed is None:
        _add_issue(issues, f"{label}-timestamp-invalid", f"{label} generated_at is invalid")
    elif parsed > now_utc:
        _add_issue(issues, f"{label}-timestamp-future", f"{label} generated_at is in the future")
    elif now_utc - parsed > MAX_EVIDENCE_AGE:
        _add_issue(issues, f"{label}-stale", f"{label} evidence is older than 24 hours")
    return value if isinstance(value, str) else ""


def _validate_inventory_date(value: object, now_utc: datetime, issues: list[Issue]) -> str:
    if not isinstance(value, str):
        _add_issue(issues, "similarity-date-invalid", "similarity generated_date is invalid")
        return ""
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        _add_issue(issues, "similarity-date-invalid", "similarity generated_date is invalid")
        return value
    if parsed_date.isoformat() != value:
        _add_issue(issues, "similarity-date-invalid", "similarity generated_date is not ISO date")
        return value

    now_local = now_utc.astimezone(LOCAL_TIMEZONE)
    generated_local = datetime.combine(parsed_date, time.min, tzinfo=LOCAL_TIMEZONE)
    if parsed_date > now_local.date():
        _add_issue(issues, "similarity-date-future", "similarity generated_date is in the future")
    elif parsed_date != now_local.date() or now_local - generated_local > MAX_EVIDENCE_AGE:
        _add_issue(
            issues, "similarity-stale", "similarity date is outside the current 24-hour review day"
        )
    return value


def _safe_local_path(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or len(parts) < 5
        or parts[:3] != ["opendata", "data", "providers"]
        or path.suffix != ".py"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return path.as_posix()


def _safe_baseline_path(value: object) -> str | None:
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or len(parts) < 5
        or parts[:2] != ["openbb_platform", "providers"]
        or path.suffix != ".py"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return path.as_posix()


def registered_provider_packages(root: Path, issues: list[Issue]) -> dict[str, Path]:
    """Walk the live provider root once and map each registered package name to its directory.

    A provider package is a direct child of ``opendata/data/providers`` that holds a
    ``registration.py``. Every consumer of the reviewed package set must come through this
    function, so the set a stored artifact is bound to and the set measured on disk are derived
    by one walk and cannot drift apart.
    """
    provider_root = root / PROVIDER_ROOT_REL
    package_dirs: dict[str, Path] = {}
    if not provider_root.is_dir():
        _add_issue(issues, "provider-source-root-missing", "provider source root is missing")
        return package_dirs

    try:
        registrations = sorted(provider_root.rglob("registration.py"))
    except OSError:
        _add_issue(
            issues, "provider-discovery-failed", "provider registration files cannot be listed"
        )
        return package_dirs

    for registration in registrations:
        if registration.is_symlink():
            _add_issue(
                issues, "provider-registration-symlink", "provider registration is a symlink"
            )
            continue
        package_dir = registration.parent
        if package_dir.parent != provider_root:
            _add_issue(
                issues,
                "provider-registration-layout-invalid",
                "registered provider is not a direct child of the provider root",
            )
            continue
        provider = package_dir.name
        if provider in package_dirs:
            _add_issue(
                issues, "provider-registration-duplicate", "provider registration is duplicated"
            )
        package_dirs[provider] = package_dir

    if not set(package_dirs) >= EXPECTED_BASELINE_PROVIDERS:
        _add_issue(
            issues,
            "provider-package-set-mismatch",
            "registered provider packages no longer cover the pinned OpenBB baseline set",
        )

    return package_dirs


def _discover_sources(
    root: Path, issues: list[Issue]
) -> tuple[dict[str, str], dict[str, int], dict[str, int], list[str]]:
    current_hashes: dict[str, str] = {}
    provider_hashes: dict[str, dict[str, str]] = {}
    line_counts: dict[str, int] = {}
    openbb_imports: list[str] = []

    package_dirs = registered_provider_packages(root, issues)
    if not package_dirs:
        return current_hashes, {}, line_counts, openbb_imports

    for provider in package_dirs:
        provider_hashes[provider] = {}

    for provider, package_dir in sorted(package_dirs.items()):
        try:
            source_files = sorted(package_dir.rglob("*.py"))
        except OSError:
            _add_issue(
                issues, "provider-source-list-failed", f"cannot list Python files for {provider}"
            )
            continue
        for source_path in source_files:
            relative = source_path.relative_to(root).as_posix()
            if source_path.is_symlink():
                _add_issue(
                    issues, "provider-source-symlink", f"provider source is a symlink: {relative}"
                )
                continue
            try:
                content = source_path.read_bytes()
            except OSError:
                _add_issue(
                    issues,
                    "provider-source-unreadable",
                    f"provider source cannot be read: {relative}",
                )
                continue
            digest = hashlib.sha256(content).hexdigest()
            current_hashes[relative] = digest
            provider_hashes[provider][relative] = digest
            try:
                text = content.decode("utf-8-sig")
                tree = ast.parse(text, filename=relative)
            except (UnicodeError, SyntaxError, ValueError):
                _add_issue(
                    issues,
                    "provider-source-parse-error",
                    f"provider source cannot be parsed: {relative}",
                )
                continue
            line_counts[relative] = len(text.splitlines())
            for node in ast.walk(tree):
                imported_roots: list[str] = []
                if isinstance(node, ast.Import):
                    imported_roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported_roots.append(node.module.split(".", 1)[0])
                if any(name == "openbb" or name.startswith("openbb_") for name in imported_roots):
                    openbb_imports.append(relative)
                    _add_issue(
                        issues,
                        "openbb-source-import",
                        f"provider source imports an OpenBB root: {relative}",
                    )
                    break

    provider_counts = {name: len(files) for name, files in provider_hashes.items()}
    return current_hashes, provider_counts, line_counts, sorted(set(openbb_imports))


def _validate_reviewer(payload: dict[str, Any], label: str, issues: list[Issue]) -> bool:
    reviewer = _as_dict(payload.get("reviewer"))
    if reviewer is None:
        _add_issue(issues, f"{label}-reviewer-invalid", f"{label} reviewer record is missing")
        return False
    valid = True
    if not _nonempty_text(reviewer.get("name")):
        _add_issue(issues, f"{label}-reviewer-name-missing", f"{label} reviewer name is empty")
        valid = False
    if reviewer.get("kind") != "AI":
        _add_issue(issues, f"{label}-reviewer-kind-invalid", f"{label} reviewer kind is not AI")
        valid = False
    if reviewer.get("authorization") != EXPECTED_REVIEW_AUTHORIZATION:
        _add_issue(
            issues,
            f"{label}-authorization-missing",
            f"{label} does not record the direct user authorization",
        )
        valid = False
    return valid


def _validate_review_packages(
    review: dict[str, Any],
    current_hashes: dict[str, str],
    provider_counts: dict[str, int],
    live_providers: frozenset[str],
    issues: list[Issue],
) -> tuple[dict[str, str], list[str]]:
    saved_hashes: dict[str, str] = {}
    reviewed_names: list[str] = []
    raw_packages = review.get("packages")
    if not isinstance(raw_packages, list):
        _add_issue(issues, "review-packages-invalid", "source review package list is missing")
        raw_packages = []
    package_records: dict[str, dict[str, Any]] = {}
    for value in raw_packages:
        record = _as_dict(value)
        if record is None:
            _add_issue(
                issues, "review-package-invalid", "source review contains an invalid package entry"
            )
            continue
        provider = record.get("provider")
        if not isinstance(provider, str) or not provider:
            _add_issue(
                issues, "review-package-name-invalid", "source review package name is invalid"
            )
            continue
        if provider in package_records:
            _add_issue(
                issues, "review-package-duplicate", f"source review duplicates package {provider}"
            )
            continue
        package_records[provider] = record

    if set(package_records) != live_providers:
        _add_issue(
            issues,
            "review-package-set-mismatch",
            "source review package set differs from the packages registered on disk",
        )

    for provider in sorted(set(package_records) | set(provider_counts)):
        record = package_records.get(provider)
        if record is None:
            _add_issue(
                issues, "review-package-missing", f"source review is missing package {provider}"
            )
            continue
        if record.get("reviewed") is not True:
            _add_issue(
                issues, "provider-review-incomplete", f"package {provider} is not marked reviewed"
            )
        if record.get("review_result") != EXPECTED_PACKAGE_REVIEW_RESULT:
            _add_issue(
                issues,
                "provider-review-result-invalid",
                f"package {provider} lacks the current review result",
            )
        if not _nonempty_text(record.get("comparison")):
            _add_issue(
                issues,
                "provider-review-comparison-missing",
                f"package {provider} has no comparison scope",
            )
        if not _nonempty_text(record.get("basis")):
            _add_issue(
                issues, "provider-review-basis-missing", f"package {provider} has no review basis"
            )
        if (
            record.get("reviewed") is True
            and record.get("review_result") == EXPECTED_PACKAGE_REVIEW_RESULT
        ):
            reviewed_names.append(provider)

        declared_count = record.get("python_files")
        if type(declared_count) is not int or declared_count != provider_counts.get(provider):
            _add_issue(
                issues,
                "review-package-file-count-mismatch",
                f"package {provider} file count differs from current source",
            )

        raw_map = record.get("source_files_sha256")
        package_map = _as_dict(raw_map)
        if package_map is None:
            _add_issue(
                issues,
                "review-package-manifest-invalid",
                f"package {provider} source manifest is missing",
            )
            continue
        normalized_map: dict[str, str] = {}
        for raw_path, digest in package_map.items():
            normalized_path = _safe_local_path(raw_path)
            if (
                normalized_path is None
                or normalized_path.split("/")[3] != provider
                or not _is_sha256(digest)
            ):
                _add_issue(
                    issues,
                    "review-package-manifest-invalid",
                    f"package {provider} source manifest has an invalid entry",
                )
                continue
            normalized_map[normalized_path] = cast("str", digest)
        actual_package_files = {
            path: digest
            for path, digest in current_hashes.items()
            if path.split("/")[3] == provider
        }
        _compare_manifest(f"review-{provider}", normalized_map, actual_package_files, issues)
        saved_hashes.update(normalized_map)

    _compare_manifest("review", saved_hashes, current_hashes, issues)
    return saved_hashes, sorted(reviewed_names)


def _compare_manifest(
    label: str, declared: dict[str, str], current: dict[str, str], issues: list[Issue]
) -> None:
    if set(declared) != set(current):
        _add_issue(
            issues,
            f"{label}-file-set-mismatch",
            f"{label} manifest differs from current Python file set",
        )
    for path in sorted(set(declared) & set(current)):
        if declared[path] != current[path]:
            _add_issue(issues, f"{label}-hash-mismatch", f"{label} SHA-256 differs for {path}")


def _parse_manifest_list(
    value: object, label: str, issues: list[Issue], *, baseline: bool
) -> dict[str, str]:
    if not isinstance(value, list):
        _add_issue(issues, f"{label}-manifest-invalid", f"{label} manifest must be a list")
        return {}
    result: dict[str, str] = {}
    path_validator = _safe_baseline_path if baseline else _safe_local_path
    for entry in value:
        record = _as_dict(entry)
        if record is None:
            _add_issue(issues, f"{label}-manifest-invalid", f"{label} manifest entry is invalid")
            continue
        path = path_validator(record.get("path"))
        digest = record.get("sha256")
        if path is None or not _is_sha256(digest):
            _add_issue(
                issues,
                f"{label}-manifest-invalid",
                f"{label} manifest entry has invalid path or SHA-256",
            )
            continue
        if path in result:
            _add_issue(
                issues, f"{label}-manifest-duplicate", f"{label} manifest contains a duplicate path"
            )
            continue
        result[path] = cast("str", digest)
    return result


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_span(value: object, upper_bound: int | None = None) -> bool:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(position) is not int or position < 1 for position in value)
    ):
        return False
    start, end = cast("list[int]", value)
    return start <= end and (upper_bound is None or end <= upper_bound)


def _validate_similarity(
    similarity: dict[str, Any],
    current_hashes: dict[str, str],
    provider_counts: dict[str, int],
    live_providers: frozenset[str],
    issues: list[Issue],
) -> tuple[dict[str, str], dict[str, str], int, list[dict[str, Any]]]:
    if similarity.get("schema") != "opendata-c65-provider-similarity/v1":
        _add_issue(
            issues, "similarity-schema-invalid", "similarity inventory schema is unsupported"
        )

    comparison = _as_dict(similarity.get("comparison")) or {}
    if comparison.get("baseline_commit") != EXPECTED_OPENBB_COMMIT:
        _add_issue(
            issues,
            "similarity-baseline-mismatch",
            "similarity inventory is not bound to the fixed OpenBB commit",
        )
    baseline_providers = comparison.get("providers_baseline")
    if (
        not isinstance(baseline_providers, list)
        or set(baseline_providers) != EXPECTED_BASELINE_PROVIDERS
        or len(baseline_providers) != len(EXPECTED_BASELINE_PROVIDERS)
    ):
        _add_issue(
            issues,
            "similarity-baseline-provider-set-invalid",
            "similarity inventory baseline provider set is invalid",
        )
    local_providers = comparison.get("providers_local")
    if (
        not isinstance(local_providers, list)
        or set(local_providers) != live_providers
        or len(local_providers) != len(live_providers)
    ):
        _add_issue(
            issues,
            "similarity-local-provider-set-invalid",
            "similarity inventory local provider set is invalid",
        )

    manifests = _as_dict(similarity.get("source_manifests")) or {}
    local_manifest = _parse_manifest_list(
        manifests.get("local_files_sha256"), "similarity-local", issues, baseline=False
    )
    baseline_manifest = _parse_manifest_list(
        manifests.get("baseline_files_sha256"), "similarity-baseline", issues, baseline=True
    )
    _compare_manifest("similarity-local", local_manifest, current_hashes, issues)
    if not _is_sha256(manifests.get("local_sorted_manifest_sha256")):
        _add_issue(
            issues,
            "similarity-local-manifest-digest-invalid",
            "similarity sorted local manifest SHA-256 is invalid",
        )
    if not _is_sha256(manifests.get("baseline_sorted_manifest_sha256")):
        _add_issue(
            issues,
            "similarity-baseline-manifest-digest-invalid",
            "similarity sorted baseline manifest SHA-256 is invalid",
        )

    counts = _as_dict(similarity.get("counts")) or {}
    if type(counts.get("local_provider_python_files")) is not int or counts.get(
        "local_provider_python_files"
    ) != len(current_hashes):
        _add_issue(
            issues,
            "similarity-local-file-count-mismatch",
            "similarity local Python file count differs from current source",
        )
    if type(counts.get("baseline_provider_python_files")) is not int or counts.get(
        "baseline_provider_python_files"
    ) != len(baseline_manifest):
        _add_issue(
            issues,
            "similarity-baseline-file-count-mismatch",
            "similarity baseline Python file count differs from its manifest",
        )
    for field in ("local_parse_errors", "baseline_parse_errors"):
        if not isinstance(counts.get(field), list) or counts.get(field):
            _add_issue(
                issues,
                f"similarity-{field.replace('_', '-')}-present",
                f"similarity {field} must be empty",
            )
    by_provider = _as_dict(counts.get("by_provider_local"))
    if by_provider is None or set(by_provider) != set(provider_counts):
        _add_issue(
            issues,
            "similarity-provider-counts-invalid",
            "similarity local per-provider counts do not match current packages",
        )
    else:
        for provider, actual_count in provider_counts.items():
            provider_entry = _as_dict(by_provider.get(provider)) or {}
            if (
                type(provider_entry.get("python_files")) is not int
                or provider_entry.get("python_files") != actual_count
            ):
                _add_issue(
                    issues,
                    "similarity-provider-count-mismatch",
                    f"similarity Python file count differs for {provider}",
                )

    candidates = similarity.get("candidates")
    if not isinstance(candidates, list):
        _add_issue(issues, "similarity-candidates-invalid", "similarity candidate list is missing")
        candidates = []
    if candidates:
        _add_issue(
            issues, "similarity-candidates-unreviewed", "similarity inventory contains candidates"
        )
    if type(counts.get("review_candidates")) is not int or counts.get("review_candidates") != 0:
        _add_issue(
            issues,
            "similarity-candidate-count-invalid",
            "similarity review candidate count is not zero",
        )

    raw_pairs = similarity.get("nearest_pairs")
    if not isinstance(raw_pairs, list):
        _add_issue(
            issues, "similarity-nearest-pairs-invalid", "similarity nearest-pair list is missing"
        )
        raw_pairs = []
    if type(counts.get("nearest_pairs")) is not int or counts.get("nearest_pairs") != len(
        raw_pairs
    ):
        _add_issue(
            issues,
            "similarity-nearest-pair-count-mismatch",
            "similarity nearest-pair count differs from its records",
        )
    pairs: list[dict[str, Any]] = []
    for value in raw_pairs:
        pair = _as_dict(value)
        if pair is None:
            _add_issue(
                issues,
                "similarity-nearest-pair-invalid",
                "similarity nearest-pair record is invalid",
            )
            continue
        if not _finite_number(pair.get("score")):
            _add_issue(
                issues,
                "similarity-nearest-score-invalid",
                "similarity nearest-pair score is not finite",
            )
        else:
            pairs.append(pair)
    if len(pairs) < 3:
        _add_issue(
            issues,
            "similarity-nearest-pairs-insufficient",
            "similarity inventory has fewer than three ranked pairs",
        )
    ranked_pairs = sorted(pairs, key=lambda item: float(item["score"]), reverse=True)
    return local_manifest, baseline_manifest, len(candidates), ranked_pairs


def _validate_nearest_review(
    nearest: dict[str, Any],
    expected_pairs: list[dict[str, Any]],
    current_hashes: dict[str, str],
    baseline_hashes: dict[str, str],
    line_counts: dict[str, int],
    review_sha256: str | None,
    similarity_sha256: str | None,
    now_utc: datetime,
    issues: list[Issue],
) -> tuple[str, int]:
    if nearest.get("archive_round") != "C65":
        _add_issue(
            issues, "nearest-review-round-invalid", "nearest-pair review is not archived in C65"
        )
    _validate_reviewer(nearest, "nearest-review", issues)
    nearest_generated = _validate_timestamp(
        nearest.get("generated_at"), "nearest-review", now_utc, issues
    )
    if nearest.get("review_complete") is not True:
        _add_issue(
            issues, "nearest-review-incomplete", "primary nearest-pair review is not complete"
        )
    if (
        type(nearest.get("unreviewed_candidates")) is not int
        or nearest.get("unreviewed_candidates") != 0
    ):
        _add_issue(
            issues, "nearest-unreviewed-candidates", "nearest-pair review has unreviewed candidates"
        )

    if review_sha256 is None or nearest.get("provider_review_sha256") != review_sha256:
        _add_issue(
            issues,
            "nearest-source-review-binding-mismatch",
            "nearest review SHA binding differs from source review bytes",
        )
    if similarity_sha256 is None or nearest.get("similarity_inventory_sha256") != similarity_sha256:
        _add_issue(
            issues,
            "nearest-similarity-binding-mismatch",
            "nearest review SHA binding differs from similarity inventory bytes",
        )

    raw_reviews = nearest.get("nearest_pair_reviews")
    if not isinstance(raw_reviews, list) or len(raw_reviews) != 3:
        _add_issue(
            issues,
            "nearest-review-count-invalid",
            "primary nearest review must contain exactly three pairs",
        )
        raw_reviews = raw_reviews if isinstance(raw_reviews, list) else []
    if len(expected_pairs) < 3:
        return nearest_generated, len(raw_reviews)

    seen: set[tuple[str, tuple[int, int], str, tuple[int, int]]] = set()
    for index, value in enumerate(raw_reviews[:3]):
        pair_review = _as_dict(value)
        if pair_review is None:
            _add_issue(
                issues, "nearest-review-pair-invalid", "nearest-pair review entry is invalid"
            )
            continue
        expected = expected_pairs[index]
        if set(pair_review) != set(expected) | {"reviewed", "result", "basis"}:
            _add_issue(
                issues,
                "nearest-review-pair-shape-mismatch",
                "nearest-pair review does not preserve the inventory record fields",
            )
        for field, expected_value in expected.items():
            if pair_review.get(field) != expected_value:
                _add_issue(
                    issues,
                    "nearest-review-pair-binding-mismatch",
                    "nearest-pair review differs from ranked similarity record",
                )
                break
        if pair_review.get("reviewed") is not True:
            _add_issue(
                issues, "nearest-pair-not-reviewed", "a primary nearest pair is not marked reviewed"
            )
        if pair_review.get("result") != EXPECTED_NEAREST_REVIEW_RESULT:
            _add_issue(
                issues,
                "nearest-pair-result-invalid",
                "primary nearest-pair conclusion is not the approved no-sharing result",
            )
        if not _nonempty_text(pair_review.get("basis")):
            _add_issue(
                issues, "nearest-pair-basis-missing", "a primary nearest pair has no review basis"
            )

        local_path = _safe_local_path(pair_review.get("local_path"))
        baseline_path = _safe_baseline_path(pair_review.get("baseline_path"))
        local_span = pair_review.get("local_span")
        baseline_span = pair_review.get("baseline_span")
        if local_path is None or local_path not in current_hashes:
            _add_issue(
                issues,
                "nearest-local-path-invalid",
                "nearest-pair local path is not a current provider source",
            )
        if baseline_path is None or baseline_path not in baseline_hashes:
            _add_issue(
                issues,
                "nearest-baseline-path-invalid",
                "nearest-pair baseline path is not in the pinned inventory",
            )
        local_limit = line_counts.get(local_path) if local_path is not None else None
        if not _valid_span(local_span, local_limit):
            _add_issue(
                issues,
                "nearest-local-span-invalid",
                "nearest-pair local span is outside current source lines",
            )
        if not _valid_span(baseline_span):
            _add_issue(
                issues, "nearest-baseline-span-invalid", "nearest-pair baseline span is invalid"
            )
        if (
            isinstance(local_path, str)
            and _valid_span(local_span, local_limit)
            and isinstance(baseline_path, str)
            and _valid_span(baseline_span)
        ):
            local_start, local_end = cast("list[int]", local_span)
            baseline_start, baseline_end = cast("list[int]", baseline_span)
            identity = (
                local_path,
                (local_start, local_end),
                baseline_path,
                (baseline_start, baseline_end),
            )
            if identity in seen:
                _add_issue(
                    issues,
                    "nearest-pair-duplicate",
                    "primary nearest review repeats a path/span pair",
                )
            seen.add(identity)

    return nearest_generated, len(raw_reviews)


def validate(root: Path, now: datetime | None = None) -> ValidationResult:
    """Read and validate current provider files plus the three archived review artifacts.

    The result covers every provider package registered on disk when the check runs and their
    byte-bound comparison to the pinned OpenBB provider inventory. It makes no assertion about
    historical development or source outside that scope.
    """
    issues: list[Issue] = []
    now_utc = _normalize_now(now)
    review, review_bytes = _load_document(root, SOURCE_REVIEW_REL, "source-review", issues)
    similarity, similarity_bytes = _load_document(root, SIMILARITY_REL, "similarity", issues)
    nearest, nearest_bytes = _load_document(root, NEAREST_REVIEW_REL, "nearest-review", issues)
    review = review or {}
    similarity = similarity or {}
    nearest = nearest or {}

    current_hashes, provider_counts, line_counts, openbb_imports = _discover_sources(root, issues)
    live_providers = frozenset(provider_counts)
    source_review_generated = _validate_timestamp(
        review.get("generated_at"), "source-review", now_utc, issues
    )
    similarity_generated_date = _validate_inventory_date(
        similarity.get("generated_date"), now_utc, issues
    )
    if review.get("archive_round") != "C65":
        _add_issue(issues, "source-review-round-invalid", "source review is not archived in C65")
    scope = review.get("scope")
    if not _nonempty_text(scope):
        _add_issue(issues, "source-review-scope-missing", "source review scope is empty")
    reviewer_authorized = _validate_reviewer(review, "source-review", issues)

    baseline = _as_dict(review.get("openbb_baseline")) or {}
    if baseline.get("head") != EXPECTED_OPENBB_COMMIT or baseline.get("read_only") is not True:
        _add_issue(
            issues,
            "source-review-baseline-invalid",
            "source review does not identify the fixed read-only OpenBB baseline",
        )

    review_hashes, reviewed_providers = _validate_review_packages(
        review, current_hashes, provider_counts, live_providers, issues
    )
    local_manifest, baseline_manifest, candidate_count, ranked_pairs = _validate_similarity(
        similarity, current_hashes, provider_counts, live_providers, issues
    )
    review_sha256 = hashlib.sha256(review_bytes).hexdigest() if review_bytes is not None else None
    similarity_sha256 = (
        hashlib.sha256(similarity_bytes).hexdigest() if similarity_bytes is not None else None
    )
    nearest_generated, nearest_review_count = _validate_nearest_review(
        nearest,
        ranked_pairs[:3],
        current_hashes,
        baseline_manifest,
        line_counts,
        review_sha256,
        similarity_sha256,
        now_utc,
        issues,
    )

    facts: dict[str, Any] = {
        "scope": "current_registered_provider_packages",
        "provider_names": sorted(provider_counts),
        "provider_count": len(provider_counts),
        "provider_file_counts": provider_counts,
        "python_files": len(current_hashes),
        "current_source_files_sha256": current_hashes,
        "reviewed_provider_names": reviewed_providers,
        "reviewed_python_files": len(review_hashes),
        "similarity_manifest_python_files": len(local_manifest),
        "baseline_manifest_python_files": len(baseline_manifest),
        "openbb_import_paths": openbb_imports,
        "openbb_import_count": len(openbb_imports),
        "all_current_provider_python_parsed": not any(
            issue.code == "provider-source-parse-error" for issue in issues
        ),
        "zero_openbb_imports": not openbb_imports,
        "reviewer_authorized": reviewer_authorized,
        "openbb_baseline_commit": baseline.get("head"),
        "candidate_count": candidate_count,
        "unreviewed_candidates": nearest.get("unreviewed_candidates"),
        "nearest_review_count": nearest_review_count,
        "source_review_generated_at": source_review_generated,
        "similarity_generated_date": similarity_generated_date,
        "nearest_review_generated_at": nearest_generated,
        "source_review_sha256": review_sha256,
        "similarity_inventory_sha256": similarity_sha256,
        "nearest_review_sha256": (
            hashlib.sha256(nearest_bytes).hexdigest() if nearest_bytes is not None else None
        ),
        "artifact_sha_bindings_match": (
            nearest.get("provider_review_sha256") == review_sha256
            and nearest.get("similarity_inventory_sha256") == similarity_sha256
        ),
    }
    return ValidationResult(facts=facts, issues=issues)
