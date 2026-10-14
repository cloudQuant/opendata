# C102 · ths 37 面「no exact request grant is configured」闭合面：夹具授权、产品守卫一行未动

本包只判定一个文件：`tests/test_ths_provider.py`。
它**不声明**任何上游数据源已授权 live 访问——夹具写下的 `rights_evidence` 字面就是
`test-fixture:offline-only:mock-transport`，7 个 live 站点一格未授。

## 1. 分母恒等式（两侧都是实测载体）

| 载体 | 读数 |
| --- | --- |
| `runs/pytest-before-ths-file.txt` | `37 failed, 63 passed, 7 skipped in 0.58s`，`collected 107 items` |
| `runs/pytest-after-ths-file.txt` | `100 passed, 7 skipped`，`collected 107 items`，`pytest-after-ths-file_exit=0` |

`37 + 63 + 7 = 107` 且 `100 + 7 = 107`：同一分母，移动 37 面，7 个 skip 一格未动。

全树面（`runs/suite-whole-tree-before-after.txt`，两份日志都 `collected 26551 items`）：

```
93 failed, 26184 passed, 274 skipped   ->  56 failed, 26221 passed, 274 skipped
failed  -37   passed +37   skipped ±0   collected ±0
该文件在两份日志里的 FAILED 行数：37 -> 0
```

这四个数是**同一棵树的两次实测**，不是推导：只有 ths 一族的 37 面动了，别的族一格未变。

## 2. 站点总体（44 = 37 + 7，`runs/idempotence-arm.txt`）

```
face: fetch_sites=44 with_ctx=37 without_ctx=7
face: ctx_offline_context_call_sites=37
face: ctx_less_site_classes=['TestAgainstTheLiveFuyaoApi']
```

未授的 7 站全部落在 `@pytest.mark.e2e` 的 `TestAgainstTheLiveFuyaoApi` 里，
与那 7 个 skip 一一对应。**本轮没有给 live 道发过任何夹具授权**——
那才是这族修复真正的边界：离线缝拿离线身份，live 道继续走它自己的门。

## 3. 成因（逐条读码）

- `Fetcher.fetch` 在 `transform_query` 之后、`extract_data` 之前调用
  `validate_scope_grants(capture_request_scopes())`（`opendata/data/protocol.py:313`），
  拒绝点在 `opendata/data/request_budget.py:484`。单站直跑载体：
  `opendata/data/request_budget.py:484: RequestAuthorizationError`。
- 无预算的 canonical model 落到 `_ZERO_BUDGET = RequestBudget()`（`request_budget.py:693`），
  所以 37 站同点红。这套 fail-closed 语义是已提交的（`3f05f63`，2026-10-08），**本轮产品侧零改动**。
- ths 传输层还有第二条独立拒绝：注入 `client=` 会把 `_scoped_transport_verified` 置 `False`
  （`transport/http_client.py:144`），scoped 路径随即报 `scoped request transport is not verified`；
  受支持的离线缝是 `mock_transport=`（同类第 158 行自建受限客户端）。

## 4. 修复面（只动测试文件）

1. `_mock_client` 从 `client=httpx.Client(transport=httpx.MockTransport(...))` 迁到
   `mock_transport=httpx.MockTransport(...)`，并显式注入
   `FuyaoRateLimiter(rate_per_second=1000.0, burst=24)`、`backoff_base_seconds=0.0`、
   `backoff_jitter_seconds=0.0`、`base_url=https://fuyao.test`。
   桶必须超配：`FuyaoRateLimiter.acquire()` 在桶空时**抛错**而不是等待，
   默认 5/s·burst 10 会在 20 页循环（`instrument.MAX_PAGES=20`）上抢先于适配器自身的页上限咬住。
2. `_offline_context(model)` 逐站按**该测试真正构造的 fetcher 类**派生 `canonical_model`，
   授 `RequestOperation.QUERY` + `allowed_hosts={fuyao.test}` +
   `profile=RequestBudgetProfile.BOUNDED_REQUEST`，`task_attempts=source_attempts=24`。
3. 24 的账：LIVE_SMOKE 的源上限是 3（`request_budget.py:26-31, 97-107`），20 页加一次代码解析就超限；
   有界档上限 256/64，取 24 让**适配器自己的页上限仍是咬合的那道界**。
4. 净面：`git diff --numstat` = `192 39`；改写器自报 `added=1447` 字节（72625 → 74072）。
   字节面：HEAD `3f61807267d7…` 未动，工作区 `483074e0ad0b…`（`runs/digests-after.txt`）。

## 5. 反证臂（6 臂，`runs/arms-final.txt`）

```
arm=control verdict=PASS observed=rows=1
arm=wrong-model verdict=PASS observed=no exact request grant is configured requests_sent=0
arm=no-ctx verdict=PASS observed=no exact request grant is configured requests_sent=0
arm=injected-client verdict=PASS observed=scoped request transport is not verified requests_sent=0
arm=live-smoke-quota verdict=PASS observed=task_attempts must be an integer from 0 through 10
arm=attempt-bound verdict=PASS observed=request attempt allowance exhausted pages_seen=1
face: arms=6 passed=6 failed=0 attempts=24 host=fuyao.test
arms-final_exit=0
```

