# opendata 代码质量规范

> 版本：v1.1
> 生效范围：opendata 全部新增代码与既有代码改造
> 基准：适配自 `bt_api_py` 质量体系，并按本仓库现状（继承 akshare_web 遗产）调整
> 上游来源：`docs/迭代计划/迭代1-重构数据中台/代码质量规范.md`（本文件为其仓内正式版本，二者合并后以本文件为准）

---

## 0. 核心原则

1. **fail-closed**：所有质量门禁默认阻断；工具缺失、配置缺失、快照缺失、证据缺失一律视为失败，不得静默放行。
2. **分层适用**：质量要求按**代码来源**（自研 / 搬运）与**代码新旧**（存量 / 新增）两个维度分层——继承项目不能套用"新项目零债务"假设（§1）。
3. **债务不增长（棘轮）**：存量债务快照固化，只降不升；范围缩小或静默变更即失败。
4. **触碰即达标**：任何被修改的存量文件，一次性满足新增代码标准。
5. **证据文化**：任何"通过"声明必须附可复算命令与真实输出；历史证据只作参考，不作当次通过依据。
6. **反空壳测试**：测试必须锁定行为（黄金向量 / 真实报文 / 错误翻译），禁止自指与单断言空壳（§5）。

---

## 1. 分层适用范围

| 层 | 范围 | ruff | mypy | bandit | docstring | coverage |
|----|------|------|------|--------|-----------|----------|
| **A1 自研-存量** | `opendata/` 中未被本迭代修改的文件 | 债务 ≤ 基线快照 | 债务 ≤ 基线快照 | 债务 ≤ 基线快照 | 不强制 | 不设新阈值（记录现状） |
| **A2 自研-新增/触碰** | 新增文件 + 被修改过的存量文件；`opendata/data/`、`opendata/pipeline/`、`opendata_fuyao/`、`opendata_providers/`、`scripts/` | 全规则集（§2.1）零违例 | 严格（§3）0 errors | 全量 | Google 风格 100% | ≥85%（statement+branch） |
| **B 搬运代码** | `akshare/`（迭代 A2 起更名 `opendata_http/`） | 仅 E/F（语法与未定义名），不格式化不重排 | 排除 | 排除（安全审计另行专项，见 §4） | 不要求 | 排除（以一致性对照替代） |
| **C 测试代码** | `tests/` | 全规则集但豁免 S1xx/S2xx、ANN 系列 | 渐进（不强制） | 豁免 S | 见 §5 | — |
| **D 生成产物** | codemod 报告、alembic 迁移模板 | 排除 | 排除 | 排除 | — | — |
| **E 前端** | `frontend/` | ESLint | `vue-tsc --noEmit` | — | — | 基础用例（纳入 gate） |

> **为什么 A 层要拆 A1/A2**：`opendata/`（原 `app/`）是 akshare_web 遗产，共 67 个源文件；实测 mypy 非严格、ruff 忽略含 `F821`、覆盖率阈值 70%（实测约 84%）。若按"新项目零债务"要求一步达标，估算需 30~40 人日，会让地基里程碑直接卡死或被迫放水——两者都会破坏 fail-closed 的信用。故按"**存量棘轮 + 增量零容忍 + 触碰即达标**"执行。
>
> **搬运层理由**：akshare 源码无类型注解、无规范 docstring；强行满足 A 层标准会诱发大规模改写，破坏与上游 diff 同步能力。搬运质量由**一致性对照**保证，不由静态检查保证。

---

## 2. Lint 与格式（ruff）

### 2.1 自研代码规则集

配置落 `pyproject.toml`：

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
exclude = ["akshare", "opendata_http", "alembic/versions", "frontend", ".venv", "htmlcov"]

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "TC",
          "RUF100", "S", "PERF", "ANN", "D"]
# 例外必须逐条注释理由
ignore = [
    "B008",   # FastAPI Depends() 是框架惯用法，97 处误报会淹没真信号
]

