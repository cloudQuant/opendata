"""AC-7 迭代 1B：fuyao 端点映射表覆盖度与 fail-closed 校验.

真实底稿是 ``docs/evidence/B2/fuyao-endpoint-inventory.txt``（由
``scripts/ops/fuyao_endpoint_inventory.py`` 从官方 ``llms-full.txt`` 机械抽取）。
本文件不联网：断言"映射表 == 清单快照"即"覆盖官方文档全部端点/分组 100%"，
清单本身若与文档漂移，需重新抓取快照后由这些用例把差异暴露出来。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from opendata.data.domains import load_domains
from opendata_fuyao.endpoint_map import (
    ENDPOINT_MAP_PATH,
    FAMILIES,
    STATUSES,
    FuyaoEndpointMapError,
    implemented_paths_in_code,
    load_endpoint_map,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY = REPO_ROOT / "docs/evidence/B2/fuyao-endpoint-inventory.txt"
MAP_FILE = ENDPOINT_MAP_PATH
#: 一份真实但边缘的端点，用于"漏项即报错"的漂移探针。
MAP_PROBE_PATH = "/api/a-share/valuations/snapshot"

if TYPE_CHECKING:
    from collections.abc import Callable

#: 验收文档 AC-7 原文点名的分组族。
DOC_NAMED_FAMILIES = ("基础", "A股", "期货", "期权", "基金")


def _inventory() -> tuple[list[tuple[str, str, int]], dict[str, tuple[str, str, str]]]:
    """解析清单快照：``(分组页, {path: (section, method, title)})``."""
    sections: list[tuple[str, str, int]] = []
    endpoints: dict[str, tuple[str, str, str]] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        if line.startswith("SECTION\t"):
            _, slug, title, count = line.split("\t")
            sections.append((slug, title, int(count)))
        elif line.startswith("ENDPOINT\t"):
            _, slug, method, path, title = line.split("\t")
            endpoints[path] = (slug, method, title)
    return sections, endpoints


@pytest.fixture(scope="module")
def endpoint_map():
    return load_endpoint_map()


def test_inventory_snapshot_is_present() -> None:
    assert INVENTORY.exists(), "端点清单快照缺失：请重跑 scripts/ops/fuyao_endpoint_inventory.py"
    sections, endpoints = _inventory()
    assert len(sections) > 40 and len(endpoints) > 90


def test_every_doc_section_is_mapped(endpoint_map) -> None:
    sections, _ = _inventory()
    assert {s.slug for s in endpoint_map.sections} == {slug for slug, _t, _c in sections}


def test_every_doc_endpoint_is_mapped(endpoint_map) -> None:
    _, endpoints = _inventory()
    assert endpoint_map.paths() == frozenset(endpoints)


def test_titles_follow_the_doc(endpoint_map) -> None:
    _, endpoints = _inventory()
    mapped = {e.path: e.title for e in endpoint_map.entries if e.path}
    assert {p: t for p, (_s, _m, t) in endpoints.items()} == mapped


def test_section_endpoint_counts_match(endpoint_map) -> None:
    _, endpoints = _inventory()
    per_section: dict[str, int] = {}
    for slug, _m, _t in endpoints.values():
        per_section[slug] = per_section.get(slug, 0) + 1
    assert {s.slug: s.doc_endpoints for s in endpoint_map.sections} == {
        **{s.slug: 0 for s in endpoint_map.sections if s.doc_endpoints == 0},
        **per_section,
    }


def test_doc_named_families_are_covered(endpoint_map) -> None:
    families = {s.family for s in endpoint_map.sections}
    assert set(DOC_NAMED_FAMILIES) <= families <= set(FAMILIES)
    for family in DOC_NAMED_FAMILIES:
        assert endpoint_map.by_status("planned") or any(
            e.family == family for e in endpoint_map.entries if e.path
        ), family


def test_each_family_has_consuming_scenarios(endpoint_map) -> None:
    covered = {e.family for e in endpoint_map.entries if e.scenario.strip()}
    assert covered == {s.family for s in endpoint_map.sections}
    for entry in endpoint_map.entries:
        assert len(entry.scenario.strip()) >= 6, entry


def test_implemented_entries_match_the_shipped_constants(endpoint_map) -> None:
    mapped = {e.path for e in endpoint_map.by_status("implemented")}
    assert mapped == set(implemented_paths_in_code())
    assert len(mapped) == 8  # 5 个 REST 端点 + 3 个 market-dumps 下载端点


def test_domains_are_registered(endpoint_map) -> None:
    known = set(load_domains())
    used = {e.domain for e in endpoint_map.entries if e.domain}
    assert used <= known
    assert {"stock_daily", "stock_action", "stock_adjust", "instrument"} <= used


def test_status_vocabulary_is_exhaustive(endpoint_map) -> None:
    assert {e.status for e in endpoint_map.entries} == set(STATUSES)
    planned = endpoint_map.by_status("planned")
    assert planned and all(e.path is None for e in planned)


def test_coverage_totals_reconcile(endpoint_map) -> None:
    coverage = endpoint_map.coverage()
    _, endpoints = _inventory()
    assert sum(row["doc_endpoints"] for row in coverage.values()) == len(endpoints)
    assert sum(row["mapped"] for row in coverage.values()) == len(endpoint_map.entries)


def _mutate(tmp_path: Path, mutate: Callable[[dict], None]) -> Path:
    """复制映射表到 ``tmp_path`` 并按 ``mutate`` 改一处（漂移即须报错）."""
    payload = yaml.safe_load(MAP_FILE.read_text(encoding="utf-8"))
    mutate(payload)
    target = tmp_path / "endpoint_map.yaml"
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return target


def _first_real_entry(payload: dict) -> dict:
    return next(e for e in payload["entries"] if e.get("path"))


def test_duplicate_path_fails_closed(tmp_path: Path) -> None:
    def repeat(payload: dict) -> None:
        payload["entries"].append(dict(_first_real_entry(payload)))

    with pytest.raises(FuyaoEndpointMapError, match="duplicate path"):
        load_endpoint_map(str(_mutate(tmp_path, repeat)))


def test_missing_endpoint_fails_closed(tmp_path: Path) -> None:
    def drop(payload: dict) -> None:
        payload["entries"] = [e for e in payload["entries"] if e.get("path") != MAP_PROBE_PATH]

    with pytest.raises(FuyaoEndpointMapError, match="maps 0 endpoints"):
        load_endpoint_map(str(_mutate(tmp_path, drop)))


def test_unregistered_domain_fails_closed(tmp_path: Path) -> None:
    def bump(payload: dict) -> None:
        _first_real_entry(payload)["domain"] = "made_up_domain"

    with pytest.raises(FuyaoEndpointMapError, match="unregistered domain"):
        load_endpoint_map(str(_mutate(tmp_path, bump)))


def test_unknown_status_fails_closed(tmp_path: Path) -> None:
    def bump(payload: dict) -> None:
        _first_real_entry(payload)["status"] = "maybe"

    with pytest.raises(FuyaoEndpointMapError, match="must be known"):
        load_endpoint_map(str(_mutate(tmp_path, bump)))


def test_blank_scenario_fails_closed(tmp_path: Path) -> None:
    def wipe(payload: dict) -> None:
        _first_real_entry(payload)["scenario"] = "  "

    with pytest.raises(FuyaoEndpointMapError, match="needs a title and a consuming scenario"):
        load_endpoint_map(str(_mutate(tmp_path, wipe)))


def test_planned_entry_with_path_fails_closed(tmp_path: Path) -> None:
    def fake(payload: dict) -> None:
        planned = next(e for e in payload["entries"] if e["status"] == "planned")
        planned["path"] = "/api/not-yet/real"

    with pytest.raises(FuyaoEndpointMapError, match="must not carry a path"):
        load_endpoint_map(str(_mutate(tmp_path, fake)))
