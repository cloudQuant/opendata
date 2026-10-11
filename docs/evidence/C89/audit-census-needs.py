"""C89 audit: re-measure census rows' needs_engine_capability against upstream bodies.

Read-only, offline, stdlib-only. For five census rows it reads the row from its
carrier JSON, locates the extract_data/aextract_data and transform_data bodies in
the upstream OpenBB file the row's ``evidence:`` cite points at, scans those body
lines with named predicates, maps each fired predicate onto a canonical
capability id from capability-roadmap.json (round C88), and prints a verdict.

Arm A (control) is the real run against the pinned upstream tree. Arm B is a
counterfact: the tmx source copied to /tmp with the ``transpose()`` call deleted,
proving the transpose predicate is live, not inert.

Usage: python3 docs/evidence/C89/audit-census-needs.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = Path(__file__).resolve().parents[3]
PLAN_DIR = REPO / "docs" / "迭代计划" / "迭代2-统一Provider架构与全量能力补齐"
ROADMAP = PLAN_DIR / "capability-roadmap.json"
# Root such that providers/oecd/openbb_oecd/models/unemployment.py resolves
# under it (verified this round; the instrument re-verifies at startup).
OPENBB_ROOT = Path("/Users/yunjinqi/Documents/new_projects/OpenBB/openbb_platform")

ROWS = [
    ("OBB2-cboe-EquitySearch", "census-yfinance-cboe-finviz.json"),
    ("OBB2-intrinio-CurrencyPairs", "census-intrinio-1of3.json"),
    ("OBB2-intrinio-EquitySearch", "census-intrinio-1of3.json"),
    ("OBB2-intrinio-HistoricalDividends", "census-intrinio-2of3.json"),
    ("OBB2-tmx-EtfInfo", "census-sec-tmx-fed-gov-finra.json"),
]

# Legacy census token -> canonical capability_registry id.
LEGACY_CANON = {
    "client_side_filter": "rows.filter",
    "csv_decoder": "decode.delimited",
    "field_rename_map": "columns.rename",
    "param_value_mapping": "param.value_mapping",
    "multi_request_fanout": "flow.fan_out",
    "per_item_fanout": "flow.fan_out",
    "static_no_http_source": "outside.no_request",
}

# (predicate, line regex, canonical id, optional min-occurrence count)
LINE_PREDICATES = [
    (
        "list_param_split",
        re.compile(r"""\.split\(\s*["'],["']\s*\)"""),
        "flow.fan_out",
    ),
    (
        "transpose_reshape",
        re.compile(r"\.transpose\(\)|\.pivot\(|\.melt\("),
        "shape.columnar",
    ),
    (
        "null_sentinel_rewrite",
        re.compile(r"""\.fillna\(|\.replace\(\s*["']N/A["']\s*,\s*None\s*\)"""),
        "columns.null_sentinel",
    ),
    (
        "fixed_column_selection",
        re.compile(r"\]\s*\[\s*[A-Z][A-Z0-9_]*\s*\]"),
        "columns.select",
    ),
    (
        "key_rename",
        re.compile(r"\.rename\(|by_alias=True"),
        "columns.rename",
    ),
    (
        "per_row_value_injection",
        re.compile(r"""\{\s*["'][A-Za-z0-9_]+["']\s*:\s*query\.\w+\s*,\s*\*\*"""),
        "columns.derive",
    ),
    (
        "param_string_transform",
        re.compile(r"""\b[a-z_]+\.replace\(\s*["'](?!N/A)[^"']+["']"""),
        "param.value_transform",
    ),
    (
        "client_side_row_filter",
        re.compile(
            r"""\.str\.contains\(|"""
            r"""\[[a-zA-Z_]+\[["'][^"']+["']\]\s*==|\bdf\[df\["""
        ),
        "rows.filter",
    ),
    (
        "pagination_loop",
        re.compile(r"\bwhile\b[^\n]*(?:page|offset)|next_page|page_token"),
        "request.body_pagination",
    ),
]

FETCH_CALL = re.compile(
    r"\b(get_data_one|get_data_many|fetch_csv|make_requests|amake_requests"
    r"|get_company_directory|get_all_etfs|requests\.get|session\.get)\s*\("
)


