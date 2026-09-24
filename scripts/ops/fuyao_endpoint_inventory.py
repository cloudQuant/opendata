"""fuyao 官方文档端点清单抽取器（B2 / AC-7 1B 映射表的机器可核对底稿）.

站点在根目录提供机器可读聚合 ``llms-full.txt``，本脚本从
``# REST API 参考`` 一节里抽出**全部**端点声明与文档分组页，产出
 ``docs/evidence/B2/fuyao-endpoint-inventory.txt``：

    SECTION  <slug>  <文档分组标题>
    ENDPOINT <slug>  <METHOD> <path>  <端点标题>

端点标题取声明行前最近一个标题（去掉 ``{#anchor}`` 后缀）；分组页取声明行前
最近一条 ``来源：…/docs/api-reference/<slug>/``。规划中（无路径）的分组页以
``SECTION … 0`` 记录，保证"分组 100% 覆盖"的口径可核对。

用法::

    curl -sL https://fuyao.aicubes.cn/llms-full.txt -o /tmp/fuyao-full.txt
    python scripts/ops/fuyao_endpoint_inventory.py --input /tmp/fuyao-full.txt \
        --out docs/evidence/B2/fuyao-endpoint-inventory.txt
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import date
from pathlib import Path

DOC_URL = "https://fuyao.aicubes.cn/llms-full.txt"
REST_START = "# REST API 参考"
REST_END = "# MCP 接入"
HEADING_RE = re.compile(r"^#{2,4}\s+(.*?)\s*$")
SOURCE_RE = re.compile(r"^来源：<(https?://[^>]+)>|^来源：(https?://\S+)")
ENDPOINT_RE = re.compile(r"^`?(GET|POST|PUT|DELETE)\s+(/[A-Za-z0-9/_.-]+)`?\s*$")
DUMP_ROW_RE = re.compile(r"`(GET\s+/dump/[^\s`]+)`")
SECTION_URL_RE = re.compile(r"/docs/api-reference/([a-z0-9-]+)/?$")


def _slug_for(url: str) -> str:
    match = SECTION_URL_RE.search(url)
    return match.group(1) if match else url.rsplit("/", 2)[-2]


def parse(lines: list[str]) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """抽取 REST 参考一节的分组页与端点声明.

    Returns:
        ``(sections, endpoints)``：sections 为 ``(slug, 分组页标题)``（文档顺序），
        endpoints 为 dict 列表（含 ``path/method/title/section``），按路径去重、
        保留首次出现。
    """
    starts = [i for i, ln in enumerate(lines) if ln.strip() == REST_START]
    if not starts:
        raise SystemExit(f"marker {REST_START!r} not found in the doc")
    begin = starts[0]
    ends = [i for i, ln in enumerate(lines[begin + 1 :], begin + 1) if ln.strip() == REST_END]
    end = ends[0] if ends else len(lines)

    section: str | None = None
    title: str = ""
    sections: list[tuple[str, str]] = []
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for idx in range(begin + 1, end):
        line = lines[idx].rstrip()
        heading = HEADING_RE.match(line)
        if heading:
            title = re.sub(r"\s*\{#[^}]*\}\s*$", "", heading.group(1)).strip()
            continue
        source = SOURCE_RE.match(line)
        if source:
            url = source.group(1) or source.group(2)
            section = _slug_for(url)
            if (section, title) not in sections:
                sections.append((section, title))
            continue
        match = ENDPOINT_RE.match(line)
        if match:
            method, path = match.group(1), match.group(2)
            if path not in seen:
                seen.add(path)
                endpoints.append(
                    {"path": path, "method": method, "title": title, "section": section or "?"}
                )
            continue
        for cell in DUMP_ROW_RE.findall(line):
            method, path = cell.split()
            if path not in seen:
                seen.add(path)
                endpoints.append(
                    {"path": path, "method": method, "title": title, "section": section or "?"}
                )
    return sections, endpoints


def render(
    doc_bytes: bytes, sections: list[tuple[str, str]], endpoints: list[dict[str, str]]
) -> str:
    """把抽取结果渲染成清单快照文本（含文档 sha256 头）."""
    lines = [
        "# fuyao REST API 端点清单（官方文档机器抽取快照）",
        f"# 来源：{DOC_URL}",
        f"# 抓取日期：{date.today().isoformat()}",
        f"# llms-full.txt sha256：{hashlib.sha256(doc_bytes).hexdigest()}",
        "# 抽取脚本：python scripts/ops/fuyao_endpoint_inventory.py --input <llms-full.txt>",
        f"# 分组页数：{len(sections)}  端点数：{len(endpoints)}",
        "",
    ]
    by_section: dict[str, list[dict[str, str]]] = {}
    for ep in endpoints:
        by_section.setdefault(ep["section"], []).append(ep)
    for slug, title in sections:
        if not slug:
            continue
        entries = by_section.get(slug, [])
        lines.append(f"SECTION\t{slug}\t{title}\t{len(entries)}")
        lines.extend(
            f"ENDPOINT\t{slug}\t{ep['method']}\t{ep['path']}\t{ep['title']}" for ep in entries
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """命令行入口：读取文档副本并写出清单快照."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True, help="本地 llms-full.txt 副本")
    parser.add_argument("--out", type=Path, help="输出文件；缺省打印到 stdout")
    args = parser.parse_args(argv)
    raw = args.input.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    sections, endpoints = parse(lines)
    text = render(raw, sections, endpoints)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(
            f"wrote {args.out}: {len(endpoints)} endpoints, {len(sections)} sections",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
