"""Rebuild the fixed OpenBB provider model inventory without importing OpenBB."""

from __future__ import annotations

import argparse
import ast
import csv
import difflib
import hashlib
import json
import re
import shutil

# This import is used only for the pinned metadata reader's validated Git argv.
import subprocess  # nosec B404
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

UPSTREAM_COMMIT = "3e071fcc2cd9f891cac6040ae60296dba76dab46"
DEFAULT_UPSTREAM_PATH = Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform")
LEDGER_RELATIVE_PATH = Path("docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv")
DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parents[2] / (
    "docs/proposals/openbb-migration/provider-inventory.yaml"
)
EXPECTED_COUNTS = (32, 350, 202)
EXPECTED_CREDENTIAL_SOURCE_COUNT = 15
REQUIRED_EIA_MODELS = frozenset({"PetroleumStatusReport", "ShortTermEnergyOutlook"})
LEDGER_COLUMNS = (
    "task_id",
    "provider",
    "upstream_model",
    "upstream_commit",
    "upstream_shape_file",
    "upstream_shape_sha256",
    "upstream_shape_line",
)
MANIFEST_LEDGER_COLUMNS = (
    "credential_fields",
    "implementation_task_status",
    "live_verification_status",
    "requester",
    "scenario",
    "scenario_status",
)


@dataclass(frozen=True)
class ExpectedCounts:
    """Expected inventory dimensions for a validation run."""

    source_count: int
    model_count: int
    unique_model_count: int


@dataclass(frozen=True)
class ModelAnchor:
    """Static identity and source anchor for one provider model key."""

    task_id: str
    provider: str
    provider_name: str
    model: str
    upstream_commit: str
    upstream_shape_file: str
    upstream_shape_sha256: str
    upstream_shape_line: int

    def ledger_values(self) -> dict[str, str]:
        """Return the fields compared with the planning ledger."""
        return {
            "task_id": self.task_id,
            "provider": self.provider,
            "upstream_model": self.model,
            "upstream_commit": self.upstream_commit,
            "upstream_shape_file": self.upstream_shape_file,
            "upstream_shape_sha256": self.upstream_shape_sha256,
            "upstream_shape_line": str(self.upstream_shape_line),
        }


@dataclass(frozen=True)
class ParsedProviderSource:
    """AST-derived metadata and structural issues for one source file."""

    declared_provider: str | None
    records: tuple[ModelAnchor, ...]
    credentials: tuple[str, ...]
    issues: tuple[dict[str, str], ...]


class UpstreamUnavailableError(RuntimeError):
    """Raised when the pinned Git object cannot be read locally."""


def _issue(code: str, message: str, **details: object) -> dict[str, Any]:
    """Build one JSON-safe validation issue."""
    result: dict[str, Any] = {"code": code, "message": message}
    result.update(details)
    return result


def _provider_call(node: ast.AST) -> bool:
    """Return whether an AST node calls a class named Provider."""
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    return (isinstance(function, ast.Name) and function.id == "Provider") or (
        isinstance(function, ast.Attribute) and function.attr == "Provider"
    )


def _literal_string(node: ast.AST | None) -> str | None:
    """Return a literal string value without evaluating source code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _credential_names(
    node: ast.AST | None, source_file: str
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Extract literal credential field names without evaluating provider code."""
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return (), []
    if not isinstance(node, (ast.List, ast.Tuple)):
        return (), [
            {
                "code": "CREDENTIALS_NOT_LITERAL",
                "message": "Provider credentials must be a literal list, tuple, or None.",
                "file": source_file,
                "line": str(getattr(node, "lineno", "")),
            }
        ]
    values: list[str] = []
    issues: list[dict[str, str]] = []
    for item in node.elts:
        value = _literal_string(item)
        if value is None:
            issues.append(
                {
                    "code": "CREDENTIAL_NAME_NOT_LITERAL",
                    "message": "Provider credential names must be literal strings.",
                    "file": source_file,
                    "line": str(getattr(item, "lineno", "")),
                }
            )
            continue
        values.append(value)
    duplicates = sorted(name for name, count in Counter(values).items() if count > 1)
    issues.extend(
        {
            "code": "DUPLICATE_CREDENTIAL_NAME",
            "message": "Provider credentials contain a repeated literal name.",
            "file": source_file,
            "credential": name,
        }
        for name in duplicates
    )
    return tuple(values), issues


