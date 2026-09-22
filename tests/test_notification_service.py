"""Behaviour tests for NotificationService.

Covers the WebSocket broadcast payloads, the "email only on final failure"
rule, and best-effort error handling. SMTP delivery itself is always mocked —
these tests must never touch the network.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.services.notification_service import NotificationService


def _captured_payload(broadcast: AsyncMock) -> dict:
    """Return the payload a mocked ws_manager.broadcast was called with."""
    broadcast.assert_awaited_once()
    args, _kwargs = broadcast.await_args
    return args[0]


class TestNotifyTaskFailed:
    """WebSocket notification and email escalation for failed tasks."""

    async def test_broadcast_payload_for_non_final_failure(self) -> None:
        """A retryable failure broadcasts task_failed with is_final_failure=False."""
        with patch("opendata.api.websocket.ws_manager") as manager:
            manager.broadcast = AsyncMock()
            with patch.object(
                NotificationService, "_send_failure_email", new_callable=AsyncMock
            ) as send_email:
                await NotificationService.notify_task_failed(
                    task_id=7,
                    task_name="daily-stock",
                    execution_id="exec-1",
                    error_message="boom",
                    retry_count=1,
                    max_retries=3,
                    owner_email="owner@example.com",
                )

        payload = _captured_payload(manager.broadcast)
        assert payload["type"] == "task_notification"
        data = payload["data"]
        assert data["notification_type"] == "task_failed"
        assert data["task_id"] == 7
        assert data["task_name"] == "daily-stock"
        assert data["execution_id"] == "exec-1"
        assert data["error_message"] == "boom"
        assert data["retry_count"] == 1
        assert data["max_retries"] == 3
        assert data["is_final_failure"] is False
        datetime.fromisoformat(data["timestamp"])
        # Not the last attempt yet, so no email is escalated.
        send_email.assert_not_awaited()

    async def test_escalates_to_email_on_final_failure(self) -> None:
        """The last retry escalates to the owner's email address."""
        with patch("opendata.api.websocket.ws_manager") as manager:
            manager.broadcast = AsyncMock()
            with patch.object(
                NotificationService, "_send_failure_email", new_callable=AsyncMock
            ) as send_email:
                await NotificationService.notify_task_failed(
                    task_id=9,
                    task_name="daily-fund",
                    execution_id="exec-2",
                    error_message="still broken",
                    retry_count=3,
                    max_retries=3,
                    owner_email="owner@example.com",
                )

        assert _captured_payload(manager.broadcast)["data"]["is_final_failure"] is True
        send_email.assert_awaited_once_with(
            to_email="owner@example.com",
            task_name="daily-fund",
            task_id=9,
            execution_id="exec-2",
            error_message="still broken",
            retry_count=3,
        )

    async def test_final_failure_without_owner_email_skips_email(self) -> None:
        """Without an owner address the notification stays WebSocket-only."""
        with patch("opendata.api.websocket.ws_manager") as manager:
            manager.broadcast = AsyncMock()
            with patch.object(
                NotificationService, "_send_failure_email", new_callable=AsyncMock
            ) as send_email:
                await NotificationService.notify_task_failed(
                    task_id=1,
                    task_name="t",
                    execution_id="e",
                    error_message="x",
                    retry_count=5,
                    max_retries=1,
                )

        send_email.assert_not_awaited()

    async def test_broadcast_failure_is_swallowed(self) -> None:
        """A broken WebSocket must not turn into a failure notification error."""
        with (
            patch("opendata.api.websocket.ws_manager") as manager,
            patch.object(
                NotificationService, "_send_failure_email", new_callable=AsyncMock
            ) as send_email,
        ):
            manager.broadcast = AsyncMock(side_effect=RuntimeError("ws down"))
            await NotificationService.notify_task_failed(
                task_id=2,
                task_name="t",
                execution_id="e",
                error_message="x",
                retry_count=1,
                max_retries=1,
                owner_email="owner@example.com",
            )

        # Email escalation still happens even though the broadcast failed.
        send_email.assert_awaited_once()


