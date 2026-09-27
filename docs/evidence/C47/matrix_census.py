#!/usr/bin/env python3
"""C47 现场读数：AC-13|07 的四类信号在真库上各量得出什么（只读）.

判据原文是「**告警矩阵生效**：pipeline 失败 / 连续失败 / 分区缺失 / 磁盘水位」。
离线夹具能证明四类都有生产者、都能出帧，证明不了两件本轮最关心的事：

1. **失败生产者读的是不是真表**。控制库里有两张候选：``pipeline_progress``（分片级
   断点）与 ``task_executions``（执行记录级）。选错的后果是**恒不告警**——一张有行
   却从来没有过 ``failed`` 的表，从它上面算「连续失败」就是把「没有失败」和「没有
   读数」混为一谈。本轮把两张表的行数与状态分布都量出来，再据以定生产者。
2. **失败生产者那条 SQL 在 MySQL 上跑不跑得动**。它按 run 聚合
   ``SUM(CASE ...)``/``MIN(CASE ...)`` 并按分组列排序，``ONLY_FULL_GROUP_BY`` 有权
   拒绝；SQLite 夹具通过不代表真驱动通过。所以本轮让**生产代码自己**在真库上跑一遍，
   并把它发出的 SQL 从驱动层原样打出来。

第三条是安全边界：分区面**只报不改**。矩阵指的就是生产数仓，``REORGANIZE PARTITION``
属生产 DDL，未经确认不得执行 ⇒ 本轮把两条引擎上出现过的每一条语句按首关键字归类，
只要出现读面之外的语句（ALTER/CREATE/DROP/INSERT/UPDATE/DELETE）就以非零码退出。

只读：不打印连接串（URL 里含口令），不读取任何密钥值，不做任何写入或 DDL。
跑法见 ``docs/evidence/C47/README.md``。
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

#: Statements a read-only matrix run may legitimately send.
READ_FIRST_WORDS = {"SELECT", "SHOW", "SET", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"}


def attach(engine: Engine) -> list[str]:
    """Capture every statement one engine executes, at the driver boundary."""
    from sqlalchemy import event

    sent: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sent.append(statement)

    return sent


def report_statements(name: str, statements: list[str]) -> int:
    """Print the shape of what an engine actually sent; return the offender count."""
    words = Counter(str(statement).strip().split(None, 1)[0].upper() for statement in statements)
    print(f"{name}: {len(statements)} statements -> {dict(sorted(words.items()))}")
    offenders = [
        statement
        for statement in statements
        if str(statement).strip().split(None, 1)[0].upper() not in READ_FIRST_WORDS
    ]
    for statement in offenders:
        print(f"  NOT-READ-ONLY> {str(statement)[:200]}")
    return len(offenders)


def control_shape(engine: Engine) -> None:
    """What the two candidate failure sources in the control database hold."""
    from sqlalchemy import text

    def rows(sql: str, **params: object) -> list[tuple[Any, ...]]:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(sql), params).all()]

    total, pipelines, legs, windows = rows(
        "SELECT COUNT(*), COUNT(DISTINCT pipeline_id), "
        "COUNT(DISTINCT CONCAT(domain, '/', source)), COUNT(DISTINCT window_end) "
        "FROM pipeline_progress"
    )[0]
    print(f"\npipeline_progress: rows={total} runs={pipelines} legs={legs} windows={windows}")
    if total:
        per_run = sorted(
            int(n) for (n,) in rows("SELECT COUNT(*) FROM pipeline_progress GROUP BY pipeline_id")
        )
        print(
            "shards per run: "
            f"min={per_run[0]} median={per_run[len(per_run) // 2]} max={per_run[-1]}"
        )
        statuses = Counter(str(s) for (s,) in rows("SELECT status FROM pipeline_progress"))
        failed = sum(n for s, n in statuses.items() if s.lower().endswith("failed"))
        print(f"shard status census: {dict(sorted(statuses.items()))} | FAILED={failed}")
        newest, oldest = rows("SELECT MAX(window_end), MIN(window_end) FROM pipeline_progress")[0]
        print(f"window_end: newest={newest} oldest={oldest}")
    executions = rows("SELECT COUNT(*) FROM task_executions")[0][0]
    exec_census = rows("SELECT status, COUNT(*) FROM task_executions GROUP BY status")
    print(
        f"task_executions: rows={executions} "
        f"status census={ {str(s): int(n) for s, n in exec_census} }"
    )
    print(
        "choice: pipeline_progress is keyed by (domain, source, window) while task_executions "
        "is keyed by task_id/script_id ⇒ the per-leg streak comes from the former"
    )


def live_failure_read(engine: Engine, run_limit: int) -> None:
    """Run the producer's own aggregate on MySQL and print what the driver got."""
    from opendata.pipeline.alert_matrix import collect_failures

    print(f"\ncollect_failures(engine, run_limit={run_limit}) on the live control schema:")
    failures, legs = collect_failures(engine, run_limit=run_limit)
    print(f"  -> failures={len(failures)} legs_with_history={legs}")
    for failure in failures:
        print(
            f"  failure: {failure.domain}/{failure.source} "
            f"streak={failure.consecutive_failures} error={failure.last_error[:80]!r}"
        )
    if legs:
        print("  reading: failures=0 with legs>0 means 'nothing failed', not 'nothing was read'")
    else:
        print(
            "  reading: legs=0 -> the failure rows of the matrix are empty by fact, not by health"
        )