def extract_provider_models(
    source_text: str,
    source_file: str,
    source_sha256: str,
    upstream_commit: str = UPSTREAM_COMMIT,
) -> ParsedProviderSource:
    """Extract provider names and literal fetcher keys from one Python module."""
    issues: list[dict[str, str]] = []
    try:
        tree = ast.parse(source_text, filename=source_file)
    except SyntaxError as exc:
        return ParsedProviderSource(
            declared_provider=None,
            records=(),
            credentials=(),
            issues=(
                {
                    "code": "SOURCE_PARSE_ERROR",
                    "message": "Pinned provider module is not valid Python syntax.",
                    "file": source_file,
                    "line": str(exc.lineno or ""),
                },
            ),
        )

    provider_calls = [
        node for node in ast.walk(tree) if isinstance(node, ast.Call) and _provider_call(node)
    ]
    if len(provider_calls) != 1:
        issues.append(
            {
                "code": "PROVIDER_CALL_COUNT",
                "message": "Expected exactly one Provider(...) declaration.",
                "file": source_file,
                "actual": str(len(provider_calls)),
            }
        )

    source_parts = PurePosixPath(source_file).parts
    path_provider = source_parts[1] if len(source_parts) > 1 else ""
    records: list[ModelAnchor] = []
    credentials: tuple[str, ...] = ()
    declared_provider: str | None = None
    for provider_call in provider_calls:
        keywords = {keyword.arg: keyword.value for keyword in provider_call.keywords}
        credentials, credential_issues = _credential_names(keywords.get("credentials"), source_file)
        issues.extend(credential_issues)
        declared_provider = _literal_string(keywords.get("name"))
        if declared_provider is None:
            issues.append(
                {
                    "code": "PROVIDER_NAME_NOT_LITERAL",
                    "message": "Provider name must be a literal string for static inventory.",
                    "file": source_file,
                }
            )
        elif declared_provider.casefold() != path_provider.casefold():
            issues.append(
                {
                    "code": "PROVIDER_NAME_PATH_MISMATCH",
                    "message": "Provider name does not match its fixed source directory.",
                    "file": source_file,
                    "expected": path_provider,
                    "actual": declared_provider,
                }
            )

        fetcher_dict = keywords.get("fetcher_dict")
        if not isinstance(fetcher_dict, ast.Dict):
            issues.append(
                {
                    "code": "FETCHER_DICT_NOT_LITERAL",
                    "message": "Provider fetcher_dict must be a literal dictionary.",
                    "file": source_file,
                }
            )
            continue

        for key in fetcher_dict.keys:
            model = _literal_string(key)
            if model is None or key is None:
                issues.append(
                    {
                        "code": "MODEL_KEY_NOT_LITERAL",
                        "message": "Provider model keys must be literal strings.",
                        "file": source_file,
                        "line": str(getattr(key, "lineno", "")),
                    }
                )
                continue
            if declared_provider is None:
                continue
            records.append(
                ModelAnchor(
                    task_id=f"OBB2-{path_provider}-{model}",
                    provider=path_provider,
                    provider_name=declared_provider,
                    model=model,
                    upstream_commit=upstream_commit,
                    upstream_shape_file=source_file,
                    upstream_shape_sha256=source_sha256,
                    upstream_shape_line=key.lineno,
                )
            )

    return ParsedProviderSource(
        declared_provider=declared_provider,
        records=tuple(records),
        credentials=credentials,
        issues=tuple(issues),
    )


def _row_identity(row: Mapping[str, object]) -> tuple[str, str]:
    """Return the provider/model identity represented by a ledger row."""
    return str(row.get("provider", "")), str(row.get("upstream_model", ""))


def _anchor_identity(record: ModelAnchor) -> tuple[str, str]:
    """Return the provider/model identity represented by an AST record."""
    return record.provider, record.model


