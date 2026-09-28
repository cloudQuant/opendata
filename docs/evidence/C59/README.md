# C59 —— 「记得加 `-m "not e2e"`」不是保护：把它换成默认拒绝的闸门，并顺手发现单元面里每遍门禁都在对生产仓库做 DDL

## 1. 入口：任务要求补一道 skip 闸门，实测发现的是另一件更要紧的事

登记的原话是「给这些用例加一道未被显式环境变量放行即 skip 的闸门（形状参照 `tests/test_port_fidelity.py` 的 em 网络 skip），并让 `make gate` 的 `-m "not e2e"` 与这道闸门对账（同一份名单，不能一边放行一边 skip）」。

照着这句话做普查（按 AST 找「不在 e2e 作用域内、却用 `settings.*database_url` 构造引擎」的定义）时，量出来的不是「有几条 e2e 用例忘了 skip」，而是：

- `tests/test_freshness.py` 里有 **2 个未标记的定义**在直接对生产仓库动手（档案 `unguarded-before.txt` 面 1，第 12–13 行），
  - `TestCheckFreshness`：`DROP TABLE IF EXISTS` + `CREATE TABLE ... LIKE ods_stock_daily_akshare` + 插入 + 再 DROP
  - `TestPartitionPlanCollector`：`DROP/CREATE _probe_unpartitioned_fresh`（MySQL 专有语法，SQLite 无等价物）
- 这四条用例在上一遍门禁里是 **PASSED**，不是 skipped（同一档案面 2，第 18–25 行引自 `docs/evidence/C58/gate-run1.txt` 第 2165/2230/2233/2249/2252/2275/2291/2312 行）。PASSED 就是证据：老 fixture 的唯一非 skip 分支是「连得上」，连上了才会走到 DDL。

也就是说：`make gate` 每跑一遍，就有四条用例在无人注视的 `test-cov` 段落里对**生产 MySQL** 建表删表。它们本来被约定「靠人记得加 `-m "not e2e"`」保护 —— 而这道约定在这一遍里已经被违反了（这两格根本没标 e2e，加不加过滤都照跑）。

所以 C59 = 两件事：默认拒绝的放行闸门（原任务），和把这条误跑面收口（普查发现的缺陷）。

## 2. 闸门形状〔源码 `tests/conftest.py`，+37/−1〕

`pytest_collection_modifyitems` 在收集结束时给**每一条带 `e2e` 标记的 item** 挂 `pytest.mark.skip`，除非操作员显式放行：

- 放行变量 `OPENDATA_ALLOW_LIVE_E2E`，值必须**逐字等于** `allow-prod-and-upstream-writes`
- 真值判断不算放行：`1`／`true`／`yes`／空串／全空格／前导空格／token 加后缀 —— 七种宽松取值 6 种必须被拒（第 7 种空串本来就走 `==` 拒绝；见 §6 B1 为什么专门测这个）
- skip reason 必须同时点名**变量**和**危险面**（`live e2e leg reaches the production warehouse / upstream APIs`），否则一个人只看到「skipped」会以为用例是坏的
- 默认方向是 deny：不设变量 ⇒ skip。想跑真腿要么按门禁那样 `-m "not e2e"`（承认它不该在门禁里跑），要么打完整 token

**「同一份名单」怎么落地**：闸门和 `-m "not e2e"` 都只认 `e2e` 这一个 marker，所以两份名单在结构上不可能打架。hook 跑在 pytest 的 `-m` 摘除（`trylast`）之前，于是门禁平面里这些 item 先被 `-m` 摘掉、skip 标记无从生效；平面外它们 skip。两条路都要求「标记 = 危险」这一个事实成立。`test_the_guard_and_the_filter_key_off_the_same_marker` 把这个不变量钉住（断言 `LIVE_E2E_MARKER == "e2e"`、`pytest.ini` 里有 `e2e:` 声明、`addopts` 里有 `--strict-markers`）：一旦有人把闸门改成认别的 marker、或者删掉 marker 声明，这条立刻红。

## 3. 两条改动〔源码 `tests/test_freshness.py`，+30/−25〕

1. `TestCheckFreshness` 的 fixture 从「连生产仓库 + `CREATE TABLE ... LIKE`」换成 `create_engine("sqlite://")` + 显式 `PROBE_DDL`。三条判据（滞后 26 天 / 新鲜 / 表缺失报 missing 不抛）**留在单元面**，没有 skip 掉事。
   - 为什么能这么换：SQLite 接受反引号标识符，`SELECT MAX(\`日期\`)` 原样可跑；`MAX(<date>)` 在 SQLite 返回文本，而 `freshness._as_date` 的 docstring 已经把这个驱动差异写成它处理的输入之一 —— 判据测的是 `check_freshness`/`ods_freshness` 的行为，不是 MySQL 的日期类型。
   - `CREATE TABLE ... LIKE` 是 MySQL 专有，正是它把 DDL 拖进单元面；`PROBE_DDL` 把形状显式拼出来，代价是列名要与真实表同步维护，注释里写明了这一点。
   - `NOW()` 不是 SQLite 函数（能跑但是 NULL），两条插入的时间戳改成字面量；`:day` 绑定改 `EXPECTED.isoformat()`，因为列类型是 text。
