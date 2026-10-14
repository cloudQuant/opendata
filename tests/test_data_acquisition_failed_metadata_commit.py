"""Regression tests for failed metadata transactions during downloads."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest
from sqlalchemy.exc import PendingRollbackError

from opendata.models.task import TaskStatus
from opendata.services.data_acquisition import DataAcquisitionService

if TYPE_CHECKING:
    from datetime import datetime


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _PoisonedInterface:
    id = 77

    def __init__(self, session: _PoisonedMetadataSession) -> None:
        self._session = session

    @property
    def name(self) -> str:
        if self._session.poisoned:
            raise PendingRollbackError("metadata session requires rollback")
        return "synthetic_metadata_failure"


class _PoisonedMetadataSession:
    """Small session double that rejects ORM access until rollback."""

    def __init__(
        self,
        *,
        commit_completion_then_raise: bool,
        fail_failed_commit: bool,
        fail_metadata_rollback: bool,
    ) -> None:
        self.poisoned = False
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.commit_completion_then_raise = commit_completion_then_raise
        self.fail_failed_commit = fail_failed_commit
        self.fail_metadata_rollback = fail_metadata_rollback
        self.persisted_status = TaskStatus.PENDING
        self.persisted_start_time: datetime | None = None
        self.persisted_error: str | None = None
        self.initial_execution = SimpleNamespace(
            status=TaskStatus.PENDING,
            start_time=None,
            end_time=None,
            duration=None,
            rows_after=None,
            error_message=None,
        )
        self.reloaded_execution = SimpleNamespace(
            status=TaskStatus.PENDING,
            start_time=None,
            end_time=None,
            duration=None,
            rows_after=19,
            error_message=None,
        )
        self._interface = _PoisonedInterface(self)

    async def execute(self, _statement: object) -> _Result:
        if self.poisoned:
            raise PendingRollbackError("metadata query attempted before rollback")
        self.execute_count += 1
        if self.execute_count == 1:
            return _Result(self._interface)
        if self.execute_count == 2:
            return _Result(self.initial_execution)
        if self.execute_count == 3:
            self.reloaded_execution.status = self.persisted_status
            self.reloaded_execution.start_time = self.persisted_start_time
            self.reloaded_execution.error_message = self.persisted_error
            return _Result(self.reloaded_execution)
        raise AssertionError("unexpected metadata query")

    def _persist_current(self) -> None:
        self.persisted_status = self.reloaded_execution.status
        self.persisted_start_time = self.reloaded_execution.start_time
        self.persisted_error = self.reloaded_execution.error_message

    async def commit(self) -> None:
        self.commit_count += 1
        if self.commit_count == 1:
            self.persisted_status = self.initial_execution.status
            self.persisted_start_time = self.initial_execution.start_time
            return
        if self.commit_count == 2:
            if self.commit_completion_then_raise:
                self.persisted_status = self.initial_execution.status
                self.persisted_start_time = self.initial_execution.start_time
            self.poisoned = True
            raise RuntimeError("AC21_SYNTHETIC_METADATA_COMMIT_FAILURE")
        if self.commit_count == 3 and self.fail_failed_commit:
            self.poisoned = True
            raise RuntimeError("synthetic failed-state commit failure")
        if self.commit_count == 3:
            self._persist_current()
            return
        raise AssertionError("unexpected metadata commit")

    async def rollback(self) -> None:
        self.rollback_count += 1
        if self.fail_metadata_rollback and self.rollback_count == 1:
            raise PendingRollbackError("synthetic metadata rollback failure")
        self.poisoned = False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "completion_commit_then_raise",
        "fail_failed_commit",
        "fail_metadata_rollback",
        "expected_persisted_status",
        "expected_commit_count",
        "expected_execute_count",
        "expected_rollback_count",
    ),
    [
        (False, False, False, TaskStatus.FAILED, 3, 3, 1),
        (False, True, False, TaskStatus.RUNNING, 3, 3, 2),
        (True, False, False, TaskStatus.COMPLETED, 2, 3, 1),
        (False, False, True, TaskStatus.RUNNING, 2, 2, 1),
    ],
    ids=[
        "failed-commit-then-record-failed",
        "failed-status-write-fails",
        "do-not-overwrite-completed",
        "rollback-failure-does-not-query-poisoned-session",
    ],
)
async def test_metadata_commit_error_rolls_back_before_reloading_execution(
    completion_commit_then_raise: bool,
    fail_failed_commit: bool,
    fail_metadata_rollback: bool,
    expected_persisted_status: TaskStatus,
    expected_commit_count: int,
    expected_execute_count: int,
    expected_rollback_count: int,
) -> None:
    db = _PoisonedMetadataSession(
        commit_completion_then_raise=completion_commit_then_raise,
        fail_failed_commit=fail_failed_commit,
        fail_metadata_rollback=fail_metadata_rollback,
    )
    data_db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    service = DataAcquisitionService()

    with (
        patch.object(
            service,
            "_fetch_interface_data",
            new_callable=AsyncMock,
            return_value=pd.DataFrame({"value": [1, 2]}),
        ),
        patch.object(service, "_store_data", new_callable=AsyncMock, return_value=2),
        patch.object(service, "_count_table_rows", new_callable=AsyncMock, return_value=2),
        pytest.raises(RuntimeError, match="AC21_SYNTHETIC_METADATA_COMMIT_FAILURE"),
    ):
        await service.execute_download(
            execution_id=91,
            interface_id=77,
            parameters={},
            db=db,
            data_db=data_db,
        )

    assert db.execute_count == expected_execute_count
    assert db.commit_count == expected_commit_count
    assert db.rollback_count == expected_rollback_count
    assert db.persisted_status == expected_persisted_status
    assert db.initial_execution.status == TaskStatus.COMPLETED
    assert data_db.commit.await_count == 1
    assert data_db.rollback.await_count == 1
    assert 91 not in service._active_executions

    if expected_persisted_status == TaskStatus.FAILED:
        assert db.persisted_error == "AC21_SYNTHETIC_METADATA_COMMIT_FAILURE"
        assert db.reloaded_execution.end_time is not None
        assert db.reloaded_execution.duration >= 0
        assert db.reloaded_execution.rows_after is None
    elif expected_persisted_status == TaskStatus.COMPLETED:
        assert db.persisted_error is None
        assert db.reloaded_execution.status == TaskStatus.COMPLETED
    elif fail_metadata_rollback:
        assert db.poisoned is True
        assert db.reloaded_execution.status == TaskStatus.PENDING
