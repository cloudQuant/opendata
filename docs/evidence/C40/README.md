# C40 · 把「条目级 0/10」变成真的量过：AC-1/AC-2 十五条逐条复量

本轮的输入不是新需求。C36 把验收文档 §2–§6 的 **130 条**判据逐条登记成了台账
（`docs/quality/acceptance-item-ledger.json`），但到今天为止 AC-1 读数是
`条目级 0/10`、AC-2 是 `0/5`——**一条都没有逐条对过账**，§10 里那句"完成"只覆盖
AC 级证据面。这两格里到底有多少是量过的、多少是"看起来肯定成立"，此前没有任何代码
能回答。本轮就把这个问题变成可复算的：给这 15 条各配一份探针，只勾量出来的，其余
按实测归因记 gap。

**本轮不改 `opendata/` 与 `tests/` 一行代码**（AC-2|02 那条真实开发缺口只登记不动手，
留给 C41），全部产出 = 一份新仪器 `scripts/quality/acceptance_item_probe.py`
（2,166 行）+ 逐条读数 + 台账/文档回填。

---

## 1. 仪器：每条判据四件东西，缺一件就是自欺

```
measure(ctx) -> facts     # 只产字符串读数，不做任何判断
judge(facts) -> Verdict   # 纯函数：facts -> proven/gap，判据写在代码里而不是今天的树里
repair(key...) -> Facts   # judge 必须接受为 proven 的那份读数（*key = 抄一个实测值）
breaks: [Break]           # 每条都必须把 clean 读数打回 gap
```

`--self-test` 逐条校验四件事：**repair 用到的键必须在实测键里**（不能凭空造一个判据
根本不产的形状）、**clean 读数必须判成 proven**、**每条反事实的键必须存在**、
**每条反事实必须让 clean 变 gap**。跑出来：

```
$ python scripts/quality/acceptance_item_probe.py --self-test
OK: 15 probe(s) measured; every judge is reachable from its declared repair,
    and all 66 counterfact(s) flip a clean reading back to a gap.
```

为什么 `repair` 不是多余的：只有 `breaks` 的判据能证明"坏了会红"，证明不了"好了会绿"。
C36 之前那批门禁里就有过恒真的例子（C27 的 D10 复权判据、C31 的类型门禁恒绿）——
**一条谁都满足不了的判据是常红灯，一条谁都满足不了的判据是装饰**，两种都得靠
"声明一份 judge 必须接受为 proven 的读数"才现形。

举三条反事实（全部实测咬合）：

| 条目 | 反事实 | 为什么必须咬 |
|------|--------|--------------|
| AC-1\|02 | `enforced = 12 (缺 akshare_network)` | 判据列了 13 个 token；扫描器少查一个就是"全绿但没覆盖" |
| AC-2\|05 | `declared = 1 (table=dwd_x 硬写在注册表里)` | "三处由它派生"最怕的就是某一处其实是另一份字面量 |
| AC-1\|10 | `untracked = 1 (opendata/http_client.py 从未进库)` | "首次完整提交"漏掉一个源文件，比首提交树占比低更致命 |

## 2. 读数总账（两遍）

```
$ python scripts/quality/acceptance_item_probe.py --all          # docs/evidence/C40/probe-all.txt
census: gap=7, proven=8
$ python scripts/quality/acceptance_item_probe.py --item 'AC-1|10'  # 提交后复量，见 ac1-10-post-commit.txt
VERDICT AC-1|10: proven
```

第一遍里 AC-1|10 是 gap，`untracked-and-not-ignored files = 2`——那两条就是本轮自己的
`acceptance_item_probe.py` 和 `probe-all.txt`。代码与证据入库后（`052a30b`）复量，
`untracked=0`、`dirty=0`、残留 0、首提交 `4ee1bbca` 含必备件与四个类目 ⇒ proven。
**复量的输出写在仓库外再复制进来**：仪器自己的产物会改变被测量的工作树状态，
写进仓库就变成了自指。

