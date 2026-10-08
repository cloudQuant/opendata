# C64 本地验收复现

工作目录：`/Users/yunjinqi/Documents/new_projects/opendata`。
本轮开发基线为 `d552c081950edffce167e3dfc0be74f37a6950a1`，包含尚未提交的 C64 修改。

## 独立运行环境与固定工具版本

Python 始终通过用户 Anaconda `base` 调用。早期检查使用继承 base 的固定工具环境；最终门禁
使用 Anaconda 创建、不继承全局包的 `/tmp/opendata-c64-clean-env-20260930`。
Ruff 0.15.20、mypy 2.3.1、Bandit 1.9.4 由仓库 `dev` extra 固定。全局 Python 包未修改。

必须从独立源码副本构建：工作区历史忽略目录 `build/lib/akshare` 曾混入 wheel。
下列步骤保留原工作区，复制 git 管理和非忽略的当前文件，并用唯一临时目录避免复用旧构建结果。

```bash
export ITER01_VERIFY_ROOT="$(mktemp -d /tmp/opendata-iter01-verify.XXXXXX)"
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python - <<'PY'
from pathlib import Path
import os, shutil, subprocess
source = Path(os.environ['ITER01_VERIFY_ROOT']) / 'source'
source.mkdir()
files = subprocess.check_output(
    ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z']
).decode().split('\0')
for name in dict.fromkeys(files):
    path = Path(name)
    if name and path.is_file():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
PY
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python -m venv \
  "$ITER01_VERIFY_ROOT/venv"
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv/bin/python" -m pip install "$ITER01_VERIFY_ROOT/source[web,dev]"
```

## 完整源码门禁

仿射复权字段由仓库迁移 `0007_affine_stock_adjust` 增加。既有 0006 升级后旧因子仍在，
新增六列均为 NULL；数据库升级完成并不代表历史行已经重建。重建入口如下，需沿用下文
同一组隔离数据库环境变量；在测试库中准备注册的 THS ODS 日线与公司行动后执行：

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv/bin/python" -m scripts.ops.rebuild_adjustment_factors
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv/bin/python" -m scripts.ops.rebuild_adjustment_factors --apply
```

第一条只计算和验证，`written=0`；第二条才写入。`--start/--end` 限制写入的交易日，
计算锚点仍使用完整可用日线。新增事件后的前复权历史需要全量重建；局部重建留下的
同标的新旧版本混合窗口会拒绝复权查询。基准外事件单独计数，不能补出缺失历史。
未复权查询与成交量/成交额不受复权合成影响。

主线程实际 MySQL 演练结果见 `affine-mysql-primary.txt`：从头升级、旧 0006 升级、旧值及
溯源保留、dry-run 零写入、现金分红与送转非交换事件链、局部重建混版拒绝、幂等和
降级再升级均逐项记录。正式仓库写入未执行；官方五标的、双方法新对照见
`qfq-official-akshare-affine-real.txt`：qfq 5 PASS（中位数锚定形状判定）、hfq 5 ERROR，
总退出 1，仍不能通过 AC-11|02。真实源数据只读复制、隔离重建与清理见
`affine-real-series-primary.txt`；该记录没有使用手算价格代替源数据。

前端先按锁文件安装依赖，再安装该 Playwright 版本要求的 Chromium。浏览器安装和门禁
必须使用同一 `PLAYWRIGHT_BROWSERS_PATH`；只存在 npm 包不能证明浏览器可启动。

```bash
(cd frontend && npm ci)
export PLAYWRIGHT_BROWSERS_PATH="$ITER01_VERIFY_ROOT/pw-browsers"
(cd frontend && ./node_modules/.bin/playwright install chromium)
```

先创建本轮专用 MySQL；复现时使用新的容器名，确认 33564 未被其他进程占用。
该命令仅绑定 localhost、不挂载宿主机数据目录。容器启动后等待 `mysqladmin ping`
成功，再创建两库并执行两套迁移。

```bash
docker run --rm -d --name opendata-iteration01-verify \
  -p 127.0.0.1:33564:3306 -e MYSQL_ALLOW_EMPTY_PASSWORD=yes \
  mysql:8.0 --server-id=164 --log-bin=mysql-bin --binlog-format=ROW \
  --binlog-expire-logs-seconds=604800
