#!/usr/bin/env python3
"""Iteration-2's named acceptance cases (AC2-01…AC2-25) as recomputable judgements.

``docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/验收文档.md`` §2 states 25 cases as prose: one
row each for the operation, the pass condition and the negative boundary. Prose cannot be
signed off, so this plane turns each row into four parts -- a measurement, a judgement over
that measurement, the reading that would close the case, and counter-examples that turn that
closing reading back into a gap -- and records the outcome in
``docs/quality/ac2-case-ledger.json``.

Design rules the file enforces on itself:

* **The document is the authority on what is being judged.** Each probe names ``anchors`` -- literal
  substrings of the row it judges. If the criterion is re-worded, the probe reports ``drift``
  instead of silently judging a different requirement.
* **Reachability is declared, never inferred.** A case whose pass condition needs a live upstream
  reading, a warehouse write, a clean-environment install or a named human decision cannot be closed
  by an offline measurement, so the probe declares ``requires`` and the ledger records the missing
  leg inside the denominator. An offline face that holds is ``holds_offline``, never ``proven``.
* **No hand-filled numbers.** A count is recomputed from the tree/runtime or read from the output of
  the repo's own tool; the tool's argv is printed in the ledger's ``command`` field.
* **Judges are tested on both sides.** ``--self-test`` first requires every probe's ``closure``
  reading to judge as holding -- a judge that always returns a gap is as useless as one that never
  does -- then applies every counter-example to that reading and requires a gap. A probe with no
  counter-example is itself a finding.
* **The denominator is the document's.** All 25 rows must parse and every row must have a probe; a
  missing row or a missing probe is reported, never dropped.
"""

from __future__ import annotations

import argparse
import ast
import csv
import fnmatch
import hashlib
import json
import re
import subprocess  # nosec B404  # 3 runs: list argv only, shell never enabled
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast
from xml.etree import ElementTree  # nosec B405  # parses only the junit pytest wrote into our tmp

import tomllib
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from opendata.data.capability import Capability
    from opendata.data.provider import Provider

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
DOC_REL: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/验收文档.md"
TASK_LEDGER_REL: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
ITER1_DOC_REL: Final = "docs/迭代计划/迭代1-重构数据中台/验收文档.md"
ITER1_LEDGER_REL: Final = "docs/quality/acceptance-item-ledger.json"
LEDGER_REL: Final = "docs/quality/ac2-case-ledger.json"
INVENTORY_REL: Final = "docs/proposals/openbb-migration/provider-inventory.yaml"
MAP_REL: Final = "opendata/data/openbb_map.yaml"
RIGHTS_REL: Final = "docs/data-rights-registry.md"
VENDOR_REL: Final = "opendata/data/providers/akshare/_vendor"
THS_REL: Final = "opendata/data/providers/ths"
RATCHET_REL: Final = "docs/quality/ratchet.json"
ZERO_DEP_REL: Final = "docs/quality/zero-dep-baseline.json"
BASELINE_SNAPSHOT_REL: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/基线快照.json"
GATE_RECORD_GLOB: Final = "docs/evidence/*/gate*.txt"
UPSTREAM_COMMIT: Final = "3e071fcc2cd9f891cac6040ae60296dba76dab46"

#: Offline state vocabulary. ``holds_offline`` deliberately is not ``proven``: it says the measured
#: face holds, not that the case is closed.
HOLDS: Final = "holds_offline"
GAP: Final = "gap"
NO_FACE: Final = "no_face"
#: A leg the case needs that an offline measurement cannot produce. Recorded inside the denominator.
BLOCKED: Final = "blocked_leg"
#: The probe's anchors no longer appear in the document row it claims to judge.
DRIFT: Final = "drift"

REACH_LIVE: Final = "live_upstream"
REACH_WAREHOUSE: Final = "warehouse"
REACH_HUMAN: Final = "human_decision"
REACH_INSTALL: Final = "clean_environment"
REACH_ARCHIVE: Final = "archive_field"
#: The declared reach vocabulary: a ``requires`` leg must name one of these, not free text.
REACH_CLASSES: Final = (
    REACH_LIVE,
    REACH_WAREHOUSE,
    REACH_HUMAN,
    REACH_INSTALL,
    REACH_ARCHIVE,
)

SELF_REL: Final = "scripts/quality/ac2_case_probe.py"
CASE_COUNT: Final = 25
Facts = dict[str, str]

_UNESCAPED_PIPE: Final = re.compile(r"(?<!\\)\|")
_ROW_ID: Final = re.compile(r"^AC2-\d{2}$")


# --------------------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Case:
    """One row of §2, kept in the document's own words."""

    id: str
    line: int
    operation: str
    criterion: str
    boundary: str

    @property
    def text(self) -> str:
        """Return the document row's criterion text for this case."""
        return f"{self.operation}\n{self.criterion}\n{self.boundary}"


@dataclass(frozen=True)
class Verdict:
    """Judge outcome: a state, the readings it was derived from and why."""

    state: str
    readings: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class Break:
    """A fact mutation and the verdict it must produce. The counter-example arm."""

    label: str
    facts: Mapping[str, str]
    expect: str = GAP


@dataclass(frozen=True)
class Probe:
    """One case: what the document demands, how it is measured, and how closure would read."""

    case: str
    anchors: tuple[str, ...]
    summary: str
    measure: Callable[[], Facts]
    judge: Callable[[Facts], Verdict]
    closure: Mapping[str, str]
    breaks: tuple[Break, ...]
    requires: tuple[tuple[str, str], ...] = ()
    repair: str = ""


def expected_ids() -> tuple[str, ...]:
    """Return every case id the document's §2 table must contain."""
    return tuple(f"AC2-{index:02d}" for index in range(1, CASE_COUNT + 1))


def parse_cases(text: str) -> tuple[list[Case], list[str]]:
    """Return §2's case rows plus every line that looks like a row but does not parse."""
    cases: list[Case] = []
    malformed: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("| AC2-"):
            continue
        cells = [cell.strip() for cell in _UNESCAPED_PIPE.split(stripped.strip("|"))]
        if len(cells) != 4 or not _ROW_ID.match(cells[0]):
            malformed.append(f"line {number}: {len(cells)} cells, first={cells[0]!r}")
            continue
        cases.append(
            Case(
                id=cells[0],
                line=number,
                operation=cells[1].replace("\\|", "|"),
                criterion=cells[2].replace("\\|", "|"),
                boundary=cells[3].replace("\\|", "|"),
            )
        )
    return cases, malformed


# --------------------------------------------------------------------------------------
# measurement helpers
# --------------------------------------------------------------------------------------
_TOOL_CACHE: dict[tuple[str, ...], tuple[int, str]] = {}


def tool(argv: Sequence[str]) -> tuple[int, str]:
    """Run a repo tool once per process and return ``(exit, stdout+stderr)``. No shell."""
    key = tuple(argv)
    if key not in _TOOL_CACHE:
        proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
            list(argv),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=1800,
        )
        _TOOL_CACHE[key] = (proc.returncode, proc.stdout + proc.stderr)
    return _TOOL_CACHE[key]


def git(args: Sequence[str]) -> str:
    """Run git with a literal argv and return stdout stripped."""
    return subprocess.run(  # noqa: S603  # nosec B603 B607  # git via PATH, literal argv list
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
    ).stdout.strip()


def tool_payload(argv: Sequence[str]) -> tuple[int, dict[str, Any]]:
    """Run an instrument through ``tool`` and return ``(exit, the JSON object it printed)``.

    ``tool`` hands back stdout and stderr concatenated, and an instrument's payload is the last line
    of its stdout, so the object is found by scanning backwards for the first line that parses as a
    mapping. No payload found returns an empty dict: the judge then reads every fact it gates on as
    missing rather than as a zero, which is the difference between a gap and a silent pass.
    """
    exit_code, output = tool(argv)
    for line in reversed(output.splitlines()):
        if not line.startswith("{"):
            continue
        try:
            loaded: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            return exit_code, cast("dict[str, Any]", loaded)
    return exit_code, {}


def sha256_of(path: Path) -> str:
    """Return the sha256 hex digest of a file, or '-' when absent."""
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(rel: str) -> str:
    """Read a repo-relative text file, replacing undecodable bytes."""
    path = REPO_ROOT / rel
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def document_identity() -> dict[str, object]:
    """Return the identity block: HEAD, branch, dirt, doc hash."""
    dirty = [line for line in git(["status", "--porcelain"]).splitlines() if line.strip()]
    return {
        "head": git(["rev-parse", "--short", "HEAD"]),
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty_files": len(dirty),
        "doc_sha256": sha256_of(REPO_ROOT / DOC_REL),
        "python": sys.version.split()[0],
    }


def walk_py(rel_dir: str, *, skip_vendor: bool = False) -> list[Path]:
    """Walk *.py under a repo-relative dir, skipping pycache and vendor."""
    paths = []
    for path in (REPO_ROOT / rel_dir).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if skip_vendor and VENDOR_REL in str(path.relative_to(REPO_ROOT)):
            continue
        paths.append(path)
    return paths


def count_needle(needle: str, rel_dirs: Sequence[str], *, skip_vendor: bool = False) -> int:
    """Count literal occurrences of a needle across *.py files."""
    total = 0
    for rel_dir in rel_dirs:
        for path in walk_py(rel_dir, skip_vendor=skip_vendor):
            total += path.read_text(encoding="utf-8", errors="replace").count(needle)
    return total


def regex_total(pattern: str, rel_dirs: Sequence[str], *, skip_vendor: bool = False) -> int:
    """Count regex matches across *.py files under the given dirs."""
    compiled = re.compile(pattern, re.M)
    total = 0
    for rel_dir in rel_dirs:
        for path in walk_py(rel_dir, skip_vendor=skip_vendor):
            total += len(compiled.findall(path.read_text(encoding="utf-8", errors="replace")))
    return total


def py_count(rel_dir: str) -> int:
    """Count first-party python files under a repo-relative dir."""
    return len(walk_py(rel_dir))


def test_function_count(*rel_files: str) -> int:
    """Count test functions defined in the named test files."""
    total = 0
    for rel in rel_files:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        total += len(
            [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            ]
        )
    return total


def task_rows() -> list[dict[str, str]]:
    """Return the provider x model task rows of the iteration-2 ledger."""
    with (REPO_ROOT / TASK_LEDGER_REL).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def inventory_doc() -> dict[str, Any]:
    """Return the parsed capability-inventory document."""
    return cast("dict[str, Any]", yaml.safe_load(read(INVENTORY_REL)))


def map_doc() -> dict[str, Any]:
    """Return the parsed openbb_map.yaml document."""
    return cast("dict[str, Any]", yaml.safe_load(read(MAP_REL)))


def inventory_report() -> dict[str, Any]:
    """Recompute the provider-model inventory report from source."""
    exit_code, output = tool(["python", "scripts/quality/provider_model_inventory.py"])
    if exit_code != 0:
        raise RuntimeError(f"provider_model_inventory exit={exit_code}: {output[-400:]}")
    return cast("dict[str, Any]", json.loads(output))


def live_capabilities() -> list[Capability]:
    """The runtime registration truth, produced by the repo's own registrar."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from opendata.data import registry
    from opendata.data.providers import catalog

    registrar = registry.get_registry()
    catalog.register_providers(registrar)
    return list(registrar.capabilities())


def per_source_caps() -> dict[str, int]:
    """Return verified capability counts keyed by source."""
    census: dict[str, int] = {}
    for capability in live_capabilities():
        census[str(capability.source)] = census.get(str(capability.source), 0) + 1
    return census


def descriptor_sources() -> set[str]:
    """Return the source ids named by provider descriptors."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from opendata.data.providers import catalog

    return {provider.source for provider in catalog.list_providers()}


def retired_root_nodes() -> int:
    """AST nodes in first-party code that still import the retired top-level roots."""
    retired = {"opendata_http", "app"}
    total = 0
    for path in walk_py("opendata", skip_vendor=True):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                total += sum(1 for alias in node.names if alias.name.split(".")[0] in retired)
            elif isinstance(node, ast.ImportFrom):
                total += 1 if (node.module or "").split(".")[0] in retired else 0
    return total


HEAVY_MODULES: Final = ("sqlalchemy", "fastapi", "loguru", "apscheduler", "opendata.core.config")


def cold_import_probe(statement: str) -> str:
    """Import ``statement`` in a fresh interpreter and report which heavy modules were pulled."""
    script = (
        "import sys, json\n"
        f"{statement}\n"
        f"heavy = {list(HEAVY_MODULES)!r}\n"
        "print(json.dumps(sorted(name for name in heavy if any(\n"
        "    module == name or module.startswith(name + '.') for module in sys.modules))))\n"
    )
    proc = subprocess.run(  # noqa: S603  # nosec B603  # interpreter is this process's own
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )
    if proc.returncode != 0:
        return f"import-failed:{proc.stderr.strip()[-160:]}"
    return proc.stdout.strip() or "empty-output"


# --------------------------------------------------------------------------------------
# AC2-01 权威 = 固定 commit 上游 AST 独立重算 → 清单 → 350 身份逐行 diff
# --------------------------------------------------------------------------------------
def measure_ac2_01() -> Facts:
    """AC2-01 测量面：32目录 / 350唯一provider×model."""
    report = inventory_report()
    counts = report["counts"]
    expected = report["expected_counts"]
    authority = report["authority"]
    models = report["reconstructed_models"]
    eia = [row for row in models if row.get("provider") == "eia"]
    hashed = [row for row in models if any(str(key).startswith("upstream_sha") for key in row)]
    return {
        "status": str(report["status"]),
        "authority_source": str(authority["source"]),
        "upstream_commit": str(authority["upstream_commit"]),
        "providers": str(counts["source_count"]),
        "expected_providers": str(expected["source_count"]),
        "models": str(counts["model_count"]),
        "expected_models": str(expected["model_count"]),
        "unique_models": str(counts["unique_model_count"]),
        "expected_unique": str(expected["unique_model_count"]),
        "ledger_rows": str(counts["ledger_row_count"]),
        "unique_identities": str(counts["unique_provider_model_count"]),
        "duplicates": str(counts["model_count"] - counts["unique_provider_model_count"]),
        "source_files": str(len(report["source_files"])),
        "hashed_rows": str(len(hashed)),
        "credential_sources": str(counts["credential_source_count"]),
        "eia_models": str(len(eia)),
        "eia_credentials": json.dumps(
            report["provider_credentials"].get("eia"), ensure_ascii=False
        ),
        "issues": str(len(report["issues"])),
        "executed_upstream": str(report["real_source_status"]),
    }


def judge_ac2_01(facts: Facts) -> Verdict:
    """AC2-01 判定：上游 AST 在固定 commit 独立重算，与正式清单和 350 行台账逐行一致."""
    checks = {
        "authority is git show at the fixed commit": facts["authority_source"]
        == "git show at fixed commit",
        "upstream commit is the pinned one": facts["upstream_commit"] == UPSTREAM_COMMIT,
        "report status PASS": facts["status"] == "PASS",
        "32 provider dirs": facts["providers"] == facts["expected_providers"] == "32",
        "350 provider x model rows": facts["models"] == facts["expected_models"] == "350",
        "202 unique model names": facts["unique_models"] == facts["expected_unique"] == "202",
        "ledger rows equal model rows": facts["ledger_rows"] == facts["models"],
        "no duplicate identity": facts["duplicates"] == "0",
        "every identity carries an upstream hash": facts["hashed_rows"] == facts["models"],
        "EIA has two models": facts["eia_models"] == "2",
        "EIA credential is api_key": facts["eia_credentials"] == '["api_key"]',
        "15 credential sources": facts["credential_sources"] == "15",
        "no open issues": facts["issues"] == "0",
        "upstream implementation not executed": facts["executed_upstream"] == "NOT_EVALUATED",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "upstream AST recompute agrees with the ledger")


# --------------------------------------------------------------------------------------
# AC2-02 34 描述符 / 活 registry / 派生 map 与库存双向核对
# --------------------------------------------------------------------------------------
def measure_ac2_02() -> Facts:
    """AC2-02 测量面：34描述符 / 单一运行时注册真相."""
    caps = live_capabilities()
    sources = {str(cap.source) for cap in caps}
    descriptors = descriptor_sources()
    entries = map_doc()["entries"]
    mapped = {str(leg["provider"]) for entry in entries for leg in entry["ours"]}
    inventory = inventory_doc()
    named = [str(entry["openbb"]["model"]) for entry in entries]
    placeholders = sorted(name for name in named if name.strip().upper() in {"TBD", ""})
    duplicate_named = (
        len(named) - len(placeholders) - len({name for name in named if name not in placeholders})
    )
    ghosts = sorted(mapped - descriptors)
    missing = sorted(sources - mapped)
    declared = [fetcher for descriptor in catalog_providers() for fetcher in descriptor.fetchers]
    reused_classes = len(declared) - len({type(fetcher) for fetcher in declared})
    return {
        "descriptors": str(len(catalog_providers())),
        "descriptor_sources_unique": str(len(descriptors)),
        "registered_sources": str(len(sources)),
        "capabilities": str(len(caps)),
        "declared_fetchers": str(len(declared)),
        "reused_fetcher_classes": str(reused_classes),
        "map_entries": str(len(entries)),
        "map_ghost_providers": str(len(ghosts)),
        "map_missing_sources": str(len(missing)),
        "ghost_legs_named": ",".join(ghosts) or "-",
        "missing_legs_named": ",".join(missing) or "-",
        "duplicate_openbb_models": str(duplicate_named),
        "placeholder_openbb_models": str(len(placeholders)),
        "inventory_providers": str(inventory["provider_count"]),
        "inventory_local": str(inventory["local_provider_count"]),
        "inventory_capabilities": str(inventory["registered_capability_count"]),
        "second_registry_sites": str(
            regex_total(r"^FETCHERS\s*[:=]", ["opendata"])
            + regex_total(r"^fetcher_dict\s*[:=]", ["opendata"])
        ),
        "verified_caps": str(sum(1 for cap in caps if cap.verified)),
    }


