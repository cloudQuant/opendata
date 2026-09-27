# C1 —— 宏观 provider 开局：5 个自研源 + OECD 五会话探路 + 一次被推翻的归因

> 本 README 由 C43 补写（2026-09-27）。C1 留下 13 份读数（git 首次收录全部 2026-09-23），
> 但没有里程碑说明。文件本身各自带了日期与端点，缺的是**这批读数在共同证成什么、
> 哪几份的 `verified=True` 当时其实没有自己的实跑、以及后来被谁推翻**。
> 补写材料只有三处来源：这 13 份文件的正文、git 记录（`1651c5c…d32f212`、`64a0e89`、`cef919b`）
> 和后来几轮（C8 / C22 / C28 / C33）的正文。原文一字未改；
> 2026-09-24 那次「更正」是当时以**追加段落**写进原文件的，本次没有回滚也没有重写它。

## 这一轮在证成什么

§9 的里程碑映射把 **C1 → AC-10（自研 provider，FR-7）**。C1 是 1C 阶段第一批：
把 OpenBB 的宏观序列语义换成自研实现，落 `yfinance` / `fred` / `ecb` / `imf` / `oecd`
五个 provider 模块与 `openbb_map.yaml` 准入面（提交 `1651c5c`、`3e9698b`、`e2f1c60`、
`3eb3e36`、`acad137`、`d32f212`、`848d8a8`）。AC-10 的判据口径是
「**已启用** provider 全部注册且对照通过，不设 32 个全完成为门槛」，
所以这一轮的目标不是铺满，而是让「verified」这个词在本仓库里有可查的读数。

## 13 份读数分四类

| 类别 | 文件 | 内容 |
|------|------|------|
| P0 CPI 三源 | `ecb-live-verification.txt`、`imf-live-verification.txt`、`oecd-live-verification.txt` | ECB `ICP/…`（1 passed）、IMF `PCPIPCH/USA`（`tests/test_imf_provider.py::TestLive` 10 passed incl. live）、OECD `DSD_PRICES@DF_PRICES_HICP`（`tests/test_oecd_provider.py::TestLive` 11 passed incl. live） |
| P1 新域 | `ecb-rate-live-verification.txt`、`ecb-gdp-live-verification.txt`、`imf-gdp-unemployment-live-verification.txt`、`oecd-unemployment-live-verification.txt` | 利率（FM）、欧元区 GDP（MNA 16 维季度键）、IMF GDP/失业率、OECD 失业率 |
| 探路记录 | `oecd-exploration.txt`（会话 1）、`oecd-gdp-unemployment-exploration.txt`（会话 2）、`oecd-unemployment-exploration-3/4/5.txt` | 五会话把 OECD SDMX 的目录、维度序、码表、键语法逐层量清楚 |
| 负读数 | `yfinance-live-probe.txt` | 匿名首请求即 `YFRateLimitError`，未重试，按 R2 以 `verified=false` 注册 |

探路记录里最有价值的是**自我否证**：会话 2 写下「Y15T64 exists」是未经验证的假设，
会话 3 直接更正为「PT_BPSA / PT_BSA / Y1574S / 1574 都不存在，之前的 404 主要是单位码错」；
会话 4 把三条假设分别判为 REJECTED / OPEN / REJECTED；会话 5 拿到突破事实
——**v2 的 404 body 就是答案**（`NoResultsFound` 表示「键合法但无此序列」，与语法错的 400/403 不同），
并用 `?detail=serieskeysonly` 读出该流的完整序列键目录，不再猜键。
最终 `oecd-unemployment-live-verification.txt` 的 `verified=True` 是「键从目录里读出来 +
live e2e 首跑通过」两条一起支撑的，与下面那两份不同。

## 一次被推翻的归因（本目录最重要的教训）