最终：`AC-1 条目级 5/10`、`AC-2 条目级 4/5`。

## 3. 九条勾上的，各自承重在哪一面

| 条目 | 量到的承重面 | 为什么这一勾不是"看起来成立" |
|------|--------------|------------------------------|
| AC-1\|01 | pyproject `name=opendata`/`version=0.1.0`（AST 读，不读注释）、`opendata/__init__.py` 在库、旧 `app/` 布局跟踪数 0、`find` include=`opendata*` | 名字写在文件里不等于包结构成立，两处都要读 |
| AC-1\|02 | 条目原文列出的 13 个 token **== `check_brand.py` 里执行的 13 个**、脚本 exit 0 `OK: no brand residue...`、独立 grep `from app.`/`import app.` 在 412 个自有 .py 中 0 命中 | 脚本绿不等于脚本查了判据要查的东西；token 清单逐条对账，少一个就红 |
| AC-1\|04 | compose 6 个 `container_name` 全 `opendata_*`（不是 0 个 ≠ 全换成别的）、网络名、`opendata_user` 在 `.env.example`/compose/init.sql 三处齐、7 个机器面 token 命中 0、Makefile 17 条路径引用 0 缺失、pre-commit 8 条正则 0 条空转 | "改名覆盖补全"这四个面各自成立才叫补全；正则空转（指向不存在的路径）在这一条里会被点名 |
| AC-1\|06 | `mysql_warehouse` 不挂 `--profile`；仍带 profile 的只有 `certbot`（`ssl`） | 判据只管数据仓库容器，其余照实报 |
| AC-1\|10 | 拷贝残留形状 0、从未进库文件 0、已跟踪文件未提交改动 0、首提交含 `pyproject.toml`/`README.md`/`opendata/__init__.py` 且 `opendata/`+`scripts/`+`tests/`+`docs/` 四类目非空 | 见 §2，提交后复量 |
| AC-2\|01 | `ALL_MODELS` 参数化 9 个模型、六个 P0 名字**逐个**在列（missing `-`）、实跑 12 节点 12 passed、`to_frame`/`from_frame`/`model_dump` 三形都真被调用（dual=yes） | 六个模型名逐个查，缺一个就 gap；"文件里提到"不算 |
| AC-2\|03 | `Bar` 字段面节点 passed（模型上不允许存在复权价）、`tests/test_adjust.py` 10 passed/0 failed、官方 qfq 对照采集到 2 例（`em`、`sina(宽)`）且 **2 passed**（skip 2 只作观测） | 有至少一条真对照跑过，合成 claim 才有外证 |
| AC-2\|04 | 三段基类在 `protocol.py`（`QueryParams`/`Fetcher`/`FetchContext`）+ `Capability` 模型、`Fetcher` 三个阶段方法、`normalize_frame()` 体内**自己**做了字段映射/单位换算/键规范化（三问三 yes）、单元节点 5/5 | "基类就位"和"normalize() 真承担三件事"是两件事，分开量 |
| AC-2\|05 | `domains.yaml` 20 域 / 20 个 `rest_path` / 20 个 contract 条目、REST 路径唯一、表名与 WS 名**写进注册表的条数 0**（即这三处确实由它派生）、`tests/test_domains.py` 23 passed、派生节点 5/5 | "由它派生且一致"最容易假在"另有一处硬写但今天恰好一样"，所以量的是硬写条数 |

## 4. 六条 gap：五条要用户拍板，一条是真实开发缺口

判据不放宽 ⇒ 这六条不勾。归因全部是实测出来的，不是"再查一遍可能就绿"。

### 4.1 AC-2|02（真实开发缺口，C41 动手）

```
both classes subclass ContractModel in opendata/data/models/metadata.py = yes
the five field-set/semantics/round-trip nodes = 5/5 passed
backfill / incremental-window modules importing the contracts = 0 (-)
modules that only mention the names (docstrings, or a same-named local class) = 1 (jobs.py)
```