这六臂证明授权是**承重的**：wrong-model / no-ctx 都在 `requests_sent=0` 处拒绝（一字节未出网），
injected-client 证明 `client=` 缝在 scoped 路径上确实不可用，
live-smoke-quota 证明档位上限是真读的界，attempt-bound 证明 1 次预算就把分页停在第 1 页。

## 6. 幂等臂与我自家仪器的一处真缺陷

首跑幂等臂（`runs/idempotence-arm.txt` 之前的一次读数）暴露两件事：

1. 改写器的总体是**修复前抓的 37 个节点 id**，不是「当前缺 ctx 的站点」的活扫描——
   修完再跑仍打印 `failing_nodes=37`。单看这个数会以为修复没生效。
2. `ast.parse` **接受**重复关键字：`fetch(ctx=1, ctx=2)` 解析通过，
   只有 `compile()` 报 `keyword argument repeated: ctx`。
   也就是说我原来那道「 staged 必须 re-parse 才落盘」的守卫，**挡不住第二类破坏**。

两处都已修（`probes/ths_grant_repair.py`：已带 `ctx=` 的站点跳过并计数；落盘前改用 `compile()`），
终读数为：

```
face: failing_nodes=37 fetcher_classes=11 insertions=0 unique_insertions=0 unresolved=0 uncovered_nodes=0 already_granted=37
arm_repeated_keyword: ast.parse ACCEPTS it (a parse-only guard would not stop corruption)
arm_second_apply: duplicate_keyword=compile_refused (keyword argument repeated: ctx)
arm_probe_guard: compiles_staged=True skips_already_granted=True
verdict: PASS - 37 offline sites granted, the 7 e2e sites carry no fixture grant
```

## 7. 格式化附带面审计（三臂，均实测）

- 臂 A（`runs/collateral-audit-armA-armC.txt`）：把 `git show HEAD:tests/test_ths_provider.py`
  单独过一遍 `ruff format` → 与原文件逐字节相同：
  `armA_head_reflow_diff_exit=0`、`armA_head_reflow_diff_bytes=0`。
  HEAD 那份本来 format-clean，所以工作区里唯一需要重排的只有本轮新增文本。
- 臂 B（`runs/collateral-audit-armB.txt`）：`git diff -U0` 切成 44 个 hunk，逐 hunk 查是否含本轮编辑 token →
  `hunks_total=44 hunks_without_my_token=0`，没有任何「纯格式化」的孤立 hunk。
- 臂 C：`ruff format --diff tests/test_ths_provider.py` 现读为空（`armC_worktree_pending_format_diff_exit=0`），
  `ruff check` 干净（`ruff-final_exit=0`）。
- 过程失范也记一条：本轮我**先**跑了 `ruff format`、**后**去看 `--diff` 范围，顺序违反了
  自己「共享模块先看改动面再落笔」的纪律。上面三臂是事后补的正证，不是替代。

## 8. 我错过的数（留在账上）

| 载体 | 错数 | 真因 |
| --- | --- | --- |
| `runs/repair-dry-fabricated-zero.txt` | `insertions=0 uncovered_nodes=37 models_used=0` | 把 `Class::method` 节点 id 用 `rpartition(".")` 拆了——伪造出一个零 |
| `runs/repair-dry-borrowed-identity.txt` | `models_used=9` | 参数化接收者的 `fetcher` 被同类兄弟方法赋值污染，那一站借用了**别人的**授权身份 |
| `runs/repair-apply-refused-unparsable.txt` | `verdict: refused, staged file does not parse … line 695` | AST 的 `col_offset` 是**字节**而文件含中文文档串；`_open_paren` 又把 `(` 本身当插入点。守卫拒写，文件字节未变 |
| 落盘后首次直跑 | 仍 37 面红，但只剩一张新面孔 `task_attempts must be an integer from 0 through 10` | 夹具默认走 LIVE_SMOKE 档而 `OFFLINE_ATTEMPTS=24`；两侧都要显式声明 `BOUNDED_REQUEST` |
| `runs/arms-first-5-of-6.txt` | `arms=6 passed=5 failed=1` | 我自己的 attempt-bound 臂漏了必填查询字段 `asset_type`；顺带再次确认守卫在 `transform_query` 之后才跑 |
| 幂等臂首读 | `duplicate_keyword=UNGUARDED` | 我原以为 re-parse 守卫能挡重复关键字；实际 `ast.parse` 放行，见 §6 |

## 9. 复算命令

```
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
python -m pytest -q --no-cov -p no:cacheprovider tests/test_ths_provider.py
python docs/evidence/C102/probes/ths_grant_arms.py
python docs/evidence/C102/probes/ths_idempotence_arm.py
python docs/evidence/C102/probes/ths_grant_repair.py            # 默认 dry；--apply 才写
python docs/evidence/C102/probes/ths_format_audit.py
python docs/evidence/C102/probes/ths_hunk_audit.py
python docs/evidence/C102/probes/archive_faces.py               # 重跑并覆写全部 runs/*.txt
```

## 10. 这面**没有**声明的东西

- 没有说 ths 的任何模型已 live 验收：`live_verification_status` 未翻，7 个 e2e 站点仍走原门。
- 没有改任何产品判定：`request_budget.py` / `protocol.py` / `http_client.py` 三个文件本轮零 diff。
- 没有把别的族的红算作闭合：全树面从 93 到 56，**只**移动了 ths 一族，其余 56 面按族列在开发验收记录的新章节里。
