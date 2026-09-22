# Third-Party Notices

opendata 采用 BSL 1.1（见 [`LICENSE`](LICENSE)），但仓库内**内嵌的第三方代码与资源**仍然适用其
原始许可证。本文件登记这些第三方内容、其权利状态与边界，供消费方与合规审查使用。

---

## 1. akshare（内嵌源码，MIT）

| 项 | 内容 |
|----|------|
| 来源仓库 | https://github.com/akfamily/akshare |
| 基线 commit | `c4f6a631c259783dbc2507b6b27d179b3e88079d` |
| 基线本地副本 | 仓库根 `akshare/`（迭代 A2 起更名为 `opendata_http/`） |
| 许可证 | MIT（全文见 [`LICENSE-AKSHARE`](LICENSE-AKSHARE)） |
| 版权 | Copyright (c) 2019-2026 Albert King |
| 引入方式 | 源码级搬运，保留原版权声明与来源标注 |

### 许可边界（重要）

- `akshare/`（后为 `opendata_http/`）内的文件**保留 MIT 许可**，他人仍可按 MIT 条款取用；
  仓库整体的 BSL 1.1 **不改变**这部分代码的许可。
- 自研代码（统一契约、provider 路由、pipeline、API 服务层等）**不得置于搬运包内**，以保证
  MIT 子树可在不依赖 BSL 模块的情况下独立运行。
- 消费方的三种情形：**服务调用**（受 opendata BSL 约束）、**代码复用**（搬运文件部分适用 MIT）、
  **再分发**（需同时遵守两部分许可）。

### 人工改动登记（manual edits）

搬运代码原则上与上游逐行可 diff（便于上游同步）。迄今的人工改动**仅为移除上游硬编码凭证**，共 5 处：

| 文件 | 改动 | 原因 |
|------|------|------|
| `akshare/stock/cons.py` | 雪球 token → `os.environ.get("XQ_A_TOKEN", "")` | 上游硬编码真实凭证，禁止入库（A0.1 凭证隔离） |
| `akshare/bond/bond_convert.py` | 集思录用户名/密码 → `JSL_USER` / `JSL_PASSWORD`（`__main__` 演示块） | 同上（含个人手机号，另属 PII） |
| `akshare/bond/bond_china_money.py` | 站点 `key` → `CHINAMONEY_API_KEY` | 同上 |
| `akshare/futures/futures_hf_em.py` | 站点 `token` → `EM_API_TOKEN` | 同上 |
| `akshare/option/option_em.py` | 站点 `token` → `EM_API_TOKEN`（与上游同值） | 同上 |

以上文件在后续 `upstream.lock` 中必须标记 `manual_edits = true`，codemod 重跑时不得覆盖。
改动处均带 `# SECURITY:` 注释，缺失环境变量时降级为空值（调用可能被站点拒绝），不影响其余逻辑。

---

## 2. 内嵌第三方资源（权利状态待复核）

以下非 `.py` 资源随包分发，均为第三方站点的脚本或数据文件，**权利归属不明**，登记如下：

| 文件 | 大小 | 用途 | 权利状态 | 处置 |
|------|------|------|---------|------|
| `akshare/file_fold/calendar.json` | ≈120 KB | 交易日历数据 | 未确认 | 待复核；无法确认则改为运行时获取 |
| `akshare/stock_feature/ths.js` | ≈40 KB | 同花顺页面 JS 解密 | 未确认 | 同上 |
| `akshare/air/crypto.js` | 小 | 站点 JS | 未确认 | 同上 |
| `akshare/air/outcrypto.js` | 小 | 站点 JS | 未确认 | 同上 |
| `akshare/movie/jm.js` | 小 | 站点 JS（非金融域） | 未确认 | 该子模块 D9 已移出本迭代，随模块一并处置 |

> 权利状态核查为迭代 1A 的 A0.7 数据权利合规任务的一部分，核查结果回填本表。
> `akshare/datasets.py` 引用的 `akshare.data` 包在本仓库**不存在**，`get_ths_js()` /
> `get_crypto_info_csv()` 当前已断链——A2 搬运时修复或明确标注不可用。

---

## 3. OpenBB（仅架构参考，零源码复制）

| 项 | 内容 |
|----|------|
| 来源 | https://github.com/OpenBB-finance/OpenBB |
| 许可证 | AGPL-3.0（**传染性，禁止源码进入本仓库**） |
| 本项目的动作 | 仅阅读公开文档与接口命名（事实性信息），三段式范式由本项目独立实现 |

- 本仓库**不含**任何 OpenBB 源码或其近似复制（含"改写变量名"式复制）。
- 自研 provider 的判据：独立编写 + 接口对照表（`opendata_providers/compat/openbb_map.yaml`）
  但无源码参照 + 代码审查留档。
- CI 断言运行时包无 `import openbb`（见质量规范 §10）。

---

## 4. 数据源（数据权利 ≠ 代码许可）

代码许可不覆盖**数据权利**。opendata 集中获取第三方数据并向多消费方分发，各数据来源的
条款与再分发边界单独登记于 [`docs/data-rights-registry.md`](docs/data-rights-registry.md)。

数据免责声明见 [`README.md`](README.md) 许可证章节。
