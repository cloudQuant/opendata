# A0 地基 — 验收证据索引

> 迭代 1A / 里程碑 A0
> 日期：2026-09-22
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| Node 20.20.2 | MySQL 9.4.0
> 复算方式：所有命令均可在仓库根直接执行；本目录文件为**逐字输出**，未做美化。

## 1. 门禁总入口

| 证据文件 | 命令 | 结果 |
|---------|------|------|
| `gate.txt` | `make gate` | **PASSED**，`gate_exit=0`（9 项逐项通过） |
| GitHub Actions | `CI` 工作流（push 到 dev） | **run #3 全绿**：`secret-scan`（全历史）+ `quality-gate`（mysql:8.0、alembic、`make gate`）均 success，见 §7 |
| `pytest.txt` | `pytest tests -n 8 -m "not e2e" --cov-branch --cov-fail-under=84` | 1342 passed，覆盖率 **84.62%** ≥ 84% |
| `quality-gates.txt` | `brand-check` / `zero-dep-check` / `quality-ratchet` / `public-api-quality` | 全部 OK |
| `frontend.txt` | `npx eslint .` / `npx vue-tsc --noEmit` / `npx vitest run` | 0 error / 0 error / 73 passed |
| `rename-verification.txt` | 与参照副本逐文件归一化比对 | 103 文件、0 处非改名差异 |
| `secret-audit.txt` | `gitleaks detect --source . --config .gitleaks.toml`（全历史，CI 同款 v8.21.2）+ `git add -A -n` 审计 | **0 命中**；5 处上游硬编码凭证已处置；含凭证字面量的历史提交已重写清除 |
| `restore-drill.txt` | 备份 → 隔离恢复 → 行数比对 → 对恢复库跑 `/health` | **通过**，并暴露 8 个既有缺陷（均已修） |

## 2. AC 逐项对照

| AC | 范围 | 状态 | 证据 / 说明 |
|----|------|------|------------|
| AC-1 品牌·许可·合规 | A0 | **完成**（按本仓库实现的扫描面口径） | 品牌残留零（`quality-gates.txt`）；BSL 1.1 四要素齐备；`LICENSE-AKSHARE` + `THIRD_PARTY_NOTICES.md`；数据权利登记表 `docs/data-rights-registry.md`；双库四处一致；**凭证未入版本库**（`secret-audit.txt`：5 处上游凭证处置 + 历史清除 + 全历史 0 命中）。扫描面口径差异见 §3-1 |
| AC-16 零上游依赖 | A2 | **已建立护栏**（全量生效在 A2） | AST 扫描器 + 违规样本自测通过；存量 6 处 `import akshare` 冻结为基线（`docs/quality/zero-dep-baseline.json`），只降不升 |
| AC-17 工程质量门禁 | A0 | **完成** | `make gate` 全绿且逐项阻断；A1 基线与 A2 零容忍分层落地；`F821` 不再忽略；`F821`/RUF100 等新增规则已驱动 2 处修正；**CI 真实通过**（run #3，`b42ac32`，两 job 全绿） |
| AC-19 备份与恢复 | A0 | **完成** | 脚本 `scripts/ops/backup_mysql.sh` + 手册 `docs/operations-backup-restore.md` + **演练已执行通过**（`restore-drill.txt`） |
| AC-2/3/4/5/6/7/8/9/10/11/12/13/14/15/18 | 1A~1C | 待后续迭代 | A0 不含 |

## 3. A0 未闭合项（如实记录，供交接）

