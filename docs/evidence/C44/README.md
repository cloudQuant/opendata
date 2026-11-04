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

> 面①（自指）的判定后来被改过一次，改的是**判据本身**：操作数职责、本地算子的口径，
> 以及随之新增与关闭的反事实。规则现形与代价见第十节，`census-counterfactuals.txt` 那 7 条
> 在改动后仍逐条复算通过（含 `_ms` 那条真自指）。

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

## 十、面①（自指）改成按操作数判定：规则现形、代价、反事实

脚本抬头那句「规则写死在下面：改规则等于改判据」在这一节兑现。面①原来那样判：

```python
used_helpers = _callee_names(statement) & helpers      # 整条 assert 里出现过本地算子
touched = ({n.id for n in ast.walk(statement) if isinstance(n, ast.Name)} & production) - used_helpers
```

它把一条断言当成一团符号，不问两边各是什么身份，于是两个方向都出错：

* **假阳性**（本轮 11 处命中全是这一形）：`assert adapter.parse(_worksheet(...)) == {'code': '...'}`
  —— `_worksheet` 只是造输入，期望值写的是字面量，被测实现的输出没有被本地重算过；
* **假阴性**（危险方向：以后可能被读成「全树干净」）：
  `expected = _ms(day)` 之后 `assert result == expected`（期望值确实先被本地重算，但断言句里
  没有本地算子的调用）；`fetcher = YieldCurveFetcher(client)` 之后
  `assert _ms(day) == fetcher.millis_for(...)`（生产只出现在属性链后面，句中没有生产裸名）。

### 现在的判据（按 `ast.Compare` 的操作数各判一次）

一个操作数只有两种身份，先判身份再判命中：

1. **碰到生产的那一侧 = 被测值**。`_production_names_touched()` 认四种来路：生产裸名
   （`adapter.X`）、属性链（`fetcher.fetch(...)`）、经普通局部赋值绕过去
   （`fetcher = Fetcher(client)`）、经本地构造函数体绕过去（`snapshot = registry(...)`，
   `registry` 里点的是 `TemporalIdentityRegistry`）。这一侧**不能**当期望值来源，
   因此也不参与「本地重算」的认领（`_recomputed_by()` 遇到这种调用直接剪掉整棵参数子树——
   这就是 `_worksheet(...)` 那批误伤被关闭的地方）。
2. **没碰生产的那一侧才可能是期望值**。期望值「出自本地重算」= 它是本地算子的调用结果，
   或顺着普通局部赋值（`x = ...` / `x: T = ...`）追到这样一个调用；沿值结构递归
   （容器、算术、调用参数、推导式**的元素**），但**不**穿过 `a.b` / `a[0]` 这类成员读取，
   也不穿过推导式的 `iter`——`pytest.approx(legacy_bar.close * 0.8)` 是「相对输入陈述期望」，
   不是把生产另算一遍。
3. 命中要求：某一侧本地重算 **且** 至少另一侧跑到生产。四个面名一字未改
   （`self-reference / vacuous-assert / constant-shell / source-form-check`），
   验收棘轮按「格 id + 面名」打哈希，改名等于作废旧钉。
   这一层判不出时还有下一层：期望值那一侧「碰到生产」不再直接算被测值，
   叫生产对象**算**出来的那一侧会被当回期望值（见「再补一层」）。

配套的本地算子口径（`_does_the_work()`）也按「在算数量还是在书写」收窄了一步，
因为操作数判据把旧规则里那道偶然的护栏（必须同时出现生产裸名）拆掉之后，
书写形的本地函数会成批被点成自指：

* `REPO_ROOT / LEDGER_RELATIVE_PATH` 借的是 `/`，是路径拼接不是除法；
  除/整除只有一侧是数字字面量时才算数量换算（`us / 1_000_000`）。
* `"c" * 64` 是字符串填充，`Mult` 两侧有 str/bytes 字面量即不算重算。
* 单步换算 `date.fromisoformat("2024-01-03")` 是把测试自己写下的字面量换个写法；
  两步以上的换算链（`combine` + `timestamp`）才算把生产实现另算一遍。
  **代价写清楚**：`assert report.requested_end == d("2024-01-03")` 这一形从此不在面①里
  （旧判据在 `test_temporal_instrument_identity.py:294/382` 点过它，两处都是同一个日期书写）。

### 读数：11 → 0，但不是靠少看