def catalog_providers() -> list[Provider]:
    """Return the live provider descriptors from the catalog."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from opendata.data.providers import catalog

    return list(catalog.PROVIDERS)


def judge_ac2_02(facts: Facts) -> Verdict:
    """AC2-02 判定：34 个描述符、活 registry 与派生 map/库存双向一致，无第二注册表."""
    checks = {
        "34 provider descriptors": facts["descriptors"] == "34",
        "descriptor identities unique": facts["descriptors"] == facts["descriptor_sources_unique"],
        "32 upstream + 2 local = 34": int(facts["inventory_providers"])
        + int(facts["inventory_local"])
        == int(facts["descriptors"]),
        "derived map has no ghost provider": facts["map_ghost_providers"] == "0",
        "every registered source is mapped": facts["map_missing_sources"] == "0",
        "no duplicated openbb model key": facts["duplicate_openbb_models"] == "0",
        "no unnamed placeholder rows in the derived map": facts["placeholder_openbb_models"] == "0",
        "one runtime registration truth": facts["second_registry_sites"] == "0",
        "descriptor-declared fetchers equal live registrations": facts["declared_fetchers"]
        == facts["capabilities"],
        "no fetcher class registered twice": facts["reused_fetcher_classes"] == "0",
        "the inventory copies the live capability count": facts["inventory_capabilities"]
        == facts["capabilities"],
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "descriptors, live registry and derived map agree")


# --------------------------------------------------------------------------------------
# AC2-03 THS 迁移：11 原文件 / 11 能力 / YAML 加载 / 错误与限流表
# --------------------------------------------------------------------------------------
def measure_ac2_03() -> Facts:
    """AC2-03 测量面：11个原文件全部映射 / 现有11条能力."""
    tree = ast.parse(read("scripts/codemod/migrate_provider_layout.py"))
    move_map_entries = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            named = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "FUYAO_MOVE_MAP" in named and isinstance(node.value, ast.Dict):
                move_map_entries = len(node.value.keys)
    yaml_files = [
        f"{THS_REL}/endpoint_map.yaml",
        f"{THS_REL}/transport/error_messages.yaml",
        "opendata/data/mappings/ths.yaml",
    ]
    return {
        "move_map_entries": str(move_map_entries),
        "lazy_bindings": str(read(f"{THS_REL}/provider.py").count("LazyFetcherBinding(")),
        "ths_py_files": str(py_count(THS_REL)),
        "ths_capabilities": str(per_source_caps().get("ths", 0)),
        "yaml_present": str(sum(1 for rel in yaml_files if (REPO_ROOT / rel).is_file())),
        "yaml_load_sites": str(count_needle("yaml.safe_load", [THS_REL])),
        "error_symbols": str(
            count_needle("def error_for_upstream_code", [THS_REL])
            + count_needle("def is_retryable_category", [THS_REL])
        ),
        "retryable_fields": str(count_needle("retryable", [THS_REL])),
        "rate_limit_sites": str(count_needle("rate_limited", [THS_REL])),
        "envelope_fixture_files": str(
            len(list((REPO_ROOT / "tests/fixtures/upstream/fuyao_t1_envelopes").glob("*")))
        ),
        "ths_test_functions": str(
            test_function_count(
                "tests/test_ths_provider.py",
                "tests/test_fuyao_endpoints.py",
                "tests/test_fuyao_transport.py",
                "tests/test_fuyao_endpoint_map.py",
                "tests/test_provider_layout_migration.py",
            )
        ),
    }


def judge_ac2_03(facts: Facts) -> Verdict:
    """AC2-03 判定：THS 11 个原文件的移动映射、11 条能力、三处 YAML 与错误/限流表全部保持."""
    checks = {
        "11 original files in the move map": facts["move_map_entries"] == "11",
        "11 lazy fetcher bindings": facts["lazy_bindings"] == "11",
        "11 registered capabilities": facts["ths_capabilities"] == "11",
        "3 YAML resources present": facts["yaml_present"] == "3",
        "YAML is actually loaded": int(facts["yaml_load_sites"]) >= 2,
        "error table is readable": facts["error_symbols"] == "2",
        "rate limiting is wired": int(facts["rate_limit_sites"]) >= 1,
        "recorded envelopes available": int(facts["envelope_fixture_files"]) >= 2,
        "offline coverage exists": int(facts["ths_test_functions"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "move map, bindings, capabilities and YAML loading align"
    )


# --------------------------------------------------------------------------------------
# AC2-04 327 清单 / 2 本体 / 166 文件 532 旧导入 / 人工差异可追溯 / 幂等
# --------------------------------------------------------------------------------------
def measure_ac2_04() -> Facts:
    """AC2-04 测量面：327清单及2本体 / 166文件/532旧导入 / 逐路径函数签名保真."""
    manifest = json.loads(read(f"{VENDOR_REL}/manifest.json") or "{}")
    lock = json.loads(read(f"{VENDOR_REL}/upstream.lock") or "{}")
    vendor = REPO_ROOT / VENDOR_REL
    manifest_files = manifest.get("files", [])
    manifest_resources = manifest.get("resources", [])
    manifest_paths = {row["path"] for row in manifest_files} | {
        row["path"] for row in manifest_resources
    }
    lock_rows = lock.get("files", [])
    lock_paths = {row["path"] for row in lock_rows}
    disk_mismatch = [
        row for row in manifest_files if sha256_of(vendor / row["path"]) != row["sha256"]
    ]
    line_mismatch = []
    for row in disk_mismatch:
        path = vendor / row["path"]
        if path.is_file():
            lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if lines != row["lines"]:
                line_mismatch.append(row)
    unflagged = [row for row in line_mismatch if not row.get("manual_edits")]
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import port_scope

    try:
        inventory_rel = port_scope.scope_inventory_rel(REPO_ROOT)
    except port_scope.PortScopePathError:
        inventory_rel = "-"
    inventory = json.loads(read(inventory_rel) or "{}")
    scope = inventory.get("reconciliation", {})
    archive_rows = {str(row["path"]): row for row in inventory.get("files", [])}
    control_rows = [row for row in lock_rows if row["path"] in {"manifest.json", "upstream.lock"}]
    baseline = json.loads(read(BASELINE_SNAPSHOT_REL) or "{}").get("legacy_imports", {})
    census_rows = list(baseline.get("per_file") or [])
    census_paths = {str(row["path"]).split("/", 1)[-1] for row in census_rows}
    stale_paths = {row["path"] for row in disk_mismatch}
    # A rewrite's durable trace is the archive's own pin pair: pristine upstream bytes and ported
    # bytes are digested per path, so the claim survives re-freezing manifest.json. The drift set
    # above only exists while the manifest is stale, which is a bookkeeping defect, not a record.
    joined_paths = census_paths & set(archive_rows)
    unjoined_paths = sorted(census_paths - set(archive_rows))
    census_pin_gaps = sorted(
        path
        for path in joined_paths
        if archive_rows[path]["sha256"]["upstream_lock"]
        == archive_rows[path]["sha256"]["manifest_ported_snapshot"]
    )
    census_replay_gaps = sorted(
        path
        for path in joined_paths
        if str(archive_rows[path]["port_report"]["recorded_replay_status"]) != "PASS"
    )
    # The census is a record of legacy *import* statements, so only ``import_rewrites`` may be
    # compared against it; string rewrites belong to a different denominator and are counted
    # separately rather than folded into one total.
    import_rewrite_rows: set[str] = set()
    string_rewrite_rows: set[str] = set()
    for path, row in archive_rows.items():
        report = row["port_report"]
        if int(report["import_rewrites"]) > 0:
            import_rewrite_rows.add(path)
        if int(report["string_rewrites"]) > 0:
            string_rewrite_rows.add(path)
    # 「函数名/签名/语义保真」 is not derivable from byte equality: the archive proves the codemod
    # replay landed, not which def moved. This is the per-path AST join -- pristine def/class
    # signatures against the ported file on disk for every locked .py record -- with its own
    # checkout gate, so an unavailable pristine tree cannot read back as "no signature differences".
    signature_exit, signature = tool_payload(
        ["python", "scripts/quality/ported_signature_fidelity.py"]
    )
    signature_unregistered = [
        str(path) for path in signature.get("differing_and_not_flagged_manual", [])
    ]
    return {
        "lock_records": str(len(lock_rows)),
        "lock_python": str(sum(1 for row in lock_rows if str(row["path"]).endswith(".py"))),
        "lock_resources": str(
            len(lock_rows) - sum(1 for row in lock_rows if str(row["path"]).endswith(".py"))
        ),
        "manifest_files": str(len(manifest_files)),
        "manifest_resources": str(len(manifest_resources)),
        "control_file_rows_in_lock": str(len(control_rows)),
        "path_sets_equal": str(manifest_paths == lock_paths),
        "disk_python_files": str(py_count(VENDOR_REL)),
        "manifest_vs_disk_mismatch": str(len(disk_mismatch)),
        "same_line_rewrites": str(len(disk_mismatch) - len(line_mismatch)),
        "line_count_changed_rows": str(len(line_mismatch)),
        "line_changed_unflagged": str(len(unflagged)),
        "line_changed_paths": ",".join(row["path"] for row in unflagged) or "-",
        "manual_edits_true_rows": str(sum(1 for row in manifest_files if row.get("manual_edits"))),
        "census_files": str(baseline.get("files")),
        "census_rows": str(len(census_rows)),
        "census_row_sum": str(sum(int(row["statement_count"]) for row in census_rows)),
        "census_line_sum": str(sum(len(row["statement_lines"]) for row in census_rows)),
        "census_statements": str(baseline.get("statements")),
        "census_sha_rows": str(sum(1 for row in census_rows if row.get("file_sha256"))),
        "manifest_rows_with_rewrite_provenance": str(
            sum(
                1
                for row in manifest_files
                if any(re.search(r"rewrite|layout|migrat", str(key), re.I) for key in row)
            )
        ),
        "stale_pin_outside_census": str(len(stale_paths - census_paths)),
        "census_paths_without_manifest_drift": str(len(census_paths - stale_paths)),
        "archive_rows_missing_for_census": str(len(unjoined_paths)),
        "archive_census_pin_gap_rows": str(len(census_pin_gaps)),
        "archive_census_pin_gap_paths": ",".join(census_pin_gaps[:3]) or "-",
        "archive_census_replay_gap_rows": str(len(census_replay_gaps)),
        "archive_rewrite_rows_outside_census": str(len(import_rewrite_rows - census_paths)),
        "archive_string_rewrite_rows": str(len(string_rewrite_rows)),
        "archive_rows_carrying_rewrite_counts": str(len(import_rewrite_rows | string_rewrite_rows)),
        "archive_port_report_rows": str(scope.get("port_report_rows")),
        "archive_import_rewrite_total": str(scope.get("port_report_import_rewrite_total")),
        "archive_string_rewrite_total": str(scope.get("port_report_string_rewrite_total")),
        "archive_manual_edits_true_rows": str(scope.get("manual_edits_true_rows")),
        "archive_snapshot_matches": str(
            scope.get("manifest_snapshot_sha_matches_current_port_count")
        ),
        "retired_root_import_nodes": str(retired_root_nodes()),
        "signature_fidelity_exit": str(signature_exit),
        "signature_rows_compared": str(signature.get("rows_compared")),
        "signature_rows_equal": str(signature.get("rows_equal")),
        "signature_differing": str(signature.get("differing_total")),
        "signature_differing_unregistered": str(
            signature.get("differing_and_not_flagged_manual_total")
        ),
        "signature_differing_unregistered_paths": ",".join(signature_unregistered) or "-",
        "signature_checkout_ok": str(
            bool(signature.get("checkout_present"))
            and bool(signature.get("checkout_commit_matches_lock"))
        ),
        "signature_checkout_commit": str(signature.get("checkout_commit") or "-"),
        "signature_lock_commit": str(signature.get("lock_commit") or "-"),
    }


def judge_ac2_04(facts: Facts) -> Verdict:
    """AC2-04 判定：327 文件清单、锁与快照双向一致、逐节点改写与人工差异标记、重放幂等."""
    checks = {
        "325 python + 2 resources = 327 lock rows": int(facts["lock_python"])
        + int(facts["lock_resources"])
        == int(facts["lock_records"])
        == 327,
        "the manifest covers 325 python files and 2 resources": facts["manifest_files"] == "325"
        and facts["manifest_resources"] == "2",
        "control files are accounted separately, not inside 327": facts["control_file_rows_in_lock"]
        == "0",
        "lock and manifest name the same paths": facts["path_sets_equal"] == "True",
        "disk python file count matches the manifest": facts["disk_python_files"]
        == facts["manifest_files"],
        "the 166/532 legacy-import census is recomputable from its own rows": facts["census_files"]
        == facts["census_rows"]
        == facts["census_sha_rows"]
        == "166"
        and facts["census_statements"]
        == facts["census_row_sum"]
        == facts["census_line_sum"]
        == "532",
        "every stale pin is one of the recorded rewrites": facts["stale_pin_outside_census"] == "0",
        "every recorded rewrite has an archive row": facts["archive_rows_missing_for_census"]
        == "0",
        "every recorded rewrite left a pristine→ported pin change": facts[
            "archive_census_pin_gap_rows"
        ]
        == "0",
        "every recorded rewrite's replay is archived as passing": facts[
            "archive_census_replay_gap_rows"
        ]
        == "0",
        "no node-rewrite count falls outside the recorded census": facts[
            "archive_rewrite_rows_outside_census"
        ]
        == "0",
        "the rewrites are traceable in the scope archive": facts[
            "archive_rows_carrying_rewrite_counts"
        ]
        != "0",
        "every vendored file whose lines changed is flagged manual": facts["line_changed_unflagged"]
        == "0",
        "实际函数名/签名/语义保真 is AST-compared on every locked .py row, not asserted": facts[
            "signature_rows_compared"
        ]
        == "325",
        "the signature comparison joined the pristine checkout at the locked commit": facts[
            "signature_checkout_ok"
        ]
        == "True",
        "人工差异可追溯: every 函数名/签名 change sits on a lock row flagged manual_edits": facts[
            "signature_differing_unregistered"
        ]
        == "0",
        # rc is the instrument's own completeness claim (admissible checkout, every locked .py
        # parsed on both sides); judging it keeps "the comparison ran" out of the prose-only face.
        "签名对照面自身完整（工具 rc=0）": facts["signature_fidelity_exit"] == "0",
        "retired roots import nothing in first-party code": facts["retired_root_import_nodes"]
        == "0",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "vendor identity, rewrite census and deviation flags align"
    )


# --------------------------------------------------------------------------------------
# AC2-05 MIT 头与冷导入边界
# --------------------------------------------------------------------------------------
def measure_ac2_05() -> Facts:
    """AC2-05 测量面：MIT头与全文保留 / 不初始化settings/DB/scheduler."""
    exit_code, output = tool(["python", "scripts/quality/vendor_independence.py"])
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {}
    license_path = REPO_ROOT / VENDOR_REL / "LICENSE-AKSHARE"
    vendor_pys = walk_py(VENDOR_REL)
    headed = sum(
        1
        for path in vendor_pys
        if "MIT License" in path.read_text(encoding="utf-8", errors="replace")[:600]
    )
    metadata_arm = cold_import_probe("import opendata.data.providers.catalog")
    app_arm = cold_import_probe("import opendata.main")
    return {
        "vendor_independence_exit": str(exit_code),
        "vendor_independence_valid": str(payload.get("valid")),
        "license_file_present": str(license_path.is_file()),
        "license_lines": str(len(license_path.read_text(encoding="utf-8").splitlines()))
        if license_path.is_file()
        else "0",
        "vendor_py_files": str(len(vendor_pys)),
        "mit_header_files": str(headed),
        "heavy_after_metadata_import": metadata_arm,
        "heavy_after_app_import": app_arm,
    }


def judge_ac2_05(facts: Facts) -> Verdict:
    """AC2-05 判定：MIT 头与全文保留，冷导入不拉起重依赖."""
    checks = {
        "vendor independence audit green": facts["vendor_independence_exit"] == "0"
        and facts["vendor_independence_valid"] == "True",
        "MIT full text ships next to the tree": facts["license_file_present"] == "True",
        "every ported file keeps the MIT header": facts["mit_header_files"]
        == facts["vendor_py_files"],
        "metadata import pulls no heavy module": facts["heavy_after_metadata_import"] == "[]",
        "the detector is live (the app import does pull heavy modules)": (
            facts["heavy_after_app_import"].startswith("[")
            and facts["heavy_after_app_import"] != "[]"
        ),
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS,
        tuple(readings),
        "MIT text preserved; cold import stays light with a live control arm",
    )


# --------------------------------------------------------------------------------------
# AC2-06 发行资源
# --------------------------------------------------------------------------------------
RESOURCES: Final = (
    f"{VENDOR_REL}/file_fold/calendar.json",
    f"{VENDOR_REL}/stock_feature/ths.js",
    f"{THS_REL}/endpoint_map.yaml",
    f"{THS_REL}/transport/error_messages.yaml",
    f"{VENDOR_REL}/manifest.json",
    f"{VENDOR_REL}/upstream.lock",
    f"{VENDOR_REL}/LICENSE-AKSHARE",
)


def measure_ac2_06() -> Facts:
    """AC2-06 测量面：包数据声明、manifest、lock 与磁盘."""
    package_data = tomllib.loads(read("pyproject.toml"))["tool"]["setuptools"]["package-data"]
    covered = 0
    uncovered: list[str] = []
    for rel in RESOURCES:
        parent, name = rel.rsplit("/", 1)
        patterns = package_data.get(parent.replace("/", "."), [])
        if any(fnmatch.fnmatch(name, str(pattern)) for pattern in patterns):
            covered += 1
        else:
            uncovered.append(rel)
    dist = REPO_ROOT / "dist"
    return {
        "resource_files_on_disk": str(sum(1 for rel in RESOURCES if (REPO_ROOT / rel).is_file())),
        "resource_files_total": str(len(RESOURCES)),
        "covered_by_package_data": str(covered),
        "uncovered_paths": ",".join(uncovered) or "-",
        "built_artifacts": str(len(list(dist.glob("*"))) if dist.is_dir() else 0),
        "resource_asserting_tests": str(
            test_function_count("tests/test_port_module.py", "tests/test_port_scope.py")
        ),
        "bsl_text_in_mit_tree": str(
            "Business Source License" in read(f"{VENDOR_REL}/LICENSE-AKSHARE")
        ),
    }


def judge_ac2_06(facts: Facts) -> Verdict:
    """AC2-06 判定：七项发行资源在盘上并被 package-data 声明；wheel/sdist 待干净环境验证."""
    checks = {
        "all seven resources on disk": facts["resource_files_on_disk"]
        == facts["resource_files_total"]
        == "7",
        "package-data declares all seven": facts["covered_by_package_data"]
        == facts["resource_files_total"],
        "no BSL text inside the MIT tree": facts["bsl_text_in_mit_tree"] == "False",
        "clean-install tests exist": int(facts["resource_asserting_tests"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "declaration and disk agree; the install leg is still owed"
    )


# --------------------------------------------------------------------------------------
# AC2-07 搬迁前后 selfdev/ported/zero-dep/安全/coverage 身份
# --------------------------------------------------------------------------------------
def measure_ac2_07() -> Facts:
    """AC2-07 测量面：334.py逐文件归类不重复 / _vendor显式omit且ported仍受检."""
    exit_code, output = tool(["python", "scripts/codemod/verify_no_akshare.py"])
    walked = re.search(r"(\d+) file\(s\) walked \(([^)]*)\)", output)
    baseline = json.loads(read(ZERO_DEP_REL) or "{}")
    pyproject = tomllib.loads(read("pyproject.toml"))
    omit = pyproject["tool"]["coverage"]["run"]["omit"]
    preflight_exit, preflight_out = tool(["python", "scripts/quality/toolchain_preflight.py"])
    try:
        preflight = json.loads(preflight_out)
    except json.JSONDecodeError:
        preflight = {}
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import ported_security_evidence

    try:
        security = ported_security_evidence.validate(REPO_ROOT)
        issue_list = list(getattr(security, "issues", []))
        issues = len(issue_list)
        detail = "-"
    except Exception as error:
        issue_list = []
        issues = -1
        detail = f"{type(error).__name__}:{error}"
    ratchet = json.loads(read(RATCHET_REL) or "{}")
    files = baseline.get("files", {})
    try:
        scan_document = json.loads(read(str(ported_security_evidence.SCAN_REL)) or "{}")
        triage_document = json.loads(read(str(ported_security_evidence.TRIAGE_REL)) or "{}")
    except (OSError, ValueError):
        scan_document, triage_document = {}, {}
    carried = scan_document.get("carried_from") or triage_document.get("carried_from") or {}
    scanned_rows = sum(
        int(count)
        for count in (scan_document.get("rule_counts") or {}).values()
        if isinstance(count, int)
    )
    return {
        "zero_dep_exit": str(exit_code),
        "walker_count": walked.group(1) if walked else "unparsed",
        "walker_detail": walked.group(2) if walked else "unparsed",
        "baseline_scope_sum": str(sum(files.values())),
        "baseline_scope": ",".join(sorted(files)),
        "declared_scope": ",".join(str(entry) for entry in sorted(baseline.get("scope") or [])),
        "empty_scope_buckets": str(
            sum(1 for entry in (baseline.get("scope") or []) if not int(files.get(entry, 0)))
        ),
        "census_identity_holds": str(
            walked is not None and int(walked.group(1)) == sum(files.values())
        ),
        "coverage_source": str(pyproject["tool"]["coverage"]["run"]["source"]),
        "coverage_omits_vendor": str(any("_vendor" in str(entry) for entry in omit)),
        "vendor_file_count": str(ratchet.get("file_counts", {}).get(VENDOR_REL)),
        "direct_http_ported_pin": str(ratchet.get("metrics", {}).get("direct_http_ported")),
        "preflight_exit": str(preflight_exit),
        "preflight_status": str(preflight.get("status")),
        "preflight_issues": str(len(preflight.get("issues", []))),
        "ported_security_issue_count": str(issues),
        "ported_security_recorded_root": str(ported_security_evidence.PORT_ROOT),
        "ported_security_issue_codes": ",".join(sorted({issue.code for issue in issue_list}))
        or "-",
        "ported_security_declares_identity": str(
            str(scan_document.get("ported_root")) == ported_security_evidence.PORT_ROOT
            and str(scan_document.get("archive_round")) == ported_security_evidence.EXPECTED_ROUND
            and bool(carried.get("triage"))
            and scanned_rows > 0
            and str(carried.get("identities_matched")) == str(scanned_rows)
        ),
        "ported_security_scanned_root": str(scan_document.get("ported_root", "-")),
        "ported_security_archive_round": str(scan_document.get("archive_round", "-")),
        "ported_security_carried_round": str(carried.get("prior_round", "-")),
        "ported_security_scanned_rows": str(scanned_rows),
        "ported_security_identity_rows": str(carried.get("identities_matched", "-")),
        "ported_security_detail": detail,
    }


def judge_ac2_07(facts: Facts) -> Verdict:
    """AC2-07 判定：搬迁前后零依赖/coverage/安全/质量债务身份一致；版本漂移显式记为 ENV_BLOCKED."""
    checks = {
        "zero-dependency check green": facts["zero_dep_exit"] == "0",
        "walker census equals the frozen baseline": facts["census_identity_holds"] == "True",
        "coverage denominator stays first-party": facts["coverage_source"] == "['opendata']",
        "_vendor is omitted from coverage while ported code is still scanned": facts[
            "coverage_omits_vendor"
        ]
        == "True",
        "the vendored census is 325": facts["vendor_file_count"] == "325",
        "direct-http debt is pinned, not zeroed": int(facts["direct_http_ported_pin"] or 0) > 0,
        "the frozen scope and its buckets name the same roots": facts["declared_scope"]
        == facts["baseline_scope"],
        "no declared scope bucket walked zero files": facts["empty_scope_buckets"] == "0",
        "the archive names the tree it scanned and the round it carried from": facts[
            "ported_security_declares_identity"
        ]
        == "True",
        "the carried review was matched row-for-row against this scan": facts[
            "ported_security_identity_rows"
        ]
        == facts["ported_security_scanned_rows"]
        and int(facts["ported_security_scanned_rows"] or 0) > 0,
        "toolchain preflight is not ENV_BLOCKED": facts["preflight_status"] != "ENV_BLOCKED",
        "ported security evidence validates against the shipped root": facts[
            "ported_security_issue_count"
        ]
        == "0",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "pre/post-move identities recompute to the same numbers")


# --------------------------------------------------------------------------------------
# AC2-08 族级卡 + 350 条 provider 差异记录 + 逐条权利审阅
# --------------------------------------------------------------------------------------
def measure_ac2_08() -> Facts:
    """AC2-08 测量面：族级卡+350条provider差异记录 / requester=cloudQuant、具体scenario已确认."""
    rows = task_rows()
    rights_text = read(RIGHTS_REL)
    section = rights_text.split("## 1. 登记表")[-1].split("\n## ")[0]
    budget = read("opendata/data/request_budget.py")
    return {
        "ledger_rows": str(len(rows)),
        "rights_registry_rows": str(len(re.findall(r"^\|\s*\d+\s*\|", section, re.M))),
        "provider_delta_filled": str(sum(1 for row in rows if row["provider_delta_ref"])),
        "family_status_assessed": str(
            sum(1 for row in rows if row["contract_family_status"] != "NOT_ASSESSED")
        ),
        "scenario_filled": str(sum(1 for row in rows if row["scenario"])),
        "requester_named": str(sum(1 for row in rows if row["requester"] == "cloudQuant")),
        "rights_registered_rows": str(
            sum(1 for row in rows if row["rights_status"].startswith("REGISTERED"))
        ),
        "gate_decision_states": str(
            sum(
                1
                for token in ('ALLOWED = "ALLOWED"', 'DENIED = "DENIED"', 'UNKNOWN = "UNKNOWN"')
                if token in budget
            )
        ),
        "gate_host_scope": str("allowed_hosts" in budget),
        "domain_permission_gate": str(
            "def require_domain_semantics" in read("opendata/data/domains.py")
        ),
        "family_card_doc_files": str(
            len([path for path in (REPO_ROOT / "docs").rglob("*.md") if "族级" in path.name])
        ),
    }


def judge_ac2_08(facts: Facts) -> Verdict:
    """AC2-08 判定：族级卡与 350 条来源差异记录逐条落地，权利状态驱动操作门禁."""
    checks = {
        "350 delta records": facts["provider_delta_filled"] == "350",
        "every row has a confirmed scenario": facts["scenario_filled"] == "350",
        "requester named on every row": facts["requester_named"] == "350",
        "family status assessed on every row": facts["family_status_assessed"] == "350",
        "rights registered per model": facts["rights_registered_rows"] == "350",
        "the operation gate reads three states": facts["gate_decision_states"] == "3",
        "the gate bounds the host": facts["gate_host_scope"] == "True",
        "the domain semantics gate is present": facts["domain_permission_gate"] == "True",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "family cards, deltas and rights gates all carry content"
    )


# --------------------------------------------------------------------------------------
# AC2-09 Key/SDK 隔离
# --------------------------------------------------------------------------------------
def measure_ac2_09() -> Facts:
    """AC2-09 测量面：稳定可诊断状态 / 缺凭据不发请求."""
    provider_py = read("opendata/data/provider.py")
    rows = task_rows()
    caps = live_capabilities()
    return {
        "health_states": str(
            len(set(re.findall(r'"(ready|missing_key|missing_sdk|not_implemented)"', provider_py)))
        ),
        "credential_specs": str(count_needle("CredentialSpec(", ["opendata/data/providers"])),
        "credential_assessed_rows": str(
            sum(1 for row in rows if row["credential_requirement_status"] != "NOT_ASSESSED")
        ),
        "guard_before_client": str(count_needle("THS_NOT_CONFIGURED", [THS_REL])),
        "auto_route_skip": str(count_needle("AUTO_ROUTE_CREDENTIAL_MISSING", ["opendata/data"])),
        "health_stays_local": str("without contacting a source" in provider_py),
        "verified_sources": str(len({str(cap.source) for cap in caps if cap.verified})),
        "isolation_tests": str(
            test_function_count(
                "tests/test_provider_catalog.py",
                "tests/test_key_health.py",
                "tests/test_ported_import_closure.py",
            )
        ),
    }


def judge_ac2_09(facts: Facts) -> Verdict:
    """AC2-09 判定：缺依赖状态可诊断，必需性逐模型判定."""
    checks = {
        "four diagnosable health states": facts["health_states"] == "4",
        "a missing key refuses before the client is built": int(facts["guard_before_client"]) >= 1,
        "auto-routing skips credential-less sources": int(facts["auto_route_skip"]) >= 1,
        "the health check stays local": facts["health_stays_local"] == "True",
        "required/optional decided on every model": facts["credential_assessed_rows"] == "350",
        "isolation tests exist": int(facts["isolation_tests"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "missing key/SDK produces a diagnosis without a request")


# --------------------------------------------------------------------------------------
# DEV_DONE 的见证面：台账标签是声明，跑过的契约套件才是证据
# --------------------------------------------------------------------------------------
CONTRACT_SUITE_REL: Final = "tests/test_provider_model_contracts.py"
POSITIVE_FACE: Final = "test_typical_request_normalizes_the_declared_columns"
REFUSAL_MARKS: Final = (
    "is_refused",
    "shape_failure",
    "is_a_failure",
    "are_incomplete",
    "are_a_conflict",
    "fails_before",
)

#: One process runs the engine discovery and the contract suite once; the four AC2 cases that
#: count DEV_DONE read the same execution rather than four different ones.
_ENGINE_DECLARED_RUN: list[set[str]] = []
_CONTRACT_FACES_RUN: list[dict[str, tuple[int, int]]] = []


def engine_declared_keys() -> set[str]:
    """``source::Model`` for every model the runtime registration truth declares into the engine."""
    if not _ENGINE_DECLARED_RUN:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from opendata.data.providers import catalog

        _ENGINE_DECLARED_RUN.append(
            {f"{source}::{spec.model}" for source, spec in catalog.engine_declared_models()}
        )
    return _ENGINE_DECLARED_RUN[0]


def passing_contract_faces() -> dict[str, tuple[int, int]]:
    """Per ``source::Model``: positive and refusal contract tests this process just passed.

    The suite is run rather than read. A test file that names a model only proves it is named, and
    AC2-10 refuses 空壳/固定样本/永远空 as DEV_DONE; the executed positive arm asserts the declared
    columns come out non-empty and standardized, the refusal arms assert the same declaration turns
    a bad upstream body into a named failure. A testcase that skipped or failed carries no witness.
    """
    if not _CONTRACT_FACES_RUN:
        _CONTRACT_FACES_RUN.append(_run_contract_suite())
    return _CONTRACT_FACES_RUN[0]


def _run_contract_suite() -> dict[str, tuple[int, int]]:
    """Execute the contract suite once and read its per-testcase verdicts off the junit record."""
    with tempfile.TemporaryDirectory(prefix="ac2-contract-") as tmp:
        xml = Path(tmp) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            CONTRACT_SUITE_REL,
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-cov",
            f"--junitxml={xml}",
        ]
        exit_code, output = tool(argv)
        if not xml.is_file():
            raise RuntimeError(f"contract suite wrote no junit (exit={exit_code}): {output[-300:]}")
        faces: dict[str, list[int]] = {}
        # The file is the junit record this same process asked pytest to write into a private temp
        # directory a moment ago, so S314's untrusted-input premise does not hold here; adding
        # defusedxml would break the zero-dependency rule the same gate enforces.
        root = ElementTree.parse(xml).getroot()  # noqa: S314  # nosec B314  # own junit in our tmp
        for case in root.iter("testcase"):
            if list(case):  # a failure/error/skipped child means this testcase proved nothing
                continue
            match = re.match(r"^(?P<test>[a-zA-Z0-9_]+)\[(?P<key>[^\]]+)\]$", str(case.get("name")))
            if not match:
                continue
            entry = faces.setdefault(match.group("key"), [0, 0])
            name = match.group("test")
            if name == POSITIVE_FACE:
                entry[0] += 1
            elif any(mark in name for mark in REFUSAL_MARKS):
                entry[1] += 1
        return {key: (positive, refusal) for key, (positive, refusal) in faces.items()}


def witnessed_dev_done(rows: list[dict[str, str]]) -> set[str]:
    """Task ids whose DEV_DONE rests on a declaration plus an executed positive and refusal face."""
    declared = engine_declared_keys()
    faces = passing_contract_faces()
    witnessed = set()
    for row in rows:
        key = f"{row['provider']}::{row['upstream_model']}"
        positive, refusal = faces.get(key, (0, 0))
        if key in declared and positive > 0 and refusal > 0:
            witnessed.add(row["task_id"])
    return witnessed


def label_only_rows(rows: list[dict[str, str]], witnessed: set[str]) -> list[str]:
    """Rows the ledger stamps DEV_DONE while the executed faces name no witness for them."""
    return [
        row["task_id"]
        for row in rows
        if row["implementation_task_status"] == "DEV_DONE" and row["task_id"] not in witnessed
    ]


# --------------------------------------------------------------------------------------
# AC2-10 350 模型实例
# --------------------------------------------------------------------------------------
def measure_ac2_10() -> Facts:
    """AC2-10 测量面：分片集合覆盖适用350身份 / e2e标记及精确OPENDATA_ALLOW_LIVE_E2E闸门."""
    rows = task_rows()
    census: dict[str, int] = {}
    for row in rows:
        census[row["implementation_task_status"]] = (
            census.get(row["implementation_task_status"], 0) + 1
        )
    witnessed = witnessed_dev_done(rows)
    naked = label_only_rows(rows, witnessed)
    conftest = read("tests/conftest.py")
    contract_files = sorted(
        path.name
        for path in (REPO_ROOT / "tests").glob("test_*.py")
        if "ModelSpec" in path.read_text(encoding="utf-8", errors="replace")
    )
    return {
        "ledger_rows": str(len(rows)),
        "obb2_task_ids": str(sum(1 for row in rows if row["task_id"].startswith("OBB2-"))),
        "dev_done": str(len(witnessed)),
        "dev_done_label": str(census.get("DEV_DONE", 0)),
        "label_without_witness": str(len(naked)),
        "label_without_witness_ids": ",".join(sorted(naked)[:6]) or "-",
        "engine_declared_rows": str(
            sum(
                1
                for row in rows
                if f"{row['provider']}::{row['upstream_model']}" in engine_declared_keys()
            )
        ),
        "in_progress": str(census.get("IN_PROGRESS", 0)),
        "not_run": str(census.get("NOT_RUN", 0)),
        "live_verification_run": str(
            sum(1 for row in rows if row["live_verification_status"] not in ("NOT_RUN", ""))
        ),
        "e2e_marker_declared": str("LIVE_E2E_MARKER" in conftest),
        "gate_live_confirmation": str("allow-prod-and-upstream-writes" in conftest),
        "contract_test_files": ",".join(contract_files) or "-",
        "contract_test_functions": str(
            test_function_count(*[f"tests/{name}" for name in contract_files])
        ),
    }


def judge_ac2_10(facts: Facts) -> Verdict:
    """AC2-10 判定：350 个模型实例各有实现、离线正反例、标准契约与服务入口，真实对照另记."""
    checks = {
        "350 ledger rows": facts["ledger_rows"] == "350",
        "every row carries an OBB2 task id": facts["obb2_task_ids"] == "350",
        "all 350 are DEV_DONE with an executed witness": facts["dev_done"] == "350",
        "no DEV_DONE label sits without a witness": facts["label_without_witness"] == "0",
        "no row left at NOT_RUN": facts["not_run"] == "0",
        "a contract suite reads the specs": facts["contract_test_files"] != "-"
        and int(facts["contract_test_functions"]) > 0,
        "live legs sit behind the e2e marker and the exact token": facts["e2e_marker_declared"]
        == "True"
        and facts["gate_live_confirmation"] == "True",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "every model instance has implementation, tests and entry"
    )


# --------------------------------------------------------------------------------------
# AC2-11 既有 5 源 85 条固定模型与原 12 条能力
# --------------------------------------------------------------------------------------
def measure_ac2_11() -> Facts:
    """AC2-11 测量面：既有 5 源 85 条模型与原 12 条能力."""
    rows = task_rows()
    p0 = [row for row in rows if row["batch"] == "P0"]
    witnessed = witnessed_dev_done(p0)
    sources = sorted({row["provider"] for row in p0})
    caps = per_source_caps()
    inventory = inventory_doc()
    legacy_fetchers = sum(
        int(entry["fetchers"]) for entry in inventory["providers"] if entry["provider"] in sources
    )
    return {
        "p0_rows": str(len(p0)),
        "p0_providers": str(len(sources)),
        "p0_provider_names": ",".join(sources),
        "p0_dev_done": str(len(witnessed)),
        "p0_dev_done_label": str(
            sum(1 for row in p0 if row["implementation_task_status"] == "DEV_DONE")
        ),
        "p0_label_without_witness": str(len(label_only_rows(p0, witnessed))),
        "p0_registry_caps": str(sum(caps.get(source, 0) for source in sources)),
        "p0_inventory_fetchers": str(legacy_fetchers),
        "p0_live_verified": str(
            sum(1 for row in p0 if row["live_verification_status"] == "SOURCE_VERIFIED")
        ),
        "p0_localized_verified_caps": str(
            sum(1 for cap in live_capabilities() if str(cap.source) in sources and cap.verified)
        ),
    }


def judge_ac2_11(facts: Facts) -> Verdict:
    """AC2-11 判定：5 个既有来源的 85 条模型逐条完成 AC2-10，且原有 12 条能力语义无回退."""
    checks = {
        "85 rows across the five existing sources": facts["p0_rows"] == "85"
        and facts["p0_providers"] == "5",
        "the original 12 capabilities survive": facts["p0_localized_verified_caps"] == "12",
        "the inventory counts the same 85 for these five sources": facts["p0_inventory_fetchers"]
        == facts["p0_rows"],
        "all 85 are DEV_DONE with an executed witness": facts["p0_dev_done"] == "85",
        "no P0 label sits without a witness": facts["p0_label_without_witness"] == "0",
        "the registry still serves at least the legacy 12": int(facts["p0_registry_caps"]) >= 12,
        "no semantic regression (each of the 85 live-verified)": facts["p0_live_verified"] == "85",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "85 rows and the legacy capability set both accounted for"
    )


# --------------------------------------------------------------------------------------
# AC2-12 7 个其他宏观 33 条固定模型
# --------------------------------------------------------------------------------------
def measure_ac2_12() -> Facts:
    """AC2-12 测量面：7个其他宏观33条固定模型 / EIA0模型误记."""
    rows = task_rows()
    macro = [row for row in rows if row["phase"] == "2B" and row["batch"] == "P1"]
    macro_witnessed = witnessed_dev_done(macro)
    models = inventory_report()["reconstructed_models"]
    eia_rows = [row for row in rows if row["provider"] == "eia"]
    eia_models = [row for row in models if row.get("provider") == "eia"]
    return {
        "macro_rows": str(len(macro)),
        "macro_providers": str(len({row["provider"] for row in macro})),
        "macro_provider_names": ",".join(sorted({row["provider"] for row in macro})),
        "macro_dev_done": str(len(macro_witnessed)),
        "macro_dev_done_label": str(
            sum(1 for row in macro if row["implementation_task_status"] == "DEV_DONE")
        ),
        "macro_label_without_witness": str(len(label_only_rows(macro, macro_witnessed))),
        "macro_live_verified": str(
            sum(1 for row in macro if row["live_verification_status"] == "SOURCE_VERIFIED")
        ),
        "eia_ledger_rows": str(len(eia_rows)),
        "eia_upstream_models": str(len(eia_models)),
    }


def judge_ac2_12(facts: Facts) -> Verdict:
    """AC2-12 判定：7 个宏观来源的 33 条模型逐条完成 AC2-10 与适用真实对照，EIA 模型数按上游登记."""
    checks = {
        "33 rows across 7 macro providers": facts["macro_rows"] == "33"
        and facts["macro_providers"] == "7",
        "all 33 are DEV_DONE with an executed witness": facts["macro_dev_done"] == "33",
        "no macro label sits without a witness": facts["macro_label_without_witness"] == "0",
        "each has its applicable real comparison": facts["macro_live_verified"] == "33",
        "EIA's row count follows upstream, not a mistaken zero": facts["eia_ledger_rows"]
        == facts["eia_upstream_models"],
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "macro rows complete and EIA recorded against upstream")


# --------------------------------------------------------------------------------------
# AC2-13 5 商业/聚合 120 条 + 跨阶段 15 源 / 195 凭据暴露行
# --------------------------------------------------------------------------------------
def measure_ac2_13() -> Facts:
    """AC2-13 测量面：5商业/聚合120条模型 / 15源/195凭据暴露行."""
    rows = task_rows()
    commercial = [row for row in rows if row["phase"] == "2C"]
    commercial_witnessed = witnessed_dev_done(commercial)
    exposed = [row for row in rows if row["credential_fields"]]
    decisions: dict[str, int] = {}
    for row in rows:
        decisions[row["key_decision_status"]] = decisions.get(row["key_decision_status"], 0) + 1
    made = sum(
        count
        for state, count in decisions.items()
        if state.startswith(("APPROVED", "DECLINED", "GRANTED", "REFUSED"))
    )
    return {
        "commercial_rows": str(len(commercial)),
        "commercial_providers": str(len({row["provider"] for row in commercial})),
        "commercial_dev_done": str(len(commercial_witnessed)),
        "commercial_dev_done_label": str(
            sum(1 for row in commercial if row["implementation_task_status"] == "DEV_DONE")
        ),
        "commercial_label_without_witness": str(
            len(label_only_rows(commercial, commercial_witnessed))
        ),
        "exposed_rows": str(len(exposed)),
        "exposed_providers": str(len({row["provider"] for row in exposed})),
        "decisions_made": str(made),
        "decisions_pending": str(decisions.get("PENDING_DECISION", 0)),
        "access_assessment_pending": str(decisions.get("PENDING_ACCESS_ASSESSMENT", 0)),
        "exposed_rows_decided": str(
            sum(
                1
                for row in exposed
                if row["key_decision_status"].startswith(
                    ("APPROVED", "DECLINED", "GRANTED", "REFUSED")
                )
            )
        ),
    }


def judge_ac2_13(facts: Facts) -> Verdict:
    """AC2-13 判定：120 条商业/聚合模型逐条完成，跨阶段 15 源 195 行凭据暴露都有访问决定."""
    checks = {
        "120 rows across 5 commercial providers": facts["commercial_rows"] == "120"
        and facts["commercial_providers"] == "5",
        "all 120 are DEV_DONE with an executed witness": facts["commercial_dev_done"] == "120",
        "no commercial label sits without a witness": facts["commercial_label_without_witness"]
        == "0",
        "195 credential-exposed rows across 15 sources": facts["exposed_rows"] == "195"
        and facts["exposed_providers"] == "15",
        "every exposure row carries an access decision": facts["exposed_rows_decided"] == "195",
        "no pending decision is left implicit": facts["decisions_pending"] == "0"
        and facts["access_assessment_pending"] == "0",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "120 rows implemented and all 195 exposures decided")


# --------------------------------------------------------------------------------------
# AC2-14 15 个其他来源 112 条模型
# --------------------------------------------------------------------------------------
def measure_ac2_14() -> Facts:
    """AC2-14 测量面：15个其他来源112条模型 / 请求型Quote不扩成实时服务."""
    rows = task_rows()
    other = [row for row in rows if row["phase"] == "2D"]
    other_witnessed = witnessed_dev_done(other)
    phases = {phase for row in rows for phase in [row["phase"]]}
    return {
        "other_rows": str(len(other)),
        "other_providers": str(len({row["provider"] for row in other})),
        "other_provider_names": ",".join(sorted({row["provider"] for row in other})),
        "other_dev_done": str(len(other_witnessed)),
        "other_dev_done_label": str(
            sum(1 for row in other if row["implementation_task_status"] == "DEV_DONE")
        ),
        "other_label_without_witness": str(len(label_only_rows(other, other_witnessed))),
        "other_live_verified": str(
            sum(1 for row in other if row["live_verification_status"] == "SOURCE_VERIFIED")
        ),
        "phase_partition_sums_to_350": str(
            len(rows) == 350
            and sum(1 for row in rows if row["phase"] == "2B")
            + sum(1 for row in rows if row["phase"] == "2C")
            + len(other)
            == 350
        ),
        "phase_count": str(len(phases)),
    }


def judge_ac2_14(facts: Facts) -> Verdict:
    """AC2-14 判定：15 个其他来源的 112 条模型按快照/报价/新闻/披露/衍生品各自契约完成."""
    checks = {
        "112 rows across 15 sources": facts["other_rows"] == "112"
        and facts["other_providers"] == "15",
        "all 112 are DEV_DONE with an executed witness": facts["other_dev_done"] == "112",
        "no other-source label sits without a witness": facts["other_label_without_witness"] == "0",
        "each verified against its own contract family": facts["other_live_verified"] == "112",
        "the three phases partition the ledger": facts["phase_partition_sums_to_350"] == "True",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "other-source rows implemented inside a stable partition"
    )


# --------------------------------------------------------------------------------------
# AC2-15 显式/auto 与降级、导入零网络、逐源/全局/host 预算
# --------------------------------------------------------------------------------------
def measure_ac2_15() -> Facts:
    """AC2-15 测量面：显式源不回退 / auto排除未验证/缺配置/用途不符."""
    inventory = inventory_doc()
    budget = read("opendata/data/request_budget.py")
    config = read("opendata/core/config.py")
    caps = live_capabilities()
    return {
        "capabilities": str(len(caps)),
        "auto_routable": str(inventory["auto_routable_capability_count"]),
        "auto_condition_sites": str(count_needle("participates_in_auto", ["opendata/data"])),
        "fail_closed_messages": str(
            count_needle("no credential-eligible verified auto candidate", ["opendata/data"])
            + count_needle("are unavailable", ["opendata/data"])
        ),
        "source_budget_constant": str("MAX_SOURCE_ATTEMPTS" in budget),
        "task_budget_constant": str("MAX_TASK_ATTEMPTS" in budget),
        "global_rate_constant": str("rate_limit_per_minute" in config),
        "host_grant_scope": str("allowed_hosts" in budget),
        "host_concurrency_limiter": str(
            count_needle("concurrency_gate", ["opendata"]) + count_needle("Semaphore", ["opendata"])
        ),
        "verified_caps": str(sum(1 for cap in caps if cap.verified)),
        "import_time_fetch": cold_import_probe("import opendata.data.providers.catalog"),
        "budget_test_functions": str(
            test_function_count(
                "tests/test_request_budget.py",
                "tests/test_fetcher_request_budget.py",
                "tests/test_fallback_degradation.py",
                "tests/test_provider_request_budget_errors.py",
            )
        ),
    }


def judge_ac2_15(facts: Facts) -> Verdict:
    """AC2-15 判定：显式选择不回退，auto 排除与三层预算."""
    checks = {
        "auto routing filters by participation, health and credentials": int(
            facts["auto_condition_sites"]
        )
        >= 2
        and int(facts["fail_closed_messages"]) >= 2,
        "importing provider metadata does not fetch": facts["import_time_fetch"] == "[]",
        "per-source budget declared": facts["source_budget_constant"] == "True",
        "per-task budget declared": facts["task_budget_constant"] == "True",
        "global rate declared": facts["global_rate_constant"] == "True",
        "host scope declared": facts["host_grant_scope"] == "True",
        "a host-level limiter exists": int(facts["host_concurrency_limiter"]) >= 1,
        "budget tests exist": int(facts["budget_test_functions"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "selection, degradation and three budget scopes all bite"
    )


# --------------------------------------------------------------------------------------
# AC2-16 统一 async 包装与 350 行适用性声明
# --------------------------------------------------------------------------------------
def measure_ac2_16() -> Facts:
    """AC2-16 测量面：全350行有适用性声明 / unsupported在I/O前诊断."""
    rows = task_rows()
    census: dict[str, int] = {}
    for row in rows:
        census[row["async_mode"]] = census.get(row["async_mode"], 0) + 1
    arms = cold_import_probe(
        "import dataclasses, json\n"
        "from opendata.data.providers import catalog\n"
        "specs = catalog.engine_declared_models()\n"
        "if not specs:\n"
        "    print('no-spec'); raise SystemExit\n"
        "sample = specs[0][1]\n"
        "def attempt(mode):\n"
        "    try:\n"
        "        dataclasses.replace(sample, async_mode=mode)\n"
        "        return 'accepted'\n"
        "    except ValueError:\n"
        "        return 'rejected'\n"
        "    except Exception as error:\n"
        "        return type(error).__name__\n"
        "print('ARMS ' + json.dumps([len(specs), attempt('telepathy'), attempt('unsupported')]))\n"
    )
    arms_line = next((line[5:] for line in arms.splitlines() if line.startswith("ARMS ")), arms)
    return {
        "rows": str(len(rows)),
        "assessed": str(sum(count for mode, count in census.items() if mode != "NOT_ASSESSED")),
        "bounded_thread": str(census.get("bounded_thread", 0)),
        "unsupported": str(census.get("unsupported", 0)),
        "not_assessed": str(census.get("NOT_ASSESSED", 0)),
        "engine_read_sites": str(count_needle("async_mode", ["opendata/data/providers/_engine"])),
        "unsupported_refusal_tests": str(count_needle("UnsupportedAsyncFetcherError", ["tests"])),
        "spec_arms": arms_line,
    }


def judge_ac2_16(facts: Facts) -> Verdict:
    """AC2-16 判定：async 适用性逐行声明，拒绝在 I/O 前."""
    arms = facts["spec_arms"]
    try:
        declared, invalid, valid = json.loads(arms) if arms.startswith("[") else ("0", "-", "-")
    except (ValueError, TypeError):
        declared, invalid, valid = ("0", arms, arms)
    checks = {
        "all 350 rows carry an applicability declaration": facts["assessed"] == "350",
        "the unsupported refusal has an executed test": facts["unsupported_refusal_tests"] != "0",
        "an out-of-set async_mode is refused, not ignored": invalid == "rejected",
        "a declared mode is accepted (control arm lives)": valid == "accepted",
        "engine declarations are discoverable": int(str(declared)) >= 1,
        "the engine reads the declaration": int(facts["engine_read_sites"]) >= 1,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "async applicability is declared and enforced per model")


# --------------------------------------------------------------------------------------
# AC2-17 新模型族 Fetcher → ODS/DWD → API/CLI/SDK
# --------------------------------------------------------------------------------------
ROUTE_VERBS = ("get", "post", "put", "delete", "patch")


def provider_model_routes() -> list[tuple[str, str, str | None]]:
    """Return (module, HTTP verb, path literal) triples found by AST, not line regex."""
    found: list[tuple[str, str, str | None]] = []
    for path in sorted((REPO_ROOT / "opendata/api").glob("provider_model*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                target = deco.func
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in ROUTE_VERBS
                    and isinstance(target.value, ast.Name)
                    and target.value.id.endswith("router")
                ):
                    first = deco.args[0] if deco.args else None
                    literal = first.value if isinstance(first, ast.Constant) else None
                    found.append(
                        (path.name, target.attr, literal if isinstance(literal, str) else None)
                    )
    return found


def measure_ac2_17() -> Facts:
    """AC2-17 测量面：Fetcher→ODS/标准化/DWD或受控源查询→API/CLI/SDK / 只实现Fetcher无服务入口."""
    client = read("opendata_client/opendata_client/client.py")
    cli = read("opendata/cli.py")
    registry_src = read("opendata/data/registry.py")
    routes = provider_model_routes()
    verbs: dict[str, int] = {}
    for _, verb, _ in routes:
        verbs[verb] = verbs.get(verb, 0) + 1
    return {
        "api_route_modules": str(len({module for module, _, _ in routes})),
        "api_model_routes": str(len(routes)),
        "api_route_verbs": ",".join(f"{k}:{v}" for k, v in sorted(verbs.items())) or "-",
        "api_routes_without_path": str(sum(1 for _, _, path in routes if path is None)),
        "ods_writer_symbols": str(
            sum(
                1
                for symbol in ("class OdsWriter", "def plan_frame", "def build_upsert_sql")
                if symbol in read("opendata/pipeline/ods_writer.py")
            )
        ),
        "dwd_symbols": str(
            sum(
                1
                for symbol in (
                    "def merge_source_frames",
                    "class DwdWriter",
                    "class DwdMergeService",
                )
                if symbol in read("opendata/pipeline/dwd_merge.py")
            )
        ),
        "sdk_methods": str(
            sum(
                1
                for symbol in (
                    "def provider_models",
                    "def query_provider_model",
                    "def ingest_provider_model",
                    "def export_provider_model",
                )
                if symbol in client
            )
        ),
        "cli_model_commands": str(len(re.findall(r"provider[-_]model", cli))),
        "layer_flags_in_descriptor": str(
            sum(
                1
                for name in ("api_layer", "cli_layer", "sdk_layer", "served")
                if name in registry_src
            )
        ),
        "coverage_status_assessed": str(
            sum(1 for row in task_rows() if row["coverage_status"] != "NOT_ASSESSED")
        ),
    }


def judge_ac2_17(facts: Facts) -> Verdict:
    """AC2-17 判定：模型族从 Fetcher 到 ODS/DWD 到 API/CLI/SDK 四层可达，目录显示真实覆盖."""
    checks = {
        "API routes exposed on the provider-model modules": int(facts["api_model_routes"]) >= 4
        and int(facts["api_route_modules"]) >= 4
        and int(facts["api_routes_without_path"]) == 0,
        "the family is served for both reads and writes": "get:" in facts["api_route_verbs"]
        and "post:" in facts["api_route_verbs"],
        "ODS layer wired": facts["ods_writer_symbols"] == "3",
        "DWD layer wired": facts["dwd_symbols"] == "3",
        "SDK methods present": facts["sdk_methods"] == "4",
        "a CLI entry exists for provider models": int(facts["cli_model_commands"]) >= 1,
        "the catalog declares which layers a family reaches": int(
            facts["layer_flags_in_descriptor"]
        )
        >= 1,
        "coverage is recorded per model": facts["coverage_status_assessed"] == "350",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "a family is reachable through all four layers")


# --------------------------------------------------------------------------------------
# AC2-18 公司行动重复/冲突与修订传播
# --------------------------------------------------------------------------------------
def measure_ac2_18() -> Facts:
    """AC2-18 测量面：公司行动重复/冲突及修订传播 / 稳定业务键保留独立事件."""
    models = read("opendata/data/models/adjustment.py")
    factors = read("opendata/pipeline/factors.py")
    merge = read("opendata/pipeline/dwd_merge.py")
    gold_dirs = sorted(
        path.name
        for path in (REPO_ROOT / "tests/fixtures/upstream").iterdir()
        if path.is_dir() and "stock_action" in path.name
    )
    return {
        "contract_symbols": str(
            sum(
                1
                for symbol in (
                    "class AdjustFactor",
                    "class CorporateAction",
                    "def validate_adjustment_coefficients",
                )
                if symbol in models
            )
        ),
        "factor_symbols": str(
            sum(
                1
                for symbol in (
                    "class FactorEvent",
                    "def event_ratio",
                    "def cumulate_factors",
                    "def validate_event",
                )
                if symbol in factors
            )
        ),
        "conflict_symbols": str(
            sum(
                1
                for symbol in ("def _disagreeing_keys", "def _differs", "_diff_flag")
                if symbol in merge
            )
        ),
        "business_key_sites": str(count_needle("business_key", ["opendata"], skip_vendor=True)),
        "gold_dirs": str(len(gold_dirs)),
        "gold_dir_names": ",".join(gold_dirs) or "-",
        "conflict_fixtures": str(len(list((REPO_ROOT / "tests/fixtures").rglob("*conflict*")))),
        "offline_test_functions": str(
            test_function_count(
                "tests/test_factors.py",
                "tests/test_factor_builder_unit.py",
                "tests/test_adjust.py",
                "tests/test_dwd_merge.py",
            )
        ),
    }


def judge_ac2_18(facts: Facts) -> Verdict:
    """AC2-18 判定：公司行动的稳定业务键、冲突可审计与因子/DWD/WS 修订传播，并有 gold 样本回填."""
    checks = {
        "corporate-action contracts present": facts["contract_symbols"] == "3",
        "factor pipeline present": facts["factor_symbols"] == "4",
        "conflict detection present": facts["conflict_symbols"] == "3",
        "a stable business key is named in code": int(facts["business_key_sites"]) >= 1,
        "gold samples backfilled": int(facts["gold_dirs"]) >= 2
        and int(facts["conflict_fixtures"]) >= 1,
        "offline behaviour tests exist": int(facts["offline_test_functions"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "duplicate, conflict and revision propagation are covered"
    )


# --------------------------------------------------------------------------------------
# AC2-19 PIT：两次发布/重述样本与 as_of 查询
# --------------------------------------------------------------------------------------
def measure_ac2_19() -> Facts:
    """AC2-19 测量面：PIT两次发布/重述样本 / 公开前不可见."""
    merge = read("opendata/pipeline/dwd_merge.py")
    store = read("opendata/services/provider_model_store.py")
    client = read("opendata_client/opendata_client/client.py")
    api_pipeline = read("opendata/api/pipeline.py")
    return {
        "write_as_of_sites": str(merge.count("_as_of")),
        "api_as_of_parameter": str("as_of: date | None = Query(None" in api_pipeline),
        "read_path_as_of_sites": str(store.count("as_of")),
        "sdk_as_of_sites": str(client.count("as_of")),
        "pit_trace_columns": str(
            sum(1 for token in ('"_merged_at"', '"_diff_flag"', '"_as_of"') if token in merge)
        ),
        "pit_test_functions": str(test_function_count("tests/test_dwd_merge.py")),
        "restatement_fixtures": str(len(list((REPO_ROOT / "tests/fixtures").rglob("*restate*")))),
        "alembic_data_versions": str(len(list((REPO_ROOT / "alembic_data/versions").glob("*.py")))),
    }


def judge_ac2_19(facts: Facts) -> Verdict:
    """AC2-19 判定：as_of 读侧语义不被重述改写."""
    checks = {
        "writes stamp the as_of face": int(facts["write_as_of_sites"]) >= 3,
        "the service exposes an as_of parameter": facts["api_as_of_parameter"] == "True",
        "warehouse reads honour as_of": int(facts["read_path_as_of_sites"]) >= 3,
        "the SDK exposes as_of": int(facts["sdk_as_of_sites"]) >= 1,
        "point-in-time trace columns are recorded": facts["pit_trace_columns"] == "3",
        "two publishes/restatements are sampled offline": int(facts["restatement_fixtures"]) >= 2,
        "offline PIT tests exist": int(facts["pit_test_functions"]) > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "as_of slicing holds across write, service and SDK")


# --------------------------------------------------------------------------------------
# AC2-20 SDK 独立安装与 ws / frames / 分页
# --------------------------------------------------------------------------------------
def measure_ac2_20() -> Facts:
    """AC2-20 测量面：最小安装不漏loguru / ws extra真实存在."""
    client_pyproject = tomllib.loads(read("opendata_client/pyproject.toml"))
    root_pyproject = tomllib.loads(read("pyproject.toml"))
    client = read("opendata_client/opendata_client/client.py")
    dependencies = client_pyproject["project"].get("dependencies", [])
    extras = client_pyproject["project"].get("optional-dependencies", {})
    dist = REPO_ROOT / "dist"
    return {
        "sdk_dependencies": json.dumps(dependencies, ensure_ascii=False),
        "sdk_extras": json.dumps(sorted(extras), ensure_ascii=False),
        "loguru_declared": str(any(str(dep).startswith("loguru") for dep in dependencies)),
        "loguru_imported": str("from loguru import logger" in client),
        "ws_extra_declared": str("ws" in extras),
        "websockets_imported": str("from websockets" in client),
        "root_ws_extra_declared": str(
            "ws" in root_pyproject["project"].get("optional-dependencies", {})
        ),
        "paging_class": str("class Page" in client),
        "sdk_pit_parameter": str("as_of" in client),
        "built_sdk_artifact": str(len(list(dist.glob("opendata_client*"))) if dist.is_dir() else 0),
    }


def judge_ac2_20(facts: Facts) -> Verdict:
    """AC2-20 判定：SDK 独立安装：声明面与导入面一致，loguru 不漏，ws extra 真实存在."""
    checks = {
        "loguru is declared where it is imported": facts["loguru_declared"] == "True"
        and facts["loguru_imported"] == "True",
        "the ws extra is declared where websockets is imported": facts["ws_extra_declared"]
        == "True"
        and facts["websockets_imported"] == "True",
        "frames extra declared": '"frames"' in facts["sdk_extras"],
        "service paging is representable": facts["paging_class"] == "True",
        "PIT parameters reach the SDK": facts["sdk_pit_parameter"] == "True",
        "an installable SDK artifact exists": int(facts["built_sdk_artifact"]) >= 1,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "SDK dependency surface matches its import surface")


# --------------------------------------------------------------------------------------
# AC2-21 前端下载按钮 → 后端请求 → 任务 → 状态
# --------------------------------------------------------------------------------------
def measure_ac2_21() -> Facts:
    """AC2-21 测量面：ID语义与interface_id等实际schema一致 / 不把422显示成下载成功."""
    api_ts = read("frontend/src/api/data.ts")
    view = read("frontend/src/views/ScriptDetailView.vue")
    route = read("opendata/api/data.py")
    schemas = read("opendata/api/schemas.py")
    tests = read("frontend/src/__tests__/script-download.test.ts")
    return {
        "fe_id_sites": str(api_ts.count("interface_id")),
        "be_id_sites": str(schemas.count("interface_id")),
        "ids_match": str("interface_id" in api_ts and "interface_id" in schemas),
        "download_route": str('@router.post("/download"' in route),
        "accepted_status_only": str("status === 202" in api_ts),
        "view_uses_selected_interface": str("selectedInterface.value?.id" in view),
        "fe_test_cases": str(len(re.findall(r"\bit\(", tests))),
        "fe_422_case": str("422" in tests),
        "fe_error_not_success_case": str("without claiming success" in tests),
        "execution_id_carried_back": str("execution_id" in api_ts and "execution_id" in schemas),
    }


def judge_ac2_21(facts: Facts) -> Verdict:
    """AC2-21 判定：前端下载按钮到后端路由的任务 ID 语义一致，失败状态如实反馈."""
    checks = {
        "frontend and backend name the same id": facts["ids_match"] == "True"
        and int(facts["fe_id_sites"]) >= 1
        and int(facts["be_id_sites"]) >= 1,
        "the backend route exists and the page trusts only 202": facts["download_route"] == "True"
        and facts["accepted_status_only"] == "True",
        "non-202 statuses are not shown as success": facts["fe_422_case"] == "True"
        and facts["fe_error_not_success_case"] == "True",
        "the button reads the selected interface": facts["view_uses_selected_interface"] == "True",
        "the task id travels back": facts["execution_id_carried_back"] == "True",
        "offline component tests exist": int(facts["fe_test_cases"]) >= 5,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "the download path is id-consistent and status-honest")


# --------------------------------------------------------------------------------------
# AC2-22 请求/安全债务
# --------------------------------------------------------------------------------------
# 重试必须同时具备「可重试性判定」与「次数上界」，且判定落在真正发请求的代码上。
RETRY_GATE_RE = r"^\s*if\b[^\n]*\bretryable\b[^\n]*:\s*$"
ATTEMPT_BOUND_RE = (
    r"^(?:MAX_SOURCE_ATTEMPTS\s*=\s*[0-9]+"
    r"|\s*max_attempts[^\n]*= Field[^\n]*"
    r"|\s*for\b[^\n]*self\._max_attempts\b[^\n]*:)"
)


def measure_ac2_22() -> Facts:
    """AC2-22 测量面：配额/重试有界、永久错误不重试 / 关键风险处置有语义证据."""
    engine = read("opendata/data/providers/_engine/http_json.py")
    ratchet = json.loads(read(RATCHET_REL) or "{}")
    vendor_pys = walk_py(VENDOR_REL)
    first_party = walk_py("opendata", skip_vendor=True)

    def sites(token: str, paths: list[Path]) -> int:
        return sum(
            path.read_text(encoding="utf-8", errors="replace").count(token) for path in paths
        )

    gate_files = [
        str(path.relative_to(REPO_ROOT))
        for path in walk_py("opendata/data", skip_vendor=True)
        if re.search(RETRY_GATE_RE, path.read_text(encoding="utf-8", errors="replace"), re.M)
    ]
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.quality import ported_security_evidence

    try:
        result = ported_security_evidence.validate(REPO_ROOT)
        issues = len(result.issues)
        detail = ",".join(sorted({str(issue) for issue in result.issues}))[:160] or "-"
    except Exception as error:
        issues = -1
        detail = f"{type(error).__name__}:{error}"
    return {
        "engine_timeout_constant": str("DEFAULT_TIMEOUT = 30.0" in engine),
        "engine_timeout_applied": str("timeout = ctx.timeout" in engine),
        "throttle_handling": str(
            count_needle("429", ["opendata/data/providers"])
            + count_needle("rate_limit", ["opendata/data/providers"])
        ),
        "retryability_gates": str(regex_total(RETRY_GATE_RE, ["opendata/data"], skip_vendor=True)),
        "retry_gate_files": ",".join(sorted(gate_files))[:200] or "-",
        "attempt_bounds": str(regex_total(ATTEMPT_BOUND_RE, ["opendata/data"], skip_vendor=True)),
        "raw_diagnosis_codes": str(
            count_needle("_SHAPE_INVALID", ["opendata/data/providers/_engine"])
            + count_needle("_QUERY_INVALID", ["opendata/data/providers/_engine"])
        ),
        "direct_http_ported_pin": str(ratchet.get("metrics", {}).get("direct_http_ported")),
        "vendor_tls_disable": str(
            sites("verify=False", vendor_pys) + sites("CERT_NONE", vendor_pys)
        ),
        "first_party_tls_disable": str(
            sites("verify=False", first_party) + sites("CERT_NONE", first_party)
        ),
        "ported_security_issue_count": str(issues),
        "ported_security_detail": detail,
        "scan_archive_present": str((REPO_ROOT / str(ported_security_evidence.SCAN_REL)).is_file()),
        "triage_archive_present": str(
            (REPO_ROOT / str(ported_security_evidence.TRIAGE_REL)).is_file()
        ),
    }


def judge_ac2_22(facts: Facts) -> Verdict:
    """AC2-22 判定：请求/安全债务：超时、限流与重试有界，raw 响应可诊断，TLS/解析风险有语义证据."""
    checks = {
        "timeout is declared and applied": facts["engine_timeout_constant"] == "True"
        and facts["engine_timeout_applied"] == "True",
        "429/rate-limit paths handled": int(facts["throttle_handling"]) >= 1,
        "retry is bounded by a retryability decision": int(facts["retryability_gates"]) >= 1
        and int(facts["attempt_bounds"]) >= 1
        and "opendata/data/http_client.py" in facts["retry_gate_files"],
        "raw responses stay diagnosable": int(facts["raw_diagnosis_codes"]) >= 2,
        "direct-http debt is pinned, not deleted": int(facts["direct_http_ported_pin"] or 0) > 0,
        "first-party code never disables TLS": facts["first_party_tls_disable"] == "0",
        "the vendored TLS risk is counted, not hidden": int(facts["vendor_tls_disable"]) > 0,
        "current ported security evidence validates": facts["ported_security_issue_count"] == "0",
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "bounded requests and a re-validated security archive")


# --------------------------------------------------------------------------------------
# AC2-23 同负载前后比较
# --------------------------------------------------------------------------------------
def measure_ac2_23() -> Facts:
    """AC2-23 测量面：P95/RSS/连接数/SQL计划有记录 / 无测量不认定性能缺陷."""
    bundle = sorted((REPO_ROOT / "docs/evidence/C65").glob("write-benchmark-*.jsonl"))
    metrics: set[str] = set()
    rows = 0
    for path in bundle:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            rows += 1
            try:
                metrics.update(json.loads(line))
            except json.JSONDecodeError:
                continue
    validator = tool(["python", "scripts/quality/write_benchmark_evidence.py"])
    return {
        "bundle_files": str(len(bundle)),
        "bundle_rows": str(rows),
        "validator_exit": str(validator[0]),
        "recorded_metrics": ",".join(sorted(metrics))[:200] or "-",
        "p95_recorded": str(any("p95" in name.lower() for name in metrics)),
        "rss_recorded": str(any("rss" in name.lower() for name in metrics)),
        "connection_count_recorded": str(
            any(token in name.lower() for name in metrics for token in ("conn", "pool"))
        ),
        "explain_script_present": str(
            (REPO_ROOT / "scripts/ops/window_partition_explain.py").is_file()
        ),
        "after_run_bundles": str(
            len(list((REPO_ROOT / "docs/evidence").glob("*/write-benchmark-after*.jsonl")))
        ),
    }


def judge_ac2_23(facts: Facts) -> Verdict:
    """AC2-23 判定：同数据/索引/配置的负载前后比较，P95、RSS、连接数与 SQL 计划都有记录."""
    checks = {
        "a benchmark bundle is on record and self-validates": int(facts["bundle_rows"]) > 0
        and facts["validator_exit"] == "0",
        "P95 latency recorded": facts["p95_recorded"] == "True",
        "RSS recorded": facts["rss_recorded"] == "True",
        "connection count recorded": facts["connection_count_recorded"] == "True",
        "SQL plan captured by a repeatable script": facts["explain_script_present"] == "True",
        "an after-run exists for the same payload": int(facts["after_run_bundles"]) >= 1,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "before/after load comparison carries all four metric faces"
    )


# --------------------------------------------------------------------------------------
# AC2-24 同一冻结树：130 原 AC 读数 + 原 16 gap 关闭
# --------------------------------------------------------------------------------------
def measure_ac2_24() -> Facts:
    """AC2-24 测量面：先取130原AC读数，再关闭原16 gap / 没读数不闭合."""
    exit_code, output = tool(["python", "scripts/quality/acceptance_ledger_check.py", "--report"])
    ledger = json.loads(read(ITER1_LEDGER_REL) or "{}")
    items = ledger.get("items", {})
    states: dict[str, int] = {}
    for entry in items.values():
        state = str(entry.get("state"))
        states[state] = states.get(state, 0) + 1
    ac_1_10 = sorted(key for key in items if key.startswith("AC-1|10|"))
    iter1 = read(ITER1_DOC_REL)
    doc_groups = set(re.findall(r"^### (AC-\d+)", iter1, re.M))
    ledger_groups = {
        str(name) for name in (ledger.get("group_counts") or {}) if str(name).startswith("AC-")
    }
    group_sum = sum(int(value) for value in (ledger.get("group_counts") or {}).values())
    missing_evidence = sorted(
        {
            str(rel)
            for entry in items.values()
            for rel in (entry.get("evidence") or [])
            if not (REPO_ROOT / str(rel)).exists()
        }
    )
    silent = sorted(
        key
        for key, entry in items.items()
        if str(entry.get("state")) == "proven" and not str(entry.get("command") or "").strip()
    )
    return {
        "ledger_check_exit": str(exit_code),
        "iter1_ac_headings": str(len(doc_groups)),
        "group_heading_symdiff": str(len(doc_groups ^ ledger_groups)),
        "group_counts_sum": str(group_sum),
        "ledger_items": str(len(items)),
        "items_total_field": str(ledger.get("items_total")),
        "proven": str(states.get("proven", 0)),
        "gap": str(states.get("gap", 0)),
        "unreviewed": str(states.get("unreviewed", 0)),
        "ac_1_10_cells": ",".join(ac_1_10) or "-",
        "ac_1_10_cell_count": str(len(ac_1_10)),
        "report_tail": output.strip().splitlines()[-1][:140] if output.strip() else "-",
        "missing_evidence_files": str(len(missing_evidence)),
        "missing_evidence_named": ",".join(missing_evidence[:3]) or "-",
        "proven_without_command": str(len(silent)),
    }


def judge_ac2_24(facts: Facts) -> Verdict:
    """AC2-24 判定：同一冻结树上先取 130 条迭代1 AC 读数，再逐条关闭原 16 个 gap，身份与历史保留."""
    checks = {
        "the ledger check is green": facts["ledger_check_exit"] == "0",
        "19 iteration-1 AC groups, ledger and doc identical": facts["group_heading_symdiff"] == "0",
        "130 iteration-1 ACs in the denominator": facts["ledger_items"]
        == facts["items_total_field"]
        == facts["group_counts_sum"]
        == "130",
        "every AC carries a current reading": facts["ledger_items"] == "130"
        and facts["items_total_field"] == "130",
        "no AC left unreviewed": facts["unreviewed"] == "0",
        "every reading names archived evidence": facts["missing_evidence_files"] == "0",
        "every proven AC records the command that proved it": facts["proven_without_command"]
        == "0",
        "all 16 gaps closed": facts["gap"] == "0",
        "the AC-1|10 conflict stayed inside its own cells": int(facts["ac_1_10_cell_count"]) >= 1
        and int(facts["gap"]) <= 16,
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(HOLDS, tuple(readings), "iteration-1 readings and gap closures are current")


# --------------------------------------------------------------------------------------
# AC2-25 发布候选同源冻结
# --------------------------------------------------------------------------------------
EVIDENCE_PLANE: Final = ("docs/evidence/", "docs/quality/")


def non_evidence_diff_count(sha: str) -> int:
    """Paths differing between ``sha`` and HEAD outside the evidence plane; -1 if not resolvable.

    ``同树`` cannot mean "the record names this exact commit": a record is committed *by* the
    commit it would have to name, so that reading is unsatisfiable on any tree. It means the
    commit the record names is reachable and the two trees agree on everything that is not
    evidence, so the code under measurement is the code that was run.
    """
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return -1
    exit_code, out = tool(["git", "diff", "--name-only", f"{sha}..HEAD"])
    if exit_code != 0:
        return -1
    return sum(1 for line in out.splitlines() if line and not line.startswith(EVIDENCE_PLANE))


def measure_ac2_25() -> Facts:
    """AC2-25 测量面：所有适用子检查真实exit=0，证据同树 / 只报gate前半绿."""
    identity = document_identity()
    head = str(identity["head"])
    makefile = read("Makefile")
    declared = re.findall(r"^\s*@\$\(MAKE\) --no-print-directory ([a-z0-9-]+)", makefile, re.M)
    records: list[dict[str, Any]] = []
    for path in sorted(REPO_ROOT.glob(GATE_RECORD_GLOB)):
        text = path.read_text(encoding="utf-8", errors="replace")
        exit_match = re.search(r"GATE_EXIT=(\S+)", text)
        if not exit_match:
            continue
        sha_match = re.search(r"HEAD=([0-9a-f]{7,40})\b", text)
        sha = sha_match.group(1) if sha_match else ""
        rel = str(path.relative_to(REPO_ROOT))
        records.append(
            {
                "round": int(path.parent.name[1:]) if path.parent.name[1:].isdigit() else -1,
                "exit": exit_match.group(1),
                "path": rel,
                "sections": len(re.findall(r"===== gate: ", text)),
                "sha": sha,
                "non_evidence_diff": non_evidence_diff_count(sha),
                "tracked": 1 if tool(["git", "ls-files", "--error-unmatch", rel])[0] == 0 else 0,
            }
        )
    same_tree = sorted(
        (r for r in records if r["non_evidence_diff"] == 0),
        key=lambda r: r["round"],
        reverse=True,
    )
    latest_any = max(records, key=lambda r: r["round"], default=None)
    chosen = same_tree[0] if same_tree else None
    return {
        "head": head,
        "dirty_files": str(identity["dirty_files"]),
        "gate_records_total": str(sum(1 for r in records if r["exit"] == "0")),
        "gate_records_same_tree": str(len(same_tree)),
        "gate_record_path": chosen["path"] if chosen else "absent",
        "gate_record_head": chosen["sha"] if chosen else "absent",
        "gate_exit_line": chosen["exit"] if chosen else "absent",
        "record_tracked": str(chosen["tracked"] if chosen else 0),
        "record_non_evidence_diff": str(chosen["non_evidence_diff"] if chosen else -1),
        "latest_record_any_tree": latest_any["path"] if latest_any else "absent",
        "gate_member_runs": str(chosen["sections"] if chosen else 0),
        "gate_members_declared": str(len(set(declared))),
        "inventory_status": str(json.dumps(inventory_report()["status"])),
        "evidence_case_dirs": str(
            len([path for path in (REPO_ROOT / "docs/evidence").iterdir() if path.is_dir()])
        ),
    }


def judge_ac2_25(facts: Facts) -> Verdict:
    """AC2-25 判定：发布候选同源冻结：清单、适用 AC、独立回归、整 gate 与文档/包/镜像同树."""
    checks = {
        "the tree is clean": facts["dirty_files"] == "0",
        "a green gate run record is archived": int(facts["gate_records_total"]) >= 1,
        "a record sits on this tree's code plane": int(facts["gate_records_same_tree"]) >= 1,
        "the record is committed, not a scratch file": facts["record_tracked"] == "1",
        "the record's tree differs from HEAD only in evidence": facts["record_non_evidence_diff"]
        == "0",
        "GATE_EXIT is recorded and zero": facts["gate_exit_line"] == "0",
        "the gate target declares 17 members": facts["gate_members_declared"] == "17",
        "every declared member ran in the record": facts["gate_member_runs"]
        == facts["gate_members_declared"],
        "the inventory ships in the same state": facts["inventory_status"] == '"PASS"',
    }
    failed = [name for name, ok in checks.items() if not ok]
    readings = [f"{key}={value}" for key, value in sorted(facts.items())]
    if failed:
        return Verdict(GAP, tuple(readings), "failed: " + "; ".join(failed))
    return Verdict(
        HOLDS, tuple(readings), "the release candidate freezes on one recorded green tree"
    )


# --------------------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------------------
def probe(
    case: str,
    anchors: tuple[str, ...],
    summary: str,
    measure: Callable[[], Facts],
    judge: Callable[[Facts], Verdict],
    closure: Mapping[str, str],
    breaks: tuple[Break, ...],
    requires: tuple[tuple[str, str], ...] = (),
    repair: str = "",
) -> Probe:
    """Build one case's probe record: anchors, faces, closure and breaks."""
    return Probe(case, anchors, summary, measure, judge, closure, breaks, requires, repair)


