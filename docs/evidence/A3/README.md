# A3 — fuyao（同花顺）通道打通：真机冒烟 → dump 入库 → dwd → REST

> 本 README 由 C43 补写（2026-09-27）。A3 当时只留下了五份读数，没有里程碑说明；
> 五个文件各自带了「复算」命令行与日期，但**没有一份说明这五件事是在证成同一个里程碑**。
> 补写的材料只有两处来源：这些文件自己的正文，和 git 首次收录它们的日期（2026-09-23）。
> 原文一字未改，也没有为补写而重跑任何真机链路。

## 这一轮在证成什么

A3 是「第一条真机数据链路走通」的里程碑：同一把 fuyao key、同一个上游
（`https://fuyao.aicubes.cn`，只读），从**端点冒烟**一路做到**全市场 dump 入库**，
再经 `ods → dwd → REST` 把真实行读回来。它支撑 §10 的 AC-3（真实链路）、
AC-9（重算幂等）与 AC-10（provider 化取数）的最初几份证据。

## 五份读数各自是什么

| 文件 | 子项 | 内容 | 复算命令（原文照录） |
|------|------|------|----------------------|
| `fuyao-live-smoke.txt` | A3.2 | P0 端点真机冒烟：价格/日历/标的检索/除复权，4 个 e2e 用例通过 | `python -m pytest tests/test_fuyao_endpoints.py -m e2e -q` |
| `fuyao-dumps-smoke.txt` | A3.3 | market-dumps 预签名链接冒烟：最近 10 交易日全市场日 K + 复权因子全量，2 个用例 | `python -m pytest tests/test_fuyao_dumps.py -m e2e -q` |
| `fuyao-dump-import.txt` | A3.3b | dump 入库到 ods 的**键级幂等**实测（日 K 55,510 行重导仍 55,510；复权因子 57,442 行落库 57,441，完全重复行被 `event_key` 塌陷为 1 行） | `python -m pytest tests/test_dump_import.py -q` |
| `fuyao-provider.txt` | A3.4 | 经**注册表路由**取数（`source=ths` 的 `stock_daily` / `stock_action`），2 个 e2e 用例 | `python -m pytest tests/test_ths_provider.py -m e2e -q` |
| `fuyao-dwd-integration.txt` | A3+A4 | 真实链路端到端：dump 导入 → ths 口径映射 → `DwdMergeService` 直通 → REST `layer=dwd`，含 600519 的具体行值与进程内 ASGI + 真机 key 的 `/api/v1/data/equity/stock_daily` 应答 | `python -m pytest tests/test_ths_dwd_integration.py -q` |

（命令列按 pytest 部分照录；四份真机文件的原文前面都还有 `set -a; . ./.env; set +a;` 这一段
把 key 注入环境的前缀，表里省掉了，未改原文。）

## 这些读数**不**证明什么（照 C42 之后的口径写明白）

1. **e2e 用例不在 `make gate` 里跑**。门禁跑的是 `-m "not e2e"`，所以这五份通过是
   「当时那台机器 + 那把 key + 那个上游状态」的读数，不是每次门禁都复现的断言。
   这条边界在本仓库后来导致过两次事故级误读（C38b 的 e2e 污染日志、C41 的 `--write` 未执行），
   因此这里点名：`fuyao-live-smoke` / `fuyao-dumps-smoke` / `fuyao-provider` 三份是**真机窄跑**。
2. 涉及本地 MySQL 的写库（`test_dump_import.py` 的 e2e 用例走本地仓库），
   写的是本地实例，不是生产数仓；`ods_stock_daily_ths` 的行数是那台机器的状态。
3. A3.3b 的「57,442 行 → 落库 57,441」是**当时观察到的去重行为**，
   不是一条被断言的不变量。C26 之后才把适配层的静默丢弃做成可见化审计，
   这一行的语义请以 `docs/evidence/C26/` 的读数为准。
4. 日期面：本目录五个文件都带 `# 复算：…` 行与日期字样，但**没有** C14 之后那套
   运行环境头（branch / HEAD / python 版本 / git status）。
   因此 `python scripts/quality/evidence_traceability.py` 把 A3 的 `narrative` 面
   在本 README 落地后判为闭合，而**不会**给 A3 的 gate 日志判 date 缺口 —— A3 没有 gate 日志。

## 后续谁接着这条路

- A4 把 `ods → dwd` 的口径固定下来（见 `docs/evidence/A4/`），B4 做场景 1 真机增量
  （`docs/evidence/B4/README.md` 第 14、43 行接的是 A3 的通道与 B3 的调度观测）。
- A3 的 key 一律从 `.env` 读，本目录不含任何凭证值；`fuyao-live-smoke.txt` 顶部那句
  「已写入本项目 .env，未入库」是当时的登记口径，今天仍然成立。
