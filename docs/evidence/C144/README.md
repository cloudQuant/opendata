# C144 — wave 39 无凭证访问轮的终局面（A–L 24 臂）

日期：捕获时刻 `2026-10-19T10:09:05.892002Z`（臂 L，`tz_aware=True`、`age_hours=0.00`），
入档提交 `46f56cd8e`。本包 58 个文件：`capture-receipt.json`、`probes/`（`w53_materialize_round.py`、
`w54_access_round_face.py`、`w58_sources_credential_scan.py`）、`runs/`、`sources/` 成对捕获。
此前只有产物没有叙述档，被 `evidence_traceability` 记为 `#narrative`，本文件补上。

## 这一轮要回答的问题

台账 350 行里，有 12 行属于残差；其中哪些**真的**不需要凭证就能取数，哪些需要，哪些根本没有可用端点。
判定不靠叙述，靠 `runs/w54_access_round_face.stdout.txt` 的 12 组臂（终局行 `arms=24 arms_passed=24 PASS=True`、
`w54_access_round_face_exit=0`）。

## 摘几条决定性的读数（逐字取自该 stdout）

* **A**（响应码普查）`codes={'200': 18, 'NO_STATUS_LINE': 1, '400': 4} sums=23 of=23 forged_reads=401`
  —— 23 份捕获逐档读码，总和等于人口；伪造读数会被读成 401，说明臂能咬。
* **B** `rows=56 … products=12 vx_expiries=14 vx_dates_monotone=True vx_price_span=(18.9485, 20.625)
  counterfact=REFUSED_NON_NUMERIC_PRICE` —— 一条真实表格 + 一条数值篡改拒绝臂。
* **D**（400 的语义）`empty_captures=11 distinct=1 … four_oh_ones=4 distinct=3 details=['Parameter
  relative_periods is required', 'Parameter ticker_ids is required', '`revisions_data_items` or
  `estimates_data_items` is required'] two_bad_requests_share_one_reason=True` —— 请求形状不对时，
  服务端会点名缺哪个参数；这是后来 wave 53 落地两行 estimates 的依据之一。
* **E**（控制体）`control_http=200 control_bytes=12389 control_digest=e092c87561ade151
  counterfact_truncated_digest_collides=False (must be False)`。
* **F**（唯一无状态码的那份）`headers_bytes=0 chain=[] code=NO_STATUS_LINE attempts=3 all_timeout=True
  resolver=['69.63.187.12'] nameserver=8.8.8.8` —— 这一条不主张「端点死了」，只主张本机三次超时，
  属机器本地传输事实。
* **G**（本包自身的凭据扫描）`markers=5 payload_hits=0 hit_list=[] planted_hits=1 plant_printed=False`
  —— 落盘内容里没有凭证，植入控制仍能被检出（所以 0 不是瞎报）。
* **K**（残差人口）`residue_rows=12 … targets_credential_free=6 of=6 ledger_rows=350
  ledger_credential_bearing=195` —— 本轮点名 6 行不需要凭证；其余的归因在台账与后续波次里。
* **J**（成对捕获完整性）`pairs_on_disk_absent=0 digests_recomputed_matching=23 of=23
  one_extra_byte_digest_collides=False`。

## 复算

```bash
python docs/evidence/C144/wave39-unauthenticated-access-20261019/probes/w54_access_round_face.py
```

只读：它重数 `sources/` 的成对捕获、重算摘要、检查 receipt 的自述字段，不重新联网。

## 边界（本包不主张的事）

不主张任何行的验收状态（那以 `docs/evidence/C101/judge/claims.merged.report.json` 为准，现读 349/350）；
不主张 F 那条是端点判定；也不主张「无凭证可用」等于「可以合规再分发」——权利登记是另一条线，见
`docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/前置条件与决策清单.md` 的 ACCESS/RIGHTS 队列。