PROBES: Final = (
    probe(
        "AC2-01",
        ("32目录", "350唯一provider×model", "202名称", "EIA两键"),
        "上游 AST 在固定 commit 独立重算，与正式清单和 350 行台账逐行一致。",
        measure_ac2_01,
        judge_ac2_01,
        {},
        (
            Break("少扫一个模型行", {"models": "349", "expected_models": "349"}),
            Break("出现重复身份", {"duplicates": "1", "unique_identities": "349"}),
            Break("EIA 少一键", {"eia_models": "1"}),
            Break("清单自校而非上游重算", {"authority_source": "cache json"}),
            Break("上游实现被执行", {"executed_upstream": "RUN"}),
            Break("少扫一个来源目录", {"providers": "31", "expected_providers": "31"}),
        ),
        repair="重跑 provider_model_inventory 并逐行 diff；任何计数变化都要有上游事实。",
    ),
    probe(
        "AC2-02",
        ("34描述符", "单一运行时注册真相", "手填第二FETCHERS"),
        "34 个描述符、活 registry 与派生 map/库存双向一致，无第二注册表。",
        measure_ac2_02,
        judge_ac2_02,
        {
            "map_missing_sources": "0",
            "missing_legs_named": "-",
            "duplicate_openbb_models": "0",
            "placeholder_openbb_models": "0",
        },
        (
            Break("描述符少一个", {"descriptors": "33", "descriptor_sources_unique": "33"}),
            Break(
                "派生 map 引用幽灵 provider",
                {"map_ghost_providers": "1", "ghost_legs_named": "ghost"},
            ),
            Break("注册源未进 map", {"map_missing_sources": "1", "missing_legs_named": "sec"}),
            Break("手填第二注册表", {"second_registry_sites": "1"}),
            Break("库存能力计数偏离活 registry", {"inventory_capabilities": "45"}),
            Break("map 里重复同一个 openbb 模型", {"duplicate_openbb_models": "1"}),
            Break("map 用 TBD 占位顶替模型身份", {"placeholder_openbb_models": "13"}),
            Break("同一 fetcher 类注册两次", {"reused_fetcher_classes": "1"}),
        ),
        repair=(
            "注册与投影全部由描述符派生；派生物只能由工具重写，不能手填。"
            "当前实测缺口：bls/cboe/fmp 三个已注册源未进 openbb_map，"
            "且 17 条映射里 13 条的 openbb 模型名是 TBD 占位。"
        ),
    ),
    probe(
        "AC2-03",
        ("11个原文件全部映射", "现有11条能力"),
        "THS 11 个原文件的移动映射、11 条能力、三处 YAML 与错误/限流表全部保持。",
        measure_ac2_03,
        judge_ac2_03,
        {},
        (
            Break("移动映射缺一页", {"move_map_entries": "10"}),
            Break("能力少一条", {"ths_capabilities": "10"}),
            Break("YAML 缺失", {"yaml_present": "2"}),
            Break("错误表读取器丢失", {"error_symbols": "0"}),
            Break("限流不再触发", {"rate_limit_sites": "0"}),
        ),
        requires=(
            (
                REACH_ARCHIVE,
                "原错误/限流行为的逐条等值需要把 tests/fixtures/upstream 的录制信封逐条重放比对",
            ),
        ),
        repair="逐条重放 fuyao_t1_envelopes 并对比原错误码/限流分支。",
    ),
    probe(
        "AC2-04",
        ("327清单及2本体", "166文件/532旧导入", "人工差异可追溯", "幂等"),
        "327 文件清单、锁与快照双向一致、逐节点改写与人工差异标记、重放幂等。",
        measure_ac2_04,
        judge_ac2_04,
        {"signature_differing_unregistered": "0"},
        (
            Break("锁记录少一条", {"lock_records": "326"}),
            Break("清单与锁路径集不同", {"path_sets_equal": "False"}),
            Break("改了行数的 vendored 文件未标人工差异", {"line_changed_unflagged": "1"}),
            Break("旧根导入残留", {"retired_root_import_nodes": "3"}),
            Break("控制文件被算进 327 本体", {"control_file_rows_in_lock": "2"}),
            Break("把新的 0 覆盖旧 532 分母", {"census_statements": "0", "census_row_sum": "0"}),
            Break("分母只能靠总 count，逐行重算对不上", {"census_row_sum": "531"}),
            Break("登记过的改写文件在档案里没有行", {"archive_rows_missing_for_census": "3"}),
            Break("登记的改写没有 pristine→ported pin 变化", {"archive_census_pin_gap_rows": "1"}),
            Break("登记的改写没有一条存档重放通过", {"archive_census_replay_gap_rows": "1"}),
            Break("档案里有改写计数落在登记之外", {"archive_rewrite_rows_outside_census": "5"}),
            Break("档案里再没有一条逐节点改写计数", {"archive_rows_carrying_rewrite_counts": "0"}),
            Break("有 pin 变化落在登记之外", {"stale_pin_outside_census": "5"}),
            Break("签名 AST 对照没比满 325 条锁内 .py", {"signature_rows_compared": "324"}),
            Break("签名对照的检出不在 locked commit", {"signature_checkout_ok": "False"}),
            Break(
                "函数名/签名变了但 lock 没登记人工差异",
                {"signature_differing_unregistered": "1"},
            ),
            Break("签名对照工具自身没跑完（rc≠0）", {"signature_fidelity_exit": "2"}),
        ),
        requires=(
            (
                REACH_HUMAN,
                "逐路径 def/class 签名对照已在 325 条锁内 .py 上跑通（唯一不等的一条已具名）："
                "搬运根 facade 由 vendor_init_facade 重新生成、比 pristine 多出 "
                "__getattr__/__dir__ 两个声明，而 upstream.lock 里这条记录的 manual_edits 仍是 "
                "false，于是这一处真实存在的签名差异没有被登记成人工差异。闭环要改的是供应商 "
                "pin 文件的元数据（本轮判为需用户确认的动作），不是再补一个对照面",
            ),
        ),
        repair=(
            "档案已把 327 条的 pristine/ported 双 SHA、逐节点改写计数与重放结论逐路径落档，"
            "manifest 与磁盘同源，逐路径 def/class 签名对照也已跑通并只指出一处差异；"
            "把 __init__.py 这一条人工差异登记进 upstream.lock（manual_edits=true），"
            "本用例即闭环。"
        ),
    ),
    probe(
        "AC2-05",
        ("MIT头与全文保留", "不初始化settings/DB/scheduler"),
        "MIT 头与全文保留；导入 provider 元数据不拉起 settings/DB/重依赖，并带反向对照臂。",
        measure_ac2_05,
        judge_ac2_05,
        {},
        (
            Break(
                "独立性审计失败",
                {"vendor_independence_exit": "1", "vendor_independence_valid": "False"},
            ),
            Break("MIT 全文缺失", {"license_file_present": "False"}),
            Break("部分文件丢了许可头", {"mit_header_files": "324"}),
            Break("元数据导入拉起 sqlalchemy", {"heavy_after_metadata_import": '["sqlalchemy"]'}),
            Break("对照臂也干净（探针失效）", {"heavy_after_app_import": "[]"}),
        ),
        repair="把副作用移出包导入路径；保留 app 导入臂作为探针有效性对照。",
    ),
    probe(
        "AC2-06",
        ("calendar.json、ths.js", "manifest.json、upstream.lock和MIT全文LICENSE-AKSHARE"),
        "七项发行资源在盘上并被 package-data 声明；wheel/sdist 待干净环境验证。",
        measure_ac2_06,
        judge_ac2_06,
        {"built_artifacts": "2"},
        (
            Break("资源缺一个", {"resource_files_on_disk": "6"}),
            Break(
                "声明漏掉 ths.js", {"covered_by_package_data": "6", "uncovered_paths": RESOURCES[1]}
            ),
            Break("BSL 全文误入 MIT 树", {"bsl_text_in_mit_tree": "True"}),
            Break("没有任何资源断言测试", {"resource_asserting_tests": "0"}),
        ),
        requires=((REACH_INSTALL, "wheel+sdist 干净环境安装、pip check 与最小入口执行未做"),),
        repair="构建 wheel/sdist，在干净环境安装后读取七项资源并跑最小入口。",
    ),
    probe(
        "AC2-07",
        (
            "334.py逐文件归类不重复",
            "_vendor显式omit且ported仍受检",
            "以1071解释复算",
            "ENV_BLOCKED",
        ),
        "搬迁前后零依赖/coverage/安全/质量债务身份一致；版本漂移显式记为 ENV_BLOCKED。",
        measure_ac2_07,
        judge_ac2_07,
        {
            "preflight_status": "OK",
            "preflight_exit": "0",
            "preflight_issues": "0",
            "ported_security_issue_count": "0",
            "ported_security_detail": "-",
            "ported_security_declares_identity": "True",
            "ported_security_issue_codes": "-",
        },
        (
            Break("走到的文件数与基线不等", {"census_identity_holds": "False"}),
            Break("隐式把 _vendor 纳入 coverage 分母", {"coverage_omits_vendor": "False"}),
            Break("direct_http 计数被归零", {"direct_http_ported_pin": "0"}),
            Break("安全档案不再有效", {"ported_security_issue_count": "1"}),
            Break("基线口径被缩小", {"baseline_scope": "opendata,opendata_client"}),
            Break("某个声明范围一格文件都没扫到", {"empty_scope_buckets": "1"}),
            Break(
                "档案不写明它扫的是哪棵树、处置从哪一轮结转",
                {"ported_security_declares_identity": "False"},
            ),
            Break(
                "结转的处置没有逐条对上本轮扫描",
                {"ported_security_identity_rows": "0"},
            ),
        ),
        requires=((REACH_ARCHIVE, "coverage 新旧文件清单逐文件对照需要 test-cov 成员（约 400s）"),),
        repair=(
            "本轮已把 bandit 重扫到 docs/evidence/C74（ported_security_evidence_build，"
            "档案带 24h 窗口，下一轮验收要重跑同一个工具而不是沿用这份）；"
            "剩下的是在冻结干净树上跑 test-cov 成员，再逐文件对照 coverage 清单。"
        ),
    ),
    probe(
        "AC2-08",
        (
            "族级卡+350条provider差异记录",
            "requester=cloudQuant、具体scenario已确认",
            "限制体现在操作门禁",
        ),
        "族级卡与 350 条来源差异记录逐条落地，权利状态驱动操作门禁。",
        measure_ac2_08,
        judge_ac2_08,
        {
            "provider_delta_filled": "350",
            "scenario_filled": "350",
            "family_status_assessed": "350",
            "rights_registered_rows": "350",
            "family_card_doc_files": "1",
        },
        (
            Break("整族照抄（差异记录不足）", {"provider_delta_filled": "12"}),
            Break("scenario 留空", {"scenario_filled": "0"}),
            Break("门禁只剩两态", {"gate_decision_states": "2"}),
            Break("门禁不再限制 host", {"gate_host_scope": "False"}),
            Break("一个 verified 域勾完整个模型族", {"rights_registered_rows": "120"}),
        ),
        requires=(
            (REACH_LIVE, "逐产品条款/允许用途需要重新读取来源官方条款页"),
            (REACH_HUMAN, "三项结论与登记状态需要具名人工复核"),
        ),
        repair="逐族填差异记录与 scenario，并把已复核权利映射到门禁。",
    ),
    probe(
        "AC2-09",
        ("稳定可诊断状态", "缺凭据不发请求", "必需/可选逐模型判定"),
        "缺 Key/缺 SDK/额度与 host 限制给出可诊断状态，缺凭据不发请求，必需性逐模型判定。",
        measure_ac2_09,
        judge_ac2_09,
        {"credential_assessed_rows": "350", "verified_sources": "5"},
        (
            Break("健康检查开始触网", {"health_stays_local": "False"}),
            Break("自动路由不再跳过缺凭据源", {"auto_route_skip": "0"}),
            Break("缺凭据仍构造客户端", {"guard_before_client": "0"}),
            Break("必需性全未判定", {"credential_assessed_rows": "0"}),
            Break("可诊断状态退化", {"health_states": "2"}),
        ),
        requires=(
            (REACH_LIVE, "额度/host 真实限制与「其他 provider 仍可用」需要真实 key 与真实来源"),
        ),
        repair="逐模型判定必需/可选写入台账，并用录制响应验证缺凭据分支。",
    ),
    probe(
        "AC2-10",
        ("分片集合覆盖适用350身份", "e2e标记及精确OPENDATA_ALLOW_LIVE_E2E闸门", "空壳/固定样本"),
        "350 个模型实例各有实现、离线正反例、标准契约与服务入口，真实对照另记。",
        measure_ac2_10,
        judge_ac2_10,
        {
            "dev_done": "350",
            "dev_done_label": "350",
            "label_without_witness": "0",
            "label_without_witness_ids": "-",
            "engine_declared_rows": "350",
            "not_run": "0",
            "in_progress": "0",
            "live_verification_run": "350",
        },
        (
            Break("空壳也算 DEV_DONE", {"dev_done": "349", "not_run": "1"}),
            Break("DEV_DONE 只是标签：一条标签无见证", {"label_without_witness": "1"}),
            Break(
                "整片标签自报而执行见证为零",
                {"dev_done": "0", "dev_done_label": "350", "label_without_witness": "350"},
            ),
            Break(
                "契约套件的 ModelSpec 参数化消失",
                {"contract_test_files": "-", "contract_test_functions": "0"},
            ),
            Break("live 用例脱离闸门", {"gate_live_confirmation": "False"}),
            Break("身份缺 OBB2 task id", {"obb2_task_ids": "349"}),
        ),
        requires=((REACH_LIVE, "真实上游对照需要授权后的 e2e 腿"),),
        repair="按切片把 NOT_RUN/IN_PROGRESS 推进到 DEV_DONE，并为每片留离线正反例。",
    ),
    probe(
        "AC2-11",
        ("既有5源85条固定模型及原12条能力", "5个provider存在或全部局部verified不能当85/85"),
        "5 个既有来源的 85 条模型逐条完成 AC2-10，且原有 12 条能力语义无回退。",
        measure_ac2_11,
        judge_ac2_11,
        {
            "p0_dev_done": "85",
            "p0_dev_done_label": "85",
            "p0_label_without_witness": "0",
            "p0_live_verified": "85",
        },
        (
            Break("85 条里有未完成", {"p0_dev_done": "80"}),
            Break("P0 标签自报而无执行见证", {"p0_label_without_witness": "5"}),
            Break("原 12 条能力缩减", {"p0_localized_verified_caps": "11"}),
            Break("库存的 85 条与任务台账对不上", {"p0_inventory_fetchers": "80"}),
            Break("只靠 provider 目录存在充数", {"p0_registry_caps": "0"}),
            Break("来源数不足 5", {"p0_providers": "4"}),
        ),
        requires=(
            (REACH_ARCHIVE, "原 12 条能力的逐条身份未在任何档案登记，无法离线逐条比对"),
            (REACH_LIVE, "85 条的 SOURCE_VERIFIED 需要真实对照"),
        ),
        repair="把 5 源原能力身份登记进库存文档，再逐条推进 85 行。",
    ),
    probe(
        "AC2-12",
        ("7个其他宏观33条固定模型", "EIA0模型误记"),
        "7 个宏观来源的 33 条模型逐条完成 AC2-10 与适用真实对照，EIA 模型数按上游登记。",
        measure_ac2_12,
        judge_ac2_12,
        {
            "macro_dev_done": "33",
            "macro_dev_done_label": "33",
            "macro_label_without_witness": "0",
            "macro_live_verified": "33",
        },
        (
            Break("33 条少一条", {"macro_rows": "32"}),
            Break("宏观标签自报而无执行见证", {"macro_label_without_witness": "3"}),
            Break("EIA 记成 0 模型", {"eia_ledger_rows": "0"}),
            Break("序列单位/频率混历史值（未对照）", {"macro_live_verified": "0"}),
            Break("宏观来源不足 7", {"macro_providers": "6"}),
        ),
        requires=((REACH_LIVE, "宏观序列的真实对照需要授权 e2e 腿"),),
        repair="逐条完成 33 行，并按上游事实校正 EIA 模型数。",
    ),
    probe(
        "AC2-13",
        ("5商业/聚合120条模型", "15源/195凭据暴露行", "未声明Key不等于无需授权"),
        "120 条商业/聚合模型逐条完成，跨阶段 15 源 195 行凭据暴露都有访问决定。",
        measure_ac2_13,
        judge_ac2_13,
        {
            "commercial_dev_done": "120",
            "commercial_dev_done_label": "120",
            "commercial_label_without_witness": "0",
            "decisions_pending": "0",
            "access_assessment_pending": "0",
            "exposed_rows_decided": "195",
            "decisions_made": "195",
        },
        (
            Break("120 条里有未完成", {"commercial_dev_done": "100"}),
            Break("商业标签自报而无执行见证", {"commercial_label_without_witness": "20"}),
            Break("暴露行未被覆盖", {"exposed_rows": "155", "exposed_providers": "12"}),
            Break("未声明 Key 被当成无需授权", {"exposed_rows_decided": "0"}),
            Break(
                "不申请也算整体 PASS",
                {
                    "decisions_pending": "0",
                    "exposed_rows_decided": "195",
                    "commercial_dev_done": "120",
                    "decisions_made": "0",
                    "access_assessment_pending": "155",
                },
            ),
        ),
        requires=(
            (REACH_HUMAN, "申请/不申请的决定与配额档位需要负责人具名确认"),
            (REACH_LIVE, "当前官方档位/配额需要官方页面与真实调用"),
        ),
        repair="逐行写入访问决定，并把不申请的行显式留在台账内。",
    ),
    probe(
        "AC2-14",
        ("15个其他来源112条模型", "请求型Quote不扩成实时服务"),
        "15 个其他来源的 112 条模型按快照/报价/新闻/披露/衍生品各自契约完成。",
        measure_ac2_14,
        judge_ac2_14,
        {
            "other_dev_done": "112",
            "other_dev_done_label": "112",
            "other_label_without_witness": "0",
            "other_live_verified": "112",
        },
        (
            Break("112 条少一条", {"other_rows": "111"}),
            Break("其他来源标签自报而无执行见证", {"other_label_without_witness": "12"}),
            Break("分片不再覆盖 15 源", {"other_providers": "14"}),
            Break("阶段划分漏行", {"phase_partition_sums_to_350": "False"}),
            Break("文本/文档伪转数值域", {"other_live_verified": "0"}),
        ),
        requires=((REACH_LIVE, "112 条的真实对照与契约验证需要授权 e2e 腿"),),
        repair="逐条实现并按契约族分类验证，不把请求型报价扩成实时服务。",
    ),
    probe(
        "AC2-15",
        ("显式源不回退", "auto排除未验证/缺配置/用途不符", "逐源/全局/host预算最小上限有效"),
        "显式选择不回退，auto 排除未验证/缺配置/用途不符，导入零网络，三层预算有最小上限。",
        measure_ac2_15,
        judge_ac2_15,
        {"host_concurrency_limiter": "1", "import_time_fetch": "[]"},
        (
            Break(
                "auto 不再排除未验证", {"auto_condition_sites": "0", "fail_closed_messages": "0"}
            ),
            Break("导入时触发取数", {"import_time_fetch": '["sqlalchemy"]'}),
            Break("host 层无上限", {"host_concurrency_limiter": "0"}),
            Break("预算常量被删", {"source_budget_constant": "False"}),
            Break("全局速率缺失", {"global_rate_constant": "False"}),
        ),
        requires=((REACH_LIVE, "真实降级顺序、429 与未知额度 probe 行为需要真实来源"),),
        repair="补 host 级并发闸门，并用录制响应验证降级顺序。",
    ),
    probe(
        "AC2-16",
        ("全350行有适用性声明", "unsupported在I/O前诊断", "不要求350原生async"),
        "统一 async 包装：350 行逐行声明适用性，unsupported 在 I/O 前拒绝，非法模式被拒。",
        measure_ac2_16,
        judge_ac2_16,
        {"assessed": "350", "not_assessed": "0"},
        (
            Break("仍有行未声明", {"assessed": "349", "not_assessed": "1"}),
            Break("非法 async_mode 被接受", {"spec_arms": '[3, "accepted", "accepted"]'}),
            Break("引擎不再读声明", {"engine_read_sites": "0"}),
            Break("对照臂也失败（探针失效）", {"spec_arms": '["0", "TypeError", "TypeError"]'}),
            Break("I/O 前拒绝只剩注释，没有执行的用例", {"unsupported_refusal_tests": "0"}),
        ),
        requires=((REACH_WAREHOUSE, "取消不推进未完成水位需要真实执行与水位对照"),),
        repair="逐行判定 async_mode（bounded_thread/unsupported）并写入台账。",
    ),
    probe(
        "AC2-17",
        ("Fetcher→ODS/标准化/DWD或受控源查询→API/CLI/SDK", "只实现Fetcher无服务入口"),
        "模型族从 Fetcher 到 ODS/DWD 到 API/CLI/SDK 四层可达，目录显示真实覆盖。",
        measure_ac2_17,
        judge_ac2_17,
        {
            "cli_model_commands": "1",
            "layer_flags_in_descriptor": "1",
            "coverage_status_assessed": "350",
        },
        (
            Break(
                "只有 Fetcher 没有服务入口",
                {
                    "api_model_routes": "0",
                    "api_route_modules": "0",
                    "api_route_verbs": "-",
                    "api_routes_without_path": "0",
                },
            ),
            Break("CLI 层缺失", {"cli_model_commands": "0"}),
            Break("目录不再记录可达层", {"layer_flags_in_descriptor": "0"}),
            Break("任意 dict 逃避契约", {"coverage_status_assessed": "0"}),
            Break("DWD 层未接入", {"dwd_symbols": "0"}),
            Break("模型族只出不入库", {"api_route_verbs": "get:6"}),
            Break("路由挂到空路径", {"api_routes_without_path": "1"}),
        ),
        requires=((REACH_WAREHOUSE, "存储/查询模式的实际结果需要真实库"),),
        repair="补 provider-model CLI 入口与描述符的分层可达声明。",
    ),
    probe(
        "AC2-18",
        ("公司行动重复/冲突及修订传播", "稳定业务键保留独立事件", "无gold保持未完成"),
        "公司行动的稳定业务键、冲突可审计与因子/DWD/WS 修订传播，并有 gold 样本回填。",
        measure_ac2_18,
        judge_ac2_18,
        {"business_key_sites": "3", "conflict_fixtures": "1"},
        (
            Break("业务键退化成行序 ID", {"business_key_sites": "0"}),
            Break("冲突不再可审计", {"conflict_symbols": "0"}),
            Break("修订不传播", {"factor_symbols": "0"}),
            Break("无 gold 却声明完成", {"gold_dirs": "0"}),
        ),
        requires=((REACH_WAREHOUSE, "双源冲突行的实际合并与水位传播需要真实库写入"),),
        repair="命名稳定业务键，补双源冲突夹具与修订传播回归。",
    ),
    probe(
        "AC2-19",
        ("PIT两次发布/重述样本", "公开前不可见", "后来重述不改旧as_of结果"),
        "两次发布/重述样本下，公开前不可见、公开后见当时版本、旧 as_of 结果不被重述改写。",
        measure_ac2_19,
        judge_ac2_19,
        {"read_path_as_of_sites": "3", "sdk_as_of_sites": "2", "restatement_fixtures": "2"},
        (
            Break("读取路径不做时间切片", {"read_path_as_of_sites": "0"}),
            Break("SDK 不暴露 as_of", {"sdk_as_of_sites": "0"}),
            Break("fetched_at 被当公告日", {"pit_trace_columns": "0"}),
            Break("无重述样本", {"restatement_fixtures": "0"}),
            Break("服务层无 as_of 参数", {"api_as_of_parameter": "False"}),
        ),
        requires=((REACH_WAREHOUSE, "两次发布/重述的真实行需要授权库写入（e2e 腿）"),),
        repair="在仓库读取路径与 SDK 上加 as_of 过滤，并补重述样本。",
    ),
    probe(
        "AC2-20",
        ("最小安装不漏loguru", "ws extra真实存在", "主应用环境的额外依赖不能掩盖SDK漏声明"),
        "SDK 独立安装：声明面与导入面一致，loguru 不漏，ws extra 真实存在。",
        measure_ac2_20,
        judge_ac2_20,
        {"built_sdk_artifact": "1", "sdk_pit_parameter": "True"},
        (
            Break("SDK 用了 loguru 却不声明", {"loguru_declared": "False"}),
            Break("ws extra 名不副实", {"ws_extra_declared": "False"}),
            Break(
                "root 的额外依赖掩盖 SDK 漏声明",
                {"ws_extra_declared": "False", "root_ws_extra_declared": "True"},
            ),
            Break("没有可安装的发行物", {"built_sdk_artifact": "0"}),
            Break("PIT 参数没进 SDK", {"sdk_pit_parameter": "False"}),
        ),
        requires=((REACH_INSTALL, "干净环境独立安装与 REST/ws 实际运行未执行"),),
        repair="构建并在干净环境安装 opendata-client[ws]，跑最小 REST 与 ws 入口。",
    ),
    probe(
        "AC2-21",
        ("ID语义与interface_id等实际schema一致", "不把422显示成下载成功"),
        "前端下载按钮到后端路由的任务 ID 语义一致，失败状态如实反馈。",
        measure_ac2_21,
        judge_ac2_21,
        {},
        (
            Break("前后端 ID 语义漂移", {"ids_match": "False", "be_id_sites": "0"}),
            Break("后端路由丢失", {"download_route": "False"}),
            Break(
                "422 被显示成成功", {"fe_422_case": "False", "fe_error_not_success_case": "False"}
            ),
            Break("把 202 之外的状态当成功", {"accepted_status_only": "False"}),
            Break("按钮不看选中的接口", {"view_uses_selected_interface": "False"}),
        ),
        requires=((REACH_LIVE, "真实下载任务状态流转需要前端 e2e 成员（gate 内）"),),
        repair="保持 interface_id 单一语义，并为每个失败状态留组件测试。",
    ),
    probe(
        "AC2-22",
        ("配额/重试有界、永久错误不重试", "关键风险处置有语义证据", "不放宽TLS掩盖不可达"),
        "请求/安全债务：超时、限流与重试有界，raw 响应可诊断，TLS/解析风险有语义证据。",
        measure_ac2_22,
        judge_ac2_22,
        {"ported_security_issue_count": "0", "ported_security_detail": "-"},
        (
            Break("超时未应用", {"engine_timeout_applied": "False"}),
            Break("为降计数删调用", {"direct_http_ported_pin": "0"}),
            Break("一方代码禁用 TLS", {"first_party_tls_disable": "2"}),
            Break("安全档案失效", {"ported_security_issue_count": "1"}),
            Break("vendored TLS 风险被隐藏", {"vendor_tls_disable": "0"}),
            Break(
                "重试不看可重试性，永久错误也重跑",
                {"retryability_gates": "0", "retry_gate_files": "-"},
            ),
            Break("重试没有次数上界", {"attempt_bounds": "0"}),
            Break("重试判定写在请求路径之外", {"retry_gate_files": "opendata/data/config.py"}),
        ),
        repair=(
            "档案面已由 scripts/quality/ported_security_evidence_build.py 重扫并逐条结转上一轮审阅"
            "（写进本波新目录，不覆盖 A2/C65）；下一轮验收重跑同一个工具，而不是放宽窗口。"
        ),
    ),
    probe(
        "AC2-23",
        ("P95/RSS/连接数/SQL计划有记录", "无测量不认定性能缺陷"),
        "同数据/索引/配置的负载前后比较，P95、RSS、连接数与 SQL 计划都有记录。",
        measure_ac2_23,
        judge_ac2_23,
        {
            "p95_recorded": "True",
            "connection_count_recorded": "True",
            "after_run_bundles": "1",
        },
        (
            Break("无测量却认定缺陷", {"bundle_rows": "0", "validator_exit": "1"}),
            Break("只记 RSS", {"p95_recorded": "False"}),
            Break("连接数缺失", {"connection_count_recorded": "False"}),
            Break("没有 after 运行", {"after_run_bundles": "0"}),
            Break("SQL 计划无脚本证据", {"explain_script_present": "False"}),
        ),
        requires=((REACH_WAREHOUSE, "前后同负载基准需要真实 MySQL 运行（未授权）"),),
        repair="在授权窗口跑 benchmark_ods_write 前后两版，并留 P95/连接数指标。",
    ),
    probe(
        "AC2-24",
        ("先取130原AC读数，再关闭原16 gap", "没读数不闭合", "不凭空加第17 gap"),
        "同一冻结树上先取 130 条迭代1 AC 读数，再逐条关闭原 16 个 gap，身份与历史保留。",
        measure_ac2_24,
        judge_ac2_24,
        {
            "gap": "0",
            "unreviewed": "0",
            "ledger_check_exit": "0",
            "missing_evidence_files": "0",
            "proven": "130",
        },
        (
            Break("第 17 个 gap 被凭空加入", {"gap": "17", "ac_1_10_cell_count": "1"}),
            Break("归档缺读数", {"ledger_items": "115"}),
            Break("台账校验不再有效", {"ledger_check_exit": "1"}),
            Break("读数被缩到 129 条", {"items_total_field": "129"}),
            Break("证据引用指向已经不存在的搬迁前路径", {"missing_evidence_files": "5"}),
            Break("proven 只留下勾选没有命令", {"proven_without_command": "7"}),
        ),
        requires=(
            (REACH_HUMAN, "4 个新增 provider 的权限复核与 AC-19 运维授权"),
            (
                REACH_ARCHIVE,
                "AC-1|05 需要隔离 docker 栈重跑；"
                "AC-16|07 需要干净解释器（当前 conda 树带 akshare/openbb）",
            ),
        ),
        repair="按分组重算 manifest/port-scope 后逐条重取证，并保留原身份与历史分列。",
    ),
    probe(
        "AC2-25",
        ("所有适用子检查真实exit=0，证据同树", "只报gate前半绿", "dirty证据当提交快照"),
        "发布候选同源冻结：清单、适用 AC、独立回归、整 gate 与文档/包/镜像同树。",
        measure_ac2_25,
        judge_ac2_25,
        {
            "dirty_files": "0",
            "gate_records_total": "3",
            "gate_records_same_tree": "1",
            "gate_record_path": "docs/evidence/C74/gate.txt",
            "gate_record_head": "0a1b2c3",
            "gate_exit_line": "0",
            "record_tracked": "1",
            "record_non_evidence_diff": "0",
            "gate_member_runs": "17",
            "gate_members_declared": "17",
            "inventory_status": '"PASS"',
        },
        (
            Break("dirty 证据当提交快照", {"dirty_files": "12"}),
            Break("只报 gate 前半绿", {"gate_exit_line": "1", "gate_member_runs": "8"}),
            Break("范围被缩小", {"gate_members_declared": "16", "gate_member_runs": "16"}),
            Break("清单与树不同源", {"inventory_status": '"FAIL"'}),
            Break("没有运行记录", {"gate_records_total": "0", "gate_records_same_tree": "0"}),
            Break(
                "旧一轮的绿色记录冒充本轮冻结树",
                {"gate_records_total": "3", "gate_records_same_tree": "0"},
            ),
            Break(
                "记录命名的树上还有代码面差异",
                {"record_non_evidence_diff": "4", "gate_records_same_tree": "0"},
            ),
            Break("记录只是工作区里的暂存文件", {"record_tracked": "0"}),
        ),
        requires=(
            (REACH_ARCHIVE, "整 gate 需要在冻结干净树上跑满 17 个成员（含 a2-check 约 1300s）"),
            (REACH_INSTALL, "wheel/镜像复现未执行"),
        ),
        repair="在冻结树上跑完整 make gate 并留 17 条成员退出码，再补包与镜像。",
    ),
)


