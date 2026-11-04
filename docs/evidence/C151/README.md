# C151 — 干净环境 P0 集成留档重跑：把 archive_blind 从 1 压回 0

日期：2026-11-04（Europe/Madrid，主机时钟；档案内 `Date:` 行是 UTC）。基线：`dev @ 6db4382f13a42043bb29bd1076cda31459d17e8c`，
工作树 `docs/od-clean-env-archive-c151`，除本目录外无改动。机器：Linux 7.0.0-28-generic x86_64，18 核。

本包 4 个文件：`run_p0_integration_surface.sh`（测量仪）、`build_clean_env.sh`（构造仪）、
`clean-env-integration-run.txt`（两次干净解释器运行的完整不裁剪正文）、`clean-env-build.txt`
（两个 venv 的构造日志原文）。全部 `.txt` 入档而非 `.log`：仓库 `.gitignore` 忽略 `*.log`，
未跟踪的证据在 `make evidence-traceability` 里就是 `#untracked`。

## 这一轮要回答的问题

AC-16|07 判的是「在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过」。它的判定面有两半：
一半在**当场跑**（选择式在顶层上游包被 import hook 强制拒掉的子解释器里全绿），另一半读**最新一份**
`docs/evidence/C*/clean-env-integration-run.txt`。后者是一条刻意的棘轮：探针用
`integration_units_now()` 从 `tests/` 现读现在带 `integration` 标记的模块集，再拿 B 段里出现过的
`tests/...::...` 行对账，差集就是 `archive_blind`。加一个标记模块而不重跑干净环境，正向证据就
悄悄变成部分证据 —— 这正是本轮之前的状态：

* 标记集：10 个模块（C65 留档时是 9 个；`tests/test_financial_pit_api.py` 这一轮已在集内）；
* 最新留档 `docs/evidence/C65/clean-env-integration-run.txt`：点到 9 个模块；
* 重跑本轮探针（`--item 'AC-16|07'`，见下）读数：`archive_blind = 1` → VERDICT gap。

## 实际做了什么（每个数字都是本机跑出来的）

1. 独立源码快照：`git ls-files --cached --others --exclude-standard` 的 11130 个文件拷到
   `/tmp/od-c151/source`（沿用 C64 记录的教训：不在工作树里构建，历史忽略目录曾把旧 `build/lib`
   混进 wheel）。
2. 两个各自独立的干净 venv（`build_clean_env.sh`，`/tmp/od-c151/clean-a`、`clean-b`，
   base 解释器 CPython 3.11.15）：依赖面恰为 `pyproject.toml` 声明的 `.[web,dev]`，
   `PIP_INSTALL_EXIT=0`、`pip check` 无破洞（`PIP_CHECK_EXIT=0`）、`pip list` 136 行输出里
   grep 不到任何 akshare/openbb 行，`importlib.util.find_spec` 对 akshare / openbb /
   openbb_platform 三者各报 `absent`。
3. `pip wheel --no-deps` 从快照构建 wheel（`WHEEL_BUILD_EXIT=0`，
   `Wheel_SHA256=d6f3165bb09eb213fb2ef4a762f1bf05b96824897d7c3a383ef8d5986424b94d`），再用
   `--force-reinstall --no-deps` 把这**同一个** wheel 装进两个 venv（两次 `WHEEL_INSTALL_EXIT=0`）。
4. 校验目录 `/tmp/od-c151/verification-{a,b}`：快照的副本，**删掉 `opendata` 源码包**，`tests/`
   只留选择式实际选中的 12 个测试文件（其余 `test_*.py` 删除，非测试的 `conftest.py`、
   `fixtures/` 等原样保留）。运行目录里 `import opendata` 解析到
   `/tmp/od-c151/clean-{a,b}/lib/python3.11/site-packages/opendata/__init__.py`，
   即被测的是安装好的发行包而非工作树；档案里这两行 `IMPORT_EXIT=0` 就是该检查的退出码。
   保留的 `alembic_data/versions/`、`examples/`、`scripts/ops/`、`docs/evidence/C34/` 是被守卫
   用例按路径回读的仓库工件，不是可被 import 的顶层源码包。
