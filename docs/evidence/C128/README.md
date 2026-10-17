# C128 — 波 23 的前置测量：plane 1 剩下的 90 行，生产运行到底到得了哪几枚

日期：2026-10-17　分支：`dev`　基线提交：`f4967950`（波 22）
解释器：`/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python` CPython 3.13（门禁解释器）

结论先行：**残留 90 行不是"一类工作"，是两类**，而且划分依据不是"有没有人在这家跑过仪器"，是
"跑一遍仪器过不过"。两台探针把这条判据落成五列可读的面：

- `readiness_census.py` 五列 `bound / declared / frozen_module / is_domain_contract / witness_entry`，
  桶序 NEED_BINDING → NEED_DECLARE → DECLARE_NOT_FROZEN → LOOK_ALIKE → NEED_SHARD_ENTRY → PRODUCIBLE_NOW。
  波前读数 `residue=90 bound=58 buckets={'NEED_BINDING':32,'NEED_DECLARE':58}`，四列声明位实测全 0，
  即**没有一枚残留行声明了契约类，也没有一枚有见证条目**；三臂 `readiness_arms=3 passed=3 failed=0`，
  `identity_set=5168e1e723999312 expected_match=True`。臂 B 的"移得动"是对无扰动分桶**实测**的：
  `forced_off_need_declare=58`、`forced_on_need_declare=0`。
- `declare_candidates.py` 只给 NEED_DECLARE 那 58 枚定价：
  `counts={'contract_in_module':56,'emits':35,'witness_entry':0}`，government_us 全 5 枚判
  `DECLARE_AND_WITNESS`。它是**排序**仪器，不判定任何行 —— 判定权在 producer 运行。

本波据此吃掉 government_us 2 枚（`TreasuryAuctions`、`WeatherBulletin`：各补一行 `row_model` 声明 +
首份该 provider 见证模块的两条 `client_seam` 反例），五相 10 枚 claim 一次全 PASS，
plane 1 `260 → 262/350`，残留 `90 → 88`（`UNREVIEWED 83 → 81`、`INCOMPLETE` 仍 7），`identity_set` 未动。
波后同一台 census 读 `residue=88 bound=56 buckets={'NEED_BINDING':32,'NEED_DECLARE':56}`、
`readiness_failed=0`。叙述与归因（含 §84.10 那条 federal_reserve 谓词如何被本波仪器推翻、
以及定价仪器自己的三处假面）在 `开发验收记录.md` §85。

## 载体

- `probes/readiness_census.py`、`probes/declare_candidates.py`：两台仪器，各自带正控制与可动性闸。
- `probes/payload_secret_scan.py`：入库前的凭证扫描双臂仪器（见下）。
- `runs/readiness_census.txt`：波前面（`census_exit=0`、`readiness_arms=3 readiness_passed=3`、stderr 0 字节）。
- `runs/declare_candidates.txt`：58 枚定价面（64 行，末三行依次是 `declare_arms=2 declare_passed=2
  declare_failed=0`、`declare_exit=0`、`--- stderr bytes=0 (0 means no traceback) ---`）。**列序**是
  `row <identity> by_name= output_model= flags= contract_module= fetcher= canonical= contract=`：
  原来的 `row <identity>: fetcher=<类名>` 形状会让 gitleaks `generic-api-key` 把公开类名当凭证
  （命中 `KeyExecutives` 那一行），而放宽 `.gitleaks.toml` 会撞上
  `tests/test_acceptance_item_probe.py:707-731` 冻结的 allowlist 常量 —— 判定与豁免都不动，只改本轮
  仪器自己的列序，8 个字段一个不少。
- `runs/readiness_census_after_batch.txt`：本波入库面（`census_after_exit=0`、stderr 0 字节）。
- `runs/payload_secret_scan.txt`：列序改动没把扫描器改哑的证据面 —— 臂 A `arm_a_findings=0`，臂 B 往
  **同一份改过列序的**载体里插一枚运行时生成的 Slack 形状 token，`arm_b_findings=1`
  （`payload_secret_face PASS=True`、`scan_exit=0`、stderr 0 字节；假 token 只在临时目录，不入库）。

复算配方（仓库根）：

```sh
PYTHONPATH=. /Users/yunjinqi/opt/anaconda3/envs/py313/bin/python \
    docs/evidence/C128/plane1-residue-readiness-20261017/probes/readiness_census.py
```

`declare_candidates.py` 那份是**波前 58 枚**的面，在当前工作树上重跑只会读到 56 枚并少掉 government_us
两枚；要复现它得在波 22 状态跑：

```sh
git worktree add --detach /tmp/c128_base f4967950
# 把 probes/declare_candidates.py 放到同相对路径，然后
cd /tmp/c128_base && PYTHONPATH=. <门禁解释器> docs/evidence/C128/plane1-residue-readiness-20261017/probes/declare_candidates.py
```

`payload_secret_scan.py` 无参数，直接 `python docs/evidence/C128/.../probes/payload_secret_scan.py`
（需要 `gitleaks` 在 PATH，否则打印 `SKIPPED=1`）。

三台仪器都只做普查：除自己的臂失败外退出 0，绝不改动任何被判定的面。