| # | 项 | 原因 | 建议 |
|---|----|------|------|
| 1 | **AC-1 的 `grep -ri akshare` 扫描面口径** | 验收文档白名单只列 `opendata_http/`、`THIRD_PARTY_NOTICES.md`、`LICENSE-AKSHARE`；但零依赖扫描器自身的扫描面还含 `scripts/codemod/`、`tests/`、`docs/`，且 README／CODE_QUALITY 需**引用前身平台名**说明沿革、`docs/evidence/` 需逐字记录命令输出 | 已统一为扫描器口径并登记"沿革引用／证据"白名单（`scripts/quality/check_brand.py`）；**密钥扫描白名单已反向收紧**（见 §4-14） |
| 2 | **内嵌 `akshare/` 目录仍在根目录** | A2 才做 `akshare/` → `opendata_http/` 的 codemod 搬运与删除 | A2 执行；届时同步移除 `pyproject.toml` 的 `packages = ["akshare"]` |
| 3 | ~~CI 尚未真实跑过~~ **已闭合**：run #3（`b42ac32`）两个 job 全绿 | 首次两次失败暴露了"本地环境 ≠ 全新安装"的三处盲区（§4-16/17/18），修复后通过。本地用 py311 + 全新依赖集构建的**CI 孪生环境**复现并预演（`make gate`、a2、ratchet、全量测试均与 CI 一致） | 后续依赖漂移可先用孪生环境预演 |
| 4 | **scheduler 显式开关（D8）** | A4 范围 | 见 §5 移交项 |
| 5 | **前端 48 条 eslint warning** | 存量（`no-floating-promises` 等），非 error，不阻断门禁 | B4 随前端测试框架收口 |
| 6 | **`ENABLE_SCHEDULER` / 生产启动写库** | A4 范围（设计 §8.1） | 见 §5 移交项 |

## 4. 本轮缺陷修复清单（除机械改名外）

A0 过程中发现并修复的既有缺陷（均非本轮引入）：