def validate_inventory(
    records: Sequence[ModelAnchor],
    ledger_rows: Sequence[Mapping[str, object]],
    source_files: Sequence[str],
    expected_counts: ExpectedCounts = ExpectedCounts(*EXPECTED_COUNTS),
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Compare AST identities and anchors with the planning ledger."""
    issues: list[dict[str, Any]] = []
    identities = [_anchor_identity(record) for record in records]
    identity_counts = Counter(identities)
    source_providers = {
        parts[1] for path in source_files if len(parts := PurePosixPath(path).parts) > 1
    }
    unique_model_count = len({record.model for record in records})
    counts = {
        "source_count": len(set(source_files)),
        "provider_count": len(source_providers),
        "model_count": len(records),
        "unique_provider_model_count": len(identity_counts),
        "unique_model_count": unique_model_count,
        "ledger_row_count": len(ledger_rows),
    }

    expected_dimensions = {
        "source_count": expected_counts.source_count,
        "provider_count": expected_counts.source_count,
        "model_count": expected_counts.model_count,
        "unique_provider_model_count": expected_counts.model_count,
        "unique_model_count": expected_counts.unique_model_count,
    }
    for name, expected_value in expected_dimensions.items():
        actual = counts[name]
        if actual != expected_value:
            issues.append(
                _issue(
                    "COUNT_MISMATCH",
                    f"{name} does not match the fixed inventory denominator.",
                    field=name,
                    expected=expected_value,
                    actual=actual,
                )
            )

    duplicates = [
        {"provider": provider, "model": model, "count": count}
        for (provider, model), count in sorted(identity_counts.items())
        if count > 1
    ]
    issues.extend(
        _issue(
            "DUPLICATE_UPSTREAM_IDENTITY",
            "Pinned source contains a repeated provider/model identity.",
            **duplicate,
        )
        for duplicate in duplicates
    )

    ledger_task_counts = Counter(str(row.get("task_id", "")) for row in ledger_rows)
    for task_id, count in sorted(ledger_task_counts.items()):
        if count > 1:
            issues.append(
                _issue(
                    "DUPLICATE_LEDGER_TASK_ID",
                    "Planning ledger contains a repeated task_id.",
                    task_id=task_id,
                    count=count,
                )
            )

    ledger_identity_counts = Counter(_row_identity(row) for row in ledger_rows)
    for (provider, model), count in sorted(ledger_identity_counts.items()):
        if count > 1:
            issues.append(
                _issue(
                    "DUPLICATE_LEDGER_IDENTITY",
                    "Planning ledger contains a repeated provider/model identity.",
                    provider=provider,
                    model=model,
                    count=count,
                )
            )

    actual_by_identity = {_anchor_identity(record): record for record in records}
    rows_by_identity: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    for row in ledger_rows:
        rows_by_identity.setdefault(_row_identity(row), []).append(row)

    for identity, record in sorted(actual_by_identity.items()):
        matches = rows_by_identity.get(identity, [])
        if not matches:
            issues.append(
                _issue(
                    "MISSING_LEDGER_IDENTITY",
                    "Pinned provider/model identity is absent from the planning ledger.",
                    provider=record.provider,
                    model=record.model,
                )
            )
            continue
        row = matches[0]
        expected = record.ledger_values()
        for field in LEDGER_COLUMNS:
            actual_value = str(row.get(field, ""))
            if actual_value != expected[field]:
                issues.append(
                    _issue(
                        "LEDGER_FIELD_MISMATCH",
                        "Planning ledger field differs from pinned AST metadata.",
                        provider=record.provider,
                        model=record.model,
                        field=field,
                        expected=expected[field],
                        actual=actual_value,
                    )
                )

    issues.extend(
        _issue(
            "EXTRA_LEDGER_IDENTITY",
            "Planning ledger contains a provider/model identity absent from pinned source.",
            provider=identity[0],
            model=identity[1],
        )
        for identity in sorted(set(rows_by_identity) - set(actual_by_identity))
    )

    eia_models = {record.model for record in records if record.provider == "eia"}
    issues.extend(
        _issue(
            "REQUIRED_EIA_MODEL_MISSING",
            "Pinned EIA inventory must contain both fixed model keys.",
            provider="eia",
            model=model,
        )
        for model in sorted(REQUIRED_EIA_MODELS - eia_models)
    )

    return issues, counts


def _is_allowed_git_arguments(arguments: Sequence[str]) -> bool:
    """Accept only the fixed read-only Git shapes used by the pinned AST reader."""
    command = tuple(arguments)
    if not all(isinstance(argument, str) for argument in command):
        return False
    if command == ("rev-parse", "--show-prefix"):
        return True
    if command == ("cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}"):
        return True
    if command == ("ls-tree", "-r", "--name-only", UPSTREAM_COMMIT):
        return True
    if len(command) != 2 or command[0] != "show":
        return False

    object_prefix = f"{UPSTREAM_COMMIT}:"
    object_spec = command[1]
    if not object_spec.startswith(object_prefix):
        return False
    source_path = object_spec[len(object_prefix) :]
    path = PurePosixPath(source_path)
    parts = path.parts
    if len(parts) == 5 and parts[0] == "openbb_platform":
        provider_parts = parts[1:]
    elif len(parts) == 4:
        provider_parts = parts
    else:
        return False
    return (
        not path.is_absolute()
        and path.as_posix() == source_path
        and "\\" not in source_path
        and all(part not in {"", ".", ".."} for part in parts)
        and provider_parts[0] == "providers"
        and re.fullmatch(r"[A-Za-z0-9_]+", provider_parts[1]) is not None
        and re.fullmatch(r"openbb_[A-Za-z0-9_]+", provider_parts[2]) is not None
        and provider_parts[3] == "__init__.py"
    )


def _run_git(upstream_path: Path, *arguments: str) -> bytes:
    """Run one allowlisted read-only Git command and return its raw stdout."""
    if not _is_allowed_git_arguments(arguments):
        raise UpstreamUnavailableError("Unsupported Git command for pinned metadata reading.")
    git_executable = shutil.which("git")
    if git_executable is None:
        raise UpstreamUnavailableError("Git executable is unavailable.")
    try:
        # The argv was restricted to four fixed, read-only pinned Git shapes above.
        result = subprocess.run(  # noqa: S603  # nosec B603
            [git_executable, "-C", str(upstream_path), *arguments],
            check=True,
            capture_output=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise UpstreamUnavailableError("Pinned upstream Git object is unavailable.") from exc
    return result.stdout


def load_pinned_provider_metadata(
    upstream_path: Path,
) -> tuple[list[str], list[ModelAnchor], dict[str, list[str]], list[dict[str, str]]]:
    """Read model keys and literal credentials from the fixed Git commit only."""
    prefix = _run_git(upstream_path, "rev-parse", "--show-prefix").decode().strip("/\n")
    _run_git(upstream_path, "cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}")
    tree = _run_git(
        upstream_path,
        "ls-tree",
        "-r",
        "--name-only",
        UPSTREAM_COMMIT,
    ).decode()

    files: list[str] = []
    for git_path in tree.splitlines():
        parts = PurePosixPath(git_path).parts
        if (
            len(parts) == 4
            and parts[0] == "providers"
            and parts[2].startswith("openbb_")
            and parts[3] == "__init__.py"
        ):
            files.append(git_path)

    records: list[ModelAnchor] = []
    credentials_by_provider: dict[str, list[str]] = {}
    issues: list[dict[str, str]] = []
    if not files:
        raise UpstreamUnavailableError("Pinned provider package tree was not found in Git.")

    git_prefix = f"{prefix}/" if prefix else ""
    for source_file in sorted(files):
        raw = _run_git(
            upstream_path,
            "show",
            f"{UPSTREAM_COMMIT}:{git_prefix}{source_file}",
        )
        parsed = extract_provider_models(
            raw.decode("utf-8"),
            source_file,
            hashlib.sha256(raw).hexdigest(),
        )
        records.extend(parsed.records)
        issues.extend(parsed.issues)
        source_provider = PurePosixPath(source_file).parts[1]
        if source_provider in credentials_by_provider:
            issues.append(
                _issue(
                    "DUPLICATE_PROVIDER_SOURCE",
                    "Pinned source tree has more than one provider module for a directory.",
                    provider=source_provider,
                )
            )
        credentials_by_provider[source_provider] = list(parsed.credentials)
    return sorted(files), records, credentials_by_provider, issues


def load_pinned_sources(
    upstream_path: Path,
) -> tuple[list[str], list[ModelAnchor], list[dict[str, str]]]:
    """Read provider models from the fixed Git commit only.

    This compatibility wrapper keeps the original pure-model API while the full metadata
    loader also returns credential names for the manifest generator.
    """
    files, records, _, issues = load_pinned_provider_metadata(upstream_path)
    return files, records, issues


def load_task_ledger(path: Path) -> list[dict[str, str]]:
    """Read the model-level planning ledger as plain CSV data."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("Task ledger has no CSV header.")
        missing = sorted(set(LEDGER_COLUMNS) - set(reader.fieldnames))
        missing.extend(sorted(set(MANIFEST_LEDGER_COLUMNS) - set(reader.fieldnames)))
        missing = sorted(set(missing))
        if missing:
            raise ValueError(f"Task ledger is missing required columns: {', '.join(missing)}")
        return [dict(row) for row in reader]


def validate_credentials(
    credentials_by_provider: Mapping[str, Sequence[str]],
    ledger_rows: Sequence[Mapping[str, object]],
    expected_source_count: int | None = EXPECTED_CREDENTIAL_SOURCE_COUNT,
) -> list[dict[str, Any]]:
    """Compare literal upstream credential fields with the model task ledger."""
    issues: list[dict[str, Any]] = []
    csv_by_provider: dict[str, set[str]] = {}
    for row in ledger_rows:
        provider = str(row.get("provider", ""))
        raw = str(row.get("credential_fields", ""))
        csv_by_provider.setdefault(provider, set()).update(
            part.strip() for part in re.split(r"[,;]", raw) if part.strip()
        )

    expected_providers = set(credentials_by_provider)
    actual_providers = set(csv_by_provider)
    if expected_providers != actual_providers:
        issues.append(
            _issue(
                "CREDENTIAL_PROVIDER_SET_MISMATCH",
                "CSV credential fields and pinned provider sources cover different providers.",
                missing_in_csv=sorted(expected_providers - actual_providers),
                extra_in_csv=sorted(actual_providers - expected_providers),
            )
        )
    credential_source_count = sum(bool(values) for values in csv_by_provider.values())
    if expected_source_count is not None and credential_source_count != expected_source_count:
        issues.append(
            _issue(
                "CREDENTIAL_SOURCE_COUNT_MISMATCH",
                "CSV credential-bearing provider count differs from the fixed denominator.",
                expected=expected_source_count,
                actual=credential_source_count,
            )
        )
    for provider in sorted(expected_providers | actual_providers):
        pinned = set(credentials_by_provider.get(provider, ()))
        declared = csv_by_provider.get(provider, set())
        if pinned != declared:
            issues.append(
                _issue(
                    "CREDENTIAL_FIELDS_MISMATCH",
                    "CSV credential fields differ from literal Provider(credentials=...) metadata.",
                    provider=provider,
                    pinned=sorted(pinned),
                    ledger=sorted(declared),
                )
            )
    return issues


def build_inventory_report(upstream_path: Path, task_ledger_path: Path) -> dict[str, Any]:
    """Build a strict static inventory report from pinned source and CSV ledger."""
    files, records, credentials_by_provider, source_issues = load_pinned_provider_metadata(
        upstream_path
    )
    ledger_rows = load_task_ledger(task_ledger_path)
    issues, counts = validate_inventory(records, ledger_rows, files)
    credential_issues = validate_credentials(credentials_by_provider, ledger_rows)
    issues.extend(credential_issues)
    issues.extend(source_issues)
    status = "PASS" if not issues else "FAIL"
    counts["credential_source_count"] = sum(
        bool(values) for values in credentials_by_provider.values()
    )
    return {
        "status": status,
        "metadata_validation": status,
        "provider_implementation_status": "NOT_EVALUATED",
        "real_source_status": "NOT_EVALUATED",
        "deployment_status": "NOT_EVALUATED",
        "authority": {
            "source": "git show at fixed commit",
            "upstream_path": str(upstream_path),
            "upstream_commit": UPSTREAM_COMMIT,
            "task_ledger": str(task_ledger_path),
        },
        "expected_counts": {
            "source_count": EXPECTED_COUNTS[0],
            "model_count": EXPECTED_COUNTS[1],
            "unique_model_count": EXPECTED_COUNTS[2],
        },
        "counts": counts,
        "source_files": files,
        "provider_credentials": credentials_by_provider,
        "reconstructed_models": [asdict(record) for record in records],
        "issues": issues,
    }


def _status_summary(values: Sequence[str]) -> str:
    """Return a stable single-status summary for a set of model tasks."""
    distinct = sorted(set(values))
    return distinct[0] if len(distinct) == 1 else "MIXED"


def _yaml_flow_list(values: Sequence[str]) -> str:
    """Render fixed identifier lists in a deterministic one-line YAML flow form."""
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values):
        raise ValueError("Provider model and credential names must be plain YAML identifiers.")
    return "[" + ", ".join(values) + "]"