def body_regions(lines: list[str]) -> list[tuple[str, list[tuple[int, str]]]]:
    """Return (method_name, [(lineno, text)]) for extract/transform methods."""
    head = re.compile(r"^(\s*)(?:async\s+)?def\s+(a?extract_data|transform_data|extract_data)\b")
    found: list[tuple[str, list[tuple[int, str]]]] = []
    i = 0
    while i < len(lines):
        m = head.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        name = m.group(2)
        region: list[tuple[int, str]] = [(i + 1, lines[i])]
        j = i + 1
        while j < len(lines):
            text = lines[j]
            if text.strip():
                cur = len(text) - len(text.lstrip())
                if cur < indent or (
                    cur <= indent and re.match(r"\s*(?:@|(?:async\s+)?def\b)", text)
                ):
                    break
            region.append((j + 1, text))
            j += 1
        found.append((name, region))
        i = j
    return found


def scan_regions(
    regions: list[tuple[str, list[tuple[int, str]]]],
    registry_ids: dict[str, bool],
    out: Callable[[str], None],
) -> list[tuple[str, str, int, str]]:
    """Fire predicates over located bodies; return list of (id, evidence)."""
    fired: list[tuple[str, str, int, str]] = []
    lines_all = [(n, t) for _, reg in regions for n, t in reg]
    out("fired predicates (predicate -> canonical id, line that fired it):")
    for pred, rx, canon in LINE_PREDICATES:
        hits = [(n, t) for n, t in lines_all if rx.search(t)]
        if not hits:
            continue
        n, t = hits[0]
        gap = canon not in registry_ids
        if gap:
            out(f"  TAXONOMY GAP: {pred}  :{n}  {t.strip()}")
        else:
            present = registry_ids[canon]
            out(f"  {pred} -> {canon} (present={present}) fired at :{n}  {t.strip()}")
        for extra_n, extra_t in hits[1:]:
            out(f"      also :{extra_n}  {extra_t.strip()}")
        fired.append((pred, canon, n, t.strip()))
    fetch_sites = [(n, t) for n, t in lines_all if FETCH_CALL.search(t)]
    if len(fetch_sites) >= 2:
        canon = "flow.join"
        if canon not in registry_ids:
            out("  TAXONOMY GAP: multi_fetch_call_sites")
        else:
            out(
                f"  multi_fetch_call_sites -> {canon}"
                f" (present={registry_ids[canon]}) fired at"
                f" :{fetch_sites[1][0]}  {fetch_sites[1][1].strip()}"
            )
        fired.append(
            ("multi_fetch_call_sites", canon, fetch_sites[1][0], fetch_sites[1][1].strip())
        )
    return fired