| 面 | 改前 | 改后 |
| --- | --- | --- |
| self-reference | 11 | 0 |
| constant-shell | 19 | 19（本轮不动这一面） |
| vacuous-assert / source-form-check | 0 / 0 | 0 / 0 |
| files_scanned | 670 | 671（新增 `tests/test_shell_audit_self_reference.py`） |

（本节的 0 是**上一轮**的读数；再补一层生产自当预言机之后变成 5 → 0，见下面「再补一层」。）

11 处旧命中逐条核对，**全部是输入构造器形状**（期望值分别是 `[]`、`()`、
`{'code': ...}`、`"cell_limit_exceeded"`、一段逐字写死的字节串），按新判据不再命中；
其中 `test_temporal_instrument_identity.py:294/382` 两处是上表的 `d()` 书写形。
新判据反过来抓到 6 处旧判据看不见的**真自指**，并在本轮把用例改掉（不是把判据关掉）：

* `tests/test_ecb_series_parser.py:192/238/239/307`：`_key(prefix, values, dimensions)` 把
  生产里 `f"{prefix}." + ".".join(...)` 的 series_key 拼接另算一遍再断言相等。
  改成把期望串逐字写死（`"YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"` 等），`_key` 随之删除，
  参数化那条把期望串作为参数列出（用例数 2→2，没有减少）；
* `tests/test_source_grant_provisioning.py:564/565`：`_datetime_to_epoch_us()` 用整数把
  UTC 时刻重算成 epoch 微秒，正是被测实现做的那次换算。改成**反方向**表达：
  `_from_epoch_us(result.not_before_us) == expected_not_before`，即把观测解码到期望的口径，
  期望值是测试自己构造的那个 UTC 时刻。整数换算、无浮点，µs 级精度与改前逐位相同，
  变异 tz 偏移那条判据没有被削弱（`53 passed`）。

### 再补一层：期望值叫生产对象自己算（生产自当预言机）

一次专门的测量（临时探针分支加了四个只读数、不进判据的盲区测量臂，量完即弃）把上面
那五条盲区的实际规模量了出来：成员读取型 14 处（抽样全是合法的：读 fake client 的调用记录、
读 sha 钉住的录制夹具）、拿生产常量做算术 38 处（其中真自指约 2 处）、经 fixture 落生产 0 处、
无比较的一元断言 142 处（70 条是 `isinstance` 契约、28 条是纯真值判断，抽样 8 条里 0 条自指）。
也就是说这些盲区**不是**危险分子；剩下的危险只有一形，而它恰好被旧判据的身份判定放过：
期望值那一侧「碰到生产」就被当成被测值，于是

```python
assert pipeline.invalidated == [pipeline._pipeline_id(WINDOW)] * 2   # tests/test_c85_runner.py:160
```

两侧都碰生产、谁都不算本地重算，面①读 0。可这行的期望值是问被测对象要的：`_pipeline_id` 的
算法改掉，等号两边一起改，断言永远不会红——这正是 AC-17|08 要抓的空壳。格子里
「盲区登记为文档」但读数仍写 proven 是假绿，所以把这**一形**升进默认面（面名不变，
仍报 `self-reference`）。规则见 `self_reference_findings()` 里的 `_oracle_expectation()`：
五个条件缺一不红（期望侧直接写 `name.member(...)` 且 `name` 落到生产；不在最左操作数；
这条比较里只它这么调；另一侧确实挂着同一对象读成员；要么调的是私有成员、要么观测与期望
挂在同一个生产对象上，且该成员没被本地替身自己覆写）。它是判据不是加宽——第一次实现按
「这一侧碰到生产就行」粗判时读 45 处，逐条核对几乎全是合法写法，收窄后才 5 处。

读数（同一棵树，只换判据）：

| 面 | 补这一层前 | 补这一层后 |
| --- | --- | --- |
| self-reference | 0 | 5 → 0（5 处全部改掉用例，没有动判据） |
| constant-shell / vacuous-assert / source-form-check | 19 / 0 / 0 | 19 / 0 / 0 |
| files_scanned | 671 | 671 |