class TestNotifyTaskRecovered:
    """WebSocket notification for a task that succeeded after retrying."""

    async def test_broadcast_payload(self) -> None:
        """Recovery broadcasts task_recovered and carries the retry count."""
        with patch("opendata.api.websocket.ws_manager") as manager:
            manager.broadcast = AsyncMock()
            await NotificationService.notify_task_recovered(
                task_id=3,
                task_name="weekly-index",
                execution_id="exec-3",
                retry_count=2,
            )

        data = _captured_payload(manager.broadcast)["data"]
        assert data["notification_type"] == "task_recovered"
        assert data["task_id"] == 3
        assert data["task_name"] == "weekly-index"
        assert data["execution_id"] == "exec-3"
        assert data["retry_count"] == 2
        datetime.fromisoformat(data["timestamp"])

    async def test_broadcast_failure_is_swallowed(self) -> None:
        """Recovery notification is best-effort."""
        with patch("opendata.api.websocket.ws_manager") as manager:
            manager.broadcast = AsyncMock(side_effect=RuntimeError("ws down"))
            await NotificationService.notify_task_recovered(
                task_id=4, task_name="t", execution_id="e", retry_count=1
            )


class TestSendFailureEmail:
    """Email escalation paths, including the unconfigured-SMTP guard."""

    async def test_skips_when_smtp_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No SMTP host/user means no send attempt at all."""
        from opendata.core.config import settings

        monkeypatch.setattr(settings, "smtp_host", None)
        monkeypatch.setattr(settings, "smtp_user", None)
        with patch("opendata.services.notification_service._smtp_send") as smtp_send:
            await NotificationService._send_failure_email(
                to_email="owner@example.com",
                task_name="t",
                task_id=1,
                execution_id="e",
                error_message="x",
                retry_count=1,
            )
        smtp_send.assert_not_called()

    async def test_builds_message_and_delegates_to_smtp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured SMTP sends a message whose subject uses the opendata brand."""
        from opendata.core.config import settings

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_user", "bot@example.com")
        monkeypatch.setattr(settings, "smtp_password", "secret")
        monkeypatch.setattr(settings, "smtp_port", 587)
        monkeypatch.setattr(settings, "emails_from_email", "noreply@opendata.com")

        with patch(
            "opendata.services.notification_service._smtp_send", new_callable=MagicMock
        ) as smtp_send:
            await NotificationService._send_failure_email(
                to_email="owner@example.com",
                task_name="daily-stock",
                task_id=11,
                execution_id="exec-11",
                error_message="disk full",
                retry_count=3,
            )

        smtp_send.assert_called_once()
        kwargs = smtp_send.call_args.kwargs
        assert kwargs["host"] == "smtp.example.com"
        assert kwargs["port"] == 587
        assert kwargs["user"] == "bot@example.com"
        assert kwargs["password"] == "secret"
        message = kwargs["msg"]
        assert message["To"] == "owner@example.com"
        assert message["From"] == "noreply@opendata.com"
        assert message["Subject"] == "[opendata] 任务失败: daily-stock"

    async def test_smtp_errors_are_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing SMTP server must not propagate out of the notification path."""
        from opendata.core.config import settings

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
        monkeypatch.setattr(settings, "smtp_user", "bot@example.com")

        with patch(
            "opendata.services.notification_service._smtp_send",
            side_effect=OSError("connection refused"),
        ):
            await NotificationService._send_failure_email(
                to_email="owner@example.com",
                task_name="t",
                task_id=1,
                execution_id="e",
                error_message="x",
                retry_count=1,
            )


class TestSmtpSendHelper:
    """The synchronous SMTP helper builds a STARTTLS session on port 587."""

    def test_uses_starttls_and_login_on_587(self) -> None:
        """Port 587 triggers ehlo -> starttls -> login -> send_message."""
        from opendata.services.notification_service import _smtp_send

        message = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            _smtp_send(
                host="smtp.example.com",
                port=587,
                user="bot@example.com",
                password="secret",
                msg=message,
            )

        smtp_cls.assert_called_once_with("smtp.example.com", 587)
        assert server.ehlo.call_count == 2
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("bot@example.com", "secret")
        server.send_message.assert_called_once_with(message)

    def test_skips_starttls_and_login_when_not_configured(self) -> None:
        """A plain relay on port 25 sends without TLS or credentials."""
        from opendata.services.notification_service import _smtp_send

        message = MagicMock()
        with patch("smtplib.SMTP") as smtp_cls:
            server = smtp_cls.return_value.__enter__.return_value
            _smtp_send(
                host="relay.local",
                port=25,
                user="",
                password=None,
                msg=message,
            )

        server.starttls.assert_not_called()
        server.login.assert_not_called()
        server.send_message.assert_called_once_with(message)
