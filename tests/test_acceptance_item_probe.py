"""Tests for exact acceptance-probe evidence matchers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import tomllib

from scripts.quality import acceptance_item_probe as tool
from scripts.quality.acceptance_item_probe import (
    DWD_MERGE_REL,
    DWD_REVISION_NODES,
    GAP,
    GENERIC_API_KEY_ALLOWED_REGEXES,
    PORTED_ROOT,
    PROVEN,
    QFQ_OFFICIAL_RUN,
    REPO_ROOT,
    RIGHTS_INVENTORY,
    Context,
    _ac9_07_call_chain_facts,
    bare_token_paths,
    census_of,
    classify_secret_paths,
    config_has_no_global_path_exemption,
    covered_by,
    exact_partition_placement_assertions,
    first_party_py_files_under,
    gitleaks_leak_count,
    judge_ac1_03,
    judge_ac1_08,
    judge_ac1_09,
    judge_ac11_02,
    key_monitoring_observations,
    measure_ac1_08,
    measure_ac5_02,
    measure_ac5_07,
    measure_ac11_02,
    parse_items,
    partition_census_archive_reading,
    playwright_outcomes,
    probe_for,
    public_gitleaks_rule_shapes,
    py_files_under,
    resolve_repair,
    run_argv,
    scan_env_template,
    scope_reparented,
    scope_vanished,
    whitelist_buckets,
)
from scripts.quality.acceptance_ledger_check import parse_doc

PLAYWRIGHT_TITLES = {
    "merged_catalog": (
        "merged catalog is the default at both catalog URLs and filters by market and dataset"
    ),
    "function_detail": (
        "catalog function drilldown reaches legacy list and an actual script detail route"
    ),
}

RIGHTS_HEADER = (
    "| # | 数据源 | 接入方式 | 条款链接 | 允许本项目落库 | 允许再分发 | "
    "允许商业使用 | 复核日期 | 责任人 | 状态 |"
)
RIGHTS_SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|"


def rights_row(
    row_id: int,
    source: str,
    *,
    clause_url: str = "https://example.test/terms",
    internal_use: str = "允许",
    redistribution: str = "不允许",
    commercial_use: str = "允许",
    review_date: str = "2026-09-30",
    responsible: str = "法务负责人",
    status: str = "已复核",
) -> str:
    """Render one synthetic section-1 source row for parser regressions."""
    cells = (
        str(row_id),
        source,
        "测试接入",
        clause_url,
        internal_use,
        redistribution,
        commercial_use,
        review_date,
        responsible,
        status,
    )
    return "| " + " | ".join(cells) + " |"


def rights_context(tmp_path: Path, registry_text: str, inventory_text: str) -> Context:
    """Write only the two local text fixtures read by the AC-1|08 measure."""
    registry_path = tmp_path / "docs" / "data-rights-registry.md"
    inventory_path = tmp_path / RIGHTS_INVENTORY
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(registry_text, encoding="utf-8")
    inventory_path.write_text(inventory_text, encoding="utf-8")
    return Context(tmp_path, (), {})


def test_key_observation_archive_distinguishes_metadata_and_live_delivery() -> None:
    """Operator dates, mock reports and unknown quota never become live proof."""
    unknown = {
        "issuer_expiry_observed": "no",
        "quota_remaining_observed": "no",
        "delivery_observed": "no",
    }
    assert key_monitoring_observations("") == unknown
    assert key_monitoring_observations("[]") == unknown
    record: dict[str, object] = {
        "scope": "independent-live-key-monitoring",
        "provider": "ths",
        "observed_at": "2026-09-30T00:00:00Z",
        "expiry_provenance": "operator",
        "expiry_date": "2027-01-01",
        "remaining_quota": None,
        "notification_delivered": False,
    }
    assert key_monitoring_observations(json.dumps(record)) == unknown
    record.update(
        expiry_provenance="issuer",
        remaining_quota=123,
        notification_delivered=True,
        notification_channel="websocket",
        delivery_evidence="independent-channel-receipt",
    )
    assert set(key_monitoring_observations(json.dumps(record)).values()) == {"yes"}
    record["remaining_quota"] = True
    assert key_monitoring_observations(json.dumps(record))["quota_remaining_observed"] == "no"


def test_project_identity_probe_matches_the_release_and_rejects_version_drift() -> None:
    from scripts.quality import acceptance_item_probe as tool

    ctx = tool.load_context()
    probe = tool.probe_for("AC-1|01")
    facts = probe.measure(ctx)
    assert facts["version"] == "0.1.1"
    assert probe.judge(facts).state == PROVEN
    for version in ("0.1.0", "0.0.9", ""):
        assert probe.judge({**facts, "version": version}).state == tool.GAP


def test_item_parser_matches_all_ledger_groups_including_non_ac_planes() -> None:
    """Performance and reliability criteria share exactly the ledger identities."""
    path = REPO_ROOT / "docs/迭代计划/迭代1-重构数据中台/验收文档.md"
    text = path.read_text(encoding="utf-8")
    ledger_items, _rows = parse_doc(text)
    probes = parse_items(text)
    assert len(probes) == len(ledger_items) == 130
    assert {item.key for item in probes} == {item.key for item in ledger_items}
    assert {item.group for item in probes} >= {"§4", "§5", "§6"}


def test_ac1_08_proves_a_complete_section_1_registry(tmp_path: Path) -> None:
    """A complete source record includes URL, explicit uses, review date, and owner."""
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲"),
            rights_row(2, "来源乙", redistribution="允许", commercial_use="不允许"),
            "",
            "## 2. 待办与责任",
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲, 来源乙]\n")

    facts = measure_ac1_08(ctx)

    assert facts["table_valid"] == "yes"
    assert facts["rows"] == "2"
    assert facts["dated"] == "2"
    assert facts["undecided"] == "0"
    assert facts["unlinked"] == "0"
    assert facts["responsible_missing"] == "0"
    assert facts["uncovered"] == "0"
    assert judge_ac1_08(facts).state == PROVEN


@pytest.mark.parametrize(
    ("clause_url", "unlinked", "site_root_only"),
    [
        # A deep link is what the column asks for: neither face fires.
        ("https://example.com/legal/terms", "0", "0"),
        ("https://example.com/terms#privacy", "0", "0"),
        ("https://example.com:8443/tos/index.html", "0", "0"),
        # A URL that names no page at all: a link, but not a 条款链接.
        ("https://example.com/", "0", "1"),
        ("https://example.com", "0", "1"),
        ("http://example.com/", "0", "1"),
        # Not a URL: counted once, as prose, and never again as a site root.
        ("条款待补", "1", "0"),
        ("", "1", "0"),
        ("https://example.com/ 待补", "1", "0"),
        ("ftp://example.com/terms", "1", "0"),
        ("not a url at all", "1", "0"),
    ],
)
def test_ac1_08_clause_link_faces_fire_exclusively_and_never_double_count(
    tmp_path: Path, clause_url: str, unlinked: str, site_root_only: str
) -> None:
    """`unlinked` asks for a URL shape, `site_root_only` asks whether it reaches any page."""
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲", clause_url=clause_url),
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲]\n")

    facts = measure_ac1_08(ctx)

    assert facts["rows"] == "1"
    assert facts["unlinked"] == unlinked, clause_url
    assert facts["site_root_only"] == site_root_only, clause_url
    # The two faces are disjoint by construction: a cell has to BE a URL to be a root-only URL.
    assert not (facts["unlinked"] != "0" and facts["site_root_only"] != "0"), clause_url
    # Any nonzero face is a gap; a homepage can no longer satisfy a column named 条款链接.
    if unlinked == site_root_only == "0":
        assert judge_ac1_08(facts).state == PROVEN
    else:
        assert judge_ac1_08(facts).state == GAP


def test_ac1_08_site_root_homepage_reds_the_cell_the_old_probe_passed(tmp_path: Path) -> None:
    """The counterfactual that motivates the fix: URL-shaped, but pointing at a homepage.

    Before this face existed the same registry read `unlinked = 0/35` and judged PROVEN while 16
    rows linked `https://www.benzinga.com/`, `https://www.eia.gov/` and friends -- so the cell
    looked proven on links that carry no terms text at all.
    """
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲", clause_url="https://www.benzinga.com/"),
            rights_row(2, "来源乙", clause_url="https://www.eia.gov"),
            rights_row(3, "来源丙", clause_url="https://example.com/legal/terms"),
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲, 来源乙, 来源丙]\n")

    facts = measure_ac1_08(ctx)

    assert facts["rows"] == "3"
    assert facts["unlinked"] == "0"
    assert facts["site_root_only"] == "2"

    verdict = judge_ac1_08(facts)
    assert verdict.state == GAP
    assert any(
        reading.startswith("rows whose 条款链接 is a URL but only the site root")
        and reading.endswith("= 2/3")
        for reading in verdict.readings
    ), verdict.readings
    # The reading says which of the two conditions failed, and how many rows each covers.
    condition = next(r for r in verdict.readings if "requires unlinked = 0 AND" in r)
    assert "0 row(s) whose 条款链接 is prose" not in condition
    assert "2 row(s) whose 条款链接 is a URL that stops at the site root" in condition
    assert "site root" in verdict.reason and "prose" not in verdict.reason


def test_ac1_08_prose_row_is_counted_as_unlinked_only(tmp_path: Path) -> None:
    """The other direction: prose still lands in `unlinked` and must not leak into the new face."""
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲", clause_url="条款待补"),
            rights_row(2, "来源乙", clause_url="https://example.com/legal/terms"),
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲, 来源乙]\n")

    facts = measure_ac1_08(ctx)

    assert facts["unlinked"] == "1"
    assert facts["site_root_only"] == "0"

    verdict = judge_ac1_08(facts)
    assert verdict.state == GAP
    condition = next(r for r in verdict.readings if "requires unlinked = 0 AND" in r)
    assert "1 row(s) whose 条款链接 is prose instead of a URL" in condition
    assert "site root" not in condition.split(":", 1)[1]
    assert "prose" in verdict.reason


def test_ac1_08_deep_terms_urls_satisfy_both_faces(tmp_path: Path) -> None:
    """A registry whose every row reaches a real page stays proven -- the guard is not a veto."""
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲", clause_url="https://example.com/legal/terms"),
            rights_row(2, "来源乙", clause_url="https://example.com/privacy_policy.htm"),
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲, 来源乙]\n")

    facts = measure_ac1_08(ctx)

    assert facts["unlinked"] == "0"
    assert facts["site_root_only"] == "0"
    verdict = judge_ac1_08(facts)
    assert verdict.state == PROVEN
    assert verdict.reason == ""


def test_ac1_08_ignores_section_2_urls_and_dates(tmp_path: Path) -> None:
    """A todo row with a URL/date cannot fill missing fields in the §1 record."""
    incomplete = rights_row(
        1,
        "来源甲",
        clause_url="条款待补",
        internal_use="待确认",
        review_date="—",
        responsible="未指定",
    )
    fake_complete_todo = rights_row(99, "待办事项")
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            incomplete,
            "",
            "## 2. 待办与责任",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            fake_complete_todo,
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲]\n")

    facts = measure_ac1_08(ctx)

    assert facts["table_valid"] == "yes"
    assert facts["rows"] == "1"
    assert facts["dated"] == "0"
    assert facts["undecided"] == "1"
    assert facts["unlinked"] == "1"
    assert facts["responsible_missing"] == "1"
    assert judge_ac1_08(facts).state == GAP


def test_ac1_08_missing_registry_file_fails_closed(tmp_path: Path) -> None:
    inventory_path = tmp_path / RIGHTS_INVENTORY
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text("  rights_rows: [来源甲]\n", encoding="utf-8")
    ctx = Context(tmp_path, (), {})

    facts = measure_ac1_08(ctx)

    assert facts["table_valid"] == "no"
    assert facts["rows"] == "0"
    assert facts["uncovered"] == "1"
    assert judge_ac1_08(facts).state == GAP


@pytest.mark.parametrize(
    "table,reason",
    [
        (f"{RIGHTS_HEADER}\n{RIGHTS_SEPARATOR}\n", "empty"),
        (
            f"{RIGHTS_HEADER}\n{RIGHTS_SEPARATOR}\n| 1 | truncated |\n",
            "short row",
        ),
        (
            f"{RIGHTS_HEADER.replace(' | 责任人', '')}\n"
            f"{RIGHTS_SEPARATOR}\n"
            "| 1 | 来源甲 | 测试接入 | https://example.test/terms | 允许 | "
            "不允许 | 允许 | 2026-09-30 | 已复核 |\n",
            "missing owner column",
        ),
    ],
)
def test_ac1_08_empty_or_malformed_section_1_table_fails_closed(
    tmp_path: Path, table: str, reason: str
) -> None:
    registry = f"## 1. 登记表\n{table}"
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲]\n")

    facts = measure_ac1_08(ctx)

    assert facts["table_valid"] == "no", reason
    assert facts["rows"] == "0"
    assert judge_ac1_08(facts).state == GAP


@pytest.mark.parametrize("responsible", ["", "未指定"])
def test_ac1_08_requires_a_named_responsible_party(tmp_path: Path, responsible: str) -> None:
    registry = "\n".join(
        (
            "## 1. 登记表",
            RIGHTS_HEADER,
            RIGHTS_SEPARATOR,
            rights_row(1, "来源甲", responsible=responsible),
        )
    )
    ctx = rights_context(tmp_path, registry, "  rights_rows: [来源甲]\n")

    facts = measure_ac1_08(ctx)

    assert facts["table_valid"] == "yes"
    assert facts["responsible_missing"] == "1"
    assert judge_ac1_08(facts).state == GAP


def test_ac1_08_real_registry_measures_complete_restricted_review() -> None:
    """The complete registry keeps source-specific restrictions visible."""
    facts = measure_ac1_08(Context(REPO_ROOT, (), {}))

    assert facts["table_valid"] == "yes"
    assert facts["rows"] == "35"
    assert facts["dated"] == facts["rows"] == "35"
    assert facts["undecided"] == "0"
    assert facts["unlinked"] == "0"
    # The old probe stopped here and called the column complete. `unlinked = 0` only ever said the
    # cells are URL-shaped; how many of those URLs name no page is the face below.
    assert facts["site_root_only"] != "0"
    assert facts["responsible_missing"] == "0"
    assert facts["uncovered"] == "0"

    registry = (REPO_ROOT / "docs/data-rights-registry.md").read_text(encoding="utf-8")
    section_one = registry.split("## 2. 待办与责任", maxsplit=1)[0]
    headers = [cell.strip() for cell in RIGHTS_HEADER.strip("|").split("|")]
    rows: dict[int, dict[str, str]] = {}
    for line in section_one.splitlines():
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0].isdigit():
            assert len(cells) == len(headers)
            rows[int(cells[0])] = dict(zip(headers, cells, strict=True))

    historical_sources = {
        1: "同花顺扶摇 API",
        2: "东方财富（网页/接口）",
        3: "新浪财经",
        4: "腾讯财经",
        5: "雪球",
        6: "交易所官网（上交所/深交所/中金所等）",
        7: "国家统计局 / 中国人民银行",
        8: "Yahoo Finance（yfinance）",
        9: "FRED（圣路易斯联储）",
        10: "ECB / IMF / OECD",
        11: "商业源（FMP/Tiingo/Alpha Vantage/Intrinio/Tradier…）",
    }
    assert set(rows) == set(range(1, 36))
    # Recount the homepage cells straight from the shipped table, so the reading is pinned to the
    # data rather than to a number the probe picked: a site root is an http(s) URL whose path is
    # empty or "/". 16 of the 35 rows were 051824bd's honest count; assert the count, not the date.
    live_site_roots = {
        row_id
        for row_id, row in rows.items()
        if urlsplit(row["条款链接"]).scheme in {"http", "https"}
        and urlsplit(row["条款链接"]).netloc
        and urlsplit(row["条款链接"]).path in {"", "/"}
    }
    assert facts["site_root_only"] == str(len(live_site_roots))
    assert live_site_roots, "the new face exists to expose these rows, never to certify zero"
    assert {rows[row_id]["条款链接"] for row_id in live_site_roots} >= {
        "https://www.benzinga.com/",
        "https://www.eia.gov/",
        "https://tradingeconomics.com/",
    }
    assert {row_id: rows[row_id]["数据源"] for row_id in historical_sources} == (historical_sources)
    assert rows[12]["数据源"] == "BLS（美国劳工统计局）"
    assert rows[13]["数据源"] == "FMP（EquityHistorical / EquityQuote）"
    assert rows[14]["数据源"] == "Cboe（cdn.cboe.com 公开接口）"
    assert rows[15]["数据源"] == "美联储理事会（Board of Governors）"
    assert rows[16]["数据源"] == "Intrinio"
    assert rows[17]["数据源"] == "Benzinga（Benzinga Pro API）"
    assert rows[18]["数据源"] == "EconDB（econdb.com 静态名册 CSV）"
    assert rows[19]["数据源"] == "Tiingo（api.tiingo.com）"

    restricted = "已复核（受限）"
    not_applicable = "不适用（本迭代未启用）"
    pending_clause = "待复核（本迭代已接入，条款原文未取得）"
    assert [rows[row_id]["状态"] for row_id in range(1, 11)] == [restricted] * 10
    assert rows[11]["状态"] == not_applicable
    assert rows[12]["状态"] == restricted
    assert rows[13]["状态"] == restricted
    assert rows[14]["状态"] == restricted
    assert rows[15]["状态"] == restricted
    assert rows[16]["状态"] == restricted
    assert rows[17]["状态"] == pending_clause
    assert rows[18]["状态"] == pending_clause
    assert rows[19]["状态"] == restricted
    for row_id in range(20, 36):
        assert rows[row_id]["状态"] == pending_clause
        assert rows[row_id]["复核日期"] == "2026-10-23"
        assert "仅登记" in rows[row_id]["责任人"]
        for use in ("允许本项目落库", "允许再分发", "允许商业使用"):
            assert "暂不批准" in rows[row_id][use]
    assert "日期是登记日期，不是条款复核日期" in registry
    assert {rows[row_id]["复核日期"] for row_id in range(1, 12)} == {"2026-09-30"}
    assert rows[12]["复核日期"] == rows[13]["复核日期"] == "2026-10-08"
    assert rows[14]["复核日期"] == "2026-10-08"
    assert rows[15]["复核日期"] == "2026-10-09"
    assert rows[16]["复核日期"] == "2026-10-11"
    assert rows[17]["复核日期"] == rows[18]["复核日期"] == rows[19]["复核日期"] == "2026-10-12"

    bls = rows[12]
    assert "公共领域数据可附条件使用并注明 BLS 来源" in bls["允许本项目落库"]
    assert "不含受版权保护图片" in bls["允许本项目落库"]
    assert "受限微观数据" in bls["允许本项目落库"]
    assert "附来源、访问日期" in bls["允许再分发"]
    assert "不冒用 BLS 标识" in bls["允许再分发"]
    assert "公共领域声明未限制上述统计的商业用途" in bls["允许商业使用"]

    fmp = rows[13]
    assert "opendata/data/providers/fmp" in fmp["接入方式"]
    assert "stable historical-price-eod/full" in fmp["接入方式"]
    assert "quote" in fmp["接入方式"]
    assert "仅离线原型已接线" in fmp["接入方式"]
    for use in ("允许本项目落库", "允许再分发", "允许商业使用"):
        assert "未取得" in fmp[use]
        assert "暂不批准" in fmp[use]
    assert "个人计划" in fmp["允许商业使用"]
    assert "API Key 或代码实现推定许可" in registry

    cboe = rows[14]
    assert cboe["条款链接"] == "https://www.cboe.com/terms"
    assert "opendata/data/providers/cboe" in cboe["接入方式"]
    assert "cdn.cboe.com" in cboe["接入方式"]
    assert "无凭据" in cboe["接入方式"]
    assert (
        "AvailableIndices / IndexConstituents / IndexSearch / EquitySearch 四条模型描述符已接线"
        in cboe["接入方式"]
    )
    assert "书面同意" in cboe["允许本项目落库"]
    assert "fair use 例外不覆盖全量留存" in cboe["允许本项目落库"]
    assert "暂不批准" in cboe["允许本项目落库"]
    assert "未经书面许可不得修改、复制或分发" in cboe["允许再分发"]
    assert "暂不批准" in cboe["允许再分发"]
    assert "AS IS" in cboe["允许商业使用"]
    assert "非交易用途" in cboe["允许商业使用"]
    assert "暂不批准" in cboe["允许商业使用"]
    assert cboe["责任人"] == "cloudQuant / Codex"

    fed = rows[15]
    assert fed["条款链接"] == "https://www.federalreserve.gov/disclaimer.htm"
    assert "opendata/data/providers/federal_reserve" in fed["接入方式"]
    assert "datadownload/Output.aspx" in fed["接入方式"]
    assert "rel=H6" in fed["接入方式"] and "rel=H15" in fed["接入方式"]
    assert "无凭据" in fed["接入方式"]
    assert "MoneyMeasures / TreasuryRates / SvenssonYieldCurve 三条已接线" in fed["接入方式"]
    assert (
        "public domain and may be copied and distributed without permission"
        in fed["允许本项目落库"]
    )
    assert "Please cite to the Board as the source of the information" in fed["允许本项目落库"]
    assert "本行只覆盖这两张发布表下载包里的统计数值" in fed["允许本项目落库"]
    assert "须注明美联储理事会为来源" in fed["允许再分发"]
    assert "他方来源的版权材料须向原始权利人取得许可" in fed["允许再分发"]
    assert "unless otherwise indicated" in fed["允许商业使用"]
    assert "本轮两张表未见另示" in fed["允许商业使用"]
    assert fed["责任人"] == "cloudQuant / Codex"
    assert "同包另注册 FederalFundsRate / SOFR / OvernightBankFundingRate 三条" in fed["接入方式"]
    assert "markets.newyorkfed.org" in fed["接入方式"]
    assert "该包共 6 条能力" in fed["接入方式"]

    intrinio = rows[16]
    assert (
        "CurrencyPairs / EquitySearch / HistoricalDividends / CalendarIpo / InsiderTrading 五条"
        in intrinio["接入方式"]
    )

    benzinga = rows[17]
    econdb = rows[18]
    tiingo = rows[19]
    assert "opendata/data/providers/benzinga" in benzinga["接入方式"]
    assert "api.benzinga.com/api/v2.1/calendar/ratings" in benzinga["接入方式"]
    assert "BENZINGA_API_KEY" in benzinga["接入方式"]
    assert "PriceTarget 一条" in benzinga["接入方式"]
    assert "opendata/data/providers/econdb" in econdb["接入方式"]
    assert "static/help/main_tickers.csv" in econdb["接入方式"]
    assert "无凭据" in econdb["接入方式"]
    assert "AvailableIndicators 一条" in econdb["接入方式"]
    assert "opendata/data/providers/tiingo" in tiingo["接入方式"]
    assert "api.tiingo.com/tiingo/crypto/prices" in tiingo["接入方式"]
    assert "TIINGO_TOKEN" in tiingo["接入方式"]
    assert "CryptoHistorical 一条" in tiingo["接入方式"]
    for new_row in (benzinga, econdb, tiingo):
        for use in ("允许本项目落库", "允许再分发", "允许商业使用"):
            assert "暂不批准" in new_row[use]
        assert new_row["责任人"] == "cloudQuant / Codex"
    assert "HTTP 403" in benzinga["允许本项目落库"]
    assert "HTTP 403" in econdb["允许本项目落库"]
    assert "you may not write, save, archive, back up" in tiingo["允许本项目落库"]
    assert "Redistribution is only available upon special request" in tiingo["允许再分发"]
    assert "you must sign up for a Commercial plan" in tiingo["允许商业使用"]
    assert "数值权利属于发布机构" in econdb["允许本项目落库"]
    assert "未签订任何 Benzinga 协议" in benzinga["允许本项目落库"]

    assert "这不是供应商授权" in registry
    assert "不覆盖 cboe 其余 8 个上游模型（清单实测 11 行 = 3 DEV_DONE + 8 NOT_RUN）" in registry
    assert "evidence/C72/cboe-rights-review.md" in registry
    assert "不覆盖 Board 站点其余出版物" in registry
    assert "模型清单实测 federal_reserve 共 13 行（2 DEV_DONE + 11 NOT_RUN）" in registry
    assert "https://www.federalreserve.gov/disclaimer.htm" in registry
    assert "没有释放 FMP 或 Cboe 或美联储或 Intrinio 的真实采集、落库或对外分发动作" in registry
    assert "不足以支撑一次「已复核」" in registry
    assert "不覆盖第15行的覆盖声明" not in registry  # the split is stated in row 15's own cell
    assert "逐源条款行列入 §2 待办第 7 项" in registry

    # §1.1 cites 待办 items 6 and 7; a citation with no row in the 待办 table is a promise
    # to no one.
    todo = registry.split("## 2. 待办与责任", maxsplit=1)[1]
    todo_ids = {
        line.strip().strip("|").split("|")[0].strip()
        for line in todo.splitlines()
        if line.startswith("| ") and line.split("|")[1].strip().isdigit()
    }
    assert {"1", "2", "3", "4", "5", "6", "7"} <= todo_ids
    assert "Benzinga 与 EconDB 的条款页" in todo
    assert "markets.newyorkfed.org" in todo

    verdict = judge_ac1_08(facts)
    # #23 (`5db160ba`) rewrote the v1.8 caveat 「登记完整性不等于所有用途获准」 into the v1.9
    # sentence 「不代表独立核验或全量许可」. Same meaning, so the registry still declares its own
    # limits and the assertion right below still forbids the verdict from claiming blanket approval.
    assert "不代表独立核验或全量许可" in registry
    assert "全部获准" not in " ".join(verdict.readings)
    # The shipped column itself decides the verdict now: §1 registers 16 homepages as 条款链接, so
    # the cell is an honest gap awaiting the owner's real terms URLs. This is the outcome the fix
    # exists to produce -- the ledger still says proven, and the ratchet books that from CI.
    assert verdict.state == GAP
    assert "site root" in verdict.reason
    assert f"{facts['site_root_only']}/{facts['rows']}" in " ".join(verdict.readings)


def test_ac1_03_judge_requires_a_current_bidirectional_path_register() -> None:
    clean = {
        "buckets": "7",
        "bucket_names": "opendata_http/, docs/",
        "hit_files": "4",
        "vendored": "1",
        "outside": "0",
        "outside_sample": "",
        "outside_more": "0",
        "unreadable": "0",
        "policy_valid": "yes",
        "policy_error": "-",
        "policy_entries": "3",
        "metadata_exceptions": "3",
        "metadata_expected": "3",
        "unregistered": "0",
        "unregistered_sample": "",
        "stale_paths": "0",
        "sha_mismatch": "0",
        "metadata_invalid": "0",
        "integration_frozen": "3",
        "integration_files": "a.py, b.py",
    }
    assert judge_ac1_03(clean).state == PROVEN
    for field, value in (
        ("outside", "1"),
        ("stale_paths", "1"),
        ("sha_mismatch", "1"),
        ("metadata_invalid", "1"),
        ("metadata_exceptions", "2"),
        ("unreadable", "1"),
        ("policy_valid", "no"),
    ):
        assert judge_ac1_03({**clean, field: value}).state == GAP, field

    unreadable = judge_ac1_03({**clean, "unreadable": "7"})
    assert unreadable.state == GAP
    assert "7 tracked path(s) could not be read" in unreadable.reason
    assert "wording decision" not in unreadable.reason
    invalid_register = judge_ac1_03({**clean, "policy_valid": "no", "policy_error": "bad SHA"})
    assert "path register is invalid (bad SHA)" in invalid_register.reason


def test_context_tracked_preserves_quoted_unicode_and_newline_paths(tmp_path: Path) -> None:
    for git_args in (["init", "--quiet"], ["config", "core.quotepath", "true"]):
        code, output = run_argv(["git", *git_args], cwd=tmp_path)
        assert code == 0, output
    relative_paths = (
        "文档/中文 文件.md",
        'docs/引号 "文件".md',
        "docs/换行\n文件.md",
    )
    for index, rel in enumerate(relative_paths):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("akshare\n" if index == 0 else "plain text\n", encoding="utf-8")
    code, output = run_argv(["git", "add", "--all"], cwd=tmp_path)
    assert code == 0, output

    tracked = Context(tmp_path, (), {}).tracked()
    hits, unreadable = bare_token_paths(tmp_path, tracked)

    assert tracked == sorted(relative_paths)
    assert hits == {relative_paths[0]}
    assert unreadable == []


def test_bare_token_path_inventory_keeps_real_missing_paths_as_unreadable(
    tmp_path: Path,
) -> None:
    hits, unreadable = bare_token_paths(tmp_path, ["docs/missing.md"])

    assert hits == set()
    assert unreadable == ["docs/missing.md"]


def test_ac1_03_whitelist_reads_only_explicit_complete_paths() -> None:
    approved = (
        "裸词 `akshare` 的逐词白名单：署名、上游沿革与搬运代码沿用原路径白名单（"
        "`akshare/`，迭代 A2 起为 `opendata_http/`；`THIRD_PARTY_NOTICES.md`、"
        "`LICENSE-AKSHARE`、`README.md`、`CODE_QUALITY.md`、`scripts/codemod/`、"
        "`tests/`、`docs/`）。"
    )
    buckets = whitelist_buckets(approved)

    assert buckets == [
        f"{PORTED_ROOT}/",
        "THIRD_PARTY_NOTICES.md",
        "LICENSE-AKSHARE",
        "README.md",
        "CODE_QUALITY.md",
        "scripts/codemod/",
        "tests/",
        "docs/",
    ]
    assert covered_by("docs/quality/akshare-reference-allowlist.json", buckets)
    assert covered_by("scripts/codemod/compare_with_upstream.py", buckets)
    assert not covered_by("scripts/quality/acceptance_item_probe.py", buckets)
    assert not covered_by("opendata/api/data.py", buckets)

    assert whitelist_buckets("普通文档提及 `docs/`，但没有获批短语。") == ["docs/"]
    assert whitelist_buckets("旧文案列出 `akshare/` 和 `tests/`。") == [
        f"{PORTED_ROOT}/",
        "tests/",
    ]
    # 两个历史拼法（旧名 akshare/ 与迁移名 opendata_http/）说的是同一棵树，必须收成同一个桶
    both = whitelist_buckets("同时写旧名 `akshare/` 与迁移名 `opendata_http/`。")
    assert both == [f"{PORTED_ROOT}/"], both
    assert whitelist_buckets("非路径 token `LICENSE` 与 `daily`。") == []


def test_scope_vanished_pairs_a_renamed_root_with_its_current_census_key() -> None:
    """改名按 census 键对齐，重挂靠文件总量对齐；真缩范围两种对齐都救不了。"""
    newer = {"opendata": 215, "scripts": 57, PORTED_ROOT: 325}
    older = {"opendata": 215, "scripts": 57, "opendata_http": 313}

    assert scope_vanished(newer, older) == ()
    assert census_of({"file_counts": older}, [PORTED_ROOT]) == 313

    narrowed = {name: value for name, value in newer.items() if name != "scripts"}
    assert scope_vanished(narrowed, older) == ("scripts",)

    # 无单一身份的旧根（Fuyao 的十二个文件散进了 THS provider）：计数在他处回来就是搬家，
    # 名单里退出这件事仍然要打出来给读者看，不能当成什么都没发生。
    with_fuyao = {**older, "opendata_fuyao": 12}
    moved = {"opendata": 227, "scripts": 57, PORTED_ROOT: 325}
    assert scope_vanished(moved, with_fuyao) == ()
    assert scope_reparented(moved, with_fuyao) == ("opendata_fuyao",)

    # 别的根一个文件都没多出来：这次是真的没有被测到了
    flat = {"opendata": 215, "scripts": 57, PORTED_ROOT: 313}
    assert scope_vanished(flat, {**older, "opendata_fuyao": 4}) == ("opendata_fuyao",)


def test_selfdev_population_leaves_the_nested_vendor_tree_out() -> None:
    """搬运树嵌在 opendata/ 里：自研存量口径要按布局权威分层，同姓的首方适配层不能一起丢掉。"""
    naive = py_files_under("opendata")
    first_party = first_party_py_files_under("opendata")
    vendored = [name for name in naive if name.startswith(f"{PORTED_ROOT}/")]

    assert vendored, "搬运树不再嵌在自研根下时，这条测试就测不到混合种群了"
    assert not [name for name in first_party if name.startswith(f"{PORTED_ROOT}/")]
    assert len(naive) - len(first_party) == len(vendored)
    assert "opendata/data/providers/akshare/__init__.py" in first_party
    assert py_files_under(PORTED_ROOT) == vendored


def test_ac5_02_scope_break_turns_a_repaired_reading_red() -> None:
    ctx = Context(REPO_ROOT, (), {})
    probe = probe_for("AC-5|02")
    clean = resolve_repair(measure_ac5_02(ctx), probe.repair)

    assert probe.judge(clean).state == PROVEN
    scope_break = next(item for item in probe.breaks if "路径/批次" in item.label)
    assert probe.judge({**clean, **dict(scope_break.facts)}).state == GAP


def test_ac1_09_path_rules_distinguish_real_env_from_one_template() -> None:
    env, templates, generated = classify_secret_paths(
        [
            ".env.example",
            ".env",
            ".env.production",
            "nested/.env.local",
            ".idea/workspace.xml",
            "logs/run.pid",
            "docs/readme.md",
        ]
    )
    assert env == [".env", ".env.production", "nested/.env.local"]
    assert templates == [".env.example"]
    assert generated == [".idea/workspace.xml", "logs/run.pid"]


def test_ac1_09_template_config_has_no_global_path_exception() -> None:
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    parsed = tomllib.loads(config)

    assert config_has_no_global_path_exemption(config) is True
    assert parsed.get("allowlist", {}).get("paths", []) == []
    assert [rule["id"] for rule in parsed["rules"]] == [
        "curl-auth-header",
        "generic-api-key",
    ]
    assert ".egg-info" not in config

    excluded = "[allowlist]\npaths = ['''\\.env\\.example$''']\n"
    assert config_has_no_global_path_exemption(excluded) is False
    assert scan_env_template("key=value\n", excluded) == (
        2,
        "global-path-exemption-present-or-config-invalid",
    )


def test_ac1_09_leak_count_reads_the_scanners_own_both_arms() -> None:
    assert gitleaks_leak_count("12:35AM WRN leaks found: 441\n") == "441"
    assert gitleaks_leak_count("12:41AM INF no leaks found\n") == "0"
    assert gitleaks_leak_count("scanned 125 MB in 37s\n") == "(unreadable)"


def test_ac1_09_judge_rejects_any_global_path_exemption() -> None:
    clean = {
        "strict": "0",
        "template_paths": "1",
        "template_names": ".env.example",
        "exact_paths": "yes",
        "allow_paths": "0",
        "non_template": "0",
        "rule_blocks": "2",
        "rule_shapes": "yes",
        "registered": "5",
        "upstream_files": "5",
        "upstream_present": "5",
        "upstream_absent_names": "",
        "live_shapes": "0",
        "gitleaks_rc": "0",
        "scan_leaks": "0",
        "template_config_ok": "yes",
        "template_scan_rc": "0",
        "literal": "1",
        "env_sample": "",
        "generated_sample": "",
        "non_template_names": "",
        "secret_check_line": "exit=0; leaks=0; locations=-",
        "template_scan_summary": "findings=0; locations=-",
    }

    assert judge_ac1_09(clean).state == PROVEN
    assert judge_ac1_09({**clean, "allow_paths": "1"}).state == GAP


def test_ac1_09_accepts_only_the_two_exact_public_rule_regexes() -> None:
    config = tomllib.loads((REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    rules = {
        rule["id"]: rule
        for rule in config["rules"]
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    }

    assert list(GENERIC_API_KEY_ALLOWED_REGEXES) == rules["generic-api-key"]["allowlist"]["regexes"]
    assert public_gitleaks_rule_shapes(rules)
    api_rule = rules["generic-api-key"]
    api_allow = api_rule["allowlist"]
    for mutated_regexes in (
        [GENERIC_API_KEY_ALLOWED_REGEXES[0]],
        [GENERIC_API_KEY_ALLOWED_REGEXES[0], r"^API_KEY_FAILURE_DELAY_SECONDS=0\.\d+$"],
        [*GENERIC_API_KEY_ALLOWED_REGEXES, r"^API_KEY_FAILURE_DELAY_SECONDS=.*$"],
    ):
        mutated_rules = {
            **rules,
            "generic-api-key": {
                **api_rule,
                "allowlist": {**api_allow, "regexes": mutated_regexes},
            },
        }
        assert not public_gitleaks_rule_shapes(mutated_rules)

    probe = probe_for("AC-1|09")
    clean = {
        "strict": "0",
        "template_paths": "1",
        "template_names": ".env.example",
        "exact_paths": "yes",
        "allow_paths": "0",
        "non_template": "0",
        "rule_blocks": "2",
        "rule_shapes": "yes",
        "registered": "5",
        "upstream_files": "5",
        "upstream_present": "5",
        "upstream_absent_names": "",
        "live_shapes": "0",
        "gitleaks_rc": "0",
        "scan_leaks": "0",
        "template_config_ok": "yes",
        "template_scan_rc": "0",
        "literal": "1",
        "env_sample": "",
        "generated_sample": "",
        "non_template_names": "",
        "secret_check_line": "exit=0; leaks=0; locations=-",
        "template_scan_summary": "findings=0; locations=-",
    }
    assert probe.judge(clean).state == PROVEN
    shape_break = next(item for item in probe.breaks if "rule_shapes" in dict(item.facts))
    assert probe.judge({**clean, **dict(shape_break.facts)}).state == GAP


def test_ac1_09_template_secret_counterfact_is_reported_without_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.quality.acceptance_item_probe as probe_module

    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    fake_secret = "xoxb-" + "123456789012-" + "123456789012-" + "abcdefghijklmnopqrstuvwx"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        source = Path(argv[argv.index("--source") + 1])
        template = source / ".env.example"
        assert template.read_text(encoding="utf-8") == f"THS_API_KEY={fake_secret}\n"
        config_path = Path(argv[argv.index("--config") + 1])
        assert (
            tomllib.loads(config_path.read_text(encoding="utf-8"))
            .get("allowlist", {})
            .get("paths", [])
            == []
        )
        report = Path(argv[argv.index("--report-path") + 1])
        report.write_text(
            json.dumps(
                [
                    {
                        "RuleID": "slack-bot-token",
                        "File": ".env.example",
                        "StartLine": 1,
                        "Secret": fake_secret,
                        "Match": f"THS_API_KEY={fake_secret}",
                    }
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(probe_module.subprocess, "run", fake_run)
    code, summary = scan_env_template(f"THS_API_KEY={fake_secret}\n", config)

    assert code == 1
    assert "findings=1" in summary
    assert "slack-bot-token@.env.example:1" in summary
    assert fake_secret not in summary


def test_ac1_09_real_gitleaks_rejects_slack_shaped_template_token() -> None:
    tool = shutil.which("gitleaks")
    if tool is None:
        pytest.skip("gitleaks is required for the real template-token counterfactual")
    assert tool is not None
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    fake_token = "xoxb-" + "123456789012-" + "123456789012-" + "abcdefghijklmnopqrstuvwx"

    code, summary = scan_env_template(
        f"SLACK_BOT_TOKEN={fake_token}\n",
        config,
        tool=tool,
    )

    assert code == 1
    assert "findings=1" in summary
    assert "slack-bot-token@" in summary
    assert ".env.example:1" in summary
    assert fake_token not in summary


def test_ac1_08_registered_repair_and_counterfactuals_remain_reachable() -> None:
    ctx = Context(REPO_ROOT, (), {})
    probe = probe_for("AC-1|08")
    measured = measure_ac1_08(ctx)
    clean = resolve_repair(measured, probe.repair)

    assert probe.judge(clean).state == PROVEN
    for counterfact in probe.breaks:
        broken = {**clean, **dict(counterfact.facts)}
        assert probe.judge(broken).state == GAP, counterfact.label

    # The recipe has to stay reachable from the very reading it is meant to repair. An unparseable
    # or empty section 1 measures `rows=0` and cascades `uncovered` to the cited count, so a recipe
    # that inherited `rows` (the old `dated="*rows"` copied the *measured* rows) could never satisfy
    # `int(rows) > 0` -- a repair that cannot be repaired, and invisible here, because the healthy
    # reading above already passes.
    collapsed = {
        **measured,
        "table_valid": "no",
        "rows": "0",
        "dated": "0",
        "uncovered": measured["cited"],
    }
    assert probe.judge(resolve_repair(collapsed, probe.repair)).state == PROVEN


def test_ac1_05_runtime_counterfactuals_remain_reachable() -> None:
    """Each independent runtime or static break must still turn AC1|05 into a gap."""
    probe = probe_for("AC-1|05")
    clean = resolve_repair(
        {
            "config": "opendata / opendata_data",
            "env_example": "opendata / opendata_data",
            "compose": "opendata / opendata_data",
            "init_sql": "opendata / opendata_data",
            "runtime_endpoint": "http://127.0.0.1:33566",
            "runtime_image": "sha256:image",
            "runtime_source": "source",
            "runtime_health_database": "connected",
            "runtime_static_asset_count": "2",
            "runtime_frontend_identity": "verified",
        },
        probe.repair,
    )

    assert probe.judge(clean).state == PROVEN
    for counterfact in probe.breaks:
        broken = {**clean, **dict(counterfact.facts)}
        assert probe.judge(broken).state == GAP, counterfact.label


def test_ac5_07_current_security_proof_counterfactuals_remain_reachable() -> None:
    """Each required source, triage, and retained-risk condition can turn the judge red."""
    ctx = Context(REPO_ROOT, (), {})
    probe = probe_for("AC-5|07")
    clean = resolve_repair(measure_ac5_07(ctx), probe.repair)

    assert probe.judge(clean).state == PROVEN
    for counterfact in probe.breaks:
        broken = {**clean, **dict(counterfact.facts)}
        assert probe.judge(broken).state == GAP, counterfact.label


def playwright_report(statuses: dict[str, tuple[str, str]]) -> str:
    """Build the small JSON-reporter shape used by named E2E outcomes."""
    return json.dumps(
        {
            "suites": [
                {
                    "title": "scripts.spec.ts",
                    "suites": [
                        {
                            "title": "Authenticated",
                            "specs": [
                                {
                                    "title": PLAYWRIGHT_TITLES[key],
                                    "tests": [
                                        {
                                            "status": status,
                                            "results": [{"status": result}],
                                        }
                                    ],
                                }
                                for key, (status, result) in statuses.items()
                            ],
                        }
                    ],
                }
            ]
        }
    )


def test_playwright_outcomes_reads_named_nested_spec_results() -> None:
    """Both named specs need the expected status and a passing result."""
    output = playwright_report(
        {
            "merged_catalog": ("expected", "passed"),
            "function_detail": ("expected", "passed"),
        }
    )

    assert playwright_outcomes(output, PLAYWRIGHT_TITLES, 0) == {
        "merged_catalog": "passed",
        "function_detail": "passed",
    }


def test_playwright_outcomes_preserves_failed_and_skipped_statuses() -> None:
    """A failed or skipped named spec cannot be mistaken for a passing browser case."""
    output = playwright_report(
        {
            "merged_catalog": ("unexpected", "failed"),
            "function_detail": ("skipped", "skipped"),
        }
    )

    assert playwright_outcomes(output, PLAYWRIGHT_TITLES, 1) == {
        "merged_catalog": "failed",
        "function_detail": "skipped",
    }


def test_playwright_outcomes_preserves_ambiguous_and_runner_errors() -> None:
    """Duplicate named specs and invalid reporter output remain non-passing."""
    duplicate = json.loads(
        playwright_report(
            {
                "merged_catalog": ("expected", "passed"),
                "function_detail": ("expected", "passed"),
            }
        )
    )
    duplicate["suites"][0]["suites"][0]["specs"].append(
        duplicate["suites"][0]["suites"][0]["specs"][0]
    )

    assert playwright_outcomes(json.dumps(duplicate), PLAYWRIGHT_TITLES, 0) == {
        "merged_catalog": "ambiguous",
        "function_detail": "passed",
    }
    assert playwright_outcomes("not json", PLAYWRIGHT_TITLES, 7) == {
        "merged_catalog": "runner-exit=7",
        "function_detail": "runner-exit=7",
    }


def test_partition_matcher_requires_both_exact_queries_and_counts() -> None:
    """Only a one-row p2027 assertion and an empty pmax assertion prove placement."""
    source = """