判据两半：模型就位 ✅；**被全市场回补与增量窗口逻辑引用** ❌。实测：
`opendata/pipeline/{runner,jobs,templates,scheduling,partitions}.py`、
`api/pipeline.py`、`services/data_acquisition.py` 里没有一处 import 这两个契约；
`metadata.py` 的模块文档写着"回补以刷新 `Instrument` 为第 0 步、增量窗口取自
`TradingCalendar` 的 prev_trade_date"，而 `jobs.py` 用的是
`pipeline/trading_calendar.py` 里**同名但不同种**的 dataclass（`tier` + `open_days`），
全库唯一真消费契约的是 `pipeline/patrol.py`。**文档 claim 与代码事实相反**，
这正是"被引用"这一半不能靠"模型有单测"顶替的原因。已登记为任务 #51。

### 4.2 五条需要决策的（任务 #52）

| 条目 | 实测卡在哪 | 为什么本轮不能自作主张 |
|------|------------|------------------------|
| AC-1\|03 | 裸词命中 611 个跟踪文件，其中搬运树 315；**条目白名单之外 70 个** = 适配层包 `opendata/data/providers/akshare/` 15 个 + 55 个（`opendata/data/*` 注册表与映射 11、`scripts/ops/*_cross_check.py` 8、`opendata/pipeline/*` 5、`scripts/quality/*` 4、`opendata/api/*` 3、`core`/`models`/`data_fetch`/`utils`/`services`/`main`/`cli` 11、根文档 `ARCHITECTURE.md`+`QUICKSTART.md` 2、构建运维面 `Makefile`/`Dockerfile`/`.pre-commit-config.yaml`/`bandit.yaml`/`pyproject.toml`/`requirements.txt`/`.env.example` 7、`frontend/` 2、`alembic_data/versions/` 1、`opendata_fuyao/endpoint_map.yaml` 1） | 条目列的 7 个面写于 A0，之后 A2 改名、B5 去依赖、C 系列加对照脚本，白名单从没跟上。**扩白名单还是改文件名是口径决策**；把 70 个文件逐个改名是另一回事。另外白名单回读时 `akshare/` 与括注 `opendata_http/` 被解析成同一条（10 条里有 1 对重复），这条也在等措辞定稿 |
| AC-1\|05 | 四处配置面（`config.py` 默认值 / `.env.example` / `docker-compose.yml` / `init.sql`）**全部读到 `opendata` / `opendata_data`，consistent=yes**；运行时面 `no-engine`（`docker info` exit 1：Cannot connect to the Docker daemon） | 后半句"后端启动、前端登录、/health 正常"要真把栈起起来。**启动 Docker Desktop 不在我的自主动作范围内**；`no-engine` 与"起不来"严格区分记法，不写成 `not-run` 蒙过 |
| AC-1\|07 | BSL 1.1 ✅、四要素齐（Change Date 2030-09-22 / Change License MIT / Grant / Licensor，缺 0）✅、`LICENSE-AKSHARE` MIT ✅、 notices 四节含嵌入资源权利块 ✅、README 授权 + 免责声明 ✅；**只差商务联系方式**：README L160 现在是自标占位 `cloud@example.com（占位，发布前替换为正式联系方式）` | 联系方式是要用户提供的文本，编一个能过判据的邮箱就是造假 |
| AC-1\|08 | 登记表存在、16 行、十列齐（含复核日期/责任人两列）✅；**但复核日期真填的 0/16、允许用途仍答"待确认" 11/16、条款链接写成散文而非 URL 13/16**；覆盖面对账：`provider-inventory.yaml` 引用的 10 行全部有行、无悬空引用 | 判据要的是"覆盖全部数据源（来源/条款链接/允许用途/复核日期/责任人）"。列在不等内容在；**填它是法务/产品复核**，本轮唯一能做的是把它做成可判据（`dated/undecided/unlinked` 三个数），已做 |
| AC-1\|09 | 字面 grep `git ls-files \| grep -E '\.env\|\.idea\|\.pid'` → **1 条 `.env.example`**（按真实凭证形状 `(^\|/)\.env$`/`.idea/`/`*.pid` 重跑为 0）；`.gitleaks.toml` 全局豁免 2 条，其中非文档/模板 1 条（`\.egg-info/`）；另有 2 个 per-rule 豁免块（C36 加的形状豁免，其代价 C36 已用反事实量出）；上游 5 文件登记 5/5、残留凭证形状字面量 0；`make secret-check` exit 0 `no leaks found` | 条目前半句要求 `.env.example` **不在版本库里**，而后半句的 AC-1\|03 明文把它列为允许的面——**两条判据互相矛盾**；"白名单仅含文档/模板"按今天也不成立。这要措辞或配置决策，不能靠把 grep 改窄或把豁免解释成模板来绿 |

