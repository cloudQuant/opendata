# wave-31 step 3 —— `government_us::WeatherBulletinDownload` 落地面（本仓第 289 枚离线验收行）

日期 2026-10-18（UTC 记录见各载档 `generated_at_utc`）。本节所有数字都来自 `runs/` 里载档的原文行，
引用形式 `runs/<载档>.txt:<行>`；每条载档自己带 argv、`stderr_bytes` 与 `<name>_exit=<数字>` 收尾行。
本轮无子代理（预算 30/30 已用尽），无 push，无 live 腿：`OPENDATA_ALLOW_LIVE_E2E` 未设置，socket
全程被封。

## 1. 结论面

| 量 | 落地前 | 落地后 | 载档 |
| --- | --- | --- | --- |
| plane 1 `offline_acceptance_count` | 288 | **289** / 350 | `runs/wave31-step3-judge.txt:5` |
| `identity_set_sha256`（前 16） | `5168e1e723999312` | 同一枚，未动 | `runs/wave31-step3-judge.txt:5` |
| `claim_count` | 1565 | 1565（未动） | `runs/wave31-step3-judge.txt:5` |
| `primary_review_count` | 288 | **289** | `runs/wave31-step3-judge.txt:5` |
| `INCOMPLETE` | 25 | **24** | `runs/wave31-step3-judge.txt:5` |
| government_us 已验收行 | 4 | **5**（`WeatherBulletinDownload` 在册） | `runs/wave31-step3-judge.txt:6-8` |
| government_us 残留 | 2 | **1**，只剩 `TreasuryPrices` | `runs/wave31-step3-readiness_census.txt:8-9` |
| 残留总数 | 62 | **61** = INCOMPLETE 24 + UNREVIEWED 37 | `runs/wave31-step3-residue_axis_census.txt:79` |
| 行动图 `add_code_carrying_guard` | 13 | **12**（tmx 1 + yfinance 11） | `runs/wave31-step3-residue_axis_census.txt:66-67` |
| plane 2 `declared/dev_done/missing_model_rows` | 17/17/19 | **同一代**，15/15 臂绿 | `runs/wave31-step3-plane2_dev_done_face.txt:144` |

落地动作是三处编辑：本仓 fetcher 补 1 条声明 + 2 枚守卫，见证模块加第 5 个 CONFIG 条目，另加
C128 那台残留轴仪器 `incomplete_row_guard_check.py` 的**负对照重指**——落地前它的
`CONTROL_NEGATIVE` 是 `government_us::WeatherBulletinDownload`，这一行落地后不再是"无码站点"，
对照臂会被自己消费掉，已改指 `tmx::EquitySearch`（面：
`runs/wave31-step3-residue_axis_census.txt:72` `B_landed_negative tmx::EquitySearch
verdict=NO_CODE_SITE PASS=True`；消费的两面读数在同一波新探针里，见 §4.6）。
产品码摘要 `weather_bulletin_download.py sha256:16=67a2d8af05f3dbc3`，见证模块
`government_us.py sha256:16=e33cac345f782837`（后者的上一代是 `0664bcb56828a59f`，见 §4.1）。

### 1.1 本波实际执行的五相

producer 对 government_us 全部 6 枚 inventory 行重跑五相（`--evidence-root docs/evidence/C101`）：
`runs/wave31-step3-producer.txt:30-34` 是新行 `WeatherBulletinDownload` 的 implementation / direct /
service / contract / counterfact 五腿，逐腿 `exit=0 status=PASS`；`:35` 是分母行
`claims emitted=30 (25 PASS, 0 FAIL, 5 BLOCKED) = 6 row(s) x 5 phases`，其中 5 枚 BLOCKED 全部是
`TreasuryPrices`——它在 shard 里保留但不绑定任何 fetcher，`reason=no model witness`
（`runs/wave31-step3-producer.txt:20-24`）。BLOCKED 是打印出来的，不是 skip：
`runs/wave31-step3-weather_bulletin_download_prevalidation.txt:31` 的
`PHASE_TALLY passed=25 failed=0 blocked=0 total=25` 是探针在临时证据根下自己跑的 25 腿（5 行 × 5 相），
与 shard 的 30 腿是两个总体，别把它们读成同一个数。