class TestCrossYearWrite:
    def test_maintenance_extends_partitions_and_new_year_rows_land_alone(self, warehouse):
        with warehouse.begin() as connection:
            p2027_rows = connection.execute(
                text("SELECT COUNT(*) FROM `_probe_partition_maintenance` PARTITION (p2027)")
            ).scalar_one()
            pmax_rows = connection.execute(
                text("SELECT COUNT(*) FROM `_probe_partition_maintenance` PARTITION (pmax)")
            ).scalar_one()
        assert p2027_rows == 1
        assert pmax_rows == 0
"""
    wrong_pmax_count = source.replace("assert pmax_rows == 0", "assert pmax_rows == 1")
    missing_pmax_query = source.replace(
        """            pmax_rows = connection.execute(
                text("SELECT COUNT(*) FROM `_probe_partition_maintenance` PARTITION (pmax)")
            ).scalar_one()
""",
        "",
    )

    assert exact_partition_placement_assertions(source) == ("p2027", "pmax")
    assert exact_partition_placement_assertions(wrong_pmax_count) == ("p2027",)
    assert exact_partition_placement_assertions(missing_pmax_query) == ("p2027",)


def test_partition_census_reading_discloses_both_archives_without_claiming_freshness() -> None:
    """C57's under-registered subset stays visible beside C58's later full census."""
    ctx = Context(REPO_ROOT, (), {})

    reading = partition_census_archive_reading(ctx)

    assert "C57 historical subset" in reading
    assert "tables=20 / partitioned=6 / gap_tables=5" in reading
    assert "C58 (2026-09-28 13:40 +0200)" in reading
    assert "registered=53 / partitioned=9 / gap_tables=8" in reading
    assert "both are archived reads, not a fresh production census" in reading


def test_ac11_02_latest_official_run_with_hfq_errors_remains_a_gap() -> None:
    """The newest official run is 10 upstream errors, not the older C64 sample."""
    assert QFQ_OFFICIAL_RUN == "docs/evidence/C65/qfq-official-akshare-current.txt"

    facts = measure_ac11_02(Context(REPO_ROOT, (), {}))

    assert facts["adj_exit"] == "0"
    assert facts["adj_failed"] == "0"
    assert facts["adj_skipped"] == "0"
    assert (facts["run_ok_rows"], facts["run_fail_rows"], facts["run_error_rows"]) == (
        "0",
        "0",
        "10",
    )
    verdict = judge_ac11_02(facts)
    assert verdict.state == GAP
    assert "10 行 ERROR" in verdict.reason


def test_ac9_07_probe_measures_read_scope_and_diff_flag_contract() -> None:
    """Affected keys extend reads; behavior nodes prove diff flags follow values."""
    source = (REPO_ROOT / DWD_MERGE_REL).read_text(encoding="utf-8")

    chain_facts = _ac9_07_call_chain_facts(source)
    assert chain_facts == {
        "run_delegates_affected_keys": "yes",
        "batch_reader_gets_keys": "yes",
        "normal_reader_gets_keys": "yes",
        "scoped_reader_gets_keys": "yes",
        "reader_gets_keys": "yes",
        "batch_merges_source_frames": "yes",
        "batch_has_no_extra_diff_inputs": "yes",
        "diff_flag_has_binary_membership_shape": "yes",
        "partition_hook_resells_keys": "yes",
        "context_hook_resells_keys": "yes",
        "hook_resells_keys": "yes",
    }
    probe = probe_for("AC-9|07")
    run_count = str(len(DWD_REVISION_NODES))
    measured = {
        **chain_facts,
        "runs": run_count,
        "passed": run_count,
        "bad": "-",
        "runner_publishes_keys": "yes",
        "writer_upserts": "yes",
    }
    clean = resolve_repair(measured, probe.repair)

    assert probe.judge(clean).state == PROVEN
    for counterfact in probe.breaks:
        assert probe.judge({**clean, **dict(counterfact.facts)}).state == GAP


@pytest.fixture
def temporary_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use throwaway history with environment identities, without changing git configuration."""
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Acceptance probe test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "acceptance-probe@example.test")

    def git(*args: str) -> str:
        code, output = run_argv(["git", *args], cwd=tmp_path)
        assert code == 0, output
        return output.strip()

    git("init", "--quiet", "--initial-branch=main")
    return tmp_path, git