docker exec opendata-iteration01-verify mysqladmin -uroot ping
docker exec opendata-iteration01-verify mysql -uroot -e \
  'CREATE DATABASE opendata_iter01 CHARACTER SET utf8mb4; CREATE DATABASE opendata_iter01_data CHARACTER SET utf8mb4;'
```

在下列同一组隔离数据库环境变量下、通过 Anaconda 包装的独立解释器，先执行
`-m alembic upgrade head`，再执行 `-m alembic -c alembic_data.ini upgrade head`。
需要默认角色/接口种子时执行 `-m opendata.cli init-db`。禁止在现有业务库上复用这些命令。
检查结束仅用 `docker stop opendata-iteration01-verify` 清理自己创建的容器。

以下环境关闭调度与 Redis，测试默认排除真实上游 E2E。MySQL 地址必须指向本轮自己创建的
隔离实例；本轮实例使用 `127.0.0.1:33564`，没有挂载宿主机数据目录。

```bash
env -u OPENDATA_ALLOW_LIVE_E2E \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=33564 MYSQL_USER=root MYSQL_PASSWORD='' \
  MYSQL_DATABASE=opendata_iter01 \
  DATA_MYSQL_HOST=127.0.0.1 DATA_MYSQL_PORT=33564 \
  DATA_MYSQL_USER=root DATA_MYSQL_PASSWORD='' \
  DATA_MYSQL_DATABASE=opendata_iter01_data \
  REDIS_URL='' ENABLE_SCHEDULER=false APP_ENV=testing CI=1 \
  PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" \
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  env PATH="$ITER01_VERIFY_ROOT/venv/bin:$PATH" make gate
```

`PATH` 在 conda 激活之后设置，保证门禁及其 Python 子进程使用独立解释器。
本轮两套独立干净环境的集成选择式均为 217 passed / 0 failed / 0 skipped；其档案绑定仿射扩展后的源码，见 `clean-env-integration-run.txt`。
新增集成模块或改动查询拼 SQL 后，需要重新生成干净环境/EXPLAIN 证据，不能照搬旧档案。

前端类型检查使用 `npx vue-tsc -b --force`，检查引用的实际项目。
单独执行 `vue-tsc --noEmit` 的退出码不计为本轮项目类型检查证据。

## 干净环境与配对备份复现

以同一个最终源码副本另建第二个 Anaconda venv，安装声明的 `.[web,dev]` 依赖，
再将同一份新 wheel 安装到两个环境。第一套环境已由本文开头创建；第二套及双环境命令如下：

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  python -m venv "$ITER01_VERIFY_ROOT/venv-b"
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv-b/bin/python" -m pip install "$ITER01_VERIFY_ROOT/source[web,dev]"
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv/bin/python" -m pip wheel --no-deps \
  --wheel-dir "$ITER01_VERIFY_ROOT/wheels" "$ITER01_VERIFY_ROOT/source"
for ITER01_VERIFY_ENV in venv venv-b; do
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
    "$ITER01_VERIFY_ROOT/$ITER01_VERIFY_ENV/bin/python" -m pip install \
    --force-reinstall --no-deps "$ITER01_VERIFY_ROOT/wheels"/opendata-*.whl
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
    "$ITER01_VERIFY_ROOT/$ITER01_VERIFY_ENV/bin/python" -m pip check
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
    "$ITER01_VERIFY_ROOT/$ITER01_VERIFY_ENV/bin/python" -c \
    'import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ("akshare", "openbb", "openbb_platform"))'
  env -u OPENDATA_ALLOW_LIVE_E2E \
    MYSQL_HOST=127.0.0.1 MYSQL_PORT=33564 MYSQL_USER=root MYSQL_PASSWORD='' \
    MYSQL_DATABASE=opendata_iter01 \
    DATA_MYSQL_HOST=127.0.0.1 DATA_MYSQL_PORT=33564 \
    DATA_MYSQL_USER=root DATA_MYSQL_PASSWORD='' DATA_MYSQL_DATABASE=opendata_iter01_data \
    REDIS_URL='' ENABLE_SCHEDULER=false APP_ENV=testing \
    /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
    "$ITER01_VERIFY_ROOT/$ITER01_VERIFY_ENV/bin/python" -m pytest tests \
    -m 'integration and not e2e' --no-cov -v
done
```

