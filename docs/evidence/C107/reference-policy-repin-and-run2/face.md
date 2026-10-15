# C107 · 一格摘要重钉落地 + run2 只复跑输入真动过的三个成员

本轮的原始载体（逐字，未加工）：
- `runs/run2-member-chain.txt`（27224 字节，`sha256=3b42320324250eb7e2023234…`）—— run2 逐成员链全文，含 provenance、
  三个成员的 `MEMBER_EXIT`、成员 11 的 130 格逐条读数、成员 17 的机理面、两个读同一份 allowlist 的测试模块。
- `runs/allowlist-diff-face.txt`（677 字节，`sha256=e10854cdf1b68684454bffc4…`）—— 重钉前后**字段级**差分。

## 0. 这一刀为什么只跑三个成员

C106 run1 之后真正动过输入的是两处：`scripts/quality/acceptance_item_probe.py` 的崩溃修复（已进入 `a81c533`），
和 `docs/quality/akshare-reference-allowlist.json` 里那一格过期 digest（本轮落地）。门禁 17 个成员里输入因此改变的只有：

| 成员 | 为什么重跑 | run1 | run2 |
| --- | --- | --- | --- |
| 2 `zero-dep-check` | 钉的就是刚提交的探针字节 | `FAIL: reference policy is unusable: stale sha256: scripts/quality/acceptance_item_probe.py`（C106 载体 `:25`），`exit=2` | `MEMBER_EXIT: zero-dep-check=0`（`:20`） |
| 11 `acceptance-probe-check` | 配方本体就是那个修好的探针 | SyntaxError，`produced ZERO item readings`（C106 载体 `:138`），`exit=2` | 130/130 格有读数、941 条反例臂，`MEMBER_EXIT: acceptance-probe-check=2`（`:173`） |
| 17 `frontend-e2e` | run1 对它**没有任何产品读数** | 只拿到 `exit=2` | `MEMBER_EXIT: frontend-e2e=2`（`:215`）+ 本轮补到的机理面（§4） |

外加读同一份 allowlist 的两个测试模块：`53 passed in 10.80s`、`MEMBER_EXIT: allowlist_test_modules=0`（`:244`、`:245`）。
这**不是** 17 成员链，也不是 `GATE_EXIT` 的替代物：它按同一顺序跑同一批配方，但不因为谁红而 abort，给的是逐成员读数。
derived 读数自证：`member_exit_lines=4 steps_total=4`、`green(2)+red(2)=4 vs 4 -> HOLDS`、
`ALL_STEPS_REACHED`、`run2_chain_exit=1`（`:252`–`:256`）。

## 1. 重钉到底钉了什么（字段级，不是「改了一行」）

`runs/allowlist-diff-face.txt` 逐字打印，仪器是 `git show HEAD:` 的字节与 worktree 字节的结构对比：

    entries current=87 head=87
    changed_entry_count=1
      index=80 path=scripts/quality/acceptance_item_probe.py differing_fields=['sha256']
        head_sha256=a33e906a8acb24914d89f65cd184e7969b2a7f6ed34a64693c4731a7d09c05ed
        worktree_sha256=a8cc05ef529ac37dba1d6414e72deaff77b7034873a51cc66bc36e8193a3cca5
        unchanged_fields_kept: purpose=same category=same reviewer=same ast_exceptions=same
    non_sha_field_moves=0

三个读数要紧：母数 87 未动、被改格只有 1 条、且 `non_sha_field_moves=0` —— 也就是
`purpose`/`category`/`reviewer`/`ast_exceptions` 四格**一格没动**。这不是批准链：批准人那句话的出处仍停在 C89，
本轮只把 digest 对上刚提交的字节。整份文件的 git 差分是 `1 1`（1 增 1 删），政策字节 digest
`198395ec6d8dba7e…`（HEAD）→ `50c012b376b128a5…`（worktree，与 run2 provenance `:10` 同一读数）。

钉的**方向性**由 C106 §5 的 in-memory re-arm 提供（那份载体已在 C106 落档）：arm A 旧钉 → `target_problems=1`，
arm B 钉到 live 字节 → `0`，arm C 64 个 0 的假 digest → `1`。arm C 存在的意义是防 arm B 是空判。
钉之前 `a33e906a…` 那份旧政策的字节留在 `/tmp/c107_allowlist_preimage.json`（未入库，本轮不主张它可复算）。

## 2. 成员 11：崩溃这一条确实被修掉了，红换成了别的原因

修完之后这一遍的读数（载体 `:27`–`:28`、`:167`–`:170`）：

    本遍墙钟 = 1962.9 s，读到事实的探针 130/130，测不出来的：无
    反事实面：130/130 个探针走到了判定，941 条 break 各被施加一次、每条都要求把干净读数打回 gap
    台账↔读数：agrees=115, unflipped=0, open=0, deferred=9, stale-proof=6（共 130 格有读数）

`130/130` 对上成员 4（`ledger-check`）台账的 `items=130`（C106 run1 载体 `:36`–`:37`，
`census: items=130 proven=112 gap=18 unreviewed=0 ticked=112`），两条独立来源同一母数。
所以这条成员现在**不再是**「一行读数都没有」，它红在两件被点名的事上，都不归本刀管：