上面两列都是**本分支这棵树**（`fix/od-shell-census-precision`）的读数，`shell_audit.py`
直接面它。定义抄写那一列在这里仍是 19——那 19 处的用例修在 #186
（`fix/od-shell-constant-copy`，tip `d6b64b2e`）上，还没进 `dev`，不是本轮判据的问题。
四个面同时为 0 只在**两批修复合起来的那棵树**上成立：本轮用
`git merge-tree --write-tree` + `git commit-tree` 造了一棵临时合并树（本分支 + #186），
在那里跑同一份脚本读到 `self-reference=0 / vacuous-assert=0 / constant-shell=0 /
source-form-check=0`，`VERDICT shells=clean exit=0`。那句话属于那棵临时树，
不属于本分支，也不属于 `dev`；#186 合进来之前，本格不能凭本分支写 proven。

5 处命中逐条改成了什么（都改在**测试**里，期望值独立写下；没有删、跳、xfail 或削弱判据）：

* `tests/test_c85_runner.py:160/161`：`[pipeline._pipeline_id(WINDOW)] * 2` /
  `[pipeline._pipeline_id(WINDOW)]` → 模块级黄金常量
  `ODS_FAILURE_PIPELINE_ID = "stock_daily:ods-failure:2026-09-22..2026-09-24:c64820a90be90557"`
  （domain + source + 窗口标签 + spec/shard/hooks 摘要的前 16 位，一次算好写成数据）。
  原先这两条在算法改动时永不失败，现在会失败——判据变强不是变弱；
* `tests/test_run_ledger_database_clock.py:235`：`== run_ledger_module._datetime_to_epoch_us(...)`
  → 参数化里加一列写下来的黄金微秒 `1_793_007_000_123_456`（2026-10-26 09:30:00.123456 UTC
  距 epoch 的微秒数，`calendar.timegm` 独立算的）；tz 归一化的判据没变（naive/aware 两条参数
  都仍要求同一个数），`9 passed`；
* `tests/test_sina_fzb_fanout_journal.py:972`（以及同一个循环里同形、当时还没被点到的
  `:975-977` naive-postgres 那条）：`== fanout_journal_module._datetime_to_epoch_us(expected_time)`
  → 每个方言带一个黄金微秒列（`1_893_553_445_123_456`，sqlite 只到毫秒 →
  `1_893_553_445_123_000`），三条方言仍各判各的，`44 passed`；
* `tests/test_ths_csi500_sample_composition.py:732`：
  `== sample._sample_identity_sha256(current.identity)` → 测试自己取证自己编码：
  `os.stat(current.database_path)` 的 `(st_dev, st_ino)` 先与状态里记录的
  `database_device`/`database_inode` 对上看一眼，再断状态里的锚等于**测试写下的**
  canonical JSON（键名、`sort_keys`、`separators` 都由测试规定）的 sha256。
  实现改 payload 或改 canonical 形式时这条会红；问实现要答案时它跟着实现一起改。
  这一形不能写成字面量（device/inode 随机器变），是「独立写下的期望」而不是抄定义。

### 反事实（改判据必须自带能红的面）

`tests/test_shell_audit_self_reference.py`，35 条，全部在合成源码上钉读数：

| 条目 | 旧判据 | 新判据 |
| --- | --- | --- |
| `_ms` 直接重算 vs 生产函数（C44 那一形） | 1 | 1 |
| 期望值先写进局部变量 | 0（假阴性） | 1 |
| 生产藏在 `fetcher.millis_for(...)` 属性链后 | 0（假阴性） | 1 |
| 私有成员当预言机 `pipeline.invalidated == [pipeline._pipeline_id(W)] * 2` | 0（假阴性） | 1 |
| 公开成员当预言机 `fetcher.normalized_rows == fetcher.normalize(rows)` | 0（假阴性） | 1 |
| 导入的生产模块私有函数当预言机 `ts == module._datetime_to_epoch_us(v)` | 0（假阴性） | 1 |
| `_worksheet(...)` 造输入 + 字面量期望 | 1（假阳性） | 0 |
| `_context(...)` 造输入 + `== []` | 1（假阳性） | 0 |
| 拼路径读档案的 `ledger_rows()` | 1（假阳性） | 0 |
| 预计算黄金向量 `== 1704124800000` | 0 | 0（正控制） |
| 两侧都是生产输出的 `!=` 行为判据 | 0 | 0（正控制） |
| 单步 `date.fromisoformat` 书写 | 0 | 0（正控制，代价写在上面） |
| 相对期望 `reclaimed.fencing_token == old_claim.fencing_token + 1` | 0 | 0（正控制） |
| 相对期望 `pytest.approx(legacy_bar.close * 0.8)` | 0 | 0（正控制） |
| 生产调用只出现在被测侧 `unadjust_bars(bars, []) == tuple(bars)` | 0 | 0（正控制） |
| 被测侧是成员调用、期望是读的枚举成员 `gate.reserve(s).status is ReservationStatus.X` | 0 | 0（正控制） |
| 比较链里写着字面量 `budget.attempts_used == budget.attempts_used_for("fred") == 32` | 0 | 0（正控制） |
| round-trip 行为判据 `canonical_sha256(c) == canonical_sha256(Model.model_validate(c.model_dump()))` | 0 | 0（正控制） |
| 生产函数返回的是值，不是机器：`first["worklist_sha256"] == sha({k: v for k, v in first.items()})` | 0 | 0（正控制） |
| 本地替身覆写了那个成员（`RecordingPipeline._pipeline_id` 自己定义） | 0 | 0（正控制） |