@pytest.fixture
def clean_history_facts() -> tool.Facts:
    """Reachable commits need not all produce fragments: 588 / 587 is a valid reading."""
    return resolve_repair(
        {
            "scanner_pin": "8.30.1",
            "argv_tokens": "gitleaks detect --redact",
            "reads_scanner_root": "yes",
            "git_commits_head": "588",
            "git_commits_all": "588",
            "fragment_population": "587",
            "bytes_scanned": "100",
            "allowlist_global": "1",
            "allowlist_global_regexes": "1",
            "allowlist_rules": "2",
            "allowlist_rule_regexes": "2",
            "pair_samples": "1",
            "pair_variants": "4",
            "tree_pair_lines": "1",
            "tree_files_read": "1",
            "tree_files_skipped": "0",
            "tree_pair_stale": "0",
            "tree_pair_changed": "0",
            "control_shipped_exit": "1",
            "control_stripped_exit": "1",
        },
        tool.AC16_09.repair,
    )


def test_fragment_census_counts_all_ref_text_diffs_not_nonfragments(temporary_git) -> None:
    root, git = temporary_git
    expected: set[str] = set()

    def commit(label: str, *, fragment: bool) -> str:
        git("add", "--all")
        git("commit", "--quiet", "--allow-empty", "-m", label)
        sha = git("rev-parse", "HEAD")
        if fragment:
            expected.add(sha)
        assert tool.fragment_population(root) == str(len(expected)), label
        return sha

    text = root / "text.txt"
    text.write_text("one\ntwo\n", encoding="utf-8")
    (root / "rename.txt").write_text("unchanged\n", encoding="utf-8")
    base = commit("text add", fragment=True)
    text.write_text("one\ntwo changed\nthree\n", encoding="utf-8")
    commit("text modify", fragment=True)
    text.write_text("one\n", encoding="utf-8")
    commit("delete lines without additions", fragment=True)
    text.unlink()
    commit("whole text file deletion", fragment=False)
    binary = root / "artifact.bin"
    binary.write_bytes(b"\x00binary one")
    commit("binary add", fragment=False)
    binary.write_bytes(b"\x00binary two")
    commit("binary modify", fragment=False)
    binary.unlink()
    commit("binary deletion", fragment=False)
    (root / "empty.txt").touch()
    commit("empty file add", fragment=False)
    commit("empty commit", fragment=False)
    git("mv", "rename.txt", "renamed.txt")
    commit("pure rename", fragment=False)
    (root / "renamed.txt").chmod(0o755)
    commit("mode only", fragment=False)
    head = git("rev-parse", "HEAD")
    git("switch", "--quiet", "-c", "credential-side", base)
    (root / "credential.txt").write_text(
        "Authorization: Bearer ghp_" + "0123456789abcdef" * 2 + "0123\n", encoding="utf-8"
    )
    branch = commit("credential on non-HEAD branch", fragment=True)
    git("switch", "--quiet", "main")
    assert git("rev-parse", "HEAD") == head
    assert branch not in git("rev-list", "HEAD").splitlines()
    assert branch in git("rev-list", "--all").splitlines()
    assert tool.fragment_population(root) == str(len(expected)) == "4"
    assert int(git("rev-list", "--count", "--all")) > len(expected)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "commit:bad\n1\t0\ttext.txt\n",
        "1\t0\ttext.txt\n",
        "commit:" + "a" * 40 + "\n1\t0\n",
        "commit:" + "a" * 40 + "\n1\t0\t\n",
        "commit:" + "a" * 40 + "\n-\t1\ttext.txt\n",
        "commit:" + "a" * 40 + "\n1\tnan\ttext.txt\n",
        "commit:" + "a" * 40 + "\n1\t0\ttext.txt\nunexpected stderr\n",
    ],
)
def test_fragment_census_rejects_malformed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(tool, "run_argv", lambda argv, **kwargs: (0, output))
    assert tool.fragment_population(tmp_path) == "(unread)"


