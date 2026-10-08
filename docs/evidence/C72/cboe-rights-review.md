# C72 · Cboe 公开条款审阅（登记表第14行的依据）

复核日期：2026-10-08（UTC，主线程直接审阅公开网页；未申请账号、未购买订阅、未调用数据接口）。
责任人：cloudQuant / Codex。**这不是 Cboe 对本项目的授权，也不是冒署 cloudQuant 人工签名**——
它记录的只是"公开条款里能不能找到本项目所需用途的许可依据"这个问题的答案。

产物：`docs/data-rights-registry.md` §1 第14行（`Cboe（cdn.cboe.com 公开接口）`）与其 §1.1 段落。
判据：`scripts/quality/openbb_inventory_plane.py` 的 `RIGHTS LINK`（跑在 `make gate`，
由 `tests/test_openbb_inventory_guard.py` 双面自测）。

## 1. 为什么这一行是这轮长出来的

本轮把 cboe 的 2 个上游模型（`AvailableIndices`、`IndexConstituents`）做成了声明式引擎声明并注册进
`ProviderRegistry`。注册之后 cboe 成了"有腿的源"，而 `provider-inventory.yaml` 里它没有 `rights_rows:`，
登记表 §1 里也没有它的行——`RIGHTS LINK` 判据即报红：

```
RIGHTS LINK: `cboe` serves 2 capabilities with no `rights_rows:`, so the AC-1 gate cannot be traced from the baseline
```

同一条前置条件本来就这么写的：`docs/迭代计划/迭代2-…/前置条件与决策清单.md` 的
`RIGHTS2-cboe | cboe | 无逐源登记 | NOT_REGISTERED | 接入前逐产品三项结论；WP2-00/主线程审阅`。
即审阅动作是既定的、授权方式是"主线程审阅"，本轮只是把它补在实现之前该在的位置上。

## 2. 页面定位过程（含失败的猜测，免得下轮重复）

先按常见路径猜的两个 URL 都是 404，没有内容可分析，故不引用：

- `https://www.cboe.com/us/indices/disclaimer/` → HTTP 404
- `https://www.cboe.com/us/privacy/disclaimer/` → HTTP 404

`https://www.cboe.com/legal/` 的链接清单才是可用的入口，它列出的相关项为
`Terms & Conditions: /terms`、`Use of Content: /use-of-content`、`Disclaimers: /global-disclaimers/`、
`Copyright, Trademark & Patents: /copyright`。据此取后两页作为第14行的依据。

## 3. 实际读到的文字（2026-10-08 页面呈现，逐句）

### 3.1 https://www.cboe.com/terms — "Terms and Conditions for Use of Cboe Websites"

- "The Materials on the Website are protected by copyrights, trademarks, service marks, and/or other
  proprietary rights and laws of the United States and other countries."
- "You may not otherwise copy, reproduce, alter, store either in hard copy or in an electronic
  retrieval system."
- "without Cboe's prior written consent except to the extent that such use constitutes 'fair use'
  under the 'Copyright Act of 1976', as amended from time to time."
- "The Website and the Materials are provided to you on an 'AS IS' and 'AS AVAILABLE' basis."
- "Cboe does not guarantee the accuracy, completeness, timeliness, legality, reliability,
  operability or availability of the Materials provided on the Website or any linked website."
- "Materials are provided for general informational and educational purposes only and are not
  intended for trading purposes."

呈现说明：该页第一条与第二条是同一段落被拉取工具按句切开的片段（原文里 "store either in hard copy or
in an electronic retrieval system" 之后接的是第三方材料的一句，"without Cboe's prior written consent"
是同一限制从句的后半）。**引用时按片段读，不把它们拼成一句新的条款**；第14行的结论只依赖
"复制/修改/存入电子检索系统须先有书面同意，fair use 为例外"这一条。

### 3.2 https://www.cboe.com/global-disclaimers/

- "No data, values, or other content contained in this document may be modified, reverse-engineered,
  reproduced, or distributed without prior written permission."
- "Cboe does not guarantee the accuracy, completeness, or timeliness of the information provided."
- "The Quotes Dashboard provides delayed quotes for the VIX index."
- "Index and benchmark values for the period prior to an index's launch date are calculated by a
  theoretical approach involving back-testing historical data."

### 3.3 https://www.cboe.com/use-of-content

只呈现两句："The information provided is for general education and information purposes only." 与
"No Cboe Company is an investment adviser or tax advisor..."。该页没有给出可用于本项目三种用途的许可
依据，因此第14行不引用它。

## 4. 三列结论是怎么从上面推出来的

| 登记表列 | 结论 | 依据句 |
|---|---|---|
| 允许本项目落库 | 未取得书面同意，暂不批准 | 3.1 存储须事先书面同意；fair use 例外是针对个别引用的限制，不能作为"把接口返回整体镜像进仓库"的许可基础 |
| 允许再分发 | 暂不批准 | 3.1 复制/修改须书面同意 + 3.2 "may be modified, reverse-engineered, reproduced, or distributed without prior written permission" 的否定式 |
| 允许商业使用 | 无许可依据，暂不批准 | 3.1 材料受版权/商标保护、按 AS IS 提供、明示不担保准确与及时、且声明不用于交易；页面未出现任何商业数据服务许可条款 |
| 状态 | 已复核（受限） | 审阅已完成，缺的是"公开条款没有授予的具体用途依据"，与第2、3、8、13行同一形态 |

## 5. 边界与没做的事

- 第14行**只覆盖本轮接线的两条公开延迟指数接口**，不覆盖 cboe 其余 9 个上游模型（`EquityHistorical`、
  `OptionsChains`、`FuturesCurve` 等）。它们接入前仍须逐产品另登。
- 公开条款审阅**不代替 `SOURCE_VERIFIED`**：本轮没有对 live endpoint 做过逐字段实测，
  两条能力的 `verified=False`，`live_verification_status` 仍 `NOT_RUN`，不进 `source="auto"` 路由。
- Cboe 指数数据的授权链条比网站条款更深（指数许可、再分发许可、逐产品定价）。本轮不推定任何指数许可，
  因此第14行的三种用途全部写作"暂不批准"，而不是"附条件允许"。
- 本轮没有因该结论释放任何删除动作：cboe 的 2 条能力只有源码与 fixture，仓库里没有来自 live endpoint
  的落库数据，故 §3 处置规则的"已落库数据按结论删除或归档"对本行没有适用对象。