def probes_by_case() -> dict[str, Probe]:
    """Index the probe table by case id."""
    return {item.case: item for item in PROBES}


# --------------------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------------------
@dataclass
class Result:
    """One AC2 case's full record: row, state, faces, reason and self-test."""

    case: str
    line: int
    summary: str
    state: str
    offline_state: str
    reason: str = ""
    missing_legs: list[tuple[str, str]] = field(default_factory=list)
    readings: list[str] = field(default_factory=list)
    repair: str = ""
    falsified: str = "-"


def no_row_result(item: Probe) -> Result:
    """Build the result for a probe whose document row is missing."""
    return Result(
        case=item.case,
        line=0,
        summary=item.summary,
        state=NO_FACE,
        offline_state=NO_FACE,
        reason="§2 has no parsable row for this case id",
        repair=item.repair,
    )


def evaluate(item: Probe, case: Case | None, with_self_test: bool) -> Result:
    """Measure, judge and self-test one probe into a result record."""
    if case is None:
        return no_row_result(item)
    missing = [anchor for anchor in item.anchors if anchor not in case.text]
    if missing:
        return Result(
            case=item.case,
            line=case.line,
            summary=item.summary,
            state=DRIFT,
            offline_state=DRIFT,
            reason="anchors no longer in the row: " + ", ".join(missing),
            repair=item.repair,
        )
    try:
        facts = item.measure()
    except Exception as error:
        return Result(
            case=item.case,
            line=case.line,
            summary=item.summary,
            state=NO_FACE,
            offline_state=NO_FACE,
            reason=f"measurement raised {type(error).__name__}: {error}",
            repair=item.repair,
        )
    repaired = Facts(facts)
    repaired.update(item.closure)
    verdict = item.judge(facts)
    offline = item.judge(repaired).state
    state = verdict.state
    if state == HOLDS and item.requires:
        state = BLOCKED
    result = Result(
        case=item.case,
        line=case.line,
        summary=item.summary,
        state=state,
        offline_state=offline,
        reason=verdict.reason,
        missing_legs=list(item.requires),
        readings=list(verdict.readings),
        repair=item.repair,
    )
    if with_self_test:
        result.falsified = self_test(item, facts)["summary"]
    return result


