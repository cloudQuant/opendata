"""The pre-handover checker must report counts, never write, and flag gaps."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text

_SPEC = importlib.util.spec_from_file_location(
    "handover_check",
    Path(__file__).resolve().parents[1] / "scripts" / "port" / "handover_check.py",
)
assert _SPEC is not None and _SPEC.loader is not None
handover_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handover_check)


def _engine_with(table: str, rows: int):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
        for i in range(rows):
            conn.execute(text(f"INSERT INTO {table} (id) VALUES (:i)"), {"i": i})  # noqa: S608 - test-controlled identifier
    return engine


def test_count_match_is_reported_when_both_sides_agree() -> None:
    source = _engine_with("ak_data_scripts", 3)
    target = _engine_with("data_scripts", 3)
    row = handover_check.compare_pair(source, target, "ak_data_scripts", "data_scripts")
    assert row["verdict"] == "count_match"
    assert row["source_rows"] == 3 and row["target_rows"] == 3


def test_empty_target_is_distinguished_from_a_missing_table() -> None:
    source = _engine_with("ak_data_scripts", 3)
    empty = _engine_with("data_scripts", 0)
    missing = create_engine("sqlite://")
    assert (
        handover_check.compare_pair(source, empty, "ak_data_scripts", "data_scripts")["verdict"]
        == "target_empty"
    )
    assert (
        handover_check.compare_pair(source, missing, "ak_data_scripts", "data_scripts")["verdict"]
        == "missing_table"
    )


def test_count_difference_is_not_reported_as_match() -> None:
    source = _engine_with("ak_data_scripts", 5)
    target = _engine_with("data_scripts", 2)
    row = handover_check.compare_pair(source, target, "ak_data_scripts", "data_scripts")
    assert row["verdict"] == "count_differs"
