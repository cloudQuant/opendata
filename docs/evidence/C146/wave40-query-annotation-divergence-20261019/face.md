# Wave 40 —— query 注解与发布 schema 的两条通道

这一轮不是新功能，是把 wave 39 收口时发现的一条**真实运行时缺陷**修掉，并把修不掉的那一大片量清楚。
所有数字都来自本 bundle `runs/` 里的载体，或本文件写明的重跑命令。

## 1. 起点：一条 accepted 行会让 catalog 的 query-schema 面直接崩

`/api/v1/data/catalog` 发布每个能力可调用的参数表时，走的是
`opendata.services.provider_models.resolve_query_model_type(fetcher)`——它读静态的
`query_model`/`query_type` 声明，**从不求值注解表达式**。而 `tests/test_data_catalog.py` 用第二条通道
交叉核对：`get_type_hints(type(fetcher).transform_query)["return"]`。两条通道在注解写的是具体模型时一致；
写的是 `BaseModel` 或过时的窄模型时不一致；而注解引用了一个**只在 `TYPE_CHECKING` 下才存在**的名字时，
第二条通道根本求不出来。

wave 39 收口时这条测试死在这一行（本轮 14:37Z 复现）：

```
NameError: name '_EconomicCalendarQuery' is not defined
  tests/test_data_catalog.py:526: query_model = get_type_hints(type(fetcher).transform_query).get("return")
```

受害的是 `tradingeconomics::EconomicCalendar`——一条 `OFFLINE_ACCEPTED` 行。同期另一条
`benzinga::PriceTarget` 是同一病因的第二种形态：`from pydantic import BaseModel` 只在 `TYPE_CHECKING`
分支里 import，注解却用它，于是 `NameError: name 'BaseModel' is not defined`。

## 2. 两处修复，以及"修好了"是怎么被证明的

`tradingeconomics`：在运行时的 `else` 分支里把引擎生成的 query 模型绑到那个注解名上——
`_EconomicCalendarQuery = _GeneratedCalendarFetcher.query_model`。静态视图（`TYPE_CHECKING` 下那个类）
保持不动，所以 mypy 仍按静态视图检查，而运行时 `get_type_hints` 解析到**fetcher 真正验证的那个类**。

`benzinga`：把 `BaseModel` 提到运行时 import，并按仓里已有先例标注
`# noqa: TC002  # get_type_hints resolves this annotation at runtime`（同类先例：`opendata/api/keys.py`
的 `# noqa: TC002  # FastAPI resolves it at runtime`）。注解仍写 `BaseModel`，与本 provider 其余 fetcher
（`benzinga/models/iter2.py:181`）保持一致。

两条通道的读数（`runs/w95_query_annotation_face.txt`，重跑：
`python docs/evidence/C146/.../probes/w95_query_annotation_face.py`）：

```
E_repaired tradingeconomics::EconomicCalendar  state=resolvable  annotation_class=EconomicCalendarQuery
                                              published_fields=8  annotated_fields=8  channels_agree=true
E_repaired benzinga::PriceTarget               state=resolvable  annotation_class=BaseModel
                                              published_fields=8  annotated_fields=0  channels_agree=false
```

`benzinga` 那行 `channels_agree=false` 不是没修好，而是它属于下面第 3 节的分叉类：注解写的是基类，
发布面走的是具体模型。它的 `NameError` 消失了，通道差异保留在册。

**"机制真的会咬"是对着活模块双向验的**，不是回忆：把模块命名空间里的名字临时摘掉再求值，
必抛 `NameError`；放回去就解析成功（脚本里 `try/finally` 恢复）。

```
B_forge tradingeconomics::EconomicCalendar  with_name=resolved  without_name=NameError:name '_EconomicCalendarQuery' is not defined  restored=true
B_forge benzinga::PriceTarget               with_name=resolved  without_name=NameError:name 'BaseModel' is not defined                restored=true
```

## 3. 修不掉的那一片：114 行两条通道给出不同的 schema