def self_test(item: Probe, facts: Facts | None) -> dict[str, Any]:
    """Closure must hold; every counter-example applied to it must break."""
    notes: list[str] = []
    if not item.breaks:
        notes.append("no counter-example: the judge cannot be falsified")
    if facts is None:
        return {
            "breaks": len(item.breaks),
            "flipped": 0,
            "closure": "unmeasured",
            "notes": notes,
            "summary": "unmeasured",
        }
    repaired = Facts(facts)
    repaired.update(item.closure)
    closure = item.judge(repaired).state
    if closure != HOLDS:
        notes.append(f"closure reading does not hold ({closure})")
    flipped = 0
    for break_item in item.breaks:
        unknown = [key for key in break_item.facts if key not in facts]
        if unknown:
            notes.append(f"{break_item.label}: unknown fact keys {unknown}")
            continue
        mutated = Facts(repaired)
        mutated.update(break_item.facts)
        state = item.judge(mutated).state
        if state == break_item.expect:
            flipped += 1
        else:
            notes.append(f"{break_item.label}: judge stayed {state}, expected {break_item.expect}")
    if flipped != len(item.breaks):
        notes.append(f"{len(item.breaks) - flipped} counter-example(s) did not bite")
    return {
        "breaks": len(item.breaks),
        "flipped": flipped,
        "closure": closure,
        "notes": notes,
        "summary": f"{flipped}/{len(item.breaks)}",
    }