## 5. 三处判据自我更正与两条被拒的取巧（必须披露）

1. **AC-1|10 的"首次完整提交"原判据被写成恒红**：第一版要求首提交树文件数 ≥ 当前树
   90%。任何一轮新增文件都会让它自然 decay，最终只能靠"删掉今天的文件"变绿——
   那是一条**谁都满足不了**的判据。现改为可满足且更严的形状：残留 0 + 从未进库 0 +
   未提交改动 0 + 首提交含必备件与四类目；占比 **只报不判**（今天 712/1325 = 53.7%）。
2. **AC-1|04 的根文档面按 AC-1|03 分型**：第一版把 README/CODE_QUALITY 里
   "本项目继承自旧工程"式的沿革行也算残留（读出 2 处）。这两行是 AC-1|03
   **明文允许**的面，把它们判红等于让两条判据互相打架。现在根文档分成
   `doc_live`（仍在**使用**旧名，0 容忍）与 `doc_historical`（沿革点名旧名，3 行 / 4 文件，
   允许），而 7 个**机器面**（compose / `.env.example` / `init.sql` / `config.py` /
   `Makefile` / `.pre-commit-config.yaml` / `Dockerfile`）仍是 0 容忍，并且这一分型
   本身有反事实守着（把 `doc_live` 抬到 1 必须翻红）。
3. **`BRAND_TOKENS` 改为从 `check_brand.py` 用 AST 派生**：仪器要读同一份 token 清单，
   复制一份会漂移（首版就被自己的品牌门禁点名 14 处，因为我把 token 抄进了新文件）。
   两条路里选了派生，**没有给自己的文件开豁免**——为自己的检查扩大豁免面，
   等于把门禁的承重让给"这个文件不算"。
4. **AC-2|01 差点把真证据读成缺覆盖**：`ALL_MODELS = [Bar, AdjustFactor, ...]` 是
   **类名**列表不是字符串列表，原来的 `literal_str_tuple` 返回 `()` ⇒ 判据读出
   "六个 P0 模型缺 6 个"。补了 `literal_names()`（AST `Name` 节点）后 covered 才回到 6。
   记下来是因为这类错的方向永远是"仪器说证据不足"，而人会倾向于怀疑证据而不是仪器。
5. **"跑不了"与"数据错"分开记**：AC-1|05 的运行时面记 `no-engine`（守护进程不可达）而不是
   `not-run`（没跑）也不是 `fail`；AC-1|06 的 `docker compose config --services` 因
   `MYSQL_ROOT_PASSWORD` 需要插值而 exit 1 ⇒ 该面记 `n/a` 并改用 compose 文件静态解析，
   而不是让一个环境缺项冒充"仓库容器还挂在 profile 上"的结论。

## 6. 台账与文档回填

