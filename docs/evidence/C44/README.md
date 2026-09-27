# C44 — AC-17|08「反空壳抽审」：把三问做成能算、能红、能复算的面

判据原文（`验收文档.md:268`）：

> 反空壳抽审：无自指测试、无单断言空壳、无 `inspect.getsource` 形式检查；T1 模块具备错误翻译真实信封样例 ≥3 + 黄金向量 + normalize 真实报文 ≥3

这一格在台账里挂了 43 轮 `unreviewed`，因为它是**抽审**：没有判据面，只有「人看一眼」。
本轮先把「看一眼」拆成可算的三问，再按读数修——三问各自的回答面是：

| 判据原文的半句 | 机器可算的面 | 读数（修后） |
| --- | --- | --- |
| 无自指测试 / 无单断言空壳 / 无 `inspect.getsource` 形式检查 | `docs/evidence/C44/shell_audit.py`（§5.1 四类规则，全树 AST） | 四类命中 `0/0/0/0`，`files_scanned=161` |
| 错误翻译：真实响应信封样例 ≥3（含成功不抛） | `tests/test_fuyao_recorded_envelopes.py` + 联调录制夹具（§5.2 T1①） | `t1_error_cases=6`、`t1_success_cases=9` |
| 黄金向量：认证头/参数构造断言预计算值 | `tests/fuyao_golden.py` + `golden_millis.py`（三种独立推导 × 12 条线上锚点） | `t1_golden_days=24` |
| normalize：真实报文逐字段断言 ≥3 | 录制报文 → `normalize_bars/instruments/calendar`（§5.2 T1③） | `t1_normalize_cases=4` |

## 一、修之前先量：HEAD 树 19 处命中、T1 三问全为零

`census-head.txt` 是对 `git archive HEAD` 解出的完整树（`/tmp/c44_head`）跑的，不是对工作树跑的：

```
shell_count[self-reference] = 4      shell_count[constant-shell] = 13
shell_count[vacuous-assert] = 1      shell_count[source-form-check] = 1
files_scanned = 160   fixture_cases = 0   provenance = absent
t1_error_cases = 0    t1_success_cases = 0    t1_normalize_cases = 0    t1_golden_days = 0
VERDICT shells=found t1=gap exit=1
```

两类读数同样重要：**19 处空壳**（12 个文件）说明「无自指/无空壳」当时是假的；
**T1 四项全零**说明「真实信封」连一个样本都不存在——仓库里从未落过一份扶摇报文，
错误翻译用例的输入全是测试自己写的 `{"code": 2001}`。§5.2 末尾那句
「禁止手造理想报文替代真实样本」在 C44 之前是无从谈起的。

## 二、19 处逐条改成了什么（`census-head.txt` 的 HIT 行 ↔ 新用例名）