def test_fragment_census_deduplicates_shas_and_fails_closed_on_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    output = f"commit:{sha}\n0\t1\ttext.txt\ncommit:{sha}\n1\t0\tother.txt\n"
    monkeypatch.setattr(tool, "run_argv", lambda argv, **kwargs: (0, output))
    assert tool.fragment_population(tmp_path) == "1"
    monkeypatch.setattr(tool, "run_argv", lambda argv, **kwargs: (128, output))
    assert tool.fragment_population(tmp_path) == "(unread)"


def test_history_judge_matches_independent_census_exactly(clean_history_facts: tool.Facts) -> None:
    assert tool.judge_ac16_09(clean_history_facts).state == PROVEN
    for field, value in (
        ("commits_scanned", "586"),
        ("commits_scanned", "588"),
        ("commits_scanned", "(unread)"),
        ("fragment_population", "(unread)"),
        ("fragment_population", "0"),
        ("git_commits_head", "0"),
        ("git_commits_all", "0"),
        ("git_commits_all", "586"),
        ("git_commits_all", "(unread)"),
        ("git_shallow", "true"),
        ("git_shallow", "(unread)"),
        ("argv_scope_limits", "1"),
    ):
        assert tool.judge_ac16_09({**clean_history_facts, field: value}).state == GAP, field
    assert (
        tool.judge_ac16_09(
            {**clean_history_facts, "report_items": "1", "stderr_leaks": "1", "scan_exit": "1"}
        ).state
        == GAP
    )