2. `TestPartitionPlanCollector` 加 `@pytest.mark.e2e`。分区元数据（`information_schema.PARTITIONS`）没有 SQLite 等价物，这一格本质上就是活仓库腿，归到放行面而不是 skip 掉事。

## 4. 规模复算：三张数字，差值可加

| 平面 | 修前 | 修后 | 出处 |
| --- | --- | --- | --- |
| 全部 | 3478 | 3504 | 修后＝本档案 §8 复算命令；修前＝`docs/evidence/C58/gate-run1.txt` 第 598 行 `8 workers [3395 items]` + e2e 面 83 |
| `e2e` | 83 | 84 | 修后实测 `84/3504 tests collected (3420 deselected)` |
| `not e2e`（门禁平面） | 3395 selected | 3420 selected | 同上，`3420/3504 tests collected (84 deselected)` |

差值必须能加：**+26 条守卫模块 −1 条挪出（`TestPartitionPlanCollector`）＝ +25**，3395 → 3420 ✓；e2e 面 83 → 84 就是那条挪入 ✓；总数 3478 + 26 = 3504 ✓。

由此可预登记一条**可证伪的门禁读数预测**：上一遍单元面是 `3389 passed, 6 skipped`（C58 档案第 7607 行），这一遍应是 **3414 passed, 6 skipped**（3389 − 1 挪出 + 26 新增）。门禁日志跑完对这一行；不对就是本轮的账。

## 5. 反事实：五条 break，每条都必须咬红〔档案 `guard-counterfacts.txt`〕

`docs/evidence/C59/run_counterfacts.py` 临时改写四个文件（Makefile／pytest.ini／`tests/conftest.py`／`tests/test_freshness.py`），每段跑一次守卫模块，跑完按原文写回并用 sha256 校验还原。全程把 `MYSQL_HOST`/`DATA_MYSQL_HOST` 指到 `127.0.0.1:1`（refused），所以连「放开活腿」那段也没碰到真仓库 —— 这一点由档案正文每段第 2 行打印的 env 自证。

守卫自己也是同样的 containment 形状，这一点值得单独写明：`_run(..., released=True)` 那条判据（`test_releasing_the_token_actually_opens_the_legs`）在**强制**注入 refused 端口的前提下打开闸门，它期望的读数是 3 条 `skipped`（原因为 `unreachable`）而不是 `passed`。于是即使门禁哪天被人在带 token 的环境里跑，这 26 条里也没有任何一条可能对生产仓库动手 —— 「证明闸门会放行」这件事不需要真的连上任何数据库。

| break | 破坏什么 | 期望后果 | 实测 |
| --- | --- | --- | --- |
| B1 | 放行判断从「精确 token」换成真值判断 | 七种宽松取值里 6 种被放行 | `6 failed, 20 passed`（档案第 78 行） |
| B2 | hook 直接 return（等于没有闸门） | 危险腿不再被 skip | `10 failed, 16 passed`，且第 93 行暴露 `warehouse database unreachable: OperationalError` —— 少了闸门又不带过滤，就真的去连仓库 |
| B3 | `TestPartitionPlanCollector` 摘掉 e2e 标记 | 普查判据红 | `2 failed, 24 passed`（第 136 行，两条普查判据都红） |
| B4 | `make test` 平面丢掉 `-m "not e2e"` | Makefile 对账红 | `1 failed, 25 passed`（第 161 行） |
| B5 | `pytest.ini` 删掉 `e2e:` 声明 | marker 对账红 | `3 failed, 23 passed`（第 190 行） |

干净树 `26 passed`（第 49 行）→ 五段全红 → 还原后再跑 `26 passed`（第 217 行），四对 restore digests `identical`（第 200–203 行），`COUNTERFACT_RUNNER_EXIT=0` 与 shell 独立转述的 `RUNNER_EXIT=0` 两处都是 0。

## 6. 门禁里的对账判据（不是一次性脚本）

26 条守卫里，除 hook 单元测试外还有三条是**静态对账**，它们进了 `make gate` 的 `test-cov` 平面，因此每次门禁都在复核这件事：

- `test_no_test_outside_the_live_plane_builds_a_database_engine`：AST 扫 `tests/**/*.py`，任何不在 e2e 作用域（模块 `pytestmark` 或类/函数装饰器）内却调 `create_engine`/`create_async_engine` 且参数含 `database_url` 的定义 ⇒ 红。这条是 §1 普查面的常驻版本。
- `test_every_makefile_pytest_plane_deselects_the_live_legs`：解析 `Makefile` 里所有 pytest 平面（当前 2 个：第 94、97 行），任何一条不带 `-m "not e2e"` ⇒ 红。「一边放行一边 skip」从此不可能。
- `test_the_gate_never_sets_the_release_token`：`Makefile`/`gate` 段里出现放行 token ⇒ 红，防止有人把闸门当成噪音然后顺手在门禁里放行。

