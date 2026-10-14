# C101 — 波后整合：census 按盘上重钉、descriptor 判据补上 11 条 legacy、门禁成员 2 由红转绿

日期：2026-10-14　分支：`dev`　基线提交：`270e373411951be1752d71206379da98762d5081`
解释器：`envs/py313` CPython 3.13（门禁解释器）

结论先行：**注册面 census 从盘上重算并钉进 `tests/fixtures/registry_batch5_snapshot.json`
（digest `3755f13aaba43989373aa44c6ca3f43fa9aabd2e0b2681b100628cac0d9a7bc3`）：338 域 / 318 query-semantic /
351 绑定身份 / 329 descriptor-eligible / 22 model_key-only；语义字段 `contract`、`verified` 的迁移数
钉死为 0/0，29 条 `scenario_present` 与 10 条 `binding` 的移动逐条归因到 `360c61d` 之前的字节面；
descriptor 判据补掉了它在 HEAD 上本来就红的缺口（328 vs 317，11 条 `ths` legacy 绑定同样声明 canonical
model id，`catalog.py:193-195` 的注册条件只看 `canonical_model_ids` 是否为空）；四个消费文件 117 条断言转绿；
`make zero-dep-check` 由 exit=2 转 exit=0（8 个过期登记摘要重钉 + census 348→580 重冻，findings 仍为 0）。
两条红按"记录不翻转"留在原处：`brand-check=2` 归用户侧未跟踪许可草稿（C98 已归因），
`evidence-traceability=2` 的 70157 条 untracked 里 68863 条属于不入仓的 C100 原始凭证。**

## 这一轮做了什么，正文在哪

| 面 | 正文 | 复算命令 |
| --- | --- | --- |
| 注册 census 重钉 + descriptor 判据 | `census-rebase/face.md`、`census-rebase/verify.txt`、`census-rebase/arms.txt` | `python docs/evidence/C101/census-rebase/probes/verify_census_rebase.py`、`python docs/evidence/C101/census-rebase/probes/falsify_census_arms.py` |
| zero-dep 摘要重钉 + 扫描面重冻 | `zero-dep-repin/face.md` | `python docs/evidence/C101/probes/repin_reference_policy.py`、`make zero-dep-check` |
| 波后 plane-1 判定（5 阶段 claim + 一审） | `judge/claims.merged.report.json`、`judge/report.md` | 由 `scripts/quality/iter2_model_evidence.py` 对 `claims.merged.json`（850 行）读数 |
| 各 provider 分片 claim / fixture | `<provider>/`、`fixtures/`、`captures/`、`receipts/` | 见各 `judge/claims.reviewed.report.md` |

## 已量出来的完成度（不是口径，是读数）

- plane 1（逐条 claim 面）：`docs/evidence/C101/judge/claims.merged.report.json` 读
  `claim_count=850 / identity_count=350 / offline_acceptance_count=96 / primary_review_count=96`，
  状态分布 `{INCOMPLETE: 74, OFFLINE_ACCEPTED: 96, UNREVIEWED: 180}`。**350 个在册身份里 180 个没有任何 claim 正文**，
  分布在 19 个 provider（fmp 48、yfinance 29、tmx 24 其中 16 条已 defer、nasdaq 9、oecd 9、congress_gov 8、
  imf 8、finviz 7、famafrench 6、government_us 6、deribit 5、tradier 5、alpha_vantage 3、ecb 3、
  seeking_alpha 3、wsj 3、eia 2、biztoc 1、stockgrid 1）。
- plane 2（AC2 用例执行面）：`docs/quality/ac2-case-ledger.json` 仍是 2026-10-09T04:48:54Z 的读数
  `{holds_offline: 3, gap: 17, blocked_leg: 5}`，本轮未重测；`ac2_case_probe --self-test --all` 的下一次
  重测要在剩余 witness 落盘之后做，否则读数只反映 fixture 缺失、不反映代码。
- 子代理预算：30/30 已用尽，本轮及其后全部在主线程执行，不再有并行 worker。

因此**目标未达成**：逐条验收要求 350 条身份各有一张可复算的判定探针 + counterfact 反例，当前是 96/350。
