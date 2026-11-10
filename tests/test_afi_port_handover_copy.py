"""Offline tests for the stage-4 counted row hand-over tool.

Two in-memory SQLite engines stand in for the AFI source (lower-case enum
*values*, legacy Airflow columns, string user ids) and the OpenData target
(built from the real model metadata, enum *name* storage, integer FKs). No
network or live database is involved.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

import opendata.models  # noqa: F401  # register every model on Base.metadata
from opendata.core.database import Base
from opendata.models.data_script import ScriptFrequency
from opendata.models.task import ScheduleType, TaskStatus, TriggeredBy
from scripts.port.handover_copy import (
    SPECS,
    TableSpec,
    copy_table,
    transform_row,
    translate_enum,
)


@pytest.fixture
def engines():
    target = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(target)
    source = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    yield source, target
    source.dispose()
    target.dispose()


def _spec(name: str) -> TableSpec:
    return next(s for s in SPECS if s.opendata_table == name)


def test_translate_enum_value_to_name_and_unknown_is_none() -> None:
    assert translate_enum("daily", ScriptFrequency) == "DAILY"
    assert translate_enum(ScriptFrequency.HOURLY, ScriptFrequency) == "HOURLY"
    assert translate_enum(None, ScriptFrequency) is None
    assert translate_enum("not_a_frequency", ScriptFrequency) is None


def test_dry_run_writes_nothing(engines) -> None:
    source, target = engines
    source_engine = source
    with source_engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE ak_data_scripts (id INTEGER PRIMARY KEY, script_id VARCHAR(100), "
                "frequency VARCHAR(20), is_active BOOLEAN)"
            )
        )
        conn.execute(text("INSERT INTO ak_data_scripts VALUES (1, 's1', 'daily', 1)"))

    report = copy_table(
        source, target, _spec("data_scripts"), apply=False, default_user_id=None, batch_size=100
    )
    assert report["verdict"] == "dry_run"
    assert report["rows"] == 1
    with target.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM data_scripts")).scalar() == 0


def test_apply_translates_enums_and_is_idempotent(engines) -> None:
    source, target = engines
    with source.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE ak_data_scripts ("
                "id INTEGER PRIMARY KEY, script_id VARCHAR(100) NOT NULL, "
                "script_name VARCHAR(200), category VARCHAR(50), frequency VARCHAR(20), "
                "is_active BOOLEAN, is_custom BOOLEAN, source VARCHAR(50), timeout INTEGER, "
                "estimated_duration INTEGER, created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ak_data_scripts ("
                "id, script_id, script_name, category, frequency, is_active, "
                "is_custom, source, timeout, estimated_duration, created_at, updated_at) "
                "VALUES "
                "(1, 's1', 'One', 'stock', 'daily', 1, 0, "
                "'akshare', 60, 0, '2026-01-01', '2026-01-01'),"
                "(2, 's2', 'Two', 'fund', 'weekly', 1, 0, "
                "'akshare', 60, 0, '2026-01-01', '2026-01-01'),"
                "(3, 's3', 'Bad', 'stock', 'mystery', 1, 0, "
                "'akshare', 60, 0, '2026-01-01', '2026-01-01')"
            )
        )

    spec = _spec("data_scripts")
    first = copy_table(source, target, spec, apply=True, default_user_id=None, batch_size=2)
    assert first["verdict"] == "copied"
    assert first["rows"] == 3
    assert any("frequency='mystery'" in flag for flag in first["untranslated_enum_values"])

    with target.connect() as conn:
        rows = {r[0]: r[1] for r in conn.execute(text("SELECT id, frequency FROM data_scripts"))}
    assert rows == {1: "DAILY", 2: "WEEKLY", 3: None}

    # Re-running converges instead of duplicating (delete-by-batch-PK then insert).
    copy_table(source, target, spec, apply=True, default_user_id=None, batch_size=2)
    with target.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM data_scripts")).scalar() == 3


def test_task_executions_drop_airflow_and_translate_status(engines) -> None:
    source, target = engines
    with source.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE ak_task_executions ("
                "id INTEGER PRIMARY KEY, execution_id VARCHAR(100), "
                "task_id INTEGER, script_id VARCHAR(100), status VARCHAR(20), "
                "triggered_by VARCHAR(20), operator_id VARCHAR(36), retry_count INTEGER, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "airflow_dag_id VARCHAR(50), airflow_run_id VARCHAR(50), "
                "airflow_task_id VARCHAR(50))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ak_task_executions ("
                "id, execution_id, task_id, script_id, status, triggered_by, "
                "operator_id, retry_count, created_at, updated_at, "
                "airflow_dag_id, airflow_run_id, airflow_task_id) VALUES "
                "(10, 'ak_exec_10', NULL, 's1', 'completed', 'scheduler', "
                "'uuid-not-an-int', 0, '2026-01-01', '2026-01-01', 'dag', 'run', 'task')"
            )
        )

    report = copy_table(
        source, target, _spec("task_executions"), apply=True, default_user_id=None, batch_size=100
    )
    assert report["verdict"] == "copied"
    target_cols = {c["name"] for c in inspect(target).get_columns("task_executions")}
    assert {"airflow_dag_id", "airflow_run_id", "airflow_task_id"}.isdisjoint(target_cols)
    with target.connect() as conn:
        row = conn.execute(
            text("SELECT status, triggered_by, operator_id FROM task_executions WHERE id = 10")
        ).one()
    assert row == ("COMPLETED", "SCHEDULER", None)


def test_scheduled_tasks_get_constant_kind_and_default_user(engines) -> None:
    source, target = engines
    with source.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE ak_scheduled_tasks (id INTEGER PRIMARY KEY, name VARCHAR(255), "
                "user_id VARCHAR(36), script_id VARCHAR(100), schedule_type VARCHAR(20), "
                "schedule_expression VARCHAR(100), is_active BOOLEAN, retry_on_failure BOOLEAN, "
                "max_retries INTEGER, timeout INTEGER, parameters JSON, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ak_scheduled_tasks ("
                "id, name, user_id, script_id, schedule_type, schedule_expression, "
                "is_active, retry_on_failure, max_retries, timeout, parameters, "
                "created_at, updated_at) "
                "VALUES (7, 'T', 'uuid-abc', 's1', 'cron', '0 8 * * *', 1, 1, 3, 0, '{}', "
                "'2026-01-01', '2026-01-01')"
            )
        )

    copy_table(
        source,
        target,
        _spec("scheduled_tasks"),
        apply=True,
        default_user_id=42,
        batch_size=100,
    )
    with target.connect() as conn:
        row = conn.execute(
            text("SELECT task_kind, user_id, schedule_type FROM scheduled_tasks WHERE id = 7")
        ).one()
    assert row == ("script", 42, "CRON")


def test_missing_tables_are_reported_not_guessed(engines) -> None:
    source, target = engines
    report = copy_table(
        source, target, _spec("data_scripts"), apply=True, default_user_id=None, batch_size=100
    )
    assert report["verdict"] == "source_missing"


def test_transform_row_projects_only_target_columns() -> None:
    spec = TableSpec("ak_x", "data_scripts", "id", {"frequency": ScriptFrequency})
    row, bad = transform_row(
        {"id": 1, "frequency": "daily", "ghost": "x"},
        spec,
        target_columns={"id", "frequency"},
        default_user_id=None,
    )
    assert row == {"id": 1, "frequency": "DAILY"}
    assert "ghost" not in row
    assert bad == []


def test_enum_classes_cover_all_declared_enum_columns() -> None:
    """Guard: every enum column in the specs maps to a real OpenData enum."""
    from enum import Enum

    from opendata.models.interface import ParameterType

    allowed = (ScriptFrequency, TaskStatus, TriggeredBy, ScheduleType, ParameterType)
    for spec in SPECS:
        for column, enum_cls in spec.enum_columns.items():
            assert column  # the physical column name is non-empty
            assert issubclass(enum_cls, Enum)
            assert issubclass(enum_cls, allowed)