对 350 个已注册模型描述符逐条比对两条通道（同一份 `w95` 载体）：

```
A_population descriptors=350 outcomes={"diverge": 114, "identical": 236}
A_divergence_shapes {"base_model": 103, "missing_fields": 11, "other": 0}
C_diverge_by_provider={"benzinga": 4, "econdb": 5, "finviz": 5, "fmp": 35, "fred": 22,
                       "intrinio": 18, "tiingo": 6, "tmx": 16, "yfinance": 3}
```

* **103 行**：注解仍是 `BaseModel`（或同类无字段基类），发布面却是具体模型——参数表在注解通道里读成空。
* **11 行**：注解指向一个比发布模型**窄**的模型（少字段，例如 fred 的 `maturity`）。
* 分叉涉及 **19 个模块 / 9 个 provider**。

代价也量了（同一载体 D 臂，按 merged claims 的 pin 站点算）：

```
diverging_modules=19  pin_sites=740  total_claims=1725
claims_by_provider={"benzinga": 20, "econdb": 25, "finviz": 25, "fmp": 210,
                    "fred": 120, "intrinio": 110, "tiingo": 30, "tmx": 80, "yfinance": 120}
```

740 / 1725 个站点分散在 9 个 provider 上，**没有一个 provider 独占全部**——所以这件事的代价是
**9 次 provider 级重签**（每次约 2 分钟，wave 37/39 都实测过），不是一次全树 45 分钟。这仍是跨 provider
的产品改动，**本轮没有做**：它需要逐行确认注解与 fetcher 实际验证的模型一致，而不是批量替换。D 臂里那个
"如果一个 provider 独占全部 pin 站点就该读出 tree_wide"的伪造臂，就是防止把"provider-local"写成一句猜测。

**复查（2026-10-20，逐行看过 11 个 `missing_fields` 之后，前面的"待统一"判断要收窄）：** 这 11 行不是
"注解撒谎"，而是**共享基类方法**的注解只报了基类模型。以 fred 为例：
`_FixedFredRateFetcher.transform_query -> _FixedRateQuery`，而三个子类各自 `query_model =
TreasuryConstantMaturityQuery / SelectedTreasuryBillQuery / SelectedTreasuryConstantMaturityQuery`
（都继承 `_FixedRateQuery` 并各自多一个 `maturity`）。返回子类实例给"基类"注解是**类型正确**的，
只是不如发布面具体；一个被三个 binding 共用的方法，任何单一注解都不可能同时等于三个不同的发布模型。
所以 11 行若要"修"，只能在每个子类里重写一遍 `transform_query` 只为换一个更具体的返回注解——那是
type-only 噪音，不是缺陷修复。103 行 `BaseModel` 同理（引擎构建的模型没有静态名字）。

因此 owner 要决定的不是"要不要改 114 个适配器"，而是：**AC-18 的第二条通道要不要要求 per-binding 的
具体注解**。要，就得接受上面那种逐子类重写；不要，就让该断言改为与产品同源（那是改判定，必须 owner 签）。
在这条裁定之前，任何一边都不该由我默默改掉。

## 4. 重签半径：10 个 pin 站点，两个 provider，平面 1 回到 339

两个适配器文件都是被 pin 的输入，改字节就让钉它们 claim 失效。半径是量出来的（`w95` F 臂）：

```
pin_sites_by_target={"benzinga/models/price_target.py": 5, "tradingeconomics/models/economic_calendar.py": 5}
claims_touching_targets={"benzinga": 5, "tradingeconomics": 5}   total_claims=1725
```

重签走 wave 37 的链驱动（`w96_two_provider_resign.py` 只重指 `BUNDLE`/`RUNS`/`PRE_CHAIN_OUTPUT`，
两个 provider 各 producer→recorder，然后 merge 两次、judge 一次），读数：

```
stale_face_before stale_sites_total=10  (benzinga 5 + tradingeconomics 5)
stale_face_after  stale_sites_total=0   pin_entries_without_digest=0
merged_stable_sha  first == second       merge_idempotent=true
members            7/7 exit 0
plane 1            350 identities / 339 OFFLINE_ACCEPTED / 1725 claims
identity_set       5168e1e723999312（未变）
```