1. 6 格 `stale-proof`（台账记 proven、探针现在读出 gap）：AC-11|04、AC-16|07、AC-16|08、AC-17|02、AC-5|07、AC-6|02
   （载体 `:150`–`:155`、`:168`）。判据自己给的修法也是那一句：「台账翻上去之后没有任何东西再量它，这一遍就是那个东西」。
2. AC-17|07 现在读出 `gap`（载体 `:78`），红因仍是 C106 §3 那 56 个未登记豁免的 `quality-source-repairs/` 未跟踪
   scratch 树上的 callable —— 本轮既没豁免它也没删它，那条决定还挂着。

另外 9 格 `deferred`（`:169`：AC-17|03、AC-17|05、AC-17|06、AC-17|07、AC-17|08、AC-17|10、AC-1|02、AC-1|03、AC-1|10）
是因为工作树不干净：探针第一行就点名 `工作树：81918 entry(ies), first:  M docs/quality/akshare-reference-allowlist.json`
（`:29`）—— 脏的正是我这一格未提交的 allowlist。时刻面只在干净时判，否则 deferred。**我不预告它们提交后翻什么**：
本轮没有在干净树上重跑成员 11，deferred→判定的那一步没有读数。

## 3. 我自己的仪器也有缺陷（照例留在正文里）

`/tmp/c107_run2_chain.sh` 第 51 行是 `echo "which_npx=$(command -v npx || echo <none>)"`，`<none>` 在双引号里被当成重定向，
bash 打了 `syntax error near unexpected token 'newline'`，于是 `which_npx=` 是空的（载体 `:220`–`:222`）。
同一段里的 python 复现不受影响，`resolved_npx=/opt/homebrew/bin/npx`（`:224`）仍是有效读数，所以 §4 的结论不靠那行空值。
另一次我手跑 `verify_no_akshare.py` 时忘了带 `PATH` 前缀，它打印 `python 3.11`；成员 2 在链上的那一条才是门禁解释器
`python 3.13`（`:19` 与 provenance `:5`）。判据取链上那份。

## 4. 成员 17：run1 欠的那一份产品读数，这轮拿到了，根因是一行模块级 throw

判据把 `--list` 的非零返回码折成 `None`，然后打 `RUNNER-UNREADABLE`（`:213`），`exit 2, not a pass`。
本轮用 `scripts/quality/frontend_e2e_plane.py` `runner_totals()` 的同一 argv（`npx --no-install playwright test --list`）
和同一 `cwd=frontend` 复现，拿到了 run1 从没看到的那一层：

    list_returncode=1
    stdout_tail=['Listing tests:', 'Total: 0 tests in 0 files']
    stderr_lines=10
      STDERR| Error: OPENDATA_AC21_EVIDENCE_DIR is required
      STDERR|    at script-download-backend.spec.ts:40
      STDERR| > 40 | if (!evidenceDir) throw new Error('OPENDATA_AC21_EVIDENCE_DIR is required')

机理说清楚：`e2e/script-download-backend.spec.ts:40` 在**模块顶层**（收集期）要求 `OPENDATA_AC21_EVIDENCE_DIR`，
没设就直接 throw，于是 playwright 列测试时整棵收集失败 —— `Total: 0 tests in 0 files`。上面那 30 条 `ASSERTS` 是
平面自己的静态读（`:179`–`:209`，`ASSERTS 30 / LEAVES 30`），不是 runner 的清单。所以成员 17 的红不是「用例没写」，
是**一条把运行期要求放到收集期的守卫，把清单也一起打死**。修法要么把这行降级成收集期可见的 skip，要么让平面在
未设该环境变量时先注入一个临时目录再列 —— 两者都不是本轮的刀，本轮只负责让它有一个可归因的读数；
在那之前成员 17 对谁都不是绿灯。`OPENDATA_ALLOW_LIVE_E2E` 全程未设（provenance `:7`），这一遍没有活体腿。

## 5. 本节没主张的东西

- 没主张 `GATE_EXIT` 通过：run2 只有 4 步读数，2 绿 2 红，`run2_chain_exit=1`。
- 没主张成员 11 翻绿：崩溃这一条有读数了（130/130 + 941 臂），它仍因 6 格 stale-proof 与 AC-17|07 的 gap 红着。
- 没主张 9 格 deferred 提交后会变绿，也没主张它们会红：干净树上的那一遍还没跑。
- 没主张成员 17 已被修好：本轮只交付根因读数，没有动 `frontend/**`。
- 没主张重钉带来任何语义变化：`non_sha_field_moves=0`，批准人那句话的出处仍在 C89；这是同一批准内容下的摘要对齐。
- 没主张 `/tmp` 里的 preimage、re-arm 脚本可复算：它们留在 `/tmp`，入库的是它们打印出来的数。
- 没有 push 授权；原始链日志在 `runs/run2-member-chain.txt` 之外不再复制第二份；`docs/evidence/C100` 的未跟踪
  原始收据仍不入库。