5. 两个干净解释器各跑一遍，命令即判据原文那条选择式：
   `<venv>/bin/python -m pytest tests -m 'integration and not e2e' --no-cov -o addopts= -v`
   （`-o addopts=` 是把 `pytest.ini` 的 `--cov` 摘掉，不改任何标记/用例，无 `-n`/xdist，无 deselect）：

   | 段 | collected | deselected | selected | 结果 | 用时 | 退出 |
   | --- | --- | --- | --- | --- | --- | --- |
   | A. clean venv（clean-a） | 343 | 114 | 229 | 229 passed / 0 failed / 0 skipped | 196.93s（wall 216.06s） | `CLEAN_A_RUN_EXIT=0` |
   | B. clean venv（clean-b） | 343 | 114 | 229 | 229 passed / 0 failed / 0 skipped | 161.58s（wall 181.85s） | `CLEAN_B_RUN_EXIT=0` |

   两次都全绿，`CLEAN_RUN_EXIT=0` 由脚本按两个真实退出码合取写出（不是手填的 0）。
6. 对账段（档案 C 段）用探针自己的 `integration_units_now()` 现读标记集，与 B 段点到的模块名做
   差集：`marked_units=10`、`marked_modules_not_named_in_section_B=[]`、B 段点到 12 个模块
   （10 个标记模块 + 选择式实际选中的另 2 个函数级标记模块）。

## 本轮不能声称的事

* **不声称台账/门禁已转正。** 本轮只新增 `docs/evidence/C151/`，没有动
  `docs/quality/acceptance-gate-ratchet.json`、`scripts/quality/acceptance_item_probe.py`、
  `pyproject.toml`、`Makefile`、任何 `alembic*/versions/`，也没有改任何测试。勾记入账本的是 CI 上
  那一遍测量，不是本地一次成功运行。
* 本机探针的**活体臂**恰好也是绿的（`blocked_here=yes`、`exit=0`、229 passed / 0 failed /
  0 skipped、`block_installed=0`）。要说清：这台机器的开发解释器本来就没装 akshare/openbb，
  所以缺席既是被 hook 强制的也是环境巧合；判据读的是强制那一部分，真正的清点面在 CI 上另算。
* **构造仪是从 macOS 维护机改写到 Linux 的**：C51/C55 里的 `/Users/...`、anaconda `py313`、
  `VENV=/tmp/c55-clean/venv` 在本机不存在，换成了 `/tmp/od-c151/*` 与 uv 提供的 CPython 3.11.15；
  `/usr/bin/time -p` 本机有，计时行因此与 C55 同形。被量的东西没变。
* 留档里两段都是干净解释器，没有「开发解释器那一遍」的记录（C64/C65 已把留档改成两套干净环境，
  本轮沿用了 C64/C65 的形状而非 C51 的 A/B 形状）；因此档案里没有 `REPO_RUN_EXIT` 这一项，
  有的是 `WHEEL_BUILD_EXIT` / `WHEEL_INSTALL_EXIT_*` / `IMPORT_EXIT_*` / `PIP_CHECK_EXIT_*` /
  `CLEAN_A_RUN_EXIT` / `CLEAN_B_RUN_EXIT` / `CLEAN_RUN_EXIT`。
* 干净环境的依赖面是**声明范围的当次解析结果**（pandas 3.1.0、numpy 2.4.6、sqlalchemy 2.1.8、
  pytest 9.2.0 …），不是开发 venv 的副本；本轮没有为通过而 pin 任何版本。
* 网络很慢（~200–250 kB/s），两次构建/安装耗时较长；这不影响测量本身，但复现者要知道它不是
  一个几分钟就能重跑的仪仗。

## 复现

```bash
BASE=/tmp/od-c151 ROOT=/tmp/od-cleanenv \
  bash docs/evidence/C151/build_clean_env.sh                     # VENV=.../clean-a
VENV=$BASE/clean-b LOG=$BASE/clean-env-build-clean-b.txt \
  bash docs/evidence/C151/build_clean_env.sh
OUT=/tmp/od-cleanenv/docs/evidence/C151/clean-env-integration-run.txt \
  bash docs/evidence/C151/run_p0_integration_surface.sh
```

留档写好后，本条目在探针里的读数是：`archive_blind=0`、`archive_self_written=yes`、
`archive_absent=yes`、`archive_exit=0`（`/tmp/od-stale-venv/bin/python
scripts/quality/acceptance_item_probe.py --item 'AC-16|07'`，正文与 `ARCHIVE_ROUND=C151`
所在目录一致）。
