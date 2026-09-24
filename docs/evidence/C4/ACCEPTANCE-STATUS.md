# 迭代1 验收状态（2026-09-24 收口快照）

> 生成方式：本轮验收复核。**门禁与真机套件全绿**为基线，逐 AC 标注完成/部分/待办与阻塞原因。
> 复算命令见文末。

## 1. 全绿基线（可复算）

| 套件 | 命令 | 结果 |
|------|------|------|
| 质量门禁 | `make gate`（py313 环境） | **PASSED**：2185 passed / 3 skipped，覆盖率 **84.04%**；brand/zero-dep/js-points/a2/ratchet/public-api/前端三项逐项通过 |
| 真机 e2e | `set -a; . ./.env; set +a; pytest tests -m e2e` | **71 passed / 0 failed / 0 skipped**（含 fuyao 9 项真机、ECB/FRED/IMF/OECD 真机、MySQL 数仓、WS 协议、客户端 socket） |
| 前端 | `make gate` 内 frontend-lint/typecheck/test | eslint 0 error / vue-tsc 0 / vitest **79 passed** |

证据：本目录 `gate.txt`、`e2e-full.txt`。

## 2. AC 逐项状态

| AC | 状态 | 说明 |
|----|------|------|
| AC-1 品牌·许可·合规 | ✅ 完成 | A0 证据；本轮新增 legacy_transfer 白名单复核 |
| AC-2 契约与数据模型 | ✅ 完成 | 契约层 + `dwd_stock_adjust` 因子表（本轮）；akshare qfq 官方对照待网络 |
| AC-3 注册·能力·路由 | ✅ 完成 | A1 证据 |
| AC-4 请求治理 | ✅ 完成 | **本轮新增每日健康巡检**（`/health/patrol` + CLI cron） |
| AC-5 搬运（P0/P1） | ✅ 完成 | **本轮 P1 全量搬运**（+183 文件 → 313 py + 2 资源），manifest/lock 逐文件 sha256 一致 |
| AC-6 搬运保真对照 | 🟡 部分 | 字节级保真已建立；录制回放抽样（≥20%）与 2 个 kline 用例待 eastmoney 网络 |
| AC-7 opendata_fuyao | 🟡 部分 | **1A 最小集真机 9 项全通过**；B2 期货/期权/基金组**阻塞于官方 API 契约文档**（门户无 discovery 路由） |
| AC-8 ods 落库体系 | 🟡 部分 | 单源真机全通；双源校对待 akshare 网络 |
| AC-9 交叉校对与 dwd | 🟡 部分 | 单源直通/幂等真机验证；告警双通道投递属 1B |
| AC-10 自研 provider | 🟡 部分 | P0 六源 + P1 GDP/失业率；其余 P1/P2 待续 |
| AC-11 REST + WS API | 🟡 部分 | **本轮完成 WS 订阅/导出+公式转义/服务端 adjust/客户端订阅/B5.1+B5.2 前端分层**；B5.3 backtrader_web 真实接入待消费方授权 |
| AC-12 上游同步机制 | ✅ 完成 | **本轮同步工具 + 演练（147 文件报告 + 锁校验）** |
| AC-13 跑批与断点续拉 | 🟡 部分 | **本轮失败清单一键重试完成**；调度时间真机校准待 eastmoney |
| AC-14 真实消费方打通 | 🟡 部分 | 降级验收已声明（A5）；真实接入待授权 |
| AC-15 旧数据兼容与转移 | 🟡 部分 | **本轮评估 + 617,371 行迁移 + 指纹校验**；DROP 待用户拍板 |
| AC-16 零上游依赖 | ✅ 完成 | 基线受控 3（本轮新增 1 处数据源标签，复核留档） |
| AC-17 工程质量门禁 | ✅ 完成 | gate 全绿，2185 用例 |
| AC-18 数据目录与新鲜度 | ✅ 完成 | **本轮数据目录页完成** |
| AC-19 运维保障 | 🟡 部分 | **本轮配置项清单 + Key 监控**；配额/封禁主动轮询为演进项 |

## 3. 本轮（2026-09-24）新增交付

WS 数据订阅（协议全量）· REST 导出 + CSV 公式注入转义 · 服务端 adjust=qfq\|hfq（因子表 + 累乘）· 客户端 WS 订阅 · `/ws/executions` 挂载缺陷修复 · B5.1 interface_loader 收口 · B5.2 前端分层适配 · B4.5 数据目录页 · C2 上游同步工具与演练 · C3 旧表评估与迁移 · B1.1 P1 全量搬运 · B3.2 失败重试 · B3.4 健康巡检 · AC-19 配置项清单 · 验收台账回填。

**顺带修复的真实缺陷**（验收复核发现）：
1. `/ws/executions` 生产不可达（挂载路径）
2. `adjust` 合成日期键类型不匹配（ISO 字符串 vs date）
3. `/data/capabilities` 裸数组导致客户端崩溃
4. **ECB 利率/GDP 真机用例 series key 缺数据流前缀**——此前被"网络超时"注记掩盖，实为键格式缺陷（已更正证据）

## 4. 仍待办（阻塞/外部依赖）

| 事项 | 阻塞 |
|------|------|
| AC-6 录制回放抽样、AC-8 双源校对、AC-13 调度校准、AC-2/AC-11 akshare qfq 官方对照 | eastmoney 网络持续拒连 |
| AC-7 B2 期货/期权/基金端点 | 需官方 API 契约文档（门户无 discovery） |
| AC-10 其余 provider、B1.2 P1 域注册 | 工作量（各 provider 需契约建模 + 真机对照） |
| AC-15 STOCK_ZH_A_HIST DROP | **待用户拍板**（破坏性；工具 `--drop --yes` 就绪） |
| AC-14/B5.3 backtrader_web 真实接入 | 需消费方仓库授权 |

## 5. 复算

```bash
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
make gate                                              # 门禁全绿
set -a; . ./.env; set +a
python -m pytest tests -m e2e -q -p no:randomly --no-cov   # 真机 71 项
cd frontend && npx vitest run                          # 前端 79 项
```