普查判据自身的双向可证伪性由 `test_the_census_bites_in_both_directions` 六个合成样本守住（标了 e2e 的不报、模块级 `pytestmark` 的不报、嵌套类里的要报、`sqlite://` 的不报、变量间接传 url 的要报）—— 这条不是装饰：本轮第一次 ad-hoc 普查用子串 `data_database_url` 量出 6 处，其中 2 处是对源码文本的断言（假阳性）。按 `ast.Call` 判才准。

这 26 条里有 2 条会起嵌套 pytest 子进程（`test_forggetting_the_marker_filter_skips_instead_of_writing` 与 `test_releasing_the_token_actually_opens_the_legs`，其余判据在进程内用 `_FakeItem` 走 hook），而门禁平面是 `-n 8`。本轮专门在 xdist 下量过一次：`pytest tests/test_e2e_opt_in_guard.py -q --no-cov -m "not e2e" -n 4 -rs` ⇒ `4 workers [26 items] / 26 passed in 6.58s`，所以「worker 里再跑 pytest」不会变成门禁里的第三种意外。

## 7. 台账面：本轮一格都不翻，理由是判据原文不覆盖这件事

`AC-8|02` 的措辞判的是**应用启动路径**上的 DDL 与 alembic 归属，本轮修的是**测试 fixture** 绕过归属直接建表删表 —— 前者没有被后者的存在证伪，所以不动 ledger，只在这里披露。census 维持 `items=130 proven=43 gap=11 unreviewed=76`。

把「测试基础设施的安全」写成一条验收格子、再给它配探针，是可以做的下一步，但那要改 §2 条目级清单的口径；口径变更需要重新自测，不在本轮顺手做。

## 8. 复算命令

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"; export PYTHONPATH=.
# 平面规模（只读，不连库）
pytest tests -m "e2e"     --collect-only -qq --no-cov -p no:cacheprovider
pytest tests -m "not e2e" --collect-only -qq --no-cov -p no:cacheprovider
# 闸门与对账判据（单元面，26 条）
MYSQL_HOST=127.0.0.1 MYSQL_PORT=1 DATA_MYSQL_HOST=127.0.0.1 DATA_MYSQL_PORT=1 \
  pytest tests/test_e2e_opt_in_guard.py -q --no-cov -m "not e2e" -p no:cacheprovider
# 被改的两个模块
pytest tests/test_freshness.py -q --no-cov -m "not e2e"     # 期望 14 passed, 1 deselected
# 五条反事实（会临时改写四个文件再原样还原；勿与门禁并行）
python docs/evidence/C59/run_counterfacts.py; echo RUNNER_EXIT=$?
# 修前普查面（只读，读 HEAD 版本）
bash docs/evidence/C59/run_unguarded_face.sh
```

要在真实放行下跑活腿：`OPENDATA_ALLOW_LIVE_E2E=allow-prod-and-upstream-writes pytest tests -m "e2e"`。**本轮没有跑，也不建议在没点头的情况下跑** —— 它会对生产仓库做 DDL、对上游 API 做写。

## 9. 本轮欠账（如实登记）

1. 普查面只认 `create_engine`／`create_async_engine` 两个函数名。绕过它们的写法（`pymysql.connect`、`aiomysql` 直连、`sqlalchemy.engine.make_url` 后再建、别名导入）不在面内；下一轮如果要做「任何网络/数据库出口」的普查，得换判据而不是往这里堆函数名。
2. 闸门管的是**已标记**的腿。漏标的危险腿由 §6 第一条静态判据兜住「连库」这一种；对**上游 API 写**的漏标没有等价普查（HTTP 出口太散，本轮没有做成判据）。
3. `PROBE_DDL` 与真实 ods 表形状靠人工同步。若 `ods_stock_daily_akshare` 改列，这里不会自动红（判据只读那几列，改列名才会红）。
4. e2e 面从 83 → 84 之后，`AC-8|04`（写入基准）与 `|08`（真机双源落 ods）仍在等用户点头；本轮把它们的放行通道做成了显式 token，但没有使用。

## 10. 档案表

| 文件 | 内容 | 证据档位 |
| --- | --- | --- |
| `unguarded-before.txt` | 修前普查面（HEAD 2 处未设防定义）+ 四条用例在 C58 门禁日志里的 PASSED 读数 + 标记行 diff | 只读脚本 `run_unguarded_face.sh` 输出，`FACE_EXIT=0` |
| `guard-counterfacts.txt` | 五条反事实的完整未裁剪正文（含每段的命令、env、tally、restore digests、起止时钟） | runner 自判 `COUNTERFACT_RUNNER_EXIT=0` + shell 独立 `RUNNER_EXIT=0` |
| `gate-run1.txt` | 本轮全量门禁（含 26 条新守卫进门禁、单元面读数与 §4 预测对账）；**本目录里唯一晚于门禁遍的文件**，跑完才并入 | 日志内 `GATE_EXIT=0` |
| `run_counterfacts.py` | 反事实 runner（可重复，按原文还原） | 源码 |
| `run_unguarded_face.sh` | 修前普查复算脚本（只读 HEAD 版本） | 源码 |
| README.md | 本文件：叙述面 | 叙事 |