def snake(model: str) -> str:
    """CamelCase upstream model name -> snake_case module file stem."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", model).lower()


def main() -> int:
    """Audit the five census rows and print the face plus counterfact arm."""
    out_lines: list[str] = []

    def out(text: str = "") -> None:
        out_lines.append(text)
        print(text)

    carriers = {}
    for tid, carrier in ROWS:
        path = PLAN_DIR / carrier
        if not path.is_file():
            out(f"MISSING CARRIER {path}")
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        row = next((r for r in data["rows"] if r["task_id"] == tid), None)
        if row is None:
            out(f"MISSING CARRIER {path}#{tid}")
            return 1
        carriers[tid] = row
    roadmap = json.loads(ROADMAP.read_text(encoding="utf-8"))
    registry = roadmap["capability_registry"]
    registry_ids = {k: bool(v.get("present")) for k, v in registry.items()}

    sanity = OPENBB_ROOT / "providers/oecd/openbb_oecd/models/unemployment.py"
    out(f"round: {roadmap.get('round_id')} | registry ids read: {len(registry_ids)}")
    out(f"upstream root verified via {sanity}: {sanity.is_file()}")
    if not sanity.is_file():
        out("MISSING CARRIER (root verification failed) " + str(sanity))
        return 1
    out("")

    verdicts: list[tuple[str, str]] = []
    unresolved = 0
    results: dict[str, dict] = {}

    for tid, _carrier in ROWS:
        row = carriers[tid]
        out(f"===== {tid} =====")
        out(f"shape: {row['shape']}")
        out(f"row_extraction: {row['row_extraction']}")
        out(f"paged: {row['paged']}")
        out(f"needs_engine_capability: {json.dumps(row['needs_engine_capability'])}")
        out(f"notes: {row['notes']}")
        cite = row["evidence"]
        cite_file, _, cite_line = cite.partition(":")
        cite_path = OPENBB_ROOT / cite_file
        if not cite_path.is_file():
            out(f"MISSING CARRIER {cite_path}")
            return 1
        out(f"cited upstream file: {cite_path}  (cite line :{cite_line})")

        candidates = [cite_path]
        model_path = (
            OPENBB_ROOT
            / "providers"
            / row["provider"]
            / f"openbb_{row['provider']}"
            / "models"
            / f"{snake(row['upstream_model'])}.py"
        )
        if model_path not in candidates:
            candidates.append(model_path)
        regions: list[tuple[str, list[tuple[int, str]]]] = []
        body_file = None
        for cand in candidates:
            if not cand.is_file():
                continue
            lines = cand.read_text(encoding="utf-8").splitlines()
            found = body_regions(lines)
            if found:
                regions, body_file = found, (cand, lines)
                break
        if body_file is None:
            unresolved += 1
            out("BODY NOT LOCATED")
            out("")
            verdicts.append((tid, "BODY NOT LOCATED"))
            continue
        out(f"method bodies located in: {body_file[0]}")
        for name, reg in regions:
            out(f"-- {name} --")
            for n, t in reg:
                out(f"  :{n} {t}")
        fired = scan_regions(regions, registry_ids, out)

        censused = {LEGACY_CANON.get(x, x) for x in row["needs_engine_capability"]}
        observed = {c for _, c, _, _ in fired}
        widened = observed - censused
        missing_ids = {i for i in (censused | observed) if not registry_ids.get(i, False)}
        gaps = [f for f in fired if f[1] not in registry_ids]
        if gaps:
            verdict = "TAXONOMY-GAP"
        elif widened and (widened & missing_ids):
            verdict = "NOT-SHIPPABLE"
        elif widened:
            verdict = "NEEDS-WIDENED"
        elif missing_ids:
            verdict = "NOT-SHIPPABLE"
        else:
            verdict = "CONSISTENT"
        for cap in sorted(censused | observed):
            out(f"  capability {cap}: present={registry_ids.get(cap, 'UNKNOWN ID')}")
        out("")
        out(
            f"{tid}  {verdict}  censused={sorted(censused)}"
            f"  observed={sorted(observed)}  missing={sorted(missing_ids)}"
        )
        out(f"  fired predicate count: {len(fired)}")
        out("")
        verdicts.append((tid, verdict))
        results[tid] = {
            "regions": regions,
            "source_path": body_file[0],
            "observed": sorted({c for _, c, _, _ in fired}),
        }

    # ---- Arm B: counterfact (transpose line deleted in a /tmp copy) ----
    out("================ COUNTERFACT ARM ================")
    out("Arm A (CONTROL) = real run above against the pinned, untouched")
    out("upstream tree. Arm B = OBB2-tmx-EtfInfo re-scanned on a /tmp copy")
    out("with the transpose() call deleted; only the transpose predicate")
    out("may change. Any other diff means the instrument is unstable.")
    tmx = results.get("OBB2-tmx-EtfInfo")
    counterfact_ok = False
    if tmx:
        src = tmx["source_path"].read_text(encoding="utf-8")
        mutated = src.replace(".transpose()", "")
        tmp = Path("/tmp/c89_etf_info_no_transpose.py")  # nosec B108  # noqa: S108
        tmp.write_text(mutated, encoding="utf-8")
        out(f"temp copy written (NOT in repo): {tmp}")
        mregions = body_regions(mutated.splitlines())
        mfired = []
        for _pred, rx, canon in LINE_PREDICATES:
            hits = [
                (n, t) for n, t in ((n, t) for _, reg in mregions for n, t in reg) if rx.search(t)
            ]
            if hits:
                mfired.append(canon)
        after = sorted(set(mfired))
        before = tmx["observed"]
        out(f"observed BEFORE (control): {before}")
        out(f"observed AFTER  (variant): {after}")
        lost = sorted(set(before) - set(after))
        gained = sorted(set(after) - set(before))
        counterfact_ok = lost == ["shape.columnar"] and gained == []
        out(f"delta lost={lost} gained={gained}")
        if counterfact_ok:
            out(
                "OK: exactly the transpose predicate differs;"
                " the instrument is predicate-live, not inert"
            )
        else:
            out("COUNTERFACT FAILED: instrument is not predicate-live")
    else:
        out("COUNTERFACT SKIPPED: tmx row unresolved")
    out("")
    exit_code = 0 if unresolved == 0 and counterfact_ok else 1
    out(f"rows audited: {5 - unresolved} of 5 | unresolved: {unresolved} | exit: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
