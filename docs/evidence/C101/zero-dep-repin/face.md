# C101 — zero-dep 门禁：8 个过期引用登记摘要重钉 + 扫描面 census 重冻

- taken: 2026-10-14T15:37Z（本机 `date -u` 读数，非事后补写）
- commit before: `270e373411951be1752d71206379da98762d5081`（branch `dev`）
- command: `python docs/evidence/C101/probes/repin_reference_policy.py`（只读）→ 同脚本 `--apply` → `python scripts/codemod/verify_no_akshare.py --update` → `make zero-dep-check`
- interpreter: `envs/py313` CPython 3.13（门禁指定解释器，`python_minors` 含 3.11/3.13）
- 正文里的每个数字都来自同一次运行的打印面：`repin-read-only.txt`、`repin-apply.txt`、`make-zero-dep-check-after.txt`。判定探针 import 仓自己的 `scripts/codemod/verify_no_akshare`，不另写一份正则。

## 1. 门禁为什么红：两处红，都不在本次工作树里

`make zero-dep-check` 在 `270e373` 上是 exit=2，两处独立原因，先记录再修：

1. **引用登记（C65 register）策略不可加载**：`docs/quality/akshare-reference-allowlist.json` 里 8 个
   已登记路径的 `sha256` 与磁盘字节不符，工具在 `collect()` 之前 hard-stop
   （`FAIL: refusing to freeze an unusable reference policy: stale sha256: ...`），扫描面重冻根本走不到。
2. **扫描面 census 漂移**：`opendata` 归档 census 348，实际走 580。

第 1 点的归因不能靠猜。探针按 HEAD 字节与磁盘字节分别取摘要，8 个路径的读数全是
`pin==head=False / pin==live=False / head==live`（见 `repin-read-only.txt` 每个 `==` 段），
也就是**过期摘要在 `270e373` 提交里就已经是红的**，不是本轮工作树噪声。`git log -1 --format=%h -- <path>`
对 8 个路径逐个读数全部指向 `360c61d`（feat(iter2): 固化 C100 未提交的 32-provider 实现面）：
那次提交改了这些已登记文件的字节，却没有同步登记摘要。第 2 点同源——232 个新增 provider 文件随 `360c61d` 入仓。

## 2. 重钉的正当性判据：登记说的是"引用"，不是"字节没动"

登记条目对每个文件的断言是"这个文件把上游库名当**数据**提及，它不依赖那个库"。所以重钉摘要只有在
这条断言仍然成立时才是正当的，判据写成"依赖边不得新增"：

- 探针取 `population: registered_entries=87 stale_paths=8`（stale 集合由仓自己的
  `verify_no_akshare.reference_policy_problems` 派生，不是手抄清单）；
- 对每个路径用 `scan_source()` 在 HEAD 字节与 live 字节上各跑一遍，按 `(file, module, kind)` 取多重集，
  打印 `added` / `removed`；`kind ∈ {import, dynamic}` 的新增即 REJECT；
- 第二层 regex 级证人：load-shape 行数（`import` / `from` / `importlib.import_module(` / `__import__(`）
  HEAD vs live，与品牌名行数 HEAD vs live 一并打印。

读数：8 个路径 `findings added={}` `removed={}`、`dependency_edges_added={}`，load-shape 与品牌名行数
逐对相等（例如 `catalog.py` load_shape 40/40、brand_lines 10/10；`akshare/provider.py` 1/1 与 25/25）。
汇总面 `face: stale=8 repin_ok=8 rejected=0 dependency_edges_added=0` → 8 个全 REPIN_OK。

## 3. 反例臂：这两条必须动，否则上面那张表证明不了任何事

- `arm import-injection on opendata/data/http_client.py: REJECT(dep={('opendata/data/http_client.py', 'akshare', 'import'): 1}) -> PASS`
  ——同一路径、同一判据，注入一行真 `import akshare` 后判决翻成 REJECT。判据不是恒绿的背景噪声。
- `arm pin-corruption: stale_sha256_readings=87 of 87 -> PASS`
  ——把全部 87 个登记摘要各改一个十六进制字符，仓自己的 judge 必须仍然报 87 条 `stale sha256`。
  这条同时否证了"也许工具根本不查摘要"这个替代解释。

## 4. 落地的字节改动：16 行，只有 sha256