| 文件:行 | 规则 | 原来断的是 | 现在断的是 |
| --- | --- | --- | --- |
| test_config.py:25 | constant-shell | `settings.app_name == 'opendata'` | `test_app_name_is_the_name_the_service_shows_the_operator`：这个名字出现在给运维看的报告里 |
| test_config.py:66 | constant-shell | `settings.algorithm == 'HS256'` | `test_algorithm_is_the_one_the_signer_uses`：签名器真的按它签，换算法即红 |
| test_core.py:301 / :330 | constant-shell | 同上两项的重复抄写 | `test_settings_app_name_reaches_the_cli_report` / `test_settings_algorithm_refuses_other_algorithms` |
| test_contract_models.py:240 | constant-shell | `Bar.model_config['extra'] == 'forbid'` | `test_every_contract_model_refuses_an_undeclared_field`：9 个模型逐个喂陌生列 + 钉住 strict 来自共同基类而非各家副本 |
| test_diff_report.py:65 | constant-shell | `DQ_DIFF_REPORT_TABLE == 'dq_diff_report'` | `test_report_writer_and_retention_share_the_named_table`：写入与保留策略读到同一张表 |
| test_dump_import.py:214 | constant-shell | `FUYAO_SOURCE == 'ths'` | `test_source_label_decides_the_ods_table`：标签决定落哪张 ODS 表 |
| test_fuyao_dumps.py:205 | constant-shell | `DEFAULT_MAX_DUMP_BYTES == 512*1024*1024` | `test_default_cap_admits_the_real_dump_size`：真实 dump 尺寸过得到默认上限 |
| test_fuyao_endpoints.py:324/325/447/1622 | self-reference | 本地 `_ms()` 重算毫秒戳再断言生产函数（`_ms` 已删除） | 断言写预计算字面量，期望值不再来自被测实现（`tests/fuyao_golden.py` 承载表） |
| test_models_extended.py:28/29 | constant-shell | `UserRole.ADMIN.value == 'admin'` | `test_stored_role_strings_are_what_the_checker_uses`：权限检查器按这两个串放行/拒绝 |
| test_ods_writer.py:488 | constant-shell | `DEFAULT_BATCH_SIZE == 50000` | `test_batch_size_defaults_to_the_design_granularity`：50001 行（预计算）走默认配置正好 2 批 |
| test_retry_service.py:33/39 | constant-shell | `BASE_RETRY_DELAY == 60` / `MAX_RETRY_DELAY == 3600` | `test_backoff_ladder_plateaus_at_the_ceiling`：退避阶梯逐项算到上限后持平 |
| test_scheduler_details.py:38 | vacuous-assert | `assert True` | 真的断言 shutdown 之后 `scheduler.running is False` |
| test_sina_window_fidelity.py:197 | source-form-check | `inspect.getsource` 读源码文本 | `test_every_akshare_leg_is_triaged_by_what_it_asks_upstream`：用假 transport 观测每条腿**发给上游的窗口参数**来分类 |

普查范围外顺手补的两处同类（规则没抓到，行为反事实抓到了）：

- `test_ods_writer.py` 的替换版一度写成 `rows = DEFAULT_BATCH_SIZE + 1`——**又一个自指**，
  默认值改成 10 万它照绿。`behaviour_counterfactuals.py` 正是把默认值改掉来求证伪面，
  第一版当场没红，于是行数钉成字面量 `50_001`。
- `test_scheduler_details.py` 的 `test_valid_trigger_types` 写的是
  `assert trigger_type in ["interval", "cron", "date"]`，两边都是测试自己的字面量，
  而且漏了服务实际支持的 `once`。换成 `test_accepted_trigger_types_build_their_apscheduler_trigger`
  （四种类型各真建一次）+ 未知类型/缺 cron 表达式必须 `ValueError`。

## 三、判据面自己必须能红：24 条反事实

普查面是 AC-17|08 的判据，所以它比被测代码更需要反事实。三组合计 24 条，全部通过：

| 档案 | 条数 | 抓的是 |
| --- | --- | --- |
| `census-counterfactuals.txt` | 7 | 四类空壳各自能红（临时目录造最小命中样本）+ **正控制**：生产侧 `LABEL = Path(__file__)...` 的派生标签配字面量断言是黄金向量，不许误伤 + 夹具缺失 ⇒ T1 读数为 0 + 夹具被手改一字节 ⇒ `fixture_sha_ok=no` |
| `behaviour-counterfactuals.txt` | 12 | 每条修复后的用例：把生产值改坏 ⇒ 用例必须红，跑完按 sha256 校验还原 |
| `golden-counterfactuals.txt` | 5 | 黄金向量校验器：表被篡改一格 / 录制锚点被篡改一格 / 声明的锚点在档案里查不到 / 固定偏移推导写成 +7 / tzdata 上海被换成 UTC —— 5/5 都是「基线绿 → 篡改红」 |

正控制那条是本轮刻意加的：规则分不清「抄定义」和「黄金向量」，就会把作者推向删掉预计算断言
（预计算恰恰是 §5.2 T1② 要求的）。所以派生标签这一格必须是**能算出来的不命中**。

## 四、真实信封录下来了，并且当场证伪了一套手造样本