主审记录：`runs/wave31-step3-recorder.txt:4` `re-signing: dropped 4 earlier review(s)`，`:5-9` 五行
`REVIEW_SIGNED`，`:10` `signed=5 refused=0`。合并：`runs/wave31-step3-merge_take2.txt:4`
`shards merged=27`、`:32` `claims=1565 identities=313 reviews=289 accepted_identities=289`、
`:34` 两次取的稳定载荷 `take1=077b9a451d1ed980 take2=077b9a451d1ed980 identical=True`。

## 2. 产品码与见证项

`opendata/data/providers/government_us/models/weather_bulletin_download.py`（157 行）三处新增：

- `:96` `row_model: ClassVar[type[WeatherBulletinDownload]] = WeatherBulletinDownload`——只是声明，
  fetcher 自己不读它（§3.2 把两个读者分别驱动）。
- `:21-24` `DOCUMENT_MEDIA_TYPES = frozenset({"application/octet-stream", "application/pdf",
  "text/plain", ""})`：四个成员里 `""` 是"源站什么都不宣布"那一支，归一化在 `_client.get_bytes` 里做，
  不在 fetcher 里；`:128` 的 `Accept` 头只请求前三者。
- `:134-135` 空体守卫 `NWS_DOCUMENT_EMPTY_BODY`、`:136-137` 媒体类型守卫
  `NWS_DOCUMENT_BAD_CONTENT_TYPE`，都在受治理的 `get_bytes`（`:125`）**之后**，也就是都在
  `extract_data` 里、能把拒绝点归到契约列的一侧。

见证项 `scripts/quality/iter2_witnesses/government_us.py:412-431`：`fixture_document=_download_document`、
`client_seam=_SEAM`（`opendata.data.providers.government_us.models._client` 的
`get_shared_http_client`）、`query={"urls": "https://api.weather.gov/products/<id>/text"}`、
恰好一枚 `tamper {op: set, path: [responses, 0, content_type], value: "text/html"}`、
`refusal_class=GovernmentUSProviderError`、`refusal_code=NWS_DOCUMENT_BAD_CONTENT_TYPE`。
文档载体走 `body_b64`，因为 `get_bytes` 交回的是字节。

## 3. 反例与控制臂（20 臂，`runs/wave31-step3-weather_bulletin_download_prevalidation.txt`）

`:47` `PREVALIDATION_CHECKS arms=20 failed=0 failed_names=[]`，`:48`
`PREVALIDATION_DONE phase_failures_or_blocked=0 arm_failures=0`。逐臂：

1. `:4` `CREDENTIAL_ENV declared_names=['USDA_FAS_API_KEY'] was_unset=['USDA_FAS_API_KEY']
   already_set=[] values_printed=no`——只打印环境变量**名**，不打印值。
2. `:5` `materialized: fixtures=5`，临时根。
3. `:31` `PHASE_TALLY`（见 §1.1）。
4. `:32` `CONTROL_FIVE_SERVED`：五行各 `runtime_identity=True`，即用 `is` 比 fetcher 实发类型与
   域解析出的契约类型。
5. `:33` `CONTROL_DECLARE_REMOVED` 两个读者都驱动了：implementation 相
   `WitnessFailure: the bound fetcher publishes no typed contract row model`；direct 相
   `AttributeError: type object 'WeatherBulletinDownloadFetcher' has no attribute 'row_model'`
   站点 `iter2_claims_probe_runner.py:424:_run_direct_with_seam`；`restored_is_saved=True`
   （用 `is` 比回来，不是比名字）、恢复后 `rows=1`。
6. `:34` `CONTROL_MEDIA_TYPES`：四个成员各服务成功（含 `''` 与
   `Text/Plain; charset=utf-8` 这一支大小写/参数归一化），`application/json`、`text/html` 各从
   `weather_bulletin_download.py:137:extract_data` 拒绝。frozenset 是被**成员**定价的，不是被
   shard 里那一枚 tamper 定价的。
7. `:35` `CONTROL_EMPTY_BODY`：一枚零长应答从 `:135` 拒，同一载体的 33 字节控制体照常出票
   （`control_rows=1 control_bytes=33`）。这条臂存在的原因写在 `mechanism_note=one_tamper_slot_per_row`：
   runner 每行只取 `entry["tamper"]` 这一个 dict，第二枚守卫无法在 shard 里发表，只能在探针里咬。
8. `:36` `CONTROL_MULTI_URL_LOOP`：`urls=2 served=2 seam_calls=2 carrier_answerable=2 sends_granted=3`
   ——没有它，"守卫跑过"和"循环只跑了一次、根本没有循环"同样成立。
