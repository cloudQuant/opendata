# C140 — wave 37 无凭证访问轮（四条 credential_fields 为空的残行）

日期：捕获时刻 `2026-10-19T04:12:02.588495Z` … `2026-10-19T04:13:09.865165Z`（`capture-receipt.json` 的
`captured_utc_range`），入档提交 `b2e7421e7`。

本包 13 个文件：1 份 `capture-receipt.json` + `sources/` 下 6 组 `.headers`/`.json` 成对捕获。
它被 `evidence_traceability` 记为 `#narrative`，因为此前只有产物没有说明；本文件补上说明。

## 这一轮问的是什么

台账里 `credential_fields` 为空、却仍未落地的残行，究竟是「要凭证」还是「没有端点」。
判定方式只有一个：用公开 GET 去打，并把响应留在档里。

`capture-receipt.json` 的 `environment.auth` 逐字记录：

> No Authorization header, API key, account login, or Cookie request supplied.

命令形态（同文档 `curl_command` 字段）：`/usr/bin/curl -s -L -A <UA> -D <headers> -o <body> <url>`，
跟随重定向。

## 六条捕获与其用途（`use` 字段原文摘取）

| id | 用途（receipt 的 `use`） |
| --- | --- |
| `cboe-all-us-indices` | cboe::IndexSnapshots 候选源；无凭证；`307` 之后 `200`（cdn-api.cboe.com） |
| `seeking-alpha-earnings-calendar-missing-filter` | seeking_alpha::CalendarEarnings；证明 API 活着，并暴露它要求哪个参数 |
| `seeking-alpha-earnings-calendar-filtered` | 同一日历端点，按文档的过滤参数形状 |
| `seeking-alpha-earnings-calendar-200` | 日历端点的 200 载荷；该端点无凭证可读 |
| `seeking-alpha-symbol-data-estimates` | ForwardEps/ForwardSales 两行的源端点 |
| `seeking-alpha-symbol-data-estimates-relative-periods` | 同一估计端点；`400` 指名下一个必需参数 |

## 后续（不是本包的判定，只是记录去向）

`cboe-all-us-indices` 支撑了 wave 37 的 `cboe::IndexSnapshots` 落地；seeking_alpha 的两条 estimates
行由 wave 53 落地（`9fea1060e`，平面 1 → 349/350），其 live 侧复取今天仍是 403 PerimeterX 挑战
（`C147/…/probes/w150_blocked_retry_face.py`）。本包只主张「这些端点在无凭证下如何回答」，
不主张任何行的验收状态；行的状态以 `docs/evidence/C101/judge/claims.merged.report.json` 为准。
