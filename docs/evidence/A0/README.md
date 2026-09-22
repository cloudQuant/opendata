# A0 地基 — 验收证据索引

> 迭代 1A / 里程碑 A0
> 日期：2026-09-22
> 环境：macOS（Apple Silicon）| Python 3.13.5（conda env `py313`）| Node 20.20.2 / npm 10.8.2
> 复算方式：所有命令均可在仓库根直接执行；本目录文件为**逐字输出**，未做美化。

## 1. 门禁总入口

| 证据文件 | 命令 | 结果 |
|---------|------|------|
| `gate.txt` | `make gate` | **PASSED**（9 项逐项通过） |
| `pytest.txt` | `pytest tests -n 8 -m "not e2e" --cov-branch --cov-fail-under=84` | 1335 passed，覆盖率 **84.57%** ≥ 84% |
| `quality-gates.txt` | `brand-check` / `zero-dep-check` / `quality-ratchet` / `public-api-quality` | 全部 OK |
| `frontend.txt` | `npx eslint .` / `npx vue-tsc --noEmit` / `npx vitest run` | 0 error / 0 error / 73 passed |
| `rename-verification.txt` | 与参照副本逐文件归一化比对 | 103 文件、0 处非改名差异 |
| `secret-audit.txt` | `git add -A -n` 审计 + `detect-secrets scan` | 凭证未入库；扫描 0 命中 |

## 2. AC 逐项对照

| AC | 范围 | 状态 | 证据 / 说明 |
|----|------|------|------------|
| AC-1 品牌·许可·合规 | A0 | **部分完成** | 品牌残留零（`quality-gates.txt`）；BSL 1.1 四要素齐备（`LICENSE`）；`LICENSE-AKSHARE` + `THIRD_PARTY_NOTICES.md`；数据权利登记表 `docs/data-rights-registry.md`；双库四处一致。**未完成项**：`grep -ri akshare` 白名单口径需扩展（见 §3） |
| AC-16 零上游依赖 | A2 | **已建立护栏**（未全量生效） | AST 扫描器 + 违规样本自测通过；存量 6 处 `import akshare` 冻结为基线（`docs/quality/zero-dep-baseline.json`），只降不升。全量生效在 A2 搬运完成后 |
| AC-17 工程质量门禁 | A0 | **完成** | `make gate` 全绿且逐项阻断；A1 基线与 A2 零容忍分层落地；`F821` 已不再忽略 |
| AC-19 备份与恢复 | A0 | **部分完成** | 备份脚本 `scripts/ops/backup_mysql.sh` + 手册 `docs/operations-backup-restore.md` 已交付；**恢复演练未执行**（缺 MySQL 实例），见 §3 |
| AC-2/3/4/5/6/7/8/9/10/11/12/13/14/15/18 | 1A~1C | 待后续迭代 | A0 不含 |

## 3. A0 未闭合项（如实记录，供交接）

| # | 项 | 原因 | 建议 |
|---|----|------|------|
| 1 | **AC-1 的 `grep -ri akshare` 白名单口径** | 验收文档白名单只列 `opendata_http/`、`THIRD_PARTY_NOTICES.md`、`LICENSE-AKSHARE`，但零依赖扫描器自己的白名单（质量规范 §10）还包含 `scripts/codemod/`、`tests/`、`docs/`；此外 README/CODE_QUALITY 需**引用前身平台名**说明沿革，`docs/evidence/` 需逐字记录含 token 名的输出。三处口径不一致 | 统一为扫描器口径，并把 README/CODE_QUALITY/`docs/evidence` 作为"沿革引用/证据"白名单登记。本仓库已按此实现（`scripts/quality/check_brand.py`） |
| 2 | **内嵌 `akshare/` 目录仍在根目录** | A2 才做 `akshare/` → `opendata_http/` 的 codemod 搬运与删除；A0 提前改名会破坏 `import akshare` 的既有功能 | A2 执行；届时同步移除 `pyproject.toml` 的 `packages = ["akshare"]` |
| 3 | **恢复演练未执行** | 本机无运行中的 MySQL，且 A4 之前 ods/dwd 表尚未引入 | 在具备 MySQL 的环境执行 `docs/operations-backup-restore.md` §4 检查单并归档 |
| 4 | **CI 尚未真实跑过** | 仓库尚无首次提交、无 remote push | 首次提交后触发 CI；MySQL 依赖步骤（`alembic upgrade head`）需首次 CI 验证 |
| 5 | **scheduler 显式开关（D8）** | 属于 A4 范围（D8 落点 A0/A4，A0 只做部署拓扑相关的 compose/service 收敛） | A4 实施 `ENABLE_SCHEDULER` 生产必填 |
| 6 | **前端 48 条 eslint warning** | 存量（`no-floating-promises` 等），非 error，不阻断门禁 | B4 随前端测试框架收口 |
| 7 | **`greenlet` 依赖缺口已修** | Apple Silicon 上 SQLAlchemy 的 marker 不含 `arm64`，导致异步 DB 测试全挂 | 已在 `pyproject.toml` 的 `web` extra 显式声明 `greenlet>=3.0`（本项为本轮发现并修复的真实缺陷） |

## 4. 本轮对既有代码的改动（除机械改名外）

| 文件 | 改动 | 原因 |
|------|------|------|
| `opendata/core/config.py` | `app_name` / 双库名 / `emails_from_name` 默认值 | AC-1 品牌与双库收敛 |
| `opendata/cli.py` | 文档串与 `create_admin` 默认邮箱域 | 品牌残留 |
| `opendata/services/notification_service.py` | 失败邮件标题 `[akshare_web]` → `[opendata]` | 品牌残留 |
| `opendata/data_fetch/configs/__init__.py` | 仓库库名兜底值、注释 | 双库收敛 |
| `opendata/data_fetch/providers/akshare_provider.py` | 仓库库名兜底值、移除 2 处无用 `noqa` | 双库收敛 / 新增 RUF100 规则 |
| `akshare/stock/cons.py` | 硬编码 token → 环境变量 | 凭证隔离 |
| `pytest.ini` | `--cov=opendata`、注册 `e2e` marker | 改名 + `-m "not e2e"` 在 `--strict-markers` 下可用 |
| 12 个测试文件 | 品牌断言更新 + 6 类存量失败修复 | 使 `make gate` 可绿（详见 `rename-verification.txt` §结论 2） |

## 5. 质量基线（棘轮快照）

`docs/quality/ratchet.json`：

| 指标 | 基线值 | 含义 |
|------|-------|------|
| `ruff_selfdev` | 404 | A1 自研存量 ruff 违例（以 pydocstyle D212/D415 为主） |
| `mypy_selfdev` | 35 | A1 自研存量 mypy errors |
| `bandit_selfdev` | 6 | A1 自研存量 bandit findings |
| `ruff_ported` | 1418 | B 搬运层 E/F 违例 |
| `direct_http_ported` | 1270 | 搬运层 `requests.*` 直连调用点（与计划文档实测 1,270 处吻合） |

以上指标**只降不升**；扫描范围（包集合、文件数、工具版本）变更即失败。