9. `:37-42` `CONTROL_QUERY_PRE_TRANSPORT`：5 种 query 形状（foreign host、11 枚 URL、空白、重复等）
   全部 `seam_calls=0`，类是 `ValidationError`，站点 `:109:transform_query`。"先于传输"在这里是
   `seam_calls=0`，不是散文。
10. `:43` `CONTROL_TAMPER_ONE_CELL`：五枚声明的 tamper 各自回到自己载体、逐叶 diff，全部
    `changed_cells=1`；新行那枚路径 `['responses/0/content_type']`。
11. `:44` `CONTROL_TAMPER_BITES`：五枚全部 `match=True`（自己的声明码、未改的控制体照常出票）。
12. `:45` `CONTROL_DOC_CITES`：`fetcher_lines=157 raise_sites_total=11`
    `bare_valueerror=[37,40,55,58,67,74,79,81,122]`
    `code_carrying=[(135,'NWS_DOCUMENT_EMPTY_BODY'),(137,'NWS_DOCUMENT_BAD_CONTENT_TYPE')]
    row_model_line=96 get_bytes_line=125 cited=[…,96,…,125,134,135,137] dangling=[]
    documented_but_uncited=[]`——docstring 的行号引用与磁盘上的模块重新对齐后才绿；§5.3 说明为什么
    新加的测试文件引用不会落进这台仪器。
13. `:46` `STATIC_VERSUS_RUNTIME`：C128 残留仪器对本行的 `verdict=DOCUMENT_TRIPPED`
    `counts={'extract_data': 2}` `raises=10` `code_raises=2` `trippable=2 undocked=ROW_CLASS_NOT_CONTRACT`
    （静态计数 10 与本探针模块级计数 11 是两个总体，见 §4.3）。

## 4. 判定器伪影与本波新发现

### 4.1 一次纯注释级编辑把整片 shard 的 pin 全部换了代

`runs/wave31-step3-pin_drift_pre_production_generation1_landing.txt:5-6`（落地那一代，链路开跑前）：
`STALE … pinned=7c0c9eeef6b1eea8 disk=0664bcb56828a59f`，
`pinned_files=17 stale_files=1 rows_with_pins=4
rows_without_pins=['TreasuryPrices','WeatherBulletinDownload']
drifted_rows=[CommodityPsdData, CommodityPsdReport, TreasuryAuctions, WeatherBulletin]`。
链路之后（`runs/wave31-step3-pin_drift_pre_production_rerun.txt:5`）
`pinned_files=19 stale_files=0 rows_with_pins=5 rows_without_pins=['TreasuryPrices'] drifted_rows=[]`。

第二代是我在本波内改见证模块 docstring 的字节出处表述之后重跑的：
`runs/wave31-step3-pin_drift_pre_production.txt:5-6`
`STALE … pinned=0664bcb56828a59f disk=e33cac345f782837`
`pinned_files=19 stale_files=1 drifted_rows=[全部 5 枚已见证行]`，
`pre_chain … offline_acceptance_count=289`（`:7`）。也就是说：一行 docstring 的措辞改动，代价是
5 枚行全部重新签名、producer/recorder/merge/judge 全部重跑。本波把两代都留在档里，改后的面
（`runs/wave31-step3-pin_drift_generation1_rerun.txt:5` 与
`runs/wave31-step3-pin_drift_pre_production_rerun.txt:5`）除 `generated_at_utc` 外逐字段相同，
这点用 diff 核过。

### 4.2 provider 自己的测试不能从干净 clone 跑（本波新发现，不在本波修）

新增仪器 `probes/c100_fixture_dependency_census.py`，面在
`runs/wave31-step3-c100_fixture_dependency_census.txt`：`:9`
`test_file=tests/test_government_us_iter2.py test_functions=9 dependent_on_ignored_root=7
in_file_helpers_reading_it=['_fixture'] clean=2`，`:10-16` 逐枚点名 7 枚依赖、`:17-18` 点名 2 枚干净，
`:19` `check_ignore_exit=0 rule=.gitignore:249:/docs/evidence/C100/** …`，
`:20` 反向对照 `CONTROL_NOT_IGNORED exit=1 rule=<none> … so_the_ignore_rule_is_specific=True`
（同一条 argv 指一个已提交路径必须**不**被忽略，否则 `check-ignore` 退 0 什么也不说明），
`:21` `CONTROL_BYTE_IDENTITY inline_sha16=e81fd18a79047014 fixture_sha16=e81fd18a79047014
test_literal_sha16=e81fd18a79047014 bytes=33 all_equal=True`，
`:22` `CONTROL_WITNESS_READS … read_calls=0 names_the_ignored_root_in_prose=True`，
`:23` `gates {5 项全 True} findings=[]`。