def test_history_self_test_still_reaches_proven_and_checks_all_breaks(
    monkeypatch: pytest.MonkeyPatch, clean_history_facts: tool.Facts
) -> None:
    monkeypatch.setattr(tool, "PROBES", (tool.AC16_09,))
    findings, reading = tool.self_test_findings(
        tool.load_context(), {tool.ITEM: {**clean_history_facts, "commits_scanned": "586"}}, {}
    )
    assert findings == []
    assert "1/1" in reading
    assert f"{len(tool.AC16_09.breaks)} 条" in reading


@pytest.mark.parametrize(
    ("case", "scope"),
    [
        ("complete", ""),
        ("repaired", ""),
        ("no-origin", ""),
        ("origin-gone", ""),
        ("scoped", "--log-opts=HEAD"),
    ],
)
def test_history_measure_keeps_scan_argv_and_rejects_shallow_or_scoped_history(
    temporary_git,
    monkeypatch: pytest.MonkeyPatch,
    clean_history_facts: tool.Facts,
    case: str,
    scope: str,
) -> None:
    """The pinned argv, and a chain the measure completes *before* judging it.

    ``repaired`` is the hosted ``pull_request`` shape this cell kept flipping on: the workspace
    arrived shallow with a reachable origin, so the measure completes the chain and the scanner's
    commit count and the independent census agree on 2. ``no-origin`` and ``origin-gone`` are the
    fail-closed pair -- with nothing fetchable behind it the cell keeps the truncated 1-fragment
    reading, keeps ``shallow = true``, and is still a gap. The repair changes what history the
    measurement can see; it never changes what the judge accepts.
    """
    root, git = temporary_git
    text = root / "text.txt"
    for value in ("first\n", "second\n"):
        text.write_text(value, encoding="utf-8")
        git("add", "text.txt")
        git("commit", "--quiet", "-m", value.strip())
    truncated = case in {"no-origin", "origin-gone"}
    scan_root = root
    if case != "complete" and case != "scoped":
        scan_root = root / "shallow"
        git("clone", "--quiet", "--depth=1", root.as_uri(), str(scan_root))
    if case == "no-origin":
        git("-C", str(scan_root), "remote", "remove", "origin")
    if case == "origin-gone":
        git("-C", str(scan_root), "remote", "set-url", "origin", (root / "gone").as_uri())
    (scan_root / tool.CONFIG_REL).write_text(
        (REPO_ROOT / tool.CONFIG_REL).read_text(encoding="utf-8"), encoding="utf-8"
    )
    manifest = tool.script_module(tool.SCANNER_REL).load_manifest()
    argv = [*manifest.scan_argv, *([scope] if scope else [])]
    mod = SimpleNamespace(
        REPO_ROOT=scan_root,
        load_manifest=lambda: SimpleNamespace(scan_argv=argv, gitleaks_version="8.30.1"),
        local_tool_version=lambda manifest: "8.30.1",
        check_ci_wiring=lambda manifest: [],
    )
    monkeypatch.setattr(tool, "script_module", lambda rel: mod)
    monkeypatch.setattr(
        tool,
        "_pair_census",
        lambda *args: {
            "exempted": 1,
            "files_read": 1,
            "files_skipped": 0,
            "stale": 0,
            "changed": 0,
            "pairs": [("docs/fixture.txt", "a" * 64)],
        },
    )
    monkeypatch.setattr(
        tool,
        "_control_scan",
        lambda *args: {
            key: value
            for key, value in clean_history_facts.items()
            if key.startswith("control_") or key == "allowlist_blocks"
        },
    )

    def fake_scan(command, *, cwd):
        if command[0] == "git":
            return run_argv(command, cwd=cwd)
        assert command == [
            *argv,
            "--report-format",
            "json",
            "--report-path",
            command[-1],
        ]
        Path(command[-1]).write_text("[]", encoding="utf-8")
        count = 1 if truncated else 2
        return 0, f"{count} commits scanned\nscanned ~100 bytes\nno leaks found\n"

    monkeypatch.setattr(tool, "run_argv", fake_scan)
    facts = tool.measure_ac16_09(Context(scan_root, tool.load_context().doc_items, {}))
    assert (
        facts["history_repair"]
        == {
            "complete": "already-complete",
            "repaired": "unshallowed",
            "no-origin": "refused",
            "origin-gone": "refused",
            "scoped": "already-complete",
        }[case]
    )
    assert facts["fragment_population"] == ("1" if truncated else "2")
    assert facts["git_shallow"] == ("true" if truncated else "false")
    assert facts["argv_scope_limits"] == ("1" if scope else "0")
    assert tool.judge_ac16_09(facts).state == (GAP if truncated or scope else PROVEN), facts