`ecb-rate-live-verification.txt` 与 `ecb-gdp-live-verification.txt` 当初写的是：
live e2e **提交时没能复跑**，归因于「本会话到 `data-api.ecb.europa.eu` 的 SSL 握手反复超时」，
`verified=True` 的依据退化成「人工探测 200 + 共用 `EcbSeriesFetcher` 骨架 + 兄弟用例 CPI 通过」。
次日（2026-09-24）验收复核推翻了它：真实根因是**测试用的 series key 缺 SDMX 数据流前缀**，
门户返回的是 **400**，不是网络超时；CPI 用例因为键里带 `ICP/` 而一直通过，反而掩盖了差异。
修复 `EURO_AREA_MRR_KEY → FM/…`、`EURO_AREA_GDP_KEY → MNA/…`（提交 `64a0e89`），
修复后两条用例通过、全 e2e 套件 63 passed / 0 failed；更正段以追加方式写进两份原文件（提交 `cef919b`）。
由此定下的口径也写在原文件里：`SeriesQuery.series_id` 的契约是「完整位置严格键」，含 dataflow 前缀。

因此本目录读数的强度**并不齐平**，必须分开看：
ECB CPI / IMF / OECD CPI / OECD 失业率的 `verified=True` 各有自己的实跑断言；
ECB 利率与 ECB GDP 的初版没有，是次日补上的。一份档案里因此存在两个采集时点
（正文 2026-09-23、`更正` 段 2026-09-24），这也是 C42 之后要求「补写与更正必须写明是谁、
在哪天、以追加还是改写方式落地」的直接原因。

## 这些读数**不**证明什么

1. **同源自检不等于跨源对照。** 多数文件的判据是「适配器输出与该 API 自己的原始 CSV 相等」
   —— 它能抓住解析/键/窗口 bug，抓不住上游值本身错。跨 vendor 的逐字段对照是后来的
   C8（yfinance）、C9、C10、C20、C34 才做的，别把 C1 读成那件事。
2. **e2e 不在 `make gate` 里。** 门禁跑 `-m "not e2e"`，「1 passed / 10 passed incl. live」
   是真机窄跑读数，换网络、换上游状态就可能不成立（本目录恰好就有两例没能复跑）。
3. **负读数不等于数据正确。** `yfinance-live-probe.txt` 只证成「按 R2 以 `verified=false` 注册、
   auto 路由跳过它」；yfinance 的数据口径是 C8 轮（2026-09-24）修完并转正的，见 `docs/evidence/C8/README.md`。
4. **本目录不覆盖 C1 落地的全部东西。** 13 份文件里没有 `fred` 的独立读数
   （`imf-gdp-unemployment-live-verification.txt` 只留了一句「fred R2 waiting on key」），
   也没有 C1.1 `openbb_map.yaml` 准入面的读数；前者要等 C28 的 Key 健康面
   （`docs/evidence/C28/`），后者要等 C33 与注册表全库对账（`docs/evidence/C33/`）。
   顺带记当下事实：`opendata/data/authority.json` 收录 ecb / imf / oecd，未收录 fred 与 yfinance。
5. **AC-10 条目级没有因本目录翻勾。** 台账 `AC-10|01…|05` 到 C43 时点全为 `unreviewed`（0/5），
   §10 的 AC-10 行状态是「部分完成」。任务 #5「AC-10 provider 扩充」按设计仍是 open。

## 追溯面

本目录 13 份文件都是人工探测/测试尾档，正文不含 `===== gate:` 段，
因此 `python scripts/quality/evidence_traceability.py` 的 date / identity / command / exit 四面
按定义不适用（那四面只对 gate 日志生效）；`narrative` 面由本 README 闭合。
需要提醒的是：这四面不适用的意思是**工具不会为它们报警**，不是它们合规 ——
C1 全部早于 C14 的运行环境头约定，也没有一份留了完整命令（只有测试选择器），
复现时要按各类别自己补
`python -m pytest tests/test_imf_provider.py::TestLive -m e2e -q` 这样的命令。