结论：`tests/test_government_us_iter2.py` 在 59/258/522 行打开
`docs/evidence/C100/gov-finra/fixtures/` 下的收据，全文没有任何 skip 标记，所以 9 枚测试里 7 枚
从干净 clone 起不来；被引为"该测试期望这 33 字节"的断言本身是内联字面量（`:551`），
所以这条期望与那 33 字节的相等关系是可核的，但那枚测试**跑不起来**。本波落地的行不受影响：
见证模块 `read_calls=0`，字节内联。这条发现登记为待修，不在本波动 `tests/`（与 #60 的
batch5 钉住 15 条 gitignored 路径是同一族的复现性问题）。

### 4.3 两台仪器对同一个 fetcher 给出不同 raise 计数，是总体不同不是分歧

`CONTROL_DOC_CITES` 的 `raise_sites_total=11` 是模块作用域全部 raise 站点；C128 残留仪器的
`raises=10` 只走 `type(fetcher).__mro__` 上 `opendata.*` 自有类、且只把带 SCREAMING_SNAKE 字面量的
站点归桶。两个数各自可复算，不能互相"核对"。

### 4.4 batch5 源钉围栏：`exit=1` 是设计内拒绝

`runs/wave31-step3-batch5_fence_dry_census.txt:9` `face: matched=31 stale=0 missing=0`，`:11`
`batch5_fence_dry_census_exit=1`——该 argv 在 `stale=0` 时按设计退 1（`--apply` 故意没给，只读）。
把它读成红灯是错的，读成 0 条陈旧也是错的。

### 4.5 plane 2 面未动，且我修回了自己刚按下的一把老化

`runs/wave31-step3-plane2_dev_done_face.txt:144` 的 `arms=15 arms_passed=15 … PASS=True`
与 step-0 面一致（该探针固定写
`docs/evidence/C131/wave31-plane2-face-20261018/runs/wave31-step0-plane2-axis-census.txt`）。
第一次复跑时我让这条固定路径把 step-0 的 `C_cross_plane_join` 从 `accepted=285 residue=65`
就地改成了 `289/61`，也就是用本代读数**覆盖了 step 0 的历史面**。发现后先把当代字节复制进本波
runs（`:45` 是本波那一代的 `289/61`），再用 `git checkout --` 把 step-0 载档还原（现在是
`accepted=285 residue=65`，与 HEAD 一致），并在载档头部把这件事写明。这一步我自己又做错一次，
见 §7.5。

### 4.6 这次落地把 C128 那台仪器的负对照吃掉了，消费与重指在同一次运行里两侧可读

C128 的 `incomplete_row_guard_check.py` 用 `CONTROL_NEGATIVE` 当 arm B 的负对照，自写作之日起该
常量就是 `government_us::WeatherBulletinDownload`。本波给这枚 fetcher 加了 `row_model` 与两枚
post-response 守卫，于是它的 verdict 从 `NO_CODE_SITE` 移进 `TRIPPABLE` —— 也就是说，**落地这枚行
的动作，同时退休了为"这枚行不可见证"而写的对照臂**。这条不是口头断言：`probes/consumed_control_rearm.py`
把 C128 那台仪器**按 import 拿来**（不复制分类器），在同一进程里对两个标签各跑一次谓据：
`runs/wave31-step3-consumed_control_rearm.txt:6` `A_consumed_control … verdict=DOCUMENT_TRIPPED
trippable_now=True old_arm_b_would_pass=False`（旧臂在当前树上已不可达 True），
`:7` `B_rearmed_control tmx::EquitySearch verdict=NO_CODE_SITE class_td=0 package_td=6 PASS=True`
（新臂活，且 arm C 的 class-vs-package 比较随迁仍成立），
`:8` `C_attributable … code_raises=2 extract_sites=2 guard_raise_lines=[135, 137]
judge_status=OFFLINE_ACCEPTED`（移动可归因于本波那两枚守卫所在行，而不是分类器改动），
`:9` `CONTROLS_DIFF … readings_differ=True`，`:10` `arms=3 arms_passed=3 gates={…全 True}
all_pass=True`，`:11` `consumed_control_rearm_exit=0`。总体面 `:5` `report_rows=350 judged=350
incomplete=24 old_in=True new_in=True`——一台仪器读到的还是同一份 350 行报告，只是两枚标签的角色换了。