| # | 缺陷 | 影响 | 证据 |
|---|------|------|------|
| 1 | `greenlet` 未声明（SQLAlchemy marker 不含 macOS `arm64`） | Apple Silicon 上所有异步 DB 测试在 fixture 阶段报 `ValueError` | `pyproject.toml` 显式声明 |
| 2 | 6 类存量测试失败（CLI patch 目标错误、`asyncio.get_event_loop` 在 3.13 移除、`except Exception` 抓不到 `CancelledError`、mock 用 `scalar()` 而服务读 `one()`、`_safe_table_name` 名不存在、schema 已校验仍期望 400） | 12 个测试失败 | 见 `rename-verification.txt` §结论 2 |
| 3 | `alembic` 版本链断裂（`003` 引用不存在的 revision id） | `alembic upgrade head` 抛 `KeyError` | `tests/test_alembic_migrations.py` |
| 4 | `CREATE INDEX IF NOT EXISTS`（MySQL 不支持） | 迁移 003 必然失败 | `alembic/versions/...` |
| 5 | `alembic/script.py.mako` 模板损坏 | 新建迁移文件语法错误 | 模板修正 |
| 6 | **迁移与模型差两代（schema 漂移）** | 迁移无法到达应用可用结构 | 重建基线为单一 `0001`，`alembic check` 零漂移 |
| 7 | **`pool_pre_ping` + aiomysql 连接复用 `TypeError`** | **应用只能处理第一个请求** | `opendata/core/database.py` 注释 + `restore-drill.txt` |
| 8 | **`init_db()` 非幂等** | "分类存在但 admin 缺失"时**启动即崩溃且无法自愈** | `tests/test_database_bootstrap.py`（3 项） |
| 9 | `tests/test_cli.py` 三个空壳测试可写真实库 | 空壳断言（规范 §5.1）+ 副作用 | 改为 mock + 真实行为断言 |
| 10 | 备份脚本无前置检查／失败残留空文件／口令进进程列表 | 失败被误认为成功；凭证泄露面 | `scripts/ops/backup_mysql.sh` |
| 11 | **上游硬编码凭证 4 处未处置**（集思录用户名+密码含手机号、中国货币网 key、东财 token×2） | 真实凭证入库；CI 全历史扫描必然失败 | 改为环境变量读取 + 登记；见 `secret-audit.txt` §1 |
| 12 | **`a2_check` 在基线不可解析时静默通过** | 浅克隆或失效基线会使 **A2 门禁整体失效且无提示**（`git diff` 失败被当成"无变更"） | 改为 fail-closed：先 `rev-parse --verify` 校验基线/base ref，失败即 FAIL；`--no-git` 与浅克隆两种场景已实测拦截 |
| 13 | CI `actions/checkout` 默认 `fetch-depth: 1` | 取不到 A2 基线 commit → 门禁静默失效（与 #12 叠加） | 加 `fetch-depth: 0`；job env 设 `A2_BASE_REF: ${{ github.base_ref }}`（push 时为空则回落 `baseline.json`）；PR 额外 fetch 基线分支 |
| 14 | **`.gitleaks.toml` 白名单过宽**（`^docs/`） | 真实 token 字面量长期藏在 `docs/evidence/` 内**不被任何扫描发现**（本文件 §1 即实例） | 收紧为「仅占位符模板 + 构建产物」，并对 `curl-auth-header` 规则**按规则**放行 markdown；反向测试：向 `docs/`、`tests/` 注入假凭证均被抓到 |
| 15 | **`A2_BASE_REF` 直接取 PR 目标的 merge-base** | 目标分支早于 A0 基线时（`dev → master`），diff 覆盖 **161 个**文件，把冻结的 A1 存量按 A2 零容忍判 → 门禁必然失败 | 基准改为「merge-base 与基线 commit 中**较新者**」；实测：空 ref → 6、`master` → 6（原 161）、分支场景 → 1 |
| 16 | **`aiohttp` 从未声明**，但 vendored `akshare/__init__.py` 在模块级导入它 | CI（全新安装）上 `import akshare` 即 `ModuleNotFoundError`，测试全挂；本地环境恰好装过所以从未暴露 | 声明 `aiohttp>=3.9.0`；用 CI 孪生环境（py311+全新依赖）迭代找全缺失项 |
| 17 | **pyjwt 类型标注跨版本漂移** | CI 解析 pyjwt 2.14（`decode() -> dict[str, Any]`），本地 2.10（返回 `Any`）→ 同一 `# type: ignore[no-any-return]` 在 CI 变"unused"，mypy 计数 35→36，棘轮失败 | 改为显式注解中间变量（两版 typing 下都成立），不依赖 ignore；连带清理被触碰文件的存量债务（UTC→timezone 等），棘轮改善 ruff 404→385 / mypy 35→34 / bandit 6→5 |
| 18 | **FastAPI 0.141/starlette 1.6 的 `include_router` 不再平铺路由** | `app.routes` 里是惰性 `_IncludedRouter`（无 `path` 属性）→ 9 个内省 `app.routes` 的测试看到 0 路由而失败（真实请求路径 1333 个测试全过） | 改为断言 `app.openapi()["paths"]`（跨版本稳定）；顺带把 `test_cors_configured` 修成真正断言 CORSMiddleware 在栈中 |

## 5. 移交 A4 的发现（记录未修）

| # | 发现 | 证据 | A4 处置方向 |
|---|------|------|------------|
| 1 | `opendata/main.py` 的 `await init_db()` 在**生产模式也执行**，启动仍写库 | `restore-drill.txt` §5 | 设计 §8.1：启动只校验版本不建表 |
| 2 | `ENABLE_SCHEDULER=false` 未生效（`/health` 仍报 `scheduler: running`） | `restore-drill.txt` 的 `/health` 响应 | 决策 D8：显式配置控制调度归属 |
| 3 | 元数据库 alembic 与 `create_all` 曾长期不一致 | `restore-drill.txt` §4-6 | 已重建基线；A4 起 DDL 唯 alembic |

## 6. 质量基线（棘轮快照 `docs/quality/ratchet.json`）

| 指标 | 基线值 | 含义 |
|------|-------|------|
| `ruff_selfdev` | 404 | A1 自研存量 ruff 违例（以 pydocstyle D212/D415 为主） |
| `mypy_selfdev` | 35 | A1 自研存量 mypy errors |
| `bandit_selfdev` | 6 | A1 自研存量 bandit findings |
| `ruff_ported` | 1418 | B 搬运层 E/F 违例 |
| `direct_http_ported` | 1270 | 搬运层 `requests.*` 直连调用点（与计划文档实测 1,270 处吻合） |

