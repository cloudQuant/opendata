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
    "opendata/migration_catalog.py": (
        '_SOURCE_ROOTS = frozenset({"akshare", "openbb"})\n'
        "class LegacyCall:\n    pass\n"
        "def _collect_legacy_calls(tree):\n"
        "    calls = []\n"
        '    calls.append(LegacyCall(source_root="akshare"))\n'
        "    return calls\n"
    ),
    "opendata/migration_inventory.py": (
        '_PROVIDER_IMPORT_ROOTS = frozenset({"akshare", "openbb"})\n'
        "def _analyze_ast(tree, record, source_text):\n"
        "    direct_imports = []\n"
        '    if any(item.removeprefix("dynamic:").split(".", maxsplit=1)[0] == "akshare" '
        "for item in direct_imports):\n"
        '        record.akshare_usage = "direct_akshare_import"\n'
    ),
}

NEW_METADATA_KINDS = (
    "catalog_source_roots_akshare",
    "catalog_source_roots_openbb",
    "catalog_legacy_call_source_root",
    "inventory_provider_roots_akshare",
    "inventory_provider_roots_openbb",
    "inventory_akshare_usage_root",
)


def policy_document(root: Path) -> dict[str, object]:
    """Create a valid exact-exception policy over source files in a temp tree."""
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


def test_policy_binds_and_filters_all_twelve_exact_metadata_sites(tmp_path: Path) -> None:
    raw_policy = policy_document(tmp_path)
    policy = parse_reference_policy(raw_policy)

    assert sum(len(entry.ast_exceptions) for entry in policy.entries) == 12
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

    assert any("bind each of the 12 approved AST exceptions once" in item for item in problems)


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


def test_new_metadata_hash_drift_fails_reconciliation(tmp_path: Path) -> None:
    raw_policy = policy_document(tmp_path)
    path = "opendata/migration_catalog.py"
    target = tmp_path / path
    target.write_text(target.read_text(encoding="utf-8") + "# changed bytes\n", encoding="utf-8")

    problems = reference_policy_problems(
        raw_policy,
        tmp_path,
        actual_hit_paths=actual_akshare_paths(),
    )

    assert f"stale sha256: {path}" in problems


@pytest.mark.parametrize("kind", NEW_METADATA_KINDS)
def test_new_metadata_binding_change_fails_after_hash_is_refreshed(
    tmp_path: Path, kind: str
) -> None:
    raw_policy = policy_document(tmp_path)
    path, changed_source = _mutated_new_metadata_source(kind, "movedscope")
    target = tmp_path / path
    target.write_text(changed_source, encoding="utf-8")
    entries = raw_policy["entries"]
    assert isinstance(entries, list)
    path_entry = next(entry for entry in entries if entry["path"] == path)
    path_entry["sha256"] = hashlib.sha256(changed_source.encode("utf-8")).hexdigest()

    problems = reference_policy_problems(
        raw_policy,
        tmp_path,
        actual_hit_paths=actual_akshare_paths(),
    )

    assert not any(problem.startswith(f"stale sha256: {path}") for problem in problems)
    assert any("must match exactly one AST site" in problem for problem in problems)