选择 `tmx::EquitySearch` 的理由也是量出来的：24 枚 INCOMPLETE 里只有两枚 bound
（`runs/wave31-step3-incomplete_row_guard_check.txt:77` 的
`verdicts={'NO_CODE_SITE': 1, 'ROW_CLASS_NOT_CONTRACT': 1, 'UNBOUND': 22}`），其中只有它的
class-scoped `transform_data` 为 0（`:40` 该行 `class_counts={'transform_data': 0, …}`，同包
screen 是 6）；`federal_reserve::FomcDocuments`（`:12`，`transform_data` 5）会让 arm B 通过而把
arm C 变成空判，22 枚 unbound 则会让 arm B 天然为空。落地后那台仪器自身的两把面未重取：把注释改完
再跑，body 与载档逐行相同（唯一差异是驱动追加的 `incomplete_row_guard_check_exit=0`），所以历史面
仍是当代字节；仪器字节从 HEAD 的 `96c2ea82968d5474`（29282 B）变到 `50c31c041e90c80e`
（31226 B），变的全是散文。它不被任何 receipt pin 覆盖：`runs/wave31-step3-receipt_pin_consistency.txt:8`
`pin_face slots=20624 stale=0 … distinct_pinned_paths=586`，而对 `docs/evidence/C101/` 全树
grep 该文件名 0 命中，所以这次编辑不欠五相重签。

## 5. 对 step-0/step-2 预测的更正

1. **step-2 的谓据成立**：`docs/evidence/C131/wave31-step2-…/face.md:144` 写"若下一枚是
   `government_us::WeatherBulletinDownload`，plane 1 288 → 289"。实测 `288 → 289`
   （§1），且 `add_code_carrying_guard 13 → 12` 也如 step-0 定价的那样只掉这一枚。
2. **见证模块 docstring 的三条旧断言中，两条被本波推翻、一条存活**：
   - 旧："该证据根里没有 FAS 收据"。**错**：`usda_psd.json`（1,237 字节，sha256 `55db6e5c6e8e2911`）
     与 `usda_report.pdf`（51 字节 magic-only，sha256 `904636248025ad20`）都在，只是形状与本见证
     不同（合并对象 + stub 命名空间）。存活的窄断言是：本波两枚 PSD 体**不是**它们的转录。
   - 旧："`WeatherBulletinDownload` 未声明 `row_model`、没有任何响应后守卫，所以 shipped counterfact
     永远够不着它"。**已不成立**：本波声明 `:96`、加了 `:135/:137`。
   - 存活的机制断言：`:37,40,55,58,67,74,79,81` 与传输前的 URL 收窄 `:122` 仍是裸 `ValueError`，
     仍在任何 seam 被咨询之前 firing，所以 `tamper.path` 从 `responses` 起头的机制确实够不到它们——
     这一条与本行的可见证性无关，只说明这 9 枚 raise 没有一枚被见证。
   - 旧："所有四枚拒绝都是有意的 code-only"。现在是**五枚**（新增
     `NWS_DOCUMENT_BAD_CONTENT_TYPE`），措辞已按实测计数改写。
3. **新增的测试文件引用为什么不会触发 `CONTROL_DOC_CITES`**：这台仪器的裸引用模式带
   `(?<![\w.])` 前瞻排除，`tests/test_government_us_iter2.py:551` 的冒号前是 `y`，被排除；而
   "reads it at :59" 这种裸写会被当成**关于 fetcher** 的引用，`:522` 落在 157 行的模块外就成了
   `dangling`。我把那句改成 "opens it on lines 59, 258 and 522"（无冒号）来避开这个歧义，并把这条
   相互作用记在 §7.4——仪器的排除规则是我上一代写的，这一代的散文差一点把它踩响。
4. **`row_model` 有两个读者**，且都不是 fetcher：implementation 相（`WitnessFailure`）与 direct 相
   （`iter2_claims_probe_runner.py:424` 的裸 `AttributeError`）。任何"补个声明就算落地"的说法必须
   同时给出这两条面，§3 第 5 臂就是为这件事写的。

## 6. 残留 61 行的行动图与下一波谓据

