"""
Comprehensive tests for data acquisition API endpoints.

Covers download trigger, progress, and result endpoints.
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.data import _log_task_exception
from opendata.models.interface import DataInterface, InterfaceCategory
from opendata.models.task import TaskExecution, TaskStatus


@pytest.fixture
def log_lines():
    """Collect ``(level, message)`` emitted through loguru during a test.

    The download route answers before its background task runs, so the log
    line is the only observable half of a failed download - a mock on the
    logger would only prove the call site, not that a readable message
    reaches the logging system.
    """
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield records
    finally:
        logger.remove(handler_id)


async def _wait_until(predicate, *, attempts: int = 400, interval: float = 0.005) -> bool:
    """Yield to the loop until ``predicate()`` holds, bounded by ``attempts``.

    Returns:
        Whether the predicate held within the bound (2s at defaults).
    """
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def _create_interface(
    db: AsyncSession, name: str = "test_iface", is_active: bool = True
) -> DataInterface:
    """Create a test interface."""
    cat = InterfaceCategory(name=f"cat_{name}", description="Test", sort_order=99)
    db.add(cat)
    await db.flush()

    iface = DataInterface(
        name=name,
        display_name=f"Test {name}",
        category_id=cat.id,
        parameters={},
        is_active=is_active,
    )
    db.add(iface)
    await db.commit()
    await db.refresh(iface)
    return iface


async def _create_execution(
    db: AsyncSession, status: TaskStatus = TaskStatus.PENDING, **kwargs
) -> TaskExecution:
    """Create a test execution with required parent task."""
    import uuid

    from opendata.models.task import ScheduledTask, ScheduleType

    # Create parent task if not provided
    if "task_id" not in kwargs:
        task = ScheduledTask(
            name="Test Task",
            user_id=1,
            script_id="test_script",
            schedule_type=ScheduleType.CRON,
            schedule_expression="0 8 * * *",
            parameters={},
        )
        db.add(task)
        await db.flush()
        kwargs["task_id"] = task.id

    defaults = {
        "execution_id": f"exec_test_{uuid.uuid4().hex[:8]}",
        "script_id": "test_script",
        "status": status,
        "retry_count": 0,
    }
    defaults.update(kwargs)
    execution = TaskExecution(**defaults)
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution


class TestTriggerDownload:
    """Test download trigger endpoint."""

    @pytest.mark.asyncio
    async def test_trigger_download_success(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test triggering a download."""
        iface = await _create_interface(test_db, "download_test")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        with patch("opendata.api.data.data_service.execute_download", new_callable=AsyncMock):
            response = await test_client.post(
                "/api/data/download",
                headers=headers,
                json={
                    "interface_id": iface.id,
                    "parameters": {},
                },
            )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_trigger_download_logs_background_failure(
        self, test_client: AsyncClient, test_user_token: str, test_db, log_lines
    ):
        """A download that raises after the 202 is logged, not silently lost.

        Three things the caller cannot see have to hold together: the worker
        is awaited with the execution id that was handed back (so a reported
        202 is traceable), the failure is logged naming that id and the cause,
        and the task ends *handled* - if the exception escaped, the done
        callback would log a second line and the real failure would be
        attributed to "unhandled task" instead of to the download.

        The assertions run while the patch is still installed: the background
        task starts on the next loop tick, so leaving the ``with`` block
        earlier would let it call the real service.
        """
        iface = await _create_interface(test_db, "download_bg_fail")
        headers = {"Authorization": f"Bearer {test_user_token}"}
        worker = AsyncMock(side_effect=RuntimeError("warehouse unavailable"))

        with patch("opendata.api.data.data_service.execute_download", worker):
            response = await test_client.post(
                "/api/data/download",
                headers=headers,
                json={
                    "interface_id": iface.id,
                    "parameters": {},
                },
            )
            assert response.status_code == 202
            execution_id = response.json()["execution_id"]

            def failed() -> list[str]:
                return [
                    message
                    for level, message in log_lines
                    if level == "ERROR" and message.startswith("Background download failed")
                ]

            assert await _wait_until(failed), f"no background failure logged in {log_lines}"

            assert worker.await_count == 1
            assert worker.await_args.kwargs["execution_id"] == execution_id
            assert worker.await_args.kwargs["interface_id"] == iface.id

            (message,) = failed()
            assert f"execution {execution_id}" in message
            assert "warehouse unavailable" in message
            assert not [m for _, m in log_lines if "Unhandled exception in background task" in m], (
                "the download failure escaped the handler"
            )

    @pytest.mark.asyncio
    async def test_trigger_download_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test triggering download for non-existent interface."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/download",
            headers=headers,
            json={
                "interface_id": 99999,
                "parameters": {},
            },
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_download_inactive(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test triggering download for inactive interface."""
        iface = await _create_interface(test_db, "inactive_dl", is_active=False)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/download",
            headers=headers,
            json={
                "interface_id": iface.id,
                "parameters": {},
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_trigger_download_unauthenticated(self, test_client: AsyncClient):
        """Test triggering download without auth."""
        response = await test_client.post(
            "/api/data/download",
            json={
                "interface_id": 1,
                "parameters": {},
            },
        )
        assert response.status_code == 401


class TestDownloadProgress:
    """Test download progress endpoint."""

    @pytest.mark.asyncio
    async def test_progress_pending(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for pending execution."""
        execution = await _create_execution(test_db, TaskStatus.PENDING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 0.0

    @pytest.mark.asyncio
    async def test_progress_running(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for running execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.RUNNING,
            start_time=datetime.utcnow(),
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_progress_success(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for completed execution."""
        execution = await _create_execution(test_db, TaskStatus.COMPLETED)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 100.0

    @pytest.mark.asyncio
    async def test_progress_failed(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for failed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.FAILED,
            error_message="Test error",
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 0.0

    @pytest.mark.asyncio
    async def test_progress_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test progress for non-existent execution."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/99999/status", headers=headers)
        assert response.status_code == 404


class TestDownloadResult:
    """Test download result endpoint."""

    @pytest.mark.asyncio
    async def test_result_success(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for completed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.COMPLETED,
            end_time=datetime.now(UTC),
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_result_running(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for running execution (should return 202)."""
        execution = await _create_execution(test_db, TaskStatus.RUNNING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_result_pending(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for pending execution (should return 202)."""
        execution = await _create_execution(test_db, TaskStatus.PENDING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_result_failed(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for failed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.FAILED,
            error_message="Something went wrong",
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_result_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test result for non-existent execution."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/99999/result", headers=headers)
        assert response.status_code == 404


class TestLogTaskException:
    """The last-resort visibility path for a background task that raised.

    Every download task attaches this callback, so a failure that starts after
    the 202 has exactly one channel left: this line. The three ways a task can
    end are the whole contract - attribute a failure, and stay silent for the
    two that are not one.
    """

    @staticmethod
    def _errors(records: list[tuple[str, str]]) -> list[str]:
        """Return only the ERROR messages of captured log records."""
        return [message for level, message in records if level == "ERROR"]

    @pytest.mark.asyncio
    async def test_failed_task_is_attributed_by_type_and_reason(self, log_lines):
        """The line names the exception type and its message, not the template.

        The rendered string is asserted whole because loguru substitutes with
        ``{}``: written in the ``logging`` idiom (``"%s: %s", a, b``) the
        arguments are dropped, and an operator reading the log learns neither
        what failed nor why.
        """

        async def _worker() -> None:
            raise RuntimeError("scheduler tick lost")

        task = asyncio.create_task(_worker())
        with pytest.raises(RuntimeError):
            await task

        _log_task_exception(task)

        assert self._errors(log_lines) == [
            "Unhandled exception in background task: RuntimeError: scheduler tick lost"
        ]

    @pytest.mark.asyncio
    async def test_cancelled_task_logs_nothing(self, log_lines):
        """A cancelled task is not a failure, and must not raise on the way out.

        ``Task.exception()`` re-raises ``CancelledError`` for a cancelled task,
        so the guard has to come first - reading it in the other order turns
        the logger into a new failure inside the done callback.
        """

        async def _worker() -> None:
            await asyncio.sleep(30)

        task = asyncio.create_task(_worker())
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        _log_task_exception(task)

        assert self._errors(log_lines) == []

    @pytest.mark.asyncio
    async def test_successful_task_logs_nothing(self, log_lines):
        """The common case stays quiet, or the callback is noise."""

        async def _worker() -> int:
            return 1

        task = asyncio.create_task(_worker())
        assert await task == 1

        _log_task_exception(task)

        assert self._errors(log_lines) == []