def _yaml_count_map(values: Mapping[str, int]) -> str:
    """Render status counts as a deterministic flow mapping."""
    if not values:
        return "{}"
    return "{" + ", ".join(f"{key}: {values[key]}" for key in sorted(values)) + "}"


def _registry_status(legs: Sequence[tuple[str, bool]]) -> str:
    """Summarize actual registry coverage, separate from model task completion."""
    if not legs:
        return "待实现"
    return "已对照转正" if all(auto for _, auto in legs) else "已实现未对照"


def _render_row(block: list[str], updates: Mapping[str, str]) -> list[str]:
    """Replace owned scalar fields in one provider block and add missing fields."""
    fields = dict(updates)
    rendered: list[str] = []
    emitted: set[str] = set()
    for line in block:
        match = re.match(r"^(\s{4})(\w+):[^\n]*(\n?)$", line)
        if match is None or match.group(2) not in fields:
            rendered.append(line)
            continue
        key = match.group(2)
        if key in emitted:
            continue
        rendered.append(f"{match.group(1)}{key}: {fields[key]}{match.group(3)}")
        emitted.add(key)
    missing = [key for key in fields if key not in emitted]
    if missing:
        insert_at = next(
            (index for index, line in enumerate(rendered) if re.match(r"^    status:", line)),
            len(rendered),
        )
        newline = "\n" if rendered and rendered[-1].endswith("\n") else ""
        inserted = [f"    {key}: {fields[key]}\n" for key in missing]
        if not newline and insert_at == len(rendered):
            inserted[-1] = inserted[-1].rstrip("\n")
        rendered[insert_at:insert_at] = inserted
    return rendered