`runs/wave31-step3-residue_axis_census.txt:79`：`residue=61`
`actions={'add_code_carrying_guard': 12, 'implement_missing_model': 19, 'ledger_ruled_out': 11,
'new_provider_package': 2, 'retarget_fetcher_output': 2, 'shard_entry_needs_harness': 15}`，
`:67` 按 provider 拆：guard = `{'tmx': 1, 'yfinance': 11}`，
implement = `{'government_us': 1, 'tmx': 16, 'yfinance': 2}`。
`:73` `C_population_identity residue=61 report_rows=350 nested_sum=61 PASS=True`，
`:69` `5_needs_first_module no_witness_module={'add_code_carrying_guard': 11, …} rows=37 of=61`，
`A_landed_positive controls=2 PASS=True`、`B_landed_negative tmx::EquitySearch
verdict=NO_CODE_SITE PASS=True`（`:71-72`）。
`runs/wave31-step3-readiness_census.txt:7` buckets `NEED_BINDING 32 / NEED_DECLARE 14 /
NEED_SHARD_ENTRY 15`，`sums_to_residue=True`；`runs/wave31-step3-seam_reachability_census.txt:40`
`SEAM_REACHABLE 22 / SEAM_NEVER_USED 37 / NO_PACKAGE_ON_DISK 2`，
`accepted_plus_residual=350`。
`runs/wave31-step3-incomplete_row_guard_check_retake.txt:77`
`incomplete=24 convertible=0 verdicts={'NO_CODE_SITE': 1, 'ROW_CLASS_NOT_CONTRACT': 1, 'UNBOUND': 22}`。
`runs/wave31-step3-receipt_pin_consistency.txt:8` `slots=20624 stale=0 distinct_pinned_paths=586`。
`runs/wave31-step3-pytest_government_us_and_pin_planes.txt:17` `700 passed, 179 skipped`；
`runs/wave31-step3-pytest_collect_denominator_retake.txt:11` `collected 879 items`，
700 + 179 = 879，没有样本掉进 skip 里。

**下一波谓据（按代价排序，本波只定价、不动手）**：

- 唯一一枚" seam 可达 + 该 provider 已有见证模块 + 不需要新写守卫"的残留行是
  `federal_reserve::FomcDocuments`（`runs/wave31-step3-residue_axis_census.txt:9`：
  `bucket=NEED_DECLARE verdict=ROW_CLASS_NOT_CONTRACT seam=SEAM_REACHABLE mechanism=client_seam
  witness_module=True fake_ok=True`，动作 `retarget_fetcher_output`）。它的门是**映射裁决**
  （发布列 7 个 macro vs 5 个契约列，即未结的 #68），不是见证模块，也不是新守卫。
- 谓据：若下一波做这枚 retarget，plane 1 289 → 290、`retarget_fetcher_output 2 → 1`、
  `NEED_DECLARE 14 → 13`，`federal_reserve` 残留 1 → 0。
- 复算配方（下一条落地前必须自己跑一遍，不要引用本节数字）：
  `PYTHONPATH=. <py313> scripts/quality/iter2_claims_producer.py --provider federal_reserve
  --output docs/evidence/C101/federal_reserve/claims.json` → recorder 覆写该 provider 全部已见证身份
  → `iter2_claims_merge.py` → `iter2_model_evidence.py --inventory <清单 csv> --claims
  docs/evidence/C101/claims.merged.json --output docs/evidence/C101/judge/claims.merged.report.json`。
- 失效条件（任一成立则本谓据作废，需重取行动图）：`accepted + residue ≠ 350`；身份集不再是
  `5168e1e723999312`；arm F `witnessable_today` 从 `count=0` 变成 >0（意味着 harness 面上来了新的
  机制，批次选择要重算）；`federal_reserve` 的 6 枚已验收行里有任一枚被本波之外的编辑换了 pin。
- 失败读法：producer 里该模型任一相 `exit≠0` 时，先查 `row_model` 与发布键集的关系（service 腿要求
  键 ⊆ `row_model.model_fields`）与契约列对照表，**不要**放宽 counterfact 的声明码；若 judge 反而
  把别的 federal_reserve 行打成 `claim_receipt_hash_drift`，那是 §4.1 的重签名税，唯一正确处置是
  整片 shard 重生产 + 重签，不是把 pin 追改到磁盘字节。

## 7. 我错的部分（原文留在档，不改写）

