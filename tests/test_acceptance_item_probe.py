"""Tests for exact acceptance-probe evidence matchers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import tomllib

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
    assert facts["rows"] == "13"
    assert facts["dated"] == facts["rows"] == "13"
    assert facts["undecided"] == "0"
    assert facts["unlinked"] == "0"
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
    assert set(rows) == set(range(1, 14))
    assert {row_id: rows[row_id]["数据源"] for row_id in historical_sources} == (historical_sources)
    assert rows[12]["数据源"] == "BLS（美国劳工统计局）"
    assert rows[13]["数据源"] == "FMP（EquityHistorical / EquityQuote）"

    restricted = "已复核（受限）"
    not_applicable = "不适用（本迭代未启用）"
    assert [rows[row_id]["状态"] for row_id in range(1, 11)] == [restricted] * 10
    assert rows[11]["状态"] == not_applicable
    assert rows[12]["状态"] == restricted
    assert rows[13]["状态"] == restricted
    assert {rows[row_id]["复核日期"] for row_id in range(1, 12)} == {"2026-09-30"}
    assert rows[12]["复核日期"] == rows[13]["复核日期"] == "2026-10-08"

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
    assert "没有释放 FMP 的真实采集、落库或对外分发动作" in registry

    verdict = judge_ac1_08(facts)
    assert "登记完整性不等于所有用途获准" in registry
    assert "全部获准" not in " ".join(verdict.readings)
    assert verdict.state == PROVEN


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
    probe = probe_for("AC-5|02")

    assert probe.judge(probe.repair).state == PROVEN
    scope_break = next(item for item in probe.breaks if "路径/批次" in item.label)
    assert probe.judge({**probe.repair, **dict(scope_break.facts)}).state == GAP


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
    clean = resolve_repair(measure_ac1_08(ctx), probe.repair)

    assert probe.judge(clean).state == PROVEN
    for counterfact in probe.breaks:
        broken = {**clean, **dict(counterfact.facts)}
        assert probe.judge(broken).state == GAP, counterfact.label


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
    probe = probe_for("AC-5|07")
    clean = resolve_repair(
        {
            "scan_findings": "1048",
            "archive": "docs/evidence/C65/ported-bandit-scan.json",
            "target_recipe": "bandit -r opendata_http",
            "daily_excludes_ported": "yes",
            "bandit_exclude_dirs": "opendata_http",
            "security_issue_summary": "-",
            "archive_produced_by": "1.9.4",
            "archive_generated_at": "2026-10-01T11:30:00+00:00",
            "scanner_exit": "1",
            "scan_source_files_verified": "325",
            "scan_python_files": "325",
            "scan_source_files_saved": "325",
            "scan_source_hash": "a" * 64,
            "scan_files": "241",
            "scan_rules": "15",
            "scan_rule_names": "B301, B307",
            "scan_high_findings": "31",
            "triage_findings": "1048",
            "triage_rules": "15",
            "ported_files": "325",
        },
        probe.repair,
    )

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


def test_ac9_07_actual_merge_flow_passes_affected_keys_through_all_paths() -> None:
    """The current run, batch reader/diff, and both hook branches preserve keys."""
    source = (REPO_ROOT / DWD_MERGE_REL).read_text(encoding="utf-8")

    chain_facts = _ac9_07_call_chain_facts(source)
    assert chain_facts == {
        "run_delegates_affected_keys": "yes",
        "batch_reader_gets_keys": "yes",
        "batch_merge_gets_keys": "yes",
        "normal_reader_gets_keys": "yes",
        "scoped_reader_gets_keys": "yes",
        "reader_gets_keys": "yes",
        "keys_extend_diffs": "yes",
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


@pytest.mark.parametrize(
    ("old", "new", "failed_facts"),
    [
        (
            "affected_keys=affected_keys,",
            "affected_keys=(),",
            ("run_delegates_affected_keys", "reader_gets_keys", "keys_extend_diffs"),
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
            "extra_diff_keys=frozenset(affected_keys),",
            "extra_diff_keys=frozenset(),",
            ("batch_merge_gets_keys", "keys_extend_diffs"),
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
        "batch-diff",
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