`scripts/ops/fuyao_envelope_recorder.py` 联调录制（read-only GET，两次请求间隔 12s，
断连重试 4 次），落 9 例到 `tests/fixtures/upstream/fuyao_t1_envelopes/`
（`responses.json.gz` + `meta.json`，凭据不落盘、写盘前逐条比对过）。
第一趟录 8 例、第二趟 9 例（补 `success_calendar` 与上市前窗口探针），逐条 http/code/bytes/sha 读数见
`recording-run1.txt` 与 `recording-run2.txt`（档案定稿来自第二趟）。

真实读数的第一条就把既有断言证伪了：**无效/吊销的 Key 上游回的是 `code=2003`**
（`Invalid or revoked API key`），而文案表把 2001 记作鉴权码。分类不能凭一次观测改
（2003 同时覆盖真的权限不足），但 `opendata_fuyao/error_messages.yaml` 的 2003 文案
原文是「联系数据提供方确认账号权限或接口采购范围」——照着做的人会去谈采购而不是换 Key。
现在 2003 的 advice 第一步是换 Key，并由 `test_bad_credential_is_the_recorded_shape`
钉住（`"FUYAO_API_KEY" in caught.value.advice`），否则这段人话退回原文案没人发现。
`2001` 至今**未在线上观测到**，这点写在 yaml 注释里，不假装有读数。

另一条只能靠真机拿到的读数：上市前的窗口不报 `3001`，而是 `code=0` + 空行 +
`timestamp: null`（`test_a_pre_listing_window_normalizes_to_nothing_without_raising`）。

## 五、黄金向量这一格被加宽，没有收窄

`tests/fuyao_golden.py` 的 22 日毫秒戳表，由 `golden_millis.py` 用**三种互相独立**的推导复算
（历元日差 − 固定 +8 偏移、`timezone(timedelta(hours=8))`（不依赖 tzdata）、`ZoneInfo("Asia/Shanghai")`），
再把档案里**每条**带窗口的记录的 `start` 与 `end+1ms` 全部拉回比对：
`recorded_records_with_window=6`、`wire_anchors=12`。锚点不是只挑一条好看的，
且必须真的对齐上海零点（`datetime.fromtimestamp(..., SHANGHAI).time() == time.min`）——
「录制参数声称是上海零点」本身是可反证的事实，不能只跟推导比。

## 六、条目级判定面（探针）与两条防「读数 blindness」的判据

`scripts/quality/acceptance_item_probe.py` 新增 `AC-17|08` 探针：它**不采信**普查的
`VERDICT` 行，而是逐项重读四类计数、三个 T1 地板（≥3 / 含成功不抛 / ≥3）、
`fixture_sha_ok`、`provenance`、`t1_golden_days`，另加两条：

- `files_scanned == population`：普查扫到的文件数必须等于 `tests/**/*.py` 里的用例文件数。
  「全树为零」要是漏扫了 20 个文件，读数就只是把空壳藏在读数之外。
  （普查同时改成 `rglob`，子目录里的用例不再靠人记着改这里。）
- `untracked_tests == 0`：被判的树必须 CI 看得见。本轮跑第一遍时它是 `1`
  （`tests/test_fuyao_recorded_envelopes.py` 未跟踪），探针据此保持 `gap`——读数留在
  `probe-ac17-08-prestage.txt`；`probe-ac17-08.txt` 是暂存后的同一命令读数（`0` / `proven`）。
  这一格是靠**提交测试**关掉的，不是靠放宽判据。

`probe-self-test.txt`：`18 probe(s) measured; every judge is reachable from its declared
repair, and all 97 counterfact(s) flip a clean reading back to a gap.`

## 七、这一格现在证明什么、不证明什么

证明：`tests/**/test_*.py` 全树（161 个文件）在 §5.1 四类规则下读数为零，且四条规则
各自被合成样本证明能红；T1 三问的输入是 9 份 sha 可复算的真实录制信封；
黄金向量表被三种独立推导和 12 条线上锚点钉住。

