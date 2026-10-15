# C106 declaration↔contract alignment

判定探针：`probes/align_probe.py`（只读产品代码；三个 tamper 全在内存里 `dataclasses.replace`，
不写任何文件）。它量三件事：313 列的逐列对齐普查、13 条两向反例臂、以及关掉夹具 Literal 分支后
同一条 body 是否改口（causality）。

## 复算

```sh
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
# 引擎的 credential 检查必须在场，否则每一条臂都读成同一个 TOKEN_MISSING，臂与臂之间没有差别
export TRADINGECONOMICS_API_KEY=<一个非空 fixture 值，不发真实凭证>
python docs/evidence/C106/declaration-contract-alignment/probes/align_probe.py
echo "align_probe_exit=$?"
```

期望读数与逐臂读数在 `runs/align-probe-current.txt`，`verdict: PASS` / `align_probe_exit=0`。
探针自己会拒绝在凭证缺失时通过（precondition 面），所以「全绿」不可能是「全部因缺凭证而同样拒」。

只跑普查那一面，用来和别的 checkout 对照：

```sh
python docs/evidence/C106/declaration-contract-alignment/probes/align_probe.py --census-only
echo "head_census_exit=$?"   # 在 4fda2d8 的 detached worktree 里跑读 108（runs/align-probe-head-census.txt）
```

全量测试面（三遍，失败名做差）在 `runs/suite-three-runs.txt`；判定不依赖 collected 分母。同一档案末尾的
`mover_84_attribution` 段把 run1→run2 那个 `+84 collected` 的 mover 按**名**归到仪器口径（门禁配方带
`-m "not e2e"`、我的三遍仪器没带），不是代码长了：84 条全落在 22 个带 `@pytest.mark.e2e` 的活库/DB 文件上，
与本 slice 改动面交集为 0，且 `26468+84=26552`、`187+84=271`、`26551−84=26467` 三式零残差。

17 个门禁成员的逐条读数与 9 条红的归属在 `runs/gate-members-chain.txt`（run1 跑整树；run2 只重跑输入真的
变了的成员 2/11/17，理由逐条写在《开发验收记录》§63.4.4）。这条链按 Makefile 同一顺序跑同一批配方，但
不会因谁红而 abort，所以它给的是逐成员读数，**不是** `GATE_EXIT` 的替代物。

上面那条 before 面对照用的 detached worktree（`/private/tmp/c106_head`）在本轮用完后已
`git worktree remove`，所以复算 `runs/align-probe-head-census.txt` 需要自己重建：
`git worktree add --detach /private/tmp/c106_head 4fda2d8`，再在新 worktree 里跑 `--census-only`。
档案里那份 110 行 / 108 条 remainder 的读数是当时的载体，载体已回收 ⇒ 该面标为**需重建才复算**。

探针自己也是 A2 的目标（`docs/evidence/**` 下的 `.py` 在 a2-check 的候选集合里），所以任何改动之后
要连它自己的静态面一起复跑：

```sh
python scripts/quality/a2_check.py --files docs/evidence/C106/declaration-contract-alignment/probes/align_probe.py
echo "A2_TARGET_EXIT=$?"   # 本轮四条静态面 ok，唯一红来自 C100 不可变快照的既存 Gitleaks 扫描
```

本轮的 types/布局改写之后：`runs/align-probe-current.txt` 是重取的当前面，`runs/align-probe-head-census.txt`
的 before 面也用改写后的字节在 detached worktree 里重跑并逐字对过（110 行、108 条 remainder，差 0 行）。

## 判据的形状，为什么是这个形状

- 名字集合早就被 `build_row_model` 钉住；kind/required/nullable 没人钉，所以这一类分歧只能在
  `fetch` 时以 `*_SHAPE_INVALID` 出现。普查把「只能在运行时爆」变成「逐列可数」。
- `date` kind ↔ `datetime` 字段是引擎自己声明的时态读法（`_row_temporal_type`），例外必须**带计数**
  （`temporal_exception_covered`），否则例外会变成一个把什么都吞掉的静默 0。
- 任何「0」都要能被同一台仪器读出非零：例外关掉 → 3，kind tamper → 1，nullable tamper → 1。
- 每条臂两向都要落：接受侧给行值，拒绝侧给该模型自己的 shape 码。只有接受侧=夹具搬家；只有拒绝侧=判定放宽。

## 这一轮留下的两处知识（不是缺陷，别在下一轮当 bug 修）

1. `scale` stage 按设计拒绝 JSON 数字（`a float would drift`），所以带百分折算的列必须发文本；
   接受臂发 `0.5` 这类数字会读到 `ValueTransformError`，那是正向证据。
2. `null_sentinel` 匹配强制前的**发布文本**：`'ND'`/`''`/`'NA'`/`'N/A'` 变 null，`'n.a.'` 照旧被拒；
   而 `-999.99` 这类数字缺失值没有任何 kind 能声明，仍在 `DECLARED_GAPS`。

## 成员 11 修复面与重钉面怎么复算（`runs/member11-repair-and-repin-face.txt`）

判定器的崩溃修复落在 `scripts/quality/acceptance_item_probe.py`（交付字节 `a8cc05ef529a…`，
HEAD 仍是修复前的 `c5db910fc3c6…`）。三台仪器都是只读的，按顺序跑：

```sh
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
# 1) 被修的那一格：崩溃现在读成计数，而不是把整遍打挂
python scripts/quality/acceptance_item_probe.py --item 'AC-17|07'; echo "item_exit=$?"
# 2) 反例母数：期望 130/130 与 941（HEAD 字节读 939，差 2 = 本轮新加的两条 Break）
python scripts/quality/acceptance_item_probe.py --self-test; echo "self_test_exit=$?"
# 3) 判定器自己的四条静态面，带一个本轮未动的 tracked 文件做对照
python scripts/quality/a2_check.py --files scripts/quality/acceptance_item_probe.py;  echo "A1=$?"
python scripts/quality/a2_check.py --files opendata/data/providers/catalog.py;        echo "A2=$?"
```

第 3 步两侧都读 `exit 1`，红只来自 pinned Gitleaks 快照面，两条 run 报同一组 25 个路径 —— 那条红不属于
这五个字节。重钉排演（**不落盘**，改的是内存副本）：`/tmp/c107_repin_rearm.py`，期望
`arm_A=1 / arm_B=0 / arm_C=1 / REARM_BITES=True`；`arm_C` 是让 `arm_B` 的 0 不成为死比较的那一条。

```sh
python /tmp/c107_repin_rearm.py; echo "repin_rearm_exit=$?"
```

`--self-test` 会把整树 a2 再跑一遍（本轮实测 25-30 分钟量级），期间**不要**写仓库：它的母数就含
`docs/evidence/**` 的 `.py` 与那些不可变快照。

## 下一步（本轮未做，勿当已完成）

- 把普查变成守卫：在 `build_row_model` 里拒绝 kind/required/nullable 与被审契约不一致的声明，
  守卫必须带同一条时态例外和同一个例外计数，否则它自己就是一个静默 0。
- 迭代2 的逐条验收平面（`scripts/quality/acceptance_item_probe.py --all` + `--self-test`，
  130 项 / 350 钉住身份）仍在任务 #13/#18 上挂着；本目录不对任何 AC 编号主张已验收。