def _parse_manifest_rows(text: str) -> dict[tuple[str, str], dict[str, str]]:
    """Read row fields needed to preserve explicit requester evidence and local rows."""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    section = ""
    provider: str | None = None
    fields: dict[str, str] = {}

    def flush() -> None:
        if provider is not None:
            rows[(section, provider)] = dict(fields)

    for line in text.splitlines():
        if line in {"providers:", "local_providers:"}:
            flush()
            provider = None
            fields = {}
            section = line[:-1]
            continue
        provider_match = re.match(r"^  - provider: (\S+)\s*$", line)
        if provider_match:
            flush()
            provider = provider_match.group(1)
            fields = {}
            continue
        field_match = re.match(r"^    (\w+):\s*(.*)$", line)
        if provider is not None and field_match:
            fields[field_match.group(1)] = field_match.group(2)
    flush()
    return rows


def render_provider_inventory(
    inventory_text: str,
    models_by_provider: Mapping[str, Sequence[str]],
    credentials_by_provider: Mapping[str, Sequence[str]],
    ledger_rows: Sequence[Mapping[str, object]],
    runtime_legs: Mapping[str, Sequence[tuple[str, bool]]],
) -> str:
    """Render model/credential/task/runtime metadata into the owned manifest bytes.

    Existing scenario, requester, rights, estimate and SDK dependency fields are retained
    verbatim. Model task states come from the CSV; requester confirmation remains unassessed
    unless the source row already has explicit confirmation evidence. Source ``status`` and
    capability counts come from the current registry input.
    """
    model_providers = set(models_by_provider)
    if model_providers != set(credentials_by_provider):
        raise ValueError("Pinned model and credential metadata cover different providers.")

    rows_by_provider: dict[str, list[Mapping[str, object]]] = {}
    for row in ledger_rows:
        rows_by_provider.setdefault(str(row.get("provider", "")), []).append(row)
    if set(rows_by_provider) != model_providers:
        raise ValueError("Task ledger provider set differs from the pinned source provider set.")

    implementation_counts = Counter(
        str(row.get("implementation_task_status", "")) for row in ledger_rows
    )
    live_counts = Counter(str(row.get("live_verification_status", "")) for row in ledger_rows)
    scenario_counts = Counter(str(row.get("scenario_status", "")) for row in ledger_rows)
    implementation_by_provider: dict[str, str] = {}
    live_by_provider: dict[str, str] = {}
    scenario_by_provider: dict[str, str] = {}
    for provider, provider_rows in rows_by_provider.items():
        expected_identities = set(models_by_provider[provider])
        actual_identities = {str(row.get("upstream_model", "")) for row in provider_rows}
        if len(actual_identities) != len(provider_rows) or actual_identities != expected_identities:
            raise ValueError(f"Task ledger model identities differ for provider {provider}.")
        implementation_by_provider[provider] = _status_summary(
            [str(row.get("implementation_task_status", "")) for row in provider_rows]
        )
        live_by_provider[provider] = _status_summary(
            [str(row.get("live_verification_status", "")) for row in provider_rows]
        )
        scenario_by_provider[provider] = _status_summary(
            [str(row.get("scenario_status", "")) for row in provider_rows]
        )

    total_models = sum(len(models) for models in models_by_provider.values())
    unique_models = {model for models in models_by_provider.values() for model in models}
    existing_rows = _parse_manifest_rows(inventory_text)
    requester_counts: Counter[str] = Counter()
    for provider, models in models_by_provider.items():
        existing = existing_rows.get(("providers", provider), {})
        confirmed = bool(
            existing.get("requester")
            and existing.get("requester_confirmed_date")
            and existing.get("requester_evidence")
        )
        requester_counts["CONFIRMED" if confirmed else "NOT_ASSESSED"] += len(models)
    auto_count = sum(1 for legs in runtime_legs.values() for _, auto in legs if auto)
    capability_count = sum(len(legs) for legs in runtime_legs.values())
    top_level_updates = {
        "upstream_model_count": str(total_models),
        "unique_upstream_model_count": str(len(unique_models)),
        "registered_source_count": str(len(runtime_legs)),
        "registered_capability_count": str(capability_count),
        "auto_routable_capability_count": str(auto_count),
        "model_implementation_task_status": _status_summary(
            [str(row.get("implementation_task_status", "")) for row in ledger_rows]
        ),
        "model_live_verification_status": _status_summary(
            [str(row.get("live_verification_status", "")) for row in ledger_rows]
        ),
        "model_implementation_task_status_counts": _yaml_count_map(implementation_counts),
        "model_live_verification_task_status_counts": _yaml_count_map(live_counts),
        "model_scenario_status_counts": _yaml_count_map(scenario_counts),
        "model_requester_confirmation_status_counts": _yaml_count_map(requester_counts),
    }

    row_updates: dict[tuple[str, str], dict[str, str]] = {}
    for provider, models in models_by_provider.items():
        legs = runtime_legs.get(provider, ())
        existing = existing_rows.get(("providers", provider), {})
        requester_confirmed = bool(
            existing.get("requester")
            and existing.get("requester_confirmed_date")
            and existing.get("requester_evidence")
        )
        row_updates[("providers", provider)] = {
            "fetchers": str(len(models)),
            "models": _yaml_flow_list(models),
            "credentials": _yaml_flow_list(credentials_by_provider[provider]),
            "model_count": str(len(models)),
            "implementation_task_status": implementation_by_provider[provider],
            "live_verification_status": live_by_provider[provider],
            "model_scenario_status": scenario_by_provider[provider],
            "requester_confirmation_status": "CONFIRMED" if requester_confirmed else "NOT_ASSESSED",
            "registered_capability_count": str(len(legs)),
            "auto_routable_capability_count": str(sum(1 for _, auto in legs if auto)),
            "status": _registry_status(legs),
        }
    local_providers = [
        provider for section, provider in existing_rows if section == "local_providers"
    ]
    for provider in local_providers:
        legs = runtime_legs.get(provider, ())
        existing = existing_rows[("local_providers", provider)]
        requester_confirmed = bool(
            existing.get("requester")
            and existing.get("requester_confirmed_date")
            and existing.get("requester_evidence")
        )
        row_updates[("local_providers", provider)] = {
            "requester_confirmation_status": "CONFIRMED" if requester_confirmed else "NOT_ASSESSED",
            "registered_capability_count": str(len(legs)),
            "auto_routable_capability_count": str(sum(1 for _, auto in legs if auto)),
            "status": _registry_status(legs),
        }

    rendered: list[str] = []
    current_section = ""
    current_provider: str | None = None
    current_block: list[str] = []
    root_updates_done: set[str] = set()
    root_fields_inserted = False
    provider_head = re.compile(r"^  - provider: (\S+)\s*$")
    section_head = re.compile(r"^(providers|local_providers):\s*$")
    root_field = re.compile(r"^(\w+):\s*(.*?)(\n?)$")

    def flush_row() -> None:
        if current_provider is None:
            return
        updates = row_updates.get((current_section, current_provider))
        rendered.extend(_render_row(current_block, updates) if updates else current_block)

    for line in inventory_text.splitlines(keepends=True):
        section_match = section_head.match(line)
        row_match = provider_head.match(line)
        if current_provider is not None and (section_match or row_match):
            flush_row()
            current_provider = None
            current_block = []
        if section_match:
            if section_match.group(1) == "providers" and not root_fields_inserted:
                rendered.extend(
                    f"{key}: {value}\n"
                    for key, value in top_level_updates.items()
                    if key not in root_updates_done
                )
                root_fields_inserted = True
            current_section = section_match.group(1)
            rendered.append(line)
            continue
        if row_match:
            current_provider = row_match.group(1)
            current_block = [line]
            continue
        if not current_section:
            match = root_field.match(line)
            if match and match.group(1) in top_level_updates:
                key = match.group(1)
                if key in root_updates_done:
                    continue
                rendered.append(f"{key}: {top_level_updates[key]}{match.group(3)}")
                root_updates_done.add(key)
                continue
        if current_provider is not None:
            current_block.append(line)
        else:
            rendered.append(line)
    flush_row()
    if not root_fields_inserted:
        rendered.extend(f"{key}: {value}\n" for key, value in top_level_updates.items())
    prose_pattern = re.compile(
        r"(?m)^(# 规模：)\d+( 个 provider 目录 / )\d+( 个 fetcher 模型)(.*)$"
    )
    output = "".join(rendered)
    output, substitutions = prose_pattern.subn(
        rf"\g<1>{len(models_by_provider)}\g<2>{total_models}\g<3>\g<4>", output, count=1
    )
    if substitutions != 1:
        raise ValueError("Manifest header does not contain the fixed inventory scale line.")
    return output


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    """Write the machine report to a file or standard output."""
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pinned upstream inventory check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream-path",
        type=Path,
        default=DEFAULT_UPSTREAM_PATH,
        help="local OpenBB checkout containing the pinned Git commit",
    )
    parser.add_argument(
        "--task-ledger",
        type=Path,
        default=Path(__file__).resolve().parents[2] / LEDGER_RELATIVE_PATH,
        help="model-level planning CSV to compare with the pinned AST inventory",
    )
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    manifest_mode = parser.add_mutually_exclusive_group()
    manifest_mode.add_argument(
        "--manifest", type=Path, help="write a deterministic manifest generated from fixed inputs"
    )
    manifest_mode.add_argument(
        "--check-manifest",
        type=Path,
        help="compare a manifest byte-for-byte with deterministic fixed-input output",
    )
    args = parser.parse_args(argv)

    try:
        report = build_inventory_report(args.upstream_path, args.task_ledger)
        exit_code = 0 if report["status"] == "PASS" else 1
    except UpstreamUnavailableError as exc:
        report = {
            "status": "NOT_AVAILABLE",
            "metadata_validation": "NOT_AVAILABLE",
            "provider_implementation_status": "NOT_EVALUATED",
            "real_source_status": "NOT_EVALUATED",
            "deployment_status": "NOT_EVALUATED",
            "authority": {
                "source": "git show at fixed commit",
                "upstream_path": str(args.upstream_path),
                "upstream_commit": UPSTREAM_COMMIT,
                "task_ledger": str(args.task_ledger),
            },
            "issues": [_issue("UPSTREAM_NOT_AVAILABLE", str(exc))],
        }
        exit_code = 2
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        report = {
            "status": "FAIL",
            "metadata_validation": "FAIL",
            "provider_implementation_status": "NOT_EVALUATED",
            "real_source_status": "NOT_EVALUATED",
            "deployment_status": "NOT_EVALUATED",
            "authority": {
                "source": "git show at fixed commit",
                "upstream_path": str(args.upstream_path),
                "upstream_commit": UPSTREAM_COMMIT,
                "task_ledger": str(args.task_ledger),
            },
            "issues": [_issue("INVENTORY_READ_ERROR", str(exc))],
        }
        exit_code = 1

    manifest_path = args.manifest or args.check_manifest
    if manifest_path is not None and report.get("status") == "PASS":
        try:
            source_text = DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")
            ledger_rows = load_task_ledger(args.task_ledger)
            models_by_provider: dict[str, list[str]] = {}
            for record in report["reconstructed_models"]:
                models_by_provider.setdefault(record["provider"], []).append(record["model"])
            repo_root = Path(__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from scripts.quality.openbb_inventory_plane import live_legs

            runtime_legs = live_legs()
            generated = render_provider_inventory(
                source_text,
                models_by_provider,
                report["provider_credentials"],
                ledger_rows,
                runtime_legs,
            )
            generated_hash = hashlib.sha256(generated.encode("utf-8")).hexdigest()
            if args.check_manifest is not None:
                actual = manifest_path.read_text(encoding="utf-8")
                matches = actual == generated
                report["manifest_sync"] = {
                    "status": "PASS" if matches else "FAIL",
                    "path": str(manifest_path),
                    "generated_sha256": generated_hash,
                }
                if not matches:
                    sys.stderr.writelines(
                        difflib.unified_diff(
                            actual.splitlines(keepends=True),
                            generated.splitlines(keepends=True),
                            fromfile=str(manifest_path),
                            tofile="deterministic provider inventory",
                        )
                    )
                    exit_code = 1
            else:
                manifest_path.write_text(generated, encoding="utf-8")
                report["manifest_sync"] = {
                    "status": "WRITTEN",
                    "path": str(manifest_path),
                    "generated_sha256": generated_hash,
                }
        except UpstreamUnavailableError as exc:
            report["manifest_sync"] = {"status": "NOT_AVAILABLE", "reason": str(exc)}
            exit_code = 2
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            report["manifest_sync"] = {"status": "FAIL", "reason": str(exc)}
            exit_code = 1

    try:
        _write_report(report, args.output)
    except OSError as exc:
        sys.stderr.write(f"Could not write inventory report: {exc}\n")
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