指标**只降不升**；扫描范围（包集合、文件数、工具版本）变更即失败。棘轮的两种拦截均已实测：
A2 门禁对注入的违例报错；棘轮对注入的债务报 `ruff_selfdev: 404 -> 406`。

## 7. 收口轮：历史重写与 CI 加固（2026-09-22）

| 动作 | 内容 | 依据 |
|------|------|------|
| 提交树重写 | A0 地基提交（原 `e1e49f7`）被重写，剔除 4 处上游凭证字面量**及证据文件自身记录的 token 原值**；重写后 SHA 见 `docs/quality/baseline.json` | AC-1「凭证未入版本库」；本地 `dev` 无远端跟踪，重写不影响远端 |
| 对象回收 | 旧历史打包到仓库外 `pre-credential-rewrite.bundle`（含凭证，**用完应删除**）后 `reflog expire --expire=now --all` + `gc --prune=now` | 防止旧对象被误恢复或误推 |
| 门禁 fail-closed | `a2_check` 先校验基线/base ref 可解析，不可解析即 FAIL，不再静默放过 | 见 §4-12 |
| CI 加固 | `fetch-depth: 0`、`A2_BASE_REF`、PR 基线分支 fetch | 见 §4-13 |
| A2 基准修正 | 基准取「merge-base 与 A0 基线中较新者」，避免 `dev → master` 把 A1 存量划入 A2 | 见 §4-15 |
| 扫描面收紧 | `.gitleaks.toml` 只放行占位符与构建产物；markdown 仅对 `curl-auth-header` 规则放行 | 见 §4-14 |

**对 CI 的本地等价复现**（CI 已于 run #3 真实通过，本地复现作为快速回路保留）：

```text
1. 全新克隆 dev（等价 actions/checkout + fetch-depth: 0）
2. gitleaks detect --source . --config .gitleaks.toml   → 8 commits scanned, no leaks found
3. a2_check 四种场景（均在全新克隆内实测）：
     A2_BASE_REF 为空        → 6 个 A2 文件（回落 baseline.json）
     A2_BASE_REF=master      → 6 个 A2 文件（原为 161 → 门禁失败，见 §4-15）
     A2_BASE_REF=nope        → FAIL + 提示 fetch-depth: 0（fail-closed）
     分支场景（自 0a9fba2 起） → 1 个 A2 文件（仅分支改动）
4. make gate                                           → PASSED, gate_exit=0
```

### CI 首次真实运行（run #1 → #3）

| run | 提交 | 结果 | 失败原因 → 修复 |
|-----|------|------|----------------|
| #1 | `5a76aca` | failure（secret-scan **success**） | 棘轮工具版本守卫：CI 解析 ruff 0.16.8 vs 快照 0.15.20 → §4-16 前置；**pin 工具版本**（`d3ede07`） |
| #2 | `d3ede07` | failure | pyjwt typing 漂移致 mypy 35→36（§4-17）→ 版本无关修复（`b42ac32`，同批修 aiohttp §4-16 与路由内省 §4-18） |
| #3 | `b42ac32` | **success**（两 job 全绿） | — |

`alembic upgrade head` + `alembic check` 对 CI 的 `mysql:8.0` 亦通过（此前仅在本地 9.4 验证过）。
诊断方法：无 admin token 无法下载 CI 日志，改用 **CI 孪生环境**（conda py311 + 全新
`pip install -e ".[web,dev]"`，工具按 pin 解析）在本地逐步复现 `make gate` 的失败点，
修复后在孪生环境全绿后再推送——避免在慢网络上反复试错。

### 重写带来的 SHA 影响

历史重写会改变提交 SHA。当前仓库内所有对 A0 地基提交的引用都已改为指向重写后的提交；
阅读旧文档若见到 `e1e49f7`，即重写前的 A0 地基提交。`dev` 与 `origin/master` 的唯一共同
祖先是 `a2cf824`（Initial commit），重写未触及已推送内容。