def partition_shape(engine: Engine, current_year: int, years_ahead: int) -> None:
    """What the partition face measures, and what it refuses to touch."""
    from opendata.pipeline.alert_matrix import collect_partitions, warehouse_tables

    tables = warehouse_tables()
    print(f"\npartition surface: {len(tables)} warehouse tables in scope ({tables[:3]}[0:3] shown)")
    plans, partitioned = collect_partitions(
        engine, current_year=current_year, years_ahead=years_ahead
    )
    print(f"collect_partitions() -> partitioned_tables={partitioned} tables_with_gaps={len(plans)}")
    for table, missing in sorted(plans.items()):
        names = ", ".join(f"{name}<{bound.isoformat()}" for name, bound in missing)
        print(f"  gap {table}: {names}")
    if not plans:
        print("  no gap reported (see partitioned_tables: 0 would mean nothing is partitioned)")


def disk_shape(path: Path) -> None:
    """What the disk face measures on this host."""
    from opendata.pipeline.alert_matrix import collect_disk

    disk, measured = collect_disk(path)
    print(f"\ndisk face: configured data_dir={path!r}")
    if disk is None:
        print("  no reading (collect_disk returned None) -> the matrix reports it as such")
        return
    print(
        f"  measured volume={measured!r} used={disk.used_bytes} total={disk.total_bytes} "
        f"ratio={disk.used_ratio:.4f}"
    )
    print(
        f"  thresholds: warning>=0.80 critical>=0.90 -> this reading is "
        f"{'over warning' if disk.used_ratio >= 0.8 else 'under warning'}"
    )


async def joined_run(warehouse: Engine, control: Engine, disk_path: Path) -> None:
    """One whole matrix run on the live databases, with delivery switched off."""
    from opendata.pipeline.alert_matrix import run_alert_matrix
    from opendata.pipeline.trading_calendar import resolve_calendar

    run_date = date.today()
    # The same expectation the scheduled job feeds the matrix: the trading
    # calendar's answer, not the wall clock. Measuring against today (a
    # Sunday) would report a lag the criterion never asks for.
    calendar = await asyncio.to_thread(resolve_calendar, warehouse)
    expected = calendar.expected_data_date(run_date)
    print(
        f"\ncalendar: run_date={run_date} expected={expected} "
        f"tier={calendar.tier} decided_by={calendar.answered_from(run_date)}"
    )
    run = await run_alert_matrix(
        warehouse,
        expected=expected,
        control_engine=control,
        current_year=run_date.year,
        disk_path=disk_path,
        broadcast=None,
    )
    print(f"run_alert_matrix(expected={expected}, broadcast=None):")
    scope = run.scope.as_dict()
    print(f"  scope: {scope}")
    print(f"  reports={len(run.reports)} alerts={len(run.alerts)} delivered={run.delivered}")
    by_rule = Counter(alert.rule for alert in run.alerts)
    print(f"  alerts by rule: {dict(sorted(by_rule.items()))}")
    for alert in run.alerts:
        print(f"    [{alert.severity}] {alert.rule} {alert.subject}: {alert.detail}")
    statuses = Counter(report.status for report in run.reports)
    print(f"  freshness report statuses: {dict(sorted(statuses.items()))}")


def main() -> None:
    """Read both databases and the filesystem, then report."""
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.pool import NullPool

    from opendata.core.config import settings
    from opendata.data.providers import register_providers
    from opendata.pipeline.alert_matrix import FAILURE_RUN_LIMIT

    # The scheduled job runs inside the API process, where the provider
    # registry is populated at startup; without it the measurement would see
    # 20 dwd tables and no ods leg at all, which is not production's surface.
    register_providers()
    warehouse = create_engine(settings.data_database_url, poolclass=NullPool)
    control = create_engine(settings.database_url_sync, poolclass=NullPool)
    warehouse_sent = attach(warehouse)
    control_sent = attach(control)
    print(f"warehouse database (name only): {warehouse.url.database}")
    print(f"control database (name only): {control.url.database}")
    print(f"control tables: {sorted(inspect(control).get_table_names())}")

    control_shape(control)
    live_failure_read(control, FAILURE_RUN_LIMIT)
    today = date.today()
    partition_shape(warehouse, today.year, 2)
    disk_shape(settings.data_dir)
    asyncio.run(joined_run(warehouse, control, settings.data_dir))

    offenders = report_statements("warehouse engine", warehouse_sent)
    offenders += report_statements("control engine", control_sent)
    print(f"\nguard: non-readonly statements issued = {offenders}")
    warehouse.dispose()
    control.dispose()
    if offenders:
        print("FAIL: the matrix run was not read-only")
        sys.exit(1)
    print("OK: all four faces measured; no DDL, no writes")


if __name__ == "__main__":
    main()
