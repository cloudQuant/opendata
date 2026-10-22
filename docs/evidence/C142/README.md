# C142 — wave 38 seeking_alpha::CalendarEarnings 的捕获与分页闭合

日期：2026-10-19（入档提交 `dffa64969`）。本包 13 个文件：`capture-receipt.json`、
`probes/w44_capture_receipt.py`、`runs/w44_capture_receipt.txt`、`sources/` 下若干 `.headers`/`.json` 成对捕获。
此前没有叙述档，被 `evidence_traceability` 记为 `#narrative`，本文件补上。

## 主张与判定器

这一轮要证明的是「日历端点无凭证可用，且翻页能闭合到声明的总数」，两件事分别由两个臂记分。

`runs/w44_capture_receipt.txt` 的末段逐字：

```
pagination_closure p1_plus_p2=56 total_count=56 closes=True
capture_receipt_face captures=5 of=5 arms=5 arms_passed=5 failed=[] receipt=docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/capture-receipt.json receipt_sha256=03658660ac5f3ec4

w44_capture_receipt_exit=0
```

读法：两页相加 56 条，正好等于服务端自报的 `total_count=56` ⇒ 分页闭合；5 次捕获、5 个臂全过，
receipt 的 sha256 前缀 `03658660ac5f3ec4` 钉在 `capture-receipt.json` 上；探针退出码 0。

## 复算

```bash
python docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/probes/w44_capture_receipt.py
```

该探针把 `sources/` 里的成对捕获重新数一遍并与 receipt 里的摘要比对；它是只读的，不重新联网。

## 去向

`seeking_alpha::CalendarEarnings` 由 wave 38 落地；本包只主张端点行为与分页闭合，
不主张行的验收状态（行的状态见 `docs/evidence/C101/judge/claims.merged.report.json`，现读 349/350）。
同源的 ForwardEps/ForwardSales 两行由 wave 53 落地，其 live 侧今天仍是 403 PerimeterX 挑战。