外加两条守住判据面本身：四个面名一字不能改；被改的那批用例文件（含 11 处误伤的原件，
以及本轮改掉生产自当预言机的 4 个文件）在面①上读数为 0。
`docs/evidence/C44/census_counterfactuals.py` 的 7 条（含 `_ms` 真自指与派生标签正控制）
在改动后重跑仍 7/7。

```bash
python -m pytest tests/test_shell_audit_self_reference.py -q --no-cov   # 35 passed
python docs/evidence/C44/census_counterfactuals.py                      # 7/7
python docs/evidence/C44/shell_audit.py                                 # 自指 0，定义抄写 19
```

### 这一面现在仍然看不见的（如实登记）

1. 期望值从生产对象上**读**出来再比（`assert got == expected.rows`，`expected = _ms(...)`），
   成员读取不 recurse，看不见。测量臂量到 14 处，抽样全是合法的（读 fake client 的调用记录、
   读 sha 钉住的录制夹具），所以有意不动；
2. 期望值用生产类型书写（`assert bars == (Bar(symbol=..., close=10.0),)`，值全是字面量）
   —— 这是 §5.1/§5.2 推荐的预计算写法，**有意**不命中。但同一侧「叫生产算」的那一半
   （`bars == [pipeline._pipeline_id(w)]`、`== Budget(None).attempts_for("fred")`）
   **从本轮起命中**：旧盲区 2 已经被收窄成「只用生产类型/字面量书写」；
3. `assert got == PRODUCTION_CONST * 2` 这种「拿生产常量算一下当期望」不在面①里
   （本地算子必须是函数定义；抄定义那一格由面②管，而面②只认 `PROD == 字面量`）。
   测量臂量到 38 处，逐条看真自指只有约 2 处，其余是路径拼接与相对期望，所以没有升进判据；
4. 经 fixture 参数、`monkeypatch`、`conftest` 落到生产的来路不追（只追本模块内的赋值与 `def`）；
   测量臂量到 0 处；
5. 面①只看 `ast.Compare`；`assert production_call(local_builder(...))`（无比较的一元断言）
   不在这一面。测量臂量到 142 处，其中 70 条 `isinstance` 契约、28 条纯真值，抽样 8 条
   0 条自指，所以同样留作盲区；
6. 生产自当预言机那一层（`_oracle_expectation()`）自己的边界，写清楚免得被读成「全树干净」：
   * 期望值经**本地包装**再叫生产算（`def _pin(id): return sample._hash(id)` 之后
     `assert got == _pin(current)`）不红——这一层只认写在比较操作数里的成员调用，
     穿过本地 `def` 就会把第 1 条那类合法读取一起点红；
   * 生产调用写在**最左操作数**（`assert module._f(x) == observed`）不红：位置是本仓库的
     书写约定（左边跑被测实现），反过来写就落在盲区 1；
   * 两侧都调生产成员的行为判据（`codec.encode(a) == codec.encode(b)`、`x.key() != y.key()`）
     不红——其中确实混着「两侧都问生产」的空壳，但分不清是行为判据还是自指，判红会成批误伤；
   * 生产**函数**的返回值当接收者不算生产对象（`first = build_worklist(...)` 之后
     `first.items()`），类与函数只按「名字首字母大写」这条命名规范来分；
   * 比较链里写着字面量期望（`a.x == a.for("k") == 32`）不红，尽管中段是生产调用——
     那一条自己已经把期望钉死，改算法会红。