def _mutated_new_metadata_source(kind: str, mutation: str) -> tuple[str, str]:
    path = EXPECTED_METADATA_EXCEPTIONS[kind][0]
    source = METADATA_SOURCES[path]
    root_kinds = {
        "catalog_source_roots_akshare",
        "catalog_source_roots_openbb",
        "inventory_provider_roots_akshare",
        "inventory_provider_roots_openbb",
    }
    if kind in root_kinds:
        binding = "_SOURCE_ROOTS" if "catalog_" in kind else "_PROVIDER_IMPORT_ROOTS"
        literal = EXPECTED_METADATA_EXCEPTIONS[kind][4]
        mutations = {
            "wrongcallee": ("frozenset(", "set("),
            "wrongarg": ('{"akshare", "openbb"}', '["akshare", "openbb"]'),
            "movedscope": (f"{binding} = ", f"def build_roots():\n    {binding} = "),
            "shadow": ("", "frozenset = set\n"),
            "duplicate": ('{"akshare", "openbb"}', '{"akshare", "openbb", "akshare"}'),
            "removed": (f'"{literal}"', '"local"'),
        }
        old, new = mutations[mutation]
        mutated = source.replace(old, new, 1) if old else new + source
    elif kind == "catalog_legacy_call_source_root":
        mutations = {
            "wrongcallee": ("LegacyCall(source_root=", "OtherLegacyCall(source_root="),
            "wrongarg": ('LegacyCall(source_root="akshare")', 'LegacyCall(source="akshare")'),
            "movedscope": (
                "def _collect_legacy_calls(tree):",
                "def _collect_legacy_calls_moved(tree):",
            ),
            "shadow": (
                "    calls = []\n",
                "    LegacyCall = OtherLegacyCall\n    calls = []\n",
            ),
            "duplicate": (
                '    calls.append(LegacyCall(source_root="akshare"))\n',
                '    calls.append(LegacyCall(source_root="akshare"))\n'
                '    calls.append(LegacyCall(source_root="akshare"))\n',
            ),
            "removed": ('source_root="akshare"', 'source_root="local"'),
        }
        old, new = mutations[mutation]
        mutated = source.replace(old, new, 1)
    elif kind == "inventory_akshare_usage_root":
        mutations = {
            "wrongcallee": (
                "if any(item.removeprefix(",
                "if all(item.removeprefix(",
            ),
            "wrongarg": ('split(".", maxsplit=1)', 'split("/", maxsplit=1)'),
            "movedscope": (
                "def _analyze_ast(tree, record, source_text):",
                "def _analyze_ast_moved(tree, record, source_text):",
            ),
            "shadow": (
                "    direct_imports = []\n",
                "    any = all\n    direct_imports = []\n",
            ),
            "duplicate": (
                '    if any(item.removeprefix("dynamic:").split(".", maxsplit=1)[0] == "akshare" '
                "for item in direct_imports):\n"
                '        record.akshare_usage = "direct_akshare_import"\n',
                '    if any(item.removeprefix("dynamic:").split(".", maxsplit=1)[0] == "akshare" '
                "for item in direct_imports):\n"
                '        record.akshare_usage = "direct_akshare_import"\n'
                '    if any(item.removeprefix("dynamic:").split(".", maxsplit=1)[0] == "akshare" '
                "for item in direct_imports):\n"
                '        record.akshare_usage = "direct_akshare_import"\n',
            ),
            "removed": ('== "akshare" for item', '== "local" for item'),
        }
        old, new = mutations[mutation]
        mutated = source.replace(old, new, 1)
    else:
        raise AssertionError(f"unsupported metadata exception kind: {kind}")
    if mutated == source:
        raise AssertionError(f"mutation did not apply: {kind} {mutation}")
    return path, mutated


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


@pytest.mark.parametrize("kind", NEW_METADATA_KINDS)
def test_new_metadata_filter_keeps_neighboring_string_and_real_loaders(
    tmp_path: Path, kind: str
) -> None:
    path, _, _, _, root = EXPECTED_METADATA_EXCEPTIONS[kind]
    source = METADATA_SOURCES[path]
    neighbor_source = source + f'EXTRA = "{root}.extra"\n'
    policy = parse_reference_policy(policy_document(tmp_path))
    entry = policy.by_path[path]
    neighbor_findings = filter_metadata_findings(
        path, neighbor_source, scan_source(neighbor_source, path), entry
    )
    external_source = f"import {root}\n" + source
    external_findings = filter_metadata_findings(
        path, external_source, scan_source(external_source, path), entry
    )
    dynamic_source = f'import importlib\nimportlib.import_module("{root}")\n' + source
    dynamic_findings = filter_metadata_findings(
        path, dynamic_source, scan_source(dynamic_source, path), entry
    )

    assert [(finding.module, finding.kind) for finding in neighbor_findings] == [
        (f"{root}.extra", "string")
    ]
    assert [(finding.module, finding.kind) for finding in external_findings] == [(root, "import")]
    assert [(finding.module, finding.kind) for finding in dynamic_findings] == [(root, "dynamic")]


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