七条 arm 全绿（A 失效非零且仅这两个 provider、B 重签后归零、C merge 幂等、D 全成员 exit 0、
E 平面 1 回到 339、F 身份集不变、G 无缺摘要站点）。E 臂的方向很重要：重签**只能**让行数回到 339，
多一行或少一行都是回归而不是修复。

## 5. 这一轮明确没有做什么

* **没有**把 114 行的注解批量改成具体模型（见 §3 的代价与理由）。
* **没有**改 `tests/test_data_catalog.py` 的第二条通道，也没有改 `resolve_query_model_type`。
  两条通道的差异是这条测试的价值所在；把断言改成和产品同源，会让它再也看不见这类分叉。
* **没有**动 AC2-10 的谓词、两个判定常量或台账标签（owner 裁定见 C145 `face.md` §15）。
* `tests/test_data_catalog.py` 在本轮之后**仍然有一条红**（`test_ready_provider_domains_publish_exact_query_service_metadata`），
  红的原因是 §3 的 114 行分叉里恰好落在该测试遍历范围的那一行，不是本轮修的两处 NameError。

## 6. 载体清单

| 载体 | 内容 |
| --- | --- |
| `runs/w95_query_annotation_face.txt` | 7 条 arm：分母、双向 forge、分叉分档、半径与伪造半径、两条修复行、重签后 pin 面 |
| `runs/w96_two_provider_resign.stdout.txt` / `runs/chain_summary.wave40.txt` | 两 provider 重签的逐成员 exit、pin 面 before/after、平面 1 读数 |
| `runs/w97_a2_slice_two_files.txt` | 两个产品文件的 A2 切片（ruff/format/mypy/bandit；唯一红仍是 by-construction 的 pinned gitleaks） |
| `runs/producer_*.txt`、`recorder_*.txt`、`merge_*.txt`、`judge.txt` | 重签链的逐成员输出 |

## 7. 收口面与测试平面：这一轮没有移动测试面

对 `742517ac9`（本波 docs 提交，53 个路径）跑 wave 39 的收口面
（`runs/w101_closure_face.wave40.stdout.txt`，8/8 arms PASS）：

```
reconcile commit_paths=53 moved_vs_parent=53 worktree_drift_vs_commit=0 missing_in_commit=0
gitleaks git-mode findings=0  planted findings=2  arm_can_move=True
gitleaks dir-mode(copied_from_commit=53) findings=0  planted 2
secret_scan_check rc=0  463 commits / 391.85 MB / no leaks found
quotepath_off head_paths=53 nonascii_raw=0
```

最后一行要如实说明：这次载荷里**没有非 ASCII 路径**，所以 `nonascii_raw=0` 是一个空分母，
它不证明 quotepath 覆盖生效——覆盖是否生效的证据在 wave 39 的 §20（当时载荷里真有 CJK 路径，
读数 `nonascii_raw=1` 并打出了原始路径）。

测试平面用全量跑一遍（`runs/test_plane_w40_final.txt` / `.xml`，12:39）：

```
wave 40 = 28 failed / 26335 passed / 271 skipped    collected=26634
wave 39 = 28 failed / 26335 passed / 271 skipped    collected=26634
节点集合：shared=28  new=0  fixed=0                 注入红臂可咬
```

两轮的失败集合**逐 id 相同**，collected 也相同（`w103` 5/5 arms）。也就是说：这一轮的两个
运行时注解修复改变了 `get_type_hints` 的可用性（`NameError` 消失、calendar 两通道一致），
但**没有移动测试面的任何一行**。`tests/test_data_catalog.py` 那条红在两个平面里都在，
病因是 §3 的 114 行分叉；修复前它以 `NameError` 失败，修复后它以分叉类的字段差异失败——
同一个 node id，不同的失败文本。这一点必须说清楚，否则"集合相同"会被误读成"什么都没修"。

