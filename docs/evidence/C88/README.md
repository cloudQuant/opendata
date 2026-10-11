# C88 — 一行闭环（cboe-EquitySearch）与「词汇就绪」的三类等

本轮全部离线：未发一个请求、未设 `OPENDATA_ALLOW_LIVE_E2E`、无任何 DDL/DML、未改
`bandit.yaml`/`pyproject.toml`/覆盖率配置/`scripts/quality/acceptance_item_probe.py`。
正文判定见 [开发验收记录 §42](../../迭代计划/迭代2-统一Provider架构与全量能力补齐/开发验收记录.md)。

## 命令与读数

| # | 命令 | 本轮读数 |
| - | ---- | -------- |
| 1 | `python scripts/quality/declaration_provenance.py --source cboe` | 补注册前 `FAIL DOMAIN_UNREGISTERED`；补完后 `models=4 findings=4 violations=0 … VERDICT: DECLARATION PROVENANCE HOLDS` |
| 2 | `python -m pytest -q -p no:randomly --no-cov -o addopts=""`（§42 点名的 9 个注册面文件） | `222 passed` |
| 3 | `python scripts/quality/model_capability_census.py --check` | `--check verdict: PASS`（分母仍 238） |
| 4 | `python scripts/quality/model_capability_census.py --json …/capability-roadmap.json --round-id C88` | 只 `declaration_scan` 与四项 `production_declarations` 变；`roadmap`/`census` 两节与原档案 `==` 成立 |
| 5 | `python docs/evidence/C88/reconcile-ready-rows.py`（本轮新建仪器） | `238 = 3 空需求 + 14 词汇就绪`；14 行分区 `SHIPPED 1 / BLOCKED 9 / NO_PACKAGE 4 / UNREFUSED 0`，逐行 verbatim 拒答见 `reconciliation-face.txt` |
| 6 | `python scripts/codemod/verify_no_akshare.py` | 改前 `FAIL: archived 327 but walks 328`；`--update` 重冻后 `OK: 655 file(s) walked (opendata=328, _vendor=325, opendata_client=2)`，冻结引用数仍 0 |
| 7 | `python scripts/quality/ac2_case_probe.py --self-test` | 重冻前 `SELF_TEST FAIL failures=1`（AC2-07 `closure=gap`，`zero_dep_exit=1`）；重冻后 `SELF_TEST PASS failures=0`，25 例 `breaks==flipped`（合计 153 条反例） |
| 8 | 全量离线套件 `python -m pytest -q -p no:randomly --no-cov --timeout=900` | `4 failed / 22249 passed / 181 skipped in 481.56s`；`collected 22434 items` 而 `22249 + 181 + 4 == 22434` 成立，且本轮实跑命令行没有 `-x`（§41 的错误认领已 revert）；四条逐条重测后 `38 passed`（见 §42），第二遍全量待写者波落地后再跑 |
| 9 | `ruff check` + `ruff format --check`（本轮所有触及文件，含本目录仪器） | `All checks passed!` / `1 file already formatted` |

## 反例臂（两-sided，正向对照必须能不同）

- **库存守卫**：`tests/test_openbb_inventory_guard.py` 的 live 断言是
  `assert [str(finding) for finding in found] == []`；同文件的对照臂（剔除权利表 BLS/FMP 两行）
  在补 `registered_capability_count` 两个格子前报 **4** 条、补后报 **2** 条，即被实测消掉的是
  两条 `CAPABILITY COUNT`（`49 vs 50`、cboe `3 vs 4`），剩下的两条 `RIGHTS LINK` 是那臂自己造的。
  `rights_rows` 与 `auto_routable_capability_count` 一个字未改，`providers` 行逐 provider 求和
  实测 `== 50 ==` 顶层值。
- **修剪契约**：`tests/test_cboe_equity_search.py` 的 `CSV_BODY` 保留 cboe 上游表头的前导空格
  `" Stock Symbol, …"`，因为引擎 `_engine/decoders.py:327` 的 `_header_names` 会
  `cell.strip().lstrip("\ufeff")`；声明若按上游原样写 `source_key`，实跑读回
  `CBOE_SHAPE_INVALID missing=['symbol']`（这条就是本轮的真失败面，不是构造的反例）。
- **拒答码**：三条拒答臂分别点名引擎发布的 `EMPTY_BODY`（空体）、`DELIMITER_MISMATCH`
  （修剪后只剩一列）、`HEADER_MISMATCH`（真表却一个声明列都不发布）。主代理最初写的
  `EMPTY_TABLE` 不是空体路径的发布码，按引擎面值改码，而不是挑一个能过的断言。
- **仪器自身**：`reconcile-ready-rows.py` 的 `UNREFUSED == 0` 有可达见证——四类里三类非零，
  且 intrinio/tmx 四行一旦建包而不再写拒答就会搬进 `UNREFUSED`；只走 `ast.Assign` 的第一版
  会把 9 条 OECD 拒答读空，那才是假零，故现版本同时扫 `ast.Assign` 与 `ast.AnnAssign`。

## 本轮推翻的一条旧断言

§41 里「这次不带 `-x`」是关于命令行的事实句，而该档案不记录 argv（pytest 首屏只打印
platform/plugins 与 collected 数），已就地 revert 成「按运行记录认领」：实跑命令含 `-x`，
且本轮失败数为 0，段末 `22215 + 170 == 22385 collected` 的恒等式证明没有任何一截被提前剪掉。

## 未做（不计入完成数）

9 行 OECD 的 census 需求集仍未按 cite 收紧（#44/#45 第三次确认）；四个 wave-1 内核仍是
「模块在树上、能力面未接线」（`present=False` 为正确读数，见 §42）；intrinio 3 行与 tmx 1 行
仍 `NO_PACKAGE`；`fmp/fred/ecb/bls` 的行仍无 census 文件（#70）；350 行 `live_verification_status`
仍全 `NOT_RUN`（#74，授权阻塞）；门禁第 4 遍未跑（#66）。