1. **pin-drift 那句"链路之后必须为空"是错的**。原句
   "and after the chain has run it reads stale_files=0 and drifted_rows=[]" 的前一代版本断言
   `rows_without_pins=[]`。复测读到 `rows_without_pins=['TreasuryPrices']`——那枚保留行不绑定任何
   fetcher，天生没有 pin。已改成"链路之后应只剩 `TreasuryPrices`"，两条面并排留在 §4.1。
2. **guard-check 那条注把自己没打印的量说成打印了**。原句
   "the verdict histogram is the row this probe prices next"（step-2 驱动里）以及本波初版
   "the action-map fall this landing was priced for is printed here"。这台仪器打印的是第二轴 verdict
   直方图，**不打印**行动图；`add_code_carrying_guard 13 → 12` 的落点在同一链路的
   `residue_axis_census` 载档里。注已改写，两条臂的面各在其位。
3. **"把见证 CONFIG 加到第 5 枚会把 collected 分母推高"是从未测过的断言**。原句
   "so the denominator leg moves when a fifth model joins the witness CONFIG"。读者普查
   （`git -c core.quotepath=off grep -l iter2_witnesses -- '*.py'`）：16 个被跟踪的 `.py` 提到它，
   其中 **0 个在 `tests/` 下**（同一命令加 `-- 'tests/*.py'` 计数 0）；也就是说没有任何 parametrized
   `test_*` 集合遍历见证 CONFIG，"落地会把 collected 推高"这句从一开始就没有承载它的仪器。我能证的
   只有当代这一格：`runs/wave31-step3-pytest_collect_denominator_retake.txt:4` `collected 879 items`
   与 `runs/wave31-step3-pytest_government_us_and_pin_planes.txt:17` 的 `700 passed, 179 skipped`
   相加闭合（700 + 179 = 879），所以没有样本消失进 skip。**"与落地前同一"我删掉了**——落地前的
   collected 在本仓没有任何载档（对 C131/C130/C128 的 runs 全量 grep `collected [0-9]+ items`
   0 命中），没有前代面就不许写差值。注已换成上面这条闭合读法，并用 `--stage retake` 重取了载档
   （`runs/wave31-step3-pytest_collect_denominator_retake.txt:1` 头部就是这条更正）。
4. **普查探针初版有三处会让它自己变瞎**：(a) `ast.Keyword` 不存在（应是 `ast.keyword`），第一次跑
   直接崩在写载档之前，runs/ 里因此没有半成品；(b) "见证模块是否读 gitignored 路径"我最初写成
   子串匹配 `IGNORED_ROOT in source`，而我正在修的那句 docstring **恰好含这串字面量**——文本判据会
   把我自己的修正读成缺陷，改成 AST 统计 `read_calls`；(c) `root_is_ignored` 臂读 `lines[-2]`，而
   `-2` 是 `CONTROL_BYTE_IDENTITY` 那行，与被检的规则无关——改成直接绑 `ignored` 布尔；同时补上
   §4.2 的反向对照，否则 `check-ignore` 退 0 本身不说明任何事。另外 `ruff --fix` 把 SIM114 合成一行
   141 列的 `if`，`_read_calls` 重写为嵌套谓词 + 推导式。
5. **我把自己的 plane-2 载档覆盖成了 step-0 的字节**。为了不把 step-0 面就地老化，我在一次命令里
   排了"跑探针 → 还原 C131 → 复制"，`cp` 在 `git checkout` 之后，于是复制回来的是**已还原的
   step-0 字节**，而头部散文声称它是本代。`diff` 当场暴露（`accepted=285 residue=65`），
   重排为"跑探针 → 复制 → 还原 → 写载档"后重建，`:45` 现在真是 `289/61`。这条与 §4.5 是同一个
   动作的两半，我把它单列出来，因为"顺序错了也绿"的失败比"顺序对了但红"更贵。
6. 链路驱动在上一段会话里被我用脚本搬 `VERIFICATION_LEGS` 时搬坏（`return` 掉在函数外 + 循环重复
   一份），该文件是本会话新建且未跟踪，我删掉重写了整份；重写时又打错一个转录
   （`take1={t2}`），在落盘前改成 `take1={t1} take2={t2} identical={t1 == t2}`，与原始 merge 载档
   的字段一致（`runs/wave31-step3-merge_take2.txt:34` 可反查）。