两个解释器分别保留完整逐项输出和退出码；不能复制旧记录或将零条选中、跳过计为通过。
探针读取 `clean-env-integration-run.txt` 的既有日志契约：正文须声明 `ARCHIVE_ROUND=C64`，
使用 `===== B. clean venv =====` 分隔第二套输出，并由两次测试实际成功的验收包装器
记录 `CLEAN_RUN_EXIT=0`。只写每环境退出码而缺少该总退出码，会被现有探针拒绝；
不能事后给未执行的档案补一个成功值。
本轮还逐一比对 installed `opendata`、`opendata_fuyao`、`opendata_http`、`opendata_client`
四个运行时包的 529 个 Python 文件与工作区 SHA-256，记录于
`affine-clean-environments-refresh.txt`、`clean-env-integration-run.txt`。

备份镜像可用仅包含 `scripts/ops/Dockerfile.backup`、`backup_mysql.sh`、
`backup_runner.sh`、`backup_minute_snapshot.py` 的独立构建上下文，保持这四个文件原路径。
本轮实际镜像源码 SHA 和客户端版本记录在 `paired-backup-image-provenance.txt`。
启用 `BACKUP_MINUTE_ARCHIVE_DIR`，挂载独立分钟目录和备份输出目录，使用分库备份账号与
权限为 0600 的 env 文件；只连接自己创建的 MySQL 容器。真实演练至少包括：

1. 导入两日分钟数据，并修订其中一个时间戳，保留旧不可变文件。
2. 实际镜像执行配对备份，要求退出 0、仅有一个完整 snapshot、没有 staging 残留。
3. 验证 payload 和逐分钟文件的大小、SHA-256；安全解包 tar 到空目录。
4. SQL 恢复到两套新建的隔离 schema，逐表精确核对行数；使用恢复索引与文件重新查询，
   对照修订值、时间戳和全部记录。只比较 tar 存在或文件大小不足以证明恢复可查询。
5. 删除自己创建的索引夹具、恢复 schema、账号和临时文件，确认源库恢复原始行数。

本轮结果是 metadata 14 表、warehouse 18 表一致；2 个索引 shard、3 个文件恢复后查询 4 行。
详细口径与生产限制见 `docs/operations-backup-restore.md`。

## 有界、独立检查

每个 `.txt` 证据文件记录对应命令、环境范围、实际结果；原始测试输出保留完整行。
测试输出中的尾随空格属于原始记录，源码差异检查与原始记录分别报告。

```bash
env REDIS_URL='' ENABLE_SCHEDULER=false APP_ENV=testing \
  /Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base \
  "$ITER01_VERIFY_ROOT/venv/bin/python" -m pytest \
  tests/test_scheduled_pipeline_tasks.py tests/test_api_tasks_full.py \
  tests/test_api_tasks_lifecycle.py tests/test_scheduler.py \
  tests/test_scheduler_full.py tests/test_retry_service.py tests/test_retry_service_full.py \
  -q -m 'not e2e' --no-cov
```

隔离 MySQL 的迁移、分区写入、DWD 回填、分钟文件 API 与双库恢复是独立检查，
不代表正式仓库已迁移、生产每日备份已启用或真实供应商验收已通过。
现有 GB 表查询使用只读事务，记录在 `warehouse-gb-query.txt`，本轮写入数为零。

AC-11|04 的 EXPLAIN 档案必须绑定当前 `opendata/pipeline/query.py` 的源码摘要。
仿射扩展后本轮重新运行 `scripts/ops/window_partition_explain.py`，在每个真实连接上
确认 `@@session.transaction_read_only=1` 并拒绝写语句；当前 6 张现有分区表均裁剪，
见 `window-pruning-explain.txt`。查询模块变更后旧计划不能继续当作当前证明。