```
$ python scripts/quality/acceptance_ledger_check.py        # make ledger-check
  AC-1   条目级 5/10  §10: 完成 条目级 5/10（未逐条达标）
  AC-2   条目级 4/5   §10: 完成 条目级 4/5（未逐条达标）
census: items=130 proven=17 gap=8 unreviewed=105 ticked=17
OK: items=130 proven=17 gap=8 unreviewed=105 ticked=17 across 22 group(s) and 19 §10 row(s) reconcile.
```

- 9 条 `unreviewed → proven`（各带 `command`/`round=C40`/`date=2026-09-26`/`evidence[]`），
  §2 对应 9 个框翻勾；`proven` 的 `command` 一律写成可复算的那一条命令。
- 6 条记 `gap` 并写实测 reason；其中 AC-1|09 由 C36 的 gap **更新**为 C40 的读数
  （C36 那版 reason 说的是 gitleaks 豁免面，本轮新增的是"字面 grep 与 AC-1|03 互斥"这一条）。
- 未动 §2–§6 任何一条判据原文（台账 key 是判据原文的 sha256 前 8 位，改字即失配）。
- §10 两行的 `条目级` 披露随动，AC-1/AC-2 仍是"完成（未逐条达标）"。

## 7. 门禁（两遍，完整未裁剪）

| 文件 | 内容 |
|------|------|
| `gate-run1.txt` | 回填前最终代码树：15 个成员 + `PASSED` = **16 个标记**（`PASSED` 在第 6893 行，`GATE_EXIT=0` 在第 6894 行，共 **6,894 行**）、**3,031 passed / 6 skipped（61.15s）**、覆盖率 **88.30%**（门槛 84%）、A2 files **330**（含本轮新脚本，ruff/format/mypy/bandit 全过）。台账成员读到的仍是 C39 台账：`proven=8 gap=3 unreviewed=119 ticked=8` |
| `gate-post-backfill.txt` | 回填后台账/文档最终树：同样 **16 个标记**（`PASSED` 第 6894 行，`GATE_EXIT=0` 第 6895 行，共 **6,895 行**）、**3,031 passed / 6 skipped（60.12s）**、覆盖率 **88.30%**、A2 files **330**、前端 14 files / 106 tests、e2e 18 passed。代码树与 run1 相同，唯一差量是本轮回填：台账成员读到 `proven=17 gap=8 unreviewed=105 ticked=17`，两遍都是 `OK: … reconcile` |

台账里的 AC-17|01 备注写着"14 个 `===== gate:` 标记"，那是 C36 的证据；自 C39 起
门禁成员是 **15 项 + `PASSED` = 16 个标记**，本轮两遍的标记数与 `GATE_EXIT` 行号已逐条
在上表对齐（读自文件内，不是终端回显）。

## 8. 已知边界（不要把本轮读成"AC-1/AC-2 已量完"）

- 15 条里 9 条 proven，其余 6 条 gap 中 **5 条不是本轮能自己闭合的**（措辞矛盾 ×2、
  法务复核 ×1、真实联系文本 ×1、需要起栈 ×1），1 条是开发缺口。AC-1 要 10/10，
  还差 AC-1|03/05/07/08/09 五条的决策与落地。
- 判据只保证"今天的树满足它"，不保证"这条判据覆盖了判据作者脑子里的全部"。
  例：AC-1|02 量了 token 清单一致 + 扫描零命中，但**没有**去证明扫描器的正则不会
  在大小写/分隔符变体下漏检（那是 `check_brand.py` 自己的职责面，本轮只对账清单）。
- AC-1|04 的根文档"沿革引用"识别靠一个关键词共现规则（`PROVENANCE_MARKER` 9 个词），
  它比"整文件豁免"严，但不是语义判定；写一句"以前叫 X 现在改成 Y 用法"仍会被放过。
- 反事实测的是**判据**，不是**测量代码**。`measure_*` 里读错文件、正则写窄这类错
  不在 self-test 的覆盖里——本轮两处真实教训（§5-4 的 `literal_names`、
  以及 AC-1|04 首版分型）都靠人读数读数对出来，不是靠反事实。