不证明（披露到位）：

1. 普查人群是 `tests/**/test_*.py`。非 `test_*` 前缀的 helper、以及前端单元面
   （C29–C32 单独收口）不在这一格里。
2. 规则是**形态**判定，不是语义判定。`assert x in [literals]` 这种两边同为字面量的自指
   它抓不到（本轮第二节的 trigger 那条就是行为反事实抓的）；符号存在性型空壳
   （`hasattr(ScheduleType, "ONCE")` 一类）仍属 C43 登记的 A2 面 46 / legacy 面 63 条后续项。
3. 绑定解析失败时规则回退为「点名要求人工确认」，不会自动放行；派生标签（`Path(__file__)…`）
   有意**不算**抄定义，靠正控制那条守住。
4. 录制是单日窗口（2024-01-02..01-12 的 8 根、日历 240 行）。上游换字段名、把 `message`
   改成 `msg`，要等下一次联调录制才会暴露；档案 sha 复算只保证「档案没被手改」。
5. `t1_*` 四个读数按文件名与用例名前缀在 `tests/` 上算，只覆盖 §5.2 T1 档里 fuyao 客户端
   这一路；`opendata/data/http_client.py` 与契约层核心 Fetcher 的 T1 档面不在本轮读数里。

## 八、复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
git archive HEAD | tar -x -C /tmp/c44_head        # 修前的完整树
python docs/evidence/C44/shell_audit.py --root /tmp/c44_head   # 19 处命中，exit 1
python docs/evidence/C44/shell_audit.py                        # 全零 + t1=met，exit 0
python docs/evidence/C44/census_counterfactuals.py             # 7/7
python docs/evidence/C44/behaviour_counterfactuals.py          # 12/12
python docs/evidence/C44/golden_millis.py                      # problems=0
python docs/evidence/C44/golden_millis_counterfactuals.py      # 5/5
python scripts/quality/acceptance_item_probe.py --self-test          # 18 探针 x 97 反事实
python scripts/quality/acceptance_item_probe.py --item 'AC-17|08'    # proven，exit 0
python -m pytest tests/test_fuyao_recorded_envelopes.py -q -m "not e2e"
```

八个读数是在**门禁抓到的一次改动之后**重跑的：首轮 `make gate` 在 a2-check 上红了三项
（`ruff format` 两个用例文件、`mypy` 三个证据脚本、`bandit` 一处缺 `# nosec`），修的都是
脚本自己的类型与标注面（`_immediate_member` 参数写成 `ast.Compare` 而实参是 `ast.expr`；
`golden_millis.py` 用了 3.11 才有的 `datetime.UTC` 而 `requires-python>=3.10`；动态载入的
复算脚本改由 `Verifier` Protocol + `FACES` 运行时面检查兜住），判定内容与阈值一字未动。
重跑后 `census-head/census-worktree/census-counterfactuals/golden-millis/golden-counterfactuals/probe-self-test`
六份档案的**读数体**与修前逐字相同（抬头按新一批的采集时间与 `git status` 重写），
`behaviour-counterfactuals.txt` 只差 pytest 的两位耗时数字，
`probe-ac17-08.txt` 差的是 `untracked_tests` 那一格（见第六节）。

## 九、门禁三遍分别证的是哪棵树（含一份摘录档案）

第一遍（04:31–04:39）判红，`GATE_EXIT=2` 停在 `a2-check`：`ruff format` 两份用例、`mypy` 15 条
全在 C44 新增的证据脚本、`bandit` 两条缺 `# nosec`。**这一遍的整档没能留住**——修完后我把
同一暂存路径 `/tmp/c44-gate-run1.txt` 又跑了一遍，暂存件被覆盖。留得住的是覆盖前逐字读到的两段
（a2-check 的三项 FAIL 与 mypy 全量清单、末 40 行的 bandit 两条 issue 与 `GATE_EXIT=2`），
登记为 `gate-run1-a2-fail-excerpt.txt` 并在抬头里写明它是**摘录而非完整未裁剪日志**；
流程改正也写在那份抬头里：同一遍的暂存路径不得复用，此后每一跑用带序号的独立暂存路径。
三项的处置逐条记在那份档案的档尾，共同点是判定内容与阈值一字未动。