def build_ledger(cases: list[Case], results: list[Result], identity: dict[str, object]) -> dict:
    """Assemble the JSON ledger document for the whole plane."""
    counts: dict[str, int] = {}
    for result in results:
        counts[result.state] = counts.get(result.state, 0) + 1
    by_id = {case.id: case for case in cases}
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "document": DOC_REL,
        "identity": identity,
        "denominator": CASE_COUNT,
        "counts": counts,
        "cases": [
            {
                "case": result.case,
                "doc_line": result.line,
                "criterion": by_id[result.case].criterion if result.case in by_id else "",
                "summary": result.summary,
                "state": result.state,
                "offline_state": result.offline_state,
                "reason": result.reason,
                "missing_legs": [list(leg) for leg in result.missing_legs],
                "readings": result.readings,
                "repair": result.repair,
                "counter_examples": result.falsified,
                "command": f"python {SELF_REL} --item {result.case}",
            }
            for result in results
        ],
    }


def run_all(with_self_test: bool) -> tuple[list[Case], list[str], list[Result]]:
    """Evaluate every probe and return rows, problems and results."""
    cases, malformed = parse_cases(read(DOC_REL))
    by_id = {case.id: case for case in cases}
    registered = probes_by_case()
    results: list[Result] = []
    for case_id in expected_ids():
        case = by_id.get(case_id)
        item = registered.get(case_id)
        if item is None:
            results.append(
                Result(
                    case=case_id,
                    line=case.line if case else 0,
                    summary="",
                    state=NO_FACE,
                    offline_state=NO_FACE,
                    reason="no probe face registered for this case",
                )
            )
            continue
        results.append(evaluate(item, case, with_self_test))
    return cases, malformed, results


