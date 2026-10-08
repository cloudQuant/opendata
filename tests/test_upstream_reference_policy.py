"""Fail-closed tests for the C65 upstream-reference register and AST exemptions."""

from __future__ import annotations

import ast
import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from scripts.codemod.verify_no_akshare import (
    EXPECTED_METADATA_EXCEPTIONS,
    REFERENCE_POLICY_VERSION,
    SCANNER_VERSION,
    ReferencePolicyError,
    _dynamic_callable_names,
    filter_metadata_findings,
    parse_reference_policy,
    reference_policy_problems,
    scan_source,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


METADATA_SOURCES = {
    "opendata/data/openbb_map.py": (
        'def _parse_entry(entry):\n    return entry.get("openbb", {})\n'
    ),
    "opendata/models/data_script.py": (
        'class DataScript:\n    source: str = mapped_column(default="akshare")\n'
    ),
    "opendata/pipeline/patrol.py": (
        'def key_status():\n    keys = {"akshare": True}\n    return keys\n'
    ),
    "opendata/data/providers/akshare/provider.py": (
        'from opendata.data.provider import Provider\nPROVIDER = Provider(source="akshare")\n'
    ),
    "opendata/data/providers/akshare/registration.py": (
        "from opendata.data.providers.catalog import fetchers_for, register_provider\n"
        "def register(registry):\n"
        '    return register_provider("akshare", registry)\n'
        "def __getattr__(name):\n"
        '    if name == "FETCHERS":\n'
        '        return fetchers_for("akshare")\n'
        "    raise AttributeError(name)\n"
    ),
}

NEW_METADATA_KINDS = (
    "akshare_provider_source",
    "akshare_registration_source",
    "akshare_fetchers_source",
)


def policy_document(root: Path) -> dict[str, object]:
    """Create a valid six-exception policy over source files in a temp tree."""
    entries: list[dict[str, object]] = []
    for path, source in METADATA_SOURCES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
        matching_kinds = [
            kind
            for kind, descriptor in EXPECTED_METADATA_EXCEPTIONS.items()
            if descriptor[0] == path
        ]
        exceptions = []
        for kind in matching_kinds:
            _, scope, callee, slot, literal = EXPECTED_METADATA_EXCEPTIONS[kind]
            exceptions.append(
                {
                    "kind": kind,
                    "scope": scope,
                    "callee": callee,
                    "slot": slot,
                    "literal": literal,
                }
            )
        entries.append(
            {
                "path": path,
                "purpose": f"Reviewed metadata name used by {kind}",
                "category": "mapping",
                "reviewed_date": "2026-09-30",
                "reviewer": "owner-reviewer",
                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "ast_exceptions": exceptions,
            }
        )
    return {
        "schema_version": REFERENCE_POLICY_VERSION,
        "scanner_version": SCANNER_VERSION,
        "entries": entries,
    }


def actual_akshare_paths() -> set[str]:
    """Paths with the literal token used by the policy's reciprocal hit check."""
    return {path for path, source in METADATA_SOURCES.items() if "akshare" in source}


def test_reference_policy_requires_nonempty_entries_and_current_versions(tmp_path: Path) -> None:
    policy = policy_document(tmp_path)
    assert parse_reference_policy(policy).scanner_version == SCANNER_VERSION

    empty = {**policy, "entries": []}
    with pytest.raises(ReferencePolicyError, match="non-empty"):
        parse_reference_policy(empty)
    stale = {**policy, "scanner_version": SCANNER_VERSION - 1}
    with pytest.raises(ReferencePolicyError, match="scanner_version"):
        parse_reference_policy(stale)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("purpose", "", "purpose"),
        ("purpose", "待确认", "purpose"),
        ("category", "misc", "category"),
        ("reviewed_date", "2026-9-30", "reviewed_date"),
        ("reviewed_date", "2026-02-30", "reviewed_date"),
        ("reviewer", "未指定", "reviewer"),
        ("sha256", "ABC", "sha256"),
    ],
)
def test_policy_rejects_missing_or_unreviewed_path_metadata(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    policy = policy_document(tmp_path)
    entries = deepcopy(policy["entries"])
    assert isinstance(entries, list)
    entries[0][field] = value
    policy["entries"] = entries

    with pytest.raises(ReferencePolicyError, match=reason):
        parse_reference_policy(policy)


def test_policy_rejects_missing_fields_duplicate_paths_and_wrong_ast_context(
    tmp_path: Path,
) -> None:
    policy = policy_document(tmp_path)
    missing = deepcopy(policy)
    entries = missing["entries"]
    assert isinstance(entries, list)
    del entries[0]["reviewer"]
    with pytest.raises(ReferencePolicyError, match="fields"):
        parse_reference_policy(missing)

    duplicate = deepcopy(policy)
    duplicate_entries = duplicate["entries"]
    assert isinstance(duplicate_entries, list)
    duplicate_entries.append(deepcopy(duplicate_entries[0]))
    with pytest.raises(ReferencePolicyError, match="duplicate"):
        parse_reference_policy(duplicate)

    wrong_context = deepcopy(policy)
    wrong_entries = wrong_context["entries"]
    assert isinstance(wrong_entries, list)
    exceptions = wrong_entries[0]["ast_exceptions"]
    assert isinstance(exceptions, list)
    exceptions[0]["slot"] = "arg:1"
    with pytest.raises(ReferencePolicyError, match="misbound AST exception"):
        parse_reference_policy(wrong_context)


def test_reference_policy_binds_current_hash_and_exact_actual_hit_set(tmp_path: Path) -> None:
    policy = policy_document(tmp_path)
    entries = policy["entries"]
    assert isinstance(entries, list)
    registered = {entry["path"] for entry in entries}
    hits = actual_akshare_paths()
    assert "opendata/data/openbb_map.py" not in hits
    assert reference_policy_problems(policy, tmp_path, actual_hit_paths=hits) == []

    new_path = "opendata/new_adapter.py"
    target = tmp_path / new_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('SOURCE = "akshare.new"\n', encoding="utf-8")
    problems = reference_policy_problems(
        policy,
        tmp_path,
        actual_hit_paths=hits | {new_path},
    )
    assert f"unregistered hit path: {new_path}" in problems

    changed = tmp_path / next(iter(sorted(registered)))
    changed.write_text(changed.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    problems = reference_policy_problems(policy, tmp_path, actual_hit_paths=hits)
    assert any(problem.startswith("stale sha256:") for problem in problems)

    problems = reference_policy_problems(
        policy,
        tmp_path,
        actual_hit_paths=hits - {"opendata/models/data_script.py"},
    )
    assert any(problem.startswith("stale registered path:") for problem in problems)


def test_policy_binds_and_filters_all_six_exact_metadata_sites(tmp_path: Path) -> None:
    raw_policy = policy_document(tmp_path)
    policy = parse_reference_policy(raw_policy)

    assert sum(len(entry.ast_exceptions) for entry in policy.entries) == 6
    assert (
        reference_policy_problems(
            raw_policy,
            tmp_path,
            actual_hit_paths=actual_akshare_paths(),
        )
        == []
    )
    for entry in policy.entries:
        source = METADATA_SOURCES[entry.path]
        findings = scan_source(source, entry.path)
        assert filter_metadata_findings(entry.path, source, findings, entry) == []


def test_openbb_metadata_without_akshare_is_a_valid_exact_exception(tmp_path: Path) -> None:
    policy = policy_document(tmp_path)
    hits = actual_akshare_paths()

    assert "akshare" not in METADATA_SOURCES["opendata/data/openbb_map.py"]
    assert reference_policy_problems(policy, tmp_path, actual_hit_paths=hits) == []


def test_reference_policy_rejects_empty_exception_for_openbb_path(tmp_path: Path) -> None:
    policy = policy_document(tmp_path)
    entries = policy["entries"]
    assert isinstance(entries, list)
    openbb_entry = next(
        entry for entry in entries if entry["path"] == "opendata/data/openbb_map.py"
    )
    openbb_entry["ast_exceptions"] = []

    problems = reference_policy_problems(policy, tmp_path, actual_hit_paths=actual_akshare_paths())

    assert any("bind each of the 6 approved AST exceptions once" in item for item in problems)


@pytest.mark.parametrize(
    ("source", "expected_problem"),
    [
        (
            'def _parse_entry(entry):\n    return entry.get("scenario", {})\n',
            "exactly one AST site",
        ),
        ("def _parse_entry(entry):\n    return {}\n", "exactly one AST site"),
    ],
    ids=["wrong-context", "metadata-deleted"],
)
def test_policy_audit_rejects_changed_openbb_metadata_even_with_refreshed_hash(
    tmp_path: Path, source: str, expected_problem: str
) -> None:
    policy = policy_document(tmp_path)
    path = "opendata/data/openbb_map.py"
    target = tmp_path / path
    target.write_text(source, encoding="utf-8")
    entries = policy["entries"]
    assert isinstance(entries, list)
    openbb_entry = next(entry for entry in entries if entry["path"] == path)
    openbb_entry["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()

    problems = reference_policy_problems(policy, tmp_path, actual_hit_paths=actual_akshare_paths())

    assert any(expected_problem in item for item in problems)
    assert f"stale registered path: {path}" in problems


def test_policy_audit_rejects_arbitrary_registered_no_hit_path(tmp_path: Path) -> None:
    policy = policy_document(tmp_path)
    path = "opendata/unrelated_no_hit.py"
    source = "VALUE = 1\n"
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    entries = policy["entries"]
    assert isinstance(entries, list)
    entries.append(
        {
            "path": path,
            "purpose": "Track a source reference",
            "category": "source-adapter",
            "reviewed_date": "2026-09-30",
            "reviewer": "owner-reviewer",
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "ast_exceptions": [],
        }
    )

    problems = reference_policy_problems(policy, tmp_path, actual_hit_paths=actual_akshare_paths())

    assert f"stale registered path: {path}" in problems


def test_metadata_filter_exempts_one_ast_node_and_keeps_new_same_file_usage(tmp_path: Path) -> None:
    policy = parse_reference_policy(policy_document(tmp_path))
    path = "opendata/data/openbb_map.py"
    source = METADATA_SOURCES[path] + 'EXTRA = "akshare.extra"\n'
    raw = scan_source(source, path)
    filtered = filter_metadata_findings(path, source, raw, policy.by_path[path])

    assert sorted((finding.module, finding.kind) for finding in raw) == [
        ("akshare.extra", "string"),
        ("openbb", "string"),
    ]
    assert [(finding.module, finding.kind) for finding in filtered] == [("akshare.extra", "string")]


def test_metadata_filter_refuses_a_similar_literal_in_another_context(tmp_path: Path) -> None:
    policy = parse_reference_policy(policy_document(tmp_path))
    path = "opendata/data/openbb_map.py"
    changed_source = METADATA_SOURCES[path].replace('entry.get("openbb"', 'entry.get("scenario"')
    findings = scan_source(changed_source, path)

    with pytest.raises(ReferencePolicyError, match="exactly one AST site"):
        filter_metadata_findings(path, changed_source, findings, policy.by_path[path])


def _mutated_new_metadata_source(kind: str, mutation: str) -> tuple[str, str]:
    path = EXPECTED_METADATA_EXCEPTIONS[kind][0]
    source = METADATA_SOURCES[path]
    replacements = {
        ("akshare_provider_source", "wrongcallee"): (
            "PROVIDER = Provider(",
            "PROVIDER = other_provider(",
        ),
        ("akshare_provider_source", "wrongarg"): (
            'Provider(source="akshare")',
            'Provider(name="akshare")',
        ),
        ("akshare_provider_source", "annotated"): (
            'PROVIDER = Provider(source="akshare")',
            'PROVIDER: Provider = Provider(source="akshare")',
        ),
        ("akshare_provider_source", "movedscope"): (
            'PROVIDER = Provider(source="akshare")',
            'def build_provider():\n    return Provider(source="akshare")',
        ),
        ("akshare_provider_source", "shadow"): (
            "from opendata.data.provider import Provider\n",
            "from opendata.data.provider import Provider\nProvider = other_provider\n",
        ),
        ("akshare_provider_source", "duplicate"): (
            "",
            'PROVIDER = Provider(source="akshare")\n',
        ),
        ("akshare_provider_source", "removed"): ('source="akshare"', 'source="local"'),
        ("akshare_registration_source", "wrongcallee"): (
            'register_provider("akshare", registry)',
            'other_register_provider("akshare", registry)',
        ),
        ("akshare_registration_source", "wrongarg"): (
            'register_provider("akshare", registry)',
            'register_provider(registry, "akshare")',
        ),
        ("akshare_registration_source", "movedscope"): (
            "def register(registry):",
            "def other_register(registry):",
        ),
        ("akshare_registration_source", "shadow"): (
            "def register(registry):\n",
            "def register(registry):\n    register_provider = other_register\n",
        ),
        ("akshare_registration_source", "duplicate"): (
            '    return register_provider("akshare", registry)\n',
            '    return register_provider("akshare", registry)\n'
            '    return register_provider("akshare", registry)\n',
        ),
        ("akshare_registration_source", "removed"): ('"akshare", registry', '"local", registry'),
        ("akshare_fetchers_source", "wrongcallee"): (
            'fetchers_for("akshare")',
            'other_fetchers_for("akshare")',
        ),
        ("akshare_fetchers_source", "wrongarg"): (
            'fetchers_for("akshare")',
            'fetchers_for(source="akshare")',
        ),
        ("akshare_fetchers_source", "movedscope"): (
            '    if name == "FETCHERS":\n        return fetchers_for("akshare")\n',
            '    return fetchers_for("akshare")\n'
            '    if name == "FETCHERS":\n        raise AttributeError(name)\n',
        ),
        ("akshare_fetchers_source", "shadow"): (
            "def __getattr__(name):\n",
            "def __getattr__(name):\n    fetchers_for = other_fetchers\n",
        ),
        ("akshare_fetchers_source", "duplicate"): (
            '        return fetchers_for("akshare")\n',
            '        return fetchers_for("akshare")\n        return fetchers_for("akshare")\n',
        ),
        ("akshare_fetchers_source", "removed"): (
            'fetchers_for("akshare")',
            'fetchers_for("local")',
        ),
    }
    old, new = replacements[(kind, mutation)]
    return path, source.replace(old, new, 1) if old else source + new


@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        (kind, mutation)
        for kind in NEW_METADATA_KINDS
        for mutation in (
            "wrongcallee",
            "wrongarg",
            "movedscope",
            "shadow",
            "duplicate",
            "removed",
            *(["annotated"] if kind == "akshare_provider_source" else []),
        )
    ],
)
def test_new_metadata_exception_rejects_wrong_or_repeated_ast_sites(
    tmp_path: Path, kind: str, mutation: str
) -> None:
    policy = parse_reference_policy(policy_document(tmp_path))
    path, source = _mutated_new_metadata_source(kind, mutation)
    entry = policy.by_path[path]
    findings = scan_source(source, path)

    with pytest.raises(ReferencePolicyError, match="exactly one AST site"):
        filter_metadata_findings(path, source, findings, entry)


def test_new_metadata_filter_keeps_neighboring_string_and_external_import(tmp_path: Path) -> None:
    path = EXPECTED_METADATA_EXCEPTIONS["akshare_provider_source"][0]
    source = METADATA_SOURCES[path]
    neighbor_source = source + 'EXTRA = "akshare.extra"\n'
    policy = parse_reference_policy(policy_document(tmp_path))
    entry = policy.by_path[path]
    neighbor_findings = filter_metadata_findings(
        path, neighbor_source, scan_source(neighbor_source, path), entry
    )
    external_source = "import akshare\n" + source
    external_findings = filter_metadata_findings(
        path, external_source, scan_source(external_source, path), entry
    )
    foreign_provider_source = source.replace(
        "from opendata.data.provider import Provider",
        "from akshare import Provider",
    )
    foreign_provider_findings = scan_source(foreign_provider_source, path)

    assert [(finding.module, finding.kind) for finding in neighbor_findings] == [
        ("akshare.extra", "string")
    ]
    assert [(finding.module, finding.kind) for finding in external_findings] == [
        ("akshare", "import")
    ]
    assert any(finding.kind == "import" for finding in foreign_provider_findings)
    with pytest.raises(ReferencePolicyError, match="exactly one AST site"):
        filter_metadata_findings(path, foreign_provider_source, foreign_provider_findings, entry)


@pytest.mark.parametrize(
    "source",
    [
        """import importlib as runtime
root = "akshare"
name = root + ".data"
runtime.import_module(name)
""",
        """from importlib import import_module as load
module = "akshare.option"
load(module)
""",
        """from builtins import __import__ as dynamic
root = "openbb"
dynamic(root)
""",
        """import importlib
loader = importlib.import_module
source = "akshare.daily"
loader(source)
""",
    ],
)
def test_dynamic_import_aliases_and_variable_names_are_detected(source: str) -> None:
    findings = scan_source(source, "synthetic.py")

    assert any(finding.kind == "dynamic" for finding in findings)


def test_dynamic_callable_discovery_walks_once_and_finds_reverse_alias_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = "\n".join(f"unused_{index} = {index}" for index in range(256))
    source = (
        unrelated
        + "\nimport importlib as runtime\n"
        + "loader_0 = loader_1\n"
        + "loader_1 = loader_2\n"
        + "loader_2 = runtime.import_module\n"
        + "loader_0('akshare.data')\n"
    )
    tree = ast.parse(source, filename="synthetic.py")
    original_walk = ast.walk
    expected_node_count = sum(1 for _ in original_walk(tree))
    walk_calls = 0
    visited_nodes = 0

    def counting_walk(node: ast.AST) -> Iterator[ast.AST]:
        nonlocal walk_calls, visited_nodes
        walk_calls += 1
        for child in original_walk(node):
            visited_nodes += 1
            yield child

    with monkeypatch.context() as patch:
        patch.setattr(ast, "walk", counting_walk)
        dynamic_names = _dynamic_callable_names(tree)

    assert walk_calls == 1
    assert visited_nodes == expected_node_count
    assert {"loader_0", "loader_1", "loader_2"}.issubset(dynamic_names)
    assert any(finding.kind == "dynamic" for finding in scan_source(source, "synthetic.py"))


def test_pure_dotted_module_string_is_still_a_finding() -> None:
    findings = scan_source('MODULE = "akshare.data.daily"\n', "synthetic.py")

    assert [(finding.module, finding.kind) for finding in findings] == [
        ("akshare.data.daily", "string")
    ]