[tool.ruff.lint.pydocstyle]
convention = "google"
```

> **关键约束**：`F821`（未定义名）**不得忽略**——静态检查放过未定义名是最高风险的一类漏检。

### 2.2 搬运代码

- 搬运代码**禁止** `ruff format`、禁止 isort 重排——保持与上游逐行可 diff。
- `.pre-commit-config.yaml` 中对 `opendata_http/` 设 `exclude`，否则改名后 `ruff-format` 会全量重排搬运代码，直接违反本节并破坏 §7 棘轮前提。
- 对搬运代码只跑 `ruff check --select E,F`，其债务进棘轮快照（§7）。

### 2.3 格式

- 自研代码 `ruff format` 全量通过（CI 阻塞）；**A1 存量文件在被触碰前不强制重排**（触碰后按 A2 处理）。

---

## 3. 类型检查（mypy）

A2 层严格配置（落 `pyproject.toml`）：

```toml
[tool.mypy]
python_version = "3.10"
explicit_package_bases = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
strict_equality = true
check_untyped_defs = true
warn_return_any = true
```

- **目标：A2 层 0 errors**；A1 层建立基线快照（error 数与按文件分布），只降不升。
- `ignore_missing_imports = true` 仅限无 stub 的第三方库；禁止用 `Any` / `cast` / `type: ignore` 压债。
- 搬运层（B）整体 `exclude`，不参与 mypy。
- 渐进收紧：若第三方依赖触发系统性误报，允许**按模块** override 放宽单条规则，必须在 `pyproject.toml` 内注释理由与回归计划；禁止全局 `disable_error_code` 兜底。

---

## 4. 安全扫描（bandit）

- A2 层全量：`bandit -c bandit.yaml -r opendata opendata_fuyao opendata_providers scripts`。
- 例外必须**精确到行级**且带理由注释。
- 凭证类（S1xx）：`.env` 注入、日志脱敏、API Key 哈希落库为硬约束，违反即一票否决。
- **搬运层安全专项**：`akshare/`（后为 `opendata_http/`）不纳入日常 bandit，但在 A2 里程碑执行**一次性全量扫描**（含 `mini_racer` 执行站点 JS 的文件、子进程、动态导入面）并人工 triage 留档；JS 执行点须登记来源与 sha256。

---

## 5. 测试基线（反空壳）

### 5.1 空壳测试判定（发现必须修掉）

1. **自指测试**：测试体内重新实现被测算法与实现自比较（永远绿）。改为断言**预计算的黄金向量**。
2. **单断言空壳**：仅断言类名/常量。必须覆盖行为。
3. **`inspect.getsource` 形式检查**：断言源码文本，重构即破坏。改为行为测试。

### 5.2 模块分档最低基线

| 档 | 适用模块 | 最低要求 |
|----|---------|---------|
| **T1** | `opendata_fuyao/`、`opendata/data/http_client.py`、契约层核心 Fetcher | ①错误翻译：真实响应信封样例断言错误码 → 异常映射，≥3 用例（含成功不抛）②黄金向量：认证头/签名构造断言预计算值 ③normalize：真实报文 → 标准化模型逐字段断言，≥3 用例 |
| **T2** | 有 Key 的 providers | ①错误翻译 ≥2 ②normalize 真实报文 ≥2 |
| **T3** | 纯公开只读 providers | normalize 真实报文 ≥2（录制回放样本） |
| **T4** | 服务层（`ods_writer` / `reconciliation` / `dwd_merge` / `pipeline` / `registry` / `partition` / `freshness`） | 行为测试：key 级幂等 upsert、合并规则、差异捕获、口径映射反例、降级路由、断点续跑、修订传播、跨年分区写入；禁止 mock 被测对象自身 |
| **T5** | API 层（REST/WS 路由） | 集成测试（httpx AsyncClient / WS 测试客户端），覆盖鉴权、分页、错误码、参数注入用例 |

- **真实报文来源**：fuyao 用官方文档示例 + 联调录制；providers 用录制回放样本；禁止手造"理想报文"替代真实样本。
- 测试命令基线：`python -m pytest tests -v -n 8 --cov-branch`（`pytest-xdist` 已入 dev extra）。

---

## 6. 覆盖率门禁

- **A2 层** statement+branch ≥ **85%**，fail-closed。
- 分模块下限：`opendata/data/`、`opendata/pipeline/`、`opendata_fuyao/` ≥ **90%**。
- **A1 存量**：不设新阈值，以基线快照管理（`fail_under` 从 70 上调至 84，声明"只降不升"）；被触碰的文件按 A2 标准计入。
- **B 层**不计入 coverage（由一致性对照替代）。
- 覆盖率报告（xml + html）必须非空并归档为 CI artifact。

---

## 7. 棘轮机制（债务只降不升）

- **快照文件**：`docs/quality/ratchet.json`，记录三类债务计数与**扫描范围**：
  - B 层（搬运代码）lint 债务；
  - A1 层 mypy errors 与 ruff 违例；
  - 直连 HTTP 调用数（搬运代码内 `requests.*` 调用点，渐进收口指标）。
- **检查规则**（`make quality-ratchet`）：
  - 当前计数 > 快照 → 失败；
  - 扫描范围与快照不一致（缺失或新增未登记）→ 失败（禁止静默缩小或扩大范围）；
  - 仅当全部范围未增长且至少一项下降时，方可 `--update` 固化新低；范围扩张必须显式 `--force-update` 并走审查。
- **A2 层零容忍**：不设债务快照——任何 ruff/mypy/bandit 违例直接阻断。

---

## 8. 公共 API 质量

- `make public-api-quality` 统计 A2 层公开 callable（`__all__` 与公开类方法）的 **docstring 覆盖率**与**参数注解覆盖率**。
- 目标：**100% / 100%**；新增公开函数不带 docstring 或缺注解 → CI 阻止。
- **A1 存量**：不强制，随"触碰即达标"逐步收敛。

---

## 9. 质量检查命令（Makefile 目标）

| 命令 | 说明 |
|------|------|
| `make a2-check` | **A2 零容忍门禁**：对新增/改动文件跑 ruff 全规则 + format + mypy + bandit，任一违例即失败 |
| `make quality-ratchet` | **A1/B 债务棘轮**：存量 ruff/mypy/bandit 与直连 HTTP 计数只降不升，范围变更即失败 |
| `make public-api-quality` | A2 公共 API docstring/注解覆盖率统计（要求 100% / 100%） |
| `make zero-dep-check` | 零上游依赖断言（AST 口径 + 违规样本自测） |
| `make brand-check` | 品牌残留与 `app/` 旧包名残留检查 |
| `make test` | pytest `-n 8`，默认 `not e2e` |
| `make test-cov` | pytest `-n 8 --cov-branch` + 阈值门禁 + 报告归档 |
| `make lint` / `format` / `format-check` / `typecheck` / `security` | **全树开发者视图**：会显示 A1 存量债务，不参与门禁；A1 是否可接受由棘轮判定 |
| `make deps-audit` | pip-audit |
| `make frontend-lint` / `frontend-typecheck` / `frontend-test` | 前端三项 |
| `make gate` | **门禁聚合**：上列门禁项逐项阻断，任一子项失败即整体失败 |

> **为什么 `make lint` 不进 `gate`**：A1 存量（继承项目）自带静态检查债务，规范明确要求以**棘轮**管理而非一步清零（§1）。若把全树 `ruff check` 放进门禁，则要么永久红灯、要么被迫放宽规则——两者都会破坏 fail-closed 信用。因此门禁由 **A2 零容忍（`a2-check`）+ A1 棘轮（`quality-ratchet`）** 共同承担：前者保证新代码干净，后者保证旧债务只降不升。

---

## 10. CI 门禁（fail-closed 聚合）

- **平台：GitHub Actions**（`.github/workflows/ci.yml`），集成测试使用 `services: mysql:8.0`。
- `quality-gate` job 逐项运行 `make gate` 并**逐项显式阻断**；任何子项失败不得被汇总结果掩盖。
- **零上游依赖断言**：**分层 AST 口径**——只扫运行时包（`opendata/`、`opendata_fuyao/`、`opendata_providers/`、搬运包）的 `Import`/`ImportFrom`/`importlib.*`/`__import__` 调用，忽略注释与文档字符串；白名单显式登记（`scripts/codemod/`、`tests/`、`docs/`）且**范围静默变更即失败**；扫描器需含"故意违规样本"自测。
  - 另需**正向证据**：在未安装 akshare / openbb 的干净环境中跑 P0 域集成测试。
  - 字符串常量引用（如 `"akshare.data"`）由 codemod 改写 + 扫描器覆盖。
- **secret 扫描**：gitleaks 进 pre-commit 与 CI（含全历史扫描）。
- pre-commit：ruff + 空白符 + YAML/JSON 校验 + secret 扫描；ruff-format 对搬运层设 `exclude`；mypy / bandit 放 CI（本地可选）。
- 性能基线（可选，不阻塞）：对 `ods_writer` 批量 upsert、`dwd_merge` 建 pytest-benchmark 基线。

---

## 11. 提交与审查约定

- Conventional Commits；涉及搬运层的提交须注明来源模块与文件数。
- 质量声明必须附证据（命令 + 输出摘要 + 日期）；"此前通过"不等于"本次通过"。
- 新增第三方依赖须在提交说明中声明用途与许可证兼容性（BSL/MIT 边界）。
- **搬运前凭证检查**：搬运文件中若含上游硬编码凭证，须替换为环境变量或登记到 `docs/port-report.md` 后再入库。
