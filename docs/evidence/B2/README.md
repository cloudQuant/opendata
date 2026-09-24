# B2 — AC-7 迭代 1B：fuyao 端点映射表覆盖官方文档全部分组

验收条款：`docs/迭代计划/迭代1-重构数据中台/验收文档.md` §AC-7
「迭代 1B：端点映射表覆盖官方文档全部分组（基础/A股/期货/期权/基金）100%」。

## 1. 产物

| 文件 | 作用 |
| --- | --- |
| `opendata_fuyao/endpoint_map.yaml` | 映射表本体：文档分组页 + 每条端点的分组族/状态/中台数据域/消费场景 |
| `opendata_fuyao/endpoint_map.py` | fail-closed 加载器：状态词表、分组族词表、路径唯一、页数与端点数逐页对齐、域名必须已注册 |
| `scripts/ops/fuyao_endpoint_inventory.py` | 从官方文档机械抽取清单底稿（含文档 sha256） |
| `docs/evidence/B2/fuyao-endpoint-inventory.txt` | 清单底稿快照：57 个分组页 / 97 条端点声明 |
| `tests/test_fuyao_endpoint_map.py` | 17 个用例：双向覆盖对账 + 6 类漂移必须报错 |

## 2. 数据来源（不做人工转写）

同花顺扶摇站点在根目录提供机器可读聚合，且文档明确要求 AI/程序优先使用它：

```bash
curl -sL https://fuyao.aicubes.cn/llms-full.txt -o /tmp/fuyao-full.txt
python scripts/ops/fuyao_endpoint_inventory.py --input /tmp/fuyao-full.txt \
    --out docs/evidence/B2/fuyao-endpoint-inventory.txt
```

本轮快照：`llms-full.txt` sha256 `599fe34d0a497535f795cde31f938a7fe8ee6441eedb67882e934b4e58c002c3`，
抓取日期 2026-09-24。抽取范围是「REST API 参考」一节（`# REST API 参考` 到 `# MCP 接入`）：
MCP 工具章节是同一批端点的工具化视图，不重复计入端点总数。

映射表逐条对齐该快照，**双向**由单测把守：清单里有而表里没有 → 漏项报错；
表里有而清单里没有 → 虚构端点报错。分组页同理（`SECTION` 行与 `sections:` 集合相等）。

## 3. 覆盖结果

| 分组族 | 文档分组页 | 文档端点 | 映射条目 | implemented | available | client_only | planned |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 基础 | 4 | 3 | 3 | 3 | 0 | 0 | 0 |
| A股 | 14 | 25 | 26 | 2 | 19 | 4 | 1 |
| 指数 | 3 | 4 | 8 | 0 | 4 | 0 | 4 |
| 基金 | 17 | 34 | 34 | 0 | 34 | 0 | 0 |
| 期货 | 12 | 21 | 21 | 0 | 20 | 1 | 0 |
| 期权 | 5 | 6 | 6 | 0 | 6 | 0 | 0 |
| 资讯 | 1 | 1 | 1 | 0 | 0 | 1 | 0 |
| 导出 | 1 | 3 | 3 | 3 | 0 | 0 | 0 |
| **合计** | **57** | **97** | **102** | **8** | **83** | **6** | **5** |

（A股映射条目 26 = 文档端点 25 + `stock-basics` 规划中 1；指数 8 = 4 + `index-overview` 3 + `ths-index-membership` 1。）

**分组覆盖率 100%（57/57 页、97/97 端点均有登记）**；已接入端点 8 条（5 个 REST + 3 个 market-dumps
下载端点），与 `opendata_fuyao/endpoints.py` / `dumps.py` 的常量集合完全一致（单测对账）。

## 4. 状态口径（为什么 97 条只接了 8 条）

- `implemented`：已在 A3.2/A3.3 落地、产出契约模型并进 `source=ths` 注册表。
- `available`：上游端外已开放，本项目未接。多数对应**尚无中台数据域或尚无契约模型**的能力
  （基金画线指标、期货持仓、涨跌停池等），接入需要先立域（B1.2 那套 `domains.yaml` + 契约模型 +
  ods/dwd DDL），不是写一个 HTTP 方法就算数。这些条目是 1C/迭代 2 的候选池，映射表把
  「能接」与「该接」分开记。
- `client_only` 6 条：文档正文里带 admonition「**该能力暂未开放外部接入**」的页面
  （capital-flow 2、high-frequency 2、futures-fundamentals 1、news-events 1）——
  页面结构只是 AI 客户端内的契约说明，端外调用不作为接入范围。
- `planned` 5 条：文档标注「敬请期待」且**上游尚无端点**（指数概况 3 项、股票基础信息、
  股票所属同花顺指数查询）。登记它们是为了让"覆盖全部分组"不含水分：读者能在表里看到
  这些能力确实还不存在，而不是被漏掉。

## 5. 边界与待办

1. 映射表是**契约数据**，不改变路由：本轮没有新增 provider 注册，注册表仍是 24 个已启用
   `provider×domain`（覆盖 15 个域，其中 10 条 verified；`domains.yaml` 共登记 19 域）。
   `available` → `implemented` 的推进按域逐个走 B1.2/A3.4 那套流程。
2. 文档漂移检测依赖重新抓取快照：`llms-full.txt` 变更时需重跑 §2 命令，单测随即暴露新增/删除的
   端点。快照 sha256 写在文件头，便于确认底稿版本。
3. `futures-prices` / `options-prices` 的端外约束（分时仅当前交易日、K 线仅 `day_1`）记在
   `scenario` 文本里：这两个端点可做日 K 副源，**不能**做历史分时回补。
