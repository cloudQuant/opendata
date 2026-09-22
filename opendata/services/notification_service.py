"""
Notification service for task execution events.

Sends notifications via WebSocket (always) and email (when configured)
when tasks fail or complete after retries.
"""

from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart

from loguru import logger


class NotificationService:
    """
    Service for sending task execution notifications.

    Supports WebSocket broadcast (always available) and
    optional email notifications (when SMTP is configured).
    """

    @staticmethod
    async def notify_task_failed(
        task_id: int,
        task_name: str,
        execution_id: str,
        error_message: str,
        retry_count: int,
        max_retries: int,
        owner_email: str | None = None,
    ) -> None:
        """
        Notify about task failure.

        Sends WebSocket broadcast and optional email to task owner.
        """
        is_final_failure = retry_count >= max_retries

        # 1. WebSocket broadcast (best-effort)
        try:
            from opendata.api.websocket import ws_manager

            await ws_manager.broadcast(
                {
                    "type": "task_notification",
                    "data": {
                        "notification_type": "task_failed",
                        "task_id": task_id,
                        "task_name": task_name,
                        "execution_id": execution_id,
                        "error_message": error_message,
                        "retry_count": retry_count,
                        "max_retries": max_retries,
                        "is_final_failure": is_final_failure,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                }
            )
        except Exception as e:
            logger.debug(f"WebSocket notification failed: {e}")

        # 2. Email notification for final failures (all retries exhausted)
        if is_final_failure and owner_email:
            await NotificationService._send_failure_email(
                to_email=owner_email,
                task_name=task_name,
                task_id=task_id,
                execution_id=execution_id,
                error_message=error_message,
                retry_count=retry_count,
            )

    @staticmethod
    async def notify_task_recovered(
        task_id: int,
        task_name: str,
        execution_id: str,
        retry_count: int,
    ) -> None:
        """Notify when a previously failed task succeeds on retry."""
        try:
            from opendata.api.websocket import ws_manager

            await ws_manager.broadcast(
                {
                    "type": "task_notification",
                    "data": {
                        "notification_type": "task_recovered",
                        "task_id": task_id,
                        "task_name": task_name,
                        "execution_id": execution_id,
                        "retry_count": retry_count,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                }
            )
        except Exception as e:
            logger.debug(f"WebSocket notification failed: {e}")

    @staticmethod
    async def _send_failure_email(
        to_email: str,
        task_name: str,
        task_id: int,
        execution_id: str,
        error_message: str,
        retry_count: int,
    ) -> None:
        """Send task failure email notification (best-effort)."""
        from opendata.core.config import settings

        if not settings.smtp_host or not settings.smtp_user:
            logger.debug("SMTP not configured, skipping email notification")
            return

        host: str = settings.smtp_host
        user: str = settings.smtp_user
        try:
            import asyncio
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            subject = f"[opendata] 任务失败: {task_name}"
            body = (
                f'任务 "{task_name}" (ID: {task_id}) 执行失败。\n\n'
                f"执行ID: {execution_id}\n"
                f"重试次数: {retry_count}\n"
                f"错误信息: {error_message}\n\n"
                f"请登录系统查看详情。"
            )

            msg = MIMEMultipart()
            msg["From"] = settings.emails_from_email or user
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))

            # Run SMTP in thread pool to avoid blocking
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: _smtp_send(
                    host=host,
                    port=settings.smtp_port,
                    user=user,
                    password=settings.smtp_password,
                    msg=msg,
                ),
            )
            logger.info(f"Failure notification email sent to {to_email}")

        except Exception as e:
            logger.warning(f"Failed to send notification email: {e}")


def _smtp_send(
    host: str,
    port: int,
    user: str,
    password: str | None,
    msg: MIMEMultipart,
) -> None:
    """Synchronous SMTP send helper."""
    import smtplib

    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        if port == 587:
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.send_message(msg)
