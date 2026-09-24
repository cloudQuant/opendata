"""fuyao 端点映射表加载器（AC-7 迭代 1B，FR-6）.

映射表是**契约数据**：官方文档（``llms-full.txt`` 的「REST API 参考」）里的
每一条端点声明，加上本项目侧的分组、消费场景、接入状态与对应的中台数据域。
它不驱动任何调用路径，因此按 fail-closed 加载——表结构与文档漂移时必须报错，
而不是静默漏项（AC-7 的验收口径是"覆盖官方文档全部分组 100%"）。

清单底稿由 ``scripts/ops/fuyao_endpoint_inventory.py`` 机械抽取到
``docs/evidence/B2/fuyao-endpoint-inventory.txt``；单测按该快照比对本表的
分组页与端点集合，任一侧漂移即失败。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

#: 映射表文件（随包发布）。
ENDPOINT_MAP_PATH = Path(__file__).parent / "endpoint_map.yaml"

#: 接入状态词表（含义见 YAML 头注释）。
STATUSES = frozenset({"implemented", "available", "client_only", "planned"})

#: 官方文档分组族（AC-7 验收枚举：基础/A股/指数/基金/期货/期权/资讯/导出）。
FAMILIES = frozenset({"基础", "A股", "指数", "基金", "期货", "期权", "资讯", "导出"})


class FuyaoEndpointMapError(RuntimeError):
    """映射表结构非法时抛出（fail closed）."""


@dataclass(frozen=True)
class DocSection:
    """官方文档的一个分组页.

    Attributes:
        slug: 文档页 slug（``/docs/api-reference/<slug>/``）。
        title: 文档页标题。
        family: 所属分组族。
        doc_endpoints: 文档该页声明的端点条数（0 表示总览页或规划中页）。
    """

    slug: str
    title: str
    family: str
    doc_endpoints: int


@dataclass(frozen=True)
class EndpointEntry:
    """映射表的一条端点.

    Attributes:
        section: 所属文档分组页 slug。
        family: 所属分组族（与 section 一致，便于按族聚合）。
        method: HTTP 方法；``planned`` 条目为 ``None``。
        path: 端点路径（按文档原样，market-dumps 为 ``/dump/...``）；
            ``planned`` 条目为 ``None``。
        title: 文档中的端点名。
        status: ``implemented`` / ``available`` / ``client_only`` / ``planned``。
        domain: 本项目数据域名；无对应已注册域时为 ``None``。
        scenario: 消费场景（验收要求：非空）。
    """

    section: str
    family: str
    method: str | None
    path: str | None
    title: str
    status: str
    domain: str | None
    scenario: str


@dataclass(frozen=True)
class FuyaoEndpointMap:
    """加载后的映射表.

    Attributes:
        sections: 文档分组页（文档顺序）。
        entries: 端点条目（先 ``planned``，再按文档顺序的真实端点）。
    """

    sections: tuple[DocSection, ...]
    entries: tuple[EndpointEntry, ...]

    def paths(self) -> frozenset[str]:
        """真实端点路径集合（不含 ``planned``）."""
        return frozenset(e.path for e in self.entries if e.path)

    def by_status(self, status: str) -> tuple[EndpointEntry, ...]:
        """按接入状态过滤条目."""
        return tuple(e for e in self.entries if e.status == status)

    def coverage(self) -> dict[str, dict[str, int]]:
        """按分组族统计覆盖：文档端点数、映射条目数与各状态计数."""
        out: dict[str, dict[str, int]] = {}
        for family in sorted({s.family for s in self.sections}):
            rows = {
                "doc_sections": sum(1 for s in self.sections if s.family == family),
                "doc_endpoints": sum(s.doc_endpoints for s in self.sections if s.family == family),
                "mapped": sum(1 for e in self.entries if e.family == family),
            }
            for status in sorted(STATUSES):
                rows[status] = sum(
                    1 for e in self.entries if e.family == family and e.status == status
                )
            out[family] = rows
        return out


@lru_cache(maxsize=1)
def load_endpoint_map(path: str | None = None) -> FuyaoEndpointMap:
    """加载并校验映射表（fail closed）.

    Args:
        path: 测试用覆盖路径；缺省为随包文件。

    Returns:
        解析后的映射表。

    Raises:
        FuyaoEndpointMapError: 文件不可读、版本不符、字段缺失、状态/分组族
            非法、分组页不存在、路径重复，或 ``domain`` 指向未注册数据域。
    """
    source = Path(path) if path else ENDPOINT_MAP_PATH
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FuyaoEndpointMapError(f"fuyao endpoint map {source} is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise FuyaoEndpointMapError(f"fuyao endpoint map {source} must declare version 1")
    raw_sections = _rows(payload, "sections", source)
    raw_entries = _rows(payload, "entries", source)
    sections = tuple(_parse_section(row, source) for row in raw_sections)
    entries = tuple(_parse_entry(row, sections, source) for row in raw_entries)
    _require_unique_paths(entries, source)
    _require_section_counts(sections, entries, source)
    return FuyaoEndpointMap(sections=sections, entries=entries)


def doc_path_for(code_path: str) -> str:
    """把代码侧路径换算成文档口径路径.

    market-dumps 的下载端点在文档里写作 ``/dump/...``（浏览器登录态通道），
    代码走 API Key 变体 ``/api/dump/...``；映射表按文档原样登记。

    Args:
        code_path: 代码常量里的端点路径。

    Returns:
        映射表中应对应的路径写法。
    """
    return code_path.removeprefix("/api") if code_path.startswith("/api/dump/") else code_path


def implemented_paths_in_code() -> frozenset[str]:
    """从 ``endpoints.py`` / ``dumps.py`` 常量反推已落地端点（文档口径）."""
    from opendata_fuyao import dumps, endpoints

    rest = (
        endpoints.PRICES_ENDPOINT,
        endpoints.INDEX_PRICES_ENDPOINT,
        endpoints.FUTURES_PRICES_ENDPOINT,
        endpoints.OPTIONS_PRICES_ENDPOINT,
        endpoints.INDEX_CONSTITUENTS_ENDPOINT,
        endpoints.ADJUSTMENT_FACTORS_ENDPOINT,
        endpoints.INCOME_STATEMENTS_ENDPOINT,
        endpoints.BALANCE_SHEETS_ENDPOINT,
        endpoints.CASH_FLOW_STATEMENTS_ENDPOINT,
        endpoints.TICKERS_LIST_ENDPOINT,
        endpoints.TICKERS_SEARCH_ENDPOINT,
        endpoints.CALENDAR_ENDPOINT,
    )
    dumps_paths = tuple(spec.download_path for spec in dumps.DUMP_SPECS.values())
    return frozenset(doc_path_for(p) for p in (*rest, *dumps_paths))


def _rows(payload: Mapping[str, object], key: str, source: Path) -> list[object]:
    """取一个必需的列表字段（空即报错）."""
    raw = payload.get(key)
    if not isinstance(raw, list) or not raw:
        raise FuyaoEndpointMapError(f"fuyao endpoint map {source} needs a non-empty {key!r} list")
    return list(raw)


def _parse_section(row: object, source: Path) -> DocSection:
    """解析并校验一个分组页."""
    if not isinstance(row, Mapping):
        raise FuyaoEndpointMapError(f"section row {row!r} in {source} is not a mapping")
    slug = _text(row, "slug")
    family = _text(row, "family")
    if not slug or not family:
        raise FuyaoEndpointMapError(f"section row {row!r} in {source} has blank slug/family")
    if family not in FAMILIES:
        raise FuyaoEndpointMapError(
            f"section {slug!r} in {source} has unknown family {family!r}; "
            f"expected one of {sorted(FAMILIES)}"
        )
    count = row.get("doc_endpoints")
    if not isinstance(count, int) or count < 0:
        raise FuyaoEndpointMapError(f"section {slug!r} in {source} needs a non-negative count")
    return DocSection(slug=slug, title=_text(row, "title"), family=family, doc_endpoints=count)


def _parse_entry(row: object, sections: tuple[DocSection, ...], source: Path) -> EndpointEntry:
    """解析并校验一条端点条目."""
    if not isinstance(row, Mapping):
        raise FuyaoEndpointMapError(f"entry row {row!r} in {source} is not a mapping")
    section = _text(row, "section")
    family = _text(row, "family")
    status = _text(row, "status")
    title = _text(row, "title")
    scenario = _text(row, "scenario")
    known = {s.slug for s in sections}
    if section not in known or family not in FAMILIES or status not in STATUSES:
        raise FuyaoEndpointMapError(
            f"entry {row.get('path')!r} in {source}: section/family/status must be known "
            f"values (section={section!r}, family={family!r}, status={status!r})"
        )
    if not title or not scenario:
        raise FuyaoEndpointMapError(
            f"entry {row.get('path')!r} in {source} needs a title and a consuming scenario"
        )
    path = _text_or_none(row, "path")
    method = _text_or_none(row, "method")
    if status == "planned":
        if path or method:
            raise FuyaoEndpointMapError(
                f"planned entry {title!r} in {source} must not carry a path or method"
            )
    elif not path or not path.startswith("/") or not method:
        raise FuyaoEndpointMapError(f"{status} entry {title!r} in {source} needs METHOD /path")
    domain = _text_or_none(row, "domain")
    if domain:
        _require_registered_domain(domain, path or title, source)
    return EndpointEntry(
        section=section,
        family=family,
        method=method,
        path=path,
        title=title,
        status=status,
        domain=domain,
        scenario=scenario,
    )


def _require_unique_paths(entries: tuple[EndpointEntry, ...], source: Path) -> None:
    """路径不得重复登记."""
    seen: set[str] = set()
    for entry in entries:
        if entry.path and entry.path in seen:
            raise FuyaoEndpointMapError(f"duplicate path {entry.path!r} in {source}")
        if entry.path:
            seen.add(entry.path)


def _require_section_counts(
    sections: tuple[DocSection, ...],
    entries: tuple[EndpointEntry, ...],
    source: Path,
) -> None:
    """每个分组页的条目数必须与文档声明的端点数一致（0 即规划中/总览页）."""
    actual: dict[str, int] = {}
    for entry in entries:
        if entry.path:
            actual[entry.section] = actual.get(entry.section, 0) + 1
    for section in sections:
        got = actual.get(section.slug, 0)
        if got != section.doc_endpoints:
            raise FuyaoEndpointMapError(
                f"section {section.slug!r} in {source} maps {got} endpoints but the doc "
                f"declares {section.doc_endpoints}"
            )


def _require_registered_domain(domain: str, context: str, source: Path) -> None:
    """``domain`` 必须指向已注册数据域（不虚构域名）."""
    from opendata.data.domains import require_domain

    try:
        require_domain(domain)
    except LookupError as exc:
        raise FuyaoEndpointMapError(
            f"entry {context!r} in {source} names unregistered domain {domain!r}"
        ) from exc


def _text(row: Mapping[str, object], key: str) -> str:
    """取一个非空字符串字段（缺省为空串）."""
    raw = row.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _text_or_none(row: Mapping[str, object], key: str) -> str | None:
    """取一个可空字符串字段（``null``/缺省 → ``None``）."""
    raw = row.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


__all__ = [
    "ENDPOINT_MAP_PATH",
    "FAMILIES",
    "STATUSES",
    "DocSection",
    "EndpointEntry",
    "FuyaoEndpointMap",
    "FuyaoEndpointMapError",
    "doc_path_for",
    "implemented_paths_in_code",
    "load_endpoint_map",
]