def main(argv: list[str] | None = None) -> int:
    """CLI entry: --list, --self-test, --all, --json and --write-ledger."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list", action="store_true", help="print the 25 rows and which have probes"
    )
    parser.add_argument("--all", action="store_true", help="run every probe and print the table")
    parser.add_argument("--item", help="run one case and print its readings")
    parser.add_argument(
        "--self-test", action="store_true", help="prove every judge can be falsified"
    )
    parser.add_argument(
        "--gate-check",
        action="store_true",
        help="denominator plus falsifiability; exit 1 on breach",
    )
    parser.add_argument("--write-ledger", action="store_true", help=f"write {LEDGER_REL}")
    parser.add_argument("--json", action="store_true", help="emit the ledger as JSON")
    args = parser.parse_args(argv)

    if args.list:
        cases, malformed = parse_cases(read(DOC_REL))
        registered = probes_by_case()
        by_id = {case.id: case for case in cases}
        for case_id in expected_ids():
            case = by_id.get(case_id)
            mark = "probe" if case_id in registered else "NO_PROBE"
            print(f"{case_id} line={case.line if case else '-'} {mark}")
        print(f"rows={len(cases)}/{CASE_COUNT} probes={len(registered)} malformed={len(malformed)}")
        return 0 if len(cases) == CASE_COUNT and not malformed else 1

    if args.self_test or args.gate_check:
        failures = 0
        cases, malformed = parse_cases(read(DOC_REL))
        by_id = {case.id: case for case in cases}
        for item in PROBES:
            case = by_id.get(item.case)
            if case is None:
                print(f"{item.case} FAIL row missing from §2")
                failures += 1
                continue
            missing = [anchor for anchor in item.anchors if anchor not in case.text]
            if missing:
                print(f"{item.case} FAIL drift: {missing}")
                failures += 1
                continue
            try:
                facts = item.measure()
            except Exception as error:
                print(f"{item.case} FAIL measurement raised {type(error).__name__}: {error}")
                failures += 1
                continue
            report = self_test(item, facts)
            notes = report["notes"] or []
            if notes:
                failures += 1
            print(
                f"{item.case} {'OK' if not notes else 'FAIL'} "
                f"breaks={report['breaks']} flipped={report['flipped']} closure={report['closure']}"
                + "".join(f"\n    - {note}" for note in notes)
            )
        for case_id in expected_ids():
            if case_id not in probes_by_case():
                print(f"{case_id} FAIL no probe face")
                failures += 1
        for line in malformed:
            print(f"MALFORMED FAIL {line}")
            failures += 1
        print(f"SELF_TEST {'PASS' if failures == 0 else 'FAIL'} failures={failures}")
        return 0 if failures == 0 else 1

    want_item = args.item is not None
    if want_item:
        probe = probes_by_case().get(args.item)
        if probe is None:
            print(f"no probe registered for {args.item}")
            return 2
        cases, _ = parse_cases(read(DOC_REL))
        case = next((entry for entry in cases if entry.id == args.item), None)
        result = evaluate(probe, case, with_self_test=True)
        print(f"{result.case} state={result.state} offline={result.offline_state}")
        print(f"  summary: {result.summary}")
        print(f"  reason:  {result.reason}")
        for leg in result.missing_legs:
            print(f"  leg:     {leg[0]} -- {leg[1]}")
        for reading in result.readings:
            print(f"  {reading}")
        return 0

    if args.all or args.write_ledger:
        cases, malformed, results = run_all(with_self_test=args.write_ledger)
        identity = document_identity()
        ledger = build_ledger(cases, results, identity)
        for line in malformed:
            print("MALFORMED", line)
        for result in results:
            print(
                f"{result.case} line={result.line:<3} {result.state:<12} "
                f"offline={result.offline_state:<13} ce={result.falsified} {result.reason}"
            )
        counts: dict[str, int] = {}
        for result in results:
            counts[result.state] = counts.get(result.state, 0) + 1
        print("STATE_COUNTS", json.dumps(counts, ensure_ascii=False))
        print("IDENTITY", json.dumps(identity, ensure_ascii=False))
        if args.write_ledger:
            path = REPO_ROOT / LEDGER_REL
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"LEDGER_WRITTEN {LEDGER_REL} cases={len(results)}")
        if args.json:
            print(json.dumps(ledger, ensure_ascii=False, indent=2))
        return 0
    print(f"python {SELF_REL} --list | --all | --item | --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