def test_ensure_history_complete_repairs_a_reachable_chain_and_refuses_without_one(
    temporary_git,
) -> None:
    """One origin reachable -> the chain lands complete; none -> the repair says refused, not pass.

    This is the pair the two flipping cells hinge on: ``AC-16|09`` may only call a scan
    full-history when the repository really holds the history, and ``AC-19|02`` dates an archive's
    first landing off that chain, so a truncated one makes the record look like it landed today.
    The repair is allowed to fix the first problem and is not allowed to fake the second.
    """
    root, git = temporary_git
    text = root / "text.txt"
    for value in ("first\n", "second\n", "third\n"):
        text.write_text(value, encoding="utf-8")
        git("add", "text.txt")
        git("commit", "--quiet", "-m", value.strip())

    def shallow(root_: Path) -> str:
        return run_argv(["git", "rev-parse", "--is-shallow-repository"], cwd=root_)[1].strip()

    def head_count(root_: Path) -> str:
        return run_argv(["git", "rev-list", "--count", "HEAD"], cwd=root_)[1].strip()

    assert tool.ensure_history_complete(root) == "already-complete"

    reachable = root / "reachable"
    git("clone", "--quiet", "--depth=1", root.as_uri(), str(reachable))
    assert shallow(reachable) == "true"
    assert head_count(reachable) == "1"
    assert tool.ensure_history_complete(reachable) == "unshallowed"
    assert shallow(reachable) == "false"
    assert head_count(reachable) == "3"
    assert tool.ensure_history_complete(reachable) == "already-complete"

    stranded = root / "stranded"
    git("clone", "--quiet", "--depth=1", root.as_uri(), str(stranded))
    git("-C", str(stranded), "remote", "remove", "origin")
    assert tool.ensure_history_complete(stranded) == "refused"
    assert shallow(stranded) == "true"
    assert head_count(stranded) == "1"

    gone = root / "gone"
    git("clone", "--quiet", "--depth=1", root.as_uri(), str(gone))
    git("-C", str(gone), "remote", "set-url", "origin", (root / "never-existed").as_uri())
    assert tool.ensure_history_complete(gone) == "refused"
    assert shallow(gone) == "true"