`gate-run1-prebackfill.txt`（7,103 行／17 段标记／`GATE_EXIT=0` 在第 7097 行，读数体与暂存件
`cmp` 逐字节相同）跑在「测试与证据已 `git add`、验收文档尚未回填」的树上，证明
`untracked_tests=0` 之后普查与探针两读一致，且新增 4 个证据脚本 + 录制器（都在 a2-check 的
curated set 内）过 ruff/format/mypy/bandit 四项，其余 15 个成员同遍全绿：
`3101 passed / 6 skipped in 63.10s`、`TOTAL 88.80%`、`A2 files 349`、public-api `613/613` 双 100%、
`ledger-check items=130 proven=20 gap=7 unreviewed=103 ticked=20`（回填前那一刻的树）。
`gate-run2-postbackfill.txt`（7,131 行 = 61 行抬头 + 7,062 行原始输出 + 8 行档尾；17 段标记；
`GATE_EXIT=0` 读自文件内部第 7123 行；档尾声明的「第 62–7123 行」与暂存件 `cmp` 逐字节相同，
671,170 字节）跑在 §2 勾选、§10 台账、banner 全部回填之后，证明回填本身没有把任何一格判成更差。
两遍的原始输出按成员段逐段比对（抹掉 ANSI 码后取差集）：**17 段里 11 段字节相同**
（brand-check、zero-dep-check、js-points-check、loguru-check、a2-check 358 行全等、quality-ratchet、
public-api-quality、frontend-lint、frontend-typecheck、frontend-collection、PASSED），6 段有差别，
逐段写明差的是判定数还是噪声：

| 段 | 差别 | 是不是判定变了 |
| --- | --- | --- |
| `ledger-check` | census `proven 20 → 21`、`unreviewed 103 → 102`、`ticked 20 → 21` | 是，且这正是回填要发生的（items=130、groups=22、rows=19 不变） |
| `evidence-traceability` | `files 445（gate logs 73）→ 447（74）`、`dates 67/73 → 68/74`、`branch 61/73 → 62/74` | 否：六个面两遍都是 0；多的两个文件就是 run 1 的两份档案，它们在 run 1 扫描之后才落成 |
| `secret-check` | gitleaks 时钟戳 `4:53AM → 5:08AM`、扫描耗时 `17.1s → 16.9s` | 否：`194 commits`、`61.61 MB`、`no leaks found` 逐字相同 |
| `test-cov` | pytest 总耗时 `63.10s → 63.86s`、一条 loguru 时间戳、xdist 的 `[gwN] [NN%]` 交错顺序、run 2 报告里多落进一条 sqlite `ResourceWarning: unclosed database` | 否：`3101 passed, 6 skipped`、`TOTAL 11097 1078 2738 286 88.80%`、fail-under 与各 face 阈值行逐字相同 |
| `frontend-test` | `Start at` 与 `Duration` 数字 | 否：`Test Files 14 passed (14)`、`Tests 106 passed (106)` 相同 |
| `frontend-e2e` | 逐条 ms/s 耗时与并发完成顺序 | 否：`18 passed` 相同，18 条用例标题一字未差 |

那条 `ResourceWarning` 值得单说：它是否出现在报告里取决于 flush 时机，run 1 没有、run 2 有，
两遍的用例数与覆盖率一致 ⇒ 把它写成「run 2 多了一条失败线索」是错的，写成「两遍逐字相同」也是错的，
所以按上面这张表逐段落档。
两遍都是完整未裁剪输出，抬头带运行前 provenance；本档案是运行返回后才落成的，
所以任何一遍的日志里都不含它自己（`evidence-traceability` 那一段读到 `files 447`，加上本档才是 448）。
