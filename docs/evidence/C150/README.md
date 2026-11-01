# C150 — 八条测试 fixture 字面量的 gitleaks 放行与三臂反事实

日期：2026-11-01（`gitleaks-counterfactual.txt` 记录的三次 `detect` 运行均在该日，主机时钟
`Asia/Shanghai`）。入档提交即引入本轮的上一个提交（`e739a8dc` 的父提交），文件见 6.4。
本包 2 个文件：`gitleaks-counterfactual.py`（测量脚本）与 `gitleaks-counterfactual.txt`
（测量输出，正文以 `.txt` 入档——仓库 `.gitignore` 第 61 行忽略 `*.log`，用 `.log` 会变成
`#untracked`，正是本门禁要抓的那一类）。

## 这一轮要回答的问题

`make secret-check` 在全历史扫描上报 **10 项命中**，全部 `generic-api-key`。问题不是"要不要把红灯变绿"，
而是：这 10 项里**有没有一件真凭据**；如果没有，怎样放行才**不会**顺手放过真凭据。

## 逐条判定（不是按形状猜，是按所在行读）

10 项分布于 9 行、8 个字面量，全部是测试给自己合成行起的名字：

| 字面量 | 出现位置 | 它是什么 |
| --- | --- | --- |
| `fixture-csi500-v3-integration-run` | `test_ths_transport_source_attempt_scope_v3_integration.py:60` | `RUN_KEY`，测试自己构造的 run 键 |
| `fixture-csi500-v3-quota` | 同上 `:63`、`test_ths_csi500_v3_sample_job.py:67/304` | `QUOTA_KEY` / `budget_quota_key` |
| `fixture-csi500-v3-run` | `test_ths_csi500_v3_sample_job.py:89` | `run_key` |
| `fixture-ths-quota-v1` | `test_ths_source_attempt_gate.py:69` | `PRINCIPAL` 的配额键 |
| `fixture-observation-quota-v1` | `test_ths_financial_observation_fetcher.py:85` | 同上 |
| `fixture-quota-v1` | `test_http_client_source_attempt_gate.py:76` | `QUOTA_KEY` |
| `quota-v3-boundary` | `test_request_budget_v3_extra.py:53` | `"budget_quota_key"` |
| `stock-daily-v2-api-fixture` | `test_stock_daily_publication_api.py:83` | `"source_verification"` |

判定依据是机制而非外观：`generic-api-key` 把**字段名**（`..._quota_key`、`run_key`、`PRINCIPAL`）
读成键名，把引号里的值读成值；这与本文件里 C75（`source_key=`）和 C89（`environment_variable=`）
记录的是同一件事。值本身在同文件内构造，从不读取环境变量或配置。

## 放行方式与反事实（决定性的读数）

放行**枚举这 8 个字面量**，不给 `fixture-*`/`quota-*` 一个值类——值类会连同"真凭据恰好写进同一槽位"
一起放行。`gitleaks-counterfactual.py` 用一棵只读真实字节的临时历史测三臂，`gitleaks-counterfactual.txt`
是其输出：

* `shipped`：`real_lines.py` → `[]`（放行生效），`one_char_longer.py` → `generic-api-key`（**改一个字符就重新报**），
  `token_in_same_slot.py` → `github-pat`（**同一槽位植入的凭据仍被抓**），`generic_secret_in_same_slot.py` → `generic-api-key`。
* `without_c150`（只删这一行正则）：`real_lines.py` → `generic-api-key`，即**这 10 项全部回归**。
* `default_rules`（不加载任何 allowlist）：与 `without_c150` 一致，说明这 10 项不是被别的条目顺带放行的。

成对读数才是结论：第二臂证明"放行就是这一行"，第一/三臂证明"放行不会蔓延到别的值"。

## 6.4 边界与未主张

* 这只对 `generic-api-key` 生效；`github-pat`、`aws-access-key-id` 等具体规则未被触碰——
  `token_in_same_slot` 臂在 `shipped` 下仍报 `github-pat`，就是这条边界的测量。
* 反事实脚本自身用相邻片段拼装伪造值，避免本轮的证据文件变成下一轮的命中。
* **未主张**：真实数据源、生产部署、来源权利或历史覆盖的任何结论；本轮只处置密钥扫描门禁的假阳性。