def test_history_repair_note_names_the_provenance_in_every_reading_it_serves() -> None:
    """A reading says where its history came from, including when no repair fact was recorded."""
    assert "unshallowed" in tool.history_repair_note({"history_repair": "unshallowed"})
    assert "(未记录)" in tool.history_repair_note({})


def test_evidence_deletions_excludes_only_bytecode_from_both_sets(
    temporary_git, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, git = temporary_git
    evidence = root / tool.EVIDENCE_DIR
    evidence.mkdir(parents=True)
    for name, contents in (
        ("generated.pyc", b"\x00bytecode"),
        ("generated.pyo", b"\x00bytecode"),
        ("removed.txt", b"text evidence\n"),
        ("removed.json", b"{}\n"),
        ("removed.bin", b"\x00binary evidence"),
        ("retained.txt", b"retained evidence\n"),
        ("retained.pyc", b"\x00retained bytecode"),
        ("retained.pyo", b"\x00retained bytecode"),
    ):
        (evidence / name).write_bytes(contents)
    git("add", tool.EVIDENCE_DIR)
    git("commit", "--quiet", "-m", "evidence and accidental caches")
    for name in ("generated.pyc", "generated.pyo", "removed.txt", "removed.json", "removed.bin"):
        git("rm", "--quiet", f"{tool.EVIDENCE_DIR}/{name}")
    git("commit", "--quiet", "-m", "remove caches and real archives")
    monkeypatch.setattr(tool, "run_argv", lambda argv: run_argv(argv, cwd=root))
    assert tool.evidence_deletions() == ("3", "1")


@pytest.mark.parametrize(
    ("old", "new", "failed_facts"),
    [
        (
            "affected_keys=affected_keys,",
            "affected_keys=(),",
            ("run_delegates_affected_keys", "reader_gets_keys"),
        ),
        (
            "                set(affected_keys),",
            "set(),",
            ("batch_reader_gets_keys", "reader_gets_keys"),
        ),
        (
            "return reader(start, end, affected_keys)",
            "return reader(start, end, set())",
            ("normal_reader_gets_keys", "reader_gets_keys"),
        ),
        (
            "                affected_keys,\n                symbols=symbols,",
            "                set(),\n                symbols=symbols,",
            ("scoped_reader_gets_keys", "reader_gets_keys"),
        ),
        (
            "            merged_at=merged_at,\n        )",
            "            merged_at=merged_at,\n"
            "            extra_diff_keys=frozenset(affected_keys),\n        )",
            ("batch_has_no_extra_diff_inputs",),
        ),
        (
            "            merged_at=merged_at,\n        )",
            "            merged_at=merged_at,\n"
            '            **{"extra_diff_keys": frozenset(affected_keys)},\n        )',
            ("batch_has_no_extra_diff_inputs",),
        ),
        (
            "            merged_at=merged_at,\n        )",
            "            merged_at=merged_at,\n            **merge_options,\n        )",
            ("batch_has_no_extra_diff_inputs",),
        ),
        (
            'record["_diff_flag"] = 1 if biz_key in flagged else 0',
            'record["_diff_flag"] = 1 if True else 0',
            ("diff_flag_has_binary_membership_shape",),
        ),
        (
            "affected_keys=self._contract_keys(partition),",
            "affected_keys=set(),",
            ("partition_hook_resells_keys", "hook_resells_keys"),
        ),
        (
            "affected_keys=self._contract_keys(context),",
            "affected_keys=set(),",
            ("context_hook_resells_keys", "hook_resells_keys"),
        ),
    ],
    ids=(
        "run-batch-delegation",
        "batch-reader",
        "ordinary-reader",
        "scoped-reader",
        "affected-key-forced-diff",
        "expanded-extra-diff-kwargs",
        "unknown-expanded-kwargs",
        "diff-flag-expression-shape",
        "partition-hook",
        "context-hook",
    ),
)
def test_ac9_07_key_flow_probe_rejects_broken_call_paths(
    old: str,
    new: str,
    failed_facts: tuple[str, ...],
) -> None:
    """Each missing propagation edge is rejected against the actual source shape."""
    source = (REPO_ROOT / DWD_MERGE_REL).read_text(encoding="utf-8")
    assert source.count(old) == 1

    facts = _ac9_07_call_chain_facts(source.replace(old, new, 1))

    assert all(facts[name] == "no" for name in failed_facts)
