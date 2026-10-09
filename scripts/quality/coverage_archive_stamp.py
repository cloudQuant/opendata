"""Stamp the coverage report so AC-17|06 has an archived, git-tracked thing to be judged on.

Run immediately after ``make test-cov``. The report and the HTML tree are copied into
``docs/evidence/<round>/``, and the numbers written to the stamp are recomputed from the
*archive* rather than from the live file, so editing the archived report afterwards breaks a
recorded equality instead of passing quietly. Everything is read through the acceptance probe's
own functions, so the stamper and the judge cannot drift into two different measurements.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance_item_probe as probe  # same-directory stamper, run as a script

ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


def zip_html(root: Path, target: Path) -> int:
    """Archive ``htmlcov/`` deterministically: sorted names, fixed timestamps, deflate."""
    pages = sorted((root / "htmlcov").rglob("*.html"))
    if not pages:
        raise SystemExit("htmlcov/ holds no pages: run `make test-cov` first")
    entries = sorted((root / "htmlcov").rglob("*"))
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as book:
        for item in entries:
            if not item.is_file():
                continue
            info = zipfile.ZipInfo(str(item.relative_to(root)), date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            book.writestr(info, item.read_bytes())
    return len(pages)


def populations(c: probe.Context, rows: dict[str, Any], a2: list[str]) -> dict[str, dict[str, Any]]:
    """The four judged populations, each recomputed from the archived report."""
    _split, new_code = probe._cov_a2_split(c, rows, a2)
    fresh = probe._cov_ratio(new_code, rows)
    pops = {"new_code": {"files": fresh["files"], "combined_pct": f"{fresh['pct']:.2f}"}}
    for token in probe.COV_NAMED_ROOTS:
        keys, how, _unmapped, _absent = probe._cov_root_keys(token, rows)
        group = probe._cov_ratio(keys, rows)
        pops[token] = {
            "files": group["files"],
            "combined_pct": f"{group['pct']:.2f}",
            "resolved_by": how,
        }
    return pops


def narrative(stamp: dict[str, Any]) -> str:
    """Build the round's ``README.md`` out of the stamp itself, so the prose cannot rot.

    ``make gate``'s evidence-traceability member requires every ``docs/evidence/<round>/`` to hold
    a narrative, and a hand-typed one would state numbers that the next re-measurement refutes.
    """
    agg = stamp["aggregates"]
    lines = [
        f"# {stamp['round']} 覆盖率报告归档（AC-17|06 的判定对象）",
        "",
        f"- 生成命令：`{stamp['command']}`，随后由 `{stamp['generator']}` 归档",
        f"- 归档时间：{stamp['stamped_at']}",
        f"- 测量时的 HEAD：`{stamp['commit']}`",
        f"- `xml`：`{stamp['xml']}`（现盘 `{probe.COV_LIVE_REL}` 的逐字节副本）",
        f"- `html`：`{stamp['html_zip']}`，{stamp['html_pages']} 个页面",
        "",
        "## 戳记里记录的聚合数（由归档副本重算，不是现盘文件的重述）",
        "",
        f"- 语句：{agg['lines_covered']} / {agg['lines_valid']}",
        f"- 分支弧：{agg['branches_covered']} / {agg['branches_valid']}",
        f"- statement+branch 合并覆盖率：{agg['combined_pct']}%",
        "",
        "## 各判定总体",
        "",
        "| 总体 | 文件数 | 合并覆盖率 | 解析方式 |",
        "| --- | --- | --- | --- |",
    ]
    for name, entry in stamp["populations"].items():
        how = entry.get("resolved_by", "A2 新代码与 coverage source 的交集")
        lines.append(f"| `{name}` | {entry['files']} | {entry['combined_pct']}% | {how} |")
    lines += [
        "",
        "## 探针怎么用这份归档",
        "",
        f"- 判定的数字取自本目录的 `{Path(stamp['xml']).name}`，不是取自 gitignore 的现盘报告；"
        "改动其中任何一个计数会同时打断「逐行重算 vs coverage.py 自报」与"
        "「戳记 vs 归档现状」两组等式。",
        "- 现盘 `coverage.xml` 只作为可复现见证被读取：与归档的最大百分点差受 `drift_within` "
        "约束，不要求逐字节相等——第 12 员在第 11 员之后才重跑覆盖率。",
        "- 戳记与 HEAD 的关系由两条独立等式给出：`stamp_ancestor` 说 commit 在 HEAD 历史上，"
        "`tree_identity` 说被计量的源码根两边同一棵 git 树。",
        "- 退役目录 `opendata_fuyao` 不靠目录前缀命中：先由删除提交逐文件枚举，再过 "
        "`source_layout.py` 的历史身份映射落到存活路径，映射不上或落空者各自计数。",
        "",
        "## 重新生成",
        "",
        "```",
        "make test-cov",
        "python scripts/quality/coverage_archive_stamp.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Copy the report into the evidence round and record what the copy says."""
    c = probe.load_context()
    live = c.root / probe.COV_LIVE_REL
    if not live.is_file():
        raise SystemExit(f"{probe.COV_LIVE_REL} is missing: run `make test-cov` first")
    out_dir = c.root / probe.COV_STAMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_target = out_dir / Path(probe.COV_ARCHIVE_XML).name
    html_target = out_dir / Path(probe.COV_ARCHIVE_HTML).name
    shutil.copyfile(live, xml_target)
    pages = zip_html(c.root, html_target)

    # The stamp records the A2 new-code population, which is the group the judge re-derives.
    rows, agg = probe._cov_read(xml_target, c.root)
    identity = probe._cov_identity(rows, agg)
    if identity != probe.COV_XML_IDENTITY_FACES:
        raise SystemExit(f"archived report fails the recompute identity ({identity} face(s) agree)")
    _rc, sha = probe.run_argv(["git", "rev-parse", "HEAD"])
    commit = sha.strip()
    a2 = list(probe.script_module(probe.A2_CHECK_TOOL).resolve_files(None) or [])
    _split, new_keys = probe._cov_a2_split(c, rows, a2)
    whole = probe._cov_ratio(new_keys, rows)
    stamp = {
        "round": Path(probe.COV_STAMP_DIR).name,
        "commit": commit,
        "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "command": "make test-cov",
        "generator": "scripts/quality/coverage_archive_stamp.py",
        "xml": probe.COV_ARCHIVE_XML,
        "html_zip": probe.COV_ARCHIVE_HTML,
        "html_pages": pages,
        "aggregates": {
            "lines_valid": whole["stmts"],
            "lines_covered": whole["stmts"] - whole["miss"],
            "branches_valid": whole["arc_total"],
            "branches_covered": whole["arc_cov"],
            "combined_pct": f"{whole['pct']:.2f}",
        },
        "populations": populations(c, rows, a2),
    }
    Path(c.root / probe.COV_STAMP_REL).write_text(
        json.dumps(stamp, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "README.md").write_text(narrative(stamp), encoding="utf-8")
    print(
        f"stamped {xml_target.name} ({xml_target.stat().st_size} bytes) "
        f"+ {html_target.name} ({pages} pages) + README.md at {commit[:9]}"
    )
    for name, entry in stamp["populations"].items():
        print(f"  {name:16s} files={entry['files']:4d} combined={entry['combined_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