`--apply` 用逐串替换只改 `sha256` 字段，`purpose` / `category` / `reviewed_date` / `reviewer` /
`ast_exceptions` 没有可动的余地。`git diff -U0 docs/quality/akshare-reference-allowlist.json` 的实读数是
8 行 `- "sha256": "…"` 配 8 行 `+ "sha256": "…"`（16 行，`8 insertions(+), 8 deletions(-)`），别无一项。
应用后面探针重问仓的 judge：`post: stale_sha256_remaining=0 -> PASS`、`post: registered_entries=87 (was 87)`
——登记覆盖面一格没扩。

## 5. 扫描面重冻：census 348 → 580，findings 仍是 0

先量再冻。`file_census(DEFAULT_TARGETS)` 对 targets `['opendata', 'opendata/data/providers/akshare/_vendor',
'opendata_client']` 的 live 读数是 `{'opendata': 580, '…_vendor': 325, 'opendata_client': 2}`，
归档是 348/325/2 → delta `opendata +232`，vendor 与 client 两条 0 变化（新增只来自 provider 包）。
冻结前还量了 `collect_raw()` 与 `collect()`：raw findings=6（`kind` 全是 `string`，无 import/dynamic），
经登记抑制后 unsuppressed=0 —— 因此 `--update` 不需要 `--force-update`，baseline entries 保持
`0 frozen reference(s) across 907 file(s)`。这不是"放宽判定换绿"：条目集本来就是空集，
移动的只有那句"这次遍历到底看见了多少文件"。

复测：`make zero-dep-check` exit=0，正文
`OK: 907 file(s) walked (opendata=580, opendata/data/providers/akshare/_vendor=325, opendata_client=2), python 3.13, scanner 4; no new upstream references (frozen baseline: 0)`，
且同一目标里的 `--self-test` 仍然双向咬住（`13 violations detected, 4 compliant samples clean`）。

## 6. 同一趟遍历里读到的另外两条红：记录，不翻转

- `make brand-check` exit=2：`FAIL: 4 occurrence(s) of 'akshare_web'`，命中
  `LICENSE-REVIEW.md:115,130,184` 与 `LICENSE-SCOPE-DRAFT.md:41`。这两个文件（加 `LICENSE-SOURCE-AVAILABLE-DRAFT.md`、
  `DISCLAIMER.md`）是未跟踪的用户侧许可草稿，`git status --porcelain` 读 `??`、`git ls-files` 读空，
  `git show HEAD:` 读不到 `akshare_web`（计数 0）。同一归因在 `docs/evidence/C98/gate-members-run1.txt:19,39`
  已经写进档案（"归用户侧 …… 四个文件都是本轮未跟踪、也不进提交的用户侧许可草稿"）。
  本轮**没有**给 `check_brand.py` 加豁免路径，**没有**把这几份草稿入仓，也没有改 `PROVENANCE_FILES`：
  门禁成员 1 因此仍为红，`make gate` 依旧停在成员 1。原始正文 `make-brand-check-before.txt`。
- `make evidence-traceability` exit=2：`FAIL: 70157 new traceability violation(s) against a 0-entry baseline`，
  全部落在 `untracked` 面。按目录拆分实读数：`docs/evidence/C100/` 68863 条 + `docs/evidence/C101/` 1294 条 = 70157。
  C100 是 7.1 GB / 90894 文件的原始凭证，按用户既定口径"只提交代码 + 精选证据"不入仓，
  而该判定的 presence 面要求 `docs/evidence/` 下每个文本文件都被 git 看见过——两条口径直接冲突，
  这里按"记录不翻转"处理：baseline（0 条）不动，不跑 `--update` 冻结这 7 万条。
  提交 C101 精选证据只消掉属于本轮的 1294 条，剩余 68863 条是已记录的冲突面，不是可修的违规。
  原始正文 `make-evidence-traceability-before.txt`（census 读数 `rounds=103 / files census=71426 / gate logs=140`，
  另五面 narrative/date/identity/command/exit 均为 0 gaps）。

## 7. 本轮没做的事

没有为过门禁放宽任何判定：`check_brand.py` 的 token 集合、`verify_no_akshare.py` 的 scanner 与
`reference_policy_problems` 的摘要校验、`evidence_traceability.py` 的 baseline 语义都逐字未动；
改动只有 8 个登记摘要 + census 那一行，二者都是把归档对上已经提交的字节。