7. **我为负对照重指写的那段注释里，有一句把没载档的红说成载了，还有一句把"二选一"写成"唯一"**。
   原句（同一份 C128 仪器注释的第一版）
   "arm B read PASS=False over a classifier that had not changed behaviour" —— 我把一次只在 stdout
   里见过、从未归档的读数写成了既成事实；对全 `docs/evidence/` grep `B_landed_negative.*PASS=False`
   0 命中（该臂其他行的历史红面是有的：`PASS=False` 在被跟踪的 `docs/evidence/*` 里共 22 把载档、
   38 处，但没有一处属于这条臂），所以这句话当时没有承载物。现在换成同代可复算的两面：
   `probes/consumed_control_rearm.py` 一次进程里读旧标签 `DOCUMENT_TRIPPED`
   （`old_arm_b_would_pass=False`）与新标签 `NO_CODE_SITE`
   （`runs/wave31-step3-consumed_control_rearm.txt:6-7`），见 §4.6。第二句原句
   "``tmx::EquitySearch`` is the only other *bound* non-trippable residue row left" 把候选说成唯一：
   bound 且 non-trippable 的 INCOMPLETE 行有 2 枚（`NO_CODE_SITE` 1 + `ROW_CLASS_NOT_CONTRACT` 1，
   `runs/wave31-step3-incomplete_row_guard_check.txt:77`），`FomcDocuments` 也在其中；真正把它排除的
   是 arm C 需要 class-scoped `transform_data == 0`，而 `FomcDocuments` 是 5（`:12`）。注释已按这条
   量出来的理由改写。两处都是散文级编辑，仪器字节 `96c2ea82968d5474` → `50c31c041e90c80e`，改完后
   重跑仪器 body 与既有载档逐行相同（差的只是驱动追加的 `..._exit=0`），所以没有面被这次改写老化。

## 8. 载档清单

`docs/evidence/C132/wave31-step3-government-us-weather-bulletin-download-20261018/`

- `probes/weather_bulletin_download_prevalidation.py` —— 20 臂 + 25 相腿（§3）
- `probes/w31s3_run_chain.py` —— 落地与验证链驱动；`--stage pin-drift` 复取漂移表（写在自己命名的
  旁文件，不覆盖历史），`--stage retake --leg <name>` 用**同一份** `VERIFICATION_LEGS` 定义重取单腿
- `probes/c100_fixture_dependency_census.py` —— §4.2 的复现性普查（5 把自门闸）
- `probes/consumed_control_rearm.py` —— §4.6 的负对照消费/重指两面读数（3 臂 + 6 门闸）
- `runs/`（本波提交的 28 把面，`wave31-step3-` 前缀一律省略）：`producer`、`recorder`、
  `merge_take1`、`merge_take2`、`judge`、`pin_drift_pre_production`（第二代）、
  `pin_drift_pre_production_generation1_landing`（落地那一代）、`pin_drift_pre_production_rerun`、
  `pin_drift_generation1_rerun`、`weather_bulletin_download_prevalidation`、`prevalidation_face`
  （同一次运行的无头部前代，留作 §3 那 20 臂的字面旁证）、
  `incomplete_row_guard_check`（+ `_retake`）、`readiness_census`、`residue_axis_census`、
  `seam_reachability_census`、`receipt_pin_consistency`、`batch5_fence_dry_census`、`ruff_check`、
  `ruff_format`、`mypy`、`bandit`、`pytest_government_us_and_pin_planes`、
  `pytest_collect_denominator`（+ `_retake`）、`plane2_dev_done_face`、
  `c100_fixture_dependency_census`、`consumed_control_rearm`
- 除本文件外还提交：`docs/evidence/C128/plane1-residue-readiness-20261017/probes/incomplete_row_guard_check.py`
  （`CONTROL_NEGATIVE` 重指 + 注释改写，`96c2ea82968d5474` → `50c31c041e90c80e`）、
  `opendata/data/providers/government_us/models/weather_bulletin_download.py`、
  `scripts/quality/iter2_witnesses/government_us.py`，以及 C101 证据根的
  shard/review/receipt/merge 产物与 `probes/__pycache__` 之外的两份 tmx step-2 收尾载档
- 不提交：`runs/.chain2.log`、`runs/wave31-step3-prevalidation_face.txt.err`（`*.err` 兄弟与运行日志
  按既有裁定留在工作树）、`probes/__pycache__/`，以及 `docs/evidence/C100/**` 原始回执
