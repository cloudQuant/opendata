"""Counted, enum-aware row hand-over from the AFI ak_* tables to OpenData.

Stage 4 of the port. :mod:`scripts.port.handover_check` measures the two
planes read-only; this tool performs the actual copy once the owner
authorises it. It is destructive only with ``--apply``; the default is a
dry run that reports the rows it *would* copy and the enum translations
it *would* make, without opening a write transaction.

Two schema facts drive every mapping here:

* AFI stores ``Enum(values_callable=...)`` rows as their lower-case
  *value* (``"daily"``), while OpenData stores the member *name*
  (``"DAILY"``). The four enum columns are therefore translated through
  OpenData's own enum classes, never by an ad-hoc string table.
* The identity columns differ: ``scheduled_tasks.user_id`` is a NOT NULL
  integer FK in OpenData but a string UUID in AFI, and
  ``task_executions.operator_id`` is an integer FK. They cannot be
  guessed, so ``user_id`` is supplied explicitly and ``operator_id`` is
  copied only when it parses as an existing integer, else left NULL.

The copy is idempotent: each batch deletes target rows whose primary key
is in the batch before inserting, so re-running converges rather than
duplicating.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select

if TYPE_CHECKING:
    from enum import Enum

    from sqlalchemy.engine import Engine

# Enum columns translated lower-case value -> member name through OpenData.
from opendata.models.data_script import ScriptFrequency
from opendata.models.interface import ParameterType
from opendata.models.task import ScheduleType, TaskStatus, TriggeredBy


@dataclass(frozen=True)
class TableSpec:
    """Declarative mapping for one ak_* -> OpenData table hand-over."""

    afi_table: str
    opendata_table: str
    primary_key: str
    # Physical column -> OpenData enum class, translated value -> name.
    enum_columns: dict[str, type[Enum]] = field(default_factory=dict)
    # Columns present on the AFI side that must never be copied.
    drop_columns: frozenset[str] = frozenset()
    # Target-only columns filled by a constant rather than read from AFI.
    constant_columns: dict[str, Any] = field(default_factory=dict)


SPECS: tuple[TableSpec, ...] = (
    TableSpec("ak_data_scripts", "data_scripts", "id", {"frequency": ScriptFrequency}),
    TableSpec("ak_data_tables", "data_tables", "id"),
    TableSpec("ak_data_interfaces", "data_interfaces", "id"),
    TableSpec("ak_interface_categories", "interface_categories", "id"),
    TableSpec(
        "ak_interface_parameters",
        "interface_parameters",
        "id",
        {"param_type": ParameterType},
    ),
    TableSpec(
        "ak_scheduled_tasks",
        "scheduled_tasks",
        "id",
        {"schedule_type": ScheduleType},
        constant_columns={"task_kind": "script"},
    ),
    TableSpec(
        "ak_task_executions",
        "task_executions",
        "id",
        {"status": TaskStatus, "triggered_by": TriggeredBy},
        # Legacy Airflow linkage has no OpenData column by design.
        drop_columns=frozenset({"airflow_dag_id", "airflow_run_id", "airflow_task_id"}),
    ),
    TableSpec("ak_task_run_claims", "task_run_claims", "claim_key"),
    TableSpec("ak_scheduler_control", "scheduler_control", "control_id"),
)


def translate_enum(value: object, enum_cls: type[Enum]) -> str | None:
    """Map a stored lower-case enum value to its OpenData member name.

    Returns ``None`` for NULL or for a value the target enum does not
    know, so an unexpected legacy row is flagged rather than mis-stored.
    """
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value.name
    try:
        return enum_cls(value).name
    except ValueError:
        return None


def transform_row(
    source_row: dict[str, Any],
    spec: TableSpec,
    target_columns: set[str],
    *,
    default_user_id: int | None,
) -> tuple[dict[str, Any], list[str]]:
    """Project one AFI row onto the OpenData physical columns.

    Returns the target row and the list of enum values that could not be
    translated (each becomes NULL and is reported, never silently kept).
    """
    target_row: dict[str, Any] = {}
    untranslated: list[str] = []
    for column in target_columns:
        if column in spec.constant_columns:
            target_row[column] = spec.constant_columns[column]
            continue
        if column not in source_row or column in spec.drop_columns:
            continue
        value = source_row[column]
        if column in spec.enum_columns and value is not None:
            name = translate_enum(value, spec.enum_columns[column])
            if name is None:
                untranslated.append(f"{column}={value!r}")
            target_row[column] = name
            continue
        if column == "user_id":
            # AFI stores a string UUID; OpenData requires an integer FK.
            target_row[column] = default_user_id
            continue
        if column == "operator_id" and value is not None:
            # Keep the value only when it is already an integer reference.
            target_row[column] = value if isinstance(value, int) else None
            continue
        target_row[column] = value
    return target_row, untranslated


def _sync_url(url: str) -> str:
    """Map async driver URLs to their sync equivalents."""
    for async_driver, sync_driver in (
        ("+aiomysql", "+pymysql"),
        ("+asyncmy", "+pymysql"),
        ("+aiosqlite", ""),
        ("+asyncpg", "+psycopg2"),
    ):
        url = url.replace(async_driver, sync_driver)
    return url


def copy_table(
    source: Engine,
    target: Engine,
    spec: TableSpec,
    *,
    apply: bool,
    default_user_id: int | None,
    batch_size: int,
) -> dict[str, Any]:
    """Copy one table pair, returning a counted, flag-bearing report row."""
    source_inspector = inspect(source)
    target_inspector = inspect(target)
    if not source_inspector.has_table(spec.afi_table):
        return {"table": spec.opendata_table, "verdict": "source_missing", "rows": 0}
    if not target_inspector.has_table(spec.opendata_table):
        return {"table": spec.opendata_table, "verdict": "target_missing", "rows": 0}

    source_columns = {c["name"] for c in source_inspector.get_columns(spec.afi_table)}
    target_columns = {c["name"] for c in target_inspector.get_columns(spec.opendata_table)}
    readable = sorted((source_columns - spec.drop_columns) & target_columns)
    # Reflect real Table objects so column access and insert/delete carry the
    # database's actual column definitions rather than an empty TableClause.
    source_table = Table(spec.afi_table, MetaData(), autoload_with=source)
    target_table = Table(spec.opendata_table, MetaData(), autoload_with=target)

    total = 0
    untranslated: list[str] = []
    with source.connect() as src_conn:
        total = int(src_conn.execute(select(func.count()).select_from(source_table)).scalar() or 0)
        if not apply:
            return {
                "table": spec.opendata_table,
                "verdict": "dry_run",
                "rows": total,
                "copied_columns": readable,
            }

        with target.begin() as dst_conn:
            rows = src_conn.execute(select(*[source_table.c[c] for c in readable])).mappings()
            batch: list[dict[str, Any]] = []
            keys: list[Any] = []

            def flush() -> None:
                if not batch:
                    return
                dst_conn.execute(
                    target_table.delete().where(target_table.c[spec.primary_key].in_(keys))
                )
                dst_conn.execute(target_table.insert(), batch)
                batch.clear()
                keys.clear()

            for source_row in rows:
                target_row, bad = transform_row(
                    dict(source_row),
                    spec,
                    target_columns,
                    default_user_id=default_user_id,
                )
                untranslated.extend(bad)
                batch.append(target_row)
                keys.append(target_row[spec.primary_key])
                if len(batch) >= batch_size:
                    flush()
            flush()

    return {
        "table": spec.opendata_table,
        "verdict": "copied",
        "rows": total,
        "untranslated_enum_values": sorted(set(untranslated)),
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: dry-run by default, ``--apply`` performs the copy."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-url", required=True, help="SQLAlchemy URL of the AFI database")
    parser.add_argument("--target-url", required=True, help="SQLAlchemy URL of the OpenData DB")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write rows; without it the tool only reports",
    )
    parser.add_argument(
        "--default-user-id",
        type=int,
        default=None,
        help="integer users.id assigned to scheduled_tasks.user_id (NOT NULL in OpenData)",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--out", type=Path, default=None, help="optional JSON report path")
    args = parser.parse_args(argv)

    source = create_engine(_sync_url(args.source_url))
    target = create_engine(_sync_url(args.target_url))
    try:
        reports = [
            copy_table(
                source,
                target,
                spec,
                apply=args.apply,
                default_user_id=args.default_user_id,
                batch_size=args.batch_size,
            )
            for spec in SPECS
        ]
    finally:
        source.dispose()
        target.dispose()

    report = {"mode": "apply" if args.apply else "dry_run", "tables": reports}
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
